"""Decision-to-interrupt resolution for E2, plus the legacy fallback.

The authoritative-resolver work (``decision_bridge/resolver.py``) still owns the
normal question-id path. This module adds the two E2-specific pieces that live
beside it rather than inside it, so neither path reaches into the other:

- ``newest_decision`` / ``compensate_decision`` implement the cursor-compensation
  selection (spec item 7): the newest valid decision by ``(channel_seq,
  message_id)``, compared against the locally recorded ``last_decision_message_id``.
- ``legacy_owner_fallback`` is the bounded fallback for legacy parked owners
  that predate the persisted ``board_question_note_id`` (spec item 6). It is
  deliberately write-free: it never fabricates or mutates a question id -- a
  zero/multiple match is a safe ``legacy_owner_ambiguous`` no-resume.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from fleet_graph.decision_bridge.owners import OwnerTarget
from fleet_graph.goal_interrupt.contract import (
    DecisionInput,
    DecisionRef,
    resume_key_for,
)

#: Outcomes for the legacy fallback (closed). Only ``resolved`` resumes.
LEGACY_OUTCOME_RESOLVED = "resolved"
LEGACY_OUTCOME_AMBIGUOUS = "legacy_owner_ambiguous"

#: The identifier characters a ``folder_id`` may be made of. A folder id is
#: bounded by anything outside this set, so an exact ``wf-abc`` never matches
#: the ``wf-abc123`` inside another line's question text (spec item 6's "exact
#: ``folder_id``").
_ID_CHARS_CLASS = r"A-Za-z0-9_-"


def _folder_id_in_text(text: str, folder_id: str) -> bool:
    """Exact, delimited occurrence of ``folder_id`` in a question's text."""
    pattern = rf"(?<![{_ID_CHARS_CLASS}]){re.escape(folder_id)}(?![{_ID_CHARS_CLASS}])"
    return re.search(pattern, text) is not None


