import io
import hashlib
import logging
import os
import select
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path

import pytest

from config import (
    MEDIA_MATCH_THRESHOLD,
    TELEGRAM_MAX_IMAGE_BYTES,
    TELEGRAM_MAX_VIDEO_BYTES,
)
from modules.ai_generator import AIGenerator
from modules.database import Database
from modules.media_matcher import MediaMatcher
from modules.media_processor import (
    MediaProcessor,
    media_content_matches,
    stage_media_upload,
    validate_media_upload,
)


JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"jpeg-data"


def _insert_identity_media(
    conn,
    media_id,
    filepath,
    *,
    lifecycle_state="available",
    reserved_by_draft_id=None,
):
    file_stat = filepath.stat()
    conn.execute(
        """
        INSERT INTO media_library (
            id, filename, filepath, media_type, lifecycle_state,
            reserved_by_draft_id, file_size, file_device, file_inode,
            file_sha256
        ) VALUES (?, ?, ?, 'image', ?, ?, ?, ?, ?, ?)
        """,
        (
            media_id,
            filepath.name,
            str(filepath),
            lifecycle_state,
            reserved_by_draft_id,
            file_stat.st_size,
            file_stat.st_dev,
            file_stat.st_ino,
            hashlib.sha256(filepath.read_bytes()).hexdigest(),
        ),
    )


def _create_legacy_media_database(path, *, lifecycle_column):
    lifecycle_definition = (
        ", lifecycle_state TEXT DEFAULT 'available'"
        if lifecycle_column
        else ""
    )
    conn = sqlite3.connect(path)
    conn.executescript(f"""
        CREATE TABLE media_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL,
            media_type TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            file_deleted INTEGER DEFAULT 0
            {lifecycle_definition}
        );
        INSERT INTO media_library (filename, filepath, media_type, used)
        VALUES ('used.jpg', '/legacy/used.jpg', 'image', 1);
        INSERT INTO media_library (
            filename, filepath, media_type, used, file_deleted
        ) VALUES ('deleted.jpg', '/legacy/deleted.jpg', 'image', 1, 1);
    """)
    conn.close()


def bmff_ftyp(major_brand, compatible_brands=()):
    payload = major_brand + b"\x00\x00\x00\x00" + b"".join(compatible_brands)
    return (8 + len(payload)).to_bytes(4, "big") + b"ftyp" + payload


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


def test_ai_image_stream_failure_returns_none_without_path_reopen():
    from types import SimpleNamespace

    class FailingCompletions:
        def create(self, **_kwargs):
            raise RuntimeError("vision unavailable")

    generator = AIGenerator.__new__(AIGenerator)
    generator.client = SimpleNamespace(
        chat=SimpleNamespace(completions=FailingCompletions()),
    )

    result = generator.analyze_image(io.BytesIO(JPEG_BYTES), "gym.jpg")

    assert result is None


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


@pytest.mark.parametrize(
    ("mime_type", "content"),
    [
        ("video/mp4", bmff_ftyp(b"isom", (b"mp42",))),
        ("video/quicktime", bmff_ftyp(b"qt  ", (b"qt  ",))),
        ("video/x-m4v", bmff_ftyp(b"M4V ", (b"isom",))),
    ],
)
def test_supported_video_ftyp_brands_are_accepted(mime_type, content):
    assert media_content_matches(io.BytesIO(content), mime_type) is True


@pytest.mark.parametrize(
    "content",
    [
        bmff_ftyp(b"heic", (b"mif1",)),
        bmff_ftyp(b"heic", (b"isom",)),
        bmff_ftyp(b"avif", (b"mif1",)),
        b"\x00\x00\x00\x0cftypisom",
        b"\x00\x00\x00\x40ftypisom\x00\x00\x00\x00",
        b"\x00\x00\x00\x10freeisom\x00\x00\x00\x00",
        b"\x00\x00\x00\x11ftypisom\x00\x00\x00\x00x",
    ],
)
def test_image_or_malformed_bmff_is_rejected_as_video(content):
    assert media_content_matches(io.BytesIO(content), "video/mp4") is False


@pytest.mark.parametrize(
    "invalid_media_id",
    [True, False, "4", 4.0],
)
def test_ai_media_choice_rejects_coercible_media_ids(fake_ai, invalid_media_id):
    candidates = [{
        "id": 4,
        "media_type": "image",
        "category": "gym_visit",
        "ai_description": "Studio floor",
        "ai_tags": "studio",
        "user_context": "visit",
    }]
    fake_ai.responses = [
        '{"media_id": %s, "relevance": 86, "reason": "fit"}'
        % __import__("json").dumps(invalid_media_id)
    ]
    assert fake_ai.select_best_media("gym_strategy", "Draft", candidates) is None


@pytest.mark.parametrize(
    "invalid_fields",
    [
        {"relevance": True, "reason": "fit"},
        {"relevance": 80.5, "reason": "fit"},
        {"relevance": 101, "reason": "fit"},
        {"relevance": 80, "reason": ""},
        {"relevance": 80, "reason": 7},
        {"relevance": 80, "reason": "x" * 501},
    ],
)
def test_ai_media_choice_rejects_non_exact_relevance_and_reason(
    fake_ai, invalid_fields,
):
    import json

    candidates = [{
        "id": 4,
        "media_type": "image",
        "category": "gym_visit",
        "ai_description": "Studio floor",
        "ai_tags": "studio",
        "user_context": "visit",
    }]
    fake_ai.responses = [json.dumps({"media_id": 4, **invalid_fields})]
    assert fake_ai.select_best_media("gym_strategy", "Draft", candidates) is None


@pytest.mark.parametrize(
    "choice",
    [
        {"media_id": True, "relevance": 80, "reason": "fit"},
        {"media_id": "1", "relevance": 80, "reason": "fit"},
        {"media_id": 1.0, "relevance": 80, "reason": "fit"},
        {"media_id": 1, "relevance": True, "reason": "fit"},
        {"media_id": 1, "relevance": 80.5, "reason": "fit"},
        {"media_id": 1, "relevance": 101, "reason": "fit"},
        {"media_id": 1, "relevance": 80, "reason": ""},
        {"media_id": 1, "relevance": 80, "reason": "x" * 501},
    ],
)
def test_matcher_rejects_non_exact_choice_schema(tmp_path, choice):
    db = Database(str(tmp_path / "bot.db"))
    source_id = db.add_content_source("evergreen_idea", "Studio source")
    draft_id = _draft(db, source_id, "2026-08-12T14:00:00+02:00", "strict")
    image_path = tmp_path / "gym.jpg"
    image_path.write_bytes(JPEG_BYTES)
    media_id = MediaProcessor(db).process_new_file(
        str(image_path), "gym.jpg", "image/jpeg", len(JPEG_BYTES), "context",
    )["id"]
    if type(choice["media_id"]) is int:
        choice = {**choice, "media_id": media_id}

    assert MediaMatcher(db, ChoiceGenerator(choice)).attach_best(draft_id) is None
    assert db.get_media_by_id(media_id)["lifecycle_state"] == "available"


