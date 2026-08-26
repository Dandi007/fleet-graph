"""Shared fixtures for the dd tests: a real git repo shaped like a development."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.dd.upstream_constants import ATTEMPT_CONTEXT_CONTRACT_VERSION

DEVELOPMENT_ID = "dev-001"
SPEC_PATH = ".dev-dispatch/spec/approved.md"
INDEX_PATH = ".dev-dispatch/feedback/index.json"


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=test",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(repo),
            *args,
        ],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    workspace = tmp_path / "work"
    workspace.mkdir()
    git(workspace, "init", "-q", "-b", "main")
    write_index(workspace, entries=[])
    (workspace / SPEC_PATH).parent.mkdir(parents=True, exist_ok=True)
    (workspace / SPEC_PATH).write_text("# approved spec\n", encoding="utf-8")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-q", "-m", "seed")
    return workspace


def write_index(
    workspace: Path,
    *,
    entries: list[Any],
    development_id: str = DEVELOPMENT_ID,
    contract_version: str = ATTEMPT_CONTEXT_CONTRACT_VERSION,
) -> None:
    path = workspace / INDEX_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "contract_version": contract_version,
                "development_id": development_id,
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")
