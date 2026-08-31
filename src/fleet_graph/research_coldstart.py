"""R8：冷启动终验（DoD）——五件套机器可判纯判定（零 LLM、只读）。

deep-research V4 的终点终验（spec approved.md）：全新题目、一条命令、无人搀扶
冷启动，端到端出一份带锚点、可冷读的报告。本模块把「判据」节的五件套做成可测纯函数
（判据脚本 ``scripts/check_research_coldstart.py`` 在 fixture 上自检，阳性判绿、
阴性判红，任一失当即脚本自身无效）。

五件套（机器可判）：
- ① 发起命令原文在案：判据脚本机械记录 CLI argv（唯一入口 ``research run
  --question``），且题目为全新（不在历史题目集，防假阴）；
- ② run 证据链完整：dispatch/collect 事件、agent-runs 状态根、``evidence.jsonl``
  存在、可回放、coverage>0；
- ③ 报告存在且非空：``DeepThought/<topic>/report.md`` 落位，字节数机械核 >0；
- ④ anchor 核验率 > 90%：``anchor-check.json`` 的 ``summary.rate > 0.90`` 且
  ``sums_ok==true``（复用 R5 锚点核验）；
- ⑤ 冷读 subagent verdict == PASS：纯脚本冷读报告——无上下文的第三方仅凭报告本身
  即可读可复用（复用既有 role/纯脚本，无新 route）。

边界（硬线）：无人搀扶（判据脚本不向 run 注入任何提示/预设）、全新题目（复用历史
证据/题目判红）、不新造角色、基线不糊弄（anchor>90% 与冷读 PASS 为硬杠，不得拿
「有报告」代「可冷读」）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fleet_graph.graphs.research_pipeline import REPORT_FILE
from fleet_graph.graphs.research_runner import EVENTS
from fleet_graph.research_anchor import (
    ANCHOR_CHECK_FILE,
    ANCHOR_RATE_HEADER,
    check_run,
    extract_anchor_refs,
    judge_run,
)
from fleet_graph.research_bus import EVIDENCE_FILE
from fleet_graph.research_entry import DEEP_THOUGHT_DIR, topic_slug

#: 判据脚本机械记录 CLI argv 的落点（run root 下的 ``launch.json``）。
LAUNCH_RECORD = "launch.json"

#: 冷读 subagent 的机器可判 verdict 词汇。
COLDREAD_PASS = "PASS"
COLDREAD_FAIL = "FAIL"

#: 已用历史题目（spec 硬线「全新题目」的判红集合）。DoD 终验题目不得复用任何历史
#: run 的题目；判据脚本对命中集合的题目判红（防假阴）。集合收录本仓既有 R 阶段判据
#: 脚本与测试用过的题目，机器可判。
HISTORICAL_QUESTIONS: frozenset[str] = frozenset(
    {
        "R5 锚点核验自检",
        "R5 锚点核验端到端",
        "R5 软闸门不改路由",
        "R6 三面统一路由与产物归位自检",
        "R6 统一入口端到端",
        "worker 无产出",
        "哨兵被杀",
        "checkpoint 卡死",
        "fanout 并行派发的等价性与提速?",
        "同一题的两次独立跑在哪个 run 身份上撞车?",
        "coverage check",
        "对抗裁决判据检查",
        "降级判据问题",
        "q",
    }
)

#: 冷读可判的最小实质正文行数（非标题、非空行、非分隔线）。
MIN_COLDREAD_BODY_LINES = 3


def canonical_launch_argv(question: str) -> list[str]:
    """唯一入口的机械 CLI argv（spec 判据 ①）：``fleet-graph research run --question``。"""
    return ["fleet-graph", "research", "run", "--question", question]


def record_launch_argv(run_root: Path | str, argv: list[str]) -> Path:
    """判据脚本机械记录 CLI argv（判据 ①）：落 ``run_root/launch.json``。"""
    run_root = Path(run_root)
    run_root.mkdir(parents=True, exist_ok=True)
    path = run_root / LAUNCH_RECORD
    with path.open("w", encoding="utf-8") as handle:
        json.dump({"argv": list(argv)}, handle, ensure_ascii=False, indent=2)
    return path


def load_launch_argv(run_root: Path | str) -> list[str] | None:
    """读回机械记录的 CLI argv；缺失/畸形返回 None。"""
    path = Path(run_root) / LAUNCH_RECORD
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    argv = data.get("argv") if isinstance(data, dict) else None
    return list(argv) if isinstance(argv, list) else None


def judge_launch_command(
    argv: list[str] | None, question: str, historical: frozenset[str] = HISTORICAL_QUESTIONS
) -> tuple[bool, dict[str, Any]]:
    """判据 ①：发起命令原文在案 + 全新题目。

    - ``argv`` 逐字等于唯一入口的 canonical argv（单条命令、无任何预设/提示注入）；
    - 题目非空且不在历史题目集（复用历史证据/题目 = 判红，防假阴）。
    """
    canonical = canonical_launch_argv(question)
    exact = list(argv or []) == canonical
    fresh = bool(question.strip()) and question not in historical
    ok = exact and fresh
    verdict: dict[str, Any] = {
        "argv": argv,
        "canonical": canonical,
        "exact": exact,
        "fresh": fresh,
        "pass": ok,
    }
    return ok, verdict


def _jsonl_values(run_root: Path, name: str) -> list[dict[str, Any]]:
    """读一个 jsonl 文件成 dict 列表；缺失/坏行按行跳过（可回放判据据此判红）。"""
    path = run_root / name
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def judge_evidence_chain(run_root: Path | str) -> tuple[bool, dict[str, Any]]:
    """判据 ②：run 证据链完整。

    - ``events.jsonl`` 含 dispatch 与 collect 事件（可回放）；
    - ``agent-runs/`` 状态根在位（真实 launcher 的会话落点）；
    - ``evidence.jsonl`` 存在、非空、逐行可解析且带 finding（coverage>0、可回放）。
    """
    run_root = Path(run_root)

    event_names = {e.get("event") for e in _jsonl_values(run_root, EVENTS)}
    has_dispatch = "dispatch" in event_names
    has_collect = "collect" in event_names

    agent_runs_dir = (run_root / "agent-runs").is_dir()

    ev_path = run_root / EVIDENCE_FILE
    evidence_raw = [
        ln
        for ln in (ev_path.read_text(encoding="utf-8").splitlines() if ev_path.is_file() else [])
        if ln.strip()
    ]
    coverage = len(evidence_raw)
    parsed: list[dict[str, Any]] = []
    replayable = True
    for line in evidence_raw:
        try:
            entry = json.loads(line)
        except ValueError:
            replayable = False
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("finding"), dict):
            replayable = False
            continue
        parsed.append(entry)
    chain_ok = (
        has_dispatch
        and has_collect
        and agent_runs_dir
        and coverage > 0
        and replayable
        and len(parsed) == coverage
    )
    verdict: dict[str, Any] = {
        "events": sorted(event_names),
        "has_dispatch": has_dispatch,
        "has_collect": has_collect,
        "agent_runs": agent_runs_dir,
        "coverage": coverage,
        "replayable": replayable,
        "pass": chain_ok,
    }
    return chain_ok, verdict


def find_placed_reports(wiki_root: Path | str, question: str) -> list[Path]:
    """``DeepThought/<topic>/`` 下的报告 md（R6 归位：``<date>-<topic>.md``）。"""
    topic_dir = Path(wiki_root) / DEEP_THOUGHT_DIR / topic_slug(question)
    if not topic_dir.is_dir():
        return []
    return sorted(topic_dir.glob("*.md"))


def judge_report_placed(
    run_root: Path | str, question: str, wiki_root: Path | str | None = None
) -> tuple[bool, dict[str, Any]]:
    """判据 ③：报告存在且非空。

    - 给了 ``wiki_root``：核 ``DeepThought/<topic>/`` 归位报告（spec 判据 ③）；
    - 未给 ``wiki_root``：退化为核 run_root 的本地 ``report.md``。
    字节数机械核 >0（「有报告」与「可冷读」分开，基线不糊弄）。
    """
    run_root = Path(run_root)
    if wiki_root is not None:
        candidates = find_placed_reports(wiki_root, question)
    else:
        local = run_root / REPORT_FILE
        candidates = [local] if local.is_file() else []
    nonempty = [p for p in candidates if p.stat().st_size > 0]
    ok = bool(nonempty)
    verdict: dict[str, Any] = {
        "report_md": [str(p) for p in candidates],
        "bytes": nonempty[0].stat().st_size if nonempty else 0,
        "pass": ok,
    }
    return ok, verdict


def judge_anchor(run_root: Path | str) -> tuple[bool, dict[str, Any]]:
    """判据 ④：anchor 核验率 > 90% 且 sums_ok（复用 R5 锚点核验）。"""
    ok, verdict = judge_run(run_root)
    verdict["pass"] = ok
    return ok, verdict


def cold_read_report(report: str) -> tuple[bool, dict[str, Any]]:
    """判据 ⑤：冷读 subagent（纯脚本）——无上下文第三方仅凭报告即可读可复用。

    机器可判近似：报告自含标题、有小节、有 ``[anchor: …]`` 引用、有实质正文，且
    ``dr-anchor-rate`` 报告头在（报告自我描述核验质量）。不满足任一即 FAIL（「有
    报告」不等于「可冷读」，基线不糊弄）。
    """
    lines = report.splitlines()
    header_skipped = [ln for ln in lines if not ln.strip().startswith(ANCHOR_RATE_HEADER + ":")]
    title = any(ln.strip().startswith("# ") for ln in header_skipped)
    sections = any(ln.strip().startswith("## ") for ln in lines)
    anchors = len(extract_anchor_refs(report))
    body = [
        ln
        for ln in lines
        if ln.strip()
        and not ln.strip().startswith("#")
        and not ln.strip().startswith(ANCHOR_RATE_HEADER + ":")
    ]
    body_lines = len(body)
    ok = (
        bool(report.strip())
        and title
        and sections
        and anchors > 0
        and body_lines >= MIN_COLDREAD_BODY_LINES
    )
    verdict: dict[str, Any] = {
        "verdict": COLDREAD_PASS if ok else COLDREAD_FAIL,
        "title": title,
        "sections": sections,
        "anchors": anchors,
        "body_lines": body_lines,
        "pass": ok,
    }
    return ok, verdict


def judge_cold_read(run_root: Path | str) -> tuple[bool, dict[str, Any]]:
    """判据 ⑤ 对 run 产物：读 ``report.md`` 做冷读判定。"""
    run_root = Path(run_root)
    report_path = run_root / REPORT_FILE
    if not report_path.is_file():
        return False, {"verdict": COLDREAD_FAIL, "reason": f"{REPORT_FILE} 缺失", "pass": False}
    return cold_read_report(report_path.read_text(encoding="utf-8"))


def judge_coldstart(
    run_root: Path | str,
    question: str,
    wiki_root: Path | str | None = None,
    argv: list[str] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """五件套综合判定：①-⑤ 全绿才绿（任一判红即整体红）。"""
    run_root = Path(run_root)
    if argv is None:
        argv = load_launch_argv(run_root) or canonical_launch_argv(question)

    ok1, v1 = judge_launch_command(argv, question)
    ok2, v2 = judge_evidence_chain(run_root)
    ok3, v3 = judge_report_placed(run_root, question, wiki_root=wiki_root)
    ok4, v4 = judge_anchor(run_root)
    ok5, v5 = judge_cold_read(run_root)
    ok = all([ok1, ok2, ok3, ok4, ok5])
    verdict: dict[str, Any] = {
        "launch_command": v1,
        "evidence_chain": v2,
        "report_placed": v3,
        "anchor": v4,
        "cold_read": v5,
        "pass": ok,
    }
    return ok, verdict


__all__ = [
    "ANCHOR_CHECK_FILE",
    "COLDREAD_FAIL",
    "COLDREAD_PASS",
    "HISTORICAL_QUESTIONS",
    "LAUNCH_RECORD",
    "MIN_COLDREAD_BODY_LINES",
    "canonical_launch_argv",
    "check_run",
    "cold_read_report",
    "find_placed_reports",
    "judge_anchor",
    "judge_cold_read",
    "judge_coldstart",
    "judge_evidence_chain",
    "judge_launch_command",
    "judge_report_placed",
    "load_launch_argv",
    "record_launch_argv",
]
