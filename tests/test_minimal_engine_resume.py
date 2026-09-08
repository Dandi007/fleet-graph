"""Tests for dd-25 + dd-30: engine recovery = replay (protocol §11).

Everything drives ``engine.run_engine`` against a pre-seeded tmp goal root plus
a scripted ``FakeInvoker`` / ``FakeGitRunner`` / ``FakeBashRunner``, so the
resume dispatch in ``run_engine`` is asserted end-to-end without touching the
real agent-run, git, or acceptance shell. The goal graph and the DD seams are
the *real* wiring: the dd-30 cases re-enter an in-flight DD through the
``run_dd`` seam (impl lost → impl re-runs; CR done / FR lost → FR continues
with the CR conclusion; acceptance mid-round → the whole batch re-runs;
``dd.merged`` → the folded result is handed to the next turn, no re-entry),
while the goal-level cases (fresh start, terminal exit, lost goal turn,
control cursor, state mismatch) never reach a DD.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.minimal import events as events_mod
from fleet_graph.minimal.acceptance import Completed

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
DD_ID = "dd-01"
DD_BRANCH = f"dd/{GOAL_ID}/{DD_ID}"
DD_WORKTREE = "/wt/dd-01"

SHA_R = "a" * 40  # release tip
SHA_D = "b" * 40  # dd branch tip / impl commit
SHA_M = "d" * 40  # DD merged commit
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

DISPATCH: dict[str, Any] = {
    "spec_text": "实现 X",
    "repos": [
        {
            "path": DD_WORKTREE,
            "remote": "origin",
            "branch": DD_BRANCH,
            "spec_path": "docs/specs/x.md",
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


def _impl_committed() -> dict[str, Any]:
    return {"schema": "impl/1", "stop": "committed", "commit": SHA_D, "summary": "done"}


def _review(role: str, stop: str) -> dict[str, Any]:
    return {"schema": "review/1", "role": role, "stop": stop, "summary": stop}


def _approve() -> dict[str, Any]:
    return {"schema": "goal.review/1", "stop": "approve", "summary": "ok"}


def _dd_merge() -> dict[str, Any]:
    return {"schema": "merge/1", "stop": "merged", "merged_commit": SHA_M, "summary": "合入"}


def _dispatch_turn_prefix() -> list[tuple[str, dict[str, Any], str | None]]:
    """The dispatching turn: enrolled → turn 1 started → turn finished (dispatch).

    ``goal.turn.finished`` carries the whole Stop object — including the
    ``dispatch`` — exactly as stagerunner writes it before the graph routes to
    the run_dd node; the engine's §11 resume folds the dispatch back out of it.
    """
    return [
        ("goal.enrolled", {"title": "t"}, None),
        ("goal.turn.started", {"turn_no": 1}, None),
        (
            "goal.turn.finished",
            {
                "run_id": f"goal-{GOAL_ID}-turn-1",
                "stop": "dispatch",
                "summary": "派单",
                "dispatch": dict(DISPATCH),
            },
            None,
        ),
    ]


def _dd_opened() -> list[tuple[str, dict[str, Any], str]]:
    """The DD's own opening events: dispatched → PR opened."""
    return [
        (
            "dd.dispatched",
            {"spec_text": "实现 X", "branch": DD_BRANCH, "head_commit": SHA_R},
            DD_ID,
        ),
        (
            "dd.pr_opened",
            {"repo": DD_WORKTREE, "number": 31, "url": "https://github.com/x/y/pull/31"},
            DD_ID,
        ),
    ]


def _stage_finished(stage: str, stop: str, **extra: Any) -> tuple[str, dict[str, Any], str]:
    return ("dd.stage.finished", {"stage": stage, "stop": stop, **extra}, DD_ID)


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


