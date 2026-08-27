"""Mechanical wake facts for parked lines.

A line parked as "blocked waiting on a human decision" is woken by facts, not
by prose. Two live sources, each a single cheap read:

- **inbox**: a message arrived in the line's `agent:{alias}` channel *after*
  the blocked terminal was written. Anything earlier was already drained by the
  run that blocked. This reads channel messages (a plain GET) -- deliberately
  not `consume`, which takes a lease and would hide messages from the line the
  wake exists to restart.
- **goal.md revision**: the work folder's `fs_stat` content_revision differs
  from the one snapshotted at parking time. The revision is a hash the MCP
  computes; nothing here reads the goal text.

The third wake source needs no code: an operator clears the `parked_*` fields
from the line's stall-state file (see daemon.py).

Failure discipline: every probe here is best-effort with a short timeout, and
every failure is the *caller's* signal to fail open -- treat the line as not
parked and fall back to plain backoff. Parking saves money; a broken probe must
never be able to lock a line shut. That is why these methods raise rather than
guess: the fail-open policy lives in one place, the scheduler.
"""

from __future__ import annotations

import calendar
import time
from typing import Any, Protocol

#: Wake probes ride the 60s tick loop; a hung endpoint must cost seconds.
WAKE_TIMEOUT_SECONDS = 5.0

#: How far below head_seq the inbox probe re-reads. Only *existence* of a
#: newer-than-terminal message matters, and a parked line's channel gains
#: messages slowly, so a short tail window is enough.
INBOX_TAIL_WINDOW = 50


class WakeSignals(Protocol):
    """What the scheduler may ask about a parked line. Probes raise on failure."""

    def inbox_message_after(self, alias: str, after_epoch: float) -> bool: ...

    def goal_revision(self, folder_id: str) -> str: ...


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
    ) -> None:
        self.timeout = timeout
        self._bus: Any = bus_client
        self._wf_caller: Any = wf_caller

    def _bus_client(self) -> Any:
        if self._bus is None:
            from fleet_graph.bus.client import BusClient, HttpxTransport

            self._bus = BusClient(transport=HttpxTransport(timeout=self.timeout))
        return self._bus

    def inbox_message_after(self, alias: str, after_epoch: float) -> bool:
        client = self._bus_client()
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


__all__ = [
    "INBOX_TAIL_WINDOW",
    "WAKE_TIMEOUT_SECONDS",
    "LiveWakeSignals",
    "WakeSignals",
    "parse_bus_timestamp",
]
