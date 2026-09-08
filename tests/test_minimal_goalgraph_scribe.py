"""Tests for the goal-level scribe stage (dd-23): the read-only sixth agent.

The scribe is wired into ``goalgraph`` behind the optional ``scribe_enabled``
seam, so the tests here drive ``run_goal`` with a ``FakeInvoker`` that scripts
*both* the Goal Agent turns and the scribe runs (routed by ``--role``). The
four spec acceptance cases are covered: the seam off leaves the event sequence
byte-identical (no ``scribe.*`` event), a turn→DD→turn(done) run with a fake
scribe invokes the scribe exactly once per goal boundary in the right order,
the three failure modes (invalid JSON / evidence-gate drop / raising invoker)
leave the goal running to ``goal.done`` with one ``scribe.failed`` each, and
the scribe's ``since_seq``/``until_seq`` cursor advances monotonically without
re-observing an already-seen segment.
"""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.minimal import gitgate
from fleet_graph.minimal.control import ControlLog
from fleet_graph.minimal.events import EventLog

try:
    from langgraph.checkpoint.memory import InMemorySaver

    from fleet_graph.minimal.goalgraph import GoalDeps, run_goal
except ModuleNotFoundError as exc:
    # goalgraph is a LangGraph graph: exercising it needs the real dependency.
    # Bare ``pytest`` resolves to a user-site interpreter without the project's
    # deps installed, so a missing langgraph downgrades to per-test skips rather
    # than a collection error (see dd-17/dd-18 precedent). ``make verify`` runs
    # the full suite under uv, where langgraph==1.2.11 is pinned.
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


class FakeGitRunner:
    """Answers the gates' read-only git queries from per-cwd scripts."""

    def __init__(self) -> None:
        self.branches: dict[str, dict[str, str]] = {}

    def _head(self, cwd: str) -> str:
        script = self.branches.get(cwd) or {}
        return next(iter(script.values()), SHA_R)

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
    """The injected DD seam: writes dd.* events and returns the §7 result."""

    def __init__(self, log: EventLog, *, outcome: str = "merged") -> None:
        self.log = log
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def __call__(self, dispatch_obj: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(json.loads(json.dumps(dispatch_obj)))
        dd_id = f"dd-{len(self.calls):02d}"
        self.log.append("dd.dispatched", {"spec_text": dispatch_obj.get("spec_text")}, dd_id=dd_id)
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
    """The injected release → target seam."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> tuple[str, dict[str, Any]]:
        self.calls += 1
        return ("merged", {"merged_commit": SHA_M, "summary": "合到 main"})


def valid_scribe(in_obj: dict[str, Any], idx: int) -> tuple[int, str]:
    """A scribe/1 ``observed`` with one in-range ``event_seq`` evidence entry."""
    observation = {
        "schema": "scribe/1",
        "stop": "observed",
        "observations": [
            {
                "kind": "progress",
                "severity": "info",
                "title": f"turn {idx} 正常推进",
                "summary": "这张 DD 落在 release 上，流程按预期前进",
                "evidence": [{"event_seq": in_obj["until_seq"]}],
            }
        ],
    }
    return 0, json.dumps(observation, ensure_ascii=False)


def invalid_json_scribe(in_obj: dict[str, Any], idx: int) -> tuple[int, str]:
    return 0, "this is not a protocol object, let alone JSON"


def gate_fail_scribe(in_obj: dict[str, Any], idx: int) -> tuple[int, str]:
    """A schema-valid scribe/1 whose only evidence is out of range (gate drops it)."""
    observation = {
        "schema": "scribe/1",
        "stop": "observed",
        "observations": [
            {
                "kind": "progress",
                "severity": "info",
                "title": "外指",
                "summary": "证据指向区间之外",
                "evidence": [{"event_seq": in_obj["until_seq"] + 10_000}],
            }
        ],
    }
    return 0, json.dumps(observation, ensure_ascii=False)


def raising_scribe(in_obj: dict[str, Any], idx: int) -> tuple[int, str]:
    raise RuntimeError("scribe runtime blew up")


class FakeInvoker:
    """Scripts both roles: goal turns from a queue, scribe runs from ``scribe_fn``."""

    def __init__(
        self,
        goal_stops: list[dict[str, Any]],
        scribe_fn: Any = None,
    ) -> None:
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

    def scribe_in(self, index: int) -> dict[str, Any]:
        return json.loads(self.scribe_calls[index]["user_prompt"].split("\n", 1)[1])


class Harness:
    """One wired goal root with an optional scribe seam."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        goal_stops: list[dict[str, Any]],
        scribe_fn: Any = None,
        scribe_enabled: bool = False,
        run_dd_outcome: str = "merged",
    ) -> None:
        root = tmp_path / GOAL_ID
        self.log = EventLog(root)
        self.control = ControlLog(root)
        self.log.append("goal.enrolled", {"title": ENROLL["title"]})
        self.runner = FakeGitRunner()
        self.runner.branches["/goal/repo"] = {RELEASE: SHA_R}
        self.runner.branches["/wt/dd-01"] = {DD_BRANCH: SHA_D}
        self.invoker = FakeInvoker(goal_stops, scribe_fn=scribe_fn)
        self.run_dd = FakeRunDD(self.log, outcome=run_dd_outcome)
        self.final_merge = FakeFinalMerge()
        self.deps = GoalDeps(
            event_log=self.log,
            control_log=self.control,
            agent_invoker=self.invoker,
            git_runner=self.runner,
            run_dd=self.run_dd,
            final_merge=self.final_merge,
            session_root=str(root / "sessions"),
            timeout_s=60,
            scribe_enabled=scribe_enabled,
        )

    def kinds(self) -> list[str]:
        return [ev.kind for ev in self.log.read()]

    def events_of(self, kind: str) -> list[Any]:
        return [ev for ev in self.log.read() if ev.kind == kind]


