"""缺陷⑩（wf-8d9737）：worker_turn_timeout 变量矩阵落 record + 归因报表。

阳性：fixture 注入 AgentSessionTimeout → rounds 记录含全部矩阵字段；
report 命令分桶正确。阴性：抹掉任一矩阵字段 → 报表「变量缺失」桶接住，
绝不静默丢弃；report 对空数据如实报空（弄虚作假即红）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from fleet_graph.executors.agent_session import (
    AgentSessionSeat,
    AgentSessionTimeout,
    SeatHandle,
    SeatSpec,
)
from fleet_graph.graphs.adapters import AgentSessionWorker
from fleet_graph.graphs.goal_line import (
    TERMINAL_BLOCKED,
    TIMEOUT_MATRIX_FIELDS,
    WORKER_TURN_TIMEOUT_REASON,
    LineDeps,
    build_goal_line_graph,
    timeout_matrix_missing,
)
from fleet_graph.state.run_artifacts import RunArtifacts

REPO_ROOT = Path(__file__).parent.parent
REPORT_SCRIPT = REPO_ROOT / "scripts" / "turn-timeout-report.py"

#: 一个有名字的配置：glm-5.3 座位、3000s 预算——缺陷⑩的疑变量组合。
MATRIX = {"seat": "opencode-glm53", "model": "glm-5.3", "turn_timeout_seconds": 3000}

_UNSET = object()


def run_report(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPORT_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def report_json(*args: str) -> dict[str, Any]:
    proc = run_report(*args)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def full_matrix_record(round_no: int, **overrides: Any) -> dict[str, Any]:
    """一个矩阵齐全的超时轮 record（字段集即契约）。"""
    record: dict[str, Any] = {
        "round": round_no,
        "round_index": round_no,
        "verdict": "continue",
        "reason": WORKER_TURN_TIMEOUT_REASON,
        "prompt_sha256": "a" * 64,
        "injected": True,
        "seat": "opencode-glm53",
        "model": "glm-5.3",
        "turn_timeout_seconds": 3000,
        "input_bytes": 2048,
        "output_evidence": {
            "stdout_lines": 0,
            "last_output_at": None,
            "zero_output": True,
        },
    }
    record.update(overrides)
    return record


class FakeCoordinator:
    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def turn(self, round_no: int, coord_input: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(coord_input)
        return self.script.pop(0) if self.script else {"verdict": "done", "reason": "script end"}


class FakeWorker:
    def __init__(self, *, raises: Exception | None = None, fail_times: int = 0) -> None:
        self.raises = raises
        self.fail_times = fail_times
        self.calls = 0

    def turn(self, prompt: str, round_no: int) -> Any:
        self.calls += 1
        if self.raises is not None and (self.fail_times == 0 or self.calls <= self.fail_times):
            raise self.raises
        return {
            "schema_version": "fleet-graph.worker-turn-report/v1",
            "turn_id": "t-1",
            "outcome": "completed",
            "summary": "done",
            "did": [],
            "files": [],
            "self_tests": [],
            "blocker": None,
        }


class FakeInbox:
    def drain_then_ack(self, persist: Any) -> tuple[list[Any], list[str]]:
        persist([])
        return [], []


class FakeArtifacts:
    def __init__(self) -> None:
        self.rounds: list[dict[str, Any]] = []
        self.terminal: dict[str, Any] | None = None

    def heartbeat(self, round_no: int, phase: str, *, force: bool = False) -> bool:
        return True

    def append_round(self, line: dict[str, Any]) -> bool:
        self.rounds.append(line)
        return True

    def write_worker_report(self, round_no: int, report: dict[str, Any]) -> str:
        return "worker-report.json"

    def write_terminal(self, **kwargs: Any) -> str:
        self.terminal = kwargs
        return "terminal.json"


def run_line(
    script: list[dict[str, Any]],
    *,
    worker: FakeWorker | None = None,
    turn_variables: Any = _UNSET,
) -> tuple[FakeArtifacts, LineDeps]:
    """跑一条线。turn_variables 缺省接 MATRIX；显式 None 表示没有变量源。"""
    artifacts = FakeArtifacts()
    if turn_variables is _UNSET:
        turn_variables = lambda: dict(MATRIX)  # noqa: E731
    deps = LineDeps(
        coordinator=FakeCoordinator(script),
        worker=worker or FakeWorker(),
        inbox=FakeInbox(),
        artifacts=artifacts,
        folder_id="wf-8d9737",
        worker_report_retry_limit=1,
        turn_variables=turn_variables,
    )
    compiled = build_goal_line_graph(deps).compile(checkpointer=InMemorySaver())
    compiled.invoke(
        {"round_no": 1}, config={"configurable": {"thread_id": "t1"}, "recursion_limit": 100}
    )
    return artifacts, deps


def timeout_rounds(artifacts: FakeArtifacts) -> list[dict[str, Any]]:
    return [r for r in artifacts.rounds if r.get("reason") == WORKER_TURN_TIMEOUT_REASON]


class TestTimeoutRecordMatrix:
    def test_timeout_round_carries_the_full_variable_matrix(self) -> None:
        """阳性：fixture 注入 AgentSessionTimeout → 记录含全部矩阵字段。"""
        worker = FakeWorker(raises=AgentSessionTimeout("turn exceeded 3000s"))
        script = [
            {"verdict": "continue", "next_prompt": f"attempt {i} at the task"} for i in range(6)
        ]
        artifacts, _deps = run_line(script, worker=worker)
        rounds = timeout_rounds(artifacts)
        assert rounds, "the injected timeout must be recorded as a worker_turn_timeout round"
        for record in rounds:
            assert timeout_matrix_missing(record) == []
            assert record["seat"] == "opencode-glm53"
            assert record["model"] == "glm-5.3"
            assert record["round_index"] == record["round"]
            assert record["turn_timeout_seconds"] == 3000
            assert isinstance(record["input_bytes"], int) and record["input_bytes"] > 0
            evidence = record["output_evidence"]
            assert evidence["zero_output"] is True
            assert "stdout_lines" in evidence and "last_output_at" in evidence

    def test_matrix_field_set_is_exactly_the_contract(self) -> None:
        artifacts, _deps = run_line(
            [{"verdict": "continue", "next_prompt": "only round"}],
            worker=FakeWorker(raises=AgentSessionTimeout("dead")),
        )
        record = timeout_rounds(artifacts)[0]
        assert set(TIMEOUT_MATRIX_FIELDS) <= set(record)
        assert TIMEOUT_MATRIX_FIELDS == (
            "seat",
            "model",
            "round_index",
            "turn_timeout_seconds",
            "input_bytes",
            "output_evidence",
        )

    def test_unwired_worker_still_records_the_field_set_with_none_values(self) -> None:
        """矩阵字段的存在性是契约：没有变量源时如实记 None，绝不缺字段。"""
        artifacts, _deps = run_line(
            [{"verdict": "continue", "next_prompt": "round one"}],
            worker=FakeWorker(raises=TimeoutError("worker did not answer")),
            turn_variables=None,
        )
        record = timeout_rounds(artifacts)[0]
        assert timeout_matrix_missing(record) == []
        assert record["seat"] is None and record["model"] is None
        assert record["output_evidence"]["zero_output"] is True

    def test_timeout_streak_breaker_still_decides_the_line(self) -> None:
        """落矩阵是加法：streak breaker 的去留机制一毫不动。"""
        worker = FakeWorker(raises=AgentSessionTimeout("turn exceeded 3000s"))
        script = [
            {"verdict": "continue", "next_prompt": f"attempt {i} at the task"} for i in range(6)
        ]
        artifacts, _deps = run_line(script, worker=worker)
        assert artifacts.terminal is not None
        assert artifacts.terminal["terminal"] == TERMINAL_BLOCKED
        assert "timeouts" in artifacts.terminal["reason"]


class TestMatrixReachesNextInput:
    def test_next_coordinator_input_carries_the_dead_round_matrix(self) -> None:
        """spec 第 4 条机械透传：接手模型在下一轮输入里看见上一轮死因。"""
        worker = FakeWorker(raises=AgentSessionTimeout("turn exceeded 3000s"), fail_times=1)
        script = [
            {"verdict": "continue", "next_prompt": "first attempt"},
            {"verdict": "continue", "next_prompt": "a genuinely different second attempt"},
            {"verdict": "done"},
        ]
        artifacts, deps = run_line(script, worker=worker)
        assert artifacts.terminal is not None
        assert artifacts.terminal["terminal"] == "done", "one timeout must not block the line"
        second_input = deps.coordinator.calls[1]
        status = second_input["last_turn_status"]
        assert status["kind"] == "turn_timeout"
        matrix = status["turn_variables"]
        assert timeout_matrix_missing(matrix) == []
        assert matrix["seat"] == "opencode-glm53"
        assert matrix["round_index"] == 1
        assert matrix["turn_timeout_seconds"] == 3000
        assert matrix["output_evidence"]["zero_output"] is True


class TestWiring:
    def test_build_line_wires_the_worker_as_the_matrix_source(self, tmp_path: Path) -> None:
        from fleet_graph.graphs.runner import LineConfig, build_line

        config = LineConfig(folder_id="wf-8d9737", seat="opencode-glm53", run_root=tmp_path)
        _graph, deps = build_line(config, run_id="run-1")
        assert deps.turn_variables is not None
        matrix = deps.turn_variables()
        assert matrix["seat"] == "opencode-glm53"
        assert matrix["turn_timeout_seconds"] == 3000


class TestWorkerAdapterVariables:
    def test_worker_exposes_seat_and_budget(self) -> None:
        worker = AgentSessionWorker(
            seat=AgentSessionSeat(state_root="/tmp/unused"),
            seat_spec=SeatSpec(agent="opencode-gpt-terra"),
            seat_key="k",
            turn_timeout_seconds=1234,
        )
        assert worker.turn_variables() == {
            "seat": "opencode-gpt-terra",
            "model": None,
            "turn_timeout_seconds": 1234,
        }

    def test_model_is_read_from_session_meta_when_the_runtime_records_it(
        self, tmp_path: Path
    ) -> None:
        session_root = tmp_path / "seat"
        session_dir = session_root / "sessions" / "sess-1"
        session_dir.mkdir(parents=True)
        (session_dir / "session.json").write_text(
            json.dumps({"session_id": "sess-1", "model": "glm-5.3"})
        )
        worker = AgentSessionWorker(
            seat=AgentSessionSeat(state_root=str(tmp_path / "state")),
            seat_spec=SeatSpec(agent="opencode-glm53"),
            seat_key="k",
            turn_timeout_seconds=3000,
        )
        worker._handle = SeatHandle("k", "sess-1", str(session_root))
        assert worker.turn_variables()["model"] == "glm-5.3"


class TestExecutorEvidence:
    def test_in_band_turn_timeout_carries_zero_output_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """真机形状：agent-session 回 TURN_TIMEOUT 信封 → 零产出证据随身。"""
        import subprocess as sp

        envelope = json.dumps(
            {"ok": False, "error": {"code": "TURN_TIMEOUT", "message": "exceeded 3000s"}}
        )
        completed = sp.CompletedProcess(args=[], returncode=0, stdout=envelope, stderr="")
        monkeypatch.setattr(
            "fleet_graph.executors.agent_session.subprocess.run", lambda *a, **k: completed
        )
        seat = AgentSessionSeat(bin_path="/bin/true", state_root="/tmp/unused")
        with pytest.raises(AgentSessionTimeout) as caught:
            seat.send(SeatHandle("k", "s", "/tmp/unused"), "hi")
        evidence = caught.value.output_evidence
        assert evidence["zero_output"] is True
        assert evidence["stdout_lines"] == 0
        assert evidence["source"] == "turn_timeout_envelope"

    def test_subprocess_timeout_counts_partial_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """挂死前已吐出的 stdout 行数就是产出信号，不是零产出。"""
        import subprocess as sp

        def boom(*_args, **_kwargs):
            exc = sp.TimeoutExpired(cmd="agent-session", timeout=3060)
            exc.stdout = "partial line one\npartial line two\n"
            raise exc

        monkeypatch.setattr("fleet_graph.executors.agent_session.subprocess.run", boom)
        seat = AgentSessionSeat(bin_path="/bin/true", state_root="/tmp/unused")
        with pytest.raises(AgentSessionTimeout) as caught:
            seat.send(SeatHandle("k", "s", "/tmp/unused"), "hi")
        evidence = caught.value.output_evidence
        assert evidence["stdout_lines"] == 2
        assert evidence["zero_output"] is False
        assert evidence["source"] == "subprocess_timeout"


class TestReportBuckets:
    def test_buckets_by_seat_model_round_index(self, tmp_path: Path) -> None:
        """阳性：report 按 seat x model x round_index 分桶，总数/超时/零产出齐全。"""
        root = tmp_path / "runs"
        line_a = root / "wf-aaa"
        line_a.mkdir(parents=True)
        (line_a / "rounds.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"round": 1, "verdict": "continue", "reason": ""}),
                    json.dumps(full_matrix_record(1)),
                    json.dumps({"round": 2, "verdict": "continue", "reason": ""}),
                    json.dumps(
                        full_matrix_record(
                            2,
                            output_evidence={
                                "stdout_lines": 4,
                                "last_output_at": None,
                                "zero_output": False,
                            },
                        )
                    ),
                ]
            )
            + "\n"
        )
        line_b = root / "wf-bbb"
        line_b.mkdir(parents=True)
        (line_b / "rounds.jsonl").write_text(
            json.dumps(full_matrix_record(1, seat="opencode-dsv4pro", model="dsv4pro")) + "\n"
        )

        report = report_json(str(root))
        buckets = report["buckets"]
        assert [(b["seat"], b["model"], b["round_index"]) for b in buckets] == [
            ("opencode-glm53", "glm-5.3", 1),
            ("opencode-glm53", "glm-5.3", 2),
            ("opencode-dsv4pro", "dsv4pro", 1),
        ]
        by_key = {(b["seat"], b["round_index"]): b for b in buckets}
        a1 = by_key[("opencode-glm53", 1)]
        assert (a1["total_rounds"], a1["timeout_rounds"], a1["zero_output_timeouts"]) == (2, 1, 1)
        assert a1["timeout_rate"] == 0.5
        a2 = by_key[("opencode-glm53", 2)]
        assert (a2["total_rounds"], a2["timeout_rounds"], a2["zero_output_timeouts"]) == (2, 1, 0)
        b1 = by_key[("opencode-dsv4pro", 1)]
        assert (b1["total_rounds"], b1["timeout_rounds"], b1["zero_output_timeouts"]) == (1, 1, 1)
        assert report["totals"] == {"records": 5, "timeout_rounds": 3, "zero_output_timeouts": 2}
        assert report["missing_variables"]["count"] == 0

    def test_legacy_records_missing_fields_land_in_the_missing_bucket(self, tmp_path: Path) -> None:
        """阴性：机制前旧记录缺矩阵字段 → 「变量缺失」单列桶，不静默丢弃。"""
        root = tmp_path / "runs"
        line = root / "wf-old"
        line.mkdir(parents=True)
        (line / "rounds.jsonl").write_text(
            json.dumps(
                {
                    "round": 3,
                    "verdict": "continue",
                    "reason": WORKER_TURN_TIMEOUT_REASON,
                    "prompt_sha256": "b" * 64,
                    "injected": True,
                }
            )
            + "\n"
        )
        report = report_json(str(root))
        assert report["buckets"] == []
        missing = report["missing_variables"]
        assert missing["count"] == 1
        assert set(missing["by_field"]) == set(TIMEOUT_MATRIX_FIELDS)
        assert report["totals"]["timeout_rounds"] == 1

    def test_removing_any_single_matrix_field_is_detected(self, tmp_path: Path) -> None:
        """阴性：抹掉任一矩阵字段（如 model）→ 「变量缺失」桶接住，绝不静默。"""
        for field in TIMEOUT_MATRIX_FIELDS:
            root = tmp_path / field
            line = root / "wf-x"
            line.mkdir(parents=True)
            record = full_matrix_record(1)
            del record[field]
            (line / "rounds.jsonl").write_text(json.dumps(record) + "\n")
            report = report_json(str(root))
            assert report["missing_variables"]["by_field"] == {field: 1}, (
                f"dropping {field} must land in the 变量缺失 bucket, not pass silently"
            )

    def test_empty_data_reports_empty_and_exits_zero(self, tmp_path: Path) -> None:
        """阴性：无数据时如实报空——buckets 为空、计数为零、exit 0，绝不虚构。"""
        empty = tmp_path / "empty"
        empty.mkdir()
        report = report_json(str(empty))
        assert report["buckets"] == []
        assert report["missing_variables"]["count"] == 0
        assert report["totals"] == {
            "records": 0,
            "timeout_rounds": 0,
            "zero_output_timeouts": 0,
        }
        proc = run_report(str(empty))
        assert proc.returncode == 0

    def test_missing_path_is_a_usage_error_not_fabricated_data(self, tmp_path: Path) -> None:
        proc = run_report(str(tmp_path / "does-not-exist"))
        assert proc.returncode == 2


class TestEndToEndThroughRealArtifacts:
    def test_graph_record_lands_in_rounds_jsonl_and_is_reported(self, tmp_path: Path) -> None:
        """真落档面闭环：图的 record 经 RunArtifacts 落 rounds.jsonl → report 分桶。"""
        artifacts, _deps = run_line(
            [{"verdict": "continue", "next_prompt": "the real attempt"}],
            worker=FakeWorker(raises=AgentSessionTimeout("turn exceeded 3000s")),
        )
        run_root = tmp_path / "wf-8d9737"
        store = RunArtifacts(run_root, run_id="run-1", folder_id="wf-8d9737")
        for record in artifacts.rounds:
            assert store.append_round(record) is True
        assert store.read_rounds() == artifacts.rounds

        report = report_json(str(tmp_path))
        assert len(report["buckets"]) == 1
        bucket = report["buckets"][0]
        assert (bucket["seat"], bucket["model"], bucket["round_index"]) == (
            "opencode-glm53",
            "glm-5.3",
            1,
        )
        assert bucket["zero_output_timeouts"] == 1
        assert timeout_matrix_missing(artifacts.rounds[0]) == []
