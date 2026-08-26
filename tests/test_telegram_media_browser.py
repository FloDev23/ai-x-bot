import importlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from modules.database import Database
from modules.media_processor import MediaProcessor


JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"verified-browser-media"


class RecordingTelegramApi:
    def __init__(self):
        self.media_messages = []
        self.messages = []
        self.deleted_messages = []

    def send_media(self, chat_id, media, media_type, **kwargs):
        assert not isinstance(media, (str, bytes, Path))
        assert isinstance(media, io.BufferedIOBase)
        self.media_messages.append(
            (str(chat_id), media.read(), media_type, dict(kwargs))
        )
        return {"message_id": len(self.media_messages)}

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((str(chat_id), str(text), dict(kwargs)))
        return {"message_id": 1000 + len(self.messages)}

    def delete_message(self, chat_id, message_id):
        self.deleted_messages.append((str(chat_id), message_id))
        return True


def _stored_photo(tmp_path):
    root = tmp_path / "media"
    root.mkdir(mode=0o700)
    database = Database(str(tmp_path / "browser.sqlite"))
    staged = root / ".upload-staged.jpg"
    staged.write_bytes(JPEG_BYTES)
    record = MediaProcessor(database).process_new_file(
        str(staged), "private-studio.jpg", "image/jpeg", len(JPEG_BYTES),
        "Real studio floor",
    )
    return database, root, record


def test_browser_sends_verified_stream_with_safe_single_item_controls(tmp_path):
    """Catches pathname sends, locator disclosure, or missing browser controls."""
    spec = importlib.util.find_spec("modules.telegram_media_browser")
    assert spec is not None, "verified Telegram media browser is missing"
    MediaBrowser = importlib.import_module(
        "modules.telegram_media_browser"
    ).MediaBrowser
    database, root, record = _stored_photo(tmp_path)
    api = RecordingTelegramApi()

    token = MediaBrowser(database, api).show(
        chat_id="42", media_id=None, context="manual",
    )

    assert len(token) == 16
    assert api.media_messages[0][0:3] == ("42", JPEG_BYTES, "photo")
    caption = api.media_messages[0][3]["caption"]
    markup = api.media_messages[0][3]["reply_markup"]
    rendered = caption + repr(markup)
    labels = {
        button["text"]
        for row in markup["inline_keyboard"]
        for button in row
    }
    assert {"Precedente", "Successivo", "Usa questo", "Nessun media",
            "Gestisci media", "Annulla"} <= labels
    assert f"Media #{record['id']}" in caption
    assert "Real studio floor" in caption
    assert str(root) not in rendered
    assert record["filename"] not in rendered


def test_browser_replaces_prior_preview_only_after_new_verified_send(tmp_path):
    """Catches stale preview accumulation or deleting before a successful send."""
    from modules.telegram_media_browser import MediaBrowser

    database, _root, first = _stored_photo(tmp_path)
    second_path = _root / ".second.jpg"
    second_path.write_bytes(JPEG_BYTES + b"second")
    MediaProcessor(database).process_new_file(
        str(second_path), "second.jpg", "image/jpeg",
        len(JPEG_BYTES + b"second"), "Second studio floor",
    )
    api = RecordingTelegramApi()
    browser = MediaBrowser(database, api)

    token = browser.show(chat_id="42", media_id=first["id"], context="manual")

    assert browser.render(token=token, chat_id="42", direction="next") is True
    assert len(api.media_messages) == 2
    assert api.deleted_messages == [("42", 1)]


def test_browser_fails_closed_without_locator_leak_after_digest_swap(tmp_path):
    """Catches a browser reopening an unverified pathname after an inode swap."""
    from modules.telegram_media_browser import MediaBrowser

    database, root, record = _stored_photo(tmp_path)
    Path(record["filepath"]).unlink()
    Path(record["filepath"]).symlink_to(root / "outside.jpg")
    (root / "outside.jpg").write_bytes(JPEG_BYTES)
    api = RecordingTelegramApi()

    MediaBrowser(database, api).show(chat_id="42", media_id=record["id"], context="manual")

    rendered = " ".join(message[1] for message in api.messages)
    assert api.media_messages == []
    assert str(root) not in rendered
    assert record["filename"] not in rendered


