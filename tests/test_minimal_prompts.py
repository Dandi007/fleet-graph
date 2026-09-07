"""Tests for the minimal prompt layer (DD-10): ``*.in/1`` builders + rendering.

Everything here pins the GO-17 / GO-24 contract: the six input objects match
protocol.md field-for-field (key set, key order, schema literal, ``null``
retention), contract misuse raises ``ValueError`` before any object is built,
the user prompt is exactly one preamble line plus the input object as JSON,
and the system prompt carries the role's persona, schema literal and full stop
enum. No filesystem, no subprocess -- the module under test is pure.
"""

from __future__ import annotations

import json

import pytest

import fleet_graph.minimal as minimal_pkg
from fleet_graph.minimal.agentrun import SessionPolicy
from fleet_graph.minimal.prompts import (
    HISTORY_NOTE,
    IN_SCHEMAS,
    ROLE_PERSONA,
    SCHEMA_GOAL_REVIEW_IN,
    SCHEMA_GOAL_TURN_IN,
    SCHEMA_IMPL_IN,
    SCHEMA_MERGE_IN,
    SCHEMA_REVIEW_IN,
    SCHEMA_SCRIBE_IN,
    build_goal_review_in,
    build_goal_turn_in,
    build_impl_in,
    build_merge_in,
    build_review_in,
    build_scribe_in,
    history_handle,
    needs_system_prompt,
    render_system_prompt,
    render_user_prompt,
)

SHA = "1" * 40

GOAL_TURN_KEYS = [
    "schema",
    "goal",
    "goal_version",
    "steer_diff",
    "turn_no",
    "release_branch",
    "release_head",
    "dd_summary",
    "last_dd",
    "last_stop",
    "messages",
    "warnings",
    "history",
]
GOAL_REVIEW_KEYS = ["schema", "goal", "dd", "release_head", "history"]
IMPL_KEYS = [
    "schema",
    "dd_id",
    "round",
    "workspace",
    "branch",
    "base_commit",
    "spec_text",
    "acceptance",
    "feedback",
    "history",
]
REVIEW_KEYS = [
    "schema",
    "role",
    "dd_id",
    "round",
    "workspace",
    "branch",
    "base_commit",
    "head_commit",
    "spec_text",
    "acceptance_results",
    "cr_result",
    "history",
]
MERGE_KEYS = [
    "schema",
    "kind",
    "dd_id",
    "workspace",
    "source_branch",
    "source_head",
    "target_branch",
    "target_head",
    "acceptance",
]
SCRIBE_KEYS = [
    "schema",
    "goal_id",
    "goal_version",
    "trigger",
    "since_seq",
    "until_seq",
    "new_runs",
    "prior_observations",
    "history",
]


def _hist(**overrides: object) -> dict[str, object]:
    handle: dict[str, object] = history_handle(
        goal_run_root="/runs/g-7f3a2c", work_folder="wf-ab12cd", dd_id="dd-03"
    )
    handle.update(overrides)
    return handle


# ---------------------------------------------------------------------------
# schema constants
# ---------------------------------------------------------------------------


class TestInSchemas:
    def test_literals_match_protocol(self) -> None:
        assert SCHEMA_GOAL_TURN_IN == "goal.turn.in/1"
        assert SCHEMA_GOAL_REVIEW_IN == "goal.review.in/1"
        assert SCHEMA_IMPL_IN == "impl.in/1"
        assert SCHEMA_REVIEW_IN == "review.in/1"
        assert SCHEMA_MERGE_IN == "merge.in/1"
        assert SCHEMA_SCRIBE_IN == "scribe.in/1"

    def test_in_schemas_tuple(self) -> None:
        assert IN_SCHEMAS == (
            SCHEMA_GOAL_TURN_IN,
            SCHEMA_GOAL_REVIEW_IN,
            SCHEMA_IMPL_IN,
            SCHEMA_REVIEW_IN,
            SCHEMA_MERGE_IN,
            SCHEMA_SCRIBE_IN,
        )


# ---------------------------------------------------------------------------
# history_handle
# ---------------------------------------------------------------------------


