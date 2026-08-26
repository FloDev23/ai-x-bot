import importlib
import importlib.util
import io
from pathlib import Path

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
