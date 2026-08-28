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
- **materialize** is where the authoritative commit comes from, and it returns
  a receipt as well. **An actor reports; a sealer attests.** The sealer's
  receipt supersedes the actor's claim, so an actor that invents an
  `output_commit` changes nothing downstream -- the chain is built from what
  was actually written. What the contract's `commit_binding` still catches is
  a sealer whose attestation disagrees with the commit it produced, which is
  the difference between a broken chain and a phantom one handed onward.
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

import time
from dataclasses import dataclass, field, replace
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
from fleet_graph.dd.upstream_constants import compute_json_digest
from fleet_graph.state.run_artifacts import iso

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

# The dispatch mode vocabulary is the contract's, not ours:
# `stage-dispatch.schema.json` admits exactly `initial` and `rework`, and
# `development-lifecycle.json` spells the third value `inherit` in `next_mode`.
# Calling the first one "normal" would have produced a dispatch the plugin's
# own schema rejects. Pinned by test against both contracts.
MODE_INITIAL = "initial"
MODE_REWORK = "rework"
MODE_INHERIT = "inherit"

TERMINAL_COMPLETE = "complete"
TERMINAL_FAILED = "failed"
TERMINAL_REFUSED = "refused"
TERMINAL_BOUNDS = "bounds"
TERMINAL_FAULT = "fault"

# Terminal codes minted by the walker itself, for the two bounds exits. They
# are not in the contract's failure taxonomy because the contract declares no
# bound of its own (PipelineBounds' docstring); each names exactly one cause.
REWORK_LIMIT_REACHED = "REWORK_LIMIT_REACHED"
STEP_LIMIT_REACHED = "STEP_LIMIT_REACHED"


class PipelineFault(RuntimeError):
    """The pipeline cannot continue and will not guess. Recorded, then stop."""


class StageRefused(RuntimeError):
    """An actor ended the pipeline on purpose, with a reason worth recording.

    The human gate raises this on a REJECT decision. It is deliberately not a
    fault: nothing broke, someone said no.

    `code` names the one mechanical cause of the refusal (one code, one
    cause -- the m-1e94ea lesson). The raiser that knows why it refused says
    so here; a refusal without a code classifies by its raw text alone.
    """

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


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


@dataclass(frozen=True)
class Sealed:
    """What the materializer produced: the authoritative commit, and the receipt.

    The receipt matters as much as the commit. It is the sealer's own account
    of what it wrote, and it -- not the actor's claim -- is what the contract's
    bindings are checked against downstream. An actor reports; a sealer
    attests.
    """

    commit: str
    receipt: dict[str, Any] | None = None
    # Set only by a sealer that knows what it wrote. A sealer that merely
    # commits whatever the stage left behind leaves this None, so the stage's
    # own report still has to survive output_verify.
    produced: tuple[str, ...] | None = None


class Materializer(Protocol):
    """Produces the authoritative commit for a stage's output."""

    def materialize(self, stage: Stage, dispatch: Dispatch, outcome: StageOutcome) -> Sealed: ...


@dataclass(frozen=True)
class Replayed:
    """A stage already sealed by a previous generation, re-entered from its receipt.

    Nothing here is an actor's claim: the event and the commit come from a
    sealed receipt a replayer verified mechanically (digest chain closed,
    commit an ancestor of the current tree). The walker records it and
    advances exactly as it would for a freshly sealed stage, so every
    downstream binding still checks the same receipt.
    """

    event: str
    receipt: dict[str, Any]
    output_commit: str
    # The attempt identity the receipt was sealed under, from the receipt
    # body itself. The stages that continue this receipt's chain in the same
    # pass must dispatch under it -- the sealer reads the parent receipt at
    # exactly this identity and refuses one whose identity differs.
    attempt_id: str = ""


