import json
import re
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from modules.database import Database
from scripts import publish_once
from scripts.publish_once import (
    draft_fingerprint,
    inspect_draft,
    publish_exact_draft,
)


REPOSITORY_ROOT = Path(__file__).parents[1]
SLOT = datetime.fromisoformat("2030-01-10T14:00:00+01:00")


def test_publish_once_script_is_importable_when_invoked_by_path():
    result = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "scripts" / "publish_once.py"),
            "--help",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "ModuleNotFoundError" not in result.stderr


def approved_snapshot():
    return {
        "id": 7,
        "revision": 2,
        "publication_key": "draft:one-shot",
        "text": "A useful, approved post.",
        "category": "gym_strategy",
        "source_ids": [1],
        "score_data": {"total": 88, "hook": 9},
        "intended_slot": SLOT.isoformat(),
        "media_id": None,
        "approved_at": (SLOT - timedelta(minutes=30)).isoformat(),
        "approved_by": "floriano",
        "status": "approved",
        "published_tweet_id": None,
    }


def test_fingerprint_is_stable_and_binds_every_immutable_field():
    draft = approved_snapshot()
    fingerprint = draft_fingerprint(draft)

    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint)
    assert fingerprint == draft_fingerprint(dict(draft))

    mutations = {
        "id": 8,
        "revision": 3,
        "publication_key": "draft:changed",
        "text": "Changed text.",
        "category": "shareable_fitness",
        "source_ids": [2],
        "score_data": {"total": 89},
        "intended_slot": (SLOT + timedelta(hours=1)).isoformat(),
        "media_id": 3,
        "approved_at": (SLOT - timedelta(minutes=20)).isoformat(),
        "approved_by": "another-operator",
        "status": "published",
        "published_tweet_id": "9001",
    }
    for field, value in mutations.items():
        changed = deepcopy(draft)
        changed[field] = value
        if field in {"status", "published_tweet_id"}:
            with pytest.raises(ValueError, match="^invalid_draft_snapshot$"):
                draft_fingerprint(changed)
        else:
            assert draft_fingerprint(changed) != fingerprint


@pytest.mark.parametrize(
    "mutation",
    [
        {"id": True},
        {"revision": 0},
        {"source_ids": [True]},
        {"source_ids": [1, 1]},
        {"score_data": {"total": float("nan")}},
        {"media_id": "3"},
        {"approved_by": ""},
    ],
)
def test_fingerprint_rejects_malformed_snapshot_without_coercion(mutation):
    draft = approved_snapshot()
    draft.update(mutation)

    with pytest.raises(ValueError, match="^invalid_draft_snapshot$"):
        draft_fingerprint(draft)


def test_fingerprint_rejects_cycles_and_hostile_values_without_stringifying():
    class Hostile:
        def __str__(self):
            raise AssertionError("hostile __str__ called")

    cyclic = approved_snapshot()
    cyclic["score_data"]["cycle"] = cyclic["score_data"]
    hostile = approved_snapshot()
    hostile["score_data"]["reason"] = Hostile()

    for draft in (cyclic, hostile):
        with pytest.raises(ValueError, match="^invalid_draft_snapshot$"):
            draft_fingerprint(draft)


class SnapshotDatabase:
    def __init__(self, draft):
        self.draft = draft
        self.reads = 0

    def get_post_draft(self, draft_id):
        self.reads += 1
        return deepcopy(self.draft) if draft_id == self.draft["id"] else None


def test_inspect_returns_only_bounded_metadata():
    database = SnapshotDatabase(approved_snapshot())

    result = inspect_draft(database, 7)

    assert result == {
        "draft_id": 7,
        "revision": 2,
        "intended_slot": SLOT.isoformat(),
        "score_total": 88,
        "has_media": False,
        "fingerprint": draft_fingerprint(approved_snapshot()),
    }
    rendered = repr(result)
    assert "A useful" not in rendered
    assert "floriano" not in rendered
    assert database.reads == 1


