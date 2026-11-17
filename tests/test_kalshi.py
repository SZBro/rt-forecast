"""Tests for the read-only Kalshi client. All HTTP is mocked: no network calls."""

import gzip
import hashlib
import json
from datetime import UTC, datetime

import httpx
import pytest

from rt_forecast.collectors.kalshi import (
    KalshiClient,
    KalshiError,
    format_markets,
    main,
    parse_market,
)

# Trimmed copies of real responses (2026-11-17).
OPEN_MARKET = {
    "ticker": "KXRT-DUNE-92",
    "event_ticker": "KXRT-DUNE",
    "title": "Dune: Part Three Rotten Tomatoes score?",
    "floor_strike": 92,
    "strike_type": "greater",
    "status": "active",
    "open_time": "2026-11-12T17:00:00Z",
    "close_time": "2026-12-21T15:00:00Z",
    "expected_expiration_time": "2026-12-21T15:00:00Z",
    "result": "",
    "yes_bid_dollars": "0.4000",
    "yes_ask_dollars": "0.4500",
    "last_price_dollars": "0.4400",
    "volume_fp": "4820.04",
    "open_interest_fp": "3471.92",
}
SETTLED_MARKET = {
    **OPEN_MARKET,
    "ticker": "KXRT-UPR-58",
    "event_ticker": "KXRT-UPR",
    "floor_strike": 58,
    "status": "finalized",
    "result": "no",
    "settlement_ts": "2026-09-14T15:05:38.449041Z",
}
CUTOFF = {"market_settled_ts": "2026-09-18T00:00:00Z"}


class FakeKalshi:
    """Routes mocked requests by path and records what was asked for."""

    def __init__(self, routes):
        self.routes = routes  # path -> list of responses (served in order) or one response
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/trade-api/v2")
        route = self.routes.get(path)
        if route is None:
            return httpx.Response(404, json={"error": {"code": "not_found"}})
        response = route.pop(0) if isinstance(route, list) else route
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def make_client(tmp_path):
    sleeps: list[float] = []

    def build(routes):
        fake = FakeKalshi(routes)
        client = KalshiClient(
            tmp_path / "raw",
            transport=httpx.MockTransport(fake),
            sleep=sleeps.append,
            min_interval_s=0,
        )
        return client, fake, sleeps

    return build


def ok(body) -> httpx.Response:
    return httpx.Response(200, json=body)


# -- markets ------------------------------------------------------------------


def test_list_markets_follows_cursor_and_parses(make_client):
    page1 = {"markets": [OPEN_MARKET], "cursor": "abc"}
    page2 = {
        "markets": [{**OPEN_MARKET, "ticker": "KXRT-DUNE-97", "floor_strike": 97}],
        "cursor": "",
    }
    client, fake, _ = make_client({"/markets": [ok(page1), ok(page2)]})

    markets = client.list_markets()

    assert [m.ticker for m in markets] == ["KXRT-DUNE-92", "KXRT-DUNE-97"]
    first, second = fake.requests
    assert first.url.params["series_ticker"] == "KXRT"
    assert first.url.params["status"] == "open"
    assert "cursor" not in first.url.params
    assert second.url.params["cursor"] == "abc"

    m = markets[0]
    assert m.series_ticker == "KXRT"
    assert m.threshold == 92
    assert m.settles_at == "2026-12-21T15:00:00Z"
    assert m.settled_at is None
    assert m.result is None
    assert (m.yes_bid, m.yes_ask, m.last_price) == (0.40, 0.45, 0.44)
    assert m.volume == pytest.approx(4820.04)


def test_get_market_falls_back_to_historical(make_client):
    client, fake, _ = make_client(
        {"/historical/markets/KXRT-UPR-58": ok({"market": SETTLED_MARKET})}
    )
    m = client.get_market("KXRT-UPR-58")
    assert m.result == "no"
    assert m.settled_at == "2026-09-14T15:05:38Z"  # microseconds dropped, schema format
    assert [r.url.path.removeprefix("/trade-api/v2") for r in fake.requests] == [
        "/markets/KXRT-UPR-58",
        "/historical/markets/KXRT-UPR-58",
    ]


# -- raw saving ---------------------------------------------------------------


