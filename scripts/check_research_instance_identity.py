"""R3-fix acceptance：research run 身份按 run 实例隔离的机器可判定检查。

根因（真机逐字取证）：research run 身份是内容寻址的——同一题两次独立跑（同
research_id、同 generation）派生**相同** thread_id/run_id，第二遍的 synthesis
agent-run 发布 `agent.run.exited.v2` 撞 bus 409 `IDEMPOTENCY_CONFLICT` → exit 91 →
terminal=fault。修复（规格第 1 条）：thread 身份注入**稳定非随机**的 run 实例分量
（run_root 内容寻址后缀），不同 run_root 隔离、同 run_root kill-restart 不漂移。

检查分两步，全部用确定性 fake launcher 驱动**真实图**（不碰真实 agent-run/bus）：

1. 派生隔离：同一题不同 run_root -> 不同 thread_id / worker run_id / synthesis
   run_id；同一题同 run_root -> 恒同（幂等不变）。
2. 端到端隔离：同一题在两个不同 run_root 各跑一遍完整 run_research（fake 可控
   worker），两遍都到合法终态（converged/capped/partial，无 fault）——等价性判据①
   的机器可判版本。

打印可解析输出：

    research_id=r-<12hex>
    distinct_threads=True
    distinct_run_ids=True
    idempotent_run_ids=True
    both_legal_terminal=True

exit 0 当且仅当全部成立，否则 exit 1。
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
    DEFAULT_SOURCE,
    SYNTHESIS_ROLE,
    derive_clue_id,
    derive_research_id,
    derive_run_instance,
    synthesis_run_id,
    worker_run_id,
)
from fleet_graph.graphs.research_runner import ResearchConfig, run_research

QUESTION = "同一题的两次独立跑在哪个 run 身份上撞车?"
CLUE_TEXT = "scheduler 的基本循环"
# 同一题两次跑必须用不同的 run_root 才能算独立 run（规格判据①）。
RUN_ROOT_A = "run-a"
RUN_ROOT_B = "run-b"


def worker_payload(claim: str) -> dict[str, Any]:
    """worker.result.v1 形状：一条 evidence，无子线索（确定性、可控）。"""
    return {
        "evidences": [
            {
                "quote": claim,
                "claim": claim,
                "source": "wiki",
                "locator": "fake.md:1",
                "revision": "r1",
            }
        ],
        "proposed_clues": [],
        "materials": [],
    }


def synthesis_payload() -> dict[str, Any]:
    return {
        "report_markdown": "# 报告\n实例隔离检查通过。",
        "coverage_summary": "ok",
        "unresolved": [],
    }


class FakeTextNode:
    """seed 替身：回放脚本化文本。"""

    def __init__(self, seed_text: str) -> None:
        self.seed_text = seed_text

    def complete(self, spec: Any, prompt: str) -> SimpleNamespace:
        return SimpleNamespace(
            text=self.seed_text, model="fake", finish_reason="stop", usage={}, raw={}
        )


class FakeLauncher:
    """确定性 launcher：每次 wait 回放固定 worker.result.v1 / synthesis.result.v1。"""

    def __init__(self) -> None:
        self._roles: dict[str, str] = {}
        self._launched: set[str] = set()
        self.dispatched: list[str] = []

    def launch(self, spec: Any, run_id: str) -> RunTicket:
        if run_id in self._launched:
            return RunTicket(run_id, f"/tmp/instance/{run_id}", None, adopted=True)
        self._launched.add(run_id)
        self._roles[run_id] = spec.role
        self.dispatched.append(run_id)
        return RunTicket(run_id, f"/tmp/instance/{run_id}", None)

    def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
        role = self._roles[ticket.run_id]
        if role == SYNTHESIS_ROLE:
            return RunStatus(
                "succeeded",
                {"state": "succeeded", "exit_code": 0, "structured_result": synthesis_payload()},
            )
        return RunStatus(
            "succeeded",
            {
                "state": "succeeded",
                "exit_code": 0,
                "structured_result": worker_payload(f"事实:{ticket.run_id[:8]}"),
            },
        )


def check_derivation(tmp: Path) -> tuple[bool, bool, bool, bool, str]:
    """派生隔离检查：不同 run_root 不同、同 run_root 恒同。返回各自是否成立。"""
    a = ResearchConfig(question=QUESTION, run_root=tmp / RUN_ROOT_A)
    b = ResearchConfig(question=QUESTION, run_root=tmp / RUN_ROOT_B)
    same = ResearchConfig(question=QUESTION, run_root=tmp / RUN_ROOT_A)

    # research_id 仍内容寻址：同一题恒同（thread 身份的稳定基座）。
    research_id = derive_research_id(QUESTION)
    if not (a.research_id == b.research_id == research_id):
        print(
            f"research_id 应内容寻址恒同：{a.research_id} vs {b.research_id}",
            file=sys.stderr,
        )
        return False, False, False, False, research_id

    clue = derive_clue_id(CLUE_TEXT, DEFAULT_SOURCE)

    # 不同 run_root -> 不同 thread_id / run_id（隔离，不再撞 409）。
    distinct_threads = a.thread_id != b.thread_id
    distinct_run_ids = worker_run_id(a.thread_id, clue, 0) != worker_run_id(
        b.thread_id, clue, 0
    ) and synthesis_run_id(a.thread_id) != synthesis_run_id(b.thread_id)
    # 同 run_root -> 恒同（kill-restart 幂等不变）。
    idempotent_run_ids = (
        a.thread_id == same.thread_id
        and worker_run_id(a.thread_id, clue, 0) == worker_run_id(same.thread_id, clue, 0)
        and synthesis_run_id(a.thread_id) == synthesis_run_id(same.thread_id)
    )
    # run 实例分量必须稳定非随机（规格硬线：不掺 uuid4/时间戳）。
    inst = derive_run_instance(tmp / RUN_ROOT_A)
    stable = inst == derive_run_instance(tmp / RUN_ROOT_A) and inst.startswith("i-")
    ok = distinct_threads and distinct_run_ids and idempotent_run_ids and stable
    return ok, distinct_threads, distinct_run_ids, idempotent_run_ids, research_id


def run_one(tmp: Path, name: str) -> dict[str, Any]:
    """在两个不同 run_root 之一跑一遍完整 run_research（fake 可控 worker）。"""
    seed = FakeTextNode(json.dumps([CLUE_TEXT]))
    config = ResearchConfig(question=QUESTION, run_root=tmp / name, concurrency=1)
    return run_research(config, text_node=seed, launcher=FakeLauncher())


def check_default_path_protection() -> bool:
    """默认路径误双开保护（监督面补充断言，2026-08-31 过闸条件 1）。

    保护的唯一凭据是 cli 的默认 run_root 派生：不传 ``--run-root`` 时同一题
    两次启动必须落同一 run_root ⇒ 同一 instance/thread 身份（领养而非并跑）。
    只断言「显式异 root 异 id」看不住这条——必须断言默认路径本身。
    """
    from fleet_graph.cli import build_parser, default_research_run_root

    args = build_parser().parse_args(["research", "run", "--question", QUESTION])
    cli_default_is_none = args.run_root is None

    root_1 = default_research_run_root(QUESTION)
    root_2 = default_research_run_root(QUESTION)
    a = ResearchConfig(question=QUESTION, run_root=Path(root_1))
    b = ResearchConfig(question=QUESTION, run_root=Path(root_2))
    same_identity = root_1 == root_2 and a.thread_id == b.thread_id

    ok = cli_default_is_none and same_identity
    if not ok:
        print(
            "默认路径双开保护失效："
            f"cli_default_is_none={cli_default_is_none} roots=({root_1},{root_2}) "
            f"threads=({a.thread_id},{b.thread_id})",
            file=sys.stderr,
        )
    return ok


def check() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        (
            derivation_ok,
            distinct_threads,
            distinct_run_ids,
            idempotent_run_ids,
            research_id,
        ) = check_derivation(tmp)

        # 端到端：两遍独立跑都到合法终态（无 fault = 无 409 型故障）。
        result_a = run_one(tmp, RUN_ROOT_A)
        result_b = run_one(tmp, RUN_ROOT_B)
        legal = {"converged", "capped", "partial"}
        both_legal_terminal = (
            result_a.get("terminal") in legal and result_b.get("terminal") in legal
        )
        if not both_legal_terminal:
            print(
                f"两遍独立跑未全部到合法终态：A={result_a.get('terminal')} "
                f"B={result_b.get('terminal')}",
                file=sys.stderr,
            )

    default_path_protected = check_default_path_protection()

    print(f"research_id={research_id}")
    print(f"distinct_threads={distinct_threads}")
    print(f"distinct_run_ids={distinct_run_ids}")
    print(f"idempotent_run_ids={idempotent_run_ids}")
    print(f"both_legal_terminal={both_legal_terminal}")
    print(f"default_path_protected={default_path_protected}")
    return 0 if derivation_ok and both_legal_terminal and default_path_protected else 1


def main() -> int:
    return check()


if __name__ == "__main__":
    sys.exit(main())
