#!/usr/bin/env python3
"""Publish exactly one immutable Telegram-approved draft."""

import argparse
import hashlib
import hmac
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import (
    APPROVAL_REQUIRED,
    BOT_TIMEZONE,
    DRAFT_SCORE_THRESHOLD,
    DRY_RUN,
    PUBLISH_GRACE_SECONDS,
    validate_config,
)
from modules.database import Database
from modules.publisher import PublishResult, Publisher
from modules.twitter_client import TwitterClient, is_valid_x_tweet_id


CONFIRMATION = "PUBLISH_ONE_APPROVED_FLEXDROPIN_DRAFT"
FINGERPRINT_FIELDS = (
    "id",
    "revision",
    "publication_key",
    "text",
    "category",
    "source_ids",
    "score_data",
    "intended_slot",
    "media_id",
    "approved_at",
    "approved_by",
    "status",
    "published_tweet_id",
)
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)


def _invalid_snapshot():
    raise ValueError("invalid_draft_snapshot") from None


def draft_fingerprint(draft):
    try:
        if type(draft) is not dict:
            _invalid_snapshot()
        if type(draft.get("id")) is not int or draft["id"] <= 0:
            _invalid_snapshot()
        if type(draft.get("revision")) is not int or draft["revision"] <= 0:
            _invalid_snapshot()
        for field in (
            "publication_key",
            "text",
            "category",
            "intended_slot",
            "approved_at",
            "approved_by",
        ):
            value = draft.get(field)
            if type(value) is not str or not value or value != value.strip():
                _invalid_snapshot()
        source_ids = draft.get("source_ids")
        if (
            type(source_ids) is not list
            or not source_ids
            or any(type(source_id) is not int or source_id <= 0 for source_id in source_ids)
            or len(source_ids) != len(set(source_ids))
        ):
            _invalid_snapshot()
        if type(draft.get("score_data")) is not dict:
            _invalid_snapshot()
        media_id = draft.get("media_id")
        if media_id is not None and (type(media_id) is not int or media_id <= 0):
            _invalid_snapshot()
        if draft.get("status") != "approved":
            _invalid_snapshot()
        if draft.get("published_tweet_id") not in (None, ""):
            _invalid_snapshot()

        canonical = {
            field: draft.get(field)
            for field in FINGERPRINT_FIELDS
        }
        payload = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
    except ValueError as error:
        if str(error) == "invalid_draft_snapshot":
            raise
        _invalid_snapshot()
    except (KeyError, TypeError, OverflowError, RecursionError):
        _invalid_snapshot()


def _valid_draft_id(draft_id):
    return type(draft_id) is int and draft_id > 0


def inspect_draft(database, draft_id):
    if not _valid_draft_id(draft_id):
        raise ValueError("invalid_draft_id")
    draft = database.get_post_draft(draft_id)
    if draft is None:
        raise ValueError("draft_not_found")
    fingerprint = draft_fingerprint(draft)
    total = draft["score_data"].get("total")
    if (
        type(total) is not int
        or total < DRAFT_SCORE_THRESHOLD
        or total > 100
    ):
        _invalid_snapshot()
    return {
        "draft_id": draft["id"],
        "revision": draft["revision"],
        "intended_slot": draft["intended_slot"],
        "score_total": total,
        "has_media": draft["media_id"] is not None,
        "fingerprint": fingerprint,
    }


def publish_exact_draft(
    database,
    x_client,
    draft_id,
    expected_fingerprint,
    now,
):
    if (
        not _valid_draft_id(draft_id)
        or type(expected_fingerprint) is not str
        or _FINGERPRINT_RE.fullmatch(expected_fingerprint) is None
    ):
        return PublishResult("snapshot_changed")
    draft = database.get_post_draft(draft_id)
    if draft is None:
        return PublishResult("not_found")
    try:
        actual_fingerprint = draft_fingerprint(draft)
    except ValueError:
        return PublishResult("snapshot_changed")
    if not hmac.compare_digest(actual_fingerprint, expected_fingerprint):
        return PublishResult("snapshot_changed")
    publisher = Publisher(
        database,
        x_client,
        dry_run=False,
        clock=lambda: now,
        grace_seconds=PUBLISH_GRACE_SECONDS,
        timezone_name=BOT_TIMEZONE,
    )
    return publisher.publish(
        draft_id,
        now=now,
        expected_revision=draft["revision"],
    )


def _parser():
    parser = argparse.ArgumentParser(add_help=True, exit_on_error=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect", exit_on_error=False)
    inspect_parser.add_argument("--draft-id", required=True, type=int)
    publish_parser = subparsers.add_parser("publish", exit_on_error=False)
    publish_parser.add_argument("--draft-id", required=True, type=int)
    publish_parser.add_argument("--fingerprint", required=True)
    publish_parser.add_argument("--confirm", required=True)
    return parser


def _print(value):
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def run_cli(argv=None, *, database_factory=Database, x_client_factory=TwitterClient):
    try:
        arguments = _parser().parse_args(argv)
    except argparse.ArgumentError:
        _print({"error": "invalid_arguments"})
        return 4
    except SystemExit as error:
        if error.code == 0:
            return 0
        _print({"error": "invalid_arguments"})
        return 4

    if not _valid_draft_id(arguments.draft_id):
        _print({"error": "invalid_draft_id"})
        return 4
    if arguments.command == "publish":
        if arguments.confirm != CONFIRMATION:
            _print({"error": "invalid_confirmation"})
            return 4
        if (
            type(arguments.fingerprint) is not str
            or _FINGERPRINT_RE.fullmatch(arguments.fingerprint) is None
        ):
            _print({"error": "invalid_fingerprint"})
            return 4

    try:
        validate_config()
        if APPROVAL_REQUIRED is not True:
            raise ValueError("approval_required")
        if arguments.command == "inspect":
            if DRY_RUN is not True:
                raise ValueError("persistent_dry_run_required")
            _print(inspect_draft(database_factory(), arguments.draft_id))
            return 0
        if DRY_RUN is not False:
            raise ValueError("process_dry_run_override_required")

        now = datetime.now(ZoneInfo(BOT_TIMEZONE))
        result = publish_exact_draft(
            database_factory(),
            x_client_factory(),
            arguments.draft_id,
            arguments.fingerprint,
            now,
        )
        output = {"status": result.status}
        if result.tweet_id and is_valid_x_tweet_id(result.tweet_id):
            output["tweet_id"] = result.tweet_id
        _print(output)
        if result.status == "published":
            return 0
        if result.status == "publication_unknown":
            return 3
        return 2
    except ValueError as error:
        code = str(error)
        if code not in {
            "approval_required",
            "persistent_dry_run_required",
            "process_dry_run_override_required",
            "invalid_draft_id",
            "invalid_draft_snapshot",
            "draft_not_found",
        }:
            code = "configuration_error"
        _print({"error": code})
        return 4
    except Exception:
        _print({"error": "operation_failed"})
        return 4


def main():
    return run_cli(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
