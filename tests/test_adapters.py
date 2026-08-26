"""The real Coordinator/Worker adapters, with the CLI stubbed out."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.executors.agent_run import AgentRunSpec, RunStatus, RunTicket
from fleet_graph.graphs.adapters import (
    DISPATCHER,
    AgentRunCoordinator,
    AgentSessionWorker,
    CoordinatorFault,
    parse_envelope,
)


class FakeLauncher:
    def __init__(self, status: RunStatus) -> None:
        self.status = status
        self.specs: list[AgentRunSpec] = []
        self.run_ids: list[str] = []

    def launch(self, spec: AgentRunSpec, run_id: str) -> RunTicket:
        self.specs.append(spec)
        self.run_ids.append(run_id)
        return RunTicket(run_id, "/tmp/session-root")

    def wait(self, ticket: RunTicket, **_kwargs: Any) -> RunStatus:
        return self.status


def ok_status(structured: dict[str, Any]) -> RunStatus:
    return RunStatus(
        "succeeded", {"state": "succeeded", "exit_code": 0, "structured_result": structured}
    )


@pytest.fixture
def run_root(tmp_path: Path) -> Path:
    return tmp_path / "run"


class TestEnvelopeParsing:
    def test_prefers_structured_result(self) -> None:
        assert parse_envelope({"structured_result": {"verdict": "done"}}) == {"verdict": "done"}

    def test_accepts_the_legacy_result_field(self) -> None:
        assert parse_envelope({"result": {"verdict": "continue"}})["verdict"] == "continue"

    def test_reads_a_json_stdout_envelope(self) -> None:
        stdout = json.dumps({"structured_result": {"verdict": "done"}})
        assert parse_envelope({"stdout": stdout})["verdict"] == "done"

    def test_bare_verdict_object_in_stdout_is_accepted(self) -> None:
        assert parse_envelope({"stdout": '{"verdict": "blocked"}'})["verdict"] == "blocked"

    def test_missing_structure_is_a_fault_not_an_inference(self) -> None:
        """Guessing a verdict out of prose is the INV-3 violation itself."""
        with pytest.raises(CoordinatorFault, match="no structured_result"):
            parse_envelope({"stdout": "The work looks done to me!"})

    def test_non_json_stdout_is_a_fault(self) -> None:
        with pytest.raises(CoordinatorFault):
            parse_envelope({"stdout": "not json at all"})


class TestCoordinatorAdapter:
    def build(self, run_root: Path, status: RunStatus) -> tuple[AgentRunCoordinator, FakeLauncher]:
        launcher = FakeLauncher(status)
        coordinator = AgentRunCoordinator(
            launcher=launcher, folder_id="wf-40fa8d", thread_id="t1", run_root=run_root
        )
        return coordinator, launcher

    def test_returns_the_declared_verdict(self, run_root: Path) -> None:
        coordinator, _ = self.build(run_root, ok_status({"verdict": "done", "reason": "ok"}))
        assert coordinator.turn(1, {"round": 1})["verdict"] == "done"

    def test_writes_the_input_durably_and_passes_it_by_path(self, run_root: Path) -> None:
        """The inbox rides in this file; argv is world-readable via /proc."""
        coordinator, launcher = self.build(run_root, ok_status({"verdict": "done"}))
        coordinator.turn(2, {"round": 2, "inbox_messages": [{"message_id": "m1"}]})

        spec = launcher.specs[0]
        assert spec.input_path is not None
        written = json.loads(Path(spec.input_path).read_text())
        assert written["inbox_messages"][0]["message_id"] == "m1"
        assert "round-2-input.json" in spec.input_path

    def test_prompt_is_not_placed_in_argv(self, run_root: Path) -> None:
        coordinator, launcher = self.build(run_root, ok_status({"verdict": "done"}))
        coordinator.turn(1, {"round": 1})
        argv = launcher.specs[0].argv(bin_path="/bin/agent-run", run_id="r", session_root="/tmp/s")
        assert "--" not in argv
        assert "--prompt-file" in argv

    def test_labels_carry_folder_and_dispatcher(self, run_root: Path) -> None:
        coordinator, launcher = self.build(run_root, ok_status({"verdict": "done"}))
        coordinator.turn(1, {"round": 1})
        labels = launcher.specs[0].labels
        assert labels["work_folder"] == "wf-40fa8d"
        assert labels["dispatcher"] == DISPATCHER

    def test_run_id_is_derived_per_round(self, run_root: Path) -> None:
        """Same round re-derives the same id, so a restart re-adopts."""
        coordinator, launcher = self.build(run_root, ok_status({"verdict": "done"}))
        coordinator.turn(1, {"round": 1})
        coordinator.turn(2, {"round": 2})
        first, second = launcher.run_ids
        assert first != second

        again, launcher2 = self.build(run_root, ok_status({"verdict": "done"}))
        again.turn(1, {"round": 1})
        assert launcher2.run_ids[0] == first

    def test_uses_the_coordinator_role(self, run_root: Path) -> None:
        coordinator, launcher = self.build(run_root, ok_status({"verdict": "done"}))
        coordinator.turn(1, {"round": 1})
        assert launcher.specs[0].role == "goal_coordinator"

    def test_a_failed_run_is_a_fault(self, run_root: Path) -> None:
        failed = RunStatus("failed", {"state": "failed", "exit_code": 3})
        coordinator, _ = self.build(run_root, failed)
        with pytest.raises(CoordinatorFault, match="ended failed"):
            coordinator.turn(1, {"round": 1})

    def test_a_lost_run_is_a_fault(self, run_root: Path) -> None:
        coordinator, _ = self.build(run_root, RunStatus("lost"))
        with pytest.raises(CoordinatorFault, match="no result"):
            coordinator.turn(1, {"round": 1})


class FakeSeat:
    def __init__(self, envelope: dict[str, Any]) -> None:
        self.envelope = envelope
        self.opens = 0
        self.sends: list[str] = []

    def open(self, spec: Any, seat_key: str) -> str:
        self.opens += 1
        return f"handle-{seat_key}"

    def send(self, handle: Any, prompt: str, *, timeout_seconds: int) -> dict[str, Any]:
        self.sends.append(prompt)
        return self.envelope


class TestWorkerAdapter:
    def test_returns_the_turn_text(self) -> None:
        seat = FakeSeat({"ok": True, "text": "did the thing"})
        worker = AgentSessionWorker(seat=seat, seat_spec=object(), seat_key="k")
        assert worker.turn("go", 1) == "did the thing"

    def test_seat_is_opened_once_across_rounds(self) -> None:
        """A seat is re-entered, not reopened -- reopening leaks a worker."""
        seat = FakeSeat({"text": "ok"})
        worker = AgentSessionWorker(seat=seat, seat_spec=object(), seat_key="k")
        worker.turn("a", 1)
        worker.turn("b", 2)
        assert seat.opens == 1
        assert seat.sends == ["a", "b"]

    def test_a_textless_envelope_is_a_fault_not_an_empty_round(self) -> None:
        """Returning '' would feed an empty fact onward and look merely quiet."""
        seat = FakeSeat({"ok": True})
        worker = AgentSessionWorker(seat=seat, seat_spec=object(), seat_key="k")
        with pytest.raises(CoordinatorFault, match="no text"):
            worker.turn("go", 1)
