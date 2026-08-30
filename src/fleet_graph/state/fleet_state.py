"""M1 传感层薄版——fleet-state read-model (`127.0.0.1:7494`).

一个**只读** HTTP 服务：数据源一律 pull、一律只读（heartbeat / terminal /
dd status / decision-bridge 的 bridge.sqlite3，以及可选的 agent-bus
``work.decision.v1``），不新造生产者、不写任何被观察工件。实现集中在
这一个服务进程内；两个视图：

- ``GET /v1/lines`` → ``{"schema_version": <str>, "lines": [...]}``
- ``GET /v1/decisions`` → ``{"schema_version": <str>, "decisions": [...]}``
- ``GET /v1/harvestable`` → ``{"schema_version": <str>, "developments": [...]}``

铁律（本模块与规格一致）：

- 主键列表字段名严格为 ``lines`` / ``decisions``；``schema_version`` 必填。
- **读失败降级不 5xx 全链**：单个工件缺失/解析失败只对该条目标记
  absent/unknown，绝不让整表挂掉。
- 机械事实只读：``heartbeat_age_s`` = 现在 - heartbeat.json 的 ``updated_at``；
  ``parked`` = ``waiting_on == "decision"``（见 ``normalize_waiting_on``）；
  ``wake_facts`` 至少含 ``waiting_on`` 等机械事实。

HTTP 用标准库 ``ThreadingHTTPServer`` 实现，不引入新的依赖。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from fleet_graph.state.run_artifacts import normalize_waiting_on

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7494
DEFAULT_RUN_ROOT = Path("/data/fleet-graph/runs")
DEFAULT_DD_ROOT = Path("/data/fleet-graph/dd")
DEFAULT_LINES_CONFIG = Path("config/ronin-lines.json")
DEFAULT_BRIDGE_STATE_DIR = Path("/data/fleet-graph/decision-bridge")
DEFAULT_BRIDGE_DB_NAME = "bridge.sqlite3"

#: The main checkout whose default branch decides whether a product commit
#: has been harvested. Read-only git only: nothing here ever writes to it.
DEFAULT_REPO_PATH = Path("/data/code/self/fleet-graph")
DEFAULT_DEFAULT_BRANCH = "main"

#: 送达链状态（closed）。
STATE_PUBLISHED = "published"
STATE_BRIDGED = "bridged"
STATE_CONSUMED = "consumed"
STATE_SWALLOWED = "swallowed"

#: bridge receipt status → 送达链状态 +（swallowed 时）reason。
#: ``intent_recorded`` 即 bridge 已见过该裁决（bridged）；``resumed`` 即已送达
#: 消费（consumed）；``noop`` / ``refused`` 即被吞（swallowed，带 reason）。
_RECEIPT_STATE: dict[str, tuple[str, str | None]] = {
    "intent_recorded": (STATE_BRIDGED, None),
    "resumed": (STATE_CONSUMED, None),
    "noop": (STATE_SWALLOWED, None),
    "refused": (STATE_SWALLOWED, None),
}


@dataclass
class FleetStateConfig:
    """The read-model's bind + data-source roots."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    run_root: Path = DEFAULT_RUN_ROOT
    dd_root: Path = DEFAULT_DD_ROOT
    lines_config: Path = DEFAULT_LINES_CONFIG
    bridge_state_dir: Path = DEFAULT_BRIDGE_STATE_DIR
    bus_url: str | None = None
    #: The repo whose default branch decides whether a product commit has been
    #: harvested (content-equivalent landing check; read-only git, never write).
    #: None disables the check and degrades every `complete` development to
    #: "not landed" (harvestable) -- see `_default_landed_in_default_branch`.
    repo_path: Path | None = None
    default_branch: str = DEFAULT_DEFAULT_BRANCH
    #: Inject the `landed_in_default_branch(commit) -> bool` predicate. None
    #: uses the git-backed production default (`repo_path` + `default_branch`).
    landed_in_default_branch: Callable[[str], bool] | None = None
    clock: Callable[[], float] = time.time


def _read_json(path: Path) -> dict[str, Any] | None:
    """One artifact, or None on missing/unreadable/unparseable (never raises)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _parse_iso(value: Any) -> float | None:
    """epoch seconds from ``%Y-%m-%dT%H:%M:%SZ``; None on anything else."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except (ValueError, TypeError):
        return None


def _read_roster(config: FleetStateConfig) -> tuple[list[tuple[str, int]], Path]:
    """(folder_id, generation) pairs + the roster's run_root, fail-soft.

    The roster ``config/ronin-lines.json`` decides which lines ``/v1/lines``
    covers. A missing/unreadable/malformed roster degrades to an empty line
    list -- never a crash, never a 5xx (「漏报即缺口」).
    """
    try:
        raw = json.loads(config.lines_config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], config.run_root
    if not isinstance(raw, dict):
        return [], config.run_root
    run_root = Path(str(raw.get("run_root") or config.run_root))
    entries = raw.get("lines") or []
    lines: list[tuple[str, int]] = []
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
        lines.append((str(folder_id), generation))
    return lines, run_root


