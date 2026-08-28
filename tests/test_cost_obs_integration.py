"""The cost-observability data plane, wired into the DD lifecycle components.

The acceptance scenario (tests/test_cost_obs_acceptance.py) drives the whole
pipeline end to end; these tests pin each responsible component's contribution
directly, so a fact that stops flowing has a specific culprit rather than one
big red scenario. Each maps to one of the review findings:

- the launch and review facts come from the implement/review dispatcher;
- the promotion fact comes from the merge stage;
- the settlement and absence accounting come from the walker;
- the `dd` package imports the data plane through `dd/cost_obs.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fleet_graph.cost_obs import CostDataPlane, query
from fleet_graph.cost_obs.exposition import parse
from fleet_graph.cost_obs.rules import (
    COST_METRIC,
    LAUNCH_METRIC,
    PRESENCE_METRIC,
    PROMOTION_METRIC,
    RECORDING_RULES,
    REVIEW_METRIC,
    SETTLEMENT_METRIC,
)
from fleet_graph.dd.cost_obs import (
    COST_OBS_DIR_ENV,
    build_cost_plane,
    cost_obs_exposition_dir,
    exposition_filename_for,
)
from fleet_graph.dd.lifecycle import Lifecycle
from fleet_graph.executors.agent_run import RunStatus, RunTicket
from fleet_graph.graphs.dd_actors import (
    AgentRunStageActor,
    BoardGate,
    implement_stage,
    review_stages,
)
from fleet_graph.graphs.dd_pipeline import (
    Dispatch,
    PipelineDeps,
    Sealed,
    StageOutcome,
    build_dd_pipeline_graph,
    initial_state,
)
from fleet_graph.graphs.dd_scripts import AcceptanceStage, ConfigureStage, MergeStage

LIFECYCLE = Lifecycle.load()
COMMIT = "a" * 40


def dispatch_for(development_id: str, attempt: int = 1) -> Dispatch:
    return {
        "development_id": development_id,
        "stage": "implement",
        "mode": "initial",
        "generation": 1,
        "attempt": attempt,
        "input_commit": COMMIT,
        "required_artifacts": ["spec", "run_config"],
        "produced_artifacts": ["implementation_evidence", "product_code"],
        "contract_version": LIFECYCLE.contract_version,
    }


class Launcher:
    """A canned agent-run that answers implement and review stages by stage id."""

    def __init__(self) -> None:
        self._stage_by_run: dict[str, str] = {}

    def launch(self, spec: Any, run_id: str) -> RunTicket:
        self._stage_by_run[run_id] = str(spec.labels["stage"])
        return RunTicket(run_id, f"/tmp/{run_id}", None)

    def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
        stage = self._stage_by_run[ticket.run_id]
        if stage in review_stages(LIFECYCLE):
            declared: dict[str, Any] = {"verdict": "APPROVE", "findings": []}
        else:
            declared = {
                "actor_job_id": "job-1",
                "input_commit": COMMIT,
                "outcome": "APPLIED",
                "work_head_commit": "b" * 40,
                "verification_record": {"verification_commands": []},
            }
        return RunStatus("succeeded", {"structured_result": declared})


def make_actor(
    plane: CostDataPlane, tmp_path: Path, development_id: str = "dev-1"
) -> AgentRunStageActor:
    return AgentRunStageActor(
        launcher=Launcher(),  # type: ignore[arg-type]
        development_id=development_id,
        run_root=tmp_path,
        cost_plane=plane,
    )


def metric_values(plane: CostDataPlane, metric: str) -> list[float]:
    return [s.value for s in plane.samples() if s.name == metric]


class TestLaunchAndReviewFactsComeFromTheDispatcher:
    def test_the_implement_stage_emits_a_launch_fact(self, tmp_path: Path) -> None:
        plane = CostDataPlane()
        stage = LIFECYCLE.stages[implement_stage(LIFECYCLE) or "implement"]
        make_actor(plane, tmp_path).act(stage, dispatch_for("dev-1"))

        assert metric_values(plane, LAUNCH_METRIC) == [1.0]
        launch = next(s for s in plane.samples() if s.name == LAUNCH_METRIC)
        assert launch.label_map()["order_id"] == "dev-1"

    def test_replaying_the_launch_is_a_noop(self, tmp_path: Path) -> None:
        plane = CostDataPlane()
        stage = LIFECYCLE.stages[implement_stage(LIFECYCLE) or "implement"]
        actor = make_actor(plane, tmp_path)
        actor.act(stage, dispatch_for("dev-1"))
        actor.act(stage, dispatch_for("dev-1"))

        assert metric_values(plane, LAUNCH_METRIC) == [1.0]

    def test_the_review_stages_emit_review_facts(self, tmp_path: Path) -> None:
        plane = CostDataPlane()
        actor = make_actor(plane, tmp_path)
        for stage_id in review_stages(LIFECYCLE):
            actor.act(LIFECYCLE.stages[stage_id], dispatch_for("dev-1"))

        reviews = [s for s in plane.samples() if s.name == REVIEW_METRIC]
        assert {s.label_map()["phase"] for s in reviews} == {"continuous", "final"}
        assert {s.value for s in reviews} == {1.0}

    def test_a_failed_run_emits_no_lifecycle_fact(self, tmp_path: Path) -> None:
        plane = CostDataPlane()
        stage = LIFECYCLE.stages[implement_stage(LIFECYCLE) or "implement"]

        class FailingLauncher(Launcher):
            def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
                return RunStatus("failed", {"exit_code": 1})

        actor = AgentRunStageActor(
            launcher=FailingLauncher(),  # type: ignore[arg-type]
            development_id="dev-1",
            run_root=tmp_path,
            cost_plane=plane,
        )
        actor.act(stage, dispatch_for("dev-1"))
        assert metric_values(plane, LAUNCH_METRIC) == []


class TestPromotionFactComesFromTheMergeStage:
    def test_the_merge_stage_emits_a_promotion_fact(self, tmp_path: Path) -> None:
        plane = CostDataPlane()
        stage = LIFECYCLE.stages["merger"]
        MergeStage(
            repo=tmp_path,
            remote_url="",
            target_ref="refs/heads/main",
            publish=False,
            cost_plane=plane,
        ).act(stage, {**dispatch_for("dev-1"), "stage": "merger"})

        promotions = [s for s in plane.samples() if s.name == PROMOTION_METRIC]
        assert [s.value for s in promotions] == [1.0]
        assert promotions[0].label_map()["order_id"] == "dev-1"


class _ApprovingBoard:
    def ask(self, *, card_entity_id: str, question: str, idempotency_key: str) -> Any:
        return type("Ticket", (), {"question_note_id": "note-1", "card_entity_id": "card-1"})()

    def decision_for(self, ticket: Any) -> Any:
        return type(
            "Decision",
            (),
            {
                "message_id": "m",
                "decision": "APPROVE",
                "decided_by": "fixture",
                "question": "",
                "rationale": "",
                "card_entity_id": "card-1",
                "raw": {},
            },
        )()


class _Sealer:
    def materialize(self, stage: Any, dispatch: Dispatch, outcome: StageOutcome) -> Sealed:
        commit = f"{stage.id}-g{dispatch['generation']}-a{dispatch['attempt']}"
        receipt: dict[str, Any] = {"output_commit": commit}
        if outcome.event not in ("success", "failed"):
            receipt["verdict"] = outcome.event
        return Sealed(commit=commit, receipt=receipt, produced=tuple(stage.produced_artifacts))


def _producer(artifact: str) -> str:
    return LIFECYCLE.artifact_producers[artifact][0]


def run_settle(
    plane: CostDataPlane,
    tmp_path: Path,
    development_id: str = "dev-1",
    management_cost: Any = None,
) -> dict[str, Any]:
    scripts: dict[str, Any] = {
        _producer("run_config"): ConfigureStage(repo=tmp_path, run_config={}),
        _producer("acceptance_result"): AcceptanceStage(repo=tmp_path, declared=[], setup=[]),
        _producer("merge_result"): MergeStage(
            repo=tmp_path, remote_url="", target_ref="refs/heads/main", cost_plane=plane
        ),
        _producer("gate_decision"): BoardGate(
            board=_ApprovingBoard(),  # type: ignore[arg-type]
            card_entity_id="card-1",
            development_id=development_id,
        ),
    }
    deps = PipelineDeps(
        lifecycle=LIFECYCLE,
        dispatcher=make_actor(plane, tmp_path, development_id),
        scripts=scripts,
        materializer=_Sealer(),
        cost_plane=plane,
        management_cost=management_cost,
    )
    graph = build_dd_pipeline_graph(deps).compile()
    return graph.invoke(
        initial_state(
            development_id=development_id,
            stage="configure",
            head_commit=COMMIT,
            artifacts={"spec": COMMIT},
        ),
        config={"recursion_limit": 200},
    )


class TestSettlementAndAbsenceComeFromTheWalker:
    def test_completion_emits_a_settlement_fact(self, tmp_path: Path) -> None:
        plane = CostDataPlane()
        state = run_settle(plane, tmp_path, "dev-1")

        assert state["terminal"] == "complete"
        settlements = [s for s in plane.samples() if s.name == SETTLEMENT_METRIC]
        assert {s.label_map()["order_id"] for s in settlements} == {"dev-1"}
        assert [s.value for s in settlements] == [1.0]
        report = plane.reconcile()
        assert report.orders["dev-1"] == {"launch": 1, "settlement": 1}
        assert report.exact_once is True


class TestTheDdPackageImportsTheDataPlane:
    def test_the_exposition_dir_env_var_is_resolved(self, monkeypatch: Any) -> None:
        monkeypatch.setenv(COST_OBS_DIR_ENV, "/var/lib/node_exporter/textfile")
        assert cost_obs_exposition_dir() == Path("/var/lib/node_exporter/textfile")

    def test_build_cost_plane_returns_none_when_unwired(self, monkeypatch: Any) -> None:
        monkeypatch.delenv(COST_OBS_DIR_ENV, raising=False)
        assert build_cost_plane() is None

    def test_build_cost_plane_renders_a_scrape_file(self, tmp_path: Path) -> None:
        plane = build_cost_plane(exposition_dir=tmp_path)
        assert plane is not None
        plane.record_launch(order_id="o", development_id="o")
        path = plane.write_exposition()
        assert path.name == "cost-obs.prom"
        assert "cost_obs_launch_total" in path.read_text(encoding="utf-8")


class TestTheRulesReadTheWiredFacts:
    def test_all_four_lifecycle_rules_are_nonempty_after_a_settle(self, tmp_path: Path) -> None:
        plane = CostDataPlane()
        run_settle(plane, tmp_path, "dev-1")
        plane.record_execution_cost(
            attribution="management", order_id="dev-1", tokens=1, event_id="e"
        )
        plane.record_execution_cost(attribution="launch", order_id="dev-1", tokens=1, event_id="e")

        for rule in RECORDING_RULES:
            assert query(rule.expr, plane.samples()), rule.name
        present = query(
            f'{PRESENCE_METRIC}{{order_id="dev-1",lifecycle="settlement"}}', plane.samples()
        )
        assert [s.value for s in present] == [1.0]


class TestScrapeWiringShipsAlongsideTheProducer:
    """The scrape side the review called out: deploy carries the Prometheus
    rules and the node_exporter textfile wiring the producer writes into."""

    RULES = Path(__file__).parent.parent / "deploy" / "prometheus" / "cost-observability.rules.yml"
    SCRAPE = Path(__file__).parent.parent / "deploy" / "prometheus" / "cost-observability.yml"

    def test_the_rules_file_declares_all_five_recording_rules(self) -> None:
        text = self.RULES.read_text(encoding="utf-8")
        for rule in RECORDING_RULES:
            assert f"record: {rule.name}" in text, rule.name

    def test_the_rules_file_names_every_source_metric(self) -> None:
        text = self.RULES.read_text(encoding="utf-8")
        for metric in (
            "cost_obs_execution_cost_total",
            "cost_obs_launch_total",
            "cost_obs_review_total",
            "cost_obs_promotion_total",
            "cost_obs_settlement_total",
        ):
            assert metric in text, metric

    def test_the_scrape_config_wires_the_textfile_collector(self) -> None:
        text = self.SCRAPE.read_text(encoding="utf-8")
        assert "collector.textfile.directory" in text
        assert "cost-observability.rules.yml" in text


class TestDevelopmentsAccumulateInOneExpositionDirectory:
    """Two developments rendering into the same node_exporter textfile dir must
    not overwrite each other -- the blocker that made the fleet totals cap at
    one run."""

    def test_each_development_gets_its_own_file(self, tmp_path: Path) -> None:
        plane_a = build_cost_plane(exposition_dir=tmp_path, development_id="dev-fg-a")
        plane_b = build_cost_plane(exposition_dir=tmp_path, development_id="dev-fg-b")
        assert plane_a is not None and plane_b is not None
        plane_a.record_launch(order_id="dev-fg-a", development_id="dev-fg-a")
        plane_b.record_launch(order_id="dev-fg-b", development_id="dev-fg-b")
        plane_a.write_exposition()
        plane_b.write_exposition()

        files = sorted(p for p in tmp_path.glob("*.prom"))
        assert {f.name for f in files} == {"cost-obs-dev-fg-a.prom", "cost-obs-dev-fg-b.prom"}

    def test_the_fleet_total_accumulates_across_files(self, tmp_path: Path) -> None:
        plane_a = build_cost_plane(exposition_dir=tmp_path, development_id="dev-a")
        plane_b = build_cost_plane(exposition_dir=tmp_path, development_id="dev-b")
        assert plane_a is not None and plane_b is not None
        plane_a.record_launch(order_id="dev-a", development_id="dev-a")
        plane_b.record_launch(order_id="dev-b", development_id="dev-b")
        plane_a.write_exposition()
        plane_b.write_exposition()

        total = 0.0
        for path in tmp_path.glob("*.prom"):
            for sample in parse(path.read_text(encoding="utf-8")):
                if sample.name == LAUNCH_METRIC:
                    total += sample.value
        assert total == 2.0

    def test_a_second_write_does_not_truncate_the_other_development(self, tmp_path: Path) -> None:
        plane_a = build_cost_plane(exposition_dir=tmp_path, development_id="dev-a")
        plane_b = build_cost_plane(exposition_dir=tmp_path, development_id="dev-b")
        assert plane_a is not None and plane_b is not None
        plane_a.record_launch(order_id="dev-a", development_id="dev-a")
        plane_a.write_exposition()
        plane_b.record_launch(order_id="dev-b", development_id="dev-b")
        plane_b.write_exposition()
        # Re-render dev-a after dev-b wrote: dev-b's file must survive.
        plane_a.write_exposition()

        all_launch = [
            sample.value
            for path in tmp_path.glob("*.prom")
            for sample in parse(path.read_text(encoding="utf-8"))
            if sample.name == LAUNCH_METRIC
        ]
        assert sorted(all_launch) == [1.0, 1.0]

    def test_the_filename_is_derived_and_sanitized(self) -> None:
        assert exposition_filename_for("dev-fg-6e4f9345b320") == "cost-obs-dev-fg-6e4f9345b320.prom"
        assert exposition_filename_for("a b/c") == "cost-obs-a-b-c.prom"

    def test_the_cost_metric_carries_an_order_id_so_it_accumulates(self, tmp_path: Path) -> None:
        """Two developments emitting management spend must be distinct series
        (per-order, not byte-identical `{attribution="management"}`), or
        node_exporter drops the duplicate at gather time and rule 1's
        numerator under-counts fleet-wide."""
        plane_a = build_cost_plane(exposition_dir=tmp_path, development_id="dev-a")
        plane_b = build_cost_plane(exposition_dir=tmp_path, development_id="dev-b")
        assert plane_a is not None and plane_b is not None
        plane_a.record_execution_cost(
            attribution="management", order_id="dev-a", tokens=10.0, event_id="m:a"
        )
        plane_b.record_execution_cost(
            attribution="management", order_id="dev-b", tokens=30.0, event_id="m:b"
        )
        plane_a.write_exposition()
        plane_b.write_exposition()

        management = [
            sample
            for path in tmp_path.glob("*.prom")
            for sample in parse(path.read_text(encoding="utf-8"))
            if sample.name == COST_METRIC and sample.label_map().get("attribution") == "management"
        ]
        assert {s.label_map()["order_id"] for s in management} == {"dev-a", "dev-b"}
        assert sum(s.value for s in management) == 40.0


class TestExecutionCostComesFromTheDispatcher:
    """Rule 1 and the `unknown` bucket need a production token-spend producer;
    it is the stage actor, reading each run's reported usage."""

    def test_the_implement_run_spend_is_attributed_to_launch(self, tmp_path: Path) -> None:
        plane = CostDataPlane()
        stage = LIFECYCLE.stages[implement_stage(LIFECYCLE) or "implement"]

        class UsageLauncher(Launcher):
            def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
                return RunStatus(
                    "succeeded",
                    {
                        "structured_result": {
                            "actor_job_id": "job-1",
                            "input_commit": COMMIT,
                            "outcome": "APPLIED",
                            "work_head_commit": "b" * 40,
                            "verification_record": {"verification_commands": []},
                        },
                        "usage": {"total_tokens": 20.0},
                    },
                )

        actor = make_actor(plane, tmp_path)
        actor.launcher = UsageLauncher()  # type: ignore[assignment]
        actor.act(stage, dispatch_for("dev-1"))

        launch_cost = [
            s
            for s in plane.samples()
            if s.name == COST_METRIC and s.label_map()["attribution"] == "launch"
        ]
        assert [s.value for s in launch_cost] == [20.0]

    def test_a_failed_run_spend_is_unknown(self, tmp_path: Path) -> None:
        plane = CostDataPlane()
        stage = LIFECYCLE.stages[implement_stage(LIFECYCLE) or "implement"]

        class FailingUsageLauncher:
            def launch(self, spec: Any, run_id: str) -> RunTicket:
                return RunTicket(run_id, "/tmp", None)

            def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
                return RunStatus("failed", {"usage": {"total_tokens": 7.0}})

        actor = AgentRunStageActor(
            launcher=FailingUsageLauncher(),  # type: ignore[arg-type]
            development_id="dev-1",
            run_root=tmp_path,
            cost_plane=plane,
        )
        outcome = actor.act(stage, dispatch_for("dev-1"))

        assert outcome.event == "failed"
        unknown = [
            s
            for s in plane.samples()
            if s.name == COST_METRIC and s.label_map()["attribution"] == "unknown"
        ]
        assert [s.value for s in unknown] == [7.0]


