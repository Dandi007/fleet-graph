"""R3 Stop Response 派单与批 gate：actions[] 信封、gate 节点六项取证、第二投递路删除。

判据锚：specs/r3-stop-response-dispatch.md 行为契约 §1–§3 与阴性用例 1–9；
findings【六项取证的盲区】（S9/S10/S11 三条都要成为 gate 节点断言）、
【⑮ 返工契约】（gate REJECT 绑 board 裁决三非空）。

九个阴性用例与变异红靶（spec §三 成对：红锚 + 注入翻转）在本文件落位：

1.  ``test_actions_unknown_kind_fails_closed``（含坏 schema / 重放 idempotency_key）
2.  ``test_dispatch_action_drives_graph_edge``
3.  ``test_gate_release_requires_decided_by_dispatcher``
4.  ``test_gate_six_obligations_mechanical_first3``
5.  ``test_gate_missing_mutation_receipt_fail_closed``
6.  ``test_no_bypass_release_path``
7.  ``test_reject_contract_three_nonempty``
8.  元：见 test_m2_dd_gate_delivery.py 删/补对照表（净数不减）与全仓 grep 探针
    （``test_the_old_call_sites_grep_zero``）。
9.  ``test_dispatch_requires_dispatched_by``

信封与节点级正例（rounds 台账 schema、verdict 与 actions 正交、graph edge 扇出、
gateway 逐字透传与 launches 引用、seed 契约）与阴性用例同文件成对存放。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.dd.self_gate import EvidenceItem, enumerate_mutation_targets
from fleet_graph.dd.self_gate_evidence import diff_added_lines
from fleet_graph.graphs.dd_gate import (
    CODE_NOT_AWAITING_GATE,
    CODE_NOT_DISPATCHER,
    CODE_OBLIGATIONS_FAILED,
    CODE_REJECT_CONTRACT_INCOMPLETE,
    GraphGateNode,
)
from fleet_graph.graphs.dd_subgraph import ControlPlaneGateway
from fleet_graph.graphs.goal_line import (
    LineDeps,
    build_goal_line_graph,
    declared_actions,
    merge_pending_actions,
)
from fleet_graph.graphs.guards import LineGuards
from fleet_graph.graphs.stop_response import (
    KIND_DISPATCH,
    KIND_GATE_RELEASE,
    REASON_DUPLICATE_IDEMPOTENCY_KEY,
    REASON_UNKNOWN_KIND,
    STATUS_CONSUMED,
    STATUS_FAILED,
    declared_record,
    validate_actions,
    validate_dispatch_payload,
    validate_gate_payload,
)

LINE = "wf-r3"
DISPATCHER = "wf-r3"
REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- fakes


class FakeCoordinator:
    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def turn(
        self, round_no: int, coord_input: dict[str, Any], *, resume: bool = False
    ) -> dict[str, Any]:
        self.calls.append(coord_input)
        return self.script.pop(0) if self.script else {"verdict": "done", "reason": "end"}


class FakeWorker:
    def turn(self, prompt: str, round_no: int) -> dict[str, Any]:
        return {
            "schema_version": "fleet-graph.worker-turn-report/v1",
            "turn_id": f"t-{round_no}",
            "outcome": "completed",
            "summary": "ok",
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
        self.actions_records: list[dict[str, Any]] = []

    def heartbeat(self, round_no: int, phase: str, *, force: bool = False) -> bool:
        return True

    def append_round(self, line: dict[str, Any]) -> bool:
        self.rounds.append(line)
        return True

    def record_stop_response_actions(self, record: dict[str, Any]) -> bool:
        self.actions_records.append(record)
        return True

    def write_worker_report(self, round_no: int, report: dict[str, Any]) -> str:
        return "worker-report.json"

    def write_terminal(self, **kwargs: Any) -> str:
        return "terminal.json"


class FakeDdPort:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any]] = []

    def invoke(self, payload: dict[str, Any], *, config: Any = None) -> dict[str, Any]:
        self.payloads.append(payload)
        return self.answers.pop(0) if self.answers else {"dd_result": None}


def make_deps(
    script: list[dict[str, Any]],
    *,
    dd: FakeDdPort | None = None,
    gate: Any = None,
) -> LineDeps:
    return LineDeps(
        coordinator=FakeCoordinator(script),
        worker=FakeWorker(),
        inbox=FakeInbox(),
        artifacts=FakeArtifacts(),
        guards=LineGuards(),
        folder_id=LINE,
        dd=dd,
        gate=gate,
    )


def run_graph(deps: LineDeps) -> dict[str, Any]:
    from langgraph.checkpoint.memory import InMemorySaver

    compiled = build_goal_line_graph(deps).compile(checkpointer=InMemorySaver())
    return dict(
        compiled.invoke(
            {"round_no": 1},
            config={"configurable": {"thread_id": "t1"}, "recursion_limit": 60},
        )
    )


def dispatch_action(key: str = "k-dispatch") -> dict[str, Any]:
    return {
        "kind": KIND_DISPATCH,
        "idempotency_key": key,
        "payload": {
            "repo_path": "/tmp/repo",
            "spec_text": "# spec",
            "dispatched_by": DISPATCHER,
        },
    }


def gate_action(key: str = "k-gate") -> dict[str, Any]:
    return {
        "kind": KIND_GATE_RELEASE,
        "idempotency_key": key,
        "payload": {
            "development_id": "dev-fg-abc",
            "verdict": "APPROVE",
            "decided_by": DISPATCHER,
        },
    }


def passing_evidence() -> list[EvidenceItem]:
    return [
        EvidenceItem(name, name, True, "grounded")
        for name in (
            "acceptance_frozen",
            "diff_within_scope",
            "zero_test_deletion",
            "personally_rerun",
            "mutation_receipt",
            "regression",
        )
    ]


def _gate_workspace(tmp_path: Path, name: str = "gate-subject") -> Path:
    workspace = tmp_path / name
    workspace.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "-m",
            "seed",
        ],
        check=True,
    )
    return workspace


class FakeGatePlane:
    def __init__(
        self,
        *,
        dispatched_by: str = DISPATCHER,
        state: str = "awaiting_gate",
        workspace: Path | None = None,
    ) -> None:
        self.dispatched_by = dispatched_by
        self.state = state
        self.workspace = workspace
        self.resumed: list[tuple[str, str]] = []
        self.published: list[dict[str, Any]] = []

    def get(self, development_id: str) -> dict[str, Any]:
        return {
            "development_id": development_id,
            "state": self.state,
            "dispatched_by": self.dispatched_by,
            "generation": 1,
            "repo_path": str(self.workspace or "/tmp/repo"),
            "worktree_path": str(self.workspace or "/tmp/repo"),
        }

    def publish_gate_decision(
        self,
        development_id: str,
        *,
        decision: str,
        decided_by: str,
        reason: str = "",
        action_key: str = "",
    ) -> dict[str, Any]:
        self.published.append(
            {"development_id": development_id, "decision": decision, "decided_by": decided_by}
        )
        return {"development_id": development_id, "decision": decision, "message_id": "m-1"}

    def gate(
        self, development_id: str, resume: bool = False, action_key: str | None = None
    ) -> dict[str, Any]:
        self.resumed.append((development_id, action_key or ""))
        return {
            "development_id": development_id,
            "resume": {"unit": "u1", "mode": "resume", "generation": 1},
        }


# ------------------------------------------------- 阴性用例 1：fail-closed


def test_actions_unknown_kind_fails_closed() -> None:
    """未识别 kind / 坏 schema / 重放 idempotency_key → 各自记失败留痕，
    绝不静默吞掉，也绝不产生图边。变异（吞异常当成功）会让 failed 断言翻红。"""
    result = {
        "actions": [
            {"kind": "dd.warp.v1", "payload": {}, "idempotency_key": "a"},
            "not-an-action",
            {
                "kind": KIND_DISPATCH,
                "payload": {"repo_path": "/tmp/r", "spec_text": "s"},
                "idempotency_key": "",
            },
            dispatch_action("dup"),
            dispatch_action("dup"),
        ]
    }
    consumable, receipts = validate_actions(result, round_no=1)

    reasons = [r["reason"] for r in receipts]
    assert reasons.count(REASON_UNKNOWN_KIND) == 1
    assert reasons.count("malformed_action") == 2
    assert reasons.count(REASON_DUPLICATE_IDEMPOTENCY_KEY) == 1
    assert len(consumable) == 1
    assert all(r["status"] == STATUS_FAILED for r in receipts)
    # every failed receipt names its action (留痕), none is silent
    assert all("detail" in r for r in receipts)

    # The graph-level parse keeps only the first well-formed occurrence; the
    # replayed duplicate, the unknown kind and the malformed entries all fail.
    dispatches, releases, parse_receipts, consumable = declared_actions(
        result, round_no=1, folder_id=LINE
    )
    assert [a["idempotency_key"] for a in dispatches] == ["dup"]
    assert releases == []
    assert len(parse_receipts) == 4
    assert len(consumable) == 1


# ------------------------------------------------- 阴性用例 2：dispatch 图边


def test_dispatch_action_drives_graph_edge() -> None:
    """dd.dispatch.v1 消费后子图实例化 + rounds 台账回执含 development_id。
    变异（绕过 dispatch 节点直调内部函数不留回执）→ 台账判据红。"""
    dd = FakeDdPort()
    dd.answers.append(
        {
            "record": {
                "development_id": "dev-fg-1",
                "launch": {"unit": "u1", "generation": 1, "thread_id": "dev-fg-1:g1"},
            },
            "dd_result": {
                "development_id": "dev-fg-1",
                "state": "in_flight",
                "output_commit": "",
                "generation": 1,
            },
        }
    )
    deps = make_deps(
        [
            {
                "verdict": "continue",
                "next_prompt": "go",
                "actions": [dispatch_action()],
            },
            {"verdict": "done", "reason": "ok"},
        ],
        dd=dd,
    )
    artifacts = deps.artifacts
    state = run_graph(deps)

    # The fan-out instantiated exactly one subgraph call with the payload.
    assert len(dd.payloads) == 1
    assert dd.payloads[0]["line_folder"] == LINE
    assert dd.payloads[0]["intent"]["repo_path"] == "/tmp/repo"
    assert state["dd_results"]["dev-fg-1"]["development_id"] == "dev-fg-1"

    # The Stop-Response ledger: actions verbatim + the consumption receipt
    # carrying the development id and the launches reference.
    records = artifacts.actions_records
    declared = [r for r in records if "actions" in r]
    assert len(declared) == 1
    assert declared[0]["actions"] == [dispatch_action()]
    receipts = [r["action_receipts"][0] for r in records if "actions" not in r]
    assert receipts[0]["status"] == STATUS_CONSUMED
    assert receipts[0]["development_id"] == "dev-fg-1"
    assert receipts[0]["launches"]["unit"] == "u1"
    assert receipts[0]["launches"]["thread_id"] == "dev-fg-1:g1"


def test_actions_orthogonal_to_verdict_and_empty_legal(tmp_path: Path) -> None:
    """actions 与 verdict 正交：空数组合法；done + actions 照常消费。"""
    dd = FakeDdPort()
    dd.answers.append(
        {
            "record": {"development_id": "dev-fg-1", "launch": {}},
            "dd_result": {"development_id": "dev-fg-1", "state": "in_flight", "generation": 1},
        }
    )
    deps = make_deps(
        [{"verdict": "done", "reason": "stop", "actions": [gate_action()]}],
        dd=dd,
        gate=GraphGateNode(
            FakeGatePlane(workspace=_gate_workspace(tmp_path, "ortho-subject")),
            evidence=passing_evidence(),
        ),
    )
    state = run_graph(deps)
    assert state["terminal"] == "done"  # verdict decided the line's fate
    assert state["pending_actions"] == []  # and the action was still consumed
    assert deps.artifacts.actions_records[-1]["action_receipts"][0]["status"] == STATUS_CONSUMED


def test_the_ledger_schema_is_frozen() -> None:
    """rounds 台账的 record 名与 receipt 字段冻结进测试（契约 §1）。"""
    record = declared_record(
        round_no=3,
        at="2026-09-05T00:00:00Z",
        verdict="continue",
        actions=[dispatch_action()],
        receipts=[],
    )
    assert record["record"] == "stop_response"
    assert record["round"] == 3
    assert record["verdict"] == "continue"
    assert set(record) == {"record", "round", "at", "verdict", "actions", "action_receipts"}


# ------------------------------------------------- 阴性用例 9：dispatched_by 必填


def test_dispatch_requires_dispatched_by() -> None:
    """缺 dispatched_by / 空串 / 冒名他线 → action 失败留痕、零图边；
    带参 → gateway 逐字透传（record.dispatched_by == payload 逐字相等）。"""
    # missing
    result = {
        "actions": [
            {
                "kind": KIND_DISPATCH,
                "idempotency_key": "k1",
                "payload": {"repo_path": "/tmp/repo", "spec_text": "s"},
            }
        ]
    }
    _, receipts = validate_actions(result, round_no=1)
    dispatches, _, parse_receipts, _routable = declared_actions(result, round_no=1, folder_id=LINE)
    assert dispatches == []
    assert parse_receipts and "dispatched_by" in parse_receipts[0]["detail"]

    # empty
    assert "dispatched_by" in validate_dispatch_payload({"dispatched_by": ""})
    # impersonation is refused at the node: only the line itself dispatches
    dispatches, _, parse_receipts, consumable = declared_actions(
        {"actions": [dispatch_action()]}, round_no=1, folder_id="wf-other"
    )
    assert dispatches == []  # zero graph edges for the impersonating action
    assert consumable  # it was well-formed enough to be identified and named
    assert parse_receipts[0]["reason"] == "dispatched_by_required"

    # A wired consumer is still required for a valid action to route; an
    # unwired one is failed closed in the stop-response application.
    # (The graph-level checks live in test_unwired_consumer_fails_closed.)

    # The zero-graph-edge property, through the real graph: the fan-out never
    # fires, no create call, only the failed receipt in the ledger.
    dd = FakeDdPort()
    deps = make_deps(
        [
            {
                "verdict": "done",
                "reason": "ok",
                "actions": [
                    {
                        "kind": KIND_DISPATCH,
                        "idempotency_key": "k1",
                        "payload": {"repo_path": "/tmp/repo", "spec_text": "s"},
                    }
                ],
            },
        ],
        dd=dd,
    )
    run_graph(deps)
    assert dd.payloads == []  # development_create was never called
    declared = deps.artifacts.actions_records[0]
    assert declared["action_receipts"][0]["status"] == STATUS_FAILED
    assert "dispatched_by" in declared["action_receipts"][0]["detail"]


def test_gateway_passes_dispatched_by_through_verbatim() -> None:
    """gateway 调内部 create 时逐字透传 dispatched_by，launch 引用随 admit
    记录返回；空串在 gateway 处也被拒（第二道锁）。"""

    class Plane:
        def __init__(self) -> None:
            self.created: list[dict[str, Any]] = []
            self.started: list[str] = []

        def create(self, **kwargs: Any) -> dict[str, Any]:
            self.created.append(kwargs)
            return {"development_id": "dev-1", "already_admitted": False}

        def start(self, development_id: str) -> dict[str, Any]:
            self.started.append(development_id)
            return {"development_id": development_id, "started": True, "unit": "u1"}

    plane = Plane()
    gateway = ControlPlaneGateway(plane, sleeper=lambda _s: None)
    gateway.admit(
        {"repo_path": "/tmp/repo", "spec_text": "s", "dispatched_by": "wf-r3"},
        line_folder="wf-r3",
    )
    assert plane.created[0]["dispatched_by"] == "wf-r3"
    assert plane.started == ["dev-1"]

    with pytest.raises(ValueError, match="dispatched_by"):
        ControlPlaneGateway(Plane(), sleeper=lambda _s: None).admit(
            {"repo_path": "/tmp/repo", "spec_text": "s"}, line_folder="wf-r3"
        )


# ------------------------------------------------- 阴性用例 3：decided_by 身份


def test_gate_release_requires_decided_by_dispatcher() -> None:
    """以他线身份（decided_by ≠ dispatched_by）投 dd.gate_release.v1 →
    REJECT+留痕，单据 untouched。变异（去掉断言）→ 红。"""
    plane = FakeGatePlane()
    node = GraphGateNode(plane, evidence=passing_evidence())
    action = gate_action()
    action["payload"]["decided_by"] = "wf-foreign"

    receipt = node.consume(action, folder_id=LINE, round_no=1)

    assert receipt["status"] == STATUS_FAILED
    assert receipt["reason"] == CODE_NOT_DISPATCHER
    assert plane.resumed == [] and plane.published == []


# ------------------------------------------------- 阴性用例 4：前三项机械计算


def _mutation_source_lines() -> dict[str, list[tuple[int, str]]]:
    return {
        "src/fleet_graph/graphs/dd_gate.py": [(10, "        return collect_gate_evidence(")],
        "src/fleet_graph/graphs/goal_line.py": [(20, "            receipt = consume(")],
        "tests/test_r3_stop_response.py": [(5, "def test_x():")],
    }


def test_gate_six_obligations_mechanical_first3(tmp_path: Path) -> None:
    """前三项机械计算各自 RED：三方命令不一致 / 越界文件 / 测试删行。
    变异（把某项探测改成恒过）→ 对应断言红。"""
    from fleet_graph.dd.self_gate import (
        evidence_acceptance_frozen,
        evidence_diff_within_scope,
        evidence_zero_test_deletion,
    )

    # 1. 三方命令归一哈希比对：任何一方不一致 → RED。
    argv = [["bash", "-lc", "make verify"]]
    ok = evidence_acceptance_frozen(
        spec_argv=argv, record_acceptance_commands=argv, receipt_command=argv
    )
    bad = evidence_acceptance_frozen(
        spec_argv=argv,
        record_acceptance_commands=[["bash", "-lc", "true"]],
        receipt_command=argv,
    )
    assert ok.passed and not bad.passed

    # 2. diff name-status 对 spec 交付物清单：越界产品文件 → RED；机器文件豁免。
    ok = evidence_diff_within_scope(
        changed_product_paths=["src/a.py", ".dev-dispatch/spec/approved.md"],
        spec_deliverable_prefixes=["src/", "tests/"],
    )
    bad = evidence_diff_within_scope(
        changed_product_paths=["src/a.py", "deploy/evil.sh"],
        spec_deliverable_prefixes=["src/", "tests/"],
    )
    assert ok.passed and not bad.passed

    # 3. tests/ 删改探测：删除 → RED；修改不算删除。
    ok = evidence_zero_test_deletion(deleted_paths=["src/old.py"])
    bad = evidence_zero_test_deletion(deleted_paths=["tests/test_old.py"])
    assert ok.passed and not bad.passed

    # The enumeration substrate stays mechanical: the same diff yields the
    # same targets; test files are never targets.
    targets = enumerate_mutation_targets(_mutation_source_lines())
    assert all(target.file.startswith("src/") for target in targets)
    assert {target.call for target in targets} == {"collect_gate_evidence", "consume"}
    assert targets == enumerate_mutation_targets(_mutation_source_lines())

    # And the node fails the action when any computed obligation is red.
    evidence = passing_evidence()
    evidence[1] = EvidenceItem(
        "diff_within_scope", "product diff within spec surface", False, "out of scope"
    )
    plane = FakeGatePlane()
    receipt = GraphGateNode(plane, evidence=evidence).consume(
        gate_action(), folder_id=LINE, round_no=1
    )
    assert receipt["status"] == STATUS_FAILED
    assert receipt["reason"] == CODE_OBLIGATIONS_FAILED
    assert plane.resumed == []


# ------------------------------------------------- 阴性用例 5：变异回执缺省


def test_gate_missing_mutation_receipt_fail_closed(tmp_path: Path) -> None:
    """无变异回执 → 不释放。变异（默认放行）→ 红。"""

    class _RecordDd:
        def __init__(self, workspace: Path) -> None:
            self.record = {
                "development_id": "dev-fg-abc",
                "state": "awaiting_gate",
                "dispatched_by": DISPATCHER,
                "generation": 1,
                "target_base_commit": "",
                "worktree_path": str(workspace),
                "acceptance_commands": [],
            }

        def get(self, development_id: str) -> dict[str, Any]:
            return dict(self.record)

    # A real repo, no sealed final_review receipt anywhere: obligation 6 must
    # fail closed (the enumeration substrate cannot even be consulted).
    workspace = tmp_path / "ws"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "-m",
            "s",
        ],
        check=True,
    )
    plane = _RecordDd(workspace)

    assert diff_added_lines(workspace, "HEAD", "HEAD") == {}

    node = GraphGateNode(plane, dd_root=tmp_path / "dd")
    receipt = node.consume(gate_action(), folder_id=LINE, round_no=1)

    assert receipt["status"] == STATUS_FAILED
    assert receipt["reason"] == CODE_OBLIGATIONS_FAILED
    assert any(
        item["id"] == "mutation_receipt" and not item["passed"] for item in receipt["evidence"]
    )


# ------------------------------------------------- 阴性用例 6：无绕授权释放路


def test_no_bypass_release_path() -> None:
    """MCP/HTTP 双路释放合成 awaiting_gate 靶 → 稳定拒绝码（单测版）；
    grep 探针：decision_deliver dd 路径 / decision-bridge dd 消费 = 0。"""
    import fleet_graph.decision_bridge.bridge as bridge_mod
    import fleet_graph.decision_bridge.owners as owners_mod
    from fleet_graph.decision_mcp import (
        CODE_DD_NOT_DELIVERABLE_HERE,
        OUTCOME_REFUSED,
        deliver_decision,
    )

    # MCP 面：dd 目标 = 稳定拒绝码（call-point），dev-fg-* 线名 = 结构化拒绝。
    with pytest.raises(Exception, match="target_kind must be 'line'"):
        deliver_decision(
            line="",
            decision="APPROVE",
            reason="r",
            run_root=Path("/tmp"),
            lines=[],
            target_kind="dd",
            target_id="dev-fg-abc",
        )
    refused = deliver_decision(
        line="dev-fg-abc", decision="APPROVE", reason="r", run_root=Path("/tmp"), lines=[]
    )
    assert refused.status == OUTCOME_REFUSED
    assert refused.code == CODE_DD_NOT_DELIVERABLE_HERE

    # grep 探针：删除面的符号全仓 = 0。
    banned = (
        "deliver_decision_dd",
        "_deliver_dd",
        "_resolve_dd_target",
        "_wake_dispatching_line",
        "DdOwnerSource",
        "OWNER_KIND_DD",
        "record_decision_consumed",
    )
    src_root = REPO_ROOT / "src"
    hits: list[str] = []
    for path in src_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in banned:
            if name in text:
                hits.append(f"{path.relative_to(REPO_ROOT)}:{name}")
    assert hits == [], hits

    # 桥的 dd 消费删净：bridge 配置再无 dd_root，owner 面再无 dd 种类。
    assert not hasattr(bridge_mod.DecisionBridgeConfig, "dd_root") or "dd_root" not in {
        f for f in bridge_mod.DecisionBridgeConfig.__dataclass_fields__
    }
    assert not hasattr(owners_mod, "OWNER_KIND_DD")

    # The HTTP face is covered by verify-rebuild check 12 in the testenv;
    # its unit-level twin is the dd MCP tool refusing a verdict-shaped call
    # (development_gate carries no decision input by contract).


def test_the_old_call_sites_grep_zero() -> None:
    """同族枚举义务：改形后原 MCP 投递前取证旧调用点全仓 grep 为零。"""
    banned = ("deliver_self_gate_decision", "collect_gate_evidence(")
    hits: list[str] = []
    for path in (REPO_ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in banned:
            if name in text:
                # the collector's own module defines the name; call sites are
                # what must be zero outside dd_gate.py's in-node use
                if name == "collect_gate_evidence(" and path.name in {
                    "dd_gate.py",
                    "self_gate_evidence.py",
                }:
                    continue
                if name == "deliver_self_gate_decision" and path.name in {
                    "self_gate.py",
                    "self_gate_evidence.py",
                }:
                    continue
                hits.append(f"{path.relative_to(REPO_ROOT)}:{name}")
    assert hits == [], hits


# ------------------------------------------------- 阴性用例 7：REJECT 三非空


def test_reject_contract_three_nonempty(tmp_path: Path) -> None:
    """REJECT 缺 board 裁决任一非空项 → gate 拒收并留痕。"""
    from fleet_graph.graphs.stop_response import reject_board_incomplete

    board_fields = {
        "problem": "p",
        "suggested_answer": "a",
        "cost_of_no_answer": "c",
    }
    assert reject_board_incomplete({"board_decision": board_fields}) == []
    for missing in ("problem", "suggested_answer", "cost_of_no_answer"):
        broken = {k: v for k, v in board_fields.items() if k != missing}
        assert reject_board_incomplete({"board_decision": broken}) != []
        partial = dict(board_fields)
        partial[missing] = "   "
        assert reject_board_incomplete({"board_decision": partial}) == [missing]
    assert reject_board_incomplete({}) == ["problem", "suggested_answer", "cost_of_no_answer"]

    plane = FakeGatePlane()
    action = gate_action()
    action["payload"]["verdict"] = "REJECT"
    action["payload"]["board_decision"] = {"problem": "only the problem"}
    receipt = GraphGateNode(plane, evidence=passing_evidence()).consume(
        action, folder_id=LINE, round_no=1
    )
    assert receipt["status"] == STATUS_FAILED
    assert receipt["reason"] == CODE_REJECT_CONTRACT_INCOMPLETE
    assert plane.resumed == [] and plane.published == []

    # A fully-bound REJECT is consumed (the ⑮ rework dispatch carries it).
    plane2 = FakeGatePlane(workspace=_gate_workspace(tmp_path, "reject-subject"))
    full = gate_action(key="k-reject-ok")
    full["payload"]["verdict"] = "REJECT"
    full["payload"]["board_decision"] = board_fields
    ok = GraphGateNode(plane2, evidence=passing_evidence()).consume(
        full, folder_id=LINE, round_no=1
    )
    assert ok["status"] == STATUS_CONSUMED
    assert ok["decision"] == "REJECT"


# ------------------------------------------------- gate payload schema / state


def test_gate_payload_schema_fail_closed() -> None:
    assert "development_id" in validate_gate_payload({"verdict": "APPROVE", "decided_by": "w"})
    assert "verdict" in validate_gate_payload(
        {"development_id": "d", "verdict": "MAYBE", "decided_by": "w"}
    )
    assert "decided_by" in validate_gate_payload({"development_id": "d", "verdict": "APPROVE"})
    assert (
        validate_gate_payload({"development_id": "d", "verdict": "APPROVE", "decided_by": "w"})
        == ""
    )

    plane = FakeGatePlane(state="running")
    receipt = GraphGateNode(plane, evidence=passing_evidence()).consume(
        gate_action(), folder_id=LINE, round_no=1
    )
    assert receipt["status"] == STATUS_FAILED
    assert receipt["reason"] == CODE_NOT_AWAITING_GATE


# ------------------------------------------------- 元：unwired consumer / merge


def test_unwired_consumer_fails_closed_without_a_graph_edge() -> None:
    """gate 消费口未接线 → 失败留痕、零图边（绝不静默半跑）。"""
    dd = FakeDdPort()
    deps = make_deps(
        [{"verdict": "done", "reason": "ok", "actions": [gate_action()]}],
        dd=dd,
        gate=None,
    )
    state = run_graph(deps)
    assert state["terminal"] == "done"
    assert state["pending_actions"] == []
    declared = deps.artifacts.actions_records[0]
    assert len(declared["action_receipts"]) == 1
    receipt = declared["action_receipts"][0]
    assert receipt["status"] == STATUS_FAILED
    assert receipt["reason"] == "consumer_unwired"


def test_merge_pending_actions_reducer_is_order_free() -> None:
    """fan-out 并发消费：各任务只摘自己的 idempotency_key，合并与次序无关。"""
    actions = [dispatch_action("a"), gate_action("b")]
    marker_a = [{"idempotency_key": "a"}]
    marker_b = [{"idempotency_key": "b"}]
    after_set = merge_pending_actions(None, actions)
    assert after_set == actions
    assert merge_pending_actions(after_set, marker_a) == [actions[1]]
    assert merge_pending_actions(after_set, marker_b) == [actions[0]]
    assert merge_pending_actions(merge_pending_actions(after_set, marker_b), marker_a) == []
    # a coordinator turn replaces the channel wholesale
    fresh = [dispatch_action("c")]
    assert merge_pending_actions([], fresh) == fresh