def _read_receipts(config: FleetStateConfig) -> list[dict[str, Any]]:
    """bridge.sqlite3 receipts, read-only (``mode=ro``); [] on any failure.

    Deliberately not ``BridgeStore.open()``: that can create/init the database,
    and this service must never write an observed artifact. A missing database
    is "no receipts", and an unreadable one is also "no receipts" -- the view
    degrades, never 5xx.
    """
    db = config.bridge_state_dir / DEFAULT_BRIDGE_DB_NAME
    if not db.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM receipts").fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return []


def _receipt_to_decision(receipt: dict[str, Any]) -> dict[str, Any]:
    status = str(receipt.get("status") or "")
    state, reason = _RECEIPT_STATE.get(status, (STATE_SWALLOWED, f"unknown_status:{status}"))
    obj: dict[str, Any] = {
        "source_message_id": str(receipt.get("source_message_id") or ""),
        "state": state,
        "owner": {
            "kind": str(receipt.get("target_kind") or ""),
            "id": str(receipt.get("target_id") or ""),
            "generation": receipt.get("generation"),
        },
    }
    if state == STATE_SWALLOWED:
        reason = str(receipt.get("reason") or reason or state)
        obj["reason"] = reason
    return obj


def _default_landed_in_default_branch(config: FleetStateConfig) -> Callable[[str], bool]:
    """The production default `landed_in_default_branch(commit) -> bool`.

    Read-only git against ``config.repo_path``'s ``config.default_branch``:
    a product commit counts as *landed* when its **tree** appears among the
    trees of the commits reachable from the default branch. That is a content
    equivalence, not a literal SHA ancestry test: a squash that rewrote the
    SHA but produced the same tree (a clean squash onto the same base) is
    still detected as landed.

    Any read/query failure (missing repo, missing branch, unknown commit,
    git not available) degrades to ``False`` -- the development is treated as
    *unharvested* and stays listed -- and never raises, never 5xx (spec:
    读取/查询失败降级,绝不 5xx、绝不崩溃). ``repo_path is None`` (not
    configured) disables the check and also degrades to ``False``.
    """

    repo = config.repo_path
    branch = config.default_branch

    def landed(commit: str) -> bool:
        if not repo:
            return False
        try:
            from fleet_graph.dd.git import run_git

            tree = run_git(repo, "rev-parse", f"{commit}^{{tree}}", check=True).stdout.strip()
            trees = set(run_git(repo, "log", "--format=%T", branch, check=True).stdout.split())
            return tree in trees
        except Exception:
            return False

    return landed


def _read_published(config: FleetStateConfig, seen: set[str]) -> list[dict[str, Any]]:
    """Best-effort bus decisions without a bridge receipt → ``published``.

    Read-only against agent-bus (``work.decision.v1``). No bus_url, no
    credential, or any read failure degrades to "no published" -- the bus is an
    optional enrichment, never a hard dependency of the view.
    """
    if not config.bus_url:
        return []
    try:
        from fleet_graph.bus.board import DECISION_KINDS, WORK_NOTES
        from fleet_graph.bus.client import BusClient, load_token

        client = BusClient(base_url=config.bus_url, token=load_token())
        messages, _head = client.messages(WORK_NOTES, limit=200)
    except Exception as exc:
        log.debug("state read-model: bus published read skipped: %s", exc)
        return []
    published: list[dict[str, Any]] = []
    for message in messages:
        message_id = str(message.get("message_id") or "")
        if not message_id or message_id in seen:
            continue
        if message.get("kind") not in DECISION_KINDS:
            continue
        published.append(
            {
                "source_message_id": message_id,
                "state": STATE_PUBLISHED,
                "owner": {"kind": "", "id": "", "generation": None},
            }
        )
    return published


