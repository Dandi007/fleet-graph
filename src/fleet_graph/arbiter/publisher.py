"""The A2 publisher surface -- the only write the arbiter can perform.

Constructionally restricted to ``work.note.v1`` with ``note_type`` in
``{finding, progress}``. There is no generic publish method here and no kind
parameter: the only kind this module can emit is the note kind, and the only
note types it can emit are the two suggestion types. Nothing else in the
arbiter package reaches ``BusClient.publish``.
"""

from __future__ import annotations

from typing import Any

from fleet_graph.bus.board import NOTE_KIND, WORK_NOTES, Board
from fleet_graph.bus.client import BusClient, PublishResult

ALLOWED_NOTE_TYPES = frozenset({"finding", "progress"})


class SuggestionPublisher:
    """A ``Board``-backed publisher with a two-note-type surface only."""

    def __init__(self, board: Board) -> None:
        self._board = board
        self._client: BusClient = board.client

    def publish(
        self,
        *,
        card_entity_id: str,
        note_type: str,
        text: str,
        subject_refs: tuple[str, ...] = (),
        idempotency_key: str,
    ) -> PublishResult:
        if note_type not in ALLOWED_NOTE_TYPES:
            raise ValueError(
                f"A2 may only publish notes of type {sorted(ALLOWED_NOTE_TYPES)}, got {note_type!r}"
            )
        refs: list[dict[str, Any]] = [{"target_entity": card_entity_id}]
        for subject in subject_refs:
            if subject and subject != card_entity_id:
                refs.append({"target_entity": subject})
        return self._client.publish(
            WORK_NOTES,
            NOTE_KIND,
            {"card_entity_id": card_entity_id, "note": text, "note_type": note_type},
            idempotency_key,
            refs=refs,
        )


__all__ = ["ALLOWED_NOTE_TYPES", "SuggestionPublisher"]
