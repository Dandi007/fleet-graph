"""B3 evidence chain: phenomenon -> mechanism -> evidence, bound to the subject.

Each link is typed so the validator can name the exact failure the spec calls
out -- a removed link, a substituted unrelated event, or a human-recovery
decision with no immutable target reference.
"""

from __future__ import annotations

from fleet_graph.dd.adoption import (
    ADOPTION_MECHANISM,
    KIND_RECOVERABLE,
    AdoptionLedger,
    Discovery,
)
from fleet_graph.dd.evidence import (
    KIND_ADOPTION,
    KIND_HUMAN_RECOVERY,
    KIND_SCOPE,
    REQUIRED_KINDS,
    EvidenceChain,
    EvidenceLink,
)
from fleet_graph.dd.recovery import RECOVERY_MECHANISM, HumanRecoveryExit
from fleet_graph.dd.scope import RULE_ID, evaluate_text
from fleet_graph.dd.upstream_constants import compute_digest


def reasons(chain: EvidenceChain) -> str:
    """The validator's reasons, joined so a needle can be asserted by substring."""
    return " ".join(chain.validate())


def build_chain() -> EvidenceChain:
    """The three B1/B2 behaviours, each linked phenomenon->mechanism->evidence.

    The subject of every link is bound by digest -- not log text, not a mocked
    success flag -- and the evidence mechanism is read off the artifact the
    behaviour actually produced, so a substituted unrelated event is a mismatch
    the validator names rather than something satisfied by spelling the same
    literal twice.
    """
    scope_verdict = evaluate_text("Implement B4 as the next phase.")
    scope_subject = compute_digest("Implement B4 as the next phase.")

    ledger = AdoptionLedger()
    adoption = ledger.adopt(Discovery(signature="dev-x:g2", kind=KIND_RECOVERABLE), "ref1")

    exit_ = HumanRecoveryExit(authenticate=lambda a, n: bool(a) and bool(n))
    recovery = exit_.record(
        target_ref="ref1", decision="resume", decided_by="alice", question_note_id="n1"
    )

    return EvidenceChain(
        (
            EvidenceLink(
                kind=KIND_SCOPE,
                phenomenon="a spec declaring B4 is refused",
                mechanism=RULE_ID,
                evidence_mechanism=scope_verdict.rule_id,
                subject_ref=scope_subject,
                digest=compute_digest(
                    f"{scope_verdict.rule_id}:{[v.reference for v in scope_verdict.violations]}"
                ),
            ),
            EvidenceLink(
                kind=KIND_ADOPTION,
                phenomenon="replaying the same discovery yields one adopted record",
                mechanism=ADOPTION_MECHANISM,
                evidence_mechanism=adoption.mechanism,
                subject_ref=adoption.target_ref,
                digest=adoption.digest,
            ),
            EvidenceLink(
                kind=KIND_HUMAN_RECOVERY,
                phenomenon="suspended work resumes only from a recorded decision",
                mechanism=RECOVERY_MECHANISM,
                evidence_mechanism=recovery.mechanism,
                subject_ref=recovery.target_ref,
                digest=recovery.digest,
            ),
        )
    )


def test_a_complete_bound_chain_validates_clean() -> None:
    assert build_chain().validate() == ()


class TestRequiredLinks:
    """A valid B3 chain is one link per behaviour; any omission is invalid.

    The minimum required link set is the B1-B3 contract's three behaviours --
    scope, adoption, human recovery -- and an empty link tuple is never valid.
    """

    def test_an_empty_chain_is_never_valid(self) -> None:
        text = reasons(EvidenceChain(()))
        assert "empty" in text
        # The empty chain also names every required link as missing, deterministically.
        for kind in sorted(REQUIRED_KINDS):
            assert f"required link missing: {kind}" in text

    def test_a_complete_chain_names_no_missing_links(self) -> None:
        assert "required link missing" not in " ".join(build_chain().validate())
        assert build_chain().validate() == ()

    def test_each_required_link_omission_is_named(self) -> None:
        full = build_chain()
        for omitted in sorted(REQUIRED_KINDS):
            reduced = EvidenceChain(tuple(link for link in full.links if link.kind != omitted))
            text = reasons(reduced)
            assert f"required link missing: {omitted}" in text
            # The remaining kinds are accounted for, not reported missing.
            for present in REQUIRED_KINDS - {omitted}:
                assert f"required link missing: {present}" not in text

    def test_a_chain_built_from_a_subset_is_not_valid(self) -> None:
        """The assembled chain semantics: fewer than the required kinds is
        invalid even when every link it does carry is internally complete."""
        full = build_chain()
        scope_only = EvidenceChain((full.links[0],))
        assert scope_only.validate() != ()


