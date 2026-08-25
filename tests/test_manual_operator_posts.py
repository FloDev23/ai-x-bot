import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from modules.database import Database
from modules.draft_pipeline import DraftPipeline
from modules.manual_post_service import ManualPostService
from modules.media_processor import MediaProcessor
from modules.publication_queue import QueueReplenisher
from modules.review_translation import ReviewTranslation


NOW = datetime(2026, 8, 25, 12, 30, tzinfo=timezone.utc)
STATE_KEY = "telegram_session:operator:42"
STATE = json.dumps(
    {
        "kind": "manual_post",
        "step": "commit",
        "token": "operatorSessionToken_1234567890",
        "v": 1,
    },
    sort_keys=True,
    separators=(",", ":"),
)
TOKEN = "operatorSessionToken_1234567890"


class ForbiddenEditorialAI:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def forbidden(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            raise AssertionError(f"editorial AI invoked: {name}")

        return forbidden


def seed_state(db):
    db.set_state(STATE_KEY, STATE)


def seed_sources(db, count):
    return [
        db.add_content_source(
            "founder_note",
            f"Verified operator observation {index}.",
            verified_by="floriano",
        )
        for index in range(count)
    ]


def available_image(db, root):
    media_root = root / "operator-media"
    media_root.mkdir(mode=0o700)
    staged = media_root / "staged.jpg"
    content = b"\xff\xd8\xff\xe0operator-post-media"
    staged.write_bytes(content)
    return MediaProcessor(db).process_new_file(
        str(staged),
        "operator.jpg",
        "image/jpeg",
        len(content),
        "An operator preparing a flexible class.",
    )


def manual_service(db):
    return ManualPostService(db, now_fn=lambda: NOW)


def create(service, **overrides):
    values = {
        "text": "Empty class spots are perishable inventory.",
        "category": "founder_journey",
        "source_ids": [],
        "media_id": None,
        "state_key": STATE_KEY,
        "expected_state_value": STATE,
        "session_token": TOKEN,
        "operator": "floriano",
    }
    values.update(overrides)
    return service.create_approved_from_telegram(**values)


@pytest.mark.parametrize("source_count", [0, 1, 3])
def test_operator_text_enters_approved_reserve_without_editorial_ai(
    tmp_path, source_count,
):
    db = Database(str(tmp_path / f"operator-{source_count}.db"))
    seed_state(db)
    source_ids = seed_sources(db, source_count)
    forbidden = ForbiddenEditorialAI()
    pipeline = DraftPipeline(
        db,
        planner=forbidden,
        generator=forbidden,
        fact_guard=forbidden,
        scorer=forbidden,
        now_fn=lambda: NOW,
        review_translator=forbidden,
    )

    draft, outcome = pipeline.create_manual_from_telegram_session(
        text="Empty class spots are perishable inventory.",
        category="founder_journey",
        source_ids=source_ids,
        media_id=None,
        translation_it=None,
        state_key=STATE_KEY,
        expected_state_value=STATE,
        session_token=TOKEN,
        operator="floriano",
    )

    assert outcome == "created"
    assert draft["status"] == "approved"
    assert draft["origin"] == "manual_operator"
    assert draft["text"] == "Empty class spots are perishable inventory."
    assert draft["source_ids"] == source_ids
    assert draft["score_data"] == {"authority": "operator", "total": 75}
    queued = db.get_queue_draft(draft["id"])
    assert queued["translation_policy"] == "advisory"
    assert queued["translation_status"] == "pending"
    assert queued["approved_queue_at"] == NOW.isoformat()
    assert forbidden.calls == []
    assert db.get_state(STATE_KEY) is None


@pytest.mark.parametrize(
    ("text", "outcome"),
    [
        ("x" * 280, "created"),
        ("x" * 281, "rejected"),
        ("", "rejected"),
        ("   ", "rejected"),
        ("bad\ud800copy", "rejected"),
    ],
)
def test_operator_copy_utf8_and_character_boundary(tmp_path, text, outcome):
    db = Database(str(tmp_path / f"boundary-{len(text)}-{outcome}.db"))
    seed_state(db)

    draft, actual = create(manual_service(db), text=text)

    assert actual == outcome
    assert (draft is not None) is (outcome == "created")
    assert db.get_state(STATE_KEY) == (None if outcome == "created" else STATE)


def test_operator_media_is_reserved_and_context_is_trace_only(tmp_path):
    db = Database(str(tmp_path / "operator-media.db"))
    seed_state(db)
    media = available_image(db, tmp_path)

    draft, outcome = create(manual_service(db), media_id=media["id"])

    assert outcome == "created"
    context = db.get_media_context_source(media["id"])
    assert draft["source_ids"] == [context["id"]]
    assert draft["media_id"] == media["id"]
    reserved = db.get_media_by_id(media["id"])
    assert reserved["lifecycle_state"] == "reserved"
    assert reserved["reserved_by_draft_id"] == draft["id"]


def test_exact_replay_succeeds_and_any_payload_change_is_rejected(tmp_path):
    db = Database(str(tmp_path / "operator-replay.db"))
    seed_state(db)
    service = manual_service(db)

    first, first_outcome = create(service)
    replay, replay_outcome = create(service)
    changed, changed_outcome = create(service, operator="another_operator")

    assert first_outcome == "created"
    assert replay_outcome == "already_applied"
    assert replay["id"] == first["id"]
    assert (changed, changed_outcome) == (None, "rejected")
    assert len(db.list_post_drafts()) == 1
    with db._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM draft_evaluations").fetchone()[0] == 1


def test_exact_replay_is_independent_of_retry_clock(tmp_path):
    db = Database(str(tmp_path / "operator-replay-clock.db"))
    seed_state(db)
    first, first_outcome = create(manual_service(db))

    replay, replay_outcome = create(
        ManualPostService(db, now_fn=lambda: NOW + timedelta(minutes=5)),
    )

    assert first_outcome == "created"
    assert replay_outcome == "already_applied"
    assert replay["id"] == first["id"]


def test_exact_replay_survives_advisory_translation_update(tmp_path):
    db = Database(str(tmp_path / "operator-replay-translation.db"))
    seed_state(db)
    service = manual_service(db)
    first, first_outcome = create(service)
    assert first_outcome == "created"
    assert db.save_review_translation(first["id"], first["revision"], "Traduzione")

    replay, replay_outcome = create(service)

    assert replay_outcome == "already_applied"
    assert replay["id"] == first["id"]


def test_two_workers_create_one_direct_approved_draft(tmp_path):
    path = str(tmp_path / "operator-race.db")
    db = Database(path)
    seed_state(db)
    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait(timeout=5)
        draft, outcome = create(manual_service(Database(path)))
        results.append((draft["id"] if draft else None, outcome))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert {outcome for _draft_id, outcome in results} == {
        "created",
        "already_applied",
    }
    assert len({draft_id for draft_id, _outcome in results}) == 1
    check = Database(path)
    assert len(check.list_post_drafts()) == 1
    with check._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM draft_evaluations").fetchone()[0] == 1


def test_manual_schema_is_additive_and_queue_decoder_is_origin_aware(tmp_path):
    path = tmp_path / "schema.db"
    db = Database(str(path))
    with sqlite3.connect(path) as conn:
        draft_columns = {row[1] for row in conn.execute("PRAGMA table_info(post_drafts)")}
        queue_columns = {row[1] for row in conn.execute("PRAGMA table_info(editorial_queue)")}
        assert "origin" in draft_columns
        assert "translation_policy" in queue_columns
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)

    seed_state(db)
    draft, outcome = create(manual_service(db))
    assert outcome == "created"
    assert db.get_queue_draft(draft["id"])["source_ids"] == []


