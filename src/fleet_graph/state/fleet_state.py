"""M1 传感层薄版——fleet-state read-model (`127.0.0.1:7494`).

一个**只读** HTTP 服务：数据源一律 pull、一律只读（heartbeat / terminal /
dd status / decision-bridge 的 bridge.sqlite3，以及可选的 agent-bus
``work.decision.v1``），不新造生产者、不写任何被观察工件。实现集中在
这一个服务进程内；两个视图：

- ``GET /v1/lines`` → ``{"schema_version": <str>, "lines": [...]}``
- ``GET /v1/decisions`` → ``{"schema_version": <str>, "decisions": [...]}``
- ``GET /v1/harvestable`` → ``{"schema_version": <str>, "developments": [...]}``
- ``GET /v1/enrollments`` → ``{"schema_version": <str>, "enrollments": [...]}``

铁律（本模块与规格一致）：

- 主键列表字段名严格为 ``lines`` / ``decisions`` / ``developments`` /
  ``enrollments``；``schema_version`` 必填。
- **读失败降级不 5xx 全链**：单个工件缺失/解析失败只对该条目标记
  absent/unknown，绝不让整表挂掉。
- 机械事实只读：``heartbeat_age_s`` = 现在 - heartbeat.json 的 ``updated_at``；
  ``parked`` = ``waiting_on == "decision"``（见 ``normalize_waiting_on``）；
  ``wake_facts`` 至少含 ``waiting_on`` 等机械事实。
- 驻停声明按 run 一致性门控：``terminal.json.run_id == heartbeat.json.run_id``
  时该声明才属活 run；否则 ``wake_facts_stale=true`` 且顶层 ``run_id`` 暴露
  **活 run** 的 run_id（方向 b：保留历史声明，让消费者机械可判）。
- ``release_id`` 只消费 heartbeat.json 已持久化的值（line 进程启动时冻结），
  read 路径**绝不**重新 realpath 部署 ``current`` 符号链接——那是进程 exec 时
  解析一次的机械事实，不是链接当下指向。

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
#: The goal service's enrollment pending queue (``enroll-queue.jsonl``), read
#: read-only by ``/v1/enrollments``. None means "no enrollments" (the view
#: degrades to an empty list, never a 5xx) -- the same convention as the other
#: optional data sources.
DEFAULT_ENROLL_QUEUE = Path("/data/fleet-graph/goal/enroll-queue.jsonl")

#: 送达链状态（closed）。
STATE_PUBLISHED = "published"
STATE_BRIDGED = "bridged"
STATE_CONSUMED = "consumed"
STATE_SWALLOWED = "swallowed"

#: E5 收割回执（harvest receipt）约定：监督面收割部署后，在单卡固定挂
#: ``note_type=evidence`` 且 ``idempotency_key`` 以 ``evidence-`` 开头的回执。
#: ``harvestable()`` 用它判「已收割」；bus 读面不暴露 idempotency_key 时退化为
#: 只看 evidence note 在场（可注入实现负责精确判定）。
HARVEST_RECEIPT_NOTE_TYPE = "evidence"
HARVEST_RECEIPT_KEY_PREFIX = "evidence-"

#: E5 首跑基线水位文件（方向 B，照抄 E7 ``e7_baseline`` 先例）：首次采存量
#: complete 集合为基线、一次性出清历史；此后只审新增 complete。落在 scheduler
#: 状态目录（read-model 已读 ``.scheduler/``），删文件即文档化重扫。
E5_BASELINE_FILE = "e5-baseline.json"

#: bridge receipt status → 送达链状态 +（swallowed 时）reason。
#: ``intent_recorded`` 即 bridge 已见过该裁决（bridged）；``resumed`` 即已送达
#: 消费（consumed）；``noop`` / ``refused`` 即被吞（swallowed，带 reason）。
#: 终结态不再由这张快照单独拍板：consumed/swallowed 还会拿单据侧对账兜一道
#: （见 ``_receipt_to_decision`` / ``_document_gate_consumed``）。
_RECEIPT_STATE: dict[str, tuple[str, str | None]] = {
    "intent_recorded": (STATE_BRIDGED, None),
    "resumed": (STATE_CONSUMED, None),
    "noop": (STATE_SWALLOWED, None),
    "refused": (STATE_SWALLOWED, None),
}

#: dd 单据侧对账的机械事实词汇。human gate 走完（events.jsonl ``human_gate`` +
#: ``success``）或 status.json 离开 ``awaiting_gate``，都证明该裁决真被消费，
#: 而不是 bridge receipt 在某个评估瞬间看到的快照。
GATE_STAGE = "human_gate"
GATE_SUCCESS_EVENT = "success"
DD_AWAITING_GATE = "awaiting_gate"
#: The dd pipeline's merge stage id (lifecycle: ... -> human_gate -> merger).
#: The harvest reactor (E5 ``approved_unharvested``) must fire only *after* the
#: merge stage completes -- not after the gate approves (spec item 5 / S7: 收割
#: 触发点从「闸后」改到「merge 后」). A `complete` terminal that is not at this
#: stage is a gate-only (or corrupt) state and is not harvestable.
DD_MERGE_STAGE = "merger"

#: 对账 basis 词汇（closed）。前两者 = 单据侧证明被消费；``document_awaiting``
#: = 单据侧可读但仍停留在 waiting（真没消费）；``unreconciled`` = 有 dd 目标
#: 但单据侧缺失/不可读，显式标注、不静默归 consumed 或 swallowed；``receipt``
#: = 无 dd 目标（或非终结态），沿用 bridge receipt 快照。
BASIS_HUMAN_GATE_SUCCESS = "human_gate_success"
BASIS_LEFT_AWAITING_GATE = "left_awaiting_gate"
BASIS_DOCUMENT_AWAITING = "document_awaiting"
BASIS_UNRECONCILED = "unreconciled"
BASIS_RECEIPT = "receipt"


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
    #: The goal enrollment pending queue the /v1/enrollments view reads.
    #: Defaults to the goal service's own queue home
    #: (``/data/fleet-graph/goal/enroll-queue.jsonl``), the same home goal
    #: serve writes by default, so the read model observes the actual queue
    #: rather than going blind. ``None`` keeps the view empty (explicit
    #: opt-out; degrade, never 5xx).
    enroll_queue_path: Path | None = DEFAULT_ENROLL_QUEUE
    clock: Callable[[], float] = time.time
    #: E5 收割回执判定，``(card_entity_id) -> bool``，可注入（默认读 bus work-notes；
    #: 任何读取失败降级为「未收割」，绝不 5xx）。
    has_harvest_receipt: Callable[[str], bool] | None = None
    #: E5 首跑基线水位文件路径；None 用 ``<run_root>/.scheduler/e5-baseline.json``。
    harvest_baseline_path: Path | None = None


def _read_json(path: Path) -> dict[str, Any] | None:
    """One artifact, or None on missing/unreadable/unparseable (never raises)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """One JSONL artifact, fail-soft: bad lines degrade away, never a crash."""
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _generation_events_path(dd_root: Path, development_id: str, generation: int) -> Path:
    """Where one generation's ``events.jsonl`` lives (g1 at the dev root)."""
    dev_root = dd_root / development_id
    if generation and generation <= 1:
        return dev_root / "events.jsonl"
    return dev_root / f"g{generation}" / "events.jsonl"