class FleetStateView:
    """Builds the two read-only payloads from the configured data sources."""

    def __init__(self, config: FleetStateConfig) -> None:
        self.config = config
        self._landed_in_default_branch = (
            config.landed_in_default_branch or _default_landed_in_default_branch(config)
        )

    def lines(self) -> dict[str, Any]:
        roster, run_root = _read_roster(self.config)
        now = self.config.clock()
        line_objs: list[dict[str, Any]] = []
        for folder_id, roster_generation in roster:
            heartbeat = _read_json(run_root / folder_id / "heartbeat.json")
            terminal = _read_json(run_root / folder_id / "terminal.json")

            heartbeat_age_s: float | None = None
            if heartbeat and heartbeat.get("updated_at") is not None:
                updated_at = _parse_iso(heartbeat.get("updated_at"))
                if updated_at is not None:
                    heartbeat_age_s = max(0.0, now - updated_at)

            waiting_on, declared = normalize_waiting_on(
                terminal.get("waiting_on") if terminal else None
            )
            wake_facts: dict[str, Any] = {"waiting_on": waiting_on}
            if declared is not None:
                wake_facts["waiting_on_declared"] = declared
            if terminal:
                for key in ("reason", "at", "rounds", "pump_fault", "run_id"):
                    if terminal.get(key) is not None:
                        wake_facts[key] = terminal[key]

            generation = self._generation_for(run_root, folder_id, roster_generation)
            line_objs.append(
                {
                    "folder_id": folder_id,
                    "generation": generation,
                    "round": heartbeat.get("round") if heartbeat else None,
                    "phase": heartbeat.get("phase") if heartbeat else None,
                    "heartbeat_age_s": heartbeat_age_s,
                    "terminal": terminal.get("terminal") if terminal else None,
                    "parked": waiting_on == "decision",
                    "wake_facts": wake_facts,
                }
            )
        return {"schema_version": SCHEMA_VERSION, "lines": line_objs}

    def _generation_for(self, run_root: Path, folder_id: str, roster_generation: int) -> int:
        """The runtime generation from the scheduler's stall-state, else roster.

        The roster ``generation`` is config; the scheduler persists the actual
        per-line counter in ``<run_root>/.scheduler/<folder_id>.json``. Reading
        the persisted counter first keeps the view on the mechanical fact.
        """
        stall = _read_json(run_root / ".scheduler" / f"{folder_id}.json")
        if stall is not None:
            value = stall.get("generation")
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
        return roster_generation

    def decisions(self) -> dict[str, Any]:
        receipts = _read_receipts(self.config)
        decision_objs = [_receipt_to_decision(r) for r in receipts]
        seen = {obj["source_message_id"] for obj in decision_objs}
        decision_objs.extend(_read_published(self.config, seen))
        return {"schema_version": SCHEMA_VERSION, "decisions": decision_objs}

    def harvestable(self) -> dict[str, Any]:
        """The M2 E5 data plane: developments whose product commit has not
        landed on the default branch (read-only, degrade-don't-5xx).

        For every admitted development under the dd root we pull
        ``record.json`` (admission identity) and ``status.json`` (stage /
        terminal / head_commit). A development is *harvestable* exactly when
        it reached ``terminal == "complete"`` and its product commit has not
        landed on the default branch (content-equivalent check). refused /
        fault / empty terminal / in-flight developments are never listed, and
        a landing query failure degrades to "not landed" (listed) -- never a
        5xx, never a crash. Bad artifacts degrade that entry, never the table.
        """
        developments: list[dict[str, Any]] = []
        dd_root = self.config.dd_root
        if not dd_root.is_dir():
            return {"schema_version": SCHEMA_VERSION, "developments": developments}
        for entry in sorted(dd_root.iterdir()):
            if not entry.is_dir():
                continue
            record = _read_json(entry / "record.json")
            if record is None:
                # Not an admitted development (missing admission record):
                # nothing mechanical to say, so nothing to report.
                continue
            development_id = str(record.get("development_id") or entry.name)
            status = _read_json(entry / "status.json")
            if status is None:
                # Unreadable/missing status degrades the entry away: without
                # terminal + head_commit we cannot claim it is harvestable.
                continue
            terminal = str(status.get("terminal") or "")
            if terminal != "complete":
                # E5 approved_unharvested: only a `complete` terminal can be
                # an approved-but-unharvested development. refused / fault /
                # empty terminal / in-flight are never listed.
                continue
            head_commit = str(status.get("head_commit") or "")
            if not head_commit:
                # No product commit to check landing against: nothing
                # mechanical to say, so nothing to report.
                continue
            try:
                landed = bool(self._landed_in_default_branch(head_commit))
            except Exception:
                # A landing-query failure degrades to "not landed" (listed):
                # never a 5xx, never a crash (spec: 读取/查询失败降级).
                landed = False
            if landed:
                # The product commit's content already reached the default
                # branch: harvested, no longer the supervisor's business.
                continue
            developments.append(
                {
                    "development_id": development_id,
                    "head_commit": head_commit,
                    "stage": str(status.get("stage") or ""),
                    "terminal": terminal,
                }
            )
        return {"schema_version": SCHEMA_VERSION, "developments": developments}


class FleetStateHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config: FleetStateConfig) -> None:
        self.view = FleetStateView(config)
        super().__init__((config.host, config.port), FleetStateHandler)


class FleetStateHandler(BaseHTTPRequestHandler):
    server: FleetStateHTTPServer

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/v1/lines":
            payload = self.server.view.lines()
        elif path == "/v1/decisions":
            payload = self.server.view.decisions()
        elif path == "/v1/harvestable":
            payload = self.server.view.harvestable()
        else:
            self.send_error(404)
            return
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("state read-model: " + fmt, *args)


def serve(config: FleetStateConfig) -> None:
    """Run the read-model server until interrupted. Blocks."""
    server = FleetStateHTTPServer(config)
    try:
        log.info("fleet-state read-model serving on http://%s:%d", config.host, config.port)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


__all__ = [
    "DEFAULT_BRIDGE_STATE_DIR",
    "DEFAULT_DD_ROOT",
    "DEFAULT_DEFAULT_BRANCH",
    "DEFAULT_HOST",
    "DEFAULT_LINES_CONFIG",
    "DEFAULT_PORT",
    "DEFAULT_REPO_PATH",
    "DEFAULT_RUN_ROOT",
    "SCHEMA_VERSION",
    "FleetStateConfig",
    "FleetStateHTTPServer",
    "FleetStateView",
    "serve",
]