# ---------------------------------------------------------------------------
# 1. seam off: the event sequence is byte-identical (no scribe.* event)
# ---------------------------------------------------------------------------


def test_scribe_disabled_leaves_event_sequence_unchanged(tmp_path: Path) -> None:
    harness = Harness(tmp_path, goal_stops=[dispatch_stop(), done_stop()])

    result = run_goal(
        harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL), checkpointer=InMemorySaver()
    )

    assert result["stop"] == "done"
    assert harness.invoker.scribe_calls == []
    kinds = harness.kinds()
    assert not any(kind.startswith("scribe.") for kind in kinds)
    assert kinds == [
        "goal.enrolled",
        "goal.turn.started",
        "agent.exited",
        "goal.turn.finished",
        "dd.dispatched",
        "dd.merged",
        "goal.turn.started",
        "agent.exited",
        "goal.turn.finished",
        "goal.merged_to_target",
        "goal.done",
    ]


# ---------------------------------------------------------------------------
# 2. fake scribe invoker: exactly one scribe per goal boundary, in order
# ---------------------------------------------------------------------------


def test_scribe_runs_once_per_goal_boundary_in_order(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        goal_stops=[dispatch_stop(), done_stop()],
        scribe_fn=valid_scribe,
        scribe_enabled=True,
    )

    result = run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    assert result["stop"] == "done"
    assert len(harness.invoker.goal_calls) == 2
    # one per boundary: turn(done-dispatch) → DD → turn(done) = 3 scribe runs
    assert len(harness.invoker.scribe_calls) == 3

    observed = harness.events_of("scribe.observed")
    assert [ev.payload["trigger"] for ev in observed] == [
        "goal.turn.finished",
        "dd.merged",
        "goal.turn.finished",
    ]

    kinds = harness.kinds()
    positions = {kind: [i for i, k in enumerate(kinds) if k == kind] for kind in set(kinds)}
    assert sum(len(v) for v in positions.values()) == len(kinds)
    assert positions["scribe.observed"][0] > positions["goal.turn.finished"][0]
    assert positions["scribe.observed"][1] > positions["dd.merged"][0]
    assert positions["scribe.observed"][2] > positions["goal.turn.finished"][1]
    assert kinds[-1] == "goal.done"


