#!/usr/bin/env python3
"""Deterministic R4 acceptance samples inside the testenv (wf-4601c8 R4).

判据锚：specs/r4-release-branch-model.md 开放点 1/2（实现方回执强制作答）与
dd-acceptance 第 13/14 项「13/14 转 PASS——13 base==派单时线分支头且 merger
推线分支、14 rebase 记录存在且 release_behind 回 0」。

**开放点 2 的作答（13/14 样本如何确定性产出）**：与 R3 同族的引擎级 fixture
（复用同一真实图路径纪律，共享 testenv 面）——本驱动在 testenv 内用仓内引擎
真件跑一条完整的 goal_line 派单轮、一条完整的 dd pipeline（configure →
implement → reviews → acceptance → human_gate → merger，llm 段全部 scripted、
脚本段全部仓内真件）、一条 gate 轮与一次进程内 resume，全程零外部网关、
零真实模型调用、零手写 record.json：

1. **线分支确定性前进**：subject bare 仓的 `release/wf-r4-sample` 在派单前被
   确定性地推进一提交（固定内容/作者/日期，幂等重算），让 configure 首步
   rebase 必然走「远端已前进 → rebased:true → 新头冻结」的全路径。
2. **派单轮（真实 goal_line 图）**：Stop Response 带 ``dd.dispatch.v1``
   （target_base=旧头 base），经真实 ``DdControlPlane.create``——record.json
   的 ``remote_ref`` 即派单线分支 ``refs/heads/release/wf-r4-sample``（13 项
   探针机械读取面），单私有审计分支 ``refs/heads/dd/<dev>`` 另立 ``audit_ref``
   字段（开放点 1 的字段级方案：sealer 发布链挂审计分支，13 项判据只读
   remote_ref，收割/追溯不受影响）。
3. **configure 首步 rebase（真件）**：fetch 后把 bootstrap 材料变基到 origin
   线分支头，`target_base_commit` 冻结为新头，configure 回执记录
   ``{requested_head, actual_head, rebased: true}``，rebase 事件进
   events.jsonl（14 项探针 grep 面）。
4. **gate 轮 + 进程内 resume（真实路径）**：``dd.gate_release.v1`` 经真实
   gate 节点六项取证签封裁决并发布 board，随后以生产 resume 同款语义重入
   同一 checkpoint 线程——BoardGate 从 board 读回 APPROVE → merger 真件
   剥离机器件（`.dev-dispatch/`、`.dd-evidence/`）并 CAS 推线分支，单落到
   complete。
5. **release_behind 回 0**：configure 同步本地线分支、merger 推送后再同步，
   state 面 `/v1/lines` 对 `wf-r4-sample` 读数 `release_behind == 0`
   （读数面计算源：state/release_position.py，本地 ref 只读）。

确定性：仓库种子与提交元数据固定 → development_id 稳定；幂等：done 标记在
则跳过；fail-closed：任何一步失败即非零退出（testenv up 随之失败）。

用法：testenv_r4_sample.py --root TEST_ROOT --bus-port PORT [--bus-url URL]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import testenv_r3_sample as r3  # noqa: E402
from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402

from fleet_graph.dd.bootstrap import digest_of  # noqa: E402
from fleet_graph.dd.control_plane import (  # noqa: E402
    DdControlPlane,
    derive_acceptance_commands,
    derive_development_id,
)
from fleet_graph.dd.lifecycle import Lifecycle  # noqa: E402
from fleet_graph.graphs.dd_actors import BoardGate  # noqa: E402
from fleet_graph.graphs.dd_gate import GraphGateNode  # noqa: E402
from fleet_graph.graphs.dd_pipeline import (  # noqa: E402
    PipelineDeps,
    StageOutcome,
    initial_state,
)
from fleet_graph.graphs.dd_runner import (  # noqa: E402
    ACCEPTANCE_RESULT,
    MERGE_RESULT,
    RESULT_FILE,
    RUN_CONFIG,
    lifecycle_gate_stage,
    stage_producing,
)
from fleet_graph.graphs.dd_scripts import (  # noqa: E402
    AcceptanceStage,
    ConfigureStage,
    MergeStage,
    WorkspaceSealer,
    write_json,
)
from fleet_graph.graphs.dd_subgraph import ControlPlaneGateway, DdSubgraph  # noqa: E402
from fleet_graph.graphs.goal_line import LineDeps, build_goal_line_graph  # noqa: E402
from fleet_graph.graphs.guards import LineBounds, LineGuards  # noqa: E402
from fleet_graph.state.run_artifacts import iso, write_json_durable  # noqa: E402

LINE = "wf-r4-sample"
DISPATCHER = LINE
RUNS_DIRNAME = "runs"
SPEC_SOURCE = Path("spec") / "r4-sample.md"
RELEASE_REF = f"refs/heads/release/{LINE}"

BASE_MICRO = '''"""Micro subject module (testenv R4 sample)."""


def render() -> str:
    return "micro_v0"
'''

V2_MICRO = '''"""Micro subject module (testenv R4 sample)."""


def render() -> str:
    return format("micro_v2")
'''

MICRO_CHECK = '''"""The frozen acceptance subject: render() must return the v2 payload."""

import sys

sys.path.insert(0, "src")

import micro

assert micro.render() == "micro_v2", micro.render()
print("micro ok")
'''

SPEC_TEXT = """# Spec: testenv micro single（R4 样本）