def test_ai_media_parse_failure_does_not_log_raw_output(fake_ai, caplog):
    secret = "REFLECTED_USER_CONTEXT_839"
    fake_ai.responses = ['{"reason":"%s" broken}' % secret]
    candidates = [{
        "id": 4,
        "media_type": "image",
        "category": "gym_visit",
        "ai_description": "Studio floor",
        "ai_tags": "studio",
        "user_context": secret,
    }]
    caplog.set_level(logging.WARNING, logger="modules.ai_generator")

    assert fake_ai.select_best_media("gym_strategy", "Draft", candidates) is None

    assert secret not in caplog.text
    assert "raw:" not in caplog.text
    assert "media_choice_invalid" in caplog.text


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


def test_permanent_delete_db_transition_rejects_reserved_atomically(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    media_id = db.add_media("gym.jpg", "/tmp/gym.jpg", "image")
    assert db.reserve_media(media_id, 11)
    callback_calls = []

    deleted = db.mark_media_file_deleted(
        media_id, delete_file=lambda: callback_calls.append(media_id),
    )

    assert deleted is False
    assert callback_calls == []
    record = db.get_media_by_id(media_id)
    assert record["lifecycle_state"] == "reserved"
    assert record["reserved_by_draft_id"] == 11


def test_permanent_delete_callback_failure_rolls_back_database_state(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    media_id = db.add_media("gym.jpg", "/tmp/gym.jpg", "image")

    with pytest.raises(OSError, match="unlink failed"):
        db.mark_media_file_deleted(
            media_id,
            delete_file=lambda: (_ for _ in ()).throw(OSError("unlink failed")),
        )

    record = db.get_media_by_id(media_id)
    assert record["lifecycle_state"] == "available"
    assert record["file_deleted"] == 0


def test_reserve_and_permanent_delete_are_serialized_by_sqlite(tmp_path):
    path = tmp_path / "bot.db"
    setup = Database(str(path))
    media_id = setup.add_media("gym.jpg", "/tmp/gym.jpg", "image")
    barrier = threading.Barrier(2)
    outcomes = {}

    def reserve():
        barrier.wait()
        outcomes["reserved"] = Database(str(path)).reserve_media(media_id, 11)

    def delete():
        barrier.wait()
        outcomes["deleted"] = Database(str(path)).mark_media_file_deleted(
            media_id, delete_file=lambda: None,
        )

    threads = [threading.Thread(target=reserve), threading.Thread(target=delete)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes in (
        {"reserved": True, "deleted": False},
        {"reserved": False, "deleted": True},
    )
    state = Database(str(path)).get_media_by_id(media_id)["lifecycle_state"]
    assert state == ("reserved" if outcomes["reserved"] else "deleted")


def test_staging_retries_instead_of_following_existing_symlink(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from modules import media_processor as media_module

    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"keep-me")
    symlink = tmp_path / ".upload-collision.jpg"
    symlink.symlink_to(outside)
    values = iter((
        SimpleNamespace(hex="collision"),
        SimpleNamespace(hex="safe"),
    ))
    monkeypatch.setattr(media_module.uuid, "uuid4", lambda: next(values))

    staged = Path(stage_media_upload(io.BytesIO(JPEG_BYTES), str(tmp_path), "gym.jpg"))

    assert staged.name == ".upload-safe.jpg"
    assert not staged.is_symlink()
    assert staged.read_bytes() == JPEG_BYTES
    assert outside.read_bytes() == b"keep-me"


def test_staging_retries_fsync_after_eintr(tmp_path, monkeypatch):
    from modules import media_processor as media_module

    original_fsync = os.fsync
    attempts = 0

    def interrupted_once(fd):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise InterruptedError("staging fsync interrupted")
        return original_fsync(fd)

    monkeypatch.setattr(media_module.os, "fsync", interrupted_once)

    staged = Path(
        stage_media_upload(io.BytesIO(JPEG_BYTES), str(tmp_path), "gym.jpg")
    )

    assert attempts == 2
    assert staged.read_bytes() == JPEG_BYTES


class RaisingAI:
    def analyze_image(self, _file, _filename):
        raise RuntimeError("vision unavailable")


class RaisingDatabase:
    def add_media_with_context(self, **_values):
        raise RuntimeError("database unavailable")


@pytest.mark.parametrize("failure_source", ["ai", "database"])
def test_processor_failure_removes_staged_and_final_files(tmp_path, failure_source):
    staged = tmp_path / ".upload-staged.tmp"
    staged.write_bytes(JPEG_BYTES)
    processor = MediaProcessor(
        RaisingDatabase() if failure_source == "database" else Database(str(tmp_path / "db.sqlite")),
        RaisingAI() if failure_source == "ai" else None,
    )

    with pytest.raises(RuntimeError):
        processor.process_new_file(
            str(staged), "gym.jpg", "image/jpeg", len(JPEG_BYTES), "context",
        )

    assert list(tmp_path.glob("*.jpg")) == []
    assert not staged.exists()
    if failure_source == "ai":
        assert processor.db.get_all_media() == []


def test_database_transaction_failure_leaves_no_row_or_file(tmp_path):
    db = Database(str(tmp_path / "db.sqlite"))
    with db._conn() as conn:
        conn.execute("""
            CREATE TRIGGER fail_media_context
            BEFORE INSERT ON content_sources
            WHEN NEW.source_type = 'media_context'
            BEGIN SELECT RAISE(ABORT, 'context failure'); END
        """)
    staged = tmp_path / ".upload-staged.tmp"
    staged.write_bytes(JPEG_BYTES)

    with pytest.raises(Exception, match="context failure"):
        MediaProcessor(db).process_new_file(
            str(staged), "gym.jpg", "image/jpeg", len(JPEG_BYTES), "context",
        )

    assert db.get_all_media() == []
    with db._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM content_sources"
        ).fetchone()[0] == 0
    assert [path for path in tmp_path.iterdir() if path.suffix != ".sqlite"] == []


def test_staged_symlink_swap_cannot_change_verified_inode(tmp_path, monkeypatch):
    from modules import media_processor as media_module

    db = Database(str(tmp_path / "db.sqlite"))
    staged = tmp_path / ".upload-race.jpg"
    staged.write_bytes(JPEG_BYTES)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"do-not-touch")
    original_open = os.open
    swapped = False

    def swap_before_destination_claim(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if (
            path == "gym.jpg"
            and flags & os.O_CREAT
            and flags & os.O_EXCL
            and not swapped
        ):
            staged.unlink()
            staged.symlink_to(outside)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(media_module.os, "open", swap_before_destination_claim)

    record = MediaProcessor(db).process_new_file(
        str(staged), "gym.jpg", "image/jpeg", len(JPEG_BYTES), "context",
    )

    final_path = Path(record["filepath"])
    assert swapped is True
    assert final_path.is_file()
    assert final_path.is_symlink() is False
    assert final_path.read_bytes() == JPEG_BYTES
    assert outside.read_bytes() == b"do-not-touch"
    assert os.path.lexists(staged) is False
    assert [row["id"] for row in db.get_all_media()] == [record["id"]]
    with db._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM content_sources WHERE source_type = 'media_context'"
        ).fetchone()[0] == 1


def test_staged_symlink_swap_and_database_failure_leave_no_orphans(
    tmp_path, monkeypatch,
):
    from modules import media_processor as media_module

    db = Database(str(tmp_path / "db.sqlite"))
    with db._conn() as conn:
        conn.execute("""
            CREATE TRIGGER fail_media_context_after_swap
            BEFORE INSERT ON content_sources
            WHEN NEW.source_type = 'media_context'
            BEGIN SELECT RAISE(ABORT, 'context failure after swap'); END
        """)
    staged = tmp_path / ".upload-race.jpg"
    staged.write_bytes(JPEG_BYTES)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"do-not-touch")
    original_open = os.open
    swapped = False

    def swap_before_destination_claim(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if (
            path == "gym.jpg"
            and flags & os.O_CREAT
            and flags & os.O_EXCL
            and not swapped
        ):
            staged.unlink()
            staged.symlink_to(outside)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(media_module.os, "open", swap_before_destination_claim)

    with pytest.raises(Exception, match="context failure after swap"):
        MediaProcessor(db).process_new_file(
            str(staged), "gym.jpg", "image/jpeg", len(JPEG_BYTES), "context",
        )

    assert swapped is True
    assert db.get_all_media() == []
    with db._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM content_sources"
        ).fetchone()[0] == 0
    assert outside.read_bytes() == b"do-not-touch"
    assert os.path.lexists(staged) is False
    assert list(tmp_path.glob("gym*.jpg")) == []


