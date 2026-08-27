"""The pending-verdict view: questions nobody has answered yet.

Zero new storage on purpose. A question is pending exactly when
`Board.decision_for()` finds no decision (v1 or v2) referencing it -- the same
primitive the dd gate resumes on, so this view can never disagree with what a
suspended pipeline would do. The old supervisor's sqlite Decision Inbox is the
counter-example this replaces: a second copy of "pending" that nothing ever
resolved (r4-design §4, §6a).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from fleet_graph.bus.board import (
    CARD_KIND,
    NOTE_KIND,
    WORK_INDEX,
    WORK_NOTES,
    Board,
    GateTicket,
)
from fleet_graph.bus.client import BusClient

# The board is small (tens of messages); one page is the whole channel today.
# If a channel ever outgrows this, the fetch truncates *oldest first* and the
# head_seq comparison below turns that silent gap into a loud one.
FETCH_LIMIT = 1000


@dataclass(frozen=True)
class PendingQuestion:
    """One row of the view: a question with no decision answering it."""

    question_note_id: str
    created_at: str
    age_seconds: int
    summary: str
    card_entity_id: str
    card_title: str
    card_status: str
    work_folder_id: str
    development_id: str
    has_evidence_followup: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "question_note_id": self.question_note_id,
            "created_at": self.created_at,
            "age_seconds": self.age_seconds,
            "age": format_age(self.age_seconds),
            "summary": self.summary,
            "card_entity_id": self.card_entity_id,
            "card_title": self.card_title,
            "card_status": self.card_status,
            "work_folder_id": self.work_folder_id,
            "development_id": self.development_id,
            "has_evidence_followup": self.has_evidence_followup,
        }


def parse_iso(value: str) -> float:
    """Epoch seconds from the bus's `created_at` (RFC3339, Z or offset)."""
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def format_age(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def _card_heads(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    heads: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message.get("kind") != CARD_KIND:
            continue
        entity = str(message.get("entity_id") or "")
        current = heads.get(entity)
        if current is None or message["channel_seq"] > current["channel_seq"]:
            heads[entity] = message
    return heads


def list_pending(client: BusClient, *, now: float | None = None) -> list[PendingQuestion]:
    """Every question note on the board that no decision references. Read-only."""
    board = Board(client)
    now = time.time() if now is None else now

    notes, notes_head = client.messages(WORK_NOTES, limit=FETCH_LIMIT)
    if notes and notes[-1]["channel_seq"] < notes_head:
        raise RuntimeError(
            f"{WORK_NOTES} holds {notes_head} messages but one fetch returned "
            f"only up to seq {notes[-1]['channel_seq']}; refusing to render a "
            "view with a silent gap"
        )
    cards, _ = client.messages(WORK_INDEX, limit=FETCH_LIMIT)
    heads = _card_heads(cards)
    notes_by_id = {note["message_id"]: note for note in notes}

    pending: list[PendingQuestion] = []
    for note in notes:
        payload = note.get("payload") or {}
        if note.get("kind") != NOTE_KIND or payload.get("note_type") != "question":
            continue
        card_entity_id = str(payload.get("card_entity_id") or "")
        ticket = GateTicket(question_note_id=note["message_id"], card_entity_id=card_entity_id)
        # The one authoritative pending test. Anything cheaper would be a
        # second opinion about what the suspended gate itself will read.
        if board.decision_for(ticket) is not None:
            continue

        referencing = {ref["message_id"] for ref in client.refs_to(note["message_id"])}
        has_evidence = any(
            notes_by_id.get(ref_id, {}).get("kind") == NOTE_KIND
            and (notes_by_id.get(ref_id, {}).get("payload") or {}).get("note_type") == "evidence"
            for ref_id in referencing
        )

        head_payload = (heads.get(card_entity_id) or {}).get("payload") or {}
        created_at = str(note.get("created_at") or "")
        age = int(now - parse_iso(created_at)) if created_at else 0
        summary = str(payload.get("note") or "").strip().splitlines()
        pending.append(
            PendingQuestion(
                question_note_id=note["message_id"],
                created_at=created_at,
                age_seconds=age,
                summary=summary[0] if summary else "",
                card_entity_id=card_entity_id,
                card_title=str(head_payload.get("title") or ""),
                card_status=str(head_payload.get("status") or ""),
                work_folder_id=str(head_payload.get("work_folder_id") or ""),
                development_id=str(head_payload.get("development_id") or ""),
                has_evidence_followup=has_evidence,
            )
        )
    pending.sort(key=lambda row: row.age_seconds, reverse=True)
    return pending


def render_text(rows: list[PendingQuestion]) -> str:
    if not rows:
        return "inbox 空：板上没有待裁决的 question。"
    lines = [f"{len(rows)} 条待裁决："]
    for row in rows:
        anchor = row.work_folder_id or row.development_id or row.card_entity_id or "-"
        evidence = "有审计跟帖" if row.has_evidence_followup else "无审计跟帖"
        lines.append(
            f"- [{format_age(row.age_seconds):>6}] {row.question_note_id}  "
            f"({anchor}, {row.card_status or '?'}, {evidence})\n"
            f"    {row.summary[:120]}"
        )
    return "\n".join(lines)


__all__ = ["PendingQuestion", "format_age", "list_pending", "parse_iso", "render_text"]