def test_delete_protocol_quarantines_then_tombstones_exact_never_used_identity(tmp_path):
    """Catches a physical delete before an exact prepared DB identity exists."""
    database, _root, record = _stored_photo(tmp_path)

    assert database.delete_unused_media_safely(
        record["id"], record["revision"], record["file_sha256"],
    ) is True

    deleted = database.get_media_by_id(record["id"])
    assert deleted["lifecycle_state"] == "deleted"
    assert deleted["file_deleted"] == 1
    assert deleted["filepath"] == ""
    assert deleted["file_sha256"] is None
    assert not list(_root.glob(".delete-*"))


def test_restart_reconciles_quarantined_delete_intent_idempotently(tmp_path):
    """Catches a restart leaving a quarantined, still-usable DB record."""
    database, _root, record = _stored_photo(tmp_path)
    prepared, state = database.prepare_unused_media_delete_atomic(
        record["id"], record["revision"], record["file_sha256"],
    )
    assert state == "prepared"
    assert database.quarantine_unused_media_delete(prepared["intent_token"])

    restarted = Database(database.db_path)

    deleted = restarted.get_media_by_id(record["id"])
    assert deleted["lifecycle_state"] == "deleted"
    assert deleted["file_deleted"] == 1
    with restarted._conn() as conn:
        states = conn.execute("SELECT state FROM media_delete_intents").fetchall()
    assert [row["state"] for row in states] == ["complete"]


def test_media_view_rejects_boolean_ids_and_wrong_chat(tmp_path):
    """Catches permissive callback state decoding across chats."""
    database, _root, _record = _stored_photo(tmp_path)

    try:
        database.create_telegram_view(
            "42", "media_browser", {"target_ids": [True]},
        )
    except ValueError:
        pass
    else:
        raise AssertionError("boolean view id accepted")
    token = database.create_telegram_view(
        "42", "media_browser", {"target_ids": [], "filters": {}},
    )

    assert database.get_telegram_view(token, "99", "media_browser") is None


def test_quarantine_rejects_traversal_before_any_dirfd_mutation(tmp_path):
    """Catches a quarantine basename escaping the media root through dir_fd."""
    from modules.media_store import quarantine_verified_media

    database, root, record = _stored_photo(tmp_path)
    victim = tmp_path / "victim"
    victim.write_bytes(b"do-not-touch")

    with pytest.raises(ValueError, match="invalid_quarantine_name"):
        quarantine_verified_media(record, ".delete-/../../victim")

    assert victim.read_bytes() == b"do-not-touch"
    assert Path(record["filepath"]).exists()


def test_post_rename_fsync_failure_keeps_prepared_until_retry_succeeds(tmp_path, monkeypatch):
    """Catches claiming quarantined before the rename is directory-fsynced."""
    from modules import media_store

    database, root, record = _stored_photo(tmp_path)
    prepared, state = database.prepare_unused_media_delete_atomic(
        record["id"], record["revision"], record["file_sha256"],
    )
    assert state == "prepared"

    monkeypatch.setattr(
        media_store, "fsync_media_directory",
        lambda _fd: (_ for _ in ()).throw(OSError("injected fsync failure")),
    )

    assert database.quarantine_unused_media_delete(prepared["intent_token"]) is False

    with database._conn() as conn:
        intent = conn.execute("SELECT * FROM media_delete_intents").fetchone()
    assert intent["state"] == "prepared"
    assert intent["quarantine_name"] is None
    assert list(root.glob(".delete-*"))
    assert database.get_media_by_id(record["id"])["lifecycle_state"] == "deleting"

    monkeypatch.undo()
    assert database.quarantine_unused_media_delete(prepared["intent_token"]) is True
    with database._conn() as conn:
        recovered = conn.execute("SELECT * FROM media_delete_intents").fetchone()
    assert recovered["state"] == "quarantined"
    assert recovered["quarantine_name"] == ".delete-" + prepared["intent_token"]