def test_every_response_is_saved_gzipped_before_parsing(make_client, tmp_path):
    body = {"markets": [OPEN_MARKET], "cursor": ""}
    client, _, _ = make_client({"/markets": ok(body)})
    fetches = []
    client.on_fetch = fetches.append

    client.list_markets()

    (fetch,) = fetches
    saved = tmp_path / "raw" / fetch.file_path
    assert fetch.file_path.startswith("kalshi/")
    assert saved.name.endswith("Z_markets.json.gz")
    assert json.loads(gzip.decompress(saved.read_bytes())) == body
    assert fetch.sha256 == hashlib.sha256(saved.read_bytes()).hexdigest()
    assert fetch.byte_size == saved.stat().st_size
    assert fetch.status_code == 200
    assert fetch.fetched_at.endswith("Z")


def test_raw_is_saved_even_if_parsing_fails(make_client, tmp_path):
    # A market missing required fields: parsing raises, but the file is on disk.
    client, _, _ = make_client({"/markets": ok({"markets": [{"ticker": "X"}], "cursor": ""})})
    with pytest.raises(KeyError):
        client.list_markets()
    assert len(list((tmp_path / "raw" / "kalshi").rglob("*.json.gz"))) == 1


# -- retries ------------------------------------------------------------------


def test_retries_429_and_5xx_with_exponential_backoff(make_client):
    responses = [
        httpx.Response(429, json={"error": {"code": "too_many_requests"}}),
        httpx.Response(503),
        httpx.ConnectError("reset"),
        ok({"market": OPEN_MARKET}),
    ]
    client, fake, sleeps = make_client({"/markets/KXRT-DUNE-92": responses})

    assert client.get_market("KXRT-DUNE-92").ticker == "KXRT-DUNE-92"
    assert len(fake.requests) == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_retry_after_header_is_respected(make_client):
    responses = [httpx.Response(429, headers={"Retry-After": "7"}), ok({"market": OPEN_MARKET})]
    client, _, sleeps = make_client({"/markets/KXRT-DUNE-92": responses})
    client.get_market("KXRT-DUNE-92")
    assert sleeps == [7.0]


def test_gives_up_after_max_retries(make_client):
    client, fake, sleeps = make_client({"/markets/KXRT-DUNE-92": httpx.Response(500)})
    client.max_retries = 2
    with pytest.raises(KalshiError) as exc:
        client.get_market("KXRT-DUNE-92")
    assert exc.value.status_code == 500
    assert len(fake.requests) == 3
    assert sleeps == [1.0, 2.0]


def test_client_errors_are_not_retried(make_client):
    client, fake, sleeps = make_client({"/markets/X/orderbook": httpx.Response(400)})
    with pytest.raises(KalshiError):
        client.get_orderbook("X")
    assert len(fake.requests) == 1
    assert sleeps == []


def test_throttle_spaces_out_requests(tmp_path):
    sleeps: list[float] = []
    now = [100.0]
    client = KalshiClient(
        tmp_path,
        transport=httpx.MockTransport(lambda r: ok({"market": OPEN_MARKET})),
        sleep=sleeps.append,
        clock=lambda: now[0],
        min_interval_s=0.2,
    )
    client.get_market("A")
    now[0] += 0.05  # next call comes 50 ms later
    client.get_market("B")
    assert sleeps == [pytest.approx(0.15)]


# -- headers ------------------------------------------------------------------


def test_requests_are_anonymous_with_descriptive_user_agent(make_client):
    client, fake, _ = make_client({"/markets/KXRT-DUNE-92": ok({"market": OPEN_MARKET})})
    client.get_market("KXRT-DUNE-92")
    headers = fake.requests[0].headers
    assert headers["User-Agent"].startswith("rt-forecast/")
    assert "read-only" in headers["User-Agent"]
    assert not any(h.lower().startswith("kalshi-access") for h in headers)
    assert "authorization" not in headers


# -- candlesticks -------------------------------------------------------------

