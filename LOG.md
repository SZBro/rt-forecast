# Change log

Every change to the repo gets an entry here, newest first, in the same commit as the change.
Claude adds an entry for every change it makes; teammates add one for theirs.

Entry format:

```
## YYYY-MM-DD: Short title
- **What:** what changed, in a sentence or two.
- **Why:** the reason or decision behind it.
- **Files:** main files touched.
- **Where:** branch (task #N), commit if known. By: who (Claude = made with Claude Code).
```

---

## 2026-11-22: Saved queries and `rt-q` command
- **What:** `sql/queries/` holds named read-only queries: `passes` (snapshots per 15-minute pass), `films` (films with markets), `film` (one film's latest price at each threshold) and `market` (one market's price history). `deploy/bin/rt-q` runs them on the server, e.g. `rt-q passes`, `rt-q film dune`, `rt-q market KXRT-DUNE-80`; with no arguments it lists the queries.
- **Why:** Checking the collected data shouldn't mean retyping SQL. Queries are plain `.sql` files so anyone can read, edit or add one. `rt-q` opens the database with `-readonly` as user `rt` (so SQLite's side files stay owned by `rt`), and only lets ticker characters into the `:arg` parameter. `film` uses each market's own latest snapshot (a correlated subquery), because `ts` is the per-market fetch time, not a per-pass time.
- **Files:** `sql/queries/*.sql`, `deploy/bin/rt-q`.
- **Where:** `server/v1` (task #6). Not committed yet. By: Claude, requested by Skyler.

## 2026-11-22: Server setup: secured Linode collecting every 15 min (task #6)
- **What:** Linode Nanode 1 GB (Ubuntu 24.04, Seattle, `172.238.60.143`, hostname `rt-forecast`; Linode backups and disk encryption on). Secured: user `skyler` (sudo, `adm`) with a new SSH key from Skyler's PC; SSH key-only with root login off; `ufw` denies all incoming except rate-limited SSH; unattended-upgrades installs security updates daily and reboots at 11:00 UTC when needed; verified after a reboot. Project runs as system user `rt`: code in `/opt/rt-forecast` (`uv sync --frozen --no-dev`), data in `/var/lib/rt-forecast` (`rt.db`, `raw/`) via `.env`. `rt-collect.timer` runs `rt-collect-once` at :00/:15/:30/:45 UTC; first manual pass on the server: 153 markets, 31 s, 0 errors. `rt-backup.timer` runs `rt-backup` daily at 10:40 UTC: `sqlite3 .backup` + `PRAGMA integrity_check` + gzip (14 days kept) and an rsync mirror of `raw/` into `/var/backups/rt-forecast`; first run OK (48 KB db, 1.3 MB raw).
- **Why:** Keys instead of passwords, no root login and a deny-by-default firewall close the usual ways in. Settings live in our own drop-in files: `00-` so sshd reads it before Linode's `50-cloud-init.conf` (first value wins), `52-` so apt reads it after `50unattended-upgrades` (last value wins). A timer running a one-shot pass (not a long-running loop) isolates crashes to one pass; `Persistent=true` catches up a pass missed during a reboot; `TimeoutStartSec` stops a hung pass from blocking the next. Units are sandboxed (`ProtectSystem=strict`) so they can only write their data folders. Code and data are kept apart so updating code can't touch data. The backup runs as `rt`, not root, so SQLite's `-wal`/`-shm` files never become root-owned and lock out the collector. Reboots (11:00 UTC) and backups (10:40 UTC) stay clear of Kalshi settlement (15:00 UTC Monday). Linode backups cover losing the server; our backups cover a bad write or deleted file.
- **Next:** Linode's Shutdown Watchdog (Lassie) is on, so automatic reboots power back up. First automatic timer pass at 23:30 UTC: 153 markets, 30.8 s, 0 errors. Still to do: add Chris (his own user + key) once he sends his `.pub`.
- **Files:** `deploy/ssh/00-hardening.conf`, `deploy/apt/52rt-forecast-autoreboot`, `deploy/systemd/rt-collect.{service,timer}`, `deploy/systemd/rt-backup.{service,timer}`, `deploy/backup/rt-backup.sh`.
- **Where:** `server/v1` (task #6), branched from `main` at `c43c400`. Not committed yet. By: Claude, steps run by Skyler.

## 2026-11-17: Live collector v1 (task #5)
- **What:** `rt-collect-once` snapshots every open KXRT market: best bid/ask **and the contracts resting at each** (from the order book), plus last price, volume and open interest, into `price_snapshots`. Each response is recorded in `raw_fetches` and linked from its snapshot. `rt-collect-loop --interval N` (default 15) repeats a pass at wall-clock-aligned ticks until stopped. `rt-collect-gaps` finds missing snapshot intervals per ticker and prints them merged into outages (`--per-ticker` for the full list); exits 1 if any. Proven with a real 70-min run at a 5-min interval on 2026-11-17: 145 markets, ~30 s per pass, 0 errors; the gap check caught both the deliberate 20-min kill (4 passes missed) and an unplanned 11-min pause (laptop lid closed mid-pass; the pass resumed and finished cleanly). Schema: `price_snapshots` gains `yes_bid_size`/`yes_ask_size`; `volume`/`open_interest` are now REAL (Kalshi sends fractional contracts). New `RT_RAW_DIR` setting; commands now read `.env` (nothing did before).
- **Why:** Order-book depth can't be backfilled, so it has to be collected live before the Nov 30 films' embargoes lift. `ts` is the order-book fetch time (no look-ahead). Reruns are safe through `ON CONFLICT DO NOTHING` on (ticker, ts, source), which, unlike `INSERT OR IGNORE`, doesn't hide CHECK failures. A failing market is logged and skipped, and a pass stops after 5 failures in a row (Kalshi down). A plain loop instead of APScheduler: no extra dependency for a 20-line job. On the server (#6), a systemd timer running `rt-collect-once` is the simpler, crash-isolated option.
- **Files:** `src/rt_forecast/collectors/kalshi_live.py`, `src/rt_forecast/collectors/gaps.py`, `src/rt_forecast/db/schema.sql`, `src/rt_forecast/db/db.py`, `tests/test_kalshi_live.py`, `pyproject.toml`, `.env.example`.
- **Where:** `collector/v1` (task #5), stacked on `kalshi-client/v1`. By: Claude.

## 2026-11-17: Add this change log
- **What:** Added `LOG.md`, backfilled with everything so far, and a rule in `CLAUDE.md`: Claude updates the log automatically with every change (no prompting needed), in the same commit, and then tells the user in chat with a short list of the important items logged.
- **Why:** One place to see what changed and why, without reading every commit or PR, and without having to remember to ask for it.
- **Files:** `LOG.md`, `CLAUDE.md`.
- **Where:** `kalshi-client/v1`. By: Claude, requested by Skyler.

## 2026-11-17: Read-only Kalshi client (task #3)
- **What:** Client for Kalshi's public market-data API: `list_markets` (paginated), `get_market`, `get_candlesticks` (switches to `/historical/` for older markets) and `get_orderbook`. Every raw response is saved gzipped under `data/raw/kalshi/` before parsing. Requests are throttled to 5 per second and retried with backoff. New command `uv run rt-kalshi-markets` prints every open KXRT market (139 markets across 12 films on first run).
- **Why:** Collectors (#5) and the history backfill (#4) build on it. It's read-only with no keys, because the project is simulation only.
- **Files:** `src/rt_forecast/collectors/kalshi.py`, `tests/test_kalshi.py` (all HTTP mocked), `pyproject.toml`.
- **Where:** `kalshi-client/v1`, commit `b196f4d`. By: Claude.

## 2026-11-17: Database schema v1 (task #2)
- **What:** `schema.sql` with every v1 table (films, film_genres, people, film_people, markets, price_snapshots, reviews, rt_snapshots, raw_fetches) plus stubs for classifications, predictions and trades. Tables are STRICT, and every timestamp is checked to be UTC ISO (`YYYY-MM-DDTHH:MM:SSZ`). `db.py` provides `init_db()` and `get_connection()` (foreign keys on, WAL mode). `uv run rt-init-db` creates an empty `data/rt.db`. Added `RT_DB_PATH` to `.env.example` and the WAL side files to `.gitignore`.
- **Why:** Most other tasks write to the database. Strict types and timestamp checks catch bad data at insert time.
- **Files:** `src/rt_forecast/db/schema.sql`, `src/rt_forecast/db/db.py`, `tests/test_db.py`, `pyproject.toml`, `.gitignore`, `.env.example`.
- **Where:** `schema/v1`, commit `2be1fdc`. By: Claude.

## 2026-11-17: Policy change, simulation only
- **What:** Kalshi is now a read-only public data source: no account, no API keys, no real orders, no `LIVE_TRADING` switch. "Trades" are simulated rows in our database (buy at the ask, sell at the bid, Kalshi fees, never more than the order book showed). Removed the Kalshi key lines from `.env.example` and `cryptography` from the stack. The `trades` table has no paper/real mode and no Kalshi order id.
- **Why:** Kalshi isn't available in Skyler's state. The project's main question (do our probabilities beat the market's odds?) only needs market prices, not real trades.
- **Files:** `CLAUDE.md` (rule 7 rewritten), `README.md`, `.env.example`, `schema.sql`.
- **Where:** `schema/v1`, commit `2be1fdc`. By: Claude, decided by Skyler.

## 2026-11-16: Repo setup (task #1)
- **What:** uv project (`pyproject.toml` + `uv.lock`) with httpx, pandas and python-dotenv, and pytest + ruff as dev tools with their config. Package layout `src/rt_forecast/{collectors,db,features,models}`, a smoke test, and README Getting Started steps. The repo URL is fixed to `SZBro/rt-forecast`, and the team table is filled in. `CLAUDE.md` is committed.
- **Why:** uv's lock file gives both teammates identical package versions. One named package means `import rt_forecast` works the same in scripts, tests and on the server.
- **Files:** `pyproject.toml`, `uv.lock`, `src/rt_forecast/`, `tests/test_smoke.py`, `README.md`, `CLAUDE.md`.
- **Where:** `setup/repo`, commit `4927a57`. By: Claude.

## 2026-11-16: Project details
- **What:** README with project overview, `.gitignore` (secrets, data, models) and `.env.example`.
- **Why:** Starting point from the planning doc.
- **Files:** `README.md`, `.gitignore`, `.env.example`.
- **Where:** `main`, commit `2fec517`. By: Skyler.

## 2026-11-16: Initial commit
- **What:** Created the GitHub repo.
- **Where:** `main`, commit `2cfe4db`. By: Skyler.