class TestHistoryHandle:
    def test_work_folder_and_dd_id(self) -> None:
        handle = history_handle(
            goal_run_root="/runs/g-7f3a2c", work_folder="wf-ab12cd", dd_id="dd-03"
        )
        assert handle == {
            "work_folder": "wf-ab12cd",
            "events": "/runs/g-7f3a2c/events.jsonl",
            "dd_dir": "/runs/g-7f3a2c/dd/dd-03/",
            "note": HISTORY_NOTE,
        }
        assert list(handle) == ["work_folder", "events", "dd_dir", "note"]

    def test_no_work_folder_with_dd_id(self) -> None:
        handle = history_handle(goal_run_root="/runs/g-7f3a2c", dd_id="dd-03")
        assert "work_folder" not in handle
        assert handle["events"] == "/runs/g-7f3a2c/events.jsonl"
        assert handle["dd_dir"] == "/runs/g-7f3a2c/dd/dd-03/"

    def test_work_folder_without_dd_id(self) -> None:
        handle = history_handle(goal_run_root="/runs/g-7f3a2c", work_folder="wf-ab12cd")
        assert handle["work_folder"] == "wf-ab12cd"
        assert handle["dd_dir"] == "/runs/g-7f3a2c/dd/"

    def test_neither_work_folder_nor_dd_id(self) -> None:
        handle = history_handle(goal_run_root="/runs/g-7f3a2c")
        assert list(handle) == ["events", "dd_dir", "note"]
        assert handle["events"] == "/runs/g-7f3a2c/events.jsonl"
        assert handle["dd_dir"] == "/runs/g-7f3a2c/dd/"

    def test_note_is_the_go17_sentence(self) -> None:
        assert HISTORY_NOTE.endswith("都在这里，需要就自己读")

    def test_root_with_trailing_slash(self) -> None:
        # posixpath.join semantics are pinned: a trailing slash on the root is
        # not doubled (the module never touches the fs either way).
        handle = history_handle(goal_run_root="/runs/g-7f3a2c/")
        assert handle["events"] == "/runs/g-7f3a2c/events.jsonl"
        assert handle["dd_dir"] == "/runs/g-7f3a2c/dd/"


# ---------------------------------------------------------------------------
# build_goal_turn_in / build_goal_review_in
# ---------------------------------------------------------------------------


class TestBuildGoalTurnIn:
    def test_keys_order_and_schema(self) -> None:
        obj = build_goal_turn_in(
            goal={"goal_id": "g-7f3a2c"},
            goal_version=3,
            steer_diff=[],
            turn_no=4,
            release_branch="release/g-7f3a2c",
            release_head=SHA,
            dd_summary="3 张 DD：2 merged，1 failed",
            last_dd={"dd_id": "dd-02", "outcome": "failed"},
            last_stop=None,
            messages=[],
            warnings=["turns>=30"],
            history=_hist(),
        )
        assert list(obj) == GOAL_TURN_KEYS
        assert obj["schema"] == SCHEMA_GOAL_TURN_IN
        assert obj["goal_version"] == 3
        assert obj["turn_no"] == 4

    def test_first_round_nulls_are_kept(self) -> None:
        obj = build_goal_turn_in(
            goal={"goal_id": "g-7f3a2c"},
            goal_version=1,
            steer_diff=[],
            turn_no=1,
            release_branch="release/g-7f3a2c",
            release_head=SHA,
            dd_summary="",
            last_dd=None,
            last_stop=None,
            messages=[],
            warnings=[],
            history=_hist(),
        )
        assert obj["last_stop"] is None
        assert "last_stop" in obj and "last_dd" in obj


class TestBuildGoalReviewIn:
    def test_keys_order_and_schema(self) -> None:
        obj = build_goal_review_in(
            goal={"goal_id": "g-7f3a2c"},
            dd={"dd_id": "dd-03", "outcome": "awaiting_approval"},
            release_head=SHA,
            history=_hist(),
        )
        assert list(obj) == GOAL_REVIEW_KEYS
        assert obj["schema"] == SCHEMA_GOAL_REVIEW_IN
        assert obj["dd"]["outcome"] == "awaiting_approval"


# ---------------------------------------------------------------------------
# build_impl_in
# ---------------------------------------------------------------------------


