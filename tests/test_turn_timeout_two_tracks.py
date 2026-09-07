"""d10b 返工（wf-8d9737）：turn-timeout 两轨口径 —— 冻结验收面。

线侧轨：fixture 注入 AgentSessionTimeout 轮 → rounds 记录在既有六字段之上
必带 seat_session_id / turn_ordinal / session_age（MUT-1：采集停用即红）；
报表分桶键 = seat_session_id x turn_ordinal x session_age（MUT-2：回退旧三键
即红）；缺字段旧记录进「变量缺失」单列桶（MUT-3：静默丢弃即红）；真挂 /
长 turn 撞顶分类按「回执时刻 - 会话最后活动时刻」机械判定。
dd 侧轨：独立一节只读 events.jsonl 的 PROVIDER_UNAVAILABLE 族（implement
fence 内），development x re_prepare 代数 x detail 可析出端点分桶；不可析出
的如实标「不可得」（MUT-4：编造/空报非空即红）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
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
    TIMEOUT_CLASS_CEILING_HIT,
    TIMEOUT_CLASS_TRUE_HANG,
    TIMEOUT_MATRIX_FIELDS,
    TRUE_HANG_DELTA_EPSILON_SECONDS,
    WORKER_TURN_TIMEOUT_REASON,
    LineDeps,
    classify_turn_timeout,
    timeout_matrix_missing,
)
from fleet_graph.state.run_artifacts import RunArtifacts

REPO_ROOT = Path(__file__).parent.parent
REPORT_SCRIPT = REPO_ROOT / "scripts" / "turn-timeout-report.py"

RECEIPT_AT = 1_787_000_000.0
SESSION_LAST_ACTIVITY = RECEIPT_AT - 30.0
SESSION_SOURCE = {
    "seat": "opencode-glm53",
    "model": "glm-5.3",
    "turn_timeout_seconds": 3000,
    "seat_session_id": "sess-two-tracks-1",
    "turn_ordinal": 1,
    "session_age": 900.0,
    "session_last_activity_at": SESSION_LAST_ACTIVITY,
}


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


def timeout_rounds(artifacts: Any) -> list[dict[str, Any]]:
    return [r for r in artifacts.rounds if r.get("reason") == WORKER_TURN_TIMEOUT_REASON]


class FakeCoordinator:
    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def turn(self, round_no: int, coord_input: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(coord_input)
        return self.script.pop(0) if self.script else {"verdict": "done", "reason": "script end"}


class FakeWorker:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def turn(self, prompt: str, round_no: int) -> Any:
        raise self.exc


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
    worker: FakeWorker,
    *,
    turn_variables: Any = None,
) -> FakeArtifacts:
    artifacts = FakeArtifacts()
    deps = LineDeps(
        coordinator=FakeCoordinator(script),
        worker=worker,
        inbox=FakeInbox(),
        artifacts=artifacts,
        folder_id="wf-8d9737",
        worker_report_retry_limit=1,
        turn_variables=turn_variables
        if turn_variables is not None
        else lambda: dict(SESSION_SOURCE),
    )
    compiled = build_graph(deps)
    compiled.invoke(
        {"round_no": 1}, config={"configurable": {"thread_id": "t1"}, "recursion_limit": 100}
    )
    return artifacts


def build_graph(deps: LineDeps) -> Any:
    from fleet_graph.graphs.goal_line import build_goal_line_graph

    return build_goal_line_graph(deps).compile(checkpointer=InMemorySaver())


def timeout_exc(
    *,
    stdout_lines: int = 0,
    zero_output: bool = True,
) -> AgentSessionTimeout:
    exc = AgentSessionTimeout("turn exceeded 3000s")
    exc.output_evidence = {
        "stdout_lines": stdout_lines,
        "last_output_at": None,
        "zero_output": zero_output,
        "source": "subprocess_timeout",
    }
    return exc


class TestLineTrackRecord:
    def test_timeout_round_carries_the_session_identity_triple(self) -> None:
        """阳性：注入超时轮 → 记录在既有六字段之上带会话三元组（MUT-1 杀面）。"""
        script = [{"verdict": "continue", "next_prompt": "attempt the task"}]
        artifacts = run_line(script, FakeWorker(timeout_exc()))
        rounds = timeout_rounds(artifacts)
        assert rounds, "the injected timeout must be recorded"
        record = rounds[0]
        assert timeout_matrix_missing(record) == []
        assert record["seat_session_id"] == "sess-two-tracks-1"
        assert record["turn_ordinal"] == 1
        assert record["session_age"] == 900.0
        assert record["seat"] == "opencode-glm53"
        assert record["model"] == "glm-5.3"
        assert record["turn_timeout_seconds"] == 3000
        assert set(TIMEOUT_MATRIX_FIELDS) <= set(record)

    def test_collection_disabled_is_red_not_silent(self) -> None:
        """MUT-1：变量源不采集会话三元组 → 落档值退化为 None，用例红。"""
        script = [{"verdict": "continue", "next_prompt": "attempt the task"}]
        artifacts = run_line(script, FakeWorker(timeout_exc()), turn_variables=dict)
        record = timeout_rounds(artifacts)[0]
        assert record["seat_session_id"] is None
        assert record["seat_session_id"] != "sess-two-tracks-1"

    def test_record_carries_classification_facts(self) -> None:
        script = [{"verdict": "continue", "next_prompt": "attempt the task"}]
        artifacts = run_line(script, FakeWorker(timeout_exc()))
        record = timeout_rounds(artifacts)[0]
        assert isinstance(record["receipt_at"], float)
        assert record["session_last_activity_at"] == SESSION_LAST_ACTIVITY
        assert record["timeout_class"] == TIMEOUT_CLASS_TRUE_HANG


class TestClassification:
    def test_zero_output_is_a_true_hang_even_with_unusable_deltas(self) -> None:
        assert (
            classify_turn_timeout(
                zero_output=True,
                receipt_at=None,
                session_last_activity_at=None,
                turn_timeout_seconds=3000,
            )
            == TIMEOUT_CLASS_TRUE_HANG
        )

    def test_delta_within_epsilon_of_receipt_is_a_true_hang(self) -> None:
        assert (
            classify_turn_timeout(
                zero_output=False,
                receipt_at=RECEIPT_AT,
                session_last_activity_at=RECEIPT_AT - TRUE_HANG_DELTA_EPSILON_SECONDS,
                turn_timeout_seconds=3000,
            )
            == TIMEOUT_CLASS_TRUE_HANG
        )

    def test_still_producing_within_budget_is_a_ceiling_hit(self) -> None:
        assert (
            classify_turn_timeout(
                zero_output=False,
                receipt_at=RECEIPT_AT,
                session_last_activity_at=RECEIPT_AT - 30.0,
                turn_timeout_seconds=3000,
            )
            == TIMEOUT_CLASS_CEILING_HIT
        )

    def test_stale_activity_while_producing_is_honestly_unclassified(self) -> None:
        """产出过但最后活动早于预算窗 → 两类都判不上，如实 None。"""
        assert (
            classify_turn_timeout(
                zero_output=False,
                receipt_at=RECEIPT_AT,
                session_last_activity_at=RECEIPT_AT - 4000.0,
                turn_timeout_seconds=3000,
            )
            is None
        )

    def test_unresolvable_inputs_are_none_never_guessed(self) -> None:
        assert (
            classify_turn_timeout(
                zero_output=False,
                receipt_at=None,
                session_last_activity_at=None,
                turn_timeout_seconds=None,
            )
            is None
        )

    def test_graph_zero_output_timeout_classifies_true_hang(self) -> None:
        script = [{"verdict": "continue", "next_prompt": "attempt the task"}]
        artifacts = run_line(script, FakeWorker(timeout_exc(zero_output=True)))
        assert timeout_rounds(artifacts)[0]["timeout_class"] == TIMEOUT_CLASS_TRUE_HANG

    def test_graph_producing_timeout_classifies_ceiling_hit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("fleet_graph.graphs.goal_line.time.time", lambda: RECEIPT_AT)
        script = [{"verdict": "continue", "next_prompt": "attempt the task"}]
        worker = FakeWorker(timeout_exc(stdout_lines=7, zero_output=False))
        artifacts = run_line(script, worker)
        record = timeout_rounds(artifacts)[0]
        assert record["receipt_at"] == RECEIPT_AT
        assert record["timeout_class"] == TIMEOUT_CLASS_CEILING_HIT

    def test_streak_breaker_unchanged_by_classification(self) -> None:
        """分类是落档加法：连续超时的 streak breaker 去留机制一毫不动。"""
        script = [
            {"verdict": "continue", "next_prompt": f"attempt {i} at the task"} for i in range(6)
        ]
        artifacts = run_line(script, FakeWorker(timeout_exc()))
        assert artifacts.terminal is not None
        assert artifacts.terminal["terminal"] == TERMINAL_BLOCKED
        assert "timeouts" in artifacts.terminal["reason"]


class TestAdapterSessionIdentity:
    def _worker(self, tmp_path: Path, started_at: Any) -> AgentSessionWorker:
        session_root = tmp_path / "seat"
        session_dir = session_root / "sessions" / "sess-1"
        session_dir.mkdir(parents=True)
        meta: dict[str, Any] = {"session_id": "sess-1", "model": "glm-5.3"}
        if started_at is not None:
            meta["started_at"] = started_at
        (session_dir / "session.json").write_text(json.dumps(meta))
        worker = AgentSessionWorker(
            seat=AgentSessionSeat(bin_path="/bin/true", state_root=str(tmp_path / "state")),
            seat_spec=SeatSpec(agent="opencode-glm53"),
            seat_key="k",
            turn_timeout_seconds=3000,
        )
        worker._handle = SeatHandle("k", "sess-1", str(session_root))
        return worker

    def test_worker_reports_session_id_ordinal_and_age(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """真 send 超时路径：逐 turn 计数、会话 id 与最后活动时刻如实可取。"""
        import subprocess as sp

        def boom(*_args, **_kwargs):
            exc = sp.TimeoutExpired(cmd="agent-session", timeout=3060)
            exc.stdout = "partial line\n"
            raise exc

        monkeypatch.setattr("fleet_graph.executors.agent_session.subprocess.run", boom)
        worker = self._worker(tmp_path, started_at=time.time() - 120.0)
        worker._opened_at = time.time() - 120.0
        with pytest.raises(AgentSessionTimeout):
            worker.turn("first", 1)
        with pytest.raises(AgentSessionTimeout):
            worker.turn("second", 2)
        variables = worker.turn_variables()
        assert variables["seat_session_id"] == "sess-1"
        assert variables["turn_ordinal"] == 2
        assert variables["session_age"] == pytest.approx(120.0, abs=5.0)
        assert variables["session_last_activity_at"] is not None

    def test_runtime_start_stamp_parses_iso_from_session_meta(self, tmp_path: Path) -> None:
        """runtime 落了 start 时间戳 → 会话年龄从之（iso 形式同样解析）。"""
        from datetime import datetime

        stamp = "2026-09-03T16:00:00Z"
        expected = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        worker = self._worker(tmp_path, started_at=stamp)
        assert worker._meta_started_at() == pytest.approx(expected)

    def test_no_start_stamp_falls_back_to_first_open_observation(self, tmp_path: Path) -> None:
        worker = self._worker(tmp_path, started_at=None)
        worker._opened_at = time.time() - 10.0
        variables = worker.turn_variables()
        assert variables["session_age"] == pytest.approx(10.0, abs=5.0)


class TestReportLineTrack:
    def write_rounds(self, root: Path, folder: str, records: list[dict[str, Any]]) -> Path:
        line = root / folder
        line.mkdir(parents=True)
        (line / "rounds.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) + "\n"
        )
        return line

    def matrix_record(self, round_no: int, **overrides: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "round": round_no,
            "round_index": round_no,
            "verdict": "continue",
            "reason": WORKER_TURN_TIMEOUT_REASON,
            "seat": "opencode-glm53",
            "model": "glm-5.3",
            "turn_timeout_seconds": 3000,
            "seat_session_id": "sess-two-tracks-1",
            "turn_ordinal": round_no,
            "session_age": 900.0 + round_no,
            "input_bytes": 2048,
            "output_evidence": {"stdout_lines": 0, "last_output_at": None, "zero_output": True},
            "timeout_class": TIMEOUT_CLASS_TRUE_HANG,
        }
        record.update(overrides)
        return record

    def test_bucket_key_is_the_session_triple_not_the_old_three(self, tmp_path: Path) -> None:
        """MUT-2 杀面：同会话不同轮共用桶；不同会话不同桶。"""
        root = tmp_path / "runs"
        self.write_rounds(
            root,
            "wf-a",
            [
                self.matrix_record(1),
                self.matrix_record(2, timeout_class=TIMEOUT_CLASS_CEILING_HIT),
                self.matrix_record(
                    3,
                    seat_session_id="sess-other-2",
                    turn_ordinal=1,
                    session_age=5.0,
                    seat="opencode-dsv4pro",
                    model="dsv4pro",
                ),
            ],
        )
        report = report_json(str(root))
        keys = [
            (b["seat_session_id"], b["turn_ordinal"], b["session_age"]) for b in report["buckets"]
        ]
        assert keys == [
            ("sess-other-2", 1, 5.0),
            ("sess-two-tracks-1", 1, 901.0),
            ("sess-two-tracks-1", 2, 902.0),
        ]
        assert "round_index" not in report["buckets"][0]
        first = report["buckets"][1]
        assert (first["seat"], first["model"]) == ("opencode-glm53", "glm-5.3")
        assert (first["true_hangs"], first["ceiling_hits"], first["unclassified"]) == (1, 0, 0)
        second = report["buckets"][2]
        assert (second["true_hangs"], second["ceiling_hits"], second["unclassified"]) == (0, 1, 0)

    def test_legacy_record_missing_session_fields_lands_in_missing_bucket(
        self, tmp_path: Path
    ) -> None:
        """MUT-3 杀面：机制前旧记录缺新三元组 → 「变量缺失」单列桶，绝不静默丢弃。"""
        root = tmp_path / "runs"
        legacy = {
            "round": 3,
            "round_index": 3,
            "verdict": "continue",
            "reason": WORKER_TURN_TIMEOUT_REASON,
            "seat": "opencode-glm53",
            "model": "glm-5.3",
            "turn_timeout_seconds": 3000,
            "input_bytes": 2048,
            "output_evidence": {"stdout_lines": 0, "last_output_at": None, "zero_output": True},
        }
        self.write_rounds(root, "wf-old", [legacy])
        report = report_json(str(root))
        assert report["buckets"] == []
        missing = report["missing_variables"]
        assert missing["count"] == 1
        assert set(missing["by_field"]) == {"seat_session_id", "turn_ordinal", "session_age"}
        assert report["totals"]["timeout_rounds"] == 1
        assert report["totals"]["unclassified"] == 1

    def test_wiping_any_session_field_is_detected(self, tmp_path: Path) -> None:
        for field in ("seat_session_id", "turn_ordinal", "session_age"):
            root = tmp_path / field
            record = self.matrix_record(1)
            del record[field]
            self.write_rounds(root, "wf-x", [record])
            report = report_json(str(root))
            assert report["missing_variables"]["by_field"] == {field: 1}, (
                f"dropping {field} must land in the 变量缺失 bucket, not pass silently"
            )

    def test_empty_data_reports_empty_and_exits_zero(self, tmp_path: Path) -> None:
        """MUT-4 空面：无数据如实报空——两轨全零、exit 0，绝不把空报成非空。"""
        empty = tmp_path / "empty"
        empty.mkdir()
        report = report_json(str(empty))
        assert report["buckets"] == []
        assert report["totals"]["timeout_rounds"] == 0
        assert report["dd_provider_unavailable"]["buckets"] == []
        assert report["dd_provider_unavailable"]["totals"] == {
            "provider_unavailable": 0,
            "in_fence": 0,
            "out_of_fence": 0,
            "buckets": 0,
        }
        assert run_report(str(empty)).returncode == 0


class TestReportDdTrack:
    @staticmethod
    def write_events(path: Path, records: list[dict[str, Any]]) -> Path:
        path.mkdir(parents=True)
        (path / "events.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) + "\n"
        )
        return path

    @staticmethod
    def re_prepare(at: str, generation: int, development: str = "dev-fg-m5") -> dict[str, Any]:
        return {
            "at": at,
            "event": "re_prepare",
            "stage": "implement",
            "attempt": 1,
            "generation": generation,
            "development_id": development,
            "input_commit": "a" * 40,
            "cleaned_head": "b" * 40,
        }

    @staticmethod
    def empty_runs(tmp_path: Path) -> Path:
        """线侧轨空输入：dd 侧单测不落缺省 run 根（真实 run 根可能巨大）。"""
        empty = tmp_path / "empty-runs"
        empty.mkdir()
        return empty

    @staticmethod
    def failed(at: str, detail: str, **overrides: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "at": at,
            "stage": "implement",
            "event": "failed",
            "attempt": 1,
            "output_commit": "c" * 40,
            "failure_code": "PROVIDER_UNAVAILABLE",
            "detail": detail,
        }
        record.update(overrides)
        return record

    def test_bucketed_by_development_generation_and_endpoint(self, tmp_path: Path) -> None:
        """阳性：fence 内族事件按 development x 代数 x 端点分桶，计数与时刻如实。"""
        root = tmp_path / "dd"
        self.write_events(
            root,
            [
                self.re_prepare("2026-09-03T16:00:00Z", 2),
                self.failed(
                    "2026-09-03T16:10:00Z",
                    "implement run r1 did not finish: https://api.zhipu.example/v1 unreachable",
                ),
            ],
        )
        report = report_json(str(self.empty_runs(tmp_path)), "--dd-events", str(root))
        dd = report["dd_provider_unavailable"]
        assert dd["totals"] == {
            "provider_unavailable": 1,
            "in_fence": 1,
            "out_of_fence": 0,
            "buckets": 1,
        }
        bucket = dd["buckets"][0]
        assert bucket["development"] == "dev-fg-m5"
        assert bucket["generation"] == 2
        assert bucket["provider_endpoint"] == "api.zhipu.example"
        assert bucket["count"] == 1
        assert bucket["at_times"] == ["2026-09-03T16:10:00Z"]
        assert dd["re_prepare"] == [
            {
                "development": "dev-fg-m5",
                "generation": 2,
                "count": 1,
                "at_times": ["2026-09-03T16:00:00Z"],
            }
        ]

    def test_m5_shape_two_self_healed_examples(self, tmp_path: Path) -> None:
        """已知数据点形状：M5 单 e2/e3 两例，引擎 re_prepare 自愈。"""
        root = tmp_path / "dd"
        self.write_events(
            root,
            [
                self.re_prepare("2026-09-03T16:05:00Z", 2),
                self.failed("2026-09-03T16:10:00Z", "implement run r2 did not finish: timeout"),
                self.re_prepare("2026-09-03T16:50:00Z", 3),
                self.failed("2026-09-03T16:55:33Z", "implement run r3 did not finish: timeout"),
            ],
        )
        dd = report_json(str(self.empty_runs(tmp_path)), "--dd-events", str(root))[
            "dd_provider_unavailable"
        ]
        assert dd["totals"]["in_fence"] == 2
        assert [(b["generation"], b["count"]) for b in dd["buckets"]] == [(2, 1), (3, 1)]
        assert dd["buckets"][0]["at_times"] == ["2026-09-03T16:10:00Z"]
        assert dd["buckets"][1]["at_times"] == ["2026-09-03T16:55:33Z"]
        assert [(r["generation"], r["count"]) for r in dd["re_prepare"]] == [(2, 1), (3, 1)]

    def test_undeducible_fields_are_marked_unavailable_never_fabricated(
        self, tmp_path: Path
    ) -> None:
        """MUT-4 dd 面：端点/单号/代数析不出 → 如实「不可得」，严禁编造。"""
        root = tmp_path / "dd"
        self.write_events(
            root,
            [self.failed("2026-09-03T16:10:00Z", "implement run r1 did not finish: timeout")],
        )
        dd = report_json(str(self.empty_runs(tmp_path)), "--dd-events", str(root))[
            "dd_provider_unavailable"
        ]
        bucket = dd["buckets"][0]
        assert bucket["development"] == "不可得"
        assert bucket["generation"] == "不可得"
        assert bucket["provider_endpoint"] == "不可得"

    def test_own_fields_win_over_re_prepare_correlation(self, tmp_path: Path) -> None:
        root = tmp_path / "dd"
        self.write_events(
            root,
            [
                self.re_prepare("2026-09-03T15:00:00Z", 1, development="dev-fg-old"),
                self.failed(
                    "2026-09-03T16:10:00Z",
                    "implement run r1 died: https://api.one.example/x",
                    development_id="dev-fg-new",
                    generation=7,
                ),
            ],
        )
        bucket = report_json(str(self.empty_runs(tmp_path)), "--dd-events", str(root))[
            "dd_provider_unavailable"
        ]["buckets"][0]
        assert (bucket["development"], bucket["generation"]) == ("dev-fg-new", 7)

    def test_out_of_fence_family_counted_not_bucketed(self, tmp_path: Path) -> None:
        root = tmp_path / "dd"
        self.write_events(
            root,
            [
                self.failed(
                    "2026-09-03T16:10:00Z",
                    "review run r1 died: https://api.x.example/y",
                    stage="review",
                ),
            ],
        )
        dd = report_json(str(self.empty_runs(tmp_path)), "--dd-events", str(root))[
            "dd_provider_unavailable"
        ]
        assert dd["buckets"] == []
        assert dd["totals"] == {
            "provider_unavailable": 1,
            "in_fence": 0,
            "out_of_fence": 1,
            "buckets": 0,
        }

    def test_other_failure_codes_are_a_different_family(self, tmp_path: Path) -> None:
        root = tmp_path / "dd"
        self.write_events(
            root,
            [
                self.failed(
                    "2026-09-03T16:10:00Z",
                    "https://api.x.example died",
                    failure_code="INVALID_HANDOFF_SCHEMA",
                ),
            ],
        )
        dd = report_json(str(self.empty_runs(tmp_path)), "--dd-events", str(root))[
            "dd_provider_unavailable"
        ]
        assert dd["buckets"] == []
        assert dd["totals"]["provider_unavailable"] == 0

    def test_line_and_dd_tracks_report_together(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        dd = tmp_path / "dd"
        line_dir = runs / "wf-a"
        line_dir.mkdir(parents=True)
        (line_dir / "rounds.jsonl").write_text(
            json.dumps(
                {
                    "round": 1,
                    "round_index": 1,
                    "verdict": "continue",
                    "reason": WORKER_TURN_TIMEOUT_REASON,
                    "seat": "opencode-glm53",
                    "model": "glm-5.3",
                    "turn_timeout_seconds": 3000,
                    "seat_session_id": "s1",
                    "turn_ordinal": 1,
                    "session_age": 60.0,
                    "input_bytes": 10,
                    "output_evidence": {
                        "stdout_lines": 0,
                        "last_output_at": None,
                        "zero_output": True,
                    },
                    "timeout_class": TIMEOUT_CLASS_TRUE_HANG,
                }
            )
            + "\n"
        )
        self.write_events(
            dd,
            [self.failed("2026-09-03T16:10:00Z", "died: https://api.z.example/v1")],
        )
        report = report_json(str(runs), "--dd-events", str(dd))
        assert report["totals"]["timeout_rounds"] == 1
        assert report["buckets"][0]["seat_session_id"] == "s1"
        assert report["dd_provider_unavailable"]["totals"]["in_fence"] == 1
        assert report["dd_provider_unavailable"]["buckets"][0]["provider_endpoint"] == (
            "api.z.example"
        )


class TestEndToEndThroughRealArtifacts:
    def test_graph_record_lands_in_rounds_jsonl_and_reports_two_tracks(
        self, tmp_path: Path
    ) -> None:
        """真落档面闭环：图的 record 经 RunArtifacts 落 rounds.jsonl → 会话键分桶。"""
        script = [{"verdict": "continue", "next_prompt": "the real attempt"}]
        artifacts = run_line(script, FakeWorker(timeout_exc()))
        run_root = tmp_path / "wf-8d9737"
        store = RunArtifacts(run_root, run_id="run-1", folder_id="wf-8d9737")
        for record in artifacts.rounds:
            assert store.append_round(record) is True
        assert store.read_rounds() == artifacts.rounds

        report = report_json(str(tmp_path))
        assert len(report["buckets"]) == 1
        bucket = report["buckets"][0]
        assert bucket["seat_session_id"] == "sess-two-tracks-1"
        assert bucket["turn_ordinal"] == 1
        assert bucket["zero_output_timeouts"] == 1
        assert bucket["true_hangs"] == 1
        assert timeout_matrix_missing(artifacts.rounds[0]) == []