def test_descriptor_copy_retries_eintr_without_losing_partial_bytes(
    tmp_path, monkeypatch,
):
    from modules import media_processor as media_module

    payload = b"partial-read-and-write-payload"
    source_path = tmp_path / "source.bin"
    destination_path = tmp_path / "destination.bin"
    source_path.write_bytes(payload)
    source_fd = os.open(source_path, os.O_RDONLY)
    destination_fd = os.open(
        destination_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600,
    )
    original_read = os.read
    original_write = os.write
    original_fsync = os.fsync
    read_attempt = 0
    write_attempt = 0
    fsync_attempt = 0

    def interrupted_partial_read(fd, size):
        nonlocal read_attempt
        read_attempt += 1
        if read_attempt in {1, 3}:
            raise InterruptedError("read interrupted")
        return original_read(fd, min(size, 3))

    def interrupted_partial_write(fd, data):
        nonlocal write_attempt
        write_attempt += 1
        if write_attempt in {2, 5}:
            raise InterruptedError("write interrupted")
        return original_write(fd, data[:2])

    def interrupted_fsync(fd):
        nonlocal fsync_attempt
        fsync_attempt += 1
        if fsync_attempt == 1:
            raise InterruptedError("fsync interrupted")
        return original_fsync(fd)

    monkeypatch.setattr(media_module.os, "read", interrupted_partial_read)
    monkeypatch.setattr(media_module.os, "write", interrupted_partial_write)
    monkeypatch.setattr(media_module.os, "fsync", interrupted_fsync)
    try:
        media_module._copy_file_descriptor(source_fd, destination_fd)
    finally:
        os.close(source_fd)
        os.close(destination_fd)

    assert destination_path.read_bytes() == payload


def test_post_validation_path_swap_aborts_without_persisting_symlink(tmp_path):
    db = Database(str(tmp_path / "db.sqlite"))
    staged = tmp_path / ".upload-race.jpg"
    staged.write_bytes(JPEG_BYTES)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"do-not-touch")

    class SwapAfterValidationAI:
        consumed = None

        def analyze_image(self, media, filename=None):
            if filename is None:
                final_path = Path(media)
                self.consumed = final_path.read_bytes()
            else:
                final_path = tmp_path / filename
                media.seek(0)
                self.consumed = media.read()
            final_path.unlink()
            final_path.symlink_to(outside)
            return None

    ai = SwapAfterValidationAI()

    with pytest.raises(ValueError, match="media_file_identity_changed"):
        MediaProcessor(db, ai).process_new_file(
            str(staged), "gym.jpg", "image/jpeg", len(JPEG_BYTES), "context",
        )

    assert ai.consumed == JPEG_BYTES
    assert db.get_all_media() == []
    with db._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM content_sources"
        ).fetchone()[0] == 0
    assert outside.read_bytes() == b"do-not-touch"
    assert os.path.lexists(tmp_path / "gym.jpg") is False
    assert os.path.lexists(staged) is False


