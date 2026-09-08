"""The exception boundary in run_line: a crash must not impersonate a clean stop.

`finalise` only runs on a well-formed terminal, so a node raising an
unexpected exception would otherwise leave no terminal.json behind -- and a
12-hour-old terminal from a previous generation would masquerade as current
(the wf-a87b04 g3 incident). `run_line` writes a `fault` terminal on the way
out and re-raises, so the line crashes loudly *and* leaves a discoverable,
self-describing signal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet_graph.graphs.adapters import CoordinatorFault
from fleet_graph.graphs.runner import LineConfig, run_line


class _BoomGoalCall:
    """A Goal ReAct call port that faults -- DD01's injected seam for the same
    unexpected-node-exception path the retired round-prompt agent-run used to
    trigger."""

    def call(self, goal: str, prompt: dict) -> dict:
        raise CoordinatorFault("injected Goal call fault")


class TestFaultBoundary:
    def test_an_unexpected_node_exception_writes_a_fault_terminal_and_reraises(
        self, tmp_path: Path
    ) -> None:
        # The Goal ReAct call raises inside the graph -- an unexpected node
        # exception with no graceful path -- so run_line must write a fault
        # terminal and re-raise rather than impersonate a clean stop.
        config = LineConfig(
            folder_id="wf-fault",
            seat="s",
            run_root=tmp_path / "run",
            checkpoint_path=":memory:",
            goal_call=_BoomGoalCall(),
        )
        with pytest.raises(CoordinatorFault):
            run_line(config)

        terminal = json.loads((config.run_root / "terminal.json").read_text(encoding="utf-8"))
        assert terminal["terminal"] == "fault"
        assert terminal["pump_fault"] is True
        assert terminal["exception_class"]
        assert terminal["message"]
        assert terminal.get("traceback")
        # Self-describing: the fault terminal names the line's log.
        assert terminal["log_path"].endswith("wf-fault.log")

    def test_a_normal_terminal_carries_log_path_but_no_fault_fields(self, tmp_path: Path) -> None:
        """Section 3: finalise and the fault boundary share `log_path`, but a
        clean stop carries none of the fault-only fields."""
        from fleet_graph.state.run_artifacts import RunArtifacts

        log_path = tmp_path / "log" / "wf-fault.log"
        artifacts = RunArtifacts(
            tmp_path / "run", run_id="r", folder_id="wf-fault", log_path=log_path
        )
        artifacts.write_terminal(terminal="done", rounds=1, reason="ok")
        terminal = json.loads(artifacts.terminal_path.read_text(encoding="utf-8"))
        assert terminal["terminal"] == "done"
        assert terminal["log_path"] == str(log_path)
        assert "exception_class" not in terminal

    def test_metrics_are_flushed_to_the_textfile_even_on_a_fault(self, tmp_path: Path) -> None:
        """D3 effect side: run_line must render the protocol counters to the
        line's textfile on the way out -- including the fault path -- so the
        counters survive the process instead of dying with it (the monitoring
        gap the spec exists to close)."""
        from fleet_graph.state.line_metrics import line_metrics_filename

        prom_dir = tmp_path / "prom"
        config = LineConfig(
            folder_id="wf-fault",
            seat="s",
            run_root=tmp_path / "run",
            checkpoint_path=":memory:",
            goal_call=_BoomGoalCall(),
            metrics_dir=prom_dir,
        )
        with pytest.raises(CoordinatorFault):
            run_line(config)

        target = prom_dir / line_metrics_filename("wf-fault")
        assert target.exists(), "run_line must flush line metrics even on a fault"
        assert target.read_text(encoding="utf-8") == ""
