"""The script stages: write a file, commit it, say what happened."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import git, head
from fleet_graph.dd.lifecycle import Lifecycle
from fleet_graph.graphs.dd_pipeline import StageRefused
from fleet_graph.graphs.dd_scripts import (
    ACCEPTANCE_PATH,
    MERGE_PATH,
    PREPARED,
    RUN_CONFIG_PATH,
    AcceptanceStage,
    ConfigureStage,
    MergeStage,
    WorkspaceSealer,
    write_json,
)

LIFECYCLE = Lifecycle.load()
CONFIGURE = LIFECYCLE.stages["configure"]
ACCEPTANCE = LIFECYCLE.stages["acceptance"]
MERGER = LIFECYCLE.stages["merger"]
STAMP = "2026-08-26T05:00:00Z"


def dispatch(**overrides: Any) -> dict[str, Any]:
    return {
        "development_id": "dev-001",
        "generation": 1,
        "attempt": 1,
        "attempt_started_at": STAMP,
        "input_commit": "1" * 40,
        **overrides,
    }


class TestConfigure:
    def test_it_writes_the_run_config(self, repo: Path) -> None:
        outcome = ConfigureStage(repo=repo, run_config={"acceptance_commands": [["true"]]}).act(
            CONFIGURE, dispatch()
        )

        written = json.loads((repo / RUN_CONFIG_PATH).read_text(encoding="utf-8"))
        assert written["development_id"] == "dev-001"
        assert written["acceptance_commands"] == [["true"]]
        assert outcome.produced == CONFIGURE.produced_artifacts


class TestAcceptance:
    def _configure(self, repo: Path, commands: list[list[str]]) -> None:
        ConfigureStage(repo=repo, run_config={"acceptance_commands": commands}).act(
            CONFIGURE, dispatch()
        )

    def _acceptance(self, repo: Path, commands: list[list[str]]) -> AcceptanceStage:
        """How build_pipeline wires it: one declaration, written down by
        configure and handed to acceptance."""
        return AcceptanceStage(repo=repo, declared=commands)

    def test_a_passing_run_records_what_it_ran(self, repo: Path) -> None:
        self._configure(repo, [["true"], ["echo", "hello"]])
        outcome = self._acceptance(repo, [["true"], ["echo", "hello"]]).act(ACCEPTANCE, dispatch())

        written = json.loads((repo / ACCEPTANCE_PATH).read_text(encoding="utf-8"))
        assert written["passed"] is True
        assert [entry["command"] for entry in written["results"]] == [
            ["true"],
            ["echo", "hello"],
        ]
        assert "hello" in written["results"][1]["stdout_tail"]
        assert outcome.produced == ACCEPTANCE.produced_artifacts

    def test_a_failing_command_refuses_and_still_records(self, repo: Path) -> None:
        """The run happened and the answer was no. That is a refusal, not a fault."""
        self._configure(repo, [["false"]])
        with pytest.raises(StageRefused, match="acceptance failed"):
            self._acceptance(repo, [["false"]]).act(ACCEPTANCE, dispatch())

        written = json.loads((repo / ACCEPTANCE_PATH).read_text(encoding="utf-8"))
        assert written["passed"] is False
        assert written["results"][0]["exit_code"] != 0

    def test_it_stops_at_the_first_missing_config(self, repo: Path) -> None:
        with pytest.raises(StageRefused, match="configure did not run"):
            AcceptanceStage(repo=repo).act(ACCEPTANCE, dispatch())

    def test_the_graded_cannot_edit_the_exam(self, repo: Path) -> None:
        """Measured before it was written: the same repo with a failing test
        went from refused to `passed: true` once the acceptance command in the
        worktree was replaced with `true`. The implementer's role grants
        `write: [worktree_path]`, so it can do exactly that."""
        self._configure(repo, [["false"]])
        write_json(repo, RUN_CONFIG_PATH, {"acceptance_commands": [["true"]]})

        with pytest.raises(StageRefused, match="nobody declared"):
            self._acceptance(repo, [["false"]]).act(ACCEPTANCE, dispatch())

        assert not (repo / ACCEPTANCE_PATH).exists(), "a refused acceptance records no verdict"

    def test_commands_appearing_from_nowhere_are_refused(self, repo: Path) -> None:
        """The empty declaration is not a wildcard: a run configured with no
        acceptance commands must not execute ones the worktree supplies."""
        write_json(repo, RUN_CONFIG_PATH, {"acceptance_commands": [["touch", "pwned"]]})

        with pytest.raises(StageRefused, match="nobody declared"):
            self._acceptance(repo, []).act(ACCEPTANCE, dispatch())

        assert not (repo / "pwned").exists()

    def test_no_declared_commands_passes_vacuously(self, repo: Path) -> None:
        """Declaring nothing to check is the caller's business, not a failure --
        but the record says plainly that nothing ran."""
        self._configure(repo, [])
        self._acceptance(repo, []).act(ACCEPTANCE, dispatch())
        written = json.loads((repo / ACCEPTANCE_PATH).read_text(encoding="utf-8"))
        assert written["results"] == []
        assert written["passed"] is True


class TestMerge:
    def test_it_prepares_without_touching_the_remote(self, repo: Path) -> None:
        """The default. Pushing to a durable ref is the one step here that
        cannot be undone, so it is opted into."""
        MergeStage(
            repo=repo, remote_url="https://example.invalid/r.git", target_ref="refs/heads/main"
        ).act(MERGER, dispatch())

        written = json.loads((repo / MERGE_PATH.format(generation=1)).read_text(encoding="utf-8"))
        assert written["result"] == PREPARED
        assert written["subject_commit"] == "1" * 40

    def test_prepared_is_a_declared_result_not_a_fudge(self) -> None:
        contracts = Path(__file__).parents[1] / "src/fleet_graph/dd/contracts"
        artifacts = json.loads((contracts / "stage-artifacts.json").read_text(encoding="utf-8"))[
            "artifact_kinds"
        ]
        assert PREPARED in artifacts["merge_result"]["required_fields"]["result"]

    def test_publishing_to_a_missing_remote_refuses(self, repo: Path) -> None:
        stage = MergeStage(
            repo=repo,
            remote_url=str(repo / "no-such-remote.git"),
            target_ref="refs/heads/main",
            publish=True,
        )
        with pytest.raises(StageRefused, match="merge refused"):
            stage.act(MERGER, dispatch(input_commit=head(repo)))


class TestWorkspaceSealer:
    def test_it_commits_what_the_stage_left_behind(self, repo: Path) -> None:
        before = head(repo)
        (repo / "note.txt").write_text("written by a stage\n", encoding="utf-8")

        sealed = WorkspaceSealer(repo=repo).materialize(
            CONFIGURE, dispatch(input_commit=before), _outcome()
        )

        assert sealed.commit == head(repo) != before
        assert sealed.receipt is not None
        assert sealed.receipt["output_commit"] == sealed.commit
        assert git(repo, "show", "--name-only", "--format=", "HEAD").strip() == "note.txt"

    def test_it_uses_the_frozen_attempt_time(self, repo: Path) -> None:
        WorkspaceSealer(repo=repo).materialize(CONFIGURE, dispatch(), _outcome())
        assert git(repo, "log", "-1", "--format=%aI").startswith("2026-08-26T05:00:00")

    def test_a_stage_that_wrote_nothing_still_moves_the_chain(self, repo: Path) -> None:
        """An empty commit keeps the forward chain intact rather than stalling
        it -- and the receipt still names a real commit."""
        before = head(repo)
        sealed = WorkspaceSealer(repo=repo).materialize(MERGER, dispatch(), _outcome())
        assert sealed.commit != before

    def test_it_publishes_to_the_durable_ref_when_told(self, repo: Path, tmp_path: Path) -> None:
        """Not the merge -- the chain. The plugin sealer verifies the remote
        head equals the commit it was handed, so a stage that seals locally
        and never publishes severs the link before the next stage runs."""
        bare = tmp_path / "durable.git"
        git(repo, "init", "-q", "--bare", str(bare))

        sealed = WorkspaceSealer(
            repo=repo, remote_url=str(bare), remote_ref="refs/heads/dev-1"
        ).materialize(CONFIGURE, dispatch(), _outcome())

        listed = git(repo, "ls-remote", str(bare), "refs/heads/dev-1")
        assert sealed.commit in listed

    def test_without_a_remote_it_only_commits(self, repo: Path) -> None:
        before = head(repo)
        sealed = WorkspaceSealer(repo=repo).materialize(CONFIGURE, dispatch(), _outcome())
        assert sealed.commit != before

    def test_a_declared_verdict_survives_into_the_receipt(self, repo: Path) -> None:
        sealed = WorkspaceSealer(repo=repo).materialize(
            CONFIGURE, dispatch(), _outcome({"verdict": "APPROVE"})
        )
        assert sealed.receipt is not None and sealed.receipt["verdict"] == "APPROVE"


def _outcome(receipt: dict[str, Any] | None = None) -> Any:
    from fleet_graph.graphs.dd_pipeline import StageOutcome

    return StageOutcome(receipt=receipt)


class TestAcceptanceContext:
    """The reconfigurable acceptance context (R1-c): setup commands run first,
    the env overlays both, and both are held to the same anti-tamper rule as
    the acceptance commands themselves."""

    def _wire(
        self,
        repo: Path,
        *,
        commands: list[list[str]],
        setup: list[list[str]] | None = None,
        env: dict[str, str] | None = None,
    ) -> AcceptanceStage:
        run_config = {
            "acceptance_commands": commands,
            "setup_commands": setup or [],
            "acceptance_env": env or {},
        }
        ConfigureStage(repo=repo, run_config=run_config).act(CONFIGURE, dispatch())
        return AcceptanceStage(repo=repo, declared=commands, setup=setup or [], env=env or {})

    def test_setup_runs_before_acceptance_and_its_products_are_visible(self, repo: Path) -> None:
        stage = self._wire(
            repo,
            commands=[["test", "-f", "prepared.txt"]],
            setup=[["touch", "prepared.txt"]],
        )
        stage.act(ACCEPTANCE, dispatch())
        record = json.loads((repo / ACCEPTANCE_PATH).read_text(encoding="utf-8"))
        assert record["passed"] is True
        assert record["setup_results"][0]["exit_code"] == 0

    def test_a_failing_setup_refuses_with_its_own_code_not_the_tests(self, repo: Path) -> None:
        stage = self._wire(repo, commands=[["true"]], setup=[["false"]])
        with pytest.raises(StageRefused, match="setup failed") as refused:
            stage.act(ACCEPTANCE, dispatch())
        assert refused.value.code == "SETUP_FAILED"
        record = json.loads((repo / ACCEPTANCE_PATH).read_text(encoding="utf-8"))
        assert record["passed"] is False
        assert record["results"] == []  # the tests never ran; nothing pretends they did

    def test_the_env_overlay_reaches_setup_and_acceptance(self, repo: Path) -> None:
        stage = self._wire(
            repo,
            commands=[["sh", "-c", 'test "$DD_ACCEPT_FLAG" = "on"']],
            env={"DD_ACCEPT_FLAG": "on"},
        )
        stage.act(ACCEPTANCE, dispatch())
        record = json.loads((repo / ACCEPTANCE_PATH).read_text(encoding="utf-8"))
        assert record["passed"] is True

    def test_a_failing_acceptance_names_its_own_code(self, repo: Path) -> None:
        stage = self._wire(repo, commands=[["false"]])
        with pytest.raises(StageRefused, match="acceptance failed") as refused:
            stage.act(ACCEPTANCE, dispatch())
        assert refused.value.code == "ACCEPTANCE_FAILED"

    def test_the_graded_cannot_edit_the_setup_either(self, repo: Path) -> None:
        stage = self._wire(repo, commands=[["true"]], setup=[["true"]])
        config = json.loads((repo / RUN_CONFIG_PATH).read_text(encoding="utf-8"))
        config["setup_commands"] = [["touch", "pwned"]]
        write_json(repo, RUN_CONFIG_PATH, config)
        with pytest.raises(StageRefused, match="nobody declared") as refused:
            stage.act(ACCEPTANCE, dispatch())
        assert refused.value.code == "ACCEPTANCE_DECLARATION_MISMATCH"
        assert not (repo / "pwned").exists()

    def test_the_graded_cannot_edit_the_env_either(self, repo: Path) -> None:
        stage = self._wire(repo, commands=[["true"]], env={"CI": "1"})
        config = json.loads((repo / RUN_CONFIG_PATH).read_text(encoding="utf-8"))
        config["acceptance_env"] = {"CI": "1", "PATH": "/tmp/evil"}
        write_json(repo, RUN_CONFIG_PATH, config)
        with pytest.raises(StageRefused, match="nobody declared"):
            stage.act(ACCEPTANCE, dispatch())

    def test_a_legacy_run_config_without_the_new_keys_still_accepts(self, repo: Path) -> None:
        """Pre-R1-c trees carry only acceptance_commands; absent keys mean
        empty declarations, not a mismatch."""
        write_json(
            repo,
            RUN_CONFIG_PATH,
            {"development_id": "dev-001", "generation": 1, "acceptance_commands": [["true"]]},
        )
        AcceptanceStage(repo=repo, declared=[["true"]]).act(ACCEPTANCE, dispatch())
        record = json.loads((repo / ACCEPTANCE_PATH).read_text(encoding="utf-8"))
        assert record["passed"] is True