class Replayer(Protocol):
    """Decides, per stage, whether a sealed receipt stands in for a real run.

    Returns None to say "run it for real". The decision must be pure receipt
    mechanics -- prose, agent claims and history summaries do not count -- and
    once it declines a stage it must decline every later one, so a replay is
    always a prefix of the walk, never a hole in it.
    """

    def replay(self, stage: Stage, dispatch: Dispatch) -> Replayed | None: ...


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
    attempt_started_at: str
    # The sealed identity a replayed prefix pinned for the rest of this
    # attempt. Empty means derive from (generation, attempt) as always; a
    # rework clears it, because a new attempt is new work under its own
    # identity.
    pinned_attempt_id: str
    head_commit: str
    artifacts: dict[str, str]
    steps: int
    rework_count: int
    retries: dict[str, int]
    last_receipt: dict[str, Any]
    receipt_digests: dict[str, str]
    last_event: str
    last_failure_code: str
    last_failure_detail: str
    history: list[dict[str, Any]]
    terminal: str
    terminal_reason: str
    # The one mechanical cause of the ending, where one is known: a taxonomy
    # failure code, a refusal's own code, or a walker bounds code. Empty for
    # `complete` and for faults, whose raw reason is the whole story.
    terminal_code: str
    fault: bool


@dataclass
class PipelineDeps:
    """Everything the walker talks to. Injected so the wiring is testable."""

    lifecycle: Lifecycle
    dispatcher: Actor
    scripts: dict[str, Actor] = field(default_factory=dict)
    materializer: Materializer | None = None
    # Set for a restarted generation: replays the receipt-sealed prefix of the
    # previous one instead of re-dispatching agents against work that is
    # already in the tree (the F4 lesson: a fresh implement actor handed an
    # already-satisfied spec honestly reports BLOCKED, and the line jams).
    replayer: Replayer | None = None
    capability: CapabilityLock | None = None
    bounds: PipelineBounds = field(default_factory=PipelineBounds)
    observe: Any = None
    # The cost-observability data plane, when the DD lifecycle should emit the
    # launch/review/promotion/settlement source facts its recording rules
    # consume. The launch, review and promotion facts are emitted by their own
    # responsible actors (dd_actors, dd_scripts); the walker's only job is the
    # order's settlement (on completion) and its explicit absence accounting
    # (on a non-complete terminal). None means no collection.
    cost_plane: Any = None
    # Stamped once per attempt, never per step. The sealer puts this in the
    # commit it writes, so a retry that re-stamped would produce a different
    # commit for the same work -- and the forward chain would stop matching.
    clock: Any = None

    def stamp(self) -> str:
        return iso(self.clock() if self.clock is not None else time.time())

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


def _terminal(
    state_kind: str, reason: str, *, fault: bool = False, code: str = ""
) -> PipelineState:
    return {
        "terminal": state_kind,
        "terminal_reason": reason,
        "terminal_code": code,
        "fault": fault,
    }


