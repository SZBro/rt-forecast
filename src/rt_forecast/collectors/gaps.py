"""Find missing live snapshot intervals, per ticker.

The collector should leave one live price_snapshots row per ticker per
interval. A gap is two consecutive snapshots of a ticker more than
1.5 intervals apart (the slack covers a pass taking ~30 s to get round
every market). A market that is still open but hasn't been snapshotted
recently has an open gap ending "now": the collector is probably down.

    uv run rt-collect-gaps                    # 15-minute interval, all history
    uv run rt-collect-gaps --interval 5 --since 2026-11-17T18:00:00Z

Exits 1 if any gap is found, so a monitor or cron job can alert on it.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from rt_forecast.collectors.kalshi_live import DEFAULT_INTERVAL_MIN
from rt_forecast.db.db import default_db_path, get_connection, load_settings, to_utc_iso

TOLERANCE = 1.5  # a gap is more than 1.5 intervals between snapshots


@dataclass(frozen=True)
class Gap:
    ticker: str
    last_before: str  # last snapshot before the gap
    first_after: str | None  # next snapshot, or None if still missing now
    missed: int  # how many passes are missing


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def find_gaps(
    conn: sqlite3.Connection,
    interval_min: int = DEFAULT_INTERVAL_MIN,
    since: str | None = None,
    now: datetime | None = None,
) -> list[Gap]:
    """Every gap in live snapshots, sorted by ticker then time."""
    interval_s = interval_min * 60
    limit_s = interval_s * TOLERANCE
    now = now or datetime.now(UTC)
    rows = conn.execute(
        """SELECT p.ticker, p.ts, m.close_at
           FROM price_snapshots p JOIN markets m USING (ticker)
           WHERE p.source = 'live' AND p.ts >= ?
           ORDER BY p.ticker, p.ts""",
        (since or "",),
    ).fetchall()

    gaps: list[Gap] = []
    for i, row in enumerate(rows):
        ts = _parse(row["ts"])
        is_last = i + 1 == len(rows) or rows[i + 1]["ticker"] != row["ticker"]
        if not is_last:
            nxt = rows[i + 1]["ts"]
            diff = (_parse(nxt) - ts).total_seconds()
            if diff > limit_s:
                # e.g. snapshots at :00 and :45 with 15-min interval -> :15, :30 missing
                gaps.append(Gap(row["ticker"], row["ts"], nxt, round(diff / interval_s) - 1))
        else:
            # Trailing gap: only while the market is still open.
            end = min(now, _parse(row["close_at"])) if row["close_at"] else now
            diff = (end - ts).total_seconds()
            if diff > limit_s:
                # Same slack as above: a pass due a moment ago may still be running.
                missed = int((diff - (limit_s - interval_s)) // interval_s)
                gaps.append(Gap(row["ticker"], row["ts"], None, missed))
    return gaps


@dataclass(frozen=True)
class Outage:
    """Gaps that overlap in time, merged: usually the collector being down."""

    start: str  # earliest last-snapshot-before
    end: str | None  # latest first-snapshot-after; None if still ongoing
    tickers: int
    missed: int


def group_outages(gaps: list[Gap]) -> list[Outage]:
    """Merge gaps whose time spans overlap.

    A collector outage hits every ticker at once, so 145 per-ticker gaps
    usually mean one event. A gap mid-pass splits tickers into slightly
    different spans (fetched just before vs just after), which overlap and
    so still merge into one outage.
    """
    open_end = "9999"  # sorts after any timestamp: "still missing"
    groups: list[list[Gap]] = []
    group_end = ""
    for g in sorted(gaps, key=lambda g: g.last_before):
        end = g.first_after or open_end
        if groups and g.last_before <= group_end:
            groups[-1].append(g)
            group_end = max(group_end, end)
        else:
            groups.append([g])
            group_end = end
    outages = []
    for group in groups:
        end = max(g.first_after or open_end for g in group)
        outages.append(
            Outage(
                start=min(g.last_before for g in group),
                end=None if end == open_end else end,
                tickers=len({g.ticker for g in group}),
                missed=sum(g.missed for g in group),
            )
        )
    return outages


def format_gaps(gaps: list[Gap], tickers_checked: int, per_ticker: bool = False) -> str:
    if not gaps:
        return f"No gaps across {tickers_checked} tickers."
    lines = []
    if per_ticker:
        header = f"{'TICKER':<22} {'LAST BEFORE':<20}  {'FIRST AFTER':<20} {'MISSED':>6}"
        lines += [header, "-" * len(header)]
        for g in gaps:
            after = g.first_after or "(still missing)"
            lines.append(f"{g.ticker:<22} {g.last_before:<20}  {after:<20} {g.missed:>6}")
        lines.append("")
    header = f"{'OUTAGE FROM (UTC)':<20}  {'TO':<20} {'TICKERS':>7} {'MISSED':>6}"
    lines += [header, "-" * len(header)]
    for o in group_outages(gaps):
        end = o.end or "(ongoing)"
        lines.append(f"{o.start:<20}  {end:<20} {o.tickers:>7} {o.missed:>6}")
    affected = len({g.ticker for g in gaps})
    total = sum(g.missed for g in gaps)
    lines.append(
        f"\n{len(gaps)} gaps in {affected} of {tickers_checked} tickers, "
        f"{total} missed snapshots in total."
    )
    if not per_ticker:
        lines.append("Run with --per-ticker for every ticker's gaps.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    load_settings()
    parser = argparse.ArgumentParser(description="Report missing live snapshot intervals.")
    parser.add_argument("--db", type=Path, help="database file (default: $RT_DB_PATH)")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_MIN,
        help=f"the collector's interval in minutes (default: {DEFAULT_INTERVAL_MIN})",
    )
    parser.add_argument("--since", help="only check snapshots from this UTC time on")
    parser.add_argument(
        "--per-ticker", action="store_true", help="list every ticker's gaps, not just outages"
    )
    args = parser.parse_args(argv)

    since = to_utc_iso(_parse(args.since)) if args.since else None
    with closing(get_connection(args.db or default_db_path())) as conn:
        gaps = find_gaps(conn, args.interval, since)
        tickers = conn.execute(
            "SELECT COUNT(DISTINCT ticker) FROM price_snapshots WHERE source = 'live' AND ts >= ?",
            (since or "",),
        ).fetchone()[0]
    print(format_gaps(gaps, tickers, args.per_ticker))
    sys.exit(1 if gaps else 0)


if __name__ == "__main__":
    main()