def test_video_extraction_consumes_pinned_fd_when_input_path_is_swapped(
    tmp_path, monkeypatch,
):
    from modules import media_processor as media_module

    original_video = bmff_ftyp(b"isom", (b"mp42",)) + b"original-video"
    replacement_video = bmff_ftyp(b"isom", (b"mp42",)) + b"replacement-video"
    staged = tmp_path / ".upload-race.mp4"
    staged.write_bytes(original_video)
    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(replacement_video)
    consumed = {}

    def ffmpeg_probe(command, **kwargs):
        final_path = tmp_path / "gym.mp4"
        final_path.unlink()
        final_path.symlink_to(replacement)
        input_arg = command[command.index("-i") + 1]
        consumed["input_arg"] = input_arg
        input_stream = kwargs.get("stdin")
        if input_stream is None:
            consumed["bytes"] = Path(input_arg).read_bytes()
        else:
            consumed["bytes"] = input_stream.read()
        Path(command[-1]).write_bytes(JPEG_BYTES)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(media_module.subprocess, "run", ffmpeg_probe)
    db = Database(str(tmp_path / "db.sqlite"))

    with pytest.raises(ValueError, match="media_file_identity_changed"):
        MediaProcessor(db, ai_generator=object()).process_new_file(
            str(staged), "gym.mp4", "video/mp4", len(original_video), "context",
        )

    assert consumed == {"input_arg": "pipe:0", "bytes": original_video}
    assert replacement.read_bytes() == replacement_video
    assert db.get_all_media() == []
    assert os.path.lexists(tmp_path / "gym.mp4") is False


def test_database_callback_swap_fails_before_identity_commit(tmp_path):
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"do-not-touch")

    class SwapDuringDatabaseCallback(Database):
        def add_media_with_context(self, **values):
            locator = Path(values["filepath"])
            locator.unlink()
            locator.symlink_to(outside)
            return super().add_media_with_context(**values)

    db = SwapDuringDatabaseCallback(str(tmp_path / "db.sqlite"))
    staged = tmp_path / ".upload-race.jpg"
    staged.write_bytes(JPEG_BYTES)

    with pytest.raises(ValueError, match="media_file_identity_changed"):
        MediaProcessor(db).process_new_file(
            str(staged), "gym.jpg", "image/jpeg", len(JPEG_BYTES), "context",
        )

    assert db.get_all_media() == []
    with db._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM content_sources WHERE source_type = 'media_context'"
        ).fetchone()[0] == 0
    assert outside.read_bytes() == b"do-not-touch"
    assert os.path.lexists(tmp_path / "gym.jpg") is False


def test_persisted_media_identity_can_be_verified_before_future_use(tmp_path):
    from modules import media_processor as media_module

    open_verified_media = getattr(media_module, "open_verified_media", None)
    assert callable(open_verified_media)

    db = Database(str(tmp_path / "db.sqlite"))
    staged = tmp_path / ".upload-staged.jpg"
    staged.write_bytes(JPEG_BYTES)
    record = MediaProcessor(db).process_new_file(
        str(staged), "gym.jpg", "image/jpeg", len(JPEG_BYTES), "context",
    )

    assert type(record["file_device"]) is int
    assert type(record["file_inode"]) is int
    assert record["file_size"] == len(JPEG_BYTES)
    assert len(record["file_sha256"]) == 64
    with open_verified_media(record) as media_file:
        assert media_file.read() == JPEG_BYTES


def test_future_use_rejects_same_inode_same_size_content_change(tmp_path):
    from modules.media_processor import open_verified_media

    db = Database(str(tmp_path / "db.sqlite"))
    staged = tmp_path / ".upload-staged.jpg"
    staged.write_bytes(JPEG_BYTES)
    record = MediaProcessor(db).process_new_file(
        str(staged), "gym.jpg", "image/jpeg", len(JPEG_BYTES), "context",
    )
    changed_bytes = b"\xff\xd8\xff\xe1" + b"jpeg-data"
    assert len(changed_bytes) == len(JPEG_BYTES)
    Path(record["filepath"]).write_bytes(changed_bytes)

    with pytest.raises(ValueError, match="media_file_identity_changed"):
        with open_verified_media(record):
            pass


def test_mutation_revalidates_empty_snapshot_after_insert_commit(
    tmp_path, monkeypatch,
):
    from modules import database as database_module
    from modules.media_store import media_store_lock as real_media_store_lock

    snapshot_read = threading.Event()
    write_attempted = threading.Event()
    root_lock_attempted = threading.Event()
    callback_called = threading.Event()

    class ObservedDatabase(Database):
        @contextmanager
        def _conn(self):
            with super()._conn() as conn:
                if threading.current_thread().name == "media-mutation":
                    def trace(statement):
                        normalized = " ".join(statement.upper().split())
                        if (
                            normalized.startswith("SELECT")
                            and "FROM MEDIA_LIBRARY" in normalized
                            and "FILE_DEVICE" in normalized
                        ):
                            snapshot_read.set()
                        if normalized == "BEGIN IMMEDIATE":
                            write_attempted.set()

                    conn.set_trace_callback(trace)
                yield conn

    db = ObservedDatabase(str(tmp_path / "db.sqlite"))
    media_path = tmp_path / "gym.jpg"
    media_path.write_bytes(JPEG_BYTES)
    writer = sqlite3.connect(db.db_path)
    result = []
    errors = []

    @contextmanager
    def observed_media_store_lock(directory):
        if Path(directory).resolve() == tmp_path.resolve():
            root_lock_attempted.set()
        with real_media_store_lock(directory) as lease:
            yield lease

    monkeypatch.setattr(
        database_module, "media_store_lock", observed_media_store_lock,
    )

    def delete_media():
        try:
            result.append(
                db.mark_media_file_deleted(1, delete_file=callback_called.set)
            )
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=delete_media, name="media-mutation")
    retry_observed = False
    callback_during_lock = False
    try:
        with real_media_store_lock(tmp_path):
            writer.execute("BEGIN IMMEDIATE")
            _insert_identity_media(writer, 1, media_path)
            thread.start()
            assert snapshot_read.wait(timeout=1)
            assert write_attempted.wait(timeout=1)

            writer.commit()
            retry_observed = root_lock_attempted.wait(timeout=1)
            callback_during_lock = callback_called.is_set()
    finally:
        writer.close()
        thread.join(timeout=2)

    assert retry_observed, "committed target root was not locked on retry"
    assert callback_during_lock is False
    assert thread.is_alive() is False
    assert errors == []
    assert result == [True]
    assert callback_called.is_set()
    assert db.get_media_by_id(1)["lifecycle_state"] == "deleted"


