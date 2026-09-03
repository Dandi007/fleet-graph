"""Real Coordinator and Worker, backed by the agent-runtime CLI.

The graph in goal_line.py talks to two narrow ports. These are the
implementations that reach actual agents, and they are the only place in the
line that knows agent-runtime exists -- INV-4/B8 says every agent run goes
through `agent-run` or `agent-session` and never a directly spawned harness.

Both adapters put their payloads in files rather than argv. `/proc` makes argv
world-readable, and a coordinator input carries the whole inbox.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from fleet_graph.executors.agent_run import (
    AgentRunLauncher,
    AgentRunSpec,
    RunStatus,
    _classify,
    derive_run_id,
    find_result,
)
from fleet_graph.executors.agent_session import (
    AgentSessionSeat,
    SeatHandle,
    read_session_meta,
)
from fleet_graph.state.run_artifacts import write_json_durable
from fleet_graph.work_report import (
    MEDIA_TYPE_PLAIN,
    ReportProtocolError,
    decode_report,
    validate_attachment,
)

#: Upper bound on derived coordinator attempts per round. Failures normally
#: fault the line well before this; the bound only stops a pathological spin.
MAX_COORDINATOR_ATTEMPTS = 8

DISPATCHER = "fleet-graph"


class CoordinatorFault(RuntimeError):
    """The coordinator run failed, or answered in a shape we will not guess at."""


def parse_envelope(result: dict[str, Any]) -> dict[str, Any]:
    """Pull the declared result out of an agent-run envelope.

    `structured_result` is the current field; `result` is accepted for older
    envelopes. A missing one is a fault rather than something to infer from
    stdout -- inferring is the INV-3 violation this layer exists to avoid.
    """
    for key in ("structured_result", "result"):
        value = result.get(key)
        if isinstance(value, dict):
            return value

    stdout = result.get("stdout")
    if isinstance(stdout, str) and stdout.strip():
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            for key in ("structured_result", "result"):
                if isinstance(parsed.get(key), dict):
                    return parsed[key]
            if "verdict" in parsed:
                return parsed

    raise CoordinatorFault(
        f"envelope carried no structured_result/result object; keys={sorted(result)}"
    )


def parse_worker_envelope(envelope: dict[str, Any], *, round_no: int) -> dict[str, Any]:
    """Parse a worker seat envelope into a validated v1 turn report (E4a).

    This is the worker-result ingress. The report travels either in a dedicated
    ``report`` field (dict or JSON string) or, for a seat that only knows
    ``text``, as a JSON report in ``text``. Legacy prose that is not a report is
    carried forward strictly as a ``text/plain`` attachment *when a valid report
    is also present* -- the adapter never infers ``outcome``/``blocker``/``did``/
    ``files``/``self_tests`` from it. Prose alone, with no valid report, is an
    explicit protocol failure, not a semantic fallback.
    """
    report_field = envelope.get("report")
    text = envelope.get("text")
    candidate: Any
    prose_to_carry: str | None = None

    if report_field is not None:
        candidate = report_field
        if isinstance(text, str) and text.strip():
            prose_to_carry = text
    elif isinstance(text, str):
        candidate = text
    else:
        raise ReportProtocolError(
            "missing", f"worker turn {round_no} carried neither a report nor text"
        )

    report = decode_report(candidate)
    if prose_to_carry is not None and "prose_attachment" not in report:
        # The carried-forward prose is the one ingress path that can hand text
        # to a report *after* decode_report has already enforced its bounds, so
        # the size limit is re-applied explicitly here -- a seat returning a
        # valid report plus a multi-megabyte ``text`` is rejected, never
        # persisted as an oversized attachment.
        report["prose_attachment"] = validate_attachment(MEDIA_TYPE_PLAIN, prose_to_carry)
    return report


@dataclass
class AgentRunCoordinator:
    """One coordinator turn per graph round, via `agent-run --role`."""

    launcher: AgentRunLauncher
    folder_id: str
    thread_id: str
    run_root: Path
    # NOTE(2026-08-29): do NOT thread the roster seat into this run as
    # `--agent` -- the agent-run CLI holds `--role` and `--agent` mutually
    # exclusive (CONFIG_ERROR exit 90), which grounded the whole fleet for an
    # hour. The coordinator's seat is declared by the role registry
    # (profiles/roles/goal_coordinator.yaml); a fleet-wide family switch must
    # edit that file, not this argv.
    role: str = "goal_coordinator"
    timeout_seconds: int = 2700
    poll_interval: float = 2.0
    extra_labels: dict[str, str] | None = None
    #: The per-process launch identity minted at line generation start. One
    #: value for every round of this process; a process restart mints a new
    #: one, and a re-adopted run keeps the label it was first dispatched with.
    launch_id: str = ""

    def turn(
        self, round_no: int, coord_input: dict[str, Any], *, resume: bool = False
    ) -> dict[str, Any]:
        input_path = write_json_durable(
            self.run_root / "coord" / f"round-{round_no}-input.json", coord_input
        )

        labels = {
            "work_folder": self.folder_id,
            "dispatcher": DISPATCHER,
            "role": "supervisor",
            "goal": self.folder_id,
            "round": str(round_no),
        }
        if self.launch_id:
            labels["launch"] = self.launch_id
        labels.update(self.extra_labels or {})

        spec = AgentRunSpec(
            prompt="",
            role=self.role,
            input_path=str(input_path),
            prompt_file=str(input_path),
            structured=True,
            timeout_seconds=self.timeout_seconds,
            labels=labels,
        )
        # The node a turn derives its run id from. A *resume* turn must be a
        # genuinely new run rather than a re-adoption of the round's original
        # coordinator run: the pre-suspension run already wrote a succeeded
        # result.json (the ``blocked + waiting_on=decision`` verdict), and
        # re-adopting it would replay that stale verdict with no
        # ``acknowledged_message_id`` -- the injected decision would be
        # silently dropped (E2 spec item 3). A distinct node name gives the
        # resume its own derived run id, deterministically, so a crash after
        # the resume launches still re-adopts the resume run (never a second
        # model invocation) instead of colliding with the pre-suspension one.
        node = f"coordinator-resume-{round_no}" if resume else f"coordinator-{round_no}"

        # A failed prior attempt must not be re-adopted: its run id is already
        # registered on the bus lifecycle with that attempt's intent, and
        # re-dispatching the same id gets a 409 IDEMPOTENCY_CONFLICT -> exit 91
        # -> the round bricks forever (generation only bumps on a terminal,
        # which needs this very coordinator to run). Adopt running/succeeded;
        # a failed attempt gets the next derived attempt id. Bounded: a round
        # that fails MAX_COORDINATOR_ATTEMPTS times is a fault, not a loop.
        run_id = ""
        for attempt in range(1, MAX_COORDINATOR_ATTEMPTS + 1):
            run_id = derive_run_id(self.thread_id, node, attempt)
            prior = find_result(self.launcher.session_root_for(run_id))
            if prior is not None and _classify(prior).state == "failed":
                continue
            break
        else:
            raise CoordinatorFault(
                f"coordinator round {round_no} failed {MAX_COORDINATOR_ATTEMPTS} "
                "derived attempts in a row; refusing to spin further"
            )
        ticket = self.launcher.launch(spec, run_id)
        status: RunStatus = self.launcher.wait(
            ticket,
            poll_interval=self.poll_interval,
            deadline_seconds=self.timeout_seconds + 120,
        )

        if status.result is None:
            raise CoordinatorFault(f"coordinator run {run_id} produced no result")
        if not status.ok:
            raise CoordinatorFault(
                f"coordinator run {run_id} ended {status.state} "
                f"(exit_code={status.result.get('exit_code')})"
            )
        return parse_envelope(status.result)


@dataclass
class AgentSessionWorker:
    """The long-lived worker seat. Opened once, re-entered every round."""

    seat: AgentSessionSeat
    seat_spec: Any
    seat_key: str
    turn_timeout_seconds: int = 3000
    _handle: SeatHandle | None = None
    #: The 1-based ordinal of the turn about to run, within this process's
    #: ownership of the seat session. Counted per ``turn`` entry -- a turn
    #: that times out still counts, which is exactly the point: the timeout
    #: record's ``turn_ordinal`` names the turn that died.
    _turn_ordinal: int = 0
    #: This process's first-open wall clock. The session-age fallback when the
    #: runtime never recorded a start timestamp: for an adopted seat that is
    #: the start of the observed window, a lower bound -- never invented
    #: further back. Resolved lazily (never inside ``open``) so a seat whose
    #: handle cannot name its session keeps opening and turning untouched.
    _opened_at: float | None = None

    def open(self) -> SeatHandle:
        if self._handle is None:
            self._opened_at = time.time()
            self._handle = self.seat.open(self.seat_spec, self.seat_key)
        return self._handle

    def turn(self, prompt: str, round_no: int) -> dict[str, Any]:
        handle = self.open()
        self._turn_ordinal += 1
        envelope = self.seat.send(handle, prompt, timeout_seconds=self.turn_timeout_seconds)
        return parse_worker_envelope(envelope, round_no=round_no)

    def turn_variables(self) -> dict[str, Any]:
        """The defect-⑩ variable matrix this seat turns under.

        `seat` is the agents.yaml seat name the roster declared; `model` is
        read from the seat's own session metadata when the runtime recorded it
        there (agent-session owns the model choice; we only observe it) and is
        an honest None when it did not -- the matrix field is still recorded,
        so a runtime that never names its model buckets as seat-only evidence
        instead of silently looking attributed. `turn_timeout_seconds` is the
        exact budget this adapter passes to `send`.

        The d10-rework session identity triple is observed, never invented:
        `seat_session_id` is the agent-session-owned session id this handle
        names; `turn_ordinal` counts the turns this process has entered into
        that session; `session_age` is seconds since the session's start (see
        ``_session_started_at`` for the honest-start precedence). Alongside
        them `session_last_activity_at` is the newest mtime under the
        session's directory -- the 会话最后活动时刻 half of the two-track
        真挂/撞顶 delta. Any half that cannot be observed is an honest None.
        """
        return {
            "seat": getattr(self.seat_spec, "agent", None),
            "model": self._session_model(),
            "turn_timeout_seconds": self.turn_timeout_seconds,
            "seat_session_id": self._handle.session_id if self._handle is not None else None,
            "turn_ordinal": self._turn_ordinal,
            "session_age": self._session_age(),
            "session_last_activity_at": self._session_last_activity_at(),
        }

    def _session_model(self) -> str | None:
        if self._handle is None:
            return None
        meta = read_session_meta(self._handle.session_root, self._handle.session_id)
        if not meta:
            return None
        model = meta.get("model")
        return str(model) if model else None

    #: session.json keys a runtime may use to name when the session started.
    _SESSION_START_META_KEYS = ("started_at", "created_at", "start_time")

    def _meta_started_at(self) -> float | None:
        if self._handle is None:
            return None
        meta = read_session_meta(self._handle.session_root, self._handle.session_id)
        if not meta:
            return None
        for key in self._SESSION_START_META_KEYS:
            value = meta.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
            if isinstance(value, str) and value.strip():
                parsed = _parse_timestamp(value.strip())
                if parsed is not None:
                    return parsed
        return None

    def _session_age(self) -> float | None:
        if self._handle is None:
            return None
        started = self._meta_started_at() or self._opened_at
        if started is None:
            return None
        return max(0.0, time.time() - started)

    def _session_last_activity_at(self) -> float | None:
        if self._handle is None:
            return None
        session_dir = Path(self._handle.session_root) / "sessions" / self._handle.session_id
        newest: float | None = None
        try:
            for path in session_dir.rglob("*"):
                if path.is_file():
                    mtime = path.stat().st_mtime
                    newest = mtime if newest is None else max(newest, mtime)
        except OSError:
            return newest
        return newest


def _parse_timestamp(raw: str) -> float | None:
    """Parse an epoch string or an ISO-8601 stamp into epoch seconds.

    Mechanical and total: anything unparseable is None, never a guessed time.
    """
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


__all__ = [
    "DISPATCHER",
    "AgentRunCoordinator",
    "AgentSessionWorker",
    "CoordinatorFault",
    "parse_envelope",
    "parse_worker_envelope",
]
