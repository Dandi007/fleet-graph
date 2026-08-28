"""The acceptance scenario shared by the executable fixture and its tests.

The scenario drives one real dev-dispatch launch through the actual DD
lifecycle machinery -- the pipeline walker, the stage actors and the script
stages -- with only the external collaborators (the agent-run binary, the
plugin sealer and the work board) substituted for deterministic stand-ins.
Whatever the walker and actors emit for the launch, review, promotion and
settlement lifecycles is therefore real, not hand-minted: the facts come out of
the same code that runs a production development.

The only facts emitted directly rather than by a DD component are the token
*spend* records (``record_execution_cost`` / ``record_unknown_cost``): those
belong to token accounting, which has no lifecycle component in this
repository, so the scenario emits them as that producer. The lifecycle facts
the recording rules number two through five depend on are produced by the DD
machinery end to end.

Keeping this importable means the executable (``scripts/cost_obs_acceptance.py``)
and the pytest acceptance test assert the exact same facts instead of two
near-copies.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fleet_graph.bus.board import Decision, GateTicket
from fleet_graph.cost_obs import RECORDING_RULES, CostDataPlane, query
from fleet_graph.cost_obs.exposition import parse
from fleet_graph.cost_obs.rules import COST_METRIC, PRESENCE_METRIC
from fleet_graph.dd.lifecycle import Lifecycle
from fleet_graph.executors.agent_run import RunStatus, RunTicket
from fleet_graph.graphs.dd_actors import AgentRunStageActor, BoardGate, review_stages
from fleet_graph.graphs.dd_pipeline import (
    FAILURE_EVENT,
    SPINE_EVENT,
    Dispatch,
    PipelineDeps,
    Sealed,
    StageOutcome,
    build_dd_pipeline_graph,
    initial_state,
)
from fleet_graph.graphs.dd_scripts import AcceptanceStage, ConfigureStage, MergeStage

#: The completed order: a development driven to a full settle.
LAUNCH_ORDER = "dev-fg-cost-obs-complete"
#: The open order: a development launched but whose later lifecycles never
#: produced (the gate rejects it), so its absence is accounted explicitly.
ORPHAN_ORDER = "dev-fg-cost-obs-orphan"

SPEC_COMMIT = "0" * 40

#: The total token spend the scenario emits: 10+20+30+5+5 known plus 7 unknown.
EXPECTED_TOTAL = 77.0
EXPECTED_MANAGEMENT = 10.0


def _producer(lifecycle: Lifecycle, artifact: str) -> str:
    producers = lifecycle.artifact_producers.get(artifact, ())
    return producers[0] if len(producers) == 1 else ""


class _RecordingLauncher:
    """Answers agent-run for every llm stage with a canned success.

    The real binary is an external process; what the lifecycle cares about is
    the stage's declared result, so a deterministic stand-in is sufficient and
    keeps the fixture from needing a live agent-runtime install.
    """

    def __init__(self, review_ids: tuple[str, ...]) -> None:
        self._review_ids = frozenset(review_ids)
        self._stage_by_run: dict[str, str] = {}

    def launch(self, spec: Any, run_id: str) -> RunTicket:
        self._stage_by_run[run_id] = str(spec.labels["stage"])
        return RunTicket(run_id, f"/tmp/{run_id}", None)

    def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
        stage = self._stage_by_run[ticket.run_id]
        if stage in self._review_ids:
            declared: dict[str, Any] = {"verdict": "APPROVE", "findings": []}
        else:
            declared = {
                "actor_job_id": "job-1",
                "input_commit": SPEC_COMMIT,
                "outcome": "APPLIED",
                "work_head_commit": "1" * 40,
                "verification_record": {"verification_commands": []},
            }
        return RunStatus("succeeded", {"structured_result": declared})


class _DecidedBoard:
    """A work board with one pre-loaded verdict. Approve or reject, that is all."""

    def __init__(self, decision: str) -> None:
        self._decision = decision

    def ask(self, *, card_entity_id: str, question: str, idempotency_key: str) -> GateTicket:
        return GateTicket(question_note_id="note-1", card_entity_id=card_entity_id)

    def decision_for(self, ticket: GateTicket) -> Decision | None:
        return Decision(
            message_id="msg-decision",
            decision=self._decision,
            decided_by="fixture",
            question="",
            rationale="",
            card_entity_id="card-1",
            raw={},
        )


class _Sealer:
    """Seals each stage to a deterministic commit and an honest receipt.

    A real plugin sealer is external; this stand-in attests to the commit it
    "wrote", which is exactly what makes the walker's forward-chain and
    event bindings meaningful to check.
    """

    def materialize(self, stage: Any, dispatch: Dispatch, outcome: StageOutcome) -> Sealed:
        commit = f"{stage.id}-g{dispatch['generation']}-a{dispatch['attempt']}"
        receipt: dict[str, Any] = {"output_commit": commit}
        if outcome.event not in (SPINE_EVENT, FAILURE_EVENT):
            receipt["verdict"] = outcome.event
        return Sealed(commit=commit, receipt=receipt, produced=tuple(stage.produced_artifacts))


def _run_development(
    plane: CostDataPlane,
    lifecycle: Lifecycle,
    development_id: str,
    *,
    approve: bool,
    repo: Path,
    run_root: Path,
) -> dict[str, Any]:
    """Drive one development through the real walker, actors and scripts."""
    dispatcher = AgentRunStageActor(
        launcher=_RecordingLauncher(review_stages(lifecycle)),  # type: ignore[arg-type]
        development_id=development_id,
        run_root=run_root,
        cost_plane=plane,
    )
    gate = BoardGate(
        board=_DecidedBoard("APPROVE" if approve else "REJECT"),  # type: ignore[arg-type]
        card_entity_id="card-1",
        development_id=development_id,
        repo=repo,
    )
    scripts: dict[str, Any] = {
        _producer(lifecycle, "run_config"): ConfigureStage(repo=repo, run_config={}),
        _producer(lifecycle, "acceptance_result"): AcceptanceStage(
            repo=repo, declared=[], setup=[], env={}
        ),
        _producer(lifecycle, "merge_result"): MergeStage(
            repo=repo,
            remote_url="",
            target_ref="refs/heads/main",
            publish=False,
            cost_plane=plane,
        ),
        _producer(lifecycle, "gate_decision"): gate,
    }
    deps = PipelineDeps(
        lifecycle=lifecycle,
        dispatcher=dispatcher,
        scripts=scripts,
        materializer=_Sealer(),
        cost_plane=plane,
    )
    graph = build_dd_pipeline_graph(deps).compile()
    state = graph.invoke(
        initial_state(
            development_id=development_id,
            stage="configure",
            head_commit=SPEC_COMMIT,
            artifacts={"spec": SPEC_COMMIT},
        ),
        config={"recursion_limit": 200},
    )
    return state


def run_acceptance_scenario(exposition_dir: Path) -> dict[str, object]:
    """Drive a settle and an open order through the DD machinery, then query.

    Returns a plain dict of named results so both the script and pytest can
    assert on the same facts without re-implementing the scenario.
    """
    plane = CostDataPlane(exposition_dir=exposition_dir)
    lifecycle = Lifecycle.load()

    with tempfile.TemporaryDirectory(prefix="cost-obs-repo-") as tmp:
        repo = Path(tmp)
        run_root = Path(tmp) / "runs"
        settled = _run_development(
            plane, lifecycle, LAUNCH_ORDER, approve=True, repo=repo, run_root=run_root
        )
        orphan = _run_development(
            plane, lifecycle, ORPHAN_ORDER, approve=False, repo=repo, run_root=run_root
        )
    assert settled.get("terminal") == "complete", settled
    assert orphan.get("terminal") == "refused", orphan

    # Token spend: the token-accounting producer. These are the execution-cost
    # facts the management ratio divides and the unknown bucket records; they
    # have no DD lifecycle component and are emitted here as that producer.
    plane.record_execution_cost(attribution="management", tokens=10, event_id="run-1")
    plane.record_execution_cost(attribution="launch", tokens=20, event_id="run-1")
    plane.record_execution_cost(attribution="review", tokens=30, event_id="run-1")
    plane.record_execution_cost(attribution="promotion", tokens=5, event_id="run-1")
    plane.record_execution_cost(attribution="settlement", tokens=5, event_id="run-1")
    plane.record_unknown_cost(tokens=7, event_id="run-1")

    # Scrape wiring: render to a file, then read it back and query the bytes.
    exposition_path = plane.write_exposition()
    scraped = parse(exposition_path.read_text(encoding="utf-8"))

    per_rule: dict[str, bool] = {}
    for rule in RECORDING_RULES:
        per_rule[rule.name] = len(query(rule.expr, scraped)) > 0

    management_ratio = query(RECORDING_RULES[0].expr, scraped)
    unknown = query(f'{COST_METRIC}{{attribution="unknown"}}', scraped)
    missing_present = query(
        f'{PRESENCE_METRIC}{{order_id="{ORPHAN_ORDER}",lifecycle="settlement"}}', scraped
    )
    present_served = query(
        f'{PRESENCE_METRIC}{{order_id="{LAUNCH_ORDER}",lifecycle="settlement"}}', scraped
    )
    reconciliation = query(RECORDING_RULES[4].expr, scraped)

    # Exact-once after a replay/retry: re-run the launch and settlement
    # lifecycles for the already-settled order. The walker and the implement
    # actor call exactly these two recorders; the data plane's stable identity
    # keys make both a no-op, so the reconciliation ratio must stay exactly 1.
    replayed_launch = plane.record_launch(order_id=LAUNCH_ORDER, development_id=LAUNCH_ORDER)
    replayed_settlement = plane.record_settlement(order_id=LAUNCH_ORDER)
    report = plane.reconcile()
    rerun_reconciliation = query(RECORDING_RULES[4].expr, plane.samples())

    return {
        "rules_non_empty": all(per_rule.values()),
        "per_rule": per_rule,
        "management_ratio": [s.value for s in management_ratio],
        "unknown_tokens": [s.value for s in unknown],
        "missing_visible": [s.value for s in missing_present],
        "present_visible": [s.value for s in present_served],
        "reconciliation": [s.value for s in reconciliation],
        "reconciled_orders": report.orders,
        "exact_once": report.exact_once,
        "replay_noop": (replayed_launch is False, replayed_settlement is False),
        "rerun_reconciliation": [s.value for s in rerun_reconciliation],
    }


__all__ = [
    "EXPECTED_MANAGEMENT",
    "EXPECTED_TOTAL",
    "LAUNCH_ORDER",
    "ORPHAN_ORDER",
    "run_acceptance_scenario",
]
