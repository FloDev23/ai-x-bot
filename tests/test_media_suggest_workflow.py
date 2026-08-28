"""Media → auto-suggest post workflow tests."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from modules.database import Database
from modules.telegram_controller import TelegramController


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

class FakeTelegram:
    def __init__(self, media_library_dir):
        self.media_library_dir = Path(media_library_dir)
        self.messages = []
        self.callback_answers = []
        self.get_file_calls = []
        self.downloads = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((str(chat_id), text, kwargs))
        return {"message_id": len(self.messages)}

    def answer_callback(self, callback_id, **kwargs):
        self.callback_answers.append((callback_id, kwargs))
        return True

    def get_file(self, file_id):
        self.get_file_calls.append(file_id)
        return {
            "file_id": file_id,
            "file_path": f"photos/{file_id}.jpg",
            "file_size": 6,
        }

    def download_file(self, file_path, destination, **kwargs):
        destination.write_bytes(b"FAKE")
        self.downloads.append({"destination": destination, **kwargs})
        return destination


class FakeMediaProcessor:
    def __init__(self, media_id=44, user_context=""):
        self.media_id = media_id
        self._user_context = user_context
        self.calls = []

    def process_new_file(self, filepath, filename, mime_type, file_size, user_context):
        self.calls.append(user_context)
        return {
            "id": self.media_id,
            "lifecycle_state": "available",
            "ai_description": "Gym interior",
            "ai_tags": "gym,fitness",
            "user_context": user_context,
        }


class FakeNotifier:
    def notify_error(self, *_args):
        return None


class FakePipeline:
    def __init__(self, *, generate_text="Generated gym post.", fail=False):
        self._text = generate_text
        self._fail = fail
        self.suggest_calls = []

    def suggest_from_media_context(self, context, category):
        self.suggest_calls.append((context, category))
        if self._fail:
            return None
        return self._text


def _photo_update(update_id, caption=None):
    msg = {
        "chat": {"id": 42},
        "photo": [{"file_id": "photo-abc", "file_unique_id": "uq-abc", "width": 800, "height": 600, "file_size": 6}],
    }
    if caption is not None:
        msg["caption"] = caption
    return {"update_id": update_id, "message": msg}


def _callback(update_id, data, chat_id=42):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "message": {"chat": {"id": chat_id}},
            "data": data,
        },
    }


def _controller(tmp_path, pipeline=None, media_processor=None):
    root = tmp_path / "media"
    root.mkdir(mode=0o700, exist_ok=True)
    db = Database(str(tmp_path / "test.db"))
    telegram = FakeTelegram(root)
    ctrl = TelegramController(
        telegram_api=telegram,
        db=db,
        notifier=FakeNotifier(),
        authorized_chat_id="42",
        draft_pipeline=pipeline,
        media_processor=media_processor or FakeMediaProcessor(),
        dry_run=True,
        now_fn=lambda: datetime(2020, 6, 1, 10, 0, tzinfo=timezone.utc),
    )
    return ctrl, db, telegram


# ---------------------------------------------------------------------------
# DB: create_media_suggested_draft
# ---------------------------------------------------------------------------

def test_create_media_suggested_draft_creates_pending_advisory_draft(tmp_path):
    db = Database(str(tmp_path / "msugg.db"))
    draft, outcome = db.create_media_suggested_draft(
        text="Gym owners who simplify booking see 30% more retention.",
        category="gym_strategy",
        publication_key="media-suggest-test-001",
        intended_slot="2025-09-01T12:00:00+00:00",
    )
    assert outcome == "created"
    assert draft is not None
    assert draft["text"] == "Gym owners who simplify booking see 30% more retention."
    assert draft["category"] == "gym_strategy"
    assert draft["origin"] == "media_suggested"
    assert draft["status"] == "pending_approval"
    assert draft["source_ids"] == []


def test_create_media_suggested_draft_idempotent_on_same_key(tmp_path):
    db = Database(str(tmp_path / "msugg-idem.db"))
    _, o1 = db.create_media_suggested_draft(
        text="First version.",
        category="gym_strategy",
        publication_key="media-suggest-idempotent",
        intended_slot="2025-09-01T12:00:00+00:00",
    )
    _, o2 = db.create_media_suggested_draft(
        text="Second version.",
        category="gym_strategy",
        publication_key="media-suggest-idempotent",
        intended_slot="2025-09-01T12:00:00+00:00",
    )
    assert o1 == "created"
    assert o2 == "existing"


def test_create_media_suggested_draft_rejects_invalid_inputs(tmp_path):
    db = Database(str(tmp_path / "msugg-invalid.db"))
    _, o = db.create_media_suggested_draft(
        text="", category="gym_strategy",
        publication_key="k1", intended_slot="2025-09-01T12:00:00+00:00",
    )
    assert o == "rejected"
    _, o = db.create_media_suggested_draft(
        text="Valid text", category="invalid category!",
        publication_key="k2", intended_slot="2025-09-01T12:00:00+00:00",
    )
    assert o == "rejected"
    _, o = db.create_media_suggested_draft(
        text="Valid text", category="gym_strategy",
        publication_key="k3", intended_slot="not-a-date",
    )
    assert o == "rejected"


# ---------------------------------------------------------------------------
# DB: media_suggested origin passes queue validation
# ---------------------------------------------------------------------------

def test_media_suggested_draft_enters_editorial_queue_with_advisory_policy(tmp_path):
    db = Database(str(tmp_path / "msugg-queue.db"))
    draft, outcome = db.create_media_suggested_draft(
        text="Simple booking, loyal clients.",
        category="gym_strategy",
        publication_key="media-suggest-queue-test",
        intended_slot="2025-09-01T12:00:00+00:00",
    )
    assert outcome == "created"
    queue = db.get_queue_draft(draft["id"])
    assert queue is not None
    assert queue["origin"] == "media_suggested"
    assert queue["source_ids"] == []
    assert queue["translation_policy"] == "advisory"
    assert queue["translation_status"] == "pending"


def test_media_suggested_approve_queued_atomic_succeeds_after_translation(tmp_path):
    db = Database(str(tmp_path / "msugg-approve.db"))
    draft, _ = db.create_media_suggested_draft(
        text="Simple booking, loyal clients.",
        category="gym_strategy",
        publication_key="media-suggest-approve-test",
        intended_slot="2025-01-01T12:00:00+00:00",
    )
    assert db.save_review_translation(draft["id"], draft["revision"], "Prenotazione semplice, clienti fedeli.")
    ready = db.get_queue_draft(draft["id"])
    assert ready["translation_status"] == "ready"

    approved = db.approve_queued_draft_atomic(
        draft["id"],
        ready["revision"],
        ready["queue_revision"],
        "floriano",
        datetime.now(timezone.utc).isoformat(),
    )
    assert approved, "media_suggested with source_ids=[] must be approvable"
    final = db.get_queue_draft(draft["id"])
    assert final["status"] == "approved"


# ---------------------------------------------------------------------------
# Pipeline: suggest_from_media_context
# ---------------------------------------------------------------------------

def test_suggest_from_media_context_rejects_invalid_inputs(tmp_path):
    """suggest_from_media_context returns None for bad inputs without calling generator."""
    from modules.draft_pipeline import DraftPipeline

    class StrictGenerator:
        def generate_from_media_context(self, context, category):
            raise AssertionError("should not be called with invalid input")

    pipeline = DraftPipeline.__new__(DraftPipeline)
    pipeline.generator = StrictGenerator()

    assert pipeline.suggest_from_media_context("", "gym_strategy") is None
    assert pipeline.suggest_from_media_context("  ", "gym_strategy") is None
    assert pipeline.suggest_from_media_context("valid", "invalid_category!") is None
    assert pipeline.suggest_from_media_context("valid", "nonexistent_category") is None
    assert pipeline.suggest_from_media_context("x" * 501, "gym_strategy") is None


def test_suggest_from_media_context_calls_generator_and_returns_text(tmp_path):
    from modules.draft_pipeline import DraftPipeline

    calls = []

    class TrackingGenerator:
        def generate_from_media_context(self, context, category):
            calls.append((context, category))
            return "Simplicity sells memberships."

    pipeline = DraftPipeline.__new__(DraftPipeline)
    pipeline.generator = TrackingGenerator()

    result = pipeline.suggest_from_media_context("New gym floor revealed.", "gym_strategy")
    assert result == "Simplicity sells memberships."
    assert calls == [("New gym floor revealed.", "gym_strategy")]


def test_suggest_from_media_context_handles_generator_exception(tmp_path):
    from modules.draft_pipeline import DraftPipeline

    class BrokenGenerator:
        def generate_from_media_context(self, context, category):
            raise RuntimeError("network error")

    pipeline = DraftPipeline.__new__(DraftPipeline)
    pipeline.generator = BrokenGenerator()

    assert pipeline.suggest_from_media_context("context", "gym_strategy") is None


# ---------------------------------------------------------------------------
# Controller: _ingest_media category prompt
# ---------------------------------------------------------------------------

def test_upload_without_caption_shows_no_suggestion_prompt(tmp_path):
    pipeline = FakePipeline()
    ctrl, db, telegram = _controller(tmp_path, pipeline=pipeline)
    result = ctrl.process_update(_photo_update(1, caption=None))
    assert result == "processed"
    texts = [m[1] for m in telegram.messages]
    assert not any("Vuoi generare" in t for t in texts)


def test_upload_with_empty_caption_shows_no_suggestion_prompt(tmp_path):
    pipeline = FakePipeline()
    ctrl, db, telegram = _controller(tmp_path, pipeline=pipeline)
    result = ctrl.process_update(_photo_update(1, caption=""))
    assert result == "processed"
    texts = [m[1] for m in telegram.messages]
    assert not any("Vuoi generare" in t for t in texts)


def test_upload_without_pipeline_shows_no_suggestion_prompt(tmp_path):
    ctrl, db, telegram = _controller(tmp_path, pipeline=None)
    result = ctrl.process_update(_photo_update(1, caption="New gym floor"))
    assert result == "processed"
    texts = [m[1] for m in telegram.messages]
    assert not any("Vuoi generare" in t for t in texts)


def test_upload_with_caption_and_pipeline_shows_category_buttons(tmp_path):
    pipeline = FakePipeline()
    ctrl, db, telegram = _controller(tmp_path, pipeline=pipeline)
    result = ctrl.process_update(_photo_update(1, caption="New gym floor"))
    assert result == "processed"

    prompt_msg = next(m for m in telegram.messages if "Vuoi generare" in m[1])
    markup = prompt_msg[2]["reply_markup"]["inline_keyboard"]
    button_data = [btn["callback_data"] for row in markup for btn in row]
    assert any(d.startswith("msugg:44:") for d in button_data)
    assert "msugg:skip" in button_data


# ---------------------------------------------------------------------------
# Controller: msugg callbacks
# ---------------------------------------------------------------------------

def test_msugg_skip_returns_skipped(tmp_path):
    pipeline = FakePipeline()
    ctrl, db, telegram = _controller(tmp_path, pipeline=pipeline)
    result = ctrl.process_update(_callback(1, "msugg:skip"))
    assert result == "processed"


def _add_media_with_user_context(db, filename, media_type, context):
    media_id = db.add_media(filename, f"/tmp/{filename}", media_type)
    with db._conn() as conn:
        conn.execute("UPDATE media_library SET user_context = ? WHERE id = ?", (context, media_id))
    return media_id


def test_msugg_generate_stores_text_and_shows_confirm_buttons(tmp_path):
    pipeline = FakePipeline(generate_text="Simple booking keeps clients coming back.")
    ctrl, db, telegram = _controller(tmp_path, pipeline=pipeline)

    db.set_state("media_suggest_context_test", "New gym floor unveiled.")
    media_id = _add_media_with_user_context(db, "test.jpg", "image", "New gym floor unveiled.")

    result = ctrl.process_update(_callback(1, f"msugg:{media_id}:gym_strategy"))
    assert result == "processed"

    texts = [m[1] for m in telegram.messages]
    assert any("Simple booking keeps clients coming back." in t for t in texts)
    assert any("Conferma" in t or "Rigenera" in t for t in texts)

    stored = db.get_state(f"media_suggest:{media_id}:gym_strategy")
    assert stored == "Simple booking keeps clients coming back."
    assert pipeline.suggest_calls == [("New gym floor unveiled.", "gym_strategy")]


def test_msugg_generate_with_no_user_context_returns_error(tmp_path):
    pipeline = FakePipeline()
    ctrl, db, telegram = _controller(tmp_path, pipeline=pipeline)
    media_id = db.add_media("bare.jpg", "/tmp/bare.jpg", "image")

    result = ctrl.process_update(_callback(1, f"msugg:{media_id}:gym_strategy"))
    assert result == "processed"

    texts = [m[1] for m in telegram.messages]
    assert any("didascalia" in t.lower() or "non disponibile" in t.lower() for t in texts)
    assert pipeline.suggest_calls == []


def test_msugg_generate_failure_sends_error_message(tmp_path):
    pipeline = FakePipeline(fail=True)
    ctrl, db, telegram = _controller(tmp_path, pipeline=pipeline)
    media_id = _add_media_with_user_context(db, "fail.jpg", "image", "Photo context")

    result = ctrl.process_update(_callback(1, f"msugg:{media_id}:gym_strategy"))
    assert result == "processed"

    texts = [m[1] for m in telegram.messages]
    assert any("non riuscita" in t.lower() for t in texts)


def test_msugg_confirm_creates_draft_and_sends_card(tmp_path):
    pipeline = FakePipeline()
    ctrl, db, telegram = _controller(tmp_path, pipeline=pipeline)

    media_id = _add_media_with_user_context(db, "gym.jpg", "image", "Studio reveal")
    db.set_state(f"media_suggest:{media_id}:gym_strategy", "Every empty slot is lost revenue.")

    result = ctrl.process_update(_callback(1, f"msugg:ok:{media_id}:gym_strategy"))
    assert result == "processed"

    drafts = db.list_post_drafts()
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["text"] == "Every empty slot is lost revenue."
    assert draft["category"] == "gym_strategy"
    assert draft["origin"] == "media_suggested"
    assert draft["source_ids"] == []

    texts = [m[1] for m in telegram.messages]
    assert any("Bozza creata" in t for t in texts)


def test_msugg_confirm_without_state_returns_error(tmp_path):
    pipeline = FakePipeline()
    ctrl, db, telegram = _controller(tmp_path, pipeline=pipeline)

    result = ctrl.process_update(_callback(1, "msugg:ok:99:gym_strategy"))
    assert result == "processed"

    texts = [m[1] for m in telegram.messages]
    assert any("non più disponibile" in t.lower() or "Rigenera" in t for t in texts)
    assert db.list_post_drafts() == []


def test_msugg_invalid_category_returns_invalid_callback(tmp_path):
    pipeline = FakePipeline()
    ctrl, db, telegram = _controller(tmp_path, pipeline=pipeline)

    result = ctrl.process_update(_callback(1, "msugg:44:BADCATEGORY"))
    assert result == "processed"
    texts = [m[1] for m in telegram.messages]
    assert any("non valida" in t.lower() for t in texts)
