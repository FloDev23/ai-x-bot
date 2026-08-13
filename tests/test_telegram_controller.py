import logging
import os
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from modules.database import Database
from modules.notifier import TelegramNotifier
import modules.telegram_api as telegram_api_module
from modules.telegram_api import (
    REQUEST_TIMEOUT,
    TELEGRAM_POLL_TIMEOUT,
    TelegramApi,
    TelegramApiError,
    sanitize_error,
    telegram_media_metadata,
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


def test_update_with_message_and_callback_is_claimed_as_malformed(
    controller,
    fake_telegram,
    fake_db,
):
    update = {
        "update_id": 142,
        "message": {"chat": {"id": 42}, "text": "/status"},
        "callback_query": {
            "id": "callback-poison",
            "message": {"chat": {"id": 999}},
            "data": "draft:approve:7",
        },
    }
    assert controller.process_update(update) == "malformed"
    assert fake_db.telegram_updates[142] == {
        "chat_id": "None",
        "state": "malformed",
        "result": {},
    }
    assert fake_db.operational_mutations == []
    assert fake_telegram.messages == []
    assert fake_telegram.answered_callbacks == []


def test_update_without_supported_subtype_is_claimed_as_malformed(
    controller,
    fake_telegram,
    fake_db,
):
    assert controller.process_update({"update_id": 143, "inline_query": {}}) == (
        "malformed"
    )
    assert fake_db.telegram_updates[143]["state"] == "malformed"
    assert fake_db.operational_mutations == []
    assert fake_telegram.messages == []


@pytest.mark.parametrize("callback_id", [None, "", "bad\ncallback", "x" * 4097])
def test_callback_without_usable_id_is_malformed_before_dispatch(
    fake_db,
    fake_telegram,
    callback_id,
):
    dispatched = []
    local_controller = TelegramController(
        telegram_api=fake_telegram,
        db=fake_db,
        notifier=FakeNotifier(),
        authorized_chat_id="42",
        dispatcher=lambda update: dispatched.append(update),
    )
    update = {
        "update_id": 144,
        "callback_query": {
            "id": callback_id,
            "message": {"chat": {"id": 42}},
            "data": "draft:approve:7",
        },
    }
    assert local_controller.process_update(update) == "malformed"
    assert fake_db.telegram_updates[144]["state"] == "malformed"
    assert dispatched == []
    assert fake_telegram.answered_callbacks == []


@pytest.mark.parametrize("bad_update_id", [True, 1.0, "1", -1, 2**63])
def test_non_exact_or_out_of_range_update_id_fails_before_claim(
    controller,
    fake_db,
    bad_update_id,
):
    update = {
        "update_id": bad_update_id,
        "message": {"chat": {"id": 42}, "text": "/status"},
    }
    assert controller.process_update(update) == "malformed"
    assert fake_db.telegram_updates == {}
    assert fake_db.operational_mutations == []


def test_oversized_update_id_does_not_reach_sqlite(tmp_path, fake_telegram):
    db = Database(str(tmp_path / "strict-update-id.db"))
    controller = TelegramController(
        telegram_api=fake_telegram,
        db=db,
        notifier=FakeNotifier(),
        authorized_chat_id="42",
        dispatcher=lambda _update: pytest.fail("must not dispatch"),
    )
    update = {
        "update_id": 2**63,
        "message": {"chat": {"id": 42}, "text": "/status"},
    }
    assert controller.process_update(update) == "malformed"
    with db._conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS count FROM telegram_updates").fetchone()
    assert count["count"] == 0


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


def test_dispatch_failure_persists_only_allowlisted_exception_class(
    fake_db,
    fake_telegram,
):
    private_exception = type("Basic_private_token", (RuntimeError,), {})

    def fail_dispatch(_update):
        raise private_exception("headers=private")

    local_controller = TelegramController(
        telegram_api=fake_telegram,
        db=fake_db,
        notifier=FakeNotifier(),
        authorized_chat_id="42",
        dispatcher=fail_dispatch,
    )
    update = {"update_id": 167, "message": {"chat": {"id": 42}}}
    assert local_controller.process_update(update) == "failed"
    assert fake_db.telegram_updates[167]["result"] == {
        "error": "exception=RuntimeError"
    }


def test_unauthorized_completion_failure_is_local_only(fake_telegram):
    db = CompletionFailingDatabase()
    notifier = FakeNotifier()
    local_controller = TelegramController(
        telegram_api=fake_telegram,
        db=db,
        notifier=notifier,
        authorized_chat_id="42",
        dispatcher=lambda _update: pytest.fail("must not dispatch"),
    )
    update = {"update_id": 163, "message": {"chat": {"id": 999}}}
    assert local_controller.process_update(update) == "failed"
    assert db.complete_calls == 1
    assert notifier.errors == []
    assert fake_telegram.messages == []


class ClaimFailingDatabase:
    def __init__(self, failed_update_id=None):
        self.failed_update_id = failed_update_id
        self.claim_calls = []
        self.telegram_updates = {}

    def claim_telegram_update(self, update_id, chat_id):
        self.claim_calls.append(update_id)
        if self.failed_update_id is None or update_id == self.failed_update_id:
            raise RuntimeError("headers={'Authorization': 'private'}")
        if update_id in self.telegram_updates:
            return False
        self.telegram_updates[update_id] = {
            "chat_id": str(chat_id),
            "state": "processing",
            "result": {},
        }
        return True

    def complete_telegram_update(self, update_id, state, result):
        self.telegram_updates[update_id].update(state=state, result=dict(result))


def test_claim_exception_fails_closed_without_dispatch_or_notifier(fake_telegram):
    db = ClaimFailingDatabase()
    notifier = FakeNotifier()
    dispatched = []
    local_controller = TelegramController(
        telegram_api=fake_telegram,
        db=db,
        notifier=notifier,
        authorized_chat_id="42",
        dispatcher=lambda update: dispatched.append(update),
    )
    update = {"update_id": 164, "message": {"chat": {"id": 42}}}
    assert local_controller.process_update(update) == "failed"
    assert db.claim_calls == [164]
    assert dispatched == []
    assert notifier.errors == []


@pytest.mark.parametrize("claim_result", [1, "claimed", object()])
def test_non_boolean_claim_result_fails_closed(fake_telegram, claim_result):
    class AmbiguousClaimDatabase:
        def claim_telegram_update(self, _update_id, _chat_id):
            return claim_result

    dispatched = []
    local_controller = TelegramController(
        telegram_api=fake_telegram,
        db=AmbiguousClaimDatabase(),
        notifier=FakeNotifier(),
        authorized_chat_id="42",
        dispatcher=lambda update: dispatched.append(update),
    )
    update = {"update_id": 168, "message": {"chat": {"id": 42}}}
    assert local_controller.process_update(update) == "failed"
    assert dispatched == []


def test_stop_before_claim_has_no_side_effect(fake_db, fake_telegram):
    stop_event = RecordingStopEvent()
    stop_event.set_flag = True
    local_controller = TelegramController(
        telegram_api=fake_telegram,
        db=fake_db,
        notifier=FakeNotifier(),
        authorized_chat_id="42",
        dispatcher=lambda _update: pytest.fail("must not dispatch"),
    )
    update = {"update_id": 165, "message": {"chat": {"id": 42}}}
    assert local_controller.process_update(update, stop_event=stop_event) == "stopped"
    assert fake_db.telegram_updates == {}


def test_stop_after_claim_prevents_dispatch(fake_db, fake_telegram):
    stop_event = RecordingStopEvent()
    original_claim = fake_db.claim_telegram_update

    def claim_and_stop(update_id, chat_id):
        claimed = original_claim(update_id, chat_id)
        stop_event.set_flag = True
        return claimed

    fake_db.claim_telegram_update = claim_and_stop
    local_controller = TelegramController(
        telegram_api=fake_telegram,
        db=fake_db,
        notifier=FakeNotifier(),
        authorized_chat_id="42",
        dispatcher=lambda _update: pytest.fail("must not dispatch"),
    )
    update = {"update_id": 166, "message": {"chat": {"id": 42}}}
    assert local_controller.process_update(update, stop_event=stop_event) == "stopped"
    assert fake_db.telegram_updates[166]["state"] == "stopped"


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
    stop_event = RecordingStopEvent(stop_after_waits=6)
    api = ScriptedPollingApi([
        [{"message": {"chat": {"id": 42}}}] for _ in range(6)
    ])
    _polling_controller(api, fake_db).run_forever(stop_event)
    assert stop_event.waits == [1, 2, 4, 8, 30, 30]
    assert api.calls == [(None, 25)] * 6


def test_non_list_poll_response_uses_malformed_batch_backoff(fake_db):
    stop_event = RecordingStopEvent(stop_after_waits=6)
    api = ScriptedPollingApi([None, {}, False, "", (), 0])
    _polling_controller(api, fake_db).run_forever(stop_event)
    assert stop_event.waits == [1, 2, 4, 8, 30, 30]
    assert api.calls == [(None, 25)] * 6


def test_run_forever_isolates_claim_failure_and_processes_next_update(fake_telegram):
    stop_event = RecordingStopEvent()
    db = ClaimFailingDatabase(failed_update_id=70)
    api = ScriptedPollingApi([
        [
            {"update_id": 70, "message": {"chat": {"id": 42}}},
            {"update_id": 71, "message": {"chat": {"id": 42}}},
        ],
        [],
    ], stop_event=stop_event)
    local_controller = TelegramController(
        telegram_api=api,
        db=db,
        notifier=FakeNotifier(),
        authorized_chat_id="42",
        dispatcher=lambda _update: "ok",
    )
    local_controller.run_forever(stop_event)
    assert db.claim_calls == [70, 71]
    assert db.telegram_updates[71]["state"] == "processed"
    assert api.calls == [(None, 25), (72, 25)]


def test_run_forever_rechecks_stop_before_each_batch_entry(fake_db):
    stop_event = RecordingStopEvent()
    dispatched = []

    def dispatch(update):
        dispatched.append(update["update_id"])
        stop_event.set_flag = True
        return "ok"

    api = ScriptedPollingApi([[
        {"update_id": 72, "message": {"chat": {"id": 42}}},
        {"update_id": 73, "message": {"chat": {"id": 42}}},
    ]])
    local_controller = TelegramController(
        telegram_api=api,
        db=fake_db,
        notifier=FakeNotifier(),
        authorized_chat_id="42",
        dispatcher=dispatch,
    )
    local_controller.run_forever(stop_event)
    assert dispatched == [72]
    assert 73 not in fake_db.telegram_updates


class FakeResponse:
    def __init__(
        self,
        payload=None,
        status_code=200,
        chunks=(),
        json_error=None,
        headers=None,
        close_error=None,
    ):
        self.payload = payload if payload is not None else {"ok": True, "result": {}}
        self.status_code = status_code
        self.chunks = list(chunks)
        self.json_error = json_error
        self.headers = dict(headers or {})
        self.close_error = close_error
        self.close_calls = 0
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

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class ExplodingStatusResponse:
    @property
    def status_code(self):
        raise RuntimeError(
            "json={'data':'private'} headers=Basic private response=Bearer private"
        )


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


class ConnectionPoolLoggingRequests(FakeRequests):
    def post(self, url, **kwargs):
        try:
            raise RuntimeError(f"response body includes {url}")
        except RuntimeError:
            logging.getLogger("urllib3.connectionpool").debug(
                "Starting new HTTPS connection for %s headers=%s",
                url,
                {"Authorization": f"Basic {url}"},
                exc_info=True,
                stack_info=True,
            )
        return super().post(url, **kwargs)


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


def test_connectionpool_debug_record_never_formats_bot_token(tmp_path, caplog):
    secret = "123456789:telegram_Bot-Secret"
    requests_client = ConnectionPoolLoggingRequests(post_outcomes=[
        FakeResponse({"ok": True, "result": {"message_id": 1}})
    ])
    caplog.set_level(logging.DEBUG)
    api = TelegramApi(secret, tmp_path, requests_client=requests_client)
    assert api.send_message("42", "hello") == {"message_id": 1}
    connection_records = [
        record.getMessage()
        for record in caplog.records
        if record.name == "urllib3.connectionpool"
    ]
    assert connection_records == ["urllib3 connectionpool event"]
    for record in caplog.records:
        serialized = " ".join((
            str(record.msg),
            repr(record.args),
            record.getMessage(),
            str(record.exc_info),
            str(record.exc_text),
            str(record.stack_info),
        ))
        assert secret not in serialized


def test_connectionpool_filter_preserves_non_telegram_record(tmp_path, caplog):
    TelegramApi("123456:secret", tmp_path, requests_client=FakeRequests())
    caplog.set_level(logging.DEBUG)
    logger = logging.getLogger("urllib3.connectionpool")
    message = "Starting new HTTPS connection for %s headers=%s"
    args = (
        "https://newsapi.org/v2/everything",
        {"Authorization": "Bearer news-api-private"},
    )
    logger.debug(message, *args)
    record = caplog.records[-1]
    assert record.msg == message
    assert record.args == args
    assert record.getMessage() == (
        "Starting new HTTPS connection for https://newsapi.org/v2/everything "
        "headers={'Authorization': 'Bearer news-api-private'}"
    )


def test_connectionpool_filter_is_safe_under_concurrent_api_initialization(
    tmp_path,
    caplog,
):
    caplog.set_level(logging.DEBUG)

    def initialize_and_send(index):
        secret = f"123456789:telegram_Bot-Secret-{index}"
        api = TelegramApi(
            secret,
            tmp_path,
            requests_client=ConnectionPoolLoggingRequests(post_outcomes=[
                FakeResponse({"ok": True, "result": {"message_id": index}})
            ]),
        )
        return secret, api.send_message("42", "hello")

    with ThreadPoolExecutor(max_workers=32) as executor:
        results = list(executor.map(initialize_and_send, range(200)))

    records = [
        record
        for record in caplog.records
        if record.name == "urllib3.connectionpool"
    ]
    assert len(records) == 200
    assert [result[1]["message_id"] for result in results] == list(range(200))
    serialized = " ".join(
        " ".join((
            str(record.msg),
            repr(record.args),
            record.getMessage(),
            str(record.exc_info),
            str(record.exc_text),
            str(record.stack_info),
        ))
        for record in records
    )
    assert all(secret not in serialized for secret, _result in results)


def test_connectionpool_filter_redacts_token_only_in_traceback_or_stack(
    tmp_path,
    caplog,
):
    secret = "123456789:telegram_Bot-Secret-trace"
    TelegramApi(secret, tmp_path, requests_client=FakeRequests())
    caplog.set_level(logging.DEBUG)
    logger = logging.getLogger("urllib3.connectionpool")

    try:
        exec(compile("raise RuntimeError('generic')", secret, "exec"), {})
    except RuntimeError:
        logger.debug("generic traceback", exc_info=True)
    exec(
        compile(
            "logger.debug('generic stack', stack_info=True)",
            secret,
            "exec",
        ),
        {"logger": logger},
    )

    records = [
        record
        for record in caplog.records
        if record.name == "urllib3.connectionpool"
    ][-2:]
    assert [record.getMessage() for record in records] == [
        "urllib3 connectionpool event",
        "urllib3 connectionpool event",
    ]
    serialized = caplog.text + " ".join(
        " ".join((
            str(record.msg),
            repr(record.args),
            str(record.exc_info),
            str(record.exc_text),
            str(record.stack_info),
        ))
        for record in records
    )
    assert secret not in serialized


@pytest.mark.parametrize("origin_field", ["pathname", "filename", "module", "funcName"])
def test_connectionpool_filter_detects_registered_token_in_each_origin_field(
    tmp_path,
    caplog,
    origin_field,
):
    secret = f"123456789:telegram_Bot-Secret-{origin_field}"
    TelegramApi(secret, tmp_path, requests_client=FakeRequests())
    caplog.set_level(logging.DEBUG)
    logger = logging.getLogger("urllib3.connectionpool")
    record = logger.makeRecord(
        logger.name,
        logging.DEBUG,
        "/safe/nontelegram.py",
        1,
        "generic diagnostic",
        (),
        None,
        "generic_function",
    )
    setattr(record, origin_field, secret)
    logger.handle(record)
    captured = caplog.records[-1]
    assert captured.getMessage() == "urllib3 connectionpool event"
    serialized = caplog.text + " ".join((
        str(captured.msg),
        repr(captured.args),
        str(captured.exc_info),
        str(captured.exc_text),
        str(captured.stack_info),
        captured.pathname,
        captured.filename,
        captured.module,
        captured.funcName,
    ))
    assert secret not in serialized


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


def test_send_media_accepts_caller_owned_verified_stream_without_closing(tmp_path):
    media_stream = io.BytesIO(b"verified-jpeg")
    requests_client = FakeRequests(post_outcomes=[
        FakeResponse({"ok": True, "result": {"message_id": 3}})
    ])
    api = TelegramApi("123456:secret", tmp_path, requests_client=requests_client)

    assert api.send_media("42", media_stream, "document")["message_id"] == 3

    _url, kwargs = requests_client.posts[0]
    assert kwargs["files"]["document"] is media_stream
    assert not media_stream.closed


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


def test_post_status_property_failure_is_sanitized(tmp_path):
    api = TelegramApi(
        "123456:secret",
        tmp_path,
        requests_client=FakeRequests(post_outcomes=[ExplodingStatusResponse()]),
    )
    with pytest.raises(TelegramApiError) as raised:
        api.send_message("42", "hello")
    assert str(raised.value) == (
        "operation=transport method=sendMessage exception=RuntimeError"
    )


def test_remote_api_error_code_cannot_copy_response_payload(tmp_path):
    private_code = "private-secret-value"
    api = TelegramApi(
        "123456:secret",
        tmp_path,
        requests_client=FakeRequests(post_outcomes=[
            FakeResponse({
                "ok": False,
                "error_code": private_code,
                "description": "Bearer private body",
            })
        ]),
    )
    with pytest.raises(TelegramApiError) as raised:
        api.send_message("42", "hello")
    assert str(raised.value) == (
        "operation=api method=sendMessage code=invalid_error_code"
    )
    assert private_code not in str(raised.value)


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
    assert safe == "operation=transport method=sendMessage exception=RuntimeError"
    assert secret not in safe


def test_sanitize_error_is_allowlist_only_for_untrusted_exception_context():
    secret = "not-token-shaped-secret"
    raw = (
        f"json={{'data': 'private'}} headers=Basic-private response=secret "
        f"body=Bearer-private query=?token={secret} update_id=9 raw-update"
    )
    assert sanitize_error(
        RuntimeError(raw),
        secrets=[secret],
        operation="telegram_update",
        method="getUpdates",
        status=502,
        code=400,
    ) == (
        "operation=telegram_update method=getUpdates status=502 "
        "exception=RuntimeError code=400"
    )


def test_sanitize_error_redacts_environment_api_keys_and_request_bodies(monkeypatch):
    api_secret = "configured-private-api-key"
    monkeypatch.setenv("GROQ_API_KEY", api_secret)
    safe_token = sanitize_error(RuntimeError(f"provider failed key={api_secret}"))
    safe_body = sanitize_error(
        ValueError("request failed body={'text': 'private prompt'}")
    )
    assert api_secret not in safe_token
    assert safe_token == "exception=RuntimeError"
    assert safe_body == "exception=ValueError"


def test_sanitize_error_redacts_basic_authorization_and_assignment_updates():
    safe_header = sanitize_error(
        RuntimeError("request Authorization: Basic basic-private-value")
    )
    assert safe_header == "exception=RuntimeError"
    assert "basic-private-value" not in safe_header
    assert sanitize_error("raw update_id=9 text=private") == "exception=Exception"


def test_sanitize_error_rejects_credentials_hidden_in_exception_class_name():
    private_exception = type("Bearer_private_token", (RuntimeError,), {})
    safe = sanitize_error(private_exception("body=private"))
    assert safe == "exception=RuntimeError"
    assert "Bearer" not in safe
    assert "token" not in safe


def test_sanitize_error_uses_closed_metadata_allowlists():
    private_exception = type("OpaquePrivateValue", (BaseException,), {})
    safe = sanitize_error(
        private_exception(),
        operation="OpaquePrivateValue",
        method="OpaquePrivateValue",
        code="OpaquePrivateValue",
    )
    assert safe == "exception=BaseException"
    assert "OpaquePrivateValue" not in safe


def test_telegram_photo_metadata_selects_largest_photo_size():
    message = {
        "message_id": 1,
        "photo": [
            {
                "file_id": "small-file",
                "file_unique_id": "small-unique",
                "width": 90,
                "height": 90,
                "file_size": 800,
            },
            {
                "file_id": "large-file",
                "file_unique_id": "large-unique",
                "width": 1920,
                "height": 1080,
                "file_size": 5000,
            },
            {
                "file_id": "medium-file",
                "file_unique_id": "medium-unique",
                "width": 640,
                "height": 480,
                "file_size": 2000,
            },
        ],
    }
    metadata = telegram_media_metadata(message)
    assert metadata == {
        "file_id": "large-file",
        "message_filename": (
            "telegram-photo-"
            "8d43be05474937ae2149ae27707a67ce685d5242fb8375617480f752b76558b2.jpg"
        ),
        "mime_type": "image/jpeg",
        "expected_size": 5000,
    }


def test_telegram_photo_metadata_breaks_largest_tie_deterministically():
    first = {
        "file_id": "tie-file-a",
        "file_unique_id": "tie-unique-a",
        "width": 100,
        "height": 200,
        "file_size": 1000,
    }
    second = {
        "file_id": "tie-file-b",
        "file_unique_id": "tie-unique-b",
        "width": 200,
        "height": 100,
        "file_size": 1000,
    }
    forward = telegram_media_metadata({"photo": [first, second]})
    reverse = telegram_media_metadata({"photo": [second, first]})
    assert forward == reverse
    assert forward["file_id"] == "tie-file-b"


def test_telegram_photo_metadata_breaks_identical_unique_id_tie_deterministically():
    first = {
        "file_id": "rotating-file-a",
        "file_unique_id": "shared-unique",
        "width": 100,
        "height": 100,
        "file_size": 1000,
    }
    second = {**first, "file_id": "rotating-file-b"}
    forward = telegram_media_metadata({"photo": [first, second]})
    reverse = telegram_media_metadata({"photo": [second, first]})
    assert forward == reverse


def test_telegram_photo_metadata_canonicalizes_unsafe_ids_without_collisions():
    def metadata(unique_id):
        return telegram_media_metadata({
            "photo": [{
                "file_id": "../../raw-file-id",
                "file_unique_id": unique_id,
                "width": 100,
                "height": 100,
                "file_size": 1000,
            }]
        })

    slash = metadata("unsafe/a")
    question = metadata("unsafe?a")
    assert slash["file_id"] == "../../raw-file-id"
    assert slash["message_filename"] != question["message_filename"]
    for result in (slash, question):
        filename = result["message_filename"]
        assert filename.startswith("telegram-photo-")
        assert filename.endswith(".jpg")
        assert "/" not in filename
        assert "\\" not in filename
        assert ".." not in filename


@pytest.mark.parametrize(
    "mutation",
    [
        lambda photo: photo.pop("file_size"),
        lambda photo: photo.update(file_size=True),
        lambda photo: photo.update(file_size=0),
        lambda photo: photo.pop("file_id"),
        lambda photo: photo.pop("width"),
        lambda photo: photo.update(height=0),
        lambda photo: photo.update(file_unique_id="\ud800"),
    ],
)
def test_telegram_photo_metadata_missing_or_invalid_fields_fail_closed(mutation):
    photo = {
        "file_id": "photo-file",
        "file_unique_id": "photo-unique",
        "width": 100,
        "height": 100,
        "file_size": 1000,
    }
    mutation(photo)
    with pytest.raises(TelegramApiError) as raised:
        telegram_media_metadata({"photo": [photo]})
    assert str(raised.value) == (
        "operation=media_metadata code=invalid_media_metadata"
    )


def test_telegram_photo_metadata_does_not_fallback_from_invalid_largest_entry():
    with pytest.raises(TelegramApiError) as raised:
        telegram_media_metadata({
            "photo": [
                {
                    "file_id": "small-file",
                    "width": 100,
                    "height": 100,
                    "file_size": 1000,
                },
                {
                    "file_id": "largest-file",
                    "width": 1000,
                    "height": 1000,
                },
            ]
        })
    assert str(raised.value) == (
        "operation=media_metadata code=invalid_media_metadata"
    )


@pytest.mark.parametrize(
    ("subtype", "payload", "expected_mime", "expected_suffix"),
    [
        (
            "video",
            {
                "file_id": "video-file",
                "file_unique_id": "video-unique",
                "file_name": "Studio Tour.MOV",
                "mime_type": "video/quicktime",
                "file_size": 10_000,
            },
            "video/quicktime",
            ".mov",
        ),
        (
            "document",
            {
                "file_id": "document-file",
                "file_unique_id": "document-unique",
                "file_name": "brand-photo.PNG",
                "mime_type": "image/png",
                "file_size": 20_000,
            },
            "image/png",
            ".png",
        ),
    ],
)
def test_telegram_video_and_document_metadata_are_canonical(
    subtype,
    payload,
    expected_mime,
    expected_suffix,
):
    metadata = telegram_media_metadata({subtype: payload})
    assert metadata["file_id"] == payload["file_id"]
    assert metadata["mime_type"] == expected_mime
    assert metadata["expected_size"] == payload["file_size"]
    assert metadata["message_filename"].startswith(f"telegram-{subtype}-")
    assert metadata["message_filename"].endswith(expected_suffix)
    assert Path(metadata["message_filename"]).name == metadata["message_filename"]


def test_telegram_media_canonical_filenames_avoid_same_name_collisions():
    def metadata(file_id):
        return telegram_media_metadata({
            "document": {
                "file_id": file_id,
                "file_name": "same-name.webp",
                "mime_type": "image/webp",
                "file_size": 100,
            }
        })

    assert metadata("document-a")["message_filename"] != metadata("document-b")[
        "message_filename"
    ]


@pytest.mark.parametrize(
    ("message", "expected_code"),
    [
        ({}, "unsupported_media_type"),
        ({"animation": {}}, "unsupported_media_type"),
        ({"photo": [], "video": {}}, "invalid_media_metadata"),
        (
            {
                "video": {
                    "file_id": "video-file",
                    "file_name": "clip.mp4",
                    "mime_type": "video/mp4",
                }
            },
            "invalid_media_metadata",
        ),
        (
            {
                "video": {
                    "file_id": "video-file",
                    "file_name": "clip.mp4",
                    "mime_type": "video/quicktime",
                    "file_size": 100,
                }
            },
            "invalid_media_metadata",
        ),
        (
            {
                "video": {
                    "file_id": "video-file",
                    "file_name": "still.jpg",
                    "mime_type": "image/jpeg",
                    "file_size": 100,
                }
            },
            "unsupported_media_type",
        ),
        (
            {
                "document": {
                    "file_id": "document-file",
                    "file_name": "../clip.mp4",
                    "mime_type": "video/mp4",
                    "file_size": 100,
                }
            },
            "invalid_media_metadata",
        ),
        (
            {
                "document": {
                    "file_id": "document-file",
                    "file_name": "archive.bin",
                    "mime_type": "application/octet-stream",
                    "file_size": 100,
                }
            },
            "unsupported_media_type",
        ),
    ],
)
def test_telegram_media_metadata_malformed_messages_fail_with_reason_code(
    message,
    expected_code,
):
    with pytest.raises(TelegramApiError) as raised:
        telegram_media_metadata(message)
    assert str(raised.value) == f"operation=media_metadata code={expected_code}"


@pytest.mark.parametrize(
    ("subtype", "payload"),
    [
        (
            "photo",
            [{
                "file_id": "photo-file",
                "width": 100,
                "height": 100,
                "file_size": telegram_api_module.TELEGRAM_MAX_IMAGE_BYTES + 1,
            }],
        ),
        (
            "video",
            {
                "file_id": "video-file",
                "file_name": "clip.mp4",
                "mime_type": "video/mp4",
                "file_size": telegram_api_module.TELEGRAM_MAX_VIDEO_BYTES + 1,
            },
        ),
    ],
)
def test_telegram_media_metadata_enforces_configured_cap(subtype, payload):
    with pytest.raises(TelegramApiError) as raised:
        telegram_media_metadata({subtype: payload})
    assert str(raised.value) == "operation=media_metadata code=file_too_large"


def test_telegram_media_metadata_validation_has_no_network_or_file_side_effect(
    tmp_path,
):
    media_root = tmp_path / "media"
    requests_client = FakeRequests(get_outcomes=[pytest.fail])
    api = TelegramApi("123456:secret", media_root, requests_client=requests_client)
    with pytest.raises(TelegramApiError):
        telegram_media_metadata({
            "document": {
                "file_id": "document-file",
                "file_name": "../../unsafe.mp4",
                "mime_type": "video/mp4",
                "file_size": 100,
            }
        })
    assert requests_client.gets == []
    assert not media_root.exists()


def test_download_requires_explicit_message_media_metadata(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    requests_client = FakeRequests(get_outcomes=[FakeResponse(chunks=[b"x"])])
    api = TelegramApi("123456:secret", media_root, requests_client=requests_client)
    with pytest.raises(TypeError):
        api.download_file("photos/a.jpg", media_root / "photo.jpg")
    assert requests_client.gets == []


def test_download_trusts_message_filename_not_destination_suffix(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    requests_client = FakeRequests(get_outcomes=[FakeResponse(chunks=[b"x"])])
    api = TelegramApi("123456:secret", media_root, requests_client=requests_client)
    with pytest.raises(TelegramApiError) as raised:
        api.download_file(
            "documents/file",
            media_root / "looks-safe.jpg",
            message_filename="payload.bin",
            mime_type="image/jpeg",
            expected_size=1,
        )
    assert str(raised.value) == (
        "operation=download method=downloadFile code=invalid_media_metadata"
    )
    assert requests_client.gets == []


@pytest.mark.parametrize(
    ("filename", "mime_type"),
    [
        ("photo.jpg", "image/jpeg"),
        ("photo.jpeg", "image/jpeg"),
        ("photo.png", "image/png"),
        ("photo.webp", "image/webp"),
        ("clip.mp4", "video/mp4"),
        ("clip.mov", "video/quicktime"),
        ("clip.m4v", "video/x-m4v"),
    ],
)
def test_download_accepts_message_mime_matching_filename(
    tmp_path,
    filename,
    mime_type,
):
    media_root = tmp_path / "media"
    media_root.mkdir()
    response = FakeResponse(chunks=[b"x"], headers={"Content-Length": "1"})
    api = TelegramApi(
        "123456:secret",
        media_root,
        requests_client=FakeRequests(get_outcomes=[response]),
    )
    destination = media_root / filename
    assert api.download_file(
        "documents/file",
        destination,
        message_filename=filename,
        mime_type=mime_type,
        expected_size=1,
    ) == destination
    assert destination.read_bytes() == b"x"


@pytest.mark.parametrize(
    ("filename", "mime_type", "limit"),
    [
        ("photo.jpg", "image/jpeg", telegram_api_module.TELEGRAM_MAX_IMAGE_BYTES),
        ("photo.png", "image/png", telegram_api_module.TELEGRAM_MAX_IMAGE_BYTES),
        ("photo.webp", "image/webp", telegram_api_module.TELEGRAM_MAX_IMAGE_BYTES),
        ("clip.mp4", "video/mp4", telegram_api_module.TELEGRAM_MAX_VIDEO_BYTES),
        ("clip.mov", "video/quicktime", telegram_api_module.TELEGRAM_MAX_VIDEO_BYTES),
        ("clip.m4v", "video/x-m4v", telegram_api_module.TELEGRAM_MAX_VIDEO_BYTES),
    ],
)
def test_download_uses_exact_configured_cap_for_each_media_type(
    tmp_path,
    filename,
    mime_type,
    limit,
):
    media_root = tmp_path / "media"
    media_root.mkdir()
    api = TelegramApi(
        "123456:secret",
        media_root,
        requests_client=FakeRequests(),
    )
    accepted = media_root / f"accepted-{filename}"
    assert api._download_byte_limit(
        accepted,
        filename,
        mime_type,
        limit,
    ) == limit

    rejected = media_root / f"rejected-{filename}"
    with pytest.raises(TelegramApiError) as raised:
        api.download_file(
            "documents/file",
            rejected,
            message_filename=filename,
            mime_type=mime_type,
            expected_size=limit + 1,
        )
    assert str(raised.value) == (
        "operation=download method=downloadFile code=file_too_large"
    )
    assert not rejected.exists()


@pytest.mark.parametrize(
    ("filename", "mime_type", "expected_size", "expected_code"),
    [
        ("payload.bin", "application/octet-stream", 1, "invalid_media_metadata"),
        ("photo.jpg", "image/png", 1, "invalid_media_metadata"),
        ("photo.jpg", "", 1, "invalid_media_metadata"),
        ("photo.jpg", "image/jpeg", True, "invalid_media_metadata"),
        ("photo.jpg", "image/jpeg", 0, "invalid_media_metadata"),
        (
            "photo.jpg",
            "image/jpeg",
            telegram_api_module.TELEGRAM_MAX_IMAGE_BYTES + 1,
            "file_too_large",
        ),
        (
            "clip.mp4",
            "video/mp4",
            telegram_api_module.TELEGRAM_MAX_VIDEO_BYTES + 1,
            "file_too_large",
        ),
    ],
)
def test_download_rejects_unknown_mismatched_or_unbounded_message_metadata(
    tmp_path,
    filename,
    mime_type,
    expected_size,
    expected_code,
):
    media_root = tmp_path / "media"
    media_root.mkdir()
    requests_client = FakeRequests(get_outcomes=[FakeResponse(chunks=[b"unused"])])
    api = TelegramApi("123456:secret", media_root, requests_client=requests_client)
    with pytest.raises(TelegramApiError) as raised:
        api.download_file(
            "documents/file",
            media_root / filename,
            message_filename=filename,
            mime_type=mime_type,
            expected_size=expected_size,
        )
    assert str(raised.value) == (
        f"operation=download method=downloadFile code={expected_code}"
    )
    assert requests_client.gets == []


@pytest.mark.parametrize(
    "content_length",
    ["9" * 5000, "-1", "5, 5", True, 2**70],
)
def test_download_rejects_malformed_content_length_with_cleanup(
    tmp_path,
    content_length,
):
    media_root = tmp_path / "media"
    media_root.mkdir()
    destination = media_root / "photo.jpg"
    response = FakeResponse(
        chunks=[pytest.fail],
        headers={"Content-Length": content_length},
    )
    api = TelegramApi(
        "123456:secret",
        media_root,
        requests_client=FakeRequests(get_outcomes=[response]),
    )
    with pytest.raises(TelegramApiError) as raised:
        api.download_file(
            "photos/a.jpg",
            destination,
            message_filename="photo.jpg",
            mime_type="image/jpeg",
            expected_size=1,
        )
    assert str(raised.value) == (
        "operation=download method=downloadFile code=invalid_content_length"
    )
    assert response.close_calls == 1
    assert not destination.exists()


def test_download_requires_explicit_absolute_destination_inside_media_root(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    requests_client = FakeRequests(get_outcomes=[
        FakeResponse(chunks=[b"unused"]),
    ])
    api = TelegramApi("123456:secret", media_root, requests_client=requests_client)
    with pytest.raises(ValueError):
        api.download_file(
            "photos/a.jpg",
            Path("relative.jpg"),
            message_filename="relative.jpg",
            mime_type="image/jpeg",
            expected_size=1,
        )
    with pytest.raises(ValueError):
        api.download_file(
            "photos/a.jpg",
            tmp_path / "outside.jpg",
            message_filename="outside.jpg",
            mime_type="image/jpeg",
            expected_size=1,
        )
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
        api.download_file(
            "photos/a.jpg",
            media_root / "jump" / "escaped.jpg",
            message_filename="escaped.jpg",
            mime_type="image/jpeg",
            expected_size=1,
        )
    with pytest.raises(FileExistsError):
        api.download_file(
            "photos/a.jpg",
            existing,
            message_filename="existing.jpg",
            mime_type="image/jpeg",
            expected_size=1,
        )
    assert existing.read_bytes() == b"keep"
    assert not (outside / "escaped.jpg").exists()


def test_download_rejects_parent_symlink_even_when_target_stays_inside_root(tmp_path):
    media_root = tmp_path / "media"
    real_parent = media_root / "real"
    media_root.mkdir()
    real_parent.mkdir()
    (media_root / "alias").symlink_to(real_parent, target_is_directory=True)
    requests_client = FakeRequests(get_outcomes=[FakeResponse(chunks=[b"unsafe"])])
    api = TelegramApi("123456:secret", media_root, requests_client=requests_client)
    with pytest.raises(ValueError, match="must not contain symlinks"):
        api.download_file(
            "photos/a.jpg",
            media_root / "alias" / "escaped.jpg",
            message_filename="escaped.jpg",
            mime_type="image/jpeg",
            expected_size=1,
        )
    assert requests_client.gets == []
    assert not (real_parent / "escaped.jpg").exists()


def test_download_rejects_dotdot_as_a_lexical_parent_component(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    requests_client = FakeRequests(get_outcomes=[FakeResponse(chunks=[b"unsafe"])])
    api = TelegramApi("123456:secret", media_root, requests_client=requests_client)
    destination = media_root / "parent" / ".." / "escaped.jpg"
    with pytest.raises(ValueError, match="Invalid download destination"):
        api.download_file(
            "photos/a.jpg",
            destination,
            message_filename="escaped.jpg",
            mime_type="image/jpeg",
            expected_size=1,
        )
    assert requests_client.gets == []
    assert not (media_root / "escaped.jpg").exists()


def test_download_fails_closed_without_nofollow_support(tmp_path, monkeypatch):
    media_root = tmp_path / "media"
    media_root.mkdir()
    requests_client = FakeRequests(get_outcomes=[FakeResponse(chunks=[b"unsafe"])])
    api = TelegramApi("123456:secret", media_root, requests_client=requests_client)
    monkeypatch.delattr("modules.telegram_api.os.O_NOFOLLOW")
    with pytest.raises(RuntimeError, match="secure_nofollow_unavailable"):
        api.download_file(
            "photos/a.jpg",
            media_root / "new.jpg",
            message_filename="new.jpg",
            mime_type="image/jpeg",
            expected_size=1,
        )
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
        api.download_file(
            "photos/a.jpg",
            destination,
            message_filename="new.jpg",
            mime_type="image/jpeg",
            expected_size=7,
        )
    assert secret not in str(raised.value)
    assert not destination.exists()


def test_download_close_failure_cleans_file_and_fds_repeatedly(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    secret = "123456789:telegram_Bot-Secret"
    responses = [
        FakeResponse(
            chunks=[b"complete"],
            close_error=RuntimeError(
                f"response body=query headers=Bearer {secret} raw update_id=8"
            ),
        )
        for _ in range(20)
    ]
    api = TelegramApi(
        secret,
        media_root,
        requests_client=FakeRequests(get_outcomes=responses),
    )
    fd_count_before = len(os.listdir("/dev/fd"))
    destinations = []
    errors = []
    for index in range(20):
        destination = media_root / f"close-{index}.jpg"
        destinations.append(destination)
        with pytest.raises(TelegramApiError) as raised:
            api.download_file(
                "photos/a.jpg",
                destination,
                message_filename=f"close-{index}.jpg",
                mime_type="image/jpeg",
                expected_size=8,
            )
        errors.append(str(raised.value))
    assert len(os.listdir("/dev/fd")) == fd_count_before
    assert all(not destination.exists() for destination in destinations)
    assert all(
        error == "operation=transport method=downloadFile exception=RuntimeError"
        for error in errors
    )
    assert all(response.close_calls == 1 for response in responses)


def test_download_enforces_content_length_and_counted_image_limit(
    tmp_path,
    monkeypatch,
):
    media_root = tmp_path / "media"
    media_root.mkdir()
    monkeypatch.setattr(telegram_api_module, "TELEGRAM_MAX_IMAGE_BYTES", 5)
    declared = FakeResponse(
        chunks=[pytest.fail],
        headers={"Content-Length": "6"},
    )
    streamed = FakeResponse(chunks=[b"123", b"456"])
    api = TelegramApi(
        "123456:secret",
        media_root,
        requests_client=FakeRequests(get_outcomes=[declared, streamed]),
    )
    for name in ("declared.jpg", "streamed.jpg"):
        with pytest.raises(TelegramApiError) as raised:
            api.download_file(
                "photos/a.jpg",
                media_root / name,
                message_filename=name,
                mime_type="image/jpeg",
                expected_size=5,
            )
        assert str(raised.value) == (
            "operation=download method=downloadFile code=file_too_large"
        )
        assert not (media_root / name).exists()
    assert declared.close_calls == 1
    assert streamed.close_calls == 1


def test_download_writes_only_to_reserved_destination(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    destination = media_root / "new.jpg"
    requests_client = FakeRequests(get_outcomes=[
        FakeResponse(chunks=[b"abc", b"", b"def"]),
    ])
    api = TelegramApi("123456:secret", media_root, requests_client=requests_client)
    assert api.download_file(
        "photos/a.jpg",
        destination,
        message_filename="new.jpg",
        mime_type="image/jpeg",
        expected_size=6,
    ) == destination
    assert destination.read_bytes() == b"abcdef"
    url, kwargs = requests_client.gets[0]
    assert url == "https://api.telegram.org/file/bot123456:secret/photos/a.jpg"
    assert kwargs == {"stream": True, "timeout": REQUEST_TIMEOUT}


@pytest.mark.parametrize("content_length", ["5", None])
def test_download_rejects_bytes_shorter_than_canonical_expected_size(
    tmp_path,
    content_length,
):
    media_root = tmp_path / "media"
    media_root.mkdir()
    destination = media_root / "short.jpg"
    headers = {} if content_length is None else {"Content-Length": content_length}
    response = FakeResponse(chunks=[b"12345"], headers=headers)
    api = TelegramApi(
        "123456:secret",
        media_root,
        requests_client=FakeRequests(get_outcomes=[response]),
    )

    with pytest.raises(TelegramApiError) as raised:
        api.download_file(
            "photos/a.jpg",
            destination,
            message_filename="short.jpg",
            mime_type="image/jpeg",
            expected_size=6,
        )

    assert str(raised.value) == (
        "operation=download method=downloadFile code=invalid_media_metadata"
    )
    assert not destination.exists()


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
    assert fake_db.logged_errors[0] == (
        "operation=telegram_update",
        "RuntimeError",
        "operation=telegram_update exception=RuntimeError",
    )
    for forbidden in ("Authorization", "Bearer", "https://", "?token", "path"):
        assert forbidden not in persisted
        assert forbidden not in sent


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
    assert fake_db.logged_errors[0][2] == (
        "operation=telegram_update exception=RuntimeError"
    )
    assert "update_id" not in fake_telegram.messages[0][1]
    assert "private" not in fake_telegram.messages[0][1]


def test_notifier_allowlists_exception_class_before_db_and_telegram(
    fake_db,
    fake_telegram,
):
    private_exception = type("Bearer_private_token", (RuntimeError,), {})
    notifier = TelegramNotifier(
        "123456:secret",
        "42",
        database=fake_db,
        telegram_api=fake_telegram,
    )
    notifier.notify_error("telegram_update", private_exception("body=private"))
    assert fake_db.logged_errors[0] == (
        "operation=telegram_update",
        "RuntimeError",
        "operation=telegram_update exception=RuntimeError",
    )
    persisted_and_sent = " ".join(fake_db.logged_errors[0]) + fake_telegram.messages[0][1]
    assert "Bearer" not in persisted_and_sent
    assert "token" not in persisted_and_sent
    assert "private" not in persisted_and_sent


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
