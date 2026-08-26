"""The dd pipeline, walked from its contract.

`dd/lifecycle.py` reads the stage machine; this module walks it. The split
matters: the interpreter answers "what does the contract say comes next", and
nothing here is allowed to answer that question a second way. There is no edge
list below, no `if stage == "final_review"`, and no stage name outside a
docstring -- if you find yourself adding one, the contract is missing
something and the fix belongs there.

One stage step is the wrapper the contract declares, in the order it declares
it::

    input_verify -> actor -> materialize -> output_verify

- **input_verify** refuses to start a stage whose `required_artifacts` are not
  all on hand. A stage handed work that was never produced is the failure the
  forward chain exists to prevent.
- **actor** is an agent run for an `llm` stage and an in-process callable for a
  `script` stage. Which one is decided by the contract's `actor` field, not by
  a registry that could disagree with it.
- **materialize** is where the authoritative commit comes from, and the state's
  `head_commit` is set from it rather than from anything the actor said. The
  contract's `commit_binding` then compares the receipt's `output_commit`
  against the commit the next stage will actually start from. In dd both are
  meant to come from the deterministic sealer, so agreement is the expected
  case -- which is exactly why a disagreement is worth stopping on: one of the
  two is not what it claims to be, and the alternative is handing the next
  stage a phantom commit.
- **output_verify** refuses a stage that reported success without producing
  what it declared.

An unknown wrapper step is a fault rather than a skip. A silently skipped
`output_verify` is an unverified stage that still reports success, which is
worse than no verification at all because it looks like verification.

**The human gate holds one property worth stating plainly.** The graph
suspends on `interrupt()` and resumes on a `Command`, but the resume value is
never read as a verdict. On every resume the gate re-reads the board and takes
the decision from there. Whoever resumes the graph therefore cannot cast the
vote by resuming it -- which is the same reason `bus/board.py` has no
`work.decision.v1` publisher.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from fleet_graph.dd.capability import CapabilityError, CapabilityLock
from fleet_graph.dd.lifecycle import (
    AmbiguousSpine,
    Lifecycle,
    LifecycleError,
    Stage,
)

# The event an actor reports when nothing was steered -- the spine edge.
SPINE_EVENT = "success"
# The event that selects a declared failure exit.
FAILURE_EVENT = "failed"

STEP_INPUT_VERIFY = "input_verify"
STEP_ACTOR = "actor"
STEP_MATERIALIZE = "materialize"
STEP_OUTPUT_VERIFY = "output_verify"
KNOWN_WRAPPER_STEPS = frozenset(
    {STEP_INPUT_VERIFY, STEP_ACTOR, STEP_MATERIALIZE, STEP_OUTPUT_VERIFY}
)

MODE_NORMAL = "normal"
MODE_INHERIT = "inherit"

TERMINAL_COMPLETE = "complete"
TERMINAL_FAILED = "failed"
TERMINAL_REFUSED = "refused"
TERMINAL_BOUNDS = "bounds"
TERMINAL_FAULT = "fault"


class PipelineFault(RuntimeError):
    """The pipeline cannot continue and will not guess. Recorded, then stop."""


class StageRefused(RuntimeError):
    """An actor ended the pipeline on purpose, with a reason worth recording.

    The human gate raises this on a REJECT decision. It is deliberately not a
    fault: nothing broke, someone said no.
    """


@dataclass(frozen=True)
class GatePending(RuntimeError):
    """No decision on the board yet. The graph suspends; nothing is inferred."""

    ticket: dict[str, str]


@dataclass(frozen=True)
class StageOutcome:
    """What an actor reports. Everything else is verified, not believed."""

    event: str = SPINE_EVENT
    receipt: dict[str, Any] | None = None
    produced: tuple[str, ...] = ()
    failure_code: str = ""
    detail: str = ""


# What a stage is handed: identity, mode, the commit it starts from, and the
# artifact kinds the contract says it needs and owes.
Dispatch = dict[str, Any]


class Actor(Protocol):
    def act(self, stage: Stage, dispatch: Dispatch) -> StageOutcome: ...


class Materializer(Protocol):
    """Produces the authoritative commit for a stage's output.

    Independent of the actor on purpose: it is the second opinion the
    contract's `commit_binding` compares the receipt against.
    """

    def materialize(self, stage: Stage, dispatch: Dispatch, outcome: StageOutcome) -> str: ...


@dataclass(frozen=True)
class PipelineBounds:
    """Pure counting, INV-8 style. The contract declares no bound of its own."""

    max_steps: int = 40
    max_rework: int = 6
    max_retries: int = 2


class PipelineState(TypedDict, total=False):
    development_id: str
    stage: str
    mode: str
    generation: int
    attempt: int
    head_commit: str
    artifacts: dict[str, str]
    steps: int
    rework_count: int
    retries: dict[str, int]
    last_receipt: dict[str, Any]
    last_event: str
    last_failure_code: str
    history: list[dict[str, Any]]
    terminal: str
    terminal_reason: str
    fault: bool


@dataclass
class PipelineDeps:
    """Everything the walker talks to. Injected so the wiring is testable."""

    lifecycle: Lifecycle
    dispatcher: Actor
    scripts: dict[str, Actor] = field(default_factory=dict)
    materializer: Materializer | None = None
    capability: CapabilityLock | None = None
    bounds: PipelineBounds = field(default_factory=PipelineBounds)
    observe: Any = None

    def actor_for(self, stage: Stage) -> Actor:
        """The contract's `actor` field decides, not a registry that could differ."""
        if stage.is_llm:
            return self.dispatcher
        actor = self.scripts.get(stage.id)
        if actor is None:
            raise PipelineFault(
                f"stage {stage.id!r} is a script stage with no registered script; "
                "refusing to invent one"
            )
        return actor