def test_mutation_retries_when_target_identity_moves_to_another_root(
    tmp_path, monkeypatch,
):
    from modules import database as database_module
    from modules.media_store import media_store_lock as real_media_store_lock

    root_a = tmp_path / "a-root"
    root_b = tmp_path / "b-root"
    root_a.mkdir(mode=0o700)
    root_b.mkdir(mode=0o700)
    staged = root_a / ".upload-staged.jpg"
    staged.write_bytes(JPEG_BYTES)
    db = Database(str(tmp_path / "db.sqlite"))
    record = MediaProcessor(db).process_new_file(
        str(staged), "gym.jpg", "image/jpeg", len(JPEG_BYTES), "context",
    )
    replacement = root_b / "gym.jpg"
    replacement.write_bytes(JPEG_BYTES)

    root_a_attempted = threading.Event()
    root_b_attempted = threading.Event()
    finished = threading.Event()
    attempts = []
    result = []
    errors = []

    @contextmanager
    def observed_media_store_lock(directory):
        root = Path(directory).resolve()
        attempts.append(root)
        if root == root_a.resolve():
            root_a_attempted.set()
        if root == root_b.resolve():
            root_b_attempted.set()
        with real_media_store_lock(directory) as lease:
            yield lease

    monkeypatch.setattr(
        database_module, "media_store_lock", observed_media_store_lock,
    )

    def archive_media():
        try:
            result.append(db.archive_media(record["id"]))
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=archive_media)
    root_a_guard = real_media_store_lock(root_a)
    root_b_guard = real_media_store_lock(root_b)
    root_a_guard.__enter__()
    root_a_held = True
    root_b_held = False
    retried_to_root_b = False
    finished_while_root_b_locked = False
    try:
        thread.start()
        assert root_a_attempted.wait(timeout=1)
        replacement_stat = replacement.stat()
        with db._conn() as conn:
            conn.execute(
                """
                UPDATE media_library
                SET filepath = ?, file_device = ?, file_inode = ?,
                    file_size = ?, file_sha256 = ?
                WHERE id = ?
                """,
                (
                    str(replacement),
                    replacement_stat.st_dev,
                    replacement_stat.st_ino,
                    replacement_stat.st_size,
                    hashlib.sha256(replacement.read_bytes()).hexdigest(),
                    record["id"],
                ),
            )

        root_b_guard.__enter__()
        root_b_held = True
        root_a_guard.__exit__(None, None, None)
        root_a_held = False
        retried_to_root_b = root_b_attempted.wait(timeout=1)
        finished_while_root_b_locked = finished.is_set()
    finally:
        if root_a_held:
            root_a_guard.__exit__(None, None, None)
        if root_b_held:
            root_b_guard.__exit__(None, None, None)
        thread.join(timeout=2)

    assert retried_to_root_b, "changed target root was never acquired"
    assert finished_while_root_b_locked is False
    assert thread.is_alive() is False
    assert errors == []
    assert result == [True]
    assert attempts[:2] == [root_a.resolve(), root_b.resolve()]
    moved = db.get_media_by_id(record["id"])
    assert moved["filepath"] == str(replacement)
    assert moved["lifecycle_state"] == "archived"


def test_release_locks_only_target_roots_in_sorted_order(tmp_path, monkeypatch):
    from modules import database as database_module
    from modules.media_store import media_store_lock as real_media_store_lock

    db = Database(str(tmp_path / "db.sqlite"))
    roots = {
        name: tmp_path / name
        for name in ("z-target", "a-target", "m-unrelated")
    }
    records = {}
    for name, root in roots.items():
        root.mkdir(mode=0o700)
        staged = root / f".upload-{name}.jpg"
        staged.write_bytes(JPEG_BYTES)
        records[name] = MediaProcessor(db).process_new_file(
            str(staged), f"{name}.jpg", "image/jpeg", len(JPEG_BYTES), "context",
        )
    with db._conn() as conn:
        conn.execute(
            """
            UPDATE media_library
            SET lifecycle_state = 'reserved', reserved_by_draft_id = 17
            WHERE id IN (?, ?)
            """,
            (records["z-target"]["id"], records["a-target"]["id"]),
        )

    attempts = []

    @contextmanager
    def observed_media_store_lock(directory):
        attempts.append(Path(directory).resolve())
        with real_media_store_lock(directory) as lease:
            yield lease

    monkeypatch.setattr(
        database_module, "media_store_lock", observed_media_store_lock,
    )

    db.release_media_for_draft(17)

    expected = sorted(
        [roots["a-target"].resolve(), roots["z-target"].resolve()],
        key=str,
    )
    assert attempts == expected
    assert db.get_media_by_id(records["a-target"]["id"])[
        "lifecycle_state"
    ] == "available"
    assert db.get_media_by_id(records["z-target"]["id"])[
        "lifecycle_state"
    ] == "available"
    assert db.get_media_by_id(records["m-unrelated"]["id"])[
        "lifecycle_state"
    ] == "available"


def test_atomic_draft_replacement_locks_reserved_media_root(
    tmp_path, monkeypatch,
):
    from modules import database as database_module
    from modules.media_store import media_store_lock as real_media_store_lock

    db = Database(str(tmp_path / "db.sqlite"))
    source_id = db.add_content_source("evergreen_idea", "Verified source")
    draft_id = _draft(
        db, source_id, "2026-08-12T14:00:00+02:00", "replace-old",
    )
    staged = tmp_path / ".upload-staged.jpg"
    staged.write_bytes(JPEG_BYTES)
    record = MediaProcessor(db).process_new_file(
        str(staged), "gym.jpg", "image/jpeg", len(JPEG_BYTES), "context",
    )
    assert db.transition_post_draft(
        draft_id,
        ["pending_approval"],
        "pending_approval",
        media_id=record["id"],
    )
    assert db.reserve_media(record["id"], draft_id)
    prior = db.get_post_draft(draft_id)

    root_attempted = threading.Event()
    finished = threading.Event()
    result = []
    errors = []

    @contextmanager
    def observed_media_store_lock(directory):
        root_attempted.set()
        with real_media_store_lock(directory) as lease:
            yield lease

    monkeypatch.setattr(
        database_module, "media_store_lock", observed_media_store_lock,
    )

    def replace_draft():
        try:
            result.append(db.replace_post_draft_atomic(
                prior_draft_id=draft_id,
                expected_revision=prior["revision"],
                expected_slot=prior["intended_slot"],
                expected_category=prior["category"],
                expected_source_ids=prior["source_ids"],
                text="Replacement text",
                score_data={"total": 91},
                publication_key="replace-new",
            ))
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=replace_draft)
    with real_media_store_lock(tmp_path):
        thread.start()
        lock_was_attempted = root_attempted.wait(timeout=1)
        finished_while_locked = finished.is_set()
    thread.join(timeout=2)

    assert lock_was_attempted
    assert finished_while_locked is False
    assert thread.is_alive() is False
    assert errors == []
    replacement, outcome = result[0]
    assert outcome == "created"
    assert replacement["status"] == "pending_approval"
    assert db.get_media_by_id(record["id"])["lifecycle_state"] == "available"


