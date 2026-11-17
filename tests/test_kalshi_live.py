"""Tests for the live collector and gap checker. HTTP is mocked; the database is temporary."""

from contextlib import closing
from datetime import UTC, datetime

import httpx
import pytest

from rt_forecast.collectors import kalshi, kalshi_live
from rt_forecast.collectors.gaps import Gap, Outage, find_gaps, format_gaps, group_outages
from rt_forecast.collectors.kalshi import KalshiClient
from rt_forecast.collectors.kalshi_live import (
    MAX_CONSECUTIVE_FAILURES,
    Collector,
    collect_once,
    next_tick,
    run_loop,
)
from rt_forecast.db.db import get_connection, init_db


def market(ticker, strike=90, status="active", close="2026-11-30T15:00:00Z"):
    return {
        "ticker": ticker,
        "event_ticker": ticker.rsplit("-", 1)[0],
        "title": f"{ticker} Rotten Tomatoes score?",
        "floor_strike": strike,
        "strike_type": "greater",
        "status": status,
        "open_time": "2026-11-12T17:00:00Z",
        "close_time": close,
        "expected_expiration_time": close,
        "result": "",
        "last_price_dollars": "0.4400",
        "volume_fp": "4820.04",
        "open_interest_fp": "3471.92",
    }


BOOK = {
    "orderbook_fp": {
        "yes_dollars": [["0.2500", "259.00"], ["0.4400", "1707.75"]],
        "no_dollars": [["0.4900", "707.50"], ["0.5500", "8843.88"]],
    }
}


def ok(body):
    return httpx.Response(200, json=body)


class FakeKalshi:
    """Serves /markets and /markets/{t}/orderbook from dicts; anything else is 404."""

    def __init__(self, markets, books):
        self.markets = markets
        self.books = books  # ticker -> response, or list of responses served in order

    def __call__(self, request):
        path = request.url.path.removeprefix("/trade-api/v2")
        if path == "/markets":
            return ok({"markets": self.markets, "cursor": ""})
        ticker = path.removeprefix("/markets/").removesuffix("/orderbook")
        route = self.books.get(ticker, httpx.Response(404))
        return route.pop(0) if isinstance(route, list) else route


@pytest.fixture
def db(tmp_path):
    path = init_db(tmp_path / "rt.db")
    with closing(get_connection(path)) as conn:
        yield conn


@pytest.fixture
def make_collector(db, tmp_path):
    def build(markets, books):
        client = KalshiClient(
            tmp_path / "raw",
            transport=httpx.MockTransport(FakeKalshi(markets, books)),
            sleep=lambda s: None,
            min_interval_s=0,
            max_retries=2,
        )
        return Collector(db, client)

    return build


def freeze_fetch_time(monkeypatch, when="2026-11-17T18:00:00"):
    """Make every raw fetch stamped at the same second, like a very fast rerun."""
    fixed = datetime.fromisoformat(when).replace(tzinfo=UTC)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(kalshi, "datetime", Frozen)


# -- one pass -------------------------------------------------------------------


def test_pass_stores_prices_and_depth(db, make_collector):
    collector = make_collector([market("KXRT-DUNE-90")], {"KXRT-DUNE-90": ok(BOOK)})
    result = collector.run_pass()

    assert result.ok and result.markets == 1 and result.inserted == 1
    row = db.execute("SELECT * FROM price_snapshots").fetchone()
    assert row["ticker"] == "KXRT-DUNE-90" and row["source"] == "live"
    assert (row["yes_bid"], row["yes_bid_size"]) == (0.44, 1707.75)
    assert (row["yes_ask"], row["yes_ask_size"]) == (0.45, 8843.88)  # 1 - best NO bid
    assert row["last_price"] == 0.44
    assert row["volume"] == pytest.approx(4820.04)  # fractional contracts kept


def test_snapshot_links_to_its_raw_fetch_and_uses_its_time(db, make_collector):
    make_collector([market("KXRT-DUNE-90")], {"KXRT-DUNE-90": ok(BOOK)}).run_pass()

    fetches = db.execute("SELECT * FROM raw_fetches ORDER BY id").fetchall()
    assert len(fetches) == 2  # the market list, then the order book
    assert all(f["source"] == "kalshi" for f in fetches)
    snap = db.execute("SELECT * FROM price_snapshots").fetchone()
    book_fetch = fetches[1]
    assert snap["raw_fetch_id"] == book_fetch["id"]
    assert snap["ts"] == book_fetch["fetched_at"]
    assert book_fetch["url"].endswith("/markets/KXRT-DUNE-90/orderbook")


