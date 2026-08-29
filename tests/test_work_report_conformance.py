"""Sabotage self-verification for the work-report conformance guards.

A guard that has never caught anything is a guard you know nothing about
(HANDOFF incident C's lesson). Each guard is fed a deliberately violating tree
and must exit non-zero with the violation named -- and the real tree must pass.
The guards are AST assertions, so a sabotage sample only needs to be valid
Python; it does not need to import the modules it references.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
GUARD = REPO_ROOT / "scripts" / "check_work_report_conformance.py"

#: A worker_turn body that routes through both decoder and projection, but reads
#: the prose attachment -- the exact anti-pattern Guard W2 exists to catch. The
#: prefix is a full, otherwise-valid routing; only the prose read is added.
_GOOD_WORKER_TURN = (
    "def worker_turn(state):\n"
    "    report = decode_report(state['output'])\n"
    "    control = project_control(report)\n"
    "    if control['outcome'] == 'blocked':\n"
    "        return {'terminal': 'blocked'}\n"
    "    return {'last_turn_report': control}\n"
)


def run_guard(src_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), "--src-root", str(src_root)],
        capture_output=True,
        text=True,
    )


def sample_tree(tmp_path: Path, source: str) -> Path:
    src_root = tmp_path / "src"
    target = src_root / "fleet_graph" / "graphs" / "goal_line.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source)
    return src_root


class TestRealTree:
    def test_the_shipped_source_is_clean(self) -> None:
        proc = run_guard(REPO_ROOT / "src")
        assert proc.returncode == 0, proc.stderr


class TestGuardW1RoutePin:
    """The ordinary orchestration path must route through decoder/projection."""

    def test_missing_project_control_is_caught(self, tmp_path: Path) -> None:
        src = sample_tree(
            tmp_path,
            "def worker_turn(state):\n"
            "    report = decode_report(state['output'])\n"
            "    return {'last_turn_report': report}\n",
        )
        proc = run_guard(src)
        assert proc.returncode == 1
        assert "project_control" in proc.stderr

    def test_missing_decode_report_is_caught(self, tmp_path: Path) -> None:
        src = sample_tree(
            tmp_path,
            "def worker_turn(state):\n"
            "    report = state['output']\n"
            "    control = project_control(report)\n"
            "    return {'last_turn_report': control}\n",
        )
        proc = run_guard(src)
        assert proc.returncode == 1
        assert "decode_report" in proc.stderr

    def test_a_worker_turn_node_must_exist(self, tmp_path: Path) -> None:
        src = sample_tree(tmp_path, "pass\n")
        proc = run_guard(src)
        assert proc.returncode == 1
        assert "worker_turn" in proc.stderr

    def test_an_import_is_not_a_call(self, tmp_path: Path) -> None:
        """Merely importing the decoder is not routing through it."""
        src = sample_tree(
            tmp_path,
            "from fleet_graph.work_report import decode_report, project_control\n"
            "def worker_turn(state):\n"
            "    return {'last_turn_report': state['output']}\n",
        )
        proc = run_guard(src)
        assert proc.returncode == 1


class TestGuardW2NoProseControl:
    """The orchestration module must never read the prose attachment."""

    def test_attribute_read_is_caught(self, tmp_path: Path) -> None:
        src = sample_tree(
            tmp_path,
            _GOOD_WORKER_TURN.replace(
                "return {'last_turn_report': control}\n",
                "    verdict = report.prose_attachment['content']\n"
                "    return {'last_turn_report': control}\n",
            ),
        )
        proc = run_guard(src)
        assert proc.returncode == 1
        assert "prose_attachment" in proc.stderr

    def test_subscript_key_read_is_caught(self, tmp_path: Path) -> None:
        src = sample_tree(
            tmp_path,
            _GOOD_WORKER_TURN.replace(
                "return {'last_turn_report': control}\n",
                "    verdict = project_control(report).get('prose_attachment', 'completed')\n"
                "    return {'last_turn_report': control}\n",
            ),
        )
        proc = run_guard(src)
        assert proc.returncode == 1
        assert "prose_attachment" in proc.stderr

    def test_a_clean_routing_survives(self, tmp_path: Path) -> None:
        src = sample_tree(tmp_path, _GOOD_WORKER_TURN)
        proc = run_guard(src)
        assert proc.returncode == 0, proc.stderr
