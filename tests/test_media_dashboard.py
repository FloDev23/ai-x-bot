import io
import os
import threading
from pathlib import Path

import pytest

pytest.importorskip("flask")

from dashboard import app as dashboard_app
from modules.database import Database
from modules.media_processor import MediaProcessor


JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"jpeg-data"


@pytest.fixture
def dashboard_client(tmp_path, monkeypatch):
    import werkzeug

    monkeypatch.setattr(werkzeug, "__version__", "test", raising=False)
    database = Database(str(tmp_path / "dashboard.db"))
    media_dir = tmp_path / "media"
    media_dir.mkdir(mode=0o700)
    monkeypatch.setattr(dashboard_app, "db", database)
    monkeypatch.setattr(dashboard_app, "MEDIA_DIR", str(media_dir))
    monkeypatch.setattr(
        dashboard_app, "media_processor", MediaProcessor(database, ai_generator=None),
    )
    dashboard_app.app.config.update(TESTING=True)
    return dashboard_app.app.test_client(), database, media_dir


def test_dashboard_rejects_spoofed_mime_before_saving(dashboard_client):
    client, database, media_dir = dashboard_client

    response = client.post(
        "/media/upload",
        data={
            "file": (io.BytesIO(b"not-a-jpeg"), "gym.jpg", "image/jpeg"),
            "user_context": "Studio floor",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert list(media_dir.iterdir()) == []
    assert database.get_all_media() == []


def test_dashboard_upload_persists_context_without_creating_draft(dashboard_client):
    client, database, media_dir = dashboard_client

    response = client.post(
        "/media/upload",
        data={
            "file": (io.BytesIO(JPEG_BYTES), "gym.jpg", "image/jpeg"),
            "user_context": "  Real studio floor  ",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    record = database.get_all_media()[0]
    assert Path(record["filepath"]).read_bytes() == JPEG_BYTES
    assert record["user_context"] == "Real studio floor"
    assert database.list_post_drafts() == []


def test_archive_keeps_file_and_permanent_delete_keeps_audit_row(dashboard_client):
    client, database, media_dir = dashboard_client
    media_path = media_dir / "gym.jpg"
    media_path.write_bytes(JPEG_BYTES)
    media_id = database.add_media("gym.jpg", str(media_path), "image")

    archived = client.post(f"/media/{media_id}/archive")

    assert archived.status_code == 302
    assert media_path.exists()
    assert database.get_media_by_id(media_id)["lifecycle_state"] == "archived"

    deleted = client.post(f"/media/{media_id}/delete")

    assert deleted.status_code == 302
    assert not media_path.exists()
    record = database.get_media_by_id(media_id)
    assert record["lifecycle_state"] == "deleted"
    assert record["file_deleted"] == 1


def test_permanent_delete_never_unlinks_path_outside_media_library(
    dashboard_client, tmp_path,
):
    client, database, _media_dir = dashboard_client
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(JPEG_BYTES)
    media_id = database.add_media("outside.jpg", str(outside), "image")

    response = client.post(f"/media/{media_id}/delete")

    assert response.status_code == 400
    assert os.path.exists(outside)
    assert database.get_media_by_id(media_id)["file_deleted"] == 0


def test_permanent_delete_reserved_returns_conflict_and_keeps_ownership(
    dashboard_client,
):
    client, database, media_dir = dashboard_client
    media_path = media_dir / "gym.jpg"
    media_path.write_bytes(JPEG_BYTES)
    media_id = database.add_media("gym.jpg", str(media_path), "image")
    assert database.reserve_media(media_id, 17)

    response = client.post(f"/media/{media_id}/delete")

    assert response.status_code == 409
    assert media_path.read_bytes() == JPEG_BYTES
    record = database.get_media_by_id(media_id)
    assert record["lifecycle_state"] == "reserved"
    assert record["reserved_by_draft_id"] == 17


def test_same_name_concurrent_uploads_get_distinct_files(
    dashboard_client, monkeypatch,
):
    _client, database, _media_dir = dashboard_client
    from modules import media_processor as media_module

    original_claim = media_module._claim_final_media_path
    barrier = threading.Barrier(2)

    def synchronized_claim(staged_path, filename, source_fd):
        barrier.wait(timeout=5)
        return original_claim(staged_path, filename, source_fd)

    monkeypatch.setattr(media_module, "_claim_final_media_path", synchronized_claim)
    responses = []

    def upload():
        client = dashboard_app.app.test_client()
        responses.append(client.post(
            "/media/upload",
            data={"file": (io.BytesIO(JPEG_BYTES), "gym.jpg", "image/jpeg")},
            content_type="multipart/form-data",
        ))

    threads = [threading.Thread(target=upload) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    records = database.get_all_media()
    assert [response.status_code for response in responses] == [302, 302]
    assert len(records) == 2
    assert len({record["filepath"] for record in records}) == 2
    assert all(Path(record["filepath"]).read_bytes() == JPEG_BYTES for record in records)


def test_symlink_swap_cannot_redirect_upload_outside_library(
    dashboard_client, tmp_path, monkeypatch,
):
    client, database, media_dir = dashboard_client
    from modules import media_processor as media_module

    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"keep-me")
    original_claim = media_module._claim_final_media_path

    def swap_before_claim(staged_path, filename, source_fd):
        (media_dir / filename).symlink_to(outside)
        return original_claim(staged_path, filename, source_fd)

    monkeypatch.setattr(media_module, "_claim_final_media_path", swap_before_claim)
    response = client.post(
        "/media/upload",
        data={"file": (io.BytesIO(JPEG_BYTES), "gym.jpg", "image/jpeg")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    assert outside.read_bytes() == b"keep-me"
    record = database.get_all_media()[0]
    stored_path = Path(record["filepath"])
    assert media_dir.resolve() in stored_path.resolve().parents
    assert not stored_path.is_symlink()


def test_processor_failure_leaves_no_orphan_upload(dashboard_client, monkeypatch):
    client, database, media_dir = dashboard_client

    class BrokenProcessor:
        def process_new_file(self, *_args, **_kwargs):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(dashboard_app, "media_processor", BrokenProcessor())
    response = client.post(
        "/media/upload",
        data={"file": (io.BytesIO(JPEG_BYTES), "gym.jpg", "image/jpeg")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 500
    assert list(media_dir.iterdir()) == []
    assert database.get_all_media() == []
