import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from modules.database import Database
from modules.content_planner import ContentPlan
from modules.draft_pipeline import DraftPipeline
from modules.fact_guard import FactCheckResult


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

    assert sorted(outcome for _, outcome in results) == ["created", "existing"]
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
    kept_media_id = db.add_media("kept.jpg", "/tmp/kept.jpg", "image")
    stale_media_id = db.add_media("stale.jpg", "/tmp/stale.jpg", "image")
    assert db.transition_post_draft(
        first_id,
        ["pending_approval"],
        "pending_approval",
        media_id=kept_media_id,
    )
    assert db.transition_post_draft(
        second_id,
        ["pending_approval"],
        "pending_approval",
        media_id=stale_media_id,
    )
    assert db.reserve_media(kept_media_id, first_id)
    assert db.reserve_media(stale_media_id, second_id)

    migrated = Database(path)

    assert migrated.get_active_draft_for_slot(SLOT)["id"] == first_id
    assert migrated.get_post_draft(second_id)["status"] == "superseded"
    assert migrated.get_media_by_id(kept_media_id)["lifecycle_state"] == "reserved"
    stale_media = migrated.get_media_by_id(stale_media_id)
    assert stale_media["lifecycle_state"] == "available"
    assert stale_media["reserved_by_draft_id"] is None
    with migrated._conn() as conn:
        evaluation = conn.execute(
            "SELECT outcome, details_json FROM draft_evaluations "
            "WHERE outcome = 'migration_duplicate_slot'"
        ).fetchone()
    assert json.loads(evaluation["details_json"]) == {
        "draft_id": second_id,
        "kept_draft_id": first_id,
    }


class _FixedPlanner:
    def __init__(self, source_id):
        self.source_id = source_id

    def plan(self, intended_slot):
        return ContentPlan(
            category="gym_strategy",
            source_ids=[self.source_id],
            intended_slot=intended_slot,
            include_link=False,
        )


class _InvalidatingGenerator:
    def __init__(self, db, source_id, invalidity):
        self.db = db
        self.source_id = source_id
        self.invalidity = invalidity
        self.candidate_indices = []

    def generate_grounded_tweet(
        self, _category, _sources, _include_link, candidate_index=None
    ):
        self.candidate_indices.append(candidate_index)
        with self.db._conn() as conn:
            if self.invalidity == "revoked":
                conn.execute(
                    "UPDATE content_sources SET trust_state = 'pending' "
                    "WHERE id = ?",
                    (self.source_id,),
                )
            else:
                expires_at = (
                    "2000-01-01T00:00:00+00:00"
                    if self.invalidity == "expired"
                    else "not-a-date"
                )
                conn.execute(
                    "UPDATE content_sources SET expires_at = ? WHERE id = ?",
                    (expires_at, self.source_id),
                )
        return {"text": "A grounded draft that lost its source before insert."}


class _ApprovedGuard:
    def check(self, *_args):
        return FactCheckResult(True, [])


class _PassingScorer:
    def score_draft(self, _text, sources=None, recent_texts=None):
        del sources, recent_texts
        return {"total": 90}


class _IndexedGenerator:
    def __init__(self, texts, raw_reasoning):
        self.texts = tuple(texts)
        self.raw_reasoning = tuple(raw_reasoning)
        self.candidate_indices = []

    def generate_grounded_tweet(
        self, _category, _sources, _include_link, candidate_index=None
    ):
        self.candidate_indices.append(candidate_index)
        return {
            "text": self.texts[candidate_index],
            "raw_reasoning": self.raw_reasoning[candidate_index],
        }


class _TextScorer:
    def __init__(self, scores):
        self.scores = dict(scores)

    def score_draft(self, text, sources=None, recent_texts=None):
        del sources, recent_texts
        return dict(self.scores[text])


