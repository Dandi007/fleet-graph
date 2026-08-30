"""R2 acceptance：deep-research 多源 worker 矩阵的机器可判定覆盖检查。

一次 run 的 agent-runs 必须覆盖 ≥ 3 种 source，且其中 web ≥ 1 次（修复「dr-worker-web
派发数恒为 0」）。检查用 fake seed 产出含 source 标注、跨 ≥3 源（必须含 web）的 clue，
用记录每次派发 ``spec.role`` 的 fake launcher 跑完一次 ``run_research``，然后打印两行
可解析输出：

    sources={code-local,wiki,web}
    web=1

- 第一行是本次 run **实际派发到的去重 source 集合**（按字母序）；
- 第二行是 web 的派发次数。

exit 0 当且仅当 去重数 ≥ 3 且 web ≥ 1，否则 exit 1。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fleet_graph.executors.agent_run import RunStatus, RunTicket
from fleet_graph.graphs.research_pipeline import SOURCE_ROLE, SYNTHESIS_ROLE
from fleet_graph.graphs.research_runner import ResearchConfig, run_research

ROLE_TO_SOURCE: dict[str, str] = {role: source for source, role in SOURCE_ROLE.items()}


class FakeTextNode:
    """seed 的替身：回放脚本化文本。"""

    def __init__(self, seed_text: str) -> None:
        self.seed_text = seed_text

    def complete(self, spec: Any, prompt: str) -> SimpleNamespace:
        return SimpleNamespace(
            text=self.seed_text, model="fake", finish_reason="stop", usage={}, raw={}
        )


def worker_payload(source: str) -> dict[str, Any]:
    """worker.result.v1 形状：verdict=found + 一条 evidence + 无子线索。"""
    return {
        "clue_id": "c-fake",
        "verdict": "found",
        "evidences": [
            {
                "quote": "q",
                "claim": "c",
                "source": source,
                "locator": "fake.md:1",
                "revision": "r1",
            }
        ],
        "proposed_clues": [],
        "materials": [],
    }


def synthesis_payload() -> dict[str, Any]:
    """research-synth.result.v1 形状。"""
    return {
        "report_markdown": "# 报告\n覆盖检查通过。",
        "coverage_summary": "all sources covered",
        "unresolved": [],
    }


class FakeLauncher:
    """按 role 回放脚本，并记录每次派发的 spec.role（覆盖检查的判据）。"""

    def __init__(self, worker_results: list[dict[str, Any]]) -> None:
        self.worker_results = list(worker_results)
        self.roles: dict[str, str] = {}
        self.dispatched_roles: list[str] = []

    def launch(self, spec: Any, run_id: str) -> RunTicket:
        self.roles[run_id] = spec.role
        self.dispatched_roles.append(spec.role)
        return RunTicket(run_id, f"/tmp/coverage/{run_id}", None)

    def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
        role = self.roles[ticket.run_id]
        if role == SYNTHESIS_ROLE:
            return RunStatus(
                "succeeded",
                {"state": "succeeded", "exit_code": 0, "structured_result": synthesis_payload()},
            )
        result = self.worker_results.pop(0)
        return RunStatus(
            "succeeded", {"state": "succeeded", "exit_code": 0, "structured_result": result}
        )


def check() -> int:
    """跑一次 run_research，打印覆盖两行，按判据返回 exit code。"""
    # fake seed：跨 3 源且必须含 web 的 source 标注 clue（规范要求逐字用 6 源词汇）。
    seed_text = json.dumps(
        [
            {"text": "web 线索", "source": "web"},
            {"text": "wiki 线索", "source": "wiki"},
            {"text": "code-local 线索", "source": "code-local"},
        ]
    )
    launcher = FakeLauncher(
        [
            worker_payload("web"),
            worker_payload("wiki"),
            worker_payload("code-local"),
        ]
    )
    with tempfile.TemporaryDirectory() as td:
        config = ResearchConfig(question="coverage check", run_root=Path(td) / "run")
        result = run_research(config, text_node=FakeTextNode(seed_text), launcher=launcher)
    if result.get("terminal") not in {"converged", "capped", "partial"}:
        print(f"run 未跑通：terminal={result.get('terminal')}", file=sys.stderr)
        return 1

    # 只有 worker 派发才算 source 覆盖；synthesis 不是矩阵角色。
    dispatched_sources = sorted(
        {ROLE_TO_SOURCE[role] for role in launcher.dispatched_roles if role in ROLE_TO_SOURCE}
    )
    web_count = sum(1 for role in launcher.dispatched_roles if ROLE_TO_SOURCE.get(role) == "web")
    print(f"sources={{{','.join(dispatched_sources)}}}")
    print(f"web={web_count}")
    return 0 if len(dispatched_sources) >= 3 and web_count >= 1 else 1


def main() -> int:
    return check()


if __name__ == "__main__":
    sys.exit(main())
