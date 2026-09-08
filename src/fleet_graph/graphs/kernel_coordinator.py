"""The kernel-backed coordinator: the DD01 composition seam.

``build_line`` used to hand the goal-facing responsibility to
``AgentRunCoordinator`` + ``AgentSessionWorker`` -- the coordinator/worker
*round-prompt progression*, where every round asked the goal model for a
``verdict + next_prompt`` and looped again. DD01 replaces that progression with
the durable, serial request-to-Goal-call kernel: one accepted request produces
one Goal ReAct call and one validated Stop action List, and the kernel -- not
the round-prompt loop -- carries the goal-facing responsibility.

This module is the adapter that sits at ``graphs.runner.build_line`` and turns
:class:`fleet_graph.goal.request_kernel.GoalRequestKernel` into the
``goal_line.Coordinator`` the rest of the graph already talks to. It keeps the
graph's declared result surface (``verdict`` / ``reason`` / ``waiting_on``) so
the downstream verdict routing stays intact, while the *production* of that
result changes from "round prompt" to "serial request -> Goal call -> Stop List".

The Goal's ReAct call is an injected port (``GoalCallPort``). DD01 does not
claim live runtime compatibility: when no port is bound the coordinator answers
``blocked`` with the explicit ``goal_call_unwired`` reason -- never a simulated
Goal answer -- exactly as the spec's dependency boundary requires.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from fleet_graph.goal.request_kernel import (
    DELIVERED,
    FAILED,
    KIND_DD_RESULT,
    KIND_DD_REVIEW,
    KIND_ENROLL,
    KIND_MESSAGE,
    KIND_STEER,
    NOT_READY,
    OBSERVED_ABSENT,
    RECORD_REQUEST,
    GoalRequestKernel,
    Request,
    build_goal_prompt,
)

#: The reason a kernel-coordinated line parks with when no ReAct port is bound.
GOAL_CALL_UNWIRED = "goal_call_unwired"


class GoalCallPort(Protocol):
    """The Goal's single ReAct call: one request prompt -> one raw Stop List.

    ``prompt`` is the kernel's one-current-request prompt (``build_goal_prompt``).
    The returned dict is a raw Stop List: ``actions[]`` of
    ``{kind, payload, idempotency_key}`` plus an optional ``intent``
    (``waiting`` / ``blocked`` / ``done``). Live runtime binding is injected at
    ``build_line``; until then the coordinator reports ``goal_call_unwired``
    rather than inventing a Goal answer.
    """

    def call(self, goal: str, prompt: dict[str, Any]) -> dict[str, Any]: ...


def _intent_verdict(intent: str | None) -> tuple[str, str, str]:
    """Map a validated Stop List terminal intent -> (verdict, waiting_on, reason).

    ``done`` is an intent, never a terminal shortcut (P4): a Goal that declares
    ``done`` has finished *this request's* Stop List, so the line is done. The
    other two intents park the line; a new request wakes a ``blocked`` /
    ``waiting`` line (kernel behavior 5), which is exactly the scheduler's
    ``blocked + waiting_on`` wake contract.
    """
    if intent == "done":
        return "done", "", "goal request completed"
    if intent == "blocked":
        return "blocked", "external", "goal blocked"
    if intent == "waiting":
        return "blocked", "none", "goal waiting on external input"
    # Actions were routed but no terminal intent was declared: park and wait for
    # the next request rather than guessing a terminal.
    return "blocked", "none", "goal call produced actions without a terminal intent"


@dataclass
class KernelCoordinator:
    """The serial request-to-Goal-call coordinator (the DD01 composition seam).

    Implements ``goal_line.Coordinator``. ``kernel`` owns the durable journal,
    the per-goal ownership fence and the Stop List validation + effect routing;
    ``goal_call`` is the injected Goal ReAct call. ``thread_id`` / ``launch_id``
    are carried for launch-identity attribution so the line's labels stay
    stable across a process restart (the same contract ``runner.py`` previously
    satisfied through ``AgentRunCoordinator``).
    """

    kernel: GoalRequestKernel
    goal_call: GoalCallPort | None
    folder_id: str
    thread_id: str = ""
    launch_id: str = ""

    def turn(
        self, round_no: int, coord_input: dict[str, Any], *, resume: bool = False
    ) -> dict[str, Any]:
        """One coordinator turn = ingress a request, then drain Goal calls serially.

        The round's facts become a single durable request (persisted before we
        proceed); the kernel then services queued requests one Goal call at a
        time, in submission order, until the queue is empty or the Goal parks
        the line. The final service's terminal intent is the round's verdict.
        """
        request = self._request_for(round_no, coord_input)
        self.kernel.submit(self.folder_id, request)

        last: dict[str, Any] | None = None
        while True:
            call = self.kernel.next_goal_call(self.folder_id)
            if call is None:
                break
            last = self._run_call(call)
            # A terminal intent that is not ``done`` parks the line: remaining
            # queued requests are preserved (behavior 2/5), never discarded and
            # never silently combined into one call.
            if last["verdict"] != "done":
                break

        if last is None:
            return _parked_verdict(self.kernel, self.folder_id)
        return last

    # -- request ingress -----------------------------------------------------

    def _request_for(self, round_no: int, coord_input: dict[str, Any]) -> Request:
        """Derive the round's serial request from the graph's coordinator input.

        One line round == one request. The request identity is stable per round
        (``line:<folder>:round:<round>``) so a restart that re-enters the same
        round reuses the persisted request instead of re-appending (P5), while
        a new round is a genuinely new request. The kind is inferred from what
        woke the line; the current input is the coordinator envelope verbatim --
        one current request, never a re-appended history.
        """
        folder_id = self.folder_id
        inbox = coord_input.get("inbox_messages") or []
        decision = coord_input.get("decision")
        last_turn_report = coord_input.get("last_turn_report")
        gate_wake = coord_input.get("dd_awaiting_gate_development_id")

        if round_no == 1 and not inbox and decision is None:
            kind = KIND_ENROLL
            caller = "goal-enroll"
        elif decision is not None:
            kind = KIND_STEER
            decided_by = (decision or {}).get("decided_by") if isinstance(decision, dict) else None
            caller = str(decided_by or "goal")
        elif inbox:
            kind = KIND_MESSAGE
            first = inbox[0] if isinstance(inbox, list) and inbox else {}
            caller = str(first.get("sent_by") or "line") if isinstance(first, dict) else "line"
        elif last_turn_report is not None or bool(gate_wake):
            kind = KIND_DD_REVIEW if last_turn_report is not None else KIND_DD_RESULT
            caller = "goal-enroll"
        else:
            kind = KIND_MESSAGE
            caller = "line"

        return Request(
            request_id=f"line:{folder_id}:round:{round_no}",
            goal=folder_id,
            caller=caller,
            kind=kind,
            input=dict(coord_input),
            goal_version=self.kernel.active_version(folder_id),
        )

    # -- the serial Goal call -------------------------------------------------

    def _run_call(self, call: dict[str, Any]) -> dict[str, Any]:
        record = call.get("request") if isinstance(call.get("request"), dict) else {}
        request = Request(
            request_id=str(record.get("request_id") or call.get("request_id") or ""),
            goal=self.folder_id,
            caller=str(record.get("caller") or ""),
            kind=str(record.get("kind") or KIND_MESSAGE),
            input=record.get("input") if isinstance(record.get("input"), dict) else {},
            goal_version=str(record.get("goal_version") or ""),
            reply_to=record.get("reply_to"),
        )
        prompt = build_goal_prompt(request, history=self._history(request.request_id))
        if self.goal_call is None:
            # Release the fence and park: the Goal ReAct call is not bound, and
            # fabricating an answer would be exactly the simulated success the
            # spec forbids.
            self.kernel.finish_goal_call(self.folder_id, call["call_id"], {"actions": []})
            return {
                "verdict": "blocked",
                "waiting_on": "external",
                "reason": GOAL_CALL_UNWIRED,
            }
        stop_list = self.goal_call.call(self.folder_id, prompt)
        result = self.kernel.finish_goal_call(self.folder_id, call["call_id"], stop_list)
        return _to_verdict(result)

    def _history(self, current_request_id: str) -> list[Request]:
        """Prior requests for the goal, as pointers (never re-appended text)."""
        history: list[Request] = []
        for line in self.kernel.list_events(self.folder_id)["events"]:
            if line.get("record") != RECORD_REQUEST:
                continue
            if line.get("request_id") == current_request_id:
                continue
            history.append(
                Request(
                    request_id=str(line.get("request_id") or ""),
                    goal=self.folder_id,
                    caller=str(line.get("caller") or ""),
                    kind=str(line.get("kind") or KIND_MESSAGE),
                    input={},
                    goal_version=str(line.get("goal_version") or ""),
                )
            )
        return history


def _parked_verdict(kernel: GoalRequestKernel, goal: str) -> dict[str, Any]:
    """The verdict for a round that serviced nothing (fenced / stopped / empty)."""
    return {
        "verdict": "blocked",
        "waiting_on": "none",
        "reason": "no goal request serviced; serial kernel idle",
    }


def _to_verdict(result: dict[str, Any]) -> dict[str, Any]:
    """Map a ``finish_goal_call`` result back to the graph's verdict surface."""
    intent = result.get("intent")
    results = result.get("results") or []
    failed = [r for r in results if r.get("status") not in (DELIVERED,)]
    verdict, waiting_on, reason = _intent_verdict(intent)
    if verdict == "done":
        tail = f"; {len(failed)} failed" if failed else ""
        reason = f"goal call {result.get('call_id')} routed {len(results)} action(s){tail}"
    out: dict[str, Any] = {"verdict": verdict, "reason": reason}
    if waiting_on:
        out["waiting_on"] = waiting_on
    return out


