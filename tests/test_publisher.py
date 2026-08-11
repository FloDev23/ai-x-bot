from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import threading
from types import SimpleNamespace

import pytest
from requests import exceptions as requests_exceptions

from modules.database import Database, PostDraftPublicationClaim
from modules.media_processor import MediaProcessor
from modules.publisher import Publisher
from modules.twitter_client import TwitterClient


SLOT = datetime.fromisoformat("2030-01-10T14:00:00+01:00")


class FakePublisherDatabase:
    def __init__(self):
        self.slot = SLOT
        self.paused = False
        self.draft = {
            "id": 7,
            "publication_key": "draft:publisher",
            "text": "A useful, approved post.",
            "category": "gym_strategy",
            "source_ids": [1],
            "score_data": {"total": 88},
            "intended_slot": SLOT.isoformat(),
            "status": "approved",
            "media_id": None,
            "published_tweet_id": None,
            "revision": 1,
        }

    def get_post_draft(self, draft_id):
        if draft_id != self.draft["id"]:
            return None
        return deepcopy(self.draft)

    def transition_post_draft(
        self, draft_id, expected_statuses, new_status, **changes
    ):
        if (
            draft_id != self.draft["id"]
            or self.draft["status"] not in expected_statuses
        ):
            return False
        self.draft.update(changes)
        self.draft["status"] = new_status
        self.draft["revision"] += 1
        return True

    def claim_post_draft_for_publication(self, draft_id, expected_revision):
        if (
            draft_id != self.draft["id"]
            or self.draft["status"] != "approved"
            or self.draft["revision"] != expected_revision
        ):
            return None
        self.draft["status"] = "publishing"
        self.draft["revision"] += 1
        claim = PostDraftPublicationClaim(
            draft_id=self.draft["id"],
            revision=self.draft["revision"],
            publication_key=self.draft["publication_key"],
            text=self.draft["text"],
            category=self.draft["category"],
            source_ids_json=json.dumps(self.draft["source_ids"]),
            score_json=json.dumps(self.draft["score_data"]),
            intended_slot=self.draft["intended_slot"],
            media_id=self.draft["media_id"],
            approved_at=None,
            approved_by=None,
        )
        return deepcopy(self.draft), claim

    def _claim_matches(self, claim):
        return (
            self.draft["id"] == claim.draft_id
            and self.draft["status"] == "publishing"
            and self.draft["revision"] == claim.revision
            and self.draft["text"] == claim.text
        )

    def finalize_post_draft_publication(
        self, claim, tweet_id, expected_media=None
    ):
        if expected_media is not None or not self._claim_matches(claim):
            return False
        self.draft["status"] = "published"
        self.draft["published_tweet_id"] = tweet_id
        self.draft["revision"] += 1
        return True

    def fail_post_draft_publication(self, claim, safe_error):
        if not self._claim_matches(claim):
            return False
        self.draft["status"] = "publication_failed"
        self.draft["error"] = safe_error
        self.draft["revision"] += 1
        return True

    def restore_post_draft_publication_claim(self, claim):
        if not self._claim_matches(claim):
            return False
        self.draft["status"] = "approved"
        self.draft["revision"] += 1
        return True

    def mark_post_draft_publication_unknown(self, claim, safe_error):
        if not self._claim_matches(claim):
            return False
        self.draft["status"] = "publication_unknown"
        self.draft["error"] = safe_error
        self.draft["revision"] += 1
        return True

    def get_state(self, key, default=None):
        assert key == "paused"
        return "true" if self.paused else default


class FakeXClient:
    def __init__(self):
        self.posts = []
        self.raise_timeout = False

    def post_tweet(self, text, *_args, **_kwargs):
        if self.raise_timeout:
            raise TimeoutError("X response timed out")
        self.posts.append(text)
        return SimpleNamespace(data={"id": "tweet-123"})


@pytest.fixture
def fake_db():
    return FakePublisherDatabase()