def test_legacy_delete_media_preserves_prepared_intent_for_restart(tmp_path):
    """Catches the obsolete DELETE FROM path erasing deletion audit history."""
    database, _root, record = _stored_photo(tmp_path)
    prepared, state = database.prepare_unused_media_delete_atomic(
        record["id"], record["revision"], record["file_sha256"],
    )
    assert state == "prepared"

    assert database.delete_media(record["id"]) is False
    restarted = Database(database.db_path)

    assert restarted.get_media_by_id(record["id"])["lifecycle_state"] == "deleted"


def test_document_preview_rejects_jpeg_relabelled_as_pdf(tmp_path):
    """Catches document rendering that trusts a MIME label without content."""
    from modules.telegram_media_browser import MediaBrowser

    database, _root, record = _stored_photo(tmp_path)
    with database._conn() as conn:
        conn.execute(
            "UPDATE media_library SET media_type = 'document', mime_type = 'application/pdf' WHERE id = ?",
            (record["id"],),
        )
    api = RecordingTelegramApi()

    MediaBrowser(database, api).show(chat_id="42", media_id=record["id"], context="manual")

    assert api.media_messages == []


def test_caption_redacts_embedded_absolute_private_path():
    """Catches safe descriptions reflecting a private locator mid-sentence."""
    from modules.telegram_media_browser import MediaBrowser

    assert "private/var" not in MediaBrowser._safe_text(
        "Photo stored at /private/var/folders/secret.jpg for review",
    )


def test_management_actions_require_bound_view_and_render_archived_restore(tmp_path):
    """Catches cross-chat/stale actions and an archive browser hiding restore."""
    from modules.telegram_controller import TelegramController
    from modules.telegram_media_browser import MediaBrowser

    database, _root, record = _stored_photo(tmp_path)
    api = RecordingTelegramApi()
    browser = MediaBrowser(database, api)
    controller = TelegramController(
        api, database, SimpleNamespace(notify_error=lambda *_args: None), "42",
    )
    token = browser.show(chat_id="42", media_id=record["id"], context="manage")

    assert controller._media_browser_callback(
        "99", ["mb", "a", token, str(record["id"]), str(record["revision"])],
    ) == "media_unavailable"
    assert database.get_media_by_id(record["id"])["lifecycle_state"] == "available"

    assert controller._media_browser_callback(
        "42", ["mb", "a", token, str(record["id"]), str(record["revision"])],
    ) == "media_archived"
    archived = database.get_media_by_id(record["id"])
    manage_token = browser.show(chat_id="42", media_id=record["id"], context="manage")
    labels = {
        button["text"]
        for row in api.messages[-1][2]["reply_markup"]["inline_keyboard"]
        for button in row
    }
    assert "Ripristina" in labels
    assert controller._media_browser_callback(
        "42", ["mb", "r", manage_token, str(record["id"]), str(archived["revision"])],
    ) == "media_restored"


def test_hard_exit_after_rename_recovers_deterministic_quarantine(tmp_path):
    """Catches the rename→intent-update crash boundary leaving an orphan."""
    database, _root, record = _stored_photo(tmp_path)
    prepared, state = database.prepare_unused_media_delete_atomic(
        record["id"], record["revision"], record["file_sha256"],
    )
    assert state == "prepared"
    script = """
import os
import sys
from modules import media_store
media_store.fsync_media_directory = lambda _fd: os._exit(91)
from modules.database import Database
Database(sys.argv[1])
"""

    child = subprocess.run(
        [sys.executable, "-c", script, database.db_path],
        cwd=str(Path(__file__).resolve().parent.parent), check=False,
    )

    assert child.returncode == 91
    restarted = Database(database.db_path)
    assert restarted.get_media_by_id(record["id"])["lifecycle_state"] == "deleted"


