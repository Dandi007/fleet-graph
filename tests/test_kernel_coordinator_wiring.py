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
    ACTION_REPLY,
    DELIVERED,
    KIND_MESSAGE,
    NOT_READY,
    RECORD_REQUEST,
    UNKNOWN,
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
    coordinator = make_coordinator(
        goal_call=ScriptedGoalCall(
            {
                "actions": [
                    {
                        "kind": ACTION_DISPATCH,
                        "idempotency_key": "k1",
                        "payload": {"repo_path": "r", "dispatched_by": "wf-1", "spec_text": "s"},
                    }
                ],
                "intent": "done",
            }
        )
    )
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
    coordinator = make_coordinator(
        goal_call=ScriptedGoalCall(
            {
                "actions": [
                    {
                        "kind": ACTION_DISPATCH,
                        "idempotency_key": "K",
                        "payload": {"repo_path": "r", "dispatched_by": "wf-1", "spec_text": "s"},
                    }
                ],
                "intent": "done",
            }
        )
    )
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


class SequenceGoalCall:
    """A fake Goal call that serves one Stop List per call, oldest first."""

    def __init__(self, stop_lists: list[dict[str, Any]]) -> None:
        self._stop_lists = list(stop_lists)
        self.prompts: list[dict[str, Any]] = []

    def call(self, goal: str, prompt: dict[str, Any]) -> dict[str, Any]:
        self.prompts.append(prompt)
        if not self._stop_lists:
            return {"actions": []}
        return self._stop_lists.pop(0)


def test_each_inbox_message_is_an_independent_request() -> None:
    call = SequenceGoalCall([{"actions": [], "intent": "done"}, {"actions": [], "intent": "done"}])
    coordinator = make_coordinator(goal_call=call)
    inbox = [
        {"message_id": "m-A", "from_agent_id": "line-a", "body": "msg A"},
        {"message_id": "m-B", "from_agent_id": "line-b", "body": "msg B"},
    ]
    coordinator.turn(1, {"folder_id": GOAL, "inbox_messages": inbox})
    assert len(call.prompts) == 2  # one Goal call per message, never one combined
    assert call.prompts[0]["kind"] == KIND_MESSAGE
    assert call.prompts[0]["caller"] == "line-a"
    assert call.prompts[1]["kind"] == KIND_MESSAGE
    assert call.prompts[1]["caller"] == "line-b"


def test_message_request_carries_reply_association(tmp_path: Path) -> None:
    # Behavior 1 "reply association": a message request is durable with its
    # sender as the reply target, and that pointer reaches the Goal prompt so a
    # reply action can be addressed to the original caller (behavior 3).
    call = SequenceGoalCall([{"actions": [], "intent": "done"}])
    kernel = GoalRequestKernel(journal=Journal(home=tmp_path / "journal"))
    kernel.activate_version(GOAL, "v1")
    coordinator = KernelCoordinator(
        kernel=kernel,
        goal_call=call,
        folder_id=GOAL,
        thread_id=f"{GOAL}:g1",
        launch_id="launch-test",
    )
    inbox = [{"message_id": "m-A", "from_agent_id": "line-a", "body": "msg A"}]
    coordinator.turn(1, {"folder_id": GOAL, "inbox_messages": inbox})

    requests = [r for r in kernel.journal.scan(GOAL) if r.get("record") == RECORD_REQUEST]
    assert len(requests) == 1
    assert requests[0]["reply_to"] == "line-a"
    assert call.prompts[0]["reply_to"] == "line-a"


def test_waiting_result_does_not_suppress_queued_requests() -> None:
    call = SequenceGoalCall(
        [{"actions": [], "intent": "waiting"}, {"actions": [], "intent": "done"}]
    )
    coordinator = make_coordinator(goal_call=call)
    inbox = [
        {"message_id": "m-A", "from_agent_id": "line-a", "body": "msg A"},
        {"message_id": "m-B", "from_agent_id": "line-b", "body": "msg B"},
    ]
    verdict = coordinator.turn(1, {"folder_id": GOAL, "inbox_messages": inbox})
    # A returned waiting, but the queued B behind it was still serviced.
    assert len(call.prompts) == 2
    assert verdict["verdict"] == "done"


