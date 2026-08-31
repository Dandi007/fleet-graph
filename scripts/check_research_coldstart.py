#!/usr/bin/env python3
"""R8 acceptance：冷启动终验（DoD）五件套判据脚本（判绿 / 判红 + 自检）。

判据（approved.md「判据」节，五件套，机器可判）：
- ① 发起命令原文在案：真实 run 经唯一入口 ``research run --question``，由
  ``run_research_ticket`` 机械记录 CLI argv（落 run_root/launch.json），且题目为
  全新（不在历史题目集，防假阴）；
- ② run 证据链完整：events.jsonl 含 dispatch/collect、agent-runs 状态根、
  evidence.jsonl 存在、可回放、coverage>0；
- ③ 报告存在且非空：``DeepThought/<topic>/report.md`` 落位，字节数机械核 >0；
- ④ anchor 核验率 > 90%：``anchor-check.json`` 的 ``summary.rate > 0.90`` 且
  ``sums_ok==true``；
- ⑤ 冷读 subagent verdict == PASS：纯脚本冷读报告（无上下文第三方可读可复用）。

自检（判据 ⑥）：先经统一入口 ``run_research_ticket``（fake text node / fake
launcher，hermetic，不碰真实 LLM/agent-run/bus）跑一次**真实冷启动 run**，产物
（events/agent-runs/evidence/report/anchor-check/launch.json + wiki 归位）全部由
pipeline 落盘——判据脚本不再手工伪造 report/evidence/launch.json。对这次真实 run
判五件套绿；再由真实 run 派生五种阴性（坏 argv / 缺 launch / 断证据链 / 缺归位 /
低核验率 / 冷读残缺）判红。任一失当 = 脚本自身无效。

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
from types import SimpleNamespace
from typing import Any

from fleet_graph.executors.agent_run import RunStatus, RunTicket
from fleet_graph.graphs.research_pipeline import (
    ADVOCATE_ROLE,
    ARBITER_ROLE,
    JUDGE_ROLE,
    OPPONENT_ROLE,
    REPORT_FILE,
)
from fleet_graph.research_anchor import check_run
from fleet_graph.research_coldstart import (
    judge_anchor,
    judge_cold_read,
    judge_coldstart,
    judge_evidence_chain,
    judge_launch_command,
    judge_report_placed,
    load_launch_argv,
    record_launch_argv,
)
from fleet_graph.research_entry import run_research_ticket

QUESTION = "R8 冷启动终验：全新题目一条命令无人搀扶，端到端出带锚点可冷读报告"

#: 锚点词汇：worker 产出的 evidence 与 judge 的 RULE 行共用（结论行逐条命中 →
#: rate 接近 1.0）。report 里 judge 正文 + ruled 各重复一份 RULE 行（2N 条锚定
#: claim），另有 3 条未锚定结构行（「本轮无未决分歧」/ verdict / rationale）。
#: N=15 时 rate = 30/33 ≈ 0.909 > 0.90，留足软闸门余量。
ANCHOR_PARTS: list[tuple[str, str]] = [("wiki", f"fake.md:{i}") for i in range(1, 16)]
ANCHORS = [f"{source}@{locator}" for source, locator in ANCHOR_PARTS]


class FakeTextNode:
    """seed 的确定性回放：返回一个纯字符串线索数组（不碰真实 LLM）。"""

    def complete(self, spec: Any, prompt: str) -> SimpleNamespace:
        return SimpleNamespace(
            text=json.dumps(["R8 冷启动真实线索"]),
            model="fake",
            finish_reason="stop",
            usage={},
            raw={},
        )


class FakeLauncher:
    """worker / debate 四角色的确定性回放（不碰真实 agent-run / bus）。

    launch 与真实 launcher 同构：在 state_root（= run_root/agent-runs）下建会话根；
    wait 按 role 回放合法信封（worker.result.v1 / dr-doc.result.v1 /
    dr-arbiter.result.v1），judge 的 RULE 行与 worker evidence 共用 ANCHORS。
    """

    def __init__(self, state_root: Path) -> None:
        self.state_root = Path(state_root)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self._roles: dict[str, str] = {}
        self._launched: set[str] = set()

    def launch(self, spec: Any, run_id: str) -> Any:
        if run_id not in self._launched:
            self._launched.add(run_id)
            self._roles[run_id] = spec.role
        session = self.state_root / run_id
        session.mkdir(parents=True, exist_ok=True)
        return RunTicket(run_id, str(session), None)

    def wait(self, ticket: Any, **kwargs: Any) -> Any:
        role = self._roles[ticket.run_id]
        if role == ARBITER_ROLE:
            payload: dict[str, Any] = {"verdict": "enough", "rationale": "证据已充分，锚点核验通过"}
        elif role in {ADVOCATE_ROLE, OPPONENT_ROLE}:
            payload = {"body": "# body\n支持。"}
        elif role == JUDGE_ROLE:
            payload = {
                "body": "\n".join(
                    f"RULE: 分歧 {i} 裁决：wiki 证据成立 [anchor: {anchor}]"
                    for i, anchor in enumerate(ANCHORS, 1)
                )
            }
        else:
            payload = {
                "evidences": [
                    {
                        "quote": f"引文 {i}",
                        "claim": f"事实 {i}",
                        "source": source,
                        "locator": locator,
                    }
                    for i, (source, locator) in enumerate(ANCHOR_PARTS, 1)
                ],
                "proposed_clues": [],
                "materials": [],
            }
        return RunStatus(
            "succeeded",
            {"state": "succeeded", "exit_code": 0, "structured_result": payload},
        )


def run_real_coldstart(tmp: Path, name: str) -> tuple[Path, Path]:
    """真实冷启动 run：经统一入口跑 pipeline（fake text/launcher），产物全落盘。

    返回 (run_root, wiki_root)。判据脚本只发这一条 ticket，不向 run 注入任何
    题目相关提示/预设线索（seed 线索由 fake text node 的 pipeline 内部产出）。
    """
    run_root = tmp / name
    wiki = tmp / f"{name}-wiki"
    launcher = FakeLauncher(state_root=run_root / "agent-runs")
    result = run_research_ticket(
        QUESTION,
        run_root=run_root,
        wiki_root=wiki,
        text_node=FakeTextNode(),
        launcher=launcher,
    )
    if result.get("terminal") not in {"converged", "capped", "partial"}:
        raise AssertionError(
            f"真实冷启动 run 终态非法: {result.get('terminal')}: {result.get('terminal_reason')}"
        )
    return run_root, wiki


def build_positive(tmp: Path) -> tuple[Path, Path]:
    """合法终验产物：真实冷启动 run（五件套全绿判据的目标）。"""
    return run_real_coldstart(tmp, "positive")


def _copy_run(tmp: Path, src: Path, name: str) -> Path:
    dst = tmp / name
    shutil.copytree(src, dst)
    return dst


def build_negative_launch(tmp: Path, src: Path) -> Path:
    """① 判红：argv 非唯一入口（机械记录的 argv 不等于 canonical）。"""
    run_root = _copy_run(tmp, src, "negative-launch")
    record_launch_argv(run_root, ["fleet-graph", "research", "run", "--tier", "heavy"])
    return run_root


def build_negative_missing_launch(tmp: Path, src: Path) -> Path:
    """① 判红（判据 ⑥ 硬化）：缺 launch.json 不得自动通过（防空转放行）。"""
    run_root = _copy_run(tmp, src, "negative-missing-launch")
    (run_root / "launch.json").unlink()
    return run_root


def build_negative_chain(tmp: Path, src: Path) -> Path:
    """② 判红：缺 agent-runs 状态根（证据链不完整）。"""
    run_root = _copy_run(tmp, src, "negative-chain")
    shutil.rmtree(run_root / "agent-runs")
    return run_root


def build_negative_report(run_root: Path, empty_wiki: Path) -> Path:
    """③ 判红：wiki 域未归位（DeepThought 落点为空，只有 run_root 本地 report）。"""
    empty_wiki.mkdir(parents=True, exist_ok=True)
    return empty_wiki


def build_negative_anchor(tmp: Path, src: Path) -> Path:
    """④ 判红：多数结论行无 [anchor: …] 引用 → 核验率 ≤90%。"""
    run_root = _copy_run(tmp, src, "negative-anchor")
    bare = "\n".join(
        [f"# {QUESTION}", ""]
        + [f"- 结论 {i} 无锚点" for i in range(1, 7)]
        + ["", "## 分歧裁定", "", "- 结论 7 也无锚点"]
    )
    (run_root / REPORT_FILE).write_text(bare, encoding="utf-8")
    check_run(run_root)
    return run_root


def build_negative_coldread(tmp: Path, src: Path) -> Path:
    """⑤ 判红：报告无小节、无锚点、无实质正文 → 冷读 FAIL。"""
    run_root = _copy_run(tmp, src, "negative-coldread")
    (run_root / REPORT_FILE).write_text(f"# {QUESTION}\n", encoding="utf-8")
    check_run(run_root)
    return run_root


def self_check() -> tuple[bool, dict[str, Any]]:
    """判据 ⑥：真实冷启动 run 判绿、五种阴性判红。任一失当 = 脚本自身无效。"""
    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        pos, wiki_pos = build_positive(tmp)
        ok_pos, verdict_pos = judge_coldstart(pos, QUESTION, wiki_root=wiki_pos)
        results["positive"] = "green" if ok_pos else "red"
        results["positive_details"] = verdict_pos

        neg_launch = build_negative_launch(tmp, pos)
        ok_launch, _ = judge_launch_command(load_argv(neg_launch), QUESTION)
        results["negative_launch"] = "red" if not ok_launch else "green"

        neg_missing = build_negative_missing_launch(tmp, pos)
        ok_missing, verdict_missing = judge_coldstart(neg_missing, QUESTION, wiki_root=wiki_pos)
        results["negative_missing_launch"] = "red" if not ok_missing else "green"
        results["missing_launch_exact"] = verdict_missing["launch_command"]["exact"]

        neg_chain = build_negative_chain(tmp, pos)
        ok_chain, _ = judge_evidence_chain(neg_chain)
        results["negative_chain"] = "red" if not ok_chain else "green"

        wiki_neg_report = build_negative_report(pos, tmp / "wiki-negative-report")
        ok_report, _ = judge_report_placed(pos, QUESTION, wiki_root=wiki_neg_report)
        results["negative_report"] = "red" if not ok_report else "green"

        neg_anchor = build_negative_anchor(tmp, pos)
        ok_anchor, verdict_anchor = judge_anchor(neg_anchor)
        results["negative_anchor"] = "red" if not ok_anchor else "green"
        results["negative_anchor_rate"] = verdict_anchor["rate"]

        neg_cold = build_negative_coldread(tmp, pos)
        ok_cold, _ = judge_cold_read(neg_cold)
        results["negative_coldread"] = "red" if not ok_cold else "green"

    expected_green = results["positive"] == "green"
    negatives_red = (
        results["negative_launch"] == "red"
        and results["negative_missing_launch"] == "red"
        and results["negative_chain"] == "red"
        and results["negative_report"] == "red"
        and results["negative_anchor"] == "red"
        and results["negative_coldread"] == "red"
    )
    passed = expected_green and negatives_red
    results["self_check"] = "pass" if passed else "fail"
    return passed, results


def load_argv(run_root: Path) -> list[str] | None:
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
        help="逗号分隔的 CLI argv（缺省读 run_root/launch.json，缺失即判红）",
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
