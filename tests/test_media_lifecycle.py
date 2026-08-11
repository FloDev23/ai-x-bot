import threading

import pytest

from config import (
    MEDIA_MATCH_THRESHOLD,
    TELEGRAM_MAX_IMAGE_BYTES,
    TELEGRAM_MAX_VIDEO_BYTES,
)
from modules.ai_generator import AIGenerator
from modules.database import Database
from modules.media_matcher import MediaMatcher
from modules.media_processor import MediaProcessor, validate_media_upload


JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"jpeg-data"


class ChoiceGenerator:
    def __init__(self, choice):
        self.choice = choice

    def select_best_media(self, category, text, candidates):
        return self.choice


def _draft(db, source_id, slot, key):
    return db.create_post_draft(
        text="Existing concept about a studio floor",
        category="gym_strategy",
        source_ids=[source_id],
        score_data={"total": 86},
        intended_slot=slot,
        publication_key=key,
    )


@pytest.mark.parametrize(
    ("filename", "mime_type", "file_size", "expected_reason"),
    [
        ("photo.jpg", "video/mp4", 1024, "mime_extension_mismatch"),
        ("../../photo.jpg", "image/jpeg", 1024, "invalid_filename"),
        (r"..\photo.jpg", "image/jpeg", 1024, "invalid_filename"),
        ("photo.gif", "image/gif", 1024, "unsupported_extension"),
        ("photo.jpg", "image/jpeg", TELEGRAM_MAX_IMAGE_BYTES + 1, "file_too_large"),
        ("clip.mp4", "video/mp4", TELEGRAM_MAX_VIDEO_BYTES + 1, "file_too_large"),
    ],
)
def test_media_validation_rejects_unsafe_uploads(
    filename, mime_type, file_size, expected_reason,
):
    valid, reason = validate_media_upload(filename, mime_type, file_size)
    assert valid is False
    assert reason == expected_reason


@pytest.mark.parametrize(
    ("filename", "mime_type", "file_size"),
    [
        ("photo.jpeg", "image/jpeg", TELEGRAM_MAX_IMAGE_BYTES),
        ("graphic.png", "image/png", 2048),
        ("graphic.webp", "image/webp", 2048),
        ("clip.mp4", "video/mp4", TELEGRAM_MAX_VIDEO_BYTES),
        ("clip.mov", "video/quicktime", 2048),
        ("clip.m4v", "video/x-m4v", 2048),
    ],
)
def test_media_validation_accepts_supported_pairs_at_the_limit(
    filename, mime_type, file_size,
):
    assert validate_media_upload(filename, mime_type, file_size) == (True, "ok")


