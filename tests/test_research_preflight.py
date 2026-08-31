"""R7：部署契约 preflight 四面 + 失败语义哨兵（判据 ①②③）。

覆盖六类验证：
1. 四面纯判定：检出对齐 / 依赖齐 / role 可派 / channel 可建——已知好 fixture 判绿、
   坏 fixture 判红，缺一面判红（判据①）。
2. worker 无产出：fake worker 返回全空 → run 以响亮 fault 收尾（非 succeeded/exit 0、
   不判 converged），阴性 fixture（全空却判 converged）判红（判据②）。
3. 哨兵被杀：wait 中炸掉 → run fault（判据③前半）。
4. checkpoint 卡死：kill-restart 续跑仍炸 → run fault（判据③后半）。
5. 上游在案：立案号 dev-fg-67feadc91821 有跟踪文件引用（判据③「在案」）。
6. 判据脚本自检：`scripts/check_research_preflight.py` 无参运行 exit 0（判据 ①②③）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fleet_graph.executors.agent_run import RunStatus, RunTicket
from fleet_graph.graphs.research_runner import ResearchConfig, run_research
from fleet_graph.research_preflight import (
    REQUIRED_ROLE_YAMLS,
    judge_channel,
    judge_checkout,
    judge_deps,
    judge_role,
    preflight_green,
)
from fleet_graph.research_sentinel import (
    AGENT_RUNTIME_SEAT_CONTRACT_CASE,
    judge_loud,
    tracking_case_on_file,
    worker_output_all_empty,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_research_preflight.py"


class TestCheckoutFacet:
    def test_equal_heads_are_green(self) -> None:
        ok, _ = judge_checkout("abc123", "abc123")
        assert ok is True

    def test_mismatched_heads_are_red(self) -> None:
        ok, verdict = judge_checkout("abc123", "def456")
        assert ok is False
        assert verdict["aligned"] is False


class TestDepsFacet:
    def _write_role(self, root: Path, name: str) -> Path:
        path = root / f"{name}.yaml"
        path.write_text(f"role: {name}\nversion: 1\nruntime: claude\n", encoding="utf-8")
        return path

    def test_all_required_roles_present_are_green(self, tmp_path: Path) -> None:
        root = tmp_path / "roles"
        root.mkdir()
        for name in REQUIRED_ROLE_YAMLS:
            self._write_role(root, name)
        ok, verdict = judge_deps(root, venv_ok=True, uv_ok=True)
        assert ok is True
        assert verdict["problems"] == []

    def test_missing_role_is_red(self, tmp_path: Path) -> None:
        root = tmp_path / "roles"
        root.mkdir()
        for name in REQUIRED_ROLE_YAMLS[1:]:
            self._write_role(root, name)
        ok, verdict = judge_deps(root, venv_ok=True, uv_ok=True)
        assert ok is False
        assert any("missing" in p for p in verdict["problems"])

    def test_missing_venv_is_red(self, tmp_path: Path) -> None:
        root = tmp_path / "roles"
        root.mkdir()
        for name in REQUIRED_ROLE_YAMLS:
            self._write_role(root, name)
        ok, _ = judge_deps(root, venv_ok=False, uv_ok=True)
        assert ok is False

    def test_unparseable_role_is_red(self, tmp_path: Path) -> None:
        root = tmp_path / "roles"
        root.mkdir()
        for name in REQUIRED_ROLE_YAMLS:
            self._write_role(root, name)
        (root / "dr-arbiter.yaml").write_text("role: [unclosed\n  broken", encoding="utf-8")
        ok, verdict = judge_deps(root, venv_ok=True, uv_ok=True)
        assert ok is False
        assert any("unparseable" in p for p in verdict["problems"])


class TestRoleFacet:
    def _write_role(self, root: Path, name: str, *, runtime: str = "claude") -> Path:
        path = root / f"{name}.yaml"
        path.write_text(f"role: {name}\nversion: 1\nruntime: {runtime}\n", encoding="utf-8")
        return path

    def test_runtime_declared_are_green(self, tmp_path: Path) -> None:
        root = tmp_path / "roles"
        root.mkdir()
        for name in REQUIRED_ROLE_YAMLS:
            self._write_role(root, name)
        ok, _ = judge_role(root)
        assert ok is True

    def test_missing_runtime_is_red(self, tmp_path: Path) -> None:
        root = tmp_path / "roles"
        root.mkdir()
        self._write_role(root, "dr-arbiter", runtime="")
        for name in REQUIRED_ROLE_YAMLS[1:]:
            self._write_role(root, name)
        ok, verdict = judge_role(root)
        assert ok is False
        assert any("no runtime" in p for p in verdict["problems"])


class TestChannelFacet:
    def test_creatable_channel_is_green(self) -> None:
        ok, _ = judge_channel(lambda: None)
        assert ok is True

    def test_refused_channel_is_red(self) -> None:
        def refuse() -> None:
            raise RuntimeError("403")

        ok, verdict = judge_channel(refuse)
        assert ok is False
        assert "403" in verdict["error"]


class TestMissingFacetIsRed:
    def test_any_red_facet_makes_preflight_red(self) -> None:
        assert preflight_green({"checkout": True, "deps": True, "role": True, "channel": True})
        assert not preflight_green({"checkout": True, "deps": True, "role": True, "channel": False})
        assert not preflight_green({"checkout": True, "deps": True, "role": False, "channel": True})
        assert not preflight_green({"checkout": False, "deps": True, "role": True, "channel": True})


class _FakeTextNode:
    def __init__(self, seed_text: str) -> None:
        self.seed_text = seed_text

    def complete(self, spec: Any, prompt: str) -> SimpleNamespace:
        return SimpleNamespace(
            text=self.seed_text, model="fake", finish_reason="stop", usage={}, raw={}
        )


def _debater_payload(body: str) -> dict[str, Any]:
    return {"state": "succeeded", "exit_code": 0, "structured_result": {"body": body}}


def _arbiter_payload() -> dict[str, Any]:
    return {
        "state": "succeeded",
        "exit_code": 0,
        "structured_result": {"verdict": "enough", "rationale": "证据已充分"},
    }


class _Boom(RuntimeError):
    pass


class _FakeLauncher:
    def __init__(self, worker_payloads: list[dict[str, Any]] | None = None, *, boom: bool = False):
        from fleet_graph.graphs.research_pipeline import ARBITER_ROLE, DEBATE_ROLES

        self._arbiter = ARBITER_ROLE
        self._debate = set(DEBATE_ROLES)
        self.workers = list(worker_payloads or [])
        self.boom = boom
        self._roles: dict[str, str] = {}

    def launch(self, spec: Any, run_id: str) -> RunTicket:
        self._roles[run_id] = spec.role
        return RunTicket(run_id, f"/tmp/test-preflight/{run_id}", None)

    def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
        if self.boom:
            raise _Boom("sentinel killed during worker run")
        role = self._roles[ticket.run_id]
        if role in self._debate:
            if role == self._arbiter:
                return RunStatus("succeeded", _arbiter_payload())
            return RunStatus("succeeded", _debater_payload(f"#{role} 论证\npreflight 通过。"))
        payload = self.workers.pop(0)
        return RunStatus(
            "succeeded", {"state": "succeeded", "exit_code": 0, "structured_result": payload}
        )


def _empty_worker_payload() -> dict[str, Any]:
    return {"evidences": [], "proposed_clues": [], "materials": []}


class TestWorkerNoOutputSentinel:
    def test_all_empty_predicate(self) -> None:
        assert worker_output_all_empty(_empty_worker_payload())
        assert not worker_output_all_empty({"evidences": [{"claim": "c"}], "proposed_clues": []})
        assert not worker_output_all_empty(None)

    def test_empty_worker_output_faults_loudly(self, tmp_path: Path) -> None:
        result = run_research(
            ResearchConfig(question="worker 无产出", run_root=tmp_path / "run"),
            text_node=_FakeTextNode(json.dumps(["clue one"])),
            launcher=_FakeLauncher([_empty_worker_payload()]),
        )
        loud, verdict = judge_loud(result)
        assert loud is True
        assert result["terminal"] == "fault"
        assert result["terminal"] != "converged"
        assert verdict["exit_zero"] is False
        assert not (tmp_path / "run" / "evidence.jsonl").exists()

    def test_negative_fixture_judged_red(self) -> None:
        fake_silent: dict[str, Any] = {
            "terminal": "converged",
            "terminal_reason": "coverage 收敛",
        }
        loud, verdict = judge_loud(fake_silent)
        assert loud is False
        assert verdict["exit_zero"] is True


class TestSentinelKilled:
    def test_boom_during_wait_faults(self, tmp_path: Path) -> None:
        result = run_research(
            ResearchConfig(question="哨兵被杀", run_root=tmp_path / "run"),
            text_node=_FakeTextNode(json.dumps(["clue one"])),
            launcher=_FakeLauncher(boom=True),
        )
        loud, _ = judge_loud(result)
        assert loud is True
        assert result["terminal"] == "fault"


class TestCheckpointStuck:
    def test_stuck_checkpoint_resume_faults(self, tmp_path: Path) -> None:
        from langgraph.checkpoint.sqlite import SqliteSaver

        from fleet_graph.graphs.research_runner import build_research, resume_start

        run_root = tmp_path / "run"
        run_root.mkdir(parents=True)
        config = ResearchConfig(question="checkpoint 卡死", run_root=run_root)
        cfg = {"configurable": {"thread_id": config.thread_id}, "recursion_limit": 100}

        boom = _FakeLauncher(boom=True)
        graph, _deps = build_research(
            config, text_node=_FakeTextNode(json.dumps(["clue one"])), launcher=boom
        )
        with SqliteSaver.from_conn_string(config.resolved_checkpoint_path) as saver:
            compiled = graph.compile(checkpointer=saver)
            with pytest.raises(_Boom):
                compiled.invoke(resume_start(compiled, cfg, config), config=cfg)

        result = run_research(
            ResearchConfig(question="checkpoint 卡死", run_root=run_root),
            text_node=_FakeTextNode(json.dumps(["clue one"])),
            launcher=_FakeLauncher(boom=True),
        )
        loud, _ = judge_loud(result)
        assert loud is True
        assert result["terminal"] == "fault"


class TestTrackingCase:
    def test_agent_runtime_case_referenced_in_docs(self) -> None:
        assert tracking_case_on_file() is True
        assert AGENT_RUNTIME_SEAT_CONTRACT_CASE == "dev-fg-67feadc91821"


class TestCriterionScript:
    def test_self_check_exits_zero(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(CHECK_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert proc.returncode == 0, proc.stderr
        assert "self_check: pass" in proc.stdout
