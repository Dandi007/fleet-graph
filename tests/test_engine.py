"""业务状态机纯 fake 单元测试；不执行真实新引擎、平台或命令。"""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from fleet_graph.engine import Engine
from fleet_graph.protocol import validate_actions
from fleet_graph.store import Store

A, B = "a" * 40, "b" * 40
REPO = {"path": "/repo", "remote": "origin", "target_branch": "main", "acceptance": ["test"]}
PASS = {"type": "pass", "summary": "通过", "evidence_refs": ["test:1"]}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path)
    store.change(
        "init",
        {},
        lambda s: s.update(
            goal={
                "goal_id": "g",
                "version": 1,
                "source_branch": "release/g",
                "work_folder": "wf-test",
            },
            status="active",
            repos={"r": REPO},
        ),
    )
    runtime = Mock()
    runtime.start = AsyncMock(side_effect=lambda run_id, *args: {"run_id": run_id})
    runtime.inspect = AsyncMock(return_value={"status": "running"})
    commands = Mock()
    commands.start = AsyncMock(side_effect=lambda run_id, *args: {"run_id": run_id})
    commands.inspect = AsyncMock(return_value={"status": "running"})
    git = Mock()
    git.verify_handoff.return_value = {"head": A}
    git.ensure_pr.return_value = {"number": 1}
    git.merge.return_value = {"status": "merged", "head": A}
    git.cleanup.return_value = {"status": "cleaned"}
    git.inspect_refs.return_value = {"source_head": A, "target_head": B}
    return Engine(store, runtime, git, commands, capacity=3, scribe_interval=0)


def add_dd(engine, dd_id="d", step="impl", **extra):
    dd = {
        "dd_id": dd_id,
        "repo_ref": "r",
        "source_branch": f"dev/{dd_id}",
        "target_branch": "release/g",
        "worktree": f"/wt/{dd_id}",
        "spec_path": "SPEC.md",
        "step": step,
        "head": A,
        "goal_version": 1,
        "input_version": 1,
        "pr": {"number": 1},
        "current": {},
        "turn": 0,
        **extra,
    }
    engine.store.change("dd", {}, lambda s: s["dds"].update({dd_id: dd}))
    return dd


def collected(engine, role, result, owner="d", **extra):
    record = {
        "role": role,
        "owner": owner,
        "status": "collected",
        "result": result,
        "goal_version": 1,
        "input_version": 1,
        **extra,
    }
    engine.store.change("run", {}, lambda s: s["runs"].update({"run": record}))
    return record


def test_requests_remain_distinct_one_goal_call_many_dds(setup):
    e = setup
    e.request("human", "q1", {"message": "一"})
    e.request("human", "q2", {"message": "二"})
    add_dd(e, "a")
    add_dd(e, "b")
    run(e.schedule())
    run(e.schedule())
    state = e.store.read()
    assert state["requests"]["q1"]["status"] == "running"
    assert state["requests"]["q2"]["status"] == "pending"
    assert len([r for r in state["runs"].values() if r["role"] == "goal"]) == 1
    assert len([r for r in state["runs"].values() if r["role"] == "impl"]) == 2
    prompt = e.runtime.start.call_args_list[0].args[3]
    assert prompt["current"] == {"message": "一"}


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_baseline_is_recorded_and_impl_follows_even_failure(setup, status):
    e = setup
    add_dd(e, step="baseline_running")
    record = collected(e, "acceptance", {"status": status, "evidence": "baseline"})
    run(e.finish_run("run", record))
    dd = e.store.read()["dds"]["d"]
    assert dd["step"] == "impl" and dd["baseline"]["status"] == status
    assert dd["head"] is None


def test_impl_needs_goal_does_not_require_clean_handoff(setup):
    e = setup
    add_dd(e, step="impl_running")
    e.git.verify_handoff.side_effect = RuntimeError("dirty")
    record = collected(
        e,
        "impl",
        {
            "status": "succeeded",
            "output": {"type": "needs_goal", "message": "需要取舍", "evidence_refs": ["spec:1"]},
        },
    )
    run(e.finish_run("run", record))
    assert e.store.read()["dds"]["d"]["step"] == "needs_goal"
    assert len(e.store.read()["requests"]) == 1
    e.git.verify_handoff.assert_not_called()


