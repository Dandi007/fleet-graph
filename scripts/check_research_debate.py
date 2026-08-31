"""R4 acceptance：对抗裁决子图（advocate → opponent → judge → arbiter）机器判据。

一次 run 的 agent-runs 必须覆盖三角色每条腿（R4 判据 ③），且报告必须含
`## 分歧裁定` 段（判据 ①）、judge 的真实 OPEN DISAGREEMENT 逐字保留进报告的开放
分歧列表（判据 ②）。检查用 fake seed 单线索一轮收敛、fake worker 产出一条 evidence，
四角色按 role 回放脚本（judge 产出 1 条 RULE 裁定 + 1 条 OPEN DISAGREEMENT），用
记录每次派发 ``spec.role`` 的 fake launcher 跑完一次 ``run_research``，然后打印可
解析输出：

    roles={advocate,judge,opponent,arbiter}
    open_disagreements=1
    report_has_adjudication=yes

- ``roles`` 是本次 run 实际派发到的 debate 角色集合（按字母序）；
- ``open_disagreements`` 是报告开放分歧列表里逐字保留的 OPEN DISAGREEMENT 条数
  （judge.md 的 OPEN DISAGREEMENT 行逐字出现在 report.md 才算 1 条）；
- ``report_has_adjudication`` 是 report.md 是否含 `## 分歧裁定` 段。

exit 0 当且仅当 ①（含 `## 分歧裁定`）、②（open_disagreements ≥ 1）、③
（advocate/opponent/judge 三角色各至少一次 agent-run）全部成立，否则 exit 1。
"""

from __future__ import annotations

import json
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
    OPEN_DISAGREEMENT_MARKER,
    OPPONENT_ROLE,
)
from fleet_graph.graphs.research_runner import ResearchConfig, run_research

DEBATE_ROLE_NAMES = {
    ADVOCATE_ROLE: "advocate",
    OPPONENT_ROLE: "opponent",
    JUDGE_ROLE: "judge",
    ARBITER_ROLE: "arbiter",
}


def _open_section_lines(lines: list[str]) -> list[str]:
    """报告「### 开放分歧」小节到下一段标题之间的行（零 LLM 行级切分）。

    只取本节内容，避免 judge body 全文复述里的同一条 OPEN DISAGREEMENT 被重复计数。
    """
    section: list[str] = []
    seen = False
    for line in lines:
        if line.strip() == "### 开放分歧":
            seen = True
            continue
        if seen and line.startswith("## "):
            break
        if seen:
            section.append(line)
    return section


class FakeTextNode:
    """seed 的替身：回放脚本化文本。"""

    def __init__(self, seed_text: str) -> None:
        self.seed_text = seed_text

    def complete(self, spec: Any, prompt: str) -> SimpleNamespace:
        return SimpleNamespace(
            text=self.seed_text, model="fake", finish_reason="stop", usage={}, raw={}
        )


def worker_payload() -> dict[str, Any]:
    """worker.result.v1 形状：一条 evidence，无子线索。"""
    return {
        "evidences": [
            {
                "quote": "q",
                "claim": "c",
                "source": "web",
                "locator": "fake.md:1",
                "revision": "r1",
            }
        ],
        "proposed_clues": [],
        "materials": [],
    }


def debater_result(body: str) -> dict[str, Any]:
    """dr-doc.result.v1 形状的成功信封。"""
    return {
        "state": "succeeded",
        "exit_code": 0,
        "structured_result": {"body": body},
    }


def arbiter_result(verdict: str = "enough", rationale: str = "证据已充分") -> dict[str, Any]:
    """dr-arbiter.result.v1 形状的成功信封。"""
    return {
        "state": "succeeded",
        "exit_code": 0,
        "structured_result": {"verdict": verdict, "rationale": rationale},
    }


