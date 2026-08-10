import subprocess
import sys


def test_main_module_imports_without_legacy_scheduler_helpers():
    result = subprocess.run(
        [sys.executable, "-c", "import main"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