@pytest.mark.parametrize("role", ["cr", "fr", "acceptance"])
def test_changed_head_invalidates_all_review_evidence(setup, role):
    e = setup
    add_dd(e, step=f"{role}_running", acceptance={"status": "succeeded"}, cr={"head": A})
    e.git.verify_handoff.return_value = {"head": B}
    record = collected(e, role, {"status": "succeeded", "output": PASS})
    run(e.finish_run("run", record))
    dd = e.store.read()["dds"]["d"]
    assert dd["step"] == "impl"
    assert dd["acceptance"] is None and dd["cr"] is None and dd["head"] is None


def test_goal_version_changed_discards_stale_actions_without_losing_request(setup):
    e = setup
    e.request("human", "q1", {"message": "原请求"})
    e.store.change("version", {}, lambda s: s["goal"].update(version=2))
    record = collected(
        e,
        "goal",
        {"status": "succeeded", "output": [{"type": "waiting", "summary": "旧决策"}]},
        owner="q1",
    )
    run(e.finish_run("run", record))
    assert e.store.read()["requests"]["q1"]["status"] == "pending"
    assert e.store.read()["actions"] == {}
    run(e.schedule())
    prompt = e.runtime.start.call_args.args[3]
    assert prompt["goal_version"] == 2 and prompt["requested_goal_version"] == 1


def test_dd_goal_version_changed_requires_new_impl(setup):
    e = setup
    add_dd(e, step="fr_running")
    e.store.change("version", {}, lambda s: s["goal"].update(version=2))
    record = collected(e, "fr", {"status": "succeeded", "output": PASS})
    run(e.finish_run("run", record))
    dd = e.store.read()["dds"]["d"]
    assert dd["step"] == "impl" and dd["goal_version"] == 2


def test_finish_fold_crash_rolls_back_and_replays_exactly_once(setup):
    e = setup
    add_dd(e, step="fr_running")
    record = collected(e, "fr", {"status": "succeeded", "output": PASS})
    original = e.store.change

    def crash(kind, *args):
        if kind == "run.finished":
            raise KeyboardInterrupt("模拟提交前崩溃")
        return original(kind, *args)

    e.store.change = crash
    with pytest.raises(KeyboardInterrupt):
        run(e.finish_run("run", record))
    state = e.store.read()
    assert state["dds"]["d"]["step"] == "fr_running"
    assert state["runs"]["run"]["status"] == "collected"
    assert state["requests"] == {}
    e.store.change = original
    run(e.collect())
    run(e.collect())
    state = e.store.read()
    assert state["dds"]["d"]["step"] == "review"
    assert len(state["requests"]) == 1
    assert (
        len([ev for ev in e.store.events()["events"] if ev["kind"] == "dd.review_requested"]) == 1
    )


def test_revise_action_replay_does_not_increment_input_twice(setup):
    e = setup
    add_dd(e, step="needs_goal")
    record = {"action": {"type": "revise", "dd_id": "d", "message": "改为方案 B"}}
    run(e.execute_action("a", record))
    run(e.execute_action("a", record))
    assert e.store.read()["dds"]["d"]["input_version"] == 2


def test_mailbox_reply_replay_emits_one_delivery(setup):
    e = setup
    e.request("human", "q", {"message": "问"}, {"kind": "human", "id": "u"}, {"id": "u"})
    record = {"action": {"type": "reply", "in_reply_to": "q", "text": "答"}}
    assert run(e.execute_action("a", record)) == run(e.execute_action("a", record))
    assert len([ev for ev in e.store.events()["events"] if ev["kind"] == "reply.delivered"]) == 1


