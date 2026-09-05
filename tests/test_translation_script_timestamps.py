import json
import sqlite3
from datetime import datetime, timedelta

from scripts import translate_italian_posts_to_english
from scripts import translate_threads_to_english


def _assert_aware_utc_timestamp(value: str) -> None:
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_italian_post_translation_script_writes_aware_utc_timestamp(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "italian-posts.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE content_sources (id INTEGER PRIMARY KEY, text TEXT)")
        conn.execute(
            "CREATE TABLE post_drafts ("
            "id INTEGER PRIMARY KEY, text TEXT, category TEXT, "
            "source_ids_json TEXT, updated_at TEXT)"
        )
        conn.execute("INSERT INTO content_sources VALUES (34, 'Italian source')")
        conn.execute(
            "INSERT INTO post_drafts VALUES "
            "(19, 'Italian draft', 'legacy', '[34]', '2020-01-01T00:00:00+00:00')"
        )
    monkeypatch.setattr(translate_italian_posts_to_english, "DB", str(path))
    monkeypatch.setattr(
        translate_italian_posts_to_english,
        "TRANSLATIONS",
        {34: ("gym_strategy", "English translated post.")},
    )

    translate_italian_posts_to_english.main()

    with sqlite3.connect(path) as conn:
        source = conn.execute(
            "SELECT text FROM content_sources WHERE id = 34"
        ).fetchone()[0]
        draft = conn.execute(
            "SELECT text, category, updated_at FROM post_drafts WHERE id = 19"
        ).fetchone()
    assert source == "English translated post."
    assert draft[:2] == ("English translated post.", "gym_strategy")
    _assert_aware_utc_timestamp(draft[2])


def test_thread_translation_script_writes_aware_utc_timestamp(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "threads.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE post_drafts ("
            "id INTEGER PRIMARY KEY, text TEXT, category TEXT, "
            "thread_tweets_json TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO post_drafts VALUES "
            "(16, 'Italian thread', 'legacy', NULL, '2020-01-01T00:00:00+00:00')"
        )
    tweets = ["English opener.", "English continuation."]
    monkeypatch.setattr(translate_threads_to_english, "DB", str(path))
    monkeypatch.setattr(
        translate_threads_to_english,
        "THREADS",
        [{"draft_id": 16, "category": "founder_journey", "tweets": tweets}],
    )

    translate_threads_to_english.main()

    with sqlite3.connect(path) as conn:
        draft = conn.execute(
            "SELECT text, category, thread_tweets_json, updated_at "
            "FROM post_drafts WHERE id = 16"
        ).fetchone()
    assert draft[:3] == (
        "English opener.",
        "founder_journey",
        json.dumps(tweets, ensure_ascii=False, separators=(",", ":")),
    )
    _assert_aware_utc_timestamp(draft[3])
