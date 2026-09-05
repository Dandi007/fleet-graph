"""X-4: a failed agent run is classified from its own evidence.

The regression target is dev-fg-d9370430e0ce implement attempt 2 (run
16dbffe9-ed06-57c7-b559-2a835eaa2e89): the model finished 52 messages in
12.7 minutes, every route attempt clean (no http_status, no signal, no
error_class, exit 0), the gateway healthy -- and the run was recorded as
`PROVIDER_UNAVAILABLE` / root_cause `transport` because its structured
output contract said "no structured output found in stdout". The
classification table then granted it a bounded transport retry it never
earned, and its spend fell into `unknown`. Each fixture below is the
result.json shape the spec pinned, and each paired assertion names the
mutation that would turn it red.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fleet_graph.cost_obs import CostDataPlane
from fleet_graph.cost_obs.rules import COST_METRIC, LAUNCH_METRIC, REVIEW_METRIC
from fleet_graph.dd.egress import (
    PROVIDER_UNAVAILABLE,
    ROOT_CAUSE_DISPOSITION,
    ROOT_CAUSE_EXECUTION,
    ROOT_CAUSE_TRANSPORT,
    layer_failure,
    root_cause_for,
)
from fleet_graph.executors.agent_run import RunStatus, RunTicket, RunWaitTimeout
from fleet_graph.graphs.dd_actors import (
    AGENT_RUN_FAILED,
    AgentRunStageActor,
    classify_agent_run_failure,
)
from test_dd_actors import (
    IMPLEMENT,
    LIFECYCLE,
    REVIEW,
    RecordingLauncher,
    dispatch_for,
    make_actor,
)

RUN_ID = "16dbffe9-ed06-57c7-b559-2a835eaa2e89"

# The X-4 evidence, verbatim in shape: a clean wire, a broken contract.
X4_CONTRACT_VIOLATION: dict[str, Any] = {
    "state": "failed",
    "exit_code": 97,
    "contract_error": "no structured output found in stdout",
    "agent_error": False,
    "agent_error_subtype": None,
    "route_attempts": [
        {
            "route": "glm-5.3-flash@opencode/gw",
            "error_class": None,
            "signal": None,
            "http_status": None,
            "exit_code": 0,
            "duration_s": 763.387,
        }
    ],
    "usage": {"total_tokens": 42424},
}


def clean_route_attempt(**overrides: Any) -> dict[str, Any]:
    attempt: dict[str, Any] = {
        "route": "glm-5.3-flash@opencode/gw",
        "error_class": None,
        "signal": None,
        "http_status": None,
        "exit_code": 0,
        "duration_s": 100.0,
    }
    attempt.update(overrides)
    return attempt


def failed_run_result(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "state": "failed",
        "exit_code": 97,
        "contract_error": "",
        "agent_error": False,
        "agent_error_subtype": None,
        "route_attempts": [clean_route_attempt()],
        "usage": {"total_tokens": 1000},
    }
    result.update(overrides)
    return result


def cost_samples(plane: CostDataPlane, metric: str) -> list[dict[str, str]]:
    """The label maps of every emitted fact of one metric, in emission order."""
    return [dict(sample.labels) for sample in plane.samples() if sample.name == metric]


def failure_cost_labels(plane: CostDataPlane) -> list[dict[str, str]]:
    """The classified failed-run spend facts (the ones carrying a failure_code)."""
    return [labels for labels in cost_samples(plane, COST_METRIC) if labels.get("failure_code")]


class TestContractViolationIsNotTransport:
    """阴性用例 1 (回归靶): the X-4 result.json shape must classify as a
    contract violation, never a provider outage. Mutation red anchor: delete
    the evidence split in `classify_agent_run_failure` (back to the old
    unconditional PROVIDER_UNAVAILABLE) and every assertion here fails."""

    def test_the_x4_result_classifies_as_invalid_handoff_schema(self) -> None:
        fault = classify_agent_run_failure(X4_CONTRACT_VIOLATION, state="failed", run_id=RUN_ID)

        assert fault.failure_code == "INVALID_HANDOFF_SCHEMA"
        assert fault.failure_code != PROVIDER_UNAVAILABLE
        assert fault.unattributed is False
        assert fault.contract_error == "no structured output found in stdout"

    def test_the_detail_carries_the_contract_error_and_run_id_verbatim(self) -> None:
        fault = classify_agent_run_failure(X4_CONTRACT_VIOLATION, state="failed", run_id=RUN_ID)

        assert RUN_ID in fault.detail
        assert "no structured output found in stdout" in fault.detail

    def test_the_classification_roots_in_execution_not_transport(self) -> None:
        fault = classify_agent_run_failure(X4_CONTRACT_VIOLATION, state="failed", run_id=RUN_ID)

        assert root_cause_for(fault.failure_code, fault.detail) == ROOT_CAUSE_EXECUTION
        layered = layer_failure(fault.failure_code, fault.detail)
        assert layered["root_cause"] == ROOT_CAUSE_EXECUTION
        assert layered["disposition"] == ROOT_CAUSE_DISPOSITION[ROOT_CAUSE_EXECUTION]

    def test_a_contract_violation_is_not_a_retryable_code(self) -> None:
        fault = classify_agent_run_failure(X4_CONTRACT_VIOLATION, state="failed", run_id=RUN_ID)

        assert LIFECYCLE.is_retryable(fault.failure_code) is False

    def test_the_actor_reports_the_classified_failure(self, tmp_path: Path) -> None:
        launcher = RecordingLauncher(RunStatus("failed", dict(X4_CONTRACT_VIOLATION)))
        outcome = make_actor(tmp_path, launcher).act(IMPLEMENT, dispatch_for(IMPLEMENT))

        assert outcome.event == "failed"
        assert outcome.failure_code == "INVALID_HANDOFF_SCHEMA"
        assert "no structured output found in stdout" in (outcome.detail or "")
        assert outcome.run_in_flight is False


class TestTransportEvidenceStaysTransport:
    """阴性用例 2: real transport evidence keeps the transport code, on all
    four evidence kinds the spec names. Mutation red anchor: make the
    classifier transport-blind (everything execution) and these go red."""

    @pytest.mark.parametrize(
        "evidence",
        [
            {"http_status": 502},
            {"signal": 9},
            {"error_class": "tls_handshake_failed"},
            {"exit_code": 3},
        ],
        ids=["http_status", "signal", "error_class", "attempt_exit"],
    )
    def test_each_transport_evidence_kind_stays_provider_unavailable(
        self, evidence: dict[str, Any]
    ) -> None:
        result = failed_run_result(
            contract_error="no structured output found in stdout",
            route_attempts=[clean_route_attempt(**evidence)],
        )
        fault = classify_agent_run_failure(result, state="failed", run_id=RUN_ID)

        assert fault.failure_code == PROVIDER_UNAVAILABLE
        assert fault.unattributed is True
        assert root_cause_for(fault.failure_code, fault.detail) == ROOT_CAUSE_TRANSPORT

    def test_transport_evidence_wins_even_when_a_contract_error_rides_along(self) -> None:
        result = failed_run_result(
            contract_error="no structured output found in stdout",
            route_attempts=[clean_route_attempt(), clean_route_attempt(http_status=502)],
        )
        fault = classify_agent_run_failure(result, state="failed", run_id=RUN_ID)

        assert fault.failure_code == PROVIDER_UNAVAILABLE
        # The evidence fields travel verbatim in the detail (失败必须现形).
        assert "502" in fault.detail

    def test_the_actor_still_reports_transport_for_a_transport_failure(
        self, tmp_path: Path
    ) -> None:
        result = failed_run_result(route_attempts=[clean_route_attempt(signal=9)])
        launcher = RecordingLauncher(RunStatus("failed", result))
        outcome = make_actor(tmp_path, launcher).act(IMPLEMENT, dispatch_for(IMPLEMENT))

        assert outcome.failure_code == PROVIDER_UNAVAILABLE


class TestLostOrMissingResultStaysTransport:
    """阴性用例 3: a lost run or a missing result has no evidence to classify
    with, so transport remains the honest label. Mutation red anchor: gate
    the `result is None` / `state == "lost"` arms and these go red."""

    def test_a_lost_run_stays_provider_unavailable(self) -> None:
        fault = classify_agent_run_failure(None, state="lost", run_id=RUN_ID)

        assert fault.failure_code == PROVIDER_UNAVAILABLE
        assert fault.unattributed is True

    def test_a_failed_run_with_no_result_stays_provider_unavailable(self) -> None:
        fault = classify_agent_run_failure(None, state="failed", run_id=RUN_ID)

        assert fault.failure_code == PROVIDER_UNAVAILABLE
        assert fault.unattributed is True

    def test_the_actor_classifies_a_lost_run_as_transport(self, tmp_path: Path) -> None:
        launcher = RecordingLauncher(RunStatus("lost"))
        outcome = make_actor(tmp_path, launcher).act(IMPLEMENT, dispatch_for(IMPLEMENT))

        assert outcome.failure_code == PROVIDER_UNAVAILABLE


class TestAgentSideFailureIsTheExecutionFamily:
    """阴性用例 (行为契约 1 第三支): `agent_error: true` with no transport
    evidence is the agent's own failure -- a non-transport code carrying the
    subtype verbatim, never PROVIDER_UNAVAILABLE. Mutation red anchor: fold
    this arm back into the transport fallthrough and these go red."""

    def test_an_agent_error_gets_the_agent_run_failed_code(self) -> None:
        result = failed_run_result(agent_error=True, agent_error_subtype="tool_error")
        fault = classify_agent_run_failure(result, state="failed", run_id=RUN_ID)

        assert fault.failure_code == AGENT_RUN_FAILED
        assert fault.failure_code != PROVIDER_UNAVAILABLE
        assert fault.agent_error_subtype == "tool_error"
        assert fault.unattributed is False

    def test_the_subtype_travels_verbatim_in_the_detail(self) -> None:
        result = failed_run_result(agent_error=True, agent_error_subtype="context_overflow")
        fault = classify_agent_run_failure(result, state="failed", run_id=RUN_ID)

        assert "context_overflow" in fault.detail
        assert RUN_ID in fault.detail

    def test_an_agent_failure_without_a_subtype_still_names_the_agent(self) -> None:
        result = failed_run_result(agent_error=True, agent_error_subtype=None)
        fault = classify_agent_run_failure(result, state="failed", run_id=RUN_ID)

        assert fault.failure_code == AGENT_RUN_FAILED
        assert fault.agent_error_subtype == ""

    def test_an_agent_failure_is_not_retryable_and_roots_in_execution(self) -> None:
        result = failed_run_result(agent_error=True, agent_error_subtype="tool_error")
        fault = classify_agent_run_failure(result, state="failed", run_id=RUN_ID)

        assert LIFECYCLE.is_retryable(fault.failure_code) is False
        assert root_cause_for(fault.failure_code, fault.detail) == ROOT_CAUSE_EXECUTION


class TestFailedRunSpendAttribution:
    """阴性用例 5: a classified failed run's spend lands in the lifecycle the
    stage served, with the classification as labels -- and never in
    `unknown`. Transport interruptions keep the `unknown` status quo.
    Mutation red anchor: route the classified paths back through
    `_record_unknown_spend` and every non-unknown assertion here fails."""

    @staticmethod
    def actor(tmp_path: Path, launcher: RecordingLauncher) -> AgentRunStageActor:
        actor = make_actor(tmp_path, launcher)
        actor.cost_plane = CostDataPlane()
        return actor

    @staticmethod
    def plane_of(actor: AgentRunStageActor) -> CostDataPlane:
        assert actor.cost_plane is not None
        return actor.cost_plane

    def test_a_contract_violations_spend_is_attributed_not_unknown(self, tmp_path: Path) -> None:
        actor = self.actor(tmp_path, RecordingLauncher(RunStatus("failed", X4_CONTRACT_VIOLATION)))
        outcome = actor.act(IMPLEMENT, dispatch_for(IMPLEMENT))

        assert outcome.failure_code == "INVALID_HANDOFF_SCHEMA"
        unknown = [
            labels
            for labels in cost_samples(self.plane_of(actor), COST_METRIC)
            if labels.get("attribution") == "unknown"
        ]
        assert unknown == [], "a classified failed run must never land in unknown"

    def test_launch_fact_and_failure_cost_carry_the_classification(self, tmp_path: Path) -> None:
        actor = self.actor(tmp_path, RecordingLauncher(RunStatus("failed", X4_CONTRACT_VIOLATION)))
        actor.act(IMPLEMENT, dispatch_for(IMPLEMENT))
        plane = self.plane_of(actor)

        assert cost_samples(plane, LAUNCH_METRIC) == [
            {
                "order_id": "dev-1",
                "development_id": "dev-1",
                "generation": "1",
                "seat": "implementer",
                "model": "",
            }
        ]
        assert failure_cost_labels(plane) == [
            {
                "attribution": "launch",
                "order_id": "dev-1",
                "failure_code": "INVALID_HANDOFF_SCHEMA",
                "root_cause": "execution",
            }
        ]

    def test_a_review_failure_attributed_to_review_with_a_failed_verdict(
        self, tmp_path: Path
    ) -> None:
        result = failed_run_result(contract_error="no structured output found in stdout")
        actor = self.actor(tmp_path, RecordingLauncher(RunStatus("failed", result)))
        actor.act(REVIEW, dispatch_for(REVIEW, attempt=2))
        plane = self.plane_of(actor)

        assert cost_samples(plane, REVIEW_METRIC) == [
            {"order_id": "dev-1", "phase": "continuous", "verdict": "failed"}
        ]
        assert failure_cost_labels(plane) == [
            {
                "attribution": "review",
                "order_id": "dev-1",
                "failure_code": "INVALID_HANDOFF_SCHEMA",
                "root_cause": "execution",
            }
        ]
        assert cost_samples(plane, COST_METRIC) == failure_cost_labels(plane)

    def test_a_transport_interruption_keeps_the_unknown_status_quo(self, tmp_path: Path) -> None:
        result = failed_run_result(
            contract_error="no structured output found in stdout",
            route_attempts=[clean_route_attempt(http_status=502)],
            usage={"total_tokens": 42424},
        )
        actor = self.actor(tmp_path, RecordingLauncher(RunStatus("failed", result)))
        actor.act(IMPLEMENT, dispatch_for(IMPLEMENT))
        plane = self.plane_of(actor)

        assert cost_samples(plane, COST_METRIC) == [{"attribution": "unknown", "order_id": "dev-1"}]
        assert cost_samples(plane, LAUNCH_METRIC) == []

    def test_a_timeout_with_reported_usage_lands_in_unknown(self, tmp_path: Path) -> None:
        actor = self.actor(tmp_path, RecordingLauncher())
        actor._record_unknown_spend(RUN_ID, envelope={"usage": {"total_tokens": 99}})

        assert [
            labels["attribution"] for labels in cost_samples(self.plane_of(actor), COST_METRIC)
        ] == ["unknown"]

    def test_a_replayed_failed_run_cannot_double_count(self, tmp_path: Path) -> None:
        launcher = RecordingLauncher(RunStatus("failed", dict(X4_CONTRACT_VIOLATION)))
        actor = self.actor(tmp_path, launcher)
        actor.act(IMPLEMENT, dispatch_for(IMPLEMENT))
        actor.act(IMPLEMENT, dispatch_for(IMPLEMENT))

        assert len(failure_cost_labels(self.plane_of(actor))) == 1

    def test_an_unmeasured_failed_run_records_no_synthetic_zero(self, tmp_path: Path) -> None:
        result = dict(X4_CONTRACT_VIOLATION)
        result.pop("usage")
        actor = self.actor(tmp_path, RecordingLauncher(RunStatus("failed", result)))
        actor.act(IMPLEMENT, dispatch_for(IMPLEMENT))

        assert cost_samples(self.plane_of(actor), COST_METRIC) == []


class TestResidualKeepsTheLegacyClassification:
    """Only positive evidence re-classifies. A failed run with no evidence
    either way -- no transport fields, no contract_error, agent_error false
    -- keeps the legacy provider code, its bounded retry, and its unknown
    spend, exactly as before X-4. Mutation red anchor: classify the residual
    as an agent failure and the bounded-retry walks (test_dd_runner) go red."""

    def test_an_evidence_less_failed_run_keeps_the_provider_code(self) -> None:
        fault = classify_agent_run_failure({"exit_code": 97}, state="failed", run_id=RUN_ID)

        assert fault.failure_code == PROVIDER_UNAVAILABLE
        assert fault.unattributed is True
        assert LIFECYCLE.is_retryable(fault.failure_code) is True

    def test_a_result_with_no_route_attempts_keeps_the_legacy_code(self) -> None:
        result = failed_run_result(route_attempts=[])
        fault = classify_agent_run_failure(result, state="failed", run_id=RUN_ID)

        assert fault.failure_code == PROVIDER_UNAVAILABLE
        assert fault.unattributed is True

    def test_a_malformed_route_attempt_is_not_transport_evidence(self) -> None:
        result = failed_run_result(route_attempts=["garbage", None, 7])
        fault = classify_agent_run_failure(result, state="failed", run_id=RUN_ID)

        assert fault.failure_code == PROVIDER_UNAVAILABLE
        assert fault.unattributed is True

    def test_agent_error_false_is_not_an_agent_failure(self) -> None:
        result = failed_run_result(agent_error=False, agent_error_subtype="tool_error")
        fault = classify_agent_run_failure(result, state="failed", run_id=RUN_ID)

        assert fault.failure_code == PROVIDER_UNAVAILABLE

    def test_the_unknown_spend_path_still_runs_for_the_residual(self, tmp_path: Path) -> None:
        residual = RunStatus("failed", {"exit_code": 1, "usage": {"total_tokens": 11}})
        actor = TestFailedRunSpendAttribution.actor(tmp_path, RecordingLauncher(residual))
        actor.act(IMPLEMENT, dispatch_for(IMPLEMENT))

        assert [
            labels["attribution"]
            for labels in cost_samples(TestFailedRunSpendAttribution.plane_of(actor), COST_METRIC)
        ] == ["unknown"]


class TestClassifierIsTotalOverOddShapes:
    """The classifier reads engine-reported result.json shapes; malformed
    ones classify fail-closed instead of raising into the walker."""

    def test_an_empty_state_still_classifies(self) -> None:
        fault = classify_agent_run_failure(dict(X4_CONTRACT_VIOLATION), run_id=RUN_ID)

        assert fault.failure_code == "INVALID_HANDOFF_SCHEMA"


class TestWalkerIntegrationUntouched:
    """The zero-regression baseline: the success and timeout paths keep
    their exact previous behaviour; only the classified failed-run branch
    changed."""

    def test_a_successful_run_still_forwards_and_records_the_launch(self, tmp_path: Path) -> None:
        declared = {
            "actor_job_id": "job-1",
            "input_commit": "1" * 40,
            "work_head_commit": "2" * 40,
        }
        launcher = RecordingLauncher(
            RunStatus("succeeded", {"structured_result": declared, "usage": {"total_tokens": 5}})
        )
        actor = self.actor(tmp_path, launcher)
        outcome = actor.act(IMPLEMENT, dispatch_for(IMPLEMENT))

        assert outcome.event == "success"
        assert actor.cost_plane is not None
        assert len(cost_samples(actor.cost_plane, LAUNCH_METRIC)) == 1
        assert [
            labels["attribution"] for labels in cost_samples(actor.cost_plane, COST_METRIC)
        ] == ["launch"]

    def test_the_timeout_retry_fence_is_untouched(self, tmp_path: Path) -> None:
        launcher = RecordingLauncher()
        launcher.raise_on_wait = RunWaitTimeout(RunTicket("run-1", "/tmp/run-1", None), 90.0)
        outcome = make_actor(tmp_path, launcher).act(IMPLEMENT, dispatch_for(IMPLEMENT))

        assert outcome.event == "failed"
        assert outcome.failure_code == PROVIDER_UNAVAILABLE
        assert outcome.run_in_flight is True
        assert LIFECYCLE.is_retryable(outcome.failure_code)

    @staticmethod
    def actor(tmp_path: Path, launcher: RecordingLauncher) -> AgentRunStageActor:
        actor = make_actor(tmp_path, launcher)
        actor.cost_plane = CostDataPlane()
        return actor
