"""History backfill: every KXRT market's settlement and hourly price history.

One run does:
  1. List every KXRT market, settled or not: the live /markets list plus
     /historical/markets (markets settled before Kalshi's cutoff are only
     there). Upsert them into `markets`, with result, settled_at and the
     settled score (settlement_value).
  2. For each market, fetch hourly candlesticks from where we left off and
     insert them into `price_snapshots` with source = 'candlestick'.

Commands:

    uv run rt-backfill-kalshi                          # everything
    uv run rt-backfill-kalshi --event KXRT-RES         # one film (repeatable)

Safe to rerun or resume after a crash: inserts use ON CONFLICT DO NOTHING,
each market starts from its newest saved candle, and a settled market whose
candles already reach its close is skipped without a request.

No look-ahead (rule 1): a candle's ts is the END of its hour, the first moment
its closing bid/ask were knowable. Kalshi sends no candle for an hour with no
activity, so rows have gaps. Read prices "as of" a time by taking the latest
row with ts <= as_of; never interpolate between rows (that uses a later price).
A candle can end after close_at; a backtest must not trade after close_at.

Read-only (CLAUDE.md rule 7): this only reads public market data.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rt_forecast.collectors.kalshi import SERIES, KalshiClient, Market
from rt_forecast.collectors.kalshi_live import MAX_CONSECUTIVE_FAILURES, Collector
from rt_forecast.db.db import (
    default_db_path,
    default_raw_dir,
    get_connection,
    init_db,
    load_settings,
)

log = logging.getLogger("rt_forecast.backfill")

SOURCE = "candlestick"  # price_snapshots.source for these rows
INTERVAL_MIN = 60  # hourly: daily can be rebuilt from it, 1-minute is ~60x the requests
# Kalshi refuses requests spanning more than 5000 candles (HTTP 400); stay well under.
MAX_CANDLES_PER_REQUEST = 4000
# Keep fetching this long past close/settlement, to catch the last candle.
TAIL = timedelta(hours=1)


@dataclass
class BackfillResult:
    """What one run did, for the summary line and the exit code."""

    markets: int = 0  # markets upserted
    settled: int = 0  # of which settled
    fetched: int = 0  # markets whose candles we requested
    skipped: int = 0  # settled markets already complete: no request
    inserted: int = 0  # new price_snapshots rows
    duplicates: int = 0  # candles we already had
    errors: list[str] = field(default_factory=list)
    aborted: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors and not self.aborted


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


class Backfiller(Collector):
    """Writes market metadata and candlesticks.

    A Collector subclass so markets and raw_fetches are written by exactly the
    same code as the live collector (rule 2).
    """

    def __init__(
        self,
        conn,
        client: KalshiClient,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        super().__init__(conn, client)
        self._now = now

    def discover_markets(self, series: str = SERIES) -> list[Market]:
        """Every market in the series, live and historical, one per ticker.

        A few markets settled right around the cutoff are in both lists; the
        live copy wins (it is the more recent view, and they agree anyway).
        """
        by_ticker = {m.ticker: m for m in self.client.list_markets(series, historical=True)}
        by_ticker.update({m.ticker: m for m in self.client.list_markets(series, status=None)})
        return sorted(by_ticker.values(), key=lambda m: (m.open_at or "", m.ticker))

    def latest_candle_ts(self, ticker: str) -> str | None:
        row = self.conn.execute(
            "SELECT MAX(ts) AS ts FROM price_snapshots WHERE ticker = ? AND source = ?",
            (ticker, SOURCE),
        ).fetchone()
        return row["ts"]

    def window(self, market: Market) -> tuple[datetime, datetime] | None:
        """The time range still to fetch for a market, or None if nothing is left.

        Starts at our newest saved candle (resume), else when the market opened.
        Ends an hour after settlement (or close), or now for open markets.
        """
        latest = self.latest_candle_ts(market.ticker)
        if market.settled_at and latest and market.close_at and latest >= market.close_at:
            return None  # settled and we have candles up to its close: done
        start_iso = latest or market.open_at
        if not start_iso:
            return None
        end_iso = market.settled_at or market.close_at
        end = self._now() if end_iso is None else min(self._now(), _dt(end_iso) + TAIL)
        start = _dt(start_iso)
        return (start, end) if start < end else None

    def backfill_market(self, market: Market) -> tuple[int, int] | None:
        """Fetch and insert a market's candles. Returns (inserted, duplicates).

        None means there was nothing to fetch. Each chunk is committed on its
        own, so a crash keeps every finished chunk and the next run resumes.
        """
        span = self.window(market)
        if span is None:
            return None
        start, end = span
        step = timedelta(minutes=INTERVAL_MIN * MAX_CANDLES_PER_REQUEST)
        inserted = duplicates = 0
        while start < end:
            chunk_end = min(start + step, end)
            self._last_fetch = None
            candles = self.client.get_candlesticks(market, start, chunk_end, INTERVAL_MIN)
            if self._last_fetch is None:  # on_fetch didn't run: should never happen
                raise RuntimeError(f"no raw fetch recorded for {market.ticker}")
            fetch_id = self._last_fetch[0]
            before = self.conn.total_changes
            self.conn.executemany(
                """INSERT INTO price_snapshots
                       (ticker, ts, yes_bid, yes_ask, last_price, volume, open_interest,
                        source, raw_fetch_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (ticker, ts, source) DO NOTHING""",
                [
                    (
                        market.ticker,
                        c.end_at,  # end of the hour: when this price was knowable
                        c.yes_bid,
                        c.yes_ask,
                        c.price,
                        c.volume,
                        c.open_interest,
                        SOURCE,
                        fetch_id,
                    )
                    for c in candles
                ],
            )
            self.conn.commit()
            new = self.conn.total_changes - before
            inserted += new
            duplicates += len(candles) - new
            start = chunk_end
        return inserted, duplicates

    def run(self, series: str = SERIES, events: set[str] | None = None) -> BackfillResult:
        """The whole backfill. Never raises for a single market's failure."""
        result = BackfillResult()
        try:
            markets = self.discover_markets(series)
        except Exception as exc:
            log.error("listing %s markets failed: %s", series, exc)
            result.errors.append(f"discover_markets: {exc}")
            result.aborted = True
            return result
        if events:
            markets = [m for m in markets if m.event_ticker in events]
        self.upsert_markets(markets)
        result.markets = len(markets)
        result.settled = sum(1 for m in markets if m.settled_at)
        log.info(
            "%d markets (%d settled) across %d films",
            result.markets,
            result.settled,
            len({m.event_ticker for m in markets}),
        )

        consecutive_failures = 0
        for i, market in enumerate(markets, 1):
            try:
                counts = self.backfill_market(market)
                consecutive_failures = 0
            except Exception as exc:
                self.conn.rollback()
                consecutive_failures += 1
                log.warning("backfill of %s failed: %s", market.ticker, exc)
                result.errors.append(f"{market.ticker}: {exc}")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log.error("%d markets failed in a row; stopping", consecutive_failures)
                    result.aborted = True
                    break
                continue
            if counts is None:
                result.skipped += 1
                continue
            result.fetched += 1
            result.inserted += counts[0]
            result.duplicates += counts[1]
            log.info(
                "[%d/%d] %s: %d new candles, %d already had",
                i,
                len(markets),
                market.ticker,
                *counts,
            )
        return result


def backfill(
    db_path: str | Path | None = None,
    raw_dir: str | Path | None = None,
    events: set[str] | None = None,
    *,
    client_factory: Callable[[Path], KalshiClient] = KalshiClient,
) -> BackfillResult:
    """Open the database and a client, run the backfill, close both."""
    db_path = Path(db_path or default_db_path())
    raw_dir = Path(raw_dir or default_raw_dir())
    started = time.monotonic()
    with closing(get_connection(db_path)) as conn, client_factory(raw_dir) as client:
        result = Backfiller(conn, client).run(events=events)
    log.info(
        "backfill done in %.1fs: %d markets (%d settled), %d fetched, %d already complete, "
        "%d new candles, %d duplicates, %d errors%s",
        time.monotonic() - started,
        result.markets,
        result.settled,
        result.fetched,
        result.skipped,
        result.inserted,
        result.duplicates,
        len(result.errors),
        " (aborted)" if result.aborted else "",
    )
    return result


def main(argv: list[str] | None = None) -> None:
    load_settings()
    parser = argparse.ArgumentParser(
        description="Backfill every Kalshi KXRT market's settlement and hourly prices."
    )
    parser.add_argument("--db", type=Path, help="database file (default: $RT_DB_PATH)")
    parser.add_argument("--raw-dir", type=Path, help="raw response folder (default: $RT_RAW_DIR)")
    parser.add_argument(
        "--event",
        action="append",
        metavar="EVENT_TICKER",
        help="only this film, e.g. KXRT-RES (repeatable; default: every film)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.Formatter.converter = time.gmtime  # log times in UTC (rule 9)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    init_db(args.db)  # creates tables and adds new columns if missing
    result = backfill(args.db, args.raw_dir, set(args.event) if args.event else None)
    sys.exit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
