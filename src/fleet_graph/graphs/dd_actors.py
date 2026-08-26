"""The two actors the dd walker actually talks to.

`dd_pipeline.py` knows no stage by name and reaches nothing outside its ports.
This is the other half: the wiring that turns a `Dispatch` into an agent run,
and the human gate into a question on the work board. Stage names appear here
because this is configuration -- which role serves which stage -- and not the
machine.

Two rules carried in from the ronin line, for the same reasons:

- Every agent run goes through `agent-run` (INV-4/B8). No harness is spawned
  here, and the run id is derived, so a crashed graph re-adopts the run in
  flight instead of paying for it twice.
- The dispatch goes in a file, never in argv. `/proc` makes argv
  world-readable and a stage dispatch names commits, paths, and identities.

And one that belongs to the gate alone: **an agent cannot approve anything
here.** `BoardGate` reads decisions; it has no way to write one, because
`bus/board.py` publishes no `work.decision.v1`. A decision that is not on the
board is not a decision, and the gate's answer to that is to wait.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fleet_graph.bus.board import Board, GateTicket
from fleet_graph.dd.dispatch import derive_attempt_id
from fleet_graph.dd.lifecycle import Lifecycle, Stage
from fleet_graph.executors.agent_run import (
    AgentRunLauncher,
    AgentRunSpec,
    RunWaitTimeout,
    derive_run_id,
)
from fleet_graph.graphs.adapters import DISPATCHER, CoordinatorFault, parse_envelope
from fleet_graph.graphs.dd_pipeline import (
    FAILURE_EVENT,
    SPINE_EVENT,
    Dispatch,
    GatePending,
    StageOutcome,
    StageRefused,
)
from fleet_graph.state.run_artifacts import write_json_durable

# The roles agent-runtime already ships for these stages. They exist because
# dd has always dispatched them; fleet-graph reuses them rather than minting
# parallel ones that would drift from the personas the plugin bundle carries.
DEFAULT_ROLES = {
    "implement": "implementer",
    "continuous_review": "continuous_reviewer",
    "final_review": "final_reviewer",
}

# Which stage owns which protocol artifact, per the contract. Defined here so
# the materializer and the dispatcher agree by construction.
IMPLEMENT_HANDOFF_ARTIFACT = "attempt_context_implement_handoff"
REVIEW_ARTIFACT = "attempt_context_review"


def implement_stage(lifecycle: Lifecycle) -> str | None:
    return lifecycle.sole_producer_of(IMPLEMENT_HANDOFF_ARTIFACT)


def review_stages(lifecycle: Lifecycle) -> tuple[str, ...]:
    return lifecycle.protocol_producers.get(REVIEW_ARTIFACT, ())


# The one word in the pipeline that means "go on". The contract declares which
# values a gate_decision may carry; it does not, and cannot, say which of them
# the pipeline should treat as consent. That is a policy, so it is written
# down here rather than inferred from the order of a list in a JSON file.
GATE_APPROVE = "APPROVE"

# Failure codes from the contract's own taxonomy. Nothing here invents one:
# an unknown code is not retryable, so guessing would turn a broken run into a
# bounded retry it never earned.
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
INVALID_HANDOFF_SCHEMA = "INVALID_HANDOFF_SCHEMA"


def stage_role(stage: Stage, roles: dict[str, str]) -> str:
    return roles.get(stage.id) or DEFAULT_ROLES.get(stage.id) or f"dd_{stage.id}"


@dataclass
class AgentRunStageActor:
    """One agent run per llm stage, dispatched through `agent-run`."""

    launcher: AgentRunLauncher
    development_id: str
    run_root: Path
    worktree_path: Path = Path(".")
    lifecycle: Lifecycle = field(default_factory=Lifecycle.load)
    roles: dict[str, str] = field(default_factory=dict)
    timeouts: dict[str, int] = field(default_factory=dict)
    default_timeout_seconds: int = 3600
    poll_interval: float = 2.0
    write: bool = True
    extra_labels: dict[str, str] = field(default_factory=dict)

    def _timeout(self, stage: Stage) -> int:
        return self.timeouts.get(stage.id, self.default_timeout_seconds)

    def role_input(self, stage: Stage, dispatch: Dispatch, run_id: str) -> dict[str, Any]:
        """What the role's own input schema asks for, and nothing more.

        `attempt-context.v1.json` wants six fields; the rest of the context is
        committed in the worktree, which is where the persona reads it. Sending
        the walker's richer dispatch instead would fail the role's own
        validation -- and would be telling the agent things the contract says
        it should be reading from the tree.
        """
        return {
            "attempt_id": derive_attempt_id(
                self.development_id, dispatch["generation"], dispatch["attempt"]
            ),
            "development_id": self.development_id,
            "spec_commit": dispatch["input_commit"],
            "stage": stage.id,
            "worktree_path": str(self.worktree_path),
            "run_id": run_id,
        }

    def act(self, stage: Stage, dispatch: Dispatch) -> StageOutcome:
        attempt_tag = f"g{dispatch['generation']}-a{dispatch['attempt']}"
        run_id = derive_run_id(f"{self.development_id}:{stage.id}", attempt_tag)
        input_path = write_json_durable(
            self.run_root / "stages" / f"{stage.id}-{attempt_tag}-input.json",
            self.role_input(stage, dispatch, run_id),
        )

        labels = {
            "development": self.development_id,
            "dispatcher": DISPATCHER,
            "stage": stage.id,
            **self.extra_labels,
        }
        spec = AgentRunSpec(
            prompt="",
            role=stage_role(stage, self.roles),
            input_path=str(input_path),
            prompt_file=str(input_path),
            structured=True,
            write=self.write,
            timeout_seconds=self._timeout(stage),
            labels=labels,
        )

        # `run_id` is derived, not random: the same stage and attempt always
        # names the same run, so a restarted graph adopts the run in flight.
        ticket = self.launcher.launch(spec, run_id)

        try:
            status = self.launcher.wait(
                ticket,
                poll_interval=self.poll_interval,
                deadline_seconds=self._timeout(stage) + 120,
            )
        except RunWaitTimeout as timeout:
            # A timeout means the run is still going, not that it is gone. It
            # is reported as retryable on purpose: the run id is derived, so
            # the retry re-adopts the run in flight rather than paying for a
            # second one. Reporting it as terminal would strand a live run.
            return StageOutcome(
                event=FAILURE_EVENT,
                failure_code=PROVIDER_UNAVAILABLE,
                detail=f"{stage.id} run {run_id} did not finish: {timeout}",
            )

        if status.result is None or not status.ok:
            return StageOutcome(
                event=FAILURE_EVENT,
                failure_code=PROVIDER_UNAVAILABLE,
                detail=f"{stage.id} run {run_id} ended {status.state}",
            )

        try:
            declared = parse_envelope(status.result)
        except CoordinatorFault as fault:
            # The run succeeded but answered in a shape we will not guess at.
            # Reading meaning out of stdout here is the INV-3 violation the
            # whole layering exists to avoid.
            return StageOutcome(
                event=FAILURE_EVENT,
                failure_code=INVALID_HANDOFF_SCHEMA,
                detail=str(fault),
            )

        return self._outcome_from(stage, declared)

    def _outcome_from(self, stage: Stage, declared: dict[str, Any]) -> StageOutcome:
        """The declared result, passed on as it stands.

        The role's result *is* the actor result the sealer consumes -- an
        `implement.result.v1` or a `review.result.v2`. It is forwarded whole
        rather than filtered, because the plugin validates it against its own
        schema and says exactly which field is missing. A translation layer
        here would only get between the agent and that answer.

        A result with no verdict reports the spine event. For a stage the
        contract steers a verdict out of, that is not a quiet approval: the
        walker has no spine edge there and faults, which is the right end for
        an agent that did not answer the question it was asked.
        """
        verdict = declared.get("verdict")
        event = (
            str(verdict).strip() if isinstance(verdict, str) and verdict.strip() else SPINE_EVENT
        )
        if stage.id in review_stages(self.lifecycle):
            receipt: dict[str, Any] | None = {"review_result": declared}
        else:
            receipt = declared
        return StageOutcome(
            event=event,
            receipt=receipt,
            produced=(),
            failure_code=str(declared.get("failure_code", "")),
            detail=str(declared.get("detail", "")),
        )


@dataclass
class BoardGate:
    """The human gate: ask once, then wait for the board to answer.

    `ask` is idempotency-keyed on the development and generation, so a graph
    that dies mid-wait and re-asks lands on the same question note rather than
    posting a second one. That, not a checkpoint, is what makes waiting safe
    to restart.
    """

    board: Board
    card_entity_id: str
    development_id: str
    question: str = ""
    approve: str = GATE_APPROVE
    allowed_decisions: tuple[str, ...] = (GATE_APPROVE, "REJECT")

    def _question(self, dispatch: Dispatch) -> str:
        if self.question:
            return self.question
        return (
            f"dev-dispatch {self.development_id} g{dispatch['generation']} "
            f"已过 acceptance，请裁决是否放行 merge（commit {dispatch['input_commit']}）。"
        )

    def _ticket(self, dispatch: Dispatch) -> GateTicket:
        return self.board.ask(
            card_entity_id=self.card_entity_id,
            question=self._question(dispatch),
            idempotency_key=f"dd-gate:{self.development_id}:g{dispatch['generation']}",
        )

    def act(self, stage: Stage, dispatch: Dispatch) -> StageOutcome:
        ticket = self._ticket(dispatch)
        decision = self.board.decision_for(ticket)
        if decision is None:
            raise GatePending(ticket.to_dict())

        verdict = decision.decision.strip().upper()
        if verdict not in self.allowed_decisions:
            # Not a refusal and not an approval. Refusing to map it onto either
            # is the point: a gate that rounds an unrecognised verdict towards
            # "proceed" is not a gate.
            raise StageRefused(
                f"gate decision {decision.decision!r} is not one of "
                f"{sorted(self.allowed_decisions)}; refusing to interpret it"
            )
        if verdict != self.approve:
            operator = decision.decided_by or "an operator"
            raise StageRefused(f"gate decision {verdict} by {operator}")

        return StageOutcome(
            event=SPINE_EVENT,
            receipt={
                "stage": stage.id,
                "decision": verdict,
                "decided_by": decision.decided_by,
                "decision_message_id": decision.message_id,
                "output_commit": dispatch["input_commit"],
            },
            produced=tuple(stage.produced_artifacts),
        )


__all__ = [
    "DEFAULT_ROLES",
    "GATE_APPROVE",
    "AgentRunStageActor",
    "BoardGate",
    "implement_stage",
    "review_stages",
    "stage_role",
]
