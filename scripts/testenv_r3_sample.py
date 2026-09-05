#!/usr/bin/env python3
"""Deterministic R3 acceptance samples inside the testenv (wf-4601c8 R3).

判据锚：specs/r3-stop-response-dispatch.md 开放点 1（实现方回执强制作答）与
边界「11/17 的样本必须是经图路径真实产生的派单+gate 记录」。

**开放点 1 的作答（样本如何确定性产出）**：引擎级 fixture 驱动真实图路径——
本驱动在 testenv 内用仓内引擎真件跑两条完整的 goal_line 图执行与一条完整的
dd pipeline 图执行，全程零外部网关、零真实模型调用、零手写 record.json：

1. **派单轮（真实 goal_line 图）**：scripted coordinator 的 Stop Response 带
   ``actions=[{kind: dd.dispatch.v1, ...}]``，经 coordinator_turn 解析、
   Send 扇出、dispatch 节点消费——真实 ``ControlPlaneGateway`` 直调内部
   准入函数（``DdControlPlane.create``），record.json / bootstrap / 卡片
   发布全部是引擎真件产出；消费回执由节点写进 Stop-Response rounds 台账
   （``<runs>/<line>/coord/rounds.jsonl``）。
2. **微型单的 pipeline（真实 dd pipeline 图）**：scripted agent actor 承担
   implement / continuous_review / final_review 三个 llm 段（写一个微产品
   改动、逐字跑真变异实验），script 段（configure / acceptance / human_gate /
   merger）全部是仓内真件；human_gate 在真实 bus 板上问询、无裁决 →
   图在 gate 挂起 → 单落在 awaiting_gate。
3. **gate 轮（真实 goal_line 图）**：Stop Response 带
   ``actions=[{kind: dd.gate_release.v1, ...}]``，gate 节点机械履行六项
   取证（亲跑验收抄回显、对冻结基线的回归、对终审回执的变异核验——全部
   真件计算），断言 decided_by == dispatched_by，签封 decision-g1.json、
   发布裁决、经控制面 resume——消费回执落同一台账。

确定性：仓库种子内容与提交元数据固定 → development_id（(repo, spec, base)
派生）稳定；幂等：done 标记在则跳过；fail-closed：任何一步失败即非零退出
（testenv up 随之失败，验收不让缺样本的环境变绿）。

用法：testenv_r3_sample.py --root TEST_ROOT --bus-port PORT [--bus-url URL]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402

from fleet_graph.dd.bootstrap import digest_of  # noqa: E402
from fleet_graph.dd.control_plane import (  # noqa: E402
    DdControlPlane,
    derive_acceptance_commands,
    derive_development_id,
)
from fleet_graph.dd.dispatch import (  # noqa: E402
    derive_attempt_id,
)
from fleet_graph.dd.lifecycle import Lifecycle  # noqa: E402
from fleet_graph.dd.self_gate import enumerate_mutation_targets  # noqa: E402
from fleet_graph.dd.self_gate_evidence import diff_added_lines  # noqa: E402
from fleet_graph.graphs.dd_actors import BoardGate  # noqa: E402
from fleet_graph.graphs.dd_gate import GraphGateNode  # noqa: E402
from fleet_graph.graphs.dd_pipeline import (  # noqa: E402  # noqa: E402
    Actor,
    Dispatch,
    PipelineDeps,
    Sealed,
    StageOutcome,
    build_dd_pipeline_graph,
    initial_state,
)
from fleet_graph.graphs.dd_runner import (  # noqa: E402
    ACCEPTANCE_RESULT,
    MERGE_RESULT,
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
from fleet_graph.state.run_artifacts import RunArtifacts, iso  # noqa: E402

LINE = "wf-r3-sample"
DISPATCHER = LINE
RUNS_DIRNAME = "runs"
SPEC_SOURCE = Path("spec") / "r3-sample.md"

BASE_MICRO = '''"""Micro subject module (testenv R3 sample)."""


def render() -> str:
    return "micro_v0"
'''

V2_MICRO = '''"""Micro subject module (testenv R3 sample)."""


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

SPEC_TEXT = """# Spec: testenv micro single（R3 样本）

