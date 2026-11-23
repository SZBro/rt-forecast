"""Read-only client for Kalshi's public market-data API.

Kalshi is a data source only (CLAUDE.md rule 7): this module never logs in,
never signs requests and has no order endpoints. Every endpoint used here
works anonymously.

Every response body is saved gzipped under data/raw/kalshi/ before it is
parsed (rule 3), so a parser bug can be fixed by re-parsing old files.

List every open Rotten Tomatoes market:

    uv run rt-kalshi-markets
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

import rt_forecast
from rt_forecast.db.db import to_utc_iso

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXRT"

# Descriptive, with no personal details: says who we are and that we only read.
USER_AGENT = f"rt-forecast/{rt_forecast.__version__} (student class project; read-only market data)"

# Kalshi's documented read limit is ~20 requests/s for the basic tier; anonymous
# requests have separate, unpublished limits. 5 requests/s stays well below.
MIN_REQUEST_INTERVAL_S = 0.2

# Worth retrying: rate limited, or Kalshi/CloudFront having a bad moment.
RETRY_STATUSES = {429, 500, 502, 503, 504}

# Candlestick interval in minutes -> what the API accepts.
INTERVALS = {1, 60, 1440}


class KalshiError(Exception):
    """A request failed for good (not retryable, or out of retries)."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Parsed results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawFetch:
    """One saved response. Fields match the raw_fetches table."""

    url: str
    fetched_at: str  # UTC, schema format
    status_code: int
    file_path: str  # relative to the raw root (data/raw/), e.g. 'kalshi/...json.gz'
    sha256: str  # of the gzipped file
    byte_size: int