def test_launch_crash_recovers_same_runtime_operation(setup):
    e = setup
    add_dd(e)
    original = e.store.change

    def crash(kind, *args):
        if kind == "run.started":
            raise KeyboardInterrupt("模拟 start 成功后崩溃")
        return original(kind, *args)

    e.store.change = crash
    with pytest.raises(KeyboardInterrupt):
        run(e.schedule())
    e.store.change = original
    run(e.schedule())
    calls = e.runtime.start.call_args_list
    assert len(calls) == 2 and calls[0].args[0] == calls[1].args[0]
    assert len(e.store.read()["runs"]) == 1
    assert e.store.read()["dds"]["d"]["turn"] == 1


def test_stop_prevents_new_runs_and_resume_keeps_request(setup):
    e = setup
    e.request("human", "q", {"message": "继续"})
    add_dd(e)
    e.store.change("stop", {}, lambda s: s.update(stop="pause"))
    run(e.schedule())
    run(e.collect())
    assert e.store.read()["status"] == "stopped"
    e.runtime.start.assert_not_called()
    e.store.change("resume", {}, lambda s: s.update(stop=None, status="active"))
    run(e.schedule())
    assert e.store.read()["requests"]["q"]["status"] == "running"
    assert e.store.read()["dds"]["d"]["step"] == "impl_running"


def test_cancelled_run_failure_cannot_revive_dd(setup):
    e = setup
    add_dd(e, step="cancelled")
    record = collected(e, "impl", {"status": "failed", "error": "cancelled"})
    run(e.finish_run("run", record))
    assert e.store.read()["dds"]["d"]["step"] == "cancelled"
    assert e.store.read()["requests"] == {}


def test_multi_repo_finalize_resumes_and_rechecks_prior_target(setup):
    e = setup
    e.store.change("repo", {}, lambda s: s["repos"].update({"r2": {**REPO, "path": "/repo2"}}))
    e.git.merge.side_effect = [
        {"status": "merged", "head": A},
        {"status": "review_required"},
        {"status": "merged", "head": A, "recovered": True},
        {"status": "merged", "head": A},
    ]
    record = {"action": {"type": "done", "summary": "完成", "evidence_refs": ["test:1"]}}
    with pytest.raises(ValueError, match="收尾需要"):
        run(e.execute_action("a", record))
    assert set(e.store.read()["finalized"]) == {"r"}
    assert e.store.read()["status"] != "done"
    run(e.execute_action("a", record))
    assert e.store.read()["status"] == "done"
    assert e.git.merge.call_count == 4
    e.git.prepare_repo.assert_not_called()


def test_action_failure_becomes_separate_goal_request(setup):
    e = setup
    e.request("human", "q", {"message": "执行"})
    e.store.change(
        "actions",
        {},
        lambda s: (
            s["requests"]["q"].update(status="actions"),
            s["actions"].update(
                {
                    "a": {
                        "status": "pending",
                        "request_id": "q",
                        "action": {"type": "approve", "dd_id": "missing", "review_ref": "x"},
                    }
                }
            ),
        ),
    )
    run(e.actions())
    state = e.store.read()
    assert state["actions"]["a"]["status"] == "failed"
    assert state["requests"]["q"]["status"] == "delivered"
    assert len(state["requests"]) == 2


def test_protocol_intention_order_and_nonblocking_wait():
    validate_actions([{"type": "waiting", "summary": "等待 DD"}])
    with pytest.raises(ValueError, match="末尾"):
        validate_actions(
            [
                {"type": "waiting", "summary": "等待"},
                {"type": "reply", "in_reply_to": "q", "text": "答"},
            ]
        )


def test_steer_between_output_and_actions_invalidates_unexecuted_actions(setup):
    e = setup
    e.request("human", "q", {"message": "原请求"})
    record = collected(
        e,
        "goal",
        {"status": "succeeded", "output": [{"type": "waiting", "summary": "旧决策"}]},
        owner="q",
    )
    run(e.finish_run("run", record))
    e.store.change("version", {}, lambda s: s["goal"].update(version=2))
    run(e.actions())
    state = e.store.read()
    assert state["status"] == "active"
    assert state["requests"]["q"]["status"] == "pending"
    assert {a["status"] for a in state["actions"].values()} == {"obsolete"}


