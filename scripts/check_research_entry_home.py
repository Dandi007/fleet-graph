#!/usr/bin/env python3
"""R6 acceptance：三面统一路由 + 轻/重档分级 + 产物归位（机器可判，判据 ①②③）。

判据（approved.md「判据」节，对应 R6 三面）：

- **① 三面调用成立**：CLI 子命令（``research run`` / ``research serve``）、MCP
  tool（``research_run``）、skill（``skills/deep-research/SKILL.md``）三个入口都
  存在，且**指向同一路由**（都落到 ``research_entry.run_research_ticket``）。
  缺一判红。
- **② 轻/重档统一路由成立**：同一入口可发起 light/heavy 两档；路由判定纯函数
  ``resolve_tier`` 确定性（同输入恒得同档位）；两档产物 schema 一致
  （report + anchor-check 同 shape，仅 bounds 不同）。
- **③ 入口唯一 + 产物归位**：代码库无现役 ``bin/deep-research.sh`` /
  ``bin/deep-research-loop.sh`` / loop-engine drain 入口；且一次真实 run 的终验
  report 落 ``DeepThought/<topic>/``（wiki 域，可编程检索）。

自检（无参运行）：脚本先在**临时 fixture** 上执行全部判据（禁触真网/真库），
任一失当 exit 非零。``--run-root <dir>`` 可选对既有 run 产物复核归位判据。
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fleet_graph.cli import build_parser as build_cli_parser
from fleet_graph.graphs.research_pipeline import (
    ADVOCATE_ROLE,
    ARBITER_ROLE,
    JUDGE_ROLE,
    OPPONENT_ROLE,
    REPORT_FILE,
)
from fleet_graph.research_anchor import ANCHOR_CHECK_FILE
from fleet_graph.research_entry import (
    DEEP_THOUGHT_DIR,
    HEAVY_SCALE_THRESHOLD,
    TIER_BOUNDS,
    TIER_HEAVY,
    TIER_LIGHT,
    resolve_tier,
    run_research_ticket,
    topic_slug,
)
from fleet_graph.research_mcp import MCP_SERVER_NAME, build_research_mcp_server

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_FILE = REPO_ROOT / "skills" / "deep-research" / "SKILL.md"

QUESTION = "R6 三面统一路由与产物归位自检"
SEED_CLUES = ["单一 wiki 线索"]


def _fake_text_node() -> Any:
    class FakeTextNode:
        def complete(self, spec: Any, prompt: str) -> SimpleNamespace:
            return SimpleNamespace(
                text=json.dumps(SEED_CLUES), model="fake", finish_reason="stop", usage={}, raw={}
            )

    return FakeTextNode()


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


def run_one_ticket(tmp: Path, name: str, tier: str, wiki_root: Path) -> dict[str, Any]:
    """经统一入口跑一次真实 pipeline（fake text/launcher，temp run_root + wiki_root）。"""
    return run_research_ticket(
        QUESTION,
        tier=tier,
        run_root=tmp / name,
        wiki_root=wiki_root,
        text_node=_fake_text_node(),
        launcher=FakeLauncher(),
    )


# --- 判据 ①：三面探测 --------------------------------------------------------


def probe_cli() -> tuple[bool, dict[str, Any]]:
    """CLI 子命令存在且指向统一入口（_research_run 源码落到 run_research_ticket）。"""
    run = build_cli_parser().parse_args(["research", "run", "--question", "q?", "--tier", "light"])
    serve = build_cli_parser().parse_args(["research", "serve", "--wiki-root", "/tmp/w"])
    src = inspect.getsource(run.func)
    ok = run.tier == TIER_LIGHT and callable(run.func) and callable(serve.func)
    routed = "run_research_ticket" in src
    return (ok and routed), {"cli_run_parses": ok, "cli_routed_to_entry": routed}


def probe_mcp() -> tuple[bool, dict[str, Any]]:
    """MCP tool 存在且指向同一路由（tool 源码落到 run_research_ticket）。"""
    import asyncio

    server = build_research_mcp_server(wiki_root="/tmp/w")
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    has_tool = "research_run" in names
    src = inspect.getsource(build_research_mcp_server)
    routed = "run_research_ticket" in src
    verdict: dict[str, Any] = {
        "mcp_tool_present": has_tool,
        "mcp_routed_to_entry": routed,
        "server": MCP_SERVER_NAME,
    }
    return (has_tool and routed), verdict


def probe_skill() -> tuple[bool, dict[str, Any]]:
    """skill 入口存在且指向统一入口（SKILL.md 引用 run_research_ticket / CLI）。"""
    if not SKILL_FILE.is_file():
        return False, {"skill_file_present": False}
    text = SKILL_FILE.read_text(encoding="utf-8")
    has_file = True
    routed = "run_research_ticket" in text or "research run" in text
    return (has_file and routed), {"skill_file_present": has_file, "skill_routed_to_entry": routed}


# --- 判据 ②：轻/重档路由 -----------------------------------------------------


def probe_tier_routing() -> tuple[bool, dict[str, Any]]:
    """resolve_tier 纯函数确定性 + 两档 bounds 只差 bounds（schema 一致）。"""
    same_a = resolve_tier(scale=HEAVY_SCALE_THRESHOLD)
    same_b = resolve_tier(scale=HEAVY_SCALE_THRESHOLD)
    deterministic = same_a == same_b == TIER_HEAVY
    light = resolve_tier(scale=1)
    heavy = resolve_tier(scale=HEAVY_SCALE_THRESHOLD)
    distinct = (light, heavy) == (TIER_LIGHT, TIER_HEAVY)
    explicit = resolve_tier(tier=TIER_LIGHT) == TIER_LIGHT
    # 两档 bounds 键集合一致（同一 ResearchBounds schema）。
    keys_light = set(vars(TIER_BOUNDS[TIER_LIGHT]))
    keys_heavy = set(vars(TIER_BOUNDS[TIER_HEAVY]))
    same_schema = keys_light == keys_heavy
    return (
        deterministic and distinct and explicit and same_schema,
        {
            "deterministic": deterministic,
            "light_vs_heavy": [light, heavy],
            "explicit_tier": explicit,
            "same_bounds_schema": same_schema,
        },
    )


def probe_tier_product_alignment(tmp: Path) -> tuple[bool, dict[str, Any]]:
    """两档经同一入口各跑一遍：终态合法、产物 schema 一致、report 归位 wiki 域。"""
    wiki = tmp / "wiki"
    light_result = run_one_ticket(tmp, "run-light", TIER_LIGHT, wiki)
    heavy_result = run_one_ticket(tmp, "run-heavy", TIER_HEAVY, wiki)

    legal = {"converged", "capped", "partial"}
    light_legal = light_result.get("terminal") in legal
    heavy_legal = heavy_result.get("terminal") in legal
    both_legal = light_legal and heavy_legal

    light_root = tmp / "run-light"
    heavy_root = tmp / "run-heavy"
    light_shape = sorted(p.name for p in light_root.iterdir())
    heavy_shape = sorted(p.name for p in heavy_root.iterdir())
    schema_aligned = light_shape == heavy_shape and (
        REPORT_FILE in light_shape and ANCHOR_CHECK_FILE in light_shape
    )

    light_wiki = light_result.get("wiki") or {}
    heavy_wiki = heavy_result.get("wiki") or {}
    both_placed = light_wiki.get("placed") is True and heavy_wiki.get("placed") is True
    return (
        both_legal and schema_aligned and both_placed,
        {
            "light_terminal": light_result.get("terminal"),
            "heavy_terminal": heavy_result.get("terminal"),
            "schema_aligned": schema_aligned,
            "light_wiki_placed": light_wiki.get("placed"),
            "heavy_wiki_placed": heavy_wiki.get("placed"),
        },
    )


# --- 判据 ③：入口唯一 + 产物归位 ---------------------------------------------


def _scan_for_old_entries() -> list[str]:
    """扫现役入口引用（判据 ③ 前半）：实际的老入口脚本文件，或 src/deploy 里
    调用老 deep-research / loop-engine drain 的现役代码。

    ``scripts/`` 与 ``skills/`` 不扫——判据脚本与 skill 文档对老路径的**历史引用**
    是退役记录（spec「降级为历史引用或删除」），不是现役入口。
    """
    offenders: list[str] = []

    # 1) 实际存在的老入口脚本文件（bin/deep-research.sh / -loop.sh）。
    for pattern in ("bin/deep-research.sh", "bin/deep-research-loop.sh"):
        for path in REPO_ROOT.rglob(pattern):
            offenders.append(f"{path.relative_to(REPO_ROOT)}")

    # 2) src/ 与 deploy/ 里的现役代码/部署引用。
    banned = ("deep-research.sh", "deep-research-loop.sh", "loop-engine drain")
    scan_dirs = (REPO_ROOT / "src", REPO_ROOT / "deploy")
    for base in scan_dirs:
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix not in {".py", ".sh", ".json", ".yaml", ".yml", ".service"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for needle in banned:
                if needle in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {needle}")
    return sorted(set(offenders))


def probe_no_old_entries() -> tuple[bool, dict[str, Any]]:
    """代码库无现役老引擎入口（退役即退役，判据 ③ 前半）。"""
    offenders = _scan_for_old_entries()
    return (not offenders), {"old_entry_refs": offenders}


def probe_wiki_placement(tmp: Path) -> tuple[bool, dict[str, Any]]:
    """终验 run 报告落 DeepThought/<topic>/（wiki 域可编程检索，判据 ③ 后半）。"""
    wiki = tmp / "wiki"
    result = run_one_ticket(tmp, "run-placement", TIER_LIGHT, wiki)
    placed = result.get("wiki") or {}
    slug = topic_slug(QUESTION)
    topic_dir = wiki / DEEP_THOUGHT_DIR / slug
    report_md = list(topic_dir.glob("*.md"))
    anchor = topic_dir / ANCHOR_CHECK_FILE
    ok = bool(
        placed.get("placed") is True
        and topic_dir.is_dir()
        and bool(report_md)
        and anchor.is_file()
        and result.get("report")
    )
    return ok, {
        "topic": slug,
        "wiki_dir": str(topic_dir),
        "report_md_found": bool(report_md),
        "anchor_found": anchor.is_file(),
    }


def self_check() -> tuple[bool, dict[str, Any]]:
    """判据 ①②③ 全部在临时 fixture 上执行；任一判红 = 脚本自身无效。"""
    results: dict[str, Any] = {}
    ok_cli, results["cli"] = probe_cli()
    ok_mcp, results["mcp"] = probe_mcp()
    ok_skill, results["skill"] = probe_skill()
    ok_tier, results["tier_routing"] = probe_tier_routing()
    ok_old, results["no_old_entries"] = probe_no_old_entries()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ok_align, results["tier_product_alignment"] = probe_tier_product_alignment(tmp)
        ok_wiki, results["wiki_placement"] = probe_wiki_placement(tmp)
    ok_all = ok_cli and ok_mcp and ok_skill and ok_tier and ok_old and ok_align and ok_wiki
    results["pass"] = ok_all
    return ok_all, results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-root",
        help="对既有 run 产物复核归位判据（绿 exit 0、红 exit 非零）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.run_root:
        from fleet_graph.research_anchor import judge_run

        run_root = Path(args.run_root)
        ok, verdict = judge_run(run_root)
        verdict["pass"] = ok
        print(json.dumps(verdict, ensure_ascii=False, sort_keys=True))
        return 0 if ok else 1

    ok, results = self_check()
    print(json.dumps(results, ensure_ascii=False, sort_keys=True, default=str))
    if not ok:
        print("research-entry-home self_check: fail", file=sys.stderr)
        return 1
    print("research-entry-home self_check: pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
