#!/usr/bin/env python3
"""R5 acceptance：anchor-check 判据脚本（判绿 / 判红 + 自检）。

判据（approved.md「判据」节，机器可判）：
- ① 终验 run 的 ``anchor-check.json`` 存在，且 ``summary.rate > 0.90``；
- ② 同一份 ``anchor-check.json`` 的 ``summary.sums_ok == true``
  （由 ok/failed/unanchored/total 重算，防伪造——伪造 sums 判红）；
- ③ ``report.md`` 报告头含 ``dr-anchor-rate`` 字段；
- ④ 本脚本自检：在**阴性 fixture**（无 anchor / 核验率 ≤90% / sums 不平）上判红，
  在**阳性**（合法终验产物）上判绿——脚本自身无效即判据失败。

用法：
- 无参数：自检（判据 ④），打印可解析输出，全绿 exit 0、任一失当 exit 非零
  （acceptance 入口 ``uv run python scripts/check_research_anchor.py``）。
- ``--run-root <dir>``：对既有 run 产物执行判据 ①②③，绿 exit 0、红 exit 非零。

输出（自检）：``positive=green|red`` / ``negative_no_anchor=red|green`` /
``negative_rate=red|green`` / ``negative_sums=red|green`` / ``self_check=pass|fail``。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from fleet_graph.research_anchor import (
    ANCHOR_CHECK_FILE,
    VERDICT_OK,
    check_run,
    judge_run,
    with_rate_header,
)

QUESTION = "R5 锚点核验自检"

# 阳性 fixture 的 evidence：两条 finding，anchor 逐条派生自 source@locator。
POSITIVE_EVIDENCE: list[dict[str, str]] = [
    {
        "at": "2026-09-01T00:00:00Z",
        "clue_id": "c-pos1",
        "depth": 0,
        "finding": {"claim": "事实一", "source": "wiki", "quote": "引文一", "locator": "fake.md:1"},
    },
    {
        "at": "2026-09-01T00:00:00Z",
        "clue_id": "c-pos2",
        "depth": 0,
        "finding": {"claim": "事实二", "source": "web", "quote": "引文二", "locator": "fake.md:2"},
    },
]

POSITIVE_REPORT = "\n".join(
    [
        f"# {QUESTION}",
        "",
        "- 结论一 [anchor: wiki@fake.md:1]",
        "- 结论二 [anchor: web@fake.md:2]",
        "- 结论三 [anchor: wiki@fake.md:1]",
        "",
        "## 分歧裁定",
        "",
        "- RULE: 结论一 裁决成立 [anchor: wiki@fake.md:1]",
    ]
)

# 阴性「无 anchor」：报告有 conclusion 行但没有任何 [anchor: …] 引用 → 全 unanchored。
NO_ANCHOR_REPORT = "\n".join(
    [f"# {QUESTION}", "", "- 结论一", "- 结论二", "- 结论三", "", "## 分歧裁定", "", "- 结论四"]
)

# 阴性「核验率 ≤90%」：3 条引用只命中 1 条 → rate = 1/3 ≈ 0.333 ≤ 0.90。
RATE_REPORT = "\n".join(
    [
        f"# {QUESTION}",
        "",
        "- 结论一 [anchor: wiki@fake.md:1]",
        "- 结论二 [anchor: web@missing.md:9]",
        "- 结论三 [anchor: web@missing.md:9]",
    ]
)

RATE_EVIDENCE: list[dict[str, str]] = [
    {
        "at": "2026-09-01T00:00:00Z",
        "clue_id": "c-rate",
        "depth": 0,
        "finding": {"claim": "事实一", "source": "wiki", "quote": "引文一", "locator": "fake.md:1"},
    },
]


def write_evidence(run_root: Path, evidence: list[dict[str, Any]]) -> None:
    """把 evidence 行（含 finding 形状）写入 run_root/evidence.jsonl。"""
    path = run_root / "evidence.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for entry in evidence:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _claim(anchor: str, quote: str, claim: str) -> dict[str, str]:
    """伪造 anchor-check.json 用的 claim 项（verdict=ok）。"""
    return {"anchor": anchor, "quote": quote, "claim": claim, "verdict": VERDICT_OK}


def build_positive(run_root: Path) -> None:
    """合法终验产物：report.md（引用全命中）+ evidence.jsonl → check_run 产出。"""
    (run_root / "report.md").write_text(POSITIVE_REPORT, encoding="utf-8")
    write_evidence(run_root, POSITIVE_EVIDENCE)
    check_run(run_root)


def build_no_anchor(run_root: Path) -> None:
    """阴性「无 anchor」：有结论无引用，check_run 后全 unanchored、rate=0。"""
    (run_root / "report.md").write_text(NO_ANCHOR_REPORT, encoding="utf-8")
    write_evidence(run_root, POSITIVE_EVIDENCE)
    check_run(run_root)


def build_rate(run_root: Path) -> None:
    """阴性「核验率 ≤90%」：多数引用 miss → rate ≤ 0.90。"""
    (run_root / "report.md").write_text(RATE_REPORT, encoding="utf-8")
    write_evidence(run_root, RATE_EVIDENCE)
    check_run(run_root)


def build_sums_unbalanced(run_root: Path) -> None:
    """阴性「sums 不平」：伪造 anchor-check.json，ok+failed+unanchored != total。

    rate 设成 >0.90（1.0）也判红——软闸门判据 ② 按计数重算 sums_ok，伪造文件
    的 sums 数字对不上 → 判红（脚本自身无效即判据失败）。
    """
    report = POSITIVE_REPORT
    (run_root / "report.md").write_text(with_rate_header(report, 1.0), encoding="utf-8")
    fake: dict[str, Any] = {
        "claims": [
            _claim("wiki@fake.md:1", "引文一", "结论一"),
            _claim("web@fake.md:2", "引文二", "结论二"),
            _claim("wiki@fake.md:1", "引文一", "结论三"),
        ],
        "summary": {
            "total": 3,
            "ok": 3,
            "failed": 1,
            "unanchored": 0,
            "rate": 1.0,
            "sums_ok": False,
        },
    }
    (run_root / ANCHOR_CHECK_FILE).write_text(
        json.dumps(fake, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def self_check() -> tuple[bool, dict[str, Any]]:
    """判据 ④：阳性判绿、三种阴性判红。任一失当 = 脚本自身无效。"""
    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        positive = tmp / "positive"
        positive.mkdir()
        build_positive(positive)
        ok_pos, _ = judge_run(positive)
        results["positive"] = "green" if ok_pos else "red"

        no_anchor = tmp / "no-anchor"
        no_anchor.mkdir()
        build_no_anchor(no_anchor)
        ok_na, verdict_na = judge_run(no_anchor)
        results["negative_no_anchor"] = "red" if not ok_na else "green"
        results["no_anchor_rate"] = verdict_na["rate"]

        rate = tmp / "rate"
        rate.mkdir()
        build_rate(rate)
        ok_rate, verdict_rate = judge_run(rate)
        results["negative_rate"] = "red" if not ok_rate else "green"
        results["rate_value"] = verdict_rate["rate"]

        sums = tmp / "sums"
        sums.mkdir()
        build_sums_unbalanced(sums)
        ok_sums, verdict_sums = judge_run(sums)
        results["negative_sums"] = "red" if not ok_sums else "green"
        results["sums_ok"] = verdict_sums["sums_ok"]

    expected_green = results["positive"] == "green"
    negatives_red = (
        results["negative_no_anchor"] == "red"
        and results["negative_rate"] == "red"
        and results["negative_sums"] == "red"
    )
    passed = expected_green and negatives_red
    results["self_check"] = "pass" if passed else "fail"
    return passed, results


def judge_existing(run_root: Path) -> tuple[bool, dict[str, Any]]:
    """对既有 run 产物执行判据 ①②③。"""
    ok, verdict = judge_run(run_root)
    verdict["pass"] = ok
    return ok, verdict


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-root",
        help="对既有 run 产物执行判据 ①②③（绿 exit 0、红 exit 非零）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.run_root:
        ok, verdict = judge_existing(Path(args.run_root))
        print(json.dumps(verdict, ensure_ascii=False, sort_keys=True))
        return 0 if ok else 1

    passed, results = self_check()
    for key, value in results.items():
        print(f"{key}={value}")
    if not passed:
        print("anchor-check self_check: fail", file=sys.stderr)
        return 1
    print("anchor-check self_check: pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
