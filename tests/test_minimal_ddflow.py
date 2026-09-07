"""Tests for fleet_graph.minimal.ddflow: the minimal DD loop state machine.

Covers the full transition table (next_stage + unknown-combo errors), the
advance accumulator (approve reset, round count, terminal outcome), the
round-warning line, and build_dd_result over three representative event
sequences. Pure functions only: no IO, no agent, no git.
"""

from __future__ import annotations

import pytest

from fleet_graph.minimal.ddflow import (
    STAGES,
    TERMINAL_OUTCOMES,
    DDState,
    Transition,
    advance,
    build_dd_result,
    next_stage,
    warnings_for,
)
from fleet_graph.minimal.events import Event


def ev(seq: int, kind: str, dd_id: str, payload: dict | None = None) -> Event:
    return Event(
        ts=f"2026-09-07T01:00:{seq:02d}+00:00",
        goal_id="g-1",
        dd_id=dd_id,
        kind=kind,
        seq=seq,
        payload=payload or {},
    )


class TestConstants:
    def test_stages(self) -> None:
        assert STAGES == ("impl", "acceptance", "cr", "fr", "goal_review", "merge")

    def test_terminal_outcomes(self) -> None:
        assert TERMINAL_OUTCOMES == ("merged", "failed")


class TestNextStage:
    @pytest.mark.parametrize(
        ("stage", "stop", "expected"),
        [
            ("impl", "committed", Transition(next_stage="acceptance", outcome=None)),
            ("impl", "failed", Transition(next_stage=None, outcome="failed")),
            ("acceptance", "pass", Transition(next_stage="cr", outcome=None)),
            (
                "acceptance",
                "fail",
                Transition(
                    next_stage="impl",
                    outcome=None,
                    feedback_from="acceptance",
                    approve_reset=True,
                ),
            ),
            ("cr", "pass", Transition(next_stage="fr", outcome=None)),
            (
                "cr",
                "fail",
                Transition(next_stage="impl", outcome=None, feedback_from="cr", approve_reset=True),
            ),
            ("fr", "pass", Transition(next_stage="goal_review", outcome=None)),
            (
                "fr",
                "fail",
                Transition(next_stage="impl", outcome=None, feedback_from="fr", approve_reset=True),
            ),
            ("goal_review", "approve", Transition(next_stage="merge", outcome=None)),
            (
                "goal_review",
                "reject",
                Transition(
                    next_stage="impl",
                    outcome=None,
                    feedback_from="goal",
                    approve_reset=True,
                ),
            ),
            ("merge", "merged", Transition(next_stage=None, outcome="merged")),
            ("merge", "rebased", Transition(next_stage="cr", outcome=None, approve_reset=True)),
            (
                "merge",
                "failed",
                Transition(
                    next_stage="impl",
                    outcome=None,
                    feedback_from="merge",
                    approve_reset=True,
                ),
            ),
        ],
        ids=[
            "impl-committed",
            "impl-failed",
            "acceptance-pass",
            "acceptance-fail",
            "cr-pass",
            "cr-fail",
            "fr-pass",
            "fr-fail",
            "goal-approve",
            "goal-reject",
            "merge-merged",
            "merge-rebased",
            "merge-failed",
        ],
    )
    def test_every_transition(self, stage: str, stop: str, expected: Transition) -> None:
        assert next_stage(stage, stop) == expected

    def test_unknown_stage_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown stage"):
            next_stage("bogus", "pass")

    def test_unknown_stop_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown stop"):
            next_stage("cr", "banana")

    def test_unknown_stop_names_the_valid_stops(self) -> None:
        with pytest.raises(ValueError, match=r"\{fail, pass\}"):
            next_stage("cr", "banana")


