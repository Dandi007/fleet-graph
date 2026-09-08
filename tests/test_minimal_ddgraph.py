"""Tests for ddgraph.py (dd-17): the DD-internal loop as a LangGraph graph.

Everything drives the graph with a real ``EventLog`` on a tmp root, scripted
fakes for every IO seam (``agent_invoker`` / ``git_runner`` / ``bash_runner`` /
``pr_open`` / ``pr_cleanup`` / ``pr_mergeable`` / ``merge_fn``), so the
assertions double as the contract that the DD graph only ever acts through its
injected seams. The seven dd-17 spec acceptance cases are covered: happy path
to ``merged``, CR fail bouncing back to Impl, a rebased merge returning to CR
with approve cleared, the dd-ready gate failing with zero agent calls, a red
baseline, an agent failure that is never re-run, and an unknown (stage, stop)
surfacing ``ddflow.next_stage``'s ``ValueError`` instead of a swallowed bypass.

dd-30 adds the protocol §11 resume seam: ``resume_entry``'s fold table (pure
function), ``run_dd(initial_state=...)`` re-entering the graph at the folded
entry without re-dispatching, and the ``initial_state=None`` fresh-run
regression.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.minimal import ddflow, gitgate
from fleet_graph.minimal import events as events_mod
from fleet_graph.minimal.acceptance import Completed
from fleet_graph.minimal.events import EventLog
from fleet_graph.minimal.prlifecycle import CleanupResult, PrRef

try:
    from langgraph.checkpoint.memory import InMemorySaver

    from fleet_graph.minimal.ddgraph import (
        RESUME_TERMINAL,
        DDDeps,
        build_dd_graph,
        resume_entry,
        run_dd,
    )
except ModuleNotFoundError as exc:
    if exc.name != "langgraph":
        raise
    InMemorySaver = None
    build_dd_graph = None
    resume_entry = None
    run_dd = None

pytestmark = pytest.mark.skipif(
    run_dd is None,
    reason="langgraph not installed — ddgraph runs are exercised under make verify",
)

GOAL_ID = "g-000001"
DD_ID = "dd-01"
RELEASE = "release/g-000001"
DD_BRANCH = f"dd/{GOAL_ID}/{DD_ID}"

SHA_R = "a" * 40  # release head (base commit)
SHA_C = "b" * 40  # impl commit
SHA_R2 = "c" * 40  # rebased new_head
SHA_M = "d" * 40  # merged commit

REPOS = [{"path": "/wt/dd-01", "remote": "origin", "branch": DD_BRANCH, "spec_path": "spec.md"}]

GOAL = {
    "schema": "goal.enroll/2",
    "title": "把 X 做出来",
    "source_branch": RELEASE,
    "repos": [
        {
            "path": "/goal/repo",
            "remote": "origin",
            "target_branch": "main",
            "acceptance": ["make base"],
        }
    ],
}

ACCEPTANCE_CMDS = ["make test", "pytest tests/test_x.py"]


def _impl_committed() -> dict[str, Any]:
    return {"schema": "impl/1", "stop": "committed", "commit": SHA_C, "summary": "done"}


def _impl_failed() -> dict[str, Any]:
    return {"schema": "impl/1", "stop": "failed", "detail": "spec is contradictory"}


def _review(role: str, stop: str) -> dict[str, Any]:
    obj: dict[str, Any] = {"schema": "review/1", "role": role, "stop": stop, "summary": stop}
    if stop == "fail":
        obj["findings"] = [{"severity": "blocker", "file": "a.py", "line": 1, "detail": "no"}]
    return obj


def _approve() -> dict[str, Any]:
    return {"schema": "goal.review/1", "stop": "approve", "summary": "ok"}


class FakeGitRunner:
    """Answers the gates' read-only git queries from per-cwd scripts."""

    def __init__(self) -> None:
        self.branches: dict[str, dict[str, str]] = {}
        self.off_branch: dict[str, str] = {}
        self.dirty: set[str] = set()
        self.spec_exit = 0
        self.calls: list[tuple[list[str], str]] = []

    def _branch(self, cwd: str) -> str:
        if cwd in self.off_branch:
            return self.off_branch[cwd]
        script = self.branches.get(cwd) or {}
        return next(iter(script), DD_BRANCH)

    def _head(self, cwd: str) -> str:
        script = self.branches.get(cwd) or {}
        return script.get(self._branch(cwd)) or SHA_R

    def run(self, args: list[str], *, cwd: str) -> gitgate.CompletedResult:
        self.calls.append((list(args), cwd))
        if "ls-remote" in args:
            branch = args[-1].removeprefix("refs/heads/")
            sha = (self.branches.get(cwd) or {}).get(branch)
            out = f"{sha}\t{args[-1]}\n" if sha else ""
            return gitgate.CompletedResult(0, out, "")
        if "rev-parse" in args:
            if "--abbrev-ref" in args:
                return gitgate.CompletedResult(0, self._branch(cwd) + "\n", "")
            return gitgate.CompletedResult(0, self._head(cwd) + "\n", "")
        if "status" in args:
            out = " M src/foo.py\n" if cwd in self.dirty else ""
            return gitgate.CompletedResult(0, out, "")
        if "cat-file" in args:
            return gitgate.CompletedResult(self.spec_exit, "", "")
        if "merge-base" in args:
            return gitgate.CompletedResult(1, "", "")
        raise AssertionError(f"unrecognized git argv {args!r}")


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


