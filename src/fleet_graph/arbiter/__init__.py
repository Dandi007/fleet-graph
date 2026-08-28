"""The A2 read-only fleet arbiter: triage and suggest, never decide.

Three tools live here, under fleet-graph's consumer/orchestration layer (not the
agent-bus transport): ``a2`` is the triage/reason/publish core, ``publisher`` is
the constructionally restricted write surface (``work.note.v1`` with
``note_type`` in ``{finding, progress}`` only), and ``audit`` is the emitted-kind
query surface that proves the zero-decision claim.

Nothing in this package can publish a decision. The recommendation envelope uses
no field named decision/verdict/approve/reject/gate_release, and a suggestion
never satisfies a board gate: ``Board.decision_for`` only recognises
``work.decision.*`` kinds, which the arbiter cannot emit.
"""

from fleet_graph.arbiter.a2 import (
    ALLOWED_NOTE_TYPES,
    BLOCKED_STATUSES,
    DEFAULT_REASONING_MODEL,
    FORBIDDEN_FIELDS,
    NOTE_MARKER,
    ArbiterRun,
    EmittedMessage,
    Reasoner,
    Recommendation,
    RecommendationInvalid,
    Subject,
    TextReasoner,
    coerce_recommendation,
    collect_subjects,
    run_arbiter,
)
from fleet_graph.arbiter.audit import AuditReport, AuditRow, audit_messages, is_decision_kind
from fleet_graph.arbiter.publisher import SuggestionPublisher

__all__ = [
    "ALLOWED_NOTE_TYPES",
    "BLOCKED_STATUSES",
    "DEFAULT_REASONING_MODEL",
    "FORBIDDEN_FIELDS",
    "NOTE_MARKER",
    "ArbiterRun",
    "AuditReport",
    "AuditRow",
    "EmittedMessage",
    "Reasoner",
    "Recommendation",
    "RecommendationInvalid",
    "Subject",
    "SuggestionPublisher",
    "TextReasoner",
    "audit_messages",
    "coerce_recommendation",
    "collect_subjects",
    "is_decision_kind",
    "run_arbiter",
]