class TestBuildImplIn:
    def test_keys_order_and_schema(self) -> None:
        obj = build_impl_in(
            dd_id="dd-03",
            round=2,
            workspace="/data/worktrees/g-7f3a2c/dd-03",
            branch="dd/g-7f3a2c/dd-03",
            base_commit=SHA,
            spec_text="做 X",
            acceptance=["make test"],
            feedback=None,
            history=_hist(),
        )
        assert list(obj) == IMPL_KEYS
        assert obj["schema"] == SCHEMA_IMPL_IN
        assert obj["round"] == 2

    def test_first_round_feedback_stays_null(self) -> None:
        obj = build_impl_in(
            dd_id="dd-03",
            round=1,
            workspace="/ws",
            branch="dd/g/dd-03",
            base_commit=SHA,
            spec_text="做 X",
            acceptance=[],
            feedback=None,
            history=_hist(),
        )
        assert obj["feedback"] is None
        assert "feedback" in obj

    def test_feedback_round_carries_the_single_handoff(self) -> None:
        feedback = {"from": "cr", "detail": "改 Y", "findings": []}
        obj = build_impl_in(
            dd_id="dd-03",
            round=2,
            workspace="/ws",
            branch="dd/g/dd-03",
            base_commit=SHA,
            spec_text="做 X",
            acceptance=[],
            feedback=feedback,
            history=_hist(),
        )
        assert obj["feedback"] == feedback


# ---------------------------------------------------------------------------
# build_review_in
# ---------------------------------------------------------------------------


class TestBuildReviewIn:
    def test_cr_keys_order_schema_and_null_cr_result(self) -> None:
        obj = build_review_in(
            role="cr",
            dd_id="dd-03",
            round=2,
            workspace="/ws",
            branch="dd/g/dd-03",
            base_commit=SHA,
            head_commit="2" * 40,
            spec_text="做 X",
            acceptance_results=[{"cmd": "make test", "exit": 0, "tail": "ok"}],
            cr_result=None,
            history=_hist(),
        )
        assert list(obj) == REVIEW_KEYS
        assert obj["schema"] == SCHEMA_REVIEW_IN
        assert obj["role"] == "cr"
        assert obj["cr_result"] is None
        assert "cr_result" in obj

    def test_fr_may_carry_cr_result(self) -> None:
        cr_result = {"stop": "pass", "summary": "…", "findings": []}
        obj = build_review_in(
            role="fr",
            dd_id="dd-03",
            round=2,
            workspace="/ws",
            branch="dd/g/dd-03",
            base_commit=SHA,
            head_commit="2" * 40,
            spec_text="做 X",
            acceptance_results=[],
            cr_result=cr_result,
            history=_hist(),
        )
        assert obj["role"] == "fr"
        assert obj["cr_result"] == cr_result

    def test_unknown_role_raises(self) -> None:
        with pytest.raises(ValueError):
            build_review_in(
                role="boss",
                dd_id="dd-03",
                round=1,
                workspace="/ws",
                branch="b",
                base_commit=SHA,
                head_commit=SHA,
                spec_text="s",
                acceptance_results=[],
                cr_result=None,
                history=_hist(),
            )

    def test_cr_with_cr_result_raises(self) -> None:
        with pytest.raises(ValueError, match="cr_result"):
            build_review_in(
                role="cr",
                dd_id="dd-03",
                round=1,
                workspace="/ws",
                branch="b",
                base_commit=SHA,
                head_commit=SHA,
                spec_text="s",
                acceptance_results=[],
                cr_result={"stop": "pass"},
                history=_hist(),
            )


# ---------------------------------------------------------------------------
# build_merge_in
# ---------------------------------------------------------------------------


class TestBuildMergeIn:
    def test_dd_merge_keys_order_and_schema(self) -> None:
        obj = build_merge_in(
            kind="dd",
            dd_id="dd-03",
            workspace="/ws",
            source_branch="dd/g-7f3a2c/dd-03",
            source_head=SHA,
            target_branch="release/g-7f3a2c",
            target_head="3" * 40,
            acceptance=["make test"],
        )
        assert list(obj) == MERGE_KEYS
        assert obj["schema"] == SCHEMA_MERGE_IN
        assert obj["kind"] == "dd"
        assert obj["dd_id"] == "dd-03"
        assert "history" not in obj

    def test_release_merge_takes_null_dd_id(self) -> None:
        obj = build_merge_in(
            kind="release",
            dd_id=None,
            workspace="/ws",
            source_branch="release/g-7f3a2c",
            source_head=SHA,
            target_branch="main",
            target_head="3" * 40,
            acceptance=["make test"],
        )
        assert obj["dd_id"] is None
        assert "dd_id" in obj

    def test_release_merge_with_dd_id_raises(self) -> None:
        with pytest.raises(ValueError, match="dd_id"):
            build_merge_in(
                kind="release",
                dd_id="dd-03",
                workspace="/ws",
                source_branch="release/g",
                source_head=SHA,
                target_branch="main",
                target_head="3" * 40,
                acceptance=[],
            )

    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="kind"):
            build_merge_in(
                kind="hotfix",
                dd_id=None,
                workspace="/ws",
                source_branch="b",
                source_head=SHA,
                target_branch="main",
                target_head="3" * 40,
                acceptance=[],
            )


