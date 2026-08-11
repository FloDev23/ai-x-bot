import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from modules.database import Database
from modules.notifier import TelegramNotifier
from modules.telegram_api import (
    REQUEST_TIMEOUT,
    TELEGRAM_POLL_TIMEOUT,
    TelegramApi,
    TelegramApiError,
    sanitize_error,
)
from modules.telegram_controller import TelegramController


class FakeNotifier:
    def __init__(self):
        self.errors = []

    def notify_error(self, context, error):
        self.errors.append((context, error))


@pytest.fixture
def controller(fake_telegram, fake_db):
    def dispatch(update):
        fake_db.operational_mutations.append(update["update_id"])
        if "message" in update:
            fake_telegram.send_message(42, "ok")
        return "ok"

    return TelegramController(
        telegram_api=fake_telegram,
        db=fake_db,
        notifier=FakeNotifier(),
        authorized_chat_id="42",
        dispatcher=dispatch,
    )


def test_unknown_chat_cannot_read_or_mutate(controller, fake_telegram, fake_db):
    update = {"update_id": 10, "message": {"chat": {"id": 999}, "text": "/status"}}
    assert controller.process_update(update) == "unauthorized"
    assert fake_db.telegram_updates[10]["state"] == "unauthorized"
    assert fake_db.operational_mutations == []
    assert fake_telegram.messages == []


def test_replayed_update_is_ignored(controller, fake_telegram):
    update = {"update_id": 11, "message": {"chat": {"id": 42}, "text": "/pause"}}
    assert controller.process_update(update) == "processed"
    assert controller.process_update(update) == "duplicate"
    assert len(fake_telegram.messages) == 1


def test_callback_is_answered_once(controller, fake_telegram):
    update = {
        "update_id": 12,
        "callback_query": {
            "id": "callback-12",
            "from": {"id": 42},
            "message": {"chat": {"id": 42}},
            "data": "draft:approve:7",
        },
    }
    controller.process_update(update)
    assert fake_telegram.answered_callbacks == ["callback-12"]


def test_claim_is_visible_before_dispatch(fake_db, fake_telegram):
    observed_states = []

    def dispatch(update):
        observed_states.append(fake_db.telegram_updates[update["update_id"]]["state"])
        return "ok"

    local_controller = TelegramController(
        telegram_api=fake_telegram,
        db=fake_db,
        notifier=FakeNotifier(),
        authorized_chat_id="42",
        dispatcher=dispatch,
    )
    assert local_controller.process_update(
        {"update_id": 13, "message": {"chat": {"id": 42}, "text": "/status"}}
    ) == "processed"
    assert observed_states == ["processing"]


def test_unauthorized_callback_is_not_answered(controller, fake_telegram):
    update = {
        "update_id": 14,
        "callback_query": {
            "id": "callback-14",
            "from": {"id": 999},
            "message": {"chat": {"id": 999}},
            "data": "draft:approve:7",
        },
    }
    assert controller.process_update(update) == "unauthorized"
    assert fake_telegram.answered_callbacks == []


def test_callback_without_chat_context_is_unauthorized(controller, fake_telegram, fake_db):
    update = {
        "update_id": 141,
        "callback_query": {
            "id": "callback-141",
            "from": {"id": 42},
            "inline_message_id": "private-inline-context",
            "data": "draft:approve:7",
        },
    }
    assert controller.process_update(update) == "unauthorized"
    assert fake_db.telegram_updates[141]["chat_id"] == "None"
    assert fake_db.operational_mutations == []
    assert fake_telegram.answered_callbacks == []


def test_failed_callback_is_answered_once_with_failure_feedback(fake_db, fake_telegram):
    notifier = FakeNotifier()

    def fail_dispatch(_update):
        raise RuntimeError("dispatch failed")

    local_controller = TelegramController(
        telegram_api=fake_telegram,
        db=fake_db,
        notifier=notifier,
        authorized_chat_id="42",
        dispatcher=fail_dispatch,
    )
    update = {
        "update_id": 15,
        "callback_query": {
            "id": "callback-15",
            "message": {"chat": {"id": 42}},
            "data": "draft:approve:7",
        },
    }
    assert local_controller.process_update(update) == "failed"
    assert fake_telegram.callback_answers == [
        ("callback-15", {"text": "Operazione non riuscita."})
    ]
    assert fake_db.telegram_updates[15]["state"] == "failed"
    assert len(notifier.errors) == 1