LIVE_CANDLE = {
    "end_period_ts": 1794690000,
    "open_interest_fp": "307.19",
    "volume_fp": "11.18",
    "price": {"close_dollars": "0.4300"},
    "yes_ask": {"close_dollars": "0.4500"},
    "yes_bid": {"close_dollars": "0.4000"},
}
# The historical endpoint drops the _dollars/_fp suffixes.
HISTORICAL_CANDLE = {
    "end_period_ts": 1789185600,
    "open_interest": "110.00",
    "volume": "110.00",
    "price": {"close": None},
    "yes_ask": {"close": "0.0700"},
    "yes_bid": {"close": "0.0300"},
}
START = datetime(2026, 9, 10, tzinfo=UTC)
END = datetime(2026, 9, 16, tzinfo=UTC)


def test_candlesticks_for_open_market_use_live_endpoint(make_client):
    client, fake, _ = make_client(
        {
            "/markets/KXRT-DUNE-92": ok({"market": OPEN_MARKET}),
            "/historical/cutoff": ok(CUTOFF),
            "/series/KXRT/markets/KXRT-DUNE-92/candlesticks": ok({"candlesticks": [LIVE_CANDLE]}),
        }
    )
    (candle,) = client.get_candlesticks("KXRT-DUNE-92", START, END, interval=60)
    assert candle.end_at == "2026-11-14T21:00:00Z"
    assert (candle.yes_bid, candle.yes_ask, candle.price) == (0.40, 0.45, 0.43)
    assert candle.volume == pytest.approx(11.18)
    params = fake.requests[-1].url.params
    assert params["period_interval"] == "60"
    assert params["start_ts"] == str(int(START.timestamp()))


def test_candlesticks_switch_to_historical_before_cutoff(make_client):
    client, fake, _ = make_client(
        {
            "/historical/cutoff": ok(CUTOFF),
            "/historical/markets/KXRT-UPR-58/candlesticks": ok(
                {"candlesticks": [HISTORICAL_CANDLE]}
            ),
        }
    )
    (candle,) = client.get_candlesticks(parse_market(SETTLED_MARKET), START, END, interval=1440)
    assert fake.requests[-1].url.path.endswith("/historical/markets/KXRT-UPR-58/candlesticks")
    assert (candle.yes_bid, candle.yes_ask, candle.price) == (0.03, 0.07, None)
    assert candle.open_interest == 110.0


def test_candlesticks_reject_unknown_interval(make_client):
    client, _, _ = make_client({})
    with pytest.raises(ValueError):
        client.get_candlesticks("X", START, END, interval=15)


# -- order book ---------------------------------------------------------------


def test_orderbook_best_bid_and_ask(make_client):
    body = {
        "orderbook_fp": {
            "yes_dollars": [["0.2500", "259.00"], ["0.4400", "1707.75"]],
            "no_dollars": [["0.4900", "707.50"], ["0.5500", "8843.88"]],
        }
    }
    client, fake, _ = make_client({"/markets/KXRT-DUNE-92/orderbook": ok(body)})
    book = client.get_orderbook("KXRT-DUNE-92")
    assert book.best_yes_bid == (0.44, 1707.75)
    assert book.best_yes_ask == (0.45, 8843.88)  # 1 - best NO bid of 0.55
    assert "depth" not in fake.requests[0].url.params


def test_empty_orderbook(make_client):
    client, _, _ = make_client({"/markets/X/orderbook": ok({"orderbook_fp": {}})})
    book = client.get_orderbook("X")
    assert book.best_yes_bid is None and book.best_yes_ask is None


# -- script -------------------------------------------------------------------


def test_format_markets_shows_threshold_and_settlement_time(make_client):
    client, _, _ = make_client({"/markets": ok({"markets": [OPEN_MARKET], "cursor": ""})})
    text = format_markets(client.list_markets())
    assert "KXRT-DUNE-92" in text
    assert "> 92" in text
    assert "2026-12-21T15:00:00Z" in text
    assert "1 open markets across 1 films." in text


def test_main_prints_open_markets(monkeypatch, tmp_path, capsys):
    transport = httpx.MockTransport(lambda r: ok({"markets": [OPEN_MARKET], "cursor": ""}))
    real_init = KalshiClient.__init__

    def fake_init(self, raw_root, **kwargs):
        real_init(self, raw_root, transport=transport, min_interval_s=0)

    monkeypatch.setattr(KalshiClient, "__init__", fake_init)
    main(["--raw-root", str(tmp_path)])
    assert "KXRT-DUNE-92" in capsys.readouterr().out
