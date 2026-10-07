"""The deploy workflow asks for the CI workflow's jobs by name, and the names are kept the
same in both files by hand. These tests are what notices when they are not."""

from pathlib import Path
from typing import Any

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
GATE = "Refuse to deploy a commit that CI has not passed"


def workflow(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return loaded


def ci_check_names() -> set[str]:
    """The name each CI job reports its check run under, one per combination of its matrix."""
    names: set[str] = set()
    for key, job in workflow("ci.yml")["jobs"].items():
        name: str = job.get("name", key)
        combinations: list[dict[str, str]] = [{}]
        for variable, values in job.get("strategy", {}).get("matrix", {}).items():
            combinations = [
                {**chosen, variable: str(value)} for chosen in combinations for value in values
            ]
        for chosen in combinations:
            expanded = name
            for variable, value in chosen.items():
                expanded = expanded.replace("${{ matrix." + variable + " }}", value)
            assert "${{" not in expanded, (
                f"the name of {key} uses something this test cannot expand"
            )
            names.add(expanded)
    return names


def deploy_steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = workflow("deploy.yml")["jobs"]["deploy"]["steps"]
    return steps


def test_the_deploy_requires_every_job_of_the_ci_workflow_and_nothing_else() -> None:
    (gate,) = [step for step in deploy_steps() if step.get("name") == GATE]

    required = [
        line.strip() for line in gate["env"]["REQUIRED_CHECKS"].splitlines() if line.strip()
    ]

    assert len(required) == len(set(required))
    assert set(required) == ci_check_names()


def test_ci_is_checked_before_anything_is_built_or_any_credential_is_asked_for() -> None:
    steps = deploy_steps()
    position = [step.get("name") for step in steps].index(GATE)

    # Only the refusal to deploy anything but main comes before it.
    assert [step.get("uses") for step in steps[:position]] == [None] * position
    assert position == 1
    assert "exit 1" in steps[position]["run"]
    assert "success" in steps[position]["run"]


def test_the_deploy_job_may_read_check_runs_and_is_given_no_more_than_it_had() -> None:
    permissions = workflow("deploy.yml")["jobs"]["deploy"]["permissions"]

    assert permissions == {"contents": "read", "checks": "read", "id-token": "write"}
