"""One PostgreSQL major version everywhere the stack is run as production is run.

The RDS instance declares the major that production runs (``engine_version`` in the data
module). Compose and the sandbox scenario must run the same one, or a change is proven on a
server production does not have. The unit-test matrix in ``ci.yml`` is different on purpose:
it follows the newer majors so the next upgrade is not a surprise, and it must still include
production's. ADR 0021 records the rule and the upgrade procedure.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RDS = ROOT / "infra" / "modules" / "data" / "main.tf"
COMPOSE = ROOT / "compose.yaml"
SANDBOX = ROOT / ".github" / "workflows" / "e2e-sandbox.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def production_major() -> str:
    found = re.search(
        r'engine\s*=\s*"postgres"\s*\n\s*engine_version\s*=\s*"(\d+)"', RDS.read_text()
    )
    assert found, "the data module declares the RDS engine and its major version"
    return found.group(1)


def test_the_parameter_group_family_is_production_major() -> None:
    assert f'family      = "postgres{production_major()}"' in RDS.read_text()


def test_compose_runs_production_major() -> None:
    assert re.search(rf"image: postgres:{production_major()}\b", COMPOSE.read_text())


def test_the_sandbox_scenario_runs_production_major() -> None:
    assert re.search(rf"image: postgres:{production_major()}\b", SANDBOX.read_text())


def test_the_unit_test_matrix_includes_production_major_and_newer_lines() -> None:
    found = re.search(r"postgres:\s*\[([^\]]+)\]", CI.read_text())
    assert found, "ci.yml has a postgres matrix"
    majors = [m.strip().strip('"') for m in found.group(1).split(",")]
    assert production_major() in majors
    assert any(int(m) > int(production_major()) for m in majors), "the matrix looks ahead"


def test_pg_stat_statements_is_loaded_the_same_way_everywhere() -> None:
    rds = RDS.read_text()
    assert 'name         = "shared_preload_libraries"' in rds
    assert 'value        = "pg_stat_statements"' in rds
    assert "shared_preload_libraries=pg_stat_statements" in COMPOSE.read_text()
    init = (ROOT / "deploy" / "postgres-init.sh").read_text()
    assert "CREATE EXTENSION IF NOT EXISTS pg_stat_statements" in init
    assert "GRANT pg_read_all_stats TO corridor_app" in init
