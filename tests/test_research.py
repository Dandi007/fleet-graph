"""R5-1：research 串行闭环图（fake TextNode + fake launcher）。

覆盖规格要求的五类验证：
1. 图级端到端（一题两轮收敛）：evidence.jsonl 逐条落盘、report.md 生成、
   result.json 终态 converged。
2. capped 负例：max_clues 触顶终态为 capped 而非 converged。
3. partial 负例：一个 clue retry 耗尽 blocked，终态 partial。
4. resume：checkpoint 中断后重跑进入精确续跑分支（参照 tests/test_line_restart.py）。
5. worker run id 派生稳定：同 thread 同 clue 同 retry 派生同 id。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from fleet_graph.executors.agent_run import RunStatus, RunTicket, RunWaitTimeout, derive_run_id
from fleet_graph.graphs.research_pipeline import (
    ADVOCATE_ROLE,
    ARBITER_ROLE,
    CLUE_BLOCKED,
    CLUE_DONE,
    CLUE_OPEN,
    DEFAULT_SOURCE,
    JUDGE_ROLE,
    OPPONENT_ROLE,
    SOURCE_ROLE,
    TERMINAL_CAPPED,
    TERMINAL_CONVERGED,
    TERMINAL_FAULT,
    TERMINAL_PARTIAL,
    ResearchBounds,
    converge,
    debate_run_id,
    derive_clue_id,
    derive_research_id,
    initial_state,
    worker_run_id,
)
from fleet_graph.graphs.research_runner import (
    EVENTS,
    RESULT,
    ResearchConfig,
    build_research,
    resume_start,
    run_research,
)


class FakeTextNode:
    """seed 的替身：回放脚本化文本，并记录调用次数以证明 resume 不重放 seed。"""

    def __init__(self, seed_text: str) -> None:
        self.seed_text = seed_text
        self.calls = 0
        self.prompts: list[str] = []

    def complete(self, spec: Any, prompt: str) -> SimpleNamespace:
        self.calls += 1
        self.prompts.append(prompt)
        return SimpleNamespace(
            text=self.seed_text, model="fake", finish_reason="stop", usage={}, raw={}
        )


def worker_payload(claims: list[str], proposed: list[str]) -> dict[str, Any]:
    """worker.result.v1 形状的结构化结果（roles 侧 schema 为 SSoT）。

    R2-fix：契约无 verdict / 无 clue_id——完成与否由 evidences 判定。
    """
    return {
        "evidences": [
            {"claim": c, "source": "wiki", "quote": c, "locator": f"fake.md:{i + 1}"}
            for i, c in enumerate(claims)
        ],
        "proposed_clues": [{"clue": t, "reason": "测试线索"} for t in proposed],
        "materials": [],
    }


def worker_result(claims: list[str], proposed: list[str]) -> dict[str, Any]:
    """agent-run 成功信封：structured_result 携带契约形状的 evidences 与子线索。"""
    return {
        "state": "succeeded",
        "exit_code": 0,
        "structured_result": worker_payload(claims, proposed),
    }


def unparseable_worker_result() -> dict[str, Any]:
    """成功信封但其信封不可解析 / 不是合法 worker.result.v1。

    新契约无 verdict 字段；blocked 语义改由「信封解析失败 / run 失败（status.ok
    为假）」触发——这里返回缺 structured_result 对象的信封，collect 解析失败即走
    clue 失败路径（retry/block）。
    """
    return {"state": "succeeded", "exit_code": 0, "result": "工具面不可达的叙述"}


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


def judge_body(ruled: list[str] | None = None, open_disagreements: list[str] | None = None) -> str:
    """judge 的 dr-doc body：按 R4 约定输出 RULE: / OPEN DISAGREEMENT: 段行。"""
    lines = ["# judge 裁定"]
    for r in ruled or []:
        lines.append(r)
    for o in open_disagreements or []:
        lines.append(f"OPEN DISAGREEMENT: {o}")
    return "\n".join(lines)


def default_debate(
    ruled: list[str] | None = None,
    open_disagreements: list[str] | None = None,
    verdict: str = "enough",
    rationale: str = "证据已充分",
) -> dict[str, Any]:
    """四角色的回放信封（按角色常量寻址）。judge 用 judge_body 结构化产出。"""
    return {
        ADVOCATE_ROLE: debater_result("# advocate 论证\n正面支持结论。"),
        OPPONENT_ROLE: debater_result("# opponent 论证\n对结论路径提出反驳。"),
        JUDGE_ROLE: debater_result(judge_body(ruled, open_disagreements)),
        ARBITER_ROLE: arbiter_result(verdict, rationale),
    }


def unparseable_debate_result() -> dict[str, Any]:
    """成功信封但信封不可解析（缺 structured_result 对象）——debate 角色直接 fault。"""
    return {"state": "succeeded", "exit_code": 0, "result": "无法解析的叙述"}


def legacy_worker_result(claims: list[str], proposed: list[str]) -> dict[str, Any]:
    """旧信封：结果藏在 `result` 键（而不是 `structured_result`），只有
    parse_envelope 能拆出来——harvest 不得手搓 `.get("structured_result")`。"""
    return {
        "state": "succeeded",
        "exit_code": 0,
        "result": worker_payload(claims, proposed),
    }


class Boom(RuntimeError):
    """站替 SIGKILL：在 collect 的 wait 中炸掉，留下一个可续跑的 checkpoint。"""


class FakeLauncher:
    """worker/debate 的替身：按 role 回放脚本，记录派发的 run id。

    ``debate`` 按角色常量寻址（dict[role_str, envelope]，``"fail"`` 哨兵表示 debater
    run 失败）。``launch`` 幂等（R3）：同 run_id 重复 launch = re-adopt 在途 run（与
    真实 AgentRunLauncher 一致），只记录第一次 spawn，不重复进 ``dispatched``。
    """

    def __init__(
        self,
        worker_script: list[Any],
        debate: dict[str, Any] | None = None,
        *,
        boom: bool = False,
    ) -> None:
        self.worker_script = list(worker_script)
        self.debate = debate or {}
        self.boom = boom
        self.dispatched: list[str] = []
        self.specs: dict[str, Any] = {}
        self._roles: dict[str, str] = {}
        self._launched: set[str] = set()

    def launch(self, spec: Any, run_id: str) -> RunTicket:
        if run_id in self._launched:
            return RunTicket(run_id, f"/tmp/fake/{run_id}", None, adopted=True)
        self._launched.add(run_id)
        self._roles[run_id] = spec.role
        self.specs[run_id] = spec
        self.dispatched.append(run_id)
        return RunTicket(run_id, f"/tmp/fake/{run_id}", None)

    def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
        if self.boom:
            raise Boom("killed during worker run")
        role = self._roles[ticket.run_id]
        if role in self.debate:
            item = self.debate[role]
            if item == "fail":
                return RunStatus("failed", {"state": "failed", "exit_code": 1})
            return RunStatus("succeeded", item)
        item = self.worker_script.pop(0)
        if item == "fail":
            return RunStatus("failed", {"state": "failed", "exit_code": 1})
        if item == "timeout":
            raise RunWaitTimeout(ticket, waited_seconds=999.0)
        return RunStatus("succeeded", item)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestIdentity:
    def test_research_id_is_content_addressed(self) -> None:
        question = "fleet-graph 的调度器如何工作?"
        rid = derive_research_id(question)
        assert rid.startswith("r-")
        assert len(rid) == 14  # "r-" + 12 hex
        assert derive_research_id(question) == rid
        assert derive_research_id("另一个问题") != rid

    def test_thread_id_injects_run_instance(self, tmp_path: Path) -> None:
        # R3-fix：thread 身份注入稳定非随机的 run 实例分量（规格第 1 条）。
        config = ResearchConfig(question="q", run_root=tmp_path, generation=3)
        assert config.thread_id.startswith(f"{config.research_id}:g3:i-")
        # 同 run_root 恒同，不同 run_root 恒不同。
        same = ResearchConfig(question="q", run_root=tmp_path, generation=3)
        other = ResearchConfig(question="q", run_root=tmp_path / "run2", generation=3)
        assert config.thread_id == same.thread_id
        assert config.thread_id != other.thread_id

    def test_explicit_instance_overrides_run_root_derivation(self, tmp_path: Path) -> None:
        # 显式 instance 优先于 run_root 内容寻址，且必须稳定（规格第 1 条）。
        a = ResearchConfig(question="q", run_root=tmp_path / "a", instance="i-abc")
        b = ResearchConfig(question="q", run_root=tmp_path / "b", instance="i-abc")
        assert a.thread_id == b.thread_id
        assert a.thread_id == f"{a.research_id}:g1:i-abc"

    def test_checkpoint_defaults_to_disk_under_run_root(self, tmp_path: Path) -> None:
        config = ResearchConfig(question="q", run_root=tmp_path)
        assert config.resolved_checkpoint_path == str(tmp_path / "checkpoint.sqlite3")


class TestWorkerRunIdDerivation:
    def test_same_thread_clue_retry_derives_the_same_id(self) -> None:
        thread = "r-abcdef123456:g1:i-000000000000"
        clue = "c-fedcba654321"
        assert worker_run_id(thread, clue, 0) == worker_run_id(thread, clue, 0)
        assert worker_run_id(thread, clue, 0) == derive_run_id(thread, f"worker/{clue}", 1)

    def test_a_retry_bumps_the_derived_attempt(self) -> None:
        thread = "r-abcdef123456:g1:i-000000000000"
        clue = "c-fedcba654321"
        assert worker_run_id(thread, clue, 1) == derive_run_id(thread, f"worker/{clue}", 2)
        assert worker_run_id(thread, clue, 0) != worker_run_id(thread, clue, 1)


class TestRunInstanceIsolation:
    """R3-fix：run 身份按 run 实例隔离（规格第 1 条）。

    同一题两次独立跑（不同 run_root）派生不同 run_id——不再撞 bus 409；同一次 run 的
    kill-restart（同 run_root）仍得相同 run_id——re-adopt/幂等不回退（判据 ③④）。
    """

    def test_different_run_root_derives_different_run_ids(self, tmp_path: Path) -> None:
        a = ResearchConfig(question="q", run_root=tmp_path / "run-a")
        b = ResearchConfig(question="q", run_root=tmp_path / "run-b")
        # research_id 仍内容寻址（同一题恒同），thread/run_id 才按实例隔离。
        assert a.research_id == b.research_id
        assert a.thread_id != b.thread_id
        clue = derive_clue_id("clue one", DEFAULT_SOURCE)
        assert worker_run_id(a.thread_id, clue, 0) != worker_run_id(b.thread_id, clue, 0)
        assert debate_run_id(a.thread_id, ARBITER_ROLE) != debate_run_id(b.thread_id, ARBITER_ROLE)

    def test_same_run_root_derives_same_run_ids(self, tmp_path: Path) -> None:
        a = ResearchConfig(question="q", run_root=tmp_path / "run")
        b = ResearchConfig(question="q", run_root=tmp_path / "run")
        assert a.thread_id == b.thread_id
        clue = derive_clue_id("clue one", DEFAULT_SOURCE)
        assert worker_run_id(a.thread_id, clue, 0) == worker_run_id(b.thread_id, clue, 0)
        assert debate_run_id(a.thread_id, ARBITER_ROLE) == debate_run_id(b.thread_id, ARBITER_ROLE)

    def test_run_instance_is_stable_and_not_random(self, tmp_path: Path) -> None:
        from fleet_graph.graphs.research_pipeline import derive_run_instance

        inst = derive_run_instance(tmp_path / "run")
        assert inst.startswith("i-")
        assert len(inst) == 14  # "i-" + 12 hex
        assert inst == derive_run_instance(tmp_path / "run")
        assert inst != derive_run_instance(tmp_path / "run-other")
        # 稳定非随机：同一 run_root 恒同（kill-restart 不漂移）。
        assert ResearchConfig(question="q", run_root=tmp_path / "run").run_instance == inst


class TestConvergeIsPure:
    def _bounds(self, **overrides: int) -> ResearchBounds:
        return ResearchBounds(
            max_clues=overrides.get("max_clues", 12),
            max_depth=overrides.get("max_depth", 6),
            zero_growth_rounds=overrides.get("zero_growth_rounds", 3),
            max_rounds=overrides.get("max_rounds", 24),
        )

    def test_capped_wins_over_converged_on_max_clues(self) -> None:
        clues = [
            {"id": "c1", "status": CLUE_OPEN, "depth": 0, "retry": 0},
            {"id": "c2", "status": CLUE_OPEN, "depth": 1, "retry": 0},
        ]
        state = {"clues": clues, "rounds": 1, "coverage": 1, "zero_growth_rounds": 0}
        assert converge(state, self._bounds(max_clues=2)) == TERMINAL_CAPPED

    def test_depth_cap_is_capped(self) -> None:
        clues = [{"id": "c1", "status": CLUE_OPEN, "depth": 2, "retry": 0}]
        state = {"clues": clues, "rounds": 1, "coverage": 0, "zero_growth_rounds": 0}
        assert converge(state, self._bounds(max_depth=2)) == TERMINAL_CAPPED

    def test_no_open_clues_is_converged(self) -> None:
        clues = [{"id": "c1", "status": CLUE_DONE, "depth": 0, "retry": 0}]
        state = {"clues": clues, "rounds": 1, "coverage": 1, "zero_growth_rounds": 0}
        assert converge(state, self._bounds()) == TERMINAL_CONVERGED

    def test_blocked_clue_with_rest_converged_is_partial(self) -> None:
        clues = [
            {"id": "c1", "status": CLUE_BLOCKED, "depth": 0, "retry": 2},
            {"id": "c2", "status": CLUE_DONE, "depth": 1, "retry": 0},
        ]
        state = {"clues": clues, "rounds": 3, "coverage": 1, "zero_growth_rounds": 2}
        assert converge(state, self._bounds()) == TERMINAL_PARTIAL

    def test_open_clues_still_continue(self) -> None:
        clues = [{"id": "c1", "status": CLUE_OPEN, "depth": 0, "retry": 0}]
        state = {"clues": clues, "rounds": 0, "coverage": 0, "zero_growth_rounds": 0}
        assert converge(state, self._bounds()) == "continue"


class TestEndToEnd:
    def test_one_question_two_rounds_converges(self, tmp_path: Path) -> None:
        question = "fleet-graph 的调度器如何工作?"
        seed = FakeTextNode(json.dumps(["scheduler 的基本循环"]))
        ruled = ["RULE: 分歧一 裁决：每轮 tick 检查成立 [anchor: wiki@fake.md:1]"]
        open_disagreements = ["分歧二 双方证据均不足以裁决"]
        launcher = FakeLauncher(
            [
                worker_result(["每轮 tick 检查所有 line"], ["tick 的唤醒源"]),
                worker_result(["systemd timer 唤醒"], []),
            ],
            default_debate(ruled=ruled, open_disagreements=open_disagreements),
        )
        config = ResearchConfig(question=question, run_root=tmp_path / "run")

        result = run_research(config, text_node=seed, launcher=launcher)

        assert result["terminal"] == TERMINAL_CONVERGED
        assert result["rounds"] == 2

        # evidence.jsonl 逐条落盘（规格第 7 条：findings 只落盘不进 state），
        # 每条保留契约的结构化对象（claim/source/quote/locator），不 str() 压平。
        evidence = read_jsonl(tmp_path / "run" / "evidence.jsonl")
        assert [e["finding"]["claim"] for e in evidence] == [
            "每轮 tick 检查所有 line",
            "systemd timer 唤醒",
        ]
        assert all(e["finding"]["locator"] for e in evidence)
        assert all(e["clue_id"] for e in evidence)

        # report.md 由 debate_report 脚本节点从 judge/arbiter 产出组装（零 LLM），
        # 必须含 `## 分歧裁定` 段、judge 的 OPEN DISAGREEMENT 逐字保留、arbiter 记录。
        report = (tmp_path / "run" / "report.md").read_text(encoding="utf-8")
        assert "## 分歧裁定" in report
        assert (
            "### 已裁定分歧" in report and "### 开放分歧" in report and "### arbiter 裁决" in report
        )
        assert "OPEN DISAGREEMENT: 分歧二 双方证据均不足以裁决" in report
        assert "RULE: 分歧一 裁决：每轮 tick 检查成立 [anchor: wiki@fake.md:1]" in report
        assert "- verdict: enough" in report
        persisted = json.loads((tmp_path / "run" / RESULT).read_text(encoding="utf-8"))
        assert persisted["terminal"] == TERMINAL_CONVERGED
        assert persisted["rounds"] == 2

        # worker run id 派生稳定：与 derive_run_id(thread, f"worker/{clue_id}", retry+1) 一致。
        thread = config.thread_id
        # seed 纯字符串回填默认源，clue id 带 source 寻址（R2）。
        clue_one = derive_clue_id("scheduler 的基本循环", DEFAULT_SOURCE)
        clue_two = derive_clue_id("tick 的唤醒源", DEFAULT_SOURCE)
        # R4：debate 子图 run id 用 derive_run_id(thread, "debate/<role>", 1) 派生。
        assert launcher.dispatched == [
            derive_run_id(thread, f"worker/{clue_one}", 1),
            derive_run_id(thread, f"worker/{clue_two}", 1),
            debate_run_id(thread, "advocate"),
            debate_run_id(thread, "opponent"),
            debate_run_id(thread, "judge"),
            debate_run_id(thread, "arbiter"),
        ]
        # 四角色逐字使用已交付角色名（不新造角色）。
        for role_id, role in (
            (debate_run_id(thread, "advocate"), ADVOCATE_ROLE),
            (debate_run_id(thread, "opponent"), OPPONENT_ROLE),
            (debate_run_id(thread, "judge"), JUDGE_ROLE),
            (debate_run_id(thread, "arbiter"), ARBITER_ROLE),
        ):
            assert launcher.specs[role_id].role == role

    def test_events_are_persisted_by_the_runner(self, tmp_path: Path) -> None:
        seed = FakeTextNode(json.dumps(["clue one"]))
        launcher = FakeLauncher(
            [worker_result(["f1"], [])],
            default_debate(),
        )
        run_research(
            ResearchConfig(question="q", run_root=tmp_path / "run"),
            text_node=seed,
            launcher=launcher,
        )
        events = read_jsonl(tmp_path / "run" / EVENTS)
        # R4：四角色各一次 debate 事件 + debate_report（零 LLM 脚本节点）一次。
        # R5：finalise 之后 anchor_check（零 LLM 纯脚本节点）一次。
        assert [e["event"] for e in events] == [
            "seed",
            "dispatch",
            "collect",
            "harvest",
            "debate",
            "debate",
            "debate",
            "debate",
            "debate_report",
            "finalise",
            "anchor_check",
        ]
        arbiter_event = next(e for e in events if e.get("role") == "arbiter")
        assert arbiter_event["verdict"] == "enough"


class TestCapped:
    def test_max_clues_cap_terminates_as_capped_not_converged(self, tmp_path: Path) -> None:
        seed = FakeTextNode(json.dumps(["clue one"]))
        launcher = FakeLauncher(
            [worker_result(["f1"], ["clue two"])],
            default_debate(),
        )
        config = ResearchConfig(question="q", run_root=tmp_path / "run", max_clues=2)

        result = run_research(config, text_node=seed, launcher=launcher)

        assert result["terminal"] == TERMINAL_CAPPED
        assert result["terminal"] != TERMINAL_CONVERGED
        persisted = json.loads((tmp_path / "run" / RESULT).read_text(encoding="utf-8"))
        assert persisted["terminal"] == TERMINAL_CAPPED


class TestPartial:
    def test_retry_exhausted_blocked_clue_terminates_as_partial(self, tmp_path: Path) -> None:
        seed = FakeTextNode(json.dumps(["clue one"]))
        launcher = FakeLauncher(["fail", "fail"], default_debate())
        config = ResearchConfig(question="q", run_root=tmp_path / "run")

        result = run_research(config, text_node=seed, launcher=launcher)

        assert result["terminal"] == TERMINAL_PARTIAL
        assert result["rounds"] == 2
        # clue 板显示该 clue retry 耗尽 blocked（绝不 fault 整图）。
        persisted = json.loads((tmp_path / "run" / RESULT).read_text(encoding="utf-8"))
        assert persisted["terminal"] == TERMINAL_PARTIAL
        # 两次派发用了两个不同 attempt 的 run id。
        clue_one = derive_clue_id("clue one", DEFAULT_SOURCE)
        thread = config.thread_id
        assert launcher.dispatched[:2] == [
            derive_run_id(thread, f"worker/{clue_one}", 1),
            derive_run_id(thread, f"worker/{clue_one}", 2),
        ]


class TestWorkerWaitTimeout:
    def test_timeout_retries_then_blocks_instead_of_faulting(self, tmp_path: Path) -> None:
        # 单个 worker wait 超时绝不 fault 整图（规格第 7 条）：两次超时耗尽 retry
        # 后置 blocked，终态 partial，而不是把 RunWaitTimeout 抛穿 collect。
        seed = FakeTextNode(json.dumps(["clue one"]))
        launcher = FakeLauncher(["timeout", "timeout"], default_debate())
        config = ResearchConfig(question="q", run_root=tmp_path / "run")

        result = run_research(config, text_node=seed, launcher=launcher)

        assert result["terminal"] == TERMINAL_PARTIAL
        assert result["rounds"] == 2
        clue_one = derive_clue_id("clue one", DEFAULT_SOURCE)
        thread = config.thread_id
        assert launcher.dispatched[:2] == [
            derive_run_id(thread, f"worker/{clue_one}", 1),
            derive_run_id(thread, f"worker/{clue_one}", 2),
        ]


class TestHarvestEnvelope:
    def test_legacy_result_key_envelope_still_yields_sub_clues(self, tmp_path: Path) -> None:
        # harvest 必须复用 parse_envelope：旧信封把结果放在 `result` 键时，子线索
        # 仍要被提取并继续深挖，而不是静默丢弃后提前报 converged。
        question = "q"
        seed = FakeTextNode(json.dumps(["parent clue"]))
        launcher = FakeLauncher(
            [
                legacy_worker_result(["f1"], ["child clue"]),
                worker_result(["f2"], []),
            ],
            default_debate(),
        )
        config = ResearchConfig(question=question, run_root=tmp_path / "run")

        result = run_research(config, text_node=seed, launcher=launcher)

        # 子线索没有被丢弃：父线索 done 后继续派发 child clue，共两轮。
        assert result["rounds"] == 2
        assert result["terminal"] == TERMINAL_CONVERGED
        parent = derive_clue_id("parent clue", DEFAULT_SOURCE)
        child = derive_clue_id("child clue", DEFAULT_SOURCE)
        thread = config.thread_id
        assert launcher.dispatched[:2] == [
            derive_run_id(thread, f"worker/{parent}", 1),
            derive_run_id(thread, f"worker/{child}", 1),
        ]


class TestInputContract:
    """roles 声明 protocol.input 后 agent-run 强制 --input：spec 必须带 input_path。"""

    def test_worker_and_debate_runs_carry_input_files(self, tmp_path: Path) -> None:
        question = "fleet-graph 的调度器如何工作?"
        seed = FakeTextNode(json.dumps(["scheduler 的基本循环"]))
        launcher = FakeLauncher(
            [worker_result(["f1"], [])],
            default_debate(ruled=["RULE: 分歧一 裁决：f1 成立 [anchor: wiki@fake.md:1]"]),
        )
        config = ResearchConfig(question=question, run_root=tmp_path / "run")

        result = run_research(config, text_node=seed, launcher=launcher)
        assert result["terminal"] == TERMINAL_CONVERGED

        # worker：input 文件存在且是 deep-research.worker-input/v1 形状（含 sources）。
        clue = derive_clue_id("scheduler 的基本循环", DEFAULT_SOURCE)
        worker_spec = launcher.specs[derive_run_id(config.thread_id, f"worker/{clue}", 1)]
        assert worker_spec.input_path, "缺 --input 会被 agent-run 判 CONTRACT_ERROR"
        payload = json.loads(Path(worker_spec.input_path).read_text(encoding="utf-8"))
        assert payload == {
            "clue_id": clue,
            "clue_text": "scheduler 的基本循环",
            "depth": 0,
            "sources": [DEFAULT_SOURCE],
        }
        # R2：dispatch 按 clue.source 路由到 SOURCE_ROLE 角色（fake launcher 记录 spec.role）。
        assert worker_spec.role == SOURCE_ROLE[DEFAULT_SOURCE]

        thread = config.thread_id
        # R4 debater：--input 只携带 deep-research.debater-input/v1 manifest，语料走
        # --prompt-file。advocate/opponent/judge 共用证据形状 {anchor, quote, claim}。
        advocate_spec = launcher.specs[debate_run_id(thread, "advocate")]
        assert advocate_spec.role == ADVOCATE_ROLE
        assert advocate_spec.input_path.endswith("debate-advocate.json")
        manifest = json.loads(Path(advocate_spec.input_path).read_text(encoding="utf-8"))
        assert manifest["question"] == question
        assert manifest["evidences"] == [
            {
                "anchor": "wiki@fake.md:1",
                "quote": "f1",
                "claim": "f1",
                "clue_id": clue,
            }
        ]

        # judge：prior_arguments 逐字携带 advocate/opponent 的 body。
        judge_spec = launcher.specs[debate_run_id(thread, "judge")]
        judge_manifest = json.loads(Path(judge_spec.input_path).read_text(encoding="utf-8"))
        assert judge_manifest["prior_arguments"] == [
            (tmp_path / "run" / "debate" / "advocate.md").read_text(encoding="utf-8"),
            (tmp_path / "run" / "debate" / "opponent.md").read_text(encoding="utf-8"),
        ]
        judge_prompt = (tmp_path / "run" / "inputs" / "debate-judge-prompt.md").read_text(
            encoding="utf-8"
        )
        # 语料经 --prompt-file 投递（题面 + 证据 + 前序 body）。
        assert "# advocate 论证\n正面支持结论。" in judge_prompt

        # arbiter：deep-research.arbiter-input/v1 形状（board_stats / clue_titles /
        # recent_claims / recent_rounds）。
        arbiter_spec = launcher.specs[debate_run_id(thread, "arbiter")]
        assert arbiter_spec.role == ARBITER_ROLE
        arbiter_manifest = json.loads(Path(arbiter_spec.input_path).read_text(encoding="utf-8"))
        assert arbiter_manifest["question"] == question
        assert arbiter_manifest["board_stats"] == {
            "total": 1,
            "done": 1,
            "blocked": 0,
            "open": 0,
        }
        assert arbiter_manifest["clue_titles"] == ["scheduler 的基本循环"]
        assert arbiter_manifest["recent_claims"] == ["f1"]
        assert arbiter_manifest["recent_rounds"] == 1

        # 四角色产出逐字落 run_root/debate/（advocate.md / opponent.md / judge.md / arbiter.json）。
        assert (tmp_path / "run" / "debate" / "advocate.md").is_file()
        assert (tmp_path / "run" / "debate" / "opponent.md").is_file()
        assert (tmp_path / "run" / "debate" / "judge.md").is_file()
        arbiter_out = json.loads(
            (tmp_path / "run" / "debate" / "arbiter.json").read_text(encoding="utf-8")
        )
        assert arbiter_out["verdict"] == "enough"

    def test_each_retry_dispatch_writes_its_own_input_file(self, tmp_path: Path) -> None:
        seed = FakeTextNode(json.dumps(["clue one"]))
        launcher = FakeLauncher(["fail", worker_result(["f1"], [])], default_debate())
        config = ResearchConfig(question="q", run_root=tmp_path / "run")

        result = run_research(config, text_node=seed, launcher=launcher)
        assert result["terminal"] == TERMINAL_CONVERGED

        clue = derive_clue_id("clue one", DEFAULT_SOURCE)
        first = launcher.specs[derive_run_id(config.thread_id, f"worker/{clue}", 1)]
        second = launcher.specs[derive_run_id(config.thread_id, f"worker/{clue}", 2)]
        assert first.input_path.endswith(f"worker-{clue}-r0.json")
        assert second.input_path.endswith(f"worker-{clue}-r1.json")
        for spec in (first, second):
            assert json.loads(Path(spec.input_path).read_text(encoding="utf-8"))["clue_id"] == clue


class TestBlockedWorkerResult:
    """新契约 worker.result.v1 无 verdict 字段：blocked 语义改由「run 失败
    （status.ok 为假）/ 信封解析失败」触发，仍走 retry/block，不得计入 done。
    """

    def test_run_failure_retries_then_blocks_instead_of_done(self, tmp_path: Path) -> None:
        # run 失败（status.ok 为假）不是调查完成：走 clue 失败路径
        # （retry -> blocked -> 终态 partial），不得计入 done/coverage。
        seed = FakeTextNode(json.dumps(["clue one"]))
        launcher = FakeLauncher(["fail", "fail"], default_debate())
        config = ResearchConfig(question="q", run_root=tmp_path / "run")

        result = run_research(config, text_node=seed, launcher=launcher)

        assert result["terminal"] == TERMINAL_PARTIAL
        assert not (tmp_path / "run" / "evidence.jsonl").exists()

    def test_envelope_parse_failure_retries_then_blocks_instead_of_done(
        self, tmp_path: Path
    ) -> None:
        # 信封解析失败（structured_result 缺失）同样不是调查完成：走 clue 失败
        # 路径（retry -> blocked -> 终态 partial），不得计入 done/coverage。
        seed = FakeTextNode(json.dumps(["clue one"]))
        launcher = FakeLauncher(
            [unparseable_worker_result(), unparseable_worker_result()],
            default_debate(),
        )
        config = ResearchConfig(question="q", run_root=tmp_path / "run")

        result = run_research(config, text_node=seed, launcher=launcher)

        assert result["terminal"] == TERMINAL_PARTIAL
        assert not (tmp_path / "run" / "evidence.jsonl").exists()


class TestNoVerdictWorkerResultIsDone:
    """回归（R2-fix，R4 承接）：无 verdict 的合法 worker.result.v1（evidences 非空）
    必须判 done、coverage 增长、evidences 落 evidence.jsonl 并进入 debate 阶段
    （advocate manifest 的 evidences 非空）——而不是被当成 clue 失败 retry/block。
    """

    def test_evidences_without_verdict_are_done_and_reach_debate(self, tmp_path: Path) -> None:
        seed = FakeTextNode(json.dumps(["clue one"]))
        launcher = FakeLauncher(
            [worker_result(["事实 A", "事实 B"], [])],
            default_debate(),
        )
        config = ResearchConfig(question="q", run_root=tmp_path / "run")

        result = run_research(config, text_node=seed, launcher=launcher)

        assert result["terminal"] == TERMINAL_CONVERGED
        # coverage 随 done 数增长（无 zero-growth 误触，rounds=1 即收敛）。
        assert result["rounds"] == 1
        # evidences 落盘 evidence.jsonl。
        evidence = read_jsonl(tmp_path / "run" / "evidence.jsonl")
        assert [e["finding"]["claim"] for e in evidence] == ["事实 A", "事实 B"]
        # R4：无 verdict 的 evidences 判 done 后进入 debate 阶段（advocate manifest
        # evidences 非空，逐条带 anchor）。
        manifest = json.loads(
            (tmp_path / "run" / "inputs" / "debate-advocate.json").read_text(encoding="utf-8")
        )
        assert [ev["claim"] for ev in manifest["evidences"]] == ["事实 A", "事实 B"]
        assert all(ev["anchor"] for ev in manifest["evidences"])


class TestDebateFault:
    """R4 边界：四角色 run 失败 / 信封不可解析 → TERMINAL_FAULT（响亮，不静默），
    且链路立即停止（绝不再往下跑后面的角色）。
    """

    def test_judge_run_failure_faults_whole_graph(self, tmp_path: Path) -> None:
        seed = FakeTextNode(json.dumps(["clue one"]))
        debate = default_debate()
        debate[JUDGE_ROLE] = "fail"
        launcher = FakeLauncher([worker_result(["f1"], [])], debate)
        config = ResearchConfig(question="q", run_root=tmp_path / "run")

        result = run_research(config, text_node=seed, launcher=launcher)

        assert result["terminal"] == TERMINAL_FAULT
        assert "judge" in result["terminal_reason"]
        assert not (tmp_path / "run" / "report.md").exists()
        # judge 失败后不再派 arbiter。
        thread = config.thread_id
        assert debate_run_id(thread, "arbiter") not in launcher.dispatched

    def test_arbiter_unparseable_envelope_faults(self, tmp_path: Path) -> None:
        seed = FakeTextNode(json.dumps(["clue one"]))
        debate = default_debate()
        debate[ARBITER_ROLE] = unparseable_debate_result()
        launcher = FakeLauncher([worker_result(["f1"], [])], debate)
        config = ResearchConfig(question="q", run_root=tmp_path / "run")

        result = run_research(config, text_node=seed, launcher=launcher)

        assert result["terminal"] == TERMINAL_FAULT
        assert "arbiter" in result["terminal_reason"]

    def test_arbiter_invalid_verdict_faults(self, tmp_path: Path) -> None:
        seed = FakeTextNode(json.dumps(["clue one"]))
        debate = default_debate()
        debate[ARBITER_ROLE] = {
            "state": "succeeded",
            "exit_code": 0,
            "structured_result": {"verdict": "maybe", "rationale": "??"},
        }
        launcher = FakeLauncher([worker_result(["f1"], [])], debate)
        config = ResearchConfig(question="q", run_root=tmp_path / "run")

        result = run_research(config, text_node=seed, launcher=launcher)

        assert result["terminal"] == TERMINAL_FAULT


class TestDebateReport:
    """R4：report.md 由脚本节点从 judge/arbiter 组装，判据 ① 与「本轮无未决分歧」显式落段。"""

    def test_zero_open_disagreements_still_writes_the_section(self, tmp_path: Path) -> None:
        seed = FakeTextNode(json.dumps(["clue one"]))
        launcher = FakeLauncher([worker_result(["f1"], [])], default_debate(ruled=[]))
        config = ResearchConfig(question="q", run_root=tmp_path / "run")

        result = run_research(config, text_node=seed, launcher=launcher)
        assert result["terminal"] == TERMINAL_CONVERGED

        report = (tmp_path / "run" / "report.md").read_text(encoding="utf-8")
        assert "## 分歧裁定" in report
        assert "### 开放分歧" in report
        # 判据 ① 的弦外要求：零条未决分歧时「开放分歧」段显式写本轮无未决分歧，不省略。
        assert "本轮无未决分歧" in report
        assert "### 已裁定分歧" in report
        assert "本轮无已裁定分歧" in report
        assert "### arbiter 裁决" in report

    def test_arbiter_continue_is_recorded_but_does_not_route(self, tmp_path: Path) -> None:
        """硬线：arbiter verdict=continue 仅记录落盘（report + events），不改动
        converge 的路由语义——本单仍按 converge 判定收敛为 converged。"""
        seed = FakeTextNode(json.dumps(["clue one"]))
        launcher = FakeLauncher(
            [worker_result(["f1"], [])],
            default_debate(verdict="continue", rationale="还有未穷尽的线索"),
        )
        config = ResearchConfig(question="q", run_root=tmp_path / "run")

        result = run_research(config, text_node=seed, launcher=launcher)
        assert result["terminal"] == TERMINAL_CONVERGED  # continue 不改动 converge 路由

        report = (tmp_path / "run" / "report.md").read_text(encoding="utf-8")
        assert "- verdict: continue" in report
        assert "还有未穷尽的线索" in report
        events = read_jsonl(tmp_path / "run" / EVENTS)
        arbiter_event = next(e for e in events if e.get("role") == "arbiter")
        assert arbiter_event["verdict"] == "continue"
        report_event = next(e for e in events if e["event"] == "debate_report")
        assert report_event["verdict"] == "continue"


class TestResume:
    def test_interrupted_run_resumes_exactly_and_never_replays_seed(self, tmp_path: Path) -> None:
        question = "fleet-graph 的调度器如何工作?"
        seed = FakeTextNode(json.dumps(["scheduler 的基本循环"]))
        config = ResearchConfig(question=question, run_root=tmp_path / "run")
        (tmp_path / "run").mkdir(parents=True)
        cfg = {"configurable": {"thread_id": config.thread_id}, "recursion_limit": 100}
        db = str(tmp_path / "run" / "checkpoint.sqlite3")

        # 第一次：collect 的 wait 中炸掉，留下指向 collect 的 checkpoint。
        boom_launcher = FakeLauncher([], default_debate(), boom=True)
        graph, _deps = build_research(config, text_node=seed, launcher=boom_launcher)
        with SqliteSaver.from_conn_string(db) as saver:
            compiled = graph.compile(checkpointer=saver)
            with pytest.raises(Boom):
                compiled.invoke(resume_start(compiled, cfg, config), config=cfg)

        clue_one = derive_clue_id("scheduler 的基本循环", DEFAULT_SOURCE)
        assert boom_launcher.dispatched == [
            derive_run_id(config.thread_id, f"worker/{clue_one}", 1)
        ]

        # 第二次：同一 identity 重跑，必须精确续跑而不是重放 seed。
        good = FakeLauncher(
            [worker_result(["每轮 tick 检查所有 line"], [])],
            default_debate(),
        )
        graph2, _deps2 = build_research(config, text_node=seed, launcher=good)
        with SqliteSaver.from_conn_string(db) as saver:
            compiled2 = graph2.compile(checkpointer=saver)
            start = resume_start(compiled2, cfg, config)
            assert start is None, "中断后的重跑必须续跑，不能重放 initial_state"
            state = compiled2.invoke(start, config=cfg)

        assert seed.calls == 1, "resume 不得重放 seed（否则会重复入板）"
        assert state["terminal"] == TERMINAL_CONVERGED
        # 续跑同 id 重派即 re-adopt（launcher 已保证幂等）。
        assert good.dispatched[0] == derive_run_id(config.thread_id, f"worker/{clue_one}", 1)

    def test_fresh_thread_starts_from_initial_state(self, tmp_path: Path) -> None:
        config = ResearchConfig(question="q", run_root=tmp_path / "run")
        seed = FakeTextNode(json.dumps(["clue one"]))
        launcher = FakeLauncher([worker_result(["f1"], [])], default_debate())
        graph, _deps = build_research(config, text_node=seed, launcher=launcher)
        cfg = {"configurable": {"thread_id": config.thread_id}, "recursion_limit": 100}
        with SqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite3")) as saver:
            compiled = graph.compile(checkpointer=saver)
            snapshot = compiled.get_state(cfg)
            assert snapshot.next == ()
            assert resume_start(compiled, cfg, config) == initial_state(
                config.research_id, config.question, config.generation
            )


class TestCli:
    def test_research_run_parses(self) -> None:
        from fleet_graph.cli import build_parser

        args = build_parser().parse_args(
            ["research", "run", "--question", "q?", "--generation", "2", "--max-clues", "5"]
        )
        assert args.question == "q?"
        assert args.generation == 2
        assert args.max_clues == 5
        assert args.checkpoint is None

    def test_research_run_default_concurrency_is_four(self) -> None:
        from fleet_graph.cli import build_parser

        args = build_parser().parse_args(["research", "run", "--question", "q?"])
        assert args.concurrency == 4

    def test_research_run_accepts_explicit_concurrency(self) -> None:
        from fleet_graph.cli import build_parser

        args = build_parser().parse_args(
            ["research", "run", "--question", "q?", "--concurrency", "8"]
        )
        assert args.concurrency == 8

    def test_question_is_required(self) -> None:
        from fleet_graph.cli import build_parser

        with pytest.raises(SystemExit):
            build_parser().parse_args(["research", "run"])
