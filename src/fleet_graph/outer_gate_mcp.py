"""R5 外门 MCP —— 运行时状态与运维动作的单一 MCP 门（外门收敛）.

判据锚：specs/r5-outer-gate-mcp.md（wf-4601c8 R5）。监督面与线对引擎的
public interface 只剩 MCP：本面（``fleet-graph outer-gate serve``，:5616，
注册名 ``fleet-graph-outer-gate``）承载九个运行时工具——

- 读四件：``state_lines``（名册+线状态总览，含 R4 的 release_behind /
  deploy_behind）、``state_line``（单线详情）、``state_decisions``
  （裁决台账）、``state_takeover``（零上下文接手：六项一次调用齐）。
- 写四件：``line_revive / line_set_seat / maintenance_set / maintenance_clear``
  ——监督者 principal 专属（与 R2 ``development_create`` 外门同族鉴权），
  非监督者稳定拒绝+留痕。
- 挂 note 一件：``note_publish``（监督者与卡主本人；``work.note.v1``
  载荷与 refs 语义逐字段对齐，MCP 是门、bus 是载体）。

``:7494`` HTTP 与 CLI（``fleet-graph line``、``fleet-maint``）降为实现细节：
HTTP 保留只读 GET 供既有探针（R0 判据 03/05 等），写面与管理动作从调用面
语义里移除——写经 MCP。外门=监督者操作面+全体只读面；线的派单/批 gate
仍走图内路径（R2/R3），不经此门。

宪法第九条（失败现形）：一切拒绝带稳定拒绝码；``state_takeover`` 六项中
不可得项显式标注（``unavailable``+原因），不得省略键、不得以旧缓存冒充
现算（每项带 ``computed_at``）；上游死地址时 ``tools/list`` 仍应答、相关
工具报 ``upstream_unavailable``，禁静默空转或假数据。

S7 边界：只读视图复用 wf-525fd4 的既有投影（``FleetStateView``），不重做
读取器；``state_*`` 读四件只是同一投影上的窄工具封装。
"""

from __future__ import annotations

import calendar
import contextlib
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fleet_graph.state.fleet_state import (
    DEFAULT_RUN_ROOT,
    FleetStateConfig,
    FleetStateView,
)

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
#: 空闲端口（2026-09-05 探测：25612/5616 均空；R2 端口纪律同族——默认端口
#: 不得落在任一 ``config/*-reserved-ports.json`` 清单里）。
DEFAULT_PORT = 5616

#: FastMCP 注册名（tools/list 的 server name，与其余面互斥可辨）。
MCP_SERVER_NAME = "fleet-graph-outer-gate"

#: 生产状态根（``fleet-graph outer-gate serve`` 无参时的默认绑定，与 :7494
#: 读模型同源）。
DEFAULT_DD_ROOT = "/data/fleet-graph/dd"
DEFAULT_BRIDGE_STATE_DIR = "/data/fleet-graph/decision-bridge"
DEFAULT_LINES_CONFIG = "config/ronin-lines.json"

#: 与 R2 ``development_create`` 外门同族鉴权：监督者 principal 专属。
#: 环境变量仅供部署绑定监督面身份，测试可替换。
SUPERVISOR_PRINCIPAL_ENV = "FLEET_GRAPH_SUPERVISOR_PRINCIPAL"
SUPERVISOR_PRINCIPAL_DEFAULT = "fleet-supervisor"

#: 拒绝码（closed）。一切拒绝带稳定码，无静默成功。
CODE_NOT_SUPERVISOR = "OUTER_GATE_NON_SUPERVISOR"
CODE_LINE_NOT_FOUND = "LINE_NOT_FOUND"
CODE_LINE_UNKNOWN = "LINE_UNKNOWN"
CODE_UPSTREAM_UNAVAILABLE = "upstream_unavailable"
CODE_NOTE_FORBIDDEN = "NOTE_FORBIDDEN"
CODE_NOTE_REFS_REQUIRED = "refs_required"
CODE_NOTE_REFUSAL = "NOTE_REFUSED"
#: 留痕文件名（相对 root；与 dd 面 ``outer-gate-refusals.jsonl`` 同族）。
OUTER_GATE_REFUSALS_FILE = "outer-gate-refusals.jsonl"