def test_callback_answer_failure_is_not_retried(fake_db, fake_telegram):
    fake_telegram.callback_error = RuntimeError("answer unavailable")
    notifier = FakeNotifier()
    local_controller = TelegramController(
        telegram_api=fake_telegram,
        db=fake_db,
        notifier=notifier,
        authorized_chat_id="42",
        dispatcher=lambda _update: "ok",
    )
    update = {
        "update_id": 16,
        "callback_query": {
            "id": "callback-16",
            "message": {"chat": {"id": 42}},
            "data": "draft:approve:7",
        },
    }
    assert local_controller.process_update(update) == "failed"
    assert fake_telegram.answered_callbacks == ["callback-16"]
    assert fake_db.telegram_updates[16]["state"] == "failed"
    assert len(notifier.errors) == 1


class CompletionFailingDatabase:
    def __init__(self):
        self.claimed = set()
        self.complete_calls = 0

    def claim_telegram_update(self, update_id, _chat_id):
        if update_id in self.claimed:
            return False
        self.claimed.add(update_id)
        return True

    def complete_telegram_update(self, _update_id, _state, _result):
        self.complete_calls += 1
        raise RuntimeError("database unavailable")


def test_completion_failure_does_not_repeat_state_write_or_dispatch(fake_telegram):
    db = CompletionFailingDatabase()
    notifier = FakeNotifier()
    dispatched = []
    local_controller = TelegramController(
        telegram_api=fake_telegram,
        db=db,
        notifier=notifier,
        authorized_chat_id="42",
        dispatcher=lambda update: dispatched.append(update["update_id"]) or "ok",
    )
    update = {"update_id": 161, "message": {"chat": {"id": 42}}}
    assert local_controller.process_update(update) == "failed"
    assert local_controller.process_update(update) == "duplicate"
    assert db.complete_calls == 1
    assert dispatched == [161]
    assert len(notifier.errors) == 1


def test_dispatch_failure_survives_failed_state_persistence(fake_telegram):
    db = CompletionFailingDatabase()
    notifier = FakeNotifier()

    def fail_dispatch(_update):
        raise RuntimeError("dispatch unavailable")

    local_controller = TelegramController(
        telegram_api=fake_telegram,
        db=db,
        notifier=notifier,
        authorized_chat_id="42",
        dispatcher=fail_dispatch,
    )
    update = {"update_id": 162, "message": {"chat": {"id": 42}}}
    assert local_controller.process_update(update) == "failed"
    assert db.complete_calls == 1
    assert len(notifier.errors) == 1


def test_concurrent_replay_dispatches_once(tmp_path, fake_telegram):
    db = Database(str(tmp_path / "controller.db"))
    dispatched = []
    local_controller = TelegramController(
        telegram_api=fake_telegram,
        db=db,
        notifier=FakeNotifier(),
        authorized_chat_id="42",
        dispatcher=lambda update: dispatched.append(update["update_id"]) or "ok",
    )
    update = {
        "update_id": 17,
        "message": {"chat": {"id": 42}, "text": "/pause"},
    }

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(local_controller.process_update, [update, update]))

    assert sorted(results) == ["duplicate", "processed"]
    assert dispatched == [17]
    with db._conn() as conn:
        row = conn.execute(
            "SELECT state FROM telegram_updates WHERE update_id = 17"
        ).fetchone()
    assert row["state"] == "processed"