def test_empty_order_book_gives_null_prices(db, make_collector):
    collector = make_collector([market("X-1")], {"X-1": ok({"orderbook_fp": {}})})
    assert collector.run_pass().inserted == 1
    row = db.execute("SELECT * FROM price_snapshots").fetchone()
    assert row["yes_bid"] is None and row["yes_ask_size"] is None


def test_markets_are_upserted_keeping_first_seen(db, make_collector):
    make_collector([market("X-1")], {"X-1": ok(BOOK)}).run_pass()
    db.execute("UPDATE markets SET first_seen_at = '2026-11-01T00:00:00Z', film_id = NULL")
    db.commit()
    make_collector([market("X-1", status="closed")], {"X-1": ok(BOOK)}).run_pass()

    row = db.execute("SELECT * FROM markets").fetchone()
    assert row["status"] == "closed"
    assert row["first_seen_at"] == "2026-11-01T00:00:00Z"
    assert row["threshold"] == 90 and row["settles_at"] == "2026-11-30T15:00:00Z"


# -- reruns -------------------------------------------------------------------------


def test_rerun_at_same_timestamp_adds_no_duplicates(db, make_collector, monkeypatch):
    freeze_fetch_time(monkeypatch)
    markets = [market("X-1"), market("X-2", strike=80)]
    books = {"X-1": ok(BOOK), "X-2": ok(BOOK)}

    first = make_collector(markets, books).run_pass()
    second = make_collector(markets, books).run_pass()

    assert (first.inserted, first.duplicates) == (2, 0)
    assert (second.inserted, second.duplicates) == (0, 2)
    assert db.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0] == 2


# -- failures -----------------------------------------------------------------------


def test_one_failing_market_does_not_stop_the_others(db, make_collector):
    markets = [market("X-1"), market("X-2"), market("X-3")]
    books = {"X-1": ok(BOOK), "X-2": httpx.Response(400), "X-3": ok(BOOK)}
    result = make_collector(markets, books).run_pass()

    assert result.inserted == 2 and not result.ok and not result.aborted
    assert "X-2" in result.errors[0]
    tickers = {r[0] for r in db.execute("SELECT ticker FROM price_snapshots")}
    assert tickers == {"X-1", "X-3"}


def test_transient_errors_are_retried(db, make_collector):
    books = {"X-1": [httpx.Response(503), httpx.Response(429), ok(BOOK)]}
    result = make_collector([market("X-1")], books).run_pass()
    assert result.ok and result.inserted == 1


def test_pass_stops_early_when_kalshi_looks_down(db, make_collector):
    markets = [market(f"X-{i}") for i in range(20)]
    books = {m["ticker"]: httpx.Response(503) for m in markets}
    result = make_collector(markets, books).run_pass()

    assert result.aborted
    assert len(result.errors) == MAX_CONSECUTIVE_FAILURES


def test_failed_market_list_is_reported_not_raised(db, tmp_path):
    client = KalshiClient(
        tmp_path / "raw",
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        sleep=lambda s: None,
        min_interval_s=0,
        max_retries=1,
    )
    result = Collector(db, client).run_pass()
    assert result.aborted and "list_markets" in result.errors[0]


def test_collect_once_opens_and_closes_everything(tmp_path):
    db_path = init_db(tmp_path / "rt.db")
    fake = FakeKalshi([market("X-1")], {"X-1": ok(BOOK)})

    def factory(raw_dir):
        return KalshiClient(raw_dir, transport=httpx.MockTransport(fake), min_interval_s=0)

    result = collect_once(db_path, tmp_path / "raw", client_factory=factory)
    assert result.ok and result.inserted == 1
    assert list((tmp_path / "raw" / "kalshi").rglob("*.json.gz"))


# -- loop -----------------------------------------------------------------------------


def test_next_tick_aligns_to_the_wall_clock():
    t = datetime(2026, 11, 17, 18, 7, 30, tzinfo=UTC)
    assert next_tick(t, 15) == datetime(2026, 11, 17, 18, 15, tzinfo=UTC)
    on_the_tick = datetime(2026, 11, 17, 18, 15, tzinfo=UTC)
    assert next_tick(on_the_tick, 15) == datetime(2026, 11, 17, 18, 30, tzinfo=UTC)


