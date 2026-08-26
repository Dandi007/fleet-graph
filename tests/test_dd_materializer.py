"""Sealing a stage through the plugin materializer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conftest import DEVELOPMENT_ID, head
from fleet_graph.dd.dispatch import DevelopmentChain, StageDispatchBuilder
from fleet_graph.dd.lifecycle import Lifecycle
from fleet_graph.dd.upstream_constants import canonical_json
from fleet_graph.dd.vendor import plugin_adapter
from fleet_graph.graphs.dd_materializer import (
    AUTHOR_EMAIL,
    MaterializationFailed,
    MaterializationTarget,
    PluginMaterializer,
    StageMaterializers,
    UnservedStage,
    implement_actor_result,
)
from fleet_graph.graphs.dd_pipeline import Dispatch, StageOutcome, StageRefused

LIFECYCLE = Lifecycle.load()
IMPLEMENT = LIFECYCLE.stages["implement"]
REVIEW = LIFECYCLE.stages["continuous_review"]
GATE = LIFECYCLE.stages["human_gate"]
STAMP = "2026-08-26T04:00:00Z"
SEALED_COMMIT = "9" * 40


def applied_receipt() -> dict[str, Any]:
    return {
        "actor_job_id": "job-1",
        "input_commit": "1" * 40,
        "outcome": "APPLIED",
        "work_head_commit": "2" * 40,
        "verification_record": {"checks": []},
    }


IMPLEMENT_DIGEST = "sha256:" + "a" * 64


def review_receipt() -> dict[str, Any]:
    return {"review_result": {"verdict": "APPROVE", "findings": []}}


def make_materializer(repo: Path) -> PluginMaterializer:
    builder = StageDispatchBuilder(
        DevelopmentChain(
            development_id=DEVELOPMENT_ID,
            workspace_path=str(repo),
            target_base_commit="b" * 40,
            root_handoff_digest="sha256:" + "c" * 64,
        )
    )
    return PluginMaterializer(
        builder=builder,
        binding=object(),
        target=MaterializationTarget(
            remote_url="https://example.invalid/repo.git",
            remote_ref="refs/heads/dev-001",
            worktree=str(repo),
            state_root=str(repo / ".state"),
        ),
    )


def dispatch_for(repo: Path, stage_id: str, *, stamp: str = STAMP, attempt: int = 1) -> Dispatch:
    return {
        "development_id": DEVELOPMENT_ID,
        "stage": stage_id,
        "mode": "initial",
        "generation": 1,
        "attempt": attempt,
        "attempt_started_at": stamp,
        "input_commit": head(repo),
        "parent_receipt": {},
        "receipt_digests": {"implement": IMPLEMENT_DIGEST},
    }


class TestTheRequestIsFrozenAndReproducible:
    def test_two_builds_of_the_same_attempt_are_byte_identical(self, repo: Path) -> None:
        """Same canonical JSON means same digest means the same frozen intent.

        A retry that produced a different request would seal a second,
        differently-timestamped commit for the same work.
        """
        materializer = make_materializer(repo)
        outcome = StageOutcome(receipt=applied_receipt())
        first = materializer.request(IMPLEMENT, dispatch_for(repo, "implement"), outcome)
        second = materializer.request(IMPLEMENT, dispatch_for(repo, "implement"), outcome)
        assert canonical_json(first) == canonical_json(second)

    def test_the_commit_time_is_the_frozen_stamp_not_the_clock(self, repo: Path) -> None:
        materializer = make_materializer(repo)
        request = materializer.request(
            IMPLEMENT, dispatch_for(repo, "implement"), StageOutcome(receipt=applied_receipt())
        )
        metadata = request["commit_metadata"]
        assert metadata["author_time"] == metadata["committer_time"] == STAMP
        assert metadata["author_email"] == metadata["committer_email"] == AUTHOR_EMAIL

    def test_a_dispatch_with_no_frozen_stamp_is_refused(self, repo: Path) -> None:
        materializer = make_materializer(repo)
        with pytest.raises(MaterializationFailed, match="attempt_started_at"):
            materializer.request(
                IMPLEMENT,
                dispatch_for(repo, "implement", stamp=""),
                StageOutcome(receipt=applied_receipt()),
            )

    def test_the_request_carries_the_twelve_field_dispatch(self, repo: Path) -> None:
        materializer = make_materializer(repo)
        request = materializer.request(
            IMPLEMENT, dispatch_for(repo, "implement"), StageOutcome(receipt=applied_receipt())
        )
        assert set(request["dispatch"]) == materializer.builder.required_fields
        assert request["worktree"] == str(repo)
        assert request["remote_ref"] == "refs/heads/dev-001"


class TestTheActorResultIsChecked:
    def test_an_applied_result_must_carry_its_evidence(self) -> None:
        receipt = applied_receipt()
        del receipt["verification_record"]
        with pytest.raises(MaterializationFailed, match="verification_record"):
            implement_actor_result(receipt)

    def test_a_disputed_result_must_carry_its_rebuttal(self) -> None:
        receipt = {"actor_job_id": "j", "input_commit": "1" * 40, "outcome": "DISPUTED"}
        with pytest.raises(MaterializationFailed, match="rebuttal"):
            implement_actor_result(receipt)

    def test_a_blocked_result_carries_its_blocker(self) -> None:
        receipt = {
            "actor_job_id": "j",
            "input_commit": "1" * 40,
            "outcome": "BLOCKED",
            "blocker": {"summary": "no upstream"},
        }
        assert implement_actor_result(receipt)["blocker"] == {"summary": "no upstream"}

    def test_an_unknown_outcome_is_refused(self) -> None:
        receipt = {"actor_job_id": "j", "input_commit": "1" * 40, "outcome": "PROBABLY_FINE"}
        with pytest.raises(MaterializationFailed, match="not one of"):
            implement_actor_result(receipt)

    def test_nothing_beyond_the_declared_fields_is_forwarded(self) -> None:
        receipt = {**applied_receipt(), "chatty_extra": "ignore me"}
        assert "chatty_extra" not in implement_actor_result(receipt)

    def test_the_reviewed_receipt_comes_from_the_chain_not_the_reviewer(self, repo: Path) -> None:
        """A reviewer handing back the digest of its own subject would be
        attesting to what it is reviewing."""
        receipt = {
            **review_receipt(),
            "implementation_handoff_receipt_digest": "sha256:" + "f" * 64,
        }
        request = make_materializer(repo).request(
            REVIEW, dispatch_for(repo, "continuous_review"), StageOutcome(receipt=receipt)
        )
        assert request["implementation_handoff_receipt_digest"] == IMPLEMENT_DIGEST

    def test_a_review_with_no_sealed_implement_receipt_is_a_chain_hole(self, repo: Path) -> None:
        dispatch = dispatch_for(repo, "continuous_review")
        dispatch["receipt_digests"] = {}
        with pytest.raises(MaterializationFailed, match="chain has a hole"):
            make_materializer(repo).request(
                REVIEW, dispatch, StageOutcome(receipt=review_receipt())
            )

    def test_a_review_must_declare_a_review_result(self, repo: Path) -> None:
        with pytest.raises(MaterializationFailed, match="review_result"):
            make_materializer(repo).request(
                REVIEW,
                dispatch_for(repo, "continuous_review"),
                StageOutcome(
                    receipt={"implementation_handoff_receipt_digest": "sha256:" + "a" * 64}
                ),
            )


class TestReadingWhatTheSealerReturned:
    def _sealed(self, repo: Path, result: dict[str, Any], monkeypatch: Any, stage: Any = IMPLEMENT):
        monkeypatch.setattr(plugin_adapter, "invoke_implement_materializer", lambda *a, **k: result)
        monkeypatch.setattr(plugin_adapter, "invoke_review_materializer", lambda *a, **k: result)
        receipt = applied_receipt() if stage is IMPLEMENT else review_receipt()
        return make_materializer(repo).materialize(
            stage, dispatch_for(repo, stage.id), StageOutcome(receipt=receipt)
        )

    def test_a_receipt_becomes_the_sealed_attestation(self, repo: Path, monkeypatch: Any) -> None:
        result = {"output_commit": SEALED_COMMIT, "attempt_id": "a-1"}
        sealed = self._sealed(repo, result, monkeypatch)
        assert sealed.commit == SEALED_COMMIT
        assert sealed.receipt == result

    def test_a_failure_keeps_the_plugins_own_code_and_retry_flag(
        self, repo: Path, monkeypatch: Any
    ) -> None:
        """Overriding `retryable` would decide policy for a contract that
        already states it."""
        result = {
            "detail": "remote was down",
            "failure_code": "PROVIDER_UNAVAILABLE",
            "retryable": True,
            "verified": False,
        }
        with pytest.raises(MaterializationFailed) as failed:
            self._sealed(repo, result, monkeypatch)
        assert failed.value.failure_code == "PROVIDER_UNAVAILABLE"
        assert failed.value.retryable is True

    def test_a_non_retryable_failure_stays_non_retryable(
        self, repo: Path, monkeypatch: Any
    ) -> None:
        result = {
            "detail": "worktree dirty",
            "failure_code": "DIRTY_WORKTREE",
            "retryable": False,
            "verified": False,
        }
        with pytest.raises(MaterializationFailed) as failed:
            self._sealed(repo, result, monkeypatch)
        assert failed.value.retryable is False
        assert LIFECYCLE.is_retryable(failed.value.failure_code) is False

    @pytest.mark.parametrize(
        ("outcome", "field"), [("DISPUTED", "rebuttal"), ("BLOCKED", "blocker")]
    )
    def test_a_non_applied_receipt_is_a_refusal_not_a_fault(
        self, repo: Path, monkeypatch: Any, outcome: str, field: str
    ) -> None:
        """No `output_commit` because no commit was written. Nothing broke."""
        result = {"outcome": outcome, field: {"summary": "the spec is wrong"}, "attempt_id": "a"}
        with pytest.raises(StageRefused, match="the spec is wrong"):
            self._sealed(repo, result, monkeypatch)

    def test_a_receipt_with_no_commit_and_no_outcome_is_a_fault(
        self, repo: Path, monkeypatch: Any
    ) -> None:
        with pytest.raises(MaterializationFailed, match="no output_commit"):
            self._sealed(repo, {"attempt_id": "a-1"}, monkeypatch)

    def test_a_review_is_sealed_by_the_review_materializer(
        self, repo: Path, monkeypatch: Any
    ) -> None:
        called: list[str] = []

        def implement_seal(*a: Any, **k: Any) -> dict[str, Any]:
            called.append("implement")
            return {"output_commit": SEALED_COMMIT}

        def review_seal(*a: Any, **k: Any) -> dict[str, Any]:
            called.append("review")
            return {"output_commit": SEALED_COMMIT, "verdict": "APPROVE"}

        monkeypatch.setattr(plugin_adapter, "invoke_implement_materializer", implement_seal)
        monkeypatch.setattr(plugin_adapter, "invoke_review_materializer", review_seal)

        sealed = make_materializer(repo).materialize(
            REVIEW,
            dispatch_for(repo, "continuous_review"),
            StageOutcome(receipt=review_receipt()),
        )
        assert called == ["review"]
        assert sealed.receipt is not None and sealed.receipt["verdict"] == "APPROVE"


class TestUnservedStagesRefuse:
    def test_the_plugin_sealer_refuses_a_stage_it_does_not_serve(self, repo: Path) -> None:
        materializer = make_materializer(repo)
        assert not materializer.serves(GATE)
        with pytest.raises(UnservedStage):
            materializer.request(GATE, dispatch_for(repo, "human_gate"), StageOutcome())

    def test_a_dispatched_stage_is_not_automatically_a_sealed_one(self, repo: Path) -> None:
        """`acceptance` is in the dispatch schema's stage enum but the plugin
        ships no sealer for it. Equating the two sets would have routed it to
        the review materializer, which rejects it."""
        materializer = make_materializer(repo)
        acceptance = LIFECYCLE.stages["acceptance"]

        assert materializer.builder.serves(acceptance.id)
        assert not materializer.serves(acceptance)
        assert materializer.sealed_stages == {"implement", "continuous_review", "final_review"}

    def test_an_unrouted_stage_refuses_rather_than_passing_the_commit_through(self) -> None:
        """Carrying the previous commit forward would report a stage as sealed
        when nothing was written."""
        with pytest.raises(UnservedStage):
            StageMaterializers(by_stage={}).materialize(GATE, {}, StageOutcome())

    def test_routing_reaches_the_registered_materializer(self, repo: Path) -> None:
        class Recorder:
            def materialize(self, stage: Any, dispatch: Any, outcome: Any) -> Any:
                return "sealed-by-recorder"

        routed = StageMaterializers(by_stage={"human_gate": Recorder()})
        assert routed.materialize(GATE, {}, StageOutcome()) == "sealed-by-recorder"


class TestTheCapabilityCheckIsNotBypassed:
    def test_sealing_goes_through_the_vendored_invokers(self) -> None:
        """Those verify the pinned plugin capability before running anything.

        Calling the materialize script directly would skip that, so the
        module must reach the plugin only through them.
        """
        from fleet_graph.graphs import dd_materializer
        from source_tools import executable_source

        body = executable_source(Path(dd_materializer.__file__))
        assert "invoke_implement_materializer" in body
        assert "invoke_review_materializer" in body
        assert "materialize-handoff" not in body
        assert "subprocess" not in body
