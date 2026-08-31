"""The supervisor's event observer, parasitic on the scheduler's own tick.

There is deliberately **no second while-sleep here** (D9, r4-design §5). The
scheduler already wakes every 60s; this module is a set of read-only scans it
performs while awake, plus a launcher call when a scan finds something worth
an audit. The supervisor graph itself runs as yet another transient unit --
a *scheduled* thing, never a second scheduler.

Eight scans, one per event (r4-design §1; E5-E8 consume the read-model):

- **E1** board question with no decision referencing it -- incremental pull
  over `board:work-notes`, cursor persisted next to the stall-state files.
- **E2** terminal `blocked` + `waiting_on: "decision"` -- reads the same
  terminal.json the tick already reads. Parking (R0c) is untouched: the line
  stays parked exactly as before; this observer only hands the *fact* to the
  supervisor graph so a human gets an audit report next to the question.
- **E3** terminal `fault` / `pump_fault: true` -- same file.
- **E4** `TickResult.refusal == TOTAL_CAP_REACHED` -- in-process, straight
  from the tick's own results; deduped per cap window so a breaker that
  holds for an hour is one audit, not sixty.
- **E5/E6/E7/E8** read-model scans -- a stdlib HTTP client pulls the loopback
  state read-model (`127.0.0.1:7494`) `/v1/harvestable`, `/v1/lines`,
  `/v1/decisions`, `/v1/enrollments` and derives the events from the *synthetic
  snapshots*, never from heartbeat/terminal/bus/bridge files. The M1 read-model
  is the only data face these events have (spec: 「这三事件禁止重扫
  heartbeat/terminal/bus/bridge 文件（一律经 :7494）」). E8 additionally
  appends an over-age reminder attempt: a pending enrollment older than
  `enrollment_stale_threshold_seconds` (24h undecided) re-fires as
  `enroll:{folder_id}:g{n}` -- the ronin generation semantics, so a reminder is
  a fresh audit thread, never a replay of the original application's.

Two budgets, both plain counters (absorbed from the old supervisor's action
window -- the one part of its self-restraint worth keeping): at most
`max_launches_per_tick` supervisor runs per tick, and at most
`max_attempts_per_key` lifetime launches per event key. A supervisor that can
flood is a supervisor someone will turn off.

Failure discipline is the parking one: every scan fails open. A bus outage,
an unreadable cursor, or an unreachable read-model (:7494) costs observation,
never scheduling -- `after_tick` cannot raise.
"""

from __future__ import annotations

import contextlib
import json
import shlex
import time
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from fleet_graph.bus.board import NOTE_KIND, WORK_NOTES, Board, GateTicket
from fleet_graph.bus.client import BusClient
from fleet_graph.scheduler.ignition import DEFAULT_CAP_WINDOW_SECONDS, Refusal
from fleet_graph.scheduler.launcher import TransientLauncher
from fleet_graph.supervise.events import (
    EVENT_BOARD_QUESTION,
    SupervisorEvent,
    approved_unharvested_event,
    blocked_decision_event,
    board_question_event,
    cap_breaker_event,
    decision_swallowed_event,
    enrollment_pending_event,
    heartbeat_stale_event,
    line_fault_event,
)

DEFAULT_SUPERVISOR_STATE_ROOT = Path("/data/fleet-graph/supervisor")
DEFAULT_UNIT_PREFIX = "fleet-graph-supervisor"

#: The M1 state read-model the E5/E6/E7 scans consume (loopback).
DEFAULT_READ_MODEL_BASE_URL = "http://127.0.0.1:7494"

#: E6 staleness threshold: heartbeat_age_s strictly greater than this.
DEFAULT_HEARTBEAT_STALE_THRESHOLD_SECONDS = 300.0

#: E8 staleness threshold: a pending enrollment undecided for longer than this
#: (default 24h) gets an additional reminder attempt (``enroll:{folder_id}:g{n}``).
DEFAULT_ENROLLMENT_STALE_THRESHOLD_SECONDS = 24 * 60 * 60

#: How many board messages one tick will page through at most.
BOARD_PAGE_LIMIT = 200

#: 与 supervise/decision_publisher.DECISION_TOKEN_ENV 同值（测试钉死相等）。
#: 不 import 那个模块——Guard C 规定唯一 importer 是 supervisor act 节点，
#: 调度层要的只是这个名字，不是发布入口。
DECISION_TOKEN_ENV = "FLEET_GRAPH_DECISION_TOKEN_FILE"