@pytest.fixture
def fake_x():
    return FakeXClient()


@pytest.fixture
def publisher(fake_db, fake_x):
    return Publisher(fake_db, fake_x, dry_run=False)


def test_unapproved_draft_never_calls_x(publisher, fake_x, fake_db):
    fake_db.draft["status"] = "pending_approval"

    result = publisher.publish(fake_db.draft["id"], fake_db.slot)

    assert result.status == "not_publishable"
    assert fake_db.draft["status"] == "pending_approval"
    assert fake_x.posts == []


def test_pause_is_checked_immediately_before_write(publisher, fake_x, fake_db):
    fake_db.paused = True

    result = publisher.publish(fake_db.draft["id"], fake_db.slot)

    assert result.status == "paused"
    assert fake_db.draft["status"] == "approved"
    assert fake_x.posts == []


def test_second_publish_is_idempotent(publisher, fake_x, fake_db):
    first = publisher.publish(fake_db.draft["id"], fake_db.slot)
    second = publisher.publish(fake_db.draft["id"], fake_db.slot)

    assert first.status == "published"
    assert first.tweet_id == "tweet-123"
    assert second.status == "already_published"
    assert second.tweet_id == "tweet-123"
    assert fake_db.draft["status"] == "published"
    assert len(fake_x.posts) == 1


def test_timeout_becomes_unknown_without_retry(publisher, fake_x, fake_db):
    fake_x.raise_timeout = True

    first = publisher.publish(fake_db.draft["id"], fake_db.slot)
    second = publisher.publish(fake_db.draft["id"], fake_db.slot)

    assert first.status == "publication_unknown"
    assert second.status == "not_publishable"
    assert fake_db.draft["status"] == "publication_unknown"
    assert fake_x.posts == []


def test_dry_run_keeps_the_approved_draft_without_calling_x(fake_db, fake_x):
    result = Publisher(fake_db, fake_x).publish(fake_db.draft["id"], SLOT)

    assert result.status == "dry_run"
    assert fake_db.draft["status"] == "approved"
    assert fake_x.posts == []


def test_approved_draft_is_not_publishable_before_its_slot(fake_db, fake_x):
    result = Publisher(fake_db, fake_x, dry_run=False).publish(
        fake_db.draft["id"], SLOT - timedelta(microseconds=1)
    )

    assert result.status == "not_due"
    assert fake_db.draft["status"] == "approved"
    assert fake_x.posts == []


def test_approved_draft_expires_after_the_five_minute_grace(fake_db, fake_x):
    result = Publisher(fake_db, fake_x, dry_run=False).publish(
        fake_db.draft["id"], SLOT + timedelta(seconds=301)
    )

    assert result.status == "expired"
    assert fake_db.draft["status"] == "expired"
    assert fake_x.posts == []


def _approved_sqlite_draft(db, *, slot, key, media_record=None):
    source_id = db.add_content_source("evergreen_idea", "Verified source.")
    draft_id = db.create_post_draft(
        text="A useful, approved post.",
        category="gym_strategy",
        source_ids=[source_id],
        score_data={"total": 88},
        intended_slot=slot.isoformat(),
        publication_key=key,
    )
    if media_record is not None:
        assert db.attach_media_to_draft(media_record["id"], draft_id)
    assert db.transition_post_draft(
        draft_id,
        ["pending_approval"],
        "approved",
        approved_at=(slot - timedelta(minutes=30)).isoformat(),
        approved_by="floriano",
    )
    return draft_id


class RecordingXClient:
    def __init__(self, *, tweet_id="tweet-sqlite"):
        self.tweet_id = tweet_id
        self.posts = []
        self.media_bytes = None
        self._lock = threading.Lock()

    def post_tweet(
        self,
        text,
        media_path=None,
        media_type="image",
        **_kwargs,
    ):
        media_bytes = media_path.read() if media_path is not None else None
        with self._lock:
            self.posts.append(text)
            self.media_bytes = media_bytes
        return SimpleNamespace(data={"id": self.tweet_id})