def build_dd_pipeline_graph(deps: PipelineDeps) -> StateGraph:
    lifecycle = deps.lifecycle

    def _dispatch_for(state: PipelineState, stage: Stage) -> Dispatch:
        return {
            "development_id": state.get("development_id", ""),
            "stage": stage.id,
            "mode": state.get("mode", MODE_INITIAL),
            "generation": state.get("generation", 1),
            "attempt": state.get("attempt", 1),
            "attempt_started_at": state.get("attempt_started_at", ""),
            # The identity the sealed chain continues under, where a replayed
            # prefix pinned one. Everything that derives an attempt identity
            # downstream -- the dispatch builder, the role input, the parent
            # receipt path -- prefers this over re-deriving from the current
            # generation, so a review of replayed work names the receipt that
            # actually sealed it.
            "pinned_attempt_id": state.get("pinned_attempt_id", ""),
            # How many times this stage has already been retried. It travels
            # on the dispatch because the run id is derived, and a retry that
            # derives the same id re-adopts the run it is retrying.
            "retry": int(state.get("retries", {}).get(stage.id, 0)),
            "input_commit": state.get("head_commit", ""),
            "parent_receipt": dict(state.get("last_receipt") or {}),
            # Chain digests by the stage that sealed them. A later stage that
            # must name an earlier receipt reads it from here rather than
            # asking an agent to hand back a digest it has no business
            # authoring.
            "receipt_digests": dict(state.get("receipt_digests") or {}),
            # Which commit each artifact kind was sealed at. A review has to
            # name the implement commit it is reviewing, and that is not its
            # own input commit once a second review runs after the first.
            "artifact_commits": dict(state.get("artifacts") or {}),
            "required_artifacts": list(stage.required_artifacts),
            "produced_artifacts": list(stage.produced_artifacts),
            "contract_version": lifecycle.contract_version,
        }

    def _record(state: PipelineState, entry: dict[str, Any]) -> list[dict[str, Any]]:
        if deps.observe is not None:
            deps.observe(entry)
        return [*state.get("history", []), entry]

    def _refused(
        state: PipelineState, stage_id: str, step: str, refused: StageRefused, steps: int
    ) -> PipelineState:
        """A stage ended on purpose. Recorded and observed like any other step.

        The order launched but never completed, so the lifecycles that never
        produced a fact are accounted absent -- a bounded 0, distinguishable
        from `unknown` attribution -- rather than silently absent.
        """
        code = getattr(refused, "code", "")
        if deps.cost_plane is not None and state.get("development_id"):
            deps.cost_plane.mark_absent_if_missing(order_id=str(state.get("development_id")))
        return {
            **_terminal(TERMINAL_REFUSED, str(refused), code=code),
            "steps": steps,
            "history": _record(
                state,
                {
                    "stage": stage_id,
                    "step": step,
                    "refused": str(refused),
                    **({"refusal_code": code} if code else {}),
                },
            ),
        }

    def run_stage(state: PipelineState) -> PipelineState:
        stage_id = state.get("stage", "")
        stage = lifecycle.stages.get(stage_id)
        if stage is None:
            return _terminal(TERMINAL_FAULT, f"unknown stage {stage_id!r}", fault=True)

        steps = state.get("steps", 0) + 1
        if steps > deps.bounds.max_steps:
            return _terminal(
                TERMINAL_BOUNDS,
                f"step limit {deps.bounds.max_steps} reached",
                code=STEP_LIMIT_REACHED,
            )

        dispatch: Dispatch = _dispatch_for(state, stage)
        artifacts = dict(state.get("artifacts", {}))
        digests = dict(state.get("receipt_digests", {}))
        outcome: StageOutcome | None = None
        head_commit = state.get("head_commit", "")

        if deps.replayer is not None:
            replayed = deps.replayer.replay(stage, dispatch)
            if replayed is not None:
                # The stage is already sealed on the chain; enter its receipt
                # into the state exactly as a fresh seal would be entered, so
                # advance() checks the same bindings against the same receipt.
                # No actor runs and nothing is written -- that is the point.
                for kind in stage.produced_artifacts:
                    artifacts[kind] = replayed.output_commit
                digests[stage.id] = compute_json_digest(replayed.receipt)
                return {
                    "steps": steps,
                    "artifacts": artifacts,
                    "receipt_digests": digests,
                    "head_commit": replayed.output_commit,
                    "pinned_attempt_id": replayed.attempt_id or state.get("pinned_attempt_id", ""),
                    "last_event": replayed.event,
                    "last_receipt": dict(replayed.receipt),
                    "last_failure_code": "",
                    "last_failure_detail": "",
                    "history": _record(
                        state,
                        {
                            "stage": stage.id,
                            "event": replayed.event,
                            "attempt": dispatch["attempt"],
                            "output_commit": replayed.output_commit,
                            "replayed": True,
                        },
                    ),
                }

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
                    return _refused(state, stage.id, step, refused, steps)
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
                    sealed = deps.materializer.materialize(stage, dispatch, outcome)
                    head_commit = sealed.commit
                    if sealed.receipt is not None:
                        # The sealer attested; its account supersedes the
                        # actor's claim for every downstream binding.
                        outcome = replace(outcome, receipt=sealed.receipt)
                    if sealed.produced is not None:
                        # A seal that names what it wrote is a better witness
                        # than an agent's self-report -- it is the thing that
                        # actually wrote them.
                        outcome = replace(outcome, produced=sealed.produced)
                        digests[stage.id] = compute_json_digest(sealed.receipt)
                except StageRefused as refused:
                    # The sealer, not the actor, can end a stage too: a
                    # non-applied receipt is a legitimate "I will not apply
                    # this", which upstream also terminalises rather than
                    # reworking. Nothing broke, so it is not a fault.
                    return _refused(state, stage.id, step, refused, steps)
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
            "receipt_digests": digests,
            "head_commit": head_commit,
            "last_event": outcome.event,
            "last_receipt": outcome.receipt or {},
            "last_failure_code": outcome.failure_code,
            # The raw error text as the failing collaborator reported it. The
            # advance() terminal keeps only the code in its reason; without
            # this the original wording -- the thing an operator greps for --
            # would be gone by the time the run is a record.
            "last_failure_detail": outcome.detail if outcome.event == FAILURE_EVENT else "",
            "history": _record(
                state,
                {
                    "stage": stage.id,
                    "event": outcome.event,
                    "attempt": dispatch["attempt"],
                    "output_commit": head_commit,
                    **(
                        {"failure_code": outcome.failure_code, "detail": outcome.detail}
                        if outcome.event == FAILURE_EVENT
                        else {}
                    ),
                },
            ),
        }

    def advance(state: PipelineState) -> PipelineState:
        stage_id = state.get("stage", "")
        event = state.get("last_event", "")
        receipt = state.get("last_receipt") or None
        failure_code = str(state.get("last_failure_code", ""))

        if lifecycle.is_terminal(stage_id):
            if deps.cost_plane is not None and state.get("development_id"):
                deps.cost_plane.record_settlement(order_id=str(state.get("development_id")))
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
            return {
                **_terminal(TERMINAL_FAILED, reason, code=failure_code),
                "retries": retries,
            }

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
            return {"stage": successor, "mode": state.get("mode", MODE_INITIAL)}

        if not transition.is_rework:
            # `inherit` means what it says: an attempt that entered as rework
            # stays rework for the rest of its pass, so the stages downstream
            # of a rejection know they are looking at reworked output.
            mode = state.get("mode", MODE_INITIAL)
            if transition.next_mode != MODE_INHERIT:
                mode = transition.next_mode
            return {"stage": transition.target, "mode": mode}

        rework = state.get("rework_count", 0) + 1
        if rework > deps.bounds.max_rework:
            return _terminal(
                TERMINAL_BOUNDS,
                f"rework limit {deps.bounds.max_rework} reached at {stage_id}",
                code=REWORK_LIMIT_REACHED,
            )
        return {
            "stage": transition.target,
            "mode": transition.next_mode,
            "attempt": state.get("attempt", 1) + 1,
            "attempt_started_at": deps.stamp(),
            # A new attempt is new work under its own derived identity; the
            # replayed prefix's sealed identity ends here.
            "pinned_attempt_id": "",
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
    attempt_started_at: str = "",
) -> PipelineState:
    """The pipeline's entry state. `artifacts` seeds the root inputs (the spec)."""
    return {
        "development_id": development_id,
        "stage": stage,
        "mode": MODE_INITIAL,
        "generation": generation,
        "attempt": 1,
        "attempt_started_at": attempt_started_at or iso(time.time()),
        "head_commit": head_commit,
        "artifacts": dict(artifacts),
        "steps": 0,
        "rework_count": 0,
        "retries": {},
        "receipt_digests": {},
        "history": [],
    }


__all__ = [
    "FAILURE_EVENT",
    "MODE_INHERIT",
    "MODE_INITIAL",
    "MODE_REWORK",
    "REWORK_LIMIT_REACHED",
    "SPINE_EVENT",
    "STEP_LIMIT_REACHED",
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
    "Replayed",
    "Replayer",
    "Sealed",
    "StageOutcome",
    "StageRefused",
    "build_dd_pipeline_graph",
    "initial_state",
]