def _document_gate_consumed(dd_root: Path, development_id: str, generation: int) -> str:
    """对账单个 dd 单据：该裁决是否真被消费（而非 receipt 瞬时快照）。

    Returns one of the ``BASIS_*`` tokens:
      - ``human_gate_success`` / ``left_awaiting_gate`` → 单据侧证明消费；
      - ``document_awaiting`` → 单据侧可读、仍在等待，真没消费；
      - ``unreconciled`` → 单据侧缺失/不可读，显式标注对不上。
    顺序：先看 status.json（权威重建态）是否已离开 ``awaiting_gate``，再看
    events.jsonl 是否记了 ``human_gate`` + ``success``。
    """
    dev_root = dd_root / development_id
    if not dev_root.is_dir():
        return BASIS_UNRECONCILED
    status = _read_json(dev_root / "status.json")
    state = str(status.get("state") or "") if status else ""
    if state and state != DD_AWAITING_GATE:
        return BASIS_LEFT_AWAITING_GATE
    for entry in _read_jsonl(_generation_events_path(dd_root, development_id, generation)):
        if entry.get("stage") == GATE_STAGE and entry.get("event") == GATE_SUCCESS_EVENT:
            return BASIS_HUMAN_GATE_SUCCESS
    if status is not None and state == DD_AWAITING_GATE:
        return BASIS_DOCUMENT_AWAITING
    return BASIS_UNRECONCILED


