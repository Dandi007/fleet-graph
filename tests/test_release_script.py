"""在临时 Git 仓库和隔离命令替身中验证发布，不触及生产服务。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def release_case(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "deploy" / "release.sh"
    shutil.copyfile(source, repo / "deploy" / "release.sh")
    for argv in (
        ["git", "init", "-q"],
        ["git", "add", "."],
        [
            "git",
            "-c",
            "user.name=测试",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "测试发布脚本",
        ],
    ):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    app = tmp_path / "app"
    old = app / "releases" / "old"
    old.mkdir(parents=True)
    (app / "current").symlink_to(old)
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "uv").write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$RELEASE_TEST_LOG/uv"\n'
        '[ "${RELEASE_TEST_UV_FAIL:-0}" = 0 ] || exit 42\n'
        "mkdir -p .venv/bin\n"
        "printf '#!/bin/sh\\nexit 0\\n' > .venv/bin/fleet-graph\n"
        "chmod +x .venv/bin/fleet-graph\n"
    )
    (stub / "systemctl").write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,pathlib,sys\n"
        "root=pathlib.Path(os.environ['RELEASE_TEST_LOG'])\n"
        "args=sys.argv[1:]\n"
        "with (root/'systemctl').open('a') as f: f.write(json.dumps(args)+'\\n')\n"
        "if 'LoadState' in args: print('loaded')\n"
        "elif 'MainPID' in args: print(os.environ.get('RELEASE_TEST_PID','0'))\n"
        "elif 'restart' in args:\n"
        " marker=root/'restarted'\n"
        " if not marker.exists(): marker.touch(); sys.exit(1)\n"
    )
    for path in stub.iterdir():
        path.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{stub}:{os.environ['PATH']}",
        FLEET_GRAPH_APP_ROOT=str(app),
        RELEASE_TEST_LOG=str(tmp_path),
    )

    def run(*args: str, **overrides: str):
        return subprocess.run(
            ["bash", "deploy/release.sh", *args],
            cwd=repo,
            env={**env, **overrides},
            text=True,
            capture_output=True,
            timeout=15,
        )

    return repo, app, old, run


def test_build_failure_cleans_candidate_and_preserves_current(release_case):
    _, app, old, run = release_case
    result = run("--activate", RELEASE_TEST_UV_FAIL="1")
    assert result.returncode != 0
    assert (app / "current").resolve() == old
    assert list((app / "releases").iterdir()) == [old]
    assert not (app.parent / "systemctl").exists()


def test_no_flip_builds_with_frozen_dependencies_and_complete_marker(release_case):
    repo, app, old, run = release_case
    result = run("--no-flip")
    assert result.returncode == 0, result.stderr
    assert (app / "current").resolve() == old
    release = next(p for p in (app / "releases").iterdir() if p != old)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    assert (release / ".release-sha").read_text().strip() == sha
    assert (app.parent / "uv").read_text().splitlines() == ["sync --frozen --no-dev"]
    assert run("--no-flip").returncode == 0
    assert len((app.parent / "uv").read_text().splitlines()) == 1
    assert not (app.parent / "systemctl").exists()


def test_incomplete_existing_release_is_refused(release_case):
    _, app, old, run = release_case
    assert run("--no-flip").returncode == 0
    release = next(p for p in (app / "releases").iterdir() if p != old)
    (release / ".release-sha").unlink()
    result = run("--activate")
    assert result.returncode != 0
    assert "incomplete" in result.stderr
    assert (app / "current").resolve() == old
    assert not (app.parent / "systemctl").exists()


def test_activation_failure_restores_old_link_and_restarts_nine_units(release_case):
    _, app, old, run = release_case
    # 仅为 /proc/<pid>/cwd 核验提供真实进程，不启动任何服务或 agent。
    process = subprocess.Popen(["sleep", "30"], cwd=old)
    try:
        result = run("--activate", RELEASE_TEST_PID=str(process.pid))
    finally:
        process.terminate()
        process.wait(timeout=5)
    assert result.returncode != 0
    assert "restoring previous" in result.stderr
    assert (app / "current").resolve() == old
    calls = [json.loads(line) for line in (app.parent / "systemctl").read_text().splitlines()]
    restarts = [call for call in calls if "restart" in call]
    assert len(restarts) == 3
    assert restarts[0] == restarts[1]
    assert restarts[2] == ["--user", "restart", "fleet-graphd.service"]
    assert len([arg for arg in restarts[0] if arg.endswith(".service")]) == 8
    assert "fleet-graph-outer-gate-mcp.service" in restarts[0]
    assert "fleet-graph-arbiter.service" not in restarts[0]
