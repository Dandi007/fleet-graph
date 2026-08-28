"""The localhost dev-dispatch MCP surface. The service *is* the control plane.

The supervision plane struck the separate graph-API tier this surface used to
forward to (:5611): there is no second service behind these tools. Every real
tool drives `fleet_graph.dd.control_plane` in-process -- admission derivation,
transient-unit launches, and read-side assembly from git + checkpoint + run
artifacts all happen right here.

Tool surface (wf-a08949 goal.md 2026-08-27 use-case-family ruling; wf-13ff9e
plan.md §1 R1-d, extended by R1-c): the consumed use-case family does work --
``development_list / get / events / evidence / create / start / gate /
reconfigure``.  ``reconfigure`` is the R1-c environment/contract failure exit:
on the legacy engine it existed in name but was a permanent 409 once a
development FAILED; here it is real, scoped by schema to the acceptance
context alone, and pairs with ``start`` launching a fresh generation.  The
remaining legacy tool names stay registered so every historical caller gets an
explicit, machine-readable ``NOT_SUPPORTED`` refusal instead of an unknown-tool
error, but they perform no work: ``steer`` was a permanent 409 on the legacy
engine and is not replicated; ``relock`` / ``control`` / ``deployment_*``
belong to the legacy engine's patch surface and are outside the equivalence
scope.

Two contracts the tools themselves enforce:

- **Admission is server-side derivation.** ``development_create`` takes a repo
  path, a target base, and the spec. There is no handoff parameter, no digest
  parameter, no receipt parameter -- the whole vocabulary a client used to
  have to guess is derived by the server and returned, not requested.
- **The gate carries no verdict.** ``development_gate`` reports the pending
  question note and offers a valueless ``resume``; on resume the graph
  re-reads the board itself. Decisions travel only as ``work.decision.v1`` on
  the bus, published by a human.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Any

from fleet_graph.dd.control_plane import (
    STATE_AWAITING_GATE,
    ControlPlaneError,
    DdControlPlane,
)

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5610

#: R4-3 收尾：决议落板自动 resume 巡检的开关与间隔（默认开、60s 级）。
#: 环境变量供 systemd unit 配置；CLI `dd serve --no-auto-resume /
#: --auto-resume-interval` 覆盖环境变量。
AUTO_RESUME_ENABLED_ENV = "FLEET_GRAPH_DD_AUTO_RESUME"
AUTO_RESUME_INTERVAL_ENV = "FLEET_GRAPH_DD_AUTO_RESUME_INTERVAL"
DEFAULT_AUTO_RESUME_INTERVAL = 60.0

_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def auto_resume_enabled_from_env(environ: dict[str, str] | None = None) -> bool:
    """默认开；只有显式的否定词才关（unset/空串/其它值都算开）。"""
    raw = (environ if environ is not None else os.environ).get(AUTO_RESUME_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSE_WORDS


def auto_resume_interval_from_env(environ: dict[str, str] | None = None) -> float:
    """巡检间隔秒数；非法或非正值回落默认 60s，不让配置错拖垮服务。"""
    raw = (environ if environ is not None else os.environ).get(AUTO_RESUME_INTERVAL_ENV)
    if raw is None:
        return DEFAULT_AUTO_RESUME_INTERVAL
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_AUTO_RESUME_INTERVAL
    return value if value > 0 else DEFAULT_AUTO_RESUME_INTERVAL


class GateAutoResumer:
    """决议落板自动 resume：消掉第四道闸链路里最后一个人工步骤。

    dd development 不是 scheduler line，R0c 的停牌唤醒三源不覆盖它；而
    "The service IS the control plane"——所以这套巡检长在 dd serve 自己身上，
    不新增第二个 daemon，也不与 supervisor 耦合（supervisor 只发 decision，
    谁消费它不知道）。

    纪律（全部复用既有路径，不引入新裁决逻辑）：

    - **判定只读**：扫 awaiting_gate 用 ``DdControlPlane.list``（O(n) run
      artifacts，既有裁定的代价）；"决议是否已落板" 读的是
      ``DdControlPlane.gate`` 报告里的 ``decision_on_board``——与人工调
      development_gate 看到的是同一条板读逻辑（``_decision_on_board``）。
    - **启动即 development_gate(resume=True)**：同一个函数，同一条
      ``_launch(resume=True)`` 路径，resume 依旧无值——图内自己重读板，
      巡检无法夹带任何 verdict（第二道闸语义原样保留）。
    - **fail-open**：板不可达时 ``decision_on_board`` 为 None，按 "尚无决议"
      跳过；单个 development 判定/启动异常只记日志跳过；整个 tick 的意外
      异常也只记日志——巡检永不拖垮 MCP 面。
    - **幂等**：running 的 development 根本不在 awaiting_gate 扫描结果里；
      判定与启动之间的竞态由 ``gate(resume=True)`` 自身的 ALREADY_RUNNING
      refuse 兜住（launch 层既有判定），巡检把它当跳过处理。
    """

    def __init__(
        self,
        control: Any,
        *,
        interval: float = DEFAULT_AUTO_RESUME_INTERVAL,
        page_size: int = 20,
    ) -> None:
        self.control = control
        self.interval = interval
        self.page_size = page_size
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- one pass ---------------------------------------------------------

    def tick(self) -> dict[str, Any]:
        """一轮巡检：扫 awaiting_gate、判 decision_on_board、resume。"""
        summary: dict[str, Any] = {"scanned": 0, "resumed": [], "skipped": [], "errors": []}
        try:
            awaiting = self._awaiting_ids()
        except Exception as exc:  # fail-open：扫描失败只跳过本轮
            logger.warning("dd auto-resume: development scan failed, skipping tick: %s", exc)
            summary["errors"].append({"development_id": None, "error": str(exc)})
            return summary
        for development_id in awaiting:
            summary["scanned"] += 1
            try:
                self._consider(development_id, summary)
            except ControlPlaneError as exc:
                # 判定与启动之间状态变了（如 ALREADY_RUNNING）：既有 refuse
                # 就是幂等保护，按跳过处理。
                logger.info(
                    "dd auto-resume: %s skipped by the control plane: %s", development_id, exc.code
                )
                summary["skipped"].append({"development_id": development_id, "reason": exc.code})
            except Exception as exc:  # fail-open：单个 development 异常不崩巡检
                logger.warning("dd auto-resume: %s raised, skipping: %s", development_id, exc)
                summary["errors"].append({"development_id": development_id, "error": str(exc)})
        return summary

    def _awaiting_ids(self) -> list[str]:
        """既有 development_list 的 O(n) 扫描，翻页取全量 awaiting_gate。"""
        ids: list[str] = []
        cursor: str | None = None
        while True:
            page = self.control.list(state=STATE_AWAITING_GATE, limit=self.page_size, cursor=cursor)
            ids.extend(str(row["development_id"]) for row in page.get("developments") or [])
            cursor = page.get("cursor")
            if not cursor:
                break
        return ids

    def _consider(self, development_id: str, summary: dict[str, Any]) -> None:
        report = self.control.gate(development_id)  # 只读：与人工看闸完全同一逻辑
        if report.get("state") != STATE_AWAITING_GATE or not report.get("pending"):
            summary["skipped"].append({"development_id": development_id, "reason": "not_pending"})
            return
        if report.get("decision_on_board") is not True:
            # False = 决议未落板；None = 板不可达（fail-open，当作未落板）。
            summary["skipped"].append(
                {"development_id": development_id, "reason": "no_decision_on_board"}
            )
            return
        resumed = self.control.gate(development_id, resume=True)  # 同一条 resume 路径
        summary["resumed"].append(development_id)
        logger.info(
            "dd auto-resume: decision on board, resumed %s as %s",
            development_id,
            (resumed.get("resume") or {}).get("unit", ""),
        )

    # --- lifecycle: 随 dd serve 起、随进程止 ------------------------------

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # tick 已自吞异常，这里是最后一道保险
                logger.exception("dd auto-resume: tick crashed; the patrol continues")
            if self._stop.wait(self.interval):
                break

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run_forever, name="dd-gate-auto-resume", daemon=True)
        self._thread = thread
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()


# Legacy tool names that are registered but refuse with an explicit error
# structure instead of pretending the legacy semantics exist here.
# name -> reason, quoted in the refusal payload.
NOT_SUPPORTED_TOOLS: dict[str, str] = {
    "development_steer": ("steer was a permanent 409 on the legacy engine and is not replicated"),
    "development_relock": "relock belongs to the legacy engine's patch surface",
    "development_control": (
        "control is outside the consumed use-case family "
        "(create/start/get/list/events/evidence/gate)"
    ),
    "deployment_create": "deployment_* belongs to the legacy engine's patch surface",
    "deployment_status": "deployment_* belongs to the legacy engine's patch surface",
}

NOT_SUPPORTED_RULING = "wf-a08949 goal.md 2026-08-27 use-case-family ruling"

# The consumed use-case family: the only tools that do real work.
SUPPORTED_TOOLS: frozenset[str] = frozenset(
    {
        "development_list",
        "development_get",
        "development_events",
        "development_evidence",
        "development_create",
        "development_start",
        "development_gate",
        "development_reconfigure",
        "development_adopt",
        "development_recover",
    }
)


def port_is_available(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    """Bind-test the selected loopback port before FastMCP tries to serve it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def build_mcp_server(plane: DdControlPlane | None = None) -> Any:
    """Build all active dev-dispatch tools over the in-process control plane."""
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError

    control = plane or DdControlPlane()
    mcp = FastMCP("fleet-graph-dev-dispatch")

    def call(method: str, /, **kwargs: Any) -> dict[str, Any]:
        try:
            return dict(getattr(control, method)(**kwargs))
        except ControlPlaneError as exc:
            raise ToolError(json.dumps(exc.to_dict(), sort_keys=True)) from exc

    def refuse(tool: str) -> dict[str, Any]:
        """Raise the explicit NOT_SUPPORTED structure for a legacy-only tool."""
        raise ToolError(
            json.dumps(
                {
                    "code": "NOT_SUPPORTED",
                    "tool": tool,
                    "reason": NOT_SUPPORTED_TOOLS[tool],
                    "ruling": NOT_SUPPORTED_RULING,
                    "supported_tools": sorted(SUPPORTED_TOOLS),
                },
                sort_keys=True,
            )
        )

    @mcp.tool()
    def deployment_create(request: dict[str, Any]) -> dict[str, Any]:
        """NOT_SUPPORTED: legacy patch-surface tool, refuses explicitly."""
        return refuse("deployment_create")

    @mcp.tool()
    def deployment_status(operation_id: str) -> dict[str, Any]:
        """NOT_SUPPORTED: legacy patch-surface tool, refuses explicitly."""
        return refuse("deployment_status")

    @mcp.tool()
    def development_list(
        state: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List development statuses. O(n) over the run artifacts, by ruling."""
        return call("list", state=state, limit=limit, cursor=cursor)

    @mcp.tool()
    def development_get(development_id: str) -> dict[str, Any]:
        """One development's admission record plus its recomputed live status."""
        return call("get", development_id=development_id)

    @mcp.tool()
    def development_events(
        development_id: str,
        after: str | None = None,
        limit: int = 100,
        generation: int | None = None,
    ) -> dict[str, Any]:
        """One generation's event log (events.jsonl), paged by event id.

        Defaults to the current generation; pass `generation` to read an
        earlier one's history.
        """
        return call(
            "events",
            development_id=development_id,
            after=after,
            limit=limit,
            generation=generation,
        )

    @mcp.tool()
    def development_evidence(development_id: str) -> dict[str, Any]:
        """The evidence entry, assembled live from git + checkpoint + receipts."""
        return call("evidence", development_id=development_id)

    @mcp.tool()
    def development_create(
        repo_path: str,
        target_base: str | None = None,
        spec_text: str | None = None,
        spec_path: str | None = None,
    ) -> dict[str, Any]:
        """Admit one development. Everything else is derived server-side.

        Takes a dedicated git worktree (or clone) path, an optional target
        base (defaults to the repo's HEAD), and the approved spec as text or
        as a path. The server derives the development id, freezes the spec
        and target base into the bootstrap commit, computes the H0 handoff
        and its chain-root digest, derives the durable ref and the acceptance
        argv (from the spec's ```dd-acceptance block), and publishes the work
        board card. Idempotent for the same (repo, spec, base).
        """
        return call(
            "create",
            repo_path=repo_path,
            target_base=target_base,
            spec_text=spec_text,
            spec_path=spec_path,
        )

    @mcp.tool()
    def development_start(development_id: str) -> dict[str, Any]:
        """Run the development detached in a transient systemd unit.

        The thread identity is `{development_id}:g{generation}`: starting
        again after a kill resumes the same generation's thread and re-adopts
        agent runs in flight instead of re-dispatching sealed stages, while
        starting after a retryable terminal (or after a reconfigure) launches
        the next generation fresh -- new thread id, new derived run ids, new
        gate idempotency key, so a rerun never collides with its own past. A
        fabrication terminal refuses (final), and so does `complete`.
        Starting a development that is already running is a no-op that says
        so.
        """
        return call("start", development_id=development_id)

    @mcp.tool()
    def development_gate(development_id: str, resume: bool = False) -> dict[str, Any]:
        """The human gate's state; optionally resume the suspended thread.

        This tool accepts **no decision**. It reports the question note the
        gate is waiting on, and `resume=True` re-enters the suspended thread
        with no input at all -- the gate re-reads the board itself, so the
        caller cannot cast a verdict by resuming. Decisions travel only as
        `work.decision.v1` messages on the bus, published by a human, with
        `refs=[{"target_entity": <question_note_id>}]`.
        """
        return call("gate", development_id=development_id, resume=resume)

    @mcp.tool()
    def development_adopt(development_id: str, discoveries: list[dict[str, str]]) -> dict[str, Any]:
        """Adopt discovered in-flight/recoverable work into the governed workflow.

        Each discovery is ``{signature, kind, source, target_ref}``. The
        not-yet-adopted subset is adopted and the rest is skipped; replaying the
        same batch is idempotent, so a replayed discovery cannot duplicate
        adopted work or fork its history.
        """
        return call("adopt", development_id=development_id, discoveries=discoveries)

    @mcp.tool()
    def development_recover(
        development_id: str,
        target_ref: str = "",
        question_note_id: str = "",
    ) -> dict[str, Any]:
        """Record a human recovery decision and resume suspended work only from it.

        The tool carries no verdict: it reads the human's decision for the
        question note off the board (the governance path), seals it with its
        immutable target reference into the recovery trail, and resumes the
        suspended work from that recorded decision alone. No board decision
        means no recovery, so this never becomes a bypass around the gate.
        """
        return call(
            "recover",
            development_id=development_id,
            target_ref=target_ref,
            question_note_id=question_note_id,
        )

    @mcp.tool()
    def development_steer(
        development_id: str,
        instruction: str,
        idempotency_key: str,
        expected_revision: int,
        reason: str = "",
        urgency: str = "next_safe_boundary",
    ) -> dict[str, Any]:
        """NOT_SUPPORTED: permanent 409 on the legacy engine, refuses explicitly."""
        return refuse("development_steer")

    @mcp.tool()
    def development_reconfigure(
        development_id: str,
        acceptance_env: dict[str, str] | None = None,
        acceptance_argv: list[str] | None = None,
        setup: list[str] | None = None,
    ) -> dict[str, Any]:
        """Change a development's acceptance context -- and nothing else.

        The environment/contract failure exit (R1-c): callable while the
        development is FAILED and in every non-terminal state, so an
        acceptance environment problem (missing piece, wrong acceptance argv,
        missing setup) no longer kills the development the way the legacy
        engine's permanent 409 did. After reconfiguring, `development_start`
        launches a fresh generation with the new context.

        The scope is the schema: `acceptance_env` (env overlay for setup and
        acceptance commands), `acceptance_argv` (acceptance command lines,
        shell quoting honoured), `setup` (setup command lines run first).
        There is no spec parameter and no implementation parameter -- the
        spec stays frozen under its bootstrap digest, and a changed spec is a
        new development. A fabrication terminal (UNVERIFIED_TEST_CLAIM
        family) refuses: that exit is final.
        """
        return call(
            "reconfigure",
            development_id=development_id,
            acceptance_env=acceptance_env,
            acceptance_argv=acceptance_argv,
            setup=setup,
        )

    @mcp.tool()
    def development_control(
        development_id: str,
        action: str,
        idempotency_key: str,
        expected_revision: int,
        reason: str = "",
    ) -> dict[str, Any]:
        """NOT_SUPPORTED: outside the consumed use-case family, refuses explicitly."""
        return refuse("development_control")

    @mcp.tool()
    def development_relock(
        development_id: str,
        plugin_commit: str,
        idempotency_key: str,
        expected_revision: int,
        reason: str = "",
    ) -> dict[str, Any]:
        """NOT_SUPPORTED: legacy patch-surface tool, refuses explicitly."""
        return refuse("development_relock")

    return mcp


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    root: str | None = None,
    plugin_binding: str | None = None,
    working_directory: str | None = None,
    executable: str | None = None,
    stage_models: dict[str, str] | None = None,
    auto_resume: bool | None = None,
    auto_resume_interval: float | None = None,
) -> None:
    if not port_is_available(host, port):
        raise RuntimeError(f"fleet-graph dev-dispatch port {host}:{port} is unavailable")
    overrides: dict[str, Any] = {}
    if root:
        overrides["root"] = Path(root)
    if plugin_binding:
        overrides["plugin_binding"] = Path(plugin_binding)
    if working_directory:
        overrides["working_directory"] = working_directory
    if executable:
        overrides["executable"] = executable
    if stage_models:
        overrides["stage_models"] = stage_models
    control = DdControlPlane(**overrides)
    # R4-3 收尾：决议落板自动 resume 巡检随服务生命周期启停（daemon 线程，
    # 进程退出即止）。None = 未显式配置，回落环境变量，默认开。
    enabled = auto_resume_enabled_from_env() if auto_resume is None else auto_resume
    interval = (
        auto_resume_interval_from_env() if auto_resume_interval is None else auto_resume_interval
    )
    resumer: GateAutoResumer | None = None
    if enabled:
        resumer = GateAutoResumer(control, interval=interval)
        resumer.start()
        logger.info("dd auto-resume patrol started (interval %.0fs)", interval)
    else:
        logger.info("dd auto-resume patrol disabled by configuration")
    try:
        build_mcp_server(control).run(
            transport="streamable-http", host=host, port=port, path="/mcp"
        )
    finally:
        if resumer is not None:
            resumer.stop()


__all__ = [
    "AUTO_RESUME_ENABLED_ENV",
    "AUTO_RESUME_INTERVAL_ENV",
    "DEFAULT_AUTO_RESUME_INTERVAL",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "NOT_SUPPORTED_RULING",
    "NOT_SUPPORTED_TOOLS",
    "SUPPORTED_TOOLS",
    "GateAutoResumer",
    "auto_resume_enabled_from_env",
    "auto_resume_interval_from_env",
    "build_mcp_server",
    "port_is_available",
    "serve",
]