def _development_for_card(dd_root: Path, card_entity_id: str) -> str | None:
    """反查 ``card_entity_id`` 所属的 development（M0 补 owner 分支）。

    遍历 ``dd_root/*/record.json``（``card_entity_id`` 字段），命中则返回该单的
    ``development_id``（缺省回退目录名）；命中不到返回 None（refs 空 / 卡片错配）。
    只读、fail-soft：根缺失/记录不可读一律视为「查不到」，绝不抛异常、绝不一串
    `400` 把整表挂掉。
    """
    if not card_entity_id:
        return None
    try:
        entries = sorted(dd_root.iterdir(), key=lambda p: p.name)
    except OSError:
        return None
    for entry in entries:
        if not entry.is_dir():
            continue
        record = _read_json(entry / "record.json")
        if record is None:
            continue
        if str(record.get("card_entity_id") or "") == card_entity_id:
            return str(record.get("development_id") or entry.name)
    return None


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


def _read_enroll_queue(config: FleetStateConfig) -> list[dict[str, Any]]:
    """The goal enrollment pending queue, fail-soft (never 5xx).

    Re-reads ``enroll-queue.jsonl`` on every request (与 ``_read_roster`` 同法):
    each line is one application's current state. A missing queue, an
    unreadable file, or a bad line degrades that entry -- never the whole
    table. The default ``enroll_queue_path`` is the goal service's own queue
    home (``/data/fleet-graph/goal/enroll-queue.jsonl``); an explicit ``None``
    keeps the view empty (the safe opt-out reading).
    """
    path = config.enroll_queue_path
    if path is None or not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        # An application without a folder_id is not an application; degrade
        # the entry away rather than surfacing a shapeless row.
        if not record.get("folder_id"):
            continue
        out.append(record)
    return out


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


def _receipt_to_decision(receipt: dict[str, Any], *, dd_root: Path | None = None) -> dict[str, Any]:
    status = str(receipt.get("status") or "")
    state, reason = _RECEIPT_STATE.get(status, (STATE_SWALLOWED, f"unknown_status:{status}"))
    basis = BASIS_RECEIPT

    owner_kind = str(receipt.get("target_kind") or "")
    owner_id = str(receipt.get("target_id") or "")

    # 终结对账：bridge receipt 定终结态不再单凭快照——有 dd 目标时拿单据侧
    # （events.jsonl human_gate success / status.json 离开 awaiting_gate）再对
    # 一声。单据侧证明被消费 → 提升为 consumed（修正「已送达且被消费却误记
    # swallowed」）；可读但仍 waiting → 维持 swallowed 并标注；对不上 → 显式
    # 标注 unreconciled，不静默归 consumed 或 swallowed。
    #
    # M0 补口：当 target_id 为空（bridge 在评估瞬间无法把裁决归到某个等待方），
    # 用 card_entity_id 反查所属 development 把 owner.kind/id 补上（只补 owner
    # 并据此提升，绝不把真丢误提为 consumed）。
    if dd_root is not None and state in (STATE_CONSUMED, STATE_SWALLOWED):
        target_kind = str(receipt.get("target_kind") or "")
        target_id = str(receipt.get("target_id") or "")
        resolved_kind = target_kind if target_id else ""
        resolved_id = target_id
        if not (resolved_kind == "dd" and resolved_id) and not target_id:
            development_id = _development_for_card(
                dd_root, str(receipt.get("card_entity_id") or "")
            )
            if development_id is not None:
                resolved_kind = "dd"
                resolved_id = development_id
                owner_kind = "dd"
                owner_id = development_id
        if resolved_kind == "dd" and resolved_id:
            basis = _document_gate_consumed(
                dd_root, resolved_id, int(receipt.get("generation") or 1)
            )
            if basis in (BASIS_HUMAN_GATE_SUCCESS, BASIS_LEFT_AWAITING_GATE):
                state = STATE_CONSUMED
                reason = None

    obj: dict[str, Any] = {
        "source_message_id": str(receipt.get("source_message_id") or ""),
        "state": state,
        "basis": basis,
        "owner": {
            "kind": owner_kind,
            "id": owner_id,
            "generation": receipt.get("generation"),
        },
    }
    if state == STATE_SWALLOWED:
        reason = str(receipt.get("reason") or reason or state)
        obj["reason"] = reason
    return obj


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


