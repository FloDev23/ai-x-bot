#!/usr/bin/env python3
"""Fail-closed production preflight with bounded, non-secret output."""

import argparse
import contextlib
import io
import json
import sqlite3
from pathlib import Path

import config


class ProductionPreflightError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise ProductionPreflightError(code) from None


def _database_integrity(db_path):
    path = Path(db_path).resolve()
    if not path.is_file():
        _fail("database_unavailable")
    connection = None
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        row = connection.execute("PRAGMA integrity_check").fetchone()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        _fail("database_unavailable")
    finally:
        if connection is not None:
            try:
                connection.close()
            except (OSError, sqlite3.Error):
                pass
    if row != ("ok",):
        _fail("database_integrity_failed")
    return "ok"


def run_preflight(*, require_dry_run, db_path):
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            config.validate_config()
    except Exception:
        _fail("invalid_config")

    if config.APPROVAL_REQUIRED is not True:
        _fail("approval_required")
    if require_dry_run is not True or config.DRY_RUN is not True:
        _fail("persistent_dry_run_required")

    integrity = _database_integrity(db_path)
    trusted_domains = config.NEWS_TRUSTED_DOMAINS
    if type(trusted_domains) is not set:
        _fail("invalid_config")

    return {
        "approval_required": True,
        "config_valid": True,
        "database_integrity": integrity,
        "dry_run": True,
        "news_key_present": bool(
            isinstance(config.NEWSAPI_KEY, str) and config.NEWSAPI_KEY.strip()
        ),
        "trusted_domain_count": len(trusted_domains),
    }


def _parser():
    parser = argparse.ArgumentParser(add_help=True, exit_on_error=False)
    parser.add_argument("--require-dry-run", action="store_true", required=True)
    parser.add_argument("--db-path", required=True)
    return parser


def _print(value):
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def run_cli(argv=None):
    try:
        arguments = _parser().parse_args(argv)
    except (argparse.ArgumentError, SystemExit):
        _print({"error": "invalid_arguments"})
        return 2
    try:
        result = run_preflight(
            require_dry_run=arguments.require_dry_run,
            db_path=arguments.db_path,
        )
    except ProductionPreflightError as error:
        _print({"error": error.code})
        return 2
    _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
