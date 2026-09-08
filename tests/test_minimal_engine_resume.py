"""Tests for dd-25: engine recovery = replay (protocol §11).

Everything drives ``engine.run_engine`` against a pre-seeded tmp goal root plus
a scripted ``FakeInvoker`` / ``FakeGitRunner``, so the resume dispatch in
``run_engine`` is asserted end-to-end without touching the real agent-run or
git. The goal graph is the *real* one, but the DD seams are never reached in
these cases (a scripted ``blocked`` Goal Agent terminates the loop before any
dispatch), except where a test asserts the injected resume handoff itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.minimal import events as events_mod

try:
    from langgraph.checkpoint.memory import InMemorySaver  # noqa: F401

    from fleet_graph.minimal import engine, gitgate
    from fleet_graph.minimal.control import ControlLog
    from fleet_graph.minimal.events import EventLog
except ModuleNotFoundError as exc:
    # engine imports goalgraph/ddgraph, which are LangGraph graphs: exercising
    # them needs langgraph. Bare ``pytest`` resolves to a user-site interpreter
    # without the project's deps installed, so a missing langgraph downgrades to
    # per-test skips rather than a collection error (a module-level importorskip
    # would leave nothing collected and pytest exits 5). ``make verify`` runs
    # the full suite under uv, where langgraph==1.2.11 is pinned.
    if exc.name != "langgraph":
        raise
    engine = None
    gitgate = None
    ControlLog = None
    EventLog = None

pytestmark = pytest.mark.skipif(
    engine is None,
    reason="langgraph not installed — engine resume runs are exercised under make verify",
)

GOAL_ID = "g-7f3a2c"
RELEASE = "release/loopx-minimal"

SHA_R = "a" * 40  # release tip
SHA_X = "f" * 40  # a release_head that is not on the remote

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


def _blocked_stop() -> dict[str, Any]:
    return {
        "schema": "goal.turn/1",
        "stop": "blocked",
        "summary": "先停",
        "blocked": {"kind": "external", "detail": "upstream down"},
    }


class FakeInvoker:
    """A queue of Stop objects; records argv / prompts of every agent call."""

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

    def in_obj(self, index: int) -> dict[str, Any]:
        """The ``*.in/1`` object of call ``index`` (user prompt minus preamble)."""
        return json.loads(self.calls[index]["user_prompt"].split("\n", 1)[1])


class FakeGitRunner:
    """Answers read-only git queries; ``merge-base --is-ancestor`` is reachable
    only for shas in ``reachable``."""

    def __init__(self) -> None:
        self.branches: dict[str, dict[str, str]] = {}
        self.reachable: set[str] = set()
        self.calls: list[tuple[list[str], str]] = []

    def _branch(self, cwd: str) -> str:
        script = self.branches.get(cwd) or {}
        return next(iter(script), RELEASE)

    def _head(self, cwd: str) -> str:
        script = self.branches.get(cwd) or {}
        return script.get(self._branch(cwd)) or SHA_R

    def run(self, args: list[str], *, cwd: str) -> gitgate.CompletedResult:
        self.calls.append((list(args), cwd))
        if args and args[0] == "gh":
            return gitgate.CompletedResult(0, "", "")
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
        if "merge-base" in args:
            sha = args[-2]
            return gitgate.CompletedResult(0 if sha in self.reachable else 1, "", "")
        return gitgate.CompletedResult(0, "", "")


class Harness:
    """One goal root, pre-seeded with events, ready for ``run_engine``."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        seed: list[tuple[str, dict[str, Any], str | None]],
        stops: list[dict[str, Any]],
        git: FakeGitRunner | None = None,
    ) -> None:
        root = tmp_path / GOAL_ID
        root.mkdir(parents=True, exist_ok=True)
        (root / "goal.enroll.json").write_text(json.dumps(ENROLL), encoding="utf-8")
        self.log = EventLog(root)
        for kind, payload, dd_id in seed:
            self.log.append(kind, payload, dd_id=dd_id)
        self.invoker = FakeInvoker(stops)
        self.git = git if git is not None else FakeGitRunner()
        self.git.branches["/goal/repo"] = {RELEASE: SHA_R, "main": SHA_X}
        self.git.reachable.add(SHA_R)
        self.root = root

    def run(self, tmp_path: Path) -> int:
        return engine.run_engine(
            GOAL_ID,
            engine_root=str(tmp_path),
            agent_invoker=self.invoker,
            git_runner=self.git,
            bash_runner=object(),
            gh_runner=object(),
            timeout_s=60,
        )

    def kinds(self) -> list[str]:
        return [ev.kind for ev in self.log.read()]

    def events_of(self, kind: str) -> list[Any]:
        return [ev for ev in self.log.read() if ev.kind == kind]


# ---------------------------------------------------------------------------
# ① empty log: no engine.resumed, today's behavior unchanged
# ---------------------------------------------------------------------------


def test_empty_log_starts_fresh_without_resumed(tmp_path: Path) -> None:
    harness = Harness(tmp_path, seed=[], stops=[_blocked_stop()])

    assert harness.run(tmp_path) == engine.EXIT_BLOCKED

    kinds = harness.kinds()
    assert "engine.resumed" not in kinds
    assert kinds[0] == "engine.started"
    assert "goal.blocked" in kinds


