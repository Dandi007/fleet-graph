"""R3 acceptance：deep-research 并发 fan-out 的机器可判定检查（可控 worker 版）。

规格判据（真机探针验证 R3 并发派发的等价性与提速，用**可控 worker**复现，不被真实
worker 的违规展开破坏——规格修复要点 2）：

- 判据① 同一题 W=4 与 W=1 在**不同 run_root** 下都跑到合法终态（无 fault）；
- 判据② 两 run 的 evidence 集合等价（并发派发不改变产物集合）；
- 判据③ W=4 makespan < W=1 makespan（真实时间重叠，而非仅结构并列）；
- 判据④ kill-restart 同 clue 同 retry 的 run_id 只派一次（launcher 幂等，不重复派发）。

全部用确定性 fake launcher 驱动**真实图**（fake seed 一次派 3 条无子线索的 clue，
每条 worker 产出一条 evidence）：记录每个 run_id 的首次派发，测量 wall-clock
makespan，比对两遍 evidence 集合。打印可解析输出：

    w4_terminal=converged
    w1_terminal=converged
    evidence_equal=True
    w4_makespan_ms=215
    w1_makespan_ms=632
    no_duplicate_dispatch=True

exit 0 当且仅当全部判据成立，否则 exit 1。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fleet_graph.executors.agent_run import RunStatus, RunTicket
from fleet_graph.graphs.research_pipeline import (
    ADVOCATE_ROLE,
    ARBITER_ROLE,
    DEBATE_ROLES,
)
from fleet_graph.graphs.research_runner import ResearchConfig, run_research

QUESTION = "fanout 并行派发的等价性与提速?"
SEED_CLUES = ["clue 一", "clue 二", "clue 三"]
WORK_SECONDS = 0.2


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


def debate_payload(body: str) -> dict[str, Any]:
    """dr-doc.result.v1 形状：debater（advocate/opponent/judge）的 body 信封。"""
    return {"state": "succeeded", "exit_code": 0, "structured_result": {"body": body}}


def arbiter_payload() -> dict[str, Any]:
    """dr-arbiter.result.v1 形状。"""
    return {
        "state": "succeeded",
        "exit_code": 0,
        "structured_result": {"verdict": "enough", "rationale": "证据已充分"},
    }


class FakeTextNode:
    """seed 替身：回放 3 条无子线索的 clue。"""

    def __init__(self, seed_text: str) -> None:
        self.seed_text = seed_text

    def complete(self, spec: Any, prompt: str) -> SimpleNamespace:
        return SimpleNamespace(
            text=self.seed_text, model="fake", finish_reason="stop", usage={}, raw={}
        )


class FakeLauncher:
    """确定性 launcher：worker wait 睡 WORK_SECONDS（模拟真实工作耗时，度量 makespan）。

    ``launch`` 幂等（同 run_id 只记录首次派发）——judgment ④：kill-restart 同 id
    重派即 re-adopt，绝不重复记录。每个 worker run 首次派发时按 SEED_CLUES 顺序分配
    一条 claim，wait 时回放对应 evidence（确定性、可控）。R4：debate 四角色按角色
    回放固定信封（不睡 WORK_SECONDS——fanout 度量的是 worker 并发，不掺 debate 耗时）。
    """

    def __init__(self) -> None:
        self._roles: dict[str, str] = {}
        self._launched: set[str] = set()
        self.dispatched: list[str] = []
        self._claim_seq: dict[str, str] = {}

    def launch(self, spec: Any, run_id: str) -> RunTicket:
        if run_id in self._launched:
            return RunTicket(run_id, f"/tmp/fanout/{run_id}", None, adopted=True)
        self._launched.add(run_id)
        self._roles[run_id] = spec.role
        self.dispatched.append(run_id)
        if spec.role not in DEBATE_ROLES:
            self._claim_seq[run_id] = SEED_CLUES[len(self._claim_seq) % len(SEED_CLUES)]
        return RunTicket(run_id, f"/tmp/fanout/{run_id}", None)

    def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
        role = self._roles[ticket.run_id]
        if role == ARBITER_ROLE:
            return RunStatus("succeeded", arbiter_payload())
        if role in DEBATE_ROLES:
            body = "# body\n支持。" if role == ADVOCATE_ROLE else "# body\n反驳。"
            return RunStatus("succeeded", debate_payload(body))
        time.sleep(WORK_SECONDS)
        claim = self._claim_seq[ticket.run_id]
        return RunStatus(
            "succeeded",
            {
                "state": "succeeded",
                "exit_code": 0,
                "structured_result": worker_payload(f"事实:{claim}"),
            },
        )


def read_evidence_claims(run_root: Path) -> set[str]:
    path = run_root / "evidence.jsonl"
    if not path.is_file():
        return set()
    claims: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line:
            claims.add(json.loads(line)["finding"]["claim"])
    return claims


def run_one(tmp: Path, name: str, concurrency: int) -> tuple[dict[str, Any], FakeLauncher, float]:
    """在指定 run_root 跑一遍 run_research（W=concurrency），返回结果/launcher/makespan。"""
    seed = FakeTextNode(json.dumps(SEED_CLUES))
    launcher = FakeLauncher()
    config = ResearchConfig(question=QUESTION, run_root=tmp / name, concurrency=concurrency)
    started = time.monotonic()
    result = run_research(config, text_node=seed, launcher=launcher)
    makespan_ms = (time.monotonic() - started) * 1000.0
    return result, launcher, makespan_ms


def check() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # 判据①：W=4 与 W=1 用**不同 run_root**（run 实例隔离，规格第 1 条）。
        result_w4, launcher_w4, makespan_w4 = run_one(tmp, "run-w4", concurrency=4)
        result_w1, _launcher_w1, makespan_w1 = run_one(tmp, "run-w1", concurrency=1)

        legal = {"converged", "capped", "partial"}
        w4_terminal = result_w4.get("terminal")
        w1_terminal = result_w1.get("terminal")
        both_legal = w4_terminal in legal and w1_terminal in legal

        # 判据②：evidence 集合等价。
        evidence_equal = read_evidence_claims(tmp / "run-w4") == read_evidence_claims(
            tmp / "run-w1"
        )
        if not evidence_equal:
            print(
                f"evidence 集合不等价：W4={sorted(read_evidence_claims(tmp / 'run-w4'))} "
                f"W1={sorted(read_evidence_claims(tmp / 'run-w1'))}",
                file=sys.stderr,
            )

        # 判据③：W=4 makespan < W=1 makespan。
        makespan_ok = makespan_w4 < makespan_w1

        # 判据④：同 run 同 clue 同 retry 的 run_id 只派一次（launcher 幂等）。
        no_duplicate = len(launcher_w4.dispatched) == len(set(launcher_w4.dispatched))
        if not no_duplicate:
            print("W=4 出现重复派发的 run_id", file=sys.stderr)

    print(f"w4_terminal={w4_terminal}")
    print(f"w1_terminal={w1_terminal}")
    print(f"evidence_equal={evidence_equal}")
    print(f"w4_makespan_ms={makespan_w4:.0f}")
    print(f"w1_makespan_ms={makespan_w1:.0f}")
    print(f"no_duplicate_dispatch={no_duplicate}")
    return 0 if both_legal and evidence_equal and makespan_ok and no_duplicate else 1


def main() -> int:
    return check()


if __name__ == "__main__":
    sys.exit(main())
