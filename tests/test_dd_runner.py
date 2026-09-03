"""Assembling a development: every real part, wired together, on a real repo.

The agents and the plugin script are stood in for -- one costs money and the
other lives in another repository -- but everything between them is the
shipped code: the contract interpreter, the derived spine, the dispatch
builder reading real git blobs, the capability lock over the real bundle, and
the bindings that check the chain.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import DEVELOPMENT_ID, git, head
from fleet_graph.bus.board import Decision, GateTicket
from fleet_graph.dd.lifecycle import Lifecycle
from fleet_graph.dd.prompt import IMPLEMENT_PERSONA, IMPLEMENT_TEMPLATE
from fleet_graph.dd.vendor import plugin_adapter
from fleet_graph.executors.agent_run import RunStatus, RunTicket
from fleet_graph.graphs.dd_pipeline import (
    TERMINAL_COMPLETE,
    TERMINAL_FAULT,
    TERMINAL_REFUSED,
    Dispatch,
    Sealed,
    StageOutcome,
)
from fleet_graph.graphs.dd_runner import (
    DevelopmentConfig,
    build_pipeline,
    lifecycle_gate_stage,
    run_pipeline,
)
from fleet_graph.graphs.dd_scripts import ACCEPTANCE_PATH, GATE_PATH, RUN_CONFIG_PATH

LIFECYCLE = Lifecycle.load()


def make_config(repo: Path, tmp_path: Path) -> DevelopmentConfig:
    # A real bare repo, because the script sealer publishes to the durable ref
    # and a pipeline that cannot publish is not the one we ship.
    bare = tmp_path / "durable.git"
    if not bare.exists():
        git(repo, "init", "-q", "--bare", str(bare))
    return DevelopmentConfig(
        development_id=DEVELOPMENT_ID,
        workspace_path=repo,
        state_root=tmp_path / "state",
        run_root=tmp_path / "runs",
        remote_url=str(bare),
        remote_ref="refs/heads/dev-001",
        target_base_commit="b" * 40,
        root_handoff_digest="sha256:" + "c" * 64,
        plugin_binding=object(),
        head_commit=head(repo),
    )


class AgentRunStub:
    """Stands in for `agent-run`: answers with what each stage declares."""

    def __init__(self, verdicts: dict[str, list[str]] | None = None) -> None:
        self.verdicts = {k: list(v) for k, v in (verdicts or {}).items()}
        self.dispatched: list[str] = []

    def launch(self, spec: Any, run_id: str) -> RunTicket:
        self.stage = spec.labels["stage"]
        self.dispatched.append(self.stage)
        return RunTicket(run_id, "/tmp/x", None)

    def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
        """Answers in the roles' own result shapes: implement.result.v1 and
        review.result.v2, which is what agent-runtime actually returns."""
        stage = LIFECYCLE.stages[self.stage]
        queue = self.verdicts.get(stage.id)
        verdict = queue.pop(0) if queue else "success"
        if stage.id == "implement":
            declared: dict[str, Any] = {
                "actor_job_id": f"job-{stage.id}",
                "input_commit": "1" * 40,
                "work_head_commit": "2" * 40,
                "outcome": "APPLIED",
                "verification_record": {
                    "verification_commands": [{"argv": ["true"], "exit_code": 0}]
                },
            }
        else:
            declared = {
                "verdict": verdict,
                "findings": [],
                "review_phase": stage.id,
                "checked_items": ["read the diff against the spec"],
            }
        return RunStatus("succeeded", {"structured_result": declared})


class ScriptStub:
    """A script stage that produces what the contract says it produces."""

    def __init__(self) -> None:
        self.ran: list[str] = []

    def act(self, stage: Any, dispatch: Dispatch) -> StageOutcome:
        self.ran.append(stage.id)
        return StageOutcome(
            event="success",
            receipt={"stage": stage.id},
            produced=tuple(stage.produced_artifacts),
        )


class RealCommitSealer:
    """Writes an actual commit, so the next stage's dispatch can read it."""

    def __init__(self, repo: Path, *, verdict_from_actor: bool = False) -> None:
        self.repo = repo
        self.verdict_from_actor = verdict_from_actor
        self.commits: list[str] = []

    def seal(self, stage_id: str, outcome: StageOutcome) -> dict[str, Any]:
        git(self.repo, "commit", "-q", "--allow-empty", "-m", f"seal {stage_id}")
        commit = head(self.repo)
        self.commits.append(commit)
        receipt: dict[str, Any] = {"stage": stage_id, "output_commit": commit}
        declared = (outcome.receipt or {}).get("review_result")
        if isinstance(declared, dict) and declared.get("verdict"):
            receipt["verdict"] = declared["verdict"]
        return receipt

    def materialize(self, stage: Any, dispatch: Dispatch, outcome: StageOutcome) -> Sealed:
        receipt = self.seal(stage.id, outcome)
        return Sealed(commit=receipt["output_commit"], receipt=receipt)