class ScriptedPollingApi:
    def __init__(self, outcomes, stop_event=None):
        self.outcomes = list(outcomes)
        self.stop_event = stop_event
        self.calls = []

    def get_updates(self, offset, timeout):
        self.calls.append((offset, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if not self.outcomes and self.stop_event is not None:
            self.stop_event.set_flag = True
        return outcome


class RecordingStopEvent:
    def __init__(self, stop_after_waits=None):
        self.stop_after_waits = stop_after_waits
        self.waits = []
        self.set_flag = False

    def is_set(self):
        return self.set_flag

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.stop_after_waits is not None and len(self.waits) >= self.stop_after_waits:
            self.set_flag = True
        return self.set_flag


def _polling_controller(api, fake_db):
    return TelegramController(
        telegram_api=api,
        db=fake_db,
        notifier=FakeNotifier(),
        authorized_chat_id="42",
        dispatcher=lambda _update: "ok",
    )


def test_run_forever_advances_offset_from_highest_id_in_batch(fake_db):
    stop_event = RecordingStopEvent()
    api = ScriptedPollingApi([
        [
            {"update_id": 21, "message": {"chat": {"id": 42}}},
            {"update_id": 23, "message": {"chat": {"id": 42}}},
            {"update_id": 22, "message": {"chat": {"id": 42}}},
        ],
        [],
    ], stop_event=stop_event)
    _polling_controller(api, fake_db).run_forever(stop_event)
    assert api.calls == [(None, 25), (24, 25)]


def test_run_forever_uses_bounded_interruptible_transport_backoff(fake_db):
    stop_event = RecordingStopEvent(stop_after_waits=6)
    api = ScriptedPollingApi([
        TelegramApiError("offline") for _ in range(6)
    ])
    _polling_controller(api, fake_db).run_forever(stop_event)
    assert stop_event.waits == [1, 2, 4, 8, 30, 30]
    assert api.calls == [(None, 25)] * 6


def test_successful_poll_resets_transport_backoff(fake_db):
    stop_event = RecordingStopEvent(stop_after_waits=2)
    api = ScriptedPollingApi([
        TelegramApiError("offline"),
        [{"update_id": 31, "message": {"chat": {"id": 42}}}],
        TelegramApiError("offline again"),
    ])
    _polling_controller(api, fake_db).run_forever(stop_event)
    assert stop_event.waits == [1, 1]
    assert api.calls == [(None, 25), (None, 25), (32, 25)]


def test_empty_poll_waits_interruptibly_instead_of_busy_loop(fake_db):
    stop_event = RecordingStopEvent(stop_after_waits=1)
    api = ScriptedPollingApi([[]])
    _polling_controller(api, fake_db).run_forever(stop_event)
    assert stop_event.waits == [0.1]


def test_malformed_batch_waits_interruptibly_instead_of_busy_loop(fake_db):
    stop_event = RecordingStopEvent(stop_after_waits=1)
    api = ScriptedPollingApi([[{"message": {"chat": {"id": 42}}}]])
    _polling_controller(api, fake_db).run_forever(stop_event)
    assert stop_event.waits == [0.1]


class FakeResponse:
    def __init__(self, payload=None, status_code=200, chunks=(), json_error=None):
        self.payload = payload if payload is not None else {"ok": True, "result": {}}
        self.status_code = status_code
        self.chunks = list(chunks)
        self.json_error = json_error
        self.text = "response body must not be exposed"

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload

    def iter_content(self, chunk_size):
        assert chunk_size == 64 * 1024
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


class FakeRequests:
    def __init__(self, post_outcomes=(), get_outcomes=()):
        self.post_outcomes = list(post_outcomes)
        self.get_outcomes = list(get_outcomes)
        self.posts = []
        self.gets = []

    @staticmethod
    def _resolve(outcomes):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self._resolve(self.post_outcomes)

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return self._resolve(self.get_outcomes)


def test_messaging_transport_does_not_require_media_directory_at_startup(tmp_path):
    missing_media_root = tmp_path / "created_later"
    requests_client = FakeRequests(post_outcomes=[
        FakeResponse({"ok": True, "result": {"message_id": 1}})
    ])
    api = TelegramApi(
        "123456:secret",
        missing_media_root,
        requests_client=requests_client,
    )
    assert api.send_message("42", "hello") == {"message_id": 1}
    assert not missing_media_root.exists()


def test_get_updates_uses_exact_poll_timeout_and_allowed_updates(tmp_path):
    requests_client = FakeRequests(post_outcomes=[
        FakeResponse({"ok": True, "result": [{"update_id": 40}]})
    ])
    api = TelegramApi("123456:secret", tmp_path, requests_client=requests_client)
    assert api.get_updates(offset=40, timeout=TELEGRAM_POLL_TIMEOUT) == [
        {"update_id": 40}
    ]
    url, kwargs = requests_client.posts[0]
    assert url == "https://api.telegram.org/bot123456:secret/getUpdates"
    assert kwargs["json"] == {
        "offset": 40,
        "timeout": 25,
        "allowed_updates": ["message", "callback_query"],
    }
    assert kwargs["timeout"] == 25


def test_send_and_metadata_calls_use_ten_second_timeout(tmp_path):
    requests_client = FakeRequests(post_outcomes=[
        FakeResponse({"ok": True, "result": {"message_id": 1}}),
        FakeResponse({"ok": True, "result": {"file_path": "photos/a.jpg"}}),
        FakeResponse({"ok": True, "result": True}),
    ])
    api = TelegramApi("123456:secret", tmp_path, requests_client=requests_client)
    assert api.send_message("42", "hello")["message_id"] == 1
    assert api.get_file("file-1") == {"file_path": "photos/a.jpg"}
    assert api.answer_callback("callback-1") is True
    assert [call[1]["timeout"] for call in requests_client.posts] == [
        REQUEST_TIMEOUT,
        REQUEST_TIMEOUT,
        REQUEST_TIMEOUT,
    ]


def test_send_media_uses_bounded_multipart_request(tmp_path):
    media_path = tmp_path / "image.jpg"
    media_path.write_bytes(b"jpeg")
    requests_client = FakeRequests(post_outcomes=[
        FakeResponse({"ok": True, "result": {"message_id": 2}})
    ])
    api = TelegramApi("123456:secret", tmp_path, requests_client=requests_client)
    assert api.send_media("42", media_path, "photo", caption="real studio")["message_id"] == 2
    url, kwargs = requests_client.posts[0]
    assert url.endswith("/sendPhoto")
    assert kwargs["data"] == {"chat_id": "42", "caption": "real studio"}
    assert set(kwargs["files"]) == {"photo"}
    assert kwargs["timeout"] == REQUEST_TIMEOUT


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(status_code=502),
        FakeResponse(json_error=ValueError("not JSON")),
        FakeResponse({"ok": False, "error_code": 400, "description": "bad request"}),
    ],
)
def test_http_and_json_failures_raise_sanitized_api_errors(tmp_path, response):
    secret = "123456789:telegram_Bot-Secret"
    requests_client = FakeRequests(post_outcomes=[response])
    api = TelegramApi(secret, tmp_path, requests_client=requests_client)
    with pytest.raises(TelegramApiError) as raised:
        api.send_message("42", "hello")
    assert secret not in str(raised.value)
    assert "response body must not be exposed" not in str(raised.value)


