"""Approval-only, idempotent publication of persisted post drafts."""

from dataclasses import dataclass
from datetime import datetime, timedelta
import inspect
import logging
from zoneinfo import ZoneInfo

from modules.media_store import open_verified_media
from modules.twitter_client import (
    XPublicationPaused,
    XPublicationRejected,
    XPublicationUnknown,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublishResult:
    status: str
    tweet_id: str = ""


class Publisher:
    def __init__(
        self,
        db,
        x_client,
        dry_run=True,
        clock=None,
        grace_seconds=300,
        timezone_name="Europe/Rome",
    ):
        self.db = db
        self.x_client = x_client
        self.dry_run = dry_run
        self.timezone = ZoneInfo(timezone_name)
        self.clock = clock or (lambda: datetime.now(self.timezone))
        self.grace_seconds = grace_seconds

    def publish(self, draft_id, now=None):
        draft = self.db.get_post_draft(draft_id)
        if not draft:
            return PublishResult("not_found")
        if draft.get("published_tweet_id"):
            return PublishResult(
                "already_published", str(draft["published_tweet_id"])
            )
        if draft.get("status") != "approved":
            return PublishResult("not_publishable")

        try:
            current = self._as_aware(now if now is not None else self.clock())
            slot = self._as_aware(datetime.fromisoformat(draft["intended_slot"]))
        except (TypeError, ValueError):
            return PublishResult("not_publishable")
        if current < slot:
            return PublishResult("not_due")
        if current > slot + timedelta(seconds=self.grace_seconds):
            self.db.transition_post_draft(draft_id, ["approved"], "expired")
            return PublishResult("expired")
        if self._publication_is_paused():
            return PublishResult("paused")
        if self.dry_run:
            return PublishResult("dry_run")

        try:
            claimed = self.db.transition_post_draft(
                draft_id, ["approved"], "publishing"
            )
        except Exception as error:
            logger.error(
                "publication_claim_failed draft_id=%s error_type=%s",
                draft_id,
                type(error).__name__,
            )
            return PublishResult("publication_failed")
        if not claimed:
            return PublishResult("already_claimed")
        return self._write_claimed_draft(draft)

    def _as_aware(self, value):
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime):
            raise TypeError("invalid_publication_time")
        if value.tzinfo is None:
            return value.replace(tzinfo=self.timezone)
        return value

    def _publication_is_paused(self):
        try:
            return self.db.get_state("paused", "false") == "true"
        except Exception:
            return True

    def _publication_gate_open(self):
        return not self._publication_is_paused()

    def _write_claimed_draft(self, draft):
        media_id = draft.get("media_id")
        if media_id is None:
            return self._write_to_x(draft, None, "image", None)

        try:
            media = self.db.get_media_by_id(media_id)
        except Exception as error:
            return self._definite_local_failure(draft, error)
        if (
            not media
            or media.get("id") != media_id
            or media.get("lifecycle_state") != "reserved"
            or media.get("reserved_by_draft_id") != draft["id"]
            or media.get("file_deleted")
        ):
            return self._definite_local_failure(
                draft, ValueError("invalid_media_reservation")
            )

        media_opened = False
        write_result = None
        try:
            with open_verified_media(media) as media_file:
                media_opened = True
                write_result = self._write_to_x(
                    draft,
                    media_file,
                    media.get("media_type") or "image",
                    media.get("filename"),
                )
                return write_result
        except (OSError, PermissionError, RuntimeError, ValueError) as error:
            if write_result is not None:
                if write_result.status == "published":
                    return self._unknown_outcome(draft["id"], error)
                return write_result
            if media_opened:
                return self._unknown_outcome(draft["id"], error)
            return self._definite_local_failure(draft, error)

    def _write_to_x(self, draft, media_file, media_type, media_filename):
        if self._publication_is_paused():
            return self._restore_after_pause(draft["id"])

        try:
            response = self._call_post_tweet(
                draft["text"], media_file, media_type, media_filename
            )
        except XPublicationPaused:
            return self._restore_after_pause(draft["id"])
        except XPublicationRejected as error:
            return self._definite_x_failure(draft, error)
        except (XPublicationUnknown, TimeoutError, ConnectionError) as error:
            return self._unknown_outcome(draft["id"], error)
        except Exception as error:
            # An arbitrary client error may have happened after the write.  It
            # is therefore unsafe to reinterpret it as a definite rejection.
            return self._unknown_outcome(draft["id"], error)

        tweet_id = self._tweet_id(response)
        if not tweet_id:
            return self._unknown_outcome(
                draft["id"], ValueError("publication_response_missing_id")
            )
        try:
            finalized = self._finalize_success(draft, tweet_id)
        except Exception as error:
            return self._unknown_outcome(draft["id"], error)
        if not finalized:
            return self._unknown_outcome(
                draft["id"], RuntimeError("publication_finalization_conflict")
            )
        return PublishResult("published", tweet_id)

    def _call_post_tweet(
        self, text, media_file, media_type, media_filename
    ):
        method = self.x_client.post_tweet
        try:
            parameters = inspect.signature(method).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        names = {parameter.name for parameter in parameters}
        accepts_keywords = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        kwargs = {}
        if accepts_keywords or "before_write" in names:
            kwargs["before_write"] = self._publication_gate_open
        if accepts_keywords or "media_filename" in names:
            kwargs["media_filename"] = media_filename
        if media_file is None:
            return method(text, **kwargs)
        return method(text, media_file, media_type, **kwargs)

    @staticmethod
    def _tweet_id(response):
        data = getattr(response, "data", response)
        if not isinstance(data, dict):
            return ""
        tweet_id = data.get("id")
        return str(tweet_id) if tweet_id else ""

    def _finalize_success(self, draft, tweet_id):
        finalize = getattr(
            self.db, "finalize_post_draft_publication", None
        )
        if callable(finalize):
            return finalize(draft["id"], tweet_id)

        if draft.get("media_id") is not None:
            mark_used = getattr(self.db, "mark_media_used", None)
            if not callable(mark_used):
                return False
            mark_used(draft["media_id"], tweet_id)
        return self.db.transition_post_draft(
            draft["id"],
            ["publishing"],
            "published",
            published_tweet_id=tweet_id,
        )

    def _restore_after_pause(self, draft_id):
        try:
            self.db.transition_post_draft(
                draft_id, ["publishing"], "approved", error=None
            )
        except Exception:
            pass
        return PublishResult("paused")

    def _definite_local_failure(self, draft, error):
        return self._definite_x_failure(draft, error)

    def _definite_x_failure(self, draft, error):
        safe_error = type(error).__name__
        fail = getattr(self.db, "fail_post_draft_publication", None)
        try:
            if callable(fail):
                fail(draft["id"], safe_error)
            else:
                changed = self.db.transition_post_draft(
                    draft["id"],
                    ["publishing"],
                    "publication_failed",
                    error=safe_error,
                )
                if changed and draft.get("media_id") is not None:
                    release = getattr(self.db, "release_media_for_draft", None)
                    if callable(release):
                        release(draft["id"])
        except Exception as persistence_error:
            logger.error(
                "publication_failure_persistence_failed draft_id=%s "
                "error_type=%s",
                draft["id"],
                type(persistence_error).__name__,
            )
        return PublishResult("publication_failed")

    def _unknown_outcome(self, draft_id, error):
        try:
            self.db.transition_post_draft(
                draft_id,
                ["publishing"],
                "publication_unknown",
                error=type(error).__name__,
            )
        except Exception as persistence_error:
            logger.error(
                "publication_unknown_persistence_failed draft_id=%s "
                "error_type=%s",
                draft_id,
                type(persistence_error).__name__,
            )
        return PublishResult("publication_unknown")
