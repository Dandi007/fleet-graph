"""Pin the verify-mcp-only scaffold discipline as a regression test.

The scaffold (scripts/verify-mcp-only.sh) is the wf-525fd4 goal line's single
acceptance entry: it must exist, be executable, honestly probe every M0-M4
criterion (positive + negative) against the live surfaces, and currently
return non-zero because none of M0-M4 is delivered yet.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "verify-mcp-only.sh"

MARKERS = ["M0", "M1", "M2", "M3", "M4"]


def _run_script() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT.relative_to(REPO_ROOT))],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


class TestMcpOnlyScaffold:
    def test_script_exists_and_is_executable(self) -> None:
        assert SCRIPT.is_file(), f"scaffold missing: {SCRIPT}"
        assert os.access(SCRIPT, os.X_OK), f"scaffold not executable: {SCRIPT}"

    def test_current_state_is_honest_red(self) -> None:
        proc = _run_script()
        assert proc.returncode != 0, "M0-M4 not delivered, the scaffold must be red"
        output = proc.stdout + proc.stderr
        assert output, "scaffold produced no output at all"
        for marker in MARKERS:
            assert marker in output, f"output lacks marker {marker}:\n{output}"
        assert "阳性" in output, f"output lacks 阳性 (positive criteria):\n{output}"
        assert "阴性" in output, f"output lacks 阴性 (negative criteria):\n{output}"
        assert "No such file or directory" not in output, (
            "the scaffold must produce real per-criterion output, not substitute "
            "'No such file or directory' for a missing script"
        )

    def test_m4_section_calls_the_availability_oracle(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        assert "judge_mcp_availability" in text, (
            "M4 must drive the fleet_graph.mcp_availability oracle, not a keyword "
            "scan of src/config (goal.md M4 wants a real determination)"
        )
        assert "FastMcpSurface" in text, (
            "M4 must point the oracle at a real surface via FastMcpSurface, not "
            "substitute a text grep for the availability determination"
        )
        assert "grep_paths" not in text, (
            "the old M4 keyword-grep detection must be replaced by the oracle"
        )
