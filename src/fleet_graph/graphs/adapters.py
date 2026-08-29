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
from dataclasses import dataclass
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
from fleet_graph.executors.agent_session import AgentSessionSeat, SeatHandle
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

    def open(self) -> SeatHandle:
        if self._handle is None:
            self._handle = self.seat.open(self.seat_spec, self.seat_key)
        return self._handle

    def turn(self, prompt: str, round_no: int) -> dict[str, Any]:
        handle = self.open()
        envelope = self.seat.send(handle, prompt, timeout_seconds=self.turn_timeout_seconds)
        return parse_worker_envelope(envelope, round_no=round_no)


__all__ = [
    "DISPATCHER",
    "AgentRunCoordinator",
    "AgentSessionWorker",
    "CoordinatorFault",
    "parse_envelope",
    "parse_worker_envelope",
]
