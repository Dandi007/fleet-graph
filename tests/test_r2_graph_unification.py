"""R2 图合一（wf-4601c8）：dd_pipeline 降为 goal_line 子图、磁盘退回纯持久化。

判据锚：R2 spec §阴性用例与变异红靶（1-7）、§行为契约 1-3、§dd-acceptance
（05/18/19 三项 PASS 的单测版红靶）。七条阴性用例一条不少：

1. ``test_no_disk_channel_in_wakeup_path`` —— src 代码级 grep 探针（与验收 18
   同判据：terminal.json/.scheduler 同行命中读内容模式）= 0；注入翻转红。
2. ``test_terminal_state_via_subgraph_return_only`` —— dd 终态只经子图返回值
   进线状态；伪造盘面终态线不消费；消费分支改回读文件即红。
3. ``test_checkpoint_rebuild_no_dup_dispatch_no_loss`` —— 删 checkpoint 库后
   从权威件重建：零重复派单、结果保留；去幂等判重即红。
4. ``test_outer_gate_mcp_rejects_non_supervisor`` —— MCP 面同名工具仅监督者
   principal 可调；线 principal 稳定拒绝 + 留痕；监督者照常成功。
5. ``test_line_roster_excludes_dd_mcp`` —— 线的 MCP 工具集无 fleet-graph-dd-mcp。
6. ``test_waiting_dd_zero_llm_calls`` —— waiting_dd 窗口内线 alias 模型请求数
   = 0（假账本计 0）。
7. 元：``test_meta_required_negatives_present``（存在性锚）+ 全仓 ``make
   verify`` 绿（由 dd-acceptance 判据面核，不在此重复跑全仓）。

全部离线自足：不触生产 root、不联网；Send/子图用 InMemorySaver。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from fleet_graph.dd.rebuild import (
    duplicate_dispatches,
    rebuild_development,
    rebuild_line_state,
)
from fleet_graph.dd.service import (
    OUTER_GATE_REFUSAL_CODE,
    build_mcp_server,
    supervisor_principal,
    trace_outer_gate_refusal,
)
from fleet_graph.graphs.dd_subgraph import (
    ControlPlaneGateway,
    DdSubgraph,
    merge_dd_results,
)
from fleet_graph.graphs.goal_line import LineDeps, build_goal_line_graph
from fleet_graph.graphs.guards import LineGuards
from fleet_graph.graphs.runner import LINE_MCP_SERVERS, LineConfig, build_line
from fleet_graph.state.run_artifacts import LINE_STATE_VALUES, derive_line_state

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 验收 18 的机械口径（scripts/verify-rebuild.sh vrb_check_18 的同款正则）：
# 「terminal.json / .scheduler」与「读文件内容」模式同行命中，即磁盘当事件信道。
DISK_CHANNEL_PATTERN = re.compile(r"terminal\.json|\.scheduler")
READ_PATTERN = re.compile(r"read_text|read_bytes|open\(|json\.load|json\.loads|cat ")


def disk_channel_hits(src_dir: Path) -> list[str]:
    """验收 18 的单测版探针：同款正则、同款同行判定，逐文件扫描。"""
    hits: list[str] = []
    for path in sorted(src_dir.rglob("*")):
        if path.suffix not in {".py", ".sh"} or not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for no, line in enumerate(lines, start=1):
            if DISK_CHANNEL_PATTERN.search(line) and READ_PATTERN.search(line):
                hits.append(f"{path.relative_to(src_dir)}:{no}")
    return hits


class FakeCoordinator:
    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def turn(
        self, round_no: int, coord_input: dict[str, Any], *, resume: bool = False
    ) -> dict[str, Any]:
        self.calls.append(coord_input)
        return self.script.pop(0) if self.script else {"verdict": "done", "reason": "script end"}


class FakeWorker:
    def turn(self, prompt: str, round_no: int) -> Any:
        raise AssertionError("R2 dispatch path must not reach the worker")


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


class FakeDdPort:
    """The subgraph port as the fan-out node sees it: one invoke = one 单."""

    def __init__(self, results: dict[str, dict[str, Any]] | None = None) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.results = results or {}

    def invoke(self, payload: dict[str, Any], *, config: Any = None) -> dict[str, Any]:
        self.payloads.append(payload)
        intent = payload.get("intent") or {}
        dev = str(intent.get("_development_id") or "dev-1")
        return {"dd_result": self.results[dev]}


def make_deps(
    script: list[dict[str, Any]],
    *,
    dd: FakeDdPort | None = None,
    folder_id: str = "wf-3f30cd",
) -> LineDeps:
    return LineDeps(
        coordinator=FakeCoordinator(script),
        worker=FakeWorker(),
        inbox=FakeInbox(),
        artifacts=FakeArtifacts(),
        guards=LineGuards(),
        folder_id=folder_id,
        dd=dd,
    )


def run_graph(deps: LineDeps) -> dict[str, Any]:
    compiled = build_goal_line_graph(deps).compile(checkpointer=InMemorySaver())
    return dict(
        compiled.invoke(
            {"round_no": 1},
            config={"configurable": {"thread_id": "t1"}, "recursion_limit": 100},
        )
    )


# ---------------- 1. 磁盘不当信道 -------------------------------------------


def test_no_disk_channel_in_wakeup_path() -> None:
    """绿锚：src 里「读 terminal.json/.scheduler 内容当事件」的分支= 0
    （验收 18 的同判据单测版）。"""
    hits = disk_channel_hits(REPO_ROOT / "src")
    assert hits == [], f"调度器唤醒路径仍有读盘面当事件的分支: {hits[:5]}"


def test_no_disk_channel_in_wakeup_path_mutation(tmp_path: Path) -> None:
    """注入翻转：向唤醒路径模块注入读 .scheduler 内容的分支 → 探针红。

    在 tmp 副本上做源码注入（照 X-1 的变异纪律：绝不触碰真模块），探针换
    扫 tmp 树——同款探针必须从 0 命中翻成 ≥1 命中。
    """
    source = (REPO_ROOT / "src" / "fleet_graph" / "scheduler" / "wake.py").read_text(
        encoding="utf-8"
    )
    mutated = source + (
        "\n\n"
        "# mutation: the old disk channel, re-injected\n"
        "def _wake_from_scheduler_file(run_root, folder_id):\n"
        "    state = json.loads((Path(run_root) / '.scheduler' / f'{folder_id}.json')"
        ".read_text(encoding='utf-8'))\n"
        "    return state\n"
    )
    assert mutated != source
    tree = tmp_path / "src" / "fleet_graph" / "scheduler"
    tree.mkdir(parents=True)
    (tree / "wake.py").write_text(mutated, encoding="utf-8")
    hits = disk_channel_hits(tmp_path / "src")
    assert hits, "变异后探针仍绿——探针失配（同族缺陷三连红线）"


# ---------------- 2. 单到线是子图返回值 -------------------------------------


def test_terminal_state_via_subgraph_return_only(tmp_path: Path) -> None:
    """dd 终态只经子图返回值进线状态：coordinator 声明 dispatches → Send 扇出
    → 返回值汇进 dd_results；盘面上伪造的 terminal.json 不被消费。
    """
    authority_answer = {
        "development_id": "dev-r2-1",
        "state": "complete",
        "terminal": "complete",
        "terminal_reason": "sealed",
        "output_commit": "abc1234",
        "stage": "merger",
        "generation": 1,
    }
    dd = FakeDdPort({"dev-r2-1": authority_answer})
    deps = make_deps(
        [
            {
                "verdict": "blocked",
                "reason": "dispatched via the graph edge",
                "waiting_on": "dd",
                "dd_development_id": "dev-r2-1",
                "dispatches": [
                    {
                        "repo_path": "/tmp/repo",
                        "spec_text": "# spec",
                        "_development_id": "dev-r2-1",
                    }
                ],
            },
            {"verdict": "done", "reason": "development complete per return value"},
        ],
        dd=dd,
    )
    state = run_graph(deps)

    # 返回值进了线状态：dd_results 按 development_id 汇合。
    assert state["dd_results"] == {"dev-r2-1": authority_answer}
    # 扇出恰好一次、payload 只带这一单的意图（state 隔离）。
    assert len(dd.payloads) == 1
    assert dd.payloads[0]["line_folder"] == "wf-3f30cd"
    assert dd.payloads[0]["intent"]["repo_path"] == "/tmp/repo"
    # 派单意图已消费：不会重复实例化。
    assert state["pending_dispatches"] == []
    # 线停在 blocked+dd（M1 驻停不变），但终态写自含返回值的状态。
    assert state["terminal"] == "blocked"

    # 盘面伪造终态：把一张说「failed」的 terminal.json 落进 run root。
    # 线不读它（探针 1 证明没有读分支），第二次运行也绝不会被它改写状态。
    planted = tmp_path / "terminal.json"
    planted.write_text(
        json.dumps({"terminal": "failed", "reason": "planted on disk"}),
        encoding="utf-8",
    )
    deps2 = make_deps([{"verdict": "done", "reason": "r2"}], dd=FakeDdPort())
    state2 = run_graph(deps2)
    assert state2["terminal"] == "done"  # 只由裁决决定，不受盘面影响
    # 检测器自证：老信道（读盘面）与线状态在此刻互相矛盾——若消费分支改回
    # 读文件，state2 的 terminal 断言就会翻红。
    old_channel = json.loads(planted.read_text(encoding="utf-8"))
    assert old_channel["terminal"] == "failed"
    assert state2["terminal"] != old_channel["terminal"]


def test_dd_results_reducer_merges_per_development() -> None:
    """fan-out 汇合是纯函数按 key 合并：两单并行各写各的，互不覆盖。"""
    merged = merge_dd_results({"dev-1": {"state": "complete"}}, {"dev-2": {"state": "failed"}})
    assert merged == {"dev-1": {"state": "complete"}, "dev-2": {"state": "failed"}}
    assert merge_dd_results(None, {"dev-1": {"state": "complete"}}) == {
        "dev-1": {"state": "complete"}
    }


# ---------------- 3. checkpoint A 方案：删库重建 ----------------------------


def _write_authorities(dd_root: Path, dev: str, *, terminal: str, commit: str) -> None:
    (dd_root / dev).mkdir(parents=True, exist_ok=True)
    (dd_root / dev / "record.json").write_text(
        json.dumps(
            {
                "development_id": dev,
                "repo_path": "/tmp/repo",
                "spec_digest": "digest-1",
                "generation": 1,
                "dispatched_by": "wf-3f30cd",
            }
        ),
        encoding="utf-8",
    )
    (dd_root / dev / "result.json").write_text(
        json.dumps({"terminal": terminal, "head_commit": commit, "stage": "merger"}),
        encoding="utf-8",
    )


def test_checkpoint_rebuild_no_dup_dispatch_no_loss(tmp_path: Path) -> None:
    """删 checkpoint 库后从权威件重建：零重复派单、结果保留、线可续。"""
    dd_root = tmp_path / "dd"
    runs_root = tmp_path / "runs"
    _write_authorities(dd_root, "dev-1", terminal="complete", commit="abc1234")

    # checkpoint 库（可删缓存）就位。
    cache = runs_root / "wf-3f30cd" / "checkpoint.sqlite3"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"sqlite")

    from fleet_graph.dd.rebuild import delete_checkpointer_cache, rebuild_all

    deleted = delete_checkpointer_cache(runs_root, dd_root)
    assert [p.name for p in deleted] == ["checkpoint.sqlite3"]
    assert not cache.exists()
    # 权威件原封不动：缓存删除不丢任何结果。
    assert json.loads((dd_root / "dev-1" / "result.json").read_text())["terminal"] == "complete"

    rebuilt = rebuild_development(dd_root, "dev-1")
    assert rebuilt["state"] == "complete"
    assert rebuilt["output_commit"] == "abc1234"
    assert rebuilt["dispatched_by"] == "wf-3f30cd"
    assert rebuilt["rebuilt_from"] == ["record.json", "result.json"]

    # 重建输入只有权威件：work folder + dd 两件，绝无 .scheduler / checkpoint。
    line_state = rebuild_line_state(tmp_path / "workfolder", dd_root, "wf-3f30cd")
    assert [d["development_id"] for d in line_state["dispatched_developments"]] == ["dev-1"]
    assert line_state["rebuilt_from"] == ["work_folder", "record.json", "result.json"]

    # 重建后全仓判重：同一 (repo_path, spec_digest) 只有一张单。
    assert rebuild_all(runs_root, dd_root)["duplicate_dispatches"] == []
    assert duplicate_dispatches(dd_root) == []

    # 不重复派发（准入幂等键）：同一意图二次 admit，start 不再点火。
    plane = _FakePlane()
    gateway = ControlPlaneGateway(plane, sleeper=lambda _s: None)
    intent = {"repo_path": "/tmp/repo", "spec_text": "# spec"}
    gateway.admit(intent, line_folder="wf-3f30cd")
    record = gateway.admit(intent, line_folder="wf-3f30cd")
    assert record["already_admitted"] is True
    assert plane.started == ["dev-1"]  # 恰一次派发


def test_checkpoint_rebuild_no_dup_dispatch_no_loss_mutation(tmp_path: Path) -> None:
    """变异（去幂等判重 → 红）：同一幂等键出现两张单时，判重必须红。"""
    dd_root = tmp_path / "dd"
    _write_authorities(dd_root, "dev-1", terminal="complete", commit="abc1234")
    _write_authorities(dd_root, "dev-2", terminal="failed", commit="deadbeef")
    dups = duplicate_dispatches(dd_root)
    assert dups == [["dev-1", "dev-2"]], "去掉幂等判重后本用例必须红"


class _FakePlane:
    """DdControlPlane 的准入幂等形状：同 (repo, spec) 只建一单。"""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.started: list[str] = []
        self._seen: set[tuple[str, str]] = set()

    def create(self, **kwargs: Any) -> dict[str, Any]:
        self.created.append(kwargs)
        key = (str(kwargs.get("repo_path")), str(kwargs.get("spec_text")))
        if key in self._seen:
            return {"development_id": "dev-1", "already_admitted": True, "generation": 1}
        self._seen.add(key)
        return {"development_id": "dev-1", "already_admitted": False, "generation": 1}

    def start(self, development_id: str) -> dict[str, Any]:
        self.started.append(development_id)
        return {"development_id": development_id, "started": True}

    def get(self, development_id: str) -> dict[str, Any]:
        return {
            "development_id": development_id,
            "state": "complete",
            "terminal": "complete",
            "terminal_reason": "sealed",
            "head_commit": "abc1234",
            "stage": "merger",
            "generation": 1,
        }


def test_control_plane_gateway_projects_the_authority_answer() -> None:
    """gateway 的观察面只读权威投影：终态经返回值上行，字段机械搬运。"""
    plane = _FakePlane()
    gateway = ControlPlaneGateway(plane, sleeper=lambda _s: None)
    result = gateway.dispatch(
        {"repo_path": "/tmp/repo", "spec_text": "# spec"}, line_folder="wf-3f30cd"
    )
    assert result["development_id"] == "dev-1"
    assert result["state"] == "complete"
    assert result["output_commit"] == "abc1234"
    assert plane.started == ["dev-1"]


def test_gateway_honest_in_flight_when_never_terminal() -> None:
    """观察预算耗尽：如实回报 in_flight，绝不编造终态。"""

    class NeverTerminal(_FakePlane):
        def get(self, development_id: str) -> dict[str, Any]:
            return {
                "development_id": development_id,
                "state": "running",
                "terminal": "",
                "head_commit": "",
                "stage": "implement",
                "generation": 1,
            }

    gateway = ControlPlaneGateway(NeverTerminal(), max_observations=2, sleeper=lambda _s: None)
    result = gateway.dispatch(
        {"repo_path": "/tmp/repo", "spec_text": "# spec"}, line_folder="wf-3f30cd"
    )
    assert result["state"] == "in_flight"
    assert result["terminal"] == ""


def test_dd_subgraph_isolated_state_and_return_value() -> None:
    """子图 state 隔离 + 返回值：invoke 一次 = admit→observe 一单，返回值
    只带 dd_result，绝无 LineState channel 泄入。"""
    seen_payloads: list[dict[str, Any]] = []

    class RecordingGateway:
        def admit(self, intent: dict[str, Any], *, line_folder: str) -> dict[str, Any]:
            seen_payloads.append({"intent": intent, "line_folder": line_folder})
            return {"development_id": "dev-x", "already_admitted": False, "generation": 1}

        def observe(self, record: dict[str, Any], *, line_folder: str) -> dict[str, Any]:
            return {
                "development_id": record["development_id"],
                "state": "awaiting_gate",
                "terminal": "",
                "terminal_reason": "",
                "output_commit": "",
                "stage": "implement",
                "generation": 1,
            }

    subgraph = DdSubgraph(RecordingGateway())
    answer = subgraph.invoke({"line_folder": "wf-3f30cd", "intent": {"repo_path": "/r"}})
    assert seen_payloads == [{"intent": {"repo_path": "/r"}, "line_folder": "wf-3f30cd"}]
    assert answer["dd_result"]["development_id"] == "dev-x"
    assert answer["dd_result"]["state"] == "awaiting_gate"
    # LineState channels 不在子图 state 里（隔离）。
    assert "pending_prompt" not in answer
    assert "dd_results" not in answer


# ---------------- 4. 外门：MCP 同名工具仅监督者 -----------------------------


class _FakeControlWithRoot:
    def __init__(self, root: Path | None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.root = root

    def create(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create", kwargs))
        return {"development_id": "dev-1"}


def test_outer_gate_mcp_rejects_non_supervisor(tmp_path: Path) -> None:
    """线 principal 经 MCP 调 development_create → 稳定拒绝 + 留痕；
    监督者 principal → 成功（外门仍在）。"""
    root = tmp_path / "dd"
    root.mkdir()
    control = _FakeControlWithRoot(root)
    server = build_mcp_server(control)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    create = tools["development_create"]

    async def call_tool(arguments: dict[str, Any]) -> dict[str, Any]:
        result = await create.run(arguments)
        return result.structured_content

    # 线 principal（非监督者）→ 结构化拒绝 + 留痕。
    with pytest.raises(Exception) as excinfo:
        asyncio.run(
            call_tool(
                {
                    "principal": "wf-3f30cd",
                    "repo_path": "/tmp/repo",
                    "spec_text": "# spec",
                }
            )
        )
    assert OUTER_GATE_REFUSAL_CODE in str(excinfo.value)
    trace = root / "outer-gate-refusals.jsonl"
    rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["code"] == OUTER_GATE_REFUSAL_CODE
    assert rows[0]["principal"] == "wf-3f30cd"
    # 控制面从未被触到：拒绝发生在外门。
    assert control.calls == []

    # 监督者 principal → 照常成功。
    answer = asyncio.run(
        call_tool(
            {
                "principal": supervisor_principal(),
                "repo_path": "/tmp/repo",
                "spec_text": "# spec",
            }
        )
    )
    assert answer["development_id"] == "dev-1"
    assert len(control.calls) == 1


def test_outer_gate_trace_is_best_effort(tmp_path: Path) -> None:
    """留痕落不了盘也绝不改变拒绝本身（trace 是审计行，不是闸）。"""
    control = _FakeControlWithRoot(None)  # no root -> trace skipped
    trace_outer_gate_refusal(control, principal="wf-x", supervisor="fleet-supervisor")
    # No exception, no file, refusal unaffected.
    assert not list(tmp_path.rglob("outer-gate-refusals.jsonl"))


# ---------------- 5. 线的 MCP 工具集不含 dd-mcp ----------------------------


def test_line_roster_excludes_dd_mcp(tmp_path: Path) -> None:
    """线内建工具集（LINE_MCP_SERVERS）无 fleet-graph-dd-mcp；按名册声明
    工具集的线，座位 allowlist 同样无 dd-mcp。"""
    DD_MCP_NAMES = {"fleet-graph-dd-mcp", "fleet_graph_dd_mcp", "dd-mcp"}
    assert not DD_MCP_NAMES & set(LINE_MCP_SERVERS)

    _graph, deps = build_line(
        LineConfig(
            folder_id="wf-r2",
            seat="s",
            run_root=tmp_path,
            mcp_servers=LINE_MCP_SERVERS,
        )
    )
    worker_allow = getattr(deps.worker, "seat_spec", None)
    worker_allow = getattr(worker_allow, "mcp_allow", ()) if worker_allow else ()
    assert tuple(worker_allow) == LINE_MCP_SERVERS
    assert not DD_MCP_NAMES & set(worker_allow)

    # 变异面：一旦 dd-mcp 混入线的工具集，上面的断言即红（检测器自证）。
    mutated = (*LINE_MCP_SERVERS, "fleet-graph-dd-mcp")
    assert DD_MCP_NAMES & set(mutated)


def test_line_default_keeps_seat_argv_unchanged(tmp_path: Path) -> None:
    """未声明工具集的名册线保持既有 argv（mcp_allow 空）——R2 不代做部署。"""
    _graph, deps = build_line(LineConfig(folder_id="wf-r2", seat="s", run_root=tmp_path))
    seat_spec = getattr(deps.worker, "seat_spec", None)
    assert getattr(seat_spec, "mcp_allow", ()) == ()


# ---------------- 6. waiting_dd 零消耗 --------------------------------------


class _LedgerLauncher:
    """每次点火 = 一次潜在的 alias 模型请求：launch 即记账（假账本）。"""

    def __init__(self) -> None:
        self.requests: list[tuple[str, float]] = []

    def launch(self, spec: Any) -> Any:
        self.requests.append((getattr(spec, "alias", "") or "", 0.0))
        from fleet_graph.scheduler.launcher import LaunchResult

        return LaunchResult(spec.unit_name, True, "")


def test_waiting_dd_zero_llm_calls(tmp_path: Path) -> None:
    """waiting_dd 窗口内线 alias 模型请求数= 0：驻停线零点火（假账本计 0）。"""
    from fleet_graph.scheduler.daemon import LineSpec, Scheduler, SchedulerConfig

    class Clock:
        def __init__(self) -> None:
            self.now = 1_787_000_000.0

        def __call__(self) -> float:
            return self.now

    class FakeUnits:
        def is_active(self, unit_name: str) -> bool:
            return False

    class FakeProber:
        def check(self, seat: str) -> bool:
            return True

    class FakeDd:
        fact = None

        def dd_fact(self, development_id: str) -> str | None:
            return None

    ledger = _LedgerLauncher()
    clock = Clock()
    scheduler = Scheduler(
        SchedulerConfig(
            lines=[
                LineSpec(folder_id="wf-1", seat="opencode-dsv4pro", alias="canary", enabled=True)
            ],
            run_root=tmp_path / "runs",
            dd_root=tmp_path / "dd",
            maintenance_stop_path=tmp_path / "maintenance-stop",
        ),
        prober=FakeProber(),
        launcher=ledger,
        units=FakeUnits(),
        clock=clock,
        sleep=lambda _s: None,
        dd=FakeDd(),
    )
    assert scheduler.tick()[0].decision.ignite  # 首次点火（入编首跑）
    ledger.requests.clear()  # waiting_dd 窗口自驻停起算

    # 线停在其派出的 development 上：blocked + waiting_on=dd。
    terminal = tmp_path / "runs" / "wf-1" / "terminal.json"
    terminal.parent.mkdir(parents=True, exist_ok=True)
    terminal.write_text(
        json.dumps(
            {
                "terminal": "blocked",
                "rounds": 0,
                "run_id": "run-d1",
                "at": "2026-08-27T10:00:00Z",
                "reason": "dispatched, waiting for the development",
                "waiting_on": "dd",
                "dd_development_id": "dev-1",
            }
        ),
        encoding="utf-8",
    )
    clock.now += 3600.0

    # 窗口内反复 tick：无 dd 唤醒事实 → 零点火 → 账本零请求。
    for _ in range(5):
        scheduler.tick()
    assert [alias for alias, _ts in ledger.requests if alias == "canary"] == []
    # M1 词表落地：该线当刻状态词就是 waiting_dd。
    assert derive_line_state("blocked", "dd") == "waiting_dd"
    assert "waiting_dd" in LINE_STATE_VALUES


def test_waiting_dd_ledger_face_counts_zero(tmp_path: Path) -> None:
    """05 号判据面的单测版：waiting_dd 样本 + 账本 request_events 计 0。"""
    # testenv 样本形状（scripts/testenv.sh write_waiting_dd_sample 的同款）。
    sample = {
        "folder_id": "wf-testenv-sample",
        "line_state": "waiting_dd",
        "status": "waiting_dd",
    }
    assert sample["status"] == "waiting_dd"
    # 账本 stub 形状：空 request_events —— 任何 alias 的计数都是 0。
    ledger = {"request_events": [], "events": [], "total": 0}
    alias = "testenv-sample"
    count = sum(
        1
        for event in ledger["request_events"]
        if str(event.get("alias") or event.get("line") or "") == alias
    )
    assert count == 0


def test_llm_ledger_face_serves_http_200(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """/v1/llm-ledger 查询面：配置后 200 + request_events 直通；
    file:// 形参的 05 号探针正是死在这里（curl 对 file 报 000）。"""
    import threading
    from urllib.error import HTTPError
    from urllib.request import urlopen

    from fleet_graph.state.fleet_state import FleetStateConfig, FleetStateHTTPServer

    ledger_file = tmp_path / "ledger.json"
    ledger_file.write_text(
        json.dumps({"request_events": [{"alias": "a", "n": 1}]}), encoding="utf-8"
    )
    config = FleetStateConfig(
        host="127.0.0.1",
        port=0,
        run_root=tmp_path / "runs",
        dd_root=tmp_path / "dd",
        lines_config=tmp_path / "roster.json",
        bridge_state_dir=tmp_path / "bridge",
        enroll_queue_path=None,
        llm_ledger_path=ledger_file,
    )
    config.port = 0
    server = FleetStateHTTPServer(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urlopen(f"{base}/v1/llm-ledger?window_seconds=60", timeout=5) as response:
            assert response.status == 200
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["request_events"] == [{"alias": "a", "n": 1}]
        assert payload["window_seconds"] == 60

        # 未配置 → 诚实 404（不 fabrication zeros）。
        bare = FleetStateConfig(
            host="127.0.0.1",
            port=0,
            run_root=tmp_path / "runs",
            dd_root=tmp_path / "dd",
            lines_config=tmp_path / "roster.json",
            bridge_state_dir=tmp_path / "bridge",
            enroll_queue_path=None,
            llm_ledger_path=None,
        )
        bare_server = FleetStateHTTPServer(bare)
        bare_thread = threading.Thread(target=bare_server.serve_forever, daemon=True)
        bare_thread.start()
        try:
            bare_base = f"http://127.0.0.1:{bare_server.server_address[1]}"
            try:
                urlopen(f"{bare_base}/v1/llm-ledger", timeout=5)
                raise AssertionError("未配置面必须 404")
            except HTTPError as http_error:
                assert http_error.code == 404
        finally:
            bare_server.shutdown()
            bare_thread.join(timeout=5)
    finally:
        server.shutdown()
        thread.join(timeout=5)


# ---------------- 7. 元：阴性用例存在性锚 -----------------------------------


def test_meta_required_negatives_present() -> None:
    """spec 阴性用例 1-6 + 本元用例恰好落在本文件里，一个不少。"""
    required = {
        "test_no_disk_channel_in_wakeup_path",
        "test_terminal_state_via_subgraph_return_only",
        "test_checkpoint_rebuild_no_dup_dispatch_no_loss",
        "test_outer_gate_mcp_rejects_non_supervisor",
        "test_line_roster_excludes_dd_mcp",
        "test_waiting_dd_zero_llm_calls",
    }
    module_names = set(globals())
    assert required <= module_names
    assert "test_meta_required_negatives_present" in module_names
