"""The re-adopt contract.

plan.md P1 gates P3 on this file: if a graph process dies while agent runs are
in flight, a restarted graph must neither re-dispatch them nor lose their
results. Everything else in P1 can be re-done cheaply; this cannot.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fleet_graph.executors.agent_run import (
    AgentRunLauncher,
    AgentRunSpec,
    RunTicket,
    RunWaitTimeout,
    derive_run_id,
    find_result,
    find_run_dir,
)

FAKE = str(Path(__file__).parent / "fakes" / "fake_agent_run.py")


@pytest.fixture
def launcher(tmp_path: Path) -> AgentRunLauncher:
    wrapper = tmp_path / "agent-run"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    wrapper.chmod(0o755)
    return AgentRunLauncher(bin_path=str(wrapper), state_root=str(tmp_path / "runs"))


def dispatch_count(launcher: AgentRunLauncher, run_id: str) -> int:
    ledger = launcher.session_root_for(run_id) / "dispatch.log"
    if not ledger.exists():
        return 0
    return len([line for line in ledger.read_text().splitlines() if line.strip()])


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestDerivedRunId:
    def test_is_stable_across_calls(self) -> None:
        a = derive_run_id("thread-7", "worker_turn", 1)
        b = derive_run_id("thread-7", "worker_turn", 1)
        assert a == b

    def test_separates_threads_nodes_and_attempts(self) -> None:
        base = derive_run_id("thread-7", "worker_turn", 1)
        assert derive_run_id("thread-8", "worker_turn", 1) != base
        assert derive_run_id("thread-7", "coordinator_turn", 1) != base
        assert derive_run_id("thread-7", "worker_turn", 2) != base

    def test_rejects_attempt_below_one(self) -> None:
        with pytest.raises(ValueError, match="attempt must be >= 1"):
            derive_run_id("thread-7", "worker_turn", 0)


class TestHappyPath:
    def test_run_reaches_succeeded_and_carries_result(self, launcher: AgentRunLauncher) -> None:
        run_id = derive_run_id("t1", "n1")
        ticket = launcher.launch(AgentRunSpec(prompt="sleep=0"), run_id)
        assert ticket.adopted is False

        status = launcher.wait(ticket, poll_interval=0.05, deadline_seconds=60)
        assert status.state == "succeeded"
        assert status.ok
        assert status.result is not None
        assert status.result["run_id"] == run_id
        assert dispatch_count(launcher, run_id) == 1

    def test_nonzero_exit_is_failed_not_lost(self, launcher: AgentRunLauncher) -> None:
        run_id = derive_run_id("t1", "n-fail")
        ticket = launcher.launch(AgentRunSpec(prompt="sleep=0 exit=3"), run_id)
        status = launcher.wait(ticket, poll_interval=0.05, deadline_seconds=60)
        assert status.state == "failed"
        assert status.result is not None
        assert status.result["exit_code"] == 3

    def test_run_dir_is_discoverable_from_the_session_root(
        self, launcher: AgentRunLauncher
    ) -> None:
        run_id = derive_run_id("t1", "n-dir")
        ticket = launcher.launch(AgentRunSpec(prompt="sleep=0"), run_id)
        launcher.wait(ticket, poll_interval=0.05, deadline_seconds=60)
        run_dir = find_run_dir(ticket.session_root)
        assert run_dir is not None
        assert Path(run_dir, "result.json").exists()


class TestReAdopt:
    """The gating cases: a restart must not double-dispatch or drop a result."""

    def test_relaunch_while_running_adopts_instead_of_dispatching_again(
        self, launcher: AgentRunLauncher
    ) -> None:
        run_id = derive_run_id("t2", "worker_turn")
        spec = AgentRunSpec(prompt="sleep=2")

        first = launcher.launch(spec, run_id)
        assert first.adopted is False
        assert wait_until(lambda: dispatch_count(launcher, run_id) == 1)

        # A fresh launcher object stands in for a restarted graph process:
        # nothing carried over in memory.
        restarted = AgentRunLauncher(
            bin_path=launcher.bin_path, state_root=str(launcher.state_root)
        )
        second = restarted.launch(spec, run_id)

        assert second.adopted is True, "restart re-dispatched a run that was already going"
        assert second.session_root == first.session_root
        assert restarted.poll(second).state == "running"

        status = restarted.wait(second, poll_interval=0.05, deadline_seconds=60)
        assert status.state == "succeeded"
        assert dispatch_count(launcher, run_id) == 1, "the run was dispatched more than once"

    def test_result_survives_a_restart_that_loses_the_ticket(
        self, launcher: AgentRunLauncher
    ) -> None:
        """The checkpoint write is lost, but the derived id rebuilds the ticket."""
        run_id = derive_run_id("t3", "worker_turn")
        spec = AgentRunSpec(prompt="sleep=1")
        launcher.launch(spec, run_id)

        # Simulate the graph dying before it could persist anything at all.
        del spec
        restarted = AgentRunLauncher(
            bin_path=launcher.bin_path, state_root=str(launcher.state_root)
        )
        rebuilt = restarted.launch(
            AgentRunSpec(prompt="sleep=1"), derive_run_id("t3", "worker_turn")
        )
        assert rebuilt.adopted is True

        status = restarted.wait(rebuilt, poll_interval=0.05, deadline_seconds=60)
        assert status.ok
        assert status.result is not None
        assert status.result["run_id"] == run_id
        assert dispatch_count(launcher, run_id) == 1

    def test_adopting_a_finished_run_returns_its_result(self, launcher: AgentRunLauncher) -> None:
        run_id = derive_run_id("t4", "worker_turn")
        spec = AgentRunSpec(prompt="sleep=0")
        ticket = launcher.launch(spec, run_id)
        launcher.wait(ticket, poll_interval=0.05, deadline_seconds=60)

        restarted = AgentRunLauncher(
            bin_path=launcher.bin_path, state_root=str(launcher.state_root)
        )
        adopted = restarted.launch(spec, run_id)
        assert adopted.adopted is True
        status = restarted.poll(adopted)
        assert status.state == "succeeded"
        assert dispatch_count(launcher, run_id) == 1

    def test_a_re_adopted_run_keeps_the_labels_it_was_first_dispatched_with(
        self, launcher: AgentRunLauncher
    ) -> None:
        """kill-resume: a restarted process mints a new launch id, but the
        adopted run must keep the first dispatch's label (idempotent upsert) --
        the launcher never rewrites argv.json for an adopted session root."""
        run_id = derive_run_id("t-labels", "coordinator-1")
        launcher.launch(
            AgentRunSpec(
                prompt="sleep=2",
                labels={"role": "supervisor", "goal": "wf-x", "launch": "launch-A", "round": "1"},
            ),
            run_id,
        )
        assert wait_until(lambda: dispatch_count(launcher, run_id) == 1)

        restarted = AgentRunLauncher(
            bin_path=launcher.bin_path, state_root=str(launcher.state_root)
        )
        adopted = restarted.launch(
            AgentRunSpec(
                prompt="sleep=2",
                labels={"role": "supervisor", "goal": "wf-x", "launch": "launch-B", "round": "1"},
            ),
            run_id,
        )
        assert adopted.adopted is True

        argv = json.loads((Path(adopted.session_root) / "argv.json").read_text())
        assert "launch=launch-A" in argv
        assert "launch=launch-B" not in argv

        restarted.wait(adopted, poll_interval=0.05, deadline_seconds=60)

    def test_child_outlives_the_parent_process(self, launcher: AgentRunLauncher) -> None:
        """Detachment is the whole point -- verify it rather than assuming it.

        A grandchild launched from a subprocess we then kill must keep running
        and still write its result.
        """
        run_id = derive_run_id("t5", "worker_turn")
        script = (
            "import sys;"
            f"sys.path.insert(0, {str(Path(__file__).parents[1] / 'src')!r});"
            "from fleet_graph.executors.agent_run import AgentRunLauncher, AgentRunSpec;"
            f"AgentRunLauncher(bin_path={launcher.bin_path!r},"
            f" state_root={str(launcher.state_root)!r})"
            f".launch(AgentRunSpec(prompt='sleep=2'), {run_id!r})"
        )
        parent = subprocess.run([sys.executable, "-c", script], check=True, timeout=30)
        assert parent.returncode == 0

        # The parent is gone; the agent run should not be.
        ticket = RunTicket(run_id, str(launcher.session_root_for(run_id)))
        assert launcher.poll(ticket).state == "running"
        status = launcher.wait(ticket, poll_interval=0.05, deadline_seconds=60)
        assert status.ok
        assert dispatch_count(launcher, run_id) == 1


class TestFailureModes:
    def test_dead_process_with_no_result_is_lost_not_running(
        self, launcher: AgentRunLauncher
    ) -> None:
        run_id = derive_run_id("t6", "worker_turn")
        session_root = launcher.session_root_for(run_id)
        session_root.mkdir(parents=True)
        # A pid that is certainly not our run.
        (session_root / "launcher.pid").write_text(str(os.getpid()))

        ticket = RunTicket(run_id, str(session_root), pid=os.getpid())
        assert launcher.poll(ticket).state == "lost"

    def test_stale_session_root_is_reused_not_abandoned(self, launcher: AgentRunLauncher) -> None:
        """Crash between mkdir and exec leaves an empty root; relaunch must dispatch."""
        run_id = derive_run_id("t7", "worker_turn")
        launcher.session_root_for(run_id).mkdir(parents=True)

        ticket = launcher.launch(AgentRunSpec(prompt="sleep=0"), run_id)
        assert ticket.adopted is False
        assert launcher.wait(ticket, poll_interval=0.05, deadline_seconds=60).ok
        assert dispatch_count(launcher, run_id) == 1

    def test_result_landing_during_the_liveness_check_is_not_called_lost(
        self, launcher: AgentRunLauncher, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The poll race: finished-and-exited must never be mistaken for died.

        poll() reads the result, then checks liveness, then re-reads the result
        before declaring death. A run whose result lands in the window between
        the first read and the liveness check looks exactly like one that died
        -- and reporting `lost` there would discard a real result and invite a
        duplicate dispatch.

        The timing is constructed, not raced: `find_result` is stubbed to read
        nothing on its first call (the result has not landed yet) and to return
        the real result on its second call (it landed during the liveness
        check). The pid file points at a pid above pid_max that cannot be live,
        so the liveness branch deterministically sees the process gone. No
        subprocess, no sleep, no wall-clock dependency -- the old version raced
        a real fake-agent-run's exit against poll() and flaked under load
        (observed: `assert 'running' == 'succeeded'`).

        Known negative / reproducibility: delete the second find_result re-read
        in AgentRunLauncher.poll (the block re-reading the result before
        declaring death) and this test fails with `lost` -- the run whose
        result landed mid-check gets misjudged as dead. That mutation is the
        exact product defect this test pins.
        """
        run_id = derive_run_id("t8", "worker_turn")
        session_root = launcher.session_root_for(run_id)
        session_root.mkdir(parents=True)
        # A pid above pid_max cannot be live, so the liveness branch is
        # deterministic: poll must fall through to the re-read, never
        # short-circuit on "running".
        (session_root / "launcher.pid").write_text(str(4_194_303))

        from fleet_graph.executors import agent_run as module

        real_find_result = module.find_result
        calls = {"n": 0}

        # Lay down the result.json the real agent-run would have produced.
        run_dir = session_root / f"2026-08-26-02-30-00-000-{run_id[:6]}"
        run_dir.mkdir()
        (run_dir / "result.json").write_text(
            json.dumps(
                {"state": "succeeded", "exit_code": 0, "exit_reason": "normal", "run_id": run_id}
            )
        )

        def racing_find_result(session_root):
            # First read sees nothing (the run had not finished yet); by the
            # time we look again the result is there.
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            return real_find_result(session_root)

        monkeypatch.setattr(module, "find_result", racing_find_result)

        # pid is long dead, so poll falls through to the liveness branch.
        status = launcher.poll(RunTicket(run_id, str(session_root), pid=None))
        assert status.state == "succeeded", "a completed run was reported as lost"
        assert status.result is not None
        assert status.result["run_id"] == run_id
        assert calls["n"] == 2

    def test_wait_timeout_raises_and_never_reports_lost(self, launcher: AgentRunLauncher) -> None:
        """Giving up waiting is not the same fact as the run being gone.

        If wait() returned `lost` here, a caller would retry and the still-live
        original would make it a double dispatch -- the exact failure the
        re-adopt contract exists to prevent.
        """
        run_id = derive_run_id("t9", "worker_turn")
        ticket = launcher.launch(AgentRunSpec(prompt="sleep=3"), run_id)

        with pytest.raises(RunWaitTimeout) as excinfo:
            launcher.wait(ticket, poll_interval=0.05, deadline_seconds=0.2)

        assert excinfo.value.ticket.run_id == run_id
        assert launcher.poll(ticket).state == "running"

        # And it stays adoptable: no second dispatch, result still collected.
        status = launcher.wait(ticket, poll_interval=0.05, deadline_seconds=30)
        assert status.ok
        assert dispatch_count(launcher, run_id) == 1

    def test_find_result_tolerates_a_missing_root(self, tmp_path: Path) -> None:
        assert find_result(tmp_path / "nope") is None
        assert find_run_dir(tmp_path / "nope") is None


