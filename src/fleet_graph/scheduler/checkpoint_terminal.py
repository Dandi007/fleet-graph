"""Read a line's durable terminal state from its checkpoint, through ``get_state``.

E3 makes the durable goal-line checkpoint -- not ``terminal.json`` -- the
source the scheduler's account and parking decisions read. ``terminal.json``
downgrades to a *derived* compatibility view (fleet-sentinel and the external
pump consumers keep reading it) and to the fault-recovery fallback.

The reader deliberately opens nothing it must not touch:

- It never writes a checkpoint. A missing database is reported as "no
  checkpoint" rather than created on the scheduler's behalf, so reading a
  never-run line is a read, not a mutation.
- It returns one explicit outcome per (folder, generation): an authoritative
  terminal, an authoritative "not yet terminal" (the line is running and a
  stale ``terminal.json`` must not stand in for it), or "no checkpoint"
  (nothing ever ran for this generation -- the caller falls back).

Failure discipline mirrors the parking probes: a checkpoint that cannot be
read (corrupt sqlite, wrong permissions) is *not* a completed terminal. The
caller falls back to ``terminal.json`` and records the fault reason, so the
failure stays observable rather than silently collapsing into "no terminal".
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import StateGraph

from fleet_graph.graphs.goal_line import LineState
from fleet_graph.state.run_artifacts import WAITING_ON_DEFAULT

#: The checkpoint file a line writes, relative to its run root. Must stay the
#: same path the launcher passes as ``--checkpoint`` (scheduler/launcher.py),
#: otherwise the scheduler would read a different store than the line wrote.
CHECKPOINT_FILENAME = "checkpoint.sqlite3"


@dataclass(frozen=True)
class CheckpointTerminal:
    """What one generation's checkpoint says about the line's terminal state.

    Three fields, all explicit:

    - ``record``: the normalized terminal record (the same mechanical keys the
      scheduler always reads) when the checkpoint held a terminal; ``None``
      otherwise or when the checkpoint held no terminal yet.
    - ``authoritative``: ``True`` when the checkpoint answered for this
      generation -- with either a terminal or an explicit "not yet". ``False``
      means there is no checkpoint for this generation (nothing ever ran), and
      the caller should consult the terminal.json fallback.
    - ``fault``: a machine-readable reason when the checkpoint could not be
      read at all (``SqliteSaver`` raised). The caller must fall back to
      terminal.json and surface this, never treat it as a terminal.
    """

    record: dict[str, Any] | None
    authoritative: bool
    fault: str | None = None


def fault_tag(exc: BaseException) -> str:
    """A checkpoint-read fault's mechanical attribution, like ``probe_error_tag``.

    The class name, plus the sqlalchemy error category when present, so the
    operator can tell "file is not a database" apart from "disk on fire".
    """
    code = getattr(exc, "code", None)
    if code:
        return f"{type(exc).__name__}:{code}"
    return type(exc).__name__


def to_record(values: dict[str, Any], *, created_at: Any) -> dict[str, Any] | None:
    """Map a checkpoint state snapshot to the scheduler's terminal record keys.

    The checkpoint stores ``rounds_recorded`` (the same count terminal.json
    writes as ``rounds``); ``at`` has no direct checkpoint twin, so it is
    derived from the checkpoint's own creation timestamp -- the moment the
    terminal state was durable, which is the same second the terminal was
    written. Absent or unrecognised values are normalized rather than guessed.
    """
    terminal = values.get("terminal")
    if terminal is None:
        return None
    at: str | None = None
    if isinstance(created_at, str) and created_at:
        # get_state exposes created_at as an ISO string; drop precision to the
        # second and append Z so it matches terminal.json's own `at` format
        # (both are consumed by parse_bus_timestamp's first-19-chars read).
        at = created_at[:19] + "Z"
    elif created_at is not None:
        try:
            at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(created_at.timestamp())))
        except (OSError, ValueError, TypeError, OverflowError):
            at = None
    waiting_on = values.get("waiting_on")
    if waiting_on is None:
        waiting_on = WAITING_ON_DEFAULT
    return {
        "terminal": str(terminal),
        "rounds": int(values.get("rounds_recorded") or 0),
        "run_id": values.get("run_id"),
        "waiting_on": waiting_on,
        "at": at,
        "pump_fault": bool(values.get("pump_fault", False)),
    }


def _state_graph() -> StateGraph:
    """A minimal graph whose channels match ``LineState``.

    ``get_state`` only needs the state schema to deserialize the checkpoint; the
    node wiring is irrelevant, and building the real line here would drag the
    coordinator, worker and broker into the scheduler. ``LineState`` is imported
    from the graph module so the channels cannot drift apart.
    """
    graph: StateGraph = StateGraph(LineState)
    graph.add_node("read", lambda state: {})
    graph.set_entry_point("read")
    graph.set_finish_point("read")
    return graph


class SqliteCheckpointTerminalReader:
    """The production reader: opens the line's checkpoint and reads via get_state."""

    def __init__(self, run_root: str | Path) -> None:
        self.run_root = Path(run_root)

    def checkpoint_path(self, folder_id: str) -> Path:
        return self.run_root / folder_id / CHECKPOINT_FILENAME

    def read(self, folder_id: str, generation: int) -> CheckpointTerminal:
        """Read the terminal state of ``{folder_id}:g{generation}``.

        Never raises and never writes: a missing database returns
        ``authoritative=False`` (caller falls back to terminal.json), and a
        read fault returns ``fault=...`` (caller falls back and surfaces).
        """
        path = self.checkpoint_path(folder_id)
        if not path.exists():
            return CheckpointTerminal(record=None, authoritative=False)
        thread_id = f"{folder_id}:g{generation}"
        try:
            with SqliteSaver.from_conn_string(str(path)) as saver:
                compiled = _state_graph().compile(checkpointer=saver)
                snapshot = compiled.get_state({"configurable": {"thread_id": thread_id}})
        except Exception as exc:  # corrupt db, permissions -- never a terminal
            return CheckpointTerminal(record=None, authoritative=False, fault=fault_tag(exc))
        values = dict(snapshot.values) if snapshot.values else {}
        if not values:
            # A thread with no checkpoint at all: nothing has run yet.
            return CheckpointTerminal(record=None, authoritative=False)
        return CheckpointTerminal(
            record=to_record(values, created_at=snapshot.created_at), authoritative=True
        )


__all__ = [
    "CHECKPOINT_FILENAME",
    "CheckpointTerminal",
    "SqliteCheckpointTerminalReader",
    "fault_tag",
    "to_record",
]
