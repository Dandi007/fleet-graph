"""The immutable value objects of the E2 goal interrupt, and their helpers.

The whole point of E2 is that the *same* generation and continuation survive a
human decision as a durable fact rather than a scheduler re-ignition. Everything
in this module is therefore either an immutable value or a pure function: no IO,
no langgraph, no bus. That keeps the resume key and the checkpoint digest
reproducible -- a resume is only ever accepted for the exact
``(folder_id, generation, question_note_id)`` that asked.

Resume key shape (spec item 1)::

    e2:<folder_id>:<generation>:<question_note_id>

It is unique per suspended question and is the single identity both the normal
resume path and the cursor-compensation path must converge on (spec item 7).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

RESUME_KEY_PREFIX = "e2"

#: The canonical digest for an absent prior terminal. Distinct from the digest
#: of an empty object on purpose: "there was no previous generation" must never
#: collide with "the previous generation terminated with an empty record".
NO_PRIOR_TERMINAL_DIGEST = "sha256:" + "0" * 64


def resume_key_for(folder_id: str, generation: int, question_note_id: str) -> str:
    """The unique identity of one suspended question (spec item 1)."""
    return f"{RESUME_KEY_PREFIX}:{folder_id}:{int(generation)}:{question_note_id}"


def prior_terminal_digest(prior_terminal: dict[str, Any] | None) -> str:
    """A deterministic digest of the prior generation's terminal, or the no-prior
    sentinel. ``None`` means no prior terminal; an unparseable/non-dict value is
    canonicalised to its JSON bytes before hashing so the digest stays stable
    across a resume.

    Reproducible by construction: keys are sorted and the bytes are the exact
    UTF-8 JSON that would be written. This is what N7 compares against so a
    round-zero re-park that merely repeats the same prior blocker is rejected
    rather than re-suspended in a loop.
    """
    if prior_terminal is None:
        return NO_PRIOR_TERMINAL_DIGEST
    canonical = json.dumps(
        prior_terminal, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DecisionRef:
    """One forward reference a decision carries. Immutable by construction."""

    message_id: str = ""
    target_entity: str = ""


@dataclass(frozen=True)
class DecisionInput:
    """The immutable decision record injected into a resumed coordinator turn.

    Constructed *only* from a validated board entity and its reverse refs (spec
    item 3): every field is read off the authoritative decision message plus the
    ``resume_key`` that bound the resume. Nothing else may construct one, and
    nothing may mutate one after construction -- which is exactly what makes a
    coordinator turn able to acknowledge ``message_id`` without trusting any
    caller-supplied prose.
    """

    message_id: str
    channel_seq: int
    decision: str
    rationale: str
    decided_by: str
    question_note_id: str
    card_entity_id: str
    refs: tuple[DecisionRef, ...]
    decided_at: str
    resume_key: str

    def as_dict(self) -> dict[str, Any]:
        """The persisted envelope shape, stable so tests can assert exact keys."""
        return {
            "message_id": self.message_id,
            "channel_seq": int(self.channel_seq),
            "decision": self.decision,
            "rationale": self.rationale,
            "decided_by": self.decided_by,
            "question_note_id": self.question_note_id,
            "card_entity_id": self.card_entity_id,
            "refs": [
                {"message_id": r.message_id, "target_entity": r.target_entity} for r in self.refs
            ],
            "decided_at": self.decided_at,
            "resume_key": self.resume_key,
        }


@dataclass(frozen=True)
class InterruptCheckpoint:
    """The atomically-persisted suspension record (spec item 1).

    ``round_id`` is the line round the coordinator asked the question on. All
    fields are plain values on purpose: a checkpoint that held live objects
    could not be written atomically and re-read by a different process.
    """

    folder_id: str
    generation: int
    round_id: int
    question_note_id: str
    card_entity_id: str
    prior_terminal_digest: str
    resume_key: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "folder_id": self.folder_id,
            "generation": int(self.generation),
            "round_id": int(self.round_id),
            "question_note_id": self.question_note_id,
            "card_entity_id": self.card_entity_id,
            "prior_terminal_digest": self.prior_terminal_digest,
            "resume_key": self.resume_key,
        }


__all__ = [
    "NO_PRIOR_TERMINAL_DIGEST",
    "RESUME_KEY_PREFIX",
    "DecisionInput",
    "DecisionRef",
    "InterruptCheckpoint",
    "prior_terminal_digest",
    "resume_key_for",
]