# ---------------------------------------------------------------------------
# build_scribe_in
# ---------------------------------------------------------------------------


class TestBuildScribeIn:
    def test_keys_order_and_schema(self) -> None:
        obj = build_scribe_in(
            goal_id="g-7f3a2c",
            goal_version=3,
            trigger="dd.merged",
            since_seq=380,
            until_seq=418,
            new_runs=[{"run_id": "r1", "role": "impl", "session_dir": "/s/r1/", "stop": {}}],
            prior_observations="/runs/g-7f3a2c/observations.jsonl",
            history=_hist(),
        )
        assert list(obj) == SCRIBE_KEYS
        assert obj["schema"] == SCHEMA_SCRIBE_IN
        assert obj["until_seq"] == 418

    def test_empty_range_is_legal(self) -> None:
        obj = build_scribe_in(
            goal_id="g",
            goal_version=1,
            trigger="goal.turn.finished",
            since_seq=10,
            until_seq=10,
            new_runs=[],
            prior_observations="/o.jsonl",
            history=_hist(),
        )
        assert obj["since_seq"] == obj["until_seq"] == 10

    def test_inverted_range_raises(self) -> None:
        with pytest.raises(ValueError, match="until_seq"):
            build_scribe_in(
                goal_id="g",
                goal_version=1,
                trigger="dd.merged",
                since_seq=418,
                until_seq=380,
                new_runs=[],
                prior_observations="/o.jsonl",
                history=_hist(),
            )


# ---------------------------------------------------------------------------
# render_user_prompt
# ---------------------------------------------------------------------------


class TestRenderUserPrompt:
    def test_roundtrips_to_the_original_object(self) -> None:
        obj = build_impl_in(
            dd_id="dd-03",
            round=2,
            workspace="/ws",
            branch="dd/g/dd-03",
            base_commit=SHA,
            spec_text="做 X",
            acceptance=["make test"],
            feedback={"from": "cr", "detail": "改 Y", "findings": []},
            history=_hist(),
        )
        prompt = render_user_prompt(obj)
        _preamble, separator, rest = prompt.partition("\n")
        assert separator == "\n"
        assert json.loads(rest) == obj

    def test_exactly_one_preamble_line_then_the_dump(self) -> None:
        obj = build_merge_in(
            kind="dd",
            dd_id="dd-01",
            workspace="/ws",
            source_branch="dd/g/dd-01",
            source_head=SHA,
            target_branch="release/g",
            target_head=SHA,
            acceptance=[],
        )
        prompt = render_user_prompt(obj)
        _preamble, separator, rest = prompt.partition("\n")
        assert separator == "\n"
        assert rest == json.dumps(obj, ensure_ascii=False, indent=2)

    def test_no_history_prose_beyond_the_handle(self) -> None:
        # The only history text allowed inside the prompt is what the in-object
        # itself carries (the handle with its events.jsonl path); the renderer
        # must not append event bodies, prior reviews, or schema explanations.
        obj = build_impl_in(
            dd_id="dd-03",
            round=1,
            workspace="/ws",
            branch="dd/g/dd-03",
            base_commit=SHA,
            spec_text="做 X",
            acceptance=[],
            feedback=None,
            history=history_handle(goal_run_root="/runs/g", dd_id="dd-03"),
        )
        prompt = render_user_prompt(obj)
        preamble = prompt.partition("\n")[0]
        assert "events.jsonl" not in preamble
        assert prompt.count("events.jsonl") == 1
        assert "goal.turn.finished" not in prompt
        assert "输出契约" not in prompt

    def test_non_ascii_stays_readable(self) -> None:
        obj = build_goal_review_in(
            goal={"title": "把 X 做出来"},
            dd={"dd_id": "dd-03"},
            release_head=SHA,
            history=_hist(),
        )
        assert "\\u" not in render_user_prompt(obj)
        assert "把 X 做出来" in render_user_prompt(obj)


# ---------------------------------------------------------------------------
# ROLE_PERSONA / render_system_prompt
# ---------------------------------------------------------------------------