def test_duplicate_slot_migration_locks_reserved_media_root(
    tmp_path, monkeypatch,
):
    from modules import database as database_module
    from modules.media_store import media_store_lock as real_media_store_lock

    path = str(tmp_path / "db.sqlite")
    db = Database(path)
    source_id = db.add_content_source("evergreen_idea", "Verified source")
    with db._conn() as conn:
        conn.execute("DROP INDEX uq_post_drafts_live_intended_slot")
    slot = "2026-08-12T14:00:00+02:00"
    keeper_id = _draft(db, source_id, slot, "migration-keeper")
    stale_id = _draft(db, source_id, slot, "migration-stale")
    staged = tmp_path / ".upload-staged.jpg"
    staged.write_bytes(JPEG_BYTES)
    record = MediaProcessor(db).process_new_file(
        str(staged), "gym.jpg", "image/jpeg", len(JPEG_BYTES), "context",
    )
    assert db.transition_post_draft(
        stale_id,
        ["pending_approval"],
        "pending_approval",
        media_id=record["id"],
    )
    assert db.reserve_media(record["id"], stale_id)

    root_attempted = threading.Event()
    finished = threading.Event()
    migrated = []
    errors = []

    @contextmanager
    def observed_media_store_lock(directory):
        root_attempted.set()
        with real_media_store_lock(directory) as lease:
            yield lease

    monkeypatch.setattr(
        database_module, "media_store_lock", observed_media_store_lock,
    )

    def migrate_schema():
        try:
            migrated.append(Database(path))
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=migrate_schema)
    with real_media_store_lock(tmp_path):
        thread.start()
        lock_was_attempted = root_attempted.wait(timeout=1)
        finished_while_locked = finished.is_set()
    thread.join(timeout=2)

    assert lock_was_attempted
    assert finished_while_locked is False
    assert thread.is_alive() is False
    assert errors == []
    assert len(migrated) == 1
    assert migrated[0].get_active_draft_for_slot(slot)["id"] == keeper_id
    assert migrated[0].get_post_draft(stale_id)["status"] == "superseded"
    media = migrated[0].get_media_by_id(record["id"])
    assert media["lifecycle_state"] == "available"
    assert media["reserved_by_draft_id"] is None