class BarrierReadDatabase(Database):
    def __init__(self, db_path, barrier):
        self._publisher_barrier = barrier
        super().__init__(db_path)

    def get_post_draft(self, draft_id):
        draft = super().get_post_draft(draft_id)
        if draft and draft["status"] == "approved":
            self._publisher_barrier.wait(timeout=5)
        return draft


class PauseAfterApprovedReadDatabase(Database):
    def __init__(self, db_path, draft_read, continue_publish):
        self._draft_read = draft_read
        self._continue_publish = continue_publish
        self._paused_once = False
        super().__init__(db_path)

    def get_post_draft(self, draft_id):
        draft = super().get_post_draft(draft_id)
        if (
            draft
            and draft["status"] == "approved"
            and not self._paused_once
        ):
            self._paused_once = True
            self._draft_read.set()
            assert self._continue_publish.wait(timeout=5)
        return draft


def test_revision_change_between_read_and_claim_never_publishes_stale_text(
    tmp_path,
):
    path = str(tmp_path / "bot.db")
    setup = Database(path)
    draft_id = _approved_sqlite_draft(
        setup, slot=SLOT, key="publisher-revision-race"
    )
    draft_read = threading.Event()
    continue_publish = threading.Event()
    publisher_db = PauseAfterApprovedReadDatabase(
        path, draft_read, continue_publish
    )
    x_client = RecordingXClient()
    outcome = {}

    def publish():
        try:
            outcome["result"] = Publisher(
                publisher_db, x_client, dry_run=False
            ).publish(draft_id, SLOT)
        except Exception as error:  # pragma: no cover - surfaced below
            outcome["error"] = error

    thread = threading.Thread(target=publish)
    thread.start()
    try:
        assert draft_read.wait(timeout=5)
        assert setup.transition_post_draft(
            draft_id,
            ["approved"],
            "approved",
            text="NEW approved text",
        )
    finally:
        continue_publish.set()
        thread.join(timeout=8)

    assert not thread.is_alive()
    assert "error" not in outcome
    assert outcome["result"].status == "already_claimed"
    assert x_client.posts == []
    stored = setup.get_post_draft(draft_id)
    assert stored["status"] == "approved"
    assert stored["text"] == "NEW approved text"
    with setup._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM posted_tweets").fetchone()[0] == 0