def test_missing_quarantine_is_fsynced_before_restart_tombstone(tmp_path, monkeypatch):
    """Catches unlink→fsync crash recovery tombstoning an unpersisted unlink."""
    from modules import media_store

    database, root, record = _stored_photo(tmp_path)
    prepared, state = database.prepare_unused_media_delete_atomic(
        record["id"], record["revision"], record["file_sha256"],
    )
    assert state == "prepared"
    assert database.quarantine_unused_media_delete(prepared["intent_token"])
    with database._conn() as conn:
        intent = conn.execute("SELECT quarantine_name FROM media_delete_intents").fetchone()
    os.unlink(root / intent["quarantine_name"])
    calls = []
    real_fsync = media_store.fsync_media_directory
    monkeypatch.setattr(
        media_store, "fsync_media_directory",
        lambda fd: (calls.append(fd), real_fsync(fd))[1],
    )

    database.reconcile_media_delete_intents()

    assert calls
    assert database.get_media_by_id(record["id"])["lifecycle_state"] == "deleted"


def test_hard_exit_after_unlink_before_fsync_recovers_before_tombstone(tmp_path):
    """Catches tombstoning immediately after an unpersisted quarantine unlink."""
    database, _root, record = _stored_photo(tmp_path)
    prepared, state = database.prepare_unused_media_delete_atomic(
        record["id"], record["revision"], record["file_sha256"],
    )
    assert state == "prepared"
    assert database.quarantine_unused_media_delete(prepared["intent_token"])
    script = """
import os
import sys
from modules import media_store
media_store.fsync_media_directory = lambda _fd: os._exit(92)
from modules.database import Database
Database(sys.argv[1])
"""

    child = subprocess.run(
        [sys.executable, "-c", script, database.db_path],
        cwd=str(Path(__file__).resolve().parent.parent), check=False,
    )

    assert child.returncode == 92
    restarted = Database(database.db_path)
    assert restarted.get_media_by_id(record["id"])["lifecycle_state"] == "deleted"


