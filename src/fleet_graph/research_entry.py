"""R6：deep-research 唯一入口 —— CLI / MCP tool / skill 三面统一路由。

把 deep-research 的调用入口收敛到唯一一套路由（宪法条6「可及性」/ 条8「使用闭环」/
条9「分级」）：CLI 子命令、MCP tool、skill 三个 surface 全部落到同一个 runner
（``run_research_ticket``），按规模统一分轻/重档；终验产物（report）归位到 wiki 域
``DeepThought/<topic>/``（遵 ``wf-3f87f3`` 先例的命名纪律），run_root 仍保留中间态
（evidence.jsonl 等），归位在 finalise 侧、不破坏 R1 双源对账。

落地约定（spec 判据）：

- 路由判定是**纯函数/确定性**：``resolve_tier`` 同输入恒得同档位（机器可核验）。
- 轻/重档差异**只体现在 bounds**（``max_clues/max_depth/zero_growth_rounds/
  max_rounds/concurrency``），不派生两套产物 schema（条9「格式对齐」）。
- 三面共享同一 runner：CLI ``research run --tier ...``、MCP tool ``research_run``、
  skill 全部经 ``run_research_ticket`` 发起，不各写各的入口。
- wiki 域落点由本仓按共性判别铁律派生（``topic_slug`` + ``default_wiki_root``），
  禁止跨仓硬编码老 ``DeepThought``/katana 路径。
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from fleet_graph.graphs.research_pipeline import (
    DEFAULT_SOURCES,
    REPORT_FILE,
    ResearchBounds,
    derive_research_id,
)
from fleet_graph.graphs.research_runner import ResearchConfig, run_research
from fleet_graph.research_anchor import ANCHOR_CHECK_FILE

#: 档位词汇（spec 判据 ②：同一入口可发起 light/heavy 两档）。
TIER_LIGHT = "light"
TIER_HEAVY = "heavy"
TIERS = (TIER_LIGHT, TIER_HEAVY)

#: 规模阈值：scale（默认 = sources 数）>= 该值判重档（wf-3f87f3 C2 先例：
#: 「重档路由 sources 4 >= 阈值 4」）。
HEAVY_SCALE_THRESHOLD = 4

#: 轻/重档的 bounds —— 两档只差 bounds，产物 schema 完全一致（条9「格式对齐」）。
#: light = session 内分钟级（wf-3f87f3 C2 分流）；heavy = V2 全编排档。
TIER_BOUNDS: dict[str, ResearchBounds] = {
    TIER_LIGHT: ResearchBounds(
        max_clues=6,
        max_depth=4,
        zero_growth_rounds=3,
        max_rounds=12,
        concurrency=2,
    ),
    TIER_HEAVY: ResearchBounds(
        max_clues=24,
        max_depth=10,
        zero_growth_rounds=3,
        max_rounds=48,
        concurrency=8,
    ),
}

#: 默认 wiki 域根（物理 /data/vault/DeepThought/）。可经环境变量覆盖；测试注入
#: 临时根，禁触真网/真库。本仓只派生命名，不硬编码跨仓 katana 路径。
DEFAULT_WIKI_ROOT = "/data/vault"
WIKI_ROOT_ENV = "FLEET_GRAPH_WIKI_ROOT"
DEEP_THOUGHT_DIR = "DeepThought"

_CJK_RANGES = (
    (0x4E00, 0x9FFF),
    (0x3400, 0x4DBF),
    (0xF900, 0xFAFF),
)


def default_wiki_root() -> Path:
    """wiki 域根：``FLEET_GRAPH_WIKI_ROOT`` 环境变量优先，缺省 ``/data/vault``。"""
    raw = os.environ.get(WIKI_ROOT_ENV) or DEFAULT_WIKI_ROOT
    return Path(raw)


def topic_slug(question: str) -> str:
    """题面 -> 目录/文件名可用的确定性 slug（wf-3f87f3 命名纪律）。

    保留 CJK 与 ASCII 字母数字，其余字符折叠为连字符，连续连字符合并、去首尾。
    同一题面恒得同一 slug（机器可判）。
    """
    out: list[str] = []
    for ch in question.strip():
        if ch.isalnum():
            out.append(ch.lower())
        else:
            out.append("-")
    slug = re.sub(r"-+", "-", "".join(out)).strip("-")
    return slug or "research"


def resolve_tier(scale: int | None = None, *, tier: str | None = None) -> str:
    """轻/重档路由判定——纯函数、确定性（spec 判据 ②：同输入恒得同档位）。

    - 显式 ``tier``（light/heavy）优先，非法值抛 ``ValueError``；
    - 否则按规模（缺省 = ``DEFAULT_SOURCES`` 数）判定：``scale >= 4`` -> heavy。
    路由语义在入口/finalise 侧，不触碰 ``converge()`` 的路由语义（边界硬线）。
    """
    if tier is not None:
        if tier not in TIERS:
            raise ValueError(f"unknown tier {tier!r}; must be one of {TIERS}")
        return tier
    n = scale if scale is not None else len(DEFAULT_SOURCES)
    return TIER_HEAVY if n >= HEAVY_SCALE_THRESHOLD else TIER_LIGHT


def tier_bounds(tier: str) -> ResearchBounds:
    """档位对应的 bounds；两档产物 schema 一致，只差 bounds。"""
    if tier not in TIER_BOUNDS:
        raise ValueError(f"unknown tier {tier!r}; must be one of {TIERS}")
    return TIER_BOUNDS[tier]


def default_run_root(question: str) -> Path:
    """默认 run_root 由题面内容寻址派生（与 CLI 的 default_research_run_root 同构）。"""
    return Path("/data/fleet-graph/research") / derive_research_id(question)


def place_report(
    run_root: Path | str,
    question: str,
    *,
    wiki_root: Path | str | None = None,
    clock: Any = None,
) -> dict[str, Any]:
    """终验 report 归位到 wiki 域 ``DeepThought/<topic>/``（finalise 侧）。

    命名纪律（wf-3f87f3 先例）：``DeepThought/<topic-slug>/<date>-<topic-slug>.md``
    另附 ``anchor-check.json``。run_root 仍保留中间态（本地 report.md / evidence.jsonl
    等原样不动），故不破坏 R1 双源对账。无 report.md（fault 路径）不归位，返回
    ``placed=false``。纯 IO，不判档位、不改 converge 路由。
    """
    run_root = Path(run_root)
    root = Path(wiki_root) if wiki_root is not None else default_wiki_root()
    report_path = run_root / REPORT_FILE
    if not report_path.is_file():
        return {"placed": False, "reason": "no report.md", "dir": None}
    slug = topic_slug(question)
    topic_dir = root / DEEP_THOUGHT_DIR / slug
    topic_dir.mkdir(parents=True, exist_ok=True)
    now = clock or time.time
    date = time.strftime("%Y-%m-%d", time.gmtime(now()))
    dest = topic_dir / f"{date}-{slug}.md"
    shutil.copy2(report_path, dest)
    placed_anchor = False
    anchor_path = run_root / ANCHOR_CHECK_FILE
    if anchor_path.is_file():
        shutil.copy2(anchor_path, topic_dir / ANCHOR_CHECK_FILE)
        placed_anchor = True
    return {
        "placed": True,
        "topic": slug,
        "dir": str(topic_dir),
        "report": str(dest),
        "anchor_placed": placed_anchor,
        "date": date,
    }


def run_research_ticket(
    question: str,
    *,
    tier: str | None = None,
    scale: int | None = None,
    run_root: Path | str | None = None,
    wiki_root: Path | str | None = None,
    generation: int = 1,
    sources: list[str] | None = None,
    max_clues: int | None = None,
    concurrency: int | None = None,
    publisher: Any = None,
    text_node: Any = None,
    launcher: Any = None,
    clock: Any = None,
    checkpoint: str | None = None,
    instance: str | None = None,
) -> dict[str, Any]:
    """三面共享的 research 入口：CLI / MCP tool / skill 全部落在这里。

    - 解析档位（``resolve_tier`` 纯函数）-> 取对应 bounds 装配 ``ResearchConfig``；
      ``max_clues`` / ``concurrency`` 显式给出时覆盖档位 bounds（CLI 老参数的
      等价物），未给则用档位缺省。
    - 跑 ``run_research``（同一既有 runner + 12 个 dr-* 角色，无新 route）；
    - finalise 侧归位：终验 report 落 ``DeepThought/<topic>/``（wiki 域可检索）。
    返回 ``run_research`` 的 result，追加 ``tier`` 与 ``wiki`` 归位记录。
    """
    resolved = resolve_tier(scale=scale, tier=tier)
    bounds = tier_bounds(resolved)
    run_root = Path(run_root) if run_root is not None else default_run_root(question)
    src = list(sources) if sources is not None else list(DEFAULT_SOURCES)
    config = ResearchConfig(
        question=question,
        run_root=run_root,
        generation=generation,
        max_clues=max_clues if max_clues is not None else bounds.max_clues,
        max_depth=bounds.max_depth,
        zero_growth_rounds=bounds.zero_growth_rounds,
        max_rounds=bounds.max_rounds,
        concurrency=concurrency if concurrency is not None else bounds.concurrency,
        checkpoint_path=checkpoint,
        instance=instance,
        sources=src,
    )
    result = run_research(
        config,
        text_node=text_node,
        launcher=launcher,
        clock=clock,
        publisher=publisher,
    )
    result["tier"] = resolved
    # finalise 侧归位：只在有 report 时落 wiki 域（fault 路径不归位）。
    result["wiki"] = place_report(run_root, question, wiki_root=wiki_root, clock=clock)
    return result


__all__ = [
    "ANCHOR_CHECK_FILE",
    "DEEP_THOUGHT_DIR",
    "DEFAULT_SOURCES",
    "DEFAULT_WIKI_ROOT",
    "HEAVY_SCALE_THRESHOLD",
    "REPORT_FILE",
    "TIERS",
    "TIER_BOUNDS",
    "TIER_HEAVY",
    "TIER_LIGHT",
    "WIKI_ROOT_ENV",
    "default_run_root",
    "default_wiki_root",
    "place_report",
    "resolve_tier",
    "run_research_ticket",
    "tier_bounds",
    "topic_slug",
]
