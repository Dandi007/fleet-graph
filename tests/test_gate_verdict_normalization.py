"""E4b: gate verdict input normalization.

The gate must be wide in input and strict in output: a human may wrap the two
verdicts in a small, documented set of transport/Markdown shells, and the
normalizer removes *only* those shells to reach the exact bytes ``APPROVE`` or
``REJECT``. Everything else fails closed as ``GATE_VERDICT_UNRECOGNIZED``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from fleet_graph.bus.board import (
    FORM_BARE,
    FORM_FENCED_CODE,
    FORM_INLINE_CODE,
    FORM_LABEL,
    FORM_QUOTE,
    Decision,
    GateTicket,
    NormalizedVerdict,
    normalize_decision,
)
from fleet_graph.dd.lifecycle import Lifecycle
from fleet_graph.graphs.dd_actors import BoardGate
from fleet_graph.graphs.dd_pipeline import SPINE_EVENT, Dispatch, GatePending, StageRefused

LIFECYCLE = Lifecycle.load()
GATE = LIFECYCLE.stages["human_gate"]
COMMIT = "a" * 40


class FakeBoard:
    """Answers only what has been put on it."""

    def __init__(self, decision: Decision | None = None) -> None:
        self.decision = decision
        self.asked: list[dict[str, str]] = []

    def ask(self, *, card_entity_id: str, question: str, idempotency_key: str) -> GateTicket:
        self.asked.append(
            {
                "card_entity_id": card_entity_id,
                "question": question,
                "idempotency_key": idempotency_key,
            }
        )
        return GateTicket(question_note_id="note-1", card_entity_id=card_entity_id)

    def decision_for(self, ticket: GateTicket) -> Decision | None:
        return self.decision


def a_decision(value: str, *, by: str = "青林") -> Decision:
    return Decision(
        message_id="msg-decision",
        decision=value,
        decided_by=by,
        question="放行吗",
        rationale="",
        card_entity_id="card-1",
        raw={},
    )


def a_dispatch() -> Dispatch:
    return {
        "development_id": "dev-1",
        "stage": GATE.id,
        "mode": "initial",
        "generation": 1,
        "attempt": 1,
        "input_commit": COMMIT,
        "required_artifacts": list(GATE.required_artifacts),
        "produced_artifacts": list(GATE.produced_artifacts),
        "contract_version": LIFECYCLE.contract_version,
    }


def make_gate(board: FakeBoard) -> BoardGate:
    return BoardGate(
        board=board,  # type: ignore[arg-type]
        card_entity_id="card-1",
        development_id="dev-1",
    )


class TestNormalizerAccepts:
    @pytest.mark.parametrize(
        ("raw", "verdict", "form"),
        [
            ("APPROVE", "APPROVE", FORM_BARE),
            ("REJECT", "REJECT", FORM_BARE),
            ("  approve\r\n", "APPROVE", FORM_BARE),
            ("reject", "REJECT", FORM_BARE),
            ("\tApprove\n", "APPROVE", FORM_BARE),
            ("> reject", "REJECT", FORM_QUOTE),
            ("> APPROVE", "APPROVE", FORM_QUOTE),
            ("`Approve`", "APPROVE", FORM_INLINE_CODE),
            ("`REJECT`", "REJECT", FORM_INLINE_CODE),
            ("```\nREJECT\n```", "REJECT", FORM_FENCED_CODE),
            ("```\nAPPROVE\n```", "APPROVE", FORM_FENCED_CODE),
            ("Verdict: approve", "APPROVE", FORM_LABEL),
            ("decision: REJECT", "REJECT", FORM_LABEL),
            ("Decision: approve", "APPROVE", FORM_LABEL),
            ("> decision: REJECT", "REJECT", FORM_LABEL),
            ("> `APPRove`", "APPROVE", FORM_INLINE_CODE),
        ],
    )
    def test_normalizes_to_the_canonical_token(self, raw: str, verdict: str, form: str) -> None:
        result = normalize_decision(raw)
        assert result is not None
        assert result.verdict == verdict
        assert result.form == form

    def test_the_raw_field_is_retained_verbatim(self) -> None:
        result = normalize_decision("> decision: REJECT")
        assert result is not None
        assert result.raw == "> decision: REJECT"
        assert result.verdict == "REJECT"

    def test_returns_a_typed_result(self) -> None:
        result = normalize_decision("APPROVE")
        assert isinstance(result, NormalizedVerdict)


class TestNormalizerRefuses:
    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "APPROVE because checks pass",
            "I approve this",
            "APPROVE\nREJECT",
            "> APPROVE\nreason",  # mixed quote
            "```\nAPPROVE\nreason\n```",  # fenced prose
            "```python\nREJECT\n```",  # info string is not allowed
            "APPROVE!",
            "\u0391PPROVE",  # Unicode lookalike (Greek capital alpha)
            "APPROVE extra",
            "decision:REJECT",  # label needs whitespace
            "`APP`ROVE`",  # interior backticks
            "decision: APPROVE because checks pass",
        ],
    )
    def test_refuses_every_input_outside_the_grammar(self, raw: str) -> None:
        assert normalize_decision(raw) is None

    @pytest.mark.parametrize("raw", [None, 7, ["APPROVE"], {"decision": "APPROVE"}, True])
    def test_refuses_non_string_decisions_instead_of_coercing(self, raw: Any) -> None:
        assert normalize_decision(raw) is None


class TestBoardGateConsumesTheNormalizer:
    def test_an_approval_through_a_shell_is_the_stage_outcome(self) -> None:
        board = FakeBoard(a_decision("> decision: approve"))
        outcome = make_gate(board).act(GATE, a_dispatch())

        assert outcome.event == SPINE_EVENT
        assert outcome.receipt is not None
        assert outcome.receipt["decision"] == "APPROVE"
        assert outcome.receipt["raw_decision"] == "> decision: approve"
        assert outcome.receipt["normalization_form"] == FORM_LABEL

    def test_a_rejection_through_a_shell_uses_the_gate_rejected_code(self) -> None:
        board = FakeBoard(a_decision("`reject`"))
        with pytest.raises(StageRefused) as refused:
            make_gate(board).act(GATE, a_dispatch())

        assert refused.value.code == "GATE_REJECTED"

    def test_an_unrecognized_verdict_is_refused_with_its_own_code(self) -> None:
        board = FakeBoard(a_decision("APPROVE because checks pass"))
        with pytest.raises(StageRefused) as refused:
            make_gate(board).act(GATE, a_dispatch())

        assert refused.value.code == "GATE_VERDICT_UNRECOGNIZED"

    def test_an_unanswered_question_still_pends_before_any_normalization(self) -> None:
        board = FakeBoard()
        with pytest.raises(GatePending):
            make_gate(board).act(GATE, a_dispatch())

    def test_the_canonical_decision_is_written_into_the_product_tree(self, tmp_path) -> None:
        from fleet_graph.graphs.dd_scripts import GATE_PATH

        board = FakeBoard(a_decision("Verdict: approve"))
        gate = BoardGate(
            board=board,  # type: ignore[arg-type]
            card_entity_id="card-1",
            development_id="dev-1",
            repo=tmp_path,
        )
        gate.act(GATE, a_dispatch())

        sealed = json.loads((tmp_path / GATE_PATH.format(generation=1)).read_text(encoding="utf-8"))
        assert sealed["decision"] == "APPROVE"
        assert sealed["raw_decision"] == "Verdict: approve"
        assert sealed["normalization_form"] == FORM_LABEL