class FakeLauncher:
    """按 role 回放脚本，并记录每次派发的 spec.role（判据 ③ 的来源）。

    worker 与四角色 run 严格顺序执行（collect wait 单个 worker、debate 链顺序 wait），
    所以 wait 与 launch 同序一一对应；用 wait 计数当下正等哪个 run。``launch`` 幂等
    （R3）：同 run_id 重复 launch = re-adopt 在途 run，只记录第一次。
    """

    def __init__(self, debate: dict[str, dict[str, Any]]) -> None:
        self.debate = debate
        self.dispatched_roles: list[str] = []
        self._waits = 0
        self._launched: set[str] = set()

    def launch(self, spec: Any, run_id: str) -> RunTicket:
        if run_id not in self._launched:
            self._launched.add(run_id)
            self.dispatched_roles.append(spec.role)
        return RunTicket(run_id, f"/tmp/debate/{run_id}", None)

    def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
        role = self.dispatched_roles[self._waits]
        self._waits += 1
        if role in self.debate:
            return RunStatus("succeeded", self.debate[role])
        return RunStatus(
            "succeeded",
            {"state": "succeeded", "exit_code": 0, "structured_result": worker_payload()},
        )


def main() -> int:
    """跑一次 run_research，打印可解析判据三行，按判据返回 exit code。"""
    seed_text = json.dumps(["单一 web 线索"])
    judge_body = "\n".join(
        [
            "# 分歧裁定",
            "RULE: 分歧一 裁决：web 证据成立 [anchor: web@fake.md:1]",
            "OPEN DISAGREEMENT: 分歧二 无法用既有证据裁决，双方各自成立",
        ]
    )
    launcher = FakeLauncher(
        {
            ADVOCATE_ROLE: debater_result("# 正面论证\n支持结论。"),
            OPPONENT_ROLE: debater_result("# 反驳\n证伪路径……"),
            JUDGE_ROLE: debater_result(judge_body),
            ARBITER_ROLE: arbiter_result("enough", "已有证据足以定稿"),
        }
    )
    with tempfile.TemporaryDirectory() as td:
        run_root = Path(td) / "run"
        config = ResearchConfig(question="对抗裁决判据检查", run_root=run_root)
        result = run_research(config, text_node=FakeTextNode(seed_text), launcher=launcher)
        if result.get("terminal") not in {"converged", "capped", "partial"}:
            print(f"run 未跑通：terminal={result.get('terminal')}", file=sys.stderr)
            return 1

        roles = sorted(
            {DEBATE_ROLE_NAMES[r] for r in launcher.dispatched_roles if r in DEBATE_ROLE_NAMES}
        )

        # 判据 ①：report.md 含 `## 分歧裁定` 段（字符串级）。
        report_path = run_root / "report.md"
        report = report_path.read_text(encoding="utf-8")
        has_adjudication = "## 分歧裁定" in report

        # 判据 ②：judge 的 OPEN DISAGREEMENT ≥1，且逐字出现在报告开放分歧列表。
        judge_path = run_root / "debate" / "judge.md"
        if not judge_path.is_file():
            print("judge.md 缺失", file=sys.stderr)
            return 1
        judge_lines = judge_path.read_text(encoding="utf-8").splitlines()
        open_in_judge = [ln for ln in judge_lines if OPEN_DISAGREEMENT_MARKER in ln]

        # 只统计「### 开放分歧」小节（逐字保留的列表），不算报告正文里 judge body 的复述。
        lines = report.splitlines()
        open_section = _open_section_lines(lines)
        open_in_report = [ln for ln in open_section if OPEN_DISAGREEMENT_MARKER in ln]
        verbatim = bool(open_in_judge) and set(open_in_judge) <= set(open_in_report)

        # 判据 ③：advocate/opponent/judge 三角色各至少一次 agent-run。
        three_legs = {
            DEBATE_ROLE_NAMES[r] for r in launcher.dispatched_roles if r in DEBATE_ROLE_NAMES
        }
        legs_covered = {"advocate", "opponent", "judge"} <= three_legs

        open_count = len(open_in_report) if verbatim else 0

    print(f"roles={{{','.join(roles)}}}")
    print(f"open_disagreements={open_count}")
    print(f"report_has_adjudication={'yes' if has_adjudication else 'no'}")
    ok = has_adjudication and verbatim and legs_covered
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
