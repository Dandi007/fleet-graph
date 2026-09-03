"""The A2 read-only arbiter: suggestion-only publication, pinned by contract.

A stateful fake bus and a fake reasoning executor drive the load-bearing cases:

- question triage emits exactly one referenced ``work.note.v1`` suggestion;
- blocked diagnosis emits a recommendation but performs no control action;
- replay is idempotent (an already-referenced A2 note suppresses republication);
- a suggestion never satisfies ``Board.decision_for`` (the human gate stays open);
- decision-shaped model output cannot change the emitted kind/marker or invoke
  a decision path;
- the emitted-kind audit reports only note/suggestion classes and zero
  ``work.decision.*``, and proves it can *distinguish* a decision fixture;
- static conformance rejects any A2 import/reference to the decision publisher
  or a generic publish capability;
- the existing fourth-gate conformance stays green.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.arbiter.a2 import (
    ALLOWED_NOTE_TYPES,
    NOTE_MARKER,
    Recommendation,
    Subject,
    coerce_recommendation,
    collect_subjects,
    run_arbiter,
)
from fleet_graph.arbiter.audit import audit_messages, is_decision_kind
from fleet_graph.bus.board import Board, GateTicket
from fleet_graph.bus.client import BusError, PublishResult

REPO_ROOT = Path(__file__).parent.parent
ARBITER_PKG = REPO_ROOT / "src" / "fleet_graph" / "arbiter"

WORK_NOTES = "board:work-notes"
WORK_INDEX = "board:work-index"


# --- fixtures ---------------------------------------------------------------


class FakeBus:
    """A stateful bus: messages, refs, and publish, so replay and gate reads work."""

    def __init__(
        self,
        notes: list[dict[str, Any]] | None = None,
        cards: list[dict[str, Any]] | None = None,
        refs: dict[str, list[str]] | None = None,
        inbox: list[dict[str, Any]] | None = None,
        publish_error_for: dict[str, Exception] | None = None,
        messages_error_for: dict[str, Exception] | None = None,
        messages_fail_from: dict[str, int] | None = None,
    ) -> None:
        self.notes = list(notes or [])
        self.cards = list(cards or [])
        self.refs: dict[str, list[str]] = {k: list(v) for k, v in (refs or {}).items()}
        self.inbox = list(inbox or [])
        self.published: list[dict[str, Any]] = []
        self.publish_error_for = dict(publish_error_for or {})
        self.messages_error_for = dict(messages_error_for or {})
        self.messages_fail_from = dict(messages_fail_from or {})
        self.message_calls: dict[str, int] = {}
        self._seq = max([m.get("channel_seq", 0) for m in self.notes + self.cards], default=0)

    def messages(self, channel: str, *, limit: int = 100, after_seq: int = 0):
        self.message_calls[channel] = self.message_calls.get(channel, 0) + 1
        error = self.messages_error_for.get(channel)
        fail_from = self.messages_fail_from.get(channel, 1)
        if error is not None and self.message_calls[channel] >= fail_from:
            raise error
        if channel == WORK_NOTES:
            source = self.notes
        elif channel == WORK_INDEX:
            source = self.cards
        else:
            source = self.inbox
        selected = [m for m in source if m.get("channel_seq", 0) > after_seq]
        head = max([m.get("channel_seq", 0) for m in source], default=0)
        return selected[:limit], head

    def refs_to(self, entity_id: str) -> list[dict[str, Any]]:
        return [
            {"message_id": mid, "target_entity": entity_id} for mid in self.refs.get(entity_id, [])
        ]

    def publish(
        self,
        channel: str,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        *,
        refs: list[dict[str, str]] | None = None,
        entity_id: str | None = None,
        supersedes: str | None = None,
    ) -> PublishResult:
        card_entity_id = str(payload.get("card_entity_id") or "")
        error = self.publish_error_for.get(card_entity_id)
        if error is not None:
            raise error
        self._seq += 1
        message_id = f"msg_{self._seq}"
        record = {
            "message_id": message_id,
            "kind": kind,
            "payload": payload,
            "channel_seq": self._seq,
            "entity_id": entity_id or message_id,
            "idempotency_key": idempotency_key,
        }
        if channel == WORK_NOTES:
            self.notes.append(record)
            for ref in refs or []:
                self.refs.setdefault(ref["target_entity"], []).append(message_id)
        elif channel == WORK_INDEX:
            self.cards.append(record)
        self.published.append(
            {"channel": channel, "kind": kind, "payload": payload, "refs": refs or []}
        )
        return PublishResult(
            message_id=message_id,
            entity_id=record["entity_id"],
            channel_seq=self._seq,
            deduplicated=False,
        )


class FakeReasoner:
    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[Subject, dict[str, Any]]] = []

    def recommend(self, subject: Subject, facts: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((subject, facts))
        if not self.responses:
            return {
                "recommendation": "default suggestion",
                "evidence_refs": [],
                "consequence": "reversible",
                "escalation_target": "needs_evidence",
            }
        return self.responses.pop(0)


def note(message_id: str, seq: int, note_type: str, card: str, text: str) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "channel_seq": seq,
        "kind": "work.note.v1",
        "entity_id": message_id,
        "payload": {"card_entity_id": card, "note": text, "note_type": note_type},
    }


def card(entity: str, seq: int, **payload: Any) -> dict[str, Any]:
    return {
        "message_id": f"{entity}-rev{seq}",
        "channel_seq": seq,
        "kind": "work.card.v1",
        "entity_id": entity,
        "payload": payload,
    }


def decision(message_id: str, seq: int, card_id: str) -> dict[str, Any]:
    """The real ``work.decision.v1`` shape, read off the live bus."""
    return {
        "message_id": message_id,
        "channel_seq": seq,
        "kind": "work.decision.v1",
        "entity_id": message_id,
        "payload": {
            "card_entity_id": card_id,
            "decision": "go",
            "decided_by": "human:operator",
            "question": "merge?",
            "rationale": "cheapest",
        },
    }


def valid_response(escalation_target: str = "needs_evidence", **overrides: Any) -> dict[str, Any]:
    response: dict[str, Any] = {
        "recommendation": "looks safe to proceed",
        "evidence_refs": [],
        "consequence": "reversible",
        "escalation_target": escalation_target,
    }
    response.update(overrides)
    return response


# --- question triage --------------------------------------------------------


def test_question_triage_emits_exactly_one_referenced_note() -> None:
    bus = FakeBus(
        notes=[note("q1", 1, "question", "card-a", "should we merge this?")],
        cards=[card("card-a", 1, title="dev", status="doing")],
        refs={"q1": []},
    )
    run = run_arbiter(client=bus, reasoner=FakeReasoner([valid_response()]), publish=True)

    assert len(run.emitted) == 1
    emitted = run.emitted[0]
    assert emitted.kind == "work.note.v1"
    assert emitted.note_type in ALLOWED_NOTE_TYPES
    assert emitted.marker == "suggestion"
    assert "q1" in emitted.subject_refs
    assert len(run.refused) == 0
    assert len(run.suppressed) == 0

    assert len(bus.published) == 1
    record = bus.published[0]
    assert record["kind"] == "work.note.v1"
    assert record["payload"]["note_type"] in ALLOWED_NOTE_TYPES
    assert record["payload"]["note"].startswith(NOTE_MARKER)
    targets = {ref["target_entity"] for ref in record["refs"]}
    assert targets == {"card-a", "q1"}


def test_blocked_diagnosis_emits_recommendation_but_no_control_action() -> None:
    bus = FakeBus(
        notes=[],
        cards=[card("card-a", 1, title="dev", status="blocked")],
    )
    run = run_arbiter(client=bus, reasoner=FakeReasoner([valid_response()]), publish=True)

    assert len(run.emitted) == 1
    assert run.emitted[0].kind == "work.note.v1"
    assert run.emitted[0].note_type == "finding"  # escalation_target set -> finding

    # No control action: the only write is a suggestion note. No card revision,
    # no decision, nothing else.
    kinds = {record["kind"] for record in bus.published}
    assert kinds == {"work.note.v1"}
    assert all(record["payload"]["note_type"] in ALLOWED_NOTE_TYPES for record in bus.published)


def test_replay_is_idempotent() -> None:
    bus = FakeBus(
        notes=[note("q1", 1, "question", "card-a", "should we merge this?")],
        cards=[card("card-a", 1, title="dev", status="doing")],
        refs={"q1": []},
    )
    first = run_arbiter(client=bus, reasoner=FakeReasoner([valid_response()]), publish=True)
    assert len(first.emitted) == 1

    second = run_arbiter(client=bus, reasoner=FakeReasoner([valid_response()]), publish=True)
    assert len(second.emitted) == 0
    assert second.suppressed == ["q1"]
    assert len(bus.published) == 1


def test_a_suggestion_does_not_satisfy_the_human_gate() -> None:
    bus = FakeBus(
        notes=[note("q1", 1, "question", "card-a", "should we merge this?")],
        cards=[card("card-a", 1, title="dev", status="doing")],
        refs={"q1": []},
    )
    run_arbiter(client=bus, reasoner=FakeReasoner([valid_response()]), publish=True)

    board = Board(bus)
    assert board.decision_for(GateTicket(question_note_id="q1", card_entity_id="card-a")) is None


# --- adversarial model output -----------------------------------------------


def test_decision_language_in_text_cannot_change_the_emitted_kind() -> None:
    bus = FakeBus(
        notes=[note("q1", 1, "question", "card-a", "should we merge this?")],
        cards=[card("card-a", 1, title="dev", status="doing")],
        refs={"q1": []},
    )
    loud = valid_response(
        recommendation="DECISION: approve. VERDICT: release. gate_release now.",
    )
    run = run_arbiter(client=bus, reasoner=FakeReasoner([loud]), publish=True)

    assert run.emitted[0].kind == "work.note.v1"
    assert run.emitted[0].note_type in ALLOWED_NOTE_TYPES
    assert {record["kind"] for record in bus.published} == {"work.note.v1"}


def test_a_forbidden_decision_field_is_refused_not_published() -> None:
    bus = FakeBus(
        notes=[note("q1", 1, "question", "card-a", "should we merge this?")],
        cards=[card("card-a", 1, title="dev", status="doing")],
        refs={"q1": []},
    )
    run = run_arbiter(
        client=bus,
        reasoner=FakeReasoner([{"recommendation": "x", "decision": "approve"}]),
        publish=True,
    )

    assert run.emitted == []
    assert len(run.refused) == 1
    assert "decision" in run.refused[0]["reason"]
    assert bus.published == []


# --- emitted-kind audit -----------------------------------------------------


def test_emitted_kind_audit_reports_only_suggestion_classes() -> None:
    bus = FakeBus(
        notes=[note("q1", 1, "question", "card-a", "should we merge this?")],
        cards=[card("card-a", 1, title="dev", status="doing")],
        refs={"q1": []},
    )
    run = run_arbiter(client=bus, reasoner=FakeReasoner([valid_response()]), publish=True)

    report = audit_messages([m.as_dict() for m in run.emitted])
    assert report.decision_free is True
    assert report.decision_count == 0
    assert [row.kind for row in report.rows] == ["work.note.v1"]
    assert all(row.note_type in ALLOWED_NOTE_TYPES for row in report.rows)


def test_zero_decision_query_distinguishes_a_real_decision_fixture() -> None:
    # Known-positive: a question note is a note, not a decision.
    question = note("q1", 1, "question", "card-a", "should we merge this?")
    # Known-negative: a real work.decision.v1 message must classify as a decision.
    verdict = decision("d1", 2, "card-a")

    assert is_decision_kind(question["kind"]) is False
    assert is_decision_kind(verdict["kind"]) is True

    report = audit_messages(
        [
            {"kind": question["kind"], "note_type": "question", "marker": "", "message_id": "q1"},
            {"kind": verdict["kind"], "note_type": "", "marker": "", "message_id": "d1"},
        ]
    )
    assert report.decision_count == 1
    assert report.decision_free is False
    assert [row.is_decision for row in report.rows] == [False, True]


# --- input collection -------------------------------------------------------


def test_consultation_mentioning_arbiter_is_collected() -> None:
    bus = FakeBus(
        notes=[],
        cards=[card("card-a", 1, title="dev", status="doing")],
        inbox=[
            {
                "message_id": "c1",
                "channel_seq": 1,
                "kind": "chat",
                "payload": {"body": "hey @arbiter what should we do?", "card_entity_id": "card-a"},
            }
        ],
    )
    subjects = collect_subjects(bus, alias="arbiter")
    assert [s.kind for s in subjects] == ["consultation"]
    assert subjects[0].subject_id == "c1"


def test_a_question_with_a_decision_is_not_triaged() -> None:
    bus = FakeBus(
        notes=[note("q1", 1, "question", "card-a", "merge?"), decision("d1", 2, "card-a")],
        cards=[card("card-a", 1, title="dev", status="doing")],
        refs={"q1": ["d1"]},
    )
    assert collect_subjects(bus) == []


# --- board pagination (2026-08-31: 频道破千后裸 limit=1000 必截断摔死) ----------


def test_over_thousand_message_channel_triages_every_open_question() -> None:
    """判据: 频道 >1000 条时仍能正确 triage，而不是「不再 crash」。

    已知阴性: 本单前 main 的裸 limit=1000（升序=最老窗）在 >1000 条频道上必
    截断并抛 ``RuntimeError: fetch truncated; refusing to triage a partial
    board``——最老千条窗内读不出尾部的新 question。修复后翻页聚合必须读全，
    尾部新开放问题照常被 triage，旧已裁决问题照常被排除。
    """
    notes: list[dict[str, Any]] = []
    seq = 1
    notes.append(note("q-decided", seq, "question", "card-decided", "decided old question?"))
    seq += 1
    for i in range(1, 1201):  # 1200 条进度消息把频道推到千条以外
        notes.append(note(f"progress-{i}", seq, "progress", "card-a", f"progress {i}"))
        seq += 1
    notes.append(note("q-tail", seq, "question", "card-a", "question beyond first thousand"))
    seq += 1
    notes.append(decision("d-decided", seq, "card-decided"))
    seq += 1
    assert seq == 1204  # 最后一个已用 seq 是 1203

    # 已知阴性复述: 旧裸窗口读不到最尾的 q-tail（它在最老千条窗之外）。
    first_window, head = FakeBus(notes=notes).messages(WORK_NOTES, limit=1000)
    assert head == 1203
    assert all(m["channel_seq"] < head for m in first_window)
    assert "q-tail" not in {m["message_id"] for m in first_window}

    cards = [
        card("card-a", 1, title="dev", status="doing"),
        card("card-decided", 2, title="legacy", status="doing"),
    ]
    bus = FakeBus(
        notes=notes,
        cards=cards,
        refs={"q-decided": ["d-decided"], "q-tail": []},
    )
    subjects = collect_subjects(bus)

    # 翻页真的发生了（>1 页），而不是又退回单窗口。
    assert bus.message_calls[WORK_NOTES] >= 2

    # 尾部新问题不丢、不被截断少报；旧已裁决问题不重复 triage。
    assert [s.subject_id for s in subjects] == ["q-tail"]
    assert subjects[0].kind == "question"
    assert subjects[0].facts["card_head"] == {"title": "dev", "status": "doing"}


def test_sub_hundred_channel_is_byte_identical_to_pre_fix() -> None:
    """频道 <100 条：单页读全，subjects 与破千前逐字节一致（存量零回归）。"""
    bus = FakeBus(
        notes=[note("q1", 1, "question", "card-a", "should we merge?")],
        cards=[card("card-a", 1, title="dev", status="doing")],
        refs={"q1": []},
    )
    subjects = collect_subjects(bus)
    assert [s.as_dict() for s in subjects] == [
        {
            "kind": "question",
            "subject_id": "q1",
            "card_entity_id": "card-a",
            "source_revision": "q1",
            "facts": {
                "question": "should we merge?",
                "refs": [],
                "card_head": {"title": "dev", "status": "doing"},
            },
        }
    ]
    assert bus.message_calls[WORK_NOTES] == 1


def test_empty_board_collects_no_subjects() -> None:
    assert collect_subjects(FakeBus()) == []


def test_pagination_interruption_mid_read_refuses_loudly() -> None:
    """全量翻页中途真实失败（HTTP 5xx）仍响亮拒绝，不静默跳过。"""
    notes = [note(f"note-{i}", i, "progress", "card-a", f"p{i}") for i in range(1, 1201)]
    bus = FakeBus(
        notes=notes,
        cards=[],
        messages_error_for={WORK_NOTES: BusError(500, '{"code": "INTERNAL"}')},
        messages_fail_from={WORK_NOTES: 2},  # 第一页成功，第二页起 HTTP 500
    )
    with pytest.raises(BusError) as exc:
        collect_subjects(bus)
    assert exc.value.status == 500
    assert bus.message_calls[WORK_NOTES] == 2


# --- static conformance -----------------------------------------------------


def _direct_bus_publish_lines(source: str) -> list[int]:
    """Line numbers of direct bus-client publish calls (client.publish /
    _client.publish / board.client.publish / self._client.publish)."""
    tree = ast.parse(source)
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr != "publish":
                continue
            base = node.func.value
            direct_name = isinstance(base, ast.Name) and base.id in {"client", "_client"}
            via_attr = isinstance(base, ast.Attribute) and base.attr in {"client", "_client"}
            if direct_name or via_attr:
                lines.append(node.lineno)
    return lines


def _imports_decision_publisher(source: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("fleet_graph.supervise.decision_publisher"):
                    return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("fleet_graph.supervise.decision_publisher"):
                return True
            if module == "fleet_graph.supervise" and any(
                a.name == "decision_publisher" for a in node.names
            ):
                return True
    return False


def _arbiter_sources() -> list[tuple[str, str]]:
    return [(p.name, p.read_text(encoding="utf-8")) for p in sorted(ARBITER_PKG.glob("*.py"))]


def test_no_arbiter_module_imports_the_decision_publisher() -> None:
    for name, source in _arbiter_sources():
        assert not _imports_decision_publisher(source), f"{name} imports the decision publisher"


def test_generic_publish_is_reachable_only_inside_publisher_module() -> None:
    for name, source in _arbiter_sources():
        if name == "publisher.py":
            continue
        lines = _direct_bus_publish_lines(source)
        assert not lines, f"{name} reaches a generic bus publish at lines {lines}"


def test_the_publisher_module_only_emits_the_note_kind() -> None:
    source = (ARBITER_PKG / "publisher.py").read_text(encoding="utf-8")
    assert "work.decision" not in source
    assert "NOTE_KIND" in source
    # The publisher's only publish call must pass the note kind, not a literal.
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "publish"
            and node.args
        ):
            kind_arg = node.args[1] if len(node.args) > 1 else None
            assert isinstance(kind_arg, ast.Name) and kind_arg.id == "NOTE_KIND"


def test_sabotage_self_verification_catches_generic_publish() -> None:
    assert _direct_bus_publish_lines('client.publish("ch", "work.note.v1", {}, "k")\n') == [1]
    assert _direct_bus_publish_lines('self._client.publish("ch", "work.note.v1", {}, "k")\n') == [1]
    board_note = "board.note(card_entity_id='c', text='t', note_type='finding')\n"
    assert _direct_bus_publish_lines(board_note) == []


def test_sabotage_self_verification_catches_decision_publisher_import() -> None:
    assert _imports_decision_publisher(
        "from fleet_graph.supervise.decision_publisher import publish_release_decision\n"
    )
    assert not _imports_decision_publisher("from fleet_graph.bus.board import NOTE_KIND\n")


def test_existing_fourth_gate_conformance_remains_green() -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_supervisor_conformance.py")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


# --- recommendation contract ------------------------------------------------


def test_recommendation_contract_uses_no_decision_field_names() -> None:
    recommendation = Recommendation(
        subject_id="q1",
        recommendation="suggest",
        evidence_refs=("e1",),
        consequence="reversible",
        escalation_target="needs_evidence",
    )
    fields = set(recommendation.as_dict())
    assert not fields & {"decision", "verdict", "approve", "reject", "gate_release"}
    assert fields == {
        "subject_id",
        "recommendation",
        "evidence_refs",
        "consequence",
        "escalation_target",
    }


def test_coerce_recommendation_accepts_the_allowed_shape() -> None:
    coerced = coerce_recommendation(
        {
            "recommendation": "go",
            "evidence_refs": ["e1"],
            "consequence": "c",
            "escalation_target": "needs_evidence",
        },
        subject_id="q1",
    )
    assert coerced.subject_id == "q1"
    assert coerced.escalation_target == "needs_evidence"
    assert coerced.evidence_refs == ("e1",)


# --- ref guard: non-entity targets must not be published ---------------------


def test_non_entity_evidence_ref_is_not_published_as_target_entity() -> None:
    """A model-emitted legacy ``gate_...`` string must not become a bus ref target.

    The subject's own question note and its card are the only valid ref targets;
    arbitrary evidence strings are untrusted and must not be published as
    ``target_entity`` unless they resolve to a real board entity.
    """
    bus = FakeBus(
        notes=[note("q1", 1, "question", "card-a", "should we merge this?")],
        cards=[card("card-a", 1, title="dev", status="doing")],
        refs={"q1": []},
    )
    gated = valid_response(evidence_refs=["gate_01KZ0W3T17W5EP49MDXJQN6NGG"])
    run = run_arbiter(client=bus, reasoner=FakeReasoner([gated]), publish=True)

    assert len(run.emitted) == 1
    emitted = run.emitted[0]
    assert emitted.kind == "work.note.v1"
    assert emitted.marker == "suggestion"
    assert "gate_01KZ0W3T17W5EP49MDXJQN6NGG" not in emitted.subject_refs
    # the emitted note still references the question note, never the gate string
    assert set(emitted.subject_refs) == {"q1"}

    assert len(bus.published) == 1
    targets = {ref["target_entity"] for ref in bus.published[0]["refs"]}
    assert targets == {"card-a", "q1"}
    assert "gate_01KZ0W3T17W5EP49MDXJQN6NGG" not in targets


def test_real_board_entity_evidence_ref_still_resolves() -> None:
    """An evidence ref that names a real board entity stays a valid ref target."""
    bus = FakeBus(
        notes=[note("q1", 1, "question", "card-a", "should we merge this?")],
        cards=[
            card("card-a", 1, title="dev", status="doing"),
            card("card-b", 2, title="other", status="doing"),
        ],
        refs={"q1": []},
    )
    valid = valid_response(evidence_refs=["card-b"])
    run = run_arbiter(client=bus, reasoner=FakeReasoner([valid]), publish=True)

    assert len(run.emitted) == 1
    targets = {ref["target_entity"] for ref in bus.published[0]["refs"]}
    assert targets == {"card-a", "card-b", "q1"}


def test_non_entity_evidence_ref_survives_in_note_text_for_human_reading() -> None:
    """The non-entity string is dropped from refs but kept in the note text."""
    bus = FakeBus(
        notes=[note("q1", 1, "question", "card-a", "should we merge this?")],
        cards=[card("card-a", 1, title="dev", status="doing")],
        refs={"q1": []},
    )
    gated = valid_response(evidence_refs=["gate_01KZ0W3T17W5EP49MDXJQN6NGG"])
    run_arbiter(client=bus, reasoner=FakeReasoner([gated]), publish=True)

    assert len(bus.published) == 1
    note_text = bus.published[0]["payload"]["note"]
    assert "gate_01KZ0W3T17W5EP49MDXJQN6NGG" in note_text


# --- per-subject publish refusal ---------------------------------------------


def test_publish_failure_is_a_refusal_not_a_crash() -> None:
    """A 422 DERIVATION_ERROR on publish must be a per-subject refusal.

    The tick continues processing the remaining subjects and records the failed
    one as refused instead of raising out of the whole oneshot run.
    """
    derivation_error = BusError(
        422,
        '{"code": "DERIVATION_ERROR",'
        ' "message": "ref target entity [gate_01KZ] not found",'
        ' "details": {"retryable": false}}',
    )
    bus = FakeBus(
        notes=[note("q1", 1, "question", "card-a", "should we merge this?")],
        cards=[
            card("card-a", 1, title="dev", status="doing"),
            card("card-b", 2, title="other", status="blocked"),
        ],
        refs={"q1": []},
        publish_error_for={"card-b": derivation_error},
    )
    responses = [valid_response(), valid_response()]
    run = run_arbiter(client=bus, reasoner=FakeReasoner(responses), publish=True)

    # the question subject on card-a published; the blocked card-b publish failed
    assert len(run.emitted) == 1
    assert run.emitted[0].kind == "work.note.v1"
    assert run.emitted[0].note_type in ALLOWED_NOTE_TYPES
    assert run.emitted[0].marker == "suggestion"
    assert len(run.refused) == 1
    assert run.refused[0]["subject_id"] == "card-b"
    assert "publish failed" in run.refused[0]["reason"]
    # the refused subject did not raise and did not produce an emitted note
    assert len(bus.published) == 1
    assert {record["kind"] for record in bus.published} == {"work.note.v1"}


def test_publish_failure_kind_surface_stays_note_only() -> None:
    """Even when publish fails, nothing decision-shaped is emitted."""
    bus = FakeBus(
        notes=[note("q1", 1, "question", "card-a", "should we merge this?")],
        cards=[card("card-a", 1, title="dev", status="doing")],
        refs={"q1": []},
        publish_error_for={
            "card-a": BusError(
                422, '{"code": "DERIVATION_ERROR", "message": "boom", "details": {}}'
            )
        },
    )
    run = run_arbiter(client=bus, reasoner=FakeReasoner([valid_response()]), publish=True)

    assert run.emitted == []
    assert len(run.refused) == 1
    assert run.refused[0]["subject_id"] == "q1"
    assert bus.published == []