def test_generated_empty_sources_still_fail_closed_in_queue_decoder(tmp_path):
    db = Database(str(tmp_path / "generated-empty.db"))
    now = NOW.isoformat()
    with db._conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO post_drafts (
                publication_key, text, category, source_ids_json, score_json,
                intended_slot, status, origin, created_at, updated_at
            ) VALUES (?, ?, ?, '[]', '{"total":75}', ?, 'approved',
                      'generated', ?, ?)
            """,
            ("generated-empty", "Unsafe generated row", "founder_journey", now, now, now),
        )
        conn.execute(
            """
            INSERT INTO editorial_queue (
                draft_id, translation_status, translation_policy,
                approved_queue_at, created_at, updated_at
            ) VALUES (?, 'ready', 'required', ?, ?, ?)
            """,
            (cursor.lastrowid, now, now, now),
        )
        draft_id = cursor.lastrowid

    assert db.get_queue_draft(draft_id) is None


def test_advisory_translation_save_preserves_operator_approval(tmp_path):
    db = Database(str(tmp_path / "advisory-translation.db"))
    seed_state(db)
    draft, outcome = create(manual_service(db))
    assert outcome == "created"
    before = db.get_queue_draft(draft["id"])

    assert db.save_review_translation(draft["id"], draft["revision"], "Traduzione")

    after = db.get_queue_draft(draft["id"])
    assert after["translation_status"] == "ready"
    assert after["translation_it"] == "Traduzione"
    assert after["approved_queue_at"] == before["approved_queue_at"]


def test_pending_advisory_operator_post_is_eligible_for_planning(tmp_path):
    db = Database(str(tmp_path / "advisory-planning.db"))
    seed_state(db)
    draft, outcome = create(manual_service(db))
    assert outcome == "created"

    approved = db.list_approved_queue(NOW)

    assert [item["id"] for item in approved] == [draft["id"]]
    assert approved[0]["text"] == "Empty class spots are perishable inventory."


def test_translation_rate_limit_never_blocks_manual_planning_or_changes_x_copy(
    tmp_path,
):
    db = Database(str(tmp_path / "advisory-rate-limit.db"))
    seed_state(db)
    draft, outcome = create(manual_service(db))
    assert outcome == "created"

    class RateLimitedThenReady:
        def __init__(self):
            self.calls = 0

        def translate(self, english_text):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("provider_rate_limited")
            return ReviewTranslation("I posti vuoti sono inventario deperibile.")

    translator = RateLimitedThenReady()
    retries = QueueReplenisher(
        db=db,
        pipeline=object(),
        translator=translator,
        operator_timezone="Europe/Rome",
        approved_queue_target=14,
        pending_review_limit=5,
        daily_generation_cap=5,
    )

    assert retries.retry_pending_translations(NOW, draft_id=draft["id"]) == []
    assert [item["id"] for item in db.list_approved_queue(NOW)] == [draft["id"]]
    assert retries.retry_pending_translations(
        NOW, draft_id=draft["id"],
    ) == [draft["id"]]
    queued = db.get_queue_draft(draft["id"])
    assert queued["translation_it"] == "I posti vuoti sono inventario deperibile."
    assert queued["text"] == "Empty class spots are perishable inventory."
