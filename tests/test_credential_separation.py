"""凭证分离：决策 token 绝不进入任何 agent 子进程 env。

三条边各钉一根钉子：launcher 剥前缀（实测子进程 env）、control plane 的
env 白名单从不带上它（构造即隔离）、decision_publisher 的 env 名落在被剥
前缀之下（两个模块不靠 import 对齐，靠这条测试对齐）。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from fleet_graph.dd.control_plane import _inherited_environment
from fleet_graph.executors.agent_run import (
    DECISION_ENV_PREFIX,
    AgentRunLauncher,
    AgentRunSpec,
    scrubbed_environment,
)
from fleet_graph.supervise.decision_publisher import DECISION_TOKEN_ENV

ENV_DUMP_FAKE = """#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
opts = {}
i = 0
while i < len(argv):
    if argv[i] == "--":
        break
    if argv[i].startswith("--") and i + 1 < len(argv) and not argv[i + 1].startswith("--"):
        opts[argv[i]] = argv[i + 1]
        i += 2
        continue
    i += 1
from pathlib import Path
session_root = Path(opts["--session-root"])
run_dir = session_root / "run-1"
run_dir.mkdir(parents=True, exist_ok=True)
(run_dir / "env.json").write_text(json.dumps(dict(os.environ)))
(run_dir / "result.json").write_text(json.dumps({"state": "succeeded", "exit_code": 0}))
"""


class TestNamespaceAgreement:
    def test_decision_token_env_is_inside_the_scrubbed_prefix(self) -> None:
        assert DECISION_TOKEN_ENV.startswith(DECISION_ENV_PREFIX)

    def test_scrub_strips_the_namespace_and_keeps_the_rest(self) -> None:
        env = {
            "PATH": "/usr/bin",
            DECISION_TOKEN_ENV: "/run/decision.token",
            f"{DECISION_ENV_PREFIX}ANYTHING_FUTURE": "x",
            "FLEET_GRAPH_BUS_TOKEN_FILE": "/run/bus.token",
        }
        scrubbed = scrubbed_environment(env)
        assert DECISION_TOKEN_ENV not in scrubbed
        assert f"{DECISION_ENV_PREFIX}ANYTHING_FUTURE" not in scrubbed
        # 板凭证与 PATH 照常通过：剥的是决策命名空间，不是整个 env。
        assert scrubbed["FLEET_GRAPH_BUS_TOKEN_FILE"] == "/run/bus.token"
        assert scrubbed["PATH"] == "/usr/bin"


class TestLauncherSubprocessEnv:
    def test_spawned_agent_run_never_sees_the_decision_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        probe = tmp_path / "env_dump_fake.py"
        probe.write_text(ENV_DUMP_FAKE)
        fake = tmp_path / "agent-run"
        fake.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{probe}" "$@"\n')
        fake.chmod(0o755)
        monkeypatch.setenv(DECISION_TOKEN_ENV, str(tmp_path / "decision.token"))

        launcher = AgentRunLauncher(bin_path=str(fake), state_root=str(tmp_path / "runs"))
        ticket = launcher.launch(AgentRunSpec(prompt="hi"), "run-env-probe")
        status = launcher.wait(ticket, poll_interval=0.05, deadline_seconds=10)
        assert status.ok

        # 子进程落盘的是它真实看到的 env——不是我们构造的镜像。
        deadline = time.monotonic() + 5
        env_path = Path(ticket.session_root) / "run-1" / "env.json"
        while time.monotonic() < deadline and not env_path.exists():
            time.sleep(0.05)
        child_env = json.loads(env_path.read_text())
        assert DECISION_TOKEN_ENV not in child_env
        assert "PATH" in child_env


class TestControlPlaneWhitelist:
    def test_inherited_environment_never_forwards_the_decision_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(DECISION_TOKEN_ENV, "/run/decision.token")
        monkeypatch.setenv("FLEET_GRAPH_BUS_TOKEN_FILE", "/run/bus.token")
        env = _inherited_environment()
        assert DECISION_TOKEN_ENV not in env
        # 白名单本身照常工作：板凭证文件路径与 PATH 在。
        assert env["FLEET_GRAPH_BUS_TOKEN_FILE"] == "/run/bus.token"
        assert "PATH" in env
