"""验证入单模型与验收脚本经过真实启动入口后不失真。"""

import json
from pathlib import Path

import pytest

from fleet_graph.cli import _dd_run, build_parser


@pytest.mark.parametrize(
    "record, error",
    [
        ({"development_id": "d", "seats": {"implement": "deepseek-v4-pro"}}, None),
        ({"development_id": "d"}, None),
        (
            {
                "development_id": "d",
                "seats": {
                    "implement": "deepseek-v4-pro",
                    "continuous_review": "glm-5.3",
                    "final_review": "gpt-6-astra",
                },
            },
            None,
        ),
        (None, None),
        ({"development_id": "other", "seats": {}}, "development_id"),
        ({"development_id": "d", "seats": []}, "seats"),
        ({"development_id": "d", "seats": {"implement": ""}}, "seats"),
        (None, "unreadable"),
    ],
)
def test_record_models_reach_pipeline_or_refuse_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record: dict | None, error: str | None
) -> None:
    from fleet_graph.dd.vendor import plugin_adapter
    from fleet_graph.graphs import dd_runner

    binding = tmp_path / "binding.json"
    binding.write_text("{}")
    record_path = tmp_path / "record.json"
    if record is not None:
        record_path.write_text(json.dumps(record))
    seen = []
    monkeypatch.setattr(plugin_adapter, "load_plugin_binding", lambda _: object())
    monkeypatch.setattr(
        dd_runner,
        "run_pipeline",
        lambda config, **kw: seen.append(config) or {"terminal": "complete"},
    )
    args = build_parser().parse_args(
        [
            "dd",
            "run",
            "--development",
            "d",
            "--workspace",
            str(tmp_path),
            "--plugin-binding",
            str(binding),
            "--remote-url",
            "unused",
            "--remote-ref",
            "refs/heads/release/test",
            "--root-digest",
            "sha256:" + "a" * 64,
            "--target-base",
            "a" * 40,
            "--spec-commit",
            "b" * 40,
            *(["--record-file", str(record_path)] if record is not None or error else []),
        ]
    )
    if error:
        with pytest.raises(SystemExit, match=error):
            _dd_run(args)
        assert not seen
    else:
        assert _dd_run(args) == 0
        assert seen[0].models == (record or {}).get("seats", {})


def test_scripts_and_environment_round_trip_through_launch(tmp_path: Path) -> None:
    import shlex

    from fleet_graph.cli import _env_pairs
    from fleet_graph.dd.control_plane import DdLaunchSpec

    commands = [["sh", "-c", 'printf "%s" "http://127.0.0.1:${PORT}/$PATH_PART with spaces"']]
    setup = [["sh", "-c", 'printf "%s" "$VALUE ${VALUE}"']]
    env = {"PORT": "43210", "VALUE": "a b", "PATH_PART": "mcp"}
    spec = DdLaunchSpec(
        development_id="d",
        dev_root=tmp_path,
        workspace=tmp_path,
        plugin_binding=tmp_path / "binding.json",
        remote_url="unused",
        remote_ref="refs/heads/release/test",
        root_digest="sha256:" + "a" * 64,
        target_base_commit="a" * 40,
        acceptance_commands=commands,
        setup_commands=setup,
        acceptance_env=env,
    )
    argv = spec.argv()
    assert "--expand-environment=no" in argv
    args = build_parser().parse_args(argv[argv.index(spec.executable) + 1 :])
    assert [shlex.split(c) for c in args.accept] == commands
    assert [shlex.split(c) for c in args.setup] == setup
    assert _env_pairs(args.accept_env) == env
