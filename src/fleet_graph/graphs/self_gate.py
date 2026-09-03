"""The goal-line self-gate orchestration (wf-8d9737 M3, spec item 1).

When a goal line wakes on ``dd_awaiting_gate(dev_id)`` (M1) it does not ask a
human and it does not cast a naked verdict. It mechanically performs the six
evidence obligations -- the gate's mandatory answer fields -- and then delivers
the resulting decision through the dd delivery path (M2 ``decision_deliver``),
with ``decided_by == line principal`` validated to equal the single's
``record.json.dispatched_by`` (that check is the delivery path's own S11
authority).

This module is the mechanical *join* between the two halves: it runs
``gather_gate_evidence`` (which invokes the six obligations, never trusts a
pre-built dict) and ``gate_decision`` (which reduces the six answers to
APPROVE/REJECT), then hands the decision + evidence to an injected deliver seam.
The deliver seam is the production ``decision_deliver``; tests inject a scripted
fake, so the orchestration stays free of any transport or control-plane import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from fleet_graph.selfgate import (
    GateEvidenceInputs,
    gate_decision,
    gather_gate_evidence,
)


@dataclass
class SelfGateResult:
    """One self-gate turn's structured answer: gathered evidence + the verdict
    that rode the delivery, plus the delivery's own result (a dict)."""

    development_id: str
    principal: str
    decision: str
    evidence: dict[str, Any]
    delivery: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "development_id": self.development_id,
            "principal": self.principal,
            "decided_by": self.principal,
            "decision": self.decision,
            "evidence": self.evidence,
            "delivery": self.delivery,
        }


class DeliverPort(Protocol):
    """The delivery seam: cast one decision + its evidence to a dd single.

    The production realization is ``decision_deliver`` (M2). The seam returns a
    plain dict (or a ``DeliveryResult``-like) so this module never imports the
    decision transport -- a fake in tests records the cast instead of touching a
    control plane.
    """

    def __call__(self, decision: str, evidence: dict[str, Any]) -> dict[str, Any]: ...


def perform_line_self_gate(
    *,
    development_id: str,
    principal: str,
    inputs: GateEvidenceInputs,
    deliver: DeliverPort,
) -> SelfGateResult:
    """Run one self-gate turn: gather the six obligations, decide, deliver.

    The order is fixed and non-negotiable: gather first (so the evidence is
    *mechanical*, not a caller-forged six-key dict), decide second (so the
    verdict is a pure function of the six answers), deliver last (so the dd
    single receives a decision whose rationale is the evidence that was actually
    produced). The delivery's own authority check (``principal == dispatched_by``)
    remains the outermost gate on the other side of the seam.
    """
    evidence = gather_gate_evidence(inputs)
    decision = gate_decision(evidence)
    delivery = deliver(decision, evidence)
    delivery_dict = (
        delivery.as_dict() if hasattr(delivery, "as_dict") else dict(delivery)  # type: ignore[arg-type]
    )
    return SelfGateResult(
        development_id=development_id,
        principal=principal,
        decision=decision,
        evidence=evidence,
        delivery=delivery_dict,
    )


__all__ = [
    "DeliverPort",
    "SelfGateResult",
    "perform_line_self_gate",
]
