"""The ronin line graph: routing, breakers, and what it refuses to do."""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from fleet_graph.graphs.goal_line import (
    TERMINAL_BLOCKED,
    TERMINAL_BOUNDS,
    TERMINAL_DONE,
    TERMINAL_FAULT,
    LineDeps,
    build_goal_line_graph,
)
from fleet_graph.graphs.guards import LineBounds, LineGuards


class FakeCoordinator:
    """Replays a scripted sequence of verdicts."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def turn(self, round_no: int, coord_input: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(coord_input)
        return self.script.pop(0) if self.script else {"verdict": "done", "reason": "script end"}


class FakeWorker:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.prompts: list[str] = []
        self.raises = raises

    def turn(self, prompt: str, round_no: int) -> str:
        self.prompts.append(prompt)
        if self.raises is not None:
            raise self.raises
        return f"worker did: {prompt[:20]}"


class FakeInbox:
    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self.messages = messages or []
        self.persist_calls: list[list[dict[str, Any]]] = []

    def drain_then_ack(self, persist: Any) -> tuple[Any, list[str]]:
        self.persist_calls.append(self.messages)
        persist(self.messages)
        return self.messages, ["acked"] * len(self.messages)


class FakeArtifacts:
    def __init__(self) -> None:
        self.beats: list[tuple[int, str]] = []
        self.rounds: list[dict[str, Any]] = []
        self.terminal: dict[str, Any] | None = None

    def heartbeat(self, round_no: int, phase: str, *, force: bool = False) -> bool:
        self.beats.append((round_no, phase))
        return True

    def append_round(self, line: dict[str, Any]) -> bool:
        self.rounds.append(line)
        return True

    def write_terminal(
        self,
        *,
        terminal: str,
        rounds: int,
        reason: str | None = None,
        pump_fault: bool = False,
        waiting_on: str = "none",
        waiting_on_declared: str | None = None,
    ) -> str:
        self.terminal = {
            "terminal": terminal,
            "rounds": rounds,
            "reason": reason,
            "pump_fault": pump_fault,
            "waiting_on": waiting_on,
            "waiting_on_declared": waiting_on_declared,
        }
        return "terminal.json"


class FakeAcceptance:
    """Replays fixed facts, or raises to prove the step never faults the line."""

    def __init__(
        self, facts: dict[str, Any] | None = None, *, raises: Exception | None = None
    ) -> None:
        self.facts = facts or {"status": "ran", "results": []}
        self.raises = raises
        self.calls = 0

    def run(self) -> dict[str, Any]:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.facts


def run_line(
    script: list[dict[str, Any]],
    *,
    bounds: LineBounds | None = None,
    worker: FakeWorker | None = None,
    inbox: FakeInbox | None = None,
    acceptance: Any = None,
) -> tuple[FakeArtifacts, LineDeps]:
    artifacts = FakeArtifacts()
    deps = LineDeps(
        coordinator=FakeCoordinator(script),
        worker=worker or FakeWorker(),
        inbox=inbox or FakeInbox(),
        artifacts=artifacts,
        guards=LineGuards(bounds=bounds or LineBounds()),
        folder_id="wf-3f30cd",
        acceptance=acceptance,
    )
    compiled = build_goal_line_graph(deps).compile(checkpointer=InMemorySaver())
    compiled.invoke(
        {"round_no": 1}, config={"configurable": {"thread_id": "t1"}, "recursion_limit": 100}
    )
    return artifacts, deps


class TestTermination:
    def test_done_verdict_ends_the_line(self) -> None:
        artifacts, _ = run_line([{"verdict": "done", "reason": "acceptance passed"}])
        assert artifacts.terminal is not None
        assert artifacts.terminal["terminal"] == TERMINAL_DONE
        assert artifacts.terminal["reason"] == "acceptance passed"
        assert artifacts.terminal["pump_fault"] is False

    def test_blocked_verdict_ends_the_line(self) -> None:
        artifacts, _ = run_line([{"verdict": "blocked", "reason": "needs a ruling"}])
        assert artifacts.terminal["terminal"] == TERMINAL_BLOCKED

    def test_bounds_stop_the_line(self) -> None:
        script = [{"verdict": "continue", "next_prompt": f"step {i}"} for i in range(10)]
        artifacts, _ = run_line(script, bounds=LineBounds(max_rounds=3))
        assert artifacts.terminal["terminal"] == TERMINAL_BOUNDS
        assert "max_rounds" in artifacts.terminal["reason"]

    def test_terminal_is_always_recorded(self) -> None:
        """A line that ended with no trace is indistinguishable from one that vanished."""
        artifacts, _ = run_line([{"verdict": "done"}])
        assert artifacts.terminal is not None


class TestRounds:
    def test_a_full_round_reaches_the_worker(self) -> None:
        worker = FakeWorker()
        run_line(
            [
                {"verdict": "continue", "next_prompt": "do the first thing"},
                {"verdict": "done", "reason": "finished"},
            ],
            worker=worker,
        )
        assert worker.prompts == ["do the first thing"]

    def test_worker_output_feeds_the_next_coordinator_turn(self) -> None:
        _, deps = run_line(
            [
                {"verdict": "continue", "next_prompt": "first"},
                {"verdict": "done"},
            ]
        )
        second_input = deps.coordinator.calls[1]
        assert second_input["last_turn_output"].startswith("worker did:")

    def test_each_round_is_appended(self) -> None:
        artifacts, _ = run_line(
            [
                {"verdict": "continue", "next_prompt": "first"},
                {"verdict": "continue", "next_prompt": "second, quite different"},
                {"verdict": "done"},
            ]
        )
        assert [r["round"] for r in artifacts.rounds] == [1, 2]

    def test_heartbeat_marks_all_three_phases(self) -> None:
        artifacts, _ = run_line(
            [{"verdict": "continue", "next_prompt": "first"}, {"verdict": "done"}]
        )
        phases = {phase for _, phase in artifacts.beats}
        assert phases == {"coordinator", "worker", "acceptance"}


class TestBreakers:
    def test_repeated_prompt_is_not_injected(self) -> None:
        """INV-9: the worker must never see the same prompt twice."""
        worker = FakeWorker()
        run_line(
            [
                {"verdict": "continue", "next_prompt": "identical instruction"},
                {"verdict": "continue", "next_prompt": "a genuinely different instruction"},
                {"verdict": "continue", "next_prompt": "identical instruction"},
                {"verdict": "done"},
            ],
            worker=worker,
        )
        assert worker.prompts.count("identical instruction") == 1

    def test_a_refused_round_is_recorded_as_not_injected(self) -> None:
        artifacts, _ = run_line(
            [
                {"verdict": "continue", "next_prompt": "same"},
                {"verdict": "continue", "next_prompt": "different enough to pass"},
                {"verdict": "continue", "next_prompt": "same"},
                {"verdict": "done"},
            ]
        )
        refused = [r for r in artifacts.rounds if r["injected"] is False]
        assert refused and refused[0]["reason"] == "duplicate"

    def test_repeated_prompts_eventually_block_the_line(self) -> None:
        """Three no-op rounds trip the streak limit rather than looping forever."""
        script = [{"verdict": "continue", "next_prompt": "stuck"} for _ in range(8)]
        artifacts, _ = run_line(script, bounds=LineBounds(max_rounds=20, noop_limit=3))
        assert artifacts.terminal["terminal"] == TERMINAL_BLOCKED
        assert "without progress" in artifacts.terminal["reason"]

    def test_worker_timeout_counts_toward_its_own_limit(self) -> None:
        worker = FakeWorker(raises=TimeoutError("worker did not answer"))
        script = [
            {"verdict": "continue", "next_prompt": f"attempt number {i} at the task"}
            for i in range(6)
        ]
        artifacts, _ = run_line(script, worker=worker, bounds=LineBounds(max_rounds=20))
        assert artifacts.terminal["terminal"] == TERMINAL_BLOCKED
        assert "timeouts" in artifacts.terminal["reason"]
        assert any(r["reason"] == "worker_turn_timeout" for r in artifacts.rounds)

    def test_a_typed_session_timeout_reaches_the_same_timeout_path(self) -> None:
        """AgentSessionTimeout inherits TimeoutError, so the in-band TURN_TIMEOUT
        the seat reports must hit the graceful path -- not crash the line."""
        from fleet_graph.executors.agent_session import AgentSessionTimeout

        worker = FakeWorker(raises=AgentSessionTimeout("turn exceeded 3000s"))
        script = [
            {"verdict": "continue", "next_prompt": f"attempt number {i} at the task"}
            for i in range(6)
        ]
        artifacts, _ = run_line(script, worker=worker, bounds=LineBounds(max_rounds=20))
        assert artifacts.terminal["terminal"] == TERMINAL_BLOCKED
        assert "timeouts" in artifacts.terminal["reason"]
        assert any(r["reason"] == "worker_turn_timeout" for r in artifacts.rounds)

    def test_a_non_timeout_session_error_still_propagates(self) -> None:
        """A seat failure that is not a timeout must keep crashing, not be
        silently absorbed as a no-op round."""
        from fleet_graph.executors.agent_session import AgentSessionError

        worker = FakeWorker(raises=AgentSessionError("seat exploded"))
        script = [{"verdict": "continue", "next_prompt": "do the thing"}]
        with pytest.raises(AgentSessionError):
            run_line(script, worker=worker)


class TestFaults:
    def test_unrecognised_verdict_is_a_fault_not_a_guess(self) -> None:
        """Interpreting an unknown verdict would be the INV-3 violation itself."""
        artifacts, _ = run_line([{"verdict": "maybe?", "next_prompt": "x"}])
        assert artifacts.terminal["terminal"] == TERMINAL_FAULT
        assert artifacts.terminal["pump_fault"] is True

    def test_continue_without_a_prompt_is_a_fault(self) -> None:
        artifacts, _ = run_line([{"verdict": "continue", "next_prompt": "   "}])
        assert artifacts.terminal["terminal"] == TERMINAL_FAULT
        assert artifacts.terminal["pump_fault"] is True


class TestInboxOrdering:
    def test_messages_reach_the_coordinator_input(self) -> None:
        inbox = FakeInbox([{"message_id": "msg-1", "payload": {"text": "hi"}}])
        _, deps = run_line([{"verdict": "done"}], inbox=inbox)
        assert deps.coordinator.calls[0]["inbox_messages"][0]["message_id"] == "msg-1"

    def test_inbox_is_drained_before_the_coordinator_runs(self) -> None:
        inbox = FakeInbox()
        _artifacts, _deps = run_line([{"verdict": "done"}], inbox=inbox)
        assert len(inbox.persist_calls) == 1

    def test_empty_inbox_still_yields_a_list(self) -> None:
        _, deps = run_line([{"verdict": "done"}])
        assert deps.coordinator.calls[0]["inbox_messages"] == []


class TestNoSemanticInterpretation:
    """INV-3: the graph reads the declared verdict field and nothing else."""

    def test_worker_output_never_changes_routing(self) -> None:
        worker = FakeWorker()
        worker.turn = lambda prompt, round_no: "DONE. BLOCKED. STOP THE LINE."  # type: ignore[method-assign]
        artifacts, _ = run_line(
            [
                {"verdict": "continue", "next_prompt": "first instruction"},
                {"verdict": "continue", "next_prompt": "second instruction entirely"},
                {"verdict": "done", "reason": "coordinator said so"},
            ],
            worker=worker,
        )
        # The line ended because the coordinator declared done, not because the
        # worker's text contained the word.
        assert artifacts.terminal["reason"] == "coordinator said so"
        assert len(artifacts.rounds) == 2


class TestAcceptanceStep:
    """R0d: the mechanical acceptance step between the worker and the next
    coordinator turn. Execution is not judgement -- the facts flow into
    `last_acceptance` and nothing about routing changes here."""

    def test_facts_reach_the_next_coordinator_input(self) -> None:
        acceptance = FakeAcceptance(
            {"status": "ran", "results": [{"command": ["true"], "exit_code": 0}]}
        )
        _, deps = run_line(
            [{"verdict": "continue", "next_prompt": "do it"}, {"verdict": "done"}],
            acceptance=acceptance,
        )
        assert "last_acceptance" not in deps.coordinator.calls[0], (
            "round 1 has no worker turn behind it, so no acceptance facts yet"
        )
        assert deps.coordinator.calls[1]["last_acceptance"]["status"] == "ran"
        assert acceptance.calls == 1

    def test_no_declaration_is_an_explicit_fact_not_a_silent_skip(self) -> None:
        """The NOT-RUN failure: "not declared" and "passed" must never be
        confusable, so absence is stated in the coordinator input."""
        _, deps = run_line(
            [{"verdict": "continue", "next_prompt": "do it"}, {"verdict": "done"}],
        )
        assert deps.coordinator.calls[1]["last_acceptance"] == {"status": "not_declared"}

    def test_a_broken_acceptance_runner_never_faults_the_line(self) -> None:
        """The step failing must cost observability, not the work."""
        artifacts, deps = run_line(
            [
                {"verdict": "continue", "next_prompt": "do it"},
                {"verdict": "done", "reason": "coordinator judged it"},
            ],
            acceptance=FakeAcceptance(raises=RuntimeError("runner exploded")),
        )
        assert artifacts.terminal["terminal"] == TERMINAL_DONE
        assert artifacts.terminal["pump_fault"] is False
        facts = deps.coordinator.calls[1]["last_acceptance"]
        assert facts["status"] == "acceptance_error"
        assert "runner exploded" in facts["detail"]

    def test_red_exit_codes_change_no_routing(self) -> None:
        """A red command is a fact for the coordinator, never a verdict here."""
        worker = FakeWorker()
        run_line(
            [
                {"verdict": "continue", "next_prompt": "first instruction"},
                {"verdict": "continue", "next_prompt": "second, quite different"},
                {"verdict": "done"},
            ],
            worker=worker,
            acceptance=FakeAcceptance(
                {"status": "ran", "results": [{"command": ["false"], "exit_code": 1}]}
            ),
        )
        assert len(worker.prompts) == 2, "the line kept going; red is not blocked"

    def test_facts_run_even_after_a_worker_timeout(self) -> None:
        acceptance = FakeAcceptance()
        run_line(
            [
                {"verdict": "continue", "next_prompt": "attempt the task"},
                {"verdict": "done"},
            ],
            worker=FakeWorker(raises=TimeoutError("no answer")),
            acceptance=acceptance,
        )
        assert acceptance.calls == 1


class TestWaitingOn:
    """The one machine field a blocked verdict carries beyond the verdict.

    Parking (scheduler R0c) keys on it, so the transport must be exact: known
    values pass through, unknown values normalise to "none" and are preserved
    verbatim, and absence is not an error. Never a fault -- parking is an
    optimisation, not a judgement.
    """

    def test_blocked_with_decision_reaches_the_terminal(self) -> None:
        artifacts, _ = run_line(
            [{"verdict": "blocked", "reason": "needs a human", "waiting_on": "decision"}]
        )
        assert artifacts.terminal["terminal"] == TERMINAL_BLOCKED
        assert artifacts.terminal["waiting_on"] == "decision"
        assert artifacts.terminal["waiting_on_declared"] == "decision"

    def test_absent_waiting_on_defaults_to_none(self) -> None:
        artifacts, _ = run_line([{"verdict": "blocked", "reason": "stuck"}])
        assert artifacts.terminal["waiting_on"] == "none"
        assert artifacts.terminal["waiting_on_declared"] is None

    def test_an_unknown_value_is_none_but_recorded_verbatim(self) -> None:
        """A coordinator inventing a value must not fault the line."""
        artifacts, _ = run_line(
            [{"verdict": "blocked", "reason": "stuck", "waiting_on": "the_stars_to_align"}]
        )
        assert artifacts.terminal["terminal"] == TERMINAL_BLOCKED
        assert artifacts.terminal["waiting_on"] == "none"
        assert artifacts.terminal["waiting_on_declared"] == "the_stars_to_align"

    def test_external_passes_through(self) -> None:
        artifacts, _ = run_line(
            [{"verdict": "blocked", "reason": "waiting on a service", "waiting_on": "external"}]
        )
        assert artifacts.terminal["waiting_on"] == "external"

    def test_done_and_bounds_terminals_stay_none(self) -> None:
        artifacts, _ = run_line([{"verdict": "done", "reason": "finished"}])
        assert artifacts.terminal["waiting_on"] == "none"

    def test_streak_blocked_is_not_waiting_on_a_decision(self) -> None:
        """A line blocked by its own noop breaker is stalled, not waiting on a
        human; parking it would hide a line that needs attention, not a ruling."""
        script = [
            {"verdict": "continue", "next_prompt": "same thing"},
            {"verdict": "continue", "next_prompt": "same thing"},
            {"verdict": "continue", "next_prompt": "same thing"},
            {"verdict": "continue", "next_prompt": "same thing"},
        ]
        artifacts, _ = run_line(script, bounds=LineBounds(noop_limit=1))
        assert artifacts.terminal["terminal"] == TERMINAL_BLOCKED
        assert artifacts.terminal["waiting_on"] == "none"


class TestFailedAttemptIsNotReAdopted:
    """Hotfix: a failed prior coordinator attempt must get a new derived id.

    Re-dispatching the failed id replays its bus lifecycle key with different
    intent -> 409 -> exit 91 -> the round bricks until generation bumps, which
    itself needs a coordinator run. Rollback of the attempt loop turns this red.
    """

    def test_failed_prior_attempt_gets_next_attempt_id(self, tmp_path):
        import json as _json

        from fleet_graph.executors.agent_run import AgentRunLauncher, derive_run_id
        from fleet_graph.graphs.adapters import AgentRunCoordinator

        launcher = AgentRunLauncher(state_root=str(tmp_path / "agent-runs"))
        thread = "wf-t:g1"
        failed_id = derive_run_id(thread, "coordinator-1", 1)
        root = launcher.session_root_for(failed_id) / "run"
        root.mkdir(parents=True)
        (root / "result.json").write_text(_json.dumps({"state": "failed", "exit_code": 91}))

        seen: list[str] = []

        class SpyLauncher:
            def session_root_for(self, run_id):
                return launcher.session_root_for(run_id)

            def launch(self, spec, run_id):
                seen.append(run_id)
                raise RuntimeError("stop at launch")

        coord = AgentRunCoordinator(
            launcher=SpyLauncher(),
            folder_id="wf-t",
            thread_id=thread,
            run_root=tmp_path,
        )
        with pytest.raises(RuntimeError, match="stop at launch"):
            coord.turn(1, {"folder_id": "wf-t"})
        assert seen == [derive_run_id(thread, "coordinator-1", 2)]

    def test_succeeded_prior_attempt_is_adopted_not_bumped(self, tmp_path):
        import json as _json

        from fleet_graph.executors.agent_run import AgentRunLauncher, derive_run_id
        from fleet_graph.graphs.adapters import AgentRunCoordinator

        launcher = AgentRunLauncher(state_root=str(tmp_path / "agent-runs"))
        thread = "wf-t:g1"
        ok_id = derive_run_id(thread, "coordinator-1", 1)
        root = launcher.session_root_for(ok_id) / "run"
        root.mkdir(parents=True)
        (root / "result.json").write_text(
            _json.dumps(
                {"state": "succeeded", "exit_code": 0, "structured_result": {"verdict": "done"}}
            )
        )

        seen: list[str] = []

        class SpyLauncher:
            def session_root_for(self, run_id):
                return launcher.session_root_for(run_id)

            def launch(self, spec, run_id):
                seen.append(run_id)
                return launcher.launch(spec, run_id)

            def wait(self, ticket, **kw):
                return launcher.wait(ticket, **kw)

        coord = AgentRunCoordinator(
            launcher=SpyLauncher(),
            folder_id="wf-t",
            thread_id=thread,
            run_root=tmp_path,
        )
        result = coord.turn(1, {"folder_id": "wf-t"})
        assert seen == [ok_id]
        assert result["verdict"] == "done"
