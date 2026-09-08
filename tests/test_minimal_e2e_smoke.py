"""End-to-end smoke (dd-26): one fake agent goal from enroll to done.

Everything is real except the agents and the platform: a real bare ``origin.git``
plus a real clone (all git through real subprocesses, no network), a real
``events.jsonl`` / ``control.jsonl`` run root built with the ``runroot`` APIs,
the real ``acceptance.BashRunner`` running ``true``, and the real
``engine.run_engine`` wiring (goalgraph -> ddgraph -> stagerunner gates ->
prlifecycle -> mergegate). Two seams are fake, per the dd-26 spec:

- the ``stagerunner.AgentInvoker`` answers each stage with its scripted
  one-line Stop JSON (goal turn 1 -> ``dispatch``, impl -> committed, cr ->
  pass, fr -> pass (or ``fail`` with a blocker in the second case), goal
  review -> ``approve``, goal turn 2 -> ``done``, release merge -> ``merged``);
- the gh side is canned: ``prlifecycle`` funnels its gh argvs through the
  injected *git* runner, so that runner is a real ``SubprocessGitRunner`` for
  git argvs and a fake for gh argvs (pr create / mergeable=MERGEABLE / close);
  ``mergegate.platform_merge`` gets its own fake ``GhRunner`` (merge ok).

Two spec-wording adjustments, both mechanical reality rather than bugs:

- impl's success stop literal is ``committed`` (``impl/1`` enum), which is what
  the spec's "impl -> done" shorthand means; ``done`` is the goal-turn stop.
- the baseline acceptance pass is eventless by design (``ddgraph.baseline_node``
  writes nothing on green), so the ``dd.acceptance`` checkpoint below is the
  post-impl acceptance run of the same goal command — it sits between the impl
  and cr ``dd.stage.finished`` events in the real order.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.minimal import enroll as enroll_mod
from fleet_graph.minimal import events as events_mod
from fleet_graph.minimal import runroot as runroot_mod

try:
    from fleet_graph.minimal import control, engine, gitgate
except ModuleNotFoundError as exc:
    # engine imports goalgraph/ddgraph, which are LangGraph graphs; a missing
    # langgraph downgrades to per-test skips (dd-18 precedent, see
    # tests/test_minimal_engine.py). Anything else missing is a real bug.
    if exc.name != "langgraph":
        raise
    control = None  # type: ignore[assignment]
    engine = None  # type: ignore[assignment]
    gitgate = None  # type: ignore[assignment]

pytestmark = pytest.mark.skipif(
    engine is None or shutil.which("git") is None,
    reason="langgraph not installed or git not available (exercised under make verify)",
)

RELEASE = "release/smoke"
DD_BRANCH = "dd/smoke-1"
SPEC_PATH = "docs/specs/1-smoke.md"
SPEC_TEXT = "# 1-smoke\n\n端到端 smoke 的 spec：保持验收绿。\n"

_GIT_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(cwd: Path, *args: str) -> str:
    """Run one real git command (explicit identity, isolated config)."""
    proc = subprocess.run(
        [
            "git",
            "-c",
            "user.name=smoke",
            "-c",
            "user.email=smoke@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(cwd),
            *args,
        ],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        check=True,
    )
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# the fake seams: agent invoker + canned gh
# ---------------------------------------------------------------------------


def _dispatch_stop(worktree: str, repo: str) -> dict[str, Any]:
    return {
        "schema": "goal.turn/1",
        "stop": "dispatch",
        "summary": "派一张 smoke DD",
        "dispatch": {
            "spec_text": SPEC_TEXT,
            "repos": [
                {
                    "path": worktree,
                    "remote": "origin",
                    "branch": DD_BRANCH,
                    "spec_path": SPEC_PATH,
                    "repo_path": repo,
                }
            ],
        },
    }


def _fr_stop(stop: str) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "schema": "review/1",
        "role": "fr",
        "stop": stop,
        "summary": f"fr {stop}",
    }
    if stop == "fail":
        obj["findings"] = [{"severity": "blocker", "summary": "smoke blocker：回 impl 再来一轮"}]
    return obj


class ScriptedInvoker:
    """``stagerunner.AgentInvoker`` shape: each stage answered by stage name.

    The stage is read from the call's user prompt (the ``*.in/1`` object), not
    from a call queue, so the assertion doubles as a check that the engine
    handed each stage the right input object.
    """

    def __init__(
        self, *, dispatch: dict[str, Any], impl_commit: str, fr_stops: list[str], merge_commit: str
    ) -> None:
        self._dispatch = dispatch
        self._impl_commit = impl_commit
        self._fr_stops = list(fr_stops)
        self._merge_commit = merge_commit
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, argv: list[str], *, system_prompt: str | None, user_prompt: str
    ) -> tuple[int, str]:
        in_obj = json.loads(user_prompt.split("\n", 1)[1])
        self.calls.append({"argv": list(argv), "in_obj": in_obj})
        schema = in_obj["schema"]
        if schema == "goal.turn.in/1":
            obj = (
                self._dispatch
                if in_obj["turn_no"] == 1
                else {
                    "schema": "goal.turn/1",
                    "stop": "done",
                    "summary": "smoke goal 完成",
                }
            )
        elif schema == "impl.in/1":
            obj = {
                "schema": "impl/1",
                "stop": "committed",
                "commit": self._impl_commit,
                "summary": "smoke impl 提交",
            }
        elif schema == "review.in/1":
            if in_obj["role"] == "cr":
                obj = {"schema": "review/1", "role": "cr", "stop": "pass", "summary": "cr pass"}
            else:
                if not self._fr_stops:
                    raise AssertionError("fr called more times than scripted")
                obj = _fr_stop(self._fr_stops.pop(0))
        elif schema == "goal.review.in/1":
            obj = {"schema": "goal.review/1", "stop": "approve", "summary": "smoke approve"}
        elif schema == "merge.in/1":
            obj = {
                "schema": "merge/1",
                "stop": "merged",
                "merged_commit": self._merge_commit,
                "summary": "release 合回 target",
            }
        else:
            raise AssertionError(f"unexpected input schema {schema!r}")
        return 0, json.dumps(obj, ensure_ascii=False)


class GitWithFakeGhRunner:
    """Real ``SubprocessGitRunner`` for git argvs; canned answers for gh argvs.

    ``prlifecycle`` runs its gh calls (pr create / mergeable / close) through
    the injected git runner, so the smoke keeps git real while gh stays
    offline: create answers a PR url, the mergeable probe answers MERGEABLE.
    """

    def __init__(self) -> None:
        self._git = gitgate.SubprocessGitRunner()
        self.gh_calls: list[list[str]] = []

    def run(self, args: list[str], *, cwd: str) -> gitgate.CompletedResult:
        if args and args[0] == "gh":
            self.gh_calls.append(list(args))
            if "create" in args:
                return gitgate.CompletedResult(0, "https://github.com/acme/smoke/pull/7\n", "")
            if "mergeable" in args:
                return gitgate.CompletedResult(0, '{"mergeable": "MERGEABLE"}', "")
            if "close" in args:
                return gitgate.CompletedResult(0, "", "")
            return gitgate.CompletedResult(0, "", "")
        return self._git.run(args, cwd=cwd)


class FakeGhRunner:
    """``mergegate.GhRunner`` fake: the platform merge succeeds."""

    def __init__(self, *, merge_commit: str) -> None:
        self._merge_commit = merge_commit
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> gitgate.CompletedResult:
        self.calls.append(list(args))
        if "mergeCommit" in args:
            body = json.dumps({"mergeCommit": {"oid": self._merge_commit}})
            return gitgate.CompletedResult(0, body, "")
        return gitgate.CompletedResult(0, "", "")


# ---------------------------------------------------------------------------
# the real git world: bare origin + clone + release branch + dd worktree
# ---------------------------------------------------------------------------


@dataclass
class World:
    goal_id: str
    enroll_obj: dict[str, Any]
    run_root: runroot_mod.GoalRunRoot
    repo: Path
    worktree: Path
    invoker: ScriptedInvoker
    git_runner: GitWithFakeGhRunner
    gh_runner: FakeGhRunner


def _build_world(tmp_path: Path, *, fr_stops: list[str]) -> World:
    # bare origin + clone; main carries one seed file (real git, no network)
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(repo)],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        check=True,
    )
    _git(repo, "config", "user.name", "smoke")
    _git(repo, "config", "user.email", "smoke@example.invalid")
    (repo / "README.md").write_text("smoke\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed on main")
    _git(repo, "push", "-q", "-u", "origin", "main")

    # the release branch (the goal's source_branch) exists on the remote and
    # is what the enroll repo worktree sits on
    _git(repo, "branch", RELEASE)
    _git(repo, "push", "-q", "origin", RELEASE)
    _git(repo, "checkout", "-q", RELEASE)

    # enroll: a real goal.enroll/2 payload through the real validation probe
    enroll_payload: dict[str, Any] = {
        "schema": "goal.enroll/2",
        "work_folder": "wf-smoke1",
        "title": "smoke：从 enroll 跑到 done",
        "goal_text": "用一条假 agent 的 DD 把端到端 smoke 跑通：真 git、真 events.jsonl。",
        "source_branch": RELEASE,
        "repos": [
            {
                "path": str(repo),
                "remote": "origin",
                "target_branch": "main",
                "acceptance": ["true"],
            }
        ],
    }
    validation = enroll_mod.validate_enroll(enroll_payload)
    assert validation.ok, validation.errors
    enroll_obj = enroll_mod.normalize_enroll(enroll_payload)
    goal_id = enroll_obj["goal_id"]

    # the engine run root: real runroot APIs write the enroll + first event
    engine_root = tmp_path / "engine"
    run_root = runroot_mod.goal_run_root(goal_id, engine_root=str(engine_root))
    runroot_mod.create_run_root(run_root)
    runroot_mod.write_enroll(run_root, enroll_obj)
    events_mod.EventLog(run_root.root).append(
        "goal.enrolled",
        {"goal_id": goal_id, "title": enroll_obj["title"], "work_folder": "wf-smoke1"},
    )

    # GO-36 prep the Goal Agent would have done: dd branch + linked worktree
    # under the run root, spec committed and pushed
    _git(repo, "branch", DD_BRANCH)
    worktree = runroot_mod.worktree_path(run_root, "dd-01")
    _git(repo, "worktree", "add", str(worktree), DD_BRANCH)
    spec_file = worktree / SPEC_PATH
    spec_file.parent.mkdir(parents=True, exist_ok=True)
    spec_file.write_text(SPEC_TEXT, encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", "spec: 1-smoke")
    _git(worktree, "push", "-q", "-u", "origin", DD_BRANCH)
    dd_head = _git(worktree, "rev-parse", "HEAD")
    release_tip = gitgate.remote_tip(
        str(repo), "origin", RELEASE, runner=gitgate.SubprocessGitRunner()
    )
    assert release_tip is not None

    invoker = ScriptedInvoker(
        dispatch=_dispatch_stop(str(worktree), str(repo)),
        impl_commit=dd_head,
        fr_stops=fr_stops,
        merge_commit=release_tip,
    )
    return World(
        goal_id=goal_id,
        enroll_obj=enroll_obj,
        run_root=run_root,
        repo=repo,
        worktree=worktree,
        invoker=invoker,
        git_runner=GitWithFakeGhRunner(),
        gh_runner=FakeGhRunner(merge_commit=dd_head),
    )


def _run(world: World) -> int:
    return engine.run_engine(
        world.goal_id,
        engine_root=str(world.run_root.root.parent),
        agent_invoker=world.invoker,
        git_runner=world.git_runner,
        gh_runner=world.gh_runner,
        timeout_s=60,
    )


# ---------------------------------------------------------------------------
# ordered-subsequence assertions over events.jsonl
# ---------------------------------------------------------------------------


def _kind(kind: str):
    return lambda ev: ev.kind == kind


def _turn(no: int):
    return lambda ev: ev.kind == "goal.turn.started" and ev.payload.get("turn_no") == no


def _finished(stage: str, stop: str):
    return lambda ev: (
        ev.kind == "dd.stage.finished"
        and ev.payload.get("stage") == stage
        and ev.payload.get("stop") == stop
    )


def _assert_in_order(evs: list[events_mod.Event], checkpoints: list[tuple[str, Any]]) -> None:
    cursor = -1
    for label, match in checkpoints:
        found = next((i for i in range(cursor + 1, len(evs)) if match(evs[i])), None)
        assert found is not None, f"checkpoint {label!r} not found after position {cursor}"
        cursor = found


def _tail_checkpoints() -> list[tuple[str, Any]]:
    return [
        ("dd.merged", _kind("dd.merged")),
        ("goal.turn.started#2", _turn(2)),
        ("goal.merged_to_target", _kind("goal.merged_to_target")),
        ("goal.done", _kind("goal.done")),
        (
            "engine.exiting(done)",
            lambda ev: ev.kind == "engine.exiting" and ev.payload.get("reason") == "done",
        ),
    ]


# ---------------------------------------------------------------------------
# case 1: happy path — dispatch → impl → cr → fr(pass) → approve → merge → done
# ---------------------------------------------------------------------------


def test_smoke_goal_enroll_to_done(tmp_path: Path) -> None:
    world = _build_world(tmp_path, fr_stops=["pass"])
    assert _run(world) == engine.EXIT_DONE

    log = events_mod.EventLog(world.run_root.root)
    evs = list(log.read())
    kinds = [ev.kind for ev in evs]

    # the enroll event survived and the engine lifecycle brackets the log
    assert kinds[0] == "goal.enrolled"
    assert kinds[1] == "engine.started"
    assert kinds[-1] == "engine.exiting"
    assert not any(kind == "goal.warning" for kind in kinds)

    _assert_in_order(
        evs,
        [
            ("engine.started", _kind("engine.started")),
            ("goal.turn.started#1", _turn(1)),
            ("dd.dispatched", _kind("dd.dispatched")),
            ("dd.pr_opened", _kind("dd.pr_opened")),
            ("dd.stage.finished(impl, committed)", _finished("impl", "committed")),
            (
                "dd.acceptance(exit 0)",
                lambda ev: ev.kind == "dd.acceptance" and ev.payload.get("exit") == 0,
            ),
            ("dd.stage.finished(cr, pass)", _finished("cr", "pass")),
            ("dd.stage.finished(fr, pass)", _finished("fr", "pass")),
            ("dd.stage.finished(goal_review, approve)", _finished("goal_review", "approve")),
            ("dd.stage.finished(merge, merged)", _finished("merge", "merged")),
            *_tail_checkpoints(),
        ],
    )

    # fold: done, exactly one merged DD
    derived = events_mod.fold(log.read())
    assert derived.terminal and derived.state == "done"
    assert derived.turn_no == 2
    assert [(item.dd_id, item.outcome) for item in derived.dd_history] == [("dd-01", "merged")]

    # the DD merge went through the (fake) platform, not the Merge Agent: the
    # only merge-agent call is the release final merge
    assert [call["in_obj"]["schema"] for call in world.invoker.calls] == [
        "goal.turn.in/1",
        "impl.in/1",
        "review.in/1",
        "review.in/1",
        "goal.review.in/1",
        "goal.turn.in/1",
        "merge.in/1",
    ]
    assert any("create" in args for args in world.git_runner.gh_calls)
    assert any("mergeable" in args for args in world.git_runner.gh_calls)
    assert any("close" in args for args in world.git_runner.gh_calls)
    assert any(args[2] == "merge" for args in world.gh_runner.calls)

    # real git teardown happened: the dd worktree is gone, the remote dd
    # branch is deleted, and the goal stays off the network
    assert not world.worktree.exists()
    assert _git(world.repo, "ls-remote", "origin", f"refs/heads/{DD_BRANCH}") == ""

    # the control-plane views derive from the same log without raising
    view = control.goal_status_view(list(log.read()), alive=False)
    assert view["state"] == "done"
    row = control.goal_list_row(world.run_root.root, alive_probe=lambda pid: False)
    assert row["goal_id"] == world.goal_id
    assert row["title"] == world.enroll_obj["title"]
    assert row["state"] == "done"


# ---------------------------------------------------------------------------
# case 2: fr fails with a blocker → back to impl (round 2) → converge to merged
# ---------------------------------------------------------------------------


def test_smoke_fr_fail_loops_impl_then_merges(tmp_path: Path) -> None:
    world = _build_world(tmp_path, fr_stops=["fail", "pass"])
    assert _run(world) == engine.EXIT_DONE

    log = events_mod.EventLog(world.run_root.root)
    evs = list(log.read())

    _assert_in_order(
        evs,
        [
            ("engine.started", _kind("engine.started")),
            ("goal.turn.started#1", _turn(1)),
            ("dd.dispatched", _kind("dd.dispatched")),
            ("dd.pr_opened", _kind("dd.pr_opened")),
            ("dd.stage.finished(impl r1)", _finished("impl", "committed")),
            ("dd.stage.finished(cr r1)", _finished("cr", "pass")),
            ("dd.stage.finished(fr, fail)", _finished("fr", "fail")),
            (
                "dd.stage.finished(impl r2)",
                lambda ev: (
                    ev.kind == "dd.stage.finished"
                    and ev.payload.get("stage") == "impl"
                    and ev.payload.get("run_id") == "dd-01-impl-2"
                ),
            ),
            ("dd.stage.finished(fr, pass)", _finished("fr", "pass")),
            # goal_review only appears after the second round cleared FR
            ("dd.stage.finished(goal_review, approve)", _finished("goal_review", "approve")),
            ("dd.stage.finished(merge, merged)", _finished("merge", "merged")),
            *_tail_checkpoints(),
        ],
    )

    # rounds increment (impl ran twice), fr went fail then pass, goal_review
    # ran exactly once — the approve-reset loop is visible in the event stream
    impl_run_ids = [
        ev.payload["run_id"]
        for ev in evs
        if ev.kind == "dd.stage.finished" and ev.payload.get("stage") == "impl"
    ]
    assert impl_run_ids == ["dd-01-impl-1", "dd-01-impl-2"]
    fr_stops = [
        ev.payload["stop"]
        for ev in evs
        if ev.kind == "dd.stage.finished" and ev.payload.get("stage") == "fr"
    ]
    assert fr_stops == ["fail", "pass"]
    approve_events = [ev for ev in evs if _finished("goal_review", "approve")(ev)]
    assert len(approve_events) == 1

    # still converges: done with exactly one merged DD
    derived = events_mod.fold(log.read())
    assert derived.terminal and derived.state == "done"
    assert derived.turn_no == 2
    assert [(item.dd_id, item.outcome) for item in derived.dd_history] == [("dd-01", "merged")]

    assert [call["in_obj"]["schema"] for call in world.invoker.calls] == [
        "goal.turn.in/1",
        "impl.in/1",
        "review.in/1",
        "review.in/1",
        "impl.in/1",
        "review.in/1",
        "review.in/1",
        "goal.review.in/1",
        "goal.turn.in/1",
        "merge.in/1",
    ]
