import subprocess
import sys

import pytest


def test_main_module_imports_without_legacy_scheduler_helpers():
    result = subprocess.run(
        [sys.executable, "-c", "import main"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("GROWTH_DIGEST_TIME", "9:00"),
        ("GROWTH_DIGEST_TIME", "24:00"),
        ("GROWTH_ACCOUNT_SUGGESTION_LIMIT", "0"),
        ("GROWTH_ACCOUNT_SUGGESTION_LIMIT", "6"),
        ("GROWTH_POST_SUGGESTION_LIMIT", "11"),
        ("GROWTH_POST_QUERY_BUDGET", "3"),
        ("GROWTH_SUGGESTION_COOLDOWN_DAYS", "31"),
        ("GROWTH_UNFOLLOW_REVIEW_DAYS", "13"),
    ),
)
def test_growth_digest_configuration_fails_closed(name, value):
    script = (
        "import os; "
        f"os.environ[{name!r}] = {value!r}; "
        "import config"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert name in result.stderr


def test_growth_digest_configuration_has_bounded_release_defaults():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import config; print(config.GROWTH_DIGEST_TIME, "
                "config.GROWTH_ACCOUNT_SUGGESTION_LIMIT, "
                "config.GROWTH_POST_SUGGESTION_LIMIT, "
                "config.GROWTH_POST_QUERY_BUDGET, "
                "config.GROWTH_SUGGESTION_COOLDOWN_DAYS, "
                "config.GROWTH_UNFOLLOW_REVIEW_DAYS)"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "09:00 5 10 2 30 14"
