"""R2-fix acceptance：deep-research 多源 worker 矩阵的机器可判定覆盖检查。

一次 run 的 agent-runs 必须覆盖 ≥ 3 种 source，且其中 web ≥ 1 次（修复「dr-worker-web
派发数恒为 0」）。检查用 fake seed 产出含 source 标注、跨 ≥3 源（必须含 web）的 clue，
用记录每次派发 ``spec.role`` 的 fake launcher 跑完一次 ``run_research``，然后打印可解析
输出：

    sources={code-local,wiki,web}
    web=1
    debate_evidences=1

- ``sources`` 是本次 run **实际派发到的去重 source 集合**（按字母序）；
- ``web`` 是 web 的派发次数；
- ``debate_evidences`` 是 R4 debate 阶段 advocate manifest 投递的 evidences 条数
  （无 verdict 的 evidences 必须判 done 并进入对抗裁决子图——R2-fix 验收判据的
  R4 承接形态，synthesis 已由 debate 替代）。

exit 0 当且仅当 去重数 ≥ 3 且 web ≥ 1 且 debate_evidences ≥ 1，否则 exit 1。

R2-fix：fake ``worker_payload`` 产出**真实** worker.result.v1 形状（evidences /
proposed_clues / materials，无 verdict、无 clue_id）——覆盖检查真走修复后的 collect
路径（合法信封 + evidences list 即判 done）。
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
    OPPONENT_ROLE,
    SOURCE_ROLE,
)
from fleet_graph.graphs.research_runner import ResearchConfig, run_research

ROLE_TO_SOURCE: dict[str, str] = {role: source for source, role in SOURCE_ROLE.items()}

DEBATE_ROLES: set[str] = {ADVOCATE_ROLE, OPPONENT_ROLE, JUDGE_ROLE, ARBITER_ROLE}


class FakeTextNode:
    """seed 的替身：回放脚本化文本。"""

    def __init__(self, seed_text: str) -> None:
        self.seed_text = seed_text

    def complete(self, spec: Any, prompt: str) -> SimpleNamespace:
        return SimpleNamespace(
            text=self.seed_text, model="fake", finish_reason="stop", usage={}, raw={}
        )


def worker_payload(source: str) -> dict[str, Any]:
    """worker.result.v1 形状：无 verdict / 无 clue_id，一条 evidence + 无子线索。"""
    return {
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


def debate_payload(body: str) -> dict[str, Any]:
    """dr-doc.result.v1 形状。"""
    return {
        "state": "succeeded",
        "exit_code": 0,
        "structured_result": {"body": body},
    }


def arbiter_payload() -> dict[str, Any]:
    """dr-arbiter.result.v1 形状。"""
    return {
        "state": "succeeded",
        "exit_code": 0,
        "structured_result": {"verdict": "enough", "rationale": "证据已充分"},
    }


class FakeLauncher:
    """按 role 回放脚本，并记录每次派发的 spec.role（覆盖检查的判据）。

    ``launch`` 幂等（R3）：同 run_id 重复 launch = re-adopt 在途 run，只记录第一次；
    wait 与 launch 同序（worker 先、debate 链后）。
    """

    def __init__(self, worker_results: list[dict[str, Any]]) -> None:
        self.worker_results = list(worker_results)
        self.roles: dict[str, str] = {}
        self.dispatched_roles: list[str] = []
        self._launched: set[str] = set()

    def launch(self, spec: Any, run_id: str) -> RunTicket:
        if run_id not in self._launched:
            self._launched.add(run_id)
            self.roles[run_id] = spec.role
            self.dispatched_roles.append(spec.role)
        return RunTicket(run_id, f"/tmp/coverage/{run_id}", None)

    def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
        role = self.roles[ticket.run_id]
        if role in DEBATE_ROLES:
            if role == ARBITER_ROLE:
                return RunStatus("succeeded", arbiter_payload())
            body = "# body\n支持。" if role == ADVOCATE_ROLE else "# body\n反驳。"
            return RunStatus("succeeded", debate_payload(body))
        result = self.worker_results.pop(0)
        return RunStatus(
            "succeeded", {"state": "succeeded", "exit_code": 0, "structured_result": result}
        )


def check() -> int:
    """跑一次 run_research，打印覆盖三行，按判据返回 exit code。"""
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
        run_root = Path(td) / "run"
        config = ResearchConfig(question="coverage check", run_root=run_root)
        result = run_research(config, text_node=FakeTextNode(seed_text), launcher=launcher)
        if result.get("terminal") not in {"converged", "capped", "partial"}:
            print(f"run 未跑通：terminal={result.get('terminal')}", file=sys.stderr)
            return 1
        # 只有 worker 派发才算 source 覆盖；synthesis 不是矩阵角色。
        dispatched_sources = sorted(
            {ROLE_TO_SOURCE[role] for role in launcher.dispatched_roles if role in ROLE_TO_SOURCE}
        )
        web_count = sum(
            1 for role in launcher.dispatched_roles if ROLE_TO_SOURCE.get(role) == "web"
        )
        # R2-fix 承接（R4）：无 verdict 的 evidences 必须判 done 并进入 debate 阶段
        # （advocate manifest 的 evidences 非空）。
        try:
            manifest = json.loads(
                (run_root / "inputs" / "debate-advocate.json").read_text(encoding="utf-8")
            )
            debate_evidences = len(manifest.get("evidences") or [])
        except (OSError, json.JSONDecodeError) as exc:
            print(f"debate-advocate input 不可读：{exc}", file=sys.stderr)
            return 1
        evidence_lines = 0
        evidence_path = run_root / "evidence.jsonl"
        if evidence_path.is_file():
            evidence_lines = sum(
                1 for line in evidence_path.read_text(encoding="utf-8").splitlines() if line
            )

    print(f"sources={{{','.join(dispatched_sources)}}}")
    print(f"web={web_count}")
    print(f"debate_evidences={debate_evidences}")
    ok = (
        len(dispatched_sources) >= 3
        and web_count >= 1
        and debate_evidences >= 1
        and evidence_lines >= 1
    )
    return 0 if ok else 1


def main() -> int:
    return check()


if __name__ == "__main__":
    sys.exit(main())
