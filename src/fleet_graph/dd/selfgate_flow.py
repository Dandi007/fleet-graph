"""M3 line self-gate orchestration: the engine path that wires the six obligations.

``selfgate.py`` holds the pure judgement functions (the six evidence checkers,
``assess_evidence``, ``decide``, ``harvest_eligible``). This module is their
*production* caller -- the "线自判路径成引擎默认" seam the spec §1 names. It:

- gathers the six measured facts through an injected :class:`SelfGateFacts` port;
- runs the gate (:func:`fleet_graph.dd.selfgate.decide`), which is APPROVE only
  on a clean six-obligation gate *and* a principal equal to the single's
  ``dispatched_by``;
- templates the six results into the spec §4 rationale payload
  (:func:`template_evidence_rationale`) that rides the M2 ``decision_deliver``
  call; and
- owns the S7 harvest-after-merge eligibility wiring
  (:func:`harvest_eligibility`, backed by
  :func:`fleet_graph.dd.selfgate.harvest_eligible`) plus the
  "release/<line-id> writable repo" allowlist semantics
  (:func:`release_branch_ref`, :func:`is_release_writable_repo`).

Like ``selfgate.py`` this module touches neither git, nor the board, nor the bus:
the facts arrive through an injected port and the delivery is M2's job (the
production caller in ``decision_mcp.py`` supplies the real ``decision_deliver``).
Keeping judgement out of the write path is INV-3, and it is what keeps the whole
orchestration testable against a duck-typed gate plane.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from fleet_graph.dd.selfgate import (
    DECISION_APPROVE,
    DECISION_REJECT,
    REQUIRED_EVIDENCE,
    GateAssessment,
    decide,
    harvest_eligible,
)

#: The rationale payload's schema marker (spec §4: the evidence + verdict land as
#: the ``decision_deliver`` rationale, so the payload must be machine-readable and
#: self-describing, never prose alone).
RATIONALE_SCHEMA = "fleet-graph.selfgate-rationale/v1"

#: The S7 release-branch prefix. A line's writable-release target is always
#: ``release/<line-id>``; M5 builds the branch model itself, this order only
#: names the semantics the allowlist now circles.
RELEASE_BRANCH_PREFIX = "release"


class SelfGateFacts(Protocol):
    """The engine-side gatherer of the six mechanical obligations.

    Returns the evidence dict the pure ``assess_evidence`` consumes: one entry
    per :data:`REQUIRED_EVIDENCE` key, each an ``{"ok": bool, ...}`` object or a
    plain truthy/falsy value. The production wiring measures each fact (three-way
    acceptance argv, product diff, zero deletion, personal re-run, mutation gun,
    regression baseline); tests inject a scripted gatherer.
    """

    def gather(self, development_id: str) -> dict[str, Any]: ...


class SelfGateDelivery(Protocol):
    """Deliver one APPROVE/REJECT + rationale through the M2 decision surface.

    The production implementation is a thin adapter over
    ``decision_mcp.deliver_decision`` with ``target_kind=dd``; the protocol keeps
    the orchestration independent of the transport.
    """

    def deliver(self, development_id: str, decision: str, rationale: str) -> Any: ...


@dataclass(frozen=True)
class SelfGateResult:
    """One line self-gate run: the verdict, the named reasons, and the rationale.

    ``verdict`` is APPROVE/REJECT; ``assessment`` names exactly which obligation
    failed (or is empty on a clean gate); ``rationale`` is the §4 payload that
    rides the ``decision_deliver`` call; ``evidence`` is the measured facts as
    they were weighed.
    """

    development_id: str
    principal: str
    dispatched_by: str
    verdict: str
    assessment: GateAssessment
    rationale: str
    evidence: dict[str, Any]


def _normalize_evidence(evidence: Any) -> dict[str, Any]:
    """Reduce each of the six obligation entries to ``{"ok": bool, ...}``.

    A caller may short-circuit an obligation to a plain boolean, but the §4
    rationale must carry a stable shape, so a bare ``True`` becomes
    ``{"ok": True}`` and a bare ``False`` ``{"ok": False}``.
    """
    if not isinstance(evidence, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key in REQUIRED_EVIDENCE:
        if key not in evidence:
            continue
        item = evidence[key]
        if isinstance(item, dict):
            normalized[key] = {
                "ok": bool(item.get("ok")),
                **{k: v for k, v in item.items() if k != "ok"},
            }
        else:
            normalized[key] = {"ok": bool(item)}
    return normalized


def template_evidence_rationale(
    *,
    evidence: Any,
    assessment: GateAssessment,
    verdict: str,
    development_id: str = "",
) -> str:
    """Spec §4: template the six evidence results + verdict into a rationale payload.

    The output is a single JSON object carrying the closed six-obligation results,
    the verdict and the named violations. It is what rides the M2
    ``decision_deliver`` call's ``reason`` -- durable, machine-readable, and
    self-describing, never a bare string an operator has to re-derive by hand.
    """
    normalized = _normalize_evidence(evidence)
    payload: dict[str, Any] = {
        "schema": RATIONALE_SCHEMA,
        "development_id": development_id,
        "verdict": verdict,
        "violations": list(assessment.violations),
        "evidence": normalized,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def run_line_selfgate(
    *,
    development_id: str,
    principal: str,
    dispatched_by: str,
    facts: SelfGateFacts,
) -> SelfGateResult:
    """The one mechanical self-gate: gather six facts, decide, template the rationale.

    This is the production caller of ``decide``/``assess_evidence``. It never
    fabricates facts: the ``facts`` port is the engine-side gatherer the wiring
    supplies, and the verdict is APPROVE only on a clean gate *and* a principal
    equal to the single's ``dispatched_by`` (spec §1). Delivery is a separate
    M2 step -- this returns the judgement and its rationale payload.
    """
    evidence = facts.gather(development_id)
    verdict, assessment = decide(evidence, principal=principal, dispatched_by=dispatched_by)
    rationale = template_evidence_rationale(
        evidence=evidence,
        assessment=assessment,
        verdict=verdict,
        development_id=development_id,
    )
    return SelfGateResult(
        development_id=development_id,
        principal=principal,
        dispatched_by=dispatched_by,
        verdict=verdict,
        assessment=assessment,
        rationale=rationale,
        evidence=evidence,
    )


def harvest_eligibility(*, gate_approved: bool, merge_complete: bool) -> tuple[bool, str]:
    """The S7 reactor-facing wrapper around ``selfgate.harvest_eligible``.

    Harvest fires only after the merge segment completes, never on a bare gate
    APPROVE. The harvest reactor calls this before touching any write step so the
    "收割触发点从「闸后」改到「merge 后」" move is a machine-checked fact, not a
    prose discipline.
    """
    return harvest_eligible(gate_approved=gate_approved, merge_complete=merge_complete)


def release_branch_ref(line_id: str) -> str:
    """The S7 writable-release ref for a line: ``refs/heads/release/<line-id>``.

    This is the branch the allowlist now circles for a line's harvest writes; the
    release branch *model* itself stays M5, this order only names the semantics.
    """
    return f"refs/heads/{RELEASE_BRANCH_PREFIX}/{line_id}"


def is_release_writable_repo(
    allowlist: Any, *, repo_path: str, line_id: str
) -> tuple[bool, tuple[str, ...]]:
    """S7: is ``repo_path`` allowlisted as writable for ``release/<line-id>``?

    Delegates the branch-prefix decision to the injected harvest allowlist's
    ``authorize`` (deny-all default). Returns ``(granted, reasons)`` where
    ``reasons`` is non-empty exactly when the write is refused -- the machine
    trace the caller records, never silently swallowed.
    """
    branch = release_branch_ref(line_id)
    try:
        auth = allowlist.authorize(repo_path=repo_path, branch=branch, deploy=())
    except Exception as exc:  # a broken allowlist must refuse, never grant a write
        return False, (f"allowlist authorize raised: {type(exc).__name__}: {exc}",)
    return bool(auth.granted), tuple(getattr(auth, "reasons", ()) or ())


__all__ = [
    "DECISION_APPROVE",
    "DECISION_REJECT",
    "RATIONALE_SCHEMA",
    "RELEASE_BRANCH_PREFIX",
    "SelfGateDelivery",
    "SelfGateFacts",
    "SelfGateResult",
    "harvest_eligibility",
    "is_release_writable_repo",
    "release_branch_ref",
    "run_line_selfgate",
    "template_evidence_rationale",
]
