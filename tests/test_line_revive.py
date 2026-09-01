"""M5: line revival -- the first-class revoke that can overturn a `done`.

Covers the frozen-scope acceptance:

1. **Positive** -- a fake checkpoint `done` at generation g plus a valid revoke
   (who/basis/generation/when, matching g) makes `decide` *not* return
   TERMINAL_DONE, forces the generation bump, re-ignites the line on a fresh
   thread, surfaces `revoked:<who>:g<gen>` on the tick result, and consumes the
   record (append-only history, no repeat).
2. **Revival passthrough** -- the launcher carries the revival envelope, and the
   round-1 coordinator input carries both `revival` and `prior_terminal` (the
   old `done` terminal, so the line knows what was overturned).
3. **Negative C1** -- forged/stale terminal.json still cannot overturn the
   checkpoint: terminal_of stays `done`, tick stays TERMINAL_DONE, no revival.
4. **Negative C2** -- forged/stale revokes are inert: wrong generation, or the
   line's current checkpoint is not `done` -> no ignite, no bump, no "revived"
   trace, record not consumed.
5. **Negative C3** -- a revoke missing/blanking any C1 audit field is refused on
   write and never lands on disk.
6. **decide order** -- the revoke allowance touches only the done branch; every
   other refusal order is unchanged.
7. **CLI precheck** -- `perform_line_revive` refuses `refused: target not
   terminal_done` / `refused: generation mismatch` and writes only on match.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.scheduler.checkpoint_terminal import CheckpointTerminal
from fleet_graph.scheduler.daemon import LineSpec, Scheduler, SchedulerConfig
from fleet_graph.scheduler.ignition import IgnitionDecision, LineStatus, Refusal, decide
from fleet_graph.scheduler.launcher import LaunchResult, LaunchSpec
from fleet_graph.scheduler.revive import (
    REQUIRED_REVIVE_FIELDS,
    ReviveFieldError,
    ReviveRecord,
    ReviveStore,
    validate_revive,
)

NOW = 1_787_000_000.0


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

    def read(self, folder_id: str, generation: int) -> CheckpointTerminal:
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


class FakeLauncher:
    def __init__(self) -> None:
        self.launched: list[Any] = []

    def launch(self, spec: Any) -> LaunchResult:
        self.launched.append(spec)
        return LaunchResult(spec.unit_name, True, "")


def make(
    tmp_path: Path,
    *,
    checkpoints: Any = None,
    revives: ReviveStore | None = None,
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
        clock=lambda: NOW,
        sleep=lambda _s: None,
        checkpoints=checkpoints,
        revives=revives,
    )


def done_record(*, run_id: str = "run-done", generation: int = 1) -> dict[str, Any]:
    return {
        "terminal": "done",
        "rounds": 5,
        "run_id": run_id,
        "waiting_on": "none",
        "at": "2026-09-01T10:00:00Z",
        "pump_fault": False,
    }


def make_revive(
    *,
    folder_id: str = "wf-1",
    who: str = "alice",
    basis: str = "goal-md-ruling-42",
    generation: int = 1,
    when: str = "2026-09-01T11:00:00Z",
    reason: str = "",
) -> ReviveRecord:
    return validate_revive(
        {
            "folder_id": folder_id,
            "who": who,
            "basis": basis,
            "generation": generation,
            "when": when,
            "reason": reason,
        }
    )


def write_terminal(tmp_path: Path, folder_id: str, record: dict[str, Any]) -> None:
    path = tmp_path / "runs" / folder_id / "terminal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


# --- positive: a valid revoke overturns a checkpoint done ------------------


class TestAValidRevokeRevives:
    def test_the_done_latch_is_cleared_and_the_line_reignites(self, tmp_path: Path) -> None:
        reader = FakeCheckpointReader(records={("wf-1", 1): done_record()})
        revives = ReviveStore(tmp_path / "runs")
        revives.write(make_revive(generation=1))
        scheduler = make(tmp_path, checkpoints=reader, revives=revives)

        results = scheduler.tick()
        result = results[0]
        assert result.decision.ignite is True
        assert result.decision.refusal is not Refusal.TERMINAL_DONE
        assert result.revoke_event == "revoked:alice:g1"
        assert result.as_dict()["revoke"] == "revoked:alice:g1"

    def test_the_generation_is_bumped_and_the_launch_is_a_fresh_thread(
        self, tmp_path: Path
    ) -> None:
        reader = FakeCheckpointReader(records={("wf-1", 1): done_record()})
        revives = ReviveStore(tmp_path / "runs")
        revives.write(make_revive(generation=1))
        launcher = FakeLauncher()
        scheduler = make(tmp_path, checkpoints=reader, revives=revives)
        scheduler.launcher = launcher

        scheduler.tick()
        assert scheduler.generation_of(scheduler.config.lines[0]) == 2
        launched = launcher.launched[0]
        assert launched.generation == 2
        assert launched.unit_name == "fleet-graph-line-wf-1-g2"

    def test_the_revoke_is_consumed_into_append_only_history(self, tmp_path: Path) -> None:
        reader = FakeCheckpointReader(records={("wf-1", 1): done_record()})
        revives = ReviveStore(tmp_path / "runs")
        revives.write(make_revive(generation=1))
        scheduler = make(tmp_path, checkpoints=reader, revives=revives)

        scheduler.tick()
        assert revives.get("wf-1") is None, "a consumed revoke must not re-fire"
        assert [h["who"] for h in revives.history()] == ["alice"]
        # A second tick sees no active revoke: done holds again.
        assert scheduler.tick()[0].decision.refusal is Refusal.TERMINAL_DONE

    def test_the_launch_carries_the_revival_envelope(self, tmp_path: Path) -> None:
        reader = FakeCheckpointReader(records={("wf-1", 1): done_record()})
        revives = ReviveStore(tmp_path / "runs")
        revives.write(make_revive(generation=1, reason="goal changed upstream"))
        launcher = FakeLauncher()
        scheduler = make(tmp_path, checkpoints=reader, revives=revives)
        scheduler.launcher = launcher

        scheduler.tick()
        revival = launcher.launched[0].revival
        assert revival is not None
        assert revival["who"] == "alice"
        assert revival["basis"] == "goal-md-ruling-42"
        assert revival["generation"] == 1
        assert revival["reason"] == "goal changed upstream"

    def test_a_revoked_line_that_cannot_launch_is_still_a_refusal_not_a_pass(
        self, tmp_path: Path
    ) -> None:
        """The revoke only clears the done latch; every other gate still bites."""
        reader = FakeCheckpointReader(records={("wf-1", 1): done_record()})
        revives = ReviveStore(tmp_path / "runs")
        revives.write(make_revive(generation=1))
        scheduler = make(
            tmp_path,
            checkpoints=reader,
            revives=revives,
            lines=[LineSpec(folder_id="wf-1", seat="s", enabled=False)],
        )
        # A disabled line is not revived at all -- the one-shot must not be
        # burned on a line that cannot launch.
        scheduler.tick()
        assert revives.get("wf-1") is not None


# --- revival passthrough: launcher argv + round-1 coordinator input ---------


class TestRevivalPassthrough:
    def test_launcher_argv_carries_one_json_revival_argument(self) -> None:
        spec = LaunchSpec(
            folder_id="wf-1",
            seat="s",
            generation=2,
            revival={"who": "alice", "basis": "b-1", "generation": 1, "reason": ""},
        )
        argv = spec.argv()
        recovered = json.loads(argv[argv.index("--revival") + 1])
        assert recovered["who"] == "alice"
        assert recovered["basis"] == "b-1"

    def test_no_revival_means_no_flag(self) -> None:
        spec = LaunchSpec(folder_id="wf-1", seat="s")
        assert "--revival" not in spec.argv()

    def test_round_1_coord_input_has_both_revival_and_prior_terminal(self, tmp_path: Path) -> None:
        """The revived line's round-1 coordinator input carries the revival
        envelope and the old `done` terminal side by side, so the line can
        read who overturned what, on what basis."""
        from fleet_graph.graphs.goal_line import LineDeps, build_goal_line_graph
        from fleet_graph.graphs.guards import LineBounds, LineGuards

        class CapturingCoordinator:
            def __init__(self) -> None:
                self.inputs: list[dict[str, Any]] = []

            def turn(self, round_no: int, coord_input: dict[str, Any]) -> dict[str, Any]:
                self.inputs.append(json.loads(json.dumps(coord_input)))
                return {"verdict": "continue", "next_prompt": "keep going"}

        class NullWorker:
            def turn(self, prompt: str, round_no: int) -> str:
                return ""

        class NullInbox:
            def drain_then_ack(self, persist: Any) -> tuple[Any, list[str]]:
                persist([])
                return [], []

        class NullArtifacts:
            def heartbeat(self, round_no: int, phase: str, *, force: bool = False) -> bool:
                return True

            def append_round(self, line: dict[str, Any]) -> bool:
                return True

            def write_terminal(self, **_kwargs: Any) -> str:
                return "terminal.json"

        coordinator = CapturingCoordinator()
        deps = LineDeps(
            coordinator=coordinator,
            worker=NullWorker(),
            inbox=NullInbox(),
            artifacts=NullArtifacts(),
            guards=LineGuards(bounds=LineBounds(max_rounds=10)),
            folder_id="wf-1",
            prior_terminal=done_record(run_id="run-done"),
            revival={"who": "alice", "basis": "goal-md-ruling-42", "generation": 1, "reason": ""},
        )
        from langgraph.checkpoint.memory import InMemorySaver

        build_goal_line_graph(deps).compile(checkpointer=InMemorySaver()).invoke(
            {"round_no": 1}, config={"configurable": {"thread_id": "t1"}}
        )
        first = coordinator.inputs[0]
        assert first["revival"]["who"] == "alice"
        assert first["revival"]["basis"] == "goal-md-ruling-42"
        assert first["revival"]["generation"] == 1
        assert first["prior_terminal"]["terminal"] == "done"
        assert first["prior_terminal"]["run_id"] == "run-done"

    def test_round_1_without_revival_has_no_revival_field(self, tmp_path: Path) -> None:
        from fleet_graph.graphs.goal_line import LineDeps, build_goal_line_graph
        from fleet_graph.graphs.guards import LineGuards

        class CapturingCoordinator:
            def __init__(self) -> None:
                self.inputs: list[dict[str, Any]] = []

            def turn(self, round_no: int, coord_input: dict[str, Any]) -> dict[str, Any]:
                self.inputs.append(coord_input)
                return {"verdict": "continue", "next_prompt": "keep going"}

        class NullWorker:
            def turn(self, prompt: str, round_no: int) -> str:
                return ""

        class NullInbox:
            def drain_then_ack(self, persist: Any) -> tuple[Any, list[str]]:
                persist([])
                return [], []

        class NullArtifacts:
            def heartbeat(self, round_no: int, phase: str, *, force: bool = False) -> bool:
                return True

            def append_round(self, line: dict[str, Any]) -> bool:
                return True

            def write_terminal(self, **_kwargs: Any) -> str:
                return "terminal.json"

        coordinator = CapturingCoordinator()
        deps = LineDeps(
            coordinator=coordinator,
            worker=NullWorker(),
            inbox=NullInbox(),
            artifacts=NullArtifacts(),
            guards=LineGuards(),
            folder_id="wf-1",
            revival=None,
        )
        from langgraph.checkpoint.memory import InMemorySaver

        build_goal_line_graph(deps).compile(checkpointer=InMemorySaver()).invoke(
            {"round_no": 1}, config={"configurable": {"thread_id": "t1"}}
        )
        assert "revival" not in coordinator.inputs[0]


# --- negative C1: forged terminal.json cannot overturn the checkpoint --------


class TestForgedTerminalJsonCannotOverturnCheckpoint:
    def test_a_forged_not_done_terminal_json_does_not_revive(self, tmp_path: Path) -> None:
        """Checkpoint authoritative `done` + terminal.json forged as `blocked`:
        the checkpoint wins, the tick still refuses TERMINAL_DONE, and no
        revival happens (there is no revoke record)."""
        reader = FakeCheckpointReader(records={("wf-1", 1): done_record()})
        write_terminal(tmp_path, "wf-1", {"terminal": "blocked", "rounds": 0})
        scheduler = make(tmp_path, checkpoints=reader)

        assert scheduler.terminal_of("wf-1") == "done"
        result = scheduler.tick()[0]
        assert result.decision.refusal is Refusal.TERMINAL_DONE
        assert result.revoke_event is None
        assert "revoke" not in result.as_dict()

    def test_a_forged_revived_terminal_json_is_not_a_revive_source(self, tmp_path: Path) -> None:
        """Writing `revived` into terminal.json is not a revoke record; the
        scheduler must never read it as one."""
        reader = FakeCheckpointReader(records={("wf-1", 1): done_record()})
        write_terminal(tmp_path, "wf-1", {"terminal": "revived", "rounds": 0})
        scheduler = make(tmp_path, checkpoints=reader)

        assert scheduler.terminal_of("wf-1") == "done"
        assert scheduler.tick()[0].decision.refusal is Refusal.TERMINAL_DONE
        assert ReviveStore(tmp_path / "runs").get("wf-1") is None


# --- negative C2: forged/stale revokes are inert -----------------------------


class TestForgedStaleRevokesAreInert:
    def test_a_revoke_at_the_wrong_generation_is_inert(self, tmp_path: Path) -> None:
        """Checkpoint done at g1, revoke written for g2: no match -> no ignite,
        no bump, no revived trace, record still active."""
        reader = FakeCheckpointReader(records={("wf-1", 1): done_record()})
        revives = ReviveStore(tmp_path / "runs")
        revives.write(make_revive(generation=2))
        scheduler = make(tmp_path, checkpoints=reader, revives=revives)

        result = scheduler.tick()[0]
        assert result.decision.ignite is False
        assert result.decision.refusal is Refusal.TERMINAL_DONE
        assert result.revoke_event is None
        assert scheduler.generation_of(scheduler.config.lines[0]) == 1
        assert revives.get("wf-1") is not None

    def test_a_revoke_for_a_line_whose_checkpoint_is_not_done_is_inert(
        self, tmp_path: Path
    ) -> None:
        """Checkpoint says `blocked`, revoke matches g1: the line's current
        terminal is not `done`, so the revoke must not fire -- and a blocked
        line may still restart on its own (no TERMINAL_DONE), but no revoke
        event and no forced bump happen."""
        reader = FakeCheckpointReader(
            records={("wf-1", 1): {"terminal": "blocked", "rounds": 0, "run_id": "run-b"}}
        )
        revives = ReviveStore(tmp_path / "runs")
        revives.write(make_revive(generation=1))
        scheduler = make(tmp_path, checkpoints=reader, revives=revives)

        result = scheduler.tick()[0]
        assert result.revoke_event is None
        assert "revoke" not in result.as_dict()
        assert revives.get("wf-1") is not None
        # A blocked line restarts through the ordinary path, not a revival.
        assert result.decision.refusal is not Refusal.TERMINAL_DONE

    def test_a_revoke_for_a_running_line_with_no_terminal_is_inert(self, tmp_path: Path) -> None:
        """An authoritative checkpoint that holds no terminal yet (a running
        line) must not satisfy the `done` requirement for a revoke."""
        reader = FakeCheckpointReader(authoritative_empty={("wf-1", 1)})
        revives = ReviveStore(tmp_path / "runs")
        revives.write(make_revive(generation=1))
        scheduler = make(tmp_path, checkpoints=reader, revives=revives)

        result = scheduler.tick()[0]
        assert result.revoke_event is None
        assert revives.get("wf-1") is not None
        assert scheduler.terminal_of("wf-1") is None


# --- negative C3: missing C1 audit fields refuse the write -------------------


class TestC1AuditFieldsAreRequired:
    def test_required_fields_are_who_basis_generation_when(self) -> None:
        assert set(REQUIRED_REVIVE_FIELDS) == {"who", "basis", "generation", "when"}

    @pytest.mark.parametrize(
        "field",
        ["who", "basis", "generation", "when"],
    )
    def test_missing_any_one_field_refuses_the_write(self, field: str) -> None:
        record = make_revive()
        bad = record.as_dict()
        del bad[field]
        with pytest.raises(ReviveFieldError, match=field):
            validate_revive(bad)

    @pytest.mark.parametrize(
        "field",
        ["who", "basis", "when"],
    )
    def test_blanking_any_string_field_refuses_the_write(self, field: str) -> None:
        record = make_revive()
        bad = record.as_dict()
        bad[field] = "   "
        with pytest.raises(ReviveFieldError, match=field):
            validate_revive(bad)

    def test_a_non_integer_generation_is_refused(self) -> None:
        record = make_revive().as_dict()
        record["generation"] = "not-a-number"
        with pytest.raises(ReviveFieldError, match="integer"):
            validate_revive(record)

    def test_a_non_positive_generation_is_refused(self) -> None:
        record = make_revive().as_dict()
        record["generation"] = 0
        with pytest.raises(ReviveFieldError, match="generation must be >= 1"):
            validate_revive(record)

    def test_reason_alone_cannot_stand_in_for_basis(self) -> None:
        with pytest.raises(ReviveFieldError, match="basis"):
            validate_revive(
                {
                    "folder_id": "wf-1",
                    "who": "alice",
                    "basis": "",
                    "generation": 1,
                    "when": "2026-09-01T11:00:00Z",
                    "reason": "i have a good feeling about this",
                }
            )

    def test_a_missing_field_never_lands_on_disk(self, tmp_path: Path) -> None:
        store = ReviveStore(tmp_path / "runs")
        record = make_revive().as_dict()
        del record["who"]
        with pytest.raises(ReviveFieldError):
            store.write(ReviveRecord(**{**record, "who": ""}))  # type: ignore[arg-type]
        assert store.get("wf-1") is None
        assert not (tmp_path / "runs" / ".scheduler" / "revive.json").exists()

    def test_a_forged_record_with_blank_basis_is_dropped_from_the_live_view(
        self, tmp_path: Path
    ) -> None:
        """A record that slips onto disk without a basis (a forge) validates as
        nothing: the store drops it from the live view, so it is inert."""
        store = ReviveStore(tmp_path / "runs")
        record = make_revive().as_dict()
        record["basis"] = ""
        store._save({record["folder_id"]: make_revive()})
        (tmp_path / "runs" / ".scheduler" / "revive.json").write_text(
            json.dumps({"wf-1": {**record, "basis": ""}}), encoding="utf-8"
        )
        assert store.get("wf-1") is None


# --- decide order: only the done branch gains the revoke allowance -----------


def call(st: LineStatus, **kwargs: Any) -> IgnitionDecision:
    params: dict[str, Any] = {
        "now": NOW,
        "enabled": True,
        "maintenance_stop": False,
        "zero_progress_streak": 0,
        "gateway_healthy": True,
        "unproductive_recent": 0,
    }
    params.update(kwargs)
    return decide(st, **params)


def status(**kwargs: Any) -> LineStatus:
    base = {"folder_id": "wf-1", "seat": "s"}
    base.update(kwargs)
    return LineStatus(**base)  # type: ignore[arg-type]


class TestDecideOrderIsUnchanged:
    def test_revived_done_falls_through_to_ignition(self) -> None:
        decision = call(status(terminal="done"), revived=True)
        assert decision.ignite is True

    def test_without_revive_done_stays_final(self) -> None:
        assert call(status(terminal="done")).refusal is Refusal.TERMINAL_DONE

    def test_the_roster_still_outranks_a_revive(self) -> None:
        decision = call(status(terminal="done"), enabled=False, revived=True)
        assert decision.refusal is Refusal.LINE_DISABLED

    def test_maintenance_stop_still_wins_over_a_revive(self) -> None:
        decision = call(status(terminal="done"), maintenance_stop=True, revived=True)
        assert decision.refusal is Refusal.MAINTENANCE_STOP

    def test_already_running_still_wins_over_a_revive(self) -> None:
        decision = call(status(terminal="done", running=True), revived=True)
        assert decision.refusal is Refusal.ALREADY_RUNNING

    def test_the_gateway_probe_still_bites_a_revived_line(self) -> None:
        decision = call(status(terminal="done"), revived=True, gateway_healthy=False)
        assert decision.refusal is Refusal.GATEWAY_RED

    def test_cooldown_still_bites_a_revived_line(self) -> None:
        decision = call(status(terminal="done", last_start_at=NOW - 10), revived=True)
        assert decision.refusal is Refusal.COOLING_DOWN

    def test_the_total_cap_still_bites_a_revived_line(self) -> None:
        from fleet_graph.scheduler.ignition import DEFAULT_TOTAL_CAP

        decision = call(
            status(terminal="done"),
            revived=True,
            unproductive_recent=DEFAULT_TOTAL_CAP,
        )
        assert decision.refusal is Refusal.TOTAL_CAP_REACHED


# --- CLI precheck ------------------------------------------------------------


class TestCliPrecheck:
    def test_perform_line_revive_requires_who_and_basis(self, tmp_path: Path) -> None:
        from fleet_graph.cli import perform_line_revive

        roster = tmp_path / "lines.json"
        roster.write_text(json.dumps({"lines": [{"folder_id": "wf-1", "seat": "s"}]}))
        with pytest.raises(SystemExit, match="--who"):
            perform_line_revive(
                folder_id="wf-1",
                who="",
                basis="b",
                lines_config=roster,
                run_root=tmp_path / "runs",
            )
        with pytest.raises(SystemExit, match="--basis"):
            perform_line_revive(
                folder_id="wf-1",
                who="alice",
                basis="",
                lines_config=roster,
                run_root=tmp_path / "runs",
            )

    def test_perform_line_revive_requires_generation_or_run_id(self, tmp_path: Path) -> None:
        from fleet_graph.cli import perform_line_revive

        roster = tmp_path / "lines.json"
        roster.write_text(json.dumps({"lines": [{"folder_id": "wf-1", "seat": "s"}]}))
        with pytest.raises(SystemExit, match="--generation or --run-id"):
            perform_line_revive(
                folder_id="wf-1",
                who="alice",
                basis="b",
                lines_config=roster,
                run_root=tmp_path / "runs",
            )

    def test_perform_line_revive_refuses_when_target_is_not_done(self, tmp_path: Path) -> None:
        from fleet_graph.cli import perform_line_revive

        roster = tmp_path / "lines.json"
        roster.write_text(json.dumps({"lines": [{"folder_id": "wf-1", "seat": "s"}]}))
        reader = FakeCheckpointReader(
            records={("wf-1", 1): {"terminal": "blocked", "rounds": 0, "run_id": "run-b"}}
        )
        with pytest.raises(SystemExit, match="target not terminal_done"):
            perform_line_revive(
                folder_id="wf-1",
                who="alice",
                basis="goal-md-ruling-42",
                generation=1,
                lines_config=roster,
                run_root=tmp_path / "runs",
                checkpoints=reader,
            )

    def test_perform_line_revive_refuses_on_generation_mismatch(self, tmp_path: Path) -> None:
        from fleet_graph.cli import perform_line_revive

        roster = tmp_path / "lines.json"
        roster.write_text(json.dumps({"lines": [{"folder_id": "wf-1", "seat": "s"}]}))
        reader = FakeCheckpointReader(records={("wf-1", 1): done_record(run_id="run-done")})
        with pytest.raises(SystemExit, match="generation mismatch"):
            perform_line_revive(
                folder_id="wf-1",
                who="alice",
                basis="goal-md-ruling-42",
                generation=2,
                lines_config=roster,
                run_root=tmp_path / "runs",
                checkpoints=reader,
            )

    def test_perform_line_revive_refuses_on_run_id_mismatch(self, tmp_path: Path) -> None:
        from fleet_graph.cli import perform_line_revive

        roster = tmp_path / "lines.json"
        roster.write_text(json.dumps({"lines": [{"folder_id": "wf-1", "seat": "s"}]}))
        reader = FakeCheckpointReader(records={("wf-1", 1): done_record(run_id="run-done")})
        with pytest.raises(SystemExit, match="generation mismatch"):
            perform_line_revive(
                folder_id="wf-1",
                who="alice",
                basis="goal-md-ruling-42",
                run_id="run-other",
                lines_config=roster,
                run_root=tmp_path / "runs",
                checkpoints=reader,
            )

    def test_perform_line_revive_writes_and_bumps_on_match(self, tmp_path: Path) -> None:
        from fleet_graph.cli import perform_line_revive
        from fleet_graph.state.run_artifacts import iso

        roster = tmp_path / "lines.json"
        roster.write_text(json.dumps({"lines": [{"folder_id": "wf-1", "seat": "s"}]}))
        reader = FakeCheckpointReader(records={("wf-1", 1): done_record(run_id="run-done")})
        result = perform_line_revive(
            folder_id="wf-1",
            who="alice",
            basis="goal-md-ruling-42",
            generation=1,
            reason="goal changed upstream",
            lines_config=roster,
            run_root=tmp_path / "runs",
            checkpoints=reader,
            clock=lambda: 1_787_000_000.0,
        )
        assert result["who"] == "alice"
        assert result["basis"] == "goal-md-ruling-42"
        assert result["generation"] == 1
        assert result["when"] == iso(1_787_000_000.0)
        assert result["next_generation"] == 2
        store = ReviveStore(tmp_path / "runs")
        stored = store.get("wf-1")
        assert stored is not None
        assert stored.basis == "goal-md-ruling-42"
        assert stored.generation == 1