#: ``note_publish`` 允许的 note_type，与 bus ``work.note.v1`` 注册 schema 的
#: enum 逐字段一致（measured 2026-09-05：progress/finding/question/handoff/
#: evidence）。
NOTE_TYPES = ("progress", "finding", "question", "handoff", "evidence")

#: ``state_takeover`` 六项的封闭键表。缺项不许省略键：不可得项以
#: ``unavailable`` 显式标注。
TAKEOVER_KEYS = (
    "roster",
    "line_states",
    "awaiting_decisions",
    "pending_releases",
    "auth_mode",
    "current_release",
)

#: ``maintenance_set`` 的 gate 载荷与默认有效期（过期即失活，沿用
#: ``SchedulerDaemon._gate_expired`` 的 v23 裁决语义）。
MAINTENANCE_STOP_FILE = "maintenance-stop"
MAINTENANCE_DEFAULT_TTL_S = 3600


def supervisor_principal(environ: dict[str, str] | None = None) -> str:
    """The one principal the outer gate admits (env-bindable, default fixed)."""
    return (environ if environ is not None else os.environ).get(
        SUPERVISOR_PRINCIPAL_ENV
    ) or SUPERVISOR_PRINCIPAL_DEFAULT


def _iso(ts: float | None = None) -> str:
    stamp = time.time() if ts is None else ts
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))