# ---------------------------------------------------------------------------
# 3. the three failure modes: goal still runs to done, one scribe.failed each
# ---------------------------------------------------------------------------


def test_scribe_invalid_output_writes_failure_and_goal_done(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        goal_stops=[dispatch_stop(), done_stop()],
        scribe_fn=invalid_json_scribe,
        scribe_enabled=True,
    )

    result = run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    assert result["stop"] == "done"
    assert harness.run_dd.calls, "the DD must still run"
    failed = harness.events_of("scribe.failed")
    assert len(failed) == 3
    assert all(ev.payload["reason"] == "no_object" for ev in failed)
    assert harness.events_of("scribe.observed") == []
    kinds = harness.kinds()
    assert kinds[-1] == "goal.done"


def test_scribe_evidence_gate_drop_writes_failure_and_goal_done(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        goal_stops=[dispatch_stop(), done_stop()],
        scribe_fn=gate_fail_scribe,
        scribe_enabled=True,
    )

    result = run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    assert result["stop"] == "done"
    assert harness.run_dd.calls, "the DD must still run"
    failed = harness.events_of("scribe.failed")
    assert len(failed) == 3
    assert all(ev.payload["reason"] == "observation_without_evidence" for ev in failed)
    assert harness.events_of("scribe.observed") == []
    assert harness.kinds()[-1] == "goal.done"


def test_scribe_raising_invoker_writes_failure_and_goal_done(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        goal_stops=[dispatch_stop(), done_stop()],
        scribe_fn=raising_scribe,
        scribe_enabled=True,
    )

    result = run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    assert result["stop"] == "done"
    assert harness.run_dd.calls, "the DD must still run"
    failed = harness.events_of("scribe.failed")
    assert len(failed) == 3
    assert all(ev.payload["reason"] == "exception" for ev in failed)
    assert harness.events_of("scribe.observed") == []
    assert harness.kinds()[-1] == "goal.done"


# ---------------------------------------------------------------------------
# 4. the seq cursor is monotonic and never re-observes a segment
# ---------------------------------------------------------------------------


def test_scribe_cursor_is_monotonic_without_overlap(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        goal_stops=[dispatch_stop(), done_stop()],
        scribe_fn=valid_scribe,
        scribe_enabled=True,
    )

    run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    ranges = [
        (harness.invoker.scribe_in(i)["since_seq"], harness.invoker.scribe_in(i)["until_seq"])
        for i in range(len(harness.invoker.scribe_calls))
    ]
    assert len(ranges) == 3
    assert ranges[0][0] == 1
    for (since, until), (next_since, next_until) in pairwise(ranges):
        assert since <= until
        assert until < next_until, "until_seq must increase monotonically"
        assert next_since == until + 1, "the next run must start right after the last"


# ---------------------------------------------------------------------------
# valid observations are appended to observations.jsonl
# ---------------------------------------------------------------------------


def test_scribe_appends_observations_to_observations_log(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        goal_stops=[dispatch_stop(), done_stop()],
        scribe_fn=valid_scribe,
        scribe_enabled=True,
    )

    run_goal(harness.deps, goal_id=GOAL_ID, enroll=dict(ENROLL))

    obs_path = tmp_path / GOAL_ID / "observations.jsonl"
    assert obs_path.exists()
    lines = obs_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    for raw in lines:
        record = json.loads(raw)
        assert record["kind"] == "progress"
        assert record["trigger"] in ("goal.turn.finished", "dd.merged")
        assert record["seq_range"][0] <= record["seq_range"][1]
