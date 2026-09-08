"""DD01 composition wiring: the kernel is the line's goal-facing coordinator.

Where ``tests/test_goal_request_kernel.py`` pins the kernel's own behavior in
isolation, this module pins the *composition seam*: ``build_line`` must hand the
goal-facing responsibility to ``KernelCoordinator`` (a serial request-to-Goal-call
kernel), not to the retired round-prompt agent-run coordinator, and a
``KernelCoordinator`` must translate one request -> one Goal ReAct call -> one
validated Stop List back into the graph's verdict surface without ever
fabricating a Goal answer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fleet_graph.goal.request_kernel import (
    ACTION_DISPATCH,
    DELIVERED,
    GoalRequestKernel,
    Journal,
)
from fleet_graph.graphs.kernel_coordinator import (
    GOAL_CALL_UNWIRED,
    KernelCoordinator,
    KernelEffectPorts,
)

GOAL = "wf-1"


def make_coordinator(goal_call=None) -> KernelCoordinator:
    kernel = GoalRequestKernel(journal=Journal(), effects=KernelEffectPorts())
    kernel.activate_version(GOAL, "v1")
    return KernelCoordinator(
        kernel=kernel,
        goal_call=goal_call,
        folder_id=GOAL,
        thread_id=f"{GOAL}:g1",
        launch_id="launch-test",
    )


class ScriptedGoalCall:
    """A fake Goal ReAct call: returns a fixed Stop List and records the prompt."""

    def __init__(self, stop_list: dict[str, Any]) -> None:
        self.stop_list = stop_list
        self.prompts: list[dict[str, Any]] = []

    def call(self, goal: str, prompt: dict[str, Any]) -> dict[str, Any]:
        self.prompts.append(prompt)
        return self.stop_list


class RecordingEffects(KernelEffectPorts):
    def __init__(self) -> None:
        super().__init__()
        self.dispatches: list[dict[str, Any]] = []

    def dispatch(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
        self.dispatches.append({"payload": payload, "ctx": dict(ctx)})
        return {"ok": True, "status": DELIVERED, "detail": "ok"}


def test_build_line_composes_the_kernel_as_the_coordinator(tmp_path: Path) -> None:
    from fleet_graph.graphs.runner import LineConfig, build_line

    _graph, deps = build_line(LineConfig(folder_id=GOAL, seat="s", run_root=tmp_path))
    assert isinstance(deps.coordinator, KernelCoordinator)
    kernel = deps.coordinator.kernel
    assert isinstance(kernel, GoalRequestKernel)
    # The durable journal is a real file store under the run root, not a toy.
    assert kernel.journal.home == tmp_path / "goal-kernel"
    # launch identity is preserved for line label attribution.
    assert deps.coordinator.thread_id == f"{GOAL}:g1"


def test_one_request_produces_one_goal_call_and_a_done_terminal() -> None:
    effects = RecordingEffects()
    coordinator = make_coordinator(goal_call=ScriptedGoalCall(
        {"actions": [{"kind": ACTION_DISPATCH, "idempotency_key": "k1",
                      "payload": {"repo_path": "r", "dispatched_by": "wf-1", "spec_text": "s"}}],
         "intent": "done"}
    ))
    coordinator.kernel.effects = effects

    verdict = coordinator.turn(
        1,
        {"folder_id": GOAL, "inbox_messages": [], "last_turn_output": ""},
    )

    assert verdict["verdict"] == "done"
    assert len(effects.dispatches) == 1
    assert effects.dispatches[0]["payload"]["repo_path"] == "r"


def test_blocked_intent_parks_the_line() -> None:
    coordinator = make_coordinator(goal_call=ScriptedGoalCall({"actions": [], "intent": "blocked"}))
    verdict = coordinator.turn(1, {"folder_id": GOAL, "inbox_messages": []})
    assert verdict["verdict"] == "blocked"
    assert verdict["waiting_on"] == "external"


def test_unbound_goal_call_reports_not_ready_instead_of_a_simulated_answer() -> None:
    coordinator = make_coordinator(goal_call=None)
    verdict = coordinator.turn(1, {"folder_id": GOAL, "inbox_messages": []})
    assert verdict["verdict"] == "blocked"
    assert verdict["reason"] == GOAL_CALL_UNWIRED


def test_replay_of_a_confirmed_dispatch_does_not_re_run_the_effect() -> None:
    effects = RecordingEffects()
    coordinator = make_coordinator(goal_call=ScriptedGoalCall(
        {"actions": [{"kind": ACTION_DISPATCH, "idempotency_key": "K",
                      "payload": {"repo_path": "r", "dispatched_by": "wf-1", "spec_text": "s"}}],
         "intent": "done"}
    ))
    coordinator.kernel.effects = effects

    coordinator.turn(1, {"folder_id": GOAL, "inbox_messages": []})
    assert len(effects.dispatches) == 1
    # A later turn re-declaring the same idempotency key must not re-dispatch.
    coordinator.turn(2, {"folder_id": GOAL, "inbox_messages": []})
    assert len(effects.dispatches) == 1


def test_unbound_effect_ports_fail_closed() -> None:
    effects = KernelEffectPorts()
    assert effects.dispatch({"repo_path": "r"}, ctx={})["status"] == "not_ready"
    assert effects.reply({"text": "hi"}, ctx={})["status"] == "not_ready"
    assert effects.add_repo({"repo_path": "r"}, ctx={})["status"] == "not_ready"
    assert effects.approve({"development_id": "d"}, ctx={})["status"] == "not_ready"
    # observe answers "unknown" for effects the service cannot reconcile.
    assert effects.observe("reply", "k") == "unknown"