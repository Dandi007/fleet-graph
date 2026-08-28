"""B3 evidence chain: phenomenon -> mechanism -> evidence, checked mechanically.

Each B1/B2 behaviour is accepted only as a linked chain of three claims, and
none of the three may be vacuous:

1. a deterministic test or fixture demonstrates the externally observable
   phenomenon;
2. the test identifies the governing mechanism that enforces or recovers it;
3. the resulting artifact/receipt/event is asserted as evidence of that exact
   mechanism, bound to the tested subject by an immutable reference.

The link is *typed*, so validation can reject the specific failures the spec
names, rather than a generic "something looked off":

- a link with a missing phenomenon, mechanism, or evidence is "a link removed";
- an evidence artifact whose mechanism differs from the link's mechanism is
  "an unrelated event substituted";
- a human-recovery link whose subject reference is empty is "a decision with
  no immutable target reference".

The three evidence kinds are the three behaviours in scope: scope refusal,
idempotent adoption, and suspended-to-resumed human recovery.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

#: The behaviours B3 chains over, one evidence kind per B1/B2 behaviour.
KIND_SCOPE = "scope"
KIND_ADOPTION = "adoption"
KIND_HUMAN_RECOVERY = "human_recovery"
KINDS = frozenset({KIND_SCOPE, KIND_ADOPTION, KIND_HUMAN_RECOVERY})


@dataclass(frozen=True)
class EvidenceLink:
    """One link: what was observed, what governed it, and how it is evidenced.

    ``mechanism`` is the governing mechanism the test identified.
    ``evidence_mechanism`` is what the artifact/receipt/event actually claimed
    produced it -- the substitution check compares the two. ``subject_ref`` is
    the immutable binding to the tested subject (a commit oid, an adoption
    target, a recovery target), which is what makes the evidence *about
    something* rather than an unbound receipt.
    """

    kind: str
    phenomenon: str
    mechanism: str
    evidence_mechanism: str = ""
    subject_ref: str = ""
    digest: str = ""


@dataclass(frozen=True)
class EvidenceChain:
    """A sequence of links, one per behaviour, validated as a whole."""

    links: tuple[EvidenceLink, ...]

    def validate(self) -> tuple[str, ...]:
        """The reasons this chain is not acceptable; empty means it is.

        Each reason is attributable to the exact link it names, so a failing
        chain tells you *which* link was removed, substituted, or left without
        an immutable target -- never just "invalid".
        """
        reasons: list[str] = []
        for index, link in enumerate(self.links):
            where = f"link[{index}] ({link.kind})"
            if link.kind not in KINDS:
                reasons.append(f"{where}: unknown evidence kind {link.kind!r}")
            if not link.phenomenon:
                reasons.append(f"{where}: phenomenon missing")
            if not link.mechanism:
                reasons.append(f"{where}: mechanism missing")
            if not link.evidence_mechanism:
                reasons.append(f"{where}: evidence mechanism missing -- a link was removed")
            elif link.evidence_mechanism != link.mechanism:
                reasons.append(
                    f"{where}: substituted an unrelated event "
                    f"(evidence is of {link.evidence_mechanism!r}, not {link.mechanism!r})"
                )
            if not link.digest:
                reasons.append(f"{where}: no evidence digest -- an unbound receipt")
            if link.kind == KIND_HUMAN_RECOVERY and not link.subject_ref:
                reasons.append(
                    f"{where}: human-recovery decision has no immutable target reference"
                )
            elif not link.subject_ref:
                reasons.append(f"{where}: evidence has no immutable subject reference")
        return tuple(reasons)


def chain(links: Iterable[EvidenceLink]) -> EvidenceChain:
    return EvidenceChain(tuple(links))


__all__ = [
    "KINDS",
    "KIND_ADOPTION",
    "KIND_HUMAN_RECOVERY",
    "KIND_SCOPE",
    "EvidenceChain",
    "EvidenceLink",
    "chain",
]
