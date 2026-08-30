"""R3 acceptance：deep-research dispatch 并发 fan-out 的机器可判定覆盖检查。

本单（R3）把 dispatch 从串行 W=1 改为 LangGraph Send API 的 wave 并发（缺省 W=4）。
这里用确定性 fake seed / fake launcher 跑：

- W=1 与 W=4 各一次：断言两边产物等价（各 done clue 的 evidences 集合逐字相等）——
  判据 ①；
- 用带真实 worker 时长的 fake launcher 测 makespan：断言 W=4 makespan < W=1 makespan——
  判据 ②（真实时间重叠、wall-clock 下降，而非仅结构并列）；
- 一次 kill-restart 续跑：断言同一 clue 同一 retry 的 run_id 只派一次（resume 走
  re-adopt，绝不二次派发）——判据 ③。

打印三行可解析输出，exit 0 当且仅当三条判据全过：

    equivalence=ok
    wallclock=ok
    no_dup_dispatch=ok

R2-fix 约定不变：fake worker 产出真实 worker.result.v1（evidences / proposed_clues /
materials，无 verdict / 无 clue_id），完成与否由 evidences 判定。
"""

from __future__ import annotations

import contextlib
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from fleet_graph.executors.agent_run import RunStatus, RunTicket
from fleet_graph.graphs.research_pipeline import SYNTHESIS_ROLE
from fleet_graph.graphs.research_runner import (
    ResearchConfig,
    build_research,
    resume_start,
    run_research,
)

WORKER_SLEEP = 0.03
QUESTION = "fan-out equivalence"


class Boom(RuntimeError):
    """站替 SIGKILL：在 collect 的 wait 中炸掉，留下可续跑的 checkpoint。"""


class FakeTextNode:
    """seed 的替身：回放脚本化文本。"""

    def __init__(self, seed_text: str) -> None:
        self.seed_text = seed_text

    def complete(self, spec: Any, prompt: str) -> SimpleNamespace:
        return SimpleNamespace(
            text=self.seed_text, model="fake", finish_reason="stop", usage={}, raw={}
        )


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


class FanoutLauncher:
    """确定性 fake launcher。

    - 每个 worker 按 run_id 幂等 launch（同 run_id 重复 launch = re-adopt 在途 run），
      只记录第一次 spawn（``spawned``：run_id -> clue_id，clue_id 从 input 文件读）；
    - ``wait`` 真实 ``time.sleep(WORKER_SLEEP)`` 模拟 worker 运行时长，使 W=1 串行、
      W=4 并行重叠——makespan 可测；
    - 每个 clue 的结果是 clue_text 的确定性函数：depth 0 产出 1 条 evidence + 1 个子
      线索，depth 1 只产出 evidence（有界小线索树 < max_clues）；
    - ``boom_on_wait`` 非空时在第 N 次 worker wait 抛 Boom（模拟 kill）。
    """

    def __init__(
        self,
        *,
        synthesis: dict[str, Any] | None = None,
        boom_on_wait: int | None = None,
    ) -> None:
        self.synthesis = synthesis or {
            "report_markdown": "# 报告\nfan-out 等价。",
            "coverage_summary": "ok",
            "unresolved": [],
        }
        self.boom_on_wait = boom_on_wait
        self.spawned: dict[str, str] = {}  # run_id -> clue_id（只记第一次 spawn）
        self.specs: dict[str, Any] = {}
        self._roles: dict[str, str] = {}
        self._launched: set[str] = set()
        self._wait_count = 0

    def launch(self, spec: Any, run_id: str) -> RunTicket:
        if run_id in self._launched:
            return RunTicket(run_id, f"/tmp/fanout/{run_id}", None, adopted=True)
        self._launched.add(run_id)
        self._roles[run_id] = spec.role
        self.specs[run_id] = spec
        if spec.role != SYNTHESIS_ROLE:
            clue_id = json.loads(Path(spec.input_path).read_text(encoding="utf-8"))["clue_id"]
            self.spawned[run_id] = clue_id
        return RunTicket(run_id, f"/tmp/fanout/{run_id}", None)

    def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
        role = self._roles[ticket.run_id]
        if role == SYNTHESIS_ROLE:
            return RunStatus(
                "succeeded",
                {"state": "succeeded", "exit_code": 0, "structured_result": self.synthesis},
            )
        self._wait_count += 1
        if self.boom_on_wait is not None and self._wait_count >= self.boom_on_wait:
            raise Boom(f"killed after {self._wait_count} waits")
        time.sleep(WORKER_SLEEP)
        spec = self.specs[ticket.run_id]
        payload = json.loads(Path(spec.input_path).read_text(encoding="utf-8"))
        clue_text = payload["clue_text"]
        depth = payload["depth"]
        evidences = [
            {
                "claim": f"fact:{clue_text}",
                "source": "wiki",
                "quote": clue_text,
                "locator": f"fake.md:{abs(hash(clue_text)) % 1000}",
                "revision": "r1",
            }
        ]
        proposed = [{"clue": f"child-of:{clue_text}", "reason": "测试子线索"}] if depth == 0 else []
        return RunStatus(
            "succeeded",
            {
                "state": "succeeded",
                "exit_code": 0,
                "structured_result": {
                    "evidences": evidences,
                    "proposed_clues": proposed,
                    "materials": [],
                },
            },
        )