def _candidate_pipeline(db, source_id):
    texts = (
        "Candidate one stays out of storage.",
        "Candidate two is the SQLite winner.",
        "Candidate three stays out of storage.",
    )
    raw_reasoning = (
        "PRIVATE_GENERATOR_REASONING_LOSER_ONE",
        "PRIVATE_GENERATOR_REASONING_WINNER",
        "PRIVATE_GENERATOR_REASONING_LOSER_THREE",
    )
    scorer_reasoning = (
        "PRIVATE_SCORER_REASONING_LOSER_ONE",
        "PRIVATE_SCORER_REASONING_WINNER",
        "PRIVATE_SCORER_REASONING_LOSER_THREE",
    )
    scores = {
        texts[0]: {
            "total": 78,
            "hook": 7,
            "reasoning": scorer_reasoning[0],
        },
        texts[1]: {
            "total": 93,
            "hook": 10,
            "reasoning": scorer_reasoning[1],
        },
        texts[2]: {
            "total": 86,
            "hook": 8,
            "reasoning": scorer_reasoning[2],
        },
    }
    generator = _IndexedGenerator(texts, raw_reasoning)
    pipeline = DraftPipeline(
        db,
        _FixedPlanner(source_id),
        generator,
        _ApprovedGuard(),
        _TextScorer(scores),
    )
    return (
        pipeline,
        generator,
        texts,
        raw_reasoning + scorer_reasoning,
    )


def test_candidate_tournament_persists_only_sqlite_winner_without_reasoning(
    tmp_path,
):
    db = Database(str(tmp_path / "bot.db"))
    source_id = _source(db)
    pipeline, generator, texts, private_reasoning = (
        _candidate_pipeline(db, source_id)
    )

    draft = pipeline.create_for_slot(datetime.fromisoformat(SLOT))

    assert draft["status"] == "pending_approval"
    assert generator.candidate_indices == [0, 1, 2]
    with db._conn() as conn:
        stored = conn.execute(
            "SELECT * FROM post_drafts "
            "WHERE intended_slot = ?",
            (SLOT,),
        ).fetchall()
        evaluations = conn.execute(
            "SELECT * FROM draft_evaluations "
            "WHERE intended_slot = ?",
            (SLOT,),
        ).fetchall()
    assert len(stored) == 1
    raw_sqlite_payload = repr([dict(row) for row in stored + evaluations])
    assert all(value not in raw_sqlite_payload for value in private_reasoning)
    assert draft["text"] == texts[1]
    assert stored[0]["text"] == texts[1]
    assert draft["score_data"] == {"total": 93, "hook": 10}
    assert json.loads(stored[0]["score_json"]) == {"total": 93, "hook": 10}
    assert stored[0]["status"] == "pending_approval"
    assert [row["outcome"] for row in evaluations] == ["pending_approval"]
    assert texts[0] not in raw_sqlite_payload
    assert texts[2] not in raw_sqlite_payload


class _BarrierBeforePersistenceDatabase(Database):
    def __init__(self, path, persistence_barrier):
        self.persistence_barrier = persistence_barrier
        super().__init__(path)

    def create_or_get_post_draft(self, **values):
        self.persistence_barrier.wait(timeout=5)
        return super().create_or_get_post_draft(**values)


def test_candidate_tournament_concurrent_workers_claim_one_sqlite_draft(
    tmp_path,
):
    path = str(tmp_path / "bot.db")
    setup = Database(path)
    source_id = _source(setup)
    persistence_barrier = threading.Barrier(2)
    databases = [
        _BarrierBeforePersistenceDatabase(path, persistence_barrier)
        for _ in range(2)
    ]
    pipelines = [
        _candidate_pipeline(database, source_id)[0]
        for database in databases
    ]
    results = []
    errors = []

    def worker(index):
        try:
            results.append(
                pipelines[index].create_for_slot(datetime.fromisoformat(SLOT))
            )
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=worker, args=(index,), daemon=True)
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not [thread for thread in threads if thread.is_alive()]
    assert errors == []
    assert len(results) == 2
    assert len({draft["id"] for draft in results}) == 1
    assert {draft["text"] for draft in results} == {
        "Candidate two is the SQLite winner."
    }
    with setup._conn() as conn:
        drafts = conn.execute(
            "SELECT id, text, status FROM post_drafts WHERE intended_slot = ?",
            (SLOT,),
        ).fetchall()
        evaluations = conn.execute(
            "SELECT outcome FROM draft_evaluations WHERE intended_slot = ?",
            (SLOT,),
        ).fetchall()
    assert len(drafts) == 1
    assert drafts[0]["text"] == "Candidate two is the SQLite winner."
    assert drafts[0]["status"] == "pending_approval"
    assert [row["outcome"] for row in evaluations] == ["pending_approval"]


