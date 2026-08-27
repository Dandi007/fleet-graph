"""Stable thread identity and the kill-restart contract, on the real wiring.

tests/test_re_adopt.py pins the launcher primitives. This file pins the two
layers above them, which is where the fleet actually broke:

1. **Thread identity** (R0a): `build_line` used to fold a per-process uuid4
   into thread_id, so every restart re-randomised every derived run id and
   re-adopt could never trigger. thread_id must be `{folder_id}:g{generation}`
   and nothing else.
2. **Resume semantics** (R0a): what `invoke` does on a thread with an existing
   SqliteSaver checkpoint is measured here, not assumed -- see
   TestResumeSemantics and the `resume_start` docstring.
3. **Kill-restart** (R0b): a line built by `build_line`, killed while a (fake)
   coordinator agent-run is in flight, restarted with the same
   folder_id+generation, must derive the same run id, adopt instead of
   re-spawning, and carry the adopted result to a terminal.

Reverting the thread_id fix (thread_id = f"{folder_id}:{uuid4}") turns
TestKillRestartContract red: the restarted line derives a different run id,
spawns a second fake, and the dispatch ledger shows 2. That revert run is
recorded in the PR description.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from fleet_graph.executors.agent_run import derive_run_id
from fleet_graph.graphs.goal_line import LineDeps, build_goal_line_graph
from fleet_graph.graphs.guards import LineBounds, LineGuards
from fleet_graph.graphs.runner import LineConfig, build_line, resume_start

SLOW_FAKE = str(Path(__file__).parent / "fakes" / "fake_slow_coordinator_run.py")


def wait_until(predicate, timeout: float = 20.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


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


# --- resume semantics, measured against a real SqliteSaver -----------------


class Boom(RuntimeError):
    """Stands in for SIGKILL: aborts the invoke mid-node, after the previous
    super-step's checkpoint has been persisted -- the same durable state a
    killed process leaves behind."""


class ScriptedCoordinator:
    def __init__(self, *, die_on_round: int | None = None, done_on_round: int = 5) -> None:
        self.die_on_round = die_on_round
        self.done_on_round = done_on_round
        self.calls: list[int] = []

    def turn(self, round_no: int, coord_input: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(round_no)
        if self.die_on_round is not None and round_no >= self.die_on_round:
            raise Boom(f"killed during coordinator round {round_no}")
        if round_no >= self.done_on_round:
            return {"verdict": "done", "reason": "script end"}
        return {"verdict": "continue", "next_prompt": f"step {round_no}"}


class RecordingWorker:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def turn(self, prompt: str, round_no: int) -> str:
        self.calls.append(round_no)
        return f"did {prompt}"


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

    def write_terminal(
        self, *, terminal: str, rounds: int, reason: str | None = None, pump_fault: bool = False
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


# --- the kill-restart contract, on the production wiring --------------------


class TestKillRestartContract:
    """build_line/run_line wiring, a real detached fake agent-run, a real
    SIGKILL of the line process, and a restart under the same identity."""

    def test_restarted_line_adopts_the_in_flight_coordinator_run(self, tmp_path: Path) -> None:
        run_root = tmp_path / "run"
        folder_id = "wf-killrestart"
        # The identity every run id is derived from, and the whole point:
        # both the killed process and the restarted one must compute this.
        expected_run_id = derive_run_id(f"{folder_id}:g1", "coordinator-1")
        session_root = run_root / "agent-runs" / expected_run_id
        dispatch_log = session_root / "dispatch.log"
        release = session_root / "release"

        fake_bin = tmp_path / "agent-run"
        fake_bin.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{SLOW_FAKE}" "$@"\n')
        fake_bin.chmod(0o755)

        driver = tmp_path / "driver.py"
        driver.write_text(
            "from pathlib import Path\n"
            "from fleet_graph.graphs.runner import LineConfig, run_line\n"
            "run_line(LineConfig(\n"
            f"    folder_id={folder_id!r},\n"
            "    seat='test-seat',\n"
            f"    run_root=Path({str(run_root)!r}),\n"
            f"    agent_run_bin={str(fake_bin)!r},\n"
            "))\n"
        )

        def dispatch_count() -> int:
            if not dispatch_log.exists():
                return 0
            return len([ln for ln in dispatch_log.read_text().splitlines() if ln.strip()])

        line_proc = subprocess.Popen(
            [sys.executable, str(driver)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        fake_pid: int | None = None
        try:
            # Phase 1: the line dispatched the coordinator run exactly once and
            # the durable checkpoint reached disk (kill can only be survived
            # from state that was persisted before it).
            assert wait_until(lambda: dispatch_count() == 1), (
                "the line never dispatched its coordinator run; stderr: "
                + line_proc.stderr.peek().decode(errors="replace")[:2000]
            )
            assert wait_until(lambda: (run_root / "checkpoint.sqlite3").exists())
            fake_pid = int((session_root / "launcher.pid").read_text())

            # Kill the line process. The detached fake must survive it.
            line_proc.kill()
            line_proc.wait(timeout=10)
            os.kill(fake_pid, 0)  # raises if the in-flight run died with it

            # Phase 2: rebuild under the same folder_id + generation.
            config = LineConfig(
                folder_id=folder_id,
                seat="test-seat",
                run_root=run_root,
                agent_run_bin=str(fake_bin),
            )
            graph, deps = build_line(config)
            assert deps.coordinator.thread_id == f"{folder_id}:g1"

            launches = []
            inner = deps.coordinator.launcher
            original_launch = inner.launch

            def recording_launch(spec: Any, run_id: str) -> Any:
                ticket = original_launch(spec, run_id)
                launches.append(ticket)
                return ticket

            inner.launch = recording_launch  # type: ignore[method-assign]

            # Let the fake finish shortly after the restart has adopted it, so
            # the resumed wait() can return. If the timer loses the race and
            # the fake finishes first, launch() adopts the finished result --
            # still the adopt path, still one dispatch.
            releaser = threading.Timer(2.0, release.touch)
            releaser.start()
            try:
                invoke_config: dict[str, Any] = {
                    "configurable": {"thread_id": config.thread_id},
                    "recursion_limit": 100,
                }
                with SqliteSaver.from_conn_string(config.resolved_checkpoint_path) as saver:
                    compiled = graph.compile(checkpointer=saver)
                    start = resume_start(compiled, invoke_config)
                    assert start is None, "the restart must resume, not replay round 1"
                    state = compiled.invoke(start, config=invoke_config)
            finally:
                releaser.cancel()
                release.parent.mkdir(parents=True, exist_ok=True)
                release.touch()

            # The contract, in order of importance.
            assert [t.run_id for t in launches] == [expected_run_id], (
                "the restarted line derived a different run id -- thread "
                "identity is no longer stable"
            )
            assert launches[0].adopted is True, "restart re-dispatched an in-flight run"
            assert dispatch_count() == 1, "the fake agent-run was spawned twice"
            assert state["terminal"] == "done"
            assert (run_root / "terminal.json").exists()
        finally:
            release.parent.mkdir(parents=True, exist_ok=True)
            release.touch()
            if line_proc.poll() is None:
                line_proc.kill()
                line_proc.wait(timeout=10)
            if fake_pid is not None:
                wait_until(lambda: not _alive(fake_pid), timeout=10)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
