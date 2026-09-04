#!/usr/bin/env python3
"""lim-probe m4b — verify-lim check 15/16 的现场合成靶探针（spec-m4b 交付面 3）。

背景（spec-m4b/approved.md）：M4 机制代码在位（ack 台账 + rounds 镜像 +
wake_facts 折叠 + bare decision token 守卫），但 verify-lim check 15 的 else
分支与 check 16 的第三分支是硬编码 FAIL 脚手架，且从未有一次真实投递+drain
产生过记录。本探针把两条占位分支改为真实探针，参照 check 12 先例**现场合成
靶**：靶线、名册、run root 全部落在一次性临时目录，探针自备、跑完即清，
生产名册/生产线/真实单/总线零触碰（不读不写 /data 下任何路径）。

与 check 12 只合成「单据数据」不同，15/16 无法把合成线塞进生产名册（名册
PR 冻结面），所以靶栈由本仓库自己的产品代码在进程内组装——部署面跑的就是
同一份代码：

- 投递走真实 ``deliver_line_message``（closed payload、身份闸、别名闸）；
- 线的一个 round 走真实 goal-line 图（drain_then_ack → 回执义务 → 台账 +
  rounds 镜像 → 裁决 → finalise 落 terminal.json）；
- 调度 tick 走真实 ``Scheduler.tick()``（account → park_state → decide，
  建驻停/wake 唤醒/再驻停全是产品逻辑）；
- state 面走真实 ``FleetStateHTTPServer`` 起在临时回环端口，HTTP GET
  ``/v1/lines`` 与生产 :7494 同一 handler、同一派生代码。

check 15（ack 落档 + state 面可读）：
  投递 instruction/info/bare "APPROVE" 三条 → 一个调度 tick（priming）→
  线一个 round 消费 → 断言：
    - 台账行形状含 {round, at, message_id, outcome, reason}；
    - instruction 落 executed 回执；info 无任何 ack 行（阴性①）；
    - bare "APPROVE" 被泵守卫拒绝 rejected/message_is_not_a_decision，
      全链无任何冒充裁决的记录（阴性②）；
    - rounds.jsonl 镜像行含 line_message_acks/unacked_instructions；
    - /v1/lines 的 wake_facts.line_message_acks 最新在前，最新一条与最近
      一条 inbox instruction 的 message_id 机械比对一致。

check 16（驻停对照：仅 inbox 不解除 waiting_decision）：
  两条靶线各自走完整事件：priming tick → 线 blocked(waiting_on=decision)
  → tick 建驻停 → BEFORE 快照 → 投一条 line_message → tick（woken:inbox，
  收信事实）→ 线重跑（drain+ack，仍无裁决 → 再次 blocked）→ tick 再驻停 →
  AFTER 快照。断言：
    - 驻停事实字段 {terminal, waiting_on, line_state, parked, face 端
      terminal/waiting_on} BEFORE==AFTER（字段 diff 为空 → 绿）；
    - 收信确凿：wake tick park_event == "woken:inbox"，台账有该消息回执；
    - negative：bare "APPROVE" 线的回执是 rejected/message_is_not_a_decision，
      驻停快照全程无 dispatched_decision_consumed_at 之类裁决事实。
  parked_run_id/parked_at（驻停身份，r1→r2）是唯一合理变化，作为上下文
  报告、不进不变量字段集。

用法：python scripts/lim_probe_m4b.py --check 15|16
退出码：0 = 全部断言绿；1 = 任一断言红（并在末行给出一行式原因）。
输出：末行恒为 `M4B-PROBE <nn> PASS|FAIL — <依据>`，供 verify-lim.sh 引用。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import threading
import traceback
import urllib.request
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
for _candidate in (REPO_ROOT / "src", REPO_ROOT):
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from fleet_graph.goal.line_message import (  # noqa: E402
    DECISION_GUARD_REASON,
    KIND_INSTRUCTION,
    deliver_line_message,
)
from fleet_graph.graphs.goal_line import LineDeps, build_goal_line_graph  # noqa: E402
from fleet_graph.graphs.guards import LineBounds, LineGuards  # noqa: E402
from fleet_graph.scheduler.daemon import LineSpec, Scheduler, SchedulerConfig  # noqa: E402
from fleet_graph.scheduler.launcher import LaunchResult  # noqa: E402
from fleet_graph.state.fleet_state import FleetStateConfig, FleetStateHTTPServer  # noqa: E402
from fleet_graph.state.run_artifacts import (  # noqa: E402
    RunArtifacts,
)

#: The synthetic goal revision every probe line "consumed". Fixed: the
#: synthetic goal never changes, so the parking anchor never spuriously wakes.
GOAL_REVISION = "lim-selftest-goal-rev-0001"

#: The check-16 invariant field set: the waiting_decision *facts* (the closed
#: line-state word and its terminal/derived carriers). Park bookkeeping identity
#: (parked_run_id/parked_at) legitimately advances r1→r2 across the event and is
#: reported as context, never asserted unchanged.
INVARIANT_FIELDS = (
    "terminal",
    "waiting_on",
    "line_state",
    "parked",
    "face_terminal",
    "face_waiting_on",
)


class ProbeError(AssertionError):
    """One failed probe assertion, carrying the human-readable 依据."""


def require(condition: Any, detail: str) -> None:
    if not condition:
        raise ProbeError(detail)


# --- the synthetic stack (probe-only fakes over real product seams) ----------


class ProbeClock:
    """Monotonic stepping clock; every read advances one second."""

    def __init__(self) -> None:
        self.now = 1_787_000_000.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


class RecordingSink:
    """The line_message delivery seam: records what the real builder published."""

    def __init__(self, inbox: ProbeInbox) -> None:
        self._inbox = inbox
        self.published: list[tuple[str, str]] = []

    def publish(self, alias: str, payload: dict[str, Any]) -> str:
        message_id = f"limmsg-{uuid.uuid4().hex[:12]}"
        self._inbox.deliver(message_id, payload)
        self.published.append((alias, message_id))
        return message_id


class ProbeInbox:
    """The line's ``agent:{alias}`` channel, emulated on the documented seams.

    Consumed messages stay on the channel (the bus is a tail, not a queue), so
    the scheduler's wake predicate and the pump's one-shot drain read the same
    store exactly like ``LiveWakeSignals`` + the real inbox do.
    """

    def __init__(self, clock: ProbeClock) -> None:
        self._clock = clock
        self._mail: list[dict[str, Any]] = []

    def deliver(self, message_id: str, payload: dict[str, Any]) -> None:
        self._mail.append(
            {"id": message_id, "payload": payload, "created": self._clock(), "consumed": False}
        )

    def inbox_message_after(self, alias: str, after_epoch: float) -> bool:
        return any(entry["created"] > after_epoch for entry in self._mail)

    def drain_then_ack(self, persist: Any) -> tuple[list[dict[str, Any]], list[str]]:
        pending = [entry for entry in self._mail if not entry["consumed"]]
        messages = [{"message_id": entry["id"], "payload": entry["payload"]} for entry in pending]
        for entry in pending:
            entry["consumed"] = True
        persist(messages)
        return messages, ["acked"] * len(messages)


class ProbeWake:
    """WakeSignals over the synthetic stack — honest reads, no shortcuts."""

    def __init__(self, inboxes: dict[str, ProbeInbox]) -> None:
        self._inboxes = inboxes

    def inbox_message_after(self, alias: str, after_epoch: float) -> bool:
        inbox = self._inboxes.get(alias)
        return inbox.inbox_message_after(alias, after_epoch) if inbox else False

    def goal_revision(self, folder_id: str) -> str:
        return GOAL_REVISION

    def decision_landed(self, question_note_id: str, after_epoch: float) -> bool:
        return False


class ScriptedCoordinator:
    """The coordinator seam, scripted per round like the M4 tests drive it."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def turn(
        self, round_no: int, coord_input: dict[str, Any], resume: bool = False
    ) -> dict[str, Any]:
        self.calls.append(coord_input)
        if self.script:
            return self.script.pop(0)
        return {"verdict": "blocked", "reason": "probe script exhausted", "waiting_on": "decision"}

    @property
    def drained_messages(self) -> list[dict[str, Any]]:
        return (self.calls[0] or {}).get("inbox_messages", []) if self.calls else []


