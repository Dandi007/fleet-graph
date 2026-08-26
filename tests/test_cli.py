"""The CLI surface: what it parses, and what it refuses to guess."""

from __future__ import annotations

import json
from pathlib import Path

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

    def test_hello(self) -> None:
        assert build_parser().parse_args(["hello"]).topic
