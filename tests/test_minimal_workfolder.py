"""Tests for the GO-19 work-folder writeback (dd-31).

The pure ``progress_line`` renderer is tested directly. The wiring is exercised
through ``run_goal`` with a fake ``WorkFolderWriter`` recording every
``append_progress`` / ``append_findings`` call, plus a scribe-capable invoker
for the findings mirror. The four spec acceptance cases:

- the four goal-level boundaries each append one progress line carrying a turn /
  dd identifier;
- only ``warn`` / ``high`` observations reach findings, never ``info``;
- a writer that raises on every call still lets the goal reach ``done``, adding
  only ``goal.warning`` events (a WF outage never blocks the loop);
- with ``wf_writer`` defaulting to None nothing is written (pre-existing behavior).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.minimal import gitgate
from fleet_graph.minimal.control import ControlLog
from fleet_graph.minimal.events import EventLog
from fleet_graph.minimal.workfolder import progress_line

try:
    from langgraph.checkpoint.memory import InMemorySaver

    from fleet_graph.minimal.goalgraph import GoalDeps, run_goal
except ModuleNotFoundError as exc:
    # goalgraph is a LangGraph graph: exercising it needs the real dependency
    # (see the dd-17/dd-18 precedent for the per-test skip under bare pytest).
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

SHA_R = "a" * 40
SHA_D = "b" * 40
SHA_M = "c" * 40

ENROLL: dict[str, Any] = {
    "schema": "goal.enroll/1",
    "work_folder": "wf-ab12cd",
    "title": "把 X 功能做出来",
    "goal_text": "把 X 做出来并通过验收",
    "source_branch": RELEASE,
    "repos": [{"path": "/goal/repo", "remote": "origin", "target_branch": "main"}],
    "acceptance": ["make test"],
}


# ---------------------------------------------------------------------------
# 1. progress_line: the pure renderer
# ---------------------------------------------------------------------------


def test_progress_line_turn_carries_turn_identifier() -> None:
    line = progress_line(
        "goal.turn.finished", {"turn_no": 3, "stop": "dispatch", "summary": "先派一张单"}, ts="T"
    )
    assert "turn 3" in line
    assert "dispatch" in line
    assert "先派一张单" in line
    assert line.startswith("T ")


def test_progress_line_dd_carries_dd_identifier() -> None:
    merged = progress_line("dd.merged", {"dd_id": "dd-07", "summary": "合入 release"}, ts="T")
    assert "dd-07" in merged
    assert "merged" in merged
    failed = progress_line("dd.failed", {"dd_id": "dd-08", "summary": "做不了"}, ts="T")
    assert "dd-08" in failed
    assert "failed" in failed


def test_progress_line_done_and_blocked() -> None:
    done = progress_line("goal.done", {"summary": "X 完成"}, ts="T")
    assert "done" in done
    assert "X 完成" in done

    blocked = progress_line("goal.blocked", {"kind": "external", "summary": "缺依赖"}, ts="T")
    assert "blocked" in blocked
    assert "external" in blocked
    assert "缺依赖" in blocked


def test_progress_line_is_a_single_line_without_summary() -> None:
    assert progress_line("goal.done", {}, ts="T") == "T goal done"


# ---------------------------------------------------------------------------
# shared goalgraph harness pieces
# ---------------------------------------------------------------------------


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


class FakeGitRunner:
    def __init__(self) -> None:
        self.branches: dict[str, dict[str, str]] = {}

    def _head(self, cwd: str) -> str:
        return next(iter((self.branches.get(cwd) or {}).values()), SHA_R)

    def run(self, args: list[str], *, cwd: str) -> gitgate.CompletedResult:
        if "ls-remote" in args:
            branch = args[-1].removeprefix("refs/heads/")
            sha = (self.branches.get(cwd) or {}).get(branch)
            return gitgate.CompletedResult(0, f"{sha}\t{args[-1]}\n" if sha else "", "")
        if "rev-parse" in args:
            if "--abbrev-ref" in args:
                branch = next(iter((self.branches.get(cwd) or {}).keys()), RELEASE)
                return gitgate.CompletedResult(0, branch + "\n", "")
            return gitgate.CompletedResult(0, self._head(cwd) + "\n", "")
        return gitgate.CompletedResult(0, "", "")


class FakeRunDD:
    def __init__(self, log: EventLog) -> None:
        self.log = log
        self.calls: list[dict[str, Any]] = []

    def __call__(self, dispatch_obj: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(json.loads(json.dumps(dispatch_obj)))
        dd_id = f"dd-{len(self.calls):02d}"
        self.log.append("dd.dispatched", {"spec_text": dispatch_obj.get("spec_text")}, dd_id=dd_id)
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


class FakeFinalMerge:
    def __call__(self) -> tuple[str, dict[str, Any]]:
        return ("merged", {"merged_commit": SHA_M, "summary": "合到 main"})


class FakeWorkFolderWriter:
    """Records every WF write: progress calls (work_folder, line) and findings calls."""

    def __init__(self) -> None:
        self.progress_calls: list[tuple[str, str]] = []
        self.findings_calls: list[tuple[str, list[str]]] = []

    def append_progress(self, work_folder: str, line: str) -> None:
        self.progress_calls.append((work_folder, line))

    def append_findings(self, work_folder: str, lines: list[str]) -> None:
        self.findings_calls.append((work_folder, list(lines)))


class RaisingWorkFolderWriter:
    """Every WF write raises: a WF outage must never block the goal (GO-19)."""

    def append_progress(self, work_folder: str, line: str) -> None:
        del work_folder, line
        raise RuntimeError("work-folder MCP is down")

    def append_findings(self, work_folder: str, lines: list[str]) -> None:
        del work_folder, lines
        raise RuntimeError("work-folder MCP is down")


def _scribe_mixed(in_obj: dict[str, Any], idx: int) -> tuple[int, str]:
    """A scribe/1 ``observed`` with warn + high + info observations (all in-range)."""
    del idx
    until = in_obj["until_seq"]
    observations = [
        {
            "kind": "anomaly",
            "severity": "warn",
            "title": "warn 条",
            "summary": "值得注意",
            "evidence": [{"event_seq": until}],
        },
        {
            "kind": "quality",
            "severity": "high",
            "title": "high 条",
            "summary": "严重",
            "evidence": [{"event_seq": until}],
        },
        {
            "kind": "progress",
            "severity": "info",
            "title": "info 条",
            "summary": "正常推进",
            "evidence": [{"event_seq": until}],
        },
    ]
    return 0, json.dumps(
        {"schema": "scribe/1", "stop": "observed", "observations": observations},
        ensure_ascii=False,
    )


class FakeInvoker:
    """Scripts goal turns from a queue and (optionally) scribe runs from ``scribe_fn``."""

    def __init__(self, goal_stops: list[dict[str, Any]], scribe_fn: Any = None) -> None:
        self.goal_stops = list(goal_stops)
        self.scribe_fn = scribe_fn
        self.goal_calls: list[dict[str, Any]] = []
        self.scribe_calls: list[dict[str, Any]] = []

    def __call__(
        self, argv: list[str], *, system_prompt: str | None, user_prompt: str
    ) -> tuple[int, str]:
        role = argv[argv.index("--role") + 1]
        call = {"argv": list(argv), "system_prompt": system_prompt, "user_prompt": user_prompt}
        if role == "scribe":
            self.scribe_calls.append(call)
            in_obj = json.loads(user_prompt.split("\n", 1)[1])
            return self.scribe_fn(in_obj, len(self.scribe_calls) - 1)
        self.goal_calls.append(call)
        return 0, json.dumps(self.goal_stops.pop(0), ensure_ascii=False)


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        goal_stops: list[dict[str, Any]],
        wf_writer: Any = None,
        scribe_fn: Any = None,
        scribe_enabled: bool = False,
    ) -> None:
        root = tmp_path / GOAL_ID
        self.log = EventLog(root)
        self.control = ControlLog(root)
        self.log.append("goal.enrolled", {"title": ENROLL["title"]})
        self.runner = FakeGitRunner()
        self.runner.branches["/goal/repo"] = {RELEASE: SHA_R}
        self.runner.branches["/wt/dd-01"] = {DD_BRANCH: SHA_D}
        self.invoker = FakeInvoker(goal_stops, scribe_fn=scribe_fn)
        self.writer = wf_writer
        self.deps = GoalDeps(
            event_log=self.log,
            control_log=self.control,
            agent_invoker=self.invoker,
            git_runner=self.runner,
            run_dd=FakeRunDD(self.log),
            final_merge=FakeFinalMerge(),
            wf_writer=wf_writer,
            session_root=str(root / "sessions"),
            timeout_s=60,
            scribe_enabled=scribe_enabled,
        )

    def kinds(self) -> list[str]:
        return [ev.kind for ev in self.log.read()]

    def events_of(self, kind: str) -> list[Any]:
        return [ev for ev in self.log.read() if ev.kind == kind]

    def warnings(self) -> list[str]:
        return [ev.payload.get("message", "") for ev in self.events_of("goal.warning")]


# ---------------------------------------------------------------------------
# 2. the four goal-level boundaries each append one progress line
# ---------------------------------------------------------------------------


def test_four_boundaries_each_append_one_progress_line(tmp_path: Path) -> None:
    writer = FakeWorkFolderWriter()
    harness = Harness(tmp_path, goal_stops=[dispatch_stop(), done_stop()], wf_writer=writer)

    result = run_goal(
        harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL), checkpointer=InMemorySaver()
    )

    assert result["stop"] == "done"
    assert len(writer.progress_calls) == 4
    assert all(work_folder == "wf-ab12cd" for work_folder, _line in writer.progress_calls)
    lines = [line for _work_folder, line in writer.progress_calls]

    # turn 1 ends → dd 结束 → turn 2 ends → goal done
    assert "turn 1" in lines[0]
    assert lines[0].endswith("）：先派一张单做 X")
    assert "dd-01" in lines[1] and "merged" in lines[1] and "实现了 X" in lines[1]
    assert "turn 2" in lines[2]
    assert "done" in lines[3] and "X 已在 release 上完成" in lines[3]

    # WF writes never add events when the writer succeeds.
    assert not any("work folder" in message for message in harness.warnings())


# ---------------------------------------------------------------------------
# 3. only warn / high observations reach findings, never info
# ---------------------------------------------------------------------------


def test_findings_mirror_only_warn_and_high(tmp_path: Path) -> None:
    writer = FakeWorkFolderWriter()
    harness = Harness(
        tmp_path,
        goal_stops=[dispatch_stop(), done_stop()],
        wf_writer=writer,
        scribe_fn=_scribe_mixed,
        scribe_enabled=True,
    )

    result = run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    assert result["stop"] == "done"
    assert harness.invoker.scribe_calls, "the scribe must have run"
    # findings are mirrored once per goal boundary (turn → dd → turn = 3 scribe runs).
    assert len(writer.findings_calls) == 3
    for work_folder, lines in writer.findings_calls:
        assert work_folder == "wf-ab12cd"
        assert len(lines) == 2
        joined = "\n".join(lines)
        assert "warn 条" in joined
        assert "high 条" in joined
        assert "info 条" not in joined
        assert "[warn]" in joined
        assert "[high]" in joined


# ---------------------------------------------------------------------------
# 4. a raising writer only adds goal.warning; the goal still finishes done
# ---------------------------------------------------------------------------


def test_raising_writer_neither_blocks_nor_diverts_the_goal(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path, goal_stops=[dispatch_stop(), done_stop()], wf_writer=RaisingWorkFolderWriter()
    )

    result = run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    assert result["stop"] == "done"
    warnings = harness.warnings()
    assert warnings, "each failed WF write must land a goal.warning"
    assert len(warnings) == 4
    assert all("work folder progress append failed" in message for message in warnings)
    # the terminal done is still the last event: the fold stays a clean done.
    assert harness.kinds()[-1] == "goal.done"


# ---------------------------------------------------------------------------
# 5. wf_writer defaults to None: nothing is written, no extra events
# ---------------------------------------------------------------------------


def test_wf_writer_default_none_writes_nothing(tmp_path: Path) -> None:
    harness = Harness(tmp_path, goal_stops=[dispatch_stop(), done_stop()], wf_writer=None)

    result = run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    assert result["stop"] == "done"
    assert not any("work folder" in message for message in harness.warnings())
    assert "goal.done" in harness.kinds()