def test_two_delete_callbacks_only_prepare_one_intent(tmp_path):
    """Catches concurrent confirmation callbacks preparing two deletes."""
    database, _root, record = _stored_photo(tmp_path)
    results = []

    def prepare():
        results.append(Database(database.db_path).prepare_unused_media_delete_atomic(
            record["id"], record["revision"], record["file_sha256"],
        )[1])

    threads = [threading.Thread(target=prepare) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert results.count("prepared") == 1
    assert len(results) == 2


def test_mutating_callback_rejects_expired_wrong_kind_and_target_view(tmp_path):
    """Catches action tokens being treated as cosmetic rather than authority."""
    from modules.telegram_controller import TelegramController
    from modules.telegram_media_browser import MediaBrowser

    database, _root, record = _stored_photo(tmp_path)
    api = RecordingTelegramApi()
    controller = TelegramController(
        api, database, SimpleNamespace(notify_error=lambda *_args: None), "42",
    )
    browser = MediaBrowser(database, api)
    token = browser.show(chat_id="42", media_id=record["id"], context="manage")
    with database._conn() as conn:
        conn.execute(
            "UPDATE telegram_views SET expires_at = '2000-01-01T00:00:00+00:00' WHERE token = ?",
            (token,),
        )
    assert controller._media_browser_callback(
        "42", ["mb", "a", token, str(record["id"]), str(record["revision"])],
    ) == "media_unavailable"

    wrong_kind = database.create_telegram_view(
        "42", "media_delete_confirm", {"target_ids": [record["id"]], "filters": {}},
    )
    assert controller._media_browser_callback(
        "42", ["mb", "a", wrong_kind, str(record["id"]), str(record["revision"])],
    ) == "media_unavailable"

    wrong_target = database.create_telegram_view(
        "42", "media_browser", {"target_ids": [999], "filters": {}},
    )
    assert controller._media_browser_callback(
        "42", ["mb", "a", wrong_target, str(record["id"]), str(record["revision"])],
    ) == "media_unavailable"
    assert database.get_media_by_id(record["id"])["lifecycle_state"] == "available"


def test_archive_race_cannot_beat_exact_delete_prepare(tmp_path):
    """Catches another lifecycle action racing a delete into a usable state."""
    database, _root, record = _stored_photo(tmp_path)
    barrier = threading.Barrier(3)
    outcomes = []

    def prepare():
        barrier.wait()
        outcomes.append(("delete", Database(database.db_path).prepare_unused_media_delete_atomic(
            record["id"], record["revision"], record["file_sha256"],
        )[1]))

    def archive():
        barrier.wait()
        outcomes.append(("archive", Database(database.db_path).archive_media_atomic(
            record["id"], record["revision"], record["file_sha256"],
        )))

    threads = [threading.Thread(target=prepare), threading.Thread(target=archive)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert len(outcomes) == 2
    current = database.get_media_by_id(record["id"])
    assert not (current["lifecycle_state"] == "deleting" and any(
        name == "archive" and result is True for name, result in outcomes
    ))


def test_reservation_race_cannot_beat_exact_delete_prepare(tmp_path):
    """Catches a reservation becoming live after deletion has fenced identity."""
    database, _root, record = _stored_photo(tmp_path)
    barrier = threading.Barrier(3)
    outcomes = []

    def prepare():
        barrier.wait()
        outcomes.append(("delete", Database(database.db_path).prepare_unused_media_delete_atomic(
            record["id"], record["revision"], record["file_sha256"],
        )[1]))

    def reserve():
        barrier.wait()
        outcomes.append(("reserve", Database(database.db_path).reserve_media(record["id"], 77)))

    threads = [threading.Thread(target=prepare), threading.Thread(target=reserve)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    current = database.get_media_by_id(record["id"])
    assert not (current["lifecycle_state"] == "deleting" and any(
        name == "reserve" and result is True for name, result in outcomes
    ))


def test_verified_preview_and_delete_do_not_leak_file_descriptors(tmp_path):
    """Catches FD retention from verified stream/quarantine error paths."""
    if not Path("/dev/fd").is_dir():
        pytest.skip("fd inventory unavailable")
    baseline = len(os.listdir("/dev/fd"))
    database, _root, record = _stored_photo(tmp_path)
    api = RecordingTelegramApi()
    from modules.telegram_media_browser import MediaBrowser

    MediaBrowser(database, api).show(chat_id="42", media_id=record["id"], context="manual")
    assert database.delete_unused_media_safely(
        record["id"], record["revision"], record["file_sha256"],
    )

    assert len(os.listdir("/dev/fd")) <= baseline + 1


def test_delete_prepare_fails_closed_on_swap_and_missing_nofollow(tmp_path, monkeypatch):
    """Catches deletion reopening a swapped pathname or lacking O_NOFOLLOW."""
    from modules import media_store

    database, root, record = _stored_photo(tmp_path)
    prepared, state = database.prepare_unused_media_delete_atomic(
        record["id"], record["revision"], record["file_sha256"],
    )
    assert state == "prepared"
    Path(record["filepath"]).unlink()
    outside = root / "outside.jpg"
    outside.write_bytes(JPEG_BYTES)
    Path(record["filepath"]).symlink_to(outside)

    assert database.quarantine_unused_media_delete(prepared["intent_token"]) is False
    assert outside.read_bytes() == JPEG_BYTES

    second = tmp_path / "second"
    second.mkdir()
    database2, root2, record2 = _stored_photo(second)
    prepared2, state2 = database2.prepare_unused_media_delete_atomic(
        record2["id"], record2["revision"], record2["file_sha256"],
    )
    assert state2 == "prepared"
    monkeypatch.setattr(media_store, "_NOFOLLOW", 0)
    assert database2.quarantine_unused_media_delete(prepared2["intent_token"]) is False
    assert Path(record2["filepath"]).exists()
