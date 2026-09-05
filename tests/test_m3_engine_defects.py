"""M3.1（wf-8d9737）· 引擎六缺陷收束——裁决落地链修复的红靶与机械判据。

监督面 2026-09-03 派工（spec: .dev-dispatch/spec/approved.md），逐条钉死：

- **红靶①（缺陷 1+2，阻断）**：一张真实 ``awaiting_gate`` 单，经注册的
  ``decision_deliver`` MCP 工具（FastMCP in-process 客户端，@mcp.tool 面）
  投 REJECT：断言 board 决议读模型（``Board.decision_for``）可解析出该裁决，
  且单据 terminal=refused——不再只 resume 不送裁决。
- **红靶②（缺陷 2，阻断）**：同一单、同一裁决 action_key，首次 resume 失败
  （unit 未消费）后重投：不被 already_resumed/认领耗尽拒绝，可再次尝试
  resume；裁决真正被消费后同 action_key 幂等（不二次消费）。
- **缺陷 3**：implement 失败重试后，新回执 parent 锚定最新链头（拒绝它的
  review receipt），不再回卷到链根。
- **缺陷 4**：LINE_NOT_PARKED 拒绝语携带单据实际当前状态（动态读）。
- **缺陷 5**：读模型 / stall 文件的驻停声明收敛单一 authority（stall 快照
  为 authority、terminal 声明供 waiting 理由与 run 一致性），分叉以
  authority 为准，投递面与读模型同一推导。
- **缺陷 6**：status.json 不再作为可消费缓存被读；消费方一律读权威 run
  工件（result.json / record.json）。构造分叉（缓存说谎）断言以 authority
  为准。

S10/S11 回归不倒退：授权校验、consumed 判据仍由 test_m2 / test_m3_line_selfgate
钉住，本文件只补六缺陷的红靶。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from conftest import git, head
from fleet_graph.bus.board import Board, GateTicket
from fleet_graph.dd.chain_rules import rework_link_parent
from fleet_graph.dd.control_plane import (
    CHECKPOINT_FILE,
    LAUNCHES_FILE,
    RESULT_FILE,
    DdControlPlane,
)
from fleet_graph.dd.lifecycle import Lifecycle
from fleet_graph.dd.upstream_constants import compute_json_digest
from fleet_graph.decision_mcp import (
    CODE_LINE_NOT_PARKED,
    DECISION_APPROVE,
    DECISION_REJECT,
    OUTCOME_REFUSED,
    deliver_decision,
)
from fleet_graph.graphs.dd_pipeline import (
    SPINE_EVENT,
    PipelineBounds,
    PipelineDeps,
    Sealed,
    StageOutcome,
    build_dd_pipeline_graph,
    initial_state,
)
from fleet_graph.scheduler.launcher import LaunchResult
from fleet_graph.scheduler.wake import LiveDdWakeFacts
from fleet_graph.state.fleet_state import (
    BASIS_DOCUMENT_AWAITING,
    BASIS_LEFT_AWAITING_GATE,
    FleetStateConfig,
    FleetStateView,
)
from fleet_graph.state.run_artifacts import parked_decision_state

PRINCIPAL = "wf-8d9737"
REPO_ROOT = Path(__file__).resolve().parent.parent

SPEC = """# SPEC: add a name parameter to greet()

Make `greet(name)` return a personalised greeting.

