"""Rendering the prompt the bundle ships, and refusing a half-filled one."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.dd.prompt import (
    IMPLEMENT_PERSONA,
    IMPLEMENT_TEMPLATE,
    PromptError,
    as_value,
    bundle_resources,
    render_stage_prompt,
    render_template,
    stage_values,
)

PINNED_PLUGIN = Path(
    "/data/code/self/loop-engine-dev-dispatch-plugin-releases"
    "/76c4003bd087890867b411186a0584ea3ba4364b"
)


class TestRendering:
    def test_a_required_placeholder_is_filled(self) -> None:
        assert render_template("at {{input_commit}}", {"input_commit": "abc"}) == "at abc"

    def test_an_unresolved_required_placeholder_is_a_fault(self) -> None:
        """A prompt that still names a field it does not carry is worse than a
        short one: the agent's answer gets shaped by something it never got."""
        with pytest.raises(PromptError, match="input_commit"):
            render_template("at {{input_commit}}", {})

    def test_an_optional_placeholder_resolves_to_nothing(self) -> None:
        assert render_template("ctx:{{context?}}", {}) == "ctx:"

    def test_every_missing_required_name_is_reported_at_once(self) -> None:
        with pytest.raises(PromptError) as raised:
            render_template("{{a}} {{b}} {{c?}}", {})
        assert "'a'" in str(raised.value) and "'b'" in str(raised.value)

    def test_structures_arrive_as_json_the_agent_can_read_back(self) -> None:
        rendered = render_template("ref: {{spec_ref}}", {"spec_ref": {"b": 2, "a": 1}})
        assert rendered == 'ref: {"a":1,"b":2}'

    def test_a_none_value_counts_as_absent(self) -> None:
        with pytest.raises(PromptError):
            render_template("{{x}}", {"x": None})
        assert render_template("{{x?}}", {"x": None}) == ""

    def test_scalars_keep_their_shape(self) -> None:
        assert as_value("plain") == "plain"
        assert as_value(3) == "3"
        assert as_value(True) == "true"
        assert as_value([["pytest", "-q"]]) == '[["pytest","-q"]]'


class TestStageValues:
    def test_acceptance_commands_render_as_runnable_lines(self) -> None:
        from fleet_graph.dd.prompt import render_commands

        assert render_commands([["pytest", "-q"], ["ruff", "check", "."]]) == (
            "pytest -q, ruff check ."
        )
        assert "'a b'" in render_commands([["echo", "a b"]]), "argv quoting must survive"
        assert render_commands([]) == "(none declared)"

    def test_the_dispatch_fields_are_available_by_name_and_whole(self) -> None:
        dispatch = {"input_commit": "a" * 40, "stage": "implement", "mode": "initial"}
        values = stage_values(
            dispatch,
            worktree_path="/w",
            run_id="run-1",
            actor_job_id="job-1",
            acceptance_commands=[["true"]],
        )
        assert values["input_commit"] == "a" * 40
        assert values["dispatch"] == dispatch
        assert values["worktree_path"] == "/w"
        assert values["acceptance_commands"] == "true"

    def test_the_trigger_id_stands_in_rather_than_being_invented(self) -> None:
        """fleet-graph has no trigger store; the run id is the honest stand-in."""
        values = stage_values({}, worktree_path="/w", run_id="run-1", actor_job_id="j")
        assert values["trigger_id"] == "run-1"


