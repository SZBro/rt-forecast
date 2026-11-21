"""Import the Kaggle Rotten Tomatoes movies and critic reviews dataset."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from rt_forecast.db.db import (
    default_db_path,
    default_raw_dir,
    get_connection,
    init_db,
    load_settings,
    to_utc_iso,
)

DATASET_URL = (
    "https://www.kaggle.com/datasets/"
    "stefanoleone992/rotten-tomatoes-movies-and-critic-reviews-dataset"
)
DOWNLOAD_URL = (
    "https://www.kaggle.com/api/v1/datasets/download/"
    "stefanoleone992/rotten-tomatoes-movies-and-critic-reviews-dataset"
)


def normalize_date(value: str | None) -> str | None:
    """Convert a Kaggle review date to the schema's YYYY-MM-DD date format."""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y", "%Y/%m/%d", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def normalize_label(value: str | None) -> str | None:
    """Map Kaggle review labels to the schema's fresh/rotten values."""
    if not value:
        return None
    label = value.strip().lower()
    if label == "fresh":
        return "fresh"
    if label == "rotten":
        return "rotten"
    return None


def _gzip_csv(source: Path, raw_dir: Path) -> tuple[str, str, int]:
    """Gzip a source CSV and return relative path, SHA-256 and compressed bytes."""
    relative = f"kaggle/{source.name}.gz"
    destination = raw_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, gzip.open(destination, "wb", compresslevel=9) as dst:
        dst.write(src.read())
    compressed = destination.read_bytes()
    return relative, hashlib.sha256(compressed).hexdigest(), len(compressed)


def _record_raw(
    conn,
    url: str,
    fetched_at: str,
    relative: str,
    sha256: str,
    byte_size: int,
) -> None:
    existing = conn.execute(
        "SELECT 1 FROM raw_fetches WHERE sha256 = ? AND file_path = ?",
        (sha256, relative),
    ).fetchone()
    if existing is None:
        conn.execute(
            """INSERT INTO raw_fetches
                   (source, url, fetched_at, status_code, file_path, sha256, byte_size)
               VALUES ('kaggle', ?, ?, 200, ?, ?, ?)""",
            (url, fetched_at, relative, sha256, byte_size),
        )


def _release_year(value: str | None) -> int | None:
    date = normalize_date(value)
    return int(date[:4]) if date else None