class UnresolvedReplyEffects(KernelEffectPorts):
    """An effect port whose reply returns UNKNOWN: the delivery is outstanding
    and must keep a later ``done`` pending (P4)."""

    def __init__(self) -> None:
        super().__init__()
        self.replies: list[dict[str, Any]] = []

    def reply(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
        self.replies.append({"payload": payload, "ctx": dict(ctx)})
        return {"ok": False, "status": UNKNOWN, "detail": "outcome unknown"}


def test_done_stays_pending_while_a_prior_reply_is_unresolved() -> None:
    # Two queued messages: A requests a reply that returns UNKNOWN, B returns an
    # empty done list. The drained turn must NOT complete done while A's reply
    # delivery is still outstanding (P4).
    call = SequenceGoalCall(
        [
            {
                "actions": [
                    {
                        "kind": ACTION_REPLY,
                        "idempotency_key": "R",
                        "payload": {"text": "hi", "to": "line-a"},
                    }
                ]
            },
            {"actions": [], "intent": "done"},
        ]
    )
    effects = UnresolvedReplyEffects()
    coordinator = make_coordinator(goal_call=call)
    coordinator.kernel.effects = effects
    inbox = [
        {"message_id": "m-A", "from_agent_id": "line-a", "body": "msg A"},
        {"message_id": "m-B", "from_agent_id": "line-b", "body": "msg B"},
    ]

    verdict = coordinator.turn(1, {"folder_id": GOAL, "inbox_messages": inbox})

    assert len(call.prompts) == 2  # both messages were serviced, not suppressed
    assert verdict["verdict"] == "blocked"  # done was not declared
    assert "outstanding" in verdict["reason"]
    assert len(effects.replies) == 1


class NotReadyReplyEffects(KernelEffectPorts):
    """An effect port whose reply returns NOT_READY: the required delivery is
    neither fulfilled nor explicitly disposed, so it must keep a later
    ``done`` pending even though the attempt itself is final (P4)."""

    def __init__(self) -> None:
        super().__init__()
        self.replies: list[dict[str, Any]] = []

    def reply(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
        self.replies.append({"payload": payload, "ctx": dict(ctx)})
        return {"ok": False, "status": NOT_READY, "detail": "no reply sink bound"}


def test_done_stays_pending_while_a_prior_reply_returned_not_ready(tmp_path: Path) -> None:
    # Two queued messages: A requests a reply that returns NOT_READY from an
    # unbound reply port, B returns an empty done list. A's reply was never
    # delivered and was never explicitly retired, so the drained turn must NOT
    # complete done, and that obligation must survive reconstruction (P4).
    call = SequenceGoalCall(
        [
            {
                "actions": [
                    {
                        "kind": ACTION_REPLY,
                        "idempotency_key": "R",
                        "payload": {"text": "hi", "to": "line-a"},
                    }
                ]
            },
            {"actions": [], "intent": "done"},
        ]
    )
    home = tmp_path / "journal"
    kernel = GoalRequestKernel(journal=Journal(home=home), effects=NotReadyReplyEffects())
    kernel.activate_version(GOAL, "v1")
    coordinator = KernelCoordinator(
        kernel=kernel,
        goal_call=call,
        folder_id=GOAL,
        thread_id=f"{GOAL}:g1",
        launch_id="launch-test",
    )
    inbox = [
        {"message_id": "m-A", "from_agent_id": "line-a", "body": "msg A"},
        {"message_id": "m-B", "from_agent_id": "line-b", "body": "msg B"},
    ]

    verdict = coordinator.turn(1, {"folder_id": GOAL, "inbox_messages": inbox})

    assert len(call.prompts) == 2
    assert verdict["verdict"] == "blocked"
    assert "outstanding" in verdict["reason"]

    # Reconstruction: a rebuilt product still holds the NOT_READY reply open as
    # an outstanding obligation, so done cannot be forgotten across a restart.
    rebuilt = GoalRequestKernel(journal=Journal(home=home), effects=KernelEffectPorts())
    assert [o["idempotency_key"] for o in rebuilt.outstanding_deliveries(GOAL)] == ["R"]


def test_done_intent_with_an_unfulfilled_action_stays_pending() -> None:
    # default KernelEffectPorts is unbound, so the dispatch fails closed.
    coordinator = make_coordinator(
        goal_call=ScriptedGoalCall(
            {
                "actions": [
                    {
                        "kind": ACTION_DISPATCH,
                        "idempotency_key": "k1",
                        "payload": {"repo_path": "r", "dispatched_by": "wf-1", "spec_text": "s"},
                    }
                ],
                "intent": "done",
            }
        )
    )
    verdict = coordinator.turn(1, {"folder_id": GOAL, "inbox_messages": []})
    assert verdict["verdict"] == "blocked"
    assert "incomplete" in verdict["reason"]
