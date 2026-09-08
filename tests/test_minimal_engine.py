"""Tests for engine.py (dd-21): the per-goal engine process entry point.

Everything drives ``run_engine`` with a real ``EventLog`` / ``ControlLog`` on a
tmp engine root, a scripted ``FakeInvoker`` (a queue of Stop objects, one per
agent call), a ``FakeGitRunner`` answering every read-only git/gh query (the
real ``prlifecycle`` funnels both through the injected runner) and a
``FakeBashRunner`` for the acceptance commands. The two goalgraph seams
(``run_dd`` / ``final_merge``) are the *real* wiring — ddgraph + prlifecycle +
mergegate — so the assertions double as the contract that the engine only ever
acts through injected IO.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.minimal import events, runroot
from fleet_graph.minimal.acceptance import Completed

try:
    from langgraph.checkpoint.memory import InMemorySaver  # noqa: F401

    from fleet_graph.minimal import engine, gitgate
except ModuleNotFoundError as exc:
    # engine imports goalgraph/ddgraph, which are LangGraph graphs: exercising
    # them needs langgraph. Bare ``pytest`` resolves to a user-site interpreter
    # without the project's deps (see the pythonpath note in pyproject.toml), so
    # a missing langgraph downgrades to per-test skips — collected, skipped, exit
    # 0 — rather than a collection error (a module-level importorskip would leave
    # nothing collected and pytest exits 5). ``make verify`` runs the full suite
    # under uv, where langgraph==1.2.11 is pinned. Anything else missing is a
    # real bug and must surface.
    if exc.name != "langgraph":
        raise
    engine = None
    gitgate = None

pytestmark = pytest.mark.skipif(
    engine is None,
    reason="langgraph not installed — engine runs are exercised under make verify",
)

GOAL_ID = "g-7f3a2c"
RELEASE = "release/g-7f3a2c"
DD_BRANCH = f"dd/{GOAL_ID}/dd-01"

SHA_R = "a" * 40  # release tip
SHA_D = "b" * 40  # dd branch tip
SHA_M = "d" * 40  # DD merged commit
SHA_MAIN = "e" * 40  # release → main merged commit

ENROLL: dict[str, Any] = {
    "schema": "goal.enroll/2",
    "work_folder": "wf-ab12cd",
    "title": "把 X 做出来",
    "goal_text": "实现 X 并通过验收",
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


def _dispatch_stop() -> dict[str, Any]:
    return {
        "schema": "goal.turn/1",
        "stop": "dispatch",
        "summary": "派一张单做 X",
        "dispatch": {
            "spec_text": "实现 X",
            "repos": [
                {
                    "path": "/wt/dd-01",
                    "remote": "origin",
                    "branch": DD_BRANCH,
                    "spec_path": "docs/specs/x.md",
                }
            ],
        },
    }


def _impl_committed() -> dict[str, Any]:
    return {"schema": "impl/1", "stop": "committed", "commit": SHA_D, "summary": "done"}


def _review(role: str, stop: str) -> dict[str, Any]:
    return {"schema": "review/1", "role": role, "stop": stop, "summary": stop}


def _approve() -> dict[str, Any]:
    return {"schema": "goal.review/1", "stop": "approve", "summary": "ok"}


def _dd_merge() -> dict[str, Any]:
    return {
        "schema": "merge/1",
        "stop": "merged",
        "merged_commit": SHA_M,
        "summary": "合入 release",
    }


def _done_stop() -> dict[str, Any]:
    return {"schema": "goal.turn/1", "stop": "done", "summary": "X 已在 release 上完成"}


def _release_merge() -> dict[str, Any]:
    return {
        "schema": "merge/1",
        "stop": "merged",
        "merged_commit": SHA_MAIN,
        "summary": "合回 main",
    }


class FakeInvoker:
    """A queue of Stop objects for every agent call (goal / impl / cr / fr / merge)."""

    def __init__(self, stops: list[dict[str, Any]]) -> None:
        self.stops = list(stops)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, argv: list[str], *, system_prompt: str | None, user_prompt: str
    ) -> tuple[int, str]:
        self.calls.append(
            {"argv": list(argv), "system_prompt": system_prompt, "user_prompt": user_prompt}
        )
        return 0, json.dumps(self.stops.pop(0), ensure_ascii=False)


class FakeGitRunner:
    """Answers every read-only git/gh query (prlifecycle funnels both through it)."""

    def __init__(self) -> None:
        self.branches: dict[str, dict[str, str]] = {}
        self.calls: list[tuple[list[str], str]] = []
        self.next_pr = 31

    def _branch(self, cwd: str) -> str:
        script = self.branches.get(cwd) or {}
        return next(iter(script), RELEASE)

    def _head(self, cwd: str) -> str:
        script = self.branches.get(cwd) or {}
        return script.get(self._branch(cwd)) or SHA_R

    def run(self, args: list[str], *, cwd: str) -> gitgate.CompletedResult:
        self.calls.append((list(args), cwd))
        if args and args[0] == "gh":
            return self._gh(args)
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
            return gitgate.CompletedResult(0, "", "")
        if "cat-file" in args:
            return gitgate.CompletedResult(0, "", "")
        if "worktree" in args or "push" in args or "fetch" in args:
            return gitgate.CompletedResult(0, "", "")
        if "merge-base" in args:
            return gitgate.CompletedResult(0, "", "")
        return gitgate.CompletedResult(0, "", "")

    def _gh(self, args: list[str]) -> gitgate.CompletedResult:
        if "create" in args:
            pr = self.next_pr
            self.next_pr += 1
            return gitgate.CompletedResult(0, f"https://github.com/x/y/pull/{pr}\n", "")
        if "close" in args:
            return gitgate.CompletedResult(0, "", "")
        # every other gh query (pr view mergeable / mergeCommit / state / number,url)
        # answers empty → pr_mergeable grades it UNKNOWN → decide routes to the agent.
        return gitgate.CompletedResult(0, "", "")


class FakeBashRunner:
    def run(self, cmd: str, cwd: str, timeout_s: int, env: dict[str, str] | None) -> Completed:
        return Completed(exit_code=0, output="ok", timed_out=False)


def _harness(tmp_path: Path, *, stops: list[dict[str, Any]]) -> dict[str, Any]:
    root = tmp_path / GOAL_ID
    root.mkdir(parents=True, exist_ok=True)
    (root / "goal.enroll.json").write_text(json.dumps(ENROLL), encoding="utf-8")
    git = FakeGitRunner()
    git.branches["/goal/repo"] = {RELEASE: SHA_R, "main": SHA_MAIN}
    git.branches["/wt/dd-01"] = {DD_BRANCH: SHA_D}
    return {
        "root": root,
        "invoker": FakeInvoker(stops),
        "git": git,
        "bash": FakeBashRunner(),
    }


def _run(harness: dict[str, Any], tmp_path: Path) -> int:
    return engine.run_engine(
        GOAL_ID,
        engine_root=str(tmp_path),
        agent_invoker=harness["invoker"],
        git_runner=harness["git"],
        bash_runner=harness["bash"],
        gh_runner=object(),  # never reached: pr_mergeable grades UNKNOWN → merge agent
        timeout_s=60,
    )


def _happy_stops() -> list[dict[str, Any]]:
    return [
        _dispatch_stop(),
        _impl_committed(),
        _review("cr", "pass"),
        _review("fr", "pass"),
        _approve(),
        _dd_merge(),
        _done_stop(),
        _release_merge(),
    ]


# ---------------------------------------------------------------------------
# 1. argument parsing + the --engine-root default
# ---------------------------------------------------------------------------


def test_arg_parser_defaults_engine_root() -> None:
    parser = engine.build_parser()
    args = parser.parse_args(["--goal-id", "g-abcdef"])
    assert args.engine_root == runroot.DEFAULT_ENGINE_ROOT
    assert args.goal_id == "g-abcdef"

    args = parser.parse_args(["--goal-id", "g-abcdef", "--engine-root", "/tmp/roots"])
    assert args.engine_root == "/tmp/roots"


# ---------------------------------------------------------------------------
# 2. a missing enroll file is a startup error: non-zero exit, one printed line
# ---------------------------------------------------------------------------


def test_missing_enroll_is_startup_error(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = engine.main(["--goal-id", GOAL_ID, "--engine-root", str(tmp_path)])
    assert code == engine.EXIT_STARTUP_ERROR
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert "enroll" in err[0]


def test_bad_goal_id_is_startup_error(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = engine.main(["--goal-id", "not-an-id", "--engine-root", str(tmp_path)])
    assert code == engine.EXIT_STARTUP_ERROR
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


# ---------------------------------------------------------------------------
# 3. end-to-end: dispatch → impl → review → review → approve → merge → done
# ---------------------------------------------------------------------------


def test_end_to_end_happy_path(tmp_path: Path) -> None:
    harness = _harness(tmp_path, stops=_happy_stops())
    code = _run(harness, tmp_path)

    assert code == engine.EXIT_DONE
    root = harness["root"]
    kinds = [ev.kind for ev in events.EventLog(root).read()]

    assert kinds[0] == "engine.started"
    assert "goal.turn.started" in kinds
    assert "dd.dispatched" in kinds
    assert "dd.merged" in kinds
    assert "goal.done" in kinds
    assert kinds[-1] == "engine.exiting"

    # the required order, ignoring interleaved events
    def index(kind: str) -> int:
        return kinds.index(kind)

    assert index("engine.started") < index("goal.turn.started") < index("dd.dispatched")
    assert index("dd.merged") < index("goal.done") < index("engine.exiting")

    started = next(ev for ev in events.EventLog(root).read() if ev.kind == "engine.started")
    assert isinstance(started.payload["pid"], int)

    # exactly one DD ran to merged, and the two turns plus the four DD agents and
    # two merge agents all went through the injected invoker.
    assert len(harness["invoker"].calls) == 8
    assert [
        json.loads(call["user_prompt"].split("\n", 1)[1])["schema"]
        for call in harness["invoker"].calls
    ] == [
        "goal.turn.in/1",
        "impl.in/1",
        "review.in/1",
        "review.in/1",
        "goal.review.in/1",
        "merge.in/1",
        "goal.turn.in/1",
        "merge.in/1",
    ]


# ---------------------------------------------------------------------------
# 4. replay is idempotent: re-folding the same events.jsonl gives the same state
# ---------------------------------------------------------------------------


def test_replay_fold_is_idempotent(tmp_path: Path) -> None:
    harness = _harness(tmp_path, stops=_happy_stops())
    assert _run(harness, tmp_path) == engine.EXIT_DONE

    root = harness["root"]
    first = events.fold(events.EventLog(root).read())
    second = events.fold(events.EventLog(root).read())

    assert first == second
    assert first.terminal and first.state == "done"
    assert first.turn_no == 2
    assert [summary.outcome for summary in first.dd_history] == ["merged"]
