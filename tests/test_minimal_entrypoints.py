"""dd-28 entrypoint tests: console scripts, the minimal systemd unit, the runbook."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

from fleet_graph.minimal.mcptools import TOOL_NAMES

REPO_ROOT = Path(__file__).resolve().parent.parent


def _project_scripts() -> dict[str, str]:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return tomllib.loads(pyproject)["project"]["scripts"]


def test_console_script_targets_are_importable_and_callable() -> None:
    scripts = _project_scripts()
    for name in ("fleet-graph-minimal-engine", "fleet-graph-minimal-mcp"):
        assert name in scripts
        module_name, _, attr = scripts[name].partition(":")
        assert attr
        module = importlib.import_module(module_name)
        assert callable(getattr(module, attr))


def test_systemd_unit_has_execstart_and_working_directory() -> None:
    unit = REPO_ROOT / "deploy" / "systemd" / "fleet-graph-minimal-mcp.service"
    text = unit.read_text(encoding="utf-8")
    assert "ExecStart=" in text
    assert "WorkingDirectory=" in text


def test_runbook_lists_the_nine_tools() -> None:
    runbook = REPO_ROOT / "docs" / "minimal-runbook.md"
    text = runbook.read_text(encoding="utf-8")
    assert len(TOOL_NAMES) == 9
    for tool in TOOL_NAMES:
        assert tool in text
