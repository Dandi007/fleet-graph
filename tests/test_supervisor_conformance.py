"""Sabotage self-verification for the supervisor conformance guards.

A guard that has never caught anything is a guard you know nothing about
(HANDOFF incident C's lesson, inherited with the AST technique itself). So
each guard is fed a deliberately violating tree and must exit non-zero with
the violation named -- and the real tree must pass.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
GUARD = REPO_ROOT / "scripts" / "check_supervisor_conformance.py"


def run_guard(src_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), "--src-root", str(src_root)],
        capture_output=True,
        text=True,
    )


def sample_tree(tmp_path: Path, relative: str, source: str) -> Path:
    src_root = tmp_path / "src"
    target = src_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source)
    return src_root


class TestRealTree:
    def test_the_shipped_source_is_clean(self) -> None:
        proc = run_guard(REPO_ROOT / "src")
        assert proc.returncode == 0, proc.stderr


class TestGuardAScheduler:
    """The supervisor graph must not be able to schedule."""

    def test_import_of_ignition_is_caught(self, tmp_path: Path) -> None:
        src = sample_tree(
            tmp_path,
            "fleet_graph/graphs/supervisor.py",
            "import fleet_graph.scheduler.ignition\n",
        )
        proc = run_guard(src)
        assert proc.returncode == 1
        assert "ignition" in proc.stderr

    def test_from_import_of_launcher_is_caught(self, tmp_path: Path) -> None:
        src = sample_tree(
            tmp_path,
            "fleet_graph/supervise/audit.py",
            "from fleet_graph.scheduler.launcher import TransientLauncher\n",
        )
        proc = run_guard(src)
        assert proc.returncode == 1
        assert "launcher" in proc.stderr

    def test_package_level_smuggling_is_caught(self, tmp_path: Path) -> None:
        src = sample_tree(
            tmp_path,
            "fleet_graph/supervise/events.py",
            "from fleet_graph.scheduler import launcher\n",
        )
        proc = run_guard(src)
        assert proc.returncode == 1

    def test_the_observer_may_hold_the_launcher(self, tmp_path: Path) -> None:
        """The sanctioned direction: scheduler-side code launches supervisors."""
        src = sample_tree(
            tmp_path,
            "fleet_graph/scheduler/supervisor_events.py",
            "from fleet_graph.scheduler.launcher import TransientLauncher\n",
        )
        proc = run_guard(src)
        assert proc.returncode == 0, proc.stderr


class TestGuardBDecision:
    """No call in the repo may carry work.decision.v1 as an argument."""

    def test_literal_decision_publish_is_caught(self, tmp_path: Path) -> None:
        src = sample_tree(
            tmp_path,
            "fleet_graph/sneaky.py",
            'client.publish("board:work-notes", "work.decision.v1", {}, "key")\n',
        )
        proc = run_guard(src)
        assert proc.returncode == 1
        assert "work.decision.v1" in proc.stderr

    def test_constant_name_publish_is_caught(self, tmp_path: Path) -> None:
        src = sample_tree(
            tmp_path,
            "fleet_graph/sneaky.py",
            "from fleet_graph.bus.board import DECISION_KIND\n"
            'client.publish("c", DECISION_KIND, {}, "key")\n',
        )
        proc = run_guard(src)
        assert proc.returncode == 1

    def test_attribute_spelling_is_caught(self, tmp_path: Path) -> None:
        src = sample_tree(
            tmp_path,
            "fleet_graph/sneaky.py",
            "import fleet_graph.bus.board as board\n"
            'client.publish("c", board.DECISION_KIND, {}, "key")\n',
        )
        proc = run_guard(src)
        assert proc.returncode == 1

    def test_keyword_argument_spelling_is_caught(self, tmp_path: Path) -> None:
        src = sample_tree(
            tmp_path,
            "fleet_graph/sneaky.py",
            'client.publish("c", kind="work.decision.v1", payload={})\n',
        )
        proc = run_guard(src)
        assert proc.returncode == 1

    def test_read_paths_survive(self, tmp_path: Path) -> None:
        """The constant's definition and comparisons are not publish paths."""
        src = sample_tree(
            tmp_path,
            "fleet_graph/reader.py",
            'DECISION_KIND = "work.decision.v1"\n'
            "def is_decision(m):\n"
            '    return m.get("kind") == DECISION_KIND\n',
        )
        proc = run_guard(src)
        assert proc.returncode == 0, proc.stderr
