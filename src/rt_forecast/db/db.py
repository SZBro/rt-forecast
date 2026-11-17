"""SQLite helpers: create the database and open connections.

Create an empty database with every table:

    uv run rt-init-db                 # data/rt.db (or $RT_DB_PATH)
    uv run rt-init-db --path my.db
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

# STRICT tables need SQLite 3.37+. Python ships its own SQLite on Windows,
# but on Linux it uses the system one, which can be older (e.g. Debian 11).
MIN_SQLITE_VERSION = (3, 37, 0)


def default_db_path() -> Path:
    """Database path from $RT_DB_PATH, else data/rt.db."""
    return Path(os.environ.get("RT_DB_PATH", "data/rt.db"))


def to_utc_iso(dt: datetime) -> str:
    """Format a timestamp the one way the schema accepts: '2026-11-24T15:00:00Z'.

    Naive datetimes are rejected: Python would silently treat them as local
    time, which is exactly the kind of bug the UTC rule exists to prevent.
    """
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime {dt!r}: attach a timezone first")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_iso() -> str:
    """The current time, formatted with to_utc_iso()."""
    return to_utc_iso(datetime.now(UTC))


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection with the settings every caller needs.

    These are per-connection in SQLite, so they live here rather than in
    schema.sql: a connection opened any other way would skip them.
    """
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        raise RuntimeError(f"SQLite {sqlite3.sqlite_version} is too old; STRICT tables need 3.37+")
    conn = sqlite3.connect(db_path or default_db_path())
    conn.row_factory = sqlite3.Row  # rows behave like dicts: row["ticker"]
    conn.execute("PRAGMA foreign_keys = ON")  # off by default in SQLite
    conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer
    conn.execute("PRAGMA busy_timeout = 5000")  # wait up to 5 s for a lock
    return conn


def init_db(db_path: str | Path | None = None) -> Path:
    """Create the database file and every table. Safe to run more than once."""
    path = Path(db_path or default_db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    schema = resources.files("rt_forecast.db").joinpath("schema.sql").read_text("utf-8")
    with closing(get_connection(path)) as conn:
        conn.executescript(schema)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an empty rt-forecast database.")
    parser.add_argument(
        "--path", type=Path, help="database file (default: $RT_DB_PATH or data/rt.db)"
    )
    args = parser.parse_args()
    print(f"Initialised {init_db(args.path)}")


if __name__ == "__main__":
    main()