def newest_decision(decisions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The newest valid decision by ``(channel_seq, message_id)``, or None."""
    if not decisions:
        return None
    return max(
        decisions,
        key=lambda m: (int(m.get("channel_seq") or 0), str(m.get("message_id") or "")),
    )


def decision_is_newer(decision: dict[str, Any], existing: dict[str, Any] | None) -> bool:
    """Is ``decision`` newer than the previously recorded one?

    ``existing`` is a canary record (e.g. a compensation receipt or a resume
    receipt) carrying at least a ``channel_seq``/``last_decision_seq`` and a
    message id. Newer means strictly greater ``(channel_seq, message_id)``.
    """
    if existing is None:
        return True
    old_pair = (
        int(existing.get("channel_seq") or existing.get("last_decision_seq") or 0),
        str(existing.get("message_id") or existing.get("last_decision_message_id") or ""),
    )
    new_pair = (
        int(decision.get("channel_seq") or 0),
        str(decision.get("message_id") or ""),
    )
    return new_pair > old_pair


def compensate_decision(
    decisions: list[dict[str, Any]],
    *,
    last_decision_message_id: str,
) -> tuple[dict[str, Any] | None, bool]:
    """Cursor compensation: choose the newest valid decision and say whether it
    is a *new* decision relative to ``last_decision_message_id``.

    Returns ``(decision, need_compensation)``. ``need_compensation`` is True
    only when the newest decision is a different message than the one already
    applied -- a republish or replay of the same message yields ``(newest,
    False)`` and must not force a republish, a rollback, or a second resume.
    """
    newest = newest_decision(decisions)
    if newest is None:
        return None, False
    return newest, str(newest.get("message_id") or "") != (last_decision_message_id or "")


@dataclass(frozen=True)
class LegacyResolution:
    """The legacy fallback's answer for one decision."""

    outcome: str
    target: OwnerTarget | None = None
    reason: str = ""
    question_note_id: str = ""

    @property
    def resolved(self) -> bool:
        return self.outcome == LEGACY_OUTCOME_RESOLVED


def legacy_owner_fallback(
    *,
    folder_id: str,
    referenced_question_ids: list[str],
    question_texts: dict[str, str],
    legacy_owners: list[OwnerTarget],
) -> LegacyResolution:
    """Bounded fallback for a legacy parked owner with no persisted question id.

    A legacy parked owner (a line parked before ``board_question_note_id`` was
    persisted) can still be recovered when all of these hold, and *only* then:

    - one of the decision's references names a question whose immutable text
      carries the exact ``folder_id`` (bounded match, no substring);
    - exactly one non-terminal legacy parked owner is waiting on that
      ``folder_id`` (``target.state`` is not terminal).

    Anything else -- zero such owners, more than one, a question that does not
    name the folder, a stale owner -- is a safe ``legacy_owner_ambiguous``
    no-resume. This function never fabricates or mutates a question id.
    """
    non_terminal = [owner for owner in legacy_owners if not _is_terminal(owner)]
    matching_questions = [
        qid
        for qid in referenced_question_ids
        if _folder_id_in_text(question_texts.get(qid, ""), folder_id)
    ]
    matches = [
        (owner, qid)
        for owner in non_terminal
        for qid in matching_questions
        if owner.id == folder_id
    ]

    if len(matching_questions) == 0:
        return LegacyResolution(
            LEGACY_OUTCOME_AMBIGUOUS,
            reason=f"no referenced question names folder_id {folder_id!r}",
        )

    if len(matches) == 1:
        owner, qid = matches[0]
        return LegacyResolution(
            LEGACY_OUTCOME_RESOLVED,
            target=owner,
            reason="legacy_owner_resolution",
            question_note_id=qid,
        )

    if len(matches) == 0:
        return LegacyResolution(
            LEGACY_OUTCOME_AMBIGUOUS,
            reason=f"no non-terminal legacy parked owner for folder_id {folder_id!r}",
        )

    return LegacyResolution(
        LEGACY_OUTCOME_AMBIGUOUS,
        reason=(
            f"multiple legacy parked owners match folder_id {folder_id!r}: "
            + ", ".join(f"{o.id}:g{o.generation}" for o, _ in matches)
        ),
    )


def _is_terminal(owner: OwnerTarget) -> bool:
    """A parked line is non-terminal if its state is the waiting ``parked``
    state; any other state has moved on and is treated as terminal."""
    return bool(owner.state) and owner.state != "parked"


def decision_input_from_message(
    message: dict[str, Any],
    *,
    resume_key: str,
    question_note_id: str,
    card_entity_id: str = "",
    references: list[dict[str, str]] | None = None,
) -> DecisionInput:
    """Build the immutable ``DecisionInput`` for one validated decision.

    Every field is read off the authoritative board message (never a caller's
    prose): the payload's ``decision``/``rationale``/``decided_by``, the
    message's own ``message_id``/``channel_seq``/``created_at``, and the refs
    the decision carries (the reverse-refs surface, passed as ``references``).
    """
    payload = message.get("payload") or {}
    refs = tuple(
        DecisionRef(
            message_id=str(ref.get("message_id") or ""),
            target_entity=str(ref.get("target_entity") or ""),
        )
        for ref in (references or [])
    )
    return DecisionInput(
        message_id=str(message.get("message_id") or ""),
        channel_seq=int(message.get("channel_seq") or 0),
        decision=str(payload.get("decision") or ""),
        rationale=str(payload.get("rationale") or ""),
        decided_by=str(payload.get("decided_by") or ""),
        question_note_id=question_note_id,
        card_entity_id=str(card_entity_id or payload.get("card_entity_id") or ""),
        refs=refs,
        decided_at=str(message.get("created_at") or ""),
        resume_key=resume_key,
    )


__all__ = [
    "LEGACY_OUTCOME_AMBIGUOUS",
    "LEGACY_OUTCOME_RESOLVED",
    "LegacyResolution",
    "compensate_decision",
    "decision_input_from_message",
    "decision_is_newer",
    "legacy_owner_fallback",
    "newest_decision",
    "resume_key_for",
]