class FakeBashRunner:
    """Every acceptance command exits 0; the commands are recorded."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, cmd: str, cwd: str, timeout_s: int, env: dict[str, str] | None) -> Completed:
        self.calls.append(cmd)
        return Completed(exit_code=0, output="ok", timed_out=False)


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
        self.git.branches[DD_WORKTREE] = {DD_BRANCH: SHA_D}
        self.git.reachable.add(SHA_R)
        self.bash = FakeBashRunner()
        self.root = root

    def run(self, tmp_path: Path) -> int:
        return engine.run_engine(
            GOAL_ID,
            engine_root=str(tmp_path),
            agent_invoker=self.invoker,
            git_runner=self.git,
            bash_runner=self.bash,
            gh_runner=object(),
            timeout_s=60,
            scribe_enabled=False,  # resume runs do not script the scribe role
        )

    def kinds(self) -> list[str]:
        return [ev.kind for ev in self.log.read()]

    def events_of(self, kind: str) -> list[Any]:
        return [ev for ev in self.log.read() if ev.kind == kind]

    def in_schemas(self) -> list[str]:
        """The ``*.in/1`` schema of every agent call, in order."""
        return [
            json.loads(call["user_prompt"].split("\n", 1)[1])["schema"]
            for call in self.invoker.calls
        ]


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
# ④ last event dd.stage.started but no goal.turn.finished carries the
#    dispatch (a log no real run produces): the DD cannot be re-entered →
#    closed lost (dd-30's defensive fallback), the conclusion still lands in
#    the next turn's input
# ---------------------------------------------------------------------------


def test_unfoldable_dispatch_closes_dd_as_lost_and_feeds_last_dd(tmp_path: Path) -> None:
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


# ---------------------------------------------------------------------------
# ⑧ dd-30 ①: impl started then lost → agent.failed(lost_on_restart), impl
#    re-runs at the same round, the DD is never judged failed
# ---------------------------------------------------------------------------


def test_lost_impl_run_restarts_and_dd_resumes(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        seed=[
            *_dispatch_turn_prefix(),
            *_dd_opened(),
            ("dd.stage.started", {"stage": "impl"}, DD_ID),
        ],
        stops=[
            _impl_committed(),
            _review("cr", "pass"),
            _review("fr", "pass"),
            _approve(),
            _dd_merge(),
            _blocked_stop(),
        ],
    )

    assert harness.run(tmp_path) == engine.EXIT_BLOCKED

    kinds = harness.kinds()
    assert "dd.failed" not in kinds  # the in-flight DD is no longer judged failed
    assert kinds.count("dd.dispatched") == 1  # same DD resumed, never re-dispatched
    failed = harness.events_of("agent.failed")
    assert [(f.payload["stage"], f.payload["detail"]) for f in failed] == [
        ("impl", "lost_on_restart")
    ]

    assert harness.in_schemas() == [
        "impl.in/1",  # impl re-ran
        "review.in/1",
        "review.in/1",
        "goal.review.in/1",
        "merge.in/1",
        "goal.turn.in/1",  # then the next goal turn
    ]
    impl_in = harness.invoker.in_obj(0)
    assert impl_in["round"] == 1
    assert "dd.merged" in kinds

    turn_in = harness.invoker.in_obj(5)
    assert turn_in["turn_no"] == 2  # the DD completed → the *next* turn
    assert turn_in["last_dd"]["dd_id"] == DD_ID
    assert turn_in["last_dd"]["outcome"] == "merged"
    assert "1 merged" in turn_in["dd_summary"]


# ---------------------------------------------------------------------------
# ⑨ dd-30 ②: CR finished, FR lost → resume at FR with the CR conclusion in
#    the FR input; CR is not re-run
# ---------------------------------------------------------------------------


def test_cr_done_fr_lost_resumes_at_fr(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        seed=[
            *_dispatch_turn_prefix(),
            *_dd_opened(),
            _stage_finished("impl", "committed", commit=SHA_D, summary="done"),
            ("dd.acceptance", {"cmd": "make base", "exit": 0, "index": 0, "total": 1}, DD_ID),
            _stage_finished("acceptance", "pass"),
            _stage_finished("cr", "pass", summary="cr ok", findings=[]),
        ],
        stops=[_review("fr", "pass"), _approve(), _dd_merge(), _blocked_stop()],
    )

    assert harness.run(tmp_path) == engine.EXIT_BLOCKED

    kinds = harness.kinds()
    assert "dd.failed" not in kinds
    assert kinds.count("dd.dispatched") == 1
    failed = harness.events_of("agent.failed")
    assert [(f.payload["stage"], f.payload["detail"]) for f in failed] == [
        ("cr", "lost_on_restart")
    ]

    assert harness.in_schemas() == [
        "review.in/1",  # fr only: cr is not re-run
        "goal.review.in/1",
        "merge.in/1",
        "goal.turn.in/1",
    ]
    fr_in = harness.invoker.in_obj(0)
    assert fr_in["role"] == "fr"
    # the CR conclusion survived the crash and rode into the FR input
    assert fr_in["cr_result"] == {"stop": "pass", "summary": "cr ok", "findings": []}
    assert "dd.merged" in kinds

    turn_in = harness.invoker.in_obj(3)
    assert turn_in["last_dd"]["outcome"] == "merged"
    assert [r["role"] for r in turn_in["last_dd"]["reviews"]] == ["cr", "fr"]


# ---------------------------------------------------------------------------
# ⑩ dd-30 ③: acceptance crashed mid-batch → the whole round's commands re-run
# ---------------------------------------------------------------------------


def test_mid_acceptance_reruns_the_whole_batch(tmp_path: Path) -> None:
    dispatch = {**DISPATCH, "acceptance_extra": ["make extra"]}
    harness = Harness(
        tmp_path,
        seed=[
            ("goal.enrolled", {"title": "t"}, None),
            ("goal.turn.started", {"turn_no": 1}, None),
            (
                "goal.turn.finished",
                {
                    "run_id": f"goal-{GOAL_ID}-turn-1",
                    "stop": "dispatch",
                    "summary": "派单",
                    "dispatch": dispatch,
                },
                None,
            ),
            *_dd_opened(),
            _stage_finished("impl", "committed", commit=SHA_D, summary="done"),
            ("dd.acceptance", {"cmd": "make base", "exit": 0, "index": 0, "total": 2}, DD_ID),
        ],
        stops=[
            _review("cr", "pass"),
            _review("fr", "pass"),
            _approve(),
            _dd_merge(),
            _blocked_stop(),
        ],
    )

    assert harness.run(tmp_path) == engine.EXIT_BLOCKED

    kinds = harness.kinds()
    assert "dd.failed" not in kinds
    # the whole batch re-ran from the first command (no mid-batch checkpoint)
    assert harness.bash.calls == ["make base", "make extra"]
    acceptance = [ev.payload["cmd"] for ev in harness.events_of("dd.acceptance")]
    assert acceptance == ["make base", "make base", "make extra"]
    finished = [
        ev.payload["stage"]
        for ev in harness.events_of("dd.stage.finished")
        if ev.payload["stage"] == "acceptance"
    ]
    assert finished == ["acceptance"]  # the interrupted round never finished
    assert "dd.merged" in kinds


# ---------------------------------------------------------------------------
# ⑪ dd-30 ④: dd.merged then crash → no re-entry, the folded result object is
#    handed to the next turn
# ---------------------------------------------------------------------------


def test_merged_dd_hands_result_to_next_turn_without_reentry(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        seed=[
            *_dispatch_turn_prefix(),
            *_dd_opened(),
            _stage_finished("impl", "committed", commit=SHA_D, summary="done"),
            ("dd.acceptance", {"cmd": "make base", "exit": 0, "index": 0, "total": 1}, DD_ID),
            _stage_finished("acceptance", "pass"),
            _stage_finished("cr", "pass", summary="ok", findings=[]),
            _stage_finished("fr", "pass", summary="ok", findings=[]),
            _stage_finished("goal_review", "approve", summary="ok"),
            _stage_finished("merge", "merged", merged_commit=SHA_M),
            ("dd.merged", {"merged_commit": SHA_M}, DD_ID),
        ],
        stops=[_blocked_stop()],
    )

    assert harness.run(tmp_path) == engine.EXIT_BLOCKED

    kinds = harness.kinds()
    # no re-entry at all: exactly the seeded DD events, no agent runs, no
    # lost-run marker, no re-dispatch
    assert kinds.count("dd.dispatched") == 1
    assert kinds.count("dd.merged") == 1
    assert harness.events_of("agent.failed") == []
    assert len(harness.invoker.calls) == 1  # the next goal turn only
    assert harness.bash.calls == []

    turn_in = harness.invoker.in_obj(0)
    assert turn_in["turn_no"] == 2
    assert turn_in["last_dd"]["dd_id"] == DD_ID
    assert turn_in["last_dd"]["outcome"] == "merged"
    assert turn_in["last_dd"]["merged_commit"] == SHA_M
    assert "1 merged" in turn_in["dd_summary"]


# ---------------------------------------------------------------------------
# ⑫ dd-30 ④ multi-DD: turn 1's DD merged and was consumed by turn 2, turn 2
#     dispatched a second DD that merged right before the crash → dd-02's
#     result (not dd-01's) is handed to turn 3, no re-entry, no re-dispatch
# ---------------------------------------------------------------------------


def _dd_cycle(dd_id: str) -> list[tuple[str, dict[str, Any], str]]:
    """One DD's full event cycle from dispatch to ``dd.merged``."""
    return [
        (
            "dd.dispatched",
            {"spec_text": "实现 X", "branch": f"dd/{GOAL_ID}/{dd_id}", "head_commit": SHA_R},
            dd_id,
        ),
        (
            "dd.pr_opened",
            {"repo": DD_WORKTREE, "number": 31, "url": "https://github.com/x/y/pull/31"},
            dd_id,
        ),
        (
            "dd.stage.finished",
            {"stage": "impl", "stop": "committed", "commit": SHA_D, "summary": "done"},
            dd_id,
        ),
        ("dd.acceptance", {"cmd": "make base", "exit": 0, "index": 0, "total": 1}, dd_id),
        ("dd.stage.finished", {"stage": "acceptance", "stop": "pass"}, dd_id),
        (
            "dd.stage.finished",
            {"stage": "cr", "stop": "pass", "summary": "ok", "findings": []},
            dd_id,
        ),
        (
            "dd.stage.finished",
            {"stage": "fr", "stop": "pass", "summary": "ok", "findings": []},
            dd_id,
        ),
        ("dd.stage.finished", {"stage": "goal_review", "stop": "approve", "summary": "ok"}, dd_id),
        ("dd.stage.finished", {"stage": "merge", "stop": "merged", "merged_commit": SHA_M}, dd_id),
        ("dd.merged", {"merged_commit": SHA_M}, dd_id),
    ]