确定性微型单：`src/micro.py` 经一个真实新增生产调用点渲染 v2 载荷。
本 spec 是 testenv 验收样本（验收 11/17 项）的派单 spec，不是产品 spec。

## 交付物

- `src/micro.py`：render() 经 `format` 调用返回 `micro_v2`。

```dd-acceptance
python3 src/micro_check.py
```
"""

RECEIPT_NAMES = {
    "implement": "implement-receipt.json",
    "continuous_review": "continuous-review-receipt.json",
    "final_review": "final-review-receipt.json",
}

#: The board protocol registry entries the sample path publishes against,
#: byte-identical to the production bus's registry (fetched read-only from
#: :7490 GET /v1/protocols at delivery time, 2026-09-05). The testenv bus
#: starts with an empty registry, so the driver registers these idempotently
#: (the same upsert semantics the deployment uses).
BOARD_PROTOCOLS: dict[str, dict[str, Any]] = {
    "work.card.v1": {
        "payload_schema": {
            "additionalProperties": False,
            "properties": {
                "assignee": {"type": "string"},
                "blocked_by": {"items": {"type": "string"}, "type": "array"},
                "definition_of_done": {"type": "string"},
                "development_id": {"type": "string"},
                "intent": {"type": "string"},
                "links": {"items": {"type": "string"}, "type": "array"},
                "priority": {"enum": ["p0", "p1", "p2"]},
                "program": {"type": "string"},
                "status": {
                    "enum": ["backlog", "ready", "doing", "blocked", "review", "done", "dropped"]
                },
                "title": {"minLength": 1, "type": "string"},
                "work_folder_id": {"type": "string"},
            },
            "required": ["title", "status", "intent"],
            "type": "object",
        },
        "entity_role": "root",
        "refs_required": False,
        "description": (
            "Work board card: coordination state for one deliverable unit; "
            "deep state lives in the referenced work folder"
        ),
    },
    "work.decision.v1": {
        "payload_schema": {
            "additionalProperties": False,
            "properties": {
                "card_entity_id": {"type": "string"},
                "decided_by": {"type": "string"},
                "decision": {"type": "string"},
                "question": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["card_entity_id", "question", "decision", "decided_by"],
            "type": "object",
        },
        "entity_role": "leaf",
        "refs_required": True,
        "description": (
            "Human ruling on a question raised on a work board card; agents "
            "must never fabricate these"
        ),
    },
    "work.note.v1": {
        "payload_schema": {
            "additionalProperties": False,
            "properties": {
                "card_entity_id": {"type": "string"},
                "note": {"minLength": 1, "type": "string"},
                "note_type": {"enum": ["progress", "finding", "question", "handoff", "evidence"]},
            },
            "required": ["card_entity_id", "note"],
            "type": "object",
        },
        "entity_role": "leaf",
        "refs_required": True,
        "description": (
            "Note attached to a work board card (progress/finding/question/handoff/evidence)"
        ),
    },
}


def sh(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False, **kwargs)
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(argv)}\n"
            f"stdout: {proc.stdout[-800:]}\nstderr: {proc.stderr[-800:]}"
        )
    return proc


def git(repo: Path, *args: str) -> str:
    return sh(["git", "-C", str(repo), *args]).stdout.strip()


def commit_all(repo: Path, message: str) -> str:
    env = dict(
        os.environ,
        GIT_AUTHOR_DATE="2026-09-05T00:00:00+00:00",
        GIT_COMMITTER_DATE="2026-09-05T00:00:00+00:00",
    )
    git(repo, "add", "-A")
    sh(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Dev Dispatch",
            "-c",
            "user.email=dev-dispatch@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            message,
        ],
        env=env,
    )
    return git(repo, "rev-parse", "HEAD")


class ScriptedCoordinator:
    """The engine-level fixture's coordinator seam: two scripted Stop Responses."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)

    def turn(
        self, round_no: int, coord_input: dict[str, Any], *, resume: bool = False
    ) -> dict[str, Any]:
        if not self.script:
            return {"verdict": "done", "reason": "script end"}
        return self.script.pop(0)