def _csv_row_count(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as src:
        return sum(1 for _ in csv.DictReader(src))


def import_kaggle_dataset(
    movies_csv: Path,
    reviews_csv: Path,
    db_path: str | Path,
    raw_dir: str | Path | None = None,
) -> dict[str, int]:
    """Load films, genres and reviews without overwriting existing rows."""
    init_db(db_path)
    raw_root = Path(raw_dir) if raw_dir is not None else default_raw_dir()
    now = to_utc_iso(datetime.now(UTC))
    movie_count = _csv_row_count(movies_csv)
    review_count = _csv_row_count(reviews_csv)
    inserted_movies = 0
    inserted_reviews = 0
    inserted_genres = 0
    inserted_stub_movies = 0

    with closing(get_connection(db_path)) as conn:
        for csv_path in (movies_csv, reviews_csv):
            relative, digest, byte_size = _gzip_csv(csv_path, raw_root)
            _record_raw(conn, DOWNLOAD_URL, now, relative, digest, byte_size)
        conn.commit()

        film_ids: dict[str, int] = {}
        with movies_csv.open("r", encoding="utf-8-sig", newline="") as src:
            for row in csv.DictReader(src):
                slug = (row.get("rotten_tomatoes_link") or "").strip()
                title = (row.get("movie_title") or "").strip()
                if not slug or not title:
                    continue
                release_date = normalize_date(row.get("original_release_date"))
                try:
                    runtime = int(row["runtime"]) if row.get("runtime") else None
                except (TypeError, ValueError):
                    runtime = None
                cur = conn.execute(
                    """INSERT INTO films
                           (title, release_year, wide_release_date, mpaa_rating, runtime_min,
                            distributor, rt_slug, kaggle_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(kaggle_id) DO NOTHING""",
                    (
                        title,
                        _release_year(row.get("original_release_date")),
                        release_date,
                        (row.get("content_rating") or "").strip() or None,
                        runtime,
                        (row.get("production_company") or "").strip() or None,
                        slug,
                        slug,
                        now,
                    ),
                )
                inserted_movies += cur.rowcount
                film_id_row = conn.execute(
                    "SELECT film_id FROM films WHERE kaggle_id = ?", (slug,)
                ).fetchone()
                if film_id_row is None:
                    continue
                film_id = film_id_row["film_id"]
                film_ids[slug] = film_id

                genres = (row.get("genres") or "").split(",")
                for genre in (g.strip() for g in genres):
                    if genre:
                        cur = conn.execute(
                            "INSERT INTO film_genres (film_id, genre) VALUES (?, ?) "
                            "ON CONFLICT DO NOTHING",
                            (film_id, genre),
                        )
                        inserted_genres += cur.rowcount

        used_review_keys: Counter[tuple[int, str, str]] = Counter()
        with reviews_csv.open("r", encoding="utf-8-sig", newline="") as src:
            for row_number, row in enumerate(csv.DictReader(src), start=2):
                slug = (row.get("rotten_tomatoes_link") or "").strip()
                film_id = film_ids.get(slug)
                if film_id is None:
                    cur = conn.execute(
                        """INSERT INTO films
                               (title, rt_slug, kaggle_id, created_at)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(kaggle_id) DO NOTHING""",
                        (
                            f"Unknown Kaggle title ({slug})",
                            slug,
                            slug,
                            now,
                        ),
                    )
                    inserted_stub_movies += cur.rowcount
                    film_id_row = conn.execute(
                        "SELECT film_id FROM films WHERE kaggle_id = ?", (slug,)
                    ).fetchone()
                    if film_id_row is None:
                        continue
                    film_id = film_id_row["film_id"]
                    film_ids[slug] = film_id

                critic = (row.get("critic_name") or "").strip()
                outlet = (row.get("publisher_name") or "").strip()
                if not critic:
                    critic = f"Unknown critic (Kaggle row {row_number})"

                key = (film_id, critic.lower(), outlet.lower())
                used_review_keys[key] += 1
                if used_review_keys[key] > 1:
                    critic = f"{critic} [Kaggle row {row_number}]"

                try:
                    top_critic = 1 if (row.get("top_critic") or "").strip().lower() == "true" else 0
                except AttributeError:
                    top_critic = 0

                cur = conn.execute(
                    """INSERT INTO reviews
                           (film_id, critic_name, outlet, is_top_critic, rt_label,
                            original_score, quote, url, source, published_date,
                            first_seen_at, rt_counted_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'kaggle', ?, ?, NULL)
                       ON CONFLICT(film_id, critic_name, outlet) DO NOTHING""",
                    (
                        film_id,
                        critic,
                        outlet,
                        top_critic,
                        normalize_label(row.get("review_type")),
                        (row.get("review_score") or "").strip() or None,
                        (row.get("review_content") or "").strip() or None,
                        normalize_date(row.get("review_date")),
                        now,
                    ),
                )
                inserted_reviews += cur.rowcount

        conn.commit()
        db_movies = conn.execute("SELECT COUNT(*) FROM films").fetchone()[0]
        db_reviews = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        kaggle_movies = conn.execute(
            "SELECT COUNT(*) FROM films WHERE kaggle_id IS NOT NULL"
        ).fetchone()[0]
        kaggle_reviews = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE source = 'kaggle'"
        ).fetchone()[0]

    return {
        "csv_movies": movie_count,
        "csv_reviews": review_count,
        "inserted_movies": inserted_movies,
        "inserted_genres": inserted_genres,
        "inserted_reviews": inserted_reviews,
        "inserted_stub_movies": inserted_stub_movies,
        "db_movies": db_movies,
        "db_reviews": db_reviews,
        "kaggle_movies": kaggle_movies,
        "kaggle_reviews": kaggle_reviews,
    }


def main(argv: list[str] | None = None) -> None:
    load_settings()
    parser = argparse.ArgumentParser(description="Import Kaggle Rotten Tomatoes CSV files.")
    parser.add_argument(
        "--movies",
        type=Path,
        default=Path("data/kaggle-rotten-tomatoes/rotten_tomatoes_movies.csv"),
    )
    parser.add_argument(
        "--reviews",
        type=Path,
        default=Path("data/kaggle-rotten-tomatoes/rotten_tomatoes_critic_reviews.csv"),
    )
    parser.add_argument("--db", type=Path, help="database file (default: $RT_DB_PATH)")
    parser.add_argument("--raw-dir", type=Path, help="raw gzipped file folder")
    args = parser.parse_args(argv)

    result = import_kaggle_dataset(
        args.movies,
        args.reviews,
        args.db or default_db_path(),
        args.raw_dir or default_raw_dir(),
    )
    print(
        "Kaggle import complete: "
        f"{result['kaggle_movies']} films and {result['kaggle_reviews']} Kaggle reviews in DB "
        f"(CSV: {result['csv_movies']} films, {result['csv_reviews']} reviews)."
    )


if __name__ == "__main__":
    main()