def test_interrupted_lifecycle_schema_migration_resumes_from_pending_marker(
    tmp_path, monkeypatch,
):
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE media_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL,
            media_type TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            file_deleted INTEGER DEFAULT 0
        );
        INSERT INTO media_library (filename, filepath, media_type, used)
        VALUES ('used.jpg', '/legacy/used.jpg', 'image', 1);
        INSERT INTO media_library (
            filename, filepath, media_type, used, file_deleted
        ) VALUES ('deleted.jpg', '/legacy/deleted.jpg', 'image', 1, 1);
    """)
    conn.close()

    original_reconcile = Database._reconcile_media_schema
    interrupted = []

    def interrupt_after_schema_commit(self, migration_pending):
        interrupted.append(migration_pending)
        raise RuntimeError("injected migration interruption")

    monkeypatch.setattr(
        Database, "_reconcile_media_schema", interrupt_after_schema_commit,
    )
    with pytest.raises(RuntimeError, match="injected migration interruption"):
        Database(str(path))
    monkeypatch.setattr(Database, "_reconcile_media_schema", original_reconcile)

    recovered = Database(str(path))

    assert interrupted == [True]
    with recovered._conn() as check:
        states = check.execute(
            "SELECT lifecycle_state FROM media_library ORDER BY id"
        ).fetchall()
        marker = check.execute(
            "SELECT value FROM bot_state "
            "WHERE key = 'migration:media_lifecycle_state'"
        ).fetchone()
    assert [row["lifecycle_state"] for row in states] == ["used", "deleted"]
    assert marker["value"] == "complete"


def test_hard_crash_after_lifecycle_alter_rolls_back_and_restart_backfills(
    tmp_path,
):
    path = tmp_path / "legacy-hard-crash.sqlite"
    _create_legacy_media_database(path, lifecycle_column=False)
    child_code = r'''
import os
import sqlite3
import sys

from modules import database as database_module


class CrashAfterLifecycleAlterCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        result = super().execute(sql, parameters)
        normalized = " ".join(sql.upper().split())
        if normalized.startswith(
            "ALTER TABLE MEDIA_LIBRARY ADD COLUMN LIFECYCLE_STATE"
        ):
            print("lifecycle_alter_finished", flush=True)
            os._exit(91)
        return result


class CrashAfterLifecycleAlterConnection(sqlite3.Connection):
    def cursor(self, factory=CrashAfterLifecycleAlterCursor):
        return super().cursor(factory=factory)


real_connect = database_module.sqlite3.connect


def crash_connect(*args, **kwargs):
    kwargs["factory"] = CrashAfterLifecycleAlterConnection
    return real_connect(*args, **kwargs)


database_module.sqlite3.connect = crash_connect
database_module.Database(sys.argv[1])
'''

    crashed = subprocess.run(
        [sys.executable, "-c", child_code, str(path)],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert crashed.returncode == 91
    assert crashed.stdout.strip() == "lifecycle_alter_finished"

    check = sqlite3.connect(path)
    columns_after_crash = {
        row[1] for row in check.execute("PRAGMA table_info(media_library)")
    }
    check.close()

    recovered = Database(str(path))
    with recovered._conn() as check:
        states = check.execute(
            "SELECT lifecycle_state FROM media_library ORDER BY id"
        ).fetchall()
        marker = check.execute(
            "SELECT value FROM bot_state "
            "WHERE key = 'migration:media_lifecycle_state'"
        ).fetchone()

    assert (
        "lifecycle_state" in columns_after_crash,
        [row["lifecycle_state"] for row in states],
        marker["value"],
    ) == (False, ["used", "deleted"], "complete")


def test_concurrent_schema_init_reconciles_column_without_marker(
    tmp_path, monkeypatch,
):
    from modules import database as database_module

    path = tmp_path / "legacy-concurrent.sqlite"
    _create_legacy_media_database(path, lifecycle_column=True)
    owner_at_marker = threading.Event()
    owner_contention_observed = threading.Event()
    allow_owner_commit = threading.Event()
    owner_schema_commit_attempted = threading.Event()
    owner_schema_committed = threading.Event()
    contender_before_sqlite = threading.Event()
    allow_contender_into_sqlite = threading.Event()
    contender_begin_traced = threading.Event()
    contender_begin_returned = threading.Event()
    real_connect = sqlite3.connect

    class SchemaRaceCursor(sqlite3.Cursor):
        def execute(self, sql, parameters=()):
            normalized = " ".join(sql.upper().split())
            contender_begin = (
                normalized == "BEGIN IMMEDIATE"
                and threading.current_thread().name == "schema-init-contender"
                and not contender_before_sqlite.is_set()
            )
            if contender_begin:
                contender_before_sqlite.set()
                assert allow_contender_into_sqlite.wait(timeout=10)
            self._lifecycle_marker_read = (
                normalized
                == "SELECT VALUE FROM BOT_STATE WHERE KEY = ?"
                and parameters == ("migration:media_lifecycle_state",)
            )
            result = super().execute(sql, parameters)
            if contender_begin:
                contender_begin_returned.set()
            return result

        def fetchone(self):
            row = super().fetchone()
            if (
                self._lifecycle_marker_read
                and row is None
                and threading.current_thread().name == "schema-init-owner"
            ):
                assert self.connection.in_transaction is True
                owner_at_marker.set()
                assert contender_begin_traced.wait(timeout=10)
                assert self.connection.in_transaction is True
                assert contender_begin_returned.is_set() is False
                owner_contention_observed.set()
                assert allow_owner_commit.wait(timeout=10)
            return row

    class SchemaRaceConnection(sqlite3.Connection):
        def cursor(self, factory=SchemaRaceCursor):
            return super().cursor(factory=factory)

        def commit(self):
            if threading.current_thread().name == "schema-init-owner":
                owner_schema_commit_attempted.set()
            result = super().commit()
            if threading.current_thread().name == "schema-init-owner":
                owner_schema_committed.set()
            return result

    def synchronized_connect(*args, **kwargs):
        kwargs["factory"] = SchemaRaceConnection
        conn = real_connect(*args, **kwargs)
        if threading.current_thread().name == "schema-init-contender":
            def trace(statement):
                if " ".join(statement.upper().split()) == "BEGIN IMMEDIATE":
                    contender_begin_traced.set()

            conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(database_module.sqlite3, "connect", synchronized_connect)
    databases = []
    errors = []

    def construct_database():
        try:
            databases.append(Database(str(path)))
        except BaseException as error:
            errors.append(error)

    owner = threading.Thread(target=construct_database, name="schema-init-owner")
    contender = threading.Thread(
        target=construct_database,
        name="schema-init-contender",
    )
    owner_started = False
    contender_started = False
    try:
        owner.start()
        owner_started = True
        assert owner_at_marker.wait(timeout=5)
        contender.start()
        contender_started = True
        assert contender_before_sqlite.wait(timeout=5)
        owner_released_from_pre_call_signal = owner_schema_commit_attempted.wait(
            timeout=0.5,
        )
        assert contender_begin_traced.is_set() is False
        allow_contender_into_sqlite.set()
        assert owner_contention_observed.wait(timeout=5)
        contender_returned_while_owner_held = contender_begin_returned.wait(
            timeout=0.5,
        )
        blocked_state = (
            contender_begin_traced.is_set(),
            contender_returned_while_owner_held,
            owner_schema_commit_attempted.is_set(),
            owner_schema_committed.is_set(),
            owner.is_alive(),
            contender.is_alive(),
        )
    finally:
        allow_contender_into_sqlite.set()
        allow_owner_commit.set()
        if owner_started:
            owner.join(timeout=10)
        if contender_started:
            contender.join(timeout=10)

    assert owner_released_from_pre_call_signal is False
    assert blocked_state == (True, False, False, False, True, True)
    assert owner.is_alive() is False
    assert contender.is_alive() is False
    assert contender_begin_returned.is_set()
    assert owner_schema_committed.is_set()
    assert errors == []
    assert len(databases) == 2
    check = real_connect(path)
    states = [
        row[0]
        for row in check.execute(
            "SELECT lifecycle_state FROM media_library ORDER BY id"
        ).fetchall()
    ]
    marker = check.execute(
        "SELECT value FROM bot_state "
        "WHERE key = 'migration:media_lifecycle_state'"
    ).fetchone()
    check.close()
    assert states == ["used", "deleted"]
    assert marker == ("complete",)


def test_noop_schema_init_does_not_resolve_historical_media_roots(
    tmp_path, monkeypatch,
):
    original_media_mutation_roots = Database._media_mutation_roots
    root = tmp_path / "historical-root"
    root.mkdir(mode=0o700)
    path = str(tmp_path / "db.sqlite")
    db = Database(path)
    staged = root / ".upload-staged.jpg"
    staged.write_bytes(JPEG_BYTES)
    record = MediaProcessor(db).process_new_file(
        str(staged), "gym.jpg", "image/jpeg", len(JPEG_BYTES), "context",
    )
    Path(record["filepath"]).unlink()
    root.rmdir()

    root_scans = []

    def observe_media_mutation_roots(snapshot):
        root_scans.append(snapshot)
        return original_media_mutation_roots(snapshot)

    monkeypatch.setattr(
        Database,
        "_media_mutation_roots",
        staticmethod(observe_media_mutation_roots),
    )
    reopened = Database(path)
    reopened_again = Database(path)

    assert reopened.get_media_by_id(record["id"])["id"] == record["id"]
    assert reopened_again.get_media_by_id(record["id"])["id"] == record["id"]
    assert root_scans == []


def test_mutation_snapshot_churn_is_bounded_and_fails_closed(
    tmp_path, monkeypatch,
):
    from modules import database as database_module
    from modules.media_store import media_store_lock as real_media_store_lock

    db = Database(str(tmp_path / "db.sqlite"))
    staged = tmp_path / ".upload-staged.jpg"
    staged.write_bytes(JPEG_BYTES)
    record = MediaProcessor(db).process_new_file(
        str(staged), "gym.jpg", "image/jpeg", len(JPEG_BYTES), "context",
    )
    callback_called = threading.Event()
    attempts = []

    @contextmanager
    def change_status_before_lock(directory):
        attempts.append(Path(directory).resolve())
        if len(attempts) <= 16:
            with db._conn() as conn:
                row = conn.execute(
                    "SELECT lifecycle_state FROM media_library WHERE id = ?",
                    (record["id"],),
                ).fetchone()
                next_state = (
                    "used"
                    if row["lifecycle_state"] == "available"
                    else "available"
                )
                conn.execute(
                    "UPDATE media_library SET lifecycle_state = ? WHERE id = ?",
                    (next_state, record["id"]),
                )
        with real_media_store_lock(directory) as lease:
            yield lease

    monkeypatch.setattr(
        database_module, "media_store_lock", change_status_before_lock,
    )

    with pytest.raises(RuntimeError, match="media_store_snapshot_unstable"):
        db.mark_media_file_deleted(
            record["id"], delete_file=callback_called.set,
        )

    assert len(attempts) == 5
    assert callback_called.is_set() is False
    assert db.get_media_by_id(record["id"])["file_deleted"] == 0


def test_lifecycle_mutation_waits_for_media_store_lock(tmp_path):
    from modules import media_processor as media_module

    media_store_lock = getattr(media_module, "media_store_lock", None)
    assert callable(media_store_lock)

    db = Database(str(tmp_path / "db.sqlite"))
    staged = tmp_path / ".upload-staged.jpg"
    staged.write_bytes(JPEG_BYTES)
    record = MediaProcessor(db).process_new_file(
        str(staged), "gym.jpg", "image/jpeg", len(JPEG_BYTES), "context",
    )
    started = threading.Event()
    finished = threading.Event()
    result = []

    def archive():
        started.set()
        result.append(db.archive_media(record["id"]))
        finished.set()

    with media_store_lock(tmp_path):
        thread = threading.Thread(target=archive)
        thread.start()
        assert started.wait(timeout=1)
        assert finished.wait(timeout=0.05) is False

    thread.join(timeout=1)
    assert finished.is_set()
    assert result == [True]


def test_media_store_lock_serializes_separate_process(tmp_path):
    from modules.media_processor import media_store_lock

    script = """