class TestRemovingALinkFails:
    def test_a_missing_phenomenon_is_named(self) -> None:
        link = EvidenceLink(
            kind=KIND_SCOPE,
            phenomenon="",
            mechanism="m",
            evidence_mechanism="m",
            subject_ref="s",
            digest="d",
        )
        assert "phenomenon missing" in reasons(EvidenceChain((link,)))

    def test_a_missing_mechanism_is_named(self) -> None:
        link = EvidenceLink(
            kind=KIND_SCOPE,
            phenomenon="p",
            mechanism="",
            evidence_mechanism="m",
            subject_ref="s",
            digest="d",
        )
        assert "mechanism missing" in reasons(EvidenceChain((link,)))

    def test_a_missing_evidence_mechanism_is_named(self) -> None:
        link = EvidenceLink(
            kind=KIND_SCOPE,
            phenomenon="p",
            mechanism="m",
            evidence_mechanism="",
            subject_ref="s",
            digest="d",
        )
        assert "a link was removed" in reasons(EvidenceChain((link,)))

    def test_an_unbound_receipt_has_no_digest(self) -> None:
        link = EvidenceLink(
            kind=KIND_ADOPTION,
            phenomenon="p",
            mechanism="m",
            evidence_mechanism="m",
            subject_ref="s",
            digest="",
        )
        assert "unbound receipt" in reasons(EvidenceChain((link,)))


class TestSubstitutionFails:
    def test_an_unrelated_event_is_named(self) -> None:
        link = EvidenceLink(
            kind=KIND_SCOPE,
            phenomenon="p",
            mechanism="b1-scope-boundary",
            evidence_mechanism="some other mechanism",
            subject_ref="s",
            digest="d",
        )
        assert "substituted an unrelated event" in reasons(EvidenceChain((link,)))

    def test_the_correct_mechanism_is_accepted(self) -> None:
        link = EvidenceLink(
            kind=KIND_SCOPE,
            phenomenon="p",
            mechanism="b1-scope-boundary",
            evidence_mechanism="b1-scope-boundary",
            subject_ref="s",
            digest="d",
        )
        # Per-link integrity only: a lone link is complete in itself, but it is
        # not a complete B3 chain (the required-kind check lives below).
        assert EvidenceChain((link,)).validate(()) == ()

    def test_an_adoption_link_bound_to_a_recovery_artifact_is_named(self) -> None:
        """The mechanism field is read off the artifact, so swapping in a
        recovery decision where an adoption is claimed is a real substitution,
        not a match-by-construction."""
        exit_ = HumanRecoveryExit(authenticate=lambda a, n: bool(a) and bool(n))
        recovery = exit_.record(
            target_ref="ref1", decision="resume", decided_by="alice", question_note_id="n1"
        )
        link = EvidenceLink(
            kind=KIND_ADOPTION,
            phenomenon="adoption",
            mechanism=ADOPTION_MECHANISM,
            evidence_mechanism=recovery.mechanism,
            subject_ref=recovery.target_ref,
            digest=recovery.digest,
        )
        assert "substituted an unrelated event" in reasons(EvidenceChain((link,)))


class TestHumanRecoveryTarget:
    def test_a_decision_without_an_immutable_target_is_named(self) -> None:
        link = EvidenceLink(
            kind=KIND_HUMAN_RECOVERY,
            phenomenon="p",
            mechanism="m",
            evidence_mechanism="m",
            subject_ref="",
            digest="d",
        )
        assert "no immutable target reference" in reasons(EvidenceChain((link,)))

    def test_a_scope_link_without_a_subject_is_named_differently(self) -> None:
        link = EvidenceLink(
            kind=KIND_SCOPE,
            phenomenon="p",
            mechanism="m",
            evidence_mechanism="m",
            subject_ref="",
            digest="d",
        )
        text = reasons(EvidenceChain((link,)))
        assert "no immutable subject reference" in text
        assert "no immutable target reference" not in text


def test_an_unknown_kind_is_named() -> None:
    link = EvidenceLink(
        kind="not_a_kind",
        phenomenon="p",
        mechanism="m",
        evidence_mechanism="m",
        subject_ref="s",
        digest="d",
    )
    assert "unknown evidence kind" in reasons(EvidenceChain((link,)))
