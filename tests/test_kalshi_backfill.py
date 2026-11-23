"""Tests for the history backfill. HTTP is mocked; the database is temporary."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from rt_forecast.collectors.kalshi import KalshiClient, parse_candle, parse_settlement_value
from rt_forecast.collectors.kalshi_backfill import MAX_CANDLES_PER_REQUEST, Backfiller
from rt_forecast.db.db import get_connection, init_db

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
CUTOFF = "2026-09-22T00:00:00Z"
HOUR = timedelta(hours=1)


def ts(dt: datetime) -> int:
    return int(dt.timestamp())


def market(ticker, open_at, close_at, settled_at=None, result="", value=None, strike=90):
    return {
        "ticker": ticker,
        "event_ticker": ticker.rsplit("-", 1)[0],
        "title": f"{ticker} Rotten Tomatoes score?",
        "floor_strike": strike,
        "strike_type": "greater",
        "status": "finalized" if settled_at else "active",
        "open_time": open_at,
        "close_time": close_at,
        "expected_expiration_time": close_at,
        "settlement_ts": settled_at,
        "result": result,
        "expiration_value": value,
    }


# Settled before the cutoff: only on /historical/.
OLD = market(
    "KXRT-OLD-90",
    "2026-09-16T23:00:00Z",
    "2026-09-21T14:00:00Z",
    "2026-09-21T15:14:54Z",
    "yes",
    "95",
)
# Settled after the cutoff: on the live list.
NEW = market(
    "KXRT-NEW-40",
    "2026-11-11T16:00:00Z",
    "2026-11-16T15:00:00Z",
    "2026-11-16T17:01:09Z",
    "no",
    "35%",
    strike=40,
)
# Still trading.
OPEN = market("KXRT-OPN-80", "2026-11-20T16:00:00Z", "2026-11-30T15:00:00Z", strike=80)


def hourly(start_iso, hours, *, historical=False):
    """Candles ending every hour after start_iso, in the live or historical shape."""
    start = datetime.fromisoformat(start_iso)
    out = []
    for i in range(1, hours + 1):
        if historical:
            body = {
                "volume": "1.00",
                "open_interest": "10.00",
                "price": {"close": "0.5000"},
                "yes_bid": {"close": "0.4800"},
                "yes_ask": {"close": "0.5200"},
            }
        else:
            body = {
                "volume_fp": "1.00",
                "open_interest_fp": "10.00",
                "price": {"close_dollars": "0.3000"},
                "yes_bid": {"close_dollars": "0.2800"},
                "yes_ask": {"close_dollars": "0.3200"},
            }
        out.append({"end_period_ts": ts(start + i * HOUR), **body})
    return out


class FakeKalshi:
    """Serves the market lists and candlesticks, filtering candles by the time range.

    Like the real API, refuses a range spanning more than 5000 candles.
    """

    def __init__(self, live, historical, candles, fail=()):
        self.live = live
        self.historical = historical
        self.candles = candles  # ticker -> list of raw candles
        self.fail = set(fail)  # tickers whose candle requests return 500
        self.requests: list[httpx.Request] = []

    def candle_requests(self, ticker=None):
        return [
            r
            for r in self.requests
            if r.url.path.endswith("/candlesticks")
            and (ticker is None or f"/{ticker}/" in r.url.path)
        ]

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path.removeprefix("/trade-api/v2")
        q = request.url.params
        if path == "/markets":
            return httpx.Response(200, json={"markets": self.live, "cursor": ""})
        if path == "/historical/markets":
            return httpx.Response(200, json={"markets": self.historical, "cursor": ""})
        if path == "/historical/cutoff":
            return httpx.Response(200, json={"market_settled_ts": CUTOFF})
        if path.endswith("/candlesticks"):
            ticker = path.split("/")[-2]
            if ticker in self.fail:
                return httpx.Response(400, json={"error": "boom"})
            start, end = int(q["start_ts"]), int(q["end_ts"])
            if (end - start) / 3600 > 5000:
                return httpx.Response(400, json={"error": "max candlesticks: 5000"})
            body = [c for c in self.candles.get(ticker, []) if start <= c["end_period_ts"] <= end]
            return httpx.Response(200, json={"candlesticks": body, "ticker": ticker})
        return httpx.Response(404)


@pytest.fixture
def db(tmp_path):
    path = init_db(tmp_path / "rt.db")
    with closing(get_connection(path)) as conn:
        yield conn


@pytest.fixture
def make(db, tmp_path):
    def build(fake, now=NOW):
        client = KalshiClient(
            tmp_path / "raw",
            transport=httpx.MockTransport(fake),
            min_interval_s=0,
            sleep=lambda s: None,
        )
        return Backfiller(db, client, now=lambda: now)

    return build


def default_fake(**kw):
    return FakeKalshi(
        live=[NEW, OPEN],
        historical=[OLD],  # settled before the cutoff: ONLY on /historical/, like the real API
        candles={
            "KXRT-OLD-90": hourly("2026-09-16T23:00:00Z", 16 + 5 * 24, historical=True),
            "KXRT-NEW-40": hourly("2026-11-11T16:00:00Z", 5 * 24),
            "KXRT-OPN-80": hourly("2026-11-20T16:00:00Z", 44),  # up to NOW
        },
        **kw,
    )


def snapshots(db, ticker):
    return db.execute(
        "SELECT * FROM price_snapshots WHERE ticker = ? ORDER BY ts", (ticker,)
    ).fetchall()


# -- parsing --------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [("95", 95.0), ("33%", 33.0), (" 100 ", 100.0), ("Above 32", None), ("", None), (None, None)],
)
def test_settlement_value_parsing(text, expected):
    assert parse_settlement_value(text) == expected


def test_settlement_value_out_of_range_is_none():
    assert parse_settlement_value("150") is None


def test_candle_without_trade_uses_previous_price():
    candle = parse_candle(
        {
            "end_period_ts": ts(NOW),
            "price": {"close": None, "previous": "0.0100"},
            "yes_bid": {"close": "0.0000"},
            "yes_ask": {"close": "0.0100"},
        }
    )
    assert candle.price == 0.01


# -- the run ----------------------------------------------------------------------


def test_backfill_writes_markets_with_settlement(db, make):
    result = make(default_fake()).run()
    assert result.ok
    assert (result.markets, result.settled) == (3, 2)

    rows = {r["ticker"]: r for r in db.execute("SELECT * FROM markets")}
    assert set(rows) == {"KXRT-OLD-90", "KXRT-NEW-40", "KXRT-OPN-80"}
    old, new, opn = rows["KXRT-OLD-90"], rows["KXRT-NEW-40"], rows["KXRT-OPN-80"]
    assert (old["result"], old["settlement_value"], old["settled_at"]) == (
        "yes",
        95.0,
        "2026-09-21T15:14:54Z",
    )
    assert (new["result"], new["settlement_value"]) == ("no", 35.0)
    assert (opn["result"], opn["settlement_value"], opn["settled_at"]) == (None, None, None)


def test_candle_ts_is_end_of_hour_not_start(db, make):
    """No look-ahead: the market opened at 23:00, so its first candle is stamped 00:00."""
    make(default_fake()).run()
    rows = snapshots(db, "KXRT-OLD-90")
    assert rows[0]["ts"] == "2026-09-17T00:00:00Z"
    assert all(r["source"] == "candlestick" for r in rows)
    assert all(r["yes_bid_size"] is None and r["yes_ask_size"] is None for r in rows)


def test_candles_stop_an_hour_after_settlement_and_at_now(db, make):
    make(default_fake()).run()
    # OLD settled 15:14:54; the window ends 16:14:54, so the 16:00 candle is the last.
    assert snapshots(db, "KXRT-OLD-90")[-1]["ts"] == "2026-09-21T16:00:00Z"
    assert snapshots(db, "KXRT-OPN-80")[-1]["ts"] == "2026-11-22T12:00:00Z"


def test_historical_market_reads_historical_endpoint_and_links_raw(db, make):
    fake = default_fake()
    make(fake).run()
    (req,) = fake.candle_requests("KXRT-OLD-90")
    assert req.url.path.endswith("/historical/markets/KXRT-OLD-90/candlesticks")
    assert req.url.params["period_interval"] == "60"

    row = snapshots(db, "KXRT-OLD-90")[0]
    fetch = db.execute("SELECT * FROM raw_fetches WHERE id = ?", (row["raw_fetch_id"],)).fetchone()
    assert "/historical/markets/KXRT-OLD-90/candlesticks" in fetch["url"]
    assert (row["yes_bid"], row["yes_ask"], row["last_price"]) == (0.48, 0.52, 0.5)


def test_rerun_adds_nothing_and_skips_complete_markets(db, make):
    make(default_fake()).run()
    count = db.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0]

    fake = default_fake()
    again = make(fake, now=NOW + timedelta(minutes=10)).run()
    assert again.ok
    assert db.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0] == count
    assert again.inserted == 0
    assert again.skipped == 2  # both settled markets: no request at all
    # The open market is re-asked only from its newest candle onward.
    (req,) = fake.candle_requests()
    assert "KXRT-OPN-80" in req.url.path
    assert int(req.url.params["start_ts"]) == ts(NOW)


def test_open_market_picks_up_new_hours(db, make):
    make(default_fake()).run()
    before = len(snapshots(db, "KXRT-OPN-80"))
    later = NOW + 3 * HOUR
    fake = default_fake()
    fake.candles["KXRT-OPN-80"] = hourly("2026-11-20T16:00:00Z", 47)
    result = make(fake, now=later).run()
    assert len(snapshots(db, "KXRT-OPN-80")) == before + 3
    assert result.inserted == 3


def test_long_market_is_fetched_in_chunks(db, make):
    hours = MAX_CANDLES_PER_REQUEST + 1500  # 5500: over Kalshi's 5000-candle limit in one go
    opened = NOW - hours * HOUR
    long = market("KXRT-LNG-90", opened.strftime("%Y-%m-%dT%H:%M:%SZ"), "2026-12-21T15:00:00Z")
    fake = FakeKalshi(
        live=[long],
        historical=[],
        candles={"KXRT-LNG-90": hourly(long["open_time"], hours)},
    )
    result = make(fake).run()
    assert result.ok, result.errors
    assert len(fake.candle_requests()) == 2
    assert result.inserted == hours
    assert len(snapshots(db, "KXRT-LNG-90")) == hours


def test_failing_market_is_skipped_then_resumed(db, make):
    first = make(default_fake(fail={"KXRT-NEW-40"})).run()
    assert not first.ok
    assert first.errors and "KXRT-NEW-40" in first.errors[0]
    assert snapshots(db, "KXRT-NEW-40") == []
    assert snapshots(db, "KXRT-OLD-90")  # the others still went in

    second = make(default_fake()).run()
    assert second.ok
    assert len(snapshots(db, "KXRT-NEW-40")) == 5 * 24  # through 16:00, an hour after close
    # OLD is complete; OPN already reaches NOW (the clock didn't move). Only NEW is fetched.
    assert (second.skipped, second.fetched) == (2, 1)


def test_market_in_both_lists_is_stored_once(db, make):
    """32 real markets settled around the cutoff appear in both lists."""
    fake = default_fake()
    fake.live.append(OLD)
    result = make(fake).run()
    assert result.ok
    assert result.markets == 3
    assert len(fake.candle_requests("KXRT-OLD-90")) == 1


def test_event_filter(db, make):
    fake = default_fake()
    result = make(fake).run(events={"KXRT-NEW"})
    assert result.markets == 1
    assert {r["ticker"] for r in db.execute("SELECT ticker FROM markets")} == {"KXRT-NEW-40"}
    assert len(fake.candle_requests()) == 1


# -- migration ----------------------------------------------------------------------


def test_init_db_adds_settlement_value_to_an_old_database(tmp_path):
    """A database made before the column existed (like the server's) gains it."""
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """CREATE TABLE markets (
                   ticker TEXT PRIMARY KEY, event_ticker TEXT NOT NULL,
                   series_ticker TEXT NOT NULL, film_id INTEGER, kalshi_title TEXT,
                   threshold REAL, strike_type TEXT, open_at TEXT, close_at TEXT,
                   settles_at TEXT, status TEXT, result TEXT, settled_at TEXT,
                   first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL) STRICT"""
        )
    init_db(path)
    init_db(path)  # and running it again is harmless
    with closing(get_connection(path)) as conn:
        columns = [r["name"] for r in conn.execute("PRAGMA table_info(markets)")]
        assert columns.count("settlement_value") == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO markets (ticker, event_ticker, series_ticker, settlement_value,"
                " first_seen_at, updated_at) VALUES ('T', 'E', 'S', 150,"
                " '2026-11-22T00:00:00Z', '2026-11-22T00:00:00Z')"
            )