class FakeWorker:
    def turn(self, prompt: str, round_no: int) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "turn_id": f"t-{round_no}",
            "outcome": "completed",
            "summary": prompt[:20],
            "did": ["action"],
            "files": [],
            "self_tests": [],
            "blocker": None,
        }


class FakeUnits:
    def is_active(self, unit_name: str) -> bool:
        return False


class FakeProber:
    def check(self, seat: str) -> bool:
        return True


class FakeLauncher:
    def __init__(self) -> None:
        self.launched: list[Any] = []

    def launch(self, spec: Any) -> LaunchResult:
        self.launched.append(spec)
        return LaunchResult(spec.unit_name, True, "")


class ProbeStack:
    """One disposable synthetic fleet: temp run root, roster, bus-less inboxes."""

    def __init__(self, tmp: Path, line_ids: list[str]) -> None:
        self.tmp = tmp
        self.runs = tmp / "runs"
        self.runs.mkdir(parents=True)
        self.line_ids = line_ids
        self.aliases = {line_id: f"lim-selftest-{line_id}" for line_id in line_ids}
        self.clock = ProbeClock()
        self.inboxes = {alias: ProbeInbox(self.clock) for alias in self.aliases.values()}
        self.sinks = {alias: RecordingSink(self.inboxes[alias]) for alias in self.aliases.values()}
        self.launcher = FakeLauncher()
        self.roster = tmp / "ronin-lines.json"
        self.roster.write_text(
            json.dumps(
                {
                    "run_root": str(self.runs),
                    "lines": [
                        {
                            "folder_id": line_id,
                            "seat": "opencode-probe",
                            "alias": self.aliases[line_id],
                            "enabled": True,
                            "generation": 1,
                        }
                        for line_id in line_ids
                    ],
                }
            ),
            encoding="utf-8",
        )

    # -- delivery (the real line_message path) ----------------------------

    def deliver(self, line_id: str, text: str, kind: str, sent_by: str) -> str:
        """One real ``deliver_line_message`` call through every real guard."""
        result = deliver_line_message(
            line_id,
            text,
            kind,
            sent_by,
            resolve_alias=lambda line: self.aliases.get(line),
            sink=self.sinks[self.aliases[line_id]],
            identity_check=lambda identity: identity == sent_by and bool(identity.strip()),
            clock=self.clock,
        )
        require(result.get("delivered") is True, f"delivery refused: {result}")
        return str(result["message_id"])

    # -- scheduler (the real tick) -----------------------------------------

    def scheduler(self) -> Scheduler:
        return Scheduler(
            SchedulerConfig(
                lines=[
                    LineSpec(
                        folder_id=line_id,
                        seat="opencode-probe",
                        alias=self.aliases[line_id],
                        enabled=True,
                    )
                    for line_id in self.line_ids
                ],
                run_root=self.runs,
                dd_root=self.tmp / "dd",
                maintenance_stop_path=self.tmp / "maintenance-stop",
            ),
            prober=FakeProber(),
            launcher=self.launcher,
            units=FakeUnits(),
            clock=self.clock,
            sleep=lambda _seconds: None,
            wake=ProbeWake(self.inboxes),
        )

    # -- the line's pump (the real goal-line graph) -------------------------

    def run_line_round(
        self, line_id: str, run_id: str, script: list[dict[str, Any]]
    ) -> ScriptedCoordinator:
        from langgraph.checkpoint.memory import InMemorySaver

        coordinator = ScriptedCoordinator(script)
        artifacts = RunArtifacts(
            self.runs / line_id,
            run_id=run_id,
            folder_id=line_id,
            clock=self.clock,
        )
        deps = LineDeps(
            coordinator=coordinator,
            worker=FakeWorker(),
            inbox=self.inboxes[self.aliases[line_id]],
            artifacts=artifacts,
            guards=LineGuards(bounds=LineBounds(max_rounds=8)),
            folder_id=line_id,
            run_id=run_id,
            goal_revision=lambda: GOAL_REVISION,
        )
        compiled = build_goal_line_graph(deps).compile(checkpointer=InMemorySaver())
        compiled.invoke(
            {"round_no": 1},
            config={"configurable": {"thread_id": f"{line_id}:{run_id}"}, "recursion_limit": 60},
        )
        return coordinator

    # -- the state face (the real :7494 handler on an ephemeral port) -------

    def state_face_lines(self) -> dict[str, Any]:
        config = FleetStateConfig(
            host="127.0.0.1",
            port=0,
            run_root=self.runs,
            lines_config=self.roster,
            dd_root=self.tmp / "dd",
            bridge_state_dir=self.tmp / "bridge",
            bus_url=None,
            enroll_queue_path=None,
        )
        server = FleetStateHTTPServer(config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/lines", timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()

    # -- mechanical reads ----------------------------------------------------

    def stall_state(self, line_id: str) -> dict[str, Any]:
        path = self.runs / ".scheduler" / f"{line_id}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def terminal(self, line_id: str) -> dict[str, Any]:
        path = self.runs / line_id / "terminal.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def ack_ledger(self, line_id: str) -> list[dict[str, Any]]:
        """The ledger read back through the one discovery seam."""
        return RunArtifacts(
            self.runs / line_id, run_id="probe-read", folder_id=line_id
        ).read_line_message_acks()

    def rounds(self, line_id: str) -> list[dict[str, Any]]:
        return RunArtifacts(
            self.runs / line_id, run_id="probe-read", folder_id=line_id
        ).read_rounds()

    def face_line(self, line_id: str) -> dict[str, Any]:
        face = self.state_face_lines()
        matches = [entry for entry in face.get("lines", []) if entry.get("folder_id") == line_id]
        require(
            len(matches) == 1,
            f"/v1/lines 未覆盖靶线 {line_id}（响应 lines 数 {len(face.get('lines', []))}）",
        )
        return matches[0]

    def park_snapshot(self, line_id: str) -> dict[str, Any]:
        """The two-point snapshot pair: .scheduler fields + /v1/lines derived face."""
        stall = self.stall_state(line_id)
        terminal = self.terminal(line_id)
        face = self.face_line(line_id)
        return {
            "terminal": terminal.get("terminal"),
            "waiting_on": terminal.get("waiting_on"),
            "line_state": stall.get("line_state"),
            "parked": face.get("parked"),
            "face_terminal": face.get("terminal"),
            "face_waiting_on": (face.get("wake_facts") or {}).get("waiting_on"),
            # Context only (park identity advances r1→r2 across the event):
            "parked_run_id": stall.get("parked_run_id"),
            "decision_consumed_at": stall.get("dispatched_decision_consumed_at"),
        }


# --- check 15: the ack lands in the ledger and reads on the state face ------


def check_15() -> str:
    tmp = Path(tempfile.mkdtemp(prefix="lim-m4b-check15-"))
    try:
        stack = ProbeStack(tmp, ["wf-lim-selftest-ack"])
        line_id = stack.line_ids[0]
        sent_by = "wf-lim-selftest-supervisor"
        instruction_id = stack.deliver(
            line_id, "lim selftest instruction: report seat status", KIND_INSTRUCTION, sent_by
        )
        info_id = stack.deliver(line_id, "lim selftest info: FYI only", "info", sent_by)
        approve_id = stack.deliver(line_id, "APPROVE", KIND_INSTRUCTION, sent_by)

        scheduler = stack.scheduler()
        scheduler.tick()  # the priming schedule tick over the synthetic roster
        coordinator = stack.run_line_round(
            line_id,
            "lim-run-g1",
            [
                {
                    "verdict": "continue",
                    "next_prompt": f"lim selftest: acknowledge {instruction_id}",
                    "acks": [
                        {
                            "message_id": instruction_id,
                            "outcome": "executed",
                            "reason": "lim selftest receipt",
                        }
                    ],
                },
                {"verdict": "done", "reason": "lim selftest complete"},
            ],
        )
        scheduler.tick()  # the tick that accounts the run's terminal

        # 阴性②前置：the round actually saw all three messages.
        drained_ids = [m.get("message_id") for m in coordinator.drained_messages]
        require(
            drained_ids == [instruction_id, info_id, approve_id],
            f"round drain 与投递不一致: {drained_ids}",
        )

        # 1. The ledger, through the discovery seam, carries the contract shape.
        ledger = stack.ack_ledger(line_id)
        by_id = {row.get("message_id"): row for row in ledger}
        require(instruction_id in by_id, f"台账缺 instruction 回执行: {ledger}")
        require(approve_id in by_id, f"台账缺 bare-APPROVE 回执行: {ledger}")
        for row in ledger:
            missing = {"round", "at", "message_id", "outcome", "reason"} - set(row)
            require(not missing, f"台账行形状缺字段 {missing}: {row}")
        executed = by_id[instruction_id]
        require(
            executed.get("outcome") == "executed" and executed.get("kind") == KIND_INSTRUCTION,
            f"instruction 回执不是 executed: {executed}",
        )

        # 阴性①: the info message carries no ack row anywhere.
        require(info_id not in by_id, f"info 类消息不应有回执行: {by_id.get(info_id)}")

        # 阴性②: the bare decision token is pump-guard-rejected, never a verdict.
        guard_row = by_id[approve_id]
        require(
            guard_row.get("outcome") == "rejected"
            and guard_row.get("reason") == DECISION_GUARD_REASON,
            f"bare APPROVE 未被守卫拒绝: {guard_row}",
        )
        rounds = stack.rounds(line_id)
        mirror = [row for row in rounds if "line_message_acks" in row]
        require(bool(mirror), f"rounds.jsonl 无 line_message_acks 镜像行: {rounds}")
        mirrored_ids = {row.get("message_id") for row in mirror[0]["line_message_acks"]}
        require(
            mirrored_ids == {instruction_id, approve_id},
            f"镜像行回执集不对: {mirrored_ids}",
        )
        require(
            all("unacked_instructions" in row for row in mirror),
            "镜像行缺 unacked_instructions 字段",
        )

        # 2. The state face reads by message_id, latest first, over real HTTP.
        face = stack.face_line(line_id)
        acks = (face.get("wake_facts") or {}).get("line_message_acks")
        require(bool(acks), f"/v1/lines wake_facts 无 line_message_acks: {face}")
        face_ids = [row.get("message_id") for row in acks]
        require(
            face_ids[-1] == instruction_id and face_ids[0] == approve_id,
            f"state 面最新在前排序不对: {face_ids}",
        )
        require(info_id not in face_ids, "state 面出现 info 回执（阴性①红）")
        newest = acks[0]
        require(
            newest.get("message_id") == approve_id
            and newest.get("outcome") == "rejected"
            and newest.get("reason") == DECISION_GUARD_REASON,
            "state 面 bare-APPROVE 回执不是守卫拒绝行",
        )
        return (
            f"台账 2 行（instruction=executed, bare-APPROVE=rejected/{DECISION_GUARD_REASON}, "
            "info 零行）+ rounds 镜像在 + /v1/lines wake_facts.line_message_acks "
            f"最新在前且最新一条 message_id={str(approve_id)[:17]}… 机械比对一致"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- check 16: an inbox message alone never lifts waiting_decision -----------


def _run_parked_cycle(
    stack: ProbeStack, line_id: str, text: str, *, declare_ack: bool
) -> dict[str, Any]:
    """One full receipt event against a parked line; returns the evidence."""
    scheduler = stack.scheduler()
    scheduler.tick()  # priming: the line has not run yet
    stack.run_line_round(
        line_id,
        "lim-run-g1",
        [
            {
                "verdict": "blocked",
                "reason": "lim selftest: waiting on a human ruling",
                "waiting_on": "decision",
            }
        ],
    )
    parked = scheduler.tick()
    require(parked[0].parked, f"{line_id} 驻停未建立: {parked[0].park_event}")
    before = stack.park_snapshot(line_id)
    require(before["parked"] is True, f"{line_id} BEFORE 快照非驻停: {before}")
    require(
        before["waiting_on"] == "decision" and before["line_state"] == "waiting_decision",
        f"{line_id} BEFORE 快照不是 waiting_decision: {before}",
    )

    message_id = stack.deliver(line_id, text, KIND_INSTRUCTION, "wf-lim-selftest-supervisor")

    woken = scheduler.tick()  # the receipt tick: woken:inbox, bookkeeping only
    require(
        woken[0].park_event == "woken:inbox",
        f"{line_id} 收信 tick 未发生 inbox 唤醒: {woken[0].park_event}",
    )

    acks: list[dict[str, Any]] = (
        [
            {
                "message_id": message_id,
                "outcome": "executed",
                "reason": "lim selftest receipt",
            }
        ]
        if declare_ack
        else []
    )
    stack.run_line_round(
        line_id,
        "lim-run-g2",
        [
            {
                "verdict": "blocked",
                "reason": "lim selftest: still no ruling, inbox noted",
                "waiting_on": "decision",
                "acks": acks,
            }
        ],
    )
    settled = scheduler.tick()  # the tick that re-establishes the park on g2
    require(settled[0].parked, f"{line_id} 事件后未再驻停: {settled[0].park_event}")
    after = stack.park_snapshot(line_id)
    return {
        "before": before,
        "after": after,
        "message_id": message_id,
        "wake_event": woken[0].park_event,
    }


def check_16() -> str:
    tmp = Path(tempfile.mkdtemp(prefix="lim-m4b-check16-"))
    try:
        evidence: list[str] = []
        # Two targets: a plain instruction, and the decision-shaped bare
        # "APPROVE" — the strongest possible mail must still not lift the park.
        plans = [
            ("wf-lim-selftest-park-a", "lim selftest instruction: note the mail", True),
            ("wf-lim-selftest-park-b", "APPROVE", False),
        ]
        for index, (line_id, text, declare_ack) in enumerate(plans):
            # The stack is rebuilt per target so each event runs against a
            # pristine scheduler state; runs never share bookkeeping.
            stack = ProbeStack(tmp / f"target{index}", [line_id])
            result = _run_parked_cycle(stack, line_id, text, declare_ack=declare_ack)
            before, after = result["before"], result["after"]
            diff = {
                field: (before.get(field), after.get(field))
                for field in INVARIANT_FIELDS
                if before.get(field) != after.get(field)
            }
            require(
                not diff,
                f"{line_id} 驻停字段 diff 非空（仅 inbox 不解除 waiting_decision 被驳）: {diff}",
            )
            require(after["parked"] is True, f"{line_id} AFTER 非驻停: {after}")
            require(
                after["waiting_on"] == "decision" and after["line_state"] == "waiting_decision",
                f"{line_id} AFTER 不是 waiting_decision: {after}",
            )
            # No decision fact ever appeared on the stall face.
            require(
                before["decision_consumed_at"] is None and after["decision_consumed_at"] is None,
                f"{line_id} 驻停面出现裁决事实: {after['decision_consumed_at']}",
            )
            # The receipt face did its job across the very same event.
            ledger = stack.ack_ledger(line_id)
            rows = [row for row in ledger if row.get("message_id") == result["message_id"]]
            require(len(rows) == 1, f"{line_id} 事件后台账无该消息回执行: {ledger}")
            if declare_ack:
                require(
                    rows[0].get("outcome") == "executed", f"{line_id} 回执不是 executed: {rows[0]}"
                )
            else:
                require(
                    rows[0].get("outcome") == "rejected"
                    and rows[0].get("reason") == DECISION_GUARD_REASON,
                    f"{line_id} bare-APPROVE 回执不是守卫拒绝: {rows[0]}",
                )
            require(
                before["parked_run_id"] == "lim-run-g1" and after["parked_run_id"] == "lim-run-g2",
                f"{line_id} 驻停身份未随新 run 前进: "
                f"{before['parked_run_id']} → {after['parked_run_id']}",
            )
            evidence.append(
                f"{line_id.split('park-')[-1]}: diff=∅, wake={result['wake_event']}, "
                f"ack={rows[0].get('outcome')}"
            )
        return (
            "两条靶线（instruction / bare-APPROVE）完整收信事件 tick 前后驻停快照字段 diff 为空"
            "（waiting_decision 未解除、无任何裁决事实），且 wake=inbox 唤醒与台账回执均在："
            + "; ".join(evidence)
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


CHECKS = {"15": check_15, "16": check_16}


def main() -> int:
    parser = argparse.ArgumentParser(description="verify-lim check 15/16 现场合成靶探针")
    parser.add_argument("--check", required=True, choices=sorted(CHECKS))
    args = parser.parse_args()
    check = str(args.check).zfill(2)
    try:
        evidence = CHECKS[check]()
    except ProbeError as exc:
        print(f"M4B-PROBE {check} FAIL — {exc}")
        return 1
    except Exception as exc:
        detail = " ".join(str(exc).split())[:300]
        frame = traceback.extract_tb(sys.exc_info()[2])[-1]
        print(f"M4B-PROBE {check} FAIL — {type(exc).__name__} at {frame.name}: {detail}")
        return 1
    print(f"M4B-PROBE {check} PASS — {evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
