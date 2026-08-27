"""The CLI surface: what it parses, and what it refuses to guess."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.cli import build_parser, plugin_binding_config


class TestTheDdSubcommand:
    def test_it_parses_a_minimal_run(self) -> None:
        args = build_parser().parse_args(
            [
                "dd",
                "run",
                "--development",
                "dev-1",
                "--workspace",
                "/tmp/w",
                "--plugin-binding",
                "/tmp/b.json",
                "--remote-url",
                "https://example.invalid/r.git",
                "--remote-ref",
                "refs/heads/main",
                "--root-digest",
                "sha256:" + "a" * 64,
            ]
        )
        assert args.development == "dev-1"
        assert args.generation == 1
        assert args.spec_commit is None, "defaults to the workspace HEAD"

    def test_publishing_is_off_unless_asked_for(self) -> None:
        base = [
            "dd",
            "run",
            "--development",
            "d",
            "--workspace",
            "/tmp/w",
            "--plugin-binding",
            "/tmp/b.json",
            "--remote-url",
            "u",
            "--remote-ref",
            "refs/heads/main",
            "--root-digest",
            "sha256:" + "a" * 64,
        ]
        assert build_parser().parse_args(base).publish_merge is False
        assert build_parser().parse_args([*base, "--publish-merge"]).publish_merge is True

    def test_resuming_without_a_checkpoint_is_refused(self) -> None:
        """An in-memory checkpointer has no thread to resume; starting over
        would re-dispatch stages that are already sealed."""
        from fleet_graph.cli import _dd_run

        args = build_parser().parse_args(
            [
                "dd",
                "run",
                "--development",
                "d",
                "--workspace",
                "/tmp/w",
                "--plugin-binding",
                "/tmp/b.json",
                "--remote-url",
                "u",
                "--remote-ref",
                "refs/heads/main",
                "--root-digest",
                "sha256:" + "a" * 64,
                "--resume",
            ]
        )
        assert args.resume is True
        with pytest.raises(SystemExit, match="--checkpoint"):
            _dd_run(args)

    def test_the_base_comes_from_the_committed_identity_not_from_head(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`dd bootstrap` then `dd run` must compose. HEAD has moved past the
        base by then, and a run that claimed HEAD is refused by the review
        sealer with BINDING_MISMATCH."""
        from conftest import git, head
        from fleet_graph.cli import _dd_run
        from fleet_graph.dd import vendor
        from fleet_graph.dd.bootstrap import build_attempt_context
        from fleet_graph.graphs import dd_runner

        workspace = tmp_path / "work"
        workspace.mkdir()
        git(workspace, "init", "-q", "-b", "main")
        (workspace / "seed.txt").write_text("x\n", encoding="utf-8")
        git(workspace, "add", "-A")
        git(workspace, "commit", "-q", "-m", "seed")
        base = head(workspace)
        build_attempt_context(development_id="d", spec=b"# spec\n", target_base_commit=base).write(
            workspace
        )
        git(workspace, "add", "-A")
        git(workspace, "commit", "-q", "-m", "bootstrap")

        seen: dict[str, Any] = {}
        monkeypatch.setattr(vendor.plugin_adapter, "load_plugin_binding", lambda config: object())
        monkeypatch.setattr(
            dd_runner,
            "run_pipeline",
            lambda config, **kwargs: seen.update(config=config) or {"terminal": "complete"},
        )
        binding = tmp_path / "b.json"
        binding.write_text("{}", encoding="utf-8")

        args = build_parser().parse_args(
            [
                "dd",
                "run",
                "--development",
                "d",
                "--workspace",
                str(workspace),
                "--plugin-binding",
                str(binding),
                "--remote-url",
                "u",
                "--remote-ref",
                "refs/heads/main",
                "--root-digest",
                "sha256:" + "a" * 64,
            ]
        )
        assert _dd_run(args) == 0
        config = seen["config"]
        assert config.target_base_commit == base
        assert config.head_commit == head(workspace), "the walk still starts at HEAD"
        assert config.target_base_commit != config.head_commit

    def test_stage_model_overrides_accumulate(self) -> None:
        args = build_parser().parse_args(
            [
                "dd",
                "run",
                "--development",
                "d",
                "--workspace",
                "/tmp/w",
                "--plugin-binding",
                "/tmp/b.json",
                "--remote-url",
                "u",
                "--remote-ref",
                "refs/heads/main",
                "--root-digest",
                "sha256:" + "a" * 64,
                "--stage-model",
                "continuous_review=deepseek-v4-pro",
                "--stage-model",
                "final_review=claude-opus-5",
            ]
        )
        assert dict(p.split("=", 1) for p in args.stage_model) == {
            "continuous_review": "deepseek-v4-pro",
            "final_review": "claude-opus-5",
        }

    def test_acceptance_commands_accumulate(self) -> None:
        args = build_parser().parse_args(
            [
                "dd",
                "run",
                "--development",
                "d",
                "--workspace",
                "/tmp/w",
                "--plugin-binding",
                "/tmp/b.json",
                "--remote-url",
                "u",
                "--remote-ref",
                "refs/heads/main",
                "--root-digest",
                "sha256:" + "a" * 64,
                "--accept",
                "make verify",
                "--accept",
                "pytest -q",
            ]
        )
        assert args.accept == ["make verify", "pytest -q"]

    @pytest.mark.parametrize(
        "missing", ["--development", "--workspace", "--plugin-binding", "--remote-ref"]
    )
    def test_the_required_arguments_are_required(self, missing: str) -> None:
        argv = [
            "dd",
            "run",
            "--development",
            "d",
            "--workspace",
            "/tmp/w",
            "--plugin-binding",
            "/tmp/b.json",
            "--remote-url",
            "u",
            "--remote-ref",
            "refs/heads/main",
            "--root-digest",
            "sha256:" + "a" * 64,
        ]
        index = argv.index(missing)
        del argv[index : index + 2]
        with pytest.raises(SystemExit):
            build_parser().parse_args(argv)


