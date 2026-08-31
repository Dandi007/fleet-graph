"""R6：三面统一入口 + 轻/重档分级 + 产物归位（DeepThought/<topic>）。

覆盖五类验证：
1. 路由纯函数：``resolve_tier`` 确定性（同输入恒得同档位）、显式档位、非法档位拒绝；
   两档只差 bounds、产物 schema 一致（条9「格式对齐」）。
2. 统一入口：``run_research_ticket`` 跑通 light/heavy 两档，result 带 ``tier`` 与
   ``wiki`` 归位记录，fault 路径不归位。
3. 产物归位：``place_report`` 落 ``DeepThought/<topic>/``（遵 wf-3f87f3 命名纪律：
   ``<date>-<topic>.md`` + ``anchor-check.json``），run_root 中间态原样保留。
4. MCP surface：``build_research_mcp_server`` 注册 ``research_run`` tool，走同一入口。
5. 判据脚本自检：``scripts/check_research_entry_home.py`` 无参 exit 0（判据 ①②③）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fleet_graph.research_anchor import ANCHOR_CHECK_FILE
from fleet_graph.research_entry import (
    DEEP_THOUGHT_DIR,
    DEFAULT_SOURCES,
    HEAVY_SCALE_THRESHOLD,
    REPORT_FILE,
    TIER_BOUNDS,
    TIER_HEAVY,
    TIER_LIGHT,
    TIERS,
    place_report,
    resolve_tier,
    run_research_ticket,
    tier_bounds,
    topic_slug,
)
from fleet_graph.research_mcp import MCP_SERVER_NAME, build_research_mcp_server

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_research_entry_home.py"

QUESTION = "R6 统一入口端到端"
SEED_CLUES = ["单一 wiki 线索"]


class FakeTextNode:
    def __init__(self, seed_text: str = json.dumps(SEED_CLUES)) -> None:
        self.seed_text = seed_text

    def complete(self, spec: Any, prompt: str) -> SimpleNamespace:
        return SimpleNamespace(
            text=self.seed_text, model="fake", finish_reason="stop", usage={}, raw={}
        )


class FakeLauncher:
    """worker / debate 四角色的确定性回放（不碰真实 agent-run / bus）。"""

    def __init__(self) -> None:
        from fleet_graph.executors.agent_run import RunStatus, RunTicket

        self._roles: dict[str, str] = {}
        self._launched: set[str] = set()
        self.RunStatus = RunStatus
        self.RunTicket = RunTicket

    def launch(self, spec: Any, run_id: str) -> Any:
        if run_id not in self._launched:
            self._launched.add(run_id)
            self._roles[run_id] = spec.role
        return self.RunTicket(run_id, f"/tmp/r6/{run_id}", None)

    def wait(self, ticket: Any, **kwargs: Any) -> Any:
        from fleet_graph.graphs.research_pipeline import (
            ADVOCATE_ROLE,
            ARBITER_ROLE,
            JUDGE_ROLE,
            OPPONENT_ROLE,
        )

        role = self._roles[ticket.run_id]
        if role == ARBITER_ROLE:
            payload: dict[str, Any] = {"verdict": "enough", "rationale": "证据已充分"}
        elif role in {ADVOCATE_ROLE, OPPONENT_ROLE, JUDGE_ROLE}:
            body = "RULE: 分歧一 裁决：wiki 证据成立 [anchor: wiki@fake.md:1]"
            if role != JUDGE_ROLE:
                body = "# body\n支持。"
            payload = {"body": body}
        else:
            payload = {
                "evidences": [
                    {
                        "quote": "引文一",
                        "claim": "结论一",
                        "source": "wiki",
                        "locator": "fake.md:1",
                    }
                ],
                "proposed_clues": [],
                "materials": [],
            }
        return self.RunStatus(
            "succeeded",
            {"state": "succeeded", "exit_code": 0, "structured_result": payload},
        )


class TestResolveTier:
    def test_deterministic_same_input_same_tier(self) -> None:
        assert (
            resolve_tier(scale=HEAVY_SCALE_THRESHOLD)
            == resolve_tier(scale=HEAVY_SCALE_THRESHOLD)
            == TIER_HEAVY
        )

    def test_scale_routes_light_and_heavy(self) -> None:
        assert resolve_tier(scale=1) == TIER_LIGHT
        assert resolve_tier(scale=HEAVY_SCALE_THRESHOLD) == TIER_HEAVY

    def test_default_scale_is_sources_count(self) -> None:
        # 缺省 scale = len(DEFAULT_SOURCES)；DEFAULT_SOURCES 数 >= 阈值 -> heavy。
        assert resolve_tier() == TIER_HEAVY

    def test_explicit_tier_wins(self) -> None:
        assert resolve_tier(scale=HEAVY_SCALE_THRESHOLD, tier=TIER_LIGHT) == TIER_LIGHT

    def test_unknown_tier_refused(self) -> None:
        with pytest.raises(ValueError, match="tier"):
            resolve_tier(tier="mid")

    def test_tiers_share_bounds_schema(self) -> None:
        keys = set(vars(TIER_BOUNDS[TIER_LIGHT]))
        assert set(vars(TIER_BOUNDS[TIER_HEAVY])) == keys
        # 只差 bounds，不派生两套产物 schema。
        assert set(vars(tier_bounds(TIER_LIGHT))) == set(vars(tier_bounds(TIER_HEAVY)))


class TestTopicSlug:
    def test_deterministic_and_slugified(self) -> None:
        assert topic_slug("A  B!C?") == "a-b-c"
        assert topic_slug("中文 标题 with spaces") == "中文-标题-with-spaces"
        assert topic_slug("   ") == "research"
        assert topic_slug(QUESTION) == topic_slug(QUESTION)


class TestPlaceReport:
    def test_places_report_and_anchor_under_deep_thought(self, tmp_path: Path) -> None:
        run_root = tmp_path / "run"
        run_root.mkdir()
        (run_root / REPORT_FILE).write_text("# report\n", encoding="utf-8")
        (run_root / ANCHOR_CHECK_FILE).write_text('{"ok": true}\n', encoding="utf-8")

        placed = place_report(run_root, "some topic", wiki_root=tmp_path / "wiki")

        assert placed["placed"] is True
        slug = topic_slug("some topic")
        topic_dir = tmp_path / "wiki" / DEEP_THOUGHT_DIR / slug
        assert topic_dir.is_dir()
        assert list(topic_dir.glob("*.md"))
        assert (topic_dir / ANCHOR_CHECK_FILE).is_file()
        # run_root 中间态原样保留（不破坏 R1 双源对账）。
        assert (run_root / REPORT_FILE).is_file()
        assert (run_root / ANCHOR_CHECK_FILE).is_file()

    def test_missing_report_not_placed(self, tmp_path: Path) -> None:
        run_root = tmp_path / "run"
        run_root.mkdir()
        placed = place_report(run_root, "no report", wiki_root=tmp_path / "wiki")
        assert placed["placed"] is False


class TestRunResearchTicket:
    def _run(self, tmp_path: Path, tier: str, name: str) -> dict[str, Any]:
        return run_research_ticket(
            QUESTION,
            tier=tier,
            run_root=tmp_path / name,
            wiki_root=tmp_path / "wiki",
            text_node=FakeTextNode(),
            launcher=FakeLauncher(),
        )

    def test_light_and_heavy_both_reach_legal_terminal(self, tmp_path: Path) -> None:
        light = self._run(tmp_path, TIER_LIGHT, "run-light")
        heavy = self._run(tmp_path, TIER_HEAVY, "run-heavy")
        legal = {"converged", "capped", "partial"}
        assert light["terminal"] in legal
        assert heavy["terminal"] in legal
        assert light["tier"] == TIER_LIGHT
        assert heavy["tier"] == TIER_HEAVY

    def test_both_tiers_place_report_into_wiki(self, tmp_path: Path) -> None:
        light = self._run(tmp_path, TIER_LIGHT, "run-light")
        heavy = self._run(tmp_path, TIER_HEAVY, "run-heavy")
        assert light["wiki"]["placed"] is True
        assert heavy["wiki"]["placed"] is True
        slug = topic_slug(QUESTION)
        topic_dir = tmp_path / "wiki" / DEEP_THOUGHT_DIR / slug
        assert topic_dir.is_dir()
        assert (topic_dir / ANCHOR_CHECK_FILE).is_file()

    def test_run_root_keeps_intermediates(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, TIER_LIGHT, "run-light")
        run_root = tmp_path / "run-light"
        assert (run_root / REPORT_FILE).is_file()
        assert (run_root / ANCHOR_CHECK_FILE).is_file()
        assert result["report"] == str(run_root / REPORT_FILE)

    def test_fault_run_does_not_place(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fleet_graph import research_entry

        def boom_launcher(*a: Any, **k: Any) -> Any:
            raise RuntimeError("boom")

        monkeypatch.setattr(research_entry, "run_research", lambda *a, **k: {"terminal": "fault"})
        result = run_research_ticket(
            QUESTION, tier=TIER_LIGHT, run_root=tmp_path / "run", wiki_root=tmp_path / "wiki"
        )
        assert result["terminal"] == "fault"
        assert result["wiki"]["placed"] is False


class TestResearchMcpSurface:
    def test_research_run_tool_is_registered(self) -> None:
        import asyncio

        server = build_research_mcp_server(wiki_root="/tmp/w")
        tools = asyncio.run(server.list_tools())
        assert MCP_SERVER_NAME == "fleet-graph-research"
        assert "research_run" in {tool.name for tool in tools}

    def test_tool_is_not_on_dd_or_goal_faces(self) -> None:
        import asyncio

        from fleet_graph.dd.service import build_mcp_server
        from test_dd_service import FakeControlPlane

        dd_server = build_mcp_server(FakeControlPlane())
        dd_tools = {tool.name for tool in asyncio.run(dd_server.list_tools())}
        assert "research_run" not in dd_tools


class TestCli:
    def test_research_run_parses_tier(self) -> None:
        from fleet_graph.cli import build_parser

        args = build_parser().parse_args(["research", "run", "--question", "q?", "--tier", "light"])
        assert args.tier == TIER_LIGHT
        assert args.scale is None

    def test_research_serve_parses(self) -> None:
        from fleet_graph.cli import build_parser

        args = build_parser().parse_args(["research", "serve", "--wiki-root", "/tmp/w"])
        assert args.port == 5612

    def test_research_run_routes_to_unified_entry(self) -> None:
        import inspect

        from fleet_graph.cli import _research_run

        assert "run_research_ticket" in inspect.getsource(_research_run)


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


class TestTierBounds:
    def test_bounds_differ_between_tiers(self) -> None:
        light = TIER_BOUNDS[TIER_LIGHT]
        heavy = TIER_BOUNDS[TIER_HEAVY]
        # 两档同 schema，只差 bounds——任何一档的字段都被重档覆盖即两档不区分。
        assert light != heavy
        assert set(TIERS) == {TIER_LIGHT, TIER_HEAVY}
        assert DEFAULT_SOURCES  # sources 词汇在（路由 scale 来源）