def test_dispatch_replay_after_pr_side_effect_keeps_same_marker(setup):
    e = setup
    dd = add_dd(e)
    e.store.change("reset", {}, lambda s: s.update(dds={}))
    a = {**dd, "type": "dispatch", "summary": "实现"}
    record = {"action": a}
    original = e.store.change

    def crash(kind, *args):
        if kind == "dd.dispatched":
            raise KeyboardInterrupt("模拟 PR 创建后崩溃")
        return original(kind, *args)

    e.store.change = crash
    with pytest.raises(KeyboardInterrupt):
        run(e.execute_action("a", record))
    e.store.change = original
    run(e.execute_action("a", record))
    run(e.execute_action("a", record))
    assert len(e.store.read()["dds"]) == 1
    calls = e.git.ensure_pr.call_args_list
    assert len(calls) == 2 and calls[0].args[2] == calls[1].args[2] == "a"


def test_approval_crash_after_merge_replays_recovery_and_cleanup(setup):
    e = setup
    add_dd(
        e,
        step="review",
        review_ref="review",
        acceptance={"status": "succeeded"},
        cr={"head": A},
        fr={"head": A},
    )
    record = {"action": {"type": "approve", "dd_id": "d", "review_ref": "review"}}
    original = e.store.change

    def crash(kind, *args):
        if kind == "dd.merged":
            raise KeyboardInterrupt("模拟 merge 副作用后崩溃")
        return original(kind, *args)

    e.store.change = crash
    with pytest.raises(KeyboardInterrupt):
        run(e.execute_action("a", record))
    e.store.change = original
    assert e.store.read()["dds"]["d"]["step"] == "merging"
    e.git.merge.return_value = {"status": "merged", "head": A, "recovered": True}
    run(e.execute_action("a", record))
    run(e.execute_action("a", record))
    state = e.store.read()
    assert state["dds"]["d"]["step"] == "done"
    assert len(state["requests"]) == 1
    assert e.git.merge.call_count == 2
    assert e.git.cleanup.call_count == 1


@pytest.mark.parametrize("ticket", [None, {"run_id": "run"}])
def test_stop_recovers_or_pauses_uncommitted_launch_without_starting(setup, ticket):
    e = setup
    add_dd(e)
    record = {
        "role": "impl",
        "owner": "d",
        "status": "launching",
        "prompt": {},
        "workspace": "/wt/d",
        "schema": {},
        "scope": "d",
        "goal_version": 1,
        "input_version": 1,
    }
    e.store.change(
        "launch", {}, lambda s: (s["runs"].update({"run": record}), s.update(stop="graceful"))
    )
    e.runtime.recover = AsyncMock(return_value=ticket)
    run(e.collect())
    e.runtime.start.assert_not_called()
    state = e.store.read()
    if ticket:
        assert state["runs"]["run"]["status"] == "running"
        assert state["status"] != "stopped"
    else:
        assert state["runs"]["run"]["status"] == "paused"
        assert state["status"] == "stopped"
        e.store.change("resume", {}, lambda s: s.update(stop=None, status="active"))
        run(e.schedule())
        assert e.runtime.start.call_args.args[0] == "run"
        assert len(e.store.read()["runs"]) == 1


def test_immediate_stop_retries_stop_for_recovered_ticket(setup):
    e = setup
    add_dd(e)
    e.store.change(
        "launch",
        {},
        lambda s: (
            s["runs"].update({"run": {"role": "impl", "owner": "d", "status": "launching"}}),
            s.update(stop="immediate"),
        ),
    )
    e.runtime.recover = AsyncMock(return_value={"run_id": "run"})
    e.runtime.stop = AsyncMock()
    run(e.collect())
    e.runtime.stop.assert_awaited_once_with({"run_id": "run"})
    e.runtime.start.assert_not_called()


