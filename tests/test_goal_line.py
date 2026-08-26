"""The ronin line graph: routing, breakers, and what it refuses to do."""

from __future__ import annotations

from typing import Any

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
        self, *, terminal: str, rounds: int, reason: str | None = None, pump_fault: bool = False
    ) -> str:
        self.terminal = {
            "terminal": terminal,
            "rounds": rounds,
            "reason": reason,
            "pump_fault": pump_fault,
        }
        return "terminal.json"


def run_line(
    script: list[dict[str, Any]],
    *,
    bounds: LineBounds | None = None,
    worker: FakeWorker | None = None,
    inbox: FakeInbox | None = None,
) -> tuple[FakeArtifacts, LineDeps]:
    artifacts = FakeArtifacts()
    deps = LineDeps(
        coordinator=FakeCoordinator(script),
        worker=worker or FakeWorker(),
        inbox=inbox or FakeInbox(),
        artifacts=artifacts,
        guards=LineGuards(bounds=bounds or LineBounds()),
        folder_id="wf-3f30cd",
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

    def test_heartbeat_marks_both_phases(self) -> None:
        artifacts, _ = run_line(
            [{"verdict": "continue", "next_prompt": "first"}, {"verdict": "done"}]
        )
        phases = {phase for _, phase in artifacts.beats}
        assert phases == {"coordinator", "worker"}


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
