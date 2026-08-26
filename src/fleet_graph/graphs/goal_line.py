"""The ronin line: one round of a goal-driven work line, as a graph.

This is the pump's loop, expressed as explicit control flow instead of a while
statement. The shape is deliberately the same, because the pump's shape was
never the problem -- what was wrong was that it lived in a bare script nobody
could test, alongside a second bare script that decided when to run it.

One round:

    bounds -> drain inbox -> coordinator turn -> verdict
                                                  |- done/blocked -> terminal
                                                  `- continue -> guards -> worker turn -> loop

What this module refuses to do is as important as what it does. It never reads
meaning out of the coordinator's answer beyond the declared verdict field, it
never runs an acceptance check, and it never writes to a work folder (INV-3).
It reaches agents only through agent-run and agent-session, never by spawning a
harness itself (INV-4/B8). Both rules exist because the orchestrator becoming a
second, unaccountable coordinator is the failure mode that killed the previous
design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from fleet_graph.graphs.guards import LineGuards, PromptVerdict

COORDINATOR_ROLE = "goal_coordinator"
DISPATCHER_LABEL = "fleet-graph"

# Terminal states, matching the pump's vocabulary so terminal.json stays
# readable by everything that already consumes it.
TERMINAL_DONE = "done"
TERMINAL_BLOCKED = "blocked"
TERMINAL_BOUNDS = "bounds"
TERMINAL_FAULT = "fault"


class Verdict(TypedDict, total=False):
    verdict: str
    next_prompt: str
    reason: str
    no_progress: bool


class LineState(TypedDict, total=False):
    round_no: int
    last_turn_output: str
    last_turn_status: dict[str, Any]
    terminal: str
    terminal_reason: str
    pump_fault: bool
    rounds_recorded: int
    # Set only between the coordinator accepting a prompt and the worker
    # consuming it; never persisted anywhere durable.
    pending_prompt: str
    pending_sha: str


class Coordinator(Protocol):
    """Runs one coordinator turn and returns its declared result."""

    def turn(self, round_no: int, coord_input: dict[str, Any]) -> Verdict: ...


class Worker(Protocol):
    """Injects a prompt into the long-lived worker seat and returns its text."""

    def turn(self, prompt: str, round_no: int) -> str: ...


class InboxPort(Protocol):
    def drain_then_ack(self, persist: Any) -> tuple[Any, list[str]]: ...


class ArtifactsPort(Protocol):
    def heartbeat(self, round_no: int, phase: str, *, force: bool = False) -> bool: ...
    def append_round(self, line: dict[str, Any]) -> bool: ...
    def write_terminal(
        self, *, terminal: str, rounds: int, reason: str | None = ..., pump_fault: bool = ...
    ) -> Any: ...


@dataclass
class LineDeps:
    """Everything the graph talks to. Injected so the wiring is testable."""

    coordinator: Coordinator
    worker: Worker
    inbox: InboxPort
    artifacts: ArtifactsPort
    guards: LineGuards = field(default_factory=LineGuards)
    folder_id: str = ""
    persist_coord_input: Any = None
    clock: Any = None

    def now(self) -> float | None:
        return self.clock() if self.clock is not None else None


def build_goal_line_graph(deps: LineDeps) -> StateGraph:
    def check_bounds(state: LineState) -> LineState:
        """INV-8. Pure counting, no judgement."""
        round_no = state.get("round_no", 1)
        deps.artifacts.heartbeat(round_no, "coordinator")

        reason = deps.guards.bounds_exceeded(round_no, deps.now())
        if reason:
            return {"terminal": TERMINAL_BOUNDS, "terminal_reason": reason}

        streak = deps.guards.streak_exceeded()
        if streak:
            return {"terminal": TERMINAL_BLOCKED, "terminal_reason": streak}
        return {}

    def coordinator_turn(state: LineState) -> LineState:
        round_no = state.get("round_no", 1)
        deps.artifacts.heartbeat(round_no, "coordinator")

        coord_input: dict[str, Any] = {
            "folder_id": deps.folder_id,
            "round": round_no,
            "last_turn_output": state.get("last_turn_output", ""),
            "bounds_remaining": {
                "rounds_left": deps.guards.bounds.max_rounds - round_no + 1,
                "deadline_at": deps.guards.bounds.deadline_at,
            },
            "inbox_messages": [],
        }
        if state.get("last_turn_status"):
            coord_input["last_turn_status"] = state["last_turn_status"]

        # Must-deliver ordering: the messages land in the durable coordinator
        # input before anything is acked. See bus/inbox.py.
        def persist(messages: list[dict[str, Any]]) -> None:
            coord_input["inbox_messages"] = messages
            if deps.persist_coord_input is not None:
                deps.persist_coord_input(round_no, coord_input)

        deps.inbox.drain_then_ack(persist)

        result = deps.coordinator.turn(round_no, coord_input)
        verdict = str(result.get("verdict", "")).strip().lower()

        if verdict == TERMINAL_DONE:
            return {
                "terminal": TERMINAL_DONE,
                "terminal_reason": str(result.get("reason", "")),
            }
        if verdict == TERMINAL_BLOCKED:
            return {
                "terminal": TERMINAL_BLOCKED,
                "terminal_reason": str(result.get("reason", "")),
            }
        if verdict != "continue":
            # An unrecognised verdict is a coordinator fault, not something to
            # interpret. Guessing here would be exactly the INV-3 violation
            # this layer exists to avoid.
            return {
                "terminal": TERMINAL_FAULT,
                "terminal_reason": f"unrecognised verdict {verdict!r}",
                "pump_fault": True,
            }

        prompt = str(result.get("next_prompt", ""))
        if not prompt.strip():
            return {
                "terminal": TERMINAL_FAULT,
                "terminal_reason": "coordinator returned continue with an empty next_prompt",
                "pump_fault": True,
            }

        check = deps.guards.check_prompt(prompt, round_no)
        if check.verdict is not PromptVerdict.FRESH:
            # Refuse to inject, count it as a no-op round, and let the streak
            # limit decide whether the line is going anywhere.
            deps.guards.record_noop()
            deps.artifacts.append_round(
                {
                    "round": round_no,
                    "verdict": "continue",
                    "reason": check.verdict.value,
                    "prompt_sha256": check.sha256,
                    "similarity": check.similarity,
                    "injected": False,
                }
            )
            return {
                "round_no": round_no + 1,
                "rounds_recorded": state.get("rounds_recorded", 0) + 1,
            }

        if bool(result.get("no_progress")):
            deps.guards.record_noop()
        else:
            deps.guards.record_progress()

        deps.guards.accept_prompt(check, prompt, round_no)
        return {"pending_prompt": prompt, "pending_sha": check.sha256}

    def worker_turn(state: LineState) -> LineState:
        round_no = state.get("round_no", 1)
        deps.artifacts.heartbeat(round_no, "worker")

        prompt = state.get("pending_prompt", "")
        try:
            output = deps.worker.turn(prompt, round_no)
        except TimeoutError as exc:
            deps.guards.record_timeout()
            deps.artifacts.append_round(
                {
                    "round": round_no,
                    "verdict": "continue",
                    "reason": "worker_turn_timeout",
                    "prompt_sha256": state.get("pending_sha", ""),
                    "injected": True,
                }
            )
            return {
                "round_no": round_no + 1,
                "rounds_recorded": state.get("rounds_recorded", 0) + 1,
                "last_turn_status": {"kind": "turn_timeout", "detail": str(exc)},
                "last_turn_output": "",
            }

        deps.guards.record_turn_ok()
        deps.artifacts.append_round(
            {
                "round": round_no,
                "verdict": "continue",
                "reason": "",
                "prompt_sha256": state.get("pending_sha", ""),
                "injected": True,
            }
        )
        return {
            "round_no": round_no + 1,
            "rounds_recorded": state.get("rounds_recorded", 0) + 1,
            "last_turn_output": output,
            "last_turn_status": {},
        }

    def finalise(state: LineState) -> LineState:
        """Terminal record lands locally before anything is published."""
        deps.artifacts.write_terminal(
            terminal=state.get("terminal", TERMINAL_FAULT),
            rounds=state.get("rounds_recorded", 0),
            reason=state.get("terminal_reason") or None,
            pump_fault=bool(state.get("pump_fault", False)),
        )
        return {}

    def after_bounds(state: LineState) -> str:
        return "finalise" if state.get("terminal") else "coordinator_turn"

    def after_coordinator(state: LineState) -> str:
        if state.get("terminal"):
            return "finalise"
        if state.get("pending_prompt"):
            return "worker_turn"
        # Prompt was refused; go round again without touching the worker.
        return "check_bounds"

    graph: StateGraph = StateGraph(LineState)
    graph.add_node("check_bounds", check_bounds)
    graph.add_node("coordinator_turn", coordinator_turn)
    graph.add_node("worker_turn", worker_turn)
    graph.add_node("finalise", finalise)

    graph.add_edge(START, "check_bounds")
    graph.add_conditional_edges("check_bounds", after_bounds)
    graph.add_conditional_edges("coordinator_turn", after_coordinator)
    graph.add_edge("worker_turn", "check_bounds")
    graph.add_edge("finalise", END)
    return graph


__all__ = [
    "COORDINATOR_ROLE",
    "TERMINAL_BLOCKED",
    "TERMINAL_BOUNDS",
    "TERMINAL_DONE",
    "TERMINAL_FAULT",
    "LineDeps",
    "LineState",
    "build_goal_line_graph",
]