class TestRolePersona:
    def test_covers_exactly_the_six_roles(self) -> None:
        assert set(ROLE_PERSONA) == {"goal", "impl", "cr", "fr", "merge", "scribe"}

    @pytest.mark.parametrize(
        ("role", "phrase"),
        [
            ("goal", "只能通过派发 DD"),
            ("goal", "一个 turn 只派一张 DD"),
            ("impl", "不选分支、不切目录"),
            ("cr", "不改工作树"),
            ("cr", "blocker 或 major"),
            ("fr", "不改工作树"),
            ("fr", "blocker 或 major"),
            ("merge", "Stop 的时候必须已经处理完"),
            ("merge", "冲突自行 rebase"),
            ("scribe", "只读"),
            ("scribe", "证据"),
        ],
    )
    def test_personas_state_first_principles_and_boundaries(self, role: str, phrase: str) -> None:
        assert phrase in ROLE_PERSONA[role]


class TestRenderSystemPrompt:
    @pytest.mark.parametrize(
        ("role", "call_kind", "schema_literal", "stops"),
        [
            ("goal", "turn", "goal.turn/1", ("dispatch", "done", "blocked")),
            ("goal", "review", "goal.review/1", ("approve", "reject")),
            ("impl", None, "impl/1", ("committed", "failed")),
            ("cr", None, "review/1", ("pass", "fail")),
            ("fr", None, "review/1", ("pass", "fail")),
            ("merge", None, "merge/1", ("merged", "rebased", "failed")),
            ("scribe", None, "scribe/1", ("observed",)),
        ],
    )
    def test_contains_persona_schema_and_all_stops(
        self,
        role: str,
        call_kind: str | None,
        schema_literal: str,
        stops: tuple[str, ...],
    ) -> None:
        history = history_handle(goal_run_root="/runs/g", work_folder="wf-ab12cd")
        prompt = render_system_prompt(role, call_kind=call_kind, history=history)
        assert ROLE_PERSONA[role] in prompt
        assert schema_literal in prompt
        for stop_value in stops:
            assert stop_value in prompt

    def test_renders_stop_branch_required_fields(self) -> None:
        prompt = render_system_prompt("merge", history=history_handle(goal_run_root="/runs/g"))
        assert "merged_commit" in prompt
        assert "new_head" in prompt

    def test_states_the_one_json_object_rule(self) -> None:
        prompt = render_system_prompt("impl", history={"events": "/e.jsonl"})
        assert "恰好一个 JSON 对象" in prompt
        assert "无代码围栏" in prompt

    def test_embeds_the_history_handle_verbatim(self) -> None:
        history = history_handle(goal_run_root="/runs/g-7f3a2c", dd_id="dd-03")
        prompt = render_system_prompt("impl", history=history)
        assert json.dumps(history, ensure_ascii=False, indent=2) in prompt

    def test_harness_note_is_optional(self) -> None:
        history = {"events": "/e.jsonl"}
        with_note = render_system_prompt(
            "cr", history=history, harness_note="exec yes / network no"
        )
        assert "harness 边界" in with_note
        assert "exec yes / network no" in with_note
        without_note = render_system_prompt("cr", history=history)
        assert "harness 边界" not in without_note

    def test_unknown_role_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown role"):
            render_system_prompt("boss", history={"events": "/e.jsonl"})

    def test_goal_without_call_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="call_kind"):
            render_system_prompt("goal", history={"events": "/e.jsonl"})


# ---------------------------------------------------------------------------
# needs_system_prompt
# ---------------------------------------------------------------------------


class TestNeedsSystemPrompt:
    def test_fresh_mode_always_sends(self) -> None:
        policy = SessionPolicy("fresh", None)
        assert needs_system_prompt(policy, is_first_call=True)
        assert needs_system_prompt(policy, is_first_call=False)

    def test_resume_mode_sends_on_first_call_only(self) -> None:
        policy = SessionPolicy("resume", 0.7)
        assert needs_system_prompt(policy, is_first_call=True)
        assert not needs_system_prompt(policy, is_first_call=False)

    def test_unknown_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="mode"):
            needs_system_prompt(SessionPolicy("bogus"), is_first_call=True)


# ---------------------------------------------------------------------------
# package surface
# ---------------------------------------------------------------------------


class TestPackageSurface:
    def test_init_lists_prompts_but_does_not_import_it(self) -> None:
        assert minimal_pkg.__doc__ is not None
        assert "``prompts``" in minimal_pkg.__doc__
        assert not hasattr(minimal_pkg, "render_user_prompt")
        assert not hasattr(minimal_pkg, "ROLE_PERSONA")