class FailingInvoker(FakeInvoker):
    """A runtime that exits non-zero (protocol §0.2's agent failure)."""

    def __call__(
        self, argv: list[str], *, system_prompt: str | None, user_prompt: str
    ) -> tuple[int, str]:
        self.calls.append(
            {"argv": list(argv), "system_prompt": system_prompt, "user_prompt": user_prompt}
        )
        return 1, ""


class FakeBashRunner:
    def __init__(self) -> None:
        self.exit_codes: dict[str, int] = {}
        self.calls: list[tuple[str, str]] = []

    def run(self, cmd: str, cwd: str, timeout_s: int, env: dict[str, str] | None) -> Completed:
        self.calls.append((cmd, cwd))
        code = self.exit_codes.get(cmd, 0)
        return Completed(exit_code=code, output="ok" if code == 0 else "boom", timed_out=False)


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        stops: list[dict[str, Any]],
        merge_results: list[tuple[str, dict[str, Any]]] | None = None,
        invoker: Any = None,
    ) -> None:
        root = tmp_path / GOAL_ID
        self.log = EventLog(root)
        self.runner = FakeGitRunner()
        self.runner.branches["/wt/dd-01"] = {DD_BRANCH: SHA_R}
        self.bash = FakeBashRunner()
        self.invoker = invoker if invoker is not None else FakeInvoker(stops)
        self.merge_results = list(merge_results) if merge_results is not None else []
        self.merge_calls: list[dict[str, Any]] = []
        self.pr_open_calls: list[dict[str, Any]] = []
        self.pr_cleanup_calls: list[dict[str, Any]] = []

        def merge_fn(state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            self.merge_calls.append(dict(state))
            return self.merge_results.pop(0)

        def pr_open(worktree, *, head_branch, base_branch, title, body_file, runner):
            self.pr_open_calls.append(
                {
                    "worktree": worktree,
                    "head_branch": head_branch,
                    "base_branch": base_branch,
                    "title": title,
                    "body_file": body_file,
                }
            )
            number = len(self.pr_open_calls) * 10 + 1
            return PrRef(number=number, url=f"https://github.com/x/y/pull/{number}")

        def pr_cleanup(*, outcome, repos, runner, comment=None):
            self.pr_cleanup_calls.append({"outcome": outcome, "repos": list(repos)})
            return CleanupResult(ok=True, steps=[])

        self.deps = DDDeps(
            event_log=self.log,
            agent_invoker=self.invoker,
            git_runner=self.runner,
            bash_runner=self.bash,
            pr_open=pr_open,
            pr_cleanup=pr_cleanup,
            pr_mergeable=lambda worktree, number, *, runner: "MERGEABLE",
            merge_fn=merge_fn,
            goal=GOAL,
            release_branch=RELEASE,
            release_head=SHA_R,
            session_root=str(root / "sessions"),
            timeout_s=60,
        )

    def run_dd(self) -> dict[str, Any]:
        return run_dd(
            self.deps,
            goal_id=GOAL_ID,
            dd_id=DD_ID,
            repos=REPOS,
            spec_text="do it",
            acceptance_cmds=ACCEPTANCE_CMDS,
            checkpointer=InMemorySaver(),
        )

    def stream(self) -> dict[str, list[dict[str, Any]]]:
        self.log.append(
            "dd.dispatched",
            {"spec_text": "do it", "branch": DD_BRANCH, "head_commit": SHA_R},
            dd_id=DD_ID,
        )
        graph = build_dd_graph(self.deps, checkpointer=InMemorySaver())
        initial: dict[str, Any] = {
            "goal_id": GOAL_ID,
            "dd_id": DD_ID,
            "repos": REPOS,
            "spec_text": "do it",
            "acceptance_cmds": list(ACCEPTANCE_CMDS),
            "stage": None,
            "round": 1,
            "approve_valid": False,
            "history": (),
            "last_stop": None,
            "last_obj": None,
            "feedback": None,
            "prs": {},
            "dd_result": None,
            "terminal": None,
        }
        outputs: dict[str, list[dict[str, Any]]] = {}
        for chunk in graph.stream(
            initial,
            config={"recursion_limit": 1_000_000, "configurable": {"thread_id": DD_ID}},
        ):
            for node, update in chunk.items():
                outputs.setdefault(node, []).append(update)
        return outputs

    def kinds(self) -> list[str]:
        return [ev.kind for ev in self.log.read()]

    def events_of(self, kind: str) -> list[Any]:
        return [ev for ev in self.log.read() if ev.kind == kind]


def _in_obj(call: dict[str, Any]) -> dict[str, Any]:
    return json.loads(call["user_prompt"].split("\n", 1)[1])


# ---------------------------------------------------------------------------
# 1. happy path impl→acceptance→cr→fr→goal_review→merge→cleanup → merged
# ---------------------------------------------------------------------------


def test_happy_path_merges(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        stops=[_impl_committed(), _review("cr", "pass"), _review("fr", "pass"), _approve()],
        merge_results=[("merged", {"merged_commit": SHA_M})],
    )

    result = harness.run_dd()

    assert result["outcome"] == "merged"
    assert result["rounds"] == 1
    assert result["merged_commit"] == SHA_M
    assert result["head_commit"] == SHA_C
    assert result["impl_summary"] == "done"
    assert result["spec_text"] == "do it"
    assert result["branch"] == DD_BRANCH
    assert result["failure"] is None
    assert [(r["cmd"], r["exit"], r["tail"]) for r in result["acceptance_results"]] == [
        ("make test", 0, "ok"),
        ("pytest tests/test_x.py", 0, "ok"),
    ]
    assert result["reviews"] == [
        {"role": "cr", "stop": "pass", "summary": "pass", "findings": []},
        {"role": "fr", "stop": "pass", "summary": "pass", "findings": []},
    ]

    kinds = harness.kinds()
    assert kinds[0] == "dd.dispatched"
    assert "dd.pr_opened" in kinds
    assert kinds.count("dd.acceptance") == 2
    assert kinds.count("agent.exited") == 4
    assert kinds[-1] == "dd.merged"

    assert len(harness.pr_open_calls) == 1
    assert harness.pr_open_calls[0]["base_branch"] == RELEASE
    assert harness.pr_open_calls[0]["head_branch"] == DD_BRANCH
    assert harness.pr_cleanup_calls[0]["outcome"] == "merged"
    assert len(harness.merge_calls) == 1


# ---------------------------------------------------------------------------
# 2. cr fail → impl: approve cleared, round +1, feedback.from == "cr"
# ---------------------------------------------------------------------------


def test_cr_fail_bounces_to_impl_and_increments_round(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        stops=[
            _impl_committed(),
            _review("cr", "fail"),
            _impl_committed(),
            _review("cr", "pass"),
            _review("fr", "pass"),
            _approve(),
        ],
        merge_results=[("merged", {"merged_commit": SHA_M})],
    )

    outputs = harness.stream()
    result = ddflow.build_dd_result(harness.log.read(), DD_ID)

    assert result["outcome"] == "merged"
    assert result["rounds"] == 2

    # The first cr (fail) transitioned back to impl: round 2, approve cleared,
    # and the feedback names cr as the source.
    assert outputs["cr"][0]["stage"] == "impl"
    assert outputs["cr"][0]["round"] == 2
    assert outputs["cr"][0]["approve_valid"] is False
    assert outputs["cr"][0]["feedback"]["from"] == "cr"
    assert outputs["cr"][0]["feedback"]["findings"]

    # The second impl call carried round 2 and the cr feedback in its input.
    parsed = [_in_obj(call) for call in harness.invoker.calls]
    impl_ins = [obj for obj in parsed if obj["schema"] == "impl.in/1"]
    assert len(impl_ins) == 2
    assert impl_ins[1]["round"] == 2
    assert impl_ins[1]["feedback"]["from"] == "cr"


# ---------------------------------------------------------------------------
# 3. merge rebased → cr (approve cleared), full re-review, then merged
# ---------------------------------------------------------------------------


def test_rebased_bounces_to_cr_and_clears_approve(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        stops=[
            _impl_committed(),
            _review("cr", "pass"),
            _review("fr", "pass"),
            _approve(),
            _review("cr", "pass"),
            _review("fr", "pass"),
            _approve(),
        ],
        merge_results=[
            ("rebased", {"new_head": SHA_R2}),
            ("merged", {"merged_commit": SHA_M}),
        ],
    )

    outputs = harness.stream()
    result = ddflow.build_dd_result(harness.log.read(), DD_ID)

    assert result["outcome"] == "merged"
    assert result["rounds"] == 1  # rebased went to cr, not impl: no round bump
    assert result["merged_commit"] == SHA_M

    # The first merge (rebased) routed to cr and cleared approve.
    assert outputs["merge"][0]["stage"] == "cr"
    assert outputs["merge"][0]["approve_valid"] is False

    # The rebase re-ran the full CR → FR → Goal review, then merged: 6 stages
    # before the rebase plus 4 after (cr/fr/goal_review/merge).
    assert len(harness.events_of("dd.stage.finished")) == 10
    assert len(harness.merge_calls) == 2


# ---------------------------------------------------------------------------
# 4. dd_ready gate failure → dd.failed, zero agent calls
# ---------------------------------------------------------------------------


def test_dd_ready_failure_never_invokes_agent(tmp_path: Path) -> None:
    harness = Harness(tmp_path, stops=[], merge_results=[])
    harness.runner.branches["/wt/dd-01"] = {}  # branch missing on remote

    result = harness.run_dd()

    assert result["outcome"] == "failed"
    assert result["failure"]["stage"] == "dd_ready"
    assert len(harness.invoker.calls) == 0
    failed = harness.events_of("dd.failed")
    assert len(failed) == 1
    assert failed[0].payload["stage"] == "dd_ready"
    assert [f["code"] for f in failed[0].payload["failures"]] == ["branch_missing_on_remote"]


# ---------------------------------------------------------------------------
# 5. red baseline → dd.failed(stage=baseline), never enters impl
# ---------------------------------------------------------------------------


def test_baseline_red_fails_before_impl(tmp_path: Path) -> None:
    harness = Harness(tmp_path, stops=[], merge_results=[])
    harness.bash.exit_codes["make base"] = 1  # the goal base acceptance fails

    result = harness.run_dd()

    assert result["outcome"] == "failed"
    assert result["failure"]["stage"] == "baseline"
    assert len(harness.invoker.calls) == 0
    assert [ev.payload["stage"] for ev in harness.events_of("dd.failed")] == ["baseline"]
    assert not any(
        ev.payload.get("stage") == "impl" for ev in harness.events_of("dd.stage.finished")
    )


# ---------------------------------------------------------------------------
# 6. agent failure (ok=False) → DD failed, never re-run
# ---------------------------------------------------------------------------


def test_agent_failure_fails_without_rerun(tmp_path: Path) -> None:
    harness = Harness(tmp_path, stops=[], merge_results=[], invoker=FailingInvoker([]))

    result = harness.run_dd()

    assert result["outcome"] == "failed"
    assert result["failure"] == {"stage": "impl", "detail": "nonzero_exit"}
    assert len(harness.invoker.calls) == 1
    assert [ev.kind for ev in harness.events_of("agent.failed")] == ["agent.failed"]


# ---------------------------------------------------------------------------
# 7. unknown (stage, stop) surfaces ddflow.next_stage's ValueError, not swallowed
# ---------------------------------------------------------------------------


def test_unknown_merge_stop_raises(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        stops=[_impl_committed(), _review("cr", "pass"), _review("fr", "pass"), _approve()],
        merge_results=[("frobulated", {})],
    )

    with pytest.raises(ValueError):
        harness.run_dd()


def test_unknown_stage_stop_is_not_bypassed(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ddflow.next_stage("merge", "bogus")


# ---------------------------------------------------------------------------
# 8. resume_entry (protocol §11): fold one DD's events into (entry, overrides)
# ---------------------------------------------------------------------------


def _rev(kind: str, payload: dict[str, Any]) -> events_mod.Event:
    return events_mod.Event(ts="t", goal_id=GOAL_ID, dd_id=DD_ID, kind=kind, seq=0, payload=payload)


def test_resume_entry_dispatched_only_enters_at_dd_ready() -> None:
    entry, overrides = resume_entry([_rev("dd.dispatched", {"spec_text": "do it"})], DD_ID)

    assert entry == "dd_ready"
    assert overrides == {"round": 1, "approve_valid": False, "history": (), "prs": {}}


def test_resume_entry_lost_stage_restarts_the_same_step() -> None:
    events_list = [
        _rev("dd.dispatched", {}),
        _rev("dd.pr_opened", {"repo": REPOS[0]["path"], "number": 31, "url": "u31"}),
        _rev("dd.stage.started", {"stage": "impl"}),
        _rev("agent.spawned", {"run_id": "dd-01-impl-1", "role": "impl"}),
    ]

    entry, overrides = resume_entry(events_list, DD_ID)

    assert entry == "impl"
    assert overrides["prs"] == {REPOS[0]["path"]: {"number": 31, "url": "u31"}}


def test_resume_entry_mid_acceptance_reruns_whole_round() -> None:
    events_list = [
        _rev("dd.dispatched", {}),
        _rev("dd.pr_opened", {"repo": REPOS[0]["path"], "number": 31, "url": "u31"}),
        _rev("dd.stage.finished", {"stage": "impl", "stop": "committed", "commit": SHA_C}),
        _rev("dd.acceptance", {"cmd": "make test", "exit": 0, "index": 0, "total": 2}),
    ]

    entry, overrides = resume_entry(events_list, DD_ID)

    assert entry == "acceptance"
    assert overrides["history"] == (("impl", "committed"),)


def test_resume_entry_acceptance_fail_bounces_with_feedback_and_round() -> None:
    events_list = [
        _rev("dd.dispatched", {}),
        _rev("dd.stage.finished", {"stage": "impl", "stop": "committed"}),
        _rev("dd.acceptance", {"cmd": "make test", "exit": 1, "index": 0, "total": 2}),
        _rev("dd.stage.finished", {"stage": "acceptance", "stop": "fail"}),
    ]

    entry, overrides = resume_entry(events_list, DD_ID)

    assert entry == "impl"
    assert overrides["round"] == 2  # every re-entry into impl bumps the round
    assert overrides["approve_valid"] is False
    assert overrides["feedback"] == {
        "from": "acceptance",
        "detail": "acceptance command failed: make test",
    }


def test_resume_entry_cr_pass_restores_the_fr_handoff() -> None:
    events_list = [
        _rev("dd.dispatched", {}),
        _rev("dd.stage.finished", {"stage": "impl", "stop": "committed"}),
        _rev("dd.stage.finished", {"stage": "acceptance", "stop": "pass"}),
        _rev("dd.stage.finished", {"stage": "cr", "stop": "pass", "summary": "ok", "findings": []}),
    ]

    entry, overrides = resume_entry(events_list, DD_ID)

    assert entry == "fr"
    assert overrides["last_stop"] == "pass"
    assert overrides["last_obj"] == {"summary": "ok", "findings": []}


def test_resume_entry_terminal_transition_reenters_at_cleanup() -> None:
    merged = [
        _rev("dd.dispatched", {}),
        _rev("dd.stage.finished", {"stage": "goal_review", "stop": "approve"}),
        _rev("dd.stage.finished", {"stage": "merge", "stop": "merged", "merged_commit": SHA_M}),
    ]
    entry, overrides = resume_entry(merged, DD_ID)
    assert entry == "cleanup"
    assert overrides["terminal"] == "merged"
    assert overrides["approve_valid"] is True

    impl_failed = [
        _rev("dd.dispatched", {}),
        _rev("dd.stage.finished", {"stage": "impl", "stop": "failed", "detail": "nope"}),
    ]
    entry, overrides = resume_entry(impl_failed, DD_ID)
    assert entry == "cleanup"
    assert overrides["terminal"] == "failed"
    assert overrides["last_stop"] == "impl"  # _fail_result shape: the stage name
    assert overrides["last_obj"] == {"detail": "nope"}


def test_resume_entry_resolved_dd_is_the_terminal_sentinel() -> None:
    assert resume_entry(
        [_rev("dd.dispatched", {}), _rev("dd.merged", {"merged_commit": SHA_M})], DD_ID
    ) == (RESUME_TERMINAL, {})
    assert resume_entry(
        [_rev("dd.dispatched", {}), _rev("dd.failed", {"stage": "impl", "detail": "x"})], DD_ID
    ) == (RESUME_TERMINAL, {})


def test_resume_entry_trailing_agent_failure_closes_never_reruns() -> None:
    # a stagerunner-written failure (run_id-bearing, §0.2) one hop before
    # cleanup: the failed agent is closed out, never re-run
    stagerunner_failure = [
        _rev("dd.dispatched", {}),
        _rev("dd.stage.finished", {"stage": "fr", "stop": "pass"}),
        _rev("dd.stage.started", {"stage": "goal_review"}),
        _rev(
            "agent.failed",
            {
                "stage": "goal_review",
                "run_id": "dd-01-goal_review-1",
                "exit_code": 1,
                "detail": "agent exited with non-zero exit code 1",
            },
        ),
    ]
    entry, overrides = resume_entry(stagerunner_failure, DD_ID)
    assert entry == "cleanup"
    assert overrides["terminal"] == "failed"
    assert overrides["last_stop"] == "goal_review"

    # the engine's own lost-run marker (no run_id) names the step to restart
    # and never masks an earlier stagerunner failure
    events_list = [*stagerunner_failure, _rev("dd.stage.started", {"stage": "impl"})]
    entry, _overrides = resume_entry(events_list, DD_ID)
    assert entry == "impl"

    with_marker = [
        *stagerunner_failure,
        _rev("agent.failed", {"stage": "impl", "detail": "lost_on_restart"}),
    ]
    entry, _overrides = resume_entry(with_marker, DD_ID)
    assert entry == "cleanup"


# ---------------------------------------------------------------------------
# 9. run_dd resume: re-enter at the folded entry, never re-dispatch
# ---------------------------------------------------------------------------


def test_run_dd_resumes_at_folded_entry_without_redispatch(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        stops=[_review("fr", "pass"), _approve()],
        merge_results=[("merged", {"merged_commit": SHA_M})],
    )
    # a DD that died between CR finishing and FR starting
    harness.log.append(
        "dd.dispatched",
        {"spec_text": "do it", "branch": DD_BRANCH, "head_commit": SHA_R},
        dd_id=DD_ID,
    )
    harness.log.append(
        "dd.pr_opened",
        {"repo": REPOS[0]["path"], "number": 11, "url": "https://github.com/x/y/pull/11"},
        dd_id=DD_ID,
    )
    harness.log.append(
        "dd.stage.finished",
        {"stage": "impl", "stop": "committed", "commit": SHA_C, "summary": "done"},
        dd_id=DD_ID,
    )
    harness.log.append(
        "dd.acceptance", {"cmd": "make test", "exit": 0, "index": 0, "total": 2}, dd_id=DD_ID
    )
    harness.log.append(
        "dd.acceptance",
        {"cmd": "pytest tests/test_x.py", "exit": 0, "index": 1, "total": 2},
        dd_id=DD_ID,
    )
    harness.log.append("dd.stage.finished", {"stage": "acceptance", "stop": "pass"}, dd_id=DD_ID)
    harness.log.append(
        "dd.stage.finished",
        {"stage": "cr", "stop": "pass", "summary": "pass", "findings": []},
        dd_id=DD_ID,
    )

    entry, overrides = resume_entry(list(harness.log.read()), DD_ID)
    assert entry == "fr"

    result = run_dd(
        harness.deps,
        goal_id=GOAL_ID,
        dd_id=DD_ID,
        repos=REPOS,
        spec_text="do it",
        acceptance_cmds=ACCEPTANCE_CMDS,
        initial_state={"entry": entry, **overrides},
    )

    assert result["outcome"] == "merged"
    assert result["reviews"] == [
        {"role": "cr", "stop": "pass", "summary": "pass", "findings": []},
        {"role": "fr", "stop": "pass", "summary": "pass", "findings": []},
    ]
    kinds = harness.kinds()
    assert kinds.count("dd.dispatched") == 1  # the resume never re-dispatches
    assert kinds.count("agent.exited") == 2  # fr + goal_review only: cr not re-run
    assert kinds[-1] == "dd.merged"
    # the CR conclusion rode into the FR input via the folded handoff
    fr_in = _in_obj(harness.invoker.calls[0])
    assert fr_in["cr_result"] == {"stop": "pass", "summary": "pass", "findings": []}


def test_run_dd_initial_state_none_runs_the_fresh_path(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        stops=[_impl_committed(), _review("cr", "pass"), _review("fr", "pass"), _approve()],
        merge_results=[("merged", {"merged_commit": SHA_M})],
    )

    result = run_dd(
        harness.deps,
        goal_id=GOAL_ID,
        dd_id=DD_ID,
        repos=REPOS,
        spec_text="do it",
        acceptance_cmds=ACCEPTANCE_CMDS,
        initial_state=None,
    )

    assert result["outcome"] == "merged"
    kinds = harness.kinds()
    assert kinds[0] == "dd.dispatched"  # fresh: the opening event is written here
    assert kinds.count("dd.dispatched") == 1
    assert kinds[-1] == "dd.merged"
    assert len(harness.pr_open_calls) == 1  # fresh: the graph ran from dd_ready
