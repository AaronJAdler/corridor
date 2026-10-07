"""The end-to-end scenario, as a test. The scenario itself is ``scripts/e2e.py``."""

import subprocess
import sys

import pytest

from tests.support.postgres import REPO_ROOT

# Far longer than a run takes. A stack that hangs fails here and not in whatever runs this.
TIMEOUT_SECONDS = 300


def run_script(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - this project's own script, from a fixed argument list
        [sys.executable, str(REPO_ROOT / "scripts" / "e2e.py"), *arguments],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


@pytest.mark.e2e
def test_the_scenario_passes_against_real_processes() -> None:
    ran = run_script()

    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "End-to-end run passed." in ran.stdout


@pytest.mark.e2e
def test_the_demo_runs_against_real_processes_and_ends_with_a_sound_ledger() -> None:
    ran = run_script("demo")

    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "Ledger verification passed: no findings." in ran.stdout
