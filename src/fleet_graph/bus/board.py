"""The work board: cards, notes, and the human gate.

One structural rule shapes this module: **there is no method here that
publishes a `work.decision.v1`.** Verdicts are the human's to cast, and the
cheapest way to guarantee an agent never casts one is to give it no way to. If
you find yourself wanting to add one, that is the bug.

What an agent *may* do is ask (a `question` note), report (`progress` /
`evidence` / `finding` notes), and claim or advance a card (a `work.card.v1`
revision, CAS-guarded on the entity head).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from fleet_graph.bus.client import BusClient, BusConflict, PublishResult

WORK_INDEX = "board:work-index"
WORK_NOTES = "board:work-notes"

CARD_KIND = "work.card.v1"
NOTE_KIND = "work.note.v1"
DECISION_KIND = "work.decision.v1"
DECISION_KIND_V2 = "work.decision.v2"
#: 读径兼收的全部 decision 消息 kind。v1 是人工问答裁决的老形状（bus 端
#: schema 5 字段、additionalProperties:false，发不出 preauth/gate_release
#: 载荷）；v2 是监督面注册的 oneOf 两变体（preauth / gate_release）。识别
#: 「这是不是一条裁决」的读径一律用本元组，不点名单个版本。
DECISION_KINDS = (DECISION_KIND, DECISION_KIND_V2)

#: The one shared idempotency identity of a goal line's board card. The
#: scheduler daemon (parking escalation) and the E2 interrupt runtime both
#: materialise a goal line's card; they must converge on *one* card per line,
#: so they share one key and one payload constructor. Same key + same payload
#: makes the bus idempotency deduplicate the two producers onto one entity;
#: the ``e2-goal-line-card:`` hotfix key is gone, and nothing may depend on it.
GOAL_LINE_CARD_KEY_PREFIX = "goal-line-card"

NoteType = str  # "progress" | "evidence" | "finding" | "question"


@dataclass(frozen=True)
class Decision:
    """A human verdict answering one question note."""

    message_id: str
    decision: str
    decided_by: str
    question: str
    rationale: str
    card_entity_id: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class NormalizedVerdict:
    """The canonical token plus the raw field and shell form it was read from.

    `verdict` is always the exact ASCII bytes ``APPROVE`` or ``REJECT`` (never
    a synonym or a sentence). `raw` is the decision field verbatim, so the
    normalization can be replayed without trusting a prior run's parser.
    `form` names which wrapper, if any, was removed.
    """

    verdict: str
    raw: str
    form: str


FORM_BARE = "bare"
FORM_QUOTE = "quote"
FORM_INLINE_CODE = "inline_code"
FORM_FENCED_CODE = "fenced_code"
FORM_LABEL = "label"

_ASCII_WS = " \t\r\n"
_LABEL_RE = re.compile(r"^(?:decision|verdict):[ \t\r\n]+", re.IGNORECASE)


def _ascii_trim(value: str) -> str:
    return value.strip(_ASCII_WS)


def _ascii_upper(value: str) -> str:
    return "".join(chr(ord(ch) - 32) if "a" <= ch <= "z" else ch for ch in value)


def _quote_line_content(line: str) -> str | None:
    """The content under one Markdown quote prefix, or None.

    The ``>`` may be followed by at most one space; a second ``>`` is a nested
    blockquote level, so ``>>`` is not a single quote marker.
    """
    if not line.startswith(">"):
        return None
    if len(line) > 1 and line[1] == ">":
        return None
    rest = line[1:]
    if rest.startswith(" "):
        rest = rest[1:]
        if rest.startswith(" "):
            return None
    return rest


def _strip_markdown_quote(value: str) -> str | None:
    """Remove one outer Markdown quote wrapper, or refuse mixed quoting.

    Every non-empty line must carry exactly one quote prefix; a value that
    quotes only some of its non-empty lines is invalid and yields None.
    """
    lines = value.split("\n")
    out: list[str] = []
    seen_quote = False
    seen_plain = False
    for line in lines:
        if not line.strip(_ASCII_WS):
            out.append("")
            continue
        content = _quote_line_content(line)
        if content is None:
            seen_plain = True
            out.append(line)
        else:
            seen_quote = True
            out.append(content)
    if seen_quote and seen_plain:
        return None
    if not seen_quote:
        return value
    return "\n".join(out)


def _strip_inline_code(value: str) -> str | None:
    """Remove one single-backtick inline code shell, or None if broken.

    A triple-backtick fence is *not* an inline shell; it is left for the fenced
    step. Interior backticks inside a single-backtick shell are invalid.
    """
    if not value.startswith("`") or value.startswith("``"):
        return value
    if not value.endswith("`") or value.endswith("``"):
        return value
    interior = value[1:-1]
    if "`" in interior:
        return None
    return interior


def _strip_fenced_code(value: str) -> str | None:
    """Remove one fenced-code shell, or None if it has extra content lines.

    The opening and closing lines must be exactly three backticks with no info
    string, and exactly one non-empty content line may sit between them.
    """
    fence = "```"
    if not (value.startswith(fence + "\n") and value.endswith("\n" + fence)):
        return value
    inner = value[len(fence) + 1 : -(len(fence) + 1)]
    if inner == "" or "\n" in inner:
        return None
    return inner


def _strip_label(value: str) -> str:
    """Remove one ASCII `decision:`/`verdict:` label prefix, case-insensitively."""
    match = _LABEL_RE.match(value)
    if match is None:
        return value
    return value[match.end() :]


def normalize_decision(raw: Any) -> NormalizedVerdict | None:
    """Normalize a raw gate decision field to a canonical verdict, or None.

    Wide in input, strict in output: the exact bytes ``APPROVE`` or ``REJECT``
    survive only after a strictly bounded sequence of wrapper removals. Every
    other value -- prose, punctuation, a Unicode lookalike, a second token --
    returns None and is refused upstream as ``GATE_VERDICT_UNRECOGNIZED``.
    """
    if not isinstance(raw, str):
        return None

    original = raw
    value = _ascii_trim(raw)
    if value == "":
        return None
    form = FORM_BARE

    quoted = _strip_markdown_quote(value)
    if quoted is None:
        return None
    if quoted != value:
        form = FORM_QUOTE
        value = _ascii_trim(quoted)

    inlined = _strip_inline_code(value)
    if inlined is None:
        return None
    if inlined != value:
        form = FORM_INLINE_CODE
        value = _ascii_trim(inlined)
    else:
        fenced = _strip_fenced_code(value)
        if fenced is None:
            return None
        if fenced != value:
            form = FORM_FENCED_CODE
            value = _ascii_trim(fenced)

    labelless = _strip_label(value)
    if labelless != value:
        form = FORM_LABEL
        value = _ascii_trim(labelless)

    verdict = _ascii_upper(_ascii_trim(value))
    if verdict not in ("APPROVE", "REJECT"):
        return None
    return NormalizedVerdict(verdict=verdict, raw=original, form=form)


@dataclass(frozen=True)
class GateTicket:
    """A question that is waiting on a human. Cheap to checkpoint."""

    question_note_id: str
    card_entity_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "question_note_id": self.question_note_id,
            "card_entity_id": self.card_entity_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> GateTicket:
        return cls(
            question_note_id=data["question_note_id"],
            card_entity_id=data["card_entity_id"],
        )


def goal_line_card_key(folder_id: str) -> str:
    """The shared idempotency key of one goal line's board card."""
    return f"{GOAL_LINE_CARD_KEY_PREFIX}:{folder_id}"