def trace_outer_gate_refusal(
    root: Path | str | None,
    *,
    tool: str,
    principal: str,
    supervisor: str,
    detail: str = "",
    at: float | None = None,
) -> None:
    """Append one durable refusal row (留痕). Best effort by contract."""
    if root is None:
        return
    row = json.dumps(
        {
            "code": CODE_NOT_SUPERVISOR,
            "tool": tool,
            "principal": principal,
            "supervisor": supervisor,
            "detail": detail,
            "at": _iso(at),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    try:
        path = Path(root) / OUTER_GATE_REFUSALS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(row + "\n")
    except OSError:
        pass


class OuterGateUnavailable(RuntimeError):
    """An upstream dependency is unreachable: honest ``upstream_unavailable``.

    ``tools/list`` still answers; the tool answers with the refusal instead of
    fabricated data or a silent empty result (spec 阴性 4).
    """


def _takeover_ok(item: Any, when: str) -> dict[str, Any]:
    """A freshly computed takeover item: present with its computed_at."""
    return {"computed_at": when, "data": item}


def _takeover_unavailable(reason: str, when: str) -> dict[str, Any]:
    """An unavailable takeover item, explicitly marked (never a missing key)."""
    return {"computed_at": when, "unavailable": True, "reason": reason}


def _cached_takeover(data: Any, computed_at: str, when: str) -> dict[str, Any]:
    """A cached item that stays honest: the cache stamp travels with it."""
    return {"computed_at": when, "data": data, "cached": True, "cached_computed_at": computed_at}


def takeover_item_complete(item: Any) -> bool:
    """Is a takeover item honestly complete (present, fresh, no failure mark)?

    缺项不算齐（阴性 A/C）：缺 ``computed_at``、``computed_at`` 为空、
    ``data`` 缺失/为 ``None``、带 ``unavailable``/``error``/``failed`` 标注、
    或任何形态的缓存项缺 ``cached_computed_at``——都读「不齐」，调用方必须
    能看到缺什么、新不新。
    """
    if not isinstance(item, dict):
        return False
    if item.get("unavailable") or item.get("error") or item.get("failed"):
        return False
    if not item.get("computed_at"):
        return False
    if item.get("cached") and not item.get("cached_computed_at"):
        return False
    return item.get("data") is not None


def takeover_keys() -> tuple[str, ...]:
    """The closed six-key surface of the zero-context takeover."""
    return TAKEOVER_KEYS


def takeover_items(
    view: FleetStateView, when: str, *, run_root: Path | None = None
) -> dict[str, Any]:
    """The six takeover items from one view, item-wise honest.

    Shared by the ``state_takeover`` MCP tool and the :7494 ``/v1/takeover``
    GET (one projection, two doors -- never a second reader). Each item is
    freshly computed with its ``computed_at``; an unavailable source is an
    explicit ``unavailable`` mark with its reason (never an omitted key,
    never stale cache passed off as fresh).
    """
    items: dict[str, Any] = {}
    readers = (
        ("roster", lambda: view.roster()),
        ("line_states", lambda: view.lines()),
        ("awaiting_decisions", lambda: view.awaiting_decisions()),
        ("pending_releases", lambda: view.pending_releases()),
        ("auth_mode", lambda: view.auth_mode()),
        ("current_release", lambda: view.current_release(run_root)),
    )
    for key, read in readers:
        try:
            items[key] = _takeover_ok(read(), when)
        except Exception as exc:
            items[key] = _takeover_unavailable(str(exc), when)
    return items


def _enrich(view: FleetStateView, line: dict[str, Any]) -> dict[str, Any]:
    """One line + its dispatch projections (the takeover's per-line block)."""
    folder = str(line.get("folder_id") or "")
    return {
        "line": line,
        "dispatches": view.recent_dispatches(folder, limit=10),
    }


def build_outer_gate_mcp_server(
    state_config: FleetStateConfig,
    dd_root: Path,
    *,
    supervisor_identity_check: Callable[[str], bool] | None = None,
    revive: Callable[..., Any] | None = None,
    set_seat: Callable[..., Any] | None = None,
    maintenance_gate: Any | None = None,
    note_publisher: Any | None = None,
    card_owner: Callable[[str], str | None] | None = None,
    clock: Callable[[], float] = time.time,
    refusal_root: Path | str | None = None,
) -> Any:
    """Build the outer-gate MCP surface (读四件+写四件+note_publish).

    ``state_config`` binds the read-model data sources (same roots the :7494
    face serves); ``dd_root`` binds the dd run artifacts. All write machinery
    is injectable so tests drive the surface against scratch roots; the
    production defaults are the exact primitives the CLI implements (写经
    MCP 之后 CLI 降为实现细节).

    ``card_owner(card_entity_id) -> principal | None`` is the card-ownership
    authority for ``note_publish`` (监督者与卡主本人). The production default
    resolves ownership from the dd tree's ``record.json`` (``card_entity_id``
    → ``dispatched_by``, the dispatched_by line that owns its own card); the
    supervisory plane may bind a richer board-backed authority.
    """
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError

    mcp = FastMCP(MCP_SERVER_NAME)
    view = FleetStateView(state_config)
    check_supervisor = supervisor_identity_check
    owner_resolver = card_owner or _default_card_owner(Path(dd_root))

    def now_iso() -> str:
        return _iso(clock())

    def refuse(code: str, message: str, *, tool: str = "") -> None:
        payload: dict[str, Any] = {"code": code, "message": message}
        if tool:
            payload["tool"] = tool
        raise ToolError(json.dumps(payload, sort_keys=True, ensure_ascii=False))

    def require_supervisor(tool: str, principal: str) -> str:
        """监督者专属闸：非监督者稳定拒绝+留痕，拒绝码写进回执."""
        supervisor = supervisor_principal()
        identity = (principal or "").strip()
        if check_supervisor is not None:
            ok = check_supervisor(identity)
        else:
            ok = identity == supervisor and identity != ""
        if not ok:
            trace_outer_gate_refusal(
                refusal_root,
                tool=tool,
                principal=identity,
                supervisor=supervisor,
                detail="non-supervisor principal refused",
                at=clock(),
            )
            refuse(
                CODE_NOT_SUPERVISOR,
                f"{tool} is supervisor-only: principal {identity!r} is not the "
                f"supervisor principal {supervisor!r}",
                tool=tool,
            )
        return supervisor

    # --- 读四件 -----------------------------------------------------------

    @mcp.tool()
    def state_lines() -> dict[str, Any]:
        """Roster + every line's runtime state (read-only).

        名册+线状态总览，与 :7494 ``GET /v1/lines`` 同一视图函数、同一数据
        源（S7：复用既有投影，不重做读取器）。字段面含 R4 的
        ``release_behind / deploy_behind``（一等字段透出）。上游（名册 SSoT）
        不可达时显式报 ``upstream_unavailable``——死地址必须告警，禁静默
        空转或假数据（10 项阴性半边；:7494 GET 侧的 fail-soft 降级兼容是
        探针读数面的既有形态，MCP 工具侧按宪法第九条现形）。
        """
        try:
            view.roster()  # the roster SSoT must be freshly readable to answer
        except Exception as exc:
            raise ToolError(
                json.dumps(
                    {"code": CODE_UPSTREAM_UNAVAILABLE, "message": str(exc)},
                    sort_keys=True,
                )
            ) from exc
        return view.lines()

    @mcp.tool()
    def state_line(line_id: str) -> dict[str, Any]:
        """One line's detail: state row + its dispatch projections (read-only).

        单线详情：线状态行（含 R4 字段）+该线的派单投影（
        ``recent_dispatches``）。``LINE_NOT_FOUND`` 拒绝显式回码。
        """
        try:
            lines = view.lines().get("lines") or []
        except Exception as exc:
            raise ToolError(
                json.dumps(
                    {"code": CODE_UPSTREAM_UNAVAILABLE, "message": str(exc)},
                    sort_keys=True,
                )
            ) from exc
        for row in lines:
            if str(row.get("folder_id") or "") == line_id:
                return _enrich(view, row)
        refuse(CODE_LINE_NOT_FOUND, f"no such line: {line_id!r} is not a roster line")

    @mcp.tool()
    def state_decisions(window: int = 86400) -> dict[str, Any]:
        """The decision ledger inside ``window`` seconds (read-only).

        裁决台账（:7494 ``/v1/decisions`` 同源投影）按窗口过滤。窗口参数
        非法在调用点拒绝。
        """
        try:
            window = int(window)
        except (TypeError, ValueError):
            refuse("WINDOW_INVALID", f"window must be an integer, got {window!r}")
        if window < 0:
            refuse("WINDOW_INVALID", f"window must be >= 0, got {window}")
        cutoff = clock() - window
        payload = view.decisions()
        rows = []
        for decision in payload.get("decisions") or []:
            when = _iso_epoch(decision.get("decided_at") or decision.get("ts"))
            if when is None or when >= cutoff:
                rows.append(decision)
        return {
            "schema_version": payload.get("schema_version"),
            "window_seconds": window,
            "decisions": rows,
            "total": len(rows),
        }

    @mcp.tool()
    def state_takeover() -> dict[str, Any]:
        """Zero-context takeover: all six items in one call.

        零上下文接手六项=名册、线状态、等拍板、待上线、授权模式、当前
        release。不可得项**显式标注**（``unavailable``+原因）——不得省略
        键、不得以旧缓存冒充现算；每项带 ``computed_at``。
        """
        when = now_iso()
        items = takeover_items(view, when, run_root=Path(state_config.run_root))
        missing = [k for k in TAKEOVER_KEYS if not takeover_item_complete(items.get(k))]
        return {
            "schema_version": "1",
            "items": items,
            "complete": not missing,
            "missing": missing,
        }

    # --- 写四件（监督者专属）----------------------------------------------

    @mcp.tool()
    def line_revive(
        line_id: str,
        basis: str,
        principal: str,
        generation: int | None = None,
        run_id: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """Revive one done line (supervisor-only, audited).

        监督者 principal 专属：非监督者稳定拒绝+留痕（拒绝码
        ``OUTER_GATE_NON_SUPERVISOR`` 写进回执）。成功路径即 CLI
        ``line revive`` 的同一原语：C1 预检+revoke 记录+generation 递增。
        """
        require_supervisor("line_revive", principal)
        fn = revive
        if fn is None:
            from fleet_graph.cli import perform_line_revive

            fn = perform_line_revive
        try:
            return dict(
                fn(
                    folder_id=line_id,
                    who=principal,
                    basis=basis,
                    lines_config=Path(state_config.lines_config),
                    run_root=Path(state_config.run_root),
                    generation=generation,
                    run_id=run_id,
                    reason=reason or None,
                )
            )
        except (SystemExit, ValueError) as exc:
            refuse("REVIVE_REFUSED", str(exc), tool="line_revive")

    @mcp.tool()
    def line_set_seat(
        line_id: str,
        to_seat: str,
        reason: str,
        principal: str,
        probe: bool = True,
    ) -> dict[str, Any]:
        """Switch one line's runtime seat (supervisor-only, audited).

        监督者 principal 专属（同族鉴权+留痕）。成功路径即 CLI
        ``line set-seat`` 的同一原语：C4 探活预检+override 记录+
        generation 递增。
        """
        require_supervisor("line_set_seat", principal)
        fn = set_seat
        if fn is None:
            from fleet_graph.cli import perform_set_seat

            fn = perform_set_seat
        try:
            return dict(
                fn(
                    folder_id=line_id,
                    to_seat=to_seat,
                    reason=reason,
                    who=principal,
                    lines_config=Path(state_config.lines_config),
                    run_root=Path(state_config.run_root),
                    probe_enabled=probe,
                )
            )
        except (SystemExit, ValueError) as exc:
            refuse("SET_SEAT_REFUSED", str(exc), tool="line_set_seat")

    @mcp.tool()
    def maintenance_set(
        principal: str,
        reason: str,
        ttl_seconds: int = MAINTENANCE_DEFAULT_TTL_S,
    ) -> dict[str, Any]:
        """Hold the fleet-wide maintenance gate (supervisor-only).

        写维护闸（``maintenance-stop``，带 ``reason`` 与 ``expires_at``；
        过期即失活，v23 裁决语义）。监督者专属+留痕。``maintenance_gate``
        为测试注入点；生产默认绑定调度器 ``maintenance_stop()`` 读取的同一
        面（roster ``maintenance_stop`` 覆盖键 →
        ``SchedulerConfig.maintenance_stop_path``，缺省
        ``/data/fleet-graph/maintenance-stop``）——工具持有闸与调度器认知
        闸是同一文件。
        """
        require_supervisor("maintenance_set", principal)
        if not str(reason).strip():
            refuse(
                "MAINTENANCE_REASON_REQUIRED",
                "maintenance_set needs a reason: a fleet-wide hold without a reason is unauditable",
                tool="maintenance_set",
            )
        try:
            ttl = int(ttl_seconds)
        except (TypeError, ValueError):
            refuse(
                "MAINTENANCE_TTL_INVALID",
                f"ttl_seconds must be an integer, got {ttl_seconds!r}",
            )
        if ttl <= 0:
            refuse("MAINTENANCE_TTL_INVALID", f"ttl_seconds must be > 0, got {ttl}")
        try:
            gate = maintenance_gate
            if gate is None:
                gate = SchedulerMaintenanceGate(run_root=Path(state_config.run_root))
            gate.set(reason=str(reason), ttl_seconds=ttl, by=principal, clock=clock)
        except Exception as exc:
            refuse("MAINTENANCE_WRITE_FAILED", str(exc), tool="maintenance_set")
        return {
            "status": "held",
            "reason": str(reason),
            "ttl_seconds": ttl,
            "held_by": principal,
            "gate_path": str(getattr(gate, "path", "")),
            "computed_at": now_iso(),
        }

    @mcp.tool()
    def maintenance_clear(principal: str) -> dict[str, Any]:
        """Release the fleet-wide maintenance gate (supervisor-only).

        清维护闸。监督者专属+留痕；清除一个本就不存在的闸是幂等成功
        （``status: "clear"``），但清除已过期的闸如实标注 ``expired: true``。
        """
        require_supervisor("maintenance_clear", principal)
        expired = False
        try:
            gate = maintenance_gate
            if gate is None:
                gate = SchedulerMaintenanceGate(run_root=Path(state_config.run_root))
            expired = gate.expired()
            gate.clear()
        except Exception as exc:
            refuse("MAINTENANCE_WRITE_FAILED", str(exc), tool="maintenance_clear")
        return {
            "status": "clear",
            "expired": expired,
            "cleared_by": principal,
            "computed_at": now_iso(),
        }

    # --- 挂 note 一件 ------------------------------------------------------

    @mcp.tool()
    def note_publish(
        card: str,
        note: str,
        note_type: str,
        principal: str,
        refs: list[dict[str, str]] | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """Publish one ``work.note.v1`` onto a board card (the MCP gate; the bus
        is the carrier).

        监督者与**卡主本人**（dispatched_by 线对自己卡）可用；其余身份拒绝
        （``NOTE_FORBIDDEN``）。``refs`` 必填（协议 ``refs_required``，缺失
        拒绝码 ``refs_required``）。载荷与 refs 语义与 bus ``work.note.v1``
        逐字段对齐：``{card_entity_id, note, note_type}``+
        ``refs=[{"target_entity": card}]``——工具落 bus，不另起信道。
        """
        identity = (principal or "").strip()
        supervisor = supervisor_principal()
        if not identity:
            refuse(CODE_NOTE_FORBIDDEN, "note_publish needs a principal", tool="note_publish")
        allowed = identity == supervisor
        if not allowed:
            owner = owner_resolver(card)
            allowed = bool(owner) and owner == identity
        if not allowed:
            trace_outer_gate_refusal(
                refusal_root,
                tool="note_publish",
                principal=identity,
                supervisor=supervisor,
                detail=f"card {card!r} not owned by caller",
                at=clock(),
            )
            refuse(
                CODE_NOTE_FORBIDDEN,
                f"note_publish refused: principal {identity!r} is neither the supervisor "
                f"nor the card owner of {card!r}",
                tool="note_publish",
            )
        if not note_type or note_type not in NOTE_TYPES:
            refuse(
                "NOTE_TYPE_INVALID",
                f"note_type must be one of {list(NOTE_TYPES)}, got {note_type!r}",
                tool="note_publish",
            )
        if not str(note).strip():
            refuse("NOTE_TEXT_REQUIRED", "note must be a non-empty string", tool="note_publish")
        if not refs:
            refuse(
                CODE_NOTE_REFS_REQUIRED,
                "note_publish requires refs (work.note.v1 is refs_required); "
                'use [{"target_entity": <card_entity_id>}]',
                tool="note_publish",
            )
        fn = note_publisher
        if fn is None:
            fn = _default_note_publisher
        try:
            result = fn(
                card_entity_id=card,
                text=str(note),
                note_type=note_type,
                idempotency_key=idempotency_key or f"outer-gate-note:{clock()}",
                refs=refs,
            )
        except Exception as exc:
            raise ToolError(
                json.dumps(
                    {
                        "code": CODE_UPSTREAM_UNAVAILABLE,
                        "message": str(exc),
                        "tool": "note_publish",
                    },
                    sort_keys=True,
                )
            ) from exc
        return {
            "status": "published",
            "card_entity_id": card,
            "note_type": note_type,
            "message_id": getattr(result, "message_id", None)
            or (result.get("message_id") if isinstance(result, dict) else None),
            "entity_id": getattr(result, "entity_id", None)
            or (result.get("entity_id") if isinstance(result, dict) else None),
            "computed_at": now_iso(),
        }

    return mcp


def _default_identity_check(identity: str) -> bool:
    return identity == supervisor_principal()


def _iso_epoch(value: Any) -> float | None:
    """epoch seconds from an ISO stamp (or a numeric epoch); None otherwise."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    from datetime import datetime

    try:
        return datetime.fromisoformat(text).timestamp()
    except (ValueError, TypeError):
        return None


def _default_card_owner(dd_root: Path) -> Callable[[str], str | None]:
    """The production card-ownership resolver: ``card_entity_id`` → owner line.

    Reads the dd tree's ``record.json`` admission records (the same
    ``card_entity_id`` the control plane freezes at admission); the card
    owner is the record's ``dispatched_by`` line (the card-owner principal
    that may note its own card). Unknown cards resolve to ``None`` -- the
    supervisor is then the only allowed publisher.
    """

    def owner(card_entity_id: str) -> str | None:
        if not card_entity_id:
            return None
        try:
            entries = sorted(Path(dd_root).iterdir(), key=lambda p: p.name)
        except OSError:
            return None
        for entry in entries:
            if not entry.is_dir():
                continue
            try:
                raw = json.loads((entry / "record.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(raw, dict):
                continue
            if str(raw.get("card_entity_id") or "") == card_entity_id:
                return str(raw.get("dispatched_by") or "") or None
        return None

    return owner


def _default_note_publisher(
    *,
    card_entity_id: str,
    text: str,
    note_type: str,
    idempotency_key: str,
    refs: list[dict[str, str]],
) -> Any:
    """The production note carrier: publish ``work.note.v1`` onto the bus.

    MCP 是门，bus 是载体：载荷 ``{card_entity_id, note, note_type}`` 与
    refs ``[{"target_entity": card}]`` 与 ``Board.note`` / bus
    ``work.note.v1`` 注册 schema 逐字段一致。无凭证 / bus 不可达 = 显式
    ``upstream_unavailable``，绝不静默吞。
    """
    from fleet_graph.bus.board import WORK_NOTES, Board
    from fleet_graph.bus.client import BusClient, load_token

    client = BusClient(token=load_token())
    board = Board(client)
    return client.publish(
        board.notes_channel or WORK_NOTES,
        "work.note.v1",
        {"card_entity_id": card_entity_id, "note": text, "note_type": note_type},
        idempotency_key,
        refs=refs,
    )


class SchedulerMaintenanceGate:
    """The fleet-wide maintenance gate as an injectable seam.

    Production binds the file the scheduler's ``maintenance_stop()`` reads:
    the daemon's ``DEFAULT_MAINTENANCE_STOP`` (``/data/fleet-graph/
    maintenance-stop``) or the roster's ``maintenance_stop`` override -- the
    exact path ``SchedulerConfig`` resolves, so a tool-held gate is the same
    gate the daemon honours. Tests bind a scratch root. ``set`` writes
    ``{reason, expires_at, held_by}``; the gate is inert once ``expires_at``
    passes (the v23 ruling the daemon honours), and an unparseable flag keeps
    holding (the daemon's deliberate divergence).
    """

    def __init__(
        self, run_root: Path | str | None = None, *, path: Path | str | None = None
    ) -> None:
        if path is not None:
            self._path = Path(path)
        else:
            from fleet_graph.scheduler.daemon import DEFAULT_MAINTENANCE_STOP

            self._path = (
                Path(run_root) / MAINTENANCE_STOP_FILE
                if run_root is not None
                else DEFAULT_MAINTENANCE_STOP
            )

    @property
    def path(self) -> Path:
        return self._path

    def set(
        self,
        *,
        reason: str,
        ttl_seconds: int,
        by: str,
        clock: Callable[[], float] = time.time,
    ) -> dict[str, Any]:
        expires_at = _iso(clock() + ttl_seconds)
        payload = {"reason": reason, "expires_at": expires_at, "held_by": by}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return payload

    def expired(self) -> bool:
        """Whether the gate on disk (if any) is already past its deadline."""
        try:
            flag = json.loads(self.path.read_text(encoding="utf-8"))
            expires_at = str(flag["expires_at"]).replace("+00:00", "Z")[:19] + "Z"
            deadline = calendar.timegm(time.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ"))
        except (OSError, ValueError, TypeError, KeyError):
            return False
        return time.time() >= deadline

    def clear(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    run_root: str | None = None,
    lines_config: str | None = None,
    dd_root: str | None = None,
) -> None:
    """Run the outer-gate MCP surface on loopback.

    Data-source bindings default to the same roots the :7494 read model and
    the dd control plane serve, so production reads identical sources. An
    occupied port is a visible failure (the bind raises; the CLI prints the
    reason) -- never a crash loop with no explanation.
    """
    config = FleetStateConfig(
        run_root=Path(run_root) if run_root else DEFAULT_RUN_ROOT,
        lines_config=Path(lines_config) if lines_config else Path(DEFAULT_LINES_CONFIG),
        dd_root=Path(dd_root) if dd_root else Path(DEFAULT_DD_ROOT),
    )
    build_outer_gate_mcp_server(
        config,
        Path(DEFAULT_DD_ROOT) if not dd_root else Path(dd_root),
    ).run(transport="streamable-http", host=host, port=port, path="/mcp")


__all__ = [
    "CODE_LINE_NOT_FOUND",
    "CODE_LINE_UNKNOWN",
    "CODE_NOTE_FORBIDDEN",
    "CODE_NOTE_REFS_REQUIRED",
    "CODE_NOTE_REFUSAL",
    "CODE_NOT_SUPERVISOR",
    "CODE_UPSTREAM_UNAVAILABLE",
    "DEFAULT_BRIDGE_STATE_DIR",
    "DEFAULT_DD_ROOT",
    "DEFAULT_HOST",
    "DEFAULT_LINES_CONFIG",
    "DEFAULT_PORT",
    "DEFAULT_RUN_ROOT",
    "MAINTENANCE_STOP_FILE",
    "MCP_SERVER_NAME",
    "NOTE_TYPES",
    "OUTER_GATE_REFUSALS_FILE",
    "SUPERVISOR_PRINCIPAL_DEFAULT",
    "SUPERVISOR_PRINCIPAL_ENV",
    "TAKEOVER_KEYS",
    "OuterGateUnavailable",
    "SchedulerMaintenanceGate",
    "build_outer_gate_mcp_server",
    "serve",
    "supervisor_principal",
    "takeover_item_complete",
    "takeover_items",
    "takeover_keys",
    "trace_outer_gate_refusal",
]