class TestThePluginBindingFile:
    def test_a_whole_config_is_taken_as_is(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"plugin_producer": {"root": "/x"}, "paths": {}}))
        assert plugin_binding_config(path)["plugin_producer"]["root"] == "/x"

    def test_a_bare_section_is_wrapped(self, tmp_path: Path) -> None:
        """So the file can be a copy of what dd already runs on."""
        path = tmp_path / "section.json"
        path.write_text(json.dumps({"root": "/x", "expected_commit": "a" * 40}))
        assert plugin_binding_config(path)["plugin_producer"]["expected_commit"] == "a" * 40

    def test_something_that_is_not_an_object_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "list.json"
        path.write_text(json.dumps(["not", "a", "config"]))
        with pytest.raises(ValueError, match="does not hold an object"):
            plugin_binding_config(path)


class TestTheOtherSubcommandsStillParse:
    def test_line_run(self) -> None:
        args = build_parser().parse_args(
            ["line", "run", "--folder", "wf-1", "--seat", "opencode-dsv4pro"]
        )
        assert args.folder == "wf-1"
        assert args.generation == 1
        # None -> the durable default under run_root, never ":memory:".
        assert args.checkpoint is None

    def test_line_run_generation(self) -> None:
        args = build_parser().parse_args(
            ["line", "run", "--folder", "wf-1", "--seat", "s", "--generation", "2"]
        )
        assert args.generation == 2

    def test_hello(self) -> None:
        assert build_parser().parse_args(["hello"]).topic


class TestLineRunAcceptance:
    def test_the_flag_defaults_to_none(self) -> None:
        args = build_parser().parse_args(["line", "run", "--folder", "wf-1", "--seat", "s"])
        assert args.acceptance_json is None

    def test_the_flag_parses_as_one_json_argument(self) -> None:
        declaration = json.dumps({"argvs": [["true"]], "cwd": "/tmp", "timeout_seconds": 60})
        args = build_parser().parse_args(
            ["line", "run", "--folder", "wf-1", "--seat", "s", "--acceptance-json", declaration]
        )
        assert json.loads(args.acceptance_json)["cwd"] == "/tmp"

    def test_an_unreadable_declaration_is_refused_not_degraded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Silently degrading to "not declared" would misreport a reviewed
        declaration as absent -- an operator error worth stopping on."""
        from fleet_graph.cli import main

        with pytest.raises(SystemExit, match="acceptance-json"):
            main(
                [
                    "line",
                    "run",
                    "--folder",
                    "wf-1",
                    "--seat",
                    "s",
                    "--acceptance-json",
                    "{not json",
                ]
            )