def test_upload_only_adds_available_media_and_audit_source(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    image_path = tmp_path / "gym.jpg"
    image_path.write_bytes(JPEG_BYTES)
    processor = MediaProcessor(db)

    record = processor.process_new_file(
        str(image_path), "gym.jpg", "image/jpeg", len(JPEG_BYTES),
        "Real studio floor",
    )

    assert record["lifecycle_state"] == "available"
    assert record["user_context"] == "Real studio floor"
    assert record["mime_type"] == "image/jpeg"
    assert record["file_size"] == len(JPEG_BYTES)
    assert db.list_post_drafts() == []
    with db._conn() as conn:
        source = conn.execute(
            "SELECT * FROM content_sources WHERE source_type = 'media_context'"
        ).fetchone()
    assert source is not None
    assert str(record["id"]) in source["metadata_json"]
    assert all(
        item["source_type"] != "media_context"
        for item in db.get_eligible_sources()
    )


def test_content_signature_spoofing_is_rejected_before_database_storage(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    fake_jpeg = tmp_path / "fake.jpg"
    fake_jpeg.write_bytes(b"not-an-image")

    with pytest.raises(ValueError, match="mime_content_mismatch"):
        MediaProcessor(db).process_new_file(
            str(fake_jpeg), "fake.jpg", "image/jpeg", len(b"not-an-image"), "",
        )

    assert db.get_all_media() == []


def test_ai_media_choice_returns_relevance_schema_and_rejects_unknown_id(fake_ai):
    candidates = [{
        "id": 4,
        "media_type": "image",
        "category": "gym_visit",
        "ai_description": "Studio floor",
        "ai_tags": "studio,floor",
        "user_context": "A real customer visit",
    }]
    fake_ai.responses = [
        '{"media_id": 4, "relevance": 86, "reason": "Shows the class format"}',
        '{"media_id": 9, "relevance": 99, "reason": "Hallucinated id"}',
    ]

    assert fake_ai.select_best_media("gym_strategy", "Draft", candidates) == {
        "media_id": 4,
        "relevance": 86,
        "reason": "Shows the class format",
    }
    assert fake_ai.select_best_media("gym_strategy", "Draft", candidates) is None


def test_match_below_80_keeps_draft_text_only(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    source_id = db.add_content_source("evergreen_idea", "Studio source")
    draft_id = _draft(
        db, source_id, "2026-08-11T14:00:00+02:00", "below-threshold",
    )
    media_id = db.add_media("gym.jpg", "/tmp/gym.jpg", "image")

    result = MediaMatcher(
        db,
        ChoiceGenerator({"media_id": media_id, "relevance": 79, "reason": "Weak"}),
    ).attach_best(draft_id)

    assert MEDIA_MATCH_THRESHOLD == 80
    assert result is None
    assert db.get_post_draft(draft_id)["media_id"] is None
    assert db.get_media_by_id(media_id)["lifecycle_state"] == "available"


def test_match_reserves_media_and_appends_context_to_existing_draft(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    source_id = db.add_content_source("evergreen_idea", "Studio source")
    draft_id = _draft(
        db, source_id, "2026-08-11T14:00:00+02:00", "matching",
    )
    image_path = tmp_path / "gym.jpg"
    image_path.write_bytes(JPEG_BYTES)
    record = MediaProcessor(db).process_new_file(
        str(image_path), "gym.jpg", "image/jpeg", len(JPEG_BYTES),
        "Real studio floor",
    )

    attached = MediaMatcher(
        db,
        ChoiceGenerator({"media_id": record["id"], "relevance": 80, "reason": "Fit"}),
    ).attach_best(draft_id)

    draft = db.get_post_draft(draft_id)
    context_source = db.get_media_context_source(record["id"])
    assert attached["lifecycle_state"] == "reserved"
    assert attached["reserved_by_draft_id"] == draft_id
    assert draft["media_id"] == record["id"]
    assert draft["source_ids"] == [source_id, context_source["id"]]
    assert draft["revision"] == 1
    db.release_media_for_draft(draft_id)
    with db._conn() as conn:
        outcomes = [row["outcome"] for row in conn.execute(
            "SELECT outcome FROM draft_evaluations ORDER BY id"
        ).fetchall()]
    assert outcomes == ["media_reserved", "media_released"]


def test_concurrent_matching_reserves_media_for_only_one_existing_draft(tmp_path):
    path = tmp_path / "bot.db"
    setup = Database(str(path))
    source_id = setup.add_content_source("evergreen_idea", "Studio source")
    draft_ids = [
        _draft(setup, source_id, "2026-08-11T14:00:00+02:00", "race-1"),
        _draft(setup, source_id, "2026-08-11T20:00:00+02:00", "race-2"),
    ]
    image_path = tmp_path / "gym.jpg"
    image_path.write_bytes(JPEG_BYTES)
    media_id = MediaProcessor(setup).process_new_file(
        str(image_path), "gym.jpg", "image/jpeg", len(JPEG_BYTES), "Floor",
    )["id"]
    barrier = threading.Barrier(2)
    results = [None, None]

    def match(index):
        db = Database(str(path))
        barrier.wait()
        results[index] = MediaMatcher(
            db,
            ChoiceGenerator({"media_id": media_id, "relevance": 95, "reason": "Fit"}),
        ).attach_best(draft_ids[index])

    threads = [threading.Thread(target=match, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(result is not None for result in results) == 1
    stored = Database(str(path)).get_media_by_id(media_id)
    assert stored["lifecycle_state"] == "reserved"
    assert stored["reserved_by_draft_id"] in draft_ids


def test_failed_publication_releases_reservation(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    media_id = db.add_media("gym.jpg", "/tmp/gym.jpg", "image")
    assert db.reserve_media(media_id, 11)

    db.release_media_for_draft(11)

    media = db.get_media_by_id(media_id)
    assert media["lifecycle_state"] == "available"
    assert media["reserved_by_draft_id"] is None


def test_archive_preserves_record_while_permanent_delete_marks_file_deleted(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    media_id = db.add_media("gym.jpg", "/tmp/gym.jpg", "image")
    assert db.archive_media(media_id)
    assert db.get_media_by_id(media_id)["lifecycle_state"] == "archived"

    db.mark_media_file_deleted(media_id)

    record = db.get_media_by_id(media_id)
    assert record["lifecycle_state"] == "deleted"
    assert record["file_deleted"] == 1


def test_archive_does_not_break_an_active_reservation(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    media_id = db.add_media("gym.jpg", "/tmp/gym.jpg", "image")
    assert db.reserve_media(media_id, 11)

    assert db.archive_media(media_id) is False

    record = db.get_media_by_id(media_id)
    assert record["lifecycle_state"] == "reserved"
    assert record["reserved_by_draft_id"] == 11
