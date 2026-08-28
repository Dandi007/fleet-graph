"""B2 human recovery. Resume only from an authenticated, recorded decision."""

from __future__ import annotations

import pytest

from fleet_graph.dd.recovery import (
    HumanRecoveryExit,
    RecoveryDecision,
    RecoveryError,
    decision_digest,
)


def auth_allowed(_actor: str, _note: str) -> bool:
    return True


class TestSuspendedToResumed:
    def test_resume_returns_only_from_a_recorded_decision(self) -> None:
        exit_ = HumanRecoveryExit(
            authenticate=lambda actor, note: actor == "alice" and note == "n1"
        )
        with pytest.raises(RecoveryError, match="no recorded recovery decision"):
            exit_.resume(target_ref="ref1")

        decision = exit_.record(
            target_ref="ref1",
            decision="resume",
            decided_by="alice",
            question_note_id="n1",
        )
        resumed = exit_.resume(target_ref="ref1")
        assert resumed["resumed"] is True
        assert resumed["digest"] == decision.digest

    def test_the_decision_is_sealed_and_bound_to_its_target(self) -> None:
        exit_ = HumanRecoveryExit(authenticate=auth_allowed)
        decision = exit_.record(
            target_ref="ref1", decision="resume", decided_by="alice", question_note_id="n1"
        )
        assert decision.digest == decision_digest(
            target_ref="ref1",
            decision="resume",
            decided_by="alice",
            question_note_id="n1",
            at="",
        )
        assert decision.target_ref == "ref1"

    def test_resume_for_another_target_still_refuses(self) -> None:
        exit_ = HumanRecoveryExit(authenticate=auth_allowed)
        exit_.record(target_ref="ref1", decision="resume", decided_by="a", question_note_id="n")
        with pytest.raises(RecoveryError, match="no recorded recovery decision"):
            exit_.resume(target_ref="ref2")


class TestAuthenticationIsDelegated:
    def test_an_unauthenticated_decision_is_never_recorded(self) -> None:
        exit_ = HumanRecoveryExit(authenticate=lambda actor, note: False)
        with pytest.raises(RecoveryError, match="not authenticated"):
            exit_.record(
                target_ref="ref1", decision="resume", decided_by="alice", question_note_id="n1"
            )
        assert exit_.records() == ()

    def test_the_default_authenticator_requires_an_actor_and_an_anchor(self) -> None:
        exit_ = HumanRecoveryExit()
        with pytest.raises(RecoveryError, match="needs a decision and an actor"):
            exit_.record(target_ref="ref1", decision="resume", decided_by="", question_note_id="")
        # An actor alone is not a governance decision: the anchor is missing.
        with pytest.raises(RecoveryError, match="not authenticated"):
            exit_.record(
                target_ref="ref1", decision="resume", decided_by="alice", question_note_id=""
            )


class TestTargetIsImmutable:
    def test_a_decision_without_a_target_ref_refuses(self) -> None:
        exit_ = HumanRecoveryExit(authenticate=auth_allowed)
        with pytest.raises(RecoveryError, match="immutable target reference"):
            exit_.record(target_ref="", decision="resume", decided_by="a", question_note_id="n")

    def test_a_recovered_exit_restores_state_from_records(self) -> None:
        first = HumanRecoveryExit(authenticate=auth_allowed)
        first.record(
            target_ref="r", decision="resume", decided_by="a", question_note_id="n", at="t"
        )
        restored = HumanRecoveryExit(authenticate=auth_allowed, records=first.records())
        assert restored.resume(target_ref="r")["resumed"] is True
        assert restored.records() == first.records()


def test_recovery_decision_is_a_frozen_record() -> None:
    decision = RecoveryDecision(
        target_ref="r", decision="resume", decided_by="a", question_note_id="n", at="t", digest="d"
    )
    assert decision.as_dict()["target_ref"] == "r"
    assert decision.as_dict()["digest"] == "d"