# A node execution should need one interrupt to suspend. This is a runaway
# backstop for a caller that keeps handing back the same resume value, in the
# spirit of runner.py's recursion_limit -- not an expected path.
MAX_GATE_RECHECKS = 8


def _act_or_wait(actor: Actor, stage: Stage, dispatch: Dispatch) -> StageOutcome:
    """Run the actor, suspending the graph for as long as a human has not answered.

    `GatePending` means the board carries no decision yet. The graph suspends;
    on resume the actor runs again and re-reads the board. The resume value is
    deliberately discarded -- whoever resumes the graph must not be able to
    cast the vote by resuming it.

    Nothing is checkpointed about the open question here on purpose. The gate
    actor asks with an idempotency key, so a process that dies mid-wait and
    re-asks lands on the same note rather than a duplicate one; the key is the
    protection, not the checkpoint.
    """
    for _ in range(MAX_GATE_RECHECKS):
        try:
            return actor.act(stage, dispatch)
        except GatePending as pending:
            interrupt({"awaiting_decision": pending.ticket})
    raise PipelineFault(
        f"{stage.id} re-checked the board {MAX_GATE_RECHECKS} times without suspending; "
        "the resume value is being replayed"
    )


def _terminal(state_kind: str, reason: str, *, fault: bool = False) -> PipelineState:
    return {"terminal": state_kind, "terminal_reason": reason, "fault": fault}