class TestAgainstThePinnedBundle:
    """The template the production plugin actually ships."""

    def _resources(self) -> dict[str, str]:
        if not PINNED_PLUGIN.is_dir():
            pytest.skip("the pinned plugin release is not on this machine")
        base = PINNED_PLUGIN / "workflows/dev-dispatch/implement"
        return {
            IMPLEMENT_PERSONA: (base / "personas/implementer.md").read_text(encoding="utf-8"),
            IMPLEMENT_TEMPLATE: (base / "templates/implement.md").read_text(encoding="utf-8"),
        }

    def _dispatch(self) -> dict[str, Any]:
        return {
            "attempt_id": "att-1",
            "contract_version": "dev-dispatch.attempt-context/v1",
            "development_id": "dev-1",
            "expected_remote_head": "a" * 40,
            "feedback_ref": {"path": ".dev-dispatch/feedback/index.json", "entry_count": 0},
            "input_commit": "a" * 40,
            "materialization_intent_id": "intent-1",
            "mode": "initial",
            "parent_handoff_receipt_digest": "sha256:" + "c" * 64,
            "spec_ref": {"path": ".dev-dispatch/spec/approved.md"},
            "stage": "implement",
            "target_base_commit": "b" * 40,
        }

    def test_the_real_template_renders_with_the_real_dispatch(self) -> None:
        """Its placeholders are the StageDispatch plus a few runtime values --
        not a coincidence: both are the same contract."""
        rendered = render_stage_prompt(
            self._resources(),
            IMPLEMENT_PERSONA,
            IMPLEMENT_TEMPLATE,
            stage_values(
                self._dispatch(),
                worktree_path="/w",
                run_id="run-1",
                actor_job_id="job-1",
                acceptance_commands=[["python3", "-m", "pytest", "-q"]],
            ),
        )
        assert "{{" not in rendered, "nothing was left unfilled"
        assert "a" * 40 in rendered
        assert "python3 -m pytest -q" in rendered
        assert '[["python3"' not in rendered, "argv rendered as JSON is not an instruction"

    def test_it_carries_what_the_role_persona_leaves_out(self) -> None:
        """The whole reason for reading the prompt out of the bundle."""
        rendered = render_stage_prompt(
            self._resources(),
            IMPLEMENT_PERSONA,
            IMPLEMENT_TEMPLATE,
            stage_values(
                self._dispatch(),
                worktree_path="/w",
                run_id="r",
                actor_job_id="j",
                acceptance_commands=[["true"]],
            ),
        )
        assert "outcome" in rendered
        assert "verification_record" in rendered
        assert "contract violation" in rendered, "the anti-fabrication rule must survive"

    def test_it_overrides_the_bundles_own_envelope_example(self) -> None:
        """The bundle's example nests the payload under `result` -- that is
        loop-engine's envelope. agent-run validates `Envelope.result` itself,
        so the fields belong at the top level. An agent copying the example is
        rejected for missing exactly the three top-level fields."""
        rendered = render_stage_prompt(
            self._resources(),
            IMPLEMENT_PERSONA,
            IMPLEMENT_TEMPLATE,
            stage_values(
                self._dispatch(),
                worktree_path="/w",
                run_id="r",
                actor_job_id="job-1",
                acceptance_commands=[["true"]],
            ),
        )
        assert "Result envelope" in rendered
        assert "this overrides the example above" in rendered
        for field in ("actor_job_id", "outcome", "work_head_commit", "verification_record"):
            assert field in rendered, field

    def test_the_transport_note_can_be_turned_off(self) -> None:
        rendered = render_stage_prompt(
            self._resources(),
            IMPLEMENT_PERSONA,
            IMPLEMENT_TEMPLATE,
            stage_values(self._dispatch(), worktree_path="/w", run_id="r", actor_job_id="j"),
            transport="",
        )
        assert "Result envelope" not in rendered
        assert "Envelope.result" in rendered, "the persona says it either way"

    def test_a_missing_part_of_the_bundle_is_refused(self) -> None:
        with pytest.raises(PromptError, match="carries no"):
            render_stage_prompt({}, IMPLEMENT_PERSONA, IMPLEMENT_TEMPLATE, {})


class TestBundleDecoding:
    def test_resources_decode_by_relative_path(self) -> None:
        class Resource:
            relative_path = "implement/templates/implement.md"
            content = b"hello {{name}}"

        assert bundle_resources((Resource(),)) == {
            "implement/templates/implement.md": "hello {{name}}"
        }


class TestTheEnvelopeMismatchThatCostFourRuns:
    """Two harnesses, two envelopes; the one shipping the template is not the
    one dispatching."""

    def test_the_bundle_template_really_does_nest_under_result(self) -> None:
        if not PINNED_PLUGIN.is_dir():
            pytest.skip("the pinned plugin release is not on this machine")
        template = (
            PINNED_PLUGIN / "workflows/dev-dispatch/implement/templates/implement.md"
        ).read_text(encoding="utf-8")
        assert '"result": {' in template, "if this stops being true, the override can go"

    def test_the_override_says_plainly_which_keys_do_not_belong(self) -> None:
        from fleet_graph.dd.prompt import RESULT_TRANSPORT

        assert "No outer `result` key" in RESULT_TRANSPORT
        assert "top level" in RESULT_TRANSPORT


class TestReviewsAreLeftAlone:
    def test_the_prompt_source_declines_stages_it_should_not_reproduce(self) -> None:
        """The review stages are a multi-node workflow in the bundle. Rebuilding
        that inside the orchestration shell is the opposite of the point."""
        from fleet_graph.dd.prompt import PluginPromptSource

        source = PluginPromptSource(binding=None, builder=None, worktree_path="/w")
        for stage in ("continuous_review", "final_review", "acceptance"):
            assert source.for_stage(stage, {}, run_id="r", actor_job_id="j") is None

    def test_the_bundle_really_does_ship_a_multi_node_review(self) -> None:
        if not PINNED_PLUGIN.is_dir():
            pytest.skip("the pinned plugin release is not on this machine")
        templates = sorted(
            p.name
            for p in (
                PINNED_PLUGIN / "workflows/dev-dispatch/continuous_review/templates"
            ).iterdir()
        )
        assert len(templates) > 1, templates
        assert json.dumps(templates)
