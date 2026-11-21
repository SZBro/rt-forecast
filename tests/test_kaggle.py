import csv

from rt_forecast.collectors.kaggle import (
    import_kaggle_dataset,
    normalize_label,
)
from rt_forecast.db.db import get_connection


def write_movies(path):
    with path.open("w", encoding="utf-8", newline="") as dst:
        writer = csv.DictWriter(
            dst,
            fieldnames=[
                "rotten_tomatoes_link",
                "movie_title",
                "original_release_date",
                "content_rating",
                "runtime",
                "production_company",
                "genres",
                "tomatometer_rating",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "rotten_tomatoes_link": "m/test-film",
                "movie_title": "Test Film",
                "original_release_date": "2020-05-01",
                "content_rating": "PG-13",
                "runtime": "100",
                "production_company": "Test Studios",
                "genres": "Drama, Mystery",
                "tomatometer_rating": "67",
            }
        )


def write_reviews(path):
    with path.open("w", encoding="utf-8", newline="") as dst:
        writer = csv.DictWriter(
            dst,
            fieldnames=[
                "rotten_tomatoes_link",
                "critic_name",
                "top_critic",
                "publisher_name",
                "review_type",
                "review_score",
                "review_date",
                "review_content",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "rotten_tomatoes_link": "m/test-film",
                "critic_name": "Alice",
                "top_critic": "True",
                "publisher_name": "Example News",
                "review_type": "Fresh",
                "review_score": "4/5",
                "review_date": "2020-04-20",
                "review_content": "Good.",
            }
        )
        writer.writerow(
            {
                "rotten_tomatoes_link": "m/test-film",
                "critic_name": "Bob",
                "top_critic": "False",
                "publisher_name": "Example News",
                "review_type": "Fresh",
                "review_score": "B+",
                "review_date": "2020-04-21",
                "review_content": "Also good.",
            }
        )
        writer.writerow(
            {
                "rotten_tomatoes_link": "m/test-film",
                "critic_name": "Alice",
                "top_critic": "False",
                "publisher_name": "Example News",
                "review_type": "Rotten",
                "review_score": "2/5",
                "review_date": "2020-04-22",
                "review_content": "Not good.",
            }
        )


def test_normalize_label():
    assert normalize_label("Fresh") == "fresh"
    assert normalize_label(" unknown ") is None
    assert normalize_label("Rotten") == "rotten"


def test_kaggle_import_maps_fields_and_preserves_duplicate_keys(tmp_path):
    movies_csv = tmp_path / "movies.csv"
    reviews_csv = tmp_path / "reviews.csv"
    db_path = tmp_path / "rt.db"
    raw_dir = tmp_path / "raw"
    write_movies(movies_csv)
    write_reviews(reviews_csv)

    result = import_kaggle_dataset(movies_csv, reviews_csv, db_path, raw_dir)

    assert result["csv_movies"] == 1
    assert result["csv_reviews"] == 3
    assert result["kaggle_reviews"] == 3
    assert result["inserted_genres"] == 2

    with get_connection(db_path) as conn:
        film = conn.execute(
            "SELECT title, release_year, wide_release_date, kaggle_id FROM films"
        ).fetchone()
        assert film["title"] == "Test Film"
        assert film["release_year"] == 2020
        assert film["wide_release_date"] == "2020-05-01"
        assert film["kaggle_id"] == "m/test-film"

        reviews = conn.execute(
            "SELECT rt_label, published_date, source, critic_name "
            "FROM reviews ORDER BY review_id"
        ).fetchall()
        assert [review["rt_label"] for review in reviews] == ["fresh", "fresh", "rotten"]
        assert all(review["source"] == "kaggle" for review in reviews)
        assert all(review["published_date"] for review in reviews)
        assert len({review["critic_name"] for review in reviews}) == 3

        fresh_count = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE rt_label = 'fresh'"
        ).fetchone()[0]
        fresh_percentage = round(100 * fresh_count / len(reviews))
        assert fresh_percentage == 67
