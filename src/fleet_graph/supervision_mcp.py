"""监督面冷启动接手 M5：一个只读 MCP 工具，一次调用返回全部现状。

``fleet-graph supervision serve``（注册名 ``fleet-graph-supervision``，loopback
:5615）是监督面冷启动接手的唯一入口：一个**零参数**的工具
``supervision_handoff`` 一次调用返回一个零上下文 session 接手所需的**全部**，且
每一项都是权威值而非线索（goal.md M5，用户 2026-09-02 15:5x 追加）。

纪律（逐字对齐 goal.md M5）：

- **只读**：工具零参数、零写原语，接手是只读动作（阴性①回归：tools/list 与
  各工具 inputSchema 不得出现任何写原语）。
- **读不到 ≠ 没有**：任一「读不到/缺失」的数据项返回体必须显式
  ``unavailable`` / ``missing`` 标记，绝不返回空对象/空数组冒充「没有」
  （阴性②回归：注入缺失场景断言该标记存在）。
- **合成**：这是合成（名册 x 线状态 x 待裁决 x 待收割 x 监督卷），不是把
  ``/v1/lines`` 原样包一层；读取走既有 ``:7494`` 读模型视图与既有 dd 服务/
  名册数据源，禁止自创第二套读法。
- ``build_supervision_mcp_server`` 可无传输层单测（参考 decision_mcp.py）。
- ``:7494`` 或任一上游不可达时如实报「不可判定」（``degraded`` +
  ``unavailable_sources`` 证据）而非静默变绿。

端口遵循既有 reserved-ports 单一来源（``config/decision-mcp-reserved-ports.json``，
监督面 2026-09-02 扫描：5602-5613 连续占满，5614/5615/5616 空闲）。``DEFAULT_PORT``
不得出现在该清单内。
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fleet_graph.decision_mcp import RESERVED_PORTS_FILE, load_reserved_ports
from fleet_graph.state.fleet_state import FleetStateConfig, FleetStateView
from fleet_graph.state.run_artifacts import RELEASE_CURRENT_PATH, capture_release_id

DEFAULT_HOST = "127.0.0.1"
#: The supervision handoff surface's loopback port. 5615 was scanned free on the
#: fleet host (5602-5613 continuous occupied); it must never sit in the reserved
#: ports list (the red-able assertion in tests/test_m5_supervision_handoff.py).
DEFAULT_PORT = 5615

#: The FastMCP registration name of this surface, distinct from the
#: dev-dispatch / goal / research / decision servers.
MCP_SERVER_NAME = "fleet-graph-supervision"

SCHEMA_VERSION = "1"

#: The only authorization modes this surface reports. ``full-auto`` means the
#: supervision face may act under mechanical pre-authorization (第四道闸);
#: ``semi-auto`` means every verdict is a human's. A missing/malformed mode
#: fail-safes to ``semi-auto`` (spec: 缺失须 fail-safe 到 semi-auto).
AUTH_MODE_FULL_AUTO = "full-auto"
AUTH_MODE_SEMI_AUTO = "semi-auto"
ALLOWED_AUTH_MODES = frozenset({AUTH_MODE_FULL_AUTO, AUTH_MODE_SEMI_AUTO})
DEFAULT_AUTH_MODE = AUTH_MODE_SEMI_AUTO

#: Data-source defaults, mirroring the read model / scheduler constants so this
#: surface reads the same files without importing the CLI entrypoint.
DEFAULT_LINES_CONFIG = Path("config/ronin-lines.json")
DEFAULT_RUN_ROOT = Path("/data/fleet-graph/runs")
DEFAULT_DD_ROOT = Path("/data/fleet-graph/dd")
DEFAULT_MAINTENANCE_STOP = Path("/data/fleet-graph/maintenance-stop")


def _unavailable(source: str, reason: str) -> dict[str, Any]:
    """The explicit 「读不到」 marker. Never an empty list/object masquerading
    as 「没有」."""
    return {"unavailable": True, "source": source, "reason": reason}


def is_unavailable(value: Any) -> bool:
    return isinstance(value, dict) and value.get("unavailable") is True


def read_roster(lines_config: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Read the goal-line roster (alias + seat + enabled). Fail-soft, explicit.

    Returns ``(lines, error)``: ``error`` is a non-empty string only when the
    roster *could not be read* (missing file / malformed JSON / no ``lines``
    list) -- the 「读不到」 case. A legitimately empty roster returns
    ``([], None)``, the 「没有线」 case. The two are never conflated.
    """
    try:
        raw = json.loads(lines_config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], f"roster unreadable: {type(exc).__name__}: {exc}"
    if not isinstance(raw, dict):
        return [], "roster is not a JSON object"
    entries = raw.get("lines")
    if not isinstance(entries, list):
        return [], "roster has no 'lines' list"
    lines: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        folder_id = entry.get("folder_id")
        if not folder_id:
            continue
        try:
            generation = int(entry.get("generation") or 1)
        except (TypeError, ValueError):
            generation = 1
        lines.append(
            {
                "folder_id": str(folder_id),
                "alias": str(entry.get("alias") or ""),
                "seat": str(entry.get("seat") or ""),
                "enabled": bool(entry.get("enabled", False)),
                "generation": generation,
            }
        )
    return lines, None


