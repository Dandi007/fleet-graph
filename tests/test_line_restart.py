"""Stable thread identity and the durable-serial-kernel contract, on the real
wiring.

1. **Thread identity** (R0a): `build_line` used to fold a per-process uuid4
   into thread_id, so every restart re-randomised every derived run id and
   re-adopt could never trigger. thread_id must be `{folder_id}:g{generation}`
   and nothing else.
2. **Resume semantics** (R0a): what `invoke` does on a thread with an existing
   SqliteSaver checkpoint is measured here, not assumed -- see
   TestResumeSemantics and the `resume_start` docstring.
3. **Durable serial kernel** (DD01): `build_line` composes the Goal
   request-to-Goal-call kernel as the goal-facing coordinator, and a round's
   request + call records land in a durable journal under the run root. The
   round-prompt agent-run kill-restart contract (adopt the in-flight run)
   belongs to the retired path; the launcher primitives it used stay pinned by
   tests/test_re_adopt.py and tests/test_adapters.py.

Reverting the thread_id fix (thread_id = f"{folder_id}:{uuid4}") turns
TestThreadIdentity red: two `build_line` calls disagree on the identity every
run id is derived from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from fleet_graph.graphs.goal_line import LineDeps, build_goal_line_graph
from fleet_graph.graphs.guards import LineBounds, LineGuards
from fleet_graph.graphs.runner import LineConfig, build_line, resume_start
from fleet_graph.work_report import SCHEMA_VERSION


class TestThreadIdentity:
    def test_thread_id_is_folder_and_generation_and_nothing_else(self, tmp_path: Path) -> None:
        config = LineConfig(folder_id="wf-abc123", seat="s", run_root=tmp_path, generation=3)
        assert config.thread_id == "wf-abc123:g3"

    def test_two_processes_of_the_same_generation_share_the_thread(self, tmp_path: Path) -> None:
        """Stand-in for a kill-restart: two independent build_line calls must
        agree on the identity every run id is derived from."""
        config = LineConfig(folder_id="wf-abc123", seat="s", run_root=tmp_path)
        _, first = build_line(config)
        _, second = build_line(config)
        assert first.coordinator.thread_id == second.coordinator.thread_id == "wf-abc123:g1"

    def test_run_id_never_enters_the_thread_id(self, tmp_path: Path) -> None:
        config = LineConfig(folder_id="wf-abc123", seat="s", run_root=tmp_path)
        _, deps = build_line(config, run_id="11111111-2222-3333-4444-555555555555")
        assert "1111" not in deps.coordinator.thread_id

    def test_checkpoint_defaults_to_disk_under_run_root(self, tmp_path: Path) -> None:
        config = LineConfig(folder_id="wf-1", seat="s", run_root=tmp_path)
        assert config.resolved_checkpoint_path == str(tmp_path / "checkpoint.sqlite3")
        explicit = LineConfig(
            folder_id="wf-1", seat="s", run_root=tmp_path, checkpoint_path=":memory:"
        )
        assert explicit.resolved_checkpoint_path == ":memory:"


class TestLifecycleLabels:
    """role/goal/launch/round label injection at line assembly (spec DoD 1-2)."""

    def test_build_line_mints_a_stable_per_process_launch_id(self, tmp_path: Path) -> None:
        config = LineConfig(folder_id="wf-labels", seat="s", run_root=tmp_path, generation=2)
        _, deps = build_line(config)

        launch_id = deps.coordinator.launch_id
        assert launch_id.startswith("launch-wf-labels-g2-")
        assert deps.coordinator.launch_id == launch_id

    def test_a_caller_supplied_launch_id_is_used_verbatim(self, tmp_path: Path) -> None:
        _, deps = build_line(
            LineConfig(folder_id="wf-labels", seat="s", run_root=tmp_path, launch_id="launch-fixed")
        )
        assert deps.coordinator.launch_id == "launch-fixed"

    def test_seat_labels_carry_role_goal_and_launch(self, tmp_path: Path) -> None:
        _, deps = build_line(
            LineConfig(folder_id="wf-labels", seat="s", run_root=tmp_path, launch_id="launch-fixed")
        )
        labels = deps.worker.seat_spec.labels
        assert labels["role"] == "worker"
        assert labels["goal"] == "wf-labels"
        assert labels["launch"] == "launch-fixed"
        assert labels["work_folder"] == "wf-labels"


# --- resume semantics, measured against a real SqliteSaver -----------------


class Boom(RuntimeError):
    """Stands in for SIGKILL: aborts the invoke mid-node, after the previous
    super-step's checkpoint has been persisted -- the same durable state a
    killed process leaves behind."""


class ScriptedCoordinator:
    def __init__(
        self,
        *,
        die_on_round: int | None = None,
        done_on_round: int = 5,
        terminal_verdict: str = "done",
    ) -> None:
        self.die_on_round = die_on_round
        self.done_on_round = done_on_round
        self.terminal_verdict = terminal_verdict
        self.calls: list[int] = []

    def turn(self, round_no: int, coord_input: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(round_no)
        if self.die_on_round is not None and round_no >= self.die_on_round:
            raise Boom(f"killed during coordinator round {round_no}")
        if round_no >= self.done_on_round:
            return {"verdict": self.terminal_verdict, "reason": "script end"}
        return {"verdict": "continue", "next_prompt": f"step {round_no}"}


class RecordingWorker:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def turn(self, prompt: str, round_no: int) -> dict[str, Any]:
        self.calls.append(round_no)
        return {
            "schema_version": SCHEMA_VERSION,
            "turn_id": f"t-{round_no}",
            "outcome": "completed",
            "summary": f"did {prompt}",
            "did": [prompt],
            "files": [],
            "self_tests": [],
            "blocker": None,
        }


class NullInbox:
    def drain_then_ack(self, persist: Any) -> tuple[list[Any], list[str]]:
        persist([])
        return [], []


class RecordingArtifacts:
    def __init__(self) -> None:
        self.terminal: dict[str, Any] | None = None

    def heartbeat(self, round_no: int, phase: str, *, force: bool = False) -> bool:
        return True

    def append_round(self, line: dict[str, Any]) -> bool:
        return True

    def write_worker_report(self, round_no: int, report: dict[str, Any]) -> str:
        return "worker-report.json"

    def write_terminal(
        self,
        *,
        terminal: str,
        rounds: int,
        reason: str | None = None,
        pump_fault: bool = False,
        waiting_on: str = "none",
        waiting_on_declared: str | None = None,
        goal_revision: str | None = None,
        dd_development_id: str | None = None,
    ) -> str:
        self.terminal = {"terminal": terminal, "rounds": rounds}
        return "terminal.json"


def line_graph(coordinator: ScriptedCoordinator) -> tuple[Any, RecordingWorker]:
    worker = RecordingWorker()
    deps = LineDeps(
        coordinator=coordinator,
        worker=worker,
        inbox=NullInbox(),
        artifacts=RecordingArtifacts(),
        guards=LineGuards(bounds=LineBounds(max_rounds=50)),
        folder_id="wf-resume",
    )
    return build_goal_line_graph(deps), worker


CFG = {"configurable": {"thread_id": "wf-resume:g1"}, "recursion_limit": 200}


class TestResumeSemantics:
    """Measured behaviour of langgraph 1.2.11 + SqliteSaver 3.1.1. Every
    assertion here was observed, not read off documentation."""

    def _kill_mid_round_three(self, db: str) -> None:
        graph, _ = line_graph(ScriptedCoordinator(die_on_round=3))
        with SqliteSaver.from_conn_string(db) as saver:
            compiled = graph.compile(checkpointer=saver)
            with pytest.raises(Boom):
                compiled.invoke({"round_no": 1}, config=CFG)

    def test_fresh_thread_gets_round_one(self, tmp_path: Path) -> None:
        graph, _ = line_graph(ScriptedCoordinator())
        with SqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite3")) as saver:
            compiled = graph.compile(checkpointer=saver)
            snapshot = compiled.get_state(CFG)
            assert snapshot.next == ()
            assert snapshot.created_at is None
            assert resume_start(compiled, CFG) == {"round_no": 1}

    def test_kill_leaves_a_checkpoint_pointing_at_the_dying_node(self, tmp_path: Path) -> None:
        db = str(tmp_path / "cp.sqlite3")
        self._kill_mid_round_three(db)

        graph, _ = line_graph(ScriptedCoordinator())
        with SqliteSaver.from_conn_string(db) as saver:
            compiled = graph.compile(checkpointer=saver)
            snapshot = compiled.get_state(CFG)
            # The super-step before the kill was checkpointed; the killed node
            # is what resumes.
            assert snapshot.next == ("coordinator_turn",)
            assert snapshot.values["round_no"] == 3
            assert snapshot.values["rounds_recorded"] == 2
            assert resume_start(compiled, CFG) is None

    def test_invoke_none_resumes_the_killed_round_and_keeps_the_count(self, tmp_path: Path) -> None:
        """Observed: invoke(None) re-enters coordinator_turn at round 3 --
        rounds 1 and 2 are not replayed and rounds_recorded accumulates."""
        db = str(tmp_path / "cp.sqlite3")
        self._kill_mid_round_three(db)

        coordinator = ScriptedCoordinator()
        graph, worker = line_graph(coordinator)
        with SqliteSaver.from_conn_string(db) as saver:
            compiled = graph.compile(checkpointer=saver)
            state = compiled.invoke(resume_start(compiled, CFG), config=CFG)

        assert coordinator.calls == [3, 4, 5], "resume must start where the kill hit"
        assert worker.calls == [3, 4]
        assert state["terminal"] == "done"
        assert state["rounds_recorded"] == 4

    def test_replaying_round_one_input_double_runs_the_line(self, tmp_path: Path) -> None:
        """Observed hazard, pinned so nobody 'simplifies' resume_start away:
        handing {"round_no": 1} to a pending thread replays rounds the line
        already completed. With a stable thread_id that replay is the exact
        duplicate-dispatch shape re-adopt exists to prevent."""
        db = str(tmp_path / "cp.sqlite3")
        self._kill_mid_round_three(db)

        coordinator = ScriptedCoordinator()
        graph, _ = line_graph(coordinator)
        with SqliteSaver.from_conn_string(db) as saver:
            compiled = graph.compile(checkpointer=saver)
            state = compiled.invoke({"round_no": 1}, config=CFG)

        assert coordinator.calls == [1, 2, 3, 4, 5], "the replay this test documents"
        assert state["rounds_recorded"] == 6, "double-counted: 2 from before + 4 replayed"

    def test_completed_thread_goes_straight_to_finalise_without_a_coordinator_call(
        self, tmp_path: Path
    ) -> None:
        """Same generation relaunched after a clean terminal must not restart
        the work; a genuinely new attempt is a new generation (scheduler's
        job, out of scope here)."""
        db = str(tmp_path / "cp.sqlite3")
        first = ScriptedCoordinator(done_on_round=2)
        graph, _ = line_graph(first)
        with SqliteSaver.from_conn_string(db) as saver:
            compiled = graph.compile(checkpointer=saver)
            state = compiled.invoke({"round_no": 1}, config=CFG)
            assert state["terminal"] == "done"

        second = ScriptedCoordinator()
        graph2, _ = line_graph(second)
        with SqliteSaver.from_conn_string(db) as saver:
            compiled2 = graph2.compile(checkpointer=saver)
            start = resume_start(compiled2, CFG)
            assert start == {"round_no": 1}, "a finished thread has nothing pending"
            state = compiled2.invoke(start, config=CFG)

        assert second.calls == [], "terminal in carried state routes past the coordinator"
        assert state["terminal"] == "done"


# --- the durable serial-kernel contract, on the production wiring -------------


class TestKernelDurability:
    """DD01: the round-prompt kill-restart contract (adopt the in-flight
    agent-run) is superseded by the kernel's durable per-goal journal. ``build_line``
    composes the kernel as the goal-facing coordinator; a round whose Goal ReAct
    call is still unbound parks with ``goal_call_unwired`` and leaves its request
    + call records durably under the run root for a restart to re-observe."""

    def test_build_line_composes_the_kernel_and_writes_a_durable_journal(
        self, tmp_path: Path
    ) -> None:
        import json

        from fleet_graph.graphs.kernel_coordinator import KernelCoordinator

        run_root = tmp_path / "run"
        folder_id = "wf-kernel"
        config = LineConfig(folder_id=folder_id, seat="test-seat", run_root=run_root)

        graph, deps = build_line(config)
        assert isinstance(deps.coordinator, KernelCoordinator)
        assert deps.coordinator.thread_id == f"{folder_id}:g1"

        # One round through the real (kernel-coordinated) graph: the round parks
        # (the Goal ReAct call is unbound in this slice) and the serial records
        # land in the durable journal.
        state = graph.compile().invoke({"round_no": 1})
        assert state["terminal"] == "blocked"

        journal = run_root / "goal-kernel" / f"goal-{folder_id}.jsonl"
        assert journal.exists(), "the kernel journal must be durable under the run root"
        records = [
            line for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        parsed = [json.loads(record) for record in records]
        assert any(r["record"] == "request" for r in parsed)
        assert any(r["record"] == "call" for r in parsed)


class TestBumpedGenerationGivesAFreshThread:
    """R0a-2, across both layers: a blocked line's thread is spent (relaunching
    it no-op finalises, pinned above), so the scheduler's accounted-terminal
    generation bump is what makes re-ignition do work again. This test runs
    the real sequence: g1 runs to blocked on a durable checkpoint, the
    scheduler accounts the terminal, and the generation it hands the next
    launch must yield a thread on which the coordinator is genuinely called
    from round 1."""

    def test_blocked_line_reignites_on_a_fresh_thread_and_actually_works(
        self, tmp_path: Path
    ) -> None:
        import json

        from fleet_graph.scheduler.daemon import LineSpec, Scheduler, SchedulerConfig

        folder = "wf-bump"
        run_root = tmp_path / "runs" / folder
        run_root.mkdir(parents=True)
        db = str(run_root / "checkpoint.sqlite3")

        def cfg(generation: int) -> dict[str, Any]:
            return {
                "configurable": {"thread_id": f"{folder}:g{generation}"},
                "recursion_limit": 200,
            }

        # Generation 1 runs and ends blocked; the line leaves its terminal.
        first = ScriptedCoordinator(done_on_round=2, terminal_verdict="blocked")
        graph, _ = line_graph(first)
        with SqliteSaver.from_conn_string(db) as saver:
            state = graph.compile(checkpointer=saver).invoke({"round_no": 1}, config=cfg(1))
        assert state["terminal"] == "blocked"
        (run_root / "terminal.json").write_text(
            json.dumps(
                {"terminal": "blocked", "rounds": state["rounds_recorded"], "run_id": "r-1"}
            ),
            encoding="utf-8",
        )

        # The scheduler accounts that terminal and advances the generation.
        line = LineSpec(folder_id=folder, seat="s", enabled=True)
        scheduler = Scheduler(SchedulerConfig(lines=[line], run_root=tmp_path / "runs"))
        scheduler.account_last_run(folder, base_generation=line.generation)
        generation = scheduler.generation_of(line)

        # Re-ignite as the scheduler would: same checkpoint file, the
        # scheduler's generation. The coordinator must be genuinely called.
        second = ScriptedCoordinator(done_on_round=2)
        graph2, _ = line_graph(second)
        with SqliteSaver.from_conn_string(db) as saver:
            compiled = graph2.compile(checkpointer=saver)
            invoke_config = cfg(generation)
            state = compiled.invoke(resume_start(compiled, invoke_config), config=invoke_config)

        assert second.calls[:1] == [1], (
            "the re-ignited line never called its coordinator -- it relaunched "
            "the spent thread and no-op finalised"
        )
        assert state["terminal"] == "done"
        assert generation == 2
