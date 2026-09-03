"""A2 escalation targets: the ``needs_human`` boolean split into named destinations.

缺陷⑫（wf-8d9737）: the arbiter's single ``needs_human`` boolean could not
distinguish three genuinely different destinations, and the renderer pointed
every escalation at a human maintainer -- wrong for a dd unit past acceptance,
whose gate the dispatching line judges itself (D5). Pinned here:

- the closed three-value ``escalation_target`` vocabulary routes and renders
  per destination (``dispatching_line`` / ``supervisor_escalation`` /
  ``needs_evidence``), and a dd unit past acceptance renders
  dispatching-line self-judgment guidance and never a human-verdict pointer;
- legacy ``needs_human`` payloads stay parseable: ``true`` routes to the
  default target for the subject's form, ``false`` means no escalation;
- out-of-vocabulary or empty named targets refuse loudly (at parse time and
  at tick time as a per-subject refusal, never a publication);
- note_type follows the target: finding iff a target is set, progress
  otherwise; the SYSTEM_PROMPT carries the named field and keeps the
  decision-vocabulary red line.
"""

from __future__ import annotations

from typing import Any

import pytest

from fleet_graph.arbiter.a2 import (
    ESCALATION_DISPATCHING_LINE,
    ESCALATION_NEEDS_EVIDENCE,
    ESCALATION_SUPERVISOR_ESCALATION,
    ESCALATION_TARGETS,
    NOTE_MARKER,
    Recommendation,
    RecommendationInvalid,
    TextReasoner,
    coerce_recommendation,
    run_arbiter,
)

WORK_NOTES = "board:work-notes"
WORK_INDEX = "board:work-index"


# --- fixtures ---------------------------------------------------------------


class FakeBus:
    """A minimal stateful bus: the three read channels plus publish."""

    def __init__(
        self,
        notes: list[dict[str, Any]] | None = None,
        cards: list[dict[str, Any]] | None = None,
        refs: dict[str, list[str]] | None = None,
    ) -> None:
        self.notes = list(notes or [])
        self.cards = list(cards or [])
        self.inbox: list[dict[str, Any]] = []
        self.refs: dict[str, list[str]] = {k: list(v) for k, v in (refs or {}).items()}
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
    ) -> Any:
        from fleet_graph.bus.client import PublishResult

        del idempotency_key, entity_id, supersedes
        self._seq += 1
        message_id = f"msg_{self._seq}"
        record = {
            "message_id": message_id,
            "channel_seq": self._seq,
            "kind": kind,
            "payload": payload,
        }
        if channel == WORK_NOTES:
            self.notes.append(record)
            for ref in refs or []:
                self.refs.setdefault(ref["target_entity"], []).append(message_id)
        elif channel == WORK_INDEX:
            self.cards.append(record)
        self.published.append({"channel": channel, "kind": kind, "payload": payload})
        return PublishResult(
            message_id=message_id,
            entity_id=message_id,
            channel_seq=self._seq,
            deduplicated=False,
        )


class FixedReasoner:
    """Replays one canned response per call, ignoring subject shape."""

    def __init__(self, *responses: dict[str, Any]) -> None:
        self._responses = [dict(response) for response in responses]

    def recommend(self, subject: Any, facts: dict[str, Any]) -> dict[str, Any]:
        del subject, facts
        if not self._responses:
            raise AssertionError("no canned response left")
        return self._responses.pop(0)


def note(message_id: str, seq: int, note_type: str, card_id: str, text: str) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "channel_seq": seq,
        "kind": "work.note.v1",
        "entity_id": message_id,
        "payload": {"card_entity_id": card_id, "note": text, "note_type": note_type},
    }


def card(entity: str, seq: int, **payload: Any) -> dict[str, Any]:
    return {
        "message_id": f"{entity}-rev{seq}",
        "channel_seq": seq,
        "kind": "work.card.v1",
        "entity_id": entity,
        "payload": payload,
    }


def question_bus() -> FakeBus:
    return FakeBus(
        notes=[note("q1", 1, "question", "card-a", "should we merge this?")],
        cards=[card("card-a", 2, title="dev", status="doing")],
        refs={"q1": []},
    )


