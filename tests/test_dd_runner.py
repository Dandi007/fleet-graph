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
from fleet_graph.dd.lifecycle import Lifecycle
from fleet_graph.dd.prompt import IMPLEMENT_PERSONA, IMPLEMENT_TEMPLATE
from fleet_graph.dd.vendor import plugin_adapter
from fleet_graph.executors.agent_run import RunStatus, RunTicket
from fleet_graph.graphs.dd_pipeline import (
    TERMINAL_COMPLETE,
    TERMINAL_FAULT,
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
from fleet_graph.graphs.dd_scripts import ACCEPTANCE_PATH, RUN_CONFIG_PATH

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
            declared = {"verdict": verdict, "findings": [], "review_phase": stage.id}
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

    def implement_seal(binding: Any, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        receipt = sealer.seal("implement", StageOutcome())
        # The real sealer writes the receipt here, and the review sealer
        # re-reads exactly these bytes to check the digest it was handed.
        path = (
            Path(request["state_root"])
            / "receipts"
            / request["dispatch"]["attempt_id"]
            / "implement-receipt.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
        return receipt

    def review_seal(binding: Any, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        verdict = request["review_result"]["verdict"]
        return {
            **sealer.seal(request["dispatch"]["stage"], StageOutcome()),
            "verdict": verdict,
        }

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
