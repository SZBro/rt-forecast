-- rt-forecast database schema, v1.
--
-- Conventions (see CLAUDE.md):
--   * STRICT tables: SQLite rejects values of the wrong type.
--   * Timestamps are TEXT in exactly one UTC format: '2026-11-24T15:00:00Z'.
--     Write them with rt_forecast.db.db.to_utc_iso(); the CHECKs below reject
--     anything else. One format means text order = time order, and UNIQUE keys
--     that include a timestamp really catch duplicates.
--   * Dates (no time of day) are TEXT 'YYYY-MM-DD'.
--   * Naming: *_at = timestamp, *_date = date, ts = time-series timestamp.
--   * Time-series tables only get new rows (rule 3). Raw responses are saved
--     to disk before parsing and recorded in raw_fetches.
--   * Prices and probabilities are REAL on a 0-1 scale (dollars per contract).
--   * IF NOT EXISTS everywhere, so init_db() can run more than once.
--
-- A CHECK on a NULL value passes, so nullable timestamp columns still get the
-- format check without needing "x IS NULL OR ...".


-- ---------------------------------------------------------------------------
-- Raw responses
-- ---------------------------------------------------------------------------

-- One row per HTTP response (or imported file) saved to disk before parsing.
-- The gzipped body lives at data/raw/<file_path>; files are kept and synced
-- off the server, so a parser bug can be fixed by re-parsing instead of
-- re-fetching.
CREATE TABLE IF NOT EXISTS raw_fetches (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL
                CHECK (source IN ('kalshi', 'rt', 'rss', 'wikidata', 'kaggle')),
    url         TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
                CHECK (fetched_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    status_code INTEGER,
    file_path   TEXT NOT NULL,      -- relative to data/raw/
    sha256      TEXT NOT NULL,      -- hash of the gzipped file, to detect changes
    byte_size   INTEGER NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_raw_fetches_source_time
    ON raw_fetches (source, fetched_at);


-- ---------------------------------------------------------------------------
-- Reference tables
-- ---------------------------------------------------------------------------

-- Our own film_id links the same film across Kaggle, RT, Wikidata and Kalshi.
-- Outside IDs are UNIQUE but nullable: a film may only exist in some sources.
CREATE TABLE IF NOT EXISTS films (
    film_id           INTEGER PRIMARY KEY,
    title             TEXT NOT NULL,
    release_year      INTEGER,
    wide_release_date TEXT
                      CHECK (wide_release_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    mpaa_rating       TEXT,
    runtime_min       INTEGER,
    distributor       TEXT,
    wikidata_qid      TEXT UNIQUE,
    rt_slug           TEXT UNIQUE,
    kaggle_id         TEXT UNIQUE,
    created_at        TEXT NOT NULL
                      CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
) STRICT;

-- A film has several genres, so they get their own table.
CREATE TABLE IF NOT EXISTS film_genres (
    film_id INTEGER NOT NULL REFERENCES films (film_id),
    genre   TEXT NOT NULL,
    PRIMARY KEY (film_id, genre)
) STRICT;

-- Names are not unique ("Chris Evans" x2), so wikidata_qid links people.
CREATE TABLE IF NOT EXISTS people (
    person_id    INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    wikidata_qid TEXT UNIQUE,
    created_at   TEXT NOT NULL
                 CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
) STRICT;

-- Role is part of the key: one person can direct and write the same film.
-- People features (e.g. director average) must exclude the film being
-- predicted and any film released after as_of (rule 1).
CREATE TABLE IF NOT EXISTS film_people (
    film_id       INTEGER NOT NULL REFERENCES films (film_id),
    person_id     INTEGER NOT NULL REFERENCES people (person_id),
    role          TEXT NOT NULL CHECK (role IN ('director', 'writer', 'cast')),
    billing_order INTEGER,          -- 1 = top-billed; NULL for director/writer
    PRIMARY KEY (film_id, person_id, role)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_film_people_person
    ON film_people (person_id, role);

-- One row per Kalshi contract (one threshold of one film's event).
-- status/result/settled_at are updated in place as the market moves through
-- open -> closed -> settled; the full API responses stay in raw_fetches.
-- Only evaluation code may read result (it is the answer).
CREATE TABLE IF NOT EXISTS markets (
    ticker        TEXT PRIMARY KEY,
    event_ticker  TEXT NOT NULL,
    series_ticker TEXT NOT NULL,
    film_id       INTEGER REFERENCES films (film_id),   -- NULL until matched
    kalshi_title  TEXT,
    threshold     REAL,
    strike_type   TEXT,             -- e.g. 'greater' vs 'greater_or_equal'
    open_at       TEXT
                  CHECK (open_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    close_at      TEXT
                  CHECK (close_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    settles_at    TEXT
                  CHECK (settles_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    status        TEXT,
    result        TEXT CHECK (result IN ('yes', 'no')),
    settled_at    TEXT
                  CHECK (settled_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    first_seen_at TEXT NOT NULL
                  CHECK (first_seen_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    updated_at    TEXT NOT NULL
                  CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
) STRICT;

CREATE INDEX IF NOT EXISTS idx_markets_film
    ON markets (film_id);


-- ---------------------------------------------------------------------------
-- Time series (insert only)
-- ---------------------------------------------------------------------------

-- ts = when the price was true (for candlesticks: the end of the candle).
-- The UNIQUE key makes backfills safe to re-run and doubles as the
-- (ticker, ts) index.
--
-- Live rows take bid/ask and their sizes from the order book (depth at the
-- best price: how many contracts a simulated trade could fill). Candlesticks
-- have no book, so their sizes are NULL. Depth can't be backfilled later; the
-- full book (every level) stays in the raw file behind raw_fetch_id.
-- Kalshi allows fractional contracts, so sizes and volume are REAL.
CREATE TABLE IF NOT EXISTS price_snapshots (
    id            INTEGER PRIMARY KEY,
    ticker        TEXT NOT NULL REFERENCES markets (ticker),
    ts            TEXT NOT NULL
                  CHECK (ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    yes_bid       REAL CHECK (yes_bid BETWEEN 0 AND 1),
    yes_ask       REAL CHECK (yes_ask BETWEEN 0 AND 1),
    yes_bid_size  REAL CHECK (yes_bid_size >= 0),   -- contracts resting at yes_bid
    yes_ask_size  REAL CHECK (yes_ask_size >= 0),   -- contracts resting at yes_ask
    last_price    REAL CHECK (last_price BETWEEN 0 AND 1),
    volume        REAL,
    open_interest REAL,
    source        TEXT NOT NULL CHECK (source IN ('live', 'candlestick', 'historical')),
    raw_fetch_id  INTEGER REFERENCES raw_fetches (id),
    UNIQUE (ticker, ts, source)
) STRICT;

-- Three timestamps, because "when" depends on who is asking:
--   published_date: when the outlet published the review (a date only).
--   first_seen_at:  when OUR system first saw it. The honest filter for live
--                   and for backtests over periods we were collecting.
--   rt_counted_at:  when RT first counted it (first rt_snapshot showing it).
-- Imported historical rows (source = 'kaggle') get first_seen_at = import
-- time, so backtests on those films must filter on published_date or
-- rt_counted_at instead, or every Kaggle review would be dropped.
--
-- Unique key: RT counts one review per critic per film. The importer must
-- normalise names and give nameless rows a stand-in critic_name (e.g. from
-- the URL or Kaggle row id), or they would collide.
CREATE TABLE IF NOT EXISTS reviews (
    review_id      INTEGER PRIMARY KEY,
    film_id        INTEGER NOT NULL REFERENCES films (film_id),
    critic_name    TEXT NOT NULL,
    outlet         TEXT NOT NULL DEFAULT '',
    is_top_critic  INTEGER NOT NULL DEFAULT 0 CHECK (is_top_critic IN (0, 1)),
    rt_label       TEXT CHECK (rt_label IN ('fresh', 'rotten')),  -- NULL = not on RT yet
    original_score TEXT,            -- as written: '3/4', 'B+', ...
    quote          TEXT,
    url            TEXT,
    source         TEXT NOT NULL CHECK (source IN ('kaggle', 'rt_scrape', 'rss')),
    published_date TEXT
                   CHECK (published_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    first_seen_at  TEXT NOT NULL
                   CHECK (first_seen_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    rt_counted_at  TEXT
                   CHECK (rt_counted_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    UNIQUE (film_id, critic_name, outlet)
) STRICT;

-- Serves "all reviews for film X seen before as_of".
CREATE INDEX IF NOT EXISTS idx_reviews_film_seen
    ON reviews (film_id, first_seen_at);

-- One look at a film's RT page. Used to rebuild the score at settlement time
-- (Week 2 go/no-go check). The UNIQUE key doubles as the (film_id, ts) index.
CREATE TABLE IF NOT EXISTS rt_snapshots (
    id               INTEGER PRIMARY KEY,
    film_id          INTEGER NOT NULL REFERENCES films (film_id),
    ts               TEXT NOT NULL
                     CHECK (ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    tomatometer      INTEGER CHECK (tomatometer BETWEEN 0 AND 100),  -- NULL = no score shown yet
    review_count     INTEGER,
    fresh_count      INTEGER,
    rotten_count     INTEGER,
    top_critic_score INTEGER CHECK (top_critic_score BETWEEN 0 AND 100),
    raw_fetch_id     INTEGER REFERENCES raw_fetches (id),
    UNIQUE (film_id, ts)
) STRICT;


-- ---------------------------------------------------------------------------
-- Outputs (stubs for later weeks)
-- ---------------------------------------------------------------------------

-- Our classifier's output. Different model versions sit side by side.
CREATE TABLE IF NOT EXISTS classifications (
    id            INTEGER PRIMARY KEY,
    review_id     INTEGER NOT NULL REFERENCES reviews (review_id),
    model_version TEXT NOT NULL,
    prob_fresh    REAL NOT NULL CHECK (prob_fresh BETWEEN 0 AND 1),
    classified_at TEXT NOT NULL
                  CHECK (classified_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    UNIQUE (review_id, model_version)
) STRICT;

-- Every prediction, live or backtest (rule 5). run_id groups one run, so
-- re-running a backtest adds a new run instead of overwriting the old one.
-- created_at is when the row was written; for backtests it is long after as_of.
CREATE TABLE IF NOT EXISTS predictions (
    id                INTEGER PRIMARY KEY,
    run_id            TEXT NOT NULL,
    run_mode          TEXT NOT NULL CHECK (run_mode IN ('backtest', 'live')),
    ticker            TEXT NOT NULL REFERENCES markets (ticker),
    as_of             TEXT NOT NULL
                      CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    model_version     TEXT NOT NULL,
    prob_raw          REAL NOT NULL CHECK (prob_raw BETWEEN 0 AND 1),
    prob_calibrated   REAL CHECK (prob_calibrated BETWEEN 0 AND 1),
    market_yes_bid    REAL CHECK (market_yes_bid BETWEEN 0 AND 1),
    market_yes_ask    REAL CHECK (market_yes_ask BETWEEN 0 AND 1),
    price_snapshot_id INTEGER REFERENCES price_snapshots (id),
    created_at        TEXT NOT NULL
                      CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    UNIQUE (run_id, ticker, as_of)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_predictions_ticker_asof
    ON predictions (ticker, as_of);

-- Simulated trades only (rule 7): nothing here is ever sent to Kalshi. Each
-- row is a trade we would have made, priced from public market data so the
-- results are realistic:
--   * fill_price: buy at the ask, sell at the bid (never the midpoint).
--   * fee: Kalshi's published fee formula, charged as if the trade were real.
--   * contracts <= book_size: never fill more than the order book showed at
--     that price when we decided.
-- trade_key is built by our code from the decision (e.g. prediction id +
-- side + action), not randomly, so a restart that repeats a decision hits
-- the UNIQUE key instead of recording a duplicate trade.
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY,
    prediction_id   INTEGER NOT NULL REFERENCES predictions (id),
    ticker          TEXT NOT NULL REFERENCES markets (ticker),
    side            TEXT NOT NULL CHECK (side IN ('yes', 'no')),
    action          TEXT NOT NULL CHECK (action IN ('buy', 'sell')),
    contracts       INTEGER NOT NULL CHECK (contracts > 0),
    book_size       INTEGER NOT NULL,   -- contracts available at that price
    limit_price     REAL NOT NULL CHECK (limit_price BETWEEN 0 AND 1),
    fill_price      REAL CHECK (fill_price BETWEEN 0 AND 1),
    fee             REAL CHECK (fee >= 0),
    trade_key       TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL
                    CHECK (status IN ('pending', 'filled', 'partial', 'canceled', 'rejected')),
    placed_at       TEXT NOT NULL
                    CHECK (placed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    filled_at       TEXT
                    CHECK (filled_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    CHECK (contracts <= book_size)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_trades_ticker_placed
    ON trades (ticker, placed_at);
