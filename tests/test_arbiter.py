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
from fleet_graph.bus.client import PublishResult

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
    ) -> None:
        self.notes = list(notes or [])
        self.cards = list(cards or [])
        self.refs: dict[str, list[str]] = {k: list(v) for k, v in (refs or {}).items()}
        self.inbox = list(inbox or [])
        self.published: list[dict[str, Any]] = []
        self._seq = max([m.get("channel_seq", 0) for m in self.notes + self.cards], default=0)

    def messages(self, channel: str, *, limit: int = 100, after_seq: int = 0):
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
                "needs_human": True,
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


def valid_response(needs_human: bool = True, **overrides: Any) -> dict[str, Any]:
    response: dict[str, Any] = {
        "recommendation": "looks safe to proceed",
        "evidence_refs": [],
        "consequence": "reversible",
        "needs_human": needs_human,
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
    assert run.emitted[0].note_type == "finding"  # needs_human -> finding

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
        needs_human=True,
    )
    fields = set(recommendation.as_dict())
    assert not fields & {"decision", "verdict", "approve", "reject", "gate_release"}
    assert fields == {"subject_id", "recommendation", "evidence_refs", "consequence", "needs_human"}


def test_coerce_recommendation_accepts_the_allowed_shape() -> None:
    coerced = coerce_recommendation(
        {"recommendation": "go", "evidence_refs": ["e1"], "consequence": "c", "needs_human": True},
        subject_id="q1",
    )
    assert coerced.subject_id == "q1"
    assert coerced.needs_human is True
    assert coerced.evidence_refs == ("e1",)