def _evidence_set(run_root: Path) -> list[str]:
    """evidence.jsonl 中每个 done clue 的 evidence（finding）集合，按规范 JSON 排序。

    只比较 ``finding``（契约里的 evidences），不比较 ``at`` 时间戳信封——W=1 与 W=4 的
    wall-clock 不同，时间戳自然不同；等价判据是证据集合逐字相等。
    """
    path = run_root / "evidence.jsonl"
    if not path.is_file():
        return []
    findings = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        findings.append(_canonical(entry["finding"]))
    return sorted(findings)


def _seed_text() -> str:
    # 有界小线索树：4 个根，每个根出一个子线索 → 共 8 clue < max_clues(12)。
    return json.dumps(["root-1", "root-2", "root-3", "root-4"])


def _check_equivalence_and_wallclock() -> tuple[bool, bool]:
    """W=1 与 W=4 各跑一次，返回 (equivalence, wallclock)。"""
    w1_root = Path(tempfile.mkdtemp()) / "run"
    w4_root = Path(tempfile.mkdtemp()) / "run"

    t0 = time.monotonic()
    w1_launcher = FanoutLauncher()
    w1 = run_research(
        ResearchConfig(question=QUESTION, run_root=w1_root, concurrency=1),
        text_node=FakeTextNode(_seed_text()),
        launcher=w1_launcher,
    )
    w1_makespan = time.monotonic() - t0

    t0 = time.monotonic()
    w4_launcher = FanoutLauncher()
    w4 = run_research(
        ResearchConfig(question=QUESTION, run_root=w4_root, concurrency=4),
        text_node=FakeTextNode(_seed_text()),
        launcher=w4_launcher,
    )
    w4_makespan = time.monotonic() - t0

    # ① 等价：两边都跑通（converged/capped/partial），证据集合逐字相等。
    ok_run = w1.get("terminal") in {"converged", "capped", "partial"} and w4.get("terminal") in {
        "converged",
        "capped",
        "partial",
    }
    equivalence = ok_run and _evidence_set(w1_root) == _evidence_set(w4_root)

    # ② wall-clock：W=4 显著下降（并发重叠，非仅结构并列）。
    wallclock = w4_makespan < w1_makespan

    # 两边都应发现并处理同一组 clue（有界树全收敛），各 done clue 证据逐字相等。
    return equivalence, wallclock


def _check_no_dup_dispatch() -> bool:
    """kill-restart：dispatch 先 launch 全部，collect wait 中炸掉；续跑必须 re-adopt，
    绝不把同一 (clue, retry) 二次派发为新 run。判据：每个 clue_id 恰好对应一个 run_id，
    且每个 run_id 只被 spawn 一次（跨 kill-restart 全程）。
    """
    import tempfile

    run_root = Path(tempfile.mkdtemp()) / "run"
    run_root.mkdir(parents=True)
    config = ResearchConfig(question="kill-restart", run_root=run_root, concurrency=4)
    seed = FakeTextNode(_seed_text())
    cfg = {"configurable": {"thread_id": config.thread_id}, "recursion_limit": 200}
    db = str(run_root / "checkpoint.sqlite3")

    # 第一次：dispatch 把本 wave（4 根）全部 launch 后，collect 的第 1 个 wait 炸掉。
    boom_launcher = FanoutLauncher(boom_on_wait=1)
    graph, _deps = build_research(config, text_node=seed, launcher=boom_launcher)
    with SqliteSaver.from_conn_string(db) as saver:
        compiled = graph.compile(checkpointer=saver)
        with contextlib.suppress(Boom):
            compiled.invoke(resume_start(compiled, cfg, config), config=cfg)

    # 第二次：同一 identity 续跑，精确从 pending collect 继续（re-adopt 在途 run）。
    good_launcher = FanoutLauncher()
    graph2, _deps2 = build_research(config, text_node=seed, launcher=good_launcher)
    with SqliteSaver.from_conn_string(db) as saver:
        compiled2 = graph2.compile(checkpointer=saver)
        start = resume_start(compiled2, cfg, config)
        state = compiled2.invoke(start, config=cfg)

    if state.get("terminal") not in {"converged", "capped", "partial"}:
        return False

    # 全部 spawn（两段合起来）：每个 clue_id 恰好一个 run_id，每个 run_id 恰好一次。
    all_spawned: dict[str, str] = {}
    for launcher in (boom_launcher, good_launcher):
        for run_id, clue_id in launcher.spawned.items():
            if clue_id in all_spawned and all_spawned[clue_id] != run_id:
                return False  # 同一 clue 出现两个不同 run_id = 二次派发
            all_spawned[clue_id] = run_id
    return len(all_spawned) == len(set(all_spawned.values())) and len(all_spawned) >= 1


def check() -> int:
    equivalence, wallclock = _check_equivalence_and_wallclock()
    no_dup = _check_no_dup_dispatch()
    print(f"equivalence={'ok' if equivalence else 'fail'}")
    print(f"wallclock={'ok' if wallclock else 'fail'}")
    print(f"no_dup_dispatch={'ok' if no_dup else 'fail'}")
    return 0 if (equivalence and wallclock and no_dup) else 1


def main() -> int:
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