class ScriptedWorker:
    """A worker seat that reports a valid v1 turn report without a model."""

    def turn(self, prompt: str, round_no: int) -> dict[str, Any]:
        return {
            "schema_version": "fleet-graph.worker-turn-report/v1",
            "turn_id": f"r3-sample-turn-{round_no}",
            "outcome": "completed",
            "summary": "micro single scripted implement turn",
            "did": ["wrote the scripted product change"],
            "files": [],
            "self_tests": [],
            "blocker": None,
        }


class NullInbox:
    def drain_then_ack(self, persist: Any) -> tuple[list[Any], list[str]]:
        persist([])
        return [], []


class ScriptedImplementActor:
    """The implement stage's scripted seat: one real product change."""

    def __init__(self, workspace: Path, acceptance_commands: list[list[str]]) -> None:
        self.workspace = workspace
        self.acceptance_commands = acceptance_commands

    def act(self, stage: Any, dispatch: Dispatch) -> StageOutcome:
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


class ScriptedReviewActor:
    """A review stage's scripted seat; the final review runs the real
    mutation experiment (enumerate mechanically, delete each target in a
    throwaway copy, require the frozen acceptance red)."""

    def __init__(
        self,
        verdict: str,
        *,
        workspace: Path | None = None,
        base: str = "",
        acceptance_commands: list[list[str]] | None = None,
    ) -> None:
        self.verdict = verdict
        self.workspace = workspace
        self.base = base
        self.acceptance_commands = acceptance_commands or []

    def _mutation_evidence(self) -> dict[str, Any]:
        assert self.workspace is not None
        head = git(self.workspace, "rev-parse", "HEAD")
        targets = enumerate_mutation_targets(diff_added_lines(self.workspace, self.base, head))
        rows: list[dict[str, Any]] = []
        for target in targets:
            red = self._target_is_red(target)
            rows.append({"file": target.file, "line": target.line, "call": target.call, "red": red})
        all_red = bool(rows) and all(row["red"] for row in rows)
        return {
            "mutation_targets": rows,
            "verified_items": [
                {"item": "mutation_targets_match_mechanical_enumeration", "ok": True},
                {"item": "every_target_landed_red", "ok": all_red},
            ],
        }

    def _target_is_red(self, target: Any) -> bool:
        assert self.workspace is not None
        copy = Path(tempfile.mkdtemp(prefix="r3-mutation-"))
        try:
            shutil.copytree(self.workspace, copy / "subject", symlinks=True)
            subject = copy / "subject"
            rel = subject / target.file
            lines = rel.read_text(encoding="utf-8").splitlines(keepends=True)
            del lines[target.line - 1]
            rel.write_text("".join(lines), encoding="utf-8")
            red = False
            for argv in self.acceptance_commands:
                proc = subprocess.run(
                    list(argv), cwd=str(subject), capture_output=True, text=True, check=False
                )
                if proc.returncode != 0:
                    red = True
            return red
        finally:
            shutil.rmtree(copy, ignore_errors=True)

    def act(self, stage: Any, dispatch: Dispatch) -> StageOutcome:
        extra: dict[str, Any] = {}
        if stage.id == "final_review":
            extra = self._mutation_evidence()
        return StageOutcome(
            event=self.verdict,
            receipt={"verdict": self.verdict, **extra},
            produced=tuple(stage.produced_artifacts),
        )


