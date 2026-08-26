"""The stage machine, and the bindings that keep the chain honest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet_graph.dd.lifecycle import (
    AmbiguousSpine,
    BindingViolation,
    Lifecycle,
    UnknownTransition,
)


@pytest.fixture
def lifecycle() -> Lifecycle:
    return Lifecycle.load()


class TestTableComesFromTheContract:
    def test_all_seven_stages_are_present(self, lifecycle: Lifecycle) -> None:
        assert set(lifecycle.stages) == {
            "configure",
            "implement",
            "continuous_review",
            "final_review",
            "acceptance",
            "human_gate",
            "merger",
        }

    def test_llm_stages_are_the_dispatched_ones(self, lifecycle: Lifecycle) -> None:
        """Script stages run in-process; only llm stages cost an agent run."""
        llm = {name for name, stage in lifecycle.stages.items() if stage.is_llm}
        assert llm == {"implement", "continuous_review", "final_review"}

    @pytest.mark.parametrize(
        ("stage", "event", "target", "mode"),
        [
            ("implement", "success", "continuous_review", "inherit"),
            ("continuous_review", "APPROVE", "final_review", "inherit"),
            ("continuous_review", "REJECT", "implement", "rework"),
            ("final_review", "APPROVE", "acceptance", "inherit"),
            ("final_review", "REJECT", "implement", "rework"),
        ],
    )
    def test_declared_edges_resolve(
        self, lifecycle: Lifecycle, stage: str, event: str, target: str, mode: str
    ) -> None:
        transition = lifecycle.transition(stage, event)
        assert transition.target == target
        assert transition.next_mode == mode

    def test_rejection_sends_work_back_as_rework(self, lifecycle: Lifecycle) -> None:
        """A rejected attempt must not re-enter as though it were fresh."""
        assert lifecycle.transition("continuous_review", "REJECT").is_rework
        assert not lifecycle.transition("continuous_review", "APPROVE").is_rework

    def test_contract_version_is_exposed(self, lifecycle: Lifecycle) -> None:
        assert lifecycle.contract_version >= 2


class TestUnknownTransitionsAreFaults:
    """Guessing an edge is how a pipeline takes a path nobody designed."""

    @pytest.mark.parametrize(
        ("stage", "event"),
        [
            ("implement", "APPROVE"),
            ("continuous_review", "success"),
            ("acceptance", "APPROVE"),
            ("nonexistent_stage", "success"),
            ("final_review", "MAYBE"),
        ],
    )
    def test_undeclared_pair_raises(self, lifecycle: Lifecycle, stage: str, event: str) -> None:
        with pytest.raises(UnknownTransition, match="no declared transition"):
            lifecycle.transition(stage, event)

    def test_the_error_says_the_contract_is_authoritative(self, lifecycle: Lifecycle) -> None:
        with pytest.raises(UnknownTransition, match="contract is the authority"):
            lifecycle.transition("implement", "nope")


class TestForwardChain:
    """commit_binding stops a stage being handed work the previous one never made."""

    def test_matching_commits_advance(self, lifecycle: Lifecycle) -> None:
        transition = lifecycle.advance(
            "implement",
            "success",
            receipt={"output_commit": "abc123"},
            next_dispatch={"input_commit": "abc123"},
        )
        assert transition.target == "continuous_review"

    def test_a_severed_chain_is_refused(self, lifecycle: Lifecycle) -> None:
        with pytest.raises(BindingViolation, match="forward chain is severed"):
            lifecycle.advance(
                "implement",
                "success",
                receipt={"output_commit": "abc123"},
                next_dispatch={"input_commit": "deadbeef"},
            )

    def test_a_missing_output_commit_is_refused(self, lifecycle: Lifecycle) -> None:
        with pytest.raises(BindingViolation):
            lifecycle.advance(
                "implement",
                "success",
                receipt={},
                next_dispatch={"input_commit": "abc123"},
            )


class TestVerdictBinding:
    """A caller must not claim APPROVE while holding a REJECT receipt."""

    def test_matching_verdict_advances(self, lifecycle: Lifecycle) -> None:
        transition = lifecycle.advance(
            "continuous_review",
            "APPROVE",
            receipt={"verdict": "APPROVE", "output_commit": "c1"},
            next_dispatch={"input_commit": "c1"},
        )
        assert transition.target == "final_review"

    def test_mismatched_verdict_is_refused(self, lifecycle: Lifecycle) -> None:
        with pytest.raises(BindingViolation, match="event binding failed"):
            lifecycle.advance(
                "continuous_review",
                "APPROVE",
                receipt={"verdict": "REJECT", "output_commit": "c1"},
                next_dispatch={"input_commit": "c1"},
            )

    def test_a_required_receipt_cannot_be_omitted(self, lifecycle: Lifecycle) -> None:
        with pytest.raises(BindingViolation, match="requires a"):
            lifecycle.advance("implement", "success")


class TestFailureTaxonomy:
    def test_known_codes_carry_retryability(self, lifecycle: Lifecycle) -> None:
        assert lifecycle.failure_taxonomy
        assert lifecycle.is_retryable("INVALID_HANDOFF_SCHEMA") is False

    def test_unknown_codes_are_not_retryable(self, lifecycle: Lifecycle) -> None:
        """Retrying what the contract never described is how a broken pipeline
        becomes a loop that burns money."""
        assert lifecycle.is_retryable("SOMETHING_NOBODY_DECLARED") is False


class TestSingleSourceOfTruth:
    """One table, not two descriptions that can drift."""

    def test_the_stage_graph_is_absent_from_the_python(self) -> None:
        """No hardcoded edge list in code.

        If the transitions were also written in Python, this repo would hold
        two machines and the shipped one would be whichever drifted less. The
        interpreter reading the contract is what makes plan.md P3's schema
        equivalence test vacuous by construction.
        """
        from fleet_graph.dd import lifecycle as module

        code = Path(module.__file__).read_text(encoding="utf-8")
        body = code.split('"""', 2)[-1]  # skip the module docstring
        for stage in ("continuous_review", "final_review", "acceptance", "merger"):
            assert stage not in body, f"{stage} is hardcoded in lifecycle.py"

    def test_a_changed_contract_changes_behaviour_without_code_edits(self, tmp_path: Path) -> None:
        contract = {
            "contract_version": 99,
            "stages": [{"id": "a", "actor": "script", "wrapper": True}],
            "transitions": [{"from": "a", "on": "go", "to": "b", "next_mode": "inherit"}],
        }
        path = tmp_path / "lifecycle.json"
        path.write_text(json.dumps(contract), encoding="utf-8")

        custom = Lifecycle.load(path)
        assert custom.transition("a", "go").target == "b"
        assert custom.contract_version == 99

    def test_the_real_contract_still_drives_the_default(self, lifecycle: Lifecycle) -> None:
        assert lifecycle.transition("implement", "success").target == "continuous_review"


