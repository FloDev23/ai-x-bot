import io
import os

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
    media_dir.mkdir()
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
    assert (media_dir / "gym.jpg").read_bytes() == JPEG_BYTES
    assert database.get_all_media()[0]["user_context"] == "Real studio floor"
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