@pytest.mark.parametrize("draft_id", [True, 0, -1, "7", None])
def test_inspect_rejects_noncanonical_or_missing_draft_ids(draft_id):
    database = SnapshotDatabase(approved_snapshot())

    with pytest.raises(ValueError, match="^(invalid_draft_id|draft_not_found)$"):
        inspect_draft(database, draft_id)


def _approved_sqlite_draft(database):
    database.set_state("paused", "false")
    source_id = database.add_content_source(
        "evergreen_idea",
        "A verified source.",
    )
    draft_id = database.create_post_draft(
        text="A useful, approved post.",
        category="gym_strategy",
        source_ids=[source_id],
        score_data={"total": 88},
        intended_slot=SLOT.isoformat(),
        publication_key="draft:one-shot-sqlite",
    )
    assert database.transition_post_draft(
        draft_id,
        ["pending_approval"],
        "approved",
        approved_at=(SLOT - timedelta(minutes=30)).isoformat(),
        approved_by="floriano",
    )
    return draft_id


class RecordingX:
    def __init__(self):
        self.posts = []

    def post_tweet(self, text, **_kwargs):
        self.posts.append(text)
        return SimpleNamespace(data={"id": "9001"})


def test_publish_exact_draft_writes_once_and_second_call_never_retries(tmp_path):
    database = Database(tmp_path / "publish-once.db")
    draft_id = _approved_sqlite_draft(database)
    inspected = inspect_draft(database, draft_id)
    x_client = RecordingX()

    first = publish_exact_draft(
        database,
        x_client,
        draft_id,
        inspected["fingerprint"],
        SLOT,
    )
    second = publish_exact_draft(
        database,
        x_client,
        draft_id,
        inspected["fingerprint"],
        SLOT,
    )

    assert first.status == "published"
    assert first.tweet_id == "9001"
    assert second.status == "snapshot_changed"
    assert x_client.posts == ["A useful, approved post."]


def test_publish_exact_draft_wrong_fingerprint_or_revision_race_never_calls_x(
    tmp_path,
):
    path = tmp_path / "publish-race.db"
    setup = Database(path)
    draft_id = _approved_sqlite_draft(setup)
    inspected = inspect_draft(setup, draft_id)
    x_client = RecordingX()

    wrong = publish_exact_draft(
        setup,
        x_client,
        draft_id,
        "0" * 64,
        SLOT,
    )
    assert wrong.status == "snapshot_changed"

    class MutatingReadDatabase(Database):
        def __init__(self, db_path):
            self.reads = 0
            super().__init__(db_path)

        def get_post_draft(self, current_draft_id):
            self.reads += 1
            if self.reads == 2:
                assert self.transition_post_draft(
                    current_draft_id,
                    ["approved"],
                    "approved",
                    text="Changed after fingerprint validation.",
                )
            return super().get_post_draft(current_draft_id)

    raced = publish_exact_draft(
        MutatingReadDatabase(path),
        x_client,
        draft_id,
        inspected["fingerprint"],
        SLOT,
    )

    assert raced.status == "snapshot_changed"
    assert x_client.posts == []


def test_cli_validates_confirmation_and_mode_before_constructing_boundaries(
    monkeypatch,
    capsys,
):
    constructed = []
    monkeypatch.setattr(publish_once, "validate_config", lambda: None)
    monkeypatch.setattr(publish_once, "DRY_RUN", True)
    monkeypatch.setattr(publish_once, "APPROVAL_REQUIRED", True)

    code = publish_once.run_cli(
        [
            "publish",
            "--draft-id", "7",
            "--fingerprint", "0" * 64,
            "--confirm", "wrong",
        ],
        database_factory=lambda: constructed.append("db"),
        x_client_factory=lambda: constructed.append("x"),
    )

    assert code == 4
    assert constructed == []
    output = capsys.readouterr().out
    assert "invalid_confirmation" in output
    assert "0" * 64 not in output