def _default_has_harvest_receipt(config: FleetStateConfig) -> Callable[[str], bool]:
    """The default E5 receipt reader: an evidence note on the card (read-only).

    The harvest receipt is an ``evidence`` note whose idempotency key starts
    with ``evidence-``. The agent-bus read surface does not expose
    ``idempotency_key`` on messages, so the best the default can confirm is an
    evidence note present on the card (the ``evidence-`` prefix is enforced by
    injected implementations). Read-only against the optional bus; no bus_url,
    no credential, or any read failure degrades to *unharvested* -- never a
    5xx, never a crash.
    """
    from fleet_graph.bus.board import NOTE_KIND, WORK_NOTES
    from fleet_graph.bus.client import BusClient, load_token

    def has_receipt(card_entity_id: str) -> bool:
        if not config.bus_url or not card_entity_id:
            return False
        try:
            client = BusClient(base_url=config.bus_url, token=load_token())
            messages, _head = client.messages(WORK_NOTES, limit=200)
        except Exception as exc:
            log.debug("state read-model: harvest receipt read skipped: %s", exc)
            return False
        for message in messages:
            payload = message.get("payload") or {}
            if message.get("kind") != NOTE_KIND:
                continue
            if payload.get("note_type") != HARVEST_RECEIPT_NOTE_TYPE:
                continue
            if payload.get("card_entity_id") != card_entity_id:
                continue
            return True
        return False

    return has_receipt