def test_two_sqlite_workers_make_at_most_one_x_call(tmp_path):
    path = str(tmp_path / "bot.db")
    setup = Database(path)
    draft_id = _approved_sqlite_draft(
        setup, slot=SLOT, key="publisher-concurrency"
    )
    barrier = threading.Barrier(2)
    clients = RecordingXClient()
    databases = [
        BarrierReadDatabase(path, barrier),
        BarrierReadDatabase(path, barrier),
    ]
    results = [None, None]
    errors = []

    def publish(index):
        try:
            results[index] = Publisher(
                databases[index], clients, dry_run=False
            ).publish(draft_id, SLOT)
        except Exception as error:  # pragma: no cover - surfaced below
            errors.append(error)

    threads = [threading.Thread(target=publish, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=8)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert sorted(result.status for result in results) == [
        "already_claimed",
        "published",
    ]
    assert clients.posts == ["A useful, approved post."]
    stored = setup.get_post_draft(draft_id)
    assert stored["status"] == "published"
    assert stored["published_tweet_id"] == "tweet-sqlite"


JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"publisher-jpeg"


def _stored_media(db, tmp_path, filename="gym.jpg"):
    tmp_path.chmod(0o700)
    staged = tmp_path / (".upload-" + filename)
    staged.write_bytes(JPEG_BYTES)
    return MediaProcessor(db).process_new_file(
        str(staged), filename, "image/jpeg", len(JPEG_BYTES), "Studio floor"
    )


def test_verified_reserved_media_is_uploaded_and_preserved_on_success(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    media = _stored_media(db, tmp_path)
    draft_id = _approved_sqlite_draft(
        db, slot=SLOT, key="publisher-media-success", media_record=media
    )
    x_client = RecordingXClient(tweet_id="tweet-with-media")

    result = Publisher(db, x_client, dry_run=False).publish(draft_id, SLOT)

    assert result.status == "published"
    assert result.tweet_id == "tweet-with-media"
    assert x_client.media_bytes == JPEG_BYTES
    assert (tmp_path / "gym.jpg").read_bytes() == JPEG_BYTES
    stored_media = db.get_media_by_id(media["id"])
    assert stored_media["lifecycle_state"] == "used"
    assert stored_media["used_in_tweet_id"] == "tweet-with-media"
    assert stored_media["file_deleted"] == 0
    stored_draft = db.get_post_draft(draft_id)
    assert stored_draft["status"] == "published"
    assert stored_draft["published_tweet_id"] == "tweet-with-media"
    with db._conn() as conn:
        posted = conn.execute(
            "SELECT tweet_id, text, category FROM posted_tweets"
        ).fetchall()
    assert [tuple(row) for row in posted] == [
        ("tweet-with-media", "A useful, approved post.", "gym_strategy")
    ]


def test_media_reassigned_before_root_lease_is_never_uploaded(
    tmp_path, monkeypatch
):
    from modules import publisher as publisher_module

    path = str(tmp_path / "bot.db")
    setup = Database(path)
    media = _stored_media(setup, tmp_path)
    draft_a = _approved_sqlite_draft(
        setup,
        slot=SLOT,
        key="publisher-media-race-a",
        media_record=media,
    )
    draft_b = setup.create_post_draft(
        text="Draft B owns the media now.",
        category="gym_strategy",
        source_ids=[setup.add_content_source("evergreen_idea", "Source B.")],
        score_data={"total": 77},
        intended_slot=(SLOT + timedelta(days=1)).isoformat(),
        publication_key="publisher-media-race-b",
    )
    before_root_open = threading.Event()
    continue_open = threading.Event()
    real_open_verified_media = publisher_module.open_verified_media

    @contextmanager
    def pause_before_root_lease(record):
        before_root_open.set()
        assert continue_open.wait(timeout=5)
        with real_open_verified_media(record) as media_file:
            yield media_file

    monkeypatch.setattr(
        publisher_module, "open_verified_media", pause_before_root_lease
    )
    x_client = RecordingXClient()
    outcome = {}

    def publish():
        try:
            outcome["result"] = Publisher(
                Database(path), x_client, dry_run=False
            ).publish(draft_a, SLOT)
        except Exception as error:  # pragma: no cover - surfaced below
            outcome["error"] = error

    thread = threading.Thread(target=publish)
    thread.start()
    try:
        assert before_root_open.wait(timeout=5)
        setup.release_media_for_draft(draft_a)
        assert setup.attach_media_to_draft(media["id"], draft_b)
    finally:
        continue_open.set()
        thread.join(timeout=8)

    assert not thread.is_alive()
    assert "error" not in outcome
    assert outcome["result"].status == "publication_failed"
    assert x_client.posts == []
    assert x_client.media_bytes is None
    assert setup.get_post_draft(draft_a)["status"] == "publication_failed"
    assert setup.get_post_draft(draft_b)["media_id"] == media["id"]
    stored_media = setup.get_media_by_id(media["id"])
    assert stored_media["lifecycle_state"] == "reserved"
    assert stored_media["reserved_by_draft_id"] == draft_b
    with setup._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM posted_tweets").fetchone()[0] == 0


class RejectingXClient(RecordingXClient):
    def post_tweet(self, *_args, **_kwargs):
        from modules.twitter_client import XPublicationRejected

        raise XPublicationRejected("x_rejected_publication")


def test_definite_rejection_marks_failed_and_releases_media(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    media = _stored_media(db, tmp_path)
    draft_id = _approved_sqlite_draft(
        db, slot=SLOT, key="publisher-media-rejected", media_record=media
    )

    result = Publisher(db, RejectingXClient(), dry_run=False).publish(
        draft_id, SLOT
    )

    assert result.status == "publication_failed"
    assert db.get_post_draft(draft_id)["status"] == "publication_failed"
    stored_media = db.get_media_by_id(media["id"])
    assert stored_media["lifecycle_state"] == "available"
    assert stored_media["reserved_by_draft_id"] is None
    assert (tmp_path / "gym.jpg").read_bytes() == JPEG_BYTES


class UnknownXClient(RecordingXClient):
    def post_tweet(self, *_args, **_kwargs):
        from modules.twitter_client import XPublicationUnknown

        raise XPublicationUnknown("x_publication_outcome_unknown")


def test_unknown_outcome_keeps_media_reserved_and_is_never_retried(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    media = _stored_media(db, tmp_path)
    draft_id = _approved_sqlite_draft(
        db, slot=SLOT, key="publisher-media-unknown", media_record=media
    )
    x_client = UnknownXClient()
    publisher = Publisher(db, x_client, dry_run=False)

    first = publisher.publish(draft_id, SLOT)
    second = publisher.publish(draft_id, SLOT)

    assert first.status == "publication_unknown"
    assert second.status == "not_publishable"
    assert db.get_post_draft(draft_id)["status"] == "publication_unknown"
    stored_media = db.get_media_by_id(media["id"])
    assert stored_media["lifecycle_state"] == "reserved"
    assert stored_media["reserved_by_draft_id"] == draft_id
    assert (tmp_path / "gym.jpg").read_bytes() == JPEG_BYTES


def test_legacy_media_without_identity_fails_closed_before_x(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    media_path = tmp_path / "legacy.jpg"
    media_path.write_bytes(JPEG_BYTES)
    media_id = db.add_media("legacy.jpg", str(media_path), "image")
    draft_id = _approved_sqlite_draft(
        db, slot=SLOT, key="publisher-legacy-media"
    )
    assert db.reserve_media(media_id, draft_id)
    assert db.transition_post_draft(
        draft_id, ["approved"], "approved", media_id=media_id
    )
    x_client = RecordingXClient()

    result = Publisher(db, x_client, dry_run=False).publish(draft_id, SLOT)

    assert result.status == "publication_failed"
    assert x_client.posts == []
    assert db.get_post_draft(draft_id)["status"] == "publication_failed"
    stored_media = db.get_media_by_id(media_id)
    assert stored_media["lifecycle_state"] == "available"
    assert stored_media["reserved_by_draft_id"] is None
    assert media_path.read_bytes() == JPEG_BYTES


class UploadPausesDatabaseApi:
    def __init__(self, db):
        self.db = db
        self.uploaded = []

    def media_upload(self, filename, file=None, **_kwargs):
        self.uploaded.append((filename, file.read()))
        self.db.set_state("paused", "true")
        return SimpleNamespace(media_id_string="uploaded-media-id")


class RecordingCreateTweetApi:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def create_tweet(self, **params):
        self.calls.append(params)
        if self.error:
            raise self.error
        return SimpleNamespace(data={"id": "tweet-after-upload"})


def _twitter_client(api, create_client):
    client = TwitterClient.__new__(TwitterClient)
    client.api = api
    client.client = create_client
    return client


def test_pause_after_media_upload_prevents_the_actual_tweet_write(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    media = _stored_media(db, tmp_path)
    draft_id = _approved_sqlite_draft(
        db, slot=SLOT, key="publisher-pause-after-upload", media_record=media
    )
    upload_api = UploadPausesDatabaseApi(db)
    create_api = RecordingCreateTweetApi()
    x_client = _twitter_client(upload_api, create_api)

    result = Publisher(db, x_client, dry_run=False).publish(draft_id, SLOT)

    assert result.status == "paused"
    assert upload_api.uploaded == [("gym.jpg", JPEG_BYTES)]
    assert create_api.calls == []
    assert db.get_post_draft(draft_id)["status"] == "approved"
    assert db.get_media_by_id(media["id"])["lifecycle_state"] == "reserved"


class FailingMediaUploadApi:
    def media_upload(self, *_args, **_kwargs):
        raise requests_exceptions.HTTPError(
            "media rejected",
            response=SimpleNamespace(status_code=400),
        )


def test_media_upload_rejection_never_falls_back_to_text_only(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    media = _stored_media(db, tmp_path)
    draft_id = _approved_sqlite_draft(
        db, slot=SLOT, key="publisher-upload-rejected", media_record=media
    )
    create_api = RecordingCreateTweetApi()
    x_client = _twitter_client(FailingMediaUploadApi(), create_api)

    result = Publisher(db, x_client, dry_run=False).publish(draft_id, SLOT)

    assert result.status == "publication_failed"
    assert create_api.calls == []
    assert db.get_post_draft(draft_id)["status"] == "publication_failed"
    assert db.get_media_by_id(media["id"])["lifecycle_state"] == "available"


@pytest.mark.parametrize("transport_error", [TimeoutError("timeout"), ConnectionError("reset")])
def test_twitter_client_classifies_transport_failures_as_unknown(
    transport_error,
):
    x_client = _twitter_client(object(), RecordingCreateTweetApi(transport_error))

    with pytest.raises(Exception) as caught:
        x_client.post_tweet("Approved text")

    assert type(caught.value).__name__ == "XPublicationUnknown"


def test_twitter_client_classifies_definite_api_error_as_rejected():
    x_client = _twitter_client(
        object(),
        RecordingCreateTweetApi(
            requests_exceptions.HTTPError(
                "bad request",
                response=SimpleNamespace(status_code=400),
            )
        ),
    )

    with pytest.raises(Exception) as caught:
        x_client.post_tweet("Approved text")

    assert type(caught.value).__name__ == "XPublicationRejected"


class SuccessfulMediaUploadApi:
    def media_upload(self, *_args, **_kwargs):
        return SimpleNamespace(media_id_string="uploaded-media-id")


def test_truncated_x_response_is_unknown_and_keeps_media_reserved(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    media = _stored_media(db, tmp_path)
    draft_id = _approved_sqlite_draft(
        db, slot=SLOT, key="publisher-truncated-response", media_record=media
    )
    create_api = RecordingCreateTweetApi(
        requests_exceptions.ChunkedEncodingError("response truncated")
    )
    x_client = _twitter_client(SuccessfulMediaUploadApi(), create_api)
    publisher = Publisher(db, x_client, dry_run=False)

    first = publisher.publish(draft_id, SLOT)
    second = publisher.publish(draft_id, SLOT)

    assert first.status == "publication_unknown"
    assert second.status == "not_publishable"
    assert len(create_api.calls) == 1
    assert db.get_post_draft(draft_id)["status"] == "publication_unknown"
    stored_media = db.get_media_by_id(media["id"])
    assert stored_media["lifecycle_state"] == "reserved"
    assert stored_media["reserved_by_draft_id"] == draft_id


def test_twitter_client_rejects_a_media_path_instead_of_reopening_it(tmp_path):
    media_path = tmp_path / "unverified.jpg"
    media_path.write_bytes(JPEG_BYTES)
    create_api = RecordingCreateTweetApi()
    x_client = _twitter_client(object(), create_api)

    with pytest.raises(Exception) as caught:
        x_client.post_tweet("Approved text", str(media_path), "image")

    assert type(caught.value).__name__ == "XPublicationRejected"
    assert create_api.calls == []


class FinalizeFailureDatabase(Database):
    def finalize_post_draft_publication(self, *_args, **_kwargs):
        raise RuntimeError("sqlite commit failed after X response")


def test_database_failure_after_x_success_becomes_unknown_without_retry(tmp_path):
    path = str(tmp_path / "bot.db")
    db = FinalizeFailureDatabase(path)
    draft_id = _approved_sqlite_draft(
        db, slot=SLOT, key="publisher-db-after-x"
    )
    x_client = RecordingXClient(tweet_id="tweet-db-failure")
    publisher = Publisher(db, x_client, dry_run=False)

    first = publisher.publish(draft_id, SLOT)
    second = publisher.publish(draft_id, SLOT)

    assert first.status == "publication_unknown"
    assert second.status == "not_publishable"
    assert x_client.posts == ["A useful, approved post."]
    assert db.get_post_draft(draft_id)["status"] == "publication_unknown"


def test_real_sqlite_finalization_failure_rolls_back_and_never_retries(
    tmp_path,
):
    db = Database(str(tmp_path / "bot.db"))
    media = _stored_media(db, tmp_path)
    draft_id = _approved_sqlite_draft(
        db, slot=SLOT, key="publisher-real-db-failure", media_record=media
    )
    with db._conn() as conn:
        conn.execute("""
            CREATE TRIGGER reject_published_transition
            BEFORE UPDATE OF status ON post_drafts
            WHEN NEW.status = 'published'
            BEGIN
                SELECT RAISE(ABORT, 'simulated finalization failure');
            END
        """)
    x_client = RecordingXClient(tweet_id="tweet-real-db-failure")
    publisher = Publisher(db, x_client, dry_run=False)

    first = publisher.publish(draft_id, SLOT)
    second = publisher.publish(draft_id, SLOT)

    assert first.status == "publication_unknown"
    assert second.status == "not_publishable"
    assert x_client.posts == ["A useful, approved post."]
    assert db.get_post_draft(draft_id)["status"] == "publication_unknown"
    stored_media = db.get_media_by_id(media["id"])
    assert stored_media["lifecycle_state"] == "reserved"
    assert stored_media["used_in_tweet_id"] is None
    with db._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM posted_tweets").fetchone()[0] == 0


def test_media_context_failure_after_x_success_is_not_called_a_rejection(
    tmp_path, monkeypatch
):
    from modules import publisher as publisher_module

    db = Database(str(tmp_path / "bot.db"))
    media = _stored_media(db, tmp_path)
    draft_id = _approved_sqlite_draft(
        db, slot=SLOT, key="publisher-media-exit-failure", media_record=media
    )
    real_open_verified_media = publisher_module.open_verified_media

    @contextmanager
    def fail_on_exit(record):
        with real_open_verified_media(record) as media_file:
            yield media_file
        raise OSError("media context exit failed after X")

    monkeypatch.setattr(publisher_module, "open_verified_media", fail_on_exit)
    x_client = RecordingXClient(tweet_id="tweet-before-exit-failure")
    publisher = Publisher(db, x_client, dry_run=False)

    first = publisher.publish(draft_id, SLOT)
    second = publisher.publish(draft_id, SLOT)

    assert first.status == "publication_unknown"
    assert second.status == "already_published"
    assert x_client.posts == ["A useful, approved post."]
    assert db.get_post_draft(draft_id)["status"] == "published"


def test_legacy_main_publish_helper_cannot_bypass_draft_approval():
    from main import FlexDropinGrowthAgent

    class TrapXClient:
        def __init__(self):
            self.calls = []

        def post_tweet(self, *_args, **_kwargs):
            self.calls.append("called")
            raise AssertionError("direct X write")

    agent = FlexDropinGrowthAgent.__new__(FlexDropinGrowthAgent)
    agent.twitter_client = TrapXClient()

    with pytest.raises(RuntimeError, match="legacy_direct_publication_disabled"):
        agent._publish(
            "Unsafe text",
            category="legacy",
            topic="",
            has_link=False,
            score_total=99,
            agent_used="legacy",
        )

    assert agent.twitter_client.calls == []