```dd-acceptance
python3 -m pytest -q
```
"""


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# --- shared harness: a real control plane over a memory bus -----------------


class MemoryBusClient:
    """A bus client stand-in with just the shapes the real Board reads and
    the dd gate delivery publishes: publish / refs_to / messages. Publishing
    is idempotent on the idempotency key, like the real bus."""

    def __init__(self) -> None:
        self.store: list[dict[str, Any]] = []
        self._seq = 0

    def publish(
        self,
        channel: str,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        *,
        refs: list[dict[str, str]] | None = None,
        **_: Any,
    ) -> Any:
        for message in self.store:
            if message["idempotency_key"] == idempotency_key:
                return SimpleNamespace(message_id=message["message_id"])
        self._seq += 1
        message_id = f"msg-{self._seq}"
        self.store.append(
            {
                "message_id": message_id,
                "kind": kind,
                "payload": payload,
                "refs": refs or [],
                "idempotency_key": idempotency_key,
                "channel_seq": self._seq,
                "channel": channel,
            }
        )
        return SimpleNamespace(message_id=message_id, entity_id=message_id)

    def refs_to(self, message_id: str) -> list[dict[str, str]]:
        return [
            {"message_id": message["message_id"]}
            for message in self.store
            if any(ref.get("target_entity") == message_id for ref in message["refs"])
        ]

    def messages(
        self, channel: str, limit: int = 100, after_seq: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        selected = [
            message
            for message in self.store
            if message["channel"] == channel and message["channel_seq"] > after_seq
        ]
        head_seq = max((message["channel_seq"] for message in selected), default=0)
        return selected[:limit], head_seq


def memory_board() -> Board:
    return Board(MemoryBusClient())


class GateUnitLauncher:
    """Stands in for TransientLauncher, and plays the resumed unit.

    The real resumed graph re-reads the board and consumes the verdict per
    its semantics: a REJECT terminalises the single ``refused``; an APPROVE
    leaves the gate (running toward merge); no decision on the board means
    the graph re-suspends and the single stays parked. This fake writes
    exactly that outcome into the generation's authority result.json,
    synchronously -- the same artifacts the control plane rebuilds from.
    """

    dry_run = False

    def __init__(self, board: Board | None, *, consume: bool = True):
        self.board = board
        self.consume = consume
        self.active: set[str] = set()
        self.launched: list[Any] = []

    def launch(self, spec: Any) -> LaunchResult:
        self.launched.append(spec)
        if spec.resume and self.consume:
            self._consume(spec)
        return LaunchResult(spec.unit_name, True, "recorded")

    def _gen_root(self, spec: Any) -> Path:
        root = Path(spec.dev_root)
        return root if spec.generation <= 1 else root / f"g{spec.generation}"

    def _consume(self, spec: Any) -> None:
        assert self.board is not None
        result_path = self._gen_root(spec) / RESULT_FILE
        result: dict[str, Any] = {}
        if result_path.is_file():
            result = dict(json.loads(result_path.read_text(encoding="utf-8")))
        awaiting = result.get("awaiting") or {}
        decision = self.board.decision_for(
            GateTicket(
                question_note_id=str(awaiting.get("question_note_id") or ""),
                card_entity_id=str(awaiting.get("card_entity_id") or ""),
            )
        )
        if decision is None:
            # No verdict on the board: the gate re-suspends, the single stays
            # parked (the unit then exits -- never active).
            return
        result.pop("gate_refused", None)
        if str(decision.decision) == "REJECT":
            result["terminal"] = "refused"
            result["terminal_reason"] = decision.rationale or "gate rejected"
            result["awaiting"] = None
        else:
            result["terminal"] = None
            result["awaiting"] = None
            result["stage"] = "merger"
            self.active.add(spec.unit_name)
        result_path.write_text(json.dumps(result), encoding="utf-8")


def make_plane(
    tmp_path: Path,
    *,
    board: Board | None,
    launcher: GateUnitLauncher,
) -> DdControlPlane:
    binding = tmp_path / "plugin-binding.json"
    if not binding.exists():
        binding.write_text('{"plugin_producer": {}}', encoding="utf-8")
    return DdControlPlane(
        root=tmp_path / "dd",
        plugin_binding=binding,
        worktree_roots=(str(tmp_path),),
        working_directory=str(tmp_path),
        executable="/usr/local/bin/fleet-graph",
        launcher=launcher,
        unit_probe=lambda unit: unit in launcher.active,
        board_factory=lambda: board,
        clock=lambda: 1_700_000_000.0,
    )


def admit(
    tmp_path: Path, board: Board | None, launcher: GateUnitLauncher
) -> tuple[DdControlPlane, str, Path]:
    """A real admitted development on a scratch repo (worktree + bare origin)."""
    repo = tmp_path / "work"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "greet.py").write_text('def greet():\n    return "hello"\n', encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "seed")
    bare = tmp_path / "origin.git"
    git(repo, "init", "-q", "--bare", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    plane = make_plane(tmp_path, board=board, launcher=launcher)
    created = plane.create(str(repo), spec_text=SPEC, dispatched_by=PRINCIPAL)
    return plane, str(created["development_id"]), repo


def suspend_at_gate(plane: DdControlPlane, repo: Path, development_id: str) -> None:
    """Park the admitted development at the human gate: an awaiting note on
    its authority result.json plus a durable checkpoint thread."""
    dev_root = plane.root / development_id
    (dev_root / RESULT_FILE).write_text(
        json.dumps(
            {
                "development_id": development_id,
                "terminal": None,
                "stage": "human_gate",
                "head_commit": head(repo),
                "awaiting": {
                    "question_note_id": "msg_question_1",
                    "card_entity_id": "ent-dd-card",
                },
                "history": [],
            }
        ),
        encoding="utf-8",
    )
    (dev_root / CHECKPOINT_FILE).touch()


def call_tool(server: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """One call through the registered @mcp.tool face (in-process client)."""
    from fastmcp import Client

    async def call() -> dict[str, Any]:
        async with Client(server) as client:
            result = await client.call_tool("decision_deliver", arguments)
            return json.loads(result.content[0].text)

    return asyncio.run(call())


def resume_launches(plane: DdControlPlane, development_id: str) -> list[dict[str, Any]]:
    path = plane.root / development_id / LAUNCHES_FILE
    if not path.is_file():
        return []
    return [
        entry
        for entry in (json.loads(line) for line in path.read_text("utf-8").splitlines() if line)
        if entry.get("mode") == "resume"
    ]


def claim_files(plane: DdControlPlane, development_id: str) -> list[Path]:
    claim_dir = plane.root / development_id / "resume-claims"
    return sorted(claim_dir.rglob("*.json")) if claim_dir.is_dir() else []


# --- 红靶①：裁决送达必须落地（缺陷 1，R3 后经图内 gate 节点） ----------------


def _gate_node(plane: DdControlPlane) -> Any:
    from fleet_graph.dd.self_gate import EvidenceItem
    from fleet_graph.graphs.dd_gate import GraphGateNode

    return GraphGateNode(
        plane,
        evidence=[
            EvidenceItem(name, name, True, "grounded by the red-target harness")
            for name in (
                "acceptance_frozen",
                "diff_within_scope",
                "zero_test_deletion",
                "personally_rerun",
                "mutation_receipt",
                "regression",
            )
        ],
    )


def _gate_action(dev: str, verdict: str, key: str = "red-target-k1") -> dict[str, Any]:
    from fleet_graph.graphs.stop_response import KIND_GATE_RELEASE

    payload: dict[str, Any] = {
        "development_id": dev,
        "verdict": verdict,
        "decided_by": PRINCIPAL,
    }
    if verdict == "REJECT":
        payload["board_decision"] = {
            "problem": "验收取证不齐",
            "suggested_answer": "补齐冻结验收后重跑",
            "cost_of_no_answer": "单据在闸口无人能裁",
        }
    return {"kind": KIND_GATE_RELEASE, "payload": payload, "idempotency_key": key}


class TestRedTargetVerdictMustLand:
    """A real awaiting_gate single, the line's own graph gate node consuming
    its ``dd.gate_release.v1`` (R3: the only release path), a REJECT: the
    board's decision read model resolves the verdict, and the single
    terminalises refused."""

    def test_reject_via_the_gate_node_publishes_the_verdict_and_refuses_the_single(
        self, tmp_path: Path
    ) -> None:
        board = memory_board()
        launcher = GateUnitLauncher(board)
        plane, dev, repo = admit(tmp_path, board, launcher)
        suspend_at_gate(plane, repo, dev)

        receipt = _gate_node(plane).consume(
            _gate_action(dev, "REJECT"), folder_id=PRINCIPAL, round_no=1
        )

        assert receipt["status"] == "consumed", receipt
        assert receipt["development_id"] == dev
        assert receipt["decision"] == DECISION_REJECT

        # board.decision_for（决议读模型）可解析出该裁决——ref 到问题 note。
        ticket = GateTicket(question_note_id="msg_question_1", card_entity_id="ent-dd-card")
        decision = board.decision_for(ticket)
        assert decision is not None, "the released verdict must be resolvable on the board"
        assert decision.decision == DECISION_REJECT
        assert decision.rationale.startswith("acceptance_frozen=PASS")
        assert decision.decided_by == PRINCIPAL

        # 单据 terminal=refused（非仅 resume）。
        status = plane.get(dev)
        assert status["terminal"] == "refused"
        assert status["state"] == "refused"

    def test_approve_via_the_gate_node_consumes_the_verdict_and_leaves_the_gate(
        self, tmp_path: Path
    ) -> None:
        board = memory_board()
        launcher = GateUnitLauncher(board)
        plane, dev, repo = admit(tmp_path, board, launcher)
        suspend_at_gate(plane, repo, dev)

        receipt = _gate_node(plane).consume(
            _gate_action(dev, "APPROVE"), folder_id=PRINCIPAL, round_no=1
        )

        assert receipt["status"] == "consumed", receipt
        ticket = GateTicket(question_note_id="msg_question_1", card_entity_id="ent-dd-card")
        assert board.decision_for(ticket) is not None
        status = plane.get(dev)
        assert status["state"] != "awaiting_gate"

    def test_a_verdict_that_cannot_reach_the_read_model_is_refused_before_any_resume(
        self, tmp_path: Path
    ) -> None:
        # No board at all: publishing the verdict is impossible, so the
        # release fails closed instead of running a valueless resume (S10).
        launcher = GateUnitLauncher(memory_board())
        plane, dev, repo = admit(tmp_path, None, launcher)
        suspend_at_gate(plane, repo, dev)

        receipt = _gate_node(plane).consume(
            _gate_action(dev, "REJECT"), folder_id=PRINCIPAL, round_no=1
        )

        assert receipt["status"] == "failed"
        assert receipt["reason"] == "release_refused"
        assert launcher.launched == [], "no unit may start when the verdict cannot land"


# --- 红靶②：失败 resume 归还认领（缺陷 2） ----------------------------------


class TestRedTargetFailedResumeReturnsTheClaim:
    """Same single, same verdict action_key: after a resume that did NOT
    consume (the unit died unconsumed), the redelivery must be accepted and
    re-attempt the resume; once truly consumed, the same key is idempotent."""

    def test_redelivery_after_a_failed_resume_is_accepted_and_consumes(
        self, tmp_path: Path
    ) -> None:
        board = memory_board()
        launcher = GateUnitLauncher(board, consume=False)
        plane, dev, repo = admit(tmp_path, board, launcher)
        suspend_at_gate(plane, repo, dev)
        node = _gate_node(plane)
        action = _gate_action(dev, "REJECT")

        first = node.consume(action, folder_id=PRINCIPAL, round_no=1)
        assert first["status"] == "consumed"
        assert first["post_release_state"] == "awaiting_gate", (
            "the receipt must record honestly that the single stayed parked"
        )
        assert len(resume_launches(plane, dev)) == 1, "the first resume did launch"
        assert len(claim_files(plane, dev)) == 1, "the failed attempt held the claim"

        # The unit comes back and this time the graph consumes the verdict.
        launcher.consume = True
        second = node.consume(action, folder_id=PRINCIPAL, round_no=1)

        assert second["status"] == "consumed", second
        assert second["post_release_state"] == "refused"
        assert len(resume_launches(plane, dev)) == 2, (
            "the redelivery must be accepted and re-attempt the resume, "
            "not be refused as already_resumed"
        )
        status = plane.get(dev)
        assert status["terminal"] == "refused"
        assert len(claim_files(plane, dev)) == 1, "re-claimed for the consuming attempt"

    def test_after_true_consumption_the_same_action_key_is_idempotent(self, tmp_path: Path) -> None:
        board = memory_board()
        launcher = GateUnitLauncher(board)
        plane, dev, repo = admit(tmp_path, board, launcher)
        suspend_at_gate(plane, repo, dev)
        node = _gate_node(plane)
        action = _gate_action(dev, "REJECT")
        assert node.consume(action, folder_id=PRINCIPAL, round_no=1)["status"] == "consumed"

        third = node.consume(action, folder_id=PRINCIPAL, round_no=1)

        assert third["status"] == "failed"
        assert third["reason"] == "not_awaiting_gate"
        assert len(resume_launches(plane, dev)) == 1, "no second consumption"

    def test_the_control_plane_releases_the_claim_and_records_it(self, tmp_path: Path) -> None:
        """The gate-level mechanics: claim exists + resume launched + verdict
        unconsumed => the claim is returned (with an events trail) and the
        same action key launches again."""
        board = memory_board()
        launcher = GateUnitLauncher(board, consume=False)
        plane, dev, repo = admit(tmp_path, board, launcher)
        suspend_at_gate(plane, repo, dev)
        action_key = f"e1:d-1:dd:{dev}:1"

        first = plane.gate(dev, resume=True, action_key=action_key)
        assert first["resume"]["mode"] == "resume"
        assert len(claim_files(plane, dev)) == 1

        second = plane.gate(dev, resume=True, action_key=action_key)

        assert not second.get("already_resumed"), (
            "a failed resume must not burn the claim: the same verdict is "
            "allowed to re-attempt the resume"
        )
        assert second["resume"]["mode"] == "resume"
        assert len(launcher.launched) == 2
        assert len(claim_files(plane, dev)) == 1, "re-claimed for the new attempt"
        events = plane.root / dev / "events.jsonl"
        assert events.is_file()
        assert any(
            json.loads(line).get("event") == "resume_claim_released"
            for line in events.read_text(encoding="utf-8").splitlines()
            if line
        )


# --- 缺陷 3：implement 失败重试的回执 parent 锚定最新链头 --------------------


class RecordingSealer:
    def __init__(self) -> None:
        self.sealed: list[dict[str, Any]] = []

    def materialize(self, stage: Any, dispatch: dict[str, Any], outcome: StageOutcome) -> Sealed:
        commit = (
            f"sealed-{stage.id}-g{dispatch['generation']}"
            f"-a{dispatch['attempt']}-r{dispatch['retry']}"
        )
        receipt = dict(outcome.receipt or {})
        receipt["output_commit"] = commit
        self.sealed.append({"stage": stage.id, "receipt": receipt})
        return Sealed(commit=commit, receipt=receipt)


class ReworkRetryActor:
    """attempt 2 of implement fails once (PROVIDER_UNAVAILABLE) then succeeds;
    the continuous review rejects attempt 1 and approves attempt 2."""

    def __init__(self, lifecycle: Lifecycle) -> None:
        self.lifecycle = lifecycle
        self.dispatches: list[dict[str, Any]] = []
        self.implement_attempt2_calls = 0

    def act(self, stage: Any, dispatch: dict[str, Any]) -> StageOutcome:
        self.dispatches.append({"stage": stage.id, **dispatch})
        if stage.id == "implement" and dispatch["attempt"] == 2:
            self.implement_attempt2_calls += 1
            if self.implement_attempt2_calls == 1:
                return StageOutcome(
                    event="failed",
                    failure_code="PROVIDER_UNAVAILABLE",
                    detail="provider down",
                )
        event = SPINE_EVENT
        if stage.id == "continuous_review":
            event = "APPROVE" if dispatch["attempt"] >= 2 else "REJECT"
        if stage.id == "final_review":
            event = "APPROVE"
        return StageOutcome(
            event=event,
            receipt={
                "stage": stage.id,
                "verdict": event,
                "output_commit": dispatch["input_commit"],
            },
            produced=tuple(stage.produced_artifacts),
        )


class FlakyImplementActor:
    """Reviews answer APPROVE; everything else answers the spine; implement
    fails exactly once."""

    def __init__(self, lifecycle: Lifecycle) -> None:
        self.lifecycle = lifecycle
        self.dispatches: list[dict[str, Any]] = []
        self.implement_calls = 0

    def act(self, stage: Any, dispatch: dict[str, Any]) -> StageOutcome:
        self.dispatches.append({"stage": stage.id, **dispatch})
        if stage.id == "implement":
            self.implement_calls += 1
            if self.implement_calls == 1:
                return StageOutcome(
                    event="failed",
                    failure_code="PROVIDER_UNAVAILABLE",
                    detail="provider down",
                )
        event = "APPROVE" if stage.id in ("continuous_review", "final_review") else SPINE_EVENT
        return StageOutcome(
            event=event,
            receipt={
                "stage": stage.id,
                "verdict": event,
                "output_commit": dispatch["input_commit"],
            },
            produced=tuple(stage.produced_artifacts),
        )


def run_walker(deps: PipelineDeps) -> dict[str, Any]:
    from langgraph.checkpoint.memory import InMemorySaver

    graph = build_dd_pipeline_graph(deps)
    compiled = graph.compile(checkpointer=InMemorySaver())
    start = initial_state(
        development_id="dev-1",
        stage="configure",
        head_commit="0" * 40,
        artifacts={"spec": "0" * 40},
    )
    return compiled.invoke(
        start,
        config={
            "configurable": {"thread_id": "dev-1:g1"},
            "recursion_limit": 300,
        },
    )


def make_deps(
    lifecycle: Lifecycle, actor: Any, sealer: RecordingSealer, **bounds: Any
) -> PipelineDeps:
    scripts = {name: actor for name, stage in lifecycle.stages.items() if not stage.is_llm}
    return PipelineDeps(
        lifecycle=lifecycle,
        dispatcher=actor,
        scripts=scripts,
        materializer=sealer,
        bounds=PipelineBounds(**bounds),
    )


class TestDefectThreeReceiptParentAnchorsLatestChainHead:
    def test_a_rework_implement_retry_names_the_rejecting_review_as_parent(self) -> None:
        lifecycle = Lifecycle.load()
        actor = ReworkRetryActor(lifecycle)
        sealer = RecordingSealer()

        state = run_walker(make_deps(lifecycle, actor, sealer, max_rework=4, max_retries=2))

        assert state.get("terminal") == "complete", state.get("terminal_reason")

        rework = [e for e in actor.dispatches if e["stage"] == "implement" and e["attempt"] == 2]
        assert len(rework) == 2, "the failed rework implement is retried once"
        rejecting_receipt = next(
            s["receipt"] for s in sealer.sealed if s["stage"] == "continuous_review"
        )
        assert rejecting_receipt.get("verdict") == "REJECT"
        # The chain-head rule, from the single source both sides share: the
        # rework implement names the rejecting review's canonical digest.
        assert rework_link_parent(rejecting_receipt) == compute_json_digest(rejecting_receipt)
        for entry in rework:
            assert entry["parent_receipt"], (
                "the rework implement must not fall back to the chain root (M3.1 defect 3)"
            )
            assert entry["parent_receipt"] == rejecting_receipt, (
                "the rework implement's parent must anchor on the latest chain head"
            )

    def test_the_failed_retry_keeps_the_previous_stage_receipt(self) -> None:
        """First-attempt shape: implement fails once; the retry's dispatch
        carries the configure receipt, not the chain root."""
        lifecycle = Lifecycle.load()
        actor = FlakyImplementActor(lifecycle)
        sealer = RecordingSealer()

        state = run_walker(make_deps(lifecycle, actor, sealer, max_retries=2))

        assert state.get("terminal") == "complete"
        configure_receipt = next(s["receipt"] for s in sealer.sealed if s["stage"] == "configure")
        implement_dispatches = [e for e in actor.dispatches if e["stage"] == "implement"]
        assert len(implement_dispatches) == 2
        first, retry = implement_dispatches
        assert first["parent_receipt"] == configure_receipt
        assert retry["parent_receipt"] == configure_receipt, (
            "a failed outcome must not clear the carried receipt: the retry "
            "stays anchored on the configure seal, not the chain root"
        )


# --- 缺陷 4：LINE_NOT_PARKED 拒绝语携带实际状态 ------------------------------


ROSTER: list[Any] = [{"folder_id": "wf-1", "generation": 2}]


class TestDefectFourRefusalCarriesTheActualState:
    def test_a_running_line_is_told_its_actual_running_state(self, tmp_path: Path) -> None:
        write_json(tmp_path / "wf-1" / "heartbeat.json", {"round": 4})

        result = deliver_decision(
            line="wf-1",
            decision=DECISION_APPROVE,
            reason="live",
            run_root=tmp_path,
            lines=ROSTER,
        )

        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_LINE_NOT_PARKED
        assert "running (round 4)" in result.message
        assert "wf-1" in result.message

    def test_a_line_blocked_on_something_else_names_that_waiting_reason(
        self, tmp_path: Path
    ) -> None:
        # The scheduler retracted the park (cleared snapshot) while the line's
        # own terminal declares waiting_on=inbox: the refusal must carry the
        # ACTUAL waiting reason, not a hardcoded decision word.
        write_json(
            tmp_path / ".scheduler" / "wf-1.json",
            {"generation": 2, "parked_run_id": None, "parked_at": None},
        )
        write_json(
            tmp_path / "wf-1" / "terminal.json",
            {"terminal": "blocked", "waiting_on": "inbox", "run_id": "run-2"},
        )

        result = deliver_decision(
            line="wf-1",
            decision=DECISION_APPROVE,
            reason="live",
            run_root=tmp_path,
            lines=ROSTER,
        )

        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_LINE_NOT_PARKED
        assert "terminal=blocked" in result.message
        assert "waiting_on=inbox" in result.message

    def test_a_completed_line_is_told_its_actual_terminal(self, tmp_path: Path) -> None:
        write_json(
            tmp_path / "wf-1" / "terminal.json",
            {"terminal": "done", "waiting_on": "none", "run_id": "run-3"},
        )

        result = deliver_decision(
            line="wf-1",
            decision=DECISION_APPROVE,
            reason="live",
            run_root=tmp_path,
            lines=ROSTER,
        )

        assert result.status == OUTCOME_REFUSED
        assert "terminal=done" in result.message


# --- 缺陷 5：读模型 / stall 单一 authority -----------------------------------


def parked_stall(run_root: Path, folder_id: str, run_id: str) -> None:
    write_json(
        run_root / ".scheduler" / f"{folder_id}.json",
        {
            "generation": 2,
            "board_question_note_id": "q-1",
            "board_card_entity_id": "card-1",
            "parked_run_id": run_id,
            "parked_at": 1_700_000_000.0,
            "parked_goal_revision": "sha256:consumed",
            "parked_inbox_available": True,
        },
    )


def blocked_terminal(run_root: Path, folder_id: str, run_id: str) -> None:
    write_json(
        run_root / folder_id / "terminal.json",
        {"terminal": "blocked", "waiting_on": "decision", "run_id": run_id},
    )


def read_model(run_root: Path, tmp_path: Path) -> FleetStateView:
    roster = tmp_path / "roster.json"
    write_json(
        roster, {"lines": [{"folder_id": "wf-1", "generation": 2}], "run_root": str(run_root)}
    )
    return FleetStateView(
        FleetStateConfig(
            run_root=run_root,
            lines_config=roster,
            bridge_state_dir=tmp_path / "bridge",
            enroll_queue_path=None,
        )
    )


class TestDefectFiveSingleParkedAuthority:
    def test_consistent_park_is_parked_on_both_surfaces(self, tmp_path: Path) -> None:
        run_root = tmp_path / "runs"
        parked_stall(run_root, "wf-1", "run-1")
        blocked_terminal(run_root, "wf-1", "run-1")

        assert parked_decision_state(run_root, "wf-1").parked is True
        line = read_model(run_root, tmp_path).lines()["lines"][0]
        assert line["parked"] is True

    def test_a_superseded_park_snapshot_loses_against_the_newer_run(self, tmp_path: Path) -> None:
        run_root = tmp_path / "runs"
        parked_stall(run_root, "wf-1", "run-1")
        blocked_terminal(run_root, "wf-1", "run-2")

        park = parked_decision_state(run_root, "wf-1")
        assert park.parked is False
        assert "superseded" in park.state_word
        assert read_model(run_root, tmp_path).lines()["lines"][0]["parked"] is False

        result = deliver_decision(
            line="wf-1",
            decision=DECISION_APPROVE,
            reason="live",
            run_root=run_root,
            lines=ROSTER,
        )
        assert result.status == OUTCOME_REFUSED
        assert result.code == CODE_LINE_NOT_PARKED

    def test_a_retracted_park_is_not_parked_even_when_the_terminal_still_declares_it(
        self, tmp_path: Path
    ) -> None:
        run_root = tmp_path / "runs"
        # The stall file exists (the scheduler considered this run) but the
        # snapshot is cleared: the operator escape hatch retracted the park.
        write_json(
            run_root / ".scheduler" / "wf-1.json",
            {"generation": 2, "parked_run_id": None, "parked_at": None},
        )
        blocked_terminal(run_root, "wf-1", "run-1")

        assert parked_decision_state(run_root, "wf-1").parked is False
        assert read_model(run_root, tmp_path).lines()["lines"][0]["parked"] is False

    def test_without_scheduler_state_the_terminal_declaration_stands(self, tmp_path: Path) -> None:
        run_root = tmp_path / "runs"
        blocked_terminal(run_root, "wf-1", "run-1")

        assert parked_decision_state(run_root, "wf-1").parked is True
        assert read_model(run_root, tmp_path).lines()["lines"][0]["parked"] is True

    def test_a_dd_dispatch_park_is_not_a_decision_park(self, tmp_path: Path) -> None:
        run_root = tmp_path / "runs"
        parked_stall(run_root, "wf-1", "run-1")
        stall_path = run_root / ".scheduler" / "wf-1.json"
        stall = dict(json.loads(stall_path.read_text(encoding="utf-8")))
        stall["parked_dd_development_id"] = "dev-fg-abc"
        stall_path.write_text(json.dumps(stall), encoding="utf-8")

        assert parked_decision_state(run_root, "wf-1").parked is False


# --- 缺陷 6：status.json 不再作为可消费缓存被读 ------------------------------


class TestDefectSixStatusJsonIsNotConsumed:
    @staticmethod
    def write_development(
        dd_root: Path,
        development_id: str,
        *,
        generation: int,
        result: dict[str, Any],
        lying_status: dict[str, Any] | None,
    ) -> None:
        dev = dd_root / development_id
        dev.mkdir(parents=True, exist_ok=True)
        write_json(
            dev / "record.json", {"development_id": development_id, "generation": generation}
        )
        result_path = (
            dev / "result.json" if generation <= 1 else dev / f"g{generation}" / "result.json"
        )
        write_json(result_path, result)
        if lying_status is not None:
            # The stale cache says something DIFFERENT from the authority.
            write_json(dev / "status.json", lying_status)

    def test_the_wake_fact_follows_the_authority_not_the_lying_cache(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        self.write_development(
            dd_root,
            "dev-1",
            generation=1,
            result={"terminal": "merged", "awaiting": None},
            lying_status={"state": "awaiting_gate", "terminal": ""},
        )
        assert LiveDdWakeFacts(dd_root).dd_fact("dev-1") == "terminal"

    def test_the_harvestable_view_follows_the_authority(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        self.write_development(
            dd_root,
            "dev-1",
            generation=1,
            result={"terminal": "complete", "head_commit": "h1", "stage": "merger"},
            lying_status={"state": "awaiting_gate", "terminal": ""},
        )
        roster = tmp_path / "roster.json"
        write_json(roster, {"lines": []})
        view = FleetStateView(
            FleetStateConfig(
                run_root=tmp_path / "runs",
                lines_config=roster,
                dd_root=dd_root,
                bridge_state_dir=tmp_path / "bridge",
                enroll_queue_path=None,
                has_harvest_receipt=lambda card: False,
            )
        )
        view._load_e5_baseline = lambda: set()  # type: ignore[method-assign]
        payload = view.harvestable()
        assert [d["development_id"] for d in payload["developments"]] == ["dev-1"]

    def test_the_reconciliation_follows_the_authority(self, tmp_path: Path) -> None:
        dd_root = tmp_path / "dd"
        self.write_development(
            dd_root,
            "dev-1",
            generation=1,
            result={"terminal": "complete", "awaiting": None},
            lying_status={"state": "awaiting_gate"},
        )
        from fleet_graph.state.fleet_state import _document_gate_consumed

        assert _document_gate_consumed(dd_root, "dev-1", 1) == BASIS_LEFT_AWAITING_GATE

    def test_a_still_awaiting_authority_keeps_the_document_awaiting_basis(
        self, tmp_path: Path
    ) -> None:
        dd_root = tmp_path / "dd"
        self.write_development(
            dd_root,
            "dev-1",
            generation=1,
            result={"terminal": None, "awaiting": {"question_note_id": "q-1"}},
            lying_status={"state": "complete", "terminal": "complete"},
        )
        from fleet_graph.state.fleet_state import _document_gate_consumed

        assert _document_gate_consumed(dd_root, "dev-1", 1) == BASIS_DOCUMENT_AWAITING

    def test_the_read_sides_no_longer_read_the_cache_file(self) -> None:
        """读即红：the wake probe and the read model must not consume
        status.json anywhere in their source (the file is a rebuildable
        projection, never an authority)."""
        for relpath in (
            "src/fleet_graph/scheduler/wake.py",
            "src/fleet_graph/state/fleet_state.py",
        ):
            source = (REPO_ROOT / relpath).read_text(encoding="utf-8")
            assert "status.json" not in source, relpath