确定性微型单：`src/micro.py` 经一个真实新增生产调用点渲染 v2 载荷。
本 spec 是 testenv 验收样本（验收 13/14 项）的派单 spec，不是产品 spec。

## 交付物

- `src/micro.py`：render() 经 `format` 调用返回 `micro_v2`。

```dd-acceptance
python3 src/micro_check.py
```
"""


def git(repo: Path, *args: str) -> str:
    return r3.sh(["git", "-C", str(repo), *args]).stdout.strip()


def seed_repo(root: Path) -> tuple[Path, str]:
    """The micro subject repo (mkrepo layout) with a deterministically
    advanced line branch.

    The bare carries the pristine base on main (pushed once) and the line
    branch `release/wf-r4-sample` advanced exactly one deterministic commit
    past the base -- re-derived and re-asserted on every start, so a
    previously failed attempt can never shift the identity. The advance is
    what makes configure's first-step rebase take the full "remote advanced"
    path on every fresh run.
    """
    bare = root / "repos" / "r4-sample.git"
    clone = root / "repos" / "r4-sample"
    if not bare.is_dir():
        r3.sh(["git", "init", "-q", "--bare", "-b", "main", str(bare)])
    if not clone.is_dir():
        r3.sh(["git", "clone", "-q", str(bare), str(clone)])
    if not (clone / SPEC_SOURCE).is_file():
        (clone / "src").mkdir(parents=True, exist_ok=True)
        (clone / "src" / "micro.py").write_text(BASE_MICRO, encoding="utf-8")
        (clone / "src" / "micro_check.py").write_text(MICRO_CHECK, encoding="utf-8")
        (clone / SPEC_SOURCE).parent.mkdir(parents=True, exist_ok=True)
        (clone / SPEC_SOURCE).write_text(SPEC_TEXT, encoding="utf-8")
        (clone / "README.md").write_text("# r4-sample\n", encoding="utf-8")
        base = r3.commit_all(clone, "base: r4-sample micro single seed")
        git(clone, "push", "-q", "origin", f"{base}:refs/heads/main")
    base = git(clone, "rev-parse", "main")

    # The deterministic one-commit advance of the line branch (parent=base),
    # built with plumbing so the clone's own main stays at the pristine base.
    env = dict(
        os.environ,
        GIT_AUTHOR_DATE="2026-09-05T01:00:00+00:00",
        GIT_COMMITTER_DATE="2026-09-05T01:00:00+00:00",
    )
    base_tree = git(clone, "rev-parse", "main^{tree}")
    advanced = r3.sh(
        [
            "git",
            "-C",
            str(clone),
            "-c",
            "user.name=Dev Dispatch",
            "-c",
            "user.email=dev-dispatch@example.invalid",
            "commit-tree",
            base_tree,
            "-p",
            base,
            "-m",
            f"advance line branch for {LINE} (deterministic fixture)",
        ],
        env=env,
    ).stdout.strip()
    remote_head = ""
    try:
        probe = r3.sh(["git", "-C", str(clone), "ls-remote", "--refs", "origin", RELEASE_REF])
    except RuntimeError:
        probe = None
    if probe is not None:
        for line in probe.stdout.splitlines():
            if line.strip():
                remote_head = line.split("\t", 1)[0].strip()
    if remote_head != advanced:
        # Fixture reset of this driver's own scratch bare (never a product
        # path): the branch must sit exactly at the deterministic advance.
        if remote_head:
            git(clone, "push", "-q", "origin", f"--delete:{RELEASE_REF}")
        git(clone, "push", "-q", "origin", f"{advanced}:{RELEASE_REF}")
    git(clone, "update-ref", "-m", "r4 fixture sync", f"refs/heads/release/{LINE}", advanced)

    if (clone / ".dev-dispatch").is_dir() or git(clone, "status", "--porcelain"):
        shutil_reroot(clone, bare)
    return clone, base, advanced


def shutil_reroot(clone: Path, bare: Path) -> None:
    """A failed earlier attempt left commits in the clone: re-clone pristine."""
    import shutil

    shutil.rmtree(clone, ignore_errors=True)
    r3.sh(["git", "clone", "-q", str(bare), str(clone)])


class ScriptedImplementActor:
    """The implement stage's scripted seat: the one real product change.

    r3's implement actor is not reusable here: its v2 content carries a
    different module docstring, and the extra textual diff line would
    enumerate a non-load-bearing mutation target that can never land red.
    """

    def __init__(self, workspace: Path, acceptance_commands: list[list[str]]) -> None:
        self.workspace = workspace
        self.acceptance_commands = acceptance_commands

    def act(self, stage: Any, dispatch: Any) -> Any:
        (self.workspace / "src" / "micro.py").write_text(V2_MICRO, encoding="utf-8")
        return StageOutcome(
            event="success",
            receipt={
                "actor": "scripted-implement",
                "verification_record": {
                    "verification_commands": [
                        {"argv": list(argv), "exit_code": 0} for argv in self.acceptance_commands
                    ]
                },
            },
            produced=tuple(stage.produced_artifacts),
        )


def _multi_actor_r4(workspace: Path, base: str, acceptance_commands: list[list[str]]) -> Any:
    return r3._MultiStageActor(
        ScriptedImplementActor(workspace, acceptance_commands),
        {
            "continuous_review": r3.ScriptedReviewActor("APPROVE"),
            "final_review": r3.ScriptedReviewActor(
                "APPROVE",
                workspace=workspace,
                base=base,
                acceptance_commands=acceptance_commands,
            ),
        },
    )


def run_line_graph(
    root: Path,
    plane: Any,
    gate_node: GraphGateNode,
    script: list[dict[str, Any]],
    run_id: str,
) -> dict[str, Any]:
    artifacts = r3.RunArtifacts(root / RUNS_DIRNAME / LINE, run_id=run_id, folder_id=LINE)
    deps = LineDeps(
        coordinator=r3.ScriptedCoordinator(script),
        worker=r3.ScriptedWorker(),
        inbox=r3.NullInbox(),
        artifacts=artifacts,
        guards=LineGuards(bounds=LineBounds(max_rounds=6, noop_limit=3, timeout_limit=2)),
        folder_id=LINE,
        dd=DdSubgraph(ControlPlaneGateway(plane)),
        gate=gate_node,
    )
    compiled = build_goal_line_graph(deps).compile()
    return dict(
        compiled.invoke(
            {"round_no": 1},
            config={"configurable": {"thread_id": f"{LINE}:{run_id}"}, "recursion_limit": 60},
        )
    )


def run_pipeline(
    root: Path,
    plane: Any,
    board: Any,
    development_id: str,
    record: dict[str, Any],
    base: str,
    mutation_base: str,
    acceptance_commands: list[list[str]],
) -> dict[str, Any]:
    """The micro single's real pipeline, scripted at the llm seats only.

    Returns (uncompiled graph, state after the suspend at the gate) so
    main() can re-compile against a fresh checkpointer for the in-process
    resume -- the production resume (`dd run --resume`) re-enters exactly
    the same checkpoint thread the same way.
    """
    from fleet_graph.dd.capability import CapabilityLock

    workspace = Path(str(record["repo_path"]))
    state_root = root / "dd" / development_id / "state"
    run_root = root / "dd" / development_id
    lifecycle = Lifecycle.load()
    events_path = run_root / "events.jsonl"

    def persist_event(entry: dict[str, Any]) -> None:
        try:
            events_path.parent.mkdir(parents=True, exist_ok=True)
            with events_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({"at": iso(time.time()), **entry}, ensure_ascii=False) + "\n"
                )
                handle.flush()
        except OSError:
            pass

    remote_url = str(record["remote_url"])
    audit_ref = str(record.get("audit_ref") or record["remote_ref"])
    line_ref = str(record["remote_ref"])
    record_path = root / "dd" / development_id / "record.json"

    def sealer_for(stage_id: str) -> Any:
        base_sealer = WorkspaceSealer(repo=workspace, remote_url=remote_url, remote_ref=audit_ref)
        name = r3.RECEIPT_NAMES.get(stage_id)
        if name is None:
            return base_sealer
        actor_fields: tuple[str, ...] = ()
        if stage_id == "implement":
            actor_fields = ("verification_record",)
        if stage_id == "final_review":
            actor_fields = ("mutation_targets", "verified_items")
        return r3.ReceiptFileSealer(
            base_sealer,
            state_root=state_root,
            development_id=development_id,
            receipt_name=name,
            actor_fields=actor_fields,
        )

    scripts: dict[str, Any] = {
        stage_producing(lifecycle, RUN_CONFIG): ConfigureStage(
            repo=workspace,
            run_config={
                "acceptance_commands": [list(a) for a in acceptance_commands],
                "setup_commands": [],
                "acceptance_env": {},
            },
            line_ref=line_ref,
            requested_base=str(record["target_base_commit"]),
            record_path=str(record_path),
        ),
        stage_producing(lifecycle, ACCEPTANCE_RESULT): AcceptanceStage(
            repo=workspace,
            declared=[list(a) for a in acceptance_commands],
            setup=[],
            env={},
            timeout_seconds=600,
        ),
        lifecycle_gate_stage(lifecycle): BoardGate(
            board=board,
            card_entity_id=str(record.get("card_entity_id") or ""),
            development_id=development_id,
            repo=workspace,
        ),
        stage_producing(lifecycle, MERGE_RESULT): MergeStage(
            repo=workspace,
            remote_url=remote_url,
            target_ref=line_ref,
            publish=True,
        ),
    }
    deps = PipelineDeps(
        lifecycle=lifecycle,
        dispatcher=_multi_actor_r4(workspace, mutation_base, acceptance_commands),
        scripts=scripts,
        materializer=r3._StageMaterializers(
            {stage: sealer_for(stage) for stage in lifecycle.stages}
        ),
        capability=CapabilityLock.load(),
        # The run's event trail (events.jsonl), the face check 14's probe
        # greps for configure's rebase record -- same writer `run_pipeline`
        # wires in production.
        observe=persist_event,
    )
    from fleet_graph.graphs.dd_pipeline import build_dd_pipeline_graph

    graph = build_dd_pipeline_graph(deps)
    with SqliteSaver.from_conn_string(str(run_root / "checkpoint.sqlite3")) as saver:
        compiled = graph.compile(checkpointer=saver)
        state = dict(
            compiled.invoke(
                initial_state(
                    development_id=development_id,
                    stage="configure",
                    head_commit=str(record["bootstrap_commit"]),
                    artifacts={"spec": str(record["bootstrap_commit"])},
                    generation=1,
                    attempt_started_at=iso(time.time()),
                ),
                config={
                    "configurable": {"thread_id": f"{development_id}:g1"},
                    "recursion_limit": 120,
                },
            )
        )

    # The suspended-at-gate state is durably in the checkpoint; the gate
    # round APPROVEs, then main() re-enters this thread in-process.
    return graph, state


def write_result(run_root: Path, development_id: str, state: dict[str, Any]) -> None:
    """The authority result.json -- same projection `run_pipeline` writes."""
    from fleet_graph.graphs.dd_runner import awaiting_decision, gate_refusal

    result = {
        "development_id": development_id,
        "generation": 1,
        "terminal": state.get("terminal"),
        "terminal_reason": state.get("terminal_reason"),
        "terminal_code": state.get("terminal_code", ""),
        "fault": bool(state.get("fault", False)),
        "stage": state.get("stage"),
        "steps": state.get("steps", 0),
        "head_commit": state.get("head_commit"),
        "awaiting": awaiting_decision(state),
        "gate_refused": gate_refusal(state),
        "history": state.get("history", []),
        "written_at": iso(time.time()),
    }
    write_json_durable(run_root / RESULT_FILE, {**result})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--bus-port", required=True)
    parser.add_argument("--bus-url", default=None)
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    bus_url = args.bus_url or f"http://127.0.0.1:{args.bus_port}"
    marker_dir = root / "r4-sample"
    marker = marker_dir / "done.json"
    if marker.is_file():
        done = json.loads(marker.read_text())
        print(f"r4-sample=already development_id={done['development_id']}")
        return 0

    token_file = root / "secrets" / "fleet-graph.token"
    os.environ["FLEET_GRAPH_BUS_TOKEN_FILE"] = str(token_file)

    from fleet_graph.bus.board import WORK_INDEX, WORK_NOTES, Board
    from fleet_graph.bus.client import BusClient

    bus_client = BusClient(base_url=bus_url, token=token_file.read_text().strip())
    bus_client.create_channel(WORK_INDEX)
    bus_client.create_channel(WORK_NOTES)
    for kind, spec_def in r3.BOARD_PROTOCOLS.items():
        bus_client.post(
            "/v1/protocols",
            {
                "kind": kind,
                "payload_schema": spec_def["payload_schema"],
                "entity_role": spec_def["entity_role"],
                "refs_required": spec_def["refs_required"],
                "description": spec_def["description"],
            },
        )
    board = Board(bus_client)

    plane = DdControlPlane(
        root=root / "dd",
        plugin_binding=root / "config" / "plugin-binding.json",
        worktree_roots=(str(root),),
        launcher=r3._DryRunLauncher(),
        unit_probe=lambda unit: False,
        board_factory=lambda: board,
        environment={"PATH": os.environ.get("PATH", "")},
    )
    clone, base, advanced = seed_repo(root)
    spec_bytes = (clone / SPEC_SOURCE).read_bytes()
    acceptance_commands = derive_acceptance_commands(spec_bytes)
    development_id = derive_development_id(clone, digest_of(spec_bytes), base)
    print(f"r4-sample=development_id={development_id}")

    def micro_regression_probe(workspace: Any) -> set[str]:
        return r3.probe_acceptance(Path(str(workspace)), acceptance_commands)

    gate_node = GraphGateNode(plane, dd_root=root / "dd", regression_probe=micro_regression_probe)

    # -- dispatch round (real goal_line graph): target_base = the OLD head --
    dispatch_action = {
        "kind": "dd.dispatch.v1",
        "idempotency_key": "r4-sample-dispatch-g1",
        "payload": {
            "repo_path": str(clone),
            "target_base": base,
            "spec_path": str(clone / SPEC_SOURCE),
            "dispatched_by": DISPATCHER,
            "timeouts": {},
            "stage_models": {},
        },
    }
    run_line_graph(
        root,
        plane,
        gate_node,
        [
            {
                "verdict": "continue",
                "next_prompt": "implement the micro single on the line branch",
                "actions": [dispatch_action],
            },
            {"verdict": "done", "reason": "dispatch round complete"},
        ],
        run_id="r4-sample-dispatch",
    )

    record = plane.get(development_id)
    if not (root / "dd" / development_id / "record.json").is_file():
        raise RuntimeError("the dispatch round did not admit the micro single")
    if not str(record["remote_ref"]).startswith("refs/heads/release/"):
        raise RuntimeError(f"record.remote_ref {record['remote_ref']!r} is not the line branch")

    # -- micro single baseline (machine file, taken on the frozen base) ------
    # R4: the base the order is frozen at is the dispatch-time line branch
    # head (the deterministic advance); the gate checks the baseline anchor
    # against exactly that.
    workspace = Path(str(record["repo_path"]))
    baseline_failed = sorted(r3.probe_acceptance(workspace, acceptance_commands))
    write_json(
        workspace,
        ".dd-evidence/regression-baseline.json",
        {
            "failed_tests": baseline_failed,
            "passed": 0,
            "failed": len(baseline_failed),
            "skipped": 0,
            "base_commit": advanced,
        },
    )

    # -- the real pipeline run, to the gate -----------------------------------
    graph, state = run_pipeline(
        root, plane, board, development_id, record, base, advanced, acceptance_commands
    )
    run_root = root / "dd" / development_id
    # The authority result.json first: the control plane derives the gate
    # suspension (awaiting_gate) from it during rebuild_status.
    write_result(run_root, development_id, state)
    status = plane.get(development_id)
    if str(status.get("state") or "") != "awaiting_gate":
        raise RuntimeError(
            f"the micro single settled at {status.get('state')!r} instead of awaiting_gate"
        )

    # -- gate round (real goal_line graph) ------------------------------------
    gate_action = {
        "kind": "dd.gate_release.v1",
        "idempotency_key": "r4-sample-gate-g1",
        "payload": {
            "development_id": development_id,
            "verdict": "APPROVE",
            "decided_by": DISPATCHER,
            "evidence": {"basis": "graph gate node recomputes all six obligations"},
        },
    }
    run_line_graph(
        root,
        plane,
        gate_node,
        [
            {
                "verdict": "done",
                "reason": "gate round complete",
                "actions": [gate_action],
            }
        ],
        run_id="r4-sample-gate",
    )

    # -- in-process resume: production resume semantics on the same thread ---
    with SqliteSaver.from_conn_string(str(run_root / "checkpoint.sqlite3")) as saver:
        compiled = graph.compile(checkpointer=saver)
        thread_config = {
            "configurable": {"thread_id": f"{development_id}:g1"},
            "recursion_limit": 120,
        }
        final_state = dict(compiled.invoke(None, config=thread_config))
    write_result(run_root, development_id, final_state)

    # -- verify the sample is what checks 13/14 read --------------------------
    final_status = plane.get(development_id)
    if str(final_status.get("terminal") or "") != "complete":
        raise RuntimeError(
            f"the resumed single ended {final_status.get('terminal')!r} instead of complete"
        )
    frozen_record = json.loads(
        (root / "dd" / development_id / "record.json").read_text(encoding="utf-8")
    )
    if not str(frozen_record["remote_ref"]).startswith("refs/heads/release/"):
        raise RuntimeError("check 13 surface broken: remote_ref is not the line branch")
    if len(str(frozen_record["target_base_commit"])) != 40:
        raise RuntimeError("check 13 surface broken: target_base_commit is not a full commit")
    merge_result = json.loads(
        git(
            workspace,
            "show",
            f"{final_state.get('head_commit')}:.dev-dispatch/merge/result-g1.json",
        )
    )
    released = str(merge_result.get("released_commit") or "")
    if not released:
        raise RuntimeError("the merger sealed no released_commit")
    remote_head = ""
    for line in git(workspace, "ls-remote", "--refs", "origin", RELEASE_REF).splitlines():
        if line.strip():
            remote_head = line.split("\t", 1)[0].strip()
    if remote_head != released:
        raise RuntimeError(
            f"the merger did not land the release branch: {remote_head or '<absent>'} "
            f"!= released {released}"
        )
    tree = git(workspace, "ls-tree", "--name-only", "-r", released)
    for stripped in (".dev-dispatch", ".dd-evidence"):
        if any(name == stripped or name.startswith(f"{stripped}/") for name in tree.splitlines()):
            raise RuntimeError(f"machine parts leaked onto the line branch: {stripped}")
    local_head = git(workspace, "rev-parse", f"refs/heads/release/{LINE}")
    if local_head != released:
        raise RuntimeError("the dispatch-side branch view did not follow the release push")
    events_text = (run_root / "events.jsonl").read_text(encoding="utf-8")
    if "rebase" not in events_text or "release/" not in events_text:
        raise RuntimeError("the configure rebase record never reached the event trail")
    if '"rebased":true' not in events_text.replace(" ", "").replace("\n", ""):
        raise RuntimeError("the configure receipt shows no rebased:true")
    decision_file = workspace / ".dev-dispatch" / "gate" / "decision-g1.json"
    if not decision_file.is_file():
        raise RuntimeError("the gate node sealed no decision file")
    decision = json.loads(decision_file.read_text(encoding="utf-8"))
    if decision.get("decided_by") != DISPATCHER:
        raise RuntimeError(
            f"decided_by {decision.get('decided_by')!r} != dispatched_by {DISPATCHER!r}"
        )
    ledger = root / RUNS_DIRNAME / LINE / "coord" / "rounds.jsonl"
    ledger_text = ledger.read_text(encoding="utf-8")
    if "dd.dispatch.v1" not in ledger_text or "dd.gate_release.v1" not in ledger_text:
        raise RuntimeError("the Stop-Response rounds ledger lacks the actions halves")

    marker_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"development_id": development_id, "at": iso(time.time())}), encoding="utf-8"
    )
    print(f"r4-sample=ok development_id={development_id} released={released[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
