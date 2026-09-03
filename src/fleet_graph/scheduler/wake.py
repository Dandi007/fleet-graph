"""Mechanical wake facts for parked lines.

A line parked as "blocked waiting on a human decision" is woken by facts, not
by prose. Three live sources, each a single cheap read:

- **inbox**: a message arrived in the line's `agent:{alias}` channel *after*
  the blocked terminal was written. Anything earlier was already drained by the
  run that blocked. This reads channel messages (a plain GET) -- deliberately
  not `consume`, which takes a lease and would hide messages from the line the
  wake exists to restart. The channel is private and owner-only readable, so
  the probe authenticates with the line's own mirrored token
  (LINE_TOKEN_PATH_TEMPLATE), falling back to the service token only when
  that file is absent.
- **goal.md revision**: the work folder's `fs_stat` content_revision differs
  from the one snapshotted at parking time. The revision is a hash the MCP
  computes; nothing here reads the goal text.
- **board decision** (D5): a `work.decision.v1` landed on `board:work-notes`
  referencing the parked line's own question note, created after the parking
  instant, and signed (`payload.decided_by` non-empty). The read is two plain
  GETs -- the refs endpoint (how an answer finds its question; the real bus
  does not inline refs on served messages) plus the channel tail -- with the
  service credential, the same family every other board reader uses. No
  publish, no consume, no lease: the wake observes the ruling, it never takes
  it.

The other wake sources need no code here: an operator clears the `parked_*`
fields from the line's stall-state file, and the decision bridge's consumed
fact (`dispatched_decision_consumed_at`) is read straight off that same file
by the scheduler (see daemon.py).

Failure discipline: every probe here is best-effort with a short timeout, and
every failure is the *caller's* signal to fail open -- treat the line as not
parked and fall back to plain backoff. Parking saves money; a broken probe must
never be able to lock a line shut. That is why these methods raise rather than
guess: the fail-open policy lives in one place, the scheduler.
"""

from __future__ import annotations

import calendar
import json
import os
import time
from pathlib import Path
from typing import Any, Protocol

from fleet_graph.bus.board import DECISION_KIND, WORK_NOTES
from fleet_graph.bus.tokens import (
    LINE_TOKEN_PATH_ENV,
    LINE_TOKEN_PATH_TEMPLATE,
    resolve_line_token,
)

#: The dd status value that means a development has reached the human gate --
#: the first of the two M1 dd wake facts, ``dd_awaiting_gate(dev_id)``. Kept
#: local rather than importing the dd control plane's constant so the scheduler
#: stays decoupled from the pipeline's internals.
DD_AWAITING_GATE_STATE = "awaiting_gate"

#: Wake probes ride the 60s tick loop; a hung endpoint must cost seconds.
WAKE_TIMEOUT_SECONDS = 5.0

#: How far below head_seq the inbox probe re-reads. Only *existence* of a
#: newer-than-terminal message matters, and a parked line's channel gains
#: messages slowly, so a short tail window is enough.
INBOX_TAIL_WINDOW = 50

#: How far below head_seq the board-decision probe re-reads `board:work-notes`.
#: The channel carries every board's notes and rulings, so it moves faster than
#: a line inbox; the window still only has to cover the tail a fresh ruling
#: sits in (the refs endpoint already narrowed the candidates by message id).
DECISION_TAIL_WINDOW = 200


class WakeSignals(Protocol):
    """What the scheduler may ask about a parked line. Probes raise on failure."""

    def inbox_message_after(self, alias: str, after_epoch: float) -> bool: ...

    def goal_revision(self, folder_id: str) -> str: ...

    def decision_landed(self, question_note_id: str, after_epoch: float) -> bool: ...


def probe_error_tag(exc: BaseException) -> str:
    """A probe failure's mechanical attribution: class name, plus HTTP status.

    `BusError` (and anything else carrying a `status` int) tags as
    `BusError:403` rather than a bare class name, because the difference
    matters operationally: 403 is a token ACL gap (structural -- fix the
    grant), 404 a missing channel, and a timeout class name a transport
    problem. The real fleet hit exactly this: every establish attempt logged
    `probe_failed:BusError` and the log could not say the inbox source was
    ACL-blocked rather than the bus being down.
    """
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return f"{type(exc).__name__}:{status}"
    return type(exc).__name__


