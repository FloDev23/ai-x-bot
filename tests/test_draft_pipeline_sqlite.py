import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from modules.database import Database
from modules.draft_pipeline import DraftPipeline


SLOT = "2030-01-10T12:00:00+00:00"
LIVE_STATUSES = [
    "pending_approval",
    "approved",
    "publishing",
    "published",
    "publication_unknown",
]


def _source(db):
    return db.add_content_source("evergreen_idea", "A verified source.")


def _draft_values(source_id, publication_key, slot=SLOT, text="Draft."):
    return {
        "text": text,
        "category": "gym_strategy",
        "source_ids": [source_id],
        "score_data": {"total": 90},
        "intended_slot": slot,
        "publication_key": publication_key,
    }


def test_concurrent_create_or_get_claims_one_live_draft_and_one_audit(tmp_path):
    path = str(tmp_path / "bot.db")
    setup = Database(path)
    source_id = _source(setup)
    databases = [Database(path), Database(path)]
    barrier = threading.Barrier(2)
    results = []

    def worker(index):
        barrier.wait()
        results.append(
            databases[index].create_or_get_post_draft(
                **_draft_values(source_id, f"concurrent-{index}")
            )
        )

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(created for _, created in results) == [False, True]
    assert len({draft["id"] for draft, _ in results}) == 1
    with setup._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM post_drafts WHERE intended_slot = ? "
            "AND status IN ('pending_approval', 'approved', 'publishing', "
            "'published', 'publication_unknown')",
            (SLOT,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM draft_evaluations "
            "WHERE outcome = 'pending_approval'"
        ).fetchone()[0] == 1


