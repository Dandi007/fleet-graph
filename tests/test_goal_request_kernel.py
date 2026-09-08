"""The DD01 request-kernel: serial request-to-Goal-call, offline, fake ports.

Every port here is a fake or an isolated in-memory journal. No agent, model,
daemon, systemd, shared service or new engine process is started; nothing
reaches network, git, the work-folder MCP or a real runtime. The tests map
one-to-one onto the spec's development-acceptance list (serial A/B/message
ordering, DD independence, one-current-request prompt, action partial failure,
same-request replay, reply reconciliation, crash before/after effect receipt,
stopped versus waiting wakeups, explicit goal versions, stale-version approval
refusal, unknown Runtime outcome, lossless pagination).
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from fleet_graph.goal.request_kernel import (
    ACTION_DISPATCH,
    ACTION_REPLY,
    DELIVERED,
    FAILED,
    KIND_DD_RESULT,
    KIND_DD_REVIEW,
    KIND_MESSAGE,
    KIND_STEER,
    MODE_BLOCKED,
    MODE_RUNNING,
    MODE_STOPPED,
    MODE_STOPPING,
    MODE_WAITING,
    NOT_READY,
    OBSERVED_ABSENT,
    OBSERVED_CONFIRMED,
    OBSERVED_UNKNOWN,
    RECORD_ACTION,
    RECORD_ACTION_RESULT,
    RECORD_CALL,
    RECORD_CALL_RESULT,
    RECORD_CALL_UNAVAILABLE,
    RECORD_CONTROL,
    RECORD_REQUEST,
    RECORD_STOP_LIST,
    RECORD_VERSION,
    UNKNOWN,
    GoalRequestKernel,
    Journal,
    Request,
    build_goal_prompt,
    business_guards,
    validate_stop_list,
)
from fleet_graph.graphs.kernel_coordinator import KernelCoordinator

GOAL = "wf-1"


# --- fakes ------------------------------------------------------------------


class FakeEffects:
    """A controllable effect port. ``dispatch`` and ``reply`` deliver (or fail)
    on command; ``observe`` answers reconciliation queries from an explicit
    table and otherwise assumes nothing ran yet (``absent``)."""

    def __init__(self) -> None:
        self.dispatches: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []
        self.approvals: list[dict[str, Any]] = []
        self.rejects: list[dict[str, Any]] = []
        self.add_repos: list[dict[str, Any]] = []
        # (kind, idempotency_key) -> delivery result dict; None means "not set,
        # derive the default (deliver ok)"
        self.delivery: dict[tuple[str, str], dict[str, Any]] = {}
        # (kind, idempotency_key) -> observation, overrides the default
        self.observations: dict[tuple[str, str], str] = {}

    def _lookup(self, kind: str, key: str, default: dict[str, Any]) -> dict[str, Any]:
        return self.delivery.get((kind, key), default)

    def observe(self, effect: str, key: str) -> str:
        return self.observations.get((effect, key), OBSERVED_ABSENT)

    def dispatch(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
        self.dispatches.append({"payload": payload, "ctx": dict(ctx)})
        return self._lookup(ACTION_DISPATCH, str(ctx.get("idempotency_key") or ""), {"ok": True})

    def approve(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
        self.approvals.append({"payload": payload, "ctx": dict(ctx)})
        return {"ok": True}

    def reject(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
        self.rejects.append({"payload": payload, "ctx": dict(ctx)})
        return {"ok": True}

    def add_repo(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
        self.add_repos.append({"payload": payload, "ctx": dict(ctx)})
        return self._lookup("add_repo", str(ctx.get("idempotency_key") or ""), {"ok": True})

    def reply(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
        self.replies.append({"payload": payload, "ctx": dict(ctx)})
        return self._lookup(ACTION_REPLY, str(ctx.get("idempotency_key") or ""), {"ok": True})


class RaisingEffects(FakeEffects):
    """An effect port that raises mid-dispatch: the true downstream state is
    unknown, so the resulting FAILED outcome must stay reconcilable."""

    def dispatch(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
        self.dispatches.append({"payload": payload, "ctx": dict(ctx)})
        raise RuntimeError("boom")


class CrashEffects(FakeEffects):
    """An effect port that *dies* mid-dispatch on chosen keys: it writes its
    intent first (via the kernel's intent-before-effect ordering), then raises a
    ``KeyboardInterrupt`` that ``_deliver`` does not catch -- simulating a real
    process crash after the action intent but before its result receipt."""

    def __init__(self, crash_on: set[str] | None = None) -> None:
        super().__init__()
        self.crash_on = crash_on or set()

    def dispatch(self, payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
        key = str(ctx.get("idempotency_key") or "")
        self.dispatches.append({"payload": payload, "ctx": dict(ctx)})
        if key in self.crash_on:
            raise KeyboardInterrupt
        return self._lookup(ACTION_DISPATCH, key, {"ok": True})


class FakeRuntime:
    """A runtime control port that records stop/resume and can be told how a
    cancellation request came back."""

    def __init__(self) -> None:
        self.stops: list[dict[str, Any]] = []
        self.resumes: list[str] = []
        self.stop_answer: dict[str, Any] = {"terminated": True}

    def stop(self, goal: str, *, mode: str) -> dict[str, Any]:
        self.stops.append({"goal": goal, "mode": mode})
        return dict(self.stop_answer)

    def resume(self, goal: str) -> dict[str, Any]:
        self.resumes.append(goal)
        return {"goal": goal, "resumed": True}

    def observe(self, goal: str, action_id: str) -> str:
        return OBSERVED_UNKNOWN


def make_request(
    request_id: str,
    *,
    kind: str = KIND_DD_RESULT,
    goal: str = GOAL,
    caller: str = "line-a",
    version: str = "v1",
    input: dict[str, Any] | None = None,
) -> Request:
    return Request(
        request_id=request_id,
        goal=goal,
        caller=caller,
        kind=kind,
        input=input if input is not None else {"note": request_id},
        goal_version=version,
    )


def make_kernel(
    effects: FakeEffects | None = None, runtime: FakeRuntime | None = None
) -> GoalRequestKernel:
    kernel = GoalRequestKernel(journal=Journal(), effects=effects or FakeEffects(), runtime=runtime)
    kernel.activate_version(GOAL, "v1")
    return kernel


def dispatch_action(key: str, *, repo: str = "repo-a") -> dict[str, Any]:
    return {
        "kind": ACTION_DISPATCH,
        "idempotency_key": key,
        "payload": {"repo_path": repo, "dispatched_by": "line-a", "spec_text": "s"},
    }


def reply_action(key: str) -> dict[str, Any]:
    return {
        "kind": ACTION_REPLY,
        "idempotency_key": key,
        "payload": {"text": "hello", "to": "line-a"},
    }


def run_one(
    kernel: GoalRequestKernel, request: Request, stop_list: dict[str, Any]
) -> dict[str, Any]:
    kernel.submit(GOAL, request)
    call = kernel.next_goal_call(GOAL)
    assert call is not None
    return kernel.finish_goal_call(GOAL, call["call_id"], stop_list)


# --- serial A / B / message ordering ----------------------------------------


class TestSerialOrdering:
    def test_requests_deliver_one_at_a_time_in_submission_order(self) -> None:
        kernel = make_kernel()
        a = make_request("A", kind=KIND_DD_REVIEW)
        b = make_request("B", kind=KIND_DD_REVIEW)
        msg = make_request("M", kind=KIND_MESSAGE)
        kernel.submit(GOAL, a)
        kernel.submit(GOAL, b)
        kernel.submit(GOAL, msg)

        first = kernel.next_goal_call(GOAL)
        assert first is not None and first["request_id"] == "A"
        # while A is in flight, the fence admits nothing else on this goal
        assert kernel.next_goal_call(GOAL) is None
        kernel.finish_goal_call(GOAL, first["call_id"], {"actions": []})

        second = kernel.next_goal_call(GOAL)
        assert second is not None and second["request_id"] == "B"
        kernel.finish_goal_call(GOAL, second["call_id"], {"actions": []})

        third = kernel.next_goal_call(GOAL)
        assert third is not None and third["request_id"] == "M"
        assert kernel.next_goal_call(GOAL) is None  # queue drained

    def test_queued_requests_are_not_lost_while_a_call_is_in_flight(self) -> None:
        kernel = make_kernel()
        kernel.submit(GOAL, make_request("A"))
        kernel.submit(GOAL, make_request("B"))
        call = kernel.next_goal_call(GOAL)
        assert call is not None and call["request_id"] == "A"
        # B is still waiting, not discarded by the fence
        kernel.finish_goal_call(GOAL, call["call_id"], {"actions": []})
        nxt = kernel.next_goal_call(GOAL)
        assert nxt is not None and nxt["request_id"] == "B"


# --- DD independence ---------------------------------------------------------


class TestDdIndependence:
    def test_a_call_on_one_goal_does_not_block_another_goal(self) -> None:
        effects = FakeEffects()
        kernel = make_kernel(effects)
        kernel.activate_version("wf-2", "v1")
        kernel.submit("wf-1", make_request("A", goal="wf-1"))
        kernel.submit("wf-2", make_request("B", goal="wf-2", caller="line-b"))

        a_call = kernel.next_goal_call("wf-1")
        assert a_call is not None
        # wf-2 is not blocked by wf-1's in-flight call
        b_call = kernel.next_goal_call("wf-2")
        assert b_call is not None and b_call["request_id"] == "B"
        kernel.finish_goal_call("wf-2", b_call["call_id"], {"actions": []})
        assert kernel.next_goal_call("wf-1") is None  # wf-1 still fenced
        kernel.finish_goal_call("wf-1", a_call["call_id"], {"actions": []})
        assert kernel.next_goal_call("wf-1") is None


# --- one-current-request prompt ---------------------------------------------


class TestOneCurrentRequestPrompt:
    def test_prompt_carries_only_the_current_request(self) -> None:
        current = make_request("cur", input={"goal": "do the thing"})
        history = [
            make_request("h1", input={"old": "text 1"}),
            make_request("h2", input={"old": "text 2"}),
        ]
        prompt = build_goal_prompt(current, history=history)
        assert prompt["request_id"] == "cur"
        assert prompt["input"] == {"goal": "do the thing"}
        # history is by pointer only, never appended as text
        assert prompt["history_refs"] == ["h1", "h2"]
        assert "old" not in prompt["input"]
        assert all(isinstance(ref, str) and "text" not in ref for ref in prompt["history_refs"])

    def test_prompt_carries_the_reply_association(self) -> None:
        # Behavior 1 "reply association": the current request's reply target is
        # carried in the prompt as a pointer so the Goal can address its reply
        # to the original caller without re-deriving it.
        current = make_request("cur", input={"goal": "do the thing"})
        current = Request(
            request_id=current.request_id,
            goal=current.goal,
            caller=current.caller,
            kind=current.kind,
            input=current.input,
            goal_version=current.goal_version,
            reply_to="line-a",
        )
        prompt = build_goal_prompt(current)
        assert prompt["reply_to"] == "line-a"


# --- action partial failure --------------------------------------------------


class TestActionPartialFailure:
    def test_a_failed_item_does_not_erase_earlier_successes(self) -> None:
        effects = FakeEffects()
        effects.delivery[(ACTION_DISPATCH, "k2")] = {
            "ok": False,
            "status": "failed",
            "detail": "boom",
        }
        kernel = make_kernel(effects)

        result = run_one(
            kernel,
            make_request("A"),
            {
                "actions": [
                    dispatch_action("k1"),
                    dispatch_action("k2"),
                    reply_action("k3"),
                ]
            },
        )
        statuses = [r["status"] for r in result["results"]]
        assert statuses[0] == DELIVERED
        assert statuses[1] == FAILED
        assert statuses[2] == DELIVERED
        # the earlier success and the later success both landed durably
        assert len(effects.dispatches) == 2
        assert len(effects.replies) == 1

    def test_a_missing_effect_port_fails_closed_not_ready(self) -> None:
        kernel = GoalRequestKernel(journal=Journal())
        kernel.activate_version(GOAL, "v1")
        result = run_one(kernel, make_request("A"), {"actions": [dispatch_action("k1")]})
        assert result["results"][0]["status"] == NOT_READY
        assert result["results"][0]["detail"] == "no effect port bound"


# --- same-request replay / no duplicate dispatch ----------------------------


class TestSameRequestReplay:
    def test_duplicate_request_identity_reuses_the_persisted_record(self) -> None:
        kernel = make_kernel()
        first = kernel.submit(GOAL, make_request("A"))
        second = kernel.submit(GOAL, make_request("A"))
        assert first["duplicate"] is False
        assert second["duplicate"] is True
        assert second["record"]["seq"] == first["record"]["seq"]
        # no second request row was appended
        requests = [r for r in kernel.list_events(GOAL)["events"] if r["record"] == RECORD_REQUEST]
        assert len(requests) == 1

    def test_a_confirmed_dispatch_is_not_re_run_on_replay(self) -> None:
        effects = FakeEffects()
        kernel = make_kernel(effects)
        result = run_one(kernel, make_request("A"), {"actions": [dispatch_action("K")]})
        assert result["results"][0]["status"] == DELIVERED
        assert len(effects.dispatches) == 1

        # A later, distinct call re-declares the same idempotency key: the
        # stored delivery result is the answer, the effect is not re-run.
        replay = run_one(kernel, make_request("B"), {"actions": [dispatch_action("K")]})
        assert replay["results"][0]["status"] == DELIVERED
        assert replay["results"][0]["reconciled"] is True
        assert len(effects.dispatches) == 1


# --- reply reconciliation ----------------------------------------------------


class TestReplyReconciliation:
    def test_reply_is_a_side_effect_associated_with_caller_and_request(self) -> None:
        effects = FakeEffects()
        kernel = make_kernel(effects)
        request = make_request("A", caller="line-a")
        kernel.submit(GOAL, request)
        call = kernel.next_goal_call(GOAL)
        assert call is not None
        result = kernel.finish_goal_call(GOAL, call["call_id"], {"actions": [reply_action("r1")]})
        assert result["results"][0]["status"] == DELIVERED
        assert effects.replies[0]["ctx"]["caller"] == "line-a"
        assert effects.replies[0]["ctx"]["request_id"] == "A"

    def test_a_confirmed_reply_is_not_re_sent(self) -> None:
        effects = FakeEffects()
        kernel = make_kernel(effects)
        run_one(kernel, make_request("A"), {"actions": [reply_action("R")]})
        assert len(effects.replies) == 1
        replay = run_one(kernel, make_request("B"), {"actions": [reply_action("R")]})
        assert replay["results"][0]["reconciled"] is True
        assert len(effects.replies) == 1


# --- crash before / after effect receipt ------------------------------------


class TestCrashBoundary:
    def _seed_prior_intent_without_result(
        self, kernel: GoalRequestKernel, kind: str, key: str
    ) -> None:
        """Simulate a crash *after* the effect ran but *before* its result was
        written: the action-intent row exists, the result row does not."""
        kernel.journal.append(
            GOAL,
            {
                "record": RECORD_ACTION,
                "goal": GOAL,
                "action_id": f"dead:{kind}:{key}",
                "kind": kind,
                "idempotency_key": key,
                "payload": {},
                "request_id": "crashed",
                "run_id": "run-crashed",
                "list_index": 1,
            },
        )

    def test_confirmed_observation_skips_re_delivery(self) -> None:
        effects = FakeEffects()
        effects.observations[(ACTION_DISPATCH, "K")] = OBSERVED_CONFIRMED
        kernel = make_kernel(effects)
        self._seed_prior_intent_without_result(kernel, ACTION_DISPATCH, "K")
        result = run_one(kernel, make_request("A"), {"actions": [dispatch_action("K")]})
        assert result["results"][0]["status"] == DELIVERED
        assert result["results"][0]["reconciled"] is True
        assert effects.dispatches == []  # never re-executed

    def test_absent_observation_re_delivers(self) -> None:
        effects = FakeEffects()
        effects.observations[(ACTION_DISPATCH, "K")] = OBSERVED_ABSENT
        kernel = make_kernel(effects)
        self._seed_prior_intent_without_result(kernel, ACTION_DISPATCH, "K")
        result = run_one(kernel, make_request("A"), {"actions": [dispatch_action("K")]})
        assert result["results"][0]["status"] == DELIVERED
        assert len(effects.dispatches) == 1

    def test_unknown_observation_is_recoverable_and_not_re_run(self) -> None:
        effects = FakeEffects()
        effects.observations[(ACTION_DISPATCH, "K")] = OBSERVED_UNKNOWN
        kernel = make_kernel(effects)
        self._seed_prior_intent_without_result(kernel, ACTION_DISPATCH, "K")
        result = run_one(kernel, make_request("A"), {"actions": [dispatch_action("K")]})
        assert result["results"][0]["status"] == UNKNOWN
        assert len(effects.dispatches) == 0


# --- stopped versus waiting wakeups -----------------------------------------


class TestStoppedVsWaiting:
    def test_waiting_does_not_suppress_already_queued_requests(self) -> None:
        kernel = make_kernel()
        kernel.submit(GOAL, make_request("A"))
        kernel.waiting(GOAL, waiting_on="decision")
        # a waiting goal still serves its queued request
        call = kernel.next_goal_call(GOAL)
        assert call is not None and call["request_id"] == "A"

    def test_blocked_is_woken_by_a_new_request(self) -> None:
        kernel = make_kernel()
        kernel.blocked(GOAL, blocker="needs human")
        assert kernel.next_goal_call(GOAL) is None  # parked
        kernel.submit(GOAL, make_request("A"))  # new input wakes it
        call = kernel.next_goal_call(GOAL)
        assert call is not None and call["request_id"] == "A"

    def test_stopped_persists_but_starts_nothing_until_resume(self) -> None:
        effects = FakeEffects()
        kernel = make_kernel(effects)
        kernel.stop(GOAL)
        # message/steer persist into the queue even while stopped
        kernel.submit(GOAL, make_request("M", kind=KIND_MESSAGE))
        kernel.submit(GOAL, make_request("S", kind=KIND_STEER))
        assert kernel.next_goal_call(GOAL) is None  # no call, no effect

        kernel.resume(GOAL)
        call = kernel.next_goal_call(GOAL)
        assert call is not None and call["request_id"] == "M"
        assert len(effects.dispatches) == 0  # nothing ran while stopped

    def test_immediate_stop_records_a_missing_cancellation_without_pretending(self) -> None:
        runtime = FakeRuntime()
        runtime.stop_answer = {"ack": "unsupported"}  # no `terminated`
        kernel = make_kernel(runtime=runtime)
        result = kernel.stop(GOAL, mode="immediate")
        assert result["stopped"] is False
        assert result["cancel"] == {"ack": "unsupported"}
        assert runtime.stops == [{"goal": GOAL, "mode": "immediate"}]


# --- explicit goal versions --------------------------------------------------


class TestExplicitGoalVersions:
    def test_a_version_is_an_explicit_immutable_event(self) -> None:
        kernel = make_kernel()
        assert kernel.active_version(GOAL) == "v1"
        kernel.activate_version(GOAL, "v2")
        assert kernel.active_version(GOAL) == "v2"
        # editing goal material is not modelled here as a version event, so
        # only activate_version moves it
        kernel.submit(GOAL, make_request("A", version="v1"))
        assert kernel.active_version(GOAL) == "v2"


# --- stale-version approval refusal ------------------------------------------


class TestStaleVersionApproval:
    def test_approval_bound_to_a_stale_version_is_refused(self) -> None:
        effects = FakeEffects()
        kernel = make_kernel(effects)
        kernel.activate_version(GOAL, "v2")
        action = {
            "kind": "approve",
            "idempotency_key": "ap1",
            "payload": {"development_id": "d1", "verdict": "APPROVE", "goal_version": "v1"},
        }
        result = run_one(kernel, make_request("A"), {"actions": [action]})
        assert result["results"][0]["status"] == FAILED
        assert "stale_version" in result["results"][0]["detail"]
        assert effects.approvals == []

    def test_approval_at_the_active_version_runs(self) -> None:
        effects = FakeEffects()
        kernel = make_kernel(effects)
        action = {
            "kind": "approve",
            "idempotency_key": "ap1",
            "payload": {"development_id": "d1", "verdict": "APPROVE", "goal_version": "v1"},
        }
        result = run_one(kernel, make_request("A"), {"actions": [action]})
        assert result["results"][0]["status"] == DELIVERED
        assert len(effects.approvals) == 1


# --- business guards / stop-list validation ----------------------------------


class TestStopListValidation:
    def test_malformed_actions_fail_closed_without_blocking_siblings(self) -> None:
        consumable, receipts, intent = validate_stop_list(
            {
                "actions": [
                    {"kind": "bogus", "payload": {}, "idempotency_key": "x"},
                    dispatch_action("ok"),
                    {"kind": ACTION_DISPATCH},  # no payload, no key
                ],
                "intent": "waiting",
            }
        )
        assert [a["idempotency_key"] for a in consumable] == ["ok"]
        assert len(receipts) == 2
        assert intent == "waiting"

    def test_unknown_intent_is_observable_not_fabricated(self) -> None:
        _consumable, _receipts, intent = validate_stop_list({"actions": [], "intent": "mystery"})
        assert intent is None

    def test_dispatch_requires_exactly_one_repo(self) -> None:
        multi = {
            "kind": ACTION_DISPATCH,
            "idempotency_key": "m",
            "payload": {
                "repo_path": "r",
                "repo_paths": ["a", "b"],
                "dispatched_by": "line-a",
            },
        }
        assert "one repo" in business_guards(multi, active_version="v1")
        missing = {
            "kind": ACTION_DISPATCH,
            "idempotency_key": "m",
            "payload": {
                "dispatched_by": "line-a",
            },
        }
        assert (
            business_guards(missing, active_version="v1")
            == "dispatch requires exactly one repo_path"
        )


# --- unknown runtime outcome -------------------------------------------------


class TestUnknownRuntimeOutcome:
    def test_a_non_terminating_stop_is_recorded_honestly(self) -> None:
        runtime = FakeRuntime()
        runtime.stop_answer = {}  # no termination signal at all
        kernel = make_kernel(runtime=runtime)
        result = kernel.stop(GOAL, mode="immediate")
        assert result["stopped"] is False
        assert result["cancel"] == {}


# --- lossless pagination -----------------------------------------------------


class TestLosslessPagination:
    def test_every_recorded_event_is_traversable_in_order(self) -> None:
        effects = FakeEffects()
        kernel = make_kernel(effects)
        kernel.submit(GOAL, make_request("A"))
        run_one(kernel, make_request("B"), {"actions": [dispatch_action("k1"), reply_action("k2")]})
        kernel.activate_version(GOAL, "v2")
        kernel.stop(GOAL)

        all_events: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = kernel.list_events(GOAL, cursor=cursor, limit=3)
            all_events.extend(page["events"])
            cursor = page["next_cursor"]
            if cursor is None:
                break

        total = kernel.list_events(GOAL)["total"]
        assert len(all_events) == total
        records = [e["record"] for e in all_events]
        assert records.count("request") == 2
        assert records.count("call") == 1
        assert records.count("action") == 2
        assert records.count("action_result") == 2
        assert records.count("version") == 2  # v1 (init) + v2
        assert records.count("control") >= 1
        # seq strictly increasing
        seqs = [int(e["seq"]) for e in all_events]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

    def test_no_raw_payload_is_summarized_away(self) -> None:
        kernel = make_kernel()
        kernel.submit(GOAL, make_request("A", input={"deep": {"fact": 42}}))
        events = kernel.list_events(GOAL)["events"]
        request = next(e for e in events if e["record"] == "request")
        assert request["input"]["deep"]["fact"] == 42


# --- durable reconstruction (P6 / behaviors 1, 4, 7) -------------------------


class TestDurableReconstruction:
    def test_kernel_reconstructs_versions_queue_and_stop_from_durable_journal(
        self, tmp_path
    ) -> None:
        home = tmp_path / "journal"
        kernel = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        kernel.activate_version(GOAL, "v1")
        kernel.submit(GOAL, make_request("A"))
        kernel.submit(GOAL, make_request("B"))
        # A is served with a confirmed dispatch; B stays queued; then stop.
        call = kernel.next_goal_call(GOAL)
        assert call is not None and call["request_id"] == "A"
        kernel.finish_goal_call(GOAL, call["call_id"], {"actions": [dispatch_action("K")]})
        kernel.stop(GOAL)

        # A rebuilt product reconstructs the same state from the JSONL files.
        rebuilt = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        assert rebuilt.active_version(GOAL) == "v1"
        assert rebuilt.next_goal_call(GOAL) is None  # stop survived the rebuild
        rebuilt.resume(GOAL)
        nxt = rebuilt.next_goal_call(GOAL)
        assert nxt is not None and nxt["request_id"] == "B"  # queued request survived

    def test_confirmed_dispatch_is_not_re_run_after_reconstruction(self, tmp_path) -> None:
        home = tmp_path / "journal"
        kernel = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        kernel.activate_version(GOAL, "v1")
        result = run_one(kernel, make_request("A"), {"actions": [dispatch_action("K")]})
        assert result["results"][0]["status"] == DELIVERED

        rebuilt = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        replay = run_one(rebuilt, make_request("B"), {"actions": [dispatch_action("K")]})
        assert replay["results"][0]["status"] == DELIVERED
        assert replay["results"][0]["reconciled"] is True

    def test_incomplete_tail_is_skipped_without_faulting_the_journal(self, tmp_path) -> None:
        home = tmp_path / "journal"
        kernel = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        kernel.activate_version(GOAL, "v1")
        # Simulate a crash mid-append: a partial trailing line.
        path = home / "goal-wf-1.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"record": "request", "goal": "wf-1", "requ')

        rebuilt = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        assert rebuilt.active_version(GOAL) == "v1"


# --- raw event completeness (behavior 6) --------------------------------------


class TestRawEventCompleteness:
    def test_call_result_record_preserves_stop_list_and_intent(self) -> None:
        kernel = make_kernel(FakeEffects())
        run_one(
            kernel, make_request("A"), {"actions": [dispatch_action("k1")], "intent": "waiting"}
        )
        call_results = [
            e for e in kernel.list_events(GOAL)["events"] if e["record"] == RECORD_CALL_RESULT
        ]
        assert len(call_results) == 1
        assert call_results[0]["intent"] == "waiting"
        assert call_results[0]["actions"] == [dispatch_action("k1")]

    def test_downstream_delivery_fields_are_retained_in_the_result_record(self) -> None:
        effects = FakeEffects()
        effects.delivery[(ACTION_DISPATCH, "k1")] = {
            "ok": True,
            "status": DELIVERED,
            "development_id": "d-1",
            "evidence": {"sha": "abc"},
        }
        kernel = make_kernel(effects)
        run_one(kernel, make_request("A"), {"actions": [dispatch_action("k1")]})
        records = [
            e
            for e in kernel.list_events(GOAL)["events"]
            if e["record"] == RECORD_ACTION_RESULT and e.get("kind") == ACTION_DISPATCH
        ]
        assert len(records) == 1
        assert records[0]["development_id"] == "d-1"
        assert records[0]["evidence"] == {"sha": "abc"}

    def test_malformed_action_receipts_are_journaled(self) -> None:
        kernel = make_kernel()
        malformed = [{"kind": "bogus", "payload": {}, "idempotency_key": "x"}]
        result = run_one(kernel, make_request("A"), {"actions": malformed})
        assert result["receipts"] != []
        records = [
            e
            for e in kernel.list_events(GOAL)["events"]
            if e["record"] == RECORD_ACTION_RESULT
            and "unknown action kind" in str(e.get("detail", ""))
        ]
        assert len(records) == 1


# --- same-list add_repo dependency (P1) --------------------------------------


class TestAddRepoDependency:
    def test_dependent_dispatch_refused_when_add_repo_failed(self) -> None:
        effects = FakeEffects()
        effects.delivery[("add_repo", "ar1")] = {"ok": False, "status": FAILED, "detail": "boom"}
        kernel = make_kernel(effects)
        result = run_one(
            kernel,
            make_request("A"),
            {
                "actions": [
                    {
                        "kind": "add_repo",
                        "idempotency_key": "ar1",
                        "payload": {"repo_path": "repo-a"},
                    },
                    dispatch_action("k1", repo="repo-a"),
                ]
            },
        )
        statuses = {r["kind"]: r for r in result["results"]}
        assert statuses["add_repo"]["status"] == FAILED
        assert statuses["dispatch"]["status"] == FAILED
        assert statuses["dispatch"]["detail"].startswith("dependent dispatch refused")

    def test_dependent_dispatch_runs_when_add_repo_succeeded(self) -> None:
        kernel = make_kernel(FakeEffects())
        result = run_one(
            kernel,
            make_request("A"),
            {
                "actions": [
                    {
                        "kind": "add_repo",
                        "idempotency_key": "ar1",
                        "payload": {"repo_path": "repo-a"},
                    },
                    dispatch_action("k1", repo="repo-a"),
                ]
            },
        )
        statuses = {r["kind"]: r for r in result["results"]}
        assert statuses["add_repo"]["status"] == DELIVERED
        assert statuses["dispatch"]["status"] == DELIVERED

    def test_dispatch_without_same_list_add_repo_is_not_dependent(self) -> None:
        kernel = make_kernel(FakeEffects())
        result = run_one(kernel, make_request("A"), {"actions": [dispatch_action("k1")]})
        assert result["results"][0]["status"] == DELIVERED

    def test_dispatch_ordered_before_same_list_add_repo_is_refused(self) -> None:
        effects = FakeEffects()
        kernel = make_kernel(effects)
        # The dependency is identified before effects run: a dispatch for a
        # repo whose add_repo only appears *later* in the same list must not
        # execute ahead of admission.
        result = run_one(
            kernel,
            make_request("A"),
            {
                "actions": [
                    dispatch_action("k1", repo="repo-a"),
                    {
                        "kind": "add_repo",
                        "idempotency_key": "ar1",
                        "payload": {"repo_path": "repo-a"},
                    },
                ]
            },
        )
        statuses = {r["kind"]: r for r in result["results"]}
        assert statuses["dispatch"]["status"] == FAILED
        assert statuses["dispatch"]["detail"].startswith("dependent dispatch refused")
        # the later add_repo still ran and delivered independently of the refusal
        assert statuses["add_repo"]["status"] == DELIVERED
        assert effects.dispatches == []
        assert len(effects.add_repos) == 1


# --- stop authority over the in-flight result (behavior 5) --------------------


class TestStopAuthority:
    def test_graceful_stop_drains_in_flight_result_then_remains_stopped(self) -> None:
        effects = FakeEffects()
        kernel = make_kernel(effects)
        kernel.submit(GOAL, make_request("A"))
        call = kernel.next_goal_call(GOAL)
        assert call is not None
        kernel.stop(GOAL)  # graceful while a call is in flight
        assert kernel.next_goal_call(GOAL) is None  # nothing new starts
        result = kernel.finish_goal_call(
            GOAL, call["call_id"], {"actions": [dispatch_action("k1")], "intent": "waiting"}
        )
        # the in-flight result was drained exactly once
        assert result["results"][0]["status"] == DELIVERED
        assert len(effects.dispatches) == 1
        # stopped remains authoritative over the returned waiting intent
        assert kernel.next_goal_call(GOAL) is None
        kernel.resume(GOAL)
        assert kernel.next_goal_call(GOAL) is None  # now running, queue empty


# --- incomplete-tail isolation (finding: tail fragment hides the next append) ---


class TestIncompleteTailIsolation:
    def test_incomplete_tail_is_isolated_before_a_further_append(self, tmp_path) -> None:
        home = tmp_path / "journal"
        kernel = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        kernel.activate_version(GOAL, "v1")
        path = home / "goal-wf-1.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"record": "request", "goal": "wf-1", "requ')

        # First reconstruction isolates the fragment; the journal ends cleanly.
        rebuilt = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        assert rebuilt.active_version(GOAL) == "v1"
        assert path.with_suffix(path.suffix + ".corrupt").exists()

        # A later append writes a clean, standalone line that survives a second
        # reconstruction instead of concatenating onto the orphaned fragment.
        rebuilt.submit(GOAL, make_request("A"))
        again = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        requests = [e for e in again.list_events(GOAL)["events"] if e["record"] == RECORD_REQUEST]
        assert [r["request_id"] for r in requests] == ["A"]

    def test_complete_record_without_trailing_newline_is_normalized(self, tmp_path) -> None:
        home = tmp_path / "journal"
        kernel = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        kernel.activate_version(GOAL, "v1")
        kernel.submit(GOAL, make_request("A"))
        path = home / "goal-wf-1.jsonl"
        # Simulate a crash between the JSON object write and its terminating
        # newline: the file ends with a COMPLETE, valid record but no '\n'.
        content = path.read_text(encoding="utf-8")
        assert content.endswith("\n")
        path.write_text(content[:-1], encoding="utf-8")
        assert not path.read_text(encoding="utf-8").endswith("\n")

        # load() accepts that object and normalizes the non-newline-terminated
        # tail back to a newline-terminated journal, preserving the record.
        rebuilt = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        assert path.read_text(encoding="utf-8").endswith("\n")
        assert rebuilt.active_version(GOAL) == "v1"
        requests = [e for e in rebuilt.list_events(GOAL)["events"] if e["record"] == RECORD_REQUEST]
        assert [r["request_id"] for r in requests] == ["A"]

        # A later append writes a standalone line: reloading reconstructs both
        # records instead of rejecting a concatenated line.
        rebuilt.submit(GOAL, make_request("B"))
        again = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        requests = [e for e in again.list_events(GOAL)["events"] if e["record"] == RECORD_REQUEST]
        assert [r["request_id"] for r in requests] == ["A", "B"]


# --- interrupted-call recovery (behavior 7 / P6) ------------------------------


class TestInterruptedCallRecovery:
    def test_a_call_without_result_requeues_its_request(self, tmp_path) -> None:
        home = tmp_path / "journal"
        kernel = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        kernel.activate_version(GOAL, "v1")
        kernel.submit(GOAL, make_request("A"))
        call = kernel.next_goal_call(GOAL)
        assert call is not None
        # crash after RECORD_CALL, before any result or call_result

        rebuilt = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        nxt = rebuilt.next_goal_call(GOAL)
        assert nxt is not None and nxt["request_id"] == "A"

    def test_interrupted_call_recovers_delivered_effect_without_re_running(self, tmp_path) -> None:
        home = tmp_path / "journal"
        kernel = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        kernel.activate_version(GOAL, "v1")
        kernel.submit(GOAL, make_request("A"))
        call = kernel.next_goal_call(GOAL)
        assert call is not None
        # Simulate one effect delivered + its result durably persisted, then a
        # crash before RECORD_CALL_RESULT was written.
        call_id = call["call_id"]
        action_id = f"{call_id}:1:dispatch:k1"
        kernel.journal.append(
            GOAL,
            {
                "record": RECORD_ACTION,
                "goal": GOAL,
                "action_id": action_id,
                "call_id": call_id,
                "request_id": "A",
                "run_id": call["run_id"],
                "list_index": 1,
                "kind": ACTION_DISPATCH,
                "payload": dispatch_action("k1")["payload"],
                "idempotency_key": "k1",
            },
        )
        kernel.journal.append(
            GOAL,
            {
                "record": RECORD_ACTION_RESULT,
                "goal": GOAL,
                "action_id": action_id,
                "request_id": "A",
                "run_id": call["run_id"],
                "list_index": 1,
                "kind": ACTION_DISPATCH,
                "status": DELIVERED,
                "detail": "",
                "final": True,
            },
        )

        effects2 = FakeEffects()
        rebuilt = GoalRequestKernel(journal=Journal(home=home), effects=effects2)
        nxt = rebuilt.next_goal_call(GOAL)
        assert nxt is not None and nxt["request_id"] == "A"
        outcome = rebuilt.finish_goal_call(
            GOAL, nxt["call_id"], {"actions": [dispatch_action("k1")]}
        )
        assert outcome["results"][0]["status"] == DELIVERED
        assert outcome["results"][0]["reconciled"] is True
        assert effects2.dispatches == []  # the confirmed effect is not re-run


# --- uncertain-outcome reconciliation (behavior 4) ----------------------------


class TestOutstandingDeliveries:
    def test_unresolved_reply_is_an_outstanding_obligation_across_calls(self) -> None:
        effects = FakeEffects()
        effects.delivery[(ACTION_REPLY, "R")] = {"ok": False, "status": UNKNOWN, "detail": "?"}
        kernel = make_kernel(effects)
        first = run_one(kernel, make_request("A"), {"actions": [reply_action("R")]})
        assert first["results"][0]["status"] == UNKNOWN

        # A second call that returns an empty done list must still see the
        # unresolved reply as an outstanding delivery obligation.
        assert len(kernel.outstanding_deliveries(GOAL)) == 1
        run_one(kernel, make_request("B"), {"actions": [], "intent": "done"})
        assert [o["idempotency_key"] for o in kernel.outstanding_deliveries(GOAL)] == ["R"]

    def test_a_delivered_action_retires_its_obligation(self, tmp_path) -> None:
        home = tmp_path / "journal"
        effects = FakeEffects()
        kernel = GoalRequestKernel(journal=Journal(home=home), effects=effects)
        kernel.activate_version(GOAL, "v1")
        effects.delivery[(ACTION_REPLY, "R")] = {"ok": False, "status": UNKNOWN}
        run_one(kernel, make_request("A"), {"actions": [reply_action("R")]})
        assert len(kernel.outstanding_deliveries(GOAL)) == 1

        # A rebuilt product reconstructs the same obligation from the journal,
        # then resolving it (a confirmed observation) retires it.
        rebuilt = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        assert len(rebuilt.outstanding_deliveries(GOAL)) == 1
        rebuilt.effects.observations[(ACTION_REPLY, "R")] = OBSERVED_CONFIRMED  # type: ignore[union-attr]
        run_one(rebuilt, make_request("B"), {"actions": [reply_action("R")]})
        assert rebuilt.outstanding_deliveries(GOAL) == []

    def test_a_not_ready_delivery_is_final_but_stays_outstanding(self) -> None:
        effects = FakeEffects()
        effects.delivery[(ACTION_REPLY, "R")] = {
            "ok": False,
            "status": NOT_READY,
            "detail": "no reply sink bound",
        }
        kernel = make_kernel(effects)
        first = run_one(kernel, make_request("A"), {"actions": [reply_action("R")]})
        assert first["results"][0]["status"] == NOT_READY
        # Final for the attempt (never retried), but neither fulfilled nor
        # explicitly disposed, so the required reply stays outstanding.
        assert [o["idempotency_key"] for o in kernel.outstanding_deliveries(GOAL)] == ["R"]
        # An empty ``done`` list from a later call cannot retire that obligation.
        run_one(kernel, make_request("B"), {"actions": [], "intent": "done"})
        assert [o["idempotency_key"] for o in kernel.outstanding_deliveries(GOAL)] == ["R"]

    def test_an_explicit_refusal_disposes_its_obligation(self) -> None:
        effects = FakeEffects()
        kernel = make_kernel(effects)
        kernel.activate_version(GOAL, "v2")
        action = {
            "kind": "approve",
            "idempotency_key": "ap1",
            "payload": {"development_id": "d1", "verdict": "APPROVE", "goal_version": "v1"},
        }
        result = run_one(kernel, make_request("A"), {"actions": [action]})
        assert result["results"][0]["status"] == FAILED
        # The stale-version refusal is an explicit disposition: the delivery was
        # rejected outright, not merely left unfulfilled, so it is not
        # outstanding and cannot keep a later ``done`` pending forever.
        assert kernel.outstanding_deliveries(GOAL) == []


class TestUncertainReconciliation:
    def test_persisted_unknown_reconciles_to_confirmed(self) -> None:
        effects = FakeEffects()
        kernel = make_kernel(effects)
        effects.delivery[(ACTION_REPLY, "R")] = {"ok": False, "status": UNKNOWN, "detail": "?"}
        first = run_one(kernel, make_request("A"), {"actions": [reply_action("R")]})
        assert first["results"][0]["status"] == UNKNOWN

        effects.observations[(ACTION_REPLY, "R")] = OBSERVED_CONFIRMED
        replay = run_one(kernel, make_request("B"), {"actions": [reply_action("R")]})
        assert replay["results"][0]["status"] == DELIVERED
        assert replay["results"][0]["reconciled"] is True
        assert len(effects.replies) == 1  # never re-sent

    def test_persisted_unknown_re_delivers_when_absent(self) -> None:
        effects = FakeEffects()
        kernel = make_kernel(effects)
        effects.delivery[(ACTION_REPLY, "R")] = {"ok": False, "status": UNKNOWN}
        first = run_one(kernel, make_request("A"), {"actions": [reply_action("R")]})
        assert first["results"][0]["status"] == UNKNOWN

        effects.delivery[(ACTION_REPLY, "R")] = {"ok": True}
        effects.observations[(ACTION_REPLY, "R")] = OBSERVED_ABSENT
        replay = run_one(kernel, make_request("B"), {"actions": [reply_action("R")]})
        assert replay["results"][0]["status"] == DELIVERED
        assert len(effects.replies) == 2  # first send + one re-delivery

    def test_exception_during_delivery_is_reconcilable(self) -> None:
        effects = RaisingEffects()
        kernel = make_kernel(effects)
        first = run_one(kernel, make_request("A"), {"actions": [dispatch_action("K")]})
        assert first["results"][0]["status"] == FAILED

        effects.observations[(ACTION_DISPATCH, "K")] = OBSERVED_CONFIRMED
        replay = run_one(kernel, make_request("B"), {"actions": [dispatch_action("K")]})
        assert replay["results"][0]["status"] == DELIVERED
        assert replay["results"][0]["reconciled"] is True

    def test_unknown_observation_is_persisted_and_survives_reconstruction(self, tmp_path) -> None:
        # A crash leaves an action intent durably behind with no delivery result.
        home = tmp_path / "journal"
        crashed = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        crashed.activate_version(GOAL, "v1")
        crashed.journal.append(
            GOAL,
            {
                "record": RECORD_ACTION,
                "goal": GOAL,
                "action_id": "dead:dispatch:K",
                "kind": ACTION_DISPATCH,
                "idempotency_key": "K",
                "payload": {"repo_path": "repo-a"},
                "request_id": "crashed",
                "run_id": "run-crashed",
                "list_index": 1,
            },
        )

        # Reconstruct and reconcile the orphaned intent: the external-effect port
        # answers "unknown", so the outcome must be persisted, not returned and
        # silently dropped.
        effects = FakeEffects()
        effects.observations[(ACTION_DISPATCH, "K")] = OBSERVED_UNKNOWN
        rebuilt = GoalRequestKernel(journal=Journal(home=home), effects=effects)
        result = run_one(rebuilt, make_request("A"), {"actions": [dispatch_action("K")]})
        assert result["results"][0]["status"] == UNKNOWN
        assert result["results"][0]["reconciled"] is True

        # A second reconstruction exposes the persisted unknown outcome through
        # pagination, carrying the original action identity and the current
        # request/run/index attribution.
        rebuilt_again = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        outcomes = [
            e
            for e in rebuilt_again.list_events(GOAL)["events"]
            if e["record"] == RECORD_ACTION_RESULT and e.get("action_id") == "dead:dispatch:K"
        ]
        assert len(outcomes) == 1
        assert outcomes[0]["kind"] == ACTION_DISPATCH
        assert outcomes[0]["status"] == UNKNOWN
        assert outcomes[0]["detail"] == "outcome unknown; recoverable and requires Goal judgement"
        assert outcomes[0]["final"] is False
        assert outcomes[0]["request_id"] == "A"
        assert outcomes[0]["run_id"]
        assert outcomes[0]["list_index"] == 1


# --- finding 1: interrupted call resumes its persisted list, never re-Goal -----


class TestResumePersistedStopList:
    def _crash_mid_list(self, tmp_path, *, crash_on: set[str]) -> tuple[str, list[dict[str, Any]]]:
        home = tmp_path / "journal"
        effects = CrashEffects(crash_on=crash_on)
        kernel = GoalRequestKernel(journal=Journal(home=home), effects=effects)
        kernel.activate_version(GOAL, "v1")
        kernel.submit(GOAL, make_request("A"))
        call = kernel.next_goal_call(GOAL)
        assert call is not None and call["request_id"] == "A"
        with pytest.raises(KeyboardInterrupt):
            kernel.finish_goal_call(
                GOAL, call["call_id"], {"actions": [dispatch_action("k1"), dispatch_action("k2")]}
            )
        return call["call_id"], list(effects.dispatches)

    def test_validated_stop_list_is_durable_before_effects(self, tmp_path) -> None:
        call_id, _ = self._crash_mid_list(tmp_path, crash_on={"k2"})
        durable = Journal(home=tmp_path / "journal")
        stop_lists = [
            line
            for line in durable.scan(GOAL)
            if line.get("record") == RECORD_STOP_LIST and line.get("call_id") == call_id
        ]
        assert len(stop_lists) == 1
        assert [a["idempotency_key"] for a in stop_lists[0]["actions"]] == ["k1", "k2"]
        assert [a["list_index"] for a in stop_lists[0]["actions"]] == [1, 2]

    def test_interrupted_call_resumes_outstanding_items_without_replay_of_source_list(
        self, tmp_path
    ) -> None:
        call_id, crashed = self._crash_mid_list(tmp_path, crash_on={"k2"})
        # k1 delivered + confirmed before the crash; k2 intent written, no result.
        assert [d["ctx"]["idempotency_key"] for d in crashed] == ["k1", "k2"]

        effects2 = FakeEffects()
        rebuilt = GoalRequestKernel(journal=Journal(home=tmp_path / "journal"), effects=effects2)
        nxt = rebuilt.next_goal_call(GOAL)
        # The interrupted call resumes under its original identities, not a
        # fresh Goal answer: same call/request, no re-supplied list.
        assert nxt is not None and nxt["resume"] is True
        assert nxt["call_id"] == call_id
        assert nxt["request_id"] == "A"
        outcome = rebuilt.finish_goal_call(GOAL, nxt["call_id"], nxt["stop_list"])
        statuses = {r["idempotency_key"]: r for r in outcome["results"]}
        assert statuses["k1"]["status"] == DELIVERED
        assert statuses["k1"]["reconciled"] is True  # confirmed effect not re-run
        assert statuses["k2"]["status"] == DELIVERED
        # only the not-yet-confirmed k2 was re-delivered; k1 was reconciled.
        assert [d["ctx"]["idempotency_key"] for d in effects2.dispatches] == ["k2"]

    def test_coordinator_does_not_reinvoke_goal_for_a_resumed_call(self, tmp_path) -> None:
        self._crash_mid_list(tmp_path, crash_on={"k2"})

        class RecordingGoalPort:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def call(self, goal: str, prompt: dict[str, Any]) -> dict[str, Any]:
                self.calls.append(str(prompt.get("request_id") or ""))
                return {"actions": [], "intent": "done"}

        port = RecordingGoalPort()
        effects2 = FakeEffects()
        rebuilt = GoalRequestKernel(journal=Journal(home=tmp_path / "journal"), effects=effects2)
        coord = KernelCoordinator(kernel=rebuilt, goal_call=port, folder_id=GOAL)
        coord.turn(1, {})

        # The interrupted request "A" was resumed from its persisted list and the
        # Goal was never re-invoked for it; only the round-1 enroll fallback
        # reached the Goal port.
        assert "A" not in port.calls
        assert port.calls == [f"line:{GOAL}:enroll"]
        assert [d["ctx"]["idempotency_key"] for d in effects2.dispatches] == ["k2"]


# --- finding 2: confirmed immediate stop suspends unstarted effects -------------


class TestStopSuspendsEffects:
    def test_confirmed_immediate_stop_preserves_result_and_runs_nothing(self) -> None:
        runtime = FakeRuntime()  # stop_answer defaults to terminated=True
        effects = FakeEffects()
        kernel = make_kernel(effects, runtime)
        kernel.submit(GOAL, make_request("A"))
        call = kernel.next_goal_call(GOAL)
        assert call is not None

        kernel.stop(GOAL, mode="immediate")  # confirmed -> MODE_STOPPED
        result = kernel.finish_goal_call(
            GOAL, call["call_id"], {"actions": [dispatch_action("k1"), reply_action("k2")]}
        )
        assert result["suspended"] is True
        assert effects.dispatches == []  # nothing ran while stopped
        assert effects.replies == []
        assert kernel.next_goal_call(GOAL) is None  # fence still held / stopped

        kernel.resume(GOAL)
        nxt = kernel.next_goal_call(GOAL)
        assert nxt is not None and nxt["resume"] is True
        outcome = kernel.finish_goal_call(GOAL, nxt["call_id"], nxt["stop_list"])
        assert [r["status"] for r in outcome["results"]] == [DELIVERED, DELIVERED]
        assert len(effects.dispatches) == 1
        assert len(effects.replies) == 1

    def test_stop_during_multi_item_list_suspends_only_unstarted_items(self) -> None:
        runtime = FakeRuntime()
        effects = FakeEffects()
        kernel = make_kernel(effects, runtime)
        original_dispatch = effects.dispatch

        def dispatch_that_stops(payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
            if str(ctx.get("idempotency_key") or "") == "k1":
                kernel.stop(GOAL, mode="immediate")
            return original_dispatch(payload, ctx=ctx)

        effects.dispatch = dispatch_that_stops  # type: ignore[method-assign]
        kernel.submit(GOAL, make_request("A"))
        call = kernel.next_goal_call(GOAL)
        assert call is not None
        result = kernel.finish_goal_call(
            GOAL, call["call_id"], {"actions": [dispatch_action("k1"), dispatch_action("k2")]}
        )
        assert result["suspended"] is True
        # k1 landed before the stop; k2 was suspended without running.
        assert [d["ctx"]["idempotency_key"] for d in effects.dispatches] == ["k1"]

        kernel.resume(GOAL)
        nxt = kernel.next_goal_call(GOAL)
        assert nxt is not None and nxt["resume"] is True
        outcome = kernel.finish_goal_call(GOAL, nxt["call_id"], nxt["stop_list"])
        statuses = {r["idempotency_key"]: r for r in outcome["results"]}
        assert statuses["k1"]["status"] == DELIVERED
        assert statuses["k1"]["reconciled"] is True  # not re-run on resume
        assert statuses["k2"]["status"] == DELIVERED
        assert [d["ctx"]["idempotency_key"] for d in effects.dispatches] == ["k1", "k2"]


# --- final review rf-3c65b36f: stop is serialized with effect admission ----------


class TestStopSerializedWithEffectAdmission:
    @pytest.mark.parametrize("kind", ["approve", "reject"])
    def test_confirmed_stop_lands_between_mode_check_and_review_delivery(self, kind: str) -> None:
        # An approve/reject can pass the loop-top mode check, then park acquiring
        # ``_version_lock`` while a confirmed immediate stop persists
        # ``MODE_STOPPED`` -- and then deliver to the external port after the
        # stop, with no resume. The fix re-checks ``MODE_STOPPED`` under the
        # per-goal admission lock at the delivery boundary; holding
        # ``_version_lock`` here parks the review in exactly that window.
        import threading
        import time

        runtime = FakeRuntime()  # stop_answer defaults to terminated=True
        effects = FakeEffects()
        kernel = make_kernel(effects, runtime)  # active version v1

        action = {
            "kind": kind,
            "idempotency_key": f"{kind}-1",
            "payload": {"development_id": "d1", "verdict": "APPROVE", "goal_version": "v1"},
        }
        kernel.submit(GOAL, make_request("A"))
        call = kernel.next_goal_call(GOAL)
        assert call is not None

        box: dict[str, Any] = {}

        def deliver() -> None:
            box["result"] = kernel.finish_goal_call(GOAL, call["call_id"], {"actions": [action]})

        kernel._version_lock.acquire()  # park the review before its port admission
        worker = threading.Thread(target=deliver)
        worker.start()
        try:
            time.sleep(0.1)  # the review is now parked on ``_version_lock``
            assert effects.approvals == [] and effects.rejects == []  # nothing yet
            resp = kernel.stop(GOAL, mode="immediate")
            assert resp["stopped"] is True
        finally:
            kernel._version_lock.release()

        worker.join(timeout=30)
        assert not worker.is_alive()
        result = box["result"]

        # The confirmed stop won: the review was suspended, never delivered.
        assert result["suspended"] is True
        assert effects.approvals == [] and effects.rejects == []
        assert kernel.next_goal_call(GOAL) is None  # fence held / stopped

        # On resume the preserved action is delivered exactly once.
        kernel.resume(GOAL)
        nxt = kernel.next_goal_call(GOAL)
        assert nxt is not None and nxt["resume"] is True
        outcome = kernel.finish_goal_call(GOAL, nxt["call_id"], nxt["stop_list"])
        assert [r["status"] for r in outcome["results"]] == [DELIVERED]
        if kind == "approve":
            assert len(effects.approvals) == 1
        else:
            assert len(effects.rejects) == 1


# --- finding 3: crash-safe incomplete-tail repair preserves the valid prefix -----


class TestCrashSafeRepair:
    def test_repair_interruption_preserves_the_acknowledged_prefix(
        self, tmp_path, monkeypatch
    ) -> None:
        home = tmp_path / "journal"
        kernel = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        kernel.activate_version(GOAL, "v1")
        kernel.submit(GOAL, make_request("A"))
        path = home / "goal-wf-1.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"record": "request", "goal": "wf-1", "requ')
        original = path.read_text()

        import fleet_graph.goal.request_kernel as rk

        real_replace = rk.os.replace

        def fail_replace(src: str, dst: str) -> None:
            raise OSError("simulated crash during atomic replace")

        monkeypatch.setattr(rk.os, "replace", fail_replace)
        with pytest.raises(OSError):
            GoalRequestKernel(journal=Journal(home=home))

        # The original journal (valid prefix + fragment) was never truncated in
        # place; a retry re-runs the same repair.
        assert path.read_text() == original

        monkeypatch.setattr(rk.os, "replace", real_replace)
        rebuilt = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        requests = [e for e in rebuilt.list_events(GOAL)["events"] if e["record"] == RECORD_REQUEST]
        assert [r["request_id"] for r in requests] == ["A"]


# --- finding: version-bound review reads the live version at the effect boundary ---


class TestLiveVersionAtReviewBoundary:
    def test_a_mid_list_version_activation_refuses_a_later_stale_review(self) -> None:
        effects = FakeEffects()
        kernel = make_kernel(effects)
        kernel.activate_version(GOAL, "v1")

        def activate_v2_and_dispatch(payload: dict[str, Any], *, ctx: Any) -> dict[str, Any]:
            # An explicit version event lands *while this list is executing*.
            kernel.activate_version(GOAL, "v2")
            return {"ok": True}

        effects.dispatch = activate_v2_and_dispatch  # type: ignore[method-assign]
        approve_at_v1 = {
            "kind": "approve",
            "idempotency_key": "ap1",
            "payload": {"development_id": "d1", "verdict": "APPROVE", "goal_version": "v1"},
        }
        result = run_one(
            kernel,
            make_request("A"),
            {"actions": [dispatch_action("k1"), approve_at_v1]},
        )
        by_kind = {r["kind"]: r for r in result["results"]}
        assert by_kind["dispatch"]["status"] == DELIVERED
        # The later approve bound to v1 is refused against the *current* v2,
        # not the pre-list snapshot that cached v1.
        assert by_kind["approve"]["status"] == FAILED
        assert "stale_version" in by_kind["approve"]["detail"]
        assert effects.approvals == []  # the stale review never reached the gate

    def test_concurrent_version_activation_is_excluded_from_review_delivery(self) -> None:
        # The race the final review found: ``finish_goal_call`` reads the active
        # version, then does journal operations before invoking the external
        # approve/reject port. If ``activate_version`` lands in that gap, a
        # review naming the then-stale version reaches the port after activation.
        # The fix serializes the two under a per-kernel version lock; this
        # barrier-controlled test pins that a review admitted for v1 cannot
        # complete its delivery around a concurrent v2 activation.
        import threading
        import time

        effects = FakeEffects()
        kernel = make_kernel(effects)  # active version v1

        port_reached = threading.Barrier(2)
        port_release = threading.Event()
        observed_at_port: list[str] = []
        original_approve = effects.approve

        def blocking_approve(payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
            # Capture the active version at the moment the review actually
            # reaches the external gate, then park here so the main thread can
            # attempt a concurrent activation while delivery is in progress.
            observed_at_port.append(kernel.active_version(GOAL))
            port_reached.wait(timeout=30)
            port_release.wait(timeout=30)
            return original_approve(payload, ctx=ctx)

        effects.approve = blocking_approve  # type: ignore[method-assign]

        approve_at_v1 = {
            "kind": "approve",
            "idempotency_key": "ap1",
            "payload": {"development_id": "d1", "verdict": "APPROVE", "goal_version": "v1"},
        }
        kernel.submit(GOAL, make_request("A"))
        call = kernel.next_goal_call(GOAL)
        assert call is not None

        box: dict[str, Any] = {}

        def deliver() -> None:
            box["result"] = kernel.finish_goal_call(
                GOAL, call["call_id"], {"actions": [approve_at_v1]}
            )

        deliver_thread = threading.Thread(target=deliver)
        deliver_thread.start()
        # The review has been admitted for v1 and is inside the approve port,
        # still inside the version-lock critical section.
        port_reached.wait(timeout=30)

        activation_done = threading.Event()

        def activate() -> None:
            kernel.activate_version(GOAL, "v2")
            activation_done.set()

        activate_thread = threading.Thread(target=activate)
        activate_thread.start()

        # With serialization, activation must stay parked while the review
        # delivery is in flight. Without the lock it would land here and the v1
        # review would then complete its delivery after v2 became active.
        time.sleep(0.3)
        assert not activation_done.is_set(), "version activation landed during review admission"

        port_release.set()
        deliver_thread.join(timeout=30)
        activate_thread.join(timeout=30)
        assert not deliver_thread.is_alive()
        assert not activate_thread.is_alive()

        # The v1 review was delivered while v1 was still active; activation
        # finished only after delivery released the critical section.
        assert observed_at_port == ["v1"]
        assert box["result"]["results"][0]["status"] == DELIVERED
        assert len(effects.approvals) == 1
        assert activation_done.is_set()
        assert kernel.active_version(GOAL) == "v2"


# --- request-transition atomicity (final review rf-49b88ef6) -------------------


class TestRequestTransitionAtomicity:
    def test_concurrent_duplicate_submission_is_deduplicated(self) -> None:
        # The final review's race: two concurrent ``submit`` callers of the same
        # identity can both pass ``_find_request`` before either appends,
        # creating duplicate queued requests and Goal calls. The fix serializes
        # the dedup -> append -> enqueue transition under ``_request_lock``; the
        # slice here widens the window so the regression is deterministic.
        import threading
        import time

        kernel = make_kernel()
        real_append = kernel.journal.append

        def slow_request_append(goal: str, record: dict[str, Any]) -> dict[str, Any]:
            if record.get("record") == RECORD_REQUEST:
                time.sleep(0.05)  # widen find -> append
            return real_append(goal, record)

        kernel.journal.append = slow_request_append  # type: ignore[method-assign]

        request = make_request("A")
        outcomes: list[dict[str, Any]] = []
        barrier = threading.Barrier(2)

        def submit_once() -> None:
            barrier.wait()
            outcomes.append(kernel.submit(GOAL, request))

        t1 = threading.Thread(target=submit_once)
        t2 = threading.Thread(target=submit_once)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # exactly one acceptance, exactly one duplicate ack, exactly one record
        assert sorted(o["duplicate"] for o in outcomes) == [False, True]
        requests = [
            e for e in kernel.list_events(GOAL)["events"] if e["record"] == RECORD_REQUEST
        ]
        assert [r["request_id"] for r in requests] == ["A"]

    def test_concurrent_distinct_submissions_keep_journal_and_queue_order(self) -> None:
        # The final review's race: distinct submissions can append A then B
        # durably but enqueue B then A. Serialized acceptance keeps the queue in
        # durable journal order, so the drain order equals the record order.
        import threading
        import time

        kernel = make_kernel()
        real_append = kernel.journal.append

        def slow_request_append(goal: str, record: dict[str, Any]) -> dict[str, Any]:
            if record.get("record") == RECORD_REQUEST:
                time.sleep(0.05)  # widen append -> enqueue
            return real_append(goal, record)

        kernel.journal.append = slow_request_append  # type: ignore[method-assign]

        barrier = threading.Barrier(2)

        def submit_a() -> None:
            barrier.wait()
            kernel.submit(GOAL, make_request("A"))

        def submit_b() -> None:
            barrier.wait()
            kernel.submit(GOAL, make_request("B"))

        ta = threading.Thread(target=submit_a)
        tb = threading.Thread(target=submit_b)
        ta.start()
        tb.start()
        ta.join()
        tb.join()

        journal_order = [
            e["request_id"] for e in kernel.list_events(GOAL)["events"] if e["record"] == RECORD_REQUEST
        ]
        assert sorted(journal_order) == ["A", "B"]

        served: list[str] = []
        while True:
            nxt = kernel.next_goal_call(GOAL)
            if nxt is None:
                break
            served.append(str(nxt["request_id"]))
            kernel.finish_goal_call(GOAL, nxt["call_id"], {"actions": []})

        # every accepted request is served exactly once, in durable journal
        # order -- never a queue that diverged from the append order.
        assert served == journal_order

    def test_concurrent_next_goal_call_claims_exactly_once_without_losing_a_request(
        self,
    ) -> None:
        # The final review's race: two ``next_goal_call`` callers both pass the
        # inflight check and pop separate requests before ``claim_call``; the
        # loser raises after removing its request, leaving it unserved until
        # reconstruction. The fix serializes the fence/mode/queue admission so
        # exactly one caller claims and no request is dropped.
        import threading
        import time

        kernel = make_kernel()
        kernel.submit(GOAL, make_request("A"))
        kernel.submit(GOAL, make_request("B"))

        real_inflight = kernel.journal.inflight

        def slow_inflight(goal: str) -> str | None:
            time.sleep(0.05)  # widen the check -> pop window
            return real_inflight(goal)

        kernel.journal.inflight = slow_inflight  # type: ignore[method-assign]

        calls: list[dict[str, Any] | None] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def open_call() -> None:
            barrier.wait()
            try:
                calls.append(kernel.next_goal_call(GOAL))
            except Exception as exc:  # a lost claim must not surface as a raise
                errors.append(exc)

        t1 = threading.Thread(target=open_call)
        t2 = threading.Thread(target=open_call)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors
        winners = [c for c in calls if c is not None]
        assert len(winners) == 1
        winner = winners[0]
        assert winner["request_id"] == "A"

        # Finishing the winner releases the fence; the other queued request
        # (the one a racing loser would have popped and dropped) is still
        # servable, in order.
        kernel.finish_goal_call(GOAL, winner["call_id"], {"actions": []})
        nxt = kernel.next_goal_call(GOAL)
        assert nxt is not None and nxt["request_id"] == "B"


# --- resumed-execution ownership (final review rf-25eeb86e) --------------------


class TestResumedExecutionOwnership:
    def _interrupted_dispatch(self, tmp_path) -> tuple[str, Any]:
        """Build a durable journal holding an interrupted two-item dispatch: its
        Stop List was validated and persisted, k1 was delivered + confirmed, and
        k2 crashed with only its intent written. Returns ``(call_id, home)``."""
        home = tmp_path / "journal"
        effects = CrashEffects(crash_on={"k2"})
        kernel = GoalRequestKernel(journal=Journal(home=home), effects=effects)
        kernel.activate_version(GOAL, "v1")
        kernel.submit(GOAL, make_request("A"))
        call = kernel.next_goal_call(GOAL)
        assert call is not None and call["request_id"] == "A"
        with pytest.raises(KeyboardInterrupt):
            kernel.finish_goal_call(
                GOAL, call["call_id"], {"actions": [dispatch_action("k1"), dispatch_action("k2")]}
            )
        return call["call_id"], home

    def test_concurrent_next_goal_call_admits_a_resume_exactly_once(self, tmp_path) -> None:
        # Offline reconstruction: a rebuilt product re-derives exactly one
        # pending-resume from the durable journal and re-acquires the fence for
        # that call. Two concurrent callers must not both receive the interrupted
        # call -- the loser observes no pending resume and the held fence, and
        # gets ``None`` instead of re-executing the same call (rf-25eeb86e).
        import threading

        call_id, home = self._interrupted_dispatch(tmp_path)
        rebuilt = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())

        calls: list[dict[str, Any] | None] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def open_call() -> None:
            barrier.wait()
            try:
                calls.append(rebuilt.next_goal_call(GOAL))
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=open_call)
        t2 = threading.Thread(target=open_call)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors
        winners = [c for c in calls if c is not None]
        assert len(winners) == 1
        assert winners[0]["resume"] is True
        assert winners[0]["call_id"] == call_id
        # the losing caller observed the fence, never the interrupted call
        assert calls.count(None) == 1

    def test_concurrent_resume_execution_runs_effects_once_and_releases_the_fence_once(
        self, tmp_path
    ) -> None:
        # Even if a duplicated resume envelope reached two executors, only one
        # may own execution: the other neither runs an effect nor releases the
        # fence a second time (rf-25eeb86e). The owner is parked inside its one
        # outstanding delivery so the loser deterministically observes the held
        # execution token rather than racing to claim after the owner finished.
        import threading
        import time

        call_id, home = self._interrupted_dispatch(tmp_path)
        effects2 = FakeEffects()
        rebuilt = GoalRequestKernel(journal=Journal(home=home), effects=effects2)
        nxt = rebuilt.next_goal_call(GOAL)
        assert nxt is not None and nxt["resume"] is True and nxt["call_id"] == call_id

        in_delivery = threading.Barrier(2)
        release = threading.Event()
        real_dispatch = effects2.dispatch

        def blocking_dispatch(payload: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
            # Park here *before* delegating to the recording dispatch so only the
            # owner's single delivery is recorded (delegation appends to
            # ``effects2.dispatches`` exactly once per delivery).
            in_delivery.wait(timeout=30)
            release.wait(timeout=30)
            return real_dispatch(payload, ctx=ctx)

        effects2.dispatch = blocking_dispatch  # type: ignore[method-assign]

        results: list[dict[str, Any]] = []
        errors: list[Exception] = []
        start = threading.Barrier(2)

        def finish() -> None:
            start.wait()
            try:
                results.append(rebuilt.finish_goal_call(GOAL, call_id, nxt["stop_list"]))
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=finish)
        t2 = threading.Thread(target=finish)
        t1.start()
        t2.start()

        # The owner reaches k2's dispatch and parks on ``in_delivery``; the loser
        # then fails ``_claim_execution`` and returns ``executing_elsewhere``
        # without touching an effect or the fence.
        in_delivery.wait(timeout=30)
        deadline = time.time() + 10
        while sum(t.is_alive() for t in (t1, t2)) > 1 and time.time() < deadline:
            time.sleep(0.005)

        # Exactly one thread (the winner) remains parked inside the effect; the
        # loser has already returned its busy result before we release it.
        assert sum(t.is_alive() for t in (t1, t2)) == 1
        busy = [r for r in results if r.get("executing_elsewhere")]
        owners = [r for r in results if not r.get("executing_elsewhere")]
        assert len(busy) == 1
        assert len(owners) == 0

        release.set()
        t1.join(timeout=30)
        t2.join(timeout=30)
        assert not t1.is_alive()
        assert not t2.is_alive()
        assert not errors  # no AssertionError from a second fence release

        owners = [r for r in results if not r.get("executing_elsewhere")]
        busy = [r for r in results if r.get("executing_elsewhere")]
        assert len(owners) == 1
        assert len(busy) == 1

        # The outstanding k2 was delivered exactly once (k1 was reconciled from
        # its pre-crash confirmed delivery, never re-run).
        assert [d["ctx"]["idempotency_key"] for d in effects2.dispatches] == ["k2"]
        # The fence was released exactly once.
        assert rebuilt.journal.inflight(GOAL) is None


# --- finding: complete raw Goal response survives interruption (lossless events) ---


class TestRawResponsePreservation:
    def test_interrupted_call_preserves_malformed_entries_and_raw_intent(
        self, tmp_path
    ) -> None:
        home = tmp_path / "journal"
        effects = CrashEffects(crash_on={"k2"})
        kernel = GoalRequestKernel(journal=Journal(home=home), effects=effects)
        kernel.activate_version(GOAL, "v1")
        kernel.submit(GOAL, make_request("A"))
        call = kernel.next_goal_call(GOAL)
        assert call is not None and call["request_id"] == "A"

        raw = {
            "actions": [
                {"kind": "bogus", "payload": {"x": 1}, "idempotency_key": "bad"},
                dispatch_action("k1"),
                dispatch_action("k2"),
            ],
            "intent": "mystery",  # an unsupported intent value
        }
        with pytest.raises(KeyboardInterrupt):
            kernel.finish_goal_call(GOAL, call["call_id"], raw)

        # The stop list -- durable *before* any effect -- retains the complete
        # raw response, not a normalized projection.
        durable = Journal(home=home)
        stop_lists = [
            line
            for line in durable.scan(GOAL)
            if line.get("record") == RECORD_STOP_LIST and line.get("call_id") == call["call_id"]
        ]
        assert len(stop_lists) == 1
        sl = stop_lists[0]
        assert sl["raw_intent"] == "mystery"  # the unsupported intent survives
        assert sl["raw_actions"] == raw["actions"]  # malformed entry retained verbatim
        # routable actions keep their *original* list positions (2 and 3), never
        # rewritten by the normalization that dropped the interleaved malformed
        # entry at position 1.
        assert [a["idempotency_key"] for a in sl["actions"]] == ["k1", "k2"]
        assert [a["list_index"] for a in sl["actions"]] == [2, 3]
        assert [(r["idempotency_key"], r.get("list_index")) for r in sl["malformed"]] == [
            ("bad", 1)
        ]

        # Resume: pagination at the resumed call still exposes the raw outcome,
        # not the reduced list reconstructed from the normalized projection.
        effects2 = FakeEffects()
        rebuilt = GoalRequestKernel(journal=Journal(home=home), effects=effects2)
        nxt = rebuilt.next_goal_call(GOAL)
        assert nxt is not None and nxt["resume"] is True
        rebuilt.finish_goal_call(GOAL, nxt["call_id"], nxt["stop_list"])

        call_results = [
            e
            for e in rebuilt.list_events(GOAL)["events"]
            if e.get("record") == RECORD_CALL_RESULT and e.get("call_id") == call["call_id"]
        ]
        assert len(call_results) == 1
        assert call_results[0]["raw_intent"] == "mystery"
        assert call_results[0]["actions"] == raw["actions"]


# --- final review: an unavailable Goal-call port must not consume requests -----


class TestUnboundGoalCallPreservesRequests:
    def test_requests_survive_an_unbound_port_and_deliver_once_in_order(self, tmp_path) -> None:
        home = tmp_path / "journal"
        kernel = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        kernel.activate_version(GOAL, "v1")
        coord = KernelCoordinator(kernel=kernel, goal_call=None, folder_id=GOAL)

        inbox = [
            {"message_id": "m-A", "from_agent_id": "line-a", "body": "A"},
            {"message_id": "m-B", "from_agent_id": "line-b", "body": "B"},
            {"message_id": "m-C", "from_agent_id": "line-c", "body": "C"},
        ]
        verdict = coord.turn(1, {"folder_id": GOAL, "inbox_messages": inbox})
        assert verdict["verdict"] == "blocked"
        assert verdict["reason"] == "goal_call_unwired"

        events = kernel.list_events(GOAL)["events"]
        # Nothing was fabricated into a served request: no Stop List, no
        # completed call result -- and the capability failure is recorded.
        assert not [e for e in events if e["record"] == RECORD_STOP_LIST]
        assert not [e for e in events if e["record"] == RECORD_CALL_RESULT]
        assert any(e["record"] == RECORD_CALL_UNAVAILABLE for e in events)

        # Reconstruct a fresh product and bind a fake Goal port: every original
        # request is then delivered exactly once, oldest first.
        class RecordingGoalCall:
            def __init__(self) -> None:
                self.request_ids: list[str] = []

            def call(self, goal: str, prompt: dict[str, Any]) -> dict[str, Any]:
                self.request_ids.append(str(prompt.get("request_id") or ""))
                return {"actions": [], "intent": "done"}

        port = RecordingGoalCall()
        rebuilt = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        while True:
            nxt = rebuilt.next_goal_call(GOAL)
            if nxt is None:
                break
            rebuilt.finish_goal_call(
                GOAL, nxt["call_id"], port.call(GOAL, {"request_id": nxt["request_id"]})
            )

        assert port.request_ids == [
            f"line:{GOAL}:message:m-A",
            f"line:{GOAL}:message:m-B",
            f"line:{GOAL}:message:m-C",
        ]


# --- final review: completion + lifecycle transition recover as one operation ---


class TestCompletionModeRecovery:
    def _crash_window(self, home: Any, *, pre_mode: str, intent: str | None) -> str:
        """Write a durable journal capturing the *finished* call result with its
        resulting lifecycle-mode record still missing -- the window a crash
        between the completion record and the mode persistence used to leave
        open. Returns the call id."""
        journal = Journal(home=home)
        journal.append(GOAL, {"record": RECORD_VERSION, "goal": GOAL, "version": "v1", "at": "t"})
        journal.append(
            GOAL,
            {
                "record": RECORD_REQUEST,
                "goal": GOAL,
                "request_id": "A",
                "caller": "line-a",
                "kind": KIND_DD_RESULT,
                "input": {"note": "A"},
                "goal_version": "v1",
                "accepted_at": "t",
            },
        )
        call_id = "call:crash"
        journal.append(
            GOAL,
            {
                "record": RECORD_CALL,
                "goal": GOAL,
                "call_id": call_id,
                "request_id": "A",
                "run_id": "run-A",
                "caller": "line-a",
                "goal_version": "v1",
                "at": "t",
            },
        )
        journal.append(
            GOAL,
            {"record": RECORD_CONTROL, "goal": GOAL, "control": "mode", "value": pre_mode, "at": "t"},
        )
        journal.append(
            GOAL,
            {
                "record": RECORD_STOP_LIST,
                "goal": GOAL,
                "call_id": call_id,
                "request_id": "A",
                "run_id": "run-A",
                "intent": intent,
                "actions": [],
                "malformed": [],
                "at": "t",
            },
        )
        # The completed call result is durable, but the separately-persisted
        # resulting mode record never landed (the crash window).
        journal.append(
            GOAL,
            {
                "record": RECORD_CALL_RESULT,
                "goal": GOAL,
                "call_id": call_id,
                "request_id": "A",
                "run_id": "run-A",
                "intent": intent,
                "actions": [],
                "at": "t",
            },
        )
        return call_id

    def test_graceful_stop_drain_reconstructs_as_stopped_not_stuck(self, tmp_path) -> None:
        # A graceful stop drained the in-flight call; the completed result is
        # durable but the resulting ``stopped`` mode record is missing. Restore
        # must derive the drained stop instead of leaving the goal stuck in
        # ``stopping`` with no in-flight call and no pending transition.
        home = tmp_path / "journal"
        self._crash_window(home, pre_mode=MODE_STOPPING, intent="done")
        rebuilt = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())

        assert rebuilt._state[GOAL]["mode"] == MODE_STOPPED
        assert rebuilt._state[GOAL]["mode"] != MODE_STOPPING
        assert rebuilt.journal.inflight(GOAL) is None
        assert rebuilt.next_goal_call(GOAL) is None  # stopped: nothing starts
        rebuilt.resume(GOAL)
        assert rebuilt._state[GOAL]["mode"] == MODE_RUNNING

    @pytest.mark.parametrize(
        ("intent", "expected_mode"),
        [(None, MODE_RUNNING), ("waiting", MODE_WAITING), ("blocked", MODE_BLOCKED)],
    )
    def test_terminal_intent_reconstructs_when_mode_record_is_missing(
        self, tmp_path, intent: str | None, expected_mode: str
    ) -> None:
        # A completed call whose terminal-intent mode record never landed must
        # still reconstruct the intent's lifecycle mode -- the completed call
        # result carries it, and a missing record must not leave the goal
        # ``running`` when the Goal meant waiting/blocked.
        home = tmp_path / "journal"
        self._crash_window(home, pre_mode=MODE_RUNNING, intent=intent)
        rebuilt = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        assert rebuilt._state[GOAL]["mode"] == expected_mode

    def test_live_path_persists_call_result_and_mode_together(self, tmp_path) -> None:
        # The live path no longer leaves the crash window open: the completed
        # call result and the resulting ``stopped`` mode land as one atomic
        # append, so a fresh reconstruction from disk sees one consistent
        # completion and never needs to guess the transition.
        home = tmp_path / "journal"
        kernel = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        kernel.activate_version(GOAL, "v1")
        kernel.submit(GOAL, make_request("A"))
        call = kernel.next_goal_call(GOAL)
        assert call is not None
        kernel.stop(GOAL, mode="graceful")
        kernel.finish_goal_call(
            GOAL, call["call_id"], {"actions": [dispatch_action("k1")], "intent": "waiting"}
        )

        events = kernel.list_events(GOAL)["events"]
        mode_values = [
            e["value"]
            for e in events
            if e.get("record") == RECORD_CONTROL and e.get("control") == "mode"
        ]
        assert mode_values[-1] == MODE_STOPPED
        assert any(e.get("record") == RECORD_CALL_RESULT for e in events)

        rebuilt = GoalRequestKernel(journal=Journal(home=home), effects=FakeEffects())
        assert rebuilt._state[GOAL]["mode"] == MODE_STOPPED
        assert rebuilt.journal.inflight(GOAL) is None


# --- final review: completion must not overwrite a concurrent stop ----------


class _PausingJournal(Journal):
    """A journal whose ``append_many`` pauses at a barrier.

    The completion tail calls ``append_many`` *after* it has read the current
    lifecycle mode but *before* it releases the fence. Pausing there holds the
    exact lost-stop window open -- mode already read, fence still held -- so a
    test can issue a concurrent stop and assert it is not overwritten."""

    def __init__(self) -> None:
        super().__init__()
        self.at_append_many = threading.Event()
        self.proceed = threading.Event()

    def append_many(self, goal: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.at_append_many.set()
        self.proceed.wait(timeout=10)
        return super().append_many(goal, records)


class TestStopNotLostByConcurrentCompletion:
    def _concurrent_completion_and_stop(
        self, mode: str, runtime: FakeRuntime | None
    ) -> None:
        journal = _PausingJournal()
        kernel = GoalRequestKernel(
            journal=journal, effects=FakeEffects(), runtime=runtime
        )
        kernel.activate_version(GOAL, "v1")
        kernel.submit(GOAL, make_request("A"))
        call = kernel.next_goal_call(GOAL)
        assert call is not None

        def finish() -> None:
            kernel.finish_goal_call(GOAL, call["call_id"], {"actions": []})

        def stop_it() -> None:
            kernel.stop(GOAL, mode=mode)

        worker = threading.Thread(target=finish)
        worker.start()
        # Hold the completion tail open: mode already read, fence still held.
        assert journal.at_append_many.wait(timeout=5)
        stopper = threading.Thread(target=stop_it)
        stopper.start()
        # Let the completion tail finish. A correct kernel serializes the stop
        # with the completion tail, so the stop either drains this in-flight
        # call or lands after the fence is released -- never overwritten by the
        # stale mode read.
        journal.proceed.set()
        worker.join(timeout=10)
        stopper.join(timeout=10)
        assert not worker.is_alive()
        assert not stopper.is_alive()

        # The stop is authoritative regardless of interleaving: the goal rests
        # stopped and no queued request can start without resume.
        assert kernel._state[GOAL]["mode"] == MODE_STOPPED
        assert kernel.journal.inflight(GOAL) is None
        assert kernel.next_goal_call(GOAL) is None

    def test_graceful_stop_is_not_lost_by_concurrent_completion(self) -> None:
        self._concurrent_completion_and_stop("graceful", runtime=None)

    def test_confirmed_immediate_stop_is_not_lost_by_concurrent_completion(self) -> None:
        self._concurrent_completion_and_stop("immediate", runtime=FakeRuntime())