def parse_bus_timestamp(value: Any) -> float:
    """Epoch seconds from an ISO-8601 UTC stamp, fractional part ignored.

    The bus writes millisecond precision ("...T16:28:00.123Z"), terminal.json
    writes seconds ("...T16:28:00Z"); truncating to 19 characters makes both
    parse with one format. Raises on anything else -- the caller fails open.
    """
    text = str(value)
    return float(calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")))


class LiveWakeSignals:
    """The production WakeSignals, over agent-bus and the work-folder MCP.

    Both clients are built lazily on first use: the scheduler may run without
    a bus token or without the MCP reachable, and in both cases the probe
    raising is exactly the fail-open behaviour the caller wants.
    """

    def __init__(
        self,
        *,
        timeout: float = WAKE_TIMEOUT_SECONDS,
        bus_client: Any = None,
        wf_caller: Any = None,
        line_token_template: str | None = None,
        line_bus_factory: Any = None,
    ) -> None:
        self.timeout = timeout
        self._bus: Any = bus_client
        self._wf_caller: Any = wf_caller
        self.line_token_template = (
            line_token_template or os.environ.get(LINE_TOKEN_PATH_ENV) or LINE_TOKEN_PATH_TEMPLATE
        )
        #: Test seam: token -> bus client. Production builds a BusClient.
        self._line_bus_factory = line_bus_factory
        #: alias -> (token, client); rebuilt if the token file's content moves.
        self._line_bus: dict[str, tuple[str, Any]] = {}

    def _bus_client(self) -> Any:
        if self._bus is None:
            from fleet_graph.bus.client import BusClient, HttpxTransport

            self._bus = BusClient(transport=HttpxTransport(timeout=self.timeout))
        return self._bus

    def _line_token(self, alias: str) -> str | None:
        """The line's own bus token, or None when it cannot be resolved.

        The token stays in memory only -- never in argv, logs, or error text.
        Shared with the line process's inbox drain via
        ``fleet_graph.bus.tokens.resolve_line_token``, so the wake probe and
        the content path authenticate with the same credential family and can
        never drift apart.
        """
        return resolve_line_token(alias, template=self.line_token_template).token

    def _make_line_client(self, token: str) -> Any:
        if self._line_bus_factory is not None:
            return self._line_bus_factory(token)
        from fleet_graph.bus.client import BusClient, HttpxTransport

        return BusClient(token=token, transport=HttpxTransport(timeout=self.timeout))

    def _inbox_client(self, alias: str) -> Any:
        """Per-line credential first; the service token only as fallback.

        The inbox belongs to the line: `agent:{alias}` is owner-only and the
        owner is the line's pump, so its mirrored token is the *correct*
        credential -- the channel ACL is deliberately not widened. A missing
        or unreadable token file falls back to the service client, whose 403
        then degrades this one source at establishment (#89 semantics).
        """
        token = self._line_token(alias)
        if token is None:
            return self._bus_client()
        cached = self._line_bus.get(alias)
        if cached is None or cached[0] != token:
            cached = (token, self._make_line_client(token))
            self._line_bus[alias] = cached
        return cached[1]

    def inbox_message_after(self, alias: str, after_epoch: float) -> bool:
        client = self._inbox_client(alias)
        channel = f"agent:{alias}"
        _, head_seq = client.messages(channel, limit=1)
        if head_seq <= 0:
            return False
        tail, _ = client.messages(
            channel, limit=INBOX_TAIL_WINDOW, after_seq=max(0, head_seq - INBOX_TAIL_WINDOW)
        )
        return any(parse_bus_timestamp(message.get("created_at")) > after_epoch for message in tail)

    def goal_revision(self, folder_id: str) -> str:
        from fleet_graph.state.work_folder import FastMCPCaller, WorkFolder

        if self._wf_caller is None:
            self._wf_caller = FastMCPCaller(timeout=self.timeout)
        stat = WorkFolder(folder_id, self._wf_caller).stat("goal.md")
        revision = str(stat.get("content_revision") or "")
        if not revision:
            raise RuntimeError(f"fs_stat goal.md for {folder_id} returned no content_revision")
        return revision

    @staticmethod
    def _message_targets_question(message: dict[str, Any], question_note_id: str) -> bool:
        """Whether one served message's inline refs point at the question note.

        The authoritative source is the refs endpoint (`refs_to`), but a served
        message that *does* carry inline refs must be honoured too, so a bus
        shape that inlines them can never make the probe miss a real ruling.
        """
        refs = message.get("refs")
        if not isinstance(refs, list):
            return False
        return any(
            isinstance(ref, dict) and str(ref.get("target_entity") or "") == question_note_id
            for ref in refs
        )

    def decision_landed(self, question_note_id: str, after_epoch: float) -> bool:
        """Has a `work.decision.v1` answering this question landed after `after_epoch`?

        The D5 gate ruling is a fact on `board:work-notes`; the probe is two
        plain GETs with the service credential -- the refs endpoint names the
        messages that reference the question (the real bus does not inline
        refs on served messages), then the channel tail is read for one that
        is the decision kind, was created after `after_epoch` (the parking
        instant), and carries a non-empty `payload.decided_by`. A ruling that
        answers a *different* question, or that nobody signed, is not this
        park's fact. Read-only by discipline: no publish, no consume, no
        lease -- a probe that touched a write surface would be the bug.
        """
        client = self._bus_client()
        candidates = {
            str(ref.get("message_id") or "")
            for ref in client.refs_to(question_note_id)
            if isinstance(ref, dict) and str(ref.get("target_entity") or "") == question_note_id
        }
        candidates.discard("")
        # The bus pages ascending: learn the head first, then read the tail
        # window a fresh ruling can actually sit in -- the same recipe the
        # board's own decision reader follows.
        _, head_seq = client.messages(WORK_NOTES, limit=1)
        tail, _ = client.messages(
            WORK_NOTES,
            limit=DECISION_TAIL_WINDOW,
            after_seq=max(0, head_seq - DECISION_TAIL_WINDOW),
        )
        for message in tail:
            referenced = str(
                message.get("message_id") or ""
            ) in candidates or self._message_targets_question(message, question_note_id)
            if not referenced:
                continue
            if message.get("kind") != DECISION_KIND:
                continue
            if parse_bus_timestamp(message.get("created_at")) <= after_epoch:
                continue
            payload = message.get("payload")
            if not isinstance(payload, dict):
                continue
            if not str(payload.get("decided_by") or "").strip():
                continue
            return True
        return False


class DdWakeFacts(Protocol):
    """The M1 dd wake source for a dispatched line's park.

    The scheduler asks one question -- has the development the line dispatched
    reached a wake point yet? -- and the probe answers with a fact or None.
    A probe failure raises, exactly like ``WakeSignals``: the fail-open policy
    lives in the caller, and a broken probe must never be able to lock a line
    shut.
    """

    def dd_fact(self, development_id: str) -> str | None: ...


def classify_dd_fact(status: dict[str, Any]) -> str | None:
    """Map one dd status record to its wake fact, or None when not yet wakeable.

    The two M1 facts are the whole vocabulary:

    - ``"awaiting_gate"`` -- the development reached ``state: "awaiting_gate"``
      (its acceptance is green and it is at the gate);
    - ``"terminal"`` -- the development's ``terminal`` field is set (any
      terminal: merged / rejected / failed / fault).

    A record with neither signals a development still running -- no wake fact
    yet. This is a *projection* of the dd status file, never new prose.
    """
    state = str(status.get("state") or "")
    terminal = str(status.get("terminal") or "")
    if state == DD_AWAITING_GATE_STATE:
        return "awaiting_gate"
    if terminal:
        return "terminal"
    return None


class LiveDdWakeFacts:
    """The production DdWakeFacts, derived from the single's authority artifacts.

    M3.1 defect 6: this probe used to read the dd development's rebuildable
    status cache -- a file with no invalidation logic, whose stale/lagging
    values were then cited as machine facts. The fact is now derived from the
    authority run artifacts the control plane itself rebuilds from: the
    admission record's generation (``record.json``) picks the generation's
    ``result.json``, whose ``awaiting``/``terminal`` fields are the mechanical
    wake facts. A missing or unreadable authority raises -- the caller fails
    open rather than parking on a guess.
    """

    #: The control plane's run-artifact names (kept local so the scheduler
    #: stays decoupled from the dd package).
    RECORD_FILE = "record.json"
    RESULT_FILE = "result.json"

    def __init__(self, dd_root: str | Path) -> None:
        self.dd_root = Path(dd_root)

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return raw if isinstance(raw, dict) else None

    def dd_fact(self, development_id: str) -> str | None:
        dev_root = self.dd_root / development_id
        record = self._read_json(dev_root / self.RECORD_FILE)
        if record is None:
            raise RuntimeError(
                f"dd admission record for {development_id} unreadable: "
                f"{dev_root / self.RECORD_FILE}"
            )
        try:
            generation = max(1, int(record.get("generation") or 1))
        except (TypeError, ValueError):
            generation = 1
        result_path = (
            dev_root / self.RESULT_FILE
            if generation <= 1
            else dev_root / f"g{generation}" / self.RESULT_FILE
        )
        result = self._read_json(result_path)
        if result is None:
            raise RuntimeError(f"dd run result for {development_id} unreadable: {result_path}")
        # The same precedence the control plane's rebuild enforces: a pending
        # question means the single sits at the gate; otherwise any terminal
        # terminalises the wake.
        status = {
            "state": "awaiting_gate" if result.get("awaiting") else "",
            "terminal": str(result.get("terminal") or ""),
        }
        return classify_dd_fact(status)


__all__ = [
    "DD_AWAITING_GATE_STATE",
    "DECISION_TAIL_WINDOW",
    "INBOX_TAIL_WINDOW",
    "LINE_TOKEN_PATH_ENV",
    "LINE_TOKEN_PATH_TEMPLATE",
    "WAKE_TIMEOUT_SECONDS",
    "DdWakeFacts",
    "LiveDdWakeFacts",
    "LiveWakeSignals",
    "WakeSignals",
    "classify_dd_fact",
    "parse_bus_timestamp",
    "probe_error_tag",
]