@dataclass(frozen=True)
class Market:
    """One Kalshi contract: one threshold of one film."""

    ticker: str  # 'KXRT-DUNE-92'
    event_ticker: str  # 'KXRT-DUNE' (the film)
    series_ticker: str  # 'KXRT'
    title: str
    threshold: float | None  # YES wins if the Tomatometer is above this...
    strike_type: str | None  # ...where 'greater' means strictly above
    status: str  # 'active', 'closed', 'finalized', ...
    open_at: str | None
    close_at: str | None
    settles_at: str | None  # when the score is read (Monday 10:00 AM ET)
    settled_at: str | None  # when Kalshi actually settled it; None until then
    result: str | None  # 'yes' / 'no' once settled
    settlement_value: float | None  # the Tomatometer Kalshi settled on; None if unparseable
    yes_bid: float | None  # dollars per contract, 0-1
    yes_ask: float | None
    last_price: float | None
    volume: float | None  # contracts; Kalshi now allows fractional ones
    open_interest: float | None
    raw: dict[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class Candle:
    """One candlestick. Prices are closing values for the period, 0-1."""

    end_at: str  # end of the period, UTC
    yes_bid: float | None
    yes_ask: float | None
    price: float | None  # last trade as of end_at (may be from an earlier period)
    volume: float | None
    open_interest: float | None


@dataclass(frozen=True)
class Orderbook:
    """Resting bids. Kalshi only lists bids: a NO bid at p is a YES ask at 1 - p.

    Each side is a list of (price, size), sorted from lowest to highest price,
    as the API returns it.
    """

    ticker: str
    yes_bids: list[tuple[float, float]]
    no_bids: list[tuple[float, float]]

    @property
    def best_yes_bid(self) -> tuple[float, float] | None:
        """(price, size) we could sell YES at, or None if nobody is bidding."""
        return self.yes_bids[-1] if self.yes_bids else None

    @property
    def best_yes_ask(self) -> tuple[float, float] | None:
        """(price, size) we could buy YES at, from the best NO bid."""
        if not self.no_bids:
            return None
        price, size = self.no_bids[-1]
        return round(1 - price, 4), size


# ---------------------------------------------------------------------------
# Field parsing
# ---------------------------------------------------------------------------


def _num(d: dict[str, Any] | None, key: str) -> float | None:
    """Read a number that may be named key, key_dollars or key_fp.

    Kalshi sends prices and sizes as strings ('0.4400'), and the live and
    historical endpoints disagree on the suffix, so try all three.
    """
    if not d:
        return None
    for name in (f"{key}_dollars", f"{key}_fp", key):
        value = d.get(name)
        if value is not None and value != "":
            return float(value)
    return None


def _ts(value: str | None) -> str | None:
    """Kalshi timestamp ('2026-11-12T16:30:28.475799Z') -> schema format."""
    if not value:
        return None
    return to_utc_iso(datetime.fromisoformat(value))


def parse_settlement_value(value: str | None) -> float | None:
    """Kalshi's expiration_value ('95', '33%') -> 95.0. Anything else -> None.

    It is free text: one film (KXRT-SCR) has 'Above 32', which is not a score.
    Better no value than a wrong one; the raw text stays in the raw files.
    """
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*%?\s*", value or "")
    if not match:
        return None
    score = float(match.group(1))
    return score if 0 <= score <= 100 else None


def parse_market(m: dict[str, Any]) -> Market:
    return Market(
        ticker=m["ticker"],
        event_ticker=m["event_ticker"],
        series_ticker=m["event_ticker"].split("-")[0],
        title=m.get("title", ""),
        threshold=m.get("floor_strike"),
        strike_type=m.get("strike_type"),
        status=m.get("status", ""),
        open_at=_ts(m.get("open_time")),
        close_at=_ts(m.get("close_time")),
        settles_at=_ts(m.get("expected_expiration_time") or m.get("close_time")),
        settled_at=_ts(m.get("settlement_ts")),
        result=m.get("result") or None,
        settlement_value=parse_settlement_value(m.get("expiration_value")),
        yes_bid=_num(m, "yes_bid"),
        yes_ask=_num(m, "yes_ask"),
        last_price=_num(m, "last_price"),
        volume=_num(m, "volume"),
        open_interest=_num(m, "open_interest"),
        raw=m,
    )


def parse_candle(c: dict[str, Any]) -> Candle:
    """Closing values of one period, stamped with the period's END.

    The close is the state at the end of the period, so that is the earliest
    moment it was knowable. Stamping it with the start would leak up to one
    period of the future into a backtest (rule 1).
    """
    price = _num(c.get("price"), "close")
    if price is None:
        # No trade this period: 'previous' is the last trade before it, which
        # matches what a live snapshot's last_price means.
        price = _num(c.get("price"), "previous")
    return Candle(
        end_at=to_utc_iso(datetime.fromtimestamp(c["end_period_ts"], UTC)),
        yes_bid=_num(c.get("yes_bid"), "close"),
        yes_ask=_num(c.get("yes_ask"), "close"),
        price=price,
        volume=_num(c, "volume"),
        open_interest=_num(c, "open_interest"),
    )


def parse_orderbook(ticker: str, body: dict[str, Any]) -> Orderbook:
    book = body.get("orderbook_fp") or body.get("orderbook") or {}

    def side(name: str) -> list[tuple[float, float]]:
        levels = book.get(f"{name}_dollars") or book.get(name) or []
        return [(float(price), float(size)) for price, size in levels]

    return Orderbook(ticker=ticker, yes_bids=side("yes"), no_bids=side("no"))


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class KalshiClient:
    """Read-only Kalshi market-data client.

    transport, sleep and clock can be swapped out in tests (e.g. with
    httpx.MockTransport), so pytest never touches the network or actually waits.
    """

    def __init__(
        self,
        raw_root: str | Path = "data/raw",
        *,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 5,
        backoff_s: float = 1.0,
        max_backoff_s: float = 60.0,
        min_interval_s: float = MIN_REQUEST_INTERVAL_S,
        on_fetch: Callable[[RawFetch], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.raw_root = Path(raw_root)
        # No auth headers, ever: these endpoints are public.
        self.http = httpx.Client(
            base_url=BASE_URL,
            transport=transport,
            timeout=30.0,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s
        self.min_interval_s = min_interval_s
        self.on_fetch = on_fetch  # e.g. the collector inserting into raw_fetches
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None
        self._cutoff: str | None = None

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> KalshiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low level ----------------------------------------------------------

    def _throttle(self) -> None:
        """Leave at least min_interval_s between requests."""
        if self._last_request_at is not None:
            wait = self._last_request_at + self.min_interval_s - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = self._clock()

    def _retry_delay(self, attempt: int, response: httpx.Response | None) -> float:
        """Exponential backoff (1, 2, 4, ... s), or what the server asks for."""
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                return min(float(retry_after), self.max_backoff_s)
        return min(self.backoff_s * 2**attempt, self.max_backoff_s)

    def _save_raw(self, response: httpx.Response) -> RawFetch:
        """Gzip the body to data/raw/kalshi/YYYY/MM/DD/<utc time>_<path>.json.gz."""
        now = datetime.now(UTC)
        slug = re.sub(r"[^A-Za-z0-9.-]+", "_", response.url.path.split("/trade-api/v2/")[-1])
        stamp = now.strftime("%Y%m%dT%H%M%S.%fZ")  # microseconds: names never collide
        rel = Path("kalshi", now.strftime("%Y/%m/%d"), f"{stamp}_{slug.strip('_')}.json.gz")
        path = self.raw_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        data = gzip.compress(response.content)
        path.write_bytes(data)
        fetch = RawFetch(
            url=str(response.url),
            fetched_at=to_utc_iso(now),
            status_code=response.status_code,
            file_path=rel.as_posix(),
            sha256=hashlib.sha256(data).hexdigest(),
            byte_size=len(data),
        )
        if self.on_fetch:
            self.on_fetch(fetch)
        return fetch

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET a path, retrying transient failures, and save the body before parsing."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        error = ""
        response: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            response = None
            try:
                response = self.http.get(path, params=params)
            except httpx.TransportError as exc:  # timeout, connection reset, DNS...
                error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    self._save_raw(response)
                    return response.json()
                error = f"HTTP {response.status_code}: {response.text[:200]}"
                if response.status_code not in RETRY_STATUSES:
                    raise KalshiError(f"GET {path} failed: {error}", response.status_code)
            if attempt < self.max_retries:
                self._sleep(self._retry_delay(attempt, response))
        status = response.status_code if response is not None else None
        raise KalshiError(f"GET {path} failed after {self.max_retries + 1} tries: {error}", status)

    # -- endpoints ----------------------------------------------------------

    def list_markets(
        self, series_ticker: str = SERIES, status: str | None = "open", *, historical: bool = False
    ) -> list[Market]:
        """Every market in a series, following the cursor across pages.

        status: 'unopened', 'open', 'closed', 'settled' or None for all.
        historical=True reads markets settled before the cutoff from /historical/.
        """
        path = "/historical/markets" if historical else "/markets"
        params: dict[str, Any] = {"series_ticker": series_ticker, "limit": 1000}
        if not historical:
            params["status"] = status
        markets: list[Market] = []
        cursor = None
        while True:
            body = self.get_json(path, {**params, "cursor": cursor})
            markets.extend(parse_market(m) for m in body.get("markets", []))
            cursor = body.get("cursor")
            if not cursor:
                return markets

    def get_market(self, ticker: str) -> Market:
        """One market. Falls back to /historical/ once Kalshi has moved it there."""
        try:
            body = self.get_json(f"/markets/{ticker}")
        except KalshiError as exc:
            if exc.status_code != 404:
                raise
            body = self.get_json(f"/historical/markets/{ticker}")
        return parse_market(body["market"])

    def historical_cutoff(self) -> str:
        """Markets settled before this time are only on /historical/. Cached per client."""
        if self._cutoff is None:
            self._cutoff = _ts(self.get_json("/historical/cutoff")["market_settled_ts"])
        return self._cutoff

    def get_candlesticks(
        self, market: Market | str, start: datetime, end: datetime, interval: int = 60
    ) -> list[Candle]:
        """Candles between start and end. interval is in minutes: 1, 60 or 1440.

        Markets settled before Kalshi's historical cutoff are read from the
        /historical/ endpoint; everything else from the live one.
        """
        if interval not in INTERVALS:
            raise ValueError(f"interval must be one of {sorted(INTERVALS)}, not {interval}")
        if isinstance(market, str):
            market = self.get_market(market)
        params = {
            "start_ts": int(start.timestamp()),
            "end_ts": int(end.timestamp()),
            "period_interval": interval,
        }
        if market.settled_at and market.settled_at < self.historical_cutoff():
            path = f"/historical/markets/{market.ticker}/candlesticks"
        else:
            path = f"/series/{market.series_ticker}/markets/{market.ticker}/candlesticks"
        body = self.get_json(path, params)
        return [parse_candle(c) for c in body.get("candlesticks", [])]

    def get_orderbook(self, ticker: str, depth: int | None = None) -> Orderbook:
        """Resting bids on both sides. depth=None returns every price level."""
        body = self.get_json(f"/markets/{ticker}/orderbook", {"depth": depth})
        return parse_orderbook(ticker, body)


# ---------------------------------------------------------------------------
# Script: list open Rotten Tomatoes markets
# ---------------------------------------------------------------------------


def format_markets(markets: list[Market]) -> str:
    """A table of markets grouped by film, lowest threshold first."""
    if not markets:
        return "No open markets."
    rows = sorted(markets, key=lambda m: (m.settles_at or "", m.event_ticker, m.threshold or 0))
    header = f"{'TICKER':<20} {'THRESHOLD':>9}  {'SETTLES (UTC)':<20} {'BID':>5} {'ASK':>5}  TITLE"
    lines = [header, "-" * len(header)]
    for m in rows:
        threshold = f"> {m.threshold:g}" if m.threshold is not None else "?"
        bid = f"{m.yes_bid:.2f}" if m.yes_bid is not None else "-"
        ask = f"{m.yes_ask:.2f}" if m.yes_ask is not None else "-"
        lines.append(
            f"{m.ticker:<20} {threshold:>9}  {m.settles_at or '?':<20} {bid:>5} {ask:>5}  {m.title}"
        )
    films = len({m.event_ticker for m in markets})
    lines.append(f"\n{len(markets)} open markets across {films} films.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="List open Kalshi Rotten Tomatoes markets.")
    parser.add_argument("--series", default=SERIES, help=f"series ticker (default: {SERIES})")
    parser.add_argument("--raw-root", default="data/raw", help="where raw responses are saved")
    args = parser.parse_args(argv)
    with KalshiClient(args.raw_root) as client:
        print(format_markets(client.list_markets(args.series, status="open")))


if __name__ == "__main__":
    main()