class TestTheManagementCostIsEmittedByTheWalker:
    def test_management_is_emitted_per_managed_order(self, tmp_path: Path) -> None:
        plane = CostDataPlane()
        state = run_settle(plane, tmp_path, "dev-1", management_cost=lambda _oid: 10.0)

        assert state["terminal"] == "complete"
        management = [
            s
            for s in plane.samples()
            if s.name == COST_METRIC and s.label_map()["attribution"] == "management"
        ]
        assert [s.value for s in management] == [10.0]

    def test_unmeasured_management_is_absent_not_a_measured_zero(self, tmp_path: Path) -> None:
        plane = CostDataPlane()
        state = run_settle(plane, tmp_path, "dev-1")

        assert state["terminal"] == "complete"
        management = [
            s
            for s in plane.samples()
            if s.name == COST_METRIC and s.label_map()["attribution"] == "management"
        ]
        # Unmeasured manager spend is accounted absent -- distinguishable from
        # a genuine zero measurement -- rather than faked as a definite 0.
        assert management == []


class TestAbsenceAccountingOnNonCompleteTerminals:
    """A development that dies on `failed`, `bounds` or `fault` must still mark
    its unproduced lifecycles absent -- a bounded 0, not a silent gap."""

    def test_a_failure_terminal_accounts_every_lifecycle_absent(self, tmp_path: Path) -> None:
        plane = CostDataPlane()

        class FailingDispatch:
            def act(self, stage: Any, dispatch: Dispatch) -> StageOutcome:
                return StageOutcome(event="failed", failure_code="INVALID_HANDOFF_SCHEMA")

        scripts: dict[str, Any] = {
            _producer("run_config"): ConfigureStage(repo=tmp_path, run_config={}),
            _producer("acceptance_result"): AcceptanceStage(repo=tmp_path, declared=[], setup=[]),
            _producer("merge_result"): MergeStage(
                repo=tmp_path, remote_url="", target_ref="refs/heads/main", cost_plane=plane
            ),
            _producer("gate_decision"): BoardGate(
                board=_ApprovingBoard(),  # type: ignore[arg-type]
                card_entity_id="card-1",
                development_id="dev-1",
            ),
        }
        deps = PipelineDeps(
            lifecycle=LIFECYCLE,
            dispatcher=FailingDispatch(),  # type: ignore[arg-type]
            scripts=scripts,
            materializer=_Sealer(),
            cost_plane=plane,
        )
        graph = build_dd_pipeline_graph(deps).compile()
        state = graph.invoke(
            initial_state(
                development_id="dev-1",
                stage="configure",
                head_commit=COMMIT,
                artifacts={"spec": COMMIT},
            ),
            config={"recursion_limit": 200},
        )

        assert state["terminal"] == "failed"
        for lifecycle in ("launch", "review", "promotion", "settlement"):
            present = query(
                f'{PRESENCE_METRIC}{{order_id="dev-1",lifecycle="{lifecycle}"}}',
                plane.samples(),
            )
            assert [s.value for s in present] == [0.0], lifecycle