def goal_line_card_payload(*, folder_id: str, title: str) -> dict[str, Any]:
    """The shared ``work.card.v1`` payload of one goal line's board card.

    ``title`` must be identical across both producers for the same ``folder_id``
    so the payload is byte-identical and the bus deduplicates rather than
    conflict-ing. Both the scheduler's parking escalation and the interrupt
    runtime collapse to ``folder_id`` as the title (the design's sanctioned
    alternative to threading the roster alias into the line process, which the
    production launch chain does not deliver), so the two payloads always agree.
    """
    return {
        "title": title,
        "status": "doing",
        "intent": f"goal-line escalation surface for {folder_id}",
        "work_folder_id": folder_id,
    }


class Board:
    def __init__(
        self,
        client: BusClient,
        *,
        index_channel: str = WORK_INDEX,
        notes_channel: str = WORK_NOTES,
        observability_channel: str | None = None,
    ) -> None:
        self.client = client
        self.index_channel = index_channel
        self.notes_channel = notes_channel
        self.observability_channel = observability_channel

    # --- cards -----------------------------------------------------------

    def publish_card(self, payload: dict[str, Any], idempotency_key: str) -> PublishResult:
        return self.client.publish(self.index_channel, CARD_KIND, payload, idempotency_key)

    def revise_card(
        self,
        *,
        entity_id: str,
        supersedes: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> PublishResult:
        """Publish a new revision of a card.

        Raises BusConflict when `supersedes` is no longer the head -- someone
        claimed or advanced the card first. Re-read with `card_head` and decide
        again rather than clobbering their revision.
        """
        return self.client.publish(
            self.index_channel,
            CARD_KIND,
            payload,
            idempotency_key,
            entity_id=entity_id,
            supersedes=supersedes,
        )

    def card_head(self, entity_id: str) -> dict[str, Any] | None:
        """The newest revision of one card, by channel order."""
        messages, _ = self.client.messages(self.index_channel, limit=1000)
        revisions = [
            m for m in messages if m.get("entity_id") == entity_id and m.get("kind") == CARD_KIND
        ]
        if not revisions:
            return None
        return max(revisions, key=lambda m: m["channel_seq"])

    # --- notes -----------------------------------------------------------

    def note(
        self,
        *,
        card_entity_id: str,
        text: str,
        note_type: NoteType,
        idempotency_key: str,
    ) -> PublishResult:
        return self.client.publish(
            self.notes_channel,
            NOTE_KIND,
            {"card_entity_id": card_entity_id, "note": text, "note_type": note_type},
            idempotency_key,
            refs=[{"target_entity": card_entity_id}],
        )

    def evidence(self, *, card_entity_id: str, text: str, idempotency_key: str) -> PublishResult:
        return self.note(
            card_entity_id=card_entity_id,
            text=text,
            note_type="evidence",
            idempotency_key=idempotency_key,
        )

    def progress(self, *, card_entity_id: str, text: str, idempotency_key: str) -> PublishResult:
        return self.note(
            card_entity_id=card_entity_id,
            text=text,
            note_type="progress",
            idempotency_key=idempotency_key,
        )

    # --- human gate ------------------------------------------------------

    def ask(self, *, card_entity_id: str, question: str, idempotency_key: str) -> GateTicket:
        """Raise a question for a human and return a ticket to wait on.

        The ticket is the whole state: checkpoint it, and a restarted graph can
        resume waiting without re-asking. The idempotency key is what stops a
        retry from posting the same question twice.
        """
        result = self.note(
            card_entity_id=card_entity_id,
            text=question,
            note_type="question",
            idempotency_key=idempotency_key,
        )
        return GateTicket(question_note_id=result.message_id, card_entity_id=card_entity_id)

    def decision_for(self, ticket: GateTicket) -> Decision | None:
        """The verdict answering this question, or None while it is still open.

        Resolution goes through the ref graph rather than text matching: a
        decision is an answer to *this* question only if it references it.
        """
        referencing = self.client.refs_to(ticket.question_note_id)
        if not referencing:
            return None
        candidate_ids = {ref["message_id"] for ref in referencing}

        messages, _ = self.client.messages(self.notes_channel, limit=1000)
        decisions = [
            m
            for m in messages
            if m["message_id"] in candidate_ids and m.get("kind") in DECISION_KINDS
        ]
        if not decisions:
            return None
        newest = max(decisions, key=lambda m: m["channel_seq"])
        payload = newest.get("payload", {})
        return Decision(
            message_id=newest["message_id"],
            decision=str(payload.get("decision", "")),
            decided_by=str(payload.get("decided_by", "")),
            question=str(payload.get("question", "")),
            rationale=str(payload.get("rationale", "")),
            card_entity_id=str(payload.get("card_entity_id", ticket.card_entity_id)),
            raw=newest,
        )

    # --- observability ---------------------------------------------------

    def observe(self, event: dict[str, Any], idempotency_key: str) -> None:
        """Best-effort telemetry to the bypass channel.

        Inherited from the pump (findings-recon 3a): the observability write
        must never be able to stall or fail the work it is observing. Every
        error here is swallowed on purpose.
        """
        if not self.observability_channel:
            return
        try:
            self.client.publish(self.observability_channel, "gd.event.v1", event, idempotency_key)
        except Exception:
            # Swallowed on purpose: see docstring. Telemetry must not bite.
            return


__all__ = [
    "FORM_BARE",
    "FORM_FENCED_CODE",
    "FORM_INLINE_CODE",
    "FORM_LABEL",
    "FORM_QUOTE",
    "GOAL_LINE_CARD_KEY_PREFIX",
    "Board",
    "BusConflict",
    "Decision",
    "GateTicket",
    "NormalizedVerdict",
    "goal_line_card_key",
    "goal_line_card_payload",
    "normalize_decision",
]