def _http_get_json(url: str, timeout: float = 5.0) -> dict[str, Any] | None:
    """GET one read-model view; None on any failure (fail-open).

    The read-model is loopback-only, so the request explicitly bypasses
    HTTP(S)_PROXY: a proxy in the daemon environment must never try to route
    a 127.0.0.1 call (spec 交付 C: 回环,显式绕过 HTTP(S)_PROXY).
    """
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as response:
            raw = response.read()
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def observer_environment(
    line_environment: dict[str, str], daemon_environ: Mapping[str, str]
) -> dict[str, str]:
    """The env a supervisor transient unit gets: the lines' env, plus the
    decision credential -- and only here.

    The credential comes from the daemon's own environment (the systemd
    EnvironmentFile), never from the config's line_environment: putting it
    there would hand every line pump the key the fourth gate exists to keep
    away from lines. agent children are scrubbed either way
    (executors/agent_run.py), but a pump process has no business holding it
    at all.
    """
    env = dict(line_environment)
    token_file = daemon_environ.get(DECISION_TOKEN_ENV, "")
    if token_file:
        env[DECISION_TOKEN_ENV] = token_file
    return env


@dataclass(frozen=True)
class SupervisorLaunchSpec:
    """argv for one short-run `fleet-graph supervisor run` transient unit.

    Duck-typed against TransientLauncher's LaunchSpec surface (`argv()`,
    `unit_name`, `log_file`) rather than subclassing it: the launcher stays
    exactly as reviewed, and this spec cannot accidentally inherit line
    semantics like generations or acceptance declarations.
    """

    event: SupervisorEvent
    run_root: Path
    state_root: Path = DEFAULT_SUPERVISOR_STATE_ROOT
    unit_prefix: str = DEFAULT_UNIT_PREFIX
    working_directory: str = "/data/apps/fleet-graph/current"
    executable: str = "/data/apps/fleet-graph/current/.venv/bin/fleet-graph"
    environment: dict[str, str] = field(default_factory=dict)
    log_path: Path | None = None
    #: M3 harvest (E5): observer-side passthrough of the `supervisor run`
    #: harvest write flags. All default empty/None -- an unconfigured observer
    #: emits no harvest flag, so the run keeps its deny-all + 'main' defaults
    #: (零放宽).
    harvest_allowlist_path: str | None = None
    harvest_default_branch: str | None = None
    harvest_deploy: tuple[str, ...] = ()
    repo: str | None = None

    @property
    def unit_name(self) -> str:
        # Stable per event key -- a re-launch while the previous attempt is
        # still running collides on the unit name and fails loudly instead of
        # double-running the same audit.
        return f"{self.unit_prefix}-{self.event.key}"

    @property
    def log_file(self) -> Path:
        return self.log_path or Path(f"/data/fleet-graph/logs/supervisor-{self.event.key}.log")

    def argv(self) -> list[str]:
        argv = [
            "systemd-run",
            "--user",
            "--collect",
            "--unit",
            self.unit_name,
            f"--working-directory={self.working_directory}",
        ]
        for key, value in sorted(self.environment.items()):
            argv += [f"--setenv={key}={value}"]
        argv += [
            f"--property=StandardOutput=append:{self.log_file}",
            f"--property=StandardError=append:{self.log_file}",
            self.executable,
            "supervisor",
            "run",
            "--event-json",
            json.dumps(self.event.as_dict(), ensure_ascii=False, sort_keys=True),
            "--run-root",
            str(self.run_root),
            "--state-root",
            str(self.state_root),
        ]
        # M3 harvest (E5): 在 --state-root 之后按需追加（词法顺序稳定，测试按
        # `in argv` 断言）。全部缺省 None/空 → 不发射任何 harvest 旗标，保持
        # deny-all 默认拒绝语义零放宽。
        if self.harvest_allowlist_path is not None:
            argv += ["--harvest-allowlist", self.harvest_allowlist_path]
        if self.harvest_default_branch is not None:
            argv += ["--harvest-default-branch", self.harvest_default_branch]
        for word in self.harvest_deploy:
            # cli `--harvest-deploy` 是 action="append"：每个词一个旗标。
            argv += ["--harvest-deploy", word]
        if self.repo is not None:
            argv += ["--repo", self.repo]
        return argv


