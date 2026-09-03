"""A2: the read-only fleet arbiter -- triage and suggest, never decide.

Rescope of ``wf-7cd0a7`` (see the development spec). The arbiter reads board
facts through the existing bus client abstractions and reasons through a
read-only executor seam; it may publish only ``work.note.v1`` finding/progress
notes that are plainly marked suggestions. There is no verdict authority here:
no code path constructs a decision, imports the decision publisher, or hands
the reasoning path a generic publish capability.

Authorities this module refuses by construction:

- no ``work.decision.v1`` / ``work.decision.v2`` publication -- the only writes
  go through ``arbiter/publisher.py``, whose surface is ``work.note.v1`` with
  ``note_type`` in ``{finding, progress}``;
- no import / call / subprocess / dynamic-import / alias of
  ``fleet_graph.supervise.decision_publisher`` (pinned by the supervisor
  conformance guard);
- no merge, gate release, cancel, deployment, schema/token lifecycle, or
  capability mutation;
- no model harness spawned directly -- reasoning goes through
  ``executors/text_node.TextNode`` (in-process, gateway-only), or an injected
  seam in tests.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from fleet_graph.bus.board import (
    CARD_KIND,
    NOTE_KIND,
    WORK_INDEX,
    WORK_NOTES,
    Board,
    GateTicket,
)
from fleet_graph.bus.client import BusClient
from fleet_graph.executors.text_node import TextNode, TextSpec

#: The closed set of note types the A2 publisher may emit. Nothing else.
ALLOWED_NOTE_TYPES = frozenset({"finding", "progress"})

#: Field names the recommendation contract must never carry. Their presence is
#: treated as a decision-shaped model output and refused outright -- the model
#: cannot smuggle a verdict field into a note.
FORBIDDEN_FIELDS = ("decision", "verdict", "approve", "reject", "gate_release")

#: Card head statuses the arbiter treats as a blocked development worth a
#: read-only diagnosis. Terminal states are deliberately absent.
BLOCKED_STATUSES = frozenset({"blocked", "awaiting_gate"})

#: The closed vocabulary of named escalation targets. It replaces the bare
#: ``needs_human`` boolean, which could not tell apart three genuinely
#: different destinations for a suggestion (缺陷⑫, wf-8d9737).
ESCALATION_TARGETS = frozenset({"dispatching_line", "supervisor_escalation", "needs_evidence"})

#: A dd unit past acceptance / at its gate: the normal course is
#: dispatching-line self-judgment (D5 -- the dispatching line judges its own
#: gate), not waiting on a person.
ESCALATION_DISPATCHING_LINE = "dispatching_line"

#: A class-B escalation: the supervisor must answer (direction / production
#: action ruling).
ESCALATION_SUPERVISOR_ESCALATION = "supervisor_escalation"

#: Nobody can judge on the current evidence: go back and gather what is
#: missing (named in the recommendation text).
ESCALATION_NEEDS_EVIDENCE = "needs_evidence"

#: Back-compat routing of a legacy ``needs_human: true`` payload: the boolean
#: cannot name a destination, so the default target is routed by the subject's
#: form. A blocked/awaiting-gate development card is the dd-unit-at-gate shape
#: (dispatching line owns it); an addressed consultation is a direction ask
#: (supervisor answers); an open question note has no addressee -- go back for
#: evidence. Unknown forms degrade to evidence gathering as well.
LEGACY_TRUE_TARGET_BY_SUBJECT_KIND = {
    "blocked": ESCALATION_DISPATCHING_LINE,
    "consultation": ESCALATION_SUPERVISOR_ESCALATION,
    "question": ESCALATION_NEEDS_EVIDENCE,
}
LEGACY_TRUE_DEFAULT_TARGET = ESCALATION_NEEDS_EVIDENCE

#: Every A2 note is stamped with this prefix, so a suggestion is plainly marked
#: and replay-idempotency can recognise it by content, not by a private index.
NOTE_MARKER = "[A2 suggestion — not a decision]"

NOTES_LIMIT = 1000

#: Board reads page the channel ascending and aggregate in PAGE_SIZE windows.
#: PAGE_SIZE stays at the old single-read ceiling so a sub-thousand channel
#: reads in exactly one page, byte-identical to the pre-fix behaviour.
PAGE_SIZE = NOTES_LIMIT

#: Paging safety ceiling: a channel cannot need more pages than it has head
#: seqs per page. Exceeding it means the pages are not catching up to head --
#: a real pagination interruption, which must be refused loudly, not skipped.
MAX_PAGES = 1000

#: Default logical model for the read-only reasoning role. Orchestration code
#: names a logical model; the gateway resolves keys and failover (invariant 3).
DEFAULT_REASONING_MODEL = "deepseek-v4-flash"


@dataclass(frozen=True)
class Recommendation:
    """The model's recommendation envelope. Fields are fixed; nothing here is
    named decision / verdict / approve / reject / gate_release."""

    subject_id: str
    recommendation: str
    evidence_refs: tuple[str, ...]
    consequence: str
    escalation_target: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "recommendation": self.recommendation,
            "evidence_refs": list(self.evidence_refs),
            "consequence": self.consequence,
            "escalation_target": self.escalation_target,
        }


class RecommendationInvalid(ValueError):
    """The model answered in a shape the arbiter will not act on."""


def _coerce_escalation_target(raw: dict[str, Any], *, subject_kind: str) -> str:
    """Resolve the named escalation target, with legacy-boolean back-compat.

    A named ``escalation_target`` must sit inside the closed vocabulary -- an
    out-of-vocabulary or empty value is refused outright. A legacy payload
    carrying only ``needs_human`` stays parseable: ``true`` routes to the
    default target for the subject's form, ``false`` (or absence) means no
    escalation (empty target), so old readers and old payloads keep working.
    """
    if "escalation_target" in raw:
        target = raw.get("escalation_target")
        if not isinstance(target, str) or target not in ESCALATION_TARGETS:
            raise RecommendationInvalid(
                f"escalation_target must be one of {sorted(ESCALATION_TARGETS)}, "
                f"got {target!r} -- refused"
            )
        return target
    if raw.get("needs_human"):
        return LEGACY_TRUE_TARGET_BY_SUBJECT_KIND.get(subject_kind, LEGACY_TRUE_DEFAULT_TARGET)
    return ""


def coerce_recommendation(raw: Any, *, subject_id: str, subject_kind: str = "") -> Recommendation:
    """Parse a model result into a Recommendation, refusing decision-shaped output.

    Only the five allowed keys are read. A payload carrying any forbidden field
    name is refused outright. Free text is preserved as-is: it can never change
    the emitted kind or marker, which the publisher fixes.

    ``subject_kind`` (question / blocked / consultation) only feeds the
    legacy ``needs_human: true`` back-compat routing; it never overrides a
    named ``escalation_target``.
    """
    if not isinstance(raw, dict):
        raise RecommendationInvalid(f"recommendation must be an object, got {type(raw).__name__}")
    offending = sorted(name for name in FORBIDDEN_FIELDS if name in raw)
    if offending:
        raise RecommendationInvalid(
            f"model output carries forbidden field(s) {offending} -- refused"
        )
    recommendation = raw.get("recommendation")
    if not isinstance(recommendation, str) or not recommendation.strip():
        raise RecommendationInvalid("recommendation text is required")
    evidence = raw.get("evidence_refs")
    if evidence is None:
        evidence_refs: tuple[str, ...] = ()
    elif isinstance(evidence, list) and all(isinstance(item, str) for item in evidence):
        evidence_refs = tuple(evidence)
    else:
        raise RecommendationInvalid("evidence_refs must be a list of strings")
    consequence = raw.get("consequence")
    if not isinstance(consequence, str):
        consequence = ""
    return Recommendation(
        subject_id=subject_id,
        recommendation=recommendation.strip(),
        evidence_refs=evidence_refs,
        consequence=consequence,
        escalation_target=_coerce_escalation_target(raw, subject_kind=subject_kind),
    )


@dataclass(frozen=True)
class Subject:
    """One immutable triage input the arbiter reads off the board.

    ``source_revision`` is the message id of the newest fact this subject is
    derived from; it is part of the idempotency key so replay cannot duplicate
    a recommendation for an unchanged subject.
    """

    kind: str  # "question" | "blocked" | "consultation"
    subject_id: str
    card_entity_id: str
    source_revision: str
    facts: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "subject_id": self.subject_id,
            "card_entity_id": self.card_entity_id,
            "source_revision": self.source_revision,
            "facts": self.facts,
        }


class Reasoner(Protocol):
    """The read-only reasoning seam. Tests substitute a fake; production uses
    :class:`TextReasoner` (in-process, gateway-only)."""

    def recommend(self, subject: Subject, facts: dict[str, Any]) -> dict[str, Any]: ...


class TextReasoner:
    """Invokes the reasoning role through ``executors/text_node``.

    Read-only by construction: a pure-text completion has no tools, no repo,
    and no publish path. The system prompt constrains the shape to the five
    allowed fields and forbids decision vocabulary as field names.
    """

    SYSTEM_PROMPT = (
        "You are the A2 fleet arbiter, a read-only triage and suggestion role. "
        "You have no decision authority and no tools. Given board facts, return "
        "exactly one JSON object with these keys only: "
        '"subject_id" (string), "recommendation" (string), '
        '"evidence_refs" (array of strings, may be empty), '
        '"consequence" (string, a reversibility note), '
        '"escalation_target" (one of: '
        '"dispatching_line" -- a dd unit past acceptance, route to the '
        "dispatching line: the unit's dispatched_by self-judges the gate; "
        '"supervisor_escalation" -- a supervisor must answer this escalation; '
        '"needs_evidence" -- nobody can judge yet, name the missing evidence '
        'in "recommendation"). '
        "Never emit keys named decision, verdict, approve, reject, or gate_release."
    )

    def __init__(
        self, *, model: str = DEFAULT_REASONING_MODEL, node: TextNode | None = None
    ) -> None:
        self.model = model
        self._node = node if node is not None else TextNode()

    def recommend(self, subject: Subject, facts: dict[str, Any]) -> dict[str, Any]:
        prompt = json.dumps({"subject": subject.as_dict(), "facts": facts}, ensure_ascii=False)
        result = self._node.complete(
            TextSpec(model=self.model, system=self.SYSTEM_PROMPT, require_text=True),
            prompt,
        )
        return _parse_json_object(result.text)


def _parse_json_object(text: str) -> dict[str, Any]:
    """Extract a JSON object from model text, tolerating fences and prose."""
    stripped = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end <= start:
            raise RecommendationInvalid(
                f"reasoning role returned no JSON object: {stripped[:200]!r}"
            ) from None
        try:
            parsed = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise RecommendationInvalid(f"reasoning role returned unparseable JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RecommendationInvalid(
            f"reasoning role returned {type(parsed).__name__}, not an object"
        )
    return parsed


@dataclass(frozen=True)
class EmittedMessage:
    """One message an A2 run emitted (or would emit, in dry-run)."""

    kind: str
    note_type: str
    marker: str
    message_id: str
    subject_refs: tuple[str, ...]
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "note_type": self.note_type,
            "marker": self.marker,
            "message_id": self.message_id,
            "subject_refs": list(self.subject_refs),
        }


@dataclass
class ArbiterRun:
    """The outcome of one A2 tick: what was emitted, suppressed, refused."""

    emitted: list[EmittedMessage] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = True

    def audit(self) -> Any:
        from fleet_graph.arbiter.audit import audit_messages

        return audit_messages([message.as_dict() for message in self.emitted])


# --- board reading ----------------------------------------------------------


def _read_full_channel(
    client: BusClient,
    channel_id: str,
    *,
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES,
) -> list[dict[str, Any]]:
    """Read a whole channel by sequential page aggregation (ascending).

    选型理由: #178 给 decision_for 修的是固定尾窗 (先学 head_seq, 再读
    after_seq=head-N 的尾部窗口)——那一支只找「最新一条裁决」, 尾窗足够。但
    arbiter 的板面读取要枚举*全部*未决 question/consultation 与最新卡面,
    固定尾窗在频道超过窗口后会把旧开放问题丢出窗外——那不是「拒绝残缺」, 是
    静默漏诊 (缺陷族第九式), 且与破千前 (整条频道落在单个窗口内) 的结果
    不一致。这里改为顺序翻页聚合: 以当次 GET 返回的 head_seq 为终态判据,
    翻到本页末 seq 追平 head 为止; 频道任意长都能读全, 翻页间隔里 head 前移
    则继续追。只有真实读取失败 (HTTP 错误由 client 抛 BusError; 翻页中断 /
    空窗追不平 head / 超过页数上限) 才触发「残缺板面拒绝」的响亮语义——
    绝不因频道长度触发。
    """
    collected: list[dict[str, Any]] = []
    after_seq = 0
    for _ in range(max_pages):
        page, head_seq = client.messages(channel_id, limit=page_size, after_seq=after_seq)
        if not page:
            if head_seq == 0:
                return collected
            raise RuntimeError(
                f"{channel_id} page empty at seq {after_seq} (head {head_seq}); "
                "refusing to triage a partial board"
            )
        collected.extend(page)
        if page[-1]["channel_seq"] >= head_seq:
            return collected
        after_seq = page[-1]["channel_seq"]
    raise RuntimeError(
        f"{channel_id} read exceeded {max_pages} pages; refusing to triage a partial board"
    )


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


def _message_body(message: dict[str, Any]) -> str:
    payload = message.get("payload") or {}
    body = payload.get("body")
    if isinstance(body, str) and body:
        return body
    return str(payload.get("note") or payload.get("summary") or "")


def collect_subjects(client: BusClient, *, alias: str | None = None) -> list[Subject]:
    """Read immutable board facts and shape them into triage subjects.

    Three input classes: open ``work.note.v1`` question notes (and their refs),
    blocked/non-terminal development cards, and -- when ``alias`` is given --
    consultation messages in the arbiter inbox that mention ``arbiter``. Message
    bodies are treated as untrusted data, never instructions.
    """
    board = Board(client)
    notes = _read_full_channel(client, WORK_NOTES)
    cards = _read_full_channel(client, WORK_INDEX)
    heads = _card_heads(cards)

    subjects: list[Subject] = []

    for note in notes:
        payload = note.get("payload") or {}
        if note.get("kind") != NOTE_KIND or payload.get("note_type") != "question":
            continue
        card_entity_id = str(payload.get("card_entity_id") or "")
        question_note_id = note["message_id"]
        ticket = GateTicket(question_note_id=question_note_id, card_entity_id=card_entity_id)
        if board.decision_for(ticket) is not None:
            continue
        refs = client.refs_to(question_note_id)
        subjects.append(
            Subject(
                kind="question",
                subject_id=question_note_id,
                card_entity_id=card_entity_id,
                source_revision=question_note_id,
                facts={
                    "question": str(payload.get("note") or ""),
                    "refs": [dict(ref) for ref in refs],
                    "card_head": (heads.get(card_entity_id) or {}).get("payload") or {},
                },
            )
        )

    for entity, head in heads.items():
        payload = head.get("payload") or {}
        status = str(payload.get("status") or "")
        if status not in BLOCKED_STATUSES:
            continue
        subjects.append(
            Subject(
                kind="blocked",
                subject_id=entity,
                card_entity_id=entity,
                source_revision=head["message_id"],
                facts={"status": status, "card_head": payload},
            )
        )

    if alias:
        inbox_messages = _read_full_channel(client, f"agent:{alias}")
        for message in inbox_messages:
            body = _message_body(message)
            if "arbiter" not in body.lower():
                continue
            payload = message.get("payload") or {}
            subjects.append(
                Subject(
                    kind="consultation",
                    subject_id=message["message_id"],
                    card_entity_id=str(payload.get("card_entity_id") or ""),
                    source_revision=message["message_id"],
                    facts={"body": body},
                )
            )

    return subjects


# --- publishing -------------------------------------------------------------


def _already_referenced(client: BusClient, subject: Subject, notes: list[dict[str, Any]]) -> bool:
    """True when an A2 note already references this subject (replay suppression)."""
    refs = client.refs_to(subject.subject_id)
    if not refs:
        return False
    by_id = {message["message_id"]: message for message in notes}
    for ref in refs:
        message = by_id.get(ref.get("message_id"))
        if message is None:
            continue
        payload = message.get("payload") or {}
        if message.get("kind") == NOTE_KIND and str(payload.get("note") or "").startswith(
            NOTE_MARKER
        ):
            return True
    return False


def _board_entities(notes: list[dict[str, Any]], cards: list[dict[str, Any]]) -> frozenset[str]:
    """The real board entity ids the arbiter observed this tick.

    A published ``target_entity`` must resolve to a real board entity: a card
    entity id (a root ``work.card.v1``) or a note entity id (a real
    ``work.note.v1``). Model-emitted ``evidence_refs`` are untrusted strings and
    are published only when they match one of these ids.
    """
    ids: set[str] = set()
    for message in (*notes, *cards):
        entity = str(message.get("entity_id") or "")
        if entity:
            ids.add(entity)
    return frozenset(ids)


def _subject_refs(
    subject: Subject,
    recommendation: Recommendation,
    known_entities: frozenset[str],
) -> tuple[str, ...]:
    """The ref targets an A2 note may carry, restricted to real board entities.

    ``card_entity_id`` (added by the publisher) and the subject's own
    ``subject_id`` -- its question note -- are always valid ref targets. Model-
    emitted ``evidence_refs`` are untrusted strings and survive only when they
    resolve to a real board entity; non-entity strings stay out of the published
    refs (they remain in the note text and the recommendation envelope for human
    reading).
    """
    refs = [subject.subject_id, *recommendation.evidence_refs]
    seen = {subject.card_entity_id}
    ordered: list[str] = []
    for ref in refs:
        if not ref or ref in seen:
            continue
        if ref != subject.subject_id and ref not in known_entities:
            continue
        seen.add(ref)
        ordered.append(ref)
    return tuple(ordered)


#: Per-target guidance rendered into the note body. ``dispatching_line`` must
#: point at the dispatching line's self-judgment (D5) and never at a person
#: taking the call; ``supervisor_escalation`` keeps the supervisor-must-answer
#: wording; ``needs_evidence`` composes its line with the missing-evidence
#: text, so it lives in ``_render_note``.
_GUIDANCE_LINES = {
    ESCALATION_DISPATCHING_LINE: (
        "guidance: route to the dispatching line -- the unit's dispatched_by "
        "self-judges (D5: the dispatching line owns its own gate)"
    ),
    ESCALATION_SUPERVISOR_ESCALATION: (
        "guidance: supervisor escalation -- the supervisor must answer this "
        "escalation (direction / production-action ruling)"
    ),
}


def _render_note(recommendation: Recommendation) -> str:
    target = recommendation.escalation_target
    lines = [
        NOTE_MARKER,
        f"subject_id: {recommendation.subject_id}",
        f"escalation_target: {target or 'none'}",
        f"recommendation: {recommendation.recommendation}",
    ]
    if target == ESCALATION_NEEDS_EVIDENCE:
        lines.append(
            "guidance: needs evidence -- go back for evidence; "
            f"missing: {recommendation.recommendation}"
        )
    elif target in _GUIDANCE_LINES:
        lines.append(_GUIDANCE_LINES[target])
    if recommendation.consequence:
        lines.append(f"consequence: {recommendation.consequence}")
    if recommendation.evidence_refs:
        lines.append("evidence_refs: " + ", ".join(recommendation.evidence_refs))
    return "\n".join(lines)


# --- one-tick entry point ---------------------------------------------------


def run_arbiter(
    *,
    client: BusClient,
    reasoner: Reasoner,
    subjects: list[Subject] | None = None,
    publish: bool = False,
    alias: str | None = None,
) -> ArbiterRun:
    """One tick: collect subjects, reason over each, publish suggestions.

    ``publish=False`` is the default and the safe state: the arbiter reasons and
    records what it *would* publish, but writes nothing to the board. Only an
    explicit ``publish=True`` turns suggestions into notes.
    """
    from fleet_graph.arbiter.publisher import SuggestionPublisher

    if subjects is None:
        subjects = collect_subjects(client, alias=alias)
    publisher = SuggestionPublisher(Board(client))
    run = ArbiterRun(dry_run=not publish)
    notes = _read_full_channel(client, WORK_NOTES)
    cards = _read_full_channel(client, WORK_INDEX)
    known_entities = _board_entities(notes, cards)
    seen: set[str] = set()

    for subject in subjects:
        if subject.subject_id in seen or _already_referenced(client, subject, notes):
            run.suppressed.append(subject.subject_id)
            continue
        seen.add(subject.subject_id)
        try:
            raw = reasoner.recommend(subject, subject.facts)
            recommendation = coerce_recommendation(
                raw, subject_id=subject.subject_id, subject_kind=subject.kind
            )
        except Exception as exc:  # a refusal, not a crash: one bad subject must not sink the tick
            run.refused.append({"subject_id": subject.subject_id, "reason": str(exc)[:400]})
            continue
        note_type = "finding" if recommendation.escalation_target else "progress"
        text = _render_note(recommendation)
        idempotency_key = f"arbiter-a2:{subject.subject_id}:{subject.source_revision}"
        subject_refs = _subject_refs(subject, recommendation, known_entities)
        message_id = ""
        if publish:
            try:
                result = publisher.publish(
                    card_entity_id=subject.card_entity_id,
                    note_type=note_type,
                    text=text,
                    subject_refs=subject_refs,
                    idempotency_key=idempotency_key,
                )
            except Exception as exc:  # a refused publish, not a crash: keep the tick alive
                reason = f"publish failed: {str(exc)[:400]}"
                run.refused.append({"subject_id": subject.subject_id, "reason": reason})
                continue
            message_id = result.message_id
        run.emitted.append(
            EmittedMessage(
                kind=NOTE_KIND,
                note_type=note_type,
                marker="suggestion",
                message_id=message_id,
                subject_refs=subject_refs,
                dry_run=not publish,
            )
        )
    return run


__all__ = [
    "ALLOWED_NOTE_TYPES",
    "BLOCKED_STATUSES",
    "DEFAULT_REASONING_MODEL",
    "ESCALATION_DISPATCHING_LINE",
    "ESCALATION_NEEDS_EVIDENCE",
    "ESCALATION_SUPERVISOR_ESCALATION",
    "ESCALATION_TARGETS",
    "FORBIDDEN_FIELDS",
    "NOTE_MARKER",
    "ArbiterRun",
    "EmittedMessage",
    "Reasoner",
    "Recommendation",
    "RecommendationInvalid",
    "Subject",
    "TextReasoner",
    "coerce_recommendation",
    "collect_subjects",
    "run_arbiter",
]
