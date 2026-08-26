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

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from fleet_graph.dd.capability import CapabilityLock
from fleet_graph.dd.dispatch import DevelopmentChain, StageDispatchBuilder
from fleet_graph.dd.lifecycle import Lifecycle
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
from fleet_graph.graphs.dd_scripts import (
    AcceptanceStage,
    ConfigureStage,
    MergeStage,
    WorkspaceSealer,
)
from fleet_graph.state.run_artifacts import iso

# The root input every stage requires and no stage produces.
SPEC_ARTIFACT = "spec"

# Artifact kinds used to find the stage that owns each script default.
RUN_CONFIG = "run_config"
ACCEPTANCE_RESULT = "acceptance_result"
MERGE_RESULT = "merge_result"
GATE_DECISION = "gate_decision"


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
    checkpoint_path: str = ":memory:"
    max_steps: int = 40
    max_rework: int = 6
    max_retries: int = 2
    verify_worktree_head: bool = True
    run_config: dict[str, Any] = field(default_factory=dict)
    acceptance_timeout_seconds: int = 1800
    # Pushing to a durable ref is the one step here that cannot be undone.
    publish_merge: bool = False

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
) -> tuple[Any, PipelineDeps]:
    """Wire a development. Returns the graph and the deps it holds."""
    lifecycle = Lifecycle.load()

    dispatcher = AgentRunStageActor(
        launcher=launcher or AgentRunLauncher(state_root=str(config.run_root / "agent-runs")),
        development_id=config.development_id,
        run_root=config.run_root,
        worktree_path=config.workspace_path,
        roles=config.roles,
        timeouts=config.timeouts,
    )

    builder = StageDispatchBuilder(
        DevelopmentChain(
            development_id=config.development_id,
            workspace_path=str(config.workspace_path),
            target_base_commit=config.target_base_commit,
            root_handoff_digest=config.root_handoff_digest,
        )
    )
    sealer = PluginMaterializer(
        builder=builder,
        binding=config.plugin_binding,
        target=MaterializationTarget(
            remote_url=config.remote_url,
            remote_ref=config.remote_ref,
            worktree=str(config.workspace_path),
            state_root=str(config.state_root),
        ),
        verify_worktree_head=config.verify_worktree_head,
    )

    # Defaults that make an assembled pipeline runnable. A caller-supplied
    # entry always wins; nothing here is mandatory.
    registered: dict[str, Actor] = {
        stage_producing(lifecycle, RUN_CONFIG): ConfigureStage(
            repo=config.workspace_path, run_config=config.run_config
        ),
        stage_producing(lifecycle, ACCEPTANCE_RESULT): AcceptanceStage(
            repo=config.workspace_path,
            timeout_seconds=config.acceptance_timeout_seconds,
        ),
        stage_producing(lifecycle, MERGE_RESULT): MergeStage(
            repo=config.workspace_path,
            remote_url=config.remote_url,
            target_ref=config.remote_ref,
            publish=config.publish_merge,
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
        )
    registered.update(scripts or {})

    deps = PipelineDeps(
        lifecycle=lifecycle,
        dispatcher=dispatcher,
        scripts=registered,
        # The plugin ships two sealers, and the contract says which stages
        # own their outputs. That is a narrower set than the dispatch schema's
        # stage enum -- `acceptance` is dispatched but not sealed here. The
        # rest (configure, the gate, the merge, acceptance) have no sealer in
        # this repo yet, and an unrouted stage refuses rather than passing a
        # commit through.
        materializer=StageMaterializers(
            by_stage={
                # Everything the plugin does not seal commits its own output.
                **{
                    name: WorkspaceSealer(repo=config.workspace_path)
                    for name in lifecycle.stages
                    if name not in sealer.sealed_stages
                },
                **{stage: sealer for stage in sealer.sealed_stages},
                **(materializers or {}),
            }
        ),
        capability=capability if capability is not None else CapabilityLock.load(),
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
) -> dict[str, Any]:
    graph, _deps = build_pipeline(
        config,
        scripts=scripts,
        materializers=materializers,
        board=board,
        gate_card_entity_id=gate_card_entity_id,
        launcher=launcher,
        clock=clock,
    )
    now = clock or time.time

    with SqliteSaver.from_conn_string(config.checkpoint_path) as saver:
        compiled = graph.compile(checkpointer=saver)
        state = compiled.invoke(
            initial_state(
                development_id=config.development_id,
                stage=config.start_stage,
                head_commit=config.head_commit,
                artifacts={SPEC_ARTIFACT: config.head_commit},
                generation=config.generation,
                attempt_started_at=iso(now()),
            ),
            config={
                "configurable": {"thread_id": config.thread_id},
                # The bounds are the real limit; this is a runaway backstop.
                "recursion_limit": config.max_steps * 4 + 20,
            },
        )

    return {
        "development_id": config.development_id,
        "terminal": state.get("terminal"),
        "terminal_reason": state.get("terminal_reason"),
        "fault": bool(state.get("fault", False)),
        "stage": state.get("stage"),
        "steps": state.get("steps", 0),
        "head_commit": state.get("head_commit"),
        "history": state.get("history", []),
    }


__all__ = [
    "SPEC_ARTIFACT",
    "DevelopmentConfig",
    "build_pipeline",
    "lifecycle_gate_stage",
    "run_pipeline",
    "stage_producing",
]
