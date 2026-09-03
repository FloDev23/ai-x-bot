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


def test_x_api_budget_configuration_has_backward_compatible_defaults():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import config; print(config.X_API_MONTHLY_BUDGET_MICROUSD, "
                "config.X_API_UNIT_COSTS_MICROUSD['post_read'], "
                "config.X_API_UNIT_COSTS_MICROUSD['content_create_with_url'])"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0 5000 200000"


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("X_API_MONTHLY_BUDGET_USD", "-1"),
        ("X_API_MONTHLY_BUDGET_USD", "nan"),
        ("X_API_MONTHLY_BUDGET_USD", "1.0000001"),
        ("X_API_POST_READ_UNIT_COST_USD", "free"),
    ),
)
def test_x_api_budget_configuration_rejects_noncanonical_usd(name, value):
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


def test_x_api_usd_configuration_converts_exactly_to_microusd():
    script = (
        "import os; "
        "os.environ['X_API_MONTHLY_BUDGET_USD'] = '12.345678'; "
        "import config; print(config.X_API_MONTHLY_BUDGET_MICROUSD)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "12345678"
