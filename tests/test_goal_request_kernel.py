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

from typing import Any

from fleet_graph.goal.request_kernel import (
    ACTION_DISPATCH,
    ACTION_REPLY,
    DELIVERED,
    FAILED,
    KIND_DD_RESULT,
    KIND_DD_REVIEW,
    KIND_MESSAGE,
    KIND_STEER,
    NOT_READY,
    OBSERVED_ABSENT,
    OBSERVED_CONFIRMED,
    OBSERVED_UNKNOWN,
    RECORD_ACTION,
    RECORD_ACTION_RESULT,
    RECORD_CALL_RESULT,
    RECORD_REQUEST,
    UNKNOWN,
    GoalRequestKernel,
    Journal,
    Request,
    build_goal_prompt,
    business_guards,
    validate_stop_list,
)

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