def test_scribe_failure_does_not_move_cursor_or_block_dd(setup):
    e = setup
    add_dd(e)
    record = collected(
        e, "scribe", {"status": "failed", "error": "模型失败"}, owner="scribe", event_range=[1, 9]
    )
    run(e.finish_run("run", record))
    assert e.store.read()["scribe_cursor"] == 0
    run(e.schedule())
    assert e.store.read()["dds"]["d"]["step"] == "impl_running"


def test_scribe_success_commits_observation_and_cursor_together(setup):
    e = setup
    record = collected(
        e,
        "scribe",
        {
            "status": "succeeded",
            "output": {
                "observations": [
                    {
                        "title": "发现",
                        "summary": "需要注意",
                        "kind": "progress",
                        "severity": "info",
                        "evidence_refs": ["event:1"],
                    }
                ]
            },
        },
        owner="scribe",
        event_range=[1, 9],
    )
    run(e.finish_run("run", record))
    state = e.store.read()
    assert state["scribe_cursor"] == 9
    assert len(state["observations"]) == 1
    assert state["observations"][0]["event_range"] == [1, 9]


def test_waiting_does_not_swallow_next_queued_request(setup):
    e = setup
    e.request("human", "q1", {"message": "先处理"})
    e.request("human", "q2", {"message": "再处理"})
    record = collected(
        e,
        "goal",
        {"status": "succeeded", "output": [{"type": "waiting", "summary": "等下一轮"}]},
        owner="q1",
    )
    run(e.finish_run("run", record))
    run(e.actions())
    assert e.store.read()["status"] == "waiting"
    run(e.schedule())
    assert e.store.read()["requests"]["q2"]["status"] == "running"
    assert e.runtime.start.call_args.args[3]["current"] == {"message": "再处理"}


def test_compiled_graph_single_tick_uses_fake_ports_only(setup):
    e = setup
    add_dd(e, step="baseline")
    e.request("human", "q", {"message": "推进"})
    # 仅单次进程内图调用，所有外部节点均由 fixture 的 fake port 截断。
    result = run(e.graph.ainvoke({"tick": 0}))
    assert result["tick"] == 4
    assert e.store.read()["dds"]["d"]["step"] == "baseline_running"
    assert e.store.read()["requests"]["q"]["status"] == "running"
    e.commands.start.assert_awaited_once()
    e.runtime.start.assert_awaited_once()


def test_uncertain_goal_launch_blocks_next_goal_but_not_other_dds(setup):
    e = setup
    e.request("human", "q1", {"message": "一"})
    e.request("human", "q2", {"message": "二"})
    run(e.schedule())
    e.runtime.inspect.return_value = {"status": "lost", "error": "launch_uncertain"}
    run(e.collect())
    state = e.store.read()
    assert next(iter(state["runs"].values()))["status"] == "uncertain"
    assert state["requests"]["q1"]["status"] == "running"
    add_dd(e)
    run(e.schedule())
    state = e.store.read()
    assert state["requests"]["q2"]["status"] == "pending"
    assert state["dds"]["d"]["step"] == "impl_running"
    assert len([r for r in state["runs"].values() if r["role"] == "goal"]) == 1
    e.runtime.inspect.return_value = {"status": "running"}
    run(e.collect())
    assert e.runtime.inspect.await_count >= 3


def test_uncertain_run_prevents_false_stopped_and_owner_redispatch(setup):
    e = setup
    add_dd(e)
    run(e.schedule())
    e.runtime.inspect.return_value = {"status": "lost", "error": "launch_uncertain"}
    run(e.collect())
    e.store.change("steer", {}, lambda s: s["dds"]["d"].update(step="impl"))
    run(e.schedule())
    assert e.runtime.start.await_count == 1
    e.store.change("stop", {}, lambda s: s.update(stop="graceful"))
    run(e.collect())
    assert e.store.read()["status"] != "stopped"
    e.runtime.inspect.return_value = {"status": "failed", "error": "confirmed_absent"}
    run(e.collect())
    assert e.store.read()["status"] == "stopped"
    assert e.store.read()["dds"]["d"]["step"] == "interrupted"