def test_loop_survives_a_crashing_pass_and_sleeps_until_the_tick():
    calls, sleeps = [], []

    def collect(db_path, raw_dir):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")

    clock = datetime(2026, 11, 17, 18, 7, 30, tzinfo=UTC)
    run_loop(15, collect=collect, sleep=sleeps.append, now=lambda: clock, max_passes=3)

    assert len(calls) == 3  # the crash didn't stop the loop
    assert sleeps == [450.0, 450.0]  # 18:07:30 -> 18:15:00


# -- gap check ------------------------------------------------------------------------


def add_snapshots(db, ticker, times, close_at="2026-11-30T15:00:00Z"):
    db.execute(
        """INSERT OR IGNORE INTO markets (ticker, event_ticker, series_ticker, close_at,
               first_seen_at, updated_at)
           VALUES (?, 'E', 'KXRT', ?, '2026-11-17T00:00:00Z', '2026-11-17T00:00:00Z')""",
        (ticker, close_at),
    )
    db.executemany(
        "INSERT INTO price_snapshots (ticker, ts, source) VALUES (?, ?, 'live')",
        [(ticker, f"2026-11-17T{t}Z") for t in times],
    )
    db.commit()


NOW = datetime(2026, 11, 17, 19, 0, 20, tzinfo=UTC)


def test_no_gaps_when_every_interval_is_present(db):
    # Small jitter (a pass takes a while to get round) is not a gap.
    add_snapshots(db, "X-1", ["18:00:05", "18:15:20", "18:30:02", "18:45:31", "19:00:10"])
    assert find_gaps(db, 15, now=NOW) == []


def test_gap_is_found_with_missed_count(db):
    add_snapshots(db, "X-1", ["18:00:05", "18:15:20", "18:45:31", "19:00:10"])
    add_snapshots(db, "X-2", ["18:00:06", "18:15:21", "18:30:03", "18:45:32", "19:00:11"])
    gaps = find_gaps(db, 15, now=NOW)
    assert gaps == [Gap("X-1", "2026-11-17T18:15:20Z", "2026-11-17T18:45:31Z", 1)]
    assert "1 gaps in 1 of 2 tickers" in format_gaps(gaps, 2)
    assert "X-1" in format_gaps(gaps, 2, per_ticker=True)


def test_long_outage_counts_every_missed_pass(db):
    add_snapshots(db, "X-1", ["18:00:05", "19:00:10"])
    assert find_gaps(db, 15, now=NOW)[0].missed == 3  # 18:15, 18:30, 18:45


def test_open_market_with_no_recent_snapshot_has_trailing_gap(db):
    add_snapshots(db, "X-1", ["18:00:05", "18:15:20"])
    gaps = find_gaps(db, 15, now=NOW)
    assert gaps == [Gap("X-1", "2026-11-17T18:15:20Z", None, 2)]


def test_closed_market_is_not_a_trailing_gap(db):
    add_snapshots(db, "X-1", ["18:00:05", "18:15:20"], close_at="2026-11-17T18:20:00Z")
    assert find_gaps(db, 15, now=NOW) == []


def test_overlapping_gaps_merge_into_one_outage(db):
    # A stall during the 18:15 pass: X-1 was next fetched at 18:30, X-2 when the
    # stall ended at 18:24. Different spans, but they overlap: one outage.
    add_snapshots(db, "X-1", ["18:00:01", "18:30:01", "18:45:01"])
    add_snapshots(db, "X-2", ["18:00:02", "18:24:00", "18:30:02", "18:45:02"])
    add_snapshots(db, "X-3", ["18:00:03", "18:15:41", "18:30:03", "18:45:03"])
    now = datetime(2026, 11, 17, 18, 50, tzinfo=UTC)
    outages = group_outages(find_gaps(db, 15, now=now))
    assert outages == [Outage("2026-11-17T18:00:01Z", "2026-11-17T18:30:01Z", 2, 2)]
    assert "Run with --per-ticker" in format_gaps(find_gaps(db, 15, now=now), 3)


def test_since_ignores_older_gaps(db):
    add_snapshots(db, "X-1", ["17:00:00", "18:00:05", "18:15:20", "18:30:02", "18:45:31"])
    assert find_gaps(db, 15, since="2026-11-17T18:00:00Z", now=NOW) == []


def test_collector_module_has_no_order_endpoints():
    """Rule 7: the collector only reads."""
    import inspect

    source = inspect.getsource(kalshi_live).lower()
    assert "/orders" not in source and ".post(" not in source