class FleetStateView:
    """Builds the two read-only payloads from the configured data sources."""

    def __init__(self, config: FleetStateConfig) -> None:
        self.config = config
        #: E5 receipt predicate, injectable. Defaults to the read-only bus
        #: reader; any read failure degrades to unharvested, never a 5xx.
        self.has_harvest_receipt = config.has_harvest_receipt or _default_has_harvest_receipt(
            config
        )

    def _e5_baseline_path(self) -> Path:
        return self.config.harvest_baseline_path or (
            self.config.run_root / ".scheduler" / E5_BASELINE_FILE
        )

    def _load_e5_baseline(self) -> set[str] | None:
        """The E5 first-run baseline, or None when it does not exist yet.

        The baseline is the set of development_ids that were complete at first
        observation; those are cleared once and never re-listed. Missing or
        unreadable watermark = first-run semantics (照抄 E7 ``e7_baseline``).
        """
        path = self._e5_baseline_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        ids = raw.get("development_ids") if isinstance(raw, dict) else None
        if not isinstance(ids, list):
            return None
        return {str(dev_id) for dev_id in ids}

    def _write_e5_baseline(self, development_ids: set[str]) -> None:
        """Persist the first-run baseline. Fail-soft: losing it just re-adopts
        the current complete set as baseline next observation."""
        path = self._e5_baseline_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"development_ids": sorted(development_ids)}, sort_keys=True),
                encoding="utf-8",
            )
        except OSError:
            log.debug("state read-model: e5 baseline write skipped: %s", path)

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

            # run 一致性门控（SSoT：run_id 一致才属活 run）：
            # - 顶层 run_id 永远暴露「活 run」的 heartbeat.run_id；
            # - terminal 声明缺失/不可比对/与活 run 不一致 → wake_facts_stale=true，
            #   保留历史声明但让消费者机械可判（方向 b，本卷选路）。
            live_run_id = heartbeat.get("run_id") if heartbeat else None
            declared_run_id = terminal.get("run_id") if terminal else None
            wake_facts_stale = terminal is not None and (
                live_run_id is None or declared_run_id is None or live_run_id != declared_run_id
            )

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
                    "run_id": live_run_id,
                    "wake_facts_stale": wake_facts_stale,
                    # The release this generation actually runs, frozen by the
                    # line process at startup. The read model only consumes the
                    # persisted heartbeat value -- it never re-resolves the
                    # deploy `current` symlink (that would report what the
                    # symlink points at *now*, not what the process exec'd).
                    "release_id": heartbeat.get("release_id") if heartbeat else None,
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
        decision_objs = [_receipt_to_decision(r, dd_root=self.config.dd_root) for r in receipts]
        seen = {obj["source_message_id"] for obj in decision_objs}
        decision_objs.extend(_read_published(self.config, seen))
        return {"schema_version": SCHEMA_VERSION, "decisions": decision_objs}

    def enrollments(self) -> dict[str, Any]:
        """The goal enrollment applications the supervisory face watches.

        Re-reads the goal service's ``enroll-queue.jsonl`` on every request
        (spec 交付 B.1: 与 ``_read_roster`` 同法); bad rows degrade per entry,
        never 5xx. This is the only data face the E8 ``enrollment_pending``
        event has (same source discipline as E5-E7).
        """
        return {"schema_version": SCHEMA_VERSION, "enrollments": _read_enroll_queue(self.config)}

    def harvestable(self) -> dict[str, Any]:
        """The M2 E5 data plane: complete developments with no harvest receipt.

        E5 ``approved_unharvested`` ⇔ ``terminal == "complete"`` **and** the
        card carries no harvest receipt (an ``evidence`` note whose idempotency
        key starts with ``evidence-``). ``refused`` / ``fault`` / any non-complete
        terminal / in-flight developments are **never** listed.

        First-run baseline exemption (direction B, per the E7 ``e7_baseline``
        precedent): the first observation adopts the current complete set as
        baseline and clears it (the historical 147); afterwards only *new*
        complete developments are reviewed, combined with direction A (the
        receipt check) for subsequent harvests. Bad artifacts degrade that
        entry, never the whole table.
        """
        developments: list[dict[str, Any]] = []
        dd_root = self.config.dd_root
        if not dd_root.is_dir():
            return {"schema_version": SCHEMA_VERSION, "developments": developments}

        baseline = self._load_e5_baseline()
        first_run = baseline is None
        if first_run:
            baseline = set()

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
                # Unreadable/missing status degrades the entry away.
                continue
            terminal = str(status.get("terminal") or "")
            # refused / fault / any non-complete terminal / in-flight never
            # listed (目标语义): only `complete` can be approved_unharvested.
            if terminal != "complete":
                continue
            # Harvest fires after the merge stage (spec item 5 / S7), not after
            # the gate: `complete` alone is the pipeline terminal, and in that
            # pipeline it is reached at the merger stage. A `complete` record
            # whose stage is not the merge stage (a gate-only or mis-derived
            # cache) is *not* harvestable -- the trigger point is post-merge,
            # fail-closed toward delaying a harvest rather than reaping early.
            if str(status.get("stage") or "") != DD_MERGE_STAGE:
                continue
            if first_run:
                # 首跑基线豁免：存量 complete 一次性出清，不入列。
                baseline.add(development_id)
                continue
            if development_id in baseline:
                # 已出清的历史 complete：不再重报。
                continue
            card_entity_id = str(record.get("card_entity_id") or "")
            if self.has_harvest_receipt(card_entity_id):
                # 方向 A：卡上有收割回执（evidence note + evidence- 前缀）→ 已收割。
                continue
            developments.append(
                {
                    "development_id": development_id,
                    "head_commit": str(status.get("head_commit") or ""),
                    "stage": str(status.get("stage") or ""),
                    "terminal": terminal,
                }
            )

        if first_run:
            self._write_e5_baseline(baseline)
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
        elif path == "/v1/enrollments":
            payload = self.server.view.enrollments()
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
    "DEFAULT_ENROLL_QUEUE",
    "DEFAULT_HOST",
    "DEFAULT_LINES_CONFIG",
    "DEFAULT_PORT",
    "DEFAULT_RUN_ROOT",
    "SCHEMA_VERSION",
    "FleetStateConfig",
    "FleetStateHTTPServer",
    "FleetStateView",
    "serve",
]
