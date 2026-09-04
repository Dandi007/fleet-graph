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

from fleet_graph.bus.board import Board, GateTicket, normalize_decision
from fleet_graph.cost_obs import CostDataPlane
from fleet_graph.cost_obs.classify import LAUNCH, REVIEW
from fleet_graph.dd.dispatch import derive_attempt_id
from fleet_graph.dd.egress import PROVIDER_UNAVAILABLE
from fleet_graph.dd.git import run_git
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
from fleet_graph.graphs.dd_scripts import GATE_PATH, write_json
from fleet_graph.state.run_artifacts import write_json_durable

# The roles agent-runtime already ships for these stages. They exist because
# dd has always dispatched them; fleet-graph reuses them rather than minting
# parallel ones that would drift from the personas the plugin bundle carries.
DEFAULT_ROLES = {
    "implement": "implementer",
    "continuous_review": "continuous_reviewer",
    "final_review": "final_reviewer",
}

# The artifact whose producer is, by definition, the stage that changes the
# product. Asking the contract which stage that is beats writing the name down,
# and it is the only stage that may be dispatched with write.
PRODUCT_ARTIFACT = "product_code"

# agent-runtime's `attempt-context.v1.json` has its own stage vocabulary, and
# it is not the contract's: `review` where dd says `continuous_review`, and
# `final-review` -- hyphen -- where dd says `final_review`. Two vocabularies
# for the same three stages, so the translation has to be written down. Its
# values are pinned against that schema's enum by test.
ROLE_STAGE = {
    "implement": "implement",
    "continuous_review": "review",
    "final_review": "final-review",
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
# bounded retry it never earned. PROVIDER_UNAVAILABLE is the egress layer's
# flat code -- the legacy alias whose root cause reads `transport`.
INVALID_HANDOFF_SCHEMA = "INVALID_HANDOFF_SCHEMA"


def _usage_tokens(envelope: dict[str, Any] | None) -> float:
    """The token spend of one agent run, read from its result envelope.

    agent-run's ``result.json`` carries a ``usage`` block; accept the single
    ``total_tokens`` form and the ``prompt_tokens`` + ``completion_tokens``
    split. Returns ``0.0`` when no usable spend is present, so a caller never
    fabricates a token count the run never reported.
    """
    if not isinstance(envelope, dict):
        return 0.0
    usage = envelope.get("usage")
    if isinstance(usage, dict):
        total = usage.get("total_tokens")
        if isinstance(total, (int, float)):
            return float(total)
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if isinstance(prompt, (int, float)) or isinstance(completion, (int, float)):
            return float(prompt or 0) + float(completion or 0)
    total = envelope.get("total_tokens")
    if isinstance(total, (int, float)):
        return float(total)
    return 0.0


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
    # Where the stage's own prompt comes from. None means the role's persona
    # stands on its own, which is the right answer only where the bundle has
    # nothing better to say.
    prompts: Any = None
    roles: dict[str, str] = field(default_factory=dict)
    timeouts: dict[str, int] = field(default_factory=dict)
    # Per-stage model override. The role's selector is the default and stays
    # the policy; this is the caller's lever for a stage whose seat is not
    # working out, without editing config that other consumers share.
    models: dict[str, str] = field(default_factory=dict)
    model_runtime: str = "opencode"
    default_timeout_seconds: int = 3600
    poll_interval: float = 2.0
    extra_labels: dict[str, str] = field(default_factory=dict)
    #: The bounded principal that dispatched this development (a line folder or
    #: a human subject), carried from DevelopmentConfig. Never a run_id/uuid:
    #: the label must name a stable bounded subject, not an unbounded identity.
    #: Empty falls back to the dispatcher constant, the one bounded system
    #: subject always available when no finer provenance was recorded.
    dispatched_by: str = ""
    # When wired, this actor is the responsible producer of the launch (the
    # implement stage it dispatches) and review (the continuous/final review
    # stages) lifecycle facts the cost-observability recording rules consume.
    # None means the data plane is not collecting -- the DD dispatch still
    # runs, it just does not emit the facts.
    cost_plane: CostDataPlane | None = None
    #: When a fresh dispatch is about to start from a worktree that does not
    #: satisfy the attempt precondition (HEAD != the attempt's input_commit,
    #: or a dirty tree), restore it first: `reset --hard <input_commit>` +
    #: `clean`, the same sanctioned reset the actor contract allows, done here
    #: so the retry never has to declare BLOCKED on its predecessor's remnant
    #: commit. The action is recorded as `event=re_prepare` (with the cleared
    #: HEAD sha) through `observe`, when one is wired. Never runs while
    #: re-adopting a run still in flight: that run owns its workspace (#167).
    #: Off by default so the actor stays inert outside the engine that wires
    #: its worktree -- the runner enables it for the stages that share the
    #: attempt-context worktree.
    reprepare_worktree: bool = False
    #: The runner's event sink (`<run_root>/events.jsonl`). Wired so the
    #: re-prepare action is auditable like every other stage event.
    observe: Any = None

    def writes(self, stage: Stage) -> bool:
        """Only the stage the contract says produces product code.

        A role's own `write` declaration is a ceiling, not a grant: agent-run
        gives write only when the caller asks for it *and* the role allows it,
        and asking for it where the role forbids it is refused outright
        ("write tightening"). Reviewers declare write false, and a reviewer
        that writes to the subject workspace has its verdict discarded -- so
        asking on their behalf was never going to work, and should not.
        """
        return PRODUCT_ARTIFACT in stage.produced_artifacts

    def _timeout(self, stage: Stage) -> int:
        return self.timeouts.get(stage.id, self.default_timeout_seconds)

    def _reprepare_worktree(self, stage: Stage, dispatch: Dispatch) -> None:
        """Restore the worktree before a fresh dispatch whose precondition fails.

        A previous attempt that did its work but never reported (the
        contract_violation / no-structured-output exit) leaves a committed
        remnant at HEAD: the next attempt's exact-commit check would find
        HEAD != input_commit and refuse (BLOCKED), jamming the retry. This is
        the same sanctioned reset the actor contract allows -- `reset --hard
        <input_commit>` plus `clean` -- performed here by the engine so the
        retry starts from a worktree that satisfies its precondition. The
        cleared remnant commit is not preserved (git reflog keeps it), and the
        action is recorded as `event=re_prepare` with the cleaned HEAD sha, so
        the recovery is auditable.

        Never runs while re-adopting a run still in flight (#167): that run
        owns its workspace. Only a run that has truly terminally failed (or
        been lost) earns a fresh dispatch, and only then is its residue
        cleared. The caller gates on `re_adopt` before invoking this.
        """
        if not self.reprepare_worktree:
            return
        workspace = Path(self.worktree_path)
        input_commit = str(dispatch.get("input_commit") or "")
        if not input_commit:
            return
        head = run_git(workspace, "rev-parse", "HEAD")
        if head.returncode != 0:
            # Not even resolvable as a repo; leave the actor-side check to
            # refuse rather than guess.
            return
        current = head.stdout.strip()
        dirty = run_git(workspace, "status", "--porcelain=v1", "--untracked-files=all")
        if dirty.returncode != 0:
            return
        if current == input_commit and not dirty.stdout.strip():
            # The precondition already holds; nothing to restore.
            return
        cleared_head = current
        reset = run_git(workspace, "reset", "--hard", "--quiet", input_commit)
        if reset.returncode != 0:
            return
        run_git(workspace, "clean", "-fd")
        if self.observe is not None:
            self.observe(
                {
                    "event": "re_prepare",
                    "stage": stage.id,
                    "attempt": int(dispatch.get("attempt", 1)),
                    "generation": int(dispatch.get("generation", 1)),
                    "development_id": self.development_id,
                    "input_commit": input_commit,
                    "cleaned_head": cleared_head,
                }
            )

    def role_input(self, stage: Stage, dispatch: Dispatch, run_id: str) -> dict[str, Any]:
        """What the role's own input schema asks for, and nothing more.

        `attempt-context.v1.json` wants six fields; the rest of the context is
        committed in the worktree, which is where the persona reads it. Sending
        the walker's richer dispatch instead would fail the role's own
        validation -- and would be telling the agent things the contract says
        it should be reading from the tree.
        """
        return {
            # The identity the sealed chain continues under: a replayed
            # prefix pins the receipt's own identity, and the role reads the
            # committed context (`.dev-dispatch/handoffs/<attempt_id>/...`)
            # at exactly that identity -- re-deriving from the current
            # generation would point it at handoffs nobody sealed.
            "attempt_id": str(dispatch.get("pinned_attempt_id") or "")
            or derive_attempt_id(self.development_id, dispatch["generation"], dispatch["attempt"]),
            "development_id": self.development_id,
            "spec_commit": dispatch["input_commit"],
            "stage": ROLE_STAGE.get(stage.id, stage.id),
            "worktree_path": str(self.worktree_path),
            "run_id": run_id,
        }

    def act(self, stage: Stage, dispatch: Dispatch) -> StageOutcome:
        attempt_tag = f"g{dispatch['generation']}-a{dispatch['attempt']}"
        retry = int(dispatch.get("retry", 0))
        re_adopt = bool(dispatch.get("re_adopt", False))
        # A fresh dispatch may only start from a worktree that satisfies the
        # attempt precondition. A run still in flight (re_adopt) owns its
        # workspace and is never re-prepared; only a genuinely terminal/lost
        # run earns a fresh dispatch, and only then is its residue cleared.
        if not re_adopt:
            self._reprepare_worktree(stage, dispatch)
        # A timeout retry re-adopts the run still in flight: derive the
        # ORIGINAL run id (the retry-0 one), so the idempotent launcher adopts
        # the in-flight run instead of paying for a second one. Only a run
        # that has truly terminally failed or been lost earns a fresh id --
        # that is the deliberate-retry bump below, kept for when `re_adopt`
        # is not set.
        if retry and not re_adopt:
            attempt_tag = f"{attempt_tag}-r{retry}"
        # `derive_run_id`'s attempt dimension is exactly this: bump it and you
        # get a genuinely new run instead of re-adopting the old one. Without
        # it a bounded retry re-adopts the completed run it is retrying and
        # returns the same answer, which makes the bound decorative.
        run_id = derive_run_id(
            f"{self.development_id}:{stage.id}",
            attempt_tag,
            attempt=(1 if re_adopt else retry + 1),
        )
        role_input = self.role_input(stage, dispatch, run_id)
        input_path = write_json_durable(
            self.run_root / "stages" / f"{stage.id}-{attempt_tag}-input.json", role_input
        )
        prompt_path = input_path
        if self.prompts is not None:
            rendered = self.prompts.for_stage(
                stage.id,
                dispatch,
                run_id=run_id,
                actor_job_id=role_input["attempt_id"],
            )
            if rendered:
                prompt_path = self.run_root / "stages" / f"{stage.id}-{attempt_tag}-prompt.md"
                prompt_path.parent.mkdir(parents=True, exist_ok=True)
                prompt_path.write_text(rendered, encoding="utf-8")

        labels = {
            "development": self.development_id,
            "dispatcher": DISPATCHER,
            "stage": stage.id,
            "role": "dd-worker",
            "order": self.development_id,
            "attempt": str(int(dispatch.get("attempt", 1))),
            "dispatched_by": self.dispatched_by or DISPATCHER,
            **self.extra_labels,
        }
        model = self.models.get(stage.id)
        spec = AgentRunSpec(
            prompt="",
            role=stage_role(stage, self.roles),
            # agent-run resolves `--model` through a chain and wants the
            # runtime named alongside it.
            runtime=self.model_runtime if model else None,
            model=model,
            input_path=str(input_path),
            prompt_file=str(prompt_path),
            structured=True,
            write=self.writes(stage),
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
            # is reported as retryable on purpose: `run_in_flight` lets the
            # retry re-adopt the same run in flight (same run_id, continue
            # waiting) rather than paying for a second one. Reporting it as
            # terminal would strand a live run, and bumping the run id here
            # would abandon it -- the double-burn window this fence closes.
            # Any spend the run already made cannot be attributed to a
            # lifecycle it never completed, so it lands in `unknown`.
            self._record_unknown_spend(run_id, envelope=None)
            return StageOutcome(
                event=FAILURE_EVENT,
                failure_code=PROVIDER_UNAVAILABLE,
                detail=f"{stage.id} run {run_id} did not finish: {timeout}",
                run_in_flight=True,
            )

        if status.result is None or not status.ok:
            self._record_unknown_spend(run_id, envelope=status.result)
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
            # whole layering exists to avoid. Its spend is thereby unattributed.
            self._record_unknown_spend(run_id, envelope=status.result)
            return StageOutcome(
                event=FAILURE_EVENT,
                failure_code=INVALID_HANDOFF_SCHEMA,
                detail=str(fault),
            )

        outcome = self._outcome_from(stage, declared)
        self._record_cost_obs(stage, dispatch, outcome, envelope=status.result, run_id=run_id)
        return outcome

    def _record_cost_obs(
        self,
        stage: Stage,
        dispatch: Dispatch,
        outcome: StageOutcome,
        *,
        envelope: dict[str, Any] | None,
        run_id: str,
    ) -> None:
        """Emit the lifecycle fact -- and execution cost -- this stage owns.

        The walker reaches nothing outside its ports and places no opinion here;
        this actor is the component that actually owns the launch and review
        lifecycles, so this is where they are emitted. Idempotency is the data
        plane's: a replayed launch or a second review of the same phase+attempt
        is a no-op by stable identity key, so a retry or rework cannot
        double-count.

        The same run is also the production producer of the execution-cost
        facts rule 1 and the `unknown` bucket consume: the token spend the run
        reported is attributed to the lifecycle class the run served. This is
        the real token-accounting source, not a fixture total.
        """
        if self.cost_plane is None:
            return
        order_id = self.development_id
        tokens = _usage_tokens(envelope)
        if stage.id == implement_stage(self.lifecycle):
            self.cost_plane.record_launch(
                order_id=order_id,
                development_id=order_id,
                generation=int(dispatch.get("generation", 1)),
                seat=stage_role(stage, self.roles),
                model=str(self.models.get(stage.id) or ""),
            )
            if tokens > 0:
                self.cost_plane.record_execution_cost(
                    attribution=LAUNCH, order_id=order_id, tokens=tokens, event_id=run_id
                )
            return
        reviews = review_stages(self.lifecycle)
        if stage.id in reviews and outcome.event not in (FAILURE_EVENT, SPINE_EVENT):
            phase = "continuous" if stage.id == reviews[0] else "final"
            self.cost_plane.record_review(
                order_id=order_id,
                phase=phase,
                verdict=str(outcome.event).lower(),
                attempt=int(dispatch.get("attempt", 1)),
            )
            if tokens > 0:
                self.cost_plane.record_execution_cost(
                    attribution=REVIEW, order_id=order_id, tokens=tokens, event_id=run_id
                )

    def _record_unknown_spend(self, run_id: str, *, envelope: dict[str, Any] | None) -> None:
        """Account a run's spend as `unknown` when nothing attributes it.

        A run that failed, timed out, or answered in a shape we will not guess
        at still spent tokens on a lifecycle that never completed, so those
        tokens genuinely lack a lifecycle attribution. They are kept observable
        under `unknown` -- spec requirement 3 -- rather than dropped or
        silently relabelled as a known class. Unmeasured spend is left absent,
        never minted as a synthetic zero.
        """
        if self.cost_plane is None:
            return
        tokens = _usage_tokens(envelope)
        if tokens > 0:
            self.cost_plane.record_unknown_cost(
                order_id=self.development_id, tokens=tokens, event_id=run_id
            )

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
    # Where the verdict is written down. Without it the only record of who let
    # a development through lives in the run's history -- and the run ends.
    repo: Path | None = None
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

    def _seal_decision(
        self,
        dispatch: Dispatch,
        verdict: str,
        normalized: Any,
        decision: Any,
        ticket: GateTicket,
    ) -> None:
        """Write the verdict record to the gate's decision path.

        An APPROVE is sealed by the stage's own materialize step (the
        WorkspaceSealer commits it); a REJECT terminalises at the actor step,
        so its record would otherwise exist nowhere but the board. Sealing
        both is what lets the next generation's start read the rejecting
        verdict -- message id, decided_by, rationale -- as its authoritative
        rework input (wf-8d9737 rework contract A). The control plane commits
        the worktree copy when it starts that generation.
        """
        if self.repo is None:
            return
        write_json(
            self.repo,
            GATE_PATH.format(generation=dispatch.get("generation", 1)),
            {
                "development_id": self.development_id,
                "decision": verdict,
                "raw_decision": normalized.raw,
                "normalization_form": normalized.form,
                "decided_by": decision.decided_by,
                "decision_message_id": decision.message_id,
                "rationale": str(getattr(decision, "rationale", "") or ""),
                "question_note_id": ticket.question_note_id,
                "card_entity_id": ticket.card_entity_id,
                "output_commit": dispatch["input_commit"],
            },
        )

    def act(self, stage: Stage, dispatch: Dispatch) -> StageOutcome:
        ticket = self._ticket(dispatch)
        decision = self.board.decision_for(ticket)
        if decision is None:
            raise GatePending(ticket.to_dict())

        normalized = normalize_decision(decision.decision)
        if normalized is None:
            # Not a refusal and not an approval. Refusing to map it onto either
            # is the point: a gate that rounds an unrecognised verdict towards
            # "proceed" is not a gate. It is resumable -- a malformed verdict is
            # a fixable input, not a decision -- so the graph suspends and a
            # later resume re-reads the board for a proper verdict.
            raise StageRefused(
                f"gate decision {decision.decision!r} is not one of "
                f"{sorted(self.allowed_decisions)}; refusing to interpret it",
                code="GATE_VERDICT_UNRECOGNIZED",
                resumable=True,
                ticket=ticket.to_dict(),
            )
        verdict = normalized.verdict
        if verdict not in self.allowed_decisions:
            raise StageRefused(
                f"gate decision {verdict!r} is not one of "
                f"{sorted(self.allowed_decisions)}; refusing to interpret it",
                code="GATE_VERDICT_UNRECOGNIZED",
            )
        if verdict != self.approve:
            operator = decision.decided_by or "an operator"
            self._seal_decision(dispatch, verdict, normalized, decision, ticket)
            raise StageRefused(f"gate decision {verdict} by {operator}", code="GATE_REJECTED")

        record = {
            "stage": stage.id,
            "decision": verdict,
            "raw_decision": normalized.raw,
            "normalization_form": normalized.form,
            "decided_by": decision.decided_by,
            "decision_message_id": decision.message_id,
            "rationale": str(getattr(decision, "rationale", "") or ""),
            "question_note_id": ticket.question_note_id,
            "card_entity_id": ticket.card_entity_id,
            "output_commit": dispatch["input_commit"],
        }
        if self.repo is not None:
            # Sealed into the product tree, like the reviews and the merge
            # result. A gate whose verdict is not attributable afterwards is
            # a gate nobody can audit.
            self._seal_decision(dispatch, verdict, normalized, decision, ticket)
        return StageOutcome(
            event=SPINE_EVENT,
            receipt=record,
            produced=tuple(stage.produced_artifacts),
        )


__all__ = [
    "DEFAULT_ROLES",
    "GATE_APPROVE",
    "PRODUCT_ARTIFACT",
    "ROLE_STAGE",
    "AgentRunStageActor",
    "BoardGate",
    "implement_stage",
    "review_stages",
    "stage_role",
]
