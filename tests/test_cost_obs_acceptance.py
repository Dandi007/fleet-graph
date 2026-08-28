"""The executable acceptance fixture runs clean and returns the promised facts.

The fixture is the executable proof the spec's acceptance criteria describe:
one launch plus DD settlement, all five recording-rule queries non-empty,
unknown/missing separately visible, and exact-once preserved after a replay.
This test runs it as a *subprocess* -- the same way an operator or a grading
seal would -- so a broken import, a swallowed exception, or a non-zero exit
cannot hide behind an in-process assertion.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from fleet_graph.cost_obs.acceptance import (
    EXPECTED_MANAGEMENT,
    EXPECTED_TOTAL,
    run_acceptance_scenario,
)

REPO_ROOT = Path(__file__).parent.parent
FIXTURE = REPO_ROOT / "scripts" / "cost_obs_acceptance.py"


def test_fixture_exits_zero_and_reports_pass() -> None:
    proc = subprocess.run(
        [sys.executable, str(FIXTURE)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "cost-observability acceptance: PASS" in proc.stdout


def test_fixture_scenario_satisfies_the_acceptance_criteria(tmp_path: Path) -> None:
    results = run_acceptance_scenario(tmp_path)
    assert results["rules_non_empty"] is True
    assert results["unknown_tokens"] == [7.0]
    assert results["missing_visible"] == [0.0]
    assert results["present_visible"] == [1.0]
    assert results["reconciliation"] == [1.0]
    assert results["exact_once"] is True
    assert results["replay_noop"] == (True, True)
    assert results["rerun_reconciliation"] == [1.0]
    assert results["management_ratio"][0] == pytest.approx(EXPECTED_MANAGEMENT / EXPECTED_TOTAL)