@pytest.mark.parametrize("observed", ["merged", "review_required"])
def test_stale_approval_replay_only_recovers_already_applied_merge(setup, observed):
    e = setup
    add_dd(
        e,
        step="merging",
        review_ref="review",
        approved="review",
        acceptance={"status": "succeeded"},
        cr={"head": A},
        fr={"head": A},
    )
    e.store.change("steer", {}, lambda s: s["goal"].update(version=2))
    e.git.recover_merge.return_value = {"status": observed, "head": A}
    record = {"action": {"type": "approve", "dd_id": "d", "review_ref": "review"}}
    if observed == "merged":
        run(e.execute_action("a", record))
        assert e.store.read()["dds"]["d"]["step"] == "done"
    else:
        with pytest.raises(ValueError, match="目标版本"):
            run(e.execute_action("a", record))
        assert e.store.read()["dds"]["d"]["step"] == "impl"
    e.git.merge.assert_not_called()


def test_done_runs_final_scribe_once_ignoring_interval_and_collects_it(setup):
    e = setup
    e.scribe_interval = 10**12
    e.store.change("goal.done", {}, lambda s: s.update(status="done", scribe_attempt=10**12))
    run(e.observe())
    assert e.store.read()["final_scribe_attempted"] is True
    e.runtime.start.assert_awaited_once()
    assert e.runtime.start.call_args.args[3]["final"] is True
    run(e.observe())
    assert e.runtime.start.await_count == 1
    e.runtime.inspect.return_value = {
        "status": "succeeded",
        "output": {
            "observations": [
                {
                    "title": "最终观察",
                    "summary": "交付完成",
                    "kind": "delivery",
                    "severity": "info",
                    "evidence_refs": ["event:2"],
                }
            ]
        },
    }
    run(e.collect())
    run(e.observe())
    state = e.store.read()
    assert state["status"] == "done" and len(state["observations"]) == 1
    assert e.runtime.start.await_count == 1
    assert all(r["status"] == "finished" for r in state["runs"].values())


def test_failed_final_scribe_does_not_retry_or_change_done(setup):
    e = setup
    e.store.change("goal.done", {}, lambda s: s.update(status="done"))
    run(e.observe())
    e.runtime.inspect.return_value = {"status": "failed", "error": "观察失败"}
    run(e.collect())
    run(e.observe())
    assert e.store.read()["status"] == "done"
    assert e.store.read()["scribe_cursor"] == 0
    assert e.runtime.start.await_count == 1


def test_final_scribe_does_not_start_from_only_its_own_events(setup):
    e = setup
    seq = e.store.change("goal.done", {}, lambda s: s.update(status="done"))
    e.store.change("scribe.observed", {}, lambda s: s.update(scribe_cursor=seq))
    e.store.change("scribe.failed", {"error": "自身记录"})
    run(e.observe())
    e.runtime.start.assert_not_called()
    assert not e.store.read().get("final_scribe_attempted")


@pytest.mark.parametrize("recovery", [None, {"run_id": "existing"}, "query_failed"])
def test_final_scribe_launch_error_leaves_no_launching_state(setup, recovery):
    e = setup
    e.store.change("goal.done", {}, lambda s: s.update(status="done"))
    e.runtime.start.side_effect = RuntimeError("start回执丢失")
    if recovery == "query_failed":
        e.runtime.recover = AsyncMock(side_effect=RuntimeError("核查失败"))
    else:
        e.runtime.recover = AsyncMock(return_value=recovery)
    run(e.observe())
    state = e.store.read()
    record = next(iter(state["runs"].values()))
    assert state["status"] == "done"
    assert record["status"] == ("finished" if recovery is None else "uncertain")
    assert record["result"]["error"] == (
        "launch_failed" if recovery is None else "launch_uncertain"
    )
    run(e.observe())
    assert e.runtime.start.await_count == 1
