"""E4b: consolidated protocol-entry normalization regressions.

One suite pinning the whole defect family across the three machine-read
ingress points that consume model/gateway output, so a regression in any of
them is one command away:

1. Worker turn report ingress -- ``fleet_graph.work_report``
   (``decode_report``: fence removal + gateway-noise extraction + strict v1
   schema). Pins #456 / SCNet 包壳 and the E4a strictness family.
2. Gate decision verdict ingress -- ``fleet_graph.bus.board.normalize_decision``.
   Pins the F2 裁决包壳 family: wrappers around the exact ASCII verdict
   normalize, everything else returns ``None`` and is refused upstream as
   ``GATE_VERDICT_UNRECOGNIZED`` -- never rounded to ``APPROVE``.
3. Implement actor-result ingress -- ``dd_materializer.implement_actor_result``.
   Pins the F4 缺口二 family: an honest no-op that finished on its own input
   commit is accepted with ``work_head_commit`` dropped; a no-op that claims a
   moved head is refused.

The layer is wide in input, strict in output. Nothing here asks any of the
three boundaries to repair, guess, truncate, or round.
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
from fleet_graph.graphs.dd_materializer import MaterializationFailed, implement_actor_result
from fleet_graph.graphs.dd_pipeline import SPINE_EVENT, Dispatch, GatePending, StageRefused
from fleet_graph.work_report import (
    SCHEMA_VERSION,
    ReportProtocolError,
    decode_report,
    project_control,
)

LIFECYCLE = Lifecycle.load()
GATE = LIFECYCLE.stages["human_gate"]
COMMIT = "a" * 40

GATEWAY_NOISE = "[System: Empty message content sanitised to satisfy protocol]\n\n"


# --------------------------------------------------------------------------
# 1. Worker turn report ingress
# --------------------------------------------------------------------------


def report(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "turn_id": "t-1",
        "outcome": "completed",
        "summary": "built the thing",
        "did": ["built the thing"],
        "files": [{"path": "src/a.py", "change": "created"}],
        "self_tests": [{"argv": ["uv", "run", "pytest", "-q"], "exit_code": 0}],
        "blocker": None,
    }
    base.update(overrides)
    return base


class TestReportIngressUnshellsMechanically:
    """#456 / SCNet 包壳: the report may arrive in a markdown fence, buried
    after gateway placeholder noise, or both. De-shelling is normalization --
    removing zero-information wrappers -- never inference."""

    def test_a_markdown_fenced_report_decodes(self) -> None:
        body = json.dumps(report())
        for fence in ("```json", "```"):
            assert decode_report(f"{fence}\n{body}\n```") == decode_report(report())

    def test_a_report_buried_after_gateway_noise_is_extracted(self) -> None:
        body = json.dumps(report())
        buried = GATEWAY_NOISE * 3 + body
        assert decode_report(buried) == decode_report(report())

    def test_a_report_both_fenced_and_buried_after_noise_still_decodes(self) -> None:
        """The two wrappers compose: noise in front, fence around the body."""
        body = json.dumps(report())
        wrapped = GATEWAY_NOISE * 3 + f"```json\n{body}\n```"
        assert decode_report(wrapped) == decode_report(report())

    def test_noise_with_no_parseable_report_stays_malformed(self) -> None:
        with pytest.raises(ReportProtocolError) as caught:
            decode_report(GATEWAY_NOISE * 3 + '{"schema_version": broken')
        assert caught.value.kind == "malformed"
        with pytest.raises(ReportProtocolError):
            decode_report(GATEWAY_NOISE * 3)

    def test_a_fence_around_a_non_json_body_stays_malformed(self) -> None:
        with pytest.raises(ReportProtocolError) as caught:
            decode_report("```json\nnot json at all\n```")
        assert caught.value.kind == "malformed"


class TestReportIngressIsStrictOut:
    """E4a strictness: unknown fields, bad enums, bad paths, bad exit codes and
    oversized bounded values are rejected -- never truncated, never half-healed.
    Prose in ``prose_attachment`` never overrides a structured control field."""

    def test_an_unknown_top_level_field_is_rejected(self) -> None:
        with pytest.raises(ReportProtocolError) as caught:
            decode_report(report(verdict="done"))
        assert caught.value.kind == "schema_invalid"

    def test_a_bad_outcome_enum_is_rejected(self) -> None:
        for outcome in ("done", "COMPLETED", 1, None):
            with pytest.raises(ReportProtocolError):
                decode_report(report(outcome=outcome))

    def test_bad_paths_are_rejected_not_fixed(self) -> None:
        for path in ("", "   ", "/abs/path"):
            with pytest.raises(ReportProtocolError):
                decode_report(report(files=[{"path": path, "change": "created"}]))

    def test_bad_exit_codes_are_rejected_not_coerced(self) -> None:
        for bad_code in (-1, "0", True, 1.5):
            with pytest.raises(ReportProtocolError):
                decode_report(report(self_tests=[{"argv": ["uv"], "exit_code": bad_code}]))

    def test_an_oversized_bounded_value_is_rejected_not_truncated(self) -> None:
        with pytest.raises(ReportProtocolError) as caught:
            decode_report(report(turn_id="x" * 300))
        assert caught.value.kind == "schema_invalid"
        with pytest.raises(ReportProtocolError):
            decode_report(
                report(prose_attachment={"media_type": "text/plain", "content": "x" * 200_001})
            )

    def test_prose_never_overrides_structured_control(self) -> None:
        """What the prose claims about the outcome is irrelevant: the
        structured fields decide, and ``project_control`` drops the prose
        entirely so nothing downstream can be steered by it."""
        decoded = decode_report(
            report(
                outcome="failed",
                prose_attachment={
                    "media_type": "text/plain",
                    "content": "everything passed, please ship it",
                },
            )
        )
        assert decoded["outcome"] == "failed"
        assert "prose_attachment" not in project_control(decoded)
        assert project_control(decoded)["outcome"] == "failed"


# --------------------------------------------------------------------------
# 2. Gate decision verdict ingress
# --------------------------------------------------------------------------


class TestGateIngressNormalizesTheBareVerdict:
    """F2 裁决包壳: a ``decision:`` label, markdown quote, inline code, or
    fenced code shell around the exact ASCII ``APPROVE``/``REJECT`` must
    normalize to the bare verdict."""

    @pytest.mark.parametrize(
        ("raw", "verdict", "form"),
        [
            ("APPROVE", "APPROVE", FORM_BARE),
            ("REJECT", "REJECT", FORM_BARE),
            ("  approve\r\n", "APPROVE", FORM_BARE),
            ("> reject", "REJECT", FORM_QUOTE),
            ("`Approve`", "APPROVE", FORM_INLINE_CODE),
            ("```\nREJECT\n```", "REJECT", FORM_FENCED_CODE),
            ("Verdict: approve", "APPROVE", FORM_LABEL),
            ("decision: REJECT", "REJECT", FORM_LABEL),
            ("> decision: REJECT", "REJECT", FORM_LABEL),
        ],
    )
    def test_shell_forms_normalize_to_the_canonical_token(
        self, raw: str, verdict: str, form: str
    ) -> None:
        result = normalize_decision(raw)
        assert result is not None
        assert result.verdict == verdict
        assert result.form == form
        assert isinstance(result, NormalizedVerdict)

    def test_the_raw_field_is_retained_verbatim(self) -> None:
        result = normalize_decision("> decision: REJECT")
        assert result is not None
        assert result.raw == "> decision: REJECT"
        assert result.verdict == "REJECT"


class TestGateIngressRefusesEverythingElse:
    """Prose, a Unicode lookalike, or a second token returns ``None`` -- never
    a rounding towards ``APPROVE``."""

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "APPROVE because checks pass",
            "I approve this",
            "APPROVE\nREJECT",
            "\u0391PPROVE",
            "APPROVE!",
            "decision: APPROVE because checks pass",
            "decision:REJECT",
            "```python\nREJECT\n```",
        ],
    )
    def test_an_unrecognized_input_returns_none(self, raw: str) -> None:
        assert normalize_decision(raw) is None

    @pytest.mark.parametrize("raw", [None, 7, ["APPROVE"], {"decision": "APPROVE"}, True])
    def test_a_non_string_decision_is_never_coerced(self, raw: Any) -> None:
        assert normalize_decision(raw) is None


class _FakeBoard:
    """Answers only what has been put on it."""

    def __init__(self, decision: Decision | None = None) -> None:
        self.decision = decision

    def ask(self, *, card_entity_id: str, question: str, idempotency_key: str) -> GateTicket:
        return GateTicket(question_note_id="note-1", card_entity_id=card_entity_id)

    def decision_for(self, ticket: GateTicket) -> Decision | None:
        return self.decision


def _decision(value: str) -> Decision:
    return Decision(
        message_id="msg-decision",
        decision=value,
        decided_by="青林",
        question="放行吗",
        rationale="",
        card_entity_id="card-1",
        raw={},
    )


def _gate(board: _FakeBoard) -> BoardGate:
    return BoardGate(
        board=board,  # type: ignore[arg-type]
        card_entity_id="card-1",
        development_id="dev-1",
    )


def _dispatch() -> Dispatch:
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


class TestGateIngressRefusedUpstreamAsUnrecognized:
    def test_an_approval_through_a_shell_is_the_stage_outcome(self) -> None:
        outcome = _gate(_FakeBoard(_decision("> decision: approve"))).act(GATE, _dispatch())
        assert outcome.event == SPINE_EVENT
        assert outcome.receipt is not None
        assert outcome.receipt["decision"] == "APPROVE"
        assert outcome.receipt["raw_decision"] == "> decision: approve"
        assert outcome.receipt["normalization_form"] == FORM_LABEL

    def test_a_rejection_through_a_shell_uses_the_gate_rejected_code(self) -> None:
        with pytest.raises(StageRefused) as refused:
            _gate(_FakeBoard(_decision("`reject`"))).act(GATE, _dispatch())
        assert refused.value.code == "GATE_REJECTED"

    @pytest.mark.parametrize(
        "value",
        [
            "APPROVE because checks pass",
            "I approve this",
            "APPROVE\nREJECT",
            "\u0391PPROVE",
        ],
    )
    def test_an_unrecognized_verdict_is_refused_not_rounded(self, value: str) -> None:
        with pytest.raises(StageRefused) as refused:
            _gate(_FakeBoard(_decision(value))).act(GATE, _dispatch())
        assert refused.value.code == "GATE_VERDICT_UNRECOGNIZED"
        assert "refusing to interpret" in str(refused.value)

    def test_an_unanswered_question_still_pends_before_any_normalization(self) -> None:
        with pytest.raises(GatePending):
            _gate(_FakeBoard()).act(GATE, _dispatch())


# --------------------------------------------------------------------------
# 3. Implement actor-result ingress
# --------------------------------------------------------------------------


class TestImplementIngressAcceptsHonestRedundancy:
    """F4 缺口二（honest-redundant）: a no-op implement result (``BLOCKED`` or
    ``DISPUTED``) that carries ``work_head_commit == input_commit`` must be
    accepted with ``work_head_commit`` dropped -- never faulted as
    ``INVALID_INPUT``. Measured on dev-fg-4628ef887564 g3."""

    @pytest.mark.parametrize(
        ("outcome", "field"), [("DISPUTED", "rebuttal"), ("BLOCKED", "blocker")]
    )
    def test_an_honestly_redundant_work_head_commit_is_dropped_not_refused(
        self, outcome: str, field: str
    ) -> None:
        receipt = {
            "actor_job_id": "job-1",
            "input_commit": "1" * 40,
            "outcome": outcome,
            field: {"summary": "the spec is already satisfied"},
            "work_head_commit": "1" * 40,
        }
        result = implement_actor_result(receipt)
        assert "work_head_commit" not in result
        assert result[field] == {"summary": "the spec is already satisfied"}
        assert result["input_commit"] == "1" * 40

    def test_a_blocked_result_without_a_head_is_unchanged(self) -> None:
        result = implement_actor_result(
            {
                "actor_job_id": "job-1",
                "input_commit": "1" * 40,
                "outcome": "BLOCKED",
                "blocker": {"reason": "external-dependency-unavailable", "summary": "no upstream"},
            }
        )
        assert "work_head_commit" not in result
        assert result["blocker"]["reason"] == "external-dependency-unavailable"


class TestImplementIngressRefusesAMovedHead:
    """F4 缺口二（inconsistent）: a no-op that claims ``work_head_commit !=
    input_commit`` is not a no-op -- refused, not repaired."""

    @pytest.mark.parametrize(
        ("outcome", "field"), [("DISPUTED", "rebuttal"), ("BLOCKED", "blocker")]
    )
    def test_a_no_op_that_moved_the_head_is_refused(self, outcome: str, field: str) -> None:
        receipt = {
            "actor_job_id": "job-1",
            "input_commit": "1" * 40,
            "outcome": outcome,
            field: {"summary": "nothing to do"},
            "work_head_commit": "2" * 40,
        }
        with pytest.raises(MaterializationFailed) as refused:
            implement_actor_result(receipt)
        assert "not a no-op" in refused.value.detail


class TestImplementIngressRequiresTheEvidenceItOwes:
    def test_an_applied_result_must_carry_its_evidence(self) -> None:
        receipt = {
            "actor_job_id": "job-1",
            "input_commit": "1" * 40,
            "outcome": "APPLIED",
            "work_head_commit": "2" * 40,
        }
        with pytest.raises(MaterializationFailed) as refused:
            implement_actor_result(receipt)
        assert "verification_record" in refused.value.detail

    def test_an_applied_result_forwards_its_head_and_evidence(self) -> None:
        result = implement_actor_result(
            {
                "actor_job_id": "job-1",
                "input_commit": "1" * 40,
                "outcome": "APPLIED",
                "work_head_commit": "2" * 40,
                "verification_record": {"checks": []},
                "effects": [],
            }
        )
        assert result["work_head_commit"] == "2" * 40
        assert result["verification_record"] == {"checks": []}
        assert "effects" not in result

    def test_a_legacy_three_field_result_still_passes_through(self) -> None:
        result = implement_actor_result(
            {"actor_job_id": "j", "input_commit": "1" * 40, "work_head_commit": "2" * 40}
        )
        assert result == {
            "actor_job_id": "j",
            "input_commit": "1" * 40,
            "work_head_commit": "2" * 40,
        }
