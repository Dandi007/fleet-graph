"""B3 evidence chain: phenomenon -> mechanism -> evidence, bound to the subject.

Each link is typed so the validator can name the exact failure the spec calls
out -- a removed link, a substituted unrelated event, or a human-recovery
decision with no immutable target reference.
"""

from __future__ import annotations

from fleet_graph.dd.adoption import KIND_RECOVERABLE, AdoptionLedger, Discovery
from fleet_graph.dd.evidence import (
    KIND_ADOPTION,
    KIND_HUMAN_RECOVERY,
    KIND_SCOPE,
    EvidenceChain,
    EvidenceLink,
)
from fleet_graph.dd.recovery import HumanRecoveryExit
from fleet_graph.dd.scope import evaluate_text
from fleet_graph.dd.upstream_constants import compute_digest


def reasons(chain: EvidenceChain) -> str:
    """The validator's reasons, joined so a needle can be asserted by substring."""
    return " ".join(chain.validate())


def build_chain() -> EvidenceChain:
    """The three B1/B2 behaviours, each linked phenomenon->mechanism->evidence.

    The subject of every link is bound by digest -- not log text, not a mocked
    success flag -- and the evidence mechanism is asserted to equal the rule.
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
                mechanism="b1-scope-boundary active-forbidden refusal",
                evidence_mechanism="b1-scope-boundary active-forbidden refusal",
                subject_ref=scope_subject,
                digest=compute_digest(
                    f"{scope_verdict.rule_id}:{[v.reference for v in scope_verdict.violations]}"
                ),
            ),
            EvidenceLink(
                kind=KIND_ADOPTION,
                phenomenon="replaying the same discovery yields one adopted record",
                mechanism="AdoptionLedger.adopt idempotent by signature",
                evidence_mechanism="AdoptionLedger.adopt idempotent by signature",
                subject_ref=adoption.target_ref,
                digest=adoption.digest,
            ),
            EvidenceLink(
                kind=KIND_HUMAN_RECOVERY,
                phenomenon="suspended work resumes only from a recorded decision",
                mechanism="HumanRecoveryExit.resume gated on recorded_for",
                evidence_mechanism="HumanRecoveryExit.resume gated on recorded_for",
                subject_ref=recovery.target_ref,
                digest=recovery.digest,
            ),
        )
    )


def test_a_complete_bound_chain_validates_clean() -> None:
    assert build_chain().validate() == ()


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
        assert EvidenceChain((link,)).validate() == ()


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
