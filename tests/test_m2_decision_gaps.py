"""M2 decision-surface gaps: dd-gate coverage and the delivery-wakes-the-line rule.

Two known gaps close over ``src/fleet_graph/decision_mcp.py``:

- **gap (a)**: the surface only recognised a parked *line*. dd developments
  parked at ``awaiting_gate`` (about 21% of the decision volume) fell outside
  it. The surface now carries a second, self-explanatory entry
  (``decision_deliver_gate``) whose question/card correspondence is resolved
  server-side from the dd control plane -- the caller never guesses
  question/card. An unknown development, or one not ``awaiting_gate``, is an
  explicit, machine-readable refusal, never an HTTP-200 silent swallow.

- **gap (b)**: delivering the verdict woke the single (the dd 单 advanced to
  terminal) but left the parked, decision-waiting *line* parked. After a
  successful delivery the line is now woken through its registered control
  entry (the same stall-state clear the line path uses), so it ignites on the
  next scheduler tick.

The existing four negative refusals stay green (they live in
``tests/test_decision_mcp.py``); these are the independent M2 cases on top of
them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.decision_bridge.owners import OWNER_KIND_DD
from fleet_graph.decision_mcp import (
    CODE_GATE_NOT_AWAITING,
    CODE_GATE_NOT_FOUND,
    CODE_GATE_RESUME_REFUSED,
    CODE_LINE_NOT_PARKED,
    CODE_NO_WAITING_PARTY,
    CODE_QUESTION_CARD_UNRESOLVED,
    DECISION_APPROVE,
    DECISION_REJECT,
    OUTCOME_DELIVERED,
    OUTCOME_REFUSED,
    DecisionPayloadError,
    build_decision_mcp_server,
    deliver_decision,
    deliver_decision_gate,
)
from fleet_graph.scheduler.daemon import LineSpec, Scheduler, SchedulerConfig
from fleet_graph.scheduler.ignition import Refusal
from fleet_graph.scheduler.launcher import LaunchResult

ROSTER = [{"folder_id": "wf-1", "seat": "s", "generation": 1}]


# --- line-path fixtures ------------------------------------------------------


def _stall(
    run_root: Path,
    folder_id: str,
    *,
    parked: bool = True,
    question: str = "q-1",
    card: str = "card-1",
) -> Path:
    path = run_root / ".scheduler" / f"{folder_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {
        "generation": 2,
        "board_question_note_id": question,
        "board_card_entity_id": card,
    }
    if parked:
        state.update(
            {
                "parked_run_id": "run-1",
                "parked_at": 1_700_000_000.0,
                "parked_goal_revision": "sha256:rev-1",
                "parked_inbox_available": True,
            }
        )
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def _deliver_line(run_root: Path, line: str = "wf-1", decision: str = DECISION_APPROVE) -> Any:
    return deliver_decision(
        line=line,
        decision=decision,
        reason="live drill",
        run_root=run_root,
        lines=ROSTER,
    )


# --- scheduler fixtures (the "ignites within N ticks" criterion) -------------


class FakeProber:
    def check(self, seat: str) -> bool:
        return True


class FakeUnits:
    def is_active(self, unit_name: str) -> bool:
        return False


class FakeLauncher:
    def __init__(self) -> None:
        self.launched: list[Any] = []

    def launch(self, spec: Any) -> LaunchResult:
        self.launched.append(spec)
        return LaunchResult(spec.unit_name, True, "")


class FakeWake:
    def __init__(self, revision: str = "sha256:rev-1") -> None:
        self.revision = revision

    def inbox_message_after(self, alias: str, after_epoch: float) -> bool:
        return False

    def goal_revision(self, folder_id: str) -> str:
        return self.revision


class FakeTicket:
    question_note_id = "note-123"


class FakePublishResult:
    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        self.message_id = entity_id
        self.channel_seq = 1
        self.deduplicated = False


class FakeBoard:
    def __init__(self) -> None:
        self.cards = 0

    def publish_card(self, payload: dict[str, Any], idempotency_key: str) -> FakePublishResult:
        self.cards += 1
        return FakePublishResult(f"msg-card-{self.cards}")

    def ask(self, *, card_entity_id: str, question: str, idempotency_key: str) -> FakeTicket:
        return FakeTicket()


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _scheduler(tmp_path: Path) -> tuple[Scheduler, Clock, FakeLauncher]:
    """A scheduler whose single line has blocked on a decision and is parked,
    with its board card + question materialised so the delivery surface can
    resolve it server-side."""
    clock = Clock(1_700_000_000.0)
    launcher = FakeLauncher()
    scheduler = Scheduler(
        SchedulerConfig(
            lines=[LineSpec(folder_id="wf-1", seat="opencode-dsv4pro", enabled=True)],
            run_root=tmp_path / "runs",
            maintenance_stop_path=tmp_path / "maintenance-stop",
        ),
        prober=FakeProber(),
        launcher=launcher,
        units=FakeUnits(),
        clock=clock,
        sleep=lambda _s: None,
        wake=FakeWake(),
        board=FakeBoard(),
    )
    # Prime: one launch, then the line blocks and parks on the next tick.
    assert scheduler.tick()[0].decision.ignite
    launcher.launched.clear()
    record = {
        "terminal": "blocked",
        "rounds": 0,
        "run_id": "run-1",
        "at": "2026-08-27T10:00:00Z",
        "reason": "waiting on a human ruling",
        "waiting_on": "decision",
        "goal_revision": "sha256:rev-1",
    }
    terminal = tmp_path / "runs" / "wf-1" / "terminal.json"
    terminal.parent.mkdir(parents=True, exist_ok=True)
    terminal.write_text(json.dumps(record), encoding="utf-8")
    clock.now = 1_700_003_600.0  # an hour later: past any backoff
    assert scheduler.tick()[0].decision.refusal is Refusal.PARKED_AWAITING_DECISION
    return scheduler, clock, launcher


# --- dd-gate fixtures --------------------------------------------------------


class RecordingLauncher:
    dry_run = False

    def __init__(self) -> None:
        self.specs: list[Any] = []

    def launch(self, spec: Any) -> Any:
        self.specs.append(spec)
        return LaunchResult(spec.unit_name, True, "recorded")


def _gate_plane(tmp_path: Path) -> tuple[Any, RecordingLauncher]:
    from fleet_graph.dd.control_plane import DdControlPlane

    launcher = RecordingLauncher()
    binding = tmp_path / "plugin-binding.json"
    binding.write_text('{"plugin_producer": {}}', encoding="utf-8")
    plane = DdControlPlane(
        root=tmp_path / "dd",
        plugin_binding=binding,
        worktree_roots=(str(tmp_path),),
        working_directory=str(tmp_path),
        executable="/usr/local/bin/fleet-graph",
        launcher=launcher,
        unit_probe=lambda unit: False,
        board_factory=lambda: None,
        clock=lambda: 1_700_000_000.0,
    )
    return plane, launcher


def _suspended_gate(plane: Any, dev: str, tmp_path: Path, *, dispatched_by: str) -> None:
    from fleet_graph.dd.control_plane import (
        CHECKPOINT_FILE,
        LAUNCHES_FILE,
        RECORD_FILE,
        RESULT_FILE,
    )

    dev_root = plane.root / dev
    dev_root.mkdir(parents=True, exist_ok=True)
    (dev_root / RECORD_FILE).write_text(
        json.dumps(
            {
                "development_id": dev,
                "generation": 1,
                "repo_path": str(tmp_path),
                "remote_url": "file:///dev/null",
                "remote_ref": f"refs/heads/dd/{dev}",
                "root_handoff_digest": "sha256:root",
                "target_base_commit": "0" * 40,
                "plugin_binding_path": str(tmp_path / "plugin-binding.json"),
                "card_entity_id": "card-1",
                "dispatched_by": dispatched_by,
                "spec_digest": "sha256:spec",
                "bootstrap_commit": "b" * 40,
                "acceptance_commands": [],
            }
        ),
        encoding="utf-8",
    )
    (dev_root / RESULT_FILE).write_text(
        json.dumps(
            {
                "development_id": dev,
                "terminal": None,
                "awaiting": {"question_note_id": "q-1", "card_entity_id": "card-1"},
            }
        ),
        encoding="utf-8",
    )
    (dev_root / CHECKPOINT_FILE).touch()
    (dev_root / LAUNCHES_FILE).write_text(
        json.dumps(
            {
                "seq": 1,
                "unit": f"fleet-graph-dd-{dev}.service",
                "mode": "fresh",
                "generation": 1,
                "at": "2026-08-28T00:00:00Z",
                "started": True,
                "detail": "recorded",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _parked_dispatch_line(run_root: Path, folder_id: str = "wf-1") -> Path:
    return _stall(run_root, folder_id, parked=True, question="q-1", card="card-1")


class _RefusingResumePlane:
    """A control plane that resolves an awaiting gate but whose registered
    control entry refuses the valueless resume -- drives ``CODE_GATE_RESUME_REFUSED``
    (the gate answered, but its resume was refused)."""

    def get(self, development_id: str) -> dict[str, Any]:
        return {
            "state": "awaiting_gate",
            "awaiting": {"question_note_id": "q-1", "card_entity_id": "card-1"},
            "generation": 1,
            "dispatched_by": "wf-1",
        }

    def gate(
        self, development_id: str, resume: bool = False, action_key: str | None = None
    ) -> dict[str, Any]:
        from fleet_graph.dd.control_plane import ControlPlaneError

        raise ControlPlaneError("CHECKPOINT_MISSING", f"{development_id} has no durable checkpoint")


# --- gap (a) + (b): the line path -------------------------------------------


class TestPositiveLineDeliveryIgnites:
    def test_delivery_is_consumed_and_the_line_ignites_within_ticks(self, tmp_path: Path) -> None:
        scheduler, clock, launcher = _scheduler(tmp_path)
        result = _deliver_line(tmp_path / "runs")
        assert result.status == OUTCOME_DELIVERED
        assert result.as_dict()["outcome"] == "consumed"

        # Parking is lifted through the registered control entry.
        stall = tmp_path / "runs" / ".scheduler" / "wf-1.json"
        after = json.loads(stall.read_text(encoding="utf-8"))
        assert after["parked_run_id"] is None
        assert after["parked_at"] is None

        # The line ignites within a small number of scheduler ticks.
        ignited = False
        for _ in range(3):
            clock.now += 60.0
            if scheduler.tick()[0].decision.ignite:
                ignited = True
                break
        assert ignited
        assert len(launcher.launched) == 1


class TestNegativeLineRefusals:
    def test_line_not_parked_is_an_explicit_refusal(self, tmp_path: Path) -> None:
        _stall(tmp_path / "runs", "wf-1", parked=False)
        result = _deliver_line(tmp_path / "runs")
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_LINE_NOT_PARKED
        assert result.retryable is True

    def test_unresolvable_question_card_is_an_explicit_refusal(self, tmp_path: Path) -> None:
        _stall(tmp_path / "runs", "wf-1", parked=True, question="", card="")
        result = _deliver_line(tmp_path / "runs")
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_QUESTION_CARD_UNRESOLVED

    def test_an_unregistered_line_is_no_such_waiting_party(self, tmp_path: Path) -> None:
        _stall(tmp_path / "runs", "wf-nope")
        result = _deliver_line(tmp_path / "runs", line="wf-nope")
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_NO_WAITING_PARTY

    def test_an_illegal_payload_is_a_call_point_error(self, tmp_path: Path) -> None:
        with pytest.raises(DecisionPayloadError, match="APPROVE or REJECT"):
            _deliver_line(tmp_path / "runs", decision="MAYBE")


# --- gap (a) + (b): the dd-gate path -----------------------------------------


class TestGateDelivery:
    def test_awaiting_gate_delivery_resumes_and_wakes_the_dispatched_line(
        self, tmp_path: Path
    ) -> None:
        plane, launcher = _gate_plane(tmp_path)
        _suspended_gate(plane, "dev-abc", tmp_path, dispatched_by="wf-1")
        _parked_dispatch_line(tmp_path / "runs")

        result = deliver_decision_gate(
            development_id="dev-abc",
            decision=DECISION_APPROVE,
            reason="merge it",
            dd_root=tmp_path / "dd",
            lines=ROSTER,
            run_root=tmp_path / "runs",
            plane=plane,
        )

        assert result.status == OUTCOME_DELIVERED
        assert result.as_dict()["outcome"] == "consumed"
        assert result.as_dict()["development_id"] == "dev-abc"
        assert result.target["kind"] == OWNER_KIND_DD
        assert result.target["question_note_id"] == "q-1"
        assert result.target["card_entity_id"] == "card-1"
        assert len(launcher.specs) == 1  # the gate resumed exactly once

        # gap b: the line that dispatched the development is no longer parked.
        stall = tmp_path / "runs" / ".scheduler" / "wf-1.json"
        after = json.loads(stall.read_text(encoding="utf-8"))
        assert after["parked_run_id"] is None
        assert after["parked_at"] is None
        assert result.target["line_woken"] is True

    def test_reject_is_also_a_valid_delivered_gate_verdict(self, tmp_path: Path) -> None:
        plane, launcher = _gate_plane(tmp_path)
        _suspended_gate(plane, "dev-abc", tmp_path, dispatched_by="wf-1")
        _parked_dispatch_line(tmp_path / "runs")
        result = deliver_decision_gate(
            development_id="dev-abc",
            decision=DECISION_REJECT,
            reason="do not merge",
            dd_root=tmp_path / "dd",
            lines=ROSTER,
            run_root=tmp_path / "runs",
            plane=plane,
        )
        assert result.status == OUTCOME_DELIVERED
        assert result.decision == DECISION_REJECT
        assert result.action_key == "mcp:gate:dev-abc:g1:REJECT"
        assert len(launcher.specs) == 1


class TestGateNegativeRefusals:
    def test_an_unknown_development_is_an_explicit_refusal(self, tmp_path: Path) -> None:
        plane, _ = _gate_plane(tmp_path)
        result = deliver_decision_gate(
            development_id="dev-nope",
            decision=DECISION_APPROVE,
            reason="x",
            dd_root=tmp_path / "dd",
            lines=ROSTER,
            run_root=tmp_path / "runs",
            plane=plane,
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_GATE_NOT_FOUND
        assert "not an admitted development" in result.message

    def test_a_development_not_awaiting_gate_is_a_refusal(self, tmp_path: Path) -> None:
        from fleet_graph.dd.control_plane import RESULT_FILE

        plane, launcher = _gate_plane(tmp_path)
        _suspended_gate(plane, "dev-done", tmp_path, dispatched_by="wf-1")
        # Terminate it: no longer awaiting_gate, so a delivered verdict must refuse.
        result_path = plane.root / "dev-done" / RESULT_FILE
        result_path.write_text(
            json.dumps({"development_id": "dev-done", "terminal": "complete", "awaiting": None}),
            encoding="utf-8",
        )

        result = deliver_decision_gate(
            development_id="dev-done",
            decision=DECISION_APPROVE,
            reason="x",
            dd_root=tmp_path / "dd",
            lines=ROSTER,
            run_root=tmp_path / "runs",
            plane=plane,
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_GATE_NOT_AWAITING
        assert launcher.specs == []  # nothing resumed

    def test_an_illegal_gate_payload_is_a_call_point_error(self, tmp_path: Path) -> None:
        plane, _ = _gate_plane(tmp_path)
        with pytest.raises(DecisionPayloadError, match="development_id is required"):
            deliver_decision_gate(
                development_id="  ",
                decision=DECISION_APPROVE,
                reason="x",
                dd_root=tmp_path / "dd",
                lines=ROSTER,
                run_root=tmp_path / "runs",
                plane=plane,
            )

    def test_a_gate_whose_resume_is_refused_is_an_explicit_refusal(self, tmp_path: Path) -> None:
        result = deliver_decision_gate(
            development_id="dev-abc",
            decision=DECISION_APPROVE,
            reason="x",
            dd_root=tmp_path / "dd",
            lines=ROSTER,
            run_root=tmp_path / "runs",
            plane=_RefusingResumePlane(),
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_GATE_RESUME_REFUSED
        assert "no durable checkpoint" in result.message


class TestGateWakeTargetOnly:
    def test_a_gate_with_no_parked_dispatcher_still_delivers(self, tmp_path: Path) -> None:
        """The wake is best-effort: a gate whose dispatcher is not parked (or
        unknown) still delivers and reports the truth instead of fabricating a
        wake."""
        plane, launcher = _gate_plane(tmp_path)
        _suspended_gate(plane, "dev-abc", tmp_path, dispatched_by="wf-1")
        # No parked line at all.
        result = deliver_decision_gate(
            development_id="dev-abc",
            decision=DECISION_APPROVE,
            reason="x",
            dd_root=tmp_path / "dd",
            lines=ROSTER,
            run_root=tmp_path / "runs",
            plane=plane,
        )
        assert result.status == OUTCOME_DELIVERED
        assert result.target["line_woken"] is False
        assert len(launcher.specs) == 1


# --- the MCP surface exposes two narrow, self-explanatory entries ------------


class TestMcpGateSurface:
    def test_the_gate_tool_is_registered_separately_from_the_line_tool(self) -> None:
        import asyncio

        server = build_decision_mcp_server(Path("/tmp"), ROSTER, dd_root=Path("/tmp"))
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        assert "decision_deliver" in names
        assert "decision_deliver_gate" in names

    def test_the_gate_tool_lists_its_own_required_arguments(self) -> None:
        import asyncio

        server = build_decision_mcp_server(Path("/tmp"), ROSTER, dd_root=Path("/tmp"))
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        params = set(tools["decision_deliver_gate"].parameters["properties"])
        assert {"development_id", "decision", "reason"} <= params
        required = set(tools["decision_deliver_gate"].parameters.get("required") or params)
        assert {"development_id", "decision", "reason"} <= required
