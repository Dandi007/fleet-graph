"""Sealing a stage through the plugin materializer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conftest import DEVELOPMENT_ID, git, head, write_index
from fleet_graph.dd.chain_rules import new_attempt_is_legal
from fleet_graph.dd.dispatch import DevelopmentChain, StageDispatchBuilder, derive_attempt_id
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


def seal_implement_receipt(repo: Path, attempt_id: str, payload: bytes = b'{"ok":1}') -> str:
    """Write what the real Implement sealer writes, and return its byte digest."""
    import hashlib

    path = repo / ".state" / "receipts" / attempt_id / "implement-receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


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

    def test_a_legacy_three_field_result_passes_through(self) -> None:
        """What agent-runtime's `implement.result.v1` actually returns.

        The vendored adapter defaults this shape to APPLIED. Refusing it here
        would refuse a result that bridge exists to accept, and would report a
        missing field the caller could do nothing about.
        """
        result = implement_actor_result(
            {"actor_job_id": "j", "input_commit": "1" * 40, "work_head_commit": "2" * 40}
        )
        assert result == {
            "actor_job_id": "j",
            "input_commit": "1" * 40,
            "work_head_commit": "2" * 40,
        }

    def test_fields_the_plugin_does_not_admit_are_dropped(self) -> None:
        """`implement.output.schema.json` sets additionalProperties: false, and
        the role returns `effects`."""
        result = implement_actor_result(
            {
                "actor_job_id": "j",
                "input_commit": "1" * 40,
                "work_head_commit": "2" * 40,
                "effects": [],
            }
        )
        assert "effects" not in result

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

    @pytest.mark.parametrize(
        ("outcome", "field"), [("DISPUTED", "rebuttal"), ("BLOCKED", "blocker")]
    )
    def test_an_honestly_redundant_work_head_commit_is_stripped_not_refused(
        self, outcome: str, field: str
    ) -> None:
        """A no-op that reports the head it finished on reports its input.

        Measured on dev-fg-4628ef887564 g3: the plugin's non-applied schema
        does not admit `work_head_commit`, so forwarding the honest redundancy
        turned a legitimate BLOCKED into INVALID_INPUT and a faulted line.
        Consistent means dropped, not refused.
        """
        receipt = {
            "actor_job_id": "j",
            "input_commit": "1" * 40,
            "outcome": outcome,
            field: {"summary": "the spec is already satisfied"},
            "work_head_commit": "1" * 40,
        }
        result = implement_actor_result(receipt)
        assert "work_head_commit" not in result
        assert result[field] == {"summary": "the spec is already satisfied"}
        assert result["input_commit"] == "1" * 40

    def test_a_non_applied_result_that_moved_the_head_is_refused(self) -> None:
        """A no-op that claims a different head is not a no-op."""
        receipt = {
            "actor_job_id": "j",
            "input_commit": "1" * 40,
            "outcome": "BLOCKED",
            "blocker": {"summary": "no upstream"},
            "work_head_commit": "2" * 40,
        }
        with pytest.raises(MaterializationFailed, match="not a no-op"):
            implement_actor_result(receipt)

    def test_an_unknown_outcome_is_refused(self) -> None:
        receipt = {"actor_job_id": "j", "input_commit": "1" * 40, "outcome": "PROBABLY_FINE"}
        with pytest.raises(MaterializationFailed, match="not one of"):
            implement_actor_result(receipt)

    def test_nothing_beyond_the_declared_fields_is_forwarded(self) -> None:
        receipt = {**applied_receipt(), "chatty_extra": "ignore me"}
        assert "chatty_extra" not in implement_actor_result(receipt)

    def test_the_admitted_fields_match_the_plugins_own_schema(self) -> None:
        import json
        from pathlib import Path as P

        from fleet_graph.graphs.dd_materializer import IMPLEMENT_ACTOR_FIELDS

        plugin = P(
            "/data/code/self/loop-engine-dev-dispatch-plugin-releases"
            "/76c4003bd087890867b411186a0584ea3ba4364b"
            "/workflows/dev-dispatch/implement/contracts/implement.output.schema.json"
        )
        if not plugin.is_file():
            pytest.skip("the pinned plugin release is not on this machine")
        schema = json.loads(plugin.read_text(encoding="utf-8"))
        assert set(IMPLEMENT_ACTOR_FIELDS) == set(schema["properties"])
        assert schema.get("additionalProperties") is False

    def test_a_pinned_identity_binds_the_review_to_the_replayed_receipt(self, repo: Path) -> None:
        """The g4 lesson (BINDING_MISMATCH: "Implement receipt identity does
        not match Review dispatch"): the review of a replayed implement must
        dispatch under the identity the installed receipt was sealed with.
        The dispatch's attempt_id and the parent-digest path must both follow
        the pin, so the digest sent is the bytes the sealer re-reads at that
        same identity."""
        from fleet_graph.dd.dispatch import derive_attempt_id

        materializer = make_materializer(repo)
        pinned = derive_attempt_id(DEVELOPMENT_ID, 2, 1)  # a previous generation's
        dispatch = dispatch_for(repo, "continuous_review")
        dispatch["generation"] = 4
        dispatch["pinned_attempt_id"] = pinned
        expected = seal_implement_receipt(repo, pinned)

        receipt = {
            **review_receipt(),
            "implementation_handoff_receipt_digest": "sha256:" + "f" * 64,
        }
        request = materializer.request(REVIEW, dispatch, StageOutcome(receipt=receipt))
        assert request["dispatch"]["attempt_id"] == pinned
        assert request["implementation_handoff_receipt_digest"] == expected

    def test_a_pinned_identity_with_nothing_sealed_is_still_a_chain_hole(self, repo: Path) -> None:
        """The pin changes where the expected identity comes from, never what
        is checked: no sealed receipt at the pinned identity refuses exactly
        as it would at a derived one."""
        from fleet_graph.dd.dispatch import derive_attempt_id

        dispatch = dispatch_for(repo, "continuous_review")
        dispatch["pinned_attempt_id"] = derive_attempt_id(DEVELOPMENT_ID, 2, 1)
        with pytest.raises(MaterializationFailed, match="no sealed parent receipt"):
            make_materializer(repo).request(
                REVIEW, dispatch, StageOutcome(receipt=review_receipt())
            )

    def test_the_reviewed_digest_is_the_sealed_receipts_bytes(self, repo: Path) -> None:
        """Not the reviewer's word, and not a digest of the receipt object we
        hold: the review sealer re-reads exactly those bytes."""
        materializer = make_materializer(repo)
        dispatch = dispatch_for(repo, "continuous_review")
        expected = seal_implement_receipt(repo, materializer.builder.build(dispatch)["attempt_id"])

        receipt = {
            **review_receipt(),
            "implementation_handoff_receipt_digest": "sha256:" + "f" * 64,
        }
        request = materializer.request(REVIEW, dispatch, StageOutcome(receipt=receipt))
        assert request["implementation_handoff_receipt_digest"] == expected

    def test_a_review_with_no_sealed_implement_receipt_is_a_chain_hole(self, repo: Path) -> None:
        with pytest.raises(MaterializationFailed, match="no sealed parent receipt"):
            make_materializer(repo).request(
                REVIEW,
                dispatch_for(repo, "continuous_review"),
                StageOutcome(receipt=review_receipt()),
            )

    def test_the_envelope_convention_is_stripped_from_a_review_result(self) -> None:
        """The reviewer's persona tells it to answer "with `effects: []`" --
        agent-runtime's envelope convention, which lands inside the result
        object. `review-result.schema.json` sets additionalProperties: false
        and does not admit it."""
        from fleet_graph.graphs.dd_materializer import review_actor_result

        cleaned = review_actor_result(
            {"verdict": "APPROVE", "findings": [], "effects": [], "notes": "chatty"}
        )
        assert cleaned == {"verdict": "APPROVE", "findings": []}

    def test_the_admitted_review_fields_come_from_the_schema(self) -> None:
        """Read from the contract, so a contract that grows a field does not
        need this to be remembered."""
        import json as _json

        from fleet_graph.dd.capability import CONTRACTS_DIR
        from fleet_graph.graphs.dd_materializer import review_result_fields

        schema = _json.loads(
            (CONTRACTS_DIR / "review-result.schema.json").read_text(encoding="utf-8")
        )
        assert review_result_fields() == frozenset(schema["properties"])
        assert schema.get("additionalProperties") is False
        assert "effects" not in review_result_fields()

    def test_a_review_must_declare_a_review_result(self, repo: Path) -> None:
        materializer = make_materializer(repo)
        dispatch = dispatch_for(repo, "continuous_review")
        seal_implement_receipt(repo, materializer.builder.build(dispatch)["attempt_id"])

        with pytest.raises(MaterializationFailed, match="review_result"):
            materializer.request(REVIEW, dispatch, StageOutcome(receipt={}))


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

        materializer = make_materializer(repo)
        dispatch = dispatch_for(repo, "continuous_review")
        seal_implement_receipt(repo, materializer.builder.build(dispatch)["attempt_id"])
        sealed = materializer.materialize(REVIEW, dispatch, StageOutcome(receipt=review_receipt()))
        assert called == ["review"]
        assert sealed.receipt is not None and sealed.receipt["verdict"] == "APPROVE"


class TestTheParentReceiptIsTheOneTheContractNames:
    def test_each_review_names_its_own_parent_file(self) -> None:
        """Continuous reviews the Implement receipt; Final reviews the
        Continuous one. Both by the sealed file's bytes."""
        from fleet_graph.graphs.dd_materializer import PARENT_RECEIPT_FILE

        assert PARENT_RECEIPT_FILE == {
            "continuous_review": "implement-receipt.json",
            "final_review": "continuous-review-receipt.json",
        }

    def test_implement_has_no_parent_file(self, repo: Path) -> None:
        """It is first, so its parent is the chain root the caller supplied."""
        assert make_materializer(repo).parent_digest("implement", "att-1") is None

    def test_the_filenames_match_the_plugins_own_source(self) -> None:
        plugin = Path(
            "/data/code/self/loop-engine-dev-dispatch-plugin-releases"
            "/76c4003bd087890867b411186a0584ea3ba4364b/scripts/attempt-context.py"
        )
        if not plugin.is_file():
            pytest.skip("the pinned plugin release is not on this machine")
        source = plugin.read_text(encoding="utf-8")

        from fleet_graph.graphs.dd_materializer import PARENT_RECEIPT_FILE

        for name in set(PARENT_RECEIPT_FILE.values()):
            assert f'"{name}"' in source, name


class TestTheOrderingRuleIsEnforcedAtMaterialization:
    """The pinned carrier's ordering rule holds at the materialization
    boundary as a structured refusal, instead of the non-JSON shell error the
    carrier produces when it applies the same rule (dev-fg-31b963659d16).

    A fresh continuous review is a new attempt: legal within its own chain
    only as the very first entry or the entry right after a REJECT
    (``attempt-context.py: check_chain_order``). The guard applies that rule
    generation-aware, over the committed index the carrier itself reads: each
    entry's durable ``attempt_id`` marks which generation sealed it, so an
    older generation's APPROVE-ended history is immutable context, not a
    prior-REJECT demand on the current generation's fresh attempt.

    The regressions assert the structured ``materialize()`` result -- the
    sealed commit and receipt a carrier produces for a legal fresh attempt,
    and the structured ``ORDER_VIOLATION`` the guard raises for an illegal one
    -- not merely that no shell error surfaced."""

    def _entry(self, generation: int, attempt: int, phase: str, verdict: str) -> dict[str, Any]:
        return {
            "attempt": attempt,
            "attempt_id": derive_attempt_id(DEVELOPMENT_ID, generation, attempt),
            "review_id": f"r-{phase}-g{generation}-a{attempt}",
            "review_phase": phase,
            "subject_commit": "0" * 40,
            "implementation_subject_commit": "0" * 40,
            "verdict": verdict,
            "artifact_path": ".dev-dispatch/reviews/x.json",
            "artifact_blob_oid": "0" * 40,
            "artifact_digest": "sha256:" + "0" * 64,
        }

    def _commit_index(self, repo: Path, entries: list[dict[str, Any]]) -> None:
        write_index(repo, entries=entries, development_id=DEVELOPMENT_ID)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "index")

    def _seal_via_carrier(self, monkeypatch: Any) -> dict[str, Any]:
        """Monkeypatch the pinned carrier to seal a structured review receipt.

        The real carrier lives in the pinned plugin and cannot run in a unit
        test; the ordering it enforces is mirrored by the guard this test
        exercises, so the stand-in returns the structured receipt shape the
        plugin's ``review-handoff-receipt`` admits and ``materialize()`` runs
        its full path -- the guard, then the carrier -- returning the
        structured ``Sealed`` the caller asserts.
        """
        receipt: dict[str, Any] = {"output_commit": SEALED_COMMIT, "verdict": "APPROVE"}
        monkeypatch.setattr(plugin_adapter, "invoke_review_materializer", lambda *a, **k: receipt)
        return receipt

    def test_a_cross_generation_continuous_review_materializes(
        self, repo: Path, monkeypatch: Any
    ) -> None:
        """The reported dev-fg-31b963659d16 history, end to end at the seal.

        Generation 1 ran attempt 1 (continuous APPROVE, final REJECT) then
        attempt 2 (continuous APPROVE, final APPROVE, accepted); that committed
        index is immutable history that is still present, committed, when the
        later generation's continuous review reaches the seal (the replayer did
        not -- and must not -- erase it). The generation-aware guard recognises
        those entries as older-generation history, so the fresh generation's
        continuous review is the first attempt of its own chain, not a new
        attempt inside generation 1's. The assertion is on the structured
        materialization result -- the sealed commit and receipt the carrier
        produced -- not on a request payload field (spec requirements 3, 4 and
        6)."""
        self._commit_index(
            repo,
            [
                self._entry(1, 1, "continuous", "APPROVE"),
                self._entry(1, 1, "final", "REJECT"),
                self._entry(1, 2, "continuous", "APPROVE"),
                self._entry(1, 2, "final", "APPROVE"),
            ],
        )
        dispatch = dispatch_for(repo, "continuous_review")
        dispatch["generation"] = 2
        seal_implement_receipt(repo, derive_attempt_id(DEVELOPMENT_ID, 2, 1))
        expected = self._seal_via_carrier(monkeypatch)

        sealed = make_materializer(repo).materialize(
            REVIEW, dispatch, StageOutcome(receipt=review_receipt())
        )

        assert sealed.commit == SEALED_COMMIT
        assert sealed.receipt == expected

    def test_a_same_chain_new_attempt_without_a_prior_reject_is_refused(
        self, repo: Path, monkeypatch: Any
    ) -> None:
        """A genuinely new attempt within one chain still owes its prior REJECT:
        an APPROVE-ended predecessor makes the fresh continuous review illegal
        and materialization raises the structured ORDER_VIOLATION the flat
        carrier rule dictates, not a shell error and not a silent pass-through
        (spec requirements 2 and 5)."""
        self._commit_index(
            repo,
            [
                self._entry(1, 1, "continuous", "APPROVE"),
                self._entry(1, 1, "final", "APPROVE"),
            ],
        )
        dispatch = dispatch_for(repo, "continuous_review")

        with pytest.raises(MaterializationFailed, match="ORDER_VIOLATION") as refused:
            make_materializer(repo).materialize(
                REVIEW, dispatch, StageOutcome(receipt=review_receipt())
            )

        assert refused.value.failure_code == "ORDER_VIOLATION"
        assert refused.value.retryable is False

    def test_a_same_chain_new_attempt_after_a_reject_is_legal(
        self, repo: Path, monkeypatch: Any
    ) -> None:
        """A rework attempt after a REJECT is a legal fresh continuous review:
        the committed chain ended in REJECT, so the guard permits and the
        carrier seals a structured result."""
        self._commit_index(
            repo,
            [
                self._entry(1, 1, "continuous", "APPROVE"),
                self._entry(1, 1, "final", "REJECT"),
            ],
        )
        dispatch = dispatch_for(repo, "continuous_review", attempt=2)
        seal_implement_receipt(repo, derive_attempt_id(DEVELOPMENT_ID, 1, 2))
        expected = self._seal_via_carrier(monkeypatch)

        sealed = make_materializer(repo).materialize(
            REVIEW, dispatch, StageOutcome(receipt=review_receipt())
        )
        assert sealed.commit == SEALED_COMMIT
        assert sealed.receipt == expected

    def test_the_guard_is_generation_aware(self) -> None:
        """The guard applies the ordering rule generation-aware: an inherited
        older generation's accepted history does not impose a prior-REJECT on
        the current generation's fresh attempt, while a same-generation
        APPROVE-ended chain still refuses it. The flat (generation-less) rule
        still mirrors the flattened carrier rule exactly (spec requirements
        1, 2 and 3)."""
        assert new_attempt_is_legal([]) is True
        assert new_attempt_is_legal([{"review_phase": "final", "verdict": "REJECT"}]) is True
        assert new_attempt_is_legal([{"review_phase": "continuous", "verdict": "APPROVE"}]) is False
        accepted_history = [
            self._entry(1, 1, "continuous", "APPROVE"),
            self._entry(1, 1, "final", "APPROVE"),
        ]
        # Flat: an APPROVE-ended history refuses a fresh attempt.
        assert new_attempt_is_legal(accepted_history) is False
        # Generation-aware: an older generation's accepted history is immutable.
        assert (
            new_attempt_is_legal(accepted_history, generation=2, development_id=DEVELOPMENT_ID)
            is True
        )
        # Same generation: a fresh attempt after an APPROVE chain is illegal.
        assert (
            new_attempt_is_legal(
                [
                    self._entry(2, 1, "continuous", "APPROVE"),
                    self._entry(2, 1, "final", "APPROVE"),
                ],
                generation=2,
                development_id=DEVELOPMENT_ID,
            )
            is False
        )
        assert (
            new_attempt_is_legal(
                [
                    self._entry(2, 1, "continuous", "APPROVE"),
                    self._entry(2, 1, "final", "REJECT"),
                ],
                generation=2,
                development_id=DEVELOPMENT_ID,
            )
            is True
        )


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
