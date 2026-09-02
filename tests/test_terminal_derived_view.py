"""E3: the durable goal-line checkpoint is authoritative; terminal.json is a
derived view and fault fallback.

The scheduler's ordinary terminal / account / parking decisions must come from
the line's checkpoint, read through ``get_state``. ``terminal.json`` survives
only for external readers (fleet-sentinel, the pump's terminal-facing contract)
and for the explicit fault fallback when the checkpoint cannot be read.

This file pins five things:

1. **Structural authority** -- the ordinary decision path reads the checkpoint,
   never ``terminal.json``, when the checkpoint answers (a monkeypatched
   fallback that would raise is never called).
2. **Checkpoint terminal state** -- a real line graph run into a real sqlite
   checkpoint supplies the scheduler's decision.
3. **Stale / missing terminal artifacts** -- an absent or conflicting
   terminal.json cannot change a decision the checkpoint has already settled.
4. **Checkpoint-read fault fallback** -- an unreadable checkpoint falls back to
   the derived terminal.json, records an observable reason, and is never
   silently treated as a completed terminal.
5. **Derived-view compatibility** -- ``finalise`` still materialises
   ``terminal.json``, and it carries the same run id the checkpoint exposes, so
   fleet-sentinel's file contract keeps working unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from fleet_graph.graphs.goal_line import (
    TERMINAL_BLOCKED,
    LineDeps,
    build_goal_line_graph,
)
from fleet_graph.graphs.guards import LineBounds, LineGuards
from fleet_graph.scheduler.checkpoint_terminal import (
    CheckpointTerminal,
    SqliteCheckpointTerminalReader,
    fault_tag,
    to_record,
)
from fleet_graph.scheduler.daemon import LineSpec, Scheduler, SchedulerConfig
from fleet_graph.scheduler.ignition import Refusal
from fleet_graph.state.run_artifacts import RunArtifacts
from fleet_graph.work_report import SCHEMA_VERSION


class FakeCheckpointReader:
    """A scriptable checkpoint source, shaped like SqliteCheckpointTerminalReader.

    ``records`` maps ``(folder_id, generation)`` to a terminal record dict;
    ``authoritative_empty`` marks generations whose checkpoint exists but holds
    no terminal yet (a running line); ``fault`` makes every read raise as a
    checkpoint fault.
    """

    def __init__(
        self,
        *,
        records: dict[tuple[str, int], dict[str, Any]] | None = None,
        authoritative_empty: set[tuple[str, int]] | None = None,
        fault: str | None = None,
    ) -> None:
        self.records = records or {}
        self.authoritative_empty = authoritative_empty or set()
        self.fault = fault
        self.calls: list[tuple[str, int]] = []

    def read(self, folder_id: str, generation: int) -> CheckpointTerminal:
        self.calls.append((folder_id, generation))
        if self.fault is not None:
            return CheckpointTerminal(record=None, authoritative=False, fault=self.fault)
        if (folder_id, generation) in self.authoritative_empty:
            return CheckpointTerminal(record=None, authoritative=True)
        record = self.records.get((folder_id, generation))
        if record is None:
            return CheckpointTerminal(record=None, authoritative=False)
        return CheckpointTerminal(record=record, authoritative=True)


class FakeUnits:
    def is_active(self, unit_name: str) -> bool:
        return False


class FakeProber:
    def check(self, seat: str) -> bool:
        return True


class FakeWake:
    def __init__(self, revision: str = "sha256:rev-1") -> None:
        self.revision = revision

    def inbox_message_after(self, alias: str, after_epoch: float) -> bool:
        return False

    def goal_revision(self, folder_id: str) -> str:
        return self.revision


class FakeLauncher:
    def __init__(self) -> None:
        self.launched: list[Any] = []

    def launch(self, spec: Any) -> Any:
        self.launched.append(spec)

        class _Result:
            unit_name = spec.unit_name
            started = True
            detail = ""

        return _Result()


def make(
    tmp_path: Path,
    *,
    checkpoints: Any = None,
    lines: list[LineSpec] | None = None,
) -> Scheduler:
    return Scheduler(
        SchedulerConfig(
            lines=lines or [LineSpec(folder_id="wf-1", seat="s", enabled=True)],
            run_root=tmp_path / "runs",
            maintenance_stop_path=tmp_path / "maintenance-stop",
        ),
        prober=FakeProber(),
        launcher=FakeLauncher(),
        units=FakeUnits(),
        clock=lambda: 1_000.0,
        sleep=lambda _s: None,
        wake=FakeWake(),
        checkpoints=checkpoints,
    )


def write_terminal(tmp_path: Path, folder_id: str, record: dict[str, Any]) -> None:
    path = tmp_path / "runs" / folder_id / "terminal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def blocked_record(*, run_id: str = "run-ck", waiting_on: str = "decision") -> dict[str, Any]:
    return {
        "terminal": "blocked",
        "rounds": 0,
        "run_id": run_id,
        "waiting_on": waiting_on,
        "at": "2026-08-28T10:00:00Z",
        "pump_fault": False,
    }


class TestCheckpointIsAuthoritative:
    def test_checkpoint_terminal_wins_over_a_conflicting_terminal_json(
        self, tmp_path: Path
    ) -> None:
        reader = FakeCheckpointReader(records={("wf-1", 1): {"terminal": "done"}})
        write_terminal(tmp_path, "wf-1", {"terminal": "blocked", "rounds": 0})
        scheduler = make(tmp_path, checkpoints=reader)

        assert scheduler.terminal_of("wf-1") == "done"
        assert scheduler.tick()[0].decision.refusal is Refusal.TERMINAL_DONE

    def test_a_missing_terminal_json_does_not_change_the_checkpoint_terminal(
        self, tmp_path: Path
    ) -> None:
        reader = FakeCheckpointReader(records={("wf-1", 1): {"terminal": "done"}})
        scheduler = make(tmp_path, checkpoints=reader)

        assert scheduler.terminal_of("wf-1") == "done"

    def test_a_stale_terminal_json_is_ignored(self, tmp_path: Path) -> None:
        reader = FakeCheckpointReader(records={("wf-1", 1): {"terminal": "done"}})
        write_terminal(tmp_path, "wf-1", {"terminal": "blocked", "rounds": 9})
        scheduler = make(tmp_path, checkpoints=reader)

        assert scheduler.terminal_record("wf-1")["terminal"] == "done"

    def test_the_ordinary_path_never_reads_terminal_json(self, tmp_path: Path) -> None:
        """Structural: when the checkpoint answers, the terminal.json reader is
        not consulted at all. Monkeypatching it to raise proves the decision
        path never touches it."""
        reader = FakeCheckpointReader(records={("wf-1", 1): blocked_record()})
        scheduler = make(tmp_path, checkpoints=reader)

        def explode(folder_id: str) -> dict[str, Any]:
            raise AssertionError("terminal.json was consulted despite a checkpoint answer")

        scheduler._terminal_json_record = explode  # type: ignore[method-assign]
        record = scheduler.terminal_record("wf-1")
        assert record is not None and record["terminal"] == "blocked"

    def test_an_authoritative_running_line_is_not_overridden_by_stale_json(
        self, tmp_path: Path
    ) -> None:
        """A checkpoint that exists but holds no terminal yet (the line is
        running) must read as "no terminal", not as the stale terminal.json of
        the previous run -- even a stale `done` must not read as TERMINAL_DONE."""
        reader = FakeCheckpointReader(authoritative_empty={("wf-1", 1)})
        write_terminal(tmp_path, "wf-1", {"terminal": "done", "rounds": 0, "run_id": "old-run"})
        scheduler = make(tmp_path, checkpoints=reader)

        assert scheduler.terminal_record("wf-1") is None
        assert scheduler.tick()[0].decision.refusal is not Refusal.TERMINAL_DONE

    def test_the_default_scheduler_still_reads_terminal_json(self, tmp_path: Path) -> None:
        """Backward compatibility: without a checkpoint reader the scheduler is
        unchanged and reads the derived view."""
        write_terminal(tmp_path, "wf-1", {"terminal": "done", "rounds": 0})
        assert make(tmp_path).terminal_of("wf-1") == "done"


class TestFaultFallback:
    def test_a_checkpoint_fault_falls_back_to_terminal_json(self, tmp_path: Path) -> None:
        reader = FakeCheckpointReader(fault="DatabaseError")
        write_terminal(tmp_path, "wf-1", {"terminal": "blocked", "rounds": 0, "run_id": "r-1"})
        scheduler = make(tmp_path, checkpoints=reader)

        record = scheduler.terminal_record("wf-1")
        assert record is not None and record["terminal"] == "blocked"
        assert scheduler.checkpoint_fault_reason("wf-1") == "DatabaseError"

    def test_a_checkpoint_fault_is_never_treated_as_completed(self, tmp_path: Path) -> None:
        """The fallback honours whatever terminal.json says, but an unreadable
        checkpoint must never collapse into a silent "done"."""
        reader = FakeCheckpointReader(fault="DatabaseError")
        write_terminal(tmp_path, "wf-1", {"terminal": "fault", "rounds": 0})
        scheduler = make(tmp_path, checkpoints=reader)

        assert scheduler.terminal_of("wf-1") == "fault"
        assert scheduler.terminal_of("wf-1") != "done"

    def test_a_checkpoint_fault_with_no_terminal_json_is_no_terminal(self, tmp_path: Path) -> None:
        reader = FakeCheckpointReader(fault="DiskError")
        scheduler = make(tmp_path, checkpoints=reader)
        assert scheduler.terminal_of("wf-1") is None
        assert scheduler.checkpoint_fault_reason("wf-1") == "DiskError"

    def test_a_crashed_line_without_a_checkpoint_terminal_surfaces_the_fault(
        self, tmp_path: Path
    ) -> None:
        """A crash escapes the graph before finalise, so the checkpoint holds no
        terminal. The fault-path supplementation in terminal.json is the one
        trace, and the scheduler must still honour it (never read the crash as
        "no terminal", which would pass for a merely-running line)."""
        reader = FakeCheckpointReader(authoritative_empty={("wf-1", 1)})
        write_terminal(
            tmp_path,
            "wf-1",
            {"terminal": "fault", "rounds": 0, "run_id": "r-crash", "pump_fault": True},
        )
        scheduler = make(tmp_path, checkpoints=reader)

        record = scheduler.terminal_record("wf-1")
        assert record is not None
        assert record["terminal"] == "fault"
        assert record["run_id"] == "r-crash"


class TestReader:
    def test_fault_tag_names_the_sqlalchemy_code(self) -> None:
        assert fault_tag(RuntimeError("boom")) == "RuntimeError"
        assert fault_tag(_WithCode("e", "26")) == "_WithCode:26"

    def test_to_record_maps_checkpoint_keys_to_scheduler_keys(self) -> None:
        record = to_record(
            {
                "terminal": "blocked",
                "rounds_recorded": 4,
                "run_id": "run-x",
                "waiting_on": "decision",
                "pump_fault": True,
                "goal_revision": "sha256:rev-1",
            },
            created_at="2026-08-28T10:00:00.123456+00:00",
        )
        assert record == {
            "terminal": "blocked",
            "rounds": 4,
            "run_id": "run-x",
            "waiting_on": "decision",
            "at": "2026-08-28T10:00:00Z",
            "pump_fault": True,
            "goal_revision": "sha256:rev-1",
            "dd_development_id": None,
            "line_state": "waiting_decision",
        }

    def test_to_record_with_no_terminal_is_none(self) -> None:
        assert to_record({"rounds_recorded": 3}, created_at=None) is None


class _WithCode(RuntimeError):
    def __init__(self, name: str, code: str) -> None:
        super().__init__(name)
        self.code = code


# --- the real checkpoint wiring, end to end ---------------------------------


class ScriptedCoordinator:
    def turn(self, round_no: int, coord_input: dict[str, Any]) -> dict[str, Any]:
        if round_no >= 2:
            return {"verdict": "blocked", "reason": "needs human", "waiting_on": "decision"}
        return {"verdict": "continue", "next_prompt": f"step {round_no}"}


class _Worker:
    def turn(self, prompt: str, round_no: int) -> dict:
        # E4a: a worker turn is a structured v1 report; a bare string would be
        # rejected by report validation and fault the line before it can park.
        return {
            "schema_version": SCHEMA_VERSION,
            "turn_id": f"t-{round_no}",
            "outcome": "completed",
            "summary": f"did {prompt[:40]}",
            "did": ["completed action"],
            "files": [],
            "self_tests": [],
            "blocker": None,
        }


class _Inbox:
    def drain_then_ack(self, persist: Any) -> tuple[list[Any], list[str]]:
        persist([])
        return [], []


def run_real_line(tmp_path: Path, folder_id: str, run_id: str) -> None:
    """Run a real graph into a real sqlite checkpoint + terminal.json."""
    folder = tmp_path / "runs" / folder_id
    folder.mkdir(parents=True, exist_ok=True)
    artifacts = RunArtifacts(folder, run_id=run_id, folder_id=folder_id)
    deps = LineDeps(
        coordinator=ScriptedCoordinator(),
        worker=_Worker(),
        inbox=_Inbox(),
        artifacts=artifacts,
        guards=LineGuards(bounds=LineBounds(max_rounds=10)),
        folder_id=folder_id,
        run_id=run_id,
    )
    with SqliteSaver.from_conn_string(str(folder / "checkpoint.sqlite3")) as saver:
        build_goal_line_graph(deps).compile(checkpointer=saver).invoke(
            {"round_no": 1},
            config={
                "configurable": {"thread_id": f"{folder_id}:g1"},
                "recursion_limit": 200,
            },
        )


class TestRealCheckpointWiring:
    def test_the_scheduler_reads_the_blocked_terminal_from_the_checkpoint(
        self, tmp_path: Path
    ) -> None:
        run_real_line(tmp_path, "wf-1", "run-real")
        scheduler = make(tmp_path, checkpoints=SqliteCheckpointTerminalReader(tmp_path / "runs"))

        record = scheduler.terminal_record("wf-1")
        assert record is not None
        assert record["terminal"] == TERMINAL_BLOCKED
        assert record["waiting_on"] == "decision"
        assert record["run_id"] == "run-real"
        assert record["rounds"] == 1

    def test_the_derived_terminal_json_stays_consistent_with_the_checkpoint(
        self, tmp_path: Path
    ) -> None:
        """fleet-sentinel's file contract is unchanged: finalise still wrote
        terminal.json, and its run_id matches what the checkpoint exposes."""
        run_real_line(tmp_path, "wf-1", "run-real")
        terminal = json.loads((tmp_path / "runs" / "wf-1" / "terminal.json").read_text())
        assert terminal["terminal"] == "blocked"
        assert terminal["run_id"] == "run-real"
        assert terminal["waiting_on"] == "decision"

        reader = SqliteCheckpointTerminalReader(tmp_path / "runs")
        checkpoint = reader.read("wf-1", 1)
        assert checkpoint.record is not None
        assert checkpoint.record["run_id"] == terminal["run_id"]

    def test_a_checkpoint_read_of_a_never_run_generation_is_fallthrough(
        self, tmp_path: Path
    ) -> None:
        reader = SqliteCheckpointTerminalReader(tmp_path / "runs")
        reading = reader.read("wf-never", 1)
        assert reading.authoritative is False
        assert reading.fault is None
        assert reading.record is None
