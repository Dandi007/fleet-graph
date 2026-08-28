"""The strict resolver: one ``work.decision.v1`` -> at most one waiting owner.

Everything is a structured verdict, never a fuzzy guess. A decision that names
zero owners, multiple owners, a stale owner, or that fails validation each
produce a *terminal no-op* resolution the bridge seals as a receipt -- the
cursor still advances past it, because the decision has been conclusively
considered. Only a decision validated end-to-end and mapped to exactly one
still-waiting owner yields an ``ok`` resolution.

The action key is defined once, here, because its exact shape is part of the
owner-dedup contract:

    e1:<source_message_id>:<target_kind>:<target_id>:<generation>
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from fleet_graph.bus.board import DECISION_KIND, WORK_NOTES
from fleet_graph.decision_bridge.owners import OwnerSource, OwnerTarget

#: Resolution categories (closed). ``ok`` is the only one the bridge recovers.
CATEGORY_OK = "ok"
CATEGORY_NO_WAITING_OWNER = "no_waiting_owner"
CATEGORY_AMBIGUOUS = "ambiguous"
CATEGORY_STALE = "stale"
CATEGORY_INVALID = "invalid"

#: States an owner must still be in for a resume to be valid. A development
#: that is no longer ``awaiting_gate`` (or a line no longer parked) has moved
#: on; resuming it would be recovering an arbitrary target, so it is stale.
WAITING_DD_STATE = "awaiting_gate"
WAITING_LINE_STATE = "parked"

#: A v1 decision must carry a non-empty ``decision`` token.
_DECISION_RE = re.compile(r"\S")


@dataclass(frozen=True)
class Resolution:
    category: str
    reason: str
    target: OwnerTarget | None = None
    question_note_id: str = ""
    card_entity_id: str = ""

    @property
    def ok(self) -> bool:
        return self.category == CATEGORY_OK


def action_key_for(
    source_message_id: str, target_kind: str, target_id: str, generation: int
) -> str:
    """The exact owner-dedup action key (spec item 4)."""
    return f"e1:{source_message_id}:{target_kind}:{target_id}:{generation}"


def _question_refs(message: dict[str, Any]) -> list[str]:
    """The question-note entities this decision references, from its refs.

    A ``work.decision.v1`` answering a gate references the question note via its
    ``refs``. Messages the fake bus serves carry ``refs`` inline; when absent
    we accept a ``payload.question_note_id`` as a fallback for decision
    shapes that carry the question inline. Unknown/missing -> empty.
    """
    refs = message.get("refs")
    ids: list[str] = []
    if isinstance(refs, list):
        for ref in refs:
            if isinstance(ref, dict) and ref.get("target_entity"):
                ids.append(str(ref["target_entity"]))
    if not ids:
        payload = message.get("payload") or {}
        if payload.get("question_note_id"):
            ids.append(str(payload["question_note_id"]))
    seen: list[str] = []
    for value in ids:
        if value not in seen:
            seen.append(value)
    return seen


def _validate(message: dict[str, Any]) -> str | None:
    """A refusal reason when the message is not a valid v1 decision, else None.

    Mirrors the gate's unresolved predicate, read-only: channel + exact kind +
    a non-empty decision payload + at least one ref. Nothing is published, so
    this never needs the decision-publish credential (and must never hold it).
    """
    if message.get("kind") != DECISION_KIND:
        return f"kind {message.get('kind')!r} is not {DECISION_KIND!r}"
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return "payload is not an object"
    if not _DECISION_RE.search(str(payload.get("decision") or "")):
        return "payload.decision is empty"
    if not _question_refs(message):
        return "decision references no question note (refs empty)"
    return None


def resolve_decision(
    message: dict[str, Any],
    owner_source: OwnerSource,
    *,
    channel_id: str | None = None,
) -> Resolution:
    """Map one board message to at most one waiting owner.

    ``channel_id`` (when the caller knows it) adds the channel-allowlist check:
    the bridge only ever reads ``board:work-notes``, and the resolver re-states
    that rather than trusting the poller. Validation failures, zero/multiple
    matches, and stale owners are all terminal no-op categories with a
    structured reason.
    """
    if channel_id is not None and channel_id != WORK_NOTES:
        return Resolution(
            CATEGORY_INVALID,
            f"channel {channel_id!r} is not the allowlist {WORK_NOTES!r}",
        )

    invalid = _validate(message)
    if invalid is not None:
        return Resolution(CATEGORY_INVALID, f"invalid decision: {invalid}")

    question_ids = _question_refs(message)

    matches: list[OwnerTarget] = []
    discovery_errors: list[str] = []
    for question_note_id in question_ids:
        try:
            matches.extend(owner_source.discover(question_note_id))
        except Exception as exc:  # fail open on the *read* side: resolve nothing
            discovery_errors.append(f"{type(exc).__name__}: {exc}")

    # Dedup on (kind, id, generation): a decision can only recover one owner
    # once, so the *same* owner surfaced for two of its refs is one owner.
    unique: dict[tuple[str, str, int], OwnerTarget] = {}
    for target in matches:
        unique[(target.kind, target.id, target.generation)] = target
    matches = list(unique.values())

    if not matches:
        if discovery_errors:
            return Resolution(
                CATEGORY_NO_WAITING_OWNER,
                "no waiting owner resolved the decision"
                f" (discovery failures: {'; '.join(discovery_errors)[:300]})",
            )
        return Resolution(CATEGORY_NO_WAITING_OWNER, "no waiting owner references this question")

    if len(matches) > 1:
        return Resolution(
            CATEGORY_AMBIGUOUS,
            "decision matches multiple waiting owners: "
            + ", ".join(f"{t.kind}:{t.id}:g{t.generation}" for t in matches),
        )

    target = matches[0]
    stale_reason = _stale_reason(target, message)
    if stale_reason is not None:
        return Resolution(CATEGORY_STALE, stale_reason, target=target)

    return Resolution(
        CATEGORY_OK,
        "resolved to exactly one waiting owner",
        target=target,
        question_note_id=target.question_note_id,
        card_entity_id=target.card_entity_id,
    )


def _stale_reason(target: OwnerTarget, message: dict[str, Any]) -> str | None:
    """Why this owner is stale, or None when it is still waiting.

    Re-checks the facts the gate itself holds: the owner's expected waiting
    state, the card binding, and the question identity. A mismatch means
    recovering would act on the wrong target, so it degrades to no-op.
    """
    expected_state = WAITING_DD_STATE if target.kind == "dd" else WAITING_LINE_STATE
    if target.state and target.state != expected_state:
        return (
            f"owner {target.kind}:{target.id}:g{target.generation} state "
            f"{target.state!r} is not {expected_state!r}"
        )
    payload = message.get("payload") or {}
    payload_card = str(payload.get("card_entity_id") or "")
    if payload_card and target.card_entity_id and payload_card != target.card_entity_id:
        return f"decision card {payload_card!r} does not match owner card {target.card_entity_id!r}"
    if target.question_note_id not in _question_refs(message):
        return f"owner question {target.question_note_id!r} is not referenced by the decision"
    return None


__all__ = [
    "CATEGORY_AMBIGUOUS",
    "CATEGORY_INVALID",
    "CATEGORY_NO_WAITING_OWNER",
    "CATEGORY_OK",
    "CATEGORY_STALE",
    "WAITING_DD_STATE",
    "WAITING_LINE_STATE",
    "Resolution",
    "action_key_for",
    "resolve_decision",
]