class TestAdvance:
    def test_happy_path_one_round(self) -> None:
        state = DDState()
        state = advance(state, "impl", "committed")
        state = advance(state, "acceptance", "pass")
        state = advance(state, "cr", "pass")
        state = advance(state, "fr", "pass")
        assert state.stage == "goal_review"
        assert state.round == 1
        assert state.outcome is None
        assert state.approve_valid is False

    def test_approve_then_merge_merged(self) -> None:
        state = _happy_path()
        state = advance(state, "goal_review", "approve")
        assert state.approve_valid is True
        assert state.stage == "merge"
        state = advance(state, "merge", "merged")
        assert state.outcome == "merged"
        assert state.stage is None

    def test_round_increments_when_returning_to_impl(self) -> None:
        state = DDState()
        state = advance(state, "impl", "committed")
        state = advance(state, "acceptance", "fail")
        assert state.stage == "impl"
        assert state.round == 2
        assert state.approve_valid is False

    def test_approve_reset_on_acceptance_fail(self) -> None:
        state = advance(DDState(), "impl", "committed")
        state = advance(state, "acceptance", "fail")
        assert state.approve_valid is False

    def test_approve_cleared_when_loop_after_approval(self) -> None:
        state = _happy_path()
        state = advance(state, "goal_review", "approve")
        assert state.approve_valid is True
        state = advance(state, "merge", "failed")
        assert state.stage == "impl"
        assert state.approve_valid is False
        assert state.round == 2

    def test_impl_failed_is_terminal(self) -> None:
        state = advance(DDState(), "impl", "failed")
        assert state.outcome == "failed"
        assert state.stage is None
        assert state.round == 1
        assert state.approve_valid is False

    def test_rebased_full_rerun_then_merged(self) -> None:
        state = _happy_path()
        state = advance(state, "goal_review", "approve")
        assert state.approve_valid is True

        state = advance(state, "merge", "rebased")
        assert state.stage == "cr"
        assert state.approve_valid is False

        state = advance(state, "cr", "pass")
        state = advance(state, "fr", "pass")
        assert state.stage == "goal_review"
        assert state.approve_valid is False

        state = advance(state, "goal_review", "approve")
        assert state.approve_valid is True
        state = advance(state, "merge", "merged")
        assert state.outcome == "merged"

    def test_history_records_every_stage_stop(self) -> None:
        state = DDState()
        state = advance(state, "impl", "committed")
        state = advance(state, "acceptance", "pass")
        assert state.history == (("impl", "committed"), ("acceptance", "pass"))

    def test_advance_does_not_mutate_input(self) -> None:
        state = DDState()
        advance(state, "impl", "committed")
        assert state.history == ()
        assert state.stage == "impl"
        assert state.round == 1


def _happy_path() -> DDState:
    state = DDState()
    state = advance(state, "impl", "committed")
    state = advance(state, "acceptance", "pass")
    state = advance(state, "cr", "pass")
    state = advance(state, "fr", "pass")
    return state


class TestWarnings:
    def test_no_warning_under_line(self) -> None:
        state = DDState(round=2)
        assert warnings_for(state, warn_dd_rounds=3) == []

    def test_warning_at_line(self) -> None:
        state = DDState(round=3)
        assert warnings_for(state, warn_dd_rounds=3) == ["dd_rounds>=3"]

    def test_warning_past_line(self) -> None:
        state = DDState(round=8)
        assert warnings_for(state, warn_dd_rounds=3) == ["dd_rounds>=3"]

    def test_warning_does_not_change_outcome(self) -> None:
        state = DDState(round=8, outcome=None)
        assert warnings_for(state, warn_dd_rounds=3) == ["dd_rounds>=3"]
        assert state.outcome is None
        assert state.round == 8


