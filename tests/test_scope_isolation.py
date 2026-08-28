"""B1 scope isolation. Refusal must be attributable to the declared rule."""

from __future__ import annotations

import pytest

from fleet_graph.dd.scope import (
    RULE_ID,
    ScopeBoundary,
    ScopeViolationError,
    default_boundary,
    evaluate,
    evaluate_text,
    require_scope,
)


class TestTheBoundaryIsData:
    def test_the_default_boundary_declares_the_b1_b3_phases(self) -> None:
        boundary = default_boundary()
        assert boundary.rule_id == RULE_ID
        assert boundary.phases == frozenset({"B1", "B2", "B3"})
        assert boundary.forbidden_revivals == frozenset({"katana#150", "katana#151"})

    def test_a_future_rescope_is_an_edit_to_the_declaration(self) -> None:
        boundary = ScopeBoundary(
            rule_id="b1-scope-boundary",
            label="B1-B4 isolated scope",
            phases=frozenset({"B1", "B2", "B3", "B4"}),
            forbidden_revivals=frozenset({"katana#150", "katana#151"}),
        )
        assert evaluate_text("implement B4 support", boundary).admitted


class TestStructuredMechanism:
    def test_an_in_scope_footprint_is_admitted(self) -> None:
        verdict = evaluate(phases=("B1", "B2", "B3"), revives=(), boundary=default_boundary())
        assert verdict.admitted
        assert not verdict.violations

    def test_a_declared_b4_phase_is_refused_and_attributed(self) -> None:
        verdict = evaluate(phases=("B4",), revives=(), boundary=default_boundary())
        assert not verdict.admitted
        assert verdict.rule_id == RULE_ID
        assert [violation.reference for violation in verdict.violations] == ["B4"]

    def test_a_forbidden_revival_is_refused(self) -> None:
        verdict = evaluate(phases=("B1",), revives=("katana#150",), boundary=default_boundary())
        assert not verdict.admitted
        assert [violation.reference for violation in verdict.violations] == ["katana#150"]


class TestFreeTextDetection:
    def test_an_in_scope_spec_is_admitted(self) -> None:
        verdict = evaluate_text("Add B1 scope isolation and B2 adoption.")
        assert verdict.admitted
        assert not verdict.violations

    def test_adding_b4_is_refused(self) -> None:
        verdict = evaluate_text("Implement B4 as the next phase.")
        assert not verdict.admitted
        assert verdict.rule_id == RULE_ID
        assert [violation.reference for violation in verdict.violations] == ["B4"]

    def test_reviving_katana_150_is_refused(self) -> None:
        verdict = evaluate_text("re-open katana#150 work")
        assert not verdict.admitted
        assert [violation.reference for violation in verdict.violations] == ["katana#150"]

    def test_a_deferred_b4_is_not_a_crossing(self) -> None:
        """The boundary's own deferral language respects, not crosses, the rule."""
        verdict = evaluate_text("B4 is explicitly deferred and must not be implemented.")
        assert verdict.admitted
        assert "B4" in verdict.observed_phases

    def test_a_forbidden_revival_behind_do_not_is_not_a_crossing(self) -> None:
        verdict = evaluate_text("Do not migrate, repair, or extend katana#151.")
        assert verdict.admitted

    def test_a_rejection_description_is_not_a_crossing(self) -> None:
        """Describing what the boundary rejects (the rule's own wording) is the
        boundary speaking, not the boundary being crossed."""
        verdict = evaluate_text(
            "The system must reject or quarantine work that attempts to add B4 "
            "or revive katana#150/katana#151."
        )
        assert verdict.admitted
        assert not verdict.violations

    def test_a_deferral_elsewhere_does_not_mask_a_later_active_crossing(self) -> None:
        verdict = evaluate_text(
            "B4 is explicitly deferred and must not be implemented. Implement B4 as the next phase."
        )
        assert not verdict.admitted
        assert [violation.reference for violation in verdict.violations] == ["B4"]

    def test_a_deferred_revival_elsewhere_does_not_mask_a_later_active_revival(self) -> None:
        verdict = evaluate_text(
            "Do not migrate, repair, or extend katana#150. re-open katana#150 work."
        )
        assert not verdict.admitted
        assert [violation.reference for violation in verdict.violations] == ["katana#150"]

    def test_repeated_active_crossings_of_the_same_phase_are_deduplicated(self) -> None:
        verdict = evaluate_text("Implement B4 and then implement B4 again.")
        assert not verdict.admitted
        assert [violation.reference for violation in verdict.violations] == ["B4"]

    def test_require_scope_raises_the_attributed_error(self) -> None:
        with pytest.raises(ScopeViolationError) as excinfo:
            require_scope("implement B4")
        assert excinfo.value.rule_id == RULE_ID
        assert "B4" in str(excinfo.value)