def test_second_merged_dd_result_reaches_turn3_after_first_consumed(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        seed=[
            *_dispatch_turn_prefix(),
            *_dd_cycle("dd-01"),
            ("goal.turn.started", {"turn_no": 2}, None),
            (
                "goal.turn.finished",
                {
                    "run_id": f"goal-{GOAL_ID}-turn-2",
                    "stop": "dispatch",
                    "summary": "再派一单",
                    "dispatch": dict(DISPATCH),
                },
                None,
            ),
            *_dd_cycle("dd-02"),
        ],
        stops=[_blocked_stop()],
    )

    assert harness.run(tmp_path) == engine.EXIT_BLOCKED

    kinds = harness.kinds()
    assert kinds.count("dd.dispatched") == 2
    assert kinds.count("dd.merged") == 2
    assert harness.events_of("agent.failed") == []
    assert len(harness.invoker.calls) == 1
    assert harness.bash.calls == []

    turn_in = harness.invoker.in_obj(0)
    assert turn_in["turn_no"] == 3
    assert turn_in["last_dd"]["dd_id"] == "dd-02"
    assert turn_in["last_dd"]["outcome"] == "merged"
    assert turn_in["last_dd"]["merged_commit"] == SHA_M
    assert "2 merged" in turn_in["dd_summary"]
    assert "dd-02" in turn_in["dd_summary"]
