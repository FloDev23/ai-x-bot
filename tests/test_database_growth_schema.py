import sqlite3
from datetime import datetime, timedelta
from datetime import timezone

from modules.database import Database


def test_schema_migration_preserves_existing_posts(tmp_path):
    path = tmp_path / "bot.db"
    db = Database(str(path))
    db.log_posted_tweet("existing", "legacy", tweet_id="123")
    migrated = Database(str(path))
    assert migrated.get_recent_posts(1)[0]["tweet_id"] == "123"


def test_expired_product_fact_is_not_eligible(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    source_id = db.add_content_source(
        source_type="product_fact",
        text="Partner fee is 15%.",
        verified_by="floriano",
        verified_at=(datetime.utcnow() - timedelta(days=91)).isoformat(),
    )
    assert source_id > 0
    assert db.get_eligible_sources(source_type="product_fact") == []


def test_draft_transition_is_compare_and_swap(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    source_id = db.add_content_source("evergreen_idea", "Reduce class no-shows.")
    draft_id = db.create_post_draft(
        text="A useful draft",
        category="gym_strategy",
        source_ids=[source_id],
        score_data={"total": 82},
        intended_slot="2026-08-11T14:00:00+02:00",
        publication_key="draft-20260811-1400",
    )
    assert db.transition_post_draft(draft_id, ["pending_approval"], "approved") is True
    assert db.transition_post_draft(draft_id, ["pending_approval"], "approved") is False


def test_telegram_update_can_be_claimed_once(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    assert db.claim_telegram_update(901, "42") is True
    assert db.claim_telegram_update(901, "42") is False


def test_legacy_ideas_and_media_are_migrated_once(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idea TEXT NOT NULL,
            categoria TEXT,
            priorita INTEGER DEFAULT 5,
            data_ultima_pubblicazione TEXT,
            stato TEXT DEFAULT 'nuova',
            performance REAL
        );
        INSERT INTO ideas (idea, stato) VALUES ('Keep this idea', 'nuova');
        INSERT INTO ideas (idea, stato) VALUES ('Already used', 'usata');
        CREATE TABLE media_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL,
            media_type TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            file_deleted INTEGER DEFAULT 0
        );
        INSERT INTO media_library (filename, filepath, media_type)
        VALUES ('available.jpg', '/a', 'image');
        INSERT INTO media_library (filename, filepath, media_type, used)
        VALUES ('used.jpg', '/b', 'image', 1);
        INSERT INTO media_library (
            filename, filepath, media_type, used, file_deleted
        ) VALUES ('deleted.jpg', '/c', 'image', 1, 1);
    """)
    conn.close()

    Database(str(path))
    migrated = Database(str(path))
    with migrated._conn() as check:
        sources = check.execute(
            "SELECT text FROM content_sources ORDER BY id"
        ).fetchall()
        states = check.execute(
            "SELECT lifecycle_state FROM media_library ORDER BY id"
        ).fetchall()
    assert [row["text"] for row in sources] == ["Keep this idea"]
    assert [row["lifecycle_state"] for row in states] == [
        "available", "used", "deleted",
    ]


def test_published_and_unknown_drafts_keep_their_slot_claim(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    source_id = db.add_content_source("evergreen_idea", "Source")
    for index, status in enumerate(("published", "publication_unknown"), start=1):
        slot = f"2026-08-11T{index + 13}:00:00+02:00"
        draft_id = db.create_post_draft(
            "Draft", "gym_strategy", [source_id], {"total": 90}, slot,
            f"key-{index}",
        )
        assert db.transition_post_draft(
            draft_id, ["pending_approval"], status,
        )
        assert db.get_active_draft_for_slot(slot)["id"] == draft_id


def test_digest_excludes_low_score_and_expired_offset_candidates(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    base = {
        "username": "owner",
        "profile": {"username": "owner"},
        "latest_post": {},
        "score_data": {},
        "discovery_source": "search",
    }
    db.upsert_growth_candidate({
        **base,
        "user_id": "low",
        "score": 10,
    })
    expired_with_offset = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    ).astimezone(timezone(timedelta(hours=14))).isoformat()
    db.upsert_growth_candidate({
        **base,
        "user_id": "expired",
        "score": 90,
        "profile_expires_at": expired_with_offset,
    })
    assert db.get_digest_candidates() == []


def test_legacy_media_queries_and_use_transition_respect_lifecycle(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    reserved_id = db.add_media("reserved.jpg", "/a", "image")
    archived_id = db.add_media("archived.jpg", "/b", "image")
    deleted_id = db.add_media("deleted.jpg", "/c", "image")
    assert db.reserve_media(reserved_id, 1)
    assert db.archive_media(archived_id)
    db.mark_media_file_deleted(deleted_id)

    assert db.get_unused_media() == []
    assert db.get_unused_media_pool() == []
    db.mark_media_used(deleted_id, "tweet")
    assert db.get_media_by_id(deleted_id)["lifecycle_state"] == "deleted"


def test_content_mix_excludes_audit_only_drafts(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    source_id = db.add_content_source("evergreen_idea", "Source")
    statuses = ("pending_approval", "published", "superseded", "expired")
    for index, status in enumerate(statuses):
        draft_id = db.create_post_draft(
            f"Draft {index}", "gym_strategy", [source_id], {"total": 90},
            f"2026-08-11T{10 + index}:00:00+02:00", f"mix-{index}",
        )
        if status != "pending_approval":
            assert db.transition_post_draft(
                draft_id, ["pending_approval"], status,
            )
    assert db.get_content_mix_counts() == {"gym_strategy": 2}