# ---------------------------------------------------------------------------
# Effect-port binding (kernel -> existing product ports)
# ---------------------------------------------------------------------------


@dataclass
class KernelEffectPorts:
    """Adapts the product's dd-subgraph / gate-node ports to the kernel's
    ``EffectPorts``.

    ``dispatch`` -> the dd subgraph (the graph-edge development_create, R2);
    ``approve`` / ``reject`` -> the gate node (the sole awaiting_gate release
    path, R3/S11); ``reply`` and ``add_repo`` are not wired in this slice and
    fail closed with ``not_ready`` (an explicit fact, never a simulated
    success). ``observe`` answers the kernel's crash-reconciliation query: the
    dd admission is itself idempotent, so ``absent`` (safe to re-send) is honest
    for dispatch; reply/add_repo are unwired and answer ``unknown`` so a lost
    result stays recoverable and requires Goal judgement.
    """

    folder_id: str = ""
    dd: Any = None
    gate: Any = None

    def dispatch(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
        if self.dd is None:
            return self._not_ready("no dd dispatch port bound to this line")
        try:
            answer = self.dd.invoke({"line_folder": self.folder_id, "intent": payload})
        except Exception as exc:  # a gateway fault is a fact, never a crash
            return self._failed(f"{type(exc).__name__}: {exc}")
        result = answer.get("dd_result") if isinstance(answer, dict) else None
        if isinstance(result, dict) and result.get("development_id"):
            return {
                "ok": True,
                "status": DELIVERED,
                "detail": f"development {result.get('development_id')}",
                "development_id": result.get("development_id"),
            }
        return self._failed("the dispatch subgraph returned no development")

    def approve(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
        return self._gate(payload, "APPROVE")

    def reject(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
        return self._gate(payload, "REJECT")

    def add_repo(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
        return self._not_ready("add_repo is a follow-up slice; no repo-admission port is bound")

    def reply(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
        return self._not_ready("no reply sink is bound to this line")

    def observe(self, effect: str, key: str) -> str:
        if effect == "dispatch":
            return OBSERVED_ABSENT  # dd admission is idempotent on (repo, spec, base)
        return "unknown"

    # -- helpers -------------------------------------------------------------

    def _gate(self, payload: dict[str, Any], verdict: str) -> dict[str, Any]:
        if self.gate is None:
            return self._not_ready("no gate node bound to this line")
        action = {
            "kind": "dd.gate_release.v1",
            "idempotency_key": str(payload.get("idempotency_key") or ""),
            "payload": {
                **payload,
                "verdict": verdict,
                "decided_by": str(payload.get("decided_by") or self.folder_id),
            },
        }
        try:
            receipt = self.gate.consume(action, folder_id=self.folder_id, round_no=0)
        except Exception as exc:
            return self._failed(f"{type(exc).__name__}: {exc}")
        status = str((receipt or {}).get("status") or "")
        if status == "consumed":
            return {"ok": True, "status": DELIVERED, "detail": str(receipt.get("detail") or "")}
        return self._failed(str(receipt.get("detail") or receipt.get("code") or "gate refused"))

    @staticmethod
    def _not_ready(detail: str) -> dict[str, Any]:
        return {"ok": False, "status": NOT_READY, "detail": detail}

    @staticmethod
    def _failed(detail: str) -> dict[str, Any]:
        return {"ok": False, "status": FAILED, "detail": detail}


__all__ = [
    "GOAL_CALL_UNWIRED",
    "GoalCallPort",
    "KernelCoordinator",
    "KernelEffectPorts",
]