def test_request_exception_redacts_token_header_and_query(tmp_path):
    secret = "123456789:telegram_Bot-Secret"
    error = RuntimeError(
        f"failed Authorization: Bearer {secret} "
        f"https://example.test/path?token={secret}"
    )
    api = TelegramApi(
        secret,
        tmp_path,
        requests_client=FakeRequests(post_outcomes=[error]),
    )
    with pytest.raises(TelegramApiError) as raised:
        api.send_message("42", "hello")
    safe = str(raised.value)
    assert secret not in safe
    assert "Bearer [redacted]" in safe
    assert "https://example.test/path?[redacted]" in safe


def test_sanitize_error_rejects_raw_updates_and_configured_secrets():
    secret = "not-token-shaped-secret"
    assert sanitize_error(
        f"failed token={secret} https://example.test/a?secret={secret}",
        secrets=[secret],
    ) == "failed token=[redacted] https://example.test/a?[redacted]"
    assert sanitize_error(
        "dispatch failed for {'update_id': 1, 'message': {'text': 'private'}}"
    ) == "[redacted raw Telegram payload]"


def test_sanitize_error_redacts_environment_api_keys_and_request_bodies(monkeypatch):
    api_secret = "configured-private-api-key"
    monkeypatch.setenv("GROQ_API_KEY", api_secret)
    safe_token = sanitize_error(f"provider failed key={api_secret}")
    safe_body = sanitize_error("request failed body={'text': 'private prompt'}")
    assert api_secret not in safe_token
    assert safe_body == "request failed body=[redacted]"


def test_sanitize_error_redacts_basic_authorization_and_assignment_updates():
    safe_header = sanitize_error("request Authorization: Basic basic-private-value")
    assert "basic-private-value" not in safe_header
    assert "Authorization: Basic [redacted]" in safe_header
    assert sanitize_error("raw update_id=9 text=private") == (
        "[redacted raw Telegram payload]"
    )


