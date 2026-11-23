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

## 2026-11-22: Kalshi history backfill (task #4)
- **What:** `rt-backfill-kalshi` lists every KXRT market, both live and `/historical/` (1647 markets, 107 films, back to 2025-12-30), and upserts them into `markets` with result, settled time and the new **`settlement_value`** (the Tomatometer Kalshi settled on, parsed from `expiration_value`). It then loads hourly candlesticks into `price_snapshots` with `source = 'candlestick'`, each row linked to its gzipped raw response. `--event KXRT-RES` (repeatable) limits it to some films. `init_db` now adds columns missing from older databases (`MIGRATIONS` in `db.py`), so existing databases gain `settlement_value` on their next run. Candles with no trade now take `price.previous` as last price. Real run on 4 settled films (SEN, SCR, RES, HOWT) on 2026-11-22: 62 markets, 25,314 candles in ~15 s, 0 errors; a rerun fetched nothing; all 47 parsed scores agree with yes/no.
- **Why:** Backtests need past prices, and the Week 2 go/no-go check needs Kalshi's settled score per film. A candle's `ts` is the **end** of its hour, the first moment its close was knowable (no look-ahead). Kalshi sends no candle for quiet hours, so backtests take the latest row at or before `as_of` and never interpolate. Some candles end after `close_at` (asks jump to 1.00), so backtests must not trade after close. Hourly only, under the existing `'candlestick'` source: no table rebuild on the server, daily can be rebuilt from hourly, and 1-minute data is about 60x the requests. Requests are chunked to 4000 candles because Kalshi refuses over 5000. Resume: each market starts from its newest saved candle, a settled market whose candles reach its close is skipped, and inserts use `ON CONFLICT DO NOTHING`. `settlement_value` is NULL when Kalshi's text isn't a plain number (one film says "Above 32"). Like `result`, only evaluation code may read it.
- **Files:** `src/rt_forecast/collectors/kalshi_backfill.py` (new), `src/rt_forecast/collectors/kalshi.py`, `src/rt_forecast/collectors/kalshi_live.py`, `src/rt_forecast/db/schema.sql`, `src/rt_forecast/db/db.py`, `tests/test_kalshi_backfill.py`, `pyproject.toml`.
- **Where:** `backfill/v1` (task #4). By: Claude.

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
