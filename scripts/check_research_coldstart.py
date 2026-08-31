#!/usr/bin/env python3
"""R8 acceptance：冷启动终验（DoD）五件套判据脚本（判绿 / 判红 + 自检）。

判据（approved.md「判据」节，五件套，机器可判）：
- ① 发起命令原文在案：判据脚本机械记录 CLI argv（唯一入口 ``research run
  --question``），且题目为全新（不在历史题目集，防假阴）；
- ② run 证据链完整：events.jsonl 含 dispatch/collect、agent-runs 状态根、
  evidence.jsonl 存在、可回放、coverage>0；
- ③ 报告存在且非空：``DeepThought/<topic>/report.md`` 落位，字节数机械核 >0；
- ④ anchor 核验率 > 90%：``anchor-check.json`` 的 ``summary.rate > 0.90`` 且
  ``sums_ok==true``；
- ⑤ 冷读 subagent verdict == PASS：纯脚本冷读报告（无上下文第三方可读可复用）。

用法：
- 无参数：自检（判据 ⑥），打印可解析输出，全绿 exit 0、任一失当 exit 非零
  （acceptance 入口 ``uv run python scripts/check_research_coldstart.py``）。
- ``--run-root <dir>``（可选 ``--wiki-root <dir>`` / ``--question <q>`` / ``--argv``
  逗号分隔）：对既有 run 产物执行五件套判据，绿 exit 0、红 exit 非零。

输出（自检）：``positive=green|red`` / ``negative_launch=red|green`` /
``negative_chain=red|green`` / ``negative_report=red|green`` /
``negative_anchor=red|green`` / ``negative_coldread=red|green`` /
``self_check=pass|fail``。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from fleet_graph.graphs.research_pipeline import REPORT_FILE
from fleet_graph.research_anchor import ANCHOR_CHECK_FILE, check_run
from fleet_graph.research_bus import EVIDENCE_FILE
from fleet_graph.research_coldstart import (
    canonical_launch_argv,
    judge_anchor,
    judge_cold_read,
    judge_coldstart,
    judge_evidence_chain,
    judge_launch_command,
    judge_report_placed,
    record_launch_argv,
)
from fleet_graph.research_entry import topic_slug

QUESTION = "R8 冷启动终验：全新题目一条命令无人搀扶，端到端出带锚点可冷读报告"
ARGV = canonical_launch_argv(QUESTION)

#: 锚点词汇（结论行与 evidence 共用；逐条命中 → rate=1.0）。
ANCHORS = [f"wiki@fake.md:{i}" for i in range(1, 7)]


def _finding(anchor: str) -> dict[str, str]:
    source, _, locator = anchor.partition("@")
    return {
        "claim": f"结论 {anchor}",
        "source": source,
        "quote": f"引文 {anchor}",
        "locator": locator,
    }


def _write_evidence(run_root: Path, anchors: list[str]) -> None:
    with (run_root / EVIDENCE_FILE).open("a", encoding="utf-8") as handle:
        for anchor in anchors:
            entry = {
                "at": "2026-09-01T00:00:00Z",
                "clue_id": "c-1",
                "depth": 0,
                "finding": _finding(anchor),
            }
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _write_events(run_root: Path) -> None:
    with (run_root / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event in ("dispatch", "collect", "harvest"):
            handle.write(json.dumps({"event": event, "at": "2026-09-01T00:00:00Z"}) + "\n")


def _anchored_report(question: str) -> str:
    """冷读可 PASS 且 anchor 核验率 >90% 的合法终验报告。

    每条 claim 行（结论 / RULE / verdict / rationale / 开放分歧）都带可命中 evidence
    的 ``[anchor: …]``，check_run 后 rate=1.0（>0.90）且 sums_ok；标题 + 小节 +
    锚点 + 实质正文齐全 → 冷读 PASS。标题与 markdown 标题行不计入 claim（R5 机械切分）。
    """
    lines = [f"# {question}", ""]
    for i, anchor in enumerate(ANCHORS, 1):
        lines.append(f"- 结论 {i} [anchor: {anchor}]")
    lines.extend(["", "## 分歧裁定", "", "### 已裁定分歧"])
    for i, anchor in enumerate(ANCHORS, 1):
        lines.append(f"- RULE: 分歧 {i} 裁决：wiki 证据成立 [anchor: {anchor}]")
    lines.extend(["", "### 开放分歧"])
    lines.append(f"- OPEN DISAGREEMENT: 分歧 7 留待后续 [anchor: {ANCHORS[0]}]")
    lines.extend(["", "### arbiter 裁决"])
    lines.append(f"- verdict: enough [anchor: {ANCHORS[0]}]")
    lines.append(f"- rationale: 证据已充分 [anchor: {ANCHORS[0]}]")
    return "\n".join(lines)


def _write_full_run(run_root: Path) -> None:
    """完整 run 证据链：agent-runs + events + evidence + report + anchor-check + launch。"""
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "agent-runs").mkdir()
    _write_events(run_root)
    _write_evidence(run_root, ANCHORS)
    (run_root / REPORT_FILE).write_text(_anchored_report(QUESTION), encoding="utf-8")
    check_run(run_root)
    record_launch_argv(run_root, ARGV)


def build_positive(tmp: Path) -> tuple[Path, Path]:
    """合法终验产物：完整 run 证据链 + wiki 域归位报告 + anchor-check + launch 记录。"""
    run_root = tmp / "positive"
    _write_full_run(run_root)

    wiki = tmp / "wiki-positive"
    report_dir = wiki / "DeepThought" / topic_slug(QUESTION)
    report_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(run_root / REPORT_FILE, report_dir / "2026-09-01-topic.md")
    shutil.copy2(run_root / ANCHOR_CHECK_FILE, report_dir / ANCHOR_CHECK_FILE)
    return run_root, wiki


def build_negative_launch(tmp: Path) -> Path:
    """① 判红：argv 非唯一入口（机械记录的 argv 不等于 canonical）。"""
    run_root = tmp / "negative-launch"
    run_root.mkdir(parents=True, exist_ok=True)
    record_launch_argv(run_root, ["fleet-graph", "research", "run", "--tier", "heavy"])
    return run_root


def build_negative_chain(tmp: Path) -> Path:
    """② 判红：缺 agent-runs 状态根（证据链不完整）。"""
    run_root = tmp / "negative-chain"
    _write_full_run(run_root)
    (run_root / "agent-runs").rmdir()
    return run_root


def build_negative_report(tmp: Path) -> tuple[Path, Path]:
    """③ 判红：wiki 域未归位报告（DeepThought 落点为空，只有 run_root 本地 report）。"""
    run_root = tmp / "negative-report"
    _write_full_run(run_root)
    wiki = tmp / "wiki-negative-report"
    wiki.mkdir(parents=True, exist_ok=True)
    return run_root, wiki


def build_negative_anchor(tmp: Path) -> Path:
    """④ 判红：多数结论行无 [anchor: …] 引用 → 核验率 ≤90%。"""
    run_root = tmp / "negative-anchor"
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "agent-runs").mkdir()
    _write_events(run_root)
    _write_evidence(run_root, ANCHORS)
    bare = "\n".join(
        [f"# {QUESTION}", ""]
        + [f"- 结论 {i} 无锚点" for i in range(1, 7)]
        + ["", "## 分歧裁定", "", "- 结论 7 也无锚点"]
    )
    (run_root / REPORT_FILE).write_text(bare, encoding="utf-8")
    check_run(run_root)
    record_launch_argv(run_root, ARGV)
    return run_root


def build_negative_coldread(tmp: Path) -> Path:
    """⑤ 判红：报告无小节、无锚点、无实质正文 → 冷读 FAIL。"""
    run_root = tmp / "negative-coldread"
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "agent-runs").mkdir()
    _write_events(run_root)
    _write_evidence(run_root, ANCHORS)
    (run_root / REPORT_FILE).write_text(f"# {QUESTION}\n", encoding="utf-8")
    check_run(run_root)
    record_launch_argv(run_root, ARGV)
    return run_root


def self_check() -> tuple[bool, dict[str, Any]]:
    """判据 ⑥：阳性判绿、五种阴性判红。任一失当 = 脚本自身无效。"""
    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        pos, wiki_pos = build_positive(tmp)
        ok_pos, verdict_pos = judge_coldstart(pos, QUESTION, wiki_root=wiki_pos)
        results["positive"] = "green" if ok_pos else "red"
        results["positive_details"] = verdict_pos

        neg_launch = build_negative_launch(tmp)
        ok_launch, _ = judge_launch_command(load_argv(neg_launch), QUESTION)
        results["negative_launch"] = "red" if not ok_launch else "green"

        neg_chain = build_negative_chain(tmp)
        ok_chain, _ = judge_evidence_chain(neg_chain)
        results["negative_chain"] = "red" if not ok_chain else "green"

        neg_report, wiki_neg_report = build_negative_report(tmp)
        ok_report, _ = judge_report_placed(neg_report, QUESTION, wiki_root=wiki_neg_report)
        results["negative_report"] = "red" if not ok_report else "green"

        neg_anchor = build_negative_anchor(tmp)
        ok_anchor, verdict_anchor = judge_anchor(neg_anchor)
        results["negative_anchor"] = "red" if not ok_anchor else "green"
        results["negative_anchor_rate"] = verdict_anchor["rate"]

        neg_cold = build_negative_coldread(tmp)
        ok_cold, _ = judge_cold_read(neg_cold)
        results["negative_coldread"] = "red" if not ok_cold else "green"

    expected_green = results["positive"] == "green"
    negatives_red = (
        results["negative_launch"] == "red"
        and results["negative_chain"] == "red"
        and results["negative_report"] == "red"
        and results["negative_anchor"] == "red"
        and results["negative_coldread"] == "red"
    )
    passed = expected_green and negatives_red
    results["self_check"] = "pass" if passed else "fail"
    return passed, results


def load_argv(run_root: Path) -> list[str] | None:
    from fleet_graph.research_coldstart import load_launch_argv

    return load_launch_argv(run_root)


def judge_existing(
    run_root: Path,
    *,
    wiki_root: Path | None,
    question: str,
    argv: list[str] | None,
) -> tuple[bool, dict[str, Any]]:
    """对既有 run 产物执行五件套判据。"""
    ok, verdict = judge_coldstart(run_root, question, wiki_root=wiki_root, argv=argv)
    verdict["pass"] = ok
    return ok, verdict


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-root",
        help="对既有 run 产物执行五件套判据（绿 exit 0、红 exit 非零）",
    )
    parser.add_argument("--wiki-root", default=None, help="wiki 域根（判据③归位核验）")
    parser.add_argument("--question", default=QUESTION, help="终验题目（默认自检题目）")
    parser.add_argument(
        "--argv",
        default=None,
        help="逗号分隔的 CLI argv（缺省读 run_root/launch.json，再缺省 canonical）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.run_root:
        run_root = Path(args.run_root)
        wiki = Path(args.wiki_root) if args.wiki_root else None
        cli_argv = args.argv.split(",") if args.argv else None
        ok, verdict = judge_existing(
            run_root,
            wiki_root=wiki,
            question=args.question,
            argv=cli_argv,
        )
        print(json.dumps(verdict, ensure_ascii=False, sort_keys=True, default=str))
        return 0 if ok else 1

    passed, results = self_check()
    print(json.dumps(results, ensure_ascii=False, sort_keys=True, default=str))
    if not passed:
        print("research-coldstart self_check: fail", file=sys.stderr)
        return 1
    print("research-coldstart self_check: pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