class TestArgv:
    def test_carries_run_id_session_root_and_labels(self) -> None:
        spec = AgentRunSpec(
            prompt="do the thing",
            role="goal_coordinator",
            runtime="opencode",
            write=True,
            labels={"work_folder": "wf-3f30cd", "dispatcher": "fleet-graph"},
        )
        argv = spec.argv(bin_path="/bin/agent-run", run_id="rid", session_root="/tmp/sr")

        assert argv[0] == "/bin/agent-run"
        assert argv[-2:] == ["--", "do the thing"]
        assert "--run-id" in argv and argv[argv.index("--run-id") + 1] == "rid"
        assert argv[argv.index("--session-root") + 1] == "/tmp/sr"
        assert "--write" in argv
        assert "--json" in argv
        assert "--label" in argv
        assert "dispatcher=fleet-graph" in argv
        assert "work_folder=wf-3f30cd" in argv

    def test_read_only_by_default(self) -> None:
        argv = AgentRunSpec(prompt="p").argv(
            bin_path="/bin/agent-run", run_id="r", session_root="/tmp/s"
        )
        assert "--write" not in argv


class TestLivenessIdentity:
    """`_pid_is_our_run` decides whether a live pid is *our* run.

    Getting this wrong in the pessimistic direction is expensive: a healthy run
    reported `lost` gets dispatched a second time.
    """

    def test_empty_cmdline_counts_as_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The kernel blanks cmdline for the width of an execve."""
        from fleet_graph.executors import agent_run as module

        monkeypatch.setattr(module.Path, "read_bytes", lambda self: b"")
        assert module._pid_is_our_run(os.getpid(), "any-run-id") is True

    def test_foreign_cmdline_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A recycled pid running something else must not be adopted."""
        from fleet_graph.executors import agent_run as module

        monkeypatch.setattr(module.Path, "read_bytes", lambda self: b"/usr/bin/vim\x00notes.txt")
        assert module._pid_is_our_run(os.getpid(), "our-run-id") is False

    def test_matching_cmdline_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fleet_graph.executors import agent_run as module

        monkeypatch.setattr(
            module.Path, "read_bytes", lambda self: b"/bin/sh\x00--run-id\x00our-run-id"
        )
        assert module._pid_is_our_run(os.getpid(), "our-run-id") is True

    def test_dead_pid_is_rejected(self) -> None:
        from fleet_graph.executors import agent_run as module

        # PID 2^22 is above the default pid_max and cannot be live.
        assert module._pid_is_our_run(4_194_303, "our-run-id") is False

    def test_zombie_pid_is_not_alive(self) -> None:
        """A child that exited but has not been reaped still answers kill(0).

        Treating that as alive would hang a poll on a finished run.
        """
        from fleet_graph.executors import agent_run as module

        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            assert wait_until(lambda: module._is_zombie(proc.pid), timeout=10)
            assert module._pid_is_our_run(proc.pid, "our-run-id") is False
        finally:
            proc.wait()
