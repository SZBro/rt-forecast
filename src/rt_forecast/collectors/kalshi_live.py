"""Live collector: snapshot every open KXRT market into price_snapshots.

One pass does:
  1. List open markets (one request) and upsert them into `markets`.
  2. For each market, fetch the order book and insert one `price_snapshots`
     row: best bid/ask, the contracts resting at each (depth), and the last
     price, volume and open interest from step 1.

Order-book depth is the reason this runs live: candlesticks can be backfilled
later, depth cannot. The full book (every level) is in the raw file.

Commands:

    uv run rt-collect-once                  # one pass, then exit
    uv run rt-collect-loop                  # a pass every 15 minutes until stopped
    uv run rt-collect-loop --interval 5

Read-only (CLAUDE.md rule 7): this only reads public market data.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sqlite3
import sys
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from rt_forecast.collectors.kalshi import SERIES, KalshiClient, Market, RawFetch
from rt_forecast.db.db import (
    default_db_path,
    default_raw_dir,
    get_connection,
    init_db,
    load_settings,
    utc_now_iso,
)

log = logging.getLogger("rt_forecast.collector")

DEFAULT_INTERVAL_MIN = 15

# Stop a pass after this many markets fail in a row: Kalshi is probably down,
# and retrying every remaining market would make one pass take over an hour.
MAX_CONSECUTIVE_FAILURES = 5


@dataclass
class PassResult:
    """What one pass did, for the log line and the exit code."""

    markets: int = 0
    inserted: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    aborted: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors and not self.aborted


class Collector:
    """Writes Kalshi market data into the database.

    Takes an open connection and client so tests can pass a temporary
    database and a mocked client.
    """

    def __init__(self, conn: sqlite3.Connection, client: KalshiClient):
        self.conn = conn
        self.client = client
        # The client calls this after saving each raw response to disk.
        self.client.on_fetch = self._record_fetch
        self._last_fetch: tuple[int, RawFetch] | None = None

    def _record_fetch(self, fetch: RawFetch) -> None:
        """Insert the raw_fetches row right away: the file is already on disk."""
        cur = self.conn.execute(
            """INSERT INTO raw_fetches
                   (source, url, fetched_at, status_code, file_path, sha256, byte_size)
               VALUES ('kalshi', ?, ?, ?, ?, ?, ?)""",
            (
                fetch.url,
                fetch.fetched_at,
                fetch.status_code,
                fetch.file_path,
                fetch.sha256,
                fetch.byte_size,
            ),
        )
        self.conn.commit()
        self._last_fetch = (cur.lastrowid, fetch)

    def upsert_markets(self, markets: list[Market]) -> None:
        """Add new markets; refresh status/result/times of known ones.

        markets is a reference table, updated in place (see schema.sql).
        first_seen_at and film_id are never touched on update.
        """
        now = utc_now_iso()
        self.conn.executemany(
            """INSERT INTO markets
                   (ticker, event_ticker, series_ticker, kalshi_title, threshold, strike_type,
                    open_at, close_at, settles_at, status, result, settlement_value, settled_at,
                    first_seen_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (ticker) DO UPDATE SET
                   kalshi_title = excluded.kalshi_title,
                   threshold    = excluded.threshold,
                   strike_type  = excluded.strike_type,
                   open_at      = excluded.open_at,
                   close_at     = excluded.close_at,
                   settles_at   = excluded.settles_at,
                   status       = excluded.status,
                   result       = excluded.result,
                   settlement_value = excluded.settlement_value,
                   settled_at   = excluded.settled_at,
                   updated_at   = excluded.updated_at""",
            [
                (
                    m.ticker,
                    m.event_ticker,
                    m.series_ticker,
                    m.title,
                    m.threshold,
                    m.strike_type,
                    m.open_at,
                    m.close_at,
                    m.settles_at,
                    m.status,
                    m.result,
                    m.settlement_value,
                    m.settled_at,
                    now,
                    now,
                )
                for m in markets
            ],
        )
        self.conn.commit()

    def snapshot_market(self, market: Market) -> bool:
        """Fetch one order book and store a snapshot. True if a new row was added.

        ts is when the order book was fetched, so the row says exactly when
        this price was visible (no look-ahead in backtests).
        """
        self._last_fetch = None
        book = self.client.get_orderbook(market.ticker)
        if self._last_fetch is None:  # on_fetch didn't run: should never happen
            raise RuntimeError(f"no raw fetch recorded for {market.ticker}")
        fetch_id, fetch = self._last_fetch

        bid = book.best_yes_bid  # (price, size) or None
        ask = book.best_yes_ask
        cur = self.conn.execute(
            """INSERT INTO price_snapshots
                   (ticker, ts, yes_bid, yes_ask, yes_bid_size, yes_ask_size,
                    last_price, volume, open_interest, source, raw_fetch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'live', ?)
               ON CONFLICT (ticker, ts, source) DO NOTHING""",
            (
                market.ticker,
                fetch.fetched_at,
                bid[0] if bid else None,
                ask[0] if ask else None,
                bid[1] if bid else None,
                ask[1] if ask else None,
                market.last_price,
                market.volume,
                market.open_interest,
                fetch_id,
            ),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def run_pass(self, series: str = SERIES) -> PassResult:
        """One full pass. Never raises for a single market's failure."""
        result = PassResult()
        try:
            markets = self.client.list_markets(series, status="open")
            self.upsert_markets(markets)
        except Exception as exc:  # no market list = nothing to snapshot this pass
            log.error("listing %s markets failed: %s", series, exc)
            result.errors.append(f"list_markets: {exc}")
            result.aborted = True
            return result

        result.markets = len(markets)
        consecutive_failures = 0
        for market in markets:
            try:
                if self.snapshot_market(market):
                    result.inserted += 1
                else:
                    result.duplicates += 1
                consecutive_failures = 0
            except Exception as exc:
                self.conn.rollback()
                consecutive_failures += 1
                log.warning("snapshot of %s failed: %s", market.ticker, exc)
                result.errors.append(f"{market.ticker}: {exc}")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log.error(
                        "%d markets failed in a row; stopping this pass early",
                        consecutive_failures,
                    )
                    result.aborted = True
                    break
        return result


def collect_once(
    db_path: str | Path | None = None,
    raw_dir: str | Path | None = None,
    *,
    client_factory: Callable[[Path], KalshiClient] = KalshiClient,
) -> PassResult:
    """Open the database and a client, run one pass, close both.

    A fresh connection and client per pass means a bad state (a stuck
    connection, a throttle clock) can never carry over to the next pass.
    """
    db_path = Path(db_path or default_db_path())
    raw_dir = Path(raw_dir or default_raw_dir())
    started = time.monotonic()
    with closing(get_connection(db_path)) as conn, client_factory(raw_dir) as client:
        result = Collector(conn, client).run_pass()
    log.info(
        "pass done in %.1fs: %d markets, %d new snapshots, %d duplicates, %d errors%s",
        time.monotonic() - started,
        result.markets,
        result.inserted,
        result.duplicates,
        len(result.errors),
        " (aborted)" if result.aborted else "",
    )
    return result


def next_tick(now: datetime, interval_min: int) -> datetime:
    """The next wall-clock multiple of the interval after now (e.g. :00, :15, :30, :45).

    Aligned ticks make snapshots from different days line up, and make the
    gap checker's job simple: one snapshot per ticker per interval.
    """
    interval_s = interval_min * 60
    epoch = now.timestamp()
    return datetime.fromtimestamp((epoch // interval_s + 1) * interval_s, UTC)


def run_loop(
    interval_min: int = DEFAULT_INTERVAL_MIN,
    db_path: str | Path | None = None,
    raw_dir: str | Path | None = None,
    *,
    max_passes: int | None = None,
    collect: Callable[..., PassResult] = collect_once,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """A pass now, then one at every aligned tick, until stopped.

    A failed pass is logged and the loop carries on. If a pass overruns the
    next tick, that tick is skipped (and will show up in the gap check).
    """
    passes = 0
    while True:
        try:
            collect(db_path, raw_dir)
        except Exception:  # e.g. database locked or disk full: log it, try next tick
            log.exception("pass crashed")
        passes += 1
        if max_passes is not None and passes >= max_passes:
            return
        wake = next_tick(now(), interval_min)
        log.info("next pass at %s", wake.strftime("%Y-%m-%dT%H:%M:%SZ"))
        sleep(max(0.0, (wake - now()).total_seconds()))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _setup(description: str, argv: list[str] | None, loop: bool) -> argparse.Namespace:
    load_settings()
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--db", type=Path, help="database file (default: $RT_DB_PATH)")
    parser.add_argument("--raw-dir", type=Path, help="raw response folder (default: $RT_RAW_DIR)")
    if loop:
        parser.add_argument(
            "--interval",
            type=int,
            default=DEFAULT_INTERVAL_MIN,
            help=f"minutes between passes (default: {DEFAULT_INTERVAL_MIN})",
        )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.Formatter.converter = time.gmtime  # log times in UTC (rule 9)
    # httpx logs every request at INFO (~146 lines a pass); keep its warnings only.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log.info(
        "database %s, raw files %s", args.db or default_db_path(), args.raw_dir or default_raw_dir()
    )
    init_db(args.db)  # creates tables if missing; safe every time
    return args


def main_once(argv: list[str] | None = None) -> None:
    args = _setup("Snapshot every open Kalshi KXRT market once.", argv, loop=False)
    result = collect_once(args.db, args.raw_dir)
    # Non-zero exit marks the run as failed (e.g. for a systemd timer).
    sys.exit(0 if result.ok else 1)


def main_loop(argv: list[str] | None = None) -> None:
    args = _setup("Snapshot every open Kalshi KXRT market every N minutes.", argv, loop=True)
    # systemd stops services with SIGTERM; exit cleanly instead of being killed
    # mid-write. (Writes are committed per market, so either way is safe.)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log.info("collecting every %d minutes; Ctrl+C to stop", args.interval)
    try:
        run_loop(args.interval, args.db, args.raw_dir)
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main_once()
