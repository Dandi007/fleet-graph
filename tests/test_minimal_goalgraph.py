"""Tests for goalgraph.py (dd-18): the goal-level turn loop as a LangGraph graph.

Everything drives ``run_goal`` with a real ``EventLog`` / ``ControlLog`` on a
tmp root, a scripted ``FakeInvoker`` (a queue of goal.turn/1 Stop objects that
records both prompts), a ``FakeGitRunner`` answering the read-only gate
queries from per-cwd scripts, a fake ``run_dd`` that writes its own ``dd.*``
events (as the dd-17 DD graph will), and a fake ``final_merge`` — so the
assertions double as the contract that the engine only ever acts through the
injected seams. Per-turn ``goal.turn.in/1`` objects are parsed back out of the
recorded user prompts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.minimal import gitgate
from fleet_graph.minimal.control import ControlLog
from fleet_graph.minimal.events import EventLog, fold

try:
    from langgraph.checkpoint.memory import InMemorySaver

    from fleet_graph.minimal.goalgraph import GoalDeps, run_goal
except ModuleNotFoundError as exc:
    # goalgraph is a LangGraph graph: exercising it needs the real dependency.
    # Bare ``pytest`` resolves to a user-site interpreter without the project's
    # deps installed (see the pythonpath note in pyproject.toml), so a missing
    # langgraph downgrades to per-test skips — collected, skipped, exit 0 —
    # rather than a collection error (a module-level importorskip would leave
    # nothing collected and pytest exits 5). ``make verify`` runs the full
    # suite under uv, where langgraph==1.2.11 is pinned. Anything else missing
    # is a real bug and must surface.
    if exc.name != "langgraph":
        raise
    InMemorySaver = None
    GoalDeps = None
    run_goal = None

pytestmark = pytest.mark.skipif(
    run_goal is None,
    reason="langgraph not installed — goalgraph runs are exercised under make verify",
)

GOAL_ID = "g-7f3a2c"
RELEASE = "release/g-7f3a2c"
DD_BRANCH = f"dd/{GOAL_ID}/dd-01"

SHA_R = "a" * 40  # release branch tip
SHA_D = "b" * 40  # dd branch tip
SHA_M = "c" * 40  # release → target merged commit

ENROLL: dict[str, Any] = {
    "schema": "goal.enroll/1",
    "work_folder": "wf-ab12cd",
    "title": "把 X 功能做出来",
    "goal_text": "把 X 做出来并通过验收",
    "source_branch": RELEASE,
    "repos": [{"path": "/goal/repo", "remote": "origin", "target_branch": "main"}],
    "acceptance": ["make test"],
}


def dispatch_stop() -> dict[str, Any]:
    return {
        "schema": "goal.turn/1",
        "stop": "dispatch",
        "summary": "先派一张单做 X",
        "dispatch": {
            "spec_text": "实现 X……",
            "repos": [
                {
                    "path": "/wt/dd-01",
                    "remote": "origin",
                    "branch": DD_BRANCH,
                    "spec_path": "spec.md",
                }
            ],
        },
    }


def done_stop() -> dict[str, Any]:
    return {"schema": "goal.turn/1", "stop": "done", "summary": "X 已在 release 上完成"}


def blocked_stop() -> dict[str, Any]:
    return {
        "schema": "goal.turn/1",
        "stop": "blocked",
        "summary": "缺外部依赖",
        "blocked": {"kind": "external", "detail": "上游服务没有测试环境"},
    }


class FakeGitRunner:
    """Answers the gates' read-only git queries from per-cwd scripts.

    ``branches[cwd]`` maps branch → sha (what ``ls-remote`` would return and
    what a clean worktree on that branch would show); ``off_branch[cwd]``
    overrides the branch name; ``dirty`` marks worktrees unclean. Anything
    unrecognized still answers exit 0 (``cat-file -e`` finds the spec).
    """

    def __init__(self) -> None:
        self.branches: dict[str, dict[str, str]] = {}
        self.off_branch: dict[str, str] = {}
        self.dirty: set[str] = set()
        self.calls: list[tuple[list[str], str]] = []

    def _branch(self, cwd: str) -> str:
        if cwd in self.off_branch:
            return self.off_branch[cwd]
        script = self.branches.get(cwd) or {}
        return next(iter(script), RELEASE)

    def _head(self, cwd: str) -> str:
        script = self.branches.get(cwd) or {}
        return script.get(self._branch(cwd)) or SHA_R

    def run(self, args: list[str], *, cwd: str) -> gitgate.CompletedResult:
        self.calls.append((list(args), cwd))
        if "ls-remote" in args:
            ref = args[-1]
            branch = ref.removeprefix("refs/heads/")
            sha = (self.branches.get(cwd) or {}).get(branch)
            out = f"{sha}\t{ref}\n" if sha else ""
            return gitgate.CompletedResult(0, out, "")
        if "rev-parse" in args:
            if "--abbrev-ref" in args:
                return gitgate.CompletedResult(0, self._branch(cwd) + "\n", "")
            return gitgate.CompletedResult(0, self._head(cwd) + "\n", "")
        if "status" in args:
            out = " M src/foo.py\n" if cwd in self.dirty else ""
            return gitgate.CompletedResult(0, out, "")
        return gitgate.CompletedResult(0, "", "")


class FakeInvoker:
    """A queue of Stop objects; records argv and both prompts of every call."""

    def __init__(self, stops: list[dict[str, Any]]) -> None:
        self.stops = list(stops)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, argv: list[str], *, system_prompt: str | None, user_prompt: str
    ) -> tuple[int, str]:
        self.calls.append(
            {"argv": list(argv), "system_prompt": system_prompt, "user_prompt": user_prompt}
        )
        stop = self.stops.pop(0)
        return 0, json.dumps(stop, ensure_ascii=False)

    def in_obj(self, index: int) -> dict[str, Any]:
        """The goal.turn.in/1 object of call ``index`` (user prompt minus preamble)."""
        return json.loads(self.calls[index]["user_prompt"].split("\n", 1)[1])


class FailingInvoker(FakeInvoker):
    """A runtime that always exits non-zero (protocol §0.2's agent failure)."""

    def __call__(
        self, argv: list[str], *, system_prompt: str | None, user_prompt: str
    ) -> tuple[int, str]:
        self.calls.append(
            {"argv": list(argv), "system_prompt": system_prompt, "user_prompt": user_prompt}
        )
        return 1, ""


class FakeRunDD:
    """The injected DD seam: records dispatches, writes dd.* events, returns §7."""

    def __init__(self, log: EventLog, *, outcome: str = "merged") -> None:
        self.log = log
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def __call__(self, dispatch_obj: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(json.loads(json.dumps(dispatch_obj)))
        dd_id = f"dd-{len(self.calls):02d}"
        self.log.append(
            "dd.dispatched",
            {"spec_text": dispatch_obj.get("spec_text"), "branch": DD_BRANCH},
            dd_id=dd_id,
        )
        if self.outcome == "merged":
            self.log.append("dd.merged", {"merged_commit": SHA_M}, dd_id=dd_id)
            return {
                "dd_id": dd_id,
                "spec_text": dispatch_obj.get("spec_text"),
                "outcome": "merged",
                "rounds": 1,
                "branch": DD_BRANCH,
                "head_commit": SHA_D,
                "merged_commit": SHA_M,
                "impl_summary": "实现了 X",
                "failure": None,
            }
        self.log.append("dd.failed", {"stage": "impl", "detail": "做不了"}, dd_id=dd_id)
        return {
            "dd_id": dd_id,
            "spec_text": dispatch_obj.get("spec_text"),
            "outcome": "failed",
            "rounds": 1,
            "branch": DD_BRANCH,
            "head_commit": None,
            "merged_commit": None,
            "impl_summary": None,
            "failure": {"stage": "impl", "detail": "做不了"},
        }


class FakeFinalMerge:
    """The injected release → target seam: a queue of ``(stop, payload)``."""

    def __init__(self, results: list[tuple[str, dict[str, Any]]]) -> None:
        self.results = list(results)
        self.calls = 0

    def __call__(self) -> tuple[str, dict[str, Any]]:
        self.calls += 1
        return self.results.pop(0)


class Harness:
    """One wired goal root: log, control log, runner, invoker, seams."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        stops: list[dict[str, Any]],
        invoker: FakeInvoker | None = None,
        warn_turns: int = 30,
        final_merge: FakeFinalMerge | None = None,
    ) -> None:
        root = tmp_path / GOAL_ID
        self.log = EventLog(root)
        self.control = ControlLog(root)
        # The first event of a real goal's log, written at enroll time; seeded
        # here so fold()'s goal_version (1 after enroll, +1 per steer) matches
        # steer.current_goal's.
        self.log.append("goal.enrolled", {"title": ENROLL["title"]})
        self.runner = FakeGitRunner()
        self.runner.branches["/goal/repo"] = {RELEASE: SHA_R}
        self.runner.branches["/wt/dd-01"] = {DD_BRANCH: SHA_D}
        self.invoker = invoker if invoker is not None else FakeInvoker(stops)
        self.run_dd = FakeRunDD(self.log)
        self.final_merge = final_merge or FakeFinalMerge(
            [("merged", {"merged_commit": SHA_M, "summary": "合到 main"})]
        )
        self.deps = GoalDeps(
            event_log=self.log,
            control_log=self.control,
            agent_invoker=self.invoker,
            git_runner=self.runner,
            run_dd=self.run_dd,
            final_merge=self.final_merge,
            warn_turns=warn_turns,
            session_root=str(root / "sessions"),
            timeout_s=60,
        )

    def kinds(self) -> list[str]:
        return [ev.kind for ev in self.log.read()]

    def events_of(self, kind: str) -> list[Any]:
        return [ev for ev in self.log.read() if ev.kind == kind]


# ---------------------------------------------------------------------------
# 1. dispatch → run_dd → done → final_merge merged → goal.done
# ---------------------------------------------------------------------------


def test_dispatch_then_done_merges_to_target(tmp_path: Path) -> None:
    harness = Harness(tmp_path, stops=[dispatch_stop(), done_stop()])

    result = run_goal(
        harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL), checkpointer=InMemorySaver()
    )

    assert result["stop"] == "done"
    assert result["summary"] == "X 已在 release 上完成"
    assert len(harness.run_dd.calls) == 1
    assert harness.run_dd.calls[0]["spec_text"] == "实现 X……"
    assert harness.final_merge.calls == 1

    kinds = harness.kinds()
    assert "goal.dispatch_rejected" not in kinds
    assert kinds.count("goal.turn.started") == 2
    assert kinds.count("goal.turn.finished") == 2
    assert "goal.merged_to_target" in kinds
    assert kinds[-1] == "goal.done"

    derived = fold(harness.log.read())
    assert derived.terminal and derived.state == "done"
    assert derived.turn_no == 2
    assert len(derived.dd_history) == 1
    assert derived.dd_history[0].outcome == "merged"

    in1 = harness.invoker.in_obj(0)
    assert in1["schema"] == "goal.turn.in/1"
    assert in1["turn_no"] == 1
    assert in1["goal_version"] == 1
    assert in1["steer_diff"] == []
    assert in1["last_stop"] is None
    assert in1["last_dd"] is None
    assert in1["release_branch"] == RELEASE
    assert in1["release_head"] == SHA_R
    assert in1["goal"]["title"] == ENROLL["title"]
    assert in1["history"]["events"].endswith("events.jsonl")

    in2 = harness.invoker.in_obj(1)
    assert in2["turn_no"] == 2
    assert in2["last_stop"] == dispatch_stop()
    assert in2["last_dd"]["dd_id"] == "dd-01"
    assert in2["last_dd"]["outcome"] == "merged"
    assert in2["dd_summary"] == "1 张 DD：1 merged，0 failed（dd-01：实现了 X）"


def test_goal_session_prompt_only_on_first_call(tmp_path: Path) -> None:
    harness = Harness(tmp_path, stops=[dispatch_stop(), done_stop()])
    run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    first, second = harness.invoker.calls
    assert first["system_prompt"] is not None
    assert "Goal Agent" in first["system_prompt"]
    assert second["system_prompt"] is None
    assert first["argv"][0] == "agent-run"
    assert "--role" in first["argv"] and "goal" in first["argv"]


# ---------------------------------------------------------------------------
# 2. first-turn blocked ends immediately, run_dd never called
# ---------------------------------------------------------------------------


def test_blocked_first_turn_never_runs_dd(tmp_path: Path) -> None:
    harness = Harness(tmp_path, stops=[blocked_stop()])

    result = run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    assert result["stop"] == "blocked"
    assert result["blocked"] == blocked_stop()["blocked"]
    assert harness.run_dd.calls == []
    assert harness.final_merge.calls == 0
    assert len(harness.invoker.calls) == 1

    blocked_events = harness.events_of("goal.blocked")
    assert len(blocked_events) == 1
    assert blocked_events[0].payload["kind"] == "external"
    assert blocked_events[0].payload["detail"] == "上游服务没有测试环境"

    derived = fold(harness.log.read())
    assert derived.terminal and derived.state == "blocked"
    assert harness.kinds()[-1] == "goal.blocked"


# ---------------------------------------------------------------------------
# 3. dispatch rejected by the GO-36 gate → bounced back, run_dd never called
# ---------------------------------------------------------------------------


def test_dispatch_rejected_bounces_back_to_next_turn(tmp_path: Path) -> None:
    harness = Harness(tmp_path, stops=[dispatch_stop(), done_stop()])
    # The dispatch repo is on the right branch, but the branch is missing on
    # the remote: check_dispatch_ready fails BRANCH_MISSING_ON_REMOTE.
    harness.runner.branches["/wt/dd-01"] = {}
    harness.runner.off_branch["/wt/dd-01"] = DD_BRANCH

    result = run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    # The loop continued (second turn ran and finished the goal).
    assert result["stop"] == "done"
    assert harness.run_dd.calls == []

    rejected = harness.events_of("goal.dispatch_rejected")
    assert len(rejected) == 1
    assert rejected[0].payload["errors"], "field-level errors must be recorded"
    assert any("branch_missing_on_remote" in error for error in rejected[0].payload["errors"])
    assert rejected[0].payload["failures"][0]["repo"] == "dd-01"

    in2 = harness.invoker.in_obj(1)
    assert any("dispatch rejected" in warning for warning in in2["warnings"])
    assert any("branch_missing_on_remote" in warning for warning in in2["warnings"])
    assert in2["last_stop"] == dispatch_stop()
    assert in2["turn_no"] == 1  # no DD ran, so the bounced turn stays turn 1


# ---------------------------------------------------------------------------
# 4. goal_steer bumps goal_version and reaches the next turn (GO-20)
# ---------------------------------------------------------------------------


def test_steer_bumps_version_and_injects_diff(tmp_path: Path) -> None:
    harness = Harness(tmp_path, stops=[dispatch_stop(), done_stop()])
    harness.control.append({"op": "steer", "patch": {"deadline": "2026-10-01"}})

    result = run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    assert result["stop"] == "done"
    steered = harness.events_of("goal.steered")
    assert len(steered) == 1
    assert steered[0].payload["version"] == 2
    assert steered[0].payload["diff"]["added"] == {"deadline": "2026-10-01"}

    in1 = harness.invoker.in_obj(0)
    assert in1["goal_version"] == 2
    assert in1["goal"]["deadline"] == "2026-10-01"
    assert len(in1["steer_diff"]) == 1
    assert in1["steer_diff"][0]["version"] == 2
    assert in1["steer_diff"][0]["added"] == {"deadline": "2026-10-01"}

    # The diff is per-turn: the second turn sees the new version but no diff.
    in2 = harness.invoker.in_obj(1)
    assert in2["goal_version"] == 2
    assert in2["steer_diff"] == []
    assert in2["goal"]["deadline"] == "2026-10-01"

    assert fold(harness.log.read()).goal_version == 2


# ---------------------------------------------------------------------------
# 5. goal_message is injected once and cleared after reading
# ---------------------------------------------------------------------------


def test_message_injected_once_then_cleared(tmp_path: Path) -> None:
    harness = Harness(tmp_path, stops=[dispatch_stop(), done_stop()])
    harness.control.append({"op": "message", "text": "优先做只读路径"})

    result = run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    assert result["stop"] == "done"
    in1 = harness.invoker.in_obj(0)
    assert len(in1["messages"]) == 1
    assert in1["messages"][0]["from"] == "mcp"
    assert in1["messages"][0]["text"] == "优先做只读路径"
    in2 = harness.invoker.in_obj(1)
    assert in2["messages"] == []
    assert len(harness.events_of("goal.message")) == 1
    assert len(harness.events_of("control.received")) == 1


# ---------------------------------------------------------------------------
# 6. crossing warn_turns only warns; the loop continues (GO-6.4)
# ---------------------------------------------------------------------------


def test_warn_turns_only_warns_and_loop_continues(tmp_path: Path) -> None:
    harness = Harness(tmp_path, stops=[dispatch_stop(), dispatch_stop(), done_stop()], warn_turns=2)

    result = run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    assert result["stop"] == "done"
    assert len(harness.invoker.calls) == 3
    assert len(harness.run_dd.calls) == 2

    in1 = harness.invoker.in_obj(0)
    assert in1["warnings"] == []
    in2 = harness.invoker.in_obj(1)
    assert in2["warnings"] == ["turns>=2"]
    in3 = harness.invoker.in_obj(2)
    assert in3["warnings"] == ["turns>=2"]
    assert in3["dd_summary"] == "2 张 DD：2 merged，0 failed（dd-02：实现了 X）"

    # One event per crossing, deduped against the log — not one per turn.
    warning_events = harness.events_of("goal.warning")
    assert len(warning_events) == 1
    assert warning_events[0].payload["message"] == "turns>=2"
    assert fold(harness.log.read()).warnings == ["turns>=2"]


# ---------------------------------------------------------------------------
# 7. the Goal Agent failing blocks the goal and is never re-run (protocol §0.2)
# ---------------------------------------------------------------------------


def test_agent_failure_blocks_goal_without_rerun(tmp_path: Path) -> None:
    invoker = FailingInvoker([])
    harness = Harness(tmp_path, stops=[], invoker=invoker)

    result = run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    assert result["stop"] == "blocked"
    assert len(harness.invoker.calls) == 1
    assert harness.run_dd.calls == []
    assert harness.final_merge.calls == 0
    assert harness.events_of("agent.failed")

    blocked_events = harness.events_of("goal.blocked")
    assert len(blocked_events) == 1
    assert "nonzero_exit" in blocked_events[0].payload["detail"]
    assert harness.kinds()[-1] == "goal.blocked"


# ---------------------------------------------------------------------------
# goal_stop at a step boundary ends the loop before the next turn
# ---------------------------------------------------------------------------


def test_goal_stop_ends_loop_at_step_boundary(tmp_path: Path) -> None:
    harness = Harness(tmp_path, stops=[dispatch_stop()])
    harness.control.append({"op": "stop", "mode": "graceful"})

    result = run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    assert result["stop"] == "stopped"
    assert harness.invoker.calls == []
    assert harness.run_dd.calls == []
    exiting = harness.events_of("engine.exiting")
    assert len(exiting) == 1
    assert exiting[0].payload == {"reason": "stop", "mode": "graceful"}
    derived = fold(harness.log.read())
    assert derived.terminal and derived.state == "stopped"


# ---------------------------------------------------------------------------
# an invalid steer op is recorded and ignored, the loop does not crash
# ---------------------------------------------------------------------------


def test_invalid_steer_ignored_with_event(tmp_path: Path) -> None:
    harness = Harness(tmp_path, stops=[dispatch_stop(), done_stop()])
    # Written raw because ControlLog.append refuses invalid ops at the gate;
    # the engine must still survive one landing in the file.
    harness.control.path.parent.mkdir(parents=True, exist_ok=True)
    harness.control.path.write_text(
        '{"ts":"2026-09-08T00:00:00+00:00","seq":1,"op":"steer","patch":{}}\n'
        '{"ts":"2026-09-08T00:00:01+00:00","seq":2,"op":"message","text":"收到"}\n',
        encoding="utf-8",
    )

    result = run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    assert result["stop"] == "done"
    warnings = [
        ev.payload.get("message", "")
        for ev in harness.events_of("goal.warning")
        if "steer ignored" in ev.payload.get("message", "")
    ]
    assert warnings, "the ignored steer must be recorded as an event"
    in1 = harness.invoker.in_obj(0)
    assert in1["goal_version"] == 1  # the invalid steer did not bump anything
    assert in1["messages"][0]["text"] == "收到"  # the valid op after it still ran


# ---------------------------------------------------------------------------
# final_merge rebased/failed bounces back into the next turn (GO-15 boundary)
# ---------------------------------------------------------------------------


def test_final_merge_rebased_bounces_back_to_goal_turn(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        stops=[dispatch_stop(), done_stop()],
        final_merge=FakeFinalMerge(
            [
                ("rebased", {"new_head": SHA_D, "summary": "解了冲突"}),
                ("merged", {"merged_commit": SHA_M, "summary": "合到 main"}),
            ]
        ),
    )
    # Turn 2 says done → final_merge rebased → turn 3 must run and see the
    # result as handoff content, then finish the goal.
    harness.invoker.stops.append(done_stop())

    result = run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    assert result["stop"] == "done"
    assert harness.final_merge.calls == 2
    in3 = harness.invoker.in_obj(2)
    assert any("final_merge rebased" in warning for warning in in3["warnings"])
    assert any("解了冲突" in warning for warning in in3["warnings"])
    assert any(
        "final_merge rebased" in ev.payload.get("message", "")
        for ev in harness.events_of("goal.warning")
    )
    assert harness.kinds()[-1] == "goal.done"


# ---------------------------------------------------------------------------
# validate_done: done with an in-flight DD bounces back (protocol §0.10)
# ---------------------------------------------------------------------------


def test_done_with_inflight_dd_bounces_back_to_goal_turn(tmp_path: Path) -> None:
    harness = Harness(tmp_path, stops=[done_stop(), blocked_stop()])
    # A crashed DD from before: dispatched, never resolved (no dd.merged /
    # dd.failed terminal event) — e.g. an engine restart that never resumed it.
    harness.log.append("dd.dispatched", {"spec_text": "旧的 X", "branch": DD_BRANCH}, dd_id="dd-99")

    result = run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    # The done was bounced: the next turn ran, final_merge never fired, and
    # the goal only ended when the agent said something else (blocked).
    assert result["stop"] == "blocked"
    assert harness.final_merge.calls == 0
    assert harness.run_dd.calls == []
    assert len(harness.invoker.calls) == 2
    assert "goal.done" not in harness.kinds()

    bounced = [
        ev
        for ev in harness.events_of("goal.warning")
        if ev.payload.get("reason") == "done_with_inflight_dd"
    ]
    assert len(bounced) == 1
    assert bounced[0].payload["dd_ids"] == ["dd-99"]

    # The field-level explanation is the next turn's handoff content.
    in2 = harness.invoker.in_obj(1)
    assert in2["turn_no"] == 1  # no DD ran, so the bounced turn stays turn 1
    assert in2["last_stop"] == done_stop()
    assert any(
        "done_with_inflight_dd" in warning and "dd-99" in warning for warning in in2["warnings"]
    )


def test_done_without_inflight_dd_reaches_final_merge_and_done(tmp_path: Path) -> None:
    harness = Harness(tmp_path, stops=[done_stop()])

    result = run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    assert result["stop"] == "done"
    assert harness.final_merge.calls == 1
    assert harness.kinds()[-1] == "goal.done"
    assert not any(
        ev.payload.get("reason") == "done_with_inflight_dd"
        for ev in harness.events_of("goal.warning")
    )


def test_failed_dd_is_terminal_and_does_not_block_done(tmp_path: Path) -> None:
    harness = Harness(tmp_path, stops=[done_stop()])
    # A failed DD is terminal (§0.10): not "unmerged", never a done blocker —
    # the Goal Agent already judged it via dd_summary.
    harness.log.append(
        "dd.dispatched", {"spec_text": "做不了的那个", "branch": DD_BRANCH}, dd_id="dd-01"
    )
    harness.log.append("dd.failed", {"stage": "impl", "detail": "做不了"}, dd_id="dd-01")

    result = run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    assert result["stop"] == "done"
    assert harness.final_merge.calls == 1
    assert harness.kinds()[-1] == "goal.done"
    assert not any(
        ev.payload.get("reason") == "done_with_inflight_dd"
        for ev in harness.events_of("goal.warning")
    )