@dataclass
class SupervisionConfig:
    """Bind the handoff surface's read-model roots + supervision-only facts.

    ``state`` is the same :7494 read-model config the ``fleet-graph state serve``
    service binds; reading through it (rather than HTTP) keeps the surface on the
    existing read path (禁止自创第二套读法). ``supervision_folder_id`` /
    ``authorization_mode`` are the supervision face's own handoff facts, sourced
    from the serving environment. The three ``Callable`` seams
    (``awaiting_gate`` / ``main_head`` / ``release_id``) are injectable so tests
    drive the surface against scratch data without touching production files.
    """

    state: FleetStateConfig = field(default_factory=FleetStateConfig)
    supervision_folder_id: str | None = None
    authorization_mode: str | None = None
    maintenance_stop_path: Path = DEFAULT_MAINTENANCE_STOP
    release_current_path: Path = RELEASE_CURRENT_PATH
    repo_path: Path | None = None
    clock: Callable[[], float] = time.time
    awaiting_gate: Callable[[], list[dict[str, Any]] | None] | None = None
    main_head: Callable[[], str | None] | None = None
    release_id: Callable[[], str | None] | None = None


def _gate_row(row: dict[str, Any]) -> dict[str, Any]:
    """One ``awaiting_gate`` dd development reduced to its decision facts."""
    return {
        "development_id": str(row.get("development_id") or ""),
        "state": str(row.get("state") or "awaiting_gate"),
        "stage": row.get("stage"),
        "head_commit": row.get("head_commit"),
        "awaiting": row.get("awaiting"),
    }