def test_download_requires_explicit_absolute_destination_inside_media_root(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    requests_client = FakeRequests(get_outcomes=[
        FakeResponse(chunks=[b"unused"]),
    ])
    api = TelegramApi("123456:secret", media_root, requests_client=requests_client)
    with pytest.raises(ValueError):
        api.download_file("photos/a.jpg", Path("relative.jpg"))
    with pytest.raises(ValueError):
        api.download_file("photos/a.jpg", tmp_path / "outside.jpg")
    assert requests_client.gets == []


def test_download_rejects_symlink_parent_and_existing_destination(tmp_path):
    media_root = tmp_path / "media"
    outside = tmp_path / "outside"
    media_root.mkdir()
    outside.mkdir()
    (media_root / "jump").symlink_to(outside, target_is_directory=True)
    existing = media_root / "existing.jpg"
    existing.write_bytes(b"keep")
    api = TelegramApi(
        "123456:secret",
        media_root,
        requests_client=FakeRequests(get_outcomes=[FakeResponse(chunks=[b"unused"])]),
    )
    with pytest.raises(ValueError):
        api.download_file("photos/a.jpg", media_root / "jump" / "escaped.jpg")
    with pytest.raises(FileExistsError):
        api.download_file("photos/a.jpg", existing)
    assert existing.read_bytes() == b"keep"
    assert not (outside / "escaped.jpg").exists()


def test_download_fails_closed_without_nofollow_support(tmp_path, monkeypatch):
    media_root = tmp_path / "media"
    media_root.mkdir()
    requests_client = FakeRequests(get_outcomes=[FakeResponse(chunks=[b"unsafe"])])
    api = TelegramApi("123456:secret", media_root, requests_client=requests_client)
    monkeypatch.delattr("modules.telegram_api.os.O_NOFOLLOW")
    with pytest.raises(RuntimeError, match="secure_nofollow_unavailable"):
        api.download_file("photos/a.jpg", media_root / "new.jpg")
    assert requests_client.gets == []
    assert not (media_root / "new.jpg").exists()


def test_download_is_exclusive_and_cleans_partial_file_on_failure(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    destination = media_root / "new.jpg"
    secret = "123456789:telegram_Bot-Secret"
    requests_client = FakeRequests(get_outcomes=[
        FakeResponse(chunks=[b"partial", RuntimeError(f"stream token={secret}")]),
    ])
    api = TelegramApi(secret, media_root, requests_client=requests_client)
    with pytest.raises(TelegramApiError) as raised:
        api.download_file("photos/a.jpg", destination)
    assert secret not in str(raised.value)
    assert not destination.exists()


def test_download_writes_only_to_reserved_destination(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    destination = media_root / "new.jpg"
    requests_client = FakeRequests(get_outcomes=[
        FakeResponse(chunks=[b"abc", b"", b"def"]),
    ])
    api = TelegramApi("123456:secret", media_root, requests_client=requests_client)
    assert api.download_file("photos/a.jpg", destination) == destination
    assert destination.read_bytes() == b"abcdef"
    url, kwargs = requests_client.gets[0]
    assert url == "https://api.telegram.org/file/bot123456:secret/photos/a.jpg"
    assert kwargs == {"stream": True, "timeout": REQUEST_TIMEOUT}


def test_notifier_uses_api_and_persists_one_sanitized_error(fake_db, fake_telegram):
    secret = "123456789:telegram_Bot-Secret"
    notifier = TelegramNotifier(
        secret,
        "42",
        database=fake_db,
        telegram_api=fake_telegram,
    )
    notifier.notify_error(
        "telegram_update",
        RuntimeError(
            f"Authorization: Bearer {secret} "
            f"https://example.test/path?token={secret}"
        ),
    )
    assert len(fake_db.logged_errors) == 1
    assert len(fake_telegram.messages) == 1
    persisted = " ".join(fake_db.logged_errors[0])
    sent = fake_telegram.messages[0][1]
    assert secret not in persisted
    assert secret not in sent
    assert "Bearer [redacted]" in persisted
    assert "https://example.test/path?[redacted]" in sent


def test_notifier_rejects_raw_update_from_persistence_and_message(fake_db, fake_telegram):
    notifier = TelegramNotifier(
        "123456:secret",
        "42",
        database=fake_db,
        telegram_api=fake_telegram,
    )
    notifier.notify_error(
        "telegram_update",
        RuntimeError("failed for {'update_id': 8, 'message': {'text': 'private'}}"),
    )
    assert fake_db.logged_errors[0][2] == "[redacted raw Telegram payload]"
    assert "update_id" not in fake_telegram.messages[0][1]
    assert "private" not in fake_telegram.messages[0][1]


def test_notifier_has_no_automated_engagement_summaries(fake_db, fake_telegram):
    notifier = TelegramNotifier(
        "123456:secret",
        "42",
        database=fake_db,
        telegram_api=fake_telegram,
    )
    assert not hasattr(notifier, "notify_engagement_summary")
    assert not hasattr(notifier, "notify_growth_summary")


def test_notifier_logs_only_sanitized_delivery_failure(
    fake_db,
    fake_telegram,
    caplog,
):
    secret = "123456789:telegram_Bot-Secret"
    fake_telegram.callback_error = None

    def fail_send(*_args, **_kwargs):
        raise RuntimeError(f"send failed token={secret}")

    fake_telegram.send_message = fail_send
    notifier = TelegramNotifier(
        secret,
        "42",
        database=fake_db,
        telegram_api=fake_telegram,
    )
    with caplog.at_level(logging.WARNING):
        notifier.notify_error("cycle", RuntimeError("original"))
    assert secret not in caplog.text
    assert len(fake_db.logged_errors) == 1
