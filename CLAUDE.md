# CLAUDE.md

Context for Claude Code in this repo. Read this before making changes.

## Project

A 5-week class "Moonshot" project (Nov 16 to Dec 17, 2026) by Skyler and Chris.

We predict a film's final Rotten Tomatoes Tomatometer score and compare our odds to Kalshi prediction-market prices (series `KXRT`). Kalshi settles each market on the score the **Monday after wide release at 10:00 AM ET**. The goal is to show, with honest backtests, whether our probabilities beat the market's own odds, then run a simulated trader on live prices.

**Kalshi is a read-only data source.** Kalshi is not available in our state, so we never open an account, place orders, or use API keys. "Trades" are simulated rows in our own database, priced from public Kalshi market data.

**Status:** planning is done (design doc in a shared Google Doc: Overview, Technical, Tech Stack, Week 1 tabs). Code is just starting.

## Architecture

```
Data sources → Collectors + database → Models → Edge vs. Kalshi price → Simulated trader / Backtest + dashboard
```

| Component | Approach |
|---|---|
| Kalshi data | Public market-data API, read-only, no account or keys: markets, candlesticks, order books. Settled markets older than the cutoff come from the `/historical/` endpoints. |
| Film metadata | Wikidata (SPARQL, CC0). Not TMDB: its free license forbids commercial use. |
| Reviews | Kaggle Rotten Tomatoes dataset (to about 2020), scraped Rotten Tomatoes pages (2021 to 2026), critic outlet RSS feeds. |
| Prior model | LightGBM. Predicts p (chance a review is Fresh) and N (review count at settlement), plus confidence. Baselines first: director average, genre average. |
| Review classifier | Our own: TF-IDF + logistic regression baseline, then fine-tuned DistilBERT. An LLM is only a benchmark/fallback (no paid API by default). |
| Live updater | Beta-Binomial: prior as pseudo-reviews + reviews so far, Monte Carlo over remaining reviews, drift correction for early reviews. Outputs P(score > X) for every Kalshi threshold. |
| Calibration | Isotonic regression fit on dev predictions only. |
| Decision | Edge = calibrated P minus ask price after fees. Simulate a trade only above a safety margin; size with 1/4 Kelly inside hard caps on a simulated bankroll. |
| Simulated trader | Records would-be trades in our database: buy at the ask, sell at the bid, charge Kalshi's fee formula, never fill more than the order book showed. Nothing is sent to Kalshi. |

## Rules (do not break these)

1. **No look-ahead.** Every feature and model function takes an `as_of` timestamp and uses only data from before it. People stats (e.g. director average) exclude the film being predicted and anything released after it.
2. **Same code for backtest and live.** The backtest replays history through the exact functions the live system calls.
3. **Never overwrite data.** Time-series tables only get new timestamped rows. Save raw responses (gzipped) before parsing.
4. **Splits are by film and by time.** Never random. Train < dev < test by settlement date; the test set is touched once at the end. All scripts import splits from one module.
5. **Log every prediction** with model version, `as_of`, probability and market price.
6. **Live uses frozen model versions.** Retraining is offline.
7. **Simulation only, never real orders.** No Kalshi account, no order endpoints, no trading keys, no `LIVE_TRADING` switch. The simulated trader still uses per-trade and total caps, a stale-data check, and no duplicate trades after a restart, so its results are realistic.
8. **Secrets never get committed.** Keys live in `.env`; only `.env.example` is in git. Data files and the database are not committed.
9. **All timestamps in UTC**, stored as ISO text. Convert only for display.

## Database (SQLite, `schema.sql`)

- Reference: `films`, `people`, `film_people` (role: director / writer / cast), `markets` (one row per Kalshi threshold contract).
- Time series: `price_snapshots`, `reviews` (with `published_date`, `first_seen_at`, `rt_counted_at`), `rt_snapshots`, `classifications`.
- Outputs: `predictions`, `trades` (simulated only), `raw_fetches`.
- TV markets are out of scope for v1.

## Tech stack

Python 3.11+, uv (or venv), SQLite, pandas, numpy, scipy, scikit-learn, lightgbm, shap, transformers (Colab GPU for fine-tuning), httpx, selectolax/BeautifulSoup, feedparser, pytest, ruff, Streamlit + plotly. Scheduling with cron or APScheduler; a small Linux server with systemd for the live collector.

Keep it simple: no Docker, Kubernetes, Airflow or Postgres.

## Working conventions

- Every change goes through a pull request reviewed by the other teammate. Reference issues with `Closes #N`.
- **Log every change in `LOG.md`**, newest first, in the same commit as the change: what, why, files, and where (branch, task #, who). Format is at the top of `LOG.md`.
  - Claude does this **automatically, without being asked**, for every change it makes to the repo (code, docs, config, dependencies). Update the log before committing, so the entry lands in the same commit.
  - After updating the log, **tell the user in chat**: one line saying `LOG.md` was updated, then a short bullet list of the important items logged (main features, decisions, new commands or dependencies). Skip minor details.
  - Several small changes for the same task can share one entry; edit that entry instead of adding near-duplicates.
- Run `ruff` and `pytest` before opening a PR. Add tests for the updater, splits, and order/safety logic.
- Be polite to external sites: descriptive User-Agent, delays between requests, retries with backoff. Check Rotten Tomatoes' terms and robots.txt before scraping.

## How to help us

We are also using this project to learn data science and engineering for interviews.

- For the **core pieces** (live updater, `as_of` features and splits, evaluation, trading rule, settlement-score check, classifier evaluation), explain the approach and review our code rather than writing it all for us, unless we ask you to.
- For **plumbing** (API clients, scrapers, SPARQL, scheduler, server setup, dashboard), writing code directly is fine. Keep it readable so we can explain it.
- When you make a design choice, say why in a sentence or two.

## Current week (Week 1: Nov 16 to 22)

| # | Task | Owner | Priority |
|---|---|---|---|
| 1 | Repo setup | Skyler (Chris reviews) | Must |
| 2 | Database schema v1 | Skyler (Chris reviews) | Must, first |
| 3 | Kalshi client (read-only) | Skyler | Must |
| 4 | Kalshi history backfill | Skyler | Can slip |
| 5 | Live collector v1 | Skyler | Must, most urgent after #2 |
| 6 | Server setup | Skyler | Can slip |
| 7 | Kaggle import | Chris | Must |
| 8 | Rotten Tomatoes scraper prototype | Chris | Must |
| 9 | Live Rotten Tomatoes snapshots | Chris | Should |
| 10 | Wikidata pull v1 | Chris | Can slip |

Deadline pressure: the collector must be running before the review embargoes lift for the Nov 30 films (likely Nov 24 to 26). Week 2's go/no-go check: rebuild each film's score at settlement time from review data and compare it to Kalshi's settled result.
