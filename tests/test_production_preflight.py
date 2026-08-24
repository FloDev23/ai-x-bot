import json
import sqlite3
from pathlib import Path

import pytest

from scripts import preflight_production


def _database(path):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE healthcheck (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()


def _valid_config(monkeypatch, *, dry_run=True, approval_required=True):
    monkeypatch.setattr(preflight_production.config, "validate_config", lambda: None)
    monkeypatch.setattr(preflight_production.config, "DRY_RUN", dry_run)
    monkeypatch.setattr(
        preflight_production.config,
        "APPROVAL_REQUIRED",
        approval_required,
    )
    monkeypatch.setattr(
        preflight_production.config,
        "NEWS_TRUSTED_DOMAINS",
        {"example.com"},
    )
    monkeypatch.setattr(preflight_production.config, "NEWSAPI_KEY", "configured")


def test_preflight_reports_only_bounded_operational_metadata(tmp_path, monkeypatch):
    database_path = tmp_path / "bot_data.db"
    _database(database_path)
    _valid_config(monkeypatch)

    result = preflight_production.run_preflight(
        require_dry_run=True,
        db_path=database_path,
    )

    assert result == {
        "approval_required": True,
        "config_valid": True,
        "database_integrity": "ok",
        "dry_run": True,
        "news_key_present": True,
        "trusted_domain_count": 1,
    }
    assert "configured" not in json.dumps(result)


@pytest.mark.parametrize(
    ("dry_run", "approval_required", "expected"),
    [
        (False, True, "persistent_dry_run_required"),
        ("true", True, "persistent_dry_run_required"),
        (True, False, "approval_required"),
        (True, "true", "approval_required"),
    ],
)
def test_preflight_fails_closed_on_noncanonical_safety_flags(
    tmp_path,
    monkeypatch,
    dry_run,
    approval_required,
    expected,
):
    database_path = tmp_path / "bot_data.db"
    _database(database_path)
    _valid_config(
        monkeypatch,
        dry_run=dry_run,
        approval_required=approval_required,
    )

    with pytest.raises(preflight_production.ProductionPreflightError) as error:
        preflight_production.run_preflight(
            require_dry_run=True,
            db_path=database_path,
        )

    assert error.value.code == expected


def test_preflight_sanitizes_config_failure(tmp_path, monkeypatch):
    database_path = tmp_path / "bot_data.db"
    _database(database_path)
    secret = "secret-config-payload"

    def fail_validation():
        raise ValueError(secret)

    monkeypatch.setattr(preflight_production.config, "validate_config", fail_validation)

    with pytest.raises(preflight_production.ProductionPreflightError) as error:
        preflight_production.run_preflight(
            require_dry_run=True,
            db_path=database_path,
        )

    assert error.value.code == "invalid_config"
    assert secret not in str(error.value)


def test_preflight_does_not_create_a_missing_database(tmp_path, monkeypatch):
    database_path = tmp_path / "missing.db"
    _valid_config(monkeypatch)

    with pytest.raises(preflight_production.ProductionPreflightError) as error:
        preflight_production.run_preflight(
            require_dry_run=True,
            db_path=database_path,
        )

    assert error.value.code == "database_unavailable"
    assert not database_path.exists()


def test_preflight_rejects_non_ok_integrity_result(tmp_path, monkeypatch):
    database_path = tmp_path / "bot_data.db"
    database_path.touch()
    _valid_config(monkeypatch)

    class Cursor:
        def fetchone(self):
            return ("corrupt",)

    class Connection:
        def execute(self, _query):
            return Cursor()

        def close(self):
            return None

    monkeypatch.setattr(preflight_production.sqlite3, "connect", lambda *a, **k: Connection())

    with pytest.raises(preflight_production.ProductionPreflightError) as error:
        preflight_production.run_preflight(
            require_dry_run=True,
            db_path=database_path,
        )

    assert error.value.code == "database_integrity_failed"


def test_cli_emits_only_sanitized_json(tmp_path, monkeypatch, capsys):
    database_path = tmp_path / "bot_data.db"
    _database(database_path)
    _valid_config(monkeypatch)
    monkeypatch.setattr(
        preflight_production.config,
        "validate_config",
        lambda: print("internal validation detail"),
    )

    code = preflight_production.run_cli(
        ["--require-dry-run", "--db-path", str(database_path)]
    )

    assert code == 0
    raw_output = capsys.readouterr().out
    assert raw_output.count("\n") == 1
    output = json.loads(raw_output)
    assert output["database_integrity"] == "ok"
    assert "configured" not in json.dumps(output)
    assert "internal validation detail" not in raw_output


def test_deploy_runs_preflight_before_any_service_restart():
    deploy = (Path(__file__).parents[1] / "deploy.sh").read_text(encoding="utf-8")

    preflight = deploy.index("scripts/preflight_production.py")
    first_restart = deploy.index('systemctl restart "$BOT_SERVICE"')
    assert preflight < first_restart
    assert "--require-dry-run" in deploy
    assert "cat .env" not in deploy
    assert "printenv" not in deploy
