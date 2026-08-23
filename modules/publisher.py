"""Approval-only, idempotent publication of persisted post drafts."""

from dataclasses import dataclass
from datetime import datetime, timedelta
import inspect
import logging
from typing import Optional
from zoneinfo import ZoneInfo

from modules.media_store import open_verified_media
from modules.twitter_client import (
    XPublicationPaused,
    XPublicationRejected,
    XPublicationUnknown,
    is_valid_x_tweet_id,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublishResult:
    status: str
    tweet_id: str = ""


@dataclass(frozen=True)
class _XTransportOutcome:
    kind: str
    tweet_id: str = ""
    error: Optional[Exception] = None


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
            claimed = self.db.claim_post_draft_for_publication(
                draft_id, draft.get("revision")
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
        claimed_draft, claim = claimed
        return self._write_claimed_draft(claimed_draft, claim)

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
            return self.db.get_state("paused") != "false"
        except Exception:
            return True

    def _publication_gate_open(self):
        return not self._publication_is_paused()

    def _write_claimed_draft(self, draft, claim):
        media_id = draft.get("media_id")
        if media_id is None:
            return self._write_to_x(
                draft, claim, None, "image", None, None,
            )

        try:
            media = self.db.get_media_by_id(media_id)
        except Exception as error:
            return self._definite_local_failure(claim, error)
        if (
            not media
            or media.get("id") != media_id
            or media.get("lifecycle_state") != "reserved"
            or media.get("reserved_by_draft_id") != draft["id"]
            or media.get("file_deleted")
        ):
            return self._definite_local_failure(
                claim, ValueError("invalid_media_reservation")
            )

        transport_outcome = None
        local_failure = None
        media_exit_error = None
        try:
            with open_verified_media(media) as media_file:
                try:
                    reservation_valid = (
                        self.db.validate_post_draft_publication_media(
                            claim, media,
                        )
                    )
                except Exception as error:
                    local_failure = error
                else:
                    if not reservation_valid:
                        local_failure = ValueError(
                            "media_reservation_changed_under_lease"
                        )
                if local_failure is None:
                    transport_outcome = self._transport_to_x(
                        draft,
                        media_file,
                        media.get("media_type") or "image",
                        media.get("filename"),
                    )
        except (OSError, PermissionError, RuntimeError, ValueError) as error:
            media_exit_error = error

        if transport_outcome is None:
            return self._definite_local_failure(
                claim,
                media_exit_error
                or local_failure
                or RuntimeError("media_transport_unavailable"),
            )

        write_result = self._persist_transport_outcome(
            claim,
            transport_outcome,
            media,
        )
        if media_exit_error is not None and write_result.status == "published":
            return self._unknown_outcome(claim, media_exit_error)
        return write_result

    def _write_to_x(
        self,
        draft,
        claim,
        media_file,
        media_type,
        media_filename,
        expected_media,
    ):
        outcome = self._transport_to_x(
            draft,
            media_file,
            media_type,
            media_filename,
        )
        return self._persist_transport_outcome(
            claim,
            outcome,
            expected_media,
        )

    def _transport_to_x(
        self,
        draft,
        media_file,
        media_type,
        media_filename,
    ):
        if self._publication_is_paused():
            return _XTransportOutcome("paused")

        try:
            response = self._call_post_tweet(
                draft["text"], media_file, media_type, media_filename
            )
        except XPublicationPaused:
            return _XTransportOutcome("paused")
        except XPublicationRejected as error:
            return _XTransportOutcome("rejected", error=error)
        except (XPublicationUnknown, TimeoutError, ConnectionError) as error:
            return _XTransportOutcome("unknown", error=error)
        except Exception as error:
            # An arbitrary client error may have happened after the write.  It
            # is therefore unsafe to reinterpret it as a definite rejection.
            return _XTransportOutcome("unknown", error=error)

        tweet_id = self._tweet_id(response)
        if not tweet_id:
            return _XTransportOutcome(
                "unknown",
                error=ValueError("publication_response_missing_id"),
            )
        return _XTransportOutcome("confirmed", tweet_id=tweet_id)

    def _persist_transport_outcome(
        self,
        claim,
        outcome,
        expected_media,
    ):
        if outcome.kind == "paused":
            return self._restore_after_pause(claim)
        if outcome.kind == "rejected":
            return self._definite_x_failure(claim, outcome.error)
        if outcome.kind == "unknown":
            return self._unknown_outcome(claim, outcome.error)
        if outcome.kind != "confirmed":
            return self._unknown_outcome(
                claim,
                RuntimeError("invalid_x_transport_outcome"),
            )
        if not is_valid_x_tweet_id(outcome.tweet_id):
            return self._unknown_outcome(
                claim,
                ValueError("invalid_x_publication_id"),
            )
        try:
            finalized = self._finalize_success(
                claim, outcome.tweet_id, expected_media,
            )
        except Exception as error:
            return self._unknown_outcome(claim, error)
        if not finalized:
            return self._unknown_outcome(
                claim, RuntimeError("publication_finalization_conflict")
            )
        return PublishResult("published", outcome.tweet_id)

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
        return tweet_id if is_valid_x_tweet_id(tweet_id) else ""

    def _finalize_success(self, claim, tweet_id, expected_media):
        if not is_valid_x_tweet_id(tweet_id):
            raise ValueError("invalid_x_publication_id")
        return self.db.finalize_post_draft_publication(
            claim, tweet_id, expected_media,
        )

    def _restore_after_pause(self, claim):
        try:
            self.db.restore_post_draft_publication_claim(claim)
        except Exception:
            pass
        return PublishResult("paused")

    def _definite_local_failure(self, claim, error):
        return self._definite_x_failure(claim, error)

    def _definite_x_failure(self, claim, error):
        safe_error = type(error).__name__
        try:
            self.db.fail_post_draft_publication(claim, safe_error)
        except Exception as persistence_error:
            logger.error(
                "publication_failure_persistence_failed draft_id=%s "
                "error_type=%s",
                claim.draft_id,
                type(persistence_error).__name__,
            )
        return PublishResult("publication_failed")

    def _unknown_outcome(self, claim, error):
        try:
            self.db.mark_post_draft_publication_unknown(
                claim, type(error).__name__,
            )
        except Exception as persistence_error:
            logger.error(
                "publication_unknown_persistence_failed draft_id=%s "
                "error_type=%s",
                claim.draft_id,
                type(persistence_error).__name__,
            )
        return PublishResult("publication_unknown")