def named_response(target: str, **overrides: Any) -> dict[str, Any]:
    response: dict[str, Any] = {
        "recommendation": "triage suggestion",
        "evidence_refs": [],
        "consequence": "reversible",
        "escalation_target": target,
    }
    response.update(overrides)
    return response


# --- the closed vocabulary ----------------------------------------------------


def test_escalation_target_vocabulary_is_exactly_the_three_named_destinations() -> None:
    assert {"dispatching_line", "supervisor_escalation", "needs_evidence"} == ESCALATION_TARGETS
    assert ESCALATION_TARGETS.isdisjoint({"", "none", "human", "needs_human"})


def test_recommendation_envelope_carries_the_named_target_not_the_boolean() -> None:
    recommendation = Recommendation(
        subject_id="q1",
        recommendation="suggest",
        evidence_refs=(),
        consequence="",
        escalation_target=ESCALATION_DISPATCHING_LINE,
    )
    payload = recommendation.as_dict()
    assert payload["escalation_target"] == "dispatching_line"
    assert "needs_human" not in payload


# --- dd unit past acceptance: dispatching line, never a human pointer ----------


def test_dd_unit_past_acceptance_points_at_the_dispatching_line_not_a_human() -> None:
    """已过 acceptance 的 dd 单作 subject：指引找派单线自判，不得指向人拍板。"""
    bus = FakeBus(cards=[card("card-dd", 1, title="dd unit", status="awaiting_gate")])
    run = run_arbiter(
        client=bus,
        reasoner=FixedReasoner(
            named_response(
                ESCALATION_DISPATCHING_LINE,
                recommendation="the unit already passed acceptance",
            )
        ),
        publish=True,
    )

    assert len(run.emitted) == 1
    assert run.emitted[0].note_type == "finding"
    assert len(bus.published) == 1
    text = bus.published[0]["payload"]["note"]
    assert text.startswith(NOTE_MARKER)
    assert "escalation_target: dispatching_line" in text
    # 派单线指引：找该单 dispatched_by 自判（D5：闸由派单线判）
    assert "dispatching line" in text
    assert "dispatched_by" in text
    assert "self-judges" in text
    # 不得渲染成『交人/escalate to a human maintainer』拍板指向
    assert "human" not in text.lower()


# --- each target routes and renders its own guidance ---------------------------


@pytest.mark.parametrize(
    ("target", "expected_fragments"),
    [
        (ESCALATION_DISPATCHING_LINE, ["dispatching line", "dispatched_by", "self-judges"]),
        (ESCALATION_SUPERVISOR_ESCALATION, ["supervisor", "must answer"]),
        (ESCALATION_NEEDS_EVIDENCE, ["go back for evidence", "missing:"]),
    ],
)
def test_each_target_renders_its_own_guidance_as_a_finding(
    target: str, expected_fragments: list[str]
) -> None:
    bus = question_bus()
    run = run_arbiter(client=bus, reasoner=FixedReasoner(named_response(target)), publish=True)

    assert len(run.emitted) == 1
    assert run.emitted[0].note_type == "finding"
    assert len(bus.published) == 1
    text = bus.published[0]["payload"]["note"]
    assert f"escalation_target: {target}" in text
    for fragment in expected_fragments:
        assert fragment in text, (target, fragment, text)


def test_needs_evidence_names_the_missing_evidence_in_the_note() -> None:
    bus = question_bus()
    run_arbiter(
        client=bus,
        reasoner=FixedReasoner(
            named_response(
                ESCALATION_NEEDS_EVIDENCE,
                recommendation="nobody can judge: the failing CI log is missing",
            )
        ),
        publish=True,
    )

    assert len(bus.published) == 1
    text = bus.published[0]["payload"]["note"]
    assert "escalation_target: needs_evidence" in text
    assert "missing: nobody can judge: the failing CI log is missing" in text


