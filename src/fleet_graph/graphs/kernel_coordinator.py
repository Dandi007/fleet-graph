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
        """One coordinator turn = ingress the round's independent requests, then
        drain Goal calls serially.

        Every independently identified input (a message, a steer/decision, a DD
        result or review) becomes its own durable request, persisted before we
        proceed; the kernel then services queued requests one Goal call at a
        time, in submission order, until the queue is drained or the goal's own
        mode blocks it. A waiting result must not suppress the queued requests
        behind it (behavior 2/5).
        """
        self.enqueue_requests(round_no, coord_input)

        last: dict[str, Any] | None = None
        while True:
            call = self.kernel.next_goal_call(self.folder_id)
            if call is None:
                break
            last = self._run_call(call)
            # No verdict-based break: the kernel's mode fence is what stops the
            # drain (blocked/stopped return None from next_goal_call). Breaking
            # here on a waiting result would suppress the queued requests that
            # the waiting intent must not suppress.
            if self.goal_call is None:
                # An unbound Goal ReAct port keeps the request queued (aborted,
                # never served) and every further pop would abort the same way,
                # so stop draining this round instead of burning the queue.
                break

        if last is None:
            return _parked_verdict(self.kernel, self.folder_id)
        return last

    def enqueue_requests(
        self, round_no: int, coord_input: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Durably enqueue the round's independent requests into the kernel
        journal *before* a caller acknowledges them.

        ``turn`` re-derives the same stable identities from the same
        ``coord_input`` and re-submits them here, so the request dedup reuses
        the persisted record -- never appending a duplicate. Making this a
        separate entry point lets ``goal_line.coordinator_turn``'s persist
        callback land the requests durably *before* the inbox messages are
        acked, closing the interrupt window where an acked message could be
        lost before the coordinator turn reached the kernel journal (final
        review finding; behaviors 1/7)."""
        records: list[dict[str, Any]] = []
        for request in self._requests_for(round_no, coord_input):
            records.append(self.kernel.submit(self.folder_id, request))
        return records

    # -- request ingress -----------------------------------------------------

    def _requests_for(self, round_no: int, coord_input: dict[str, Any]) -> list[Request]:
        """Derive the round's independent requests from the coordinator input.

        One line round is no longer one combined request: each independently
        identifiable input becomes its own durable request with its own caller
        and kind, so a multi-message inbox never collapses into a single Goal
        prompt, and a distinct steer delivered on the same round is never
        mistaken for a duplicate of another input. Request identity is stable by
        input identity, not by round alone (P5).
        """
        folder_id = self.folder_id
        version = self.kernel.active_version(folder_id)
        inbox = coord_input.get("inbox_messages") or []
        decision = coord_input.get("decision")
        last_turn_report = coord_input.get("last_turn_report")
        gate_wake = coord_input.get("dd_awaiting_gate_development_id")

        requests: list[Request] = []

        if isinstance(last_turn_report, dict) and last_turn_report:
            turn_id = str(last_turn_report.get("turn_id") or "")
            requests.append(
                Request(
                    request_id=f"line:{folder_id}:dd_review:{turn_id or round_no}",
                    goal=folder_id,
                    caller="worker",
                    kind=KIND_DD_REVIEW,
                    input={"last_turn_report": last_turn_report},
                    goal_version=version,
                )
            )
        elif gate_wake:
            requests.append(
                Request(
                    request_id=f"line:{folder_id}:dd_result:{gate_wake}",
                    goal=folder_id,
                    caller="goal-enroll",
                    kind=KIND_DD_RESULT,
                    input={"dd_awaiting_gate_development_id": gate_wake},
                    goal_version=version,
                )
            )

        if isinstance(decision, dict) and decision:
            decided_by = str(decision.get("decided_by") or "goal")
            decision_id = str(decision.get("message_id") or decision.get("resume_key") or "")
            requests.append(
                Request(
                    request_id=f"line:{folder_id}:steer:{decision_id or round_no}",
                    goal=folder_id,
                    caller=decided_by,
                    kind=KIND_STEER,
                    input={"decision": decision},
                    goal_version=version,
                    # reply association (behavior 1): the reply to a steer is
                    # delivered to the decision's source, never guessed later.
                    reply_to=decided_by,
                )
            )

        if isinstance(inbox, list):
            for msg in inbox:
                if not isinstance(msg, dict):
                    continue
                message_id = str(msg.get("message_id") or "")
                caller = str(msg.get("from_agent_id") or msg.get("from_alias") or "line")
                # reply association (behavior 1): a message's reply is delivered
                # to its sender; when no sender identity is present the reply
                # target is left unset rather than fabricated.
                reply_to = msg.get("from_agent_id") or msg.get("from_alias") or None
                requests.append(
                    Request(
                        request_id=f"line:{folder_id}:message:{message_id or round_no}",
                        goal=folder_id,
                        caller=caller,
                        kind=KIND_MESSAGE,
                        input={"inbox_message": msg},
                        goal_version=version,
                        reply_to=str(reply_to) if reply_to is not None else None,
                    )
                )

        if not requests:
            if round_no == 1:
                requests.append(
                    Request(
                        request_id=f"line:{folder_id}:enroll",
                        goal=folder_id,
                        caller="goal-enroll",
                        kind=KIND_ENROLL,
                        input=dict(coord_input),
                        goal_version=version,
                    )
                )
            else:
                requests.append(
                    Request(
                        request_id=f"line:{folder_id}:round:{round_no}",
                        goal=folder_id,
                        caller="line",
                        kind=KIND_MESSAGE,
                        input=dict(coord_input),
                        goal_version=version,
                    )
                )
        return requests

    # -- the serial Goal call -------------------------------------------------

    def _run_call(self, call: dict[str, Any]) -> dict[str, Any]:
        if call.get("resume"):
            # A call interrupted after its validated Stop List was durable is
            # resumed, not re-answered: re-invoking Goal could return a
            # different list and duplicate a confirmed effect or drop an
            # outstanding one (finding 1). Execute the persisted list's
            # outstanding items through reconciliation instead.
            result = self.kernel.finish_goal_call(
                self.folder_id, call["call_id"], call.get("stop_list") or {}
            )
            return _to_verdict(result, self.kernel, self.folder_id)

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
            # The Goal ReAct call is not bound. Record the explicit capability
            # failure and release the fence *without* fabricating a Stop List or
            # a completed call result: the accepted request stays pending and a
            # later bound port delivers it exactly once (final review finding).
            self.kernel.abort_unavailable_call(
                self.folder_id, call["call_id"], reason=GOAL_CALL_UNWIRED
            )
            return {
                "verdict": "blocked",
                "waiting_on": "external",
                "reason": GOAL_CALL_UNWIRED,
            }
        stop_list = self.goal_call.call(self.folder_id, prompt)
        result = self.kernel.finish_goal_call(self.folder_id, call["call_id"], stop_list)
        return _to_verdict(result, self.kernel, self.folder_id)

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


def _to_verdict(
    result: dict[str, Any],
    kernel: GoalRequestKernel | None = None,
    goal: str | None = None,
) -> dict[str, Any]:
    """Map a ``finish_goal_call`` result back to the graph's verdict surface."""
    if result.get("suspended"):
        # An immediate stop suspended the in-flight result's unstarted effects:
        # the line parks until resume, never completing on a half-drained list.
        return {
            "verdict": "blocked",
            "waiting_on": "external",
            "reason": "goal stopped; in-flight result suspended until resume",
        }

    intent = result.get("intent")
    results = result.get("results") or []
    receipts = result.get("receipts") or []
    failed = [r for r in results if r.get("status") not in (DELIVERED,)]
    malformed = [r for r in receipts if r.get("status") == FAILED]

    if intent == "done":
        incomplete = len(failed) + len(malformed)
        outstanding = kernel.outstanding_deliveries(goal) if (kernel is not None and goal) else []
        if incomplete or outstanding:
            # P4: ``done`` is an intent, never a terminal shortcut -- an
            # undelivered dispatch/reply or a malformed entry keeps the line
            # pending, and so does any earlier call's unresolved delivery (a
            # reply that returned UNKNOWN, a dispatch that failed) that must
            # not be forgotten once the fence moves to the next request.
            clauses: list[str] = []
            if incomplete:
                clauses.append(f"{incomplete} receipt(s) from this call are incomplete")
            if outstanding:
                clauses.append(f"{len(outstanding)} delivery obligation(s) are still outstanding")
            return {
                "verdict": "blocked",
                "waiting_on": "none",
                "reason": "goal declared done but " + "; ".join(clauses) + "; line remains pending",
            }
        return {
            "verdict": "done",
            "reason": f"goal call {result.get('call_id')} routed {len(results)} action(s)",
        }

    verdict, waiting_on, reason = _intent_verdict(intent)
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
        if not isinstance(answer, dict):
            return self._failed("the dispatch subgraph returned a non-dict")
        result = answer.get("dd_result")
        if not (isinstance(result, dict) and result.get("development_id")):
            return self._failed("the dispatch subgraph returned no development")
        out: dict[str, Any] = {
            "ok": True,
            "status": DELIVERED,
            "detail": f"development {result.get('development_id')}",
        }
        # Preserve the raw downstream projection (development_id, state, stage,
        # generation, output_commit, terminal, terminal_reason) and the
        # admission/launch facts as first-class fields on the delivery so the
        # kernel's raw result record retains them -- the adapter used to keep
        # only ``development_id`` and drop the DD's current-state projection and
        # admission evidence, which kernel pagination must be able to expose
        # (final review finding; behaviors 2/6).
        for key, value in result.items():
            out.setdefault(key, value)
        record = answer.get("record")
        if isinstance(record, dict):
            admission = {k: v for k, v in record.items() if k not in out}
            if admission:
                out["admission"] = admission
        return out

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
        receipt = receipt or {}
        status = str(receipt.get("status") or "")
        consumed = status == "consumed"
        out: dict[str, Any] = {
            "ok": consumed,
            "status": DELIVERED if consumed else FAILED,
            "detail": (
                str(receipt.get("detail") or "")
                if consumed
                else str(
                    receipt.get("detail") or receipt.get("reason") or receipt.get("code") or "gate refused"
                )
            ),
        }
        # Preserve the raw gate receipt fields (decision, decided_by,
        # decided_by_source, decision_file, decision_message_id,
        # post_release_state, launches, evidence, development_id, and the
        # *structured* ``reason``/``code`` of a refusal) on the delivery on
        # *both* the consumed and the refused path, so the kernel's raw result
        # record retains the release's original result and evidence -- the
        # adapter used to reduce a refused gate (gate_obligations_failed,
        # not_awaiting_gate, not_dispatcher, ...) to a bare status/detail
        # string, which kernel pagination and a rebuilt product could not query
        # (final review finding; behavior 6).
        for key, value in receipt.items():
            if key in ("ok", "status", "detail") or key in out:
                continue
            out[key] = value
        return out

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