class TestTheSpineIsDerivedNotWritten:
    """The five unconditional edges the transitions table does not carry."""

    def test_spine_falls_out_of_the_artifact_graph(self, lifecycle: Lifecycle) -> None:
        assert lifecycle.spine == {
            "configure": "implement",
            "implement": "continuous_review",
            "continuous_review": "final_review",
            "acceptance": "human_gate",
            "human_gate": "merger",
        }

    def test_the_derived_edges_agree_with_the_declared_ones(self, lifecycle: Lifecycle) -> None:
        """Where both exist they must say the same thing, or the contract is
        internally inconsistent and no runner can be trusted to walk it."""
        for source, target in lifecycle.spine.items():
            declared = [
                t.target for t in lifecycle.transitions if t.source == source and not t.is_rework
            ]
            if declared:
                assert target in declared, f"{source}: derived {target}, declared {declared}"

    def test_a_root_input_carries_no_edge(self, lifecycle: Lifecycle) -> None:
        """`spec` has no producer and five consumers; it must not imply an edge."""
        assert "spec" not in lifecycle.artifact_producers
        assert len(lifecycle.artifact_consumers["spec"]) > 1

    def test_an_artifact_nobody_consumes_carries_no_edge(self, lifecycle: Lifecycle) -> None:
        assert "product_code" not in lifecycle.artifact_consumers
        assert lifecycle.artifact_producers["product_code"] == ("implement",)

    def test_only_the_last_stage_is_terminal(self, lifecycle: Lifecycle) -> None:
        terminal = [name for name in lifecycle.stages if lifecycle.is_terminal(name)]
        assert terminal == ["merger"]

    def test_two_candidate_successors_refuse_rather_than_pick(self, tmp_path: Path) -> None:
        contract = {
            "contract_version": 99,
            "stages": [
                {"id": "a", "actor": "script", "produced_artifacts": ["x", "y"]},
                {"id": "b", "actor": "script", "required_artifacts": ["x"]},
                {"id": "c", "actor": "script", "required_artifacts": ["y"]},
            ],
            "transitions": [],
        }
        path = tmp_path / "lifecycle.json"
        path.write_text(json.dumps(contract), encoding="utf-8")

        with pytest.raises(AmbiguousSpine):
            Lifecycle.load(path).artifact_successor("a")

    def test_two_producers_of_one_artifact_carry_no_edge(self, tmp_path: Path) -> None:
        """Ambiguity on the producing side is silence, not a guess."""
        contract = {
            "contract_version": 99,
            "stages": [
                {"id": "a", "actor": "script", "produced_artifacts": ["x"]},
                {"id": "a2", "actor": "script", "produced_artifacts": ["x"]},
                {"id": "b", "actor": "script", "required_artifacts": ["x"]},
            ],
            "transitions": [],
        }
        path = tmp_path / "lifecycle.json"
        path.write_text(json.dumps(contract), encoding="utf-8")

        assert Lifecycle.load(path).artifact_successor("a") is None


class TestFailureExitsAndWrapper:
    def test_the_three_llm_stages_declare_a_failure_exit(self, lifecycle: Lifecycle) -> None:
        sources = {f.source for f in lifecycle.failure_transitions}
        assert sources == {name for name, s in lifecycle.stages.items() if s.is_llm}

    def test_failure_exit_is_terminal_never_materialised(self, lifecycle: Lifecycle) -> None:
        exit_ = lifecycle.failure_transition("implement", "failed")
        assert exit_ is not None
        assert exit_.terminal is True
        assert exit_.materialize is False
        assert exit_.receipt is False

    def test_a_script_stage_declares_no_failure_exit(self, lifecycle: Lifecycle) -> None:
        assert lifecycle.failure_transition("acceptance", "failed") is None

    def test_the_wrapper_order_comes_from_the_contract(self, lifecycle: Lifecycle) -> None:
        assert lifecycle.wrapper_steps == (
            "input_verify",
            "actor",
            "materialize",
            "output_verify",
        )