def build_dd_pipeline_graph(deps: PipelineDeps) -> StateGraph:
    lifecycle = deps.lifecycle

    def _dispatch_for(state: PipelineState, stage: Stage) -> Dispatch:
        return {
            "development_id": state.get("development_id", ""),
            "stage": stage.id,
            "mode": state.get("mode", MODE_NORMAL),
            "generation": state.get("generation", 1),
            "attempt": state.get("attempt", 1),
            "input_commit": state.get("head_commit", ""),
            "required_artifacts": list(stage.required_artifacts),
            "produced_artifacts": list(stage.produced_artifacts),
            "contract_version": lifecycle.contract_version,
        }

    def _record(state: PipelineState, entry: dict[str, Any]) -> list[dict[str, Any]]:
        if deps.observe is not None:
            deps.observe(entry)
        return [*state.get("history", []), entry]

    def run_stage(state: PipelineState) -> PipelineState:
        stage_id = state.get("stage", "")
        stage = lifecycle.stages.get(stage_id)
        if stage is None:
            return _terminal(TERMINAL_FAULT, f"unknown stage {stage_id!r}", fault=True)

        steps = state.get("steps", 0) + 1
        if steps > deps.bounds.max_steps:
            return _terminal(TERMINAL_BOUNDS, f"step limit {deps.bounds.max_steps} reached")

        dispatch: Dispatch = _dispatch_for(state, stage)
        artifacts = dict(state.get("artifacts", {}))
        outcome: StageOutcome | None = None
        head_commit = state.get("head_commit", "")

        for step in lifecycle.wrapper_steps:
            if step not in KNOWN_WRAPPER_STEPS:
                # Skipping a step we do not implement would report success for
                # work that was never wrapped. Fault instead.
                return _terminal(
                    TERMINAL_FAULT,
                    f"contract declares wrapper step {step!r} which this runner does not implement",
                    fault=True,
                )

            if step == STEP_INPUT_VERIFY:
                missing = [k for k in stage.required_artifacts if k not in artifacts]
                if missing:
                    return _terminal(
                        TERMINAL_FAULT,
                        f"{stage.id} requires {sorted(missing)} which no stage has produced",
                        fault=True,
                    )

            elif step == STEP_ACTOR:
                # Fail-closed before anything runs against the bundle. Verified
                # for script stages too: they read the same contracts an agent
                # would, so the same tampering would mislead them.
                if deps.capability is not None:
                    try:
                        deps.capability.require()
                    except CapabilityError as exc:
                        return _terminal(TERMINAL_FAULT, str(exc), fault=True)
                try:
                    outcome = _act_or_wait(deps.actor_for(stage), stage, dispatch)
                except StageRefused as refused:
                    return {
                        **_terminal(TERMINAL_REFUSED, str(refused)),
                        "steps": steps,
                        "history": _record(
                            state, {"stage": stage.id, "step": step, "refused": str(refused)}
                        ),
                    }
                except PipelineFault as fault:
                    return _terminal(TERMINAL_FAULT, str(fault), fault=True)

            elif step == STEP_MATERIALIZE:
                assert outcome is not None  # actor precedes materialize in the contract
                exit_ = lifecycle.failure_transition(stage.id, outcome.event)
                if exit_ is not None and not exit_.materialize:
                    continue
                if deps.materializer is None:
                    return _terminal(
                        TERMINAL_FAULT,
                        f"{stage.id} declares a materialize step but no materializer is wired",
                        fault=True,
                    )
                try:
                    head_commit = deps.materializer.materialize(stage, dispatch, outcome)
                except Exception as exc:  # reported as a fault, never swallowed
                    return _terminal(
                        TERMINAL_FAULT, f"materialize failed on {stage.id}: {exc}", fault=True
                    )
                for kind in outcome.produced:
                    artifacts[kind] = head_commit

            elif step == STEP_OUTPUT_VERIFY:
                assert outcome is not None
                if lifecycle.failure_transition(stage.id, outcome.event) is not None:
                    continue
                missing = [k for k in stage.produced_artifacts if k not in outcome.produced]
                if missing:
                    return _terminal(
                        TERMINAL_FAULT,
                        f"{stage.id} reported {outcome.event!r} "
                        f"without producing {sorted(missing)}",
                        fault=True,
                    )

        assert outcome is not None
        return {
            "steps": steps,
            "artifacts": artifacts,
            "head_commit": head_commit,
            "last_event": outcome.event,
            "last_receipt": outcome.receipt or {},
            "last_failure_code": outcome.failure_code,
            "history": _record(
                state,
                {
                    "stage": stage.id,
                    "event": outcome.event,
                    "attempt": dispatch["attempt"],
                    "output_commit": head_commit,
                },
            ),
        }

    def advance(state: PipelineState) -> PipelineState:
        stage_id = state.get("stage", "")
        event = state.get("last_event", "")
        receipt = state.get("last_receipt") or None
        failure_code = str(state.get("last_failure_code", ""))

        if lifecycle.is_terminal(stage_id):
            return _terminal(TERMINAL_COMPLETE, f"{stage_id} is the last declared stage")

        exit_ = lifecycle.failure_transition(stage_id, event)
        if exit_ is not None:
            retries = dict(state.get("retries", {}))
            used = retries.get(stage_id, 0)
            if lifecycle.is_retryable(failure_code) and used < deps.bounds.max_retries:
                retries[stage_id] = used + 1
                return {"retries": retries}
            reason = f"{stage_id} failed ({failure_code or 'no failure code'})"
            if used:
                reason += f" after {used} bounded retries"
            return {**_terminal(TERMINAL_FAILED, reason), "retries": retries}

        next_dispatch = {"input_commit": state.get("head_commit", "")}
        try:
            transition = lifecycle.advance(
                stage_id, event, receipt=receipt, next_dispatch=next_dispatch
            )
        except LifecycleError as exc:
            if event != SPINE_EVENT or lifecycle.events_from(stage_id):
                # The spine is for stages the contract steers no verdict out of.
                # A stage that *does* declare verdict edges must take one of
                # them: letting it fall through to the spine on some other
                # event would route around the receipt and both bindings, and
                # a review that answered "success" instead of APPROVE would
                # sail past the verdict machinery entirely.
                return _terminal(TERMINAL_FAULT, str(exc), fault=True)
            try:
                successor = lifecycle.spine.get(stage_id)
            except AmbiguousSpine as ambiguous:
                return _terminal(TERMINAL_FAULT, str(ambiguous), fault=True)
            if successor is None:
                return _terminal(TERMINAL_FAULT, str(exc), fault=True)
            return {"stage": successor, "mode": state.get("mode", MODE_NORMAL)}

        if not transition.is_rework:
            # `inherit` means what it says: an attempt that entered as rework
            # stays rework for the rest of its pass, so the stages downstream
            # of a rejection know they are looking at reworked output.
            mode = state.get("mode", MODE_NORMAL)
            if transition.next_mode != MODE_INHERIT:
                mode = transition.next_mode
            return {"stage": transition.target, "mode": mode}

        rework = state.get("rework_count", 0) + 1
        if rework > deps.bounds.max_rework:
            return _terminal(
                TERMINAL_BOUNDS, f"rework limit {deps.bounds.max_rework} reached at {stage_id}"
            )
        return {
            "stage": transition.target,
            "mode": transition.next_mode,
            "attempt": state.get("attempt", 1) + 1,
            "rework_count": rework,
        }

    def after_stage(state: PipelineState) -> str:
        return END if state.get("terminal") else "advance"

    def after_advance(state: PipelineState) -> str:
        return END if state.get("terminal") else "run_stage"

    graph: StateGraph = StateGraph(PipelineState)
    graph.add_node("run_stage", run_stage)
    graph.add_node("advance", advance)
    graph.add_edge(START, "run_stage")
    graph.add_conditional_edges("run_stage", after_stage, ["advance", END])
    graph.add_conditional_edges("advance", after_advance, ["run_stage", END])
    return graph


def initial_state(
    *,
    development_id: str,
    stage: str,
    head_commit: str,
    artifacts: dict[str, str],
    generation: int = 1,
) -> PipelineState:
    """The pipeline's entry state. `artifacts` seeds the root inputs (the spec)."""
    return {
        "development_id": development_id,
        "stage": stage,
        "mode": MODE_NORMAL,
        "generation": generation,
        "attempt": 1,
        "head_commit": head_commit,
        "artifacts": dict(artifacts),
        "steps": 0,
        "rework_count": 0,
        "retries": {},
        "history": [],
    }


__all__ = [
    "FAILURE_EVENT",
    "MODE_INHERIT",
    "MODE_NORMAL",
    "SPINE_EVENT",
    "TERMINAL_BOUNDS",
    "TERMINAL_COMPLETE",
    "TERMINAL_FAILED",
    "TERMINAL_FAULT",
    "TERMINAL_REFUSED",
    "Actor",
    "Dispatch",
    "GatePending",
    "Materializer",
    "PipelineBounds",
    "PipelineDeps",
    "PipelineFault",
    "PipelineState",
    "StageOutcome",
    "StageRefused",
    "build_dd_pipeline_graph",
    "initial_state",
]