class TestBuildDdResult:
    def test_merged_sequence(self) -> None:
        events = [
            ev(
                1,
                "dd.dispatched",
                "dd-01",
                {
                    "spec_text": "do the thing",
                    "spec_digest": "sha256:abc",
                    "branch": "dd/g-1/dd-01",
                    "head_commit": "b" * 40,
                },
            ),
            ev(
                2,
                "dd.stage.finished",
                "dd-01",
                {
                    "stage": "impl",
                    "stop": "committed",
                    "commit": "c" * 40,
                    "summary": "did the thing",
                },
            ),
            ev(3, "dd.acceptance", "dd-01", {"cmd": "make verify", "exit": 0, "tail": "ok"}),
            ev(4, "dd.stage.finished", "dd-01", {"stage": "acceptance", "stop": "pass"}),
            ev(
                5,
                "dd.stage.finished",
                "dd-01",
                {
                    "stage": "cr",
                    "stop": "pass",
                    "role": "cr",
                    "summary": "lgtm",
                    "findings": [],
                },
            ),
            ev(
                6,
                "dd.stage.finished",
                "dd-01",
                {
                    "stage": "fr",
                    "stop": "pass",
                    "role": "fr",
                    "summary": "verified",
                    "findings": [],
                },
            ),
            ev(7, "dd.stage.finished", "dd-01", {"stage": "goal_review", "stop": "approve"}),
            ev(
                8,
                "dd.stage.finished",
                "dd-01",
                {
                    "stage": "merge",
                    "stop": "merged",
                    "merged_commit": "d" * 40,
                },
            ),
            ev(9, "dd.merged", "dd-01", {"merged_commit": "d" * 40}),
        ]

        result = build_dd_result(events, "dd-01")

        assert result["dd_id"] == "dd-01"
        assert result["spec_text"] == "do the thing"
        assert result["spec_digest"] == "sha256:abc"
        assert result["outcome"] == "merged"
        assert result["rounds"] == 1
        assert result["branch"] == "dd/g-1/dd-01"
        assert result["head_commit"] == "c" * 40
        assert result["merged_commit"] == "d" * 40
        assert result["acceptance_results"] == [{"cmd": "make verify", "exit": 0, "tail": "ok"}]
        assert result["reviews"] == [
            {"role": "cr", "stop": "pass", "summary": "lgtm", "findings": []},
            {"role": "fr", "stop": "pass", "summary": "verified", "findings": []},
        ]
        assert result["impl_summary"] == "did the thing"
        assert result["failure"] is None

    def test_failed_on_cr_then_impl(self) -> None:
        events = [
            ev(
                1,
                "dd.dispatched",
                "dd-02",
                {
                    "spec_text": "fix it",
                    "spec_digest": "sha256:def",
                    "branch": "dd/g-1/dd-02",
                    "head_commit": "a" * 40,
                },
            ),
            ev(
                2,
                "dd.stage.finished",
                "dd-02",
                {
                    "stage": "impl",
                    "stop": "committed",
                    "commit": "b" * 40,
                    "summary": "first try",
                },
            ),
            ev(3, "dd.acceptance", "dd-02", {"cmd": "make verify", "exit": 0}),
            ev(4, "dd.stage.finished", "dd-02", {"stage": "acceptance", "stop": "pass"}),
            ev(
                5,
                "dd.stage.finished",
                "dd-02",
                {
                    "stage": "cr",
                    "stop": "fail",
                    "role": "cr",
                    "summary": "no",
                    "findings": [{"severity": "blocker", "detail": "broken"}],
                },
            ),
            ev(
                6,
                "dd.stage.finished",
                "dd-02",
                {
                    "stage": "impl",
                    "stop": "failed",
                    "detail": "spec is contradictory",
                },
            ),
            ev(7, "dd.failed", "dd-02", {"stage": "impl", "detail": "spec is contradictory"}),
        ]

        result = build_dd_result(events, "dd-02")

        assert result["outcome"] == "failed"
        assert result["rounds"] == 2
        assert result["head_commit"] == "b" * 40
        assert result["impl_summary"] == "first try"
        assert result["failure"] == {"stage": "impl", "detail": "spec is contradictory"}
        assert result["merged_commit"] is None
        assert result["reviews"][0]["stop"] == "fail"

    def test_awaiting_approval_sequence(self) -> None:
        events = [
            ev(
                1,
                "dd.dispatched",
                "dd-03",
                {
                    "spec_text": "ship it",
                    "spec_digest": "sha256:007",
                    "branch": "dd/g-1/dd-03",
                    "head_commit": "a" * 40,
                },
            ),
            ev(
                2,
                "dd.stage.finished",
                "dd-03",
                {
                    "stage": "impl",
                    "stop": "committed",
                    "commit": "b" * 40,
                    "summary": "done",
                },
            ),
            ev(3, "dd.stage.finished", "dd-03", {"stage": "acceptance", "stop": "pass"}),
            ev(
                4,
                "dd.stage.finished",
                "dd-03",
                {
                    "stage": "cr",
                    "stop": "pass",
                    "role": "cr",
                    "summary": "ok",
                    "findings": [],
                },
            ),
            ev(
                5,
                "dd.stage.finished",
                "dd-03",
                {
                    "stage": "fr",
                    "stop": "pass",
                    "role": "fr",
                    "summary": "ok",
                    "findings": [],
                },
            ),
        ]

        result = build_dd_result(events, "dd-03")

        assert result["outcome"] == "awaiting_approval"
        assert result["rounds"] == 1
        assert result["merged_commit"] is None
        assert result["failure"] is None
        assert result["head_commit"] == "b" * 40

    def test_in_progress_sequence(self) -> None:
        events = [
            ev(
                1,
                "dd.dispatched",
                "dd-04",
                {
                    "spec_text": "wip",
                    "branch": "dd/g-1/dd-04",
                    "head_commit": "a" * 40,
                },
            ),
            ev(
                2,
                "dd.stage.finished",
                "dd-04",
                {
                    "stage": "impl",
                    "stop": "committed",
                    "commit": "b" * 40,
                    "summary": "wip",
                },
            ),
            ev(3, "dd.stage.finished", "dd-04", {"stage": "acceptance", "stop": "pass"}),
            ev(
                4,
                "dd.stage.finished",
                "dd-04",
                {
                    "stage": "cr",
                    "stop": "pass",
                    "role": "cr",
                    "summary": "ok",
                    "findings": [],
                },
            ),
        ]

        result = build_dd_result(events, "dd-04")

        assert result["outcome"] == "in_progress"
        assert result["rounds"] == 1
        assert result["spec_digest"] is None

    def test_ignores_other_dd_events(self) -> None:
        events = [
            ev(1, "dd.dispatched", "dd-01", {"spec_text": "mine"}),
            ev(2, "dd.dispatched", "dd-99", {"spec_text": "not mine"}),
            ev(3, "dd.merged", "dd-99", {"merged_commit": "f" * 40}),
            ev(
                4,
                "dd.stage.finished",
                "dd-01",
                {
                    "stage": "impl",
                    "stop": "failed",
                    "detail": "boom",
                },
            ),
        ]

        result = build_dd_result(events, "dd-01")

        assert result["spec_text"] == "mine"
        assert result["outcome"] == "failed"
        assert result["failure"] == {"stage": "impl", "detail": "boom"}