class ReceiptFileSealer:
    """Commits like the workspace sealer and writes the stage's receipt file
    under the state root -- the same sealing surface the plugin sealer
    provides in production, minus the plugin transport."""

    def __init__(
        self,
        sealer: WorkspaceSealer,
        *,
        state_root: Path,
        development_id: str,
        receipt_name: str,
        actor_fields: tuple[str, ...] = (),
    ) -> None:
        self.sealer = sealer
        self.state_root = state_root
        self.development_id = development_id
        self.receipt_name = receipt_name
        self.actor_fields = actor_fields

    def materialize(self, stage: Any, dispatch: Dispatch, outcome: StageOutcome) -> Sealed:
        sealed = self.sealer.materialize(stage, dispatch, outcome)
        attempt_id = derive_attempt_id(
            self.development_id, int(dispatch.get("generation", 1)), int(dispatch.get("attempt", 1))
        )
        receipt: dict[str, Any] = dict(sealed.receipt or {})
        for field in self.actor_fields:
            if isinstance(outcome.receipt, dict) and field in outcome.receipt:
                receipt[field] = outcome.receipt[field]
        write_json(
            Path(self.state_root),
            f"receipts/{attempt_id}/{self.receipt_name}",
            receipt,
        )
        return Sealed(commit=sealed.commit, receipt=receipt, produced=sealed.produced)


