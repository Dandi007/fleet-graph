"""M3 line self-gate: six evidence obligations -> one self-delivered verdict.

The spec (wf-8d9737 M3) makes the dispatching line its own gate: on a
``dd_awaiting_gate`` wake the line discharges six evidence obligations and
delivers ``APPROVE``/``REJECT`` through the same dd delivery path the decision
surface already checks (principal == dispatched_by). This file pins the
non-negotiable field of each obligation, the negative criteria (missing
obligation -> delivery refused; green->red regression refused; foreign dd
delivery refused; workspace-missing / resume-not-consumed refused with a trace),
and the S12 mutation-enumeration binding -- including the instance from the
previous disposition's return: deleting the ``deliver_self_gate_decision`` call
in ``runner.py`` must leave the frozen acceptance without coverage.

Everything here runs against pure data or a duck-typed dd control plane; no live
dd root, no live git repo, no live pytest run is required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fleet_graph.dd.self_gate import (
    CODE_SELF_GATE_EVIDENCE_INCOMPLETE,
    EVIDENCE_MUTATION_RECEIPT,
    EVIDENCE_REGRESSION,
    REQUIRED_EVIDENCE,
    EvidenceItem,
    RegressionBaseline,
    collect_evidence,
    deliver_self_gate_decision,
    enumerate_mutation_targets,
    evidence_acceptance_frozen,
    evidence_diff_within_scope,
    evidence_personally_rerun,
    evidence_regression,
    evidence_zero_test_deletion,
    render_rationale,
    verify_mutation_receipt,
)
from fleet_graph.decision_mcp import (
    CODE_GATE_NOT_CONSUMED,
    CODE_NOT_DISPATCHING_LINE,
    CODE_WORKSPACE_MISSING,
    DECISION_APPROVE,
    DECISION_REJECT,
    OUTCOME_DELIVERED,
    OUTCOME_REFUSED,
    deliver_decision,
)

DD_ID = "dev-fg-abc"
PRINCIPAL = "wf-8d9737"
FROZEN = [
    ["bash", "-lc", "uv sync --frozen && uv run pytest -q tests/test_m3_line_selfgate.py"],
    ["bash", "-lc", "make verify"],
]


def _all_pass() -> list[EvidenceItem]:
    return [
        evidence_acceptance_frozen(
            spec_argv=FROZEN, record_acceptance_commands=FROZEN, receipt_command=FROZEN
        ),
        evidence_diff_within_scope(
            changed_product_paths=["src/fleet_graph/dd/self_gate.py"],
            spec_deliverable_prefixes=["src/fleet_graph/", "tests/"],
        ),
        evidence_zero_test_deletion(deleted_paths=[]),
        evidence_personally_rerun(
            rerun_command=FROZEN[0], frozen_command=FROZEN[0], rerun_echo="ok", rerun_exit_code=0
        ),
        EvidenceItem(
            EVIDENCE_MUTATION_RECEIPT,
            "mutation receipt verified",
            True,
            "receipt == enumeration (1 targets, all red)",
        ),
        evidence_regression(
            baseline=RegressionBaseline(frozenset(["test_a_red"]), failed_count=1),
            patched_failed={"test_a_red"},
            target_base_commit="base" * 10,
            comparison_base_commit="base" * 10,
        ),
    ]


class FakeDd:
    """A duck-typed dd control plane: ``get`` + ``gate`` + ``record_gate_refusal``."""

    def __init__(
        self,
        *,
        state: str = "awaiting_gate",
        dispatched_by: str = PRINCIPAL,
        generation: int = 2,
        worktree_path: str = "",
        consume_on_resume: bool = True,
        resume_exit_code: str = "",
    ) -> None:
        self.state = state
        self.dispatched_by = dispatched_by
        self.generation = generation
        self.worktree_path = worktree_path
        self.consume_on_resume = consume_on_resume
        self.resume_exit_code = resume_exit_code
        self.resumed: list[tuple[str, str]] = []
        self.refusals: list[dict[str, Any]] = []

    def get(self, development_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "development_id": development_id,
            "state": self.state,
            "dispatched_by": self.dispatched_by,
            "generation": self.generation,
            "awaiting": {"question_note_id": "q-dd-1", "card_entity_id": "card-dd-1"},
        }
        if self.worktree_path:
            payload["worktree_path"] = self.worktree_path
        return payload

    def gate(
        self, development_id: str, resume: bool = False, action_key: str | None = None
    ) -> dict[str, Any]:
        assert resume is True
        self.resumed.append((development_id, action_key or ""))
        if self.consume_on_resume and self.state == "awaiting_gate":
            self.state = "running"
        resume_payload: dict[str, Any] = {
            "development_id": development_id,
            "generation": self.generation,
        }
        if self.resume_exit_code:
            resume_payload["exit_code"] = self.resume_exit_code
        return {"state": self.state, "development_id": development_id, "resume": resume_payload}

    def record_gate_refusal(
        self, development_id: str, *, code: str, reason: str, exit_code: str = ""
    ) -> None:
        self.refusals.append(
            {
                "development_id": development_id,
                "code": code,
                "reason": reason,
                "exit_code": exit_code,
            }
        )


class TestObligationAcceptanceFrozen:
    def test_three_way_equality_passes(self) -> None:
        item = evidence_acceptance_frozen(
            spec_argv=FROZEN, record_acceptance_commands=FROZEN, receipt_command=FROZEN
        )
        assert item.passed is True

    def test_a_divergent_record_is_red(self) -> None:
        item = evidence_acceptance_frozen(
            spec_argv=FROZEN,
            record_acceptance_commands=[["make", "verify"]],
            receipt_command=FROZEN,
        )
        assert item.passed is False


class TestObligationDiffWithinScope:
    def test_product_changes_inside_scope_pass(self) -> None:
        item = evidence_diff_within_scope(
            changed_product_paths=["src/fleet_graph/dd/self_gate.py"],
            spec_deliverable_prefixes=["src/fleet_graph/"],
        )
        assert item.passed is True

    def test_machine_files_are_exempt(self) -> None:
        item = evidence_diff_within_scope(
            changed_product_paths=[
                ".dev-dispatch/spec/approved.md",
                ".dd-evidence/acceptance.json",
            ],
            spec_deliverable_prefixes=["src/fleet_graph/"],
        )
        assert item.passed is True

    def test_an_out_of_scope_product_change_is_red(self) -> None:
        item = evidence_diff_within_scope(
            changed_product_paths=["src/fleet_graph/research_anchor.py"],
            spec_deliverable_prefixes=["src/fleet_graph/dd/"],
        )
        assert item.passed is False


class TestObligationZeroTestDeletion:
    def test_no_deleted_tests_pass(self) -> None:
        assert evidence_zero_test_deletion(deleted_paths=[]).passed is True

    def test_a_deleted_test_file_is_red(self) -> None:
        item = evidence_zero_test_deletion(deleted_paths=["tests/test_old.py"])
        assert item.passed is False

    def test_an_updated_test_is_not_a_deletion(self) -> None:
        # --diff-filter=D lists only deletions; an edit is not in this set.
        assert (
            evidence_zero_test_deletion(deleted_paths=["src/fleet_graph/dd/self_gate.py"]).passed
            is True
        )


class TestObligationPersonallyRerun:
    def test_reran_frozen_command_with_echo_and_exit_0_passes(self) -> None:
        item = evidence_personally_rerun(
            rerun_command=FROZEN[0], frozen_command=FROZEN[0], rerun_echo="echo", rerun_exit_code=0
        )
        assert item.passed is True

    def test_a_divergent_command_or_failed_run_is_red(self) -> None:
        assert (
            evidence_personally_rerun(
                rerun_command=["make", "verify"],
                frozen_command=FROZEN[0],
                rerun_echo="echo",
                rerun_exit_code=0,
            ).passed
            is False
        )
        assert (
            evidence_personally_rerun(
                rerun_command=FROZEN[0],
                frozen_command=FROZEN[0],
                rerun_echo="echo",
                rerun_exit_code=1,
            ).passed
            is False
        )


class TestMutationEnumerationAndReceipt:
    def test_enumeration_extracts_added_production_call_sites(self) -> None:
        added = {
            "src/fleet_graph/graphs/runner.py": [
                (123, "    result = deliver_self_gate_decision("),
            ],
            "src/fleet_graph/dd/self_gate.py": [
                (10, "from fleet_graph.decision_mcp import deliver_decision")
            ],
        }
        targets = enumerate_mutation_targets(added)
        assert [t.call for t in targets] == ["deliver_self_gate_decision"]

    def test_enumeration_ignores_imports_defs_and_test_files(self) -> None:
        added = {
            "src/fleet_graph/dd/self_gate.py": [
                (1, "import deliver_self_gate_decision"),
                (2, "def deliver_self_gate_decision():"),
            ],
            "tests/test_x.py": [(3, "    foo()")],
        }
        assert enumerate_mutation_targets(added) == []

    def test_receipt_equal_to_enumeration_all_red_passes(self) -> None:
        enumerated = enumerate_mutation_targets(
            {"src/fleet_graph/graphs/runner.py": [(7, "    result = deliver_self_gate_decision(")]}
        )
        receipt = [
            {"file": t.file, "line": t.line, "call": t.call, "red": True} for t in enumerated
        ]
        assert (
            verify_mutation_receipt(enumerated=enumerated, receipt_targets=receipt).passed is True
        )

    def test_receipt_mismatch_or_not_red_is_refused(self) -> None:
        enumerated = enumerate_mutation_targets(
            {"src/fleet_graph/graphs/runner.py": [(7, "    result = deliver_self_gate_decision(")]}
        )
        missing = []
        assert (
            verify_mutation_receipt(enumerated=enumerated, receipt_targets=missing).passed is False
        )
        not_red = [
            {"file": t.file, "line": t.line, "call": t.call, "red": False} for t in enumerated
        ]
        item = verify_mutation_receipt(enumerated=enumerated, receipt_targets=not_red)
        assert item.passed is False
        assert "land red" in item.detail


class TestObligationRegression:
    def test_red_set_not_expanded_passes_even_when_baseline_is_red(self) -> None:
        baseline = RegressionBaseline(frozenset(["test_a", "test_b"]), failed_count=2)
        item = evidence_regression(
            baseline=baseline,
            patched_failed={"test_a", "test_b"},
            target_base_commit="base" * 10,
            comparison_base_commit="base" * 10,
        )
        assert item.passed is True

    def test_green_to_red_flip_is_refused(self) -> None:
        baseline = RegressionBaseline(frozenset(["test_a"]), failed_count=1)
        item = evidence_regression(
            baseline=baseline,
            patched_failed={"test_a", "test_newly_red"},
            target_base_commit="base" * 10,
            comparison_base_commit="base" * 10,
        )
        assert item.passed is False
        assert "test_newly_red" in item.detail

    def test_red_to_green_is_an_admitted_improvement(self) -> None:
        baseline = RegressionBaseline(frozenset(["test_a", "test_b"]), failed_count=2)
        item = evidence_regression(
            baseline=baseline,
            patched_failed={"test_b"},
            target_base_commit="base" * 10,
            comparison_base_commit="base" * 10,
        )
        assert item.passed is True

    def test_a_drifted_main_head_as_baseline_is_refused(self) -> None:
        baseline = RegressionBaseline(frozenset())
        item = evidence_regression(
            baseline=baseline,
            patched_failed=set(),
            target_base_commit="base" * 10,
            comparison_base_commit="main" * 10,
        )
        assert item.passed is False
        assert "drifted" in item.detail

    def test_a_known_flake_with_rerun_attribution_is_admitted(self) -> None:
        flaky = "tests/test_x.py::test_flaky"
        baseline = RegressionBaseline(frozenset())
        item = evidence_regression(
            baseline=baseline,
            patched_failed={flaky},
            target_base_commit="base" * 10,
            comparison_base_commit="base" * 10,
            flake_attributions={flaky: "isolated rerun on clean base passed 3/4"},
        )
        assert item.passed is True


class TestSelfGateDelivery:
    def test_all_six_pass_delivers_approve_as_principal(self, tmp_path: Path) -> None:
        dd = FakeDd()
        result = deliver_self_gate_decision(
            development_id=DD_ID,
            principal=PRINCIPAL,
            evidence=_all_pass(),
            run_root=tmp_path,
            dd=dd,
        )
        assert result.status == OUTCOME_DELIVERED
        assert result.decision == DECISION_APPROVE
        assert dd.resumed == [(DD_ID, f"mcp:dd:{DD_ID}:g2:APPROVE")]

    def test_a_failed_obligation_delivers_reject_not_a_swallow(self, tmp_path: Path) -> None:
        evidence = _all_pass()
        evidence[5] = evidence_regression(
            baseline=RegressionBaseline(frozenset()),
            patched_failed={"test_newly_red"},
            target_base_commit="base" * 10,
            comparison_base_commit="base" * 10,
        )
        dd = FakeDd()
        result = deliver_self_gate_decision(
            development_id=DD_ID,
            principal=PRINCIPAL,
            evidence=evidence,
            run_root=tmp_path,
            dd=dd,
        )
        assert result.status == OUTCOME_DELIVERED
        assert result.decision == DECISION_REJECT

    def test_missing_an_obligation_leaves_delivery_refused(self, tmp_path: Path) -> None:
        evidence = _all_pass()
        dropped = [e for e in evidence if e.id != EVIDENCE_REGRESSION]
        dd = FakeDd()
        result = deliver_self_gate_decision(
            development_id=DD_ID,
            principal=PRINCIPAL,
            evidence=dropped,
            run_root=tmp_path,
            dd=dd,
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_SELF_GATE_EVIDENCE_INCOMPLETE
        assert dd.resumed == []

    def test_a_foreign_principal_cannot_self_gate_this_single(self, tmp_path: Path) -> None:
        dd = FakeDd(dispatched_by="wf-other-line")
        result = deliver_self_gate_decision(
            development_id=DD_ID,
            principal=PRINCIPAL,
            evidence=_all_pass(),
            run_root=tmp_path,
            dd=dd,
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_NOT_DISPATCHING_LINE
        assert dd.resumed == []

    def test_the_six_required_obligations_are_exactly_named(self) -> None:
        assert len(REQUIRED_EVIDENCE) == 6
        assert collect_evidence(_all_pass()) is None
        assert render_rationale(_all_pass()).count("=") >= 6


class TestS10DeliveryMustLand:
    def test_resume_that_was_not_consumed_is_refused_with_the_unit_exit_code(
        self, tmp_path: Path
    ) -> None:
        dd = FakeDd(consume_on_resume=False, resume_exit_code="75/TEMPFAIL")
        result = deliver_decision(
            line=DD_ID,
            decision=DECISION_APPROVE,
            reason="live",
            principal=PRINCIPAL,
            run_root=tmp_path,
            lines=[],
            dd=dd,
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_GATE_NOT_CONSUMED
        assert "75/TEMPFAIL" in result.message
        assert dd.refusals
        assert dd.refusals[0]["code"] == CODE_GATE_NOT_CONSUMED
        assert dd.refusals[0]["exit_code"] == "75/TEMPFAIL"

    def test_missing_workspace_is_refused_before_any_unit_starts(self, tmp_path: Path) -> None:
        dd = FakeDd(worktree_path="/nonexistent/workspace/path")
        result = deliver_decision(
            line=DD_ID,
            decision=DECISION_APPROVE,
            reason="live",
            principal=PRINCIPAL,
            run_root=tmp_path,
            lines=[],
            dd=dd,
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_WORKSPACE_MISSING
        assert dd.resumed == []
        assert dd.refusals
        assert dd.refusals[0]["code"] == CODE_WORKSPACE_MISSING

    def test_recording_refusal_is_best_effort_so_a_fake_without_it_still_refuses(
        self, tmp_path: Path
    ) -> None:
        dd = FakeDd(consume_on_resume=False, resume_exit_code="75/TEMPFAIL")

        dd.record_gate_refusal = None  # type: ignore[method-assign]

        result = deliver_decision(
            line=DD_ID,
            decision=DECISION_APPROVE,
            reason="live",
            principal=PRINCIPAL,
            run_root=tmp_path,
            lines=[],
            dd=dd,
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_GATE_NOT_CONSUMED


class TestS11ForeignDeliveryRefused:
    def test_form_a_with_a_non_dispatching_principal_is_refused(self) -> None:
        from fleet_graph.decision_bridge.owners import OWNER_KIND_DD, OwnerTarget
        from fleet_graph.decision_mcp import deliver_decision_dd

        class Source:
            def __init__(self) -> None:
                self.target = OwnerTarget(
                    kind=OWNER_KIND_DD,
                    id=DD_ID,
                    generation=1,
                    question_note_id="q-1",
                    card_entity_id="card-1",
                    state="awaiting_gate",
                    dispatched_by=PRINCIPAL,
                )

            def discover_all(self) -> list[OwnerTarget]:
                return [self.target]

            def _control_plane(self) -> Any:
                from fleet_graph.dd.control_plane import ControlPlaneError

                class P:
                    def get(self, development_id: str) -> dict[str, Any]:
                        raise ControlPlaneError("DEVELOPMENT_NOT_FOUND", "nope")

                return P()

        result = deliver_decision_dd(
            target_id=DD_ID,
            decision=DECISION_APPROVE,
            reason="live",
            dd_source=Source(),
            principal="wf-foreign",
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_NOT_DISPATCHING_LINE

    def test_form_a_with_the_dispatching_principal_is_authorized(self) -> None:
        from fleet_graph.decision_bridge.owners import OWNER_KIND_DD, RESUME_RESUMED, OwnerTarget
        from fleet_graph.decision_mcp import deliver_decision_dd

        class Source:
            def __init__(self) -> None:
                self.target = OwnerTarget(
                    kind=OWNER_KIND_DD,
                    id=DD_ID,
                    generation=1,
                    question_note_id="q-1",
                    card_entity_id="card-1",
                    state="awaiting_gate",
                    dispatched_by=PRINCIPAL,
                )
                self.resumed: list[Any] = []

            def discover_all(self) -> list[OwnerTarget]:
                return [self.target]

            def _control_plane(self) -> Any:
                from fleet_graph.dd.control_plane import ControlPlaneError

                class P:
                    def get(self, development_id: str) -> dict[str, Any]:
                        raise ControlPlaneError("DEVELOPMENT_NOT_FOUND", "nope")

                return P()

            def resume(self, target: OwnerTarget, action_key: str) -> Any:
                from fleet_graph.decision_bridge.owners import OwnerResult

                self.resumed.append((target.id, action_key))
                return OwnerResult(RESUME_RESUMED, "resumed")

        result = deliver_decision_dd(
            target_id=DD_ID,
            decision=DECISION_APPROVE,
            reason="live",
            dd_source=Source(),
            principal=PRINCIPAL,
        )
        assert result.status == OUTCOME_DELIVERED


class TestS12RunnerCallSiteCoverage:
    def test_the_runner_self_gate_call_site_is_a_production_call(self, tmp_path: Path) -> None:
        """The S12 instance: runner.py's ``deliver_self_gate_decision`` call is a
        real production call that the mechanically-enumerated mutation targets see
        (see ``TestMutationEnumerationAndReceipt``), and it is *covered* by the
        frozen acceptance: driving ``run_self_gate`` reaches the call site."""

        added = {
            "src/fleet_graph/graphs/runner.py": [
                (1, "    result = deliver_self_gate_decision("),
            ]
        }
        calls = [t.call for t in enumerate_mutation_targets(added)]
        assert "deliver_self_gate_decision" in calls


def test_runner_self_gate_is_covered_by_the_frozen_acceptance(monkeypatch, tmp_path: Path) -> None:
    import fleet_graph.graphs.runner as runner

    calls: list[dict[str, Any]] = []

    def fake_deliver(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(runner, "deliver_self_gate_decision", fake_deliver)
    config = runner.LineConfig(
        folder_id=PRINCIPAL,
        seat="s",
        run_root=tmp_path,
        dd_awaiting_gate_development_id=DD_ID,
    )
    assert runner.run_self_gate(config, dd=object()) is None
    assert calls
    assert calls[0]["development_id"] == DD_ID
    assert calls[0]["principal"] == PRINCIPAL


def test_an_ordinary_run_is_not_a_self_gate_wake(tmp_path: Path) -> None:
    import fleet_graph.graphs.runner as runner

    config = runner.LineConfig(folder_id=PRINCIPAL, seat="s", run_root=tmp_path)
    assert runner.run_self_gate(config) is None
