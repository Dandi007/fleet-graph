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
    CLUE_BLOCKED,
    CLUE_DONE,
    CLUE_OPEN,
    DEFAULT_SOURCE,
    SOURCE_ROLE,
    SYNTHESIS_ROLE,
    TERMINAL_CAPPED,
    TERMINAL_CONVERGED,
    TERMINAL_PARTIAL,
    ResearchBounds,
    converge,
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
    """worker.result.v1 形状的结构化结果（roles 侧 schema 为 SSoT）。"""
    return {
        "clue_id": "c-fake",
        "verdict": "found" if claims else "not_found",
        "evidences": [
            {"claim": c, "source": "wiki", "quote": c, "locator": f"fake.md:{i + 1}"}
            for i, c in enumerate(claims)
        ],
        "proposed_clues": [{"clue": t, "reason": "测试线索"} for t in proposed],
    }


def worker_result(claims: list[str], proposed: list[str]) -> dict[str, Any]:
    """agent-run 成功信封：structured_result 携带契约形状的 evidences 与子线索。"""
    return {
        "state": "succeeded",
        "exit_code": 0,
        "structured_result": worker_payload(claims, proposed),
    }


def blocked_worker_result() -> dict[str, Any]:
    """verdict=blocked 的成功信封：run 成功但取证被工具面挡住。"""
    return {
        "state": "succeeded",
        "exit_code": 0,
        "structured_result": {
            "clue_id": "c-fake",
            "verdict": "blocked",
            "evidences": [],
            "proposed_clues": [],
            "notes": "wiki mcp 不可达",
        },
    }


def synthesis_result(report: str) -> dict[str, Any]:
    """research-synth.result.v1 形状的成功信封。"""
    return {
        "state": "succeeded",
        "exit_code": 0,
        "structured_result": {
            "report_markdown": report,
            "coverage_summary": "全部 clue 有证据支撑",
            "unresolved": [],
        },
    }


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
    """worker/synthesis 的替身：按 role 回放脚本，记录派发的 run id。"""

    def __init__(
        self,
        worker_script: list[Any],
        synthesis: dict[str, Any],
        *,
        boom: bool = False,
    ) -> None:
        self.worker_script = list(worker_script)
        self.synthesis = synthesis
        self.boom = boom
        self.dispatched: list[str] = []
        self.specs: dict[str, Any] = {}
        self._roles: dict[str, str] = {}

    def launch(self, spec: Any, run_id: str) -> RunTicket:
        self._roles[run_id] = spec.role
        self.specs[run_id] = spec
        self.dispatched.append(run_id)
        return RunTicket(run_id, f"/tmp/fake/{run_id}", None)

    def wait(self, ticket: RunTicket, **kwargs: Any) -> RunStatus:
        if self.boom:
            raise Boom("killed during worker run")
        role = self._roles[ticket.run_id]
        if role == SYNTHESIS_ROLE:
            return RunStatus("succeeded", self.synthesis)
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

    def test_thread_id_is_research_id_and_generation(self, tmp_path: Path) -> None:
        config = ResearchConfig(question="q", run_root=tmp_path, generation=3)
        assert config.thread_id == f"{config.research_id}:g3"

    def test_checkpoint_defaults_to_disk_under_run_root(self, tmp_path: Path) -> None:
        config = ResearchConfig(question="q", run_root=tmp_path)
        assert config.resolved_checkpoint_path == str(tmp_path / "checkpoint.sqlite3")


