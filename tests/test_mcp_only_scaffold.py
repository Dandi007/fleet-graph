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

    def test_m1_probe_covers_line_state_port_5615(self) -> None:
        """Pin the M1 probe to the line-state MCP surface :5615.

        Deliverable of wf-525fd4 M1 acceptance: the M1 probe must cover the
        line-state face's port (:5615), register-check both read-only tools,
        and field-compare generation/round/phase against the same-source
        :7494 /v1/lines answer (never a second reader).
        """
        text = SCRIPT.read_text(encoding="utf-8")
        assert "5615" in text, "M1 probe must cover the line-state MCP surface :5615"
        assert "list_line_states" in text and "get_line_state" in text, (
            "M1 probe must register-check both line-state tools"
        )
        for field in ("generation", "round", "phase"):
            assert field in text, f"M1 probe must field-compare {field}"
        assert "/v1/lines" in text, (
            "M1 probe must compare against the same-source :7494 /v1/lines"
        )

    def test_m1_probe_reports_connection_refused_when_5615_unreachable(self) -> None:
        """When :5615 is not live, the M1 阳性 criterion must honestly report
        'connection refused' evidence and count red (不可判定), never a fake
        green nor a fake 'no line-state tool'."""
        proc = _run_script()
        output = proc.stdout + proc.stderr
        lines = output.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("M1[阳性]"):
                assert "不可判定" in line, line
                evidence = lines[i + 1] if i + 1 < len(lines) else ""
                assert evidence.startswith("    证据:"), evidence
                assert "connection refused" in evidence, evidence
                return
        raise AssertionError(f"saw no M1 阳性 line:\n{output}")