@pytest.fixture
def plugin_seals(repo: Path, monkeypatch: pytest.MonkeyPatch) -> RealCommitSealer:
    """Stand in for the plugin's materialize-handoff script."""
    sealer = RealCommitSealer(repo)

    def write_receipt(request: dict[str, Any], name: str, receipt: dict[str, Any]) -> None:
        """Where the real sealer persists a receipt; later stages digest these
        exact bytes."""
        path = Path(request["state_root"]) / "receipts" / request["dispatch"]["attempt_id"] / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")))

    def implement_seal(binding: Any, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        receipt = sealer.seal("implement", StageOutcome())
        write_receipt(request, "implement-receipt.json", receipt)
        return receipt

    def review_seal(binding: Any, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        stage = request["dispatch"]["stage"]
        receipt = {
            **sealer.seal(stage, StageOutcome()),
            "verdict": request["review_result"]["verdict"],
        }
        if stage == "continuous_review":
            # The real sealer persists this, and the Final Review dispatch has
            # to name its bytes as the parent.
            write_receipt(request, "continuous-review-receipt.json", receipt)
        return receipt

    monkeypatch.setattr(plugin_adapter, "invoke_implement_materializer", implement_seal)
    monkeypatch.setattr(plugin_adapter, "invoke_review_materializer", review_seal)

    # The prompt comes from the same bundle, so the stand-in has to cover it
    # too -- otherwise the test would be asserting against a pipeline that
    # never rendered one.
    class Resource:
        def __init__(self, path: str, text: str) -> None:
            self.relative_path = path
            self.content = text.encode("utf-8")
            self.digest = "sha256:" + "0" * 64

    monkeypatch.setattr(
        plugin_adapter,
        "load_implement_stage_resources",
        lambda binding, **kwargs: (
            Resource(IMPLEMENT_PERSONA, "You are the Implementer."),
            Resource(
                IMPLEMENT_TEMPLATE,
                "input_commit: {{input_commit}}\nacceptance: {{acceptance_commands}}\n",
            ),
        ),
    )
    return sealer


def run(
    repo: Path,
    tmp_path: Path,
    *,
    verdicts: dict[str, list[str]] | None = None,
) -> tuple[dict[str, Any], AgentRunStub, ScriptStub]:
    launcher = AgentRunStub(verdicts)
    scripts = ScriptStub()
    local = RealCommitSealer(repo)
    # Every stage the plugin does not seal. `acceptance` is among them even
    # though it appears in the dispatch schema's stage enum.
    unsealed = {name: local for name, stage in LIFECYCLE.stages.items() if not stage.is_llm}
    result = run_pipeline(
        make_config(repo, tmp_path),
        scripts={name: scripts for name, s in LIFECYCLE.stages.items() if not s.is_llm},
        materializers=unsealed,
        launcher=launcher,
    )
    return result, launcher, scripts


class TestTheWholePipelineComposes:
    def test_it_walks_from_configure_to_the_last_stage(
        self, repo: Path, tmp_path: Path, plugin_seals: RealCommitSealer
    ) -> None:
        result, _launcher, _scripts = run(
            repo, tmp_path, verdicts={"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]}
        )

        assert result["terminal"] == TERMINAL_COMPLETE, result["terminal_reason"]
        assert [entry["stage"] for entry in result["history"]] == [
            "configure",
            "implement",
            "continuous_review",
            "final_review",
            "acceptance",
            "human_gate",
            "merger",
        ]

    def test_only_the_llm_stages_cost_an_agent_run(
        self, repo: Path, tmp_path: Path, plugin_seals: RealCommitSealer
    ) -> None:
        _result, launcher, scripts = run(
            repo, tmp_path, verdicts={"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]}
        )
        assert set(launcher.dispatched) == {
            name for name, stage in LIFECYCLE.stages.items() if stage.is_llm
        }
        assert set(scripts.ran) == {
            name for name, stage in LIFECYCLE.stages.items() if not stage.is_llm
        }

    def test_every_stage_starts_from_the_commit_the_last_one_sealed(
        self, repo: Path, tmp_path: Path, plugin_seals: RealCommitSealer
    ) -> None:
        result, _launcher, _scripts = run(
            repo, tmp_path, verdicts={"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]}
        )
        commits = [entry["output_commit"] for entry in result["history"]]
        assert len(set(commits)) == len(commits), "each stage sealed its own commit"
        assert result["head_commit"] == commits[-1] == head(repo)

    def test_a_rejection_reworks_and_still_finishes(
        self, repo: Path, tmp_path: Path, plugin_seals: RealCommitSealer
    ) -> None:
        result, launcher, _scripts = run(
            repo,
            tmp_path,
            verdicts={"continuous_review": ["REJECT", "APPROVE"], "final_review": ["APPROVE"]},
        )
        assert result["terminal"] == TERMINAL_COMPLETE, result["terminal_reason"]
        assert launcher.dispatched.count("implement") == 2


class TestABoundedRetryReallyRetries:
    def test_each_retry_dispatches_a_new_run(
        self, repo: Path, tmp_path: Path, plugin_seals: RealCommitSealer
    ) -> None:
        """The whole point of the bound: a retry that re-adopts the completed
        run it is retrying gets the same answer and the bound never bites."""
        run_ids: list[str] = []

        class AlwaysDown(AgentRunStub):
            def launch(self, spec: Any, run_id: str) -> RunTicket:
                run_ids.append(run_id)
                return super().launch(spec, run_id)

            def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
                if self.stage == "implement":
                    return RunStatus("failed", {"exit_code": 1})
                return super().wait(ticket, **kwargs)

        config = make_config(repo, tmp_path)
        config.max_retries = 2
        result = run_pipeline(config, scripts={"human_gate": ScriptStub()}, launcher=AlwaysDown())

        assert result["terminal"] == "failed"
        assert "2 bounded retries" in result["terminal_reason"]
        assert len(set(run_ids)) == 3, f"one dispatch per attempt, got {run_ids}"


class TestTheDefaultsMakeItRunnable:
    """Assembled means runnable. The gate is the one thing left unfilled."""

    def test_the_script_stages_need_no_registration(
        self, repo: Path, tmp_path: Path, plugin_seals: RealCommitSealer
    ) -> None:
        config = make_config(repo, tmp_path)
        config.run_config = {"acceptance_commands": [["true"]]}
        # This test's subject is script-stage defaults, not the mutation gate;
        # its config names no real base commit the gate could enumerate.
        config.mutation_inputs = False
        result = run_pipeline(
            config,
            scripts={"human_gate": ScriptStub()},
            launcher=AgentRunStub({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]}),
        )
        assert result["terminal"] == TERMINAL_COMPLETE, result["terminal_reason"]
        assert (repo / RUN_CONFIG_PATH).is_file()
        assert (repo / ACCEPTANCE_PATH).is_file()

    def test_the_gate_still_refuses_without_a_board(
        self, repo: Path, tmp_path: Path, plugin_seals: RealCommitSealer
    ) -> None:
        """No default approves on its own -- that would be an agent casting a
        human's verdict."""
        result = run_pipeline(
            make_config(repo, tmp_path),
            launcher=AgentRunStub({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]}),
        )
        assert result["terminal"] == TERMINAL_FAULT
        assert "human_gate" in result["terminal_reason"]
        assert "no registered script" in result["terminal_reason"]

    def test_a_caller_can_still_override_any_stage(self, repo: Path, tmp_path: Path) -> None:
        mine = ScriptStub()
        _graph, deps = build_pipeline(make_config(repo, tmp_path), scripts={"configure": mine})
        assert deps.scripts["configure"] is mine


class TestTheWiringReadsTheContract:
    def test_the_gate_stage_is_found_through_the_artifact_it_produces(self) -> None:
        assert lifecycle_gate_stage(LIFECYCLE) == "human_gate"

    def test_the_board_is_only_wired_when_one_is_supplied(self, repo: Path, tmp_path: Path) -> None:
        _graph, deps = build_pipeline(make_config(repo, tmp_path))
        assert "human_gate" not in deps.scripts

        class FakeBoard:
            pass

        _graph, deps = build_pipeline(
            make_config(repo, tmp_path), board=FakeBoard(), gate_card_entity_id="card-1"
        )
        assert "human_gate" in deps.scripts

    def test_the_capability_lock_is_on_by_default(self, repo: Path, tmp_path: Path) -> None:
        _graph, deps = build_pipeline(make_config(repo, tmp_path))
        assert deps.capability is not None
        assert deps.capability.require().ok

    def test_the_run_root_is_per_development(self, repo: Path, tmp_path: Path) -> None:
        config = make_config(repo, tmp_path)
        assert config.thread_id == f"{DEVELOPMENT_ID}:g1"
        assert json.dumps(str(config.run_root))

    def test_dispatched_by_flows_from_config_to_the_actor(self, repo: Path, tmp_path: Path) -> None:
        """The bounded dispatch principal is a config-chain value threaded to
        the stage actor's run labels, not an identity minted at dispatch."""
        config = make_config(repo, tmp_path)
        config.dispatched_by = "ronin-model-switch"
        _graph, deps = build_pipeline(config)

        assert deps.dispatcher.dispatched_by == "ronin-model-switch"


class FakeBoard:
    """A board that records what was asked and answers when told to.

    Faithful in the one way that matters here: `ask` is keyed, so re-asking
    with the same key must not produce a second question note.
    """

    def __init__(self) -> None:
        self.asked: dict[str, str] = {}
        self.decision: Decision | None = None

    def ask(self, *, card_entity_id: str, question: str, idempotency_key: str) -> GateTicket:
        self.asked.setdefault(idempotency_key, question)
        return GateTicket(question_note_id="note-1", card_entity_id=card_entity_id)

    def decision_for(self, ticket: GateTicket) -> Decision | None:
        return self.decision


class TestWaitingOnAHumanAndComingBack:
    """The gate suspends the run; a later invocation picks it up where it stopped."""

    def _config(self, repo: Path, tmp_path: Path) -> DevelopmentConfig:
        config = make_config(repo, tmp_path)
        # A resume has to find the thread, so the checkpoint outlives the process.
        config.checkpoint_path = str(tmp_path / "checkpoint.sqlite")
        config.run_config = {"acceptance_commands": [["true"]]}
        # This class's subject is the human gate, not the mutation gate; its
        # config names no real base commit the gate could enumerate.
        config.mutation_inputs = False
        return config

    def _run(self, config: DevelopmentConfig, board: FakeBoard, **kwargs: Any) -> dict[str, Any]:
        return run_pipeline(
            config,
            board=board,
            gate_card_entity_id="card-1",
            launcher=AgentRunStub({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]}),
            **kwargs,
        )

    def test_an_open_question_reports_which_note_is_holding_the_line(
        self, repo: Path, tmp_path: Path, plugin_seals: RealCommitSealer
    ) -> None:
        board = FakeBoard()
        result = self._run(self._config(repo, tmp_path), board)

        assert result["terminal"] is None, "waiting is not an ending"
        assert result["awaiting"] == {"question_note_id": "note-1", "card_entity_id": "card-1"}
        assert list(board.asked) == [f"dd-gate:{DEVELOPMENT_ID}:g1"]

    def test_resuming_after_the_verdict_finishes_the_run(
        self, repo: Path, tmp_path: Path, plugin_seals: RealCommitSealer
    ) -> None:
        board = FakeBoard()
        config = self._config(repo, tmp_path)
        assert self._run(config, board)["awaiting"] is not None

        board.decision = Decision(
            message_id="msg-1",
            decision="APPROVE",
            decided_by="青林",
            question="",
            rationale="",
            card_entity_id="card-1",
            raw={},
        )
        result = self._run(config, board, resume=True)

        assert result["terminal"] == TERMINAL_COMPLETE, result["terminal_reason"]
        assert result["awaiting"] is None
        # The verdict outlives the run: an assembled pipeline seals it into
        # the product tree without the caller asking for it.
        sealed = json.loads((repo / GATE_PATH.format(generation=1)).read_text(encoding="utf-8"))
        assert (sealed["decision"], sealed["decided_by"]) == ("APPROVE", "青林")
        # Same key both times: the wait restarted, the question did not.
        assert list(board.asked) == [f"dd-gate:{DEVELOPMENT_ID}:g1"]

    def test_resuming_does_not_replay_the_stages_already_sealed(
        self, repo: Path, tmp_path: Path, plugin_seals: RealCommitSealer
    ) -> None:
        """The point of the checkpoint: the agents do not run a second time."""
        board = FakeBoard()
        config = self._config(repo, tmp_path)
        self._run(config, board)

        launcher = AgentRunStub({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        board.decision = Decision(
            message_id="msg-1",
            decision="APPROVE",
            decided_by="青林",
            question="",
            rationale="",
            card_entity_id="card-1",
            raw={},
        )
        run_pipeline(
            config,
            board=board,
            gate_card_entity_id="card-1",
            launcher=launcher,
            resume=True,
        )
        assert launcher.dispatched == [], "a resume must not re-dispatch a sealed stage"

    def test_a_rejection_on_resume_ends_it_without_a_fault(
        self, repo: Path, tmp_path: Path, plugin_seals: RealCommitSealer
    ) -> None:
        board = FakeBoard()
        config = self._config(repo, tmp_path)
        self._run(config, board)

        board.decision = Decision(
            message_id="msg-2",
            decision="REJECT",
            decided_by="青林",
            question="",
            rationale="不放行",
            card_entity_id="card-1",
            raw={},
        )
        result = self._run(config, board, resume=True)

        assert result["terminal"] == TERMINAL_REFUSED
        assert "REJECT" in result["terminal_reason"]
        assert result["fault"] is False

    def test_an_unrecognized_verdict_suspends_and_resumes_to_completion(
        self, repo: Path, tmp_path: Path, plugin_seals: RealCommitSealer
    ) -> None:
        """A malformed gate verdict is a resumable refusal, not a dead end: the
        gate suspends (fail-closed), and once a proper APPROVE lands a resume
        re-reads the board and pushes through to the merger -- without
        re-materializing any already-sealed attempt-context stage."""
        board = FakeBoard()
        config = self._config(repo, tmp_path)
        self._run(config, board)

        board.decision = Decision(
            message_id="msg-bad",
            decision="needs a second look",
            decided_by="青林",
            question="",
            rationale="",
            card_entity_id="card-1",
            raw={},
        )
        refused = self._run(config, board, resume=True)
        # Fail-closed: not terminal, but also not gone -- the gate is waiting
        # for a decision it can interpret, and says so.
        assert refused["terminal"] is None, refused["terminal_reason"]
        assert refused["gate_refused"] == {
            "reason": "gate decision 'needs a second look' is not one of "
            "['APPROVE', 'REJECT']; refusing to interpret it",
            "code": "GATE_VERDICT_UNRECOGNIZED",
        }
        assert refused["awaiting"] == {"question_note_id": "note-1", "card_entity_id": "card-1"}

        board.decision = Decision(
            message_id="msg-good",
            decision="APPROVE",
            decided_by="青林",
            question="",
            rationale="",
            card_entity_id="card-1",
            raw={},
        )

        # A resume must re-read the board -- never re-dispatch the sealed
        # attempt-context stages (implement / continuous_review / final_review).
        launcher = AgentRunStub()
        result = run_pipeline(
            config,
            board=board,
            gate_card_entity_id="card-1",
            launcher=launcher,
            resume=True,
        )
        assert launcher.dispatched == [], "a resume must not re-dispatch a sealed stage"
        assert result["terminal"] == TERMINAL_COMPLETE, result["terminal_reason"]
        assert result["gate_refused"] is None
        sealed = json.loads((repo / GATE_PATH.format(generation=1)).read_text(encoding="utf-8"))
        assert sealed["decision"] == "APPROVE"

    def test_resume_preserves_the_pre_suspension_cost_facts(
        self, repo: Path, tmp_path: Path, plugin_seals: RealCommitSealer
    ) -> None:
        """A suspended development, resumed to settlement, must not lose the
        launch and review facts the pre-suspension process already rendered.

        The resume rebuilds the pipeline and therefore a fresh empty data
        plane; without rehydration the final scrape file would hold promotion +
        settlement + management only, breaking every fleet aggregate and the
        settlement reconciliation. This pins the cross-process resume the real
        lifecycle uses -- the in-process idempotency tests do not cover it.
        """
        from fleet_graph.cost_obs import query
        from fleet_graph.cost_obs.exposition import parse
        from fleet_graph.cost_obs.rules import (
            LAUNCH_METRIC,
            PROMOTION_METRIC,
            REVIEW_METRIC,
            SETTLEMENT_METRIC,
        )

        board = FakeBoard()
        config = self._config(repo, tmp_path)
        config.cost_obs_dir = str(tmp_path / "textfile")
        first = self._run(config, board)
        assert first["awaiting"] is not None

        board.decision = Decision(
            message_id="msg-1",
            decision="APPROVE",
            decided_by="青林",
            question="",
            rationale="",
            card_entity_id="card-1",
            raw={},
        )
        result = self._run(config, board, resume=True)
        assert result["terminal"] == TERMINAL_COMPLETE, result["terminal_reason"]

        scraped = parse(
            (tmp_path / "textfile" / "cost-obs-dev-001.prom").read_text(encoding="utf-8")
        )
        names = {s.name for s in scraped}
        assert {LAUNCH_METRIC, REVIEW_METRIC, PROMOTION_METRIC, SETTLEMENT_METRIC} <= names
        # The pre-suspension launch survives, and the settlement still correlates.
        assert [s.value for s in query(f"sum({LAUNCH_METRIC})", scraped)] == [1.0]
        reviews = query(f'sum({REVIEW_METRIC}{{phase=~"continuous|final"}})', scraped)
        assert [s.value for s in reviews] == [2.0]
        reconciliation = query(
            f'sum({SETTLEMENT_METRIC}{{status="settled"}}) by (order_id)'
            f" / on(order_id) sum({LAUNCH_METRIC}) by (order_id)",
            scraped,
        )
        assert [s.value for s in reconciliation] == [1.0]


class TestARestartedGenerationKeepsItsCostFacts:
    """A restarted generation -- the control plane's normal exit from a
    non-complete terminal -- rebuilds the pipeline, so it gets a fresh empty
    data plane and enters the receipt-sealed prefix with "no actor runs": the
    implement and review actors never re-emit their launch/review facts. The
    resume path rehydrates the development's own scrape file; this pins the
    same requirement on the generation n+1 path, where a receipt replayer is
    installed instead of a resume."""

    def _config(self, repo: Path, tmp_path: Path, *, generation: int) -> DevelopmentConfig:
        dev_root = tmp_path / "runs"
        run_root = dev_root if generation <= 1 else dev_root / f"g{generation}"
        return DevelopmentConfig(
            development_id=DEVELOPMENT_ID,
            workspace_path=repo,
            state_root=run_root / "state",
            run_root=run_root,
            remote_url="",
            remote_ref="refs/heads/dev-001",
            target_base_commit="b" * 40,
            root_handoff_digest="sha256:" + "c" * 64,
            plugin_binding=object(),
            head_commit=head(repo),
            generation=generation,
            cost_obs_dir=str(tmp_path / "textfile"),
            run_config={"acceptance_commands": [["true"]]},
            # This class's subject is cost-fact rehydration across a restarted
            # generation, not the mutation gate; its config names no real base
            # commit the gate could enumerate.
            mutation_inputs=False,
        )

    def test_generation_two_rehydrates_the_previous_scrape_file(
        self, repo: Path, tmp_path: Path, plugin_seals: RealCommitSealer
    ) -> None:
        from fleet_graph.cost_obs import query
        from fleet_graph.cost_obs.exposition import parse
        from fleet_graph.cost_obs.rules import (
            LAUNCH_METRIC,
            REVIEW_METRIC,
            SETTLEMENT_METRIC,
        )
        from fleet_graph.dd.cost_obs import build_cost_plane

        textfile = tmp_path / "textfile"
        # Generation 1 already rendered its launch and review facts into the
        # per-development scrape file before a later stage failed.
        prior = build_cost_plane(exposition_dir=textfile, development_id=DEVELOPMENT_ID)
        assert prior is not None
        prior.record_launch(order_id=DEVELOPMENT_ID, development_id=DEVELOPMENT_ID)
        prior.record_review(order_id=DEVELOPMENT_ID, phase="continuous", verdict="approve")
        prior.record_review(order_id=DEVELOPMENT_ID, phase="final", verdict="approve")
        prior.write_exposition()

        board = FakeBoard()
        board.decision = Decision(
            message_id="msg-1",
            decision="APPROVE",
            decided_by="青林",
            question="",
            rationale="",
            card_entity_id="card-1",
            raw={},
        )
        result = run_pipeline(
            self._config(repo, tmp_path, generation=2),
            board=board,
            gate_card_entity_id="card-1",
            launcher=AgentRunStub({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]}),
        )
        assert result["terminal"] == TERMINAL_COMPLETE, result["terminal_reason"]

        scraped = parse((textfile / "cost-obs-dev-001.prom").read_text(encoding="utf-8"))
        # launch and both reviews survive the fresh generation's overwrite; the
        # settlement still reconciles exactly-once against the surviving launch.
        assert [s.value for s in query(f"sum({LAUNCH_METRIC})", scraped)] == [1.0]
        assert [
            s.value for s in query(f'sum({REVIEW_METRIC}{{phase=~"continuous|final"}})', scraped)
        ] == [2.0]
        assert [
            s.value
            for s in query(
                f'sum({SETTLEMENT_METRIC}{{status="settled"}}) by (order_id)'
                f" / on(order_id) sum({LAUNCH_METRIC}) by (order_id)",
                scraped,
            )
        ] == [1.0]


class TestRePrepareClearsARemnantBeforeTheRetry:
    """The failed-implement-remnant scenario from the spec: an implement
    attempt that did its work but never reported (contract_violation, no
    structured output) leaves a committed remnant at HEAD. The next attempt's
    fresh dispatch must re-prepare the worktree (reset --hard to the attempt's
    input_commit + clean) before it runs, and record `event=re_prepare` in
    events.jsonl, so the retry starts from a clean tree at input_commit
    instead of being refused by the actor-side exact-commit check."""

    def test_a_failed_implement_remnant_is_re_prepared_before_the_retry(
        self, repo: Path, tmp_path: Path, plugin_seals: RealCommitSealer
    ) -> None:
        from fleet_graph.graphs.dd_runner import EVENTS_FILE

        class RemnantThenSucceed(AgentRunStub):
            def __init__(self) -> None:
                super().__init__({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
                self.implement_waits = 0
                self.remnant_sha = ""

            def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
                if self.stage == "implement" and self.implement_waits == 0:
                    self.implement_waits += 1
                    # The agent did the work and committed it, then failed to
                    # report -- the contract_violation exit.
                    (repo / "work.txt").write_text("did the work, never reported\n")
                    git(repo, "add", "-A")
                    git(repo, "commit", "-q", "-m", "implement remnant")
                    self.remnant_sha = head(repo)
                    return RunStatus("failed", {"exit_code": 97})
                if self.stage == "implement":
                    self.implement_waits += 1
                return super().wait(ticket, **kwargs)

        launcher = RemnantThenSucceed()
        scripts = ScriptStub()
        local = RealCommitSealer(repo)
        unsealed = {name: local for name, stage in LIFECYCLE.stages.items() if not stage.is_llm}
        config = make_config(repo, tmp_path)
        result = run_pipeline(
            config,
            scripts={name: scripts for name, s in LIFECYCLE.stages.items() if not s.is_llm},
            materializers=unsealed,
            launcher=launcher,
        )

        # The retry was re-prepared, then succeeded; the order completed.
        assert result["terminal"] == TERMINAL_COMPLETE, result["terminal_reason"]
        assert not git(repo, "status", "--porcelain").strip(), "the tree is clean at the end"

        lines = [
            json.loads(raw)
            for raw in (config.run_root / EVENTS_FILE).read_text(encoding="utf-8").splitlines()
            if raw
        ]
        re_prepares = [entry for entry in lines if entry.get("event") == "re_prepare"]
        assert len(re_prepares) == 1
        assert re_prepares[0]["stage"] == "implement"
        # The re-prepare cleared the exact remnant commit the failed attempt
        # left, and reset to the attempt's own input commit (the configure
        # seal, whatever it was), never the remnant itself.
        assert re_prepares[0]["cleaned_head"] == launcher.remnant_sha
        assert re_prepares[0]["input_commit"] != launcher.remnant_sha
        assert re_prepares[0]["at"]


class TestTheRunLeavesArtifactsBehind:
    """The control plane's read side assembles get/events from these files
    after the process is gone -- they are the state model, not telemetry."""

    def test_a_run_persists_its_events_and_result(
        self, repo: Path, tmp_path: Path, plugin_seals: RealCommitSealer
    ) -> None:
        from fleet_graph.graphs.dd_runner import EVENTS_FILE, RESULT_FILE

        result, _launcher, _scripts = run(
            repo, tmp_path, verdicts={"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]}
        )
        run_root = make_config(repo, tmp_path).run_root

        persisted = json.loads((run_root / RESULT_FILE).read_text(encoding="utf-8"))
        assert persisted["terminal"] == result["terminal"] == TERMINAL_COMPLETE
        assert persisted["head_commit"] == result["head_commit"]
        assert persisted["written_at"]

        lines = [
            json.loads(raw)
            for raw in (run_root / EVENTS_FILE).read_text(encoding="utf-8").splitlines()
            if raw
        ]
        assert [entry["stage"] for entry in lines] == [
            entry["stage"] for entry in result["history"]
        ]
        assert all(entry["at"] for entry in lines)
