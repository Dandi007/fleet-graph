"""The A2 emitted-kind audit surface: what a run published, and whether it
stayed suggestion-only.

The zero-decision claim is only meaningful if the audit can tell a decision
apart from a note, so :func:`is_decision_kind` is a real classifier, not a
vacuous "nothing here" check. A ``work.decision.v1`` / ``work.decision.v2`` kind
classifies as a decision; every other kind is non-decision. The acceptance
evidence is the audit over a real A2 run reporting zero decisions while the same
classifier reports one for a known-negative decision fixture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Read-only classifier prefix. Deliberately *not* the board's decision kind
#: constants: the arbiter package must never import or carry a decision kind.
DECISION_KIND_PREFIX = "work.decision"

NOTE_KIND = "work.note.v1"


def is_decision_kind(kind: Any) -> bool:
    """True for a ``work.decision.*`` message kind; False for notes and everything else."""
    return isinstance(kind, str) and kind.startswith(DECISION_KIND_PREFIX)


@dataclass(frozen=True)
class AuditRow:
    kind: str
    note_type: str
    marker: str
    message_id: str
    subject_refs: tuple[str, ...]

    @property
    def is_decision(self) -> bool:
        return is_decision_kind(self.kind)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "note_type": self.note_type,
            "marker": self.marker,
            "message_id": self.message_id,
            "subject_refs": list(self.subject_refs),
        }


@dataclass
class AuditReport:
    rows: list[AuditRow] = field(default_factory=list)

    @property
    def decision_count(self) -> int:
        return sum(1 for row in self.rows if row.is_decision)

    @property
    def decision_free(self) -> bool:
        return self.decision_count == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_free": self.decision_free,
            "decision_count": self.decision_count,
            "messages": [row.as_dict() for row in self.rows],
        }


def audit_messages(messages: list[dict[str, Any]]) -> AuditReport:
    """Classify emitted-message records and report the zero-decision claim.

    Each record carries ``kind``, ``note_type``, ``marker``, ``message_id`` and
    ``subject_refs`` -- the shape an A2 run records for every message it emits.
    """
    rows = [
        AuditRow(
            kind=str(record.get("kind") or ""),
            note_type=str(record.get("note_type") or ""),
            marker=str(record.get("marker") or ""),
            message_id=str(record.get("message_id") or ""),
            subject_refs=tuple(str(ref) for ref in (record.get("subject_refs") or [])),
        )
        for record in messages
    ]
    return AuditReport(rows=rows)


__all__ = ["AuditReport", "AuditRow", "audit_messages", "is_decision_kind"]