import sys
from pathlib import Path
from modules.media_store import media_store_lock

print('ready', flush=True)
with media_store_lock(Path(sys.argv[1])):
    print('acquired', flush=True)
"""
    with media_store_lock(tmp_path):
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path)],
            cwd=str(Path(__file__).resolve().parent.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        readable, _writable, _errors = select.select([child.stdout], [], [], 0.05)
        assert readable == []

    stdout, stderr = child.communicate(timeout=2)
    assert child.returncode == 0, stderr
    assert stdout.strip() == "acquired"


def test_lifecycle_mutation_waits_for_root_lock_in_separate_process(tmp_path):
    from modules.media_processor import media_store_lock

    db = Database(str(tmp_path / "db.sqlite"))
    staged = tmp_path / ".upload-staged.jpg"
    staged.write_bytes(JPEG_BYTES)
    record = MediaProcessor(db).process_new_file(
        str(staged), "gym.jpg", "image/jpeg", len(JPEG_BYTES), "context",
    )
    script = """
import sys
from modules.database import Database

database = Database(sys.argv[1])
print('ready', flush=True)
sys.stdin.readline()
print('archiving', flush=True)
archived = database.archive_media(int(sys.argv[2]))
print(f'archived={archived}', flush=True)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, db.db_path, str(record["id"])],
        cwd=str(Path(__file__).resolve().parent.parent),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdin is not None
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "ready"
    with media_store_lock(tmp_path):
        child.stdin.write("archive\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "archiving"
        readable, _writable, _errors = select.select([child.stdout], [], [], 0.05)
        assert readable == []

    stdout, stderr = child.communicate(timeout=2)
    assert child.returncode == 0, stderr
    assert stdout.strip() == "archived=True"
    assert db.get_media_by_id(record["id"])["lifecycle_state"] == "archived"


def test_missing_nofollow_support_rejects_before_opening_staged_target(
    tmp_path, monkeypatch,
):
    from modules import media_processor as media_module

    db = Database(str(tmp_path / "db.sqlite"))
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(JPEG_BYTES)
    staged = tmp_path / ".upload-link.jpg"
    staged.symlink_to(outside)
    open_attempts = []

    def forbidden_open(*args, **kwargs):
        open_attempts.append((args, kwargs))
        raise AssertionError("staged target must not be opened")

    monkeypatch.setattr(media_module, "_NOFOLLOW", 0)
    monkeypatch.setattr(media_module.os, "open", forbidden_open)

    with pytest.raises(RuntimeError, match="secure_nofollow_unavailable"):
        MediaProcessor(db).process_new_file(
            str(staged), "gym.jpg", "image/jpeg", len(JPEG_BYTES), "context",
        )

    assert open_attempts == []
    assert outside.read_bytes() == JPEG_BYTES
    assert db.get_all_media() == []


@pytest.mark.parametrize("trust_failure", ["permissions", "ownership"])
def test_untrusted_media_directory_is_rejected_before_staged_open(
    tmp_path, monkeypatch, trust_failure,
):
    from modules import media_processor as media_module

    media_dir = tmp_path / "media"
    media_dir.mkdir(mode=0o700)
    staged = media_dir / ".upload-staged.jpg"
    staged.write_bytes(JPEG_BYTES)
    if trust_failure == "permissions":
        media_dir.chmod(0o755)
        expected = "insecure_media_directory_permissions"
    else:
        real_euid = os.geteuid()
        monkeypatch.setattr(media_module.os, "geteuid", lambda: real_euid + 1)
        expected = "insecure_media_directory_owner"
    db = Database(str(tmp_path / "db.sqlite"))

    with pytest.raises(PermissionError, match=expected):
        MediaProcessor(db).process_new_file(
            str(staged), "gym.jpg", "image/jpeg", len(JPEG_BYTES), "context",
        )

    assert db.get_all_media() == []
    assert os.path.lexists(staged) is False
