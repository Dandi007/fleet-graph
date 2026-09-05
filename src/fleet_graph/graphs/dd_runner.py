"""Assembling and running one development through the dd pipeline.

Everything above this is a part; this is where the parts become a pipeline,
kept separate so `dd_pipeline.py` stays testable without any real
collaborator. It is the same split `graphs/runner.py` makes for the ronin line.

The four script stages are **injected, not assumed**. `configure`, `acceptance`
and `merger` are real work this repo has not written yet, and `human_gate` is
wired only when a board is supplied. Registering a placeholder that returned
success would be worse than leaving them out: the walker refuses an unregistered
script stage by name, which is a legible failure, whereas a placeholder would
report a stage as done that never ran.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from fleet_graph.dd.capability import CapabilityLock
from fleet_graph.dd.cost_obs import build_cost_plane
from fleet_graph.dd.dispatch import DevelopmentChain, StageDispatchBuilder
from fleet_graph.dd.git import run_git
from fleet_graph.dd.lifecycle import Lifecycle
from fleet_graph.dd.prompt import PluginPromptSource
from fleet_graph.executors.agent_run import AgentRunLauncher
from fleet_graph.graphs.dd_actors import AgentRunStageActor, BoardGate
from fleet_graph.graphs.dd_materializer import (
    MaterializationTarget,
    PluginMaterializer,
    StageMaterializers,
)
from fleet_graph.graphs.dd_pipeline import (
    Actor,
    PipelineBounds,
    PipelineDeps,
    build_dd_pipeline_graph,
    initial_state,
)
from fleet_graph.graphs.dd_replay import ReceiptReplayer, prior_generation_state_roots
from fleet_graph.graphs.dd_scripts import (
    GATE_PATH,
    AcceptanceStage,
    ConfigureStage,
    MergeStage,
    WorkspaceSealer,
)
from fleet_graph.state.run_artifacts import iso, write_json_durable

# The root input every stage requires and no stage produces.
SPEC_ARTIFACT = "spec"

# Run artifacts the control plane's read side assembles from. One name each,
# defined here where they are written.
EVENTS_FILE = "events.jsonl"
RESULT_FILE = "result.json"

# The stage run fence every remote retry budget defers to when the admission
# record declares no per-stage override -- the runner's own default fence.
DEFAULT_STAGE_FENCE_SECONDS = 3600

# Artifact kinds used to find the stage that owns each script default.
RUN_CONFIG = "run_config"
ACCEPTANCE_RESULT = "acceptance_result"
MERGE_RESULT = "merge_result"
GATE_DECISION = "gate_decision"

#: Rework contract B (wf-8d9737). The structured refusal code for a
#: generation that would open without a new implement dispatch -- a sealed
#: receipt replay marching a "new generation" straight past the work, as
#: measured on dev-fg-79d528db4375 g2 (re-seal replay: no implement prompt,
#: no new agent run, only acceptance.json changed).
REWORK_REPLAY_REFUSED = "REWORK_REPLAY_REFUSED"

#: Spec ⑮-b (wf-8d9737). The structured refusal code for a gate-rework launch
#: whose verdict is not bound to the board ``work.decision.v1`` it came from
#: (empty ``decision_message_id`` / ``decided_by`` / ``rationale``). Dispatch
#: face defense: even if an unbound mandate file reaches ``dd run``, the run
#: refuses instead of injecting an empty task book into an implement prompt.
REWORK_DECISION_UNBOUND = "REWORK_DECISION_UNBOUND"

#: The binding fields a gate-rework verdict must carry non-empty (spec ⑮-b).
REWORK_BINDING_FIELDS = ("decision_message_id", "decided_by", "rationale")


class ReworkDecisionUnbound(RuntimeError):
    """A gate-rework launch whose verdict has no board binding.

    Raised before the pipeline is built, so a refused generation never
    launches a stage, seals a receipt, or writes a result. The refusal is the
    observable failure the spec demands: an empty binding must never silently
    dispatch a "rework" whose task book says nothing.
    """

    def __init__(self, message: str, *, unbound: list[str]) -> None:
        super().__init__(f"{REWORK_DECISION_UNBOUND}: {message}")
        self.code = REWORK_DECISION_UNBOUND
        self.detail = message
        self.unbound = list(unbound)


class ReworkReplayRefused(RuntimeError):
    """A gate-rework generation the engine cannot assemble real work for.

    Raised before the pipeline is built, so a refused generation never
    launches a stage, seals a receipt, or writes a result. `missing` names
    what a real rework would have produced (a new implement prompt, a new
    agent run) and did not.
    """

    def __init__(self, message: str, *, missing: list[str]) -> None:
        super().__init__(f"{REWORK_REPLAY_REFUSED}: {message}")
        self.code = REWORK_REPLAY_REFUSED
        self.detail = message
        self.missing = list(missing)


@dataclass
class DevelopmentConfig:
    """One development, and where its work lives."""

    development_id: str
    workspace_path: Path
    state_root: Path
    run_root: Path
    remote_url: str
    remote_ref: str
    target_base_commit: str
    root_handoff_digest: str
    plugin_binding: Any
    head_commit: str
    generation: int = 1
    start_stage: str = "configure"
    roles: dict[str, str] = field(default_factory=dict)
    timeouts: dict[str, int] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)
    checkpoint_path: str = ":memory:"
    max_steps: int = 40
    max_rework: int = 6
    max_retries: int = 2
    verify_worktree_head: bool = True
    #: Auto re-prepare before a fresh attempt: when a previous attempt of a
    #: worktree-writing stage ended failed/contract_violation leaving a
    #: committed remnant (or a dirty tree), restore the worktree
    #: (``reset --hard`` to the attempt's input_commit + ``clean``) before
    #: dispatching the next attempt, and record ``event=re_prepare``. The
    #: engine does this so the retry never has to declare BLOCKED on its
    #: predecessor's remnant (spec: dd implement 重试自动 re-prepare).
    reprepare_worktree: bool = True
    run_config: dict[str, Any] = field(default_factory=dict)
    acceptance_timeout_seconds: int = 1800
    # The node_exporter textfile directory the cost-observability data plane
    # renders its per-development `cost-obs-<development>.prom` into, so the
    # recording rules the `deploy/prometheus` scrape config declares have
    # source facts to read. Empty means "not wired" -- the dispatch runs, it
    # just does not collect.
    cost_obs_dir: str = ""
    # The management execution cost of one order, `(order_id) -> float`, the
    # walker emits under the `management` attribution. None means manager spend
    # is not measured and is accounted absent -- never faked as a measured zero,
    # so the management/execution ratio reports absence rather than a definite 0%.
    management_cost: Any = None
    # Pushing to a durable ref is the one step here that cannot be undone.
    publish_merge: bool = False
    #: The bounded principal that dispatched this development (a line folder or
    #: a human subject), threaded through to the stage run labels as
    #: `dispatched_by`. Empty lets the actor fall back to the dispatcher. Never
    #: a run_id/uuid: the label must name a bounded subject, not an identity.
    dispatched_by: str = ""
    #: The gate REJECT verdict this generation reworks from (wf-8d9737 rework
    #: contract A), read by `dd run` from `--gate-reject-file` and injected
    #: into the implement prompt at the engine-side builder. Empty means the
    #: generation is not a gate rework: no anchor is ever injected and the
    #: receipt replay path behaves exactly as before.
    gate_reject: dict[str, Any] = field(default_factory=dict)
    #: R4: the order-private audit branch (refs/heads/dd/<dev>) the stage
    #: sealers publish to -- remote_ref is the merger's release branch. Empty
    #: falls back to remote_ref (the pre-R4 single-durable-ref layout).
    audit_ref: str = ""
    #: R4: the admission record file, so configure's first-step rebase can
    #: freeze the post-rebase head into it. Empty leaves records alone.
    record_path: str = ""

    @property
    def thread_id(self) -> str:
        return f"{self.development_id}:g{self.generation}"


def build_pipeline(
    config: DevelopmentConfig,
    *,
    scripts: dict[str, Actor] | None = None,
    materializers: dict[str, Any] | None = None,
    board: Any = None,
    gate_card_entity_id: str = "",
    launcher: Any = None,
    capability: CapabilityLock | None = None,
    clock: Any = None,
    observe: Any = None,
    replayer: Any = None,
) -> tuple[Any, PipelineDeps]:
    """Wire a development. Returns the graph and the deps it holds."""
    lifecycle = Lifecycle.load()

    # Rework contract B (wf-8d9737): a gate-rework generation must open with a
    # NEW implement dispatch -- a new prompt carrying the rejecting verdict
    # and a new agent run behind it. A receipt replayer marching the sealed
    # prefix of the rejected generation straight past implement would produce
    # a fake new generation (the dev-fg-79d528db4375 g2 shape), so a replayer
    # alongside a gate rework is refused outright...
    gate_rework = bool(config.gate_reject)
    if gate_rework:
        # Spec ⑮-b: the verdict must be bound to the board work.decision.v1
        # the gate consumed. An unbound mandate (empty decision_message_id /
        # decided_by / rationale) is refused at the dispatch face too -- the
        # control plane refuses at start; this keeps a hand-launched `dd run`
        # with a stale or hand-written unbound file from silently opening the
        # generation with an empty task book.
        unbound = [
            name
            for name in REWORK_BINDING_FIELDS
            if not str(config.gate_reject.get(name) or "").strip()
        ]
        if unbound:
            raise ReworkDecisionUnbound(
                f"development {config.development_id} g{config.generation} is a gate-rework "
                "generation whose verdict is not bound to its board work.decision.v1 "
                f"(empty: {', '.join(sorted(unbound))}); refusing to dispatch a rework "
                "with an empty binding",
                unbound=sorted(unbound),
            )
    if gate_rework and replayer is not None:
        raise ReworkReplayRefused(
            f"development {config.development_id} g{config.generation} is a gate-rework "
            "generation; a receipt replayer would re-enter the sealed prefix instead of "
            "dispatching a new implementer",
            missing=["new-implement-prompt", "new-agent-run"],
        )
    # R4: the ref each surface publishes to. The receipt chain's continuity
    # lives on the order-private audit branch (stage sealers); the merger's
    # CAS push lands on remote_ref -- the line's release branch. An empty
    # audit_ref means the legacy layout where both are remote_ref.
    publish_ref = config.audit_ref or config.remote_ref

    if replayer is None and config.generation > 1 and not gate_rework:
        # ...and a generation whose own tree seals a rejecting gate verdict
        # for the previous generation but whose launch carries no rework
        # mandate is refused too: replaying there would open exactly the fake
        # generation contract B forbids. The durable rework launch is the
        # control plane's, with --gate-reject-file set.
        prior = run_git(
            config.workspace_path,
            "show",
            f"HEAD:{GATE_PATH.format(generation=config.generation - 1)}",
        )
        if prior.returncode == 0:
            try:
                prior_decision = json.loads(prior.stdout)
            except ValueError:
                prior_decision = {}
            if str(prior_decision.get("decision") or "").strip().upper() == "REJECT":
                raise ReworkReplayRefused(
                    f"development {config.development_id} g{config.generation} starts after "
                    f"a gate REJECT of g{config.generation - 1} but carries no "
                    "--gate-reject-file; replaying the sealed prefix would open a new "
                    "generation with no new implement dispatch",
                    missing=["new-implement-prompt", "new-agent-run"],
                )
        # A restarted generation replays the receipt-sealed prefix of the
        # previous one instead of re-dispatching agents against work already
        # in the tree (F4). Generation 1 has nothing behind it, and a layout
        # the roots helper does not recognize replays nothing.
        prior_roots = prior_generation_state_roots(config.run_root, config.generation)
        if prior_roots:
            replayer = ReceiptReplayer(
                workspace=config.workspace_path,
                state_root=config.state_root,
                prior_state_roots=prior_roots,
                development_id=config.development_id,
                generation=config.generation,
                remote_url=config.remote_url,
                # The seals being replayed live on the audit branch
                # (publish_ref); verifying and re-publishing the chain must
                # target the same ref or a line dispatch's replay would land
                # receipts on the release branch and advance it past the
                # frozen base.
                remote_ref=publish_ref,
                lifecycle=lifecycle,
                run_config=dict(config.run_config or {}),
            )

    builder = StageDispatchBuilder(
        DevelopmentChain(
            development_id=config.development_id,
            workspace_path=str(config.workspace_path),
            target_base_commit=config.target_base_commit,
            root_handoff_digest=config.root_handoff_digest,
        )
    )

    # The one data plane the whole run shares. Its facts come from the actors
    # and scripts that own each lifecycle; constructing it once here means
    # launch, review, promotion and settlement all land in the same exposition
    # file the scrape config reads.
    cost_plane = build_cost_plane(config.cost_obs_dir or None, development_id=config.development_id)

    dispatcher = AgentRunStageActor(
        launcher=launcher or AgentRunLauncher(state_root=str(config.run_root / "agent-runs")),
        development_id=config.development_id,
        run_root=config.run_root,
        worktree_path=config.workspace_path,
        roles=config.roles,
        timeouts=config.timeouts,
        models=config.models,
        # The launch and review lifecycle facts are emitted by this actor; its
        # data plane is wired in for the stage producing each one.
        cost_plane=cost_plane,
        # The stage's prompt comes from the bundle the capability check
        # admitted, not from the role's own persona. See dd/prompt.py. A
        # gate-rework generation's prompt source carries the rejecting verdict
        # so the implement prompt is assembled with it (wf-8d9737 contract A).
        prompts=PluginPromptSource(
            binding=config.plugin_binding,
            builder=builder,
            worktree_path=str(config.workspace_path),
            acceptance_commands=list(config.run_config.get("acceptance_commands") or []),
            verify_worktree_head=config.verify_worktree_head,
            gate_reject=dict(config.gate_reject) if gate_rework else None,
        ),
        dispatched_by=config.dispatched_by,
        # Before a fresh dispatch, restore the worktree to the attempt's input
        # commit if a failed/contract_violation predecessor left a remnant, and
        # record the re-prepare into the run's event log. The re-adopt path
        # (a run still in flight) is excluded inside the actor.
        reprepare_worktree=config.reprepare_worktree,
        observe=observe,
    )
    # publish_ref, decided above, is what the plugin's sealers publish to.

    sealer = PluginMaterializer(
        builder=builder,
        binding=config.plugin_binding,
        target=MaterializationTarget(
            remote_url=config.remote_url,
            remote_ref=publish_ref,
            worktree=str(config.workspace_path),
            state_root=str(config.state_root),
        ),
        verify_worktree_head=config.verify_worktree_head,
    )

    # Defaults that make an assembled pipeline runnable. A caller-supplied
    # entry always wins; nothing here is mandatory.
    merge_stage = stage_producing(lifecycle, MERGE_RESULT)
    configure_stage = stage_producing(lifecycle, RUN_CONFIG)
    # R4 first-step rebase wiring: only a release-branch dispatch carries a
    # line branch to rebase onto; legacy durable refs leave the step unwired.
    line_ref = config.remote_ref if config.remote_ref.startswith("refs/heads/release/") else ""
    registered: dict[str, Actor] = {
        configure_stage: ConfigureStage(
            repo=config.workspace_path,
            run_config=config.run_config,
            # R4 first-step rebase: onto the line's release branch head, with
            # the post-rebase base frozen into the admission record.
            line_ref=line_ref,
            requested_base=config.target_base_commit,
            record_path=config.record_path if line_ref else "",
        ),
        stage_producing(lifecycle, ACCEPTANCE_RESULT): AcceptanceStage(
            repo=config.workspace_path,
            # The same declaration configure writes down. Acceptance runs this,
            # not whatever the worktree ended up containing.
            declared=[list(c) for c in (config.run_config.get("acceptance_commands") or [])],
            # The reconfigurable acceptance context (R1-c): setup runs first,
            # the env overlays both. Declared here so it has an execution
            # point -- a stored-but-unconsumed field is a dead mechanism.
            setup=[list(c) for c in (config.run_config.get("setup_commands") or [])],
            env=dict(config.run_config.get("acceptance_env") or {}),
            timeout_seconds=config.acceptance_timeout_seconds,
        ),
        merge_stage: MergeStage(
            repo=config.workspace_path,
            remote_url=config.remote_url,
            target_ref=config.remote_ref,
            publish=config.publish_merge,
            # The promotion (merge) lifecycle fact is emitted by this stage.
            cost_plane=cost_plane,
            # Egress: the CAS publish retries transport-class failures under
            # the stage's run fence, landing one evidence line per attempt.
            fence_seconds=float(config.timeouts.get(merge_stage, DEFAULT_STAGE_FENCE_SECONDS)),
            evidence=observe,
        ),
    }
    if board is not None and gate_card_entity_id:
        # The one stage with no default. An assembly that approved on its own
        # would be an agent casting a human's verdict; a caller who wants a
        # different policy registers their own actor, deliberately.
        registered[lifecycle_gate_stage(lifecycle)] = BoardGate(
            board=board,
            card_entity_id=gate_card_entity_id,
            development_id=config.development_id,
            repo=config.workspace_path,
        )
    registered.update(scripts or {})

    deps = PipelineDeps(
        lifecycle=lifecycle,
        dispatcher=dispatcher,
        scripts=registered,
        replayer=replayer,
        # The plugin ships two sealers, and the contract says which stages
        # own their outputs. That is a narrower set than the dispatch schema's
        # stage enum -- `acceptance` is dispatched but not sealed here. The
        # rest (configure, the gate, the merge, acceptance) have no sealer in
        # this repo yet, and an unrouted stage refuses rather than passing a
        # commit through.
        materializer=StageMaterializers(
            by_stage={
                # Everything the plugin does not seal commits its own output.
                # R4: those seals publish to the audit branch (publish_ref) --
                # a line dispatch's remote_ref is the release branch, and a
                # seal landing there would stack machine parts
                # (.dev-dispatch/, .dd-evidence/) onto the line branch and
                # advance it past the frozen base, refusing every merger CAS
                # push with RELEASE_HEAD_ADVANCED.
                **{
                    name: WorkspaceSealer(
                        repo=config.workspace_path,
                        remote_url=config.remote_url,
                        remote_ref=publish_ref,
                        # Egress: the durable-ref publish retries
                        # transport-class failures inside the stage's run
                        # fence; every attempt lands one evidence line.
                        fence_seconds=float(config.timeouts.get(name, DEFAULT_STAGE_FENCE_SECONDS)),
                        evidence=observe,
                    )
                    for name in lifecycle.stages
                    if name not in sealer.sealed_stages
                },
                **{stage: sealer for stage in sealer.sealed_stages},
                **(materializers or {}),
            }
        ),
        capability=capability if capability is not None else CapabilityLock.load(),
        observe=observe,
        # The settlement fact and the absent-lifecycle accounting the walker
        # owns; launch/review/promotion are emitted by their actors.
        cost_plane=cost_plane,
        management_cost=config.management_cost,
        bounds=PipelineBounds(
            max_steps=config.max_steps,
            max_rework=config.max_rework,
            max_retries=config.max_retries,
        ),
        clock=clock or time.time,
    )
    return build_dd_pipeline_graph(deps), deps


def stage_producing(lifecycle: Lifecycle, artifact: str) -> str:
    """The stage that produces this artifact, per the contract.

    Cheaper than writing four stage names down again, and it stays right if
    the contract moves an artifact to a different stage.
    """
    producers = lifecycle.artifact_producers.get(artifact, ())
    if len(producers) != 1:
        raise ValueError(f"{artifact} has producers {producers!r}; expected exactly one")
    return producers[0]


def lifecycle_gate_stage(lifecycle: Lifecycle) -> str:
    """The stage whose product is the human decision."""
    return stage_producing(lifecycle, GATE_DECISION)


def run_pipeline(
    config: DevelopmentConfig,
    *,
    scripts: dict[str, Actor] | None = None,
    materializers: dict[str, Any] | None = None,
    board: Any = None,
    gate_card_entity_id: str = "",
    launcher: Any = None,
    clock: Any = None,
    resume: bool = False,
) -> dict[str, Any]:
    """Run one development, or resume the one this thread already suspended.

    `resume=True` hands the graph no input at all. That is deliberate: the
    gate re-reads the board itself, so resuming carries no verdict and cannot
    be used to cast one. It only says "look again".

    Every history entry is also appended to `<run_root>/events.jsonl`, and the
    final summary is written to `<run_root>/result.json`, durably, before it
    is returned. Those two files -- run artifacts, not a database -- are what
    the control plane's read side assembles `events`/`get` from after this
    process is gone.
    """
    now = clock or time.time
    events_path = config.run_root / EVENTS_FILE

    def persist_event(entry: dict[str, Any]) -> None:
        try:
            events_path.parent.mkdir(parents=True, exist_ok=True)
            with events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"at": iso(now()), **entry}, ensure_ascii=False) + "\n")
                handle.flush()
        except OSError:
            # Observability must not fail the work it observes.
            pass

    graph, deps = build_pipeline(
        config,
        scripts=scripts,
        materializers=materializers,
        board=board,
        gate_card_entity_id=gate_card_entity_id,
        launcher=launcher,
        clock=clock,
        observe=persist_event,
    )

    # A resume rebuilds the pipeline and therefore a fresh in-memory data
    # plane; the checkpointer replays only the interrupted gate node, so the
    # implement and review stages -- already sealed -- never re-emit their
    # launch/review facts. Re-read this development's own scrape file so the
    # final render keeps those pre-suspension facts instead of overwriting
    # them with promotion + settlement + management alone.
    #
    # A restarted generation (control plane's normal exit from a non-complete
    # terminal) rebuilds the pipeline too, and its replayer enters every
    # receipt-sealed stage with "no actor runs" -- so implement and review
    # never re-emit launch/review there either. Rehydrate for that path as
    # well: a generation n+1 run whose build installed a receipt replayer must
    # keep generation n's launch/review facts, or it overwrites the shared
    # `cost-obs-<development>.prom` with promotion + settlement + management
    # alone. Idempotent in both directions: a rehydrated fact re-emitted on a
    # later terminal is a no-op by stable identity.
    if deps.cost_plane is not None and (resume or deps.replayer is not None):
        deps.cost_plane.rehydrate_from_file()

    with SqliteSaver.from_conn_string(config.checkpoint_path) as saver:
        compiled = graph.compile(checkpointer=saver)
        start = (
            None
            if resume
            else initial_state(
                development_id=config.development_id,
                stage=config.start_stage,
                head_commit=config.head_commit,
                artifacts={SPEC_ARTIFACT: config.head_commit},
                generation=config.generation,
                attempt_started_at=iso(now()),
            )
        )
        state = compiled.invoke(
            start,
            config={
                "configurable": {"thread_id": config.thread_id},
                # The bounds are the real limit; this is a runaway backstop.
                "recursion_limit": config.max_steps * 4 + 20,
            },
        )

    # The order is settled (or accounted absent) inside the walk; render the
    # exposition only now, once, so the scrape file reflects the finished run
    # rather than each intermediate stage.
    if deps.cost_plane is not None and deps.cost_plane.exposition_dir is not None:
        deps.cost_plane.write_exposition()

    result = {
        "development_id": config.development_id,
        "generation": config.generation,
        "terminal": state.get("terminal"),
        "terminal_reason": state.get("terminal_reason"),
        # One mechanical cause and the failing collaborator's own words. The
        # control plane's failure classification reads these two fields, so a
        # run that failed keeps its cause and its raw error past the process.
        "terminal_code": state.get("terminal_code", ""),
        "terminal_detail": state.get("last_failure_detail", "") if state.get("terminal") else "",
        "fault": bool(state.get("fault", False)),
        "stage": state.get("stage"),
        "steps": state.get("steps", 0),
        "head_commit": state.get("head_commit"),
        # Which question is holding the line. Without it the operator knows
        # only that something suspended, not what to answer.
        "awaiting": awaiting_decision(state),
        # A resumable refusal is a suspension with a reason: the gate refuses to
        # interpret the board's answer, and stays resumable rather than ending.
        "gate_refused": gate_refusal(state),
        "history": state.get("history", []),
    }
    write_json_durable(config.run_root / RESULT_FILE, {**result, "written_at": iso(now())})
    return result


def awaiting_decision(state: dict[str, Any]) -> dict[str, Any] | None:
    """The gate ticket the graph suspended on, if it suspended on one."""
    for interrupt in state.get("__interrupt__") or ():
        value = getattr(interrupt, "value", None)
        if isinstance(value, dict) and "awaiting_decision" in value:
            return dict(value["awaiting_decision"])
    return None


def gate_refusal(state: dict[str, Any]) -> dict[str, Any] | None:
    """The gate's fail-closed refusal, if the graph suspended on one.

    A resumable refusal (the gate refusing to interpret an unrecognized
    verdict) suspends with this payload beside the awaiting ticket. It never
    proceeds and never ends: the reason and the one mechanical code travel here
    so the read side can say "refused, still waiting" rather than only "waiting".
    """
    for interrupt in state.get("__interrupt__") or ():
        value = getattr(interrupt, "value", None)
        if isinstance(value, dict) and "refused" in value:
            return dict(value["refused"])
    return None


__all__ = [
    "EVENTS_FILE",
    "RESULT_FILE",
    "REWORK_BINDING_FIELDS",
    "REWORK_DECISION_UNBOUND",
    "REWORK_REPLAY_REFUSED",
    "SPEC_ARTIFACT",
    "DevelopmentConfig",
    "ReworkDecisionUnbound",
    "ReworkReplayRefused",
    "awaiting_decision",
    "build_pipeline",
    "gate_refusal",
    "lifecycle_gate_stage",
    "run_pipeline",
    "stage_producing",
]