def test_database_unique_index_rejects_a_second_live_draft_for_slot(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    source_id = _source(db)
    db.create_post_draft(**_draft_values(source_id, "first-live"))

    with pytest.raises(sqlite3.IntegrityError):
        db.create_post_draft(**_draft_values(source_id, "second-live"))


def test_schema_migration_reconciles_legacy_duplicate_live_slots(tmp_path):
    path = str(tmp_path / "bot.db")
    db = Database(path)
    source_id = _source(db)
    with db._conn() as conn:
        conn.execute("DROP INDEX uq_post_drafts_live_intended_slot")
    first_id = db.create_post_draft(**_draft_values(source_id, "legacy-first"))
    second_id = db.create_post_draft(**_draft_values(source_id, "legacy-second"))

    migrated = Database(path)

    assert migrated.get_active_draft_for_slot(SLOT)["id"] == first_id
    assert migrated.get_post_draft(second_id)["status"] == "superseded"
    with migrated._conn() as conn:
        evaluation = conn.execute(
            "SELECT outcome, details_json FROM draft_evaluations "
            "WHERE outcome = 'migration_duplicate_slot'"
        ).fetchone()
    assert json.loads(evaluation["details_json"]) == {
        "draft_id": second_id,
        "kept_draft_id": first_id,
    }


def test_concurrent_postpone_uses_revision_cas_without_last_writer_wins(tmp_path):
    path = str(tmp_path / "bot.db")
    setup = Database(path)
    source_id = _source(setup)
    draft_id = setup.create_post_draft(
        **_draft_values(source_id, "postpone-source")
    )
    revision = setup.get_post_draft(draft_id)["revision"]
    targets = [
        "2030-01-11T12:00:00+00:00",
        "2030-01-12T12:00:00+00:00",
    ]
    databases = [Database(path), Database(path)]
    barrier = threading.Barrier(2)
    results = []

    def worker(index):
        barrier.wait()
        results.append(
            databases[index].postpone_post_draft_atomic(
                draft_id,
                revision,
                ["pending_approval", "approved", "expired"],
                targets[index],
            )
        )

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [False, True]
    stored = setup.get_post_draft(draft_id)
    assert stored["intended_slot"] in targets
    assert stored["revision"] == revision + 1


def test_postpone_to_claimed_slot_returns_false_without_overwrite(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    source_id = _source(db)
    claimed_id = db.create_post_draft(
        **_draft_values(source_id, "claimed-slot")
    )
    moving_slot = "2030-01-09T12:00:00+00:00"
    moving_id = db.create_post_draft(
        **_draft_values(source_id, "moving-slot", moving_slot)
    )
    moving = db.get_post_draft(moving_id)

    assert db.postpone_post_draft_atomic(
        moving_id,
        moving["revision"],
        ["pending_approval"],
        SLOT,
    ) is False

    assert db.get_post_draft(moving_id)["intended_slot"] == moving_slot
    assert db.get_active_draft_for_slot(SLOT)["id"] == claimed_id


def test_atomic_regenerate_rolls_back_old_draft_audit_and_media_on_insert_error(
    tmp_path,
):
    db = Database(str(tmp_path / "bot.db"))
    source_id = _source(db)
    draft_id = db.create_post_draft(
        **_draft_values(source_id, "old-key", text="Old draft.")
    )
    media_id = db.add_media("photo.jpg", "/tmp/photo.jpg", "image")
    assert db.transition_post_draft(
        draft_id,
        ["pending_approval"],
        "pending_approval",
        media_id=media_id,
    )
    assert db.reserve_media(media_id, draft_id)
    prior = db.get_post_draft(draft_id)
    with db._conn() as conn:
        conn.execute("""
            CREATE TRIGGER fail_replacement
            BEFORE INSERT ON post_drafts
            WHEN NEW.publication_key = 'replacement-fails'
            BEGIN
                SELECT RAISE(ABORT, 'injected insert failure');
            END
        """)

    with pytest.raises(sqlite3.IntegrityError):
        db.replace_post_draft_atomic(
            prior_draft_id=draft_id,
            expected_revision=prior["revision"],
            expected_slot=prior["intended_slot"],
            expected_category=prior["category"],
            expected_source_ids=prior["source_ids"],
            text="Replacement.",
            score_data={"total": 91},
            publication_key="replacement-fails",
        )

    restored = db.get_post_draft(draft_id)
    assert restored["status"] == "pending_approval"
    assert restored["revision"] == prior["revision"]
    assert db.get_media_by_id(media_id)["lifecycle_state"] == "reserved"
    with db._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM post_drafts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM draft_evaluations").fetchone()[0] == 0


@pytest.mark.parametrize("concurrent_action", ["approve", "postpone"])
def test_atomic_regenerate_does_not_undo_a_concurrent_transition(
    tmp_path, concurrent_action
):
    db = Database(str(tmp_path / "bot.db"))
    source_id = _source(db)
    draft_id = db.create_post_draft(
        **_draft_values(source_id, f"old-{concurrent_action}")
    )
    prior = db.get_post_draft(draft_id)
    if concurrent_action == "approve":
        assert db.transition_post_draft(
            draft_id,
            ["pending_approval"],
            "approved",
            approved_at=datetime.now(timezone.utc).isoformat(),
            approved_by="floriano",
        )
    else:
        assert db.postpone_post_draft_atomic(
            draft_id,
            prior["revision"],
            ["pending_approval"],
            "2030-01-11T12:00:00+00:00",
        )

    replacement, outcome = db.replace_post_draft_atomic(
        prior_draft_id=draft_id,
        expected_revision=prior["revision"],
        expected_slot=prior["intended_slot"],
        expected_category=prior["category"],
        expected_source_ids=prior["source_ids"],
        text="Replacement.",
        score_data={"total": 91},
        publication_key=f"replacement-{concurrent_action}",
    )

    assert replacement is None
    assert outcome == "conflict"
    current = db.get_post_draft(draft_id)
    assert current["status"] == (
        "approved" if concurrent_action == "approve" else "pending_approval"
    )
    assert current["revision"] == prior["revision"] + 1
    with db._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM post_drafts").fetchone()[0] == 1


class _NeverPlanner:
    def plan(self, _slot):
        raise AssertionError("regeneration must not invoke the planner")


class _TrackingGenerator:
    def __init__(self):
        self.calls = 0

    def generate_grounded_tweet(self, *_args):
        self.calls += 1
        return {"text": "Generated."}


@pytest.mark.parametrize("invalidity", ["expired", "untrusted", "malformed_expiry"])
def test_regenerate_rejects_a_source_that_is_no_longer_eligible(
    tmp_path, invalidity
):
    db = Database(str(tmp_path / "bot.db"))
    if invalidity == "expired":
        source_id = db.add_content_source(
            source_type="product_fact",
            text="An expired product fact.",
            verified_by="floriano",
            verified_at=(
                datetime.now(timezone.utc) - timedelta(days=91)
            ).isoformat(),
        )
    else:
        source_id = _source(db)
        with db._conn() as conn:
            if invalidity == "untrusted":
                conn.execute(
                    "UPDATE content_sources SET trust_state = 'pending' "
                    "WHERE id = ?",
                    (source_id,),
                )
            else:
                conn.execute(
                    "UPDATE content_sources SET expires_at = 'not-a-date' "
                    "WHERE id = ?",
                    (source_id,),
                )
    draft_id = db.create_post_draft(
        **_draft_values(source_id, "ineligible-source")
    )
    generator = _TrackingGenerator()
    pipeline = DraftPipeline(
        db,
        _NeverPlanner(),
        generator,
        object(),
        object(),
    )

    assert pipeline.regenerate(draft_id) is None

    assert generator.calls == 0
    assert db.get_post_draft(draft_id)["status"] == "pending_approval"
    with db._conn() as conn:
        evaluation = conn.execute(
            "SELECT outcome, details_json FROM draft_evaluations "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert evaluation["outcome"] == "no_eligible_source"
    assert json.loads(evaluation["details_json"]) == {
        "source_ids": [source_id]
    }
