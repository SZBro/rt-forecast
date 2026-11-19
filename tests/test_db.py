import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone

import pytest

from rt_forecast.db.db import get_connection, init_db, to_utc_iso

EXPECTED_TABLES = {
    "raw_fetches",
    "films",
    "film_genres",
    "people",
    "film_people",
    "markets",
    "price_snapshots",
    "reviews",
    "rt_snapshots",
    "classifications",
    "predictions",
    "trades",
}


@pytest.fixture
def conn(tmp_path):
    c = get_connection(init_db(tmp_path / "rt.db"))
    yield c
    c.close()


def table_names(conn) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {r["name"] for r in rows}


def test_init_db_creates_every_table(conn):
    assert table_names(conn) == EXPECTED_TABLES


def test_init_db_is_safe_to_run_twice(tmp_path):
    path = tmp_path / "rt.db"
    init_db(path)
    init_db(path)
    with closing(get_connection(path)) as c:
        assert table_names(c) == EXPECTED_TABLES


def test_bad_timestamp_format_is_rejected(conn):
    # '+00:00' is what datetime.isoformat() gives; the schema only takes 'Z'.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO films (title, created_at) VALUES (?, ?)",
            ("Test", "2026-11-24T15:00:00+00:00"),
        )


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO film_genres (film_id, genre) VALUES (?, ?)", (999, "Drama"))


def test_to_utc_iso_converts_to_utc():
    eastern = timezone(timedelta(hours=-5))
    assert to_utc_iso(datetime(2026, 11, 30, 10, 0, tzinfo=eastern)) == "2026-11-30T15:00:00Z"
    assert to_utc_iso(datetime(2026, 11, 30, 15, 0, 0, 999, tzinfo=UTC)) == "2026-11-30T15:00:00Z"


def test_to_utc_iso_rejects_naive_datetime():
    with pytest.raises(ValueError):
        to_utc_iso(datetime(2026, 11, 30, 10, 0))


def insert_prediction(conn) -> int:
    ts = "2026-11-24T15:00:00Z"
    conn.execute(
        "INSERT INTO markets (ticker, event_ticker, series_ticker, first_seen_at, updated_at)"
        " VALUES ('KXRT-TEST-80', 'KXRT-TEST', 'KXRT', ?, ?)",
        (ts, ts),
    )
    cur = conn.execute(
        "INSERT INTO predictions (run_id, run_mode, ticker, as_of, model_version, prob_raw,"
        " created_at) VALUES ('test', 'live', 'KXRT-TEST-80', ?, 'v0', 0.6, ?)",
        (ts, ts),
    )
    return cur.lastrowid


def insert_trade(conn, prediction_id: int, trade_key: str, contracts: int, book_size: int):
    conn.execute(
        "INSERT INTO trades (prediction_id, ticker, side, action, contracts, book_size,"
        " limit_price, trade_key, status, placed_at)"
        " VALUES (?, 'KXRT-TEST-80', 'yes', 'buy', ?, ?, 0.5, ?, 'filled',"
        " '2026-11-24T15:00:00Z')",
        (prediction_id, contracts, book_size, trade_key),
    )


def test_trade_cannot_exceed_book_size(conn):
    pid = insert_prediction(conn)
    insert_trade(conn, pid, "t1", contracts=10, book_size=10)
    with pytest.raises(sqlite3.IntegrityError):
        insert_trade(conn, pid, "t2", contracts=11, book_size=10)


def test_duplicate_trade_key_is_rejected(conn):
    # A restart that repeats the same decision must not record a second trade.
    pid = insert_prediction(conn)
    insert_trade(conn, pid, "pred-1-yes-buy", contracts=5, book_size=10)
    with pytest.raises(sqlite3.IntegrityError):
        insert_trade(conn, pid, "pred-1-yes-buy", contracts=5, book_size=10)