@pytest.mark.parametrize("invalidity", ["revoked", "expired", "malformed"])
def test_create_revalidates_source_inside_slot_claim_transaction(
    tmp_path, invalidity
):
    db = Database(str(tmp_path / "bot.db"))
    source_id = _source(db)
    pipeline = DraftPipeline(
        db,
        _FixedPlanner(source_id),
        _InvalidatingGenerator(db, source_id, invalidity),
        _ApprovedGuard(),
        _PassingScorer(),
    )

    assert pipeline.create_for_slot(datetime.fromisoformat(SLOT)) is None

    with db._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM post_drafts").fetchone()[0] == 0
        evaluation = conn.execute(
            "SELECT outcome, details_json FROM draft_evaluations "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert evaluation["outcome"] == "no_eligible_source"
    assert json.loads(evaluation["details_json"]) == {"source_ids": [source_id]}


class _PauseAfterDraftReadDatabase(Database):
    def __init__(self, path, read_complete, continue_approval):
        self.read_complete = read_complete
        self.continue_approval = continue_approval
        self.pause_next_read = False
        super().__init__(path)

    def get_post_draft(self, draft_id):
        draft = super().get_post_draft(draft_id)
        if self.pause_next_read:
            self.pause_next_read = False
            self.read_complete.set()
            assert self.continue_approval.wait(timeout=5)
        return draft


def test_approve_loses_revision_cas_to_concurrent_postpone(tmp_path):
    path = str(tmp_path / "bot.db")
    setup = Database(path)
    source_id = _source(setup)
    draft_id = setup.create_post_draft(
        **_draft_values(source_id, "approve-postpone-race")
    )
    stale = setup.get_post_draft(draft_id)
    read_complete = threading.Event()
    continue_approval = threading.Event()
    approving_db = _PauseAfterDraftReadDatabase(
        path, read_complete, continue_approval
    )
    approving_db.pause_next_read = True
    clock_calls = []

    def transaction_clock():
        clock_calls.append("called")
        return datetime(2029, 1, 1, tzinfo=timezone.utc)

    pipeline = DraftPipeline(
        approving_db,
        _NeverPlanner(),
        object(),
        object(),
        object(),
        now_fn=transaction_clock,
    )
    result = []
    thread = threading.Thread(
        target=lambda: result.append(pipeline.approve(draft_id, "floriano"))
    )
    thread.start()
    assert read_complete.wait(timeout=5)
    assert clock_calls == []
    assert setup.postpone_post_draft_atomic(
        draft_id,
        stale["revision"],
        ["pending_approval"],
        "2030-01-11T12:00:00+00:00",
    )
    continue_approval.set()
    thread.join(timeout=5)

    assert result == [False]
    assert clock_calls == ["called"]
    current = setup.get_post_draft(draft_id)
    assert current["status"] == "pending_approval"
    assert current["intended_slot"] == "2030-01-11T12:00:00+00:00"
    assert current["approved_at"] is None
    assert current["approved_by"] is None


@pytest.mark.parametrize(
    "approval_time",
    [
        datetime.fromisoformat(SLOT),
        datetime.fromisoformat(SLOT) + timedelta(microseconds=1),
    ],
)
def test_atomic_approval_expires_at_or_after_slot(tmp_path, approval_time):
    db = Database(str(tmp_path / "bot.db"))
    source_id = _source(db)
    draft_id = db.create_post_draft(
        **_draft_values(source_id, "late-atomic-approval")
    )
    draft = db.get_post_draft(draft_id)

    assert db.approve_post_draft_atomic(
        draft_id,
        draft["revision"],
        draft["intended_slot"],
        "floriano",
        lambda: approval_time,
    ) is False

    assert db.get_post_draft(draft_id)["status"] == "expired"


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
        self.candidate_indices = []

    def generate_grounded_tweet(
        self, _category, _sources, _include_link, candidate_index=None
    ):
        self.calls += 1
        self.candidate_indices.append(candidate_index)
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
