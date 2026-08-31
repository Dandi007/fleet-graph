"""R2 acceptance：deep-research 多源 worker 矩阵的配套单测（规格第 10 条）。

覆盖规格要求的五类验证：
1. ``SOURCE_ROLE`` 逐字等于 6 个已交付 role 名（不新造 / 改名角色）;
2. dispatch 按 ``clue.source`` 选 role：fake launcher 记录每次派发 ``spec.role``，
   跨 ≥3 源（含 web）的 run 必须路由到对应的 dr-worker-* 角色;
3. collect 正确消费 ``worker.result.v1`` 的 evidences / proposed_clues，并断言
   worker input 文件是 ``deep-research.worker-input/v1`` 形状（含 ``sources``）;
4. ``derive_clue_id(text, source)``：同 text 不同 source 得不同 id，``source=None``
   与 R1 一致;
5. 未知 source 回填默认源、绝不 fault 整图。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

from fleet_graph.executors.agent_run import RunStatus, RunTicket, derive_run_id
from fleet_graph.graphs.research_pipeline import (
    DEFAULT_SOURCE,
    DEFAULT_SOURCES,
    SOURCE_ROLE,
    SYNTHESIS_ROLE,
    TERMINAL_CONVERGED,
    derive_clue_id,
)
from fleet_graph.graphs.research_runner import ResearchConfig, run_research


class FakeTextNode:
    """seed 的替身：回放脚本化文本。"""

    def __init__(self, seed_text: str) -> None:
        self.seed_text = seed_text

    def complete(self, spec: Any, prompt: str) -> SimpleNamespace:
        return SimpleNamespace(
            text=self.seed_text, model="fake", finish_reason="stop", usage={}, raw={}
        )


def worker_payload(claims: list[str], proposed: list[str]) -> dict[str, Any]:
    """worker.result.v1 形状：无 verdict / 无 clue_id（R2-fix，完成由 evidences 判定）。"""
    return {
        "evidences": [
            {"claim": c, "source": "wiki", "quote": c, "locator": f"fake.md:{i + 1}"}
            for i, c in enumerate(claims)
        ],
        "proposed_clues": [{"clue": t, "reason": "测试线索"} for t in proposed],
        "materials": [],
    }


def worker_result(claims: list[str], proposed: list[str]) -> dict[str, Any]:
    return {
        "state": "succeeded",
        "exit_code": 0,
        "structured_result": worker_payload(claims, proposed),
    }


def synthesis_result(report: str) -> dict[str, Any]:
    return {
        "state": "succeeded",
        "exit_code": 0,
        "structured_result": {
            "report_markdown": report,
            "coverage_summary": "全部 clue 有证据支撑",
            "unresolved": [],
        },
    }


class FakeLauncher:
    """按 role 回放脚本，并记录每次派发的 ``spec.role``（R2 路由判据）。

    ``launch`` 幂等（R3）：同 run_id 重复 launch = re-adopt 在途 run，只记录第一次。
    """

    def __init__(self, worker_script: list[Any], synthesis: dict[str, Any]) -> None:
        self.worker_script = list(worker_script)
        self.synthesis = synthesis
        self.dispatched: list[str] = []
        self.specs: dict[str, Any] = {}
        self._roles: dict[str, str] = {}
        self._launched: set[str] = set()

    def launch(self, spec: Any, run_id: str) -> RunTicket:
        if run_id in self._launched:
            return RunTicket(run_id, f"/tmp/fake/{run_id}", None, adopted=True)
        self._launched.add(run_id)
        self._roles[run_id] = spec.role
        self.specs[run_id] = spec
        self.dispatched.append(run_id)
        return RunTicket(run_id, f"/tmp/fake/{run_id}", None)

    def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
        role = self._roles[ticket.run_id]
        if role == SYNTHESIS_ROLE:
            return RunStatus("succeeded", self.synthesis)
        item = self.worker_script.pop(0)
        return RunStatus("succeeded", item)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestSourceRoleMatrix:
    """规格单测 1：SOURCE_ROLE 逐字等于 6 个已交付 role 名。"""

    EXPECTED: ClassVar[dict[str, str]] = {
        "code-local": "dr-worker-code-local",
        "code-remote": "dr-worker-code-remote",
        "wiki": "dr-worker-wiki",
        "feishu": "dr-worker-feishu",
        "content": "dr-worker-content",
        "web": "dr-worker-web",
    }

    def test_source_role_verbatim_matches_delivered_roles(self) -> None:
        assert SOURCE_ROLE == self.EXPECTED
        assert set(SOURCE_ROLE) == set(DEFAULT_SOURCES)
        # 不新造 / 改名 / 重注册角色：value 必须逐字等于已交付 role 名。
        assert len(set(SOURCE_ROLE.values())) == 6

    def test_default_source_is_matrix_first_element(self) -> None:
        assert DEFAULT_SOURCE == DEFAULT_SOURCES[0] == "code-local"


class TestDeriveClueIdSource:
    """规格单测 4：derive_clue_id(text, source) 的寻址语义。"""

    def test_same_text_different_source_gives_different_ids(self) -> None:
        web_id = derive_clue_id("同一题面", "web")
        wiki_id = derive_clue_id("同一题面", "wiki")
        assert web_id != wiki_id
        assert web_id == derive_clue_id("同一题面", "web")  # 确定性

    def test_source_none_matches_r1(self) -> None:
        # source=None 按 text 内容寻址，与 R1 完全一致（向后兼容）。
        r1_id = derive_clue_id("scheduler 的基本循环")
        assert r1_id.startswith("c-")
        assert derive_clue_id("scheduler 的基本循环", None) == r1_id
        # 带 source 的 id 与 R1 裸 id 不同（不互相顶撞）。
        assert derive_clue_id("scheduler 的基本循环", DEFAULT_SOURCE) != r1_id


class TestDispatchRoutesBySource:
    """规格单测 2：dispatch 按 clue.source 选 role，fake launcher 记录 spec.role。"""

    def _run(self, seed_items: list[Any], tmp_path: Path) -> FakeLauncher:
        seed = FakeTextNode(json.dumps(seed_items))
        launcher = FakeLauncher(
            [worker_result(["f"], []) for _ in seed_items],
            synthesis_result("report"),
        )
        config = ResearchConfig(question="q", run_root=tmp_path / "run")
        result = run_research(config, text_node=seed, launcher=launcher)
        assert result["terminal"] == TERMINAL_CONVERGED
        return launcher

    def test_each_seed_source_routes_to_its_own_role(self, tmp_path: Path) -> None:
        seed_items = [
            {"text": "web 线索", "source": "web"},
            {"text": "wiki 线索", "source": "wiki"},
            {"text": "code-local 线索", "source": "code-local"},
        ]
        launcher = self._run(seed_items, tmp_path)
        thread = ResearchConfig(question="q", run_root=tmp_path / "run").thread_id

        # 每个 seed clue 派发到 SOURCE_ROLE[source] 对应角色。
        for item in seed_items:
            clue_id = derive_clue_id(item["text"], item["source"])
            run_id = derive_run_id(thread, f"worker/{clue_id}", 1)
            assert launcher.specs[run_id].role == SOURCE_ROLE[item["source"]]

        # 一次 run 实际覆盖 ≥3 种 source，且 web ≥1 次（修复「web 派发数恒为 0」）。
        dispatched_sources = {SOURCE_ROLE[item["source"]] for item in seed_items}
        assert len(dispatched_sources) >= 3
        assert SOURCE_ROLE["web"] in dispatched_sources

    def test_plain_string_seed_backfills_default_source(self, tmp_path: Path) -> None:
        # 纯字符串 seed 回填默认源（code-local），路由到 dr-worker-code-local。
        launcher = self._run(["plain clue"], tmp_path)
        thread = ResearchConfig(question="q", run_root=tmp_path / "run").thread_id
        clue_id = derive_clue_id("plain clue", DEFAULT_SOURCE)
        run_id = derive_run_id(thread, f"worker/{clue_id}", 1)
        assert launcher.specs[run_id].role == SOURCE_ROLE[DEFAULT_SOURCE]


class TestCollectWorkerResultV1:
    """规格单测 3：collect 消费 worker.result.v1 的 evidences / proposed_clues。"""

    def test_worker_input_file_is_deep_research_worker_input_v1(self, tmp_path: Path) -> None:
        seed = FakeTextNode(json.dumps([{"text": "web 线索", "source": "web"}]))
        # 父线索 + 子线索两轮：第二个 worker result 无子线索，收敛。
        launcher = FakeLauncher(
            [
                worker_result(["web 事实"], ["web 子线索"]),
                worker_result(["web 子事实"], []),
            ],
            synthesis_result("report"),
        )
        config = ResearchConfig(question="q", run_root=tmp_path / "run")
        result = run_research(config, text_node=seed, launcher=launcher)
        assert result["terminal"] == TERMINAL_CONVERGED

        clue = derive_clue_id("web 线索", "web")
        thread = config.thread_id
        spec = launcher.specs[derive_run_id(thread, f"worker/{clue}", 1)]
        # deep-research.worker-input/v1 形状：clue_id / clue_text / depth / sources。
        payload = json.loads(Path(spec.input_path).read_text(encoding="utf-8"))
        assert payload == {
            "clue_id": clue,
            "clue_text": "web 线索",
            "depth": 0,
            "sources": ["web"],
        }
        # 子线索继承父 source 生成新 clue id（同 text 不同源不顶撞）。
        child = derive_clue_id("web 子线索", "web")
        assert child != clue
        child_spec = launcher.specs[derive_run_id(thread, f"worker/{child}", 1)]
        assert child_spec.role == SOURCE_ROLE["web"]

    def test_collect_consumes_evidences_and_proposed_clues(self, tmp_path: Path) -> None:
        seed = FakeTextNode(json.dumps(["clue one"]))
        launcher = FakeLauncher(
            [
                worker_result(["事实 A", "事实 B"], ["子线索 X"]),
                worker_result(["子事实"], []),
            ],
            synthesis_result("report"),
        )
        config = ResearchConfig(question="q", run_root=tmp_path / "run")
        result = run_research(config, text_node=seed, launcher=launcher)
        assert result["terminal"] == TERMINAL_CONVERGED

        # evidences 归一化为 R1 evidence 逐条 append evidence.jsonl。
        evidence = read_jsonl(tmp_path / "run" / "evidence.jsonl")
        assert [e["finding"]["claim"] for e in evidence] == ["事实 A", "事实 B", "子事实"]
        # proposed_clues 进入 harvest 生成子线索并继续派发。
        thread = config.thread_id
        child = derive_clue_id("子线索 X", DEFAULT_SOURCE)
        assert launcher.specs[derive_run_id(thread, f"worker/{child}", 1)]


class TestUnknownSourceFallback:
    """规格单测 5：未知 source 回填默认源、绝不 fault 整图。"""

    def test_unknown_source_backfills_default_and_run_converges(self, tmp_path: Path) -> None:
        seed = FakeTextNode(json.dumps([{"text": "神秘源线索", "source": "not-a-source"}]))
        launcher = FakeLauncher([worker_result(["f"], [])], synthesis_result("report"))
        config = ResearchConfig(question="q", run_root=tmp_path / "run")

        result = run_research(config, text_node=seed, launcher=launcher)

        assert result["terminal"] == TERMINAL_CONVERGED  # 不 fault 整图
        thread = config.thread_id
        clue = derive_clue_id("神秘源线索", DEFAULT_SOURCE)
        run_id = derive_run_id(thread, f"worker/{clue}", 1)
        # 回填默认源后路由到默认源角色。
        assert launcher.specs[run_id].role == SOURCE_ROLE[DEFAULT_SOURCE]

    def test_missing_source_backfills_default(self, tmp_path: Path) -> None:
        # dict 缺 source 字段：回填默认源（缺失源属 clue 级降级）。
        seed = FakeTextNode(json.dumps([{"text": "无源线索"}]))
        launcher = FakeLauncher([worker_result(["f"], [])], synthesis_result("report"))
        config = ResearchConfig(question="q", run_root=tmp_path / "run")
        result = run_research(config, text_node=seed, launcher=launcher)
        assert result["terminal"] == TERMINAL_CONVERGED
        thread = config.thread_id
        clue = derive_clue_id("无源线索", DEFAULT_SOURCE)
        assert (
            launcher.specs[derive_run_id(thread, f"worker/{clue}", 1)].role
            == SOURCE_ROLE[DEFAULT_SOURCE]
        )