def probe_acceptance(workspace: Path, acceptance_commands: list[list[str]]) -> set[str]:
    """The regression probe seam at micro scale: run the frozen acceptance
    argv verbatim and name each failing command."""
    failed: set[str] = set()
    for index, argv in enumerate(acceptance_commands):
        proc = subprocess.run(
            list(argv), cwd=str(workspace), capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            failed.add(f"acceptance:{index}")
    return failed


def seed_repo(root: Path) -> tuple[Path, str]:
    """The micro subject repo: bare origin + working clone (mkrepo layout).

    Deterministic and self-healing: the bare carries the pristine base commit
    (pushed once); every driver start re-clones from it, so a previously
    failed attempt (whose bootstrap commits poisoned the clone) can never
    shift the derived base or the development identity.
    """
    bare = root / "repos" / "r3-sample.git"
    clone = root / "repos" / "r3-sample"
    if not bare.is_dir():
        sh(["git", "init", "-q", "--bare", "-b", "main", str(bare)])
    if not clone.is_dir():
        sh(["git", "clone", "-q", str(bare), str(clone)])
    if not (clone / SPEC_SOURCE).is_file():
        (clone / "src").mkdir(parents=True, exist_ok=True)
        (clone / "src" / "micro.py").write_text(BASE_MICRO, encoding="utf-8")
        (clone / "src" / "micro_check.py").write_text(MICRO_CHECK, encoding="utf-8")
        (clone / SPEC_SOURCE).parent.mkdir(parents=True, exist_ok=True)
        (clone / SPEC_SOURCE).write_text(SPEC_TEXT, encoding="utf-8")
        (clone / "README.md").write_text("# r3-sample\n", encoding="utf-8")
        commit_all(clone, "base: r3-sample micro single seed")
        git(clone, "push", "-q", "origin", "HEAD:main")
    if (clone / ".dev-dispatch").is_dir() or git(clone, "status", "--porcelain"):
        # A failed earlier attempt left bootstrap/pipe commits in the clone:
        # re-clone from the pristine base.
        shutil.rmtree(clone, ignore_errors=True)
        sh(["git", "clone", "-q", str(bare), str(clone)])
    return clone, git(clone, "rev-parse", "HEAD")


def run_line_graph(
    root: Path,
    plane: Any,
    gate_node: GraphGateNode,
    script: list[dict[str, Any]],
    run_id: str,
) -> dict[str, Any]:
    artifacts = RunArtifacts(root / RUNS_DIRNAME / LINE, run_id=run_id, folder_id=LINE)
    deps = LineDeps(
        coordinator=ScriptedCoordinator(script),
        worker=ScriptedWorker(),
        inbox=NullInbox(),
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


def run_pipeline_to_gate(
    root: Path,
    plane: Any,
    board: Any,
    development_id: str,
    record: dict[str, Any],
    base: str,
    acceptance_commands: list[list[str]],
) -> dict[str, Any]:
    """The micro single's real pipeline run, scripted at the llm seats only."""
    from fleet_graph.dd.capability import CapabilityLock

    workspace = Path(str(record["repo_path"]))
    state_root = root / "dd" / development_id / "state"
    run_root = root / "dd" / development_id
    lifecycle = Lifecycle.load()
    remote_url = str(record["remote_url"])
    # R4：record.remote_ref 是线分支（refs/heads/release/<line>），单私有审计
    # 分支在 audit_ref；链条连续性（sealer 发布）挂在审计分支上，合并在合并段
    # 指向线分支。旧 record（无 audit_ref）回退 remote_ref——同一语义。
    audit_ref = str(record.get("audit_ref") or record["remote_ref"])
    line_ref = str(record["remote_ref"])

    def sealer_for(stage_id: str) -> Any:
        base_sealer = WorkspaceSealer(repo=workspace, remote_url=remote_url, remote_ref=audit_ref)
        name = RECEIPT_NAMES.get(stage_id)
        if name is None:
            return base_sealer
        actor_fields: tuple[str, ...] = ()
        if stage_id == "implement":
            actor_fields = ("verification_record",)
        if stage_id == "final_review":
            actor_fields = ("mutation_targets", "verified_items")
        return ReceiptFileSealer(
            base_sealer,
            state_root=state_root,
            development_id=development_id,
            receipt_name=name,
            actor_fields=actor_fields,
        )

    scripts: dict[str, Actor] = {
        stage_producing(lifecycle, RUN_CONFIG): ConfigureStage(
            repo=workspace,
            run_config={
                "acceptance_commands": [list(a) for a in acceptance_commands],
                "setup_commands": [],
                "acceptance_env": {},
            },
            # R4 首步 rebase 三件套：线分支、派单请求头（准入冻结 base）、
            # 准入记录（rebase 后新头冻结回写）。
            line_ref=line_ref,
            requested_base=str(record["target_base_commit"]),
            record_path=str(root / "dd" / development_id / "record.json"),
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
            repo=workspace, remote_url=remote_url, target_ref=line_ref, publish=False
        ),
    }
    deps = PipelineDeps(
        lifecycle=lifecycle,
        dispatcher=_multi_actor(workspace, base, acceptance_commands),
        scripts=scripts,
        materializer=_StageMaterializers({stage: sealer_for(stage) for stage in lifecycle.stages}),
        capability=CapabilityLock.load(),
    )
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

    # The authority result.json the control plane rebuilds status from --
    # the same projection `run_pipeline` writes (same fields, same file).
    from fleet_graph.graphs.dd_runner import RESULT_FILE, awaiting_decision, gate_refusal

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
    write_json(run_root, RESULT_FILE, result)
    return state


class _MultiStageActor:
    """Routes the scripted seat per llm stage id (implement / reviews)."""

    def __init__(self, implement: ScriptedImplementActor, reviews: dict[str, ScriptedReviewActor]):
        self.implement = implement
        self.reviews = reviews

    def act(self, stage: Any, dispatch: Dispatch) -> StageOutcome:
        if stage.id in self.reviews:
            return self.reviews[stage.id].act(stage, dispatch)
        return self.implement.act(stage, dispatch)


def _multi_actor(workspace: Path, base: str, acceptance_commands: list[list[str]]):
    return _MultiStageActor(
        ScriptedImplementActor(workspace, acceptance_commands),
        {
            "continuous_review": ScriptedReviewActor("APPROVE"),
            "final_review": ScriptedReviewActor(
                "APPROVE",
                workspace=workspace,
                base=base,
                acceptance_commands=acceptance_commands,
            ),
        },
    )


class _StageMaterializers:
    def __init__(self, by_stage: dict[str, Any]) -> None:
        self.by_stage = by_stage

    def materialize(self, stage: Any, dispatch: Dispatch, outcome: StageOutcome) -> Sealed:
        materializer = self.by_stage.get(stage.id)
        if materializer is None:
            raise RuntimeError(f"no materializer for stage {stage.id!r}")
        return materializer.materialize(stage, dispatch, outcome)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--bus-port", required=True)
    parser.add_argument("--bus-url", default=None)
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    bus_url = args.bus_url or f"http://127.0.0.1:{args.bus_port}"
    marker_dir = root / "r3-sample"
    marker = marker_dir / "done.json"
    if marker.is_file():
        done = json.loads(marker.read_text())
        print(f"r3-sample=already development_id={done['development_id']}")
        return 0

    token_file = root / "secrets" / "fleet-graph.token"
    os.environ["FLEET_GRAPH_BUS_TOKEN_FILE"] = str(token_file)

    from fleet_graph.bus.board import WORK_INDEX, WORK_NOTES, Board
    from fleet_graph.bus.client import BusClient

    bus_client = BusClient(base_url=bus_url, token=token_file.read_text().strip())
    # agent-bus publishes only into existing channels and only against
    # registered protocols: ensure both up front (idempotent upserts),
    # byte-identical to the production registry, exactly as the deployment
    # provisions them.
    bus_client.create_channel(WORK_INDEX)
    bus_client.create_channel(WORK_NOTES)
    for kind, spec_def in BOARD_PROTOCOLS.items():
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
        launcher=_DryRunLauncher(),
        unit_probe=lambda unit: False,
        board_factory=lambda: board,
        environment={"PATH": os.environ.get("PATH", "")},
    )
    clone, base = seed_repo(root)
    spec_bytes = (clone / SPEC_SOURCE).read_bytes()
    acceptance_commands = derive_acceptance_commands(spec_bytes)
    development_id = derive_development_id(clone, digest_of(spec_bytes), base)
    print(f"r3-sample=development_id={development_id}")

    # The regression probe seam at micro scale: run the frozen acceptance argv
    # verbatim and name each failing command (a real suite run, scaled to the
    # micro subject -- no pytest dependency, no criterion lowered).
    def micro_regression_probe(workspace: Any) -> set[str]:
        return probe_acceptance(Path(str(workspace)), acceptance_commands)

    gate_node = GraphGateNode(plane, dd_root=root / "dd", regression_probe=micro_regression_probe)

    # -- dispatch round (real goal_line graph) -------------------------------
    dispatch_action = {
        "kind": "dd.dispatch.v1",
        "idempotency_key": "r3-sample-dispatch-g1",
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
                "next_prompt": "implement the micro single",
                "actions": [dispatch_action],
            },
            {"verdict": "done", "reason": "dispatch round complete"},
        ],
        run_id="r3-sample-dispatch",
    )

    record = plane.get(development_id)
    if not (root / "dd" / development_id / "record.json").is_file():
        raise RuntimeError("the dispatch round did not admit the micro single")

    # -- micro single baseline (machine file, taken on the frozen base) ------
    workspace = Path(str(record["repo_path"]))
    baseline_failed = sorted(probe_acceptance(workspace, acceptance_commands))
    write_json(
        workspace,
        ".dd-evidence/regression-baseline.json",
        {
            "failed_tests": baseline_failed,
            "passed": 0,
            "failed": len(baseline_failed),
            "skipped": 0,
            "base_commit": base,
        },
    )

    # -- the micro single's real pipeline run, to the gate --------------------
    run_pipeline_to_gate(root, plane, board, development_id, record, base, acceptance_commands)
    status = plane.get(development_id)
    if str(status.get("state") or "") != "awaiting_gate":
        raise RuntimeError(
            f"the micro single settled at {status.get('state')!r} instead of awaiting_gate"
        )

    # -- gate round (real goal_line graph) ------------------------------------
    gate_action = {
        "kind": "dd.gate_release.v1",
        "idempotency_key": "r3-sample-gate-g1",
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
        run_id="r3-sample-gate",
    )

    # -- verify the sample is what the acceptance criteria read ---------------
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
    print(f"r3-sample=ok development_id={development_id}")
    return 0


class _DryRunLauncher:
    """A launch seam that records without starting: the testenv has no
    systemd user manager, so the control plane's launch surface runs in
    dry-run (the launch entry still lands in launches.jsonl)."""

    def __init__(self) -> None:
        self.dry_run = True

    def launch(self, spec: Any) -> Any:
        from fleet_graph.scheduler.launcher import LaunchResult

        return LaunchResult(spec.unit_name, False, "dry-run (testenv sample)")


if __name__ == "__main__":
    raise SystemExit(main())
