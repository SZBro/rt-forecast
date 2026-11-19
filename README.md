# rt-forecast

Predicting final Rotten Tomatoes Tomatometer scores and comparing them to Kalshi prediction-market prices.

> **Status:** Week 1. Repo set up; data collectors in progress.
> This is a 5-week class "Moonshot" project (Nov 16 to Dec 17, 2026) by a two-person team.

---

## What it does

Kalshi lists markets on a film's Rotten Tomatoes score, settled the Monday after wide release at 10:00 AM ET (for example, "Will *Film X* score above 60%?"). This project builds a pipeline that:

1. Predicts a film's score **before reviews exist**, from data about the film and the people who made it.
2. **Updates that prediction live** as critic reviews come in.
3. Labels new reviews Fresh or Rotten **before Rotten Tomatoes counts them**.
4. Compares the predicted odds to Kalshi's prices and **paper trades** when the gap is large enough.

The main goal is to show, with honest backtests, whether the model's probabilities beat the market's own odds.

## How it works

```
Data sources ──► Collectors + database ──► Models ──► Edge vs. Kalshi price ──► Paper trader
(Kalshi, Wikidata,  (timestamped snapshots)  (prior,                              Backtest + dashboard
 reviews, RSS)                                classifier,
                                              live updater)
```

| Component | What it does |
|---|---|
| **Prior model** | LightGBM on film features (director/writer track record, genre, distributor, release timing, festival premiere, embargo timing). Predicts the chance a review is Fresh and how many reviews to expect. |
| **Review classifier** | Labels new reviews Fresh or Rotten. Baseline: TF-IDF + logistic regression. Main model: fine-tuned DistilBERT. An LLM is used as a benchmark. |
| **Live updater** | Beta-Binomial update of the prior with the reviews so far, plus Monte Carlo simulation of the remaining reviews. Outputs P(score above X) for each Kalshi threshold. |
| **Calibration** | Isotonic regression so a predicted 70% happens about 70% of the time. |
| **Edge + paper trader** | Compares calibrated odds to the order book after fees, sizes with fractional Kelly, and logs simulated trades at real prices. |

## Design rules

- **No look-ahead:** every feature and model function takes an `as_of` time and only uses data from before it.
- **Same code for backtest and live:** the backtest replays history through the exact functions the live system runs.
- **Raw first:** every fetched response is saved before parsing, so data can be re-processed without fetching again.
- **Log everything:** every prediction is stored with its model version, time, probability and market price.
- **Frozen models live:** retraining happens offline; the live system loads a fixed model version.
- **Paper trading by default:** real orders are off unless explicitly enabled, with hard dollar caps.

## Data sources

| Source | Used for |
|---|---|
| Kalshi API (public market data) | Markets, thresholds, settlement times, price history, order books |
| Wikidata (SPARQL, CC0) | Film metadata and links between people and films |
| Kaggle Rotten Tomatoes dataset | Historical critic reviews with labels (up to about 2020) |
| Rotten Tomatoes pages | Recent reviews and live scores |
| Critic outlet RSS feeds | Full review text, often before Rotten Tomatoes adds it |

Data files are not committed to this repo. The collectors rebuild them.

## Tech stack

Python 3.11+ · SQLite · pandas · numpy/scipy · scikit-learn · LightGBM · SHAP · Hugging Face Transformers · httpx · feedparser · Streamlit · pytest · ruff

See the project's design doc for the full table of tools by task.

## Getting started

You need Git, Python 3.11+ and [uv](https://docs.astral.sh/uv/) (`pip install uv` works).

```bash
git clone https://github.com/SZBro/rt-forecast.git
cd rt-forecast
uv sync                 # creates .venv and installs the exact versions in uv.lock
cp .env.example .env    # then fill in your own values (never commit .env)
```

Check that everything works:

```bash
uv run pytest           # tests
uv run ruff check .     # lint
uv run ruff format .    # auto-format
```

Adding a package: `uv add <name>` (or `uv add --dev <name>` for dev tools), then commit both `pyproject.toml` and `uv.lock`. After pulling someone else's changes, run `uv sync` again.

Code lives in `src/rt_forecast/` (`collectors`, `db`, `features`, `models`); tests in `tests/`. Data is written to `data/` (not committed).

## Roadmap

| Week | Dates | Focus |
|---|---|---|
| 1 | Nov 16 to Nov 22 | Live collector running; Kalshi history; start review scraping |
| 2 | Nov 23 to Nov 29 | **Go/no-go:** rebuild settlement scores and check them against Kalshi results; title matching; baselines |
| 3 | Nov 30 to Dec 6 | Live updater + backtest; live paper trading on the Nov 30 markets |
| 4 | Dec 7 to Dec 13 | Review classifier; LightGBM prior; calibration |
| 5 | Dec 14 to Dec 17 | Paper trader with fees; dashboard; final evaluation and presentation |

## Team

| Name | Focus |
|---|---|
| Skyler | Infra and Kalshi data: repo, database, Kalshi client, live collector, server |
| Chris | Review data: Kaggle import, Rotten Tomatoes scraper, live RT snapshots, Wikidata |

## Disclaimer

This is a student project for learning. It is not affiliated with or endorsed by Rotten Tomatoes or Kalshi, and nothing here is financial advice. Trading is simulated by default.