class SupervisionHandoff:
    """Build the one-call supervision handoff snapshot from the configured sources."""

    def __init__(self, config: SupervisionConfig) -> None:
        self.config = config
        self.view = FleetStateView(config.state)

    # --- the snapshot -----------------------------------------------------

    def build(self) -> dict[str, Any]:
        unavailable_sources: list[str] = []
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "authorization_mode": self._auth_mode(),
            "supervision_volume": self._supervision_volume(),
        }

        roster_lines, roster_error = read_roster(self.config.state.lines_config)
        if roster_error:
            result["roster"] = _unavailable("config/ronin-lines.json", roster_error)
            unavailable_sources.append("roster")
            effective_roster: list[dict[str, Any]] | None = None
        else:
            result["roster"] = {
                "total": len(roster_lines),
                "enabled": sum(1 for line in roster_lines if line["enabled"]),
                "lines": [
                    {
                        "folder_id": line["folder_id"],
                        "alias": line["alias"],
                        "seat": line["seat"],
                        "enabled": line["enabled"],
                    }
                    for line in roster_lines
                ],
            }
            effective_roster = roster_lines

        lines_view = self._lines_view()
        result["line_status"] = self._line_status(effective_roster, lines_view)
        if is_unavailable(result["line_status"]):
            unavailable_sources.append("line_status")

        result["awaiting_decision"] = self._awaiting_decision(lines_view, effective_roster)
        if is_unavailable(result["awaiting_decision"]["parked_lines"]):
            unavailable_sources.append("awaiting_decision:parked_lines")
        if is_unavailable(result["awaiting_decision"]["gate_developments"]):
            unavailable_sources.append("awaiting_decision:gate_developments")

        result["harvestable"] = self._harvestable()
        if is_unavailable(result["harvestable"]):
            unavailable_sources.append("harvestable")

        result["releases"] = self._releases(lines_view, effective_roster)
        for key in ("main", "deployed", "running"):
            if is_unavailable(result["releases"][key]):
                unavailable_sources.append(f"releases:{key}")

        result["maintenance_window"] = self._maintenance_window()

        result["degraded"] = bool(unavailable_sources)
        result["unavailable_sources"] = unavailable_sources
        return result

    # --- the authoritative facts -----------------------------------------

    def _auth_mode(self) -> str:
        raw = self.config.authorization_mode
        if raw is None:
            return AUTH_MODE_SEMI_AUTO
        value = str(raw).strip().lower()
        return value if value in ALLOWED_AUTH_MODES else AUTH_MODE_SEMI_AUTO

    def _supervision_volume(self) -> dict[str, Any]:
        folder_id = self.config.supervision_folder_id
        if not folder_id:
            return {"folder_id": None, "missing": True}
        return {"folder_id": str(folder_id)}

    def _lines_view(self) -> dict[str, Any]:
        try:
            return self.view.lines()
        except Exception as exc:  # the surface must report, never crash
            return _unavailable("/v1/lines", f"{type(exc).__name__}: {exc}")

    def _line_status(
        self, roster_lines: list[dict[str, Any]] | None, lines_view: dict[str, Any]
    ) -> dict[str, Any]:
        """合成名册 x 线状态：每条名册线都有一行，读模型状态按 folder_id 对齐。

        A roster line that the read model did not cover is marked
        ``state_unavailable`` (读不到), never silently dropped.
        """
        if roster_lines is None:
            return _unavailable("/v1/lines", "roster (config/ronin-lines.json) unreachable")
        if is_unavailable(lines_view):
            return lines_view
        by_id = {
            str(line.get("folder_id") or ""): line
            for line in (lines_view.get("lines") or [])
            if isinstance(line, dict)
        }
        out: list[dict[str, Any]] = []
        for roster in roster_lines:
            entry: dict[str, Any] = {
                "folder_id": roster["folder_id"],
                "alias": roster["alias"],
                "seat": roster["seat"],
                "enabled": roster["enabled"],
            }
            status = by_id.get(roster["folder_id"])
            if status is None:
                entry["state_unavailable"] = True
            else:
                entry.update(
                    {
                        "terminal": status.get("terminal"),
                        "parked": status.get("parked"),
                        "wake_facts_stale": status.get("wake_facts_stale"),
                        "generation": status.get("generation"),
                        "round": status.get("round"),
                        "heartbeat_age_s": status.get("heartbeat_age_s"),
                        "release_id": status.get("release_id"),
                    }
                )
            out.append(entry)
        return {"lines": out}

    def _awaiting_decision(
        self, lines_view: dict[str, Any], roster_lines: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        """等我拍板的清单：驻停等裁决的线 + ``awaiting_gate`` 的 dd 单（两类缺一不可）。"""
        parked = self._parked_lines(lines_view, roster_lines)
        return {"parked_lines": parked, "gate_developments": self._gate_developments()}

    def _parked_lines(
        self, lines_view: dict[str, Any], roster_lines: list[dict[str, Any]] | None
    ) -> Any:
        if roster_lines is None:
            return _unavailable("/v1/lines", "roster (config/ronin-lines.json) unreachable")
        if is_unavailable(lines_view):
            return lines_view
        parked: list[dict[str, Any]] = []
        for line in lines_view.get("lines") or []:
            if not isinstance(line, dict) or line.get("parked") is not True:
                continue
            parked.append(
                {
                    "folder_id": str(line.get("folder_id") or ""),
                    "generation": line.get("generation"),
                    "waiting_on": (line.get("wake_facts") or {}).get("waiting_on"),
                }
            )
        return parked

    def _gate_developments(self) -> Any:
        if not self.config.state.dd_root.is_dir():
            return _unavailable("awaiting_gate", f"{self.config.state.dd_root} is not a directory")
        fetcher = self.config.awaiting_gate or self._default_awaiting_gate
        try:
            rows = fetcher()
        except Exception as exc:  # an unreachable dd control plane is reported, not swallowed
            return _unavailable("awaiting_gate", f"{type(exc).__name__}: {exc}")
        if rows is None:
            return _unavailable("awaiting_gate", "dd list returned None")
        return [_gate_row(row) for row in rows if isinstance(row, dict)]

    def _default_awaiting_gate(self) -> list[dict[str, Any]] | None:
        from fleet_graph.dd.control_plane import STATE_AWAITING_GATE, DdControlPlane

        plane = DdControlPlane(root=self.config.state.dd_root)
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = plane.list(state=STATE_AWAITING_GATE, limit=100, cursor=cursor)
            rows.extend(page.get("developments") or [])
            cursor = page.get("cursor")
            if not cursor:
                break
        return rows

    def _harvestable(self) -> dict[str, Any]:
        if not self.config.state.dd_root.is_dir():
            return _unavailable(
                "/v1/harvestable", f"{self.config.state.dd_root} is not a directory"
            )
        try:
            payload = self.view.harvestable()
        except Exception as exc:
            return _unavailable("/v1/harvestable", f"{type(exc).__name__}: {exc}")
        developments = payload.get("developments") if isinstance(payload, dict) else None
        if not isinstance(developments, list):
            return _unavailable("/v1/harvestable", "read model returned no 'developments' list")
        return {"developments": developments}

    def _maintenance_window(self) -> dict[str, Any]:
        path = self.config.maintenance_stop_path
        if not path.exists():
            return {"active": False, "status": "inactive"}
        try:
            flag = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"active": True, "status": "unparseable", "reason": str(exc)}
        if not isinstance(flag, dict):
            return {"active": True, "status": "unparseable", "reason": "not a JSON object"}
        expires_at = flag.get("expires_at")
        if expires_at is None:
            # No expiry reads as holding (same posture as the scheduler: a
            # window without an expiry is a window until removed).
            return {"active": True, "status": "active", "detail": flag}
        try:
            stamp = str(expires_at).replace("+00:00", "Z")
            if stamp.endswith("Z"):
                stamp = stamp[:-1] + "+00:00"
            deadline = datetime.fromisoformat(stamp).timestamp()
        except (ValueError, TypeError):
            return {
                "active": True,
                "status": "unparseable",
                "detail": flag,
                "reason": "unparseable expires_at; treated as holding",
            }
        active = self.config.clock() < deadline
        return {
            "active": active,
            "status": "active" if active else "expired",
            "detail": flag,
        }

    def _releases(
        self, lines_view: dict[str, Any], roster_lines: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        main = self._main_head()
        deployed = self._deployed_release()
        running = self._running_releases(lines_view, roster_lines)
        return {"main": main, "deployed": deployed, "running": running}

    def _main_head(self) -> Any:
        if self.config.main_head is not None:
            reader = self.config.main_head
        elif self.config.repo_path is not None:
            repo_path = self.config.repo_path

            def reader() -> str | None:
                return _git_main_head(repo_path)

        else:
            return _unavailable("main", "no repo_path or main_head seam configured")
        try:
            value = reader()
        except Exception as exc:
            return _unavailable("main", f"{type(exc).__name__}: {exc}")
        if not value:
            return _unavailable("main", "main HEAD could not be read")
        return str(value)

    def _deployed_release(self) -> Any:
        if self.config.release_id is not None:
            reader = self.config.release_id
        else:
            release_current_path = self.config.release_current_path

            def reader() -> str | None:
                return capture_release_id(release_current_path)

        try:
            value = reader()
        except Exception as exc:
            return _unavailable("deployed", f"{type(exc).__name__}: {exc}")
        if not value:
            return _unavailable("deployed", "deployed release could not be resolved")
        return str(value)

    def _running_releases(
        self, lines_view: dict[str, Any], roster_lines: list[dict[str, Any]] | None
    ) -> Any:
        if roster_lines is None:
            return _unavailable("/v1/lines", "roster (config/ronin-lines.json) unreachable")
        if is_unavailable(lines_view):
            return lines_view
        running: list[dict[str, Any]] = []
        for line in lines_view.get("lines") or []:
            if not isinstance(line, dict):
                continue
            running.append(
                {
                    "folder_id": str(line.get("folder_id") or ""),
                    "release_id": line.get("release_id"),
                }
            )
        return running


def _git_main_head(repo_path: Path) -> str | None:
    """The repo's ``HEAD`` commit, fail-soft (read-only, never a crash)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def build_supervision_mcp_server(source: Callable[[], dict[str, Any]] | None = None) -> Any:
    """Build the standalone supervision handoff MCP surface.

    ``source`` is the portable seam: a zero-argument callable returning the
    handoff snapshot. When ``None`` the surface builds from a default
    :class:`SupervisionConfig` (production serving always binds an explicit one
    via ``serve()``). The single tool ``supervision_handoff()`` takes no
    arguments -- a zero-context session calls it alone and gets everything.
    """
    from fastmcp import FastMCP

    builder = (
        source if source is not None else lambda: SupervisionHandoff(SupervisionConfig()).build()
    )
    mcp = FastMCP(MCP_SERVER_NAME)

    @mcp.tool()
    def supervision_handoff() -> dict[str, Any]:
        """One read-only call: the whole supervision cold-start handoff.

        Zero-context: a fresh session calls only this tool and receives, as
        authoritative values (never clues), the current supervision volume
        folder_id, the authorization mode (full-auto/semi-auto), the roster
        (total/enabled/per-line alias+seat), every line's run status, the
        awaiting-my-decision list (parked lines and awaiting_gate dd
        developments), the harvestable list, the maintenance-window flag, and
        the current main / deployed release / per-process running release. Any
        item that could not be read is explicitly marked ``unavailable`` or
        ``missing`` -- never silently empty. This tool exposes no write
        capability.
        """
        return builder()

    return mcp


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    lines_config: str | None = None,
    run_root: str | None = None,
    dd_root: str | None = None,
    supervise_folder_id: str | None = None,
    authorization_mode: str | None = None,
) -> None:
    """Run the standalone supervision handoff MCP surface on loopback.

    The R2 port discipline mirrors decision_mcp: it is an acceptance-time
    assertion (the red-able port check in tests/test_m5_supervision_handoff.py),
    not a runtime port probe -- FastMCP itself surfaces a bind failure visibly.
    ``supervise_folder_id`` / ``authorization_mode`` source the supervision
    face's own handoff facts; a missing authorization mode fail-safes to
    ``semi-auto``, a missing folder_id is reported as such in the snapshot.
    """
    state = FleetStateConfig(
        lines_config=Path(lines_config) if lines_config else DEFAULT_LINES_CONFIG,
        run_root=Path(run_root) if run_root else DEFAULT_RUN_ROOT,
        dd_root=Path(dd_root) if dd_root else DEFAULT_DD_ROOT,
    )
    config = SupervisionConfig(
        state=state,
        supervision_folder_id=supervise_folder_id,
        authorization_mode=authorization_mode,
    )
    build_supervision_mcp_server(lambda: SupervisionHandoff(config).build()).run(
        transport="streamable-http", host=host, port=port, path="/mcp"
    )


__all__ = [
    "ALLOWED_AUTH_MODES",
    "AUTH_MODE_FULL_AUTO",
    "AUTH_MODE_SEMI_AUTO",
    "DEFAULT_AUTH_MODE",
    "DEFAULT_HOST",
    "DEFAULT_MAINTENANCE_STOP",
    "DEFAULT_PORT",
    "MCP_SERVER_NAME",
    "RESERVED_PORTS_FILE",
    "SCHEMA_VERSION",
    "SupervisionConfig",
    "SupervisionHandoff",
    "build_supervision_mcp_server",
    "is_unavailable",
    "load_reserved_ports",
    "read_roster",
    "serve",
]