class TestWorkerRunIdDerivation:
    def test_same_thread_clue_retry_derives_the_same_id(self) -> None:
        thread = "r-abcdef123456:g1"
        clue = "c-fedcba654321"
        assert worker_run_id(thread, clue, 0) == worker_run_id(thread, clue, 0)
        assert worker_run_id(thread, clue, 0) == derive_run_id(thread, f"worker/{clue}", 1)

    def test_a_retry_bumps_the_derived_attempt(self) -> None:
        thread = "r-abcdef123456:g1"
        clue = "c-fedcba654321"
        assert worker_run_id(thread, clue, 1) == derive_run_id(thread, f"worker/{clue}", 2)
        assert worker_run_id(thread, clue, 0) != worker_run_id(thread, clue, 1)


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
        launcher = FakeLauncher(
            [
                worker_result(["每轮 tick 检查所有 line"], ["tick 的唤醒源"]),
                worker_result(["systemd timer 唤醒"], []),
            ],
            synthesis_result("# 报告\n调度器按 tick 工作。"),
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

        # report.md 由 synthesis 产出（规格：报告正文落 run root 文件）。
        assert (tmp_path / "run" / "report.md").read_text(
            encoding="utf-8"
        ) == "# 报告\n调度器按 tick 工作。"
        persisted = json.loads((tmp_path / "run" / RESULT).read_text(encoding="utf-8"))
        assert persisted["terminal"] == TERMINAL_CONVERGED
        assert persisted["rounds"] == 2

        # worker run id 派生稳定：与 derive_run_id(thread, f"worker/{clue_id}", retry+1) 一致。
        thread = config.thread_id
        # seed 纯字符串回填默认源，clue id 带 source 寻址（R2）。
        clue_one = derive_clue_id("scheduler 的基本循环", DEFAULT_SOURCE)
        clue_two = derive_clue_id("tick 的唤醒源", DEFAULT_SOURCE)
        assert launcher.dispatched == [
            derive_run_id(thread, f"worker/{clue_one}", 1),
            derive_run_id(thread, f"worker/{clue_two}", 1),
            derive_run_id(thread, "synthesis", 1),
        ]

    def test_events_are_persisted_by_the_runner(self, tmp_path: Path) -> None:
        seed = FakeTextNode(json.dumps(["clue one"]))
        launcher = FakeLauncher(
            [worker_result(["f1"], [])],
            synthesis_result("report"),
        )
        run_research(
            ResearchConfig(question="q", run_root=tmp_path / "run"),
            text_node=seed,
            launcher=launcher,
        )
        events = read_jsonl(tmp_path / "run" / EVENTS)
        assert [e["event"] for e in events] == [
            "seed",
            "dispatch",
            "collect",
            "harvest",
            "synthesis",
            "finalise",
        ]


class TestCapped:
    def test_max_clues_cap_terminates_as_capped_not_converged(self, tmp_path: Path) -> None:
        seed = FakeTextNode(json.dumps(["clue one"]))
        launcher = FakeLauncher(
            [worker_result(["f1"], ["clue two"])],
            synthesis_result("report"),
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
        launcher = FakeLauncher(["fail", "fail"], synthesis_result("report"))
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
        launcher = FakeLauncher(["timeout", "timeout"], synthesis_result("report"))
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
            synthesis_result("report"),
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

    def test_worker_and_synthesis_runs_carry_input_files(self, tmp_path: Path) -> None:
        question = "fleet-graph 的调度器如何工作?"
        seed = FakeTextNode(json.dumps(["scheduler 的基本循环"]))
        launcher = FakeLauncher([worker_result(["f1"], [])], synthesis_result("report"))
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

        # synthesis：input 只携带题面 manifest（research-synth.input.v1）。
        synth_spec = launcher.specs[derive_run_id(config.thread_id, "synthesis", 1)]
        assert synth_spec.input_path
        manifest = json.loads(Path(synth_spec.input_path).read_text(encoding="utf-8"))
        assert manifest["question"] == question
        assert manifest["clue_ids"] == [clue]

    def test_each_retry_dispatch_writes_its_own_input_file(self, tmp_path: Path) -> None:
        seed = FakeTextNode(json.dumps(["clue one"]))
        launcher = FakeLauncher(["fail", worker_result(["f1"], [])], synthesis_result("report"))
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


class TestBlockedVerdict:
    def test_blocked_verdict_retries_then_blocks_instead_of_done(self, tmp_path: Path) -> None:
        # 契约 verdict=blocked（工具面不可用）不是调查完成：走 clue 失败路径
        # （retry -> blocked -> 终态 partial），不得计入 done/coverage。
        seed = FakeTextNode(json.dumps(["clue one"]))
        launcher = FakeLauncher(
            [blocked_worker_result(), blocked_worker_result()],
            synthesis_result("report"),
        )
        config = ResearchConfig(question="q", run_root=tmp_path / "run")

        result = run_research(config, text_node=seed, launcher=launcher)

        assert result["terminal"] == TERMINAL_PARTIAL
        assert not (tmp_path / "run" / "evidence.jsonl").exists()


class TestResume:
    def test_interrupted_run_resumes_exactly_and_never_replays_seed(self, tmp_path: Path) -> None:
        question = "fleet-graph 的调度器如何工作?"
        seed = FakeTextNode(json.dumps(["scheduler 的基本循环"]))
        config = ResearchConfig(question=question, run_root=tmp_path / "run")
        (tmp_path / "run").mkdir(parents=True)
        cfg = {"configurable": {"thread_id": config.thread_id}, "recursion_limit": 100}
        db = str(tmp_path / "run" / "checkpoint.sqlite3")

        # 第一次：collect 的 wait 中炸掉，留下指向 collect 的 checkpoint。
        boom_launcher = FakeLauncher([], synthesis_result("report"), boom=True)
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
            synthesis_result("# 报告\n调度器按 tick 工作。"),
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
        launcher = FakeLauncher([worker_result(["f1"], [])], synthesis_result("report"))
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

    def test_question_is_required(self) -> None:
        from fleet_graph.cli import build_parser

        with pytest.raises(SystemExit):
            build_parser().parse_args(["research", "run"])