# ---------------------------------------------------------------------------
# ② terminal log: immediate exit, no resumed, no engine.started
# ---------------------------------------------------------------------------


def test_terminal_log_exits_without_resumed(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path, seed=[("goal.enrolled", {"title": "t"}, None), ("goal.done", {}, None)], stops=[]
    )

    assert harness.run(tmp_path) == engine.EXIT_DONE
    assert harness.kinds() == ["goal.enrolled", "goal.done"]
    assert harness.invoker.calls == []


# ---------------------------------------------------------------------------
# ③ last event goal.turn.started: agent.failed(lost_on_restart) + re-run the
#    same turn number
# ---------------------------------------------------------------------------


def test_lost_goal_turn_reruns_same_turn_number(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        seed=[
            ("goal.enrolled", {"title": "t"}, None),
            ("goal.turn.started", {"turn_no": 1}, None),
        ],
        stops=[_blocked_stop()],
    )

    assert harness.run(tmp_path) == engine.EXIT_BLOCKED

    failed = harness.events_of("agent.failed")
    assert len(failed) == 1
    assert failed[0].payload["stage"] == "goal_turn"
    assert failed[0].payload["detail"] == "lost_on_restart"
    assert harness.invoker.in_obj(0)["turn_no"] == 1


# ---------------------------------------------------------------------------
# ④ last event dd.stage.started: dd.failed(lost_on_restart) + the DD conclusion
#    lands in the next turn's input
# ---------------------------------------------------------------------------


def test_inflight_dd_ends_lost_and_feeds_last_dd(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        seed=[
            ("goal.enrolled", {"title": "t"}, None),
            ("goal.turn.started", {"turn_no": 1}, None),
            ("dd.dispatched", {"spec_text": "做 X", "branch": "dd/x"}, "dd-01"),
            ("dd.stage.started", {"stage": "impl"}, "dd-01"),
        ],
        stops=[_blocked_stop()],
    )

    assert harness.run(tmp_path) == engine.EXIT_BLOCKED

    failed = harness.events_of("dd.failed")
    assert len(failed) == 1
    assert failed[0].dd_id == "dd-01"
    assert failed[0].payload["stage"] == "impl"
    assert "lost_on_restart" in failed[0].payload["detail"]

    in_obj = harness.invoker.in_obj(0)
    assert in_obj["turn_no"] == 2  # the DD closed out → the *next* turn
    last_dd = in_obj["last_dd"]
    assert last_dd["dd_id"] == "dd-01"
    assert last_dd["outcome"] == "failed"
    assert "lost_on_restart" in last_dd["failure"]["detail"]
    assert any("lost_on_restart" in w for w in in_obj["warnings"])


# ---------------------------------------------------------------------------
# ⑤ a real ControlLog with 4 lines and control.received seq up to 3: only the
#    4th line is re-recorded on resume
# ---------------------------------------------------------------------------


def test_resume_only_replays_new_control_lines(tmp_path: Path) -> None:
    seed = [
        ("goal.enrolled", {"title": "t"}, None),
        ("control.received", {"seq": 1, "op": "message", "text": "m1"}, None),
        ("control.received", {"seq": 2, "op": "message", "text": "m2"}, None),
        ("control.received", {"seq": 3, "op": "message", "text": "m3"}, None),
        ("goal.turn.started", {"turn_no": 1}, None),
    ]
    harness = Harness(tmp_path, seed=seed, stops=[_blocked_stop()])

    control_log = ControlLog(harness.root)
    for i in range(1, 5):
        control_log.append({"op": "message", "text": f"fresh-{i}"})

    assert harness.run(tmp_path) == engine.EXIT_BLOCKED

    seqs = [ev.payload.get("seq") for ev in harness.events_of("control.received")]
    assert seqs == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# ⑥ a recorded release_head that is gone from the remote → goal.blocked
#    (state_mismatch) + exit 1
# ---------------------------------------------------------------------------


def test_state_mismatch_blocks_and_exits_1(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        seed=[
            ("goal.enrolled", {"title": "t"}, None),
            ("dd.pr_opened", {"repo": "/wt", "number": 1, "release_head": SHA_X}, "dd-01"),
        ],
        stops=[],
    )

    assert harness.run(tmp_path) == engine.EXIT_BLOCKED

    blocked = harness.events_of("goal.blocked")
    assert len(blocked) == 1
    assert blocked[0].payload["kind"] == "state_mismatch"
    assert harness.kinds()[-1] == "engine.exiting"


# ---------------------------------------------------------------------------
# ⑦ engine.resumed.from_seq == fold.last_seq
# ---------------------------------------------------------------------------


def test_engine_resumed_from_seq_equals_fold_last_seq(tmp_path: Path) -> None:
    seed = [
        ("goal.enrolled", {"title": "t"}, None),
        ("goal.turn.started", {"turn_no": 1}, None),
    ]
    harness = Harness(tmp_path, seed=seed, stops=[_blocked_stop()])
    expected_last_seq = events_mod.fold(harness.log.read()).last_seq

    assert harness.run(tmp_path) == engine.EXIT_BLOCKED

    resumed = harness.events_of("engine.resumed")
    assert len(resumed) == 1
    assert resumed[0].payload["from_seq"] == expected_last_seq