def test_no_escalation_renders_none_and_stays_a_progress_note() -> None:
    bus = question_bus()
    run = run_arbiter(
        client=bus,
        reasoner=FixedReasoner({"recommendation": "routine progress", "needs_human": False}),
        publish=True,
    )

    assert len(run.emitted) == 1
    assert run.emitted[0].note_type == "progress"
    text = bus.published[0]["payload"]["note"]
    assert "escalation_target: none" in text


# --- legacy needs_human payload back-compat ------------------------------------


@pytest.mark.parametrize(
    ("subject_kind", "expected_target"),
    [
        ("blocked", ESCALATION_DISPATCHING_LINE),
        ("consultation", ESCALATION_SUPERVISOR_ESCALATION),
        ("question", ESCALATION_NEEDS_EVIDENCE),
        ("", ESCALATION_NEEDS_EVIDENCE),
    ],
)
def test_legacy_needs_human_true_routes_to_the_subject_form_default(
    subject_kind: str, expected_target: str
) -> None:
    coerced = coerce_recommendation(
        {"recommendation": "x", "needs_human": True},
        subject_id="s1",
        subject_kind=subject_kind,
    )
    assert coerced.escalation_target == expected_target


def test_legacy_needs_human_false_and_absent_mean_no_escalation() -> None:
    false_case = coerce_recommendation(
        {"recommendation": "x", "needs_human": False}, subject_id="s1"
    )
    absent_case = coerce_recommendation({"recommendation": "x"}, subject_id="s1")
    assert false_case.escalation_target == ""
    assert absent_case.escalation_target == ""


def test_legacy_needs_human_true_payload_still_publishes_a_routed_finding() -> None:
    """旧 payload 不炸旧读者：legacy true 在 blocked 卡上解析并照常发布。"""
    bus = FakeBus(cards=[card("card-dd", 1, title="dd unit", status="blocked")])
    run = run_arbiter(
        client=bus,
        reasoner=FixedReasoner({"recommendation": "needs a look", "needs_human": True}),
        publish=True,
    )

    assert len(run.emitted) == 1
    assert run.emitted[0].note_type == "finding"
    text = bus.published[0]["payload"]["note"]
    assert "escalation_target: dispatching_line" in text
    assert "dispatching line" in text


def test_named_target_wins_over_a_legacy_boolean() -> None:
    coerced = coerce_recommendation(
        {"recommendation": "x", "needs_human": False, "escalation_target": "needs_evidence"},
        subject_id="s1",
    )
    assert coerced.escalation_target == ESCALATION_NEEDS_EVIDENCE


# --- negatives: out-of-vocabulary / empty targets refuse ------------------------


@pytest.mark.parametrize("bad_target", ["summon_a_maintainer", "", "HUMAN_GATE", None, 3])
def test_out_of_vocabulary_or_empty_named_target_refuses(bad_target: Any) -> None:
    with pytest.raises(RecommendationInvalid) as exc:
        coerce_recommendation(
            {"recommendation": "x", "escalation_target": bad_target}, subject_id="q1"
        )
    assert "escalation_target" in str(exc.value)


def test_bad_target_at_tick_is_a_refusal_never_a_publication() -> None:
    bus = question_bus()
    run = run_arbiter(
        client=bus,
        reasoner=FixedReasoner({"recommendation": "x", "escalation_target": "summon_a_maintainer"}),
        publish=True,
    )

    assert run.emitted == []
    assert len(run.refused) == 1
    assert run.refused[0]["subject_id"] == "q1"
    assert "escalation_target" in run.refused[0]["reason"]
    assert bus.published == []


# --- SYSTEM_PROMPT contract -----------------------------------------------------


def test_system_prompt_carries_the_named_target_and_keeps_the_red_line() -> None:
    prompt = TextReasoner.SYSTEM_PROMPT
    assert '"escalation_target"' in prompt
    for target in sorted(ESCALATION_TARGETS):
        assert f'"{target}"' in prompt
    assert "needs_human" not in prompt
    # The decision-vocabulary red line is untouched.
    for word in ("decision", "verdict", "approve", "reject", "gate_release"):
        assert f" {word}" in prompt