@dataclass
class ObserverConfig:
    run_root: Path
    supervisor_state_root: Path = DEFAULT_SUPERVISOR_STATE_ROOT
    #: Cursor + attempt counters, next to the scheduler's stall-state files
    #: and under the same discipline: it must survive a daemon restart, and
    #: deleting it is the documented reset.
    cursor_path: Path | None = None
    max_launches_per_tick: int = 2
    max_attempts_per_key: int = 3
    cap_window_seconds: float = DEFAULT_CAP_WINDOW_SECONDS
    #: The M1 read-model base URL the E5/E6/E7 scans consume. Loopback only;
    #: the fetch explicitly bypasses HTTP(S)_PROXY (see _http_get_json).
    read_model_base_url: str = DEFAULT_READ_MODEL_BASE_URL
    #: E6 threshold: a line is stale when its heartbeat_age_s is strictly
    #: greater than this (read-model /v1/lines).
    heartbeat_stale_threshold_seconds: float = DEFAULT_HEARTBEAT_STALE_THRESHOLD_SECONDS
    #: E8 threshold: a pending enrollment undecided for longer than this gets
    #: an additional reminder attempt (read-model /v1/enrollments).
    enrollment_stale_threshold_seconds: float = DEFAULT_ENROLLMENT_STALE_THRESHOLD_SECONDS
    unit_prefix: str = DEFAULT_UNIT_PREFIX
    working_directory: str = "/data/apps/fleet-graph/current"
    executable: str = "/data/apps/fleet-graph/current/.venv/bin/fleet-graph"
    environment: dict[str, str] = field(default_factory=dict)
    #: M3 harvest (E5): 透传给 SupervisorLaunchSpec（argv）。全部缺省
    #: None/空 → 不发射任何 harvest 旗标，保持 deny-all 默认拒绝语义零放宽。
    harvest_allowlist_path: str | None = None
    harvest_default_branch: str | None = None
    harvest_deploy: list[str] = field(default_factory=list)
    repo: str | None = None

    @property
    def resolved_cursor_path(self) -> Path:
        return self.cursor_path or (self.run_root / ".scheduler" / "supervisor-cursor.json")


class SupervisorObserver:
    def __init__(
        self,
        config: ObserverConfig,
        *,
        launcher: TransientLauncher,
        bus: BusClient | None = None,
        units: Any = None,
        observe: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.time,
        read_model: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self.config = config
        self.launcher = launcher
        self.bus = bus
        #: UnitProbe-shaped; None skips the liveness check. An audit already
        #: in flight must not burn a lifetime attempt on a name collision.
        self.units = units
        self.observe = observe
        self.clock = clock
        #: Synthetic-snapshot fetcher for the E5/E6/E7 read-model scans. Takes
        #: a view path ("/v1/lines") and returns the parsed JSON body or None
        #: on any failure (fail-open). Defaults to the stdlib HTTP client
        #: against config.read_model_base_url; tests inject a fake snapshot.
        self.read_model = read_model or self._default_read_model_fetcher()
        #: 交付 B (P3): last tick's aggregated "no progress" batch, so an
        #: identical batch is not reprinted (去重打印，不重复刷屏).
        self._last_no_progress_batch: tuple[tuple[str, int], ...] | None = None

    # --- persisted cursor state ------------------------------------------

    def _default_read_model_fetcher(self) -> Callable[[str], dict[str, Any] | None]:
        base_url = self.config.read_model_base_url.rstrip("/")

        def fetch(path: str) -> dict[str, Any] | None:
            return _http_get_json(f"{base_url}{path}")

        return fetch

    def _load_state(self) -> dict[str, Any]:
        empty: dict[str, Any] = {"board_seq": None, "attempts": {}}
        try:
            raw = json.loads(self.config.resolved_cursor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return empty
        if not isinstance(raw, dict):
            return empty
        attempts = raw.get("attempts")
        state = {
            "board_seq": raw.get("board_seq"),
            "attempts": dict(attempts) if isinstance(attempts, dict) else {},
        }
        baseline = raw.get("e7_baseline")
        if isinstance(baseline, list):
            # E7 水位（spec 交付 A.1）：已观测（已审计）的 swallowed
            # source_message_id 有序列表。只认合法列表；键缺失/损坏 = 首跑语义。
            state["e7_baseline"] = [str(x) for x in baseline]
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        path = self.config.resolved_cursor_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        except OSError:
            # Losing the cursor costs re-observation, not correctness: event
            # keys are idempotent all the way down (thread id, unit name,
            # receipt file), so a replayed event re-adopts and no-ops.
            pass

    # --- the tick hook ----------------------------------------------------

    def after_tick(
        self,
        *,
        now: float,
        folder_ids: Iterable[str],
        terminal_reader: Callable[[str], dict[str, Any] | None],
        tick_results: Iterable[Any],
    ) -> list[dict[str, Any]]:
        """Scan, budget, launch. Never raises; returns what it did for the log."""
        actions: list[dict[str, Any]] = []
        try:
            state = self._load_state()
            launched = 0

            # Terminal-derived events first: they are re-derivable every tick,
            # so deferring them to the next tick is free, whereas the board
            # cursor should only advance past questions we actually handled.
            events: list[SupervisorEvent] = []
            new_e7: dict[str, str] = {}
            try:
                events.extend(self._terminal_events(folder_ids, terminal_reader))
            except Exception as exc:  # fail open
                actions.append({"source": "terminals", "error": repr(exc)[:200]})
            try:
                events.extend(self._cap_events(tick_results, now))
            except Exception as exc:  # fail open
                actions.append({"source": "cap", "error": repr(exc)[:200]})
            try:
                # E5-E8 read-model scans share the same budget + attempt
                # counters as E2/E3/E4; an unreachable :7494 is a skipped
                # scan (an action note), never a dead tick. The decisions
                # branch also returns which E7 ids are new this tick so the
                # watermark only advances past ids we actually handled.
                read_events, new_e7, read_notes = self._read_model_events(state, now=now)
                events.extend(read_events)
                actions.extend(read_notes)
            except Exception as exc:  # fail open
                actions.append({"source": "read_model", "error": repr(exc)[:200]})

            for event in events:
                if launched >= self.config.max_launches_per_tick:
                    actions.append({"event": event.key, "action": "deferred:tick_budget"})
                    continue
                action = self._consider(event, state)
                actions.append(action)
                if action["action"].startswith("launched"):
                    launched += 1
                # E7 水位推进纪律（spec 交付 A.4，与 E1 游标同）：只有真正处置
                # 的新 id（launched / skipped:receipt_exists /
                # skipped:attempts_exhausted）才推进水位；deferred:tick_budget
                # 或 skipped:audit_in_flight 未处置，下一 tick 重扫重派。
                source_id = new_e7.get(event.key)
                if source_id is not None:
                    self._advance_e7_baseline(state, source_id, action)

            # E1 last, with whatever budget remains. The cursor advances only
            # past messages that were handled (launched, skipped, or not an
            # event); a question deferred by the budget is re-read next tick.
            try:
                remaining = self.config.max_launches_per_tick - launched
                actions.extend(self._board_scan(state, remaining=remaining))
            except Exception as exc:  # fail open
                actions.append({"source": "board", "error": repr(exc)[:200]})

            self._write_state(state)
        except Exception as exc:  # the tick must survive us, whatever happens
            actions.append({"source": "observer", "error": repr(exc)[:200]})
        # 交付 B (P3)：同一 tick 同类「无进展」action 聚合为一条计数，且与上一
        # tick 完全相同的「无进展」批去重打印——只作用于日志面（observe/print），
        # 返回的完整 actions 供调用方/测试逐条核对，不受影响。
        emitted = self._log_dedup(actions)
        if self.observe is not None:
            for action in emitted:
                with contextlib.suppress(Exception):  # telemetry must not bite
                    self.observe({"supervisor_observer": action})
        return actions

    # --- scans ------------------------------------------------------------

    def _terminal_events(
        self,
        folder_ids: Iterable[str],
        terminal_reader: Callable[[str], dict[str, Any] | None],
    ) -> list[SupervisorEvent]:
        events: list[SupervisorEvent] = []
        for folder_id in folder_ids:
            record = terminal_reader(folder_id)
            if record is None or record.get("run_id") is None:
                continue
            run_id = str(record["run_id"])
            terminal = record.get("terminal")
            if terminal == "blocked" and record.get("waiting_on") == "decision":
                events.append(blocked_decision_event(folder_id, run_id))
            elif terminal == "fault" or record.get("pump_fault") is True:
                events.append(line_fault_event(folder_id, run_id))
        return events

    def _cap_events(self, tick_results: Iterable[Any], now: float) -> list[SupervisorEvent]:
        tripped = [
            result
            for result in tick_results
            if getattr(result.decision, "refusal", None) is Refusal.TOTAL_CAP_REACHED
        ]
        if not tripped:
            return []
        bucket = int(now // self.config.cap_window_seconds)
        detail = str(tripped[0].decision.detail or "")
        return [cap_breaker_event(bucket, detail, [r.folder_id for r in tripped])]

    def _read_model_events(
        self, state: dict[str, Any], *, now: float
    ) -> tuple[list[SupervisorEvent], dict[str, str], list[dict[str, Any]]]:
        """E5/E6/E7/E8 from the read-model's synthetic snapshots (:7494).

        The M1 read-model is the only data face these events have: no direct
        heartbeat/terminal/bus/bridge file is re-read here (spec: 「这三事件
        禁止重扫 heartbeat/terminal/bus/bridge 文件（一律经 :7494）」).
        Every view fetch fails open -- an unreachable :7494 skips that scan,
        never the tick.

        Returns ``(events, new_e7, notes)``: the derived events, a mapping of
        each new E7 event key -> its ``source_message_id`` (so ``after_tick``
        can advance the watermark only past ids actually handled), and any
        action notes (e.g. ``cursor_adopted:e7_baseline``).
        """
        events: list[SupervisorEvent] = []
        new_e7: dict[str, str] = {}
        notes: list[dict[str, Any]] = []

        lines = self.read_model("/v1/lines")
        if isinstance(lines, dict):
            for line in lines.get("lines") or []:
                if not isinstance(line, dict):
                    continue
                folder_id = str(line.get("folder_id") or "")
                if not folder_id:
                    continue
                heartbeat_age_s = line.get("heartbeat_age_s")
                if heartbeat_age_s is None:
                    continue
                try:
                    age = float(heartbeat_age_s)
                except (TypeError, ValueError):
                    continue
                # E6: stale heartbeat on a line that has not terminal-ed and
                # is not parked (a parked line waiting on a decision is not a
                # stalled line; the heartbeat restores when it wakes).
                if age <= self.config.heartbeat_stale_threshold_seconds:
                    continue
                if line.get("terminal") is not None:
                    continue
                if line.get("parked") is True:
                    continue
                events.append(
                    heartbeat_stale_event(
                        folder_id,
                        age,
                        line.get("round"),
                        str(line.get("phase") or ""),
                    )
                )

        decisions = self.read_model("/v1/decisions")
        if isinstance(decisions, dict):
            swallowed = [
                decision
                for decision in decisions.get("decisions") or []
                if isinstance(decision, dict)
                and decision.get("state") == "swallowed"
                and str(decision.get("source_message_id") or "")
            ]
            baseline = state.get("e7_baseline")
            if baseline is None:
                # First run adopts the current head as its baseline, the same
                # honest reading as _board_scan (E1 board_question): swallowed
                # decisions from before we were watching are the human's
                # existing backlog, not events we observed. This tick emits no
                # E7 at all (spec 交付 A.2).
                adopted = [str(decision["source_message_id"]) for decision in swallowed]
                state["e7_baseline"] = adopted
                notes.append(
                    {
                        "source": "read_model",
                        "action": f"cursor_adopted:e7_baseline=n={len(adopted)}",
                    }
                )
            else:
                known = set(baseline)
                for decision in swallowed:
                    source_message_id = str(decision["source_message_id"])
                    if source_message_id in known:
                        continue
                    event = decision_swallowed_event(
                        source_message_id, str(decision.get("reason") or "")
                    )
                    events.append(event)
                    new_e7[event.key] = source_message_id

        harvestable = self.read_model("/v1/harvestable")
        if isinstance(harvestable, dict):
            for development in harvestable.get("developments") or []:
                if not isinstance(development, dict):
                    continue
                development_id = str(development.get("development_id") or "")
                if not development_id:
                    continue
                events.append(
                    approved_unharvested_event(
                        development_id,
                        str(development.get("head_commit") or ""),
                        str(development.get("stage") or ""),
                    )
                )

        enrollments = self.read_model("/v1/enrollments")
        if isinstance(enrollments, dict):
            for entry in enrollments.get("enrollments") or []:
                if not isinstance(entry, dict):
                    continue
                folder_id = str(entry.get("folder_id") or "")
                if not folder_id:
                    continue
                # E8: a *pending* application is a fact worth an audit; a
                # decided one (admitted/rejected/withdrawn) is no longer open.
                if str(entry.get("status") or "") != "pending":
                    continue
                submitted_at = str(entry.get("submitted_at") or "")
                events.append(
                    enrollment_pending_event(
                        folder_id,
                        alias=str(entry.get("alias") or ""),
                        submitted_at=submitted_at,
                    )
                )
                # Over-age reminder (spec: pending 超龄 24h 未裁追加提醒 attempt,
                # `{key}:g{n}` 语义沿用): each full staleness period past the
                # first adds a fresh generation key, so an application that is
                # still pending after a day gets its own reminder audit.
                age_s = self._age_seconds(submitted_at, now)
                if age_s is not None and age_s > self.config.enrollment_stale_threshold_seconds:
                    generation = int(age_s // self.config.enrollment_stale_threshold_seconds)
                    events.append(
                        enrollment_pending_event(
                            folder_id,
                            alias=str(entry.get("alias") or ""),
                            submitted_at=submitted_at,
                            reminder_generation=generation,
                        )
                    )

        return events, new_e7, notes

    @staticmethod
    def _age_seconds(submitted_at: str, now: float) -> float | None:
        """The age of an ISO UTC ``submitted_at`` stamp, or None (fail-open).

        Unparseable or empty stamps carry no mechanical age, so no reminder is
        minted from nothing -- the base E8 event still fires for the pending
        application.
        """
        if not submitted_at:
            return None
        try:
            from fleet_graph.scheduler.wake import parse_bus_timestamp

            submitted_epoch = parse_bus_timestamp(submitted_at)
        except (ValueError, TypeError):
            return None
        return max(0.0, now - submitted_epoch)

    def _advance_e7_baseline(
        self, state: dict[str, Any], source_id: str, action: dict[str, Any]
    ) -> None:
        """Advance the E7 watermark past a *handled* new id only (spec 交付 A.4).

        Only launched / skipped:receipt_exists / skipped:attempts_exhausted
        count as handled; deferred:tick_budget and skipped:audit_in_flight do
        not, so the id is re-scanned and re-dispatched next tick.
        """
        outcome = action.get("action", "")
        if not (
            outcome.startswith("launched")
            or outcome.startswith("skipped:receipt_exists")
            or outcome.startswith("skipped:attempts_exhausted")
        ):
            return
        baseline = state.get("e7_baseline")
        if isinstance(baseline, list) and source_id not in baseline:
            baseline.append(source_id)

    def _board_scan(self, state: dict[str, Any], *, remaining: int) -> list[dict[str, Any]]:
        if self.bus is None:
            return []
        actions: list[dict[str, Any]] = []
        board = Board(self.bus)
        cursor = state.get("board_seq")

        if cursor is None:
            # First run adopts the current head as its baseline, the same
            # honest reading account_last_run gives an unwitnessed terminal:
            # questions from before we were watching are the human's existing
            # backlog (`inbox list` shows them), not events we observed.
            _, head_seq = self.bus.messages(WORK_NOTES, limit=1)
            state["board_seq"] = head_seq
            return [{"source": "board", "action": f"cursor_adopted:head_seq={head_seq}"}]

        messages, _head = self.bus.messages(
            WORK_NOTES, after_seq=int(cursor), limit=BOARD_PAGE_LIMIT
        )
        for message in messages:
            seq = int(message["channel_seq"])
            payload = message.get("payload") or {}
            is_question = (
                message.get("kind") == NOTE_KIND and payload.get("note_type") == "question"
            )
            if not is_question:
                state["board_seq"] = seq
                continue
            ticket = GateTicket(
                question_note_id=message["message_id"],
                card_entity_id=str(payload.get("card_entity_id") or ""),
            )
            if board.decision_for(ticket) is not None:
                state["board_seq"] = seq
                continue
            if remaining <= 0:
                # Out of launches this tick: leave the cursor *before* this
                # question so the next tick re-reads it. Board questions are
                # not re-derivable the way terminals are.
                actions.append({"source": "board", "action": "deferred:tick_budget"})
                break
            event = board_question_event(ticket.question_note_id, ticket.card_entity_id)
            action = self._consider(event, state)
            action["source"] = "board"
            actions.append(action)
            if action["action"].startswith("launched"):
                remaining -= 1
            state["board_seq"] = seq
        return actions

    # --- budget + launch --------------------------------------------------

    def _receipt_path(self, event: SupervisorEvent) -> Path:
        return self.config.supervisor_state_root / "reports" / f"{event.key}.json"

    def _consider(self, event: SupervisorEvent, state: dict[str, Any]) -> dict[str, Any]:
        base = {"event": event.key, "type": event.type}
        if self._receipt_path(event).exists():
            return {**base, "action": "skipped:receipt_exists"}

        attempts = int(state["attempts"].get(event.key, 0))
        if attempts >= self.config.max_attempts_per_key:
            return {**base, "action": f"skipped:attempts_exhausted:{attempts}"}

        # The attempt number rides into the event and therefore into the
        # thread identity (`supervisor:{key}:a{n}`) -- each observer launch is
        # a fresh generation with its own checkpoint thread, so a re-run never
        # needs surgery on the shared sqlite. The unit name stays keyed on the
        # event alone: two attempts cannot run concurrently.
        spec = self._spec_for(replace(event, attempt=attempts + 1))
        if self.units is not None:
            try:
                if self.units.is_active(spec.unit_name):
                    # An audit for this event is still running; do not burn a
                    # lifetime attempt on a guaranteed name collision.
                    return {**base, "action": "skipped:audit_in_flight"}
            except Exception:  # fail open: worst case the launch collides
                pass

        # Counted on the attempt, not on success -- the same reading as the
        # scheduler's breaker: a launch that fails every time must still
        # exhaust its budget rather than retry forever.
        state["attempts"][event.key] = attempts + 1
        result = self.launcher.launch(spec)
        return {
            **base,
            "action": "launched" if result.started else "launch_failed",
            "unit": result.unit_name,
            "detail": result.detail[:200],
            "attempt": attempts + 1,
        }

    def _spec_for(self, event: SupervisorEvent) -> SupervisorLaunchSpec:
        # The decision credential rides into the config environment once, for
        # every event (cli.py/observer_environment). E1's board_question is the
        # one event type whose transient unit can reach the decision publisher,
        # and the spec makes it observation-only: it must not publish a
        # decision, so it must not hold the credential (E1 receives no decision
        # credential). Strip it here, per event, rather than letting it into
        # any board_question unit's --setenv.
        environment = dict(self.config.environment)
        if event.type == EVENT_BOARD_QUESTION:
            environment.pop(DECISION_TOKEN_ENV, None)
        return SupervisorLaunchSpec(
            event=event,
            run_root=self.config.run_root,
            state_root=self.config.supervisor_state_root,
            unit_prefix=self.config.unit_prefix,
            working_directory=self.config.working_directory,
            executable=self.config.executable,
            environment=environment,
            harvest_allowlist_path=self.config.harvest_allowlist_path,
            harvest_default_branch=self.config.harvest_default_branch,
            harvest_deploy=tuple(self.config.harvest_deploy),
            repo=self.config.repo,
        )

    def _log_dedup(self, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """交付 B (P3): 日志去噪——同一 tick 内同类「无进展」action 聚合为一条
        计数（如 ``skipped:receipt_exists:x<n>``），且与上一 tick 完全相同的
        「无进展」批去重打印（不重复刷屏）。

        Only the *log surface* is touched: the returned ``actions`` from
        ``after_tick`` stay complete for callers/tests; what ``observe`` prints
        is this deduplicated view. A "no progress" action is one whose action
        carries no forward movement (``deferred:*`` / ``skipped:*``).
        """
        no_progress: list[dict[str, Any]] = []
        progress: list[dict[str, Any]] = []
        for action in actions:
            if str(action.get("action", "")).startswith(("deferred:", "skipped:")):
                no_progress.append(action)
            else:
                progress.append(action)

        if not no_progress:
            return actions

        # Aggregate the same action kind within this tick into one counted row.
        first: dict[str, dict[str, Any]] = {}
        counts: dict[str, int] = {}
        for action in no_progress:
            kind = str(action.get("action", ""))
            first.setdefault(kind, action)
            counts[kind] = counts.get(kind, 0) + 1
        aggregated: list[dict[str, Any]] = []
        for kind, count in sorted(counts.items()):
            if count == 1:
                aggregated.append(first[kind])
            else:
                aggregated.append({**first[kind], "action": f"{kind}:x{count}"})

        # A batch identical to the previous tick is dropped (去重打印).
        batch = tuple(a["action"] for a in aggregated)
        if batch == self._last_no_progress_batch:
            return progress
        self._last_no_progress_batch = batch
        return progress + aggregated

    def describe(self, event: SupervisorEvent) -> str:
        return shlex.join(self._spec_for(event).argv())


# --- documented reset -------------------------------------------------------


def reset_supervisor_event(
    key: str,
    *,
    state_root: Path,
    cursor_path: Path,
    board_seq: int | None = None,
    bus: Any = None,
) -> dict[str, Any]:
    """Reset one event key's supervisor-side state so the observer re-fires it.

    Replaces the four-step surgery of 2026-08-28 (delete receipt, sqlite rows,
    cursor attempts, rewind board_seq, restart daemon) with the two steps that
    are still real under attempt-in-thread-identity:

    - delete the receipt (`reports/<key>.json`) -- the observer's "done" mark.

    The cursor's `attempts[<key>]` is deliberately **kept**: the attempt
    counter is exactly what makes the next launch a fresh thread
    (`supervisor:{key}:a{n+1}`). Clearing it re-derives the same `a{n}` and
    the relaunch lands on the old thread's terminal checkpoint as
    `resumed:already_complete` -- observed live on
    e1-msg_01M12MRW680AJZJH40182FXYW1 the very first time this command was
    used in production. The checkpoint db is untouched for the same reason:
    old threads' rows are inert once the attempt moves on. The budget cost is
    honest: a reset consumes lifetime attempts; raise max_attempts_per_key if
    an event legitimately needs many reruns.

    `board_seq` rewinding only matters for E1 (E2/E3 re-derive from terminals
    every tick, E4 from tick results): an explicit value wins; otherwise an
    `e1-<note_id>` key is located mechanically on the bus (message ->
    channel_seq -> cursor lands just before it), and when neither is possible
    the summary says so instead of guessing. The cursor is only ever moved
    *backwards* -- both on the mechanical path and on the explicit
    `--board-seq` path, where a higher value is clamped to the current cursor
    instead of skipping unprocessed questions. Re-running the reset is a
    no-op.

    Idempotent, and touches nothing but the supervisor's own state surface.
    No daemon restart is required: the observer reloads the cursor file at the
    start of every tick (`after_tick` -> `_load_state`).

    E7 (spec 交付 A.5): 需重审某历史 E7 key 时，删除 cursor 中的
    `e7_baseline`（整体删 cursor 文件仍为文档化 reset）——水位重建即重扫当前
    快照全部 swallowed。本命令不代删该键：E7 历史 key 重审是显式水位重建，
    与 receipt/attempt 的机械语义不同。
    """
    summary: dict[str, Any] = {"key": key}

    receipt = state_root / "reports" / f"{key}.json"
    try:
        receipt.unlink()
        summary["receipt"] = f"deleted:{receipt}"
    except FileNotFoundError:
        summary["receipt"] = "absent"

    try:
        raw = json.loads(cursor_path.read_text(encoding="utf-8"))
        state = raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        state = {}
    attempts = state.get("attempts")
    if not isinstance(attempts, dict):
        attempts = {}
    summary["attempts"] = (
        f"kept:{attempts[key]} (next launch is a{attempts[key] + 1})"
        if key in attempts
        else "absent"
    )
    state["attempts"] = attempts

    current_seq = state.get("board_seq")
    if board_seq is not None:
        target = int(board_seq)
        if isinstance(current_seq, int) and target > current_seq:
            # Never move the cursor forward: an operator-supplied value past
            # the current cursor would skip unprocessed questions. The explicit
            # path follows the same discipline as the mechanical path (which
            # only ever moves backwards).
            state["board_seq"] = current_seq
            summary["board_seq"] = (
                f"not_moved_forward:{current_seq} (explicit --board-seq {target} is higher)"
            )
        else:
            state["board_seq"] = target
            summary["board_seq"] = f"set:{target}"
    elif not key.startswith("e1-"):
        summary["board_seq"] = "not_applicable:terminal/cap events re-derive every tick"
    elif bus is None:
        summary["board_seq"] = "not_rewound:no bus client; pass --board-seq explicitly"
    else:
        question_note_id = key[len("e1-") :]
        try:
            message = bus.message(WORK_NOTES, question_note_id)
        except Exception as exc:
            message = None
            summary["board_seq"] = (
                f"not_rewound:bus lookup failed ({type(exc).__name__}); pass --board-seq"
            )
        if message is not None:
            target = int(message["channel_seq"]) - 1
            if isinstance(current_seq, int) and current_seq > target:
                state["board_seq"] = target
                summary["board_seq"] = f"rewound:{current_seq}->{target}"
            else:
                summary["board_seq"] = f"already_at_or_before:{current_seq}"
        elif "board_seq" not in summary:
            summary["board_seq"] = (
                f"not_rewound:note {question_note_id!r} not found in {WORK_NOTES}; pass --board-seq"
            )

    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    cursor_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    summary["cursor_path"] = str(cursor_path)
    return summary


__all__ = [
    "BOARD_PAGE_LIMIT",
    "DEFAULT_ENROLLMENT_STALE_THRESHOLD_SECONDS",
    "DEFAULT_HEARTBEAT_STALE_THRESHOLD_SECONDS",
    "DEFAULT_READ_MODEL_BASE_URL",
    "DEFAULT_SUPERVISOR_STATE_ROOT",
    "ObserverConfig",
    "SupervisorLaunchSpec",
    "SupervisorObserver",
    "observer_environment",
    "reset_supervisor_event",
]
