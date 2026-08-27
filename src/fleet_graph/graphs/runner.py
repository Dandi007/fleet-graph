"""Assemble and run one ronin line.

Everything above this is a part; this is where the parts become a line. Kept
separate from the graph itself so that goal_line.py stays testable without any
of the real collaborators.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from fleet_graph.acceptance import AcceptanceRunner, AcceptanceSpec
from fleet_graph.bus.client import BusClient
from fleet_graph.bus.inbox import Inbox
from fleet_graph.executors.agent_run import AgentRunLauncher
from fleet_graph.executors.agent_session import (
    AgentSessionSeat,
    SeatSpec,
    derive_seat_key,
)
from fleet_graph.graphs.adapters import AgentRunCoordinator, AgentSessionWorker
from fleet_graph.graphs.goal_line import LineDeps, build_goal_line_graph
from fleet_graph.graphs.guards import LineBounds, LineGuards
from fleet_graph.state.run_artifacts import RunArtifacts, write_json_durable


@dataclass
class LineConfig:
    folder_id: str
    seat: str
    run_root: Path
    max_rounds: int = 10
    noop_limit: int = 3
    timeout_limit: int = 2
    turn_timeout_seconds: int = 3000
    coordinator_timeout_seconds: int = 2700
    alias: str | None = None
    write: bool = False
    generation: int = 1
    #: None means durable: run_root / "checkpoint.sqlite3". ":memory:" stays
    #: available for tests that want a throwaway thread.
    checkpoint_path: str | None = None
    #: Test seam: the kill-restart contract test points this at a fake binary.
    #: Production leaves it None and gets DEFAULT_AGENT_RUN_BIN.
    agent_run_bin: str | None = None
    #: What the roster declared for the acceptance step (R0d). None still gets
    #: the step -- it states `not_declared` rather than staying silent.
    acceptance: AcceptanceSpec | None = None

    @property
    def inbox_alias(self) -> str | None:
        return self.alias

    @property
    def thread_id(self) -> str:
        """Stable across restarts of the same generation -- this is what makes
        `derive_run_id` reproduce the same run ids after a kill, so in-flight
        agent runs are re-adopted instead of dispatched twice. Nothing random
        may ever enter this string. Same shape as DevelopmentConfig.thread_id.
        """
        return f"{self.folder_id}:g{self.generation}"

    @property
    def resolved_checkpoint_path(self) -> str:
        return self.checkpoint_path or str(self.run_root / "checkpoint.sqlite3")


def build_line(config: LineConfig, *, run_id: str | None = None) -> tuple[Any, LineDeps]:
    """Wire a line. Returns the compiled graph and the deps it holds."""
    # run_id names this process's RunArtifacts (heartbeat/terminal attribution)
    # and nothing else. It must never leak into thread_id: a fresh uuid there
    # re-randomised every derived agent-run id on restart and broke re-adopt.
    run_id = run_id or str(uuid.uuid4())
    thread_id = config.thread_id

    artifacts = RunArtifacts(config.run_root, run_id=run_id, folder_id=config.folder_id)

    launcher_kwargs: dict[str, Any] = {"state_root": str(config.run_root / "agent-runs")}
    if config.agent_run_bin:
        launcher_kwargs["bin_path"] = config.agent_run_bin
    launcher = AgentRunLauncher(**launcher_kwargs)
    coordinator = AgentRunCoordinator(
        launcher=launcher,
        folder_id=config.folder_id,
        thread_id=thread_id,
        run_root=config.run_root,
        timeout_seconds=config.coordinator_timeout_seconds,
    )

    seat = AgentSessionSeat(state_root=str(config.run_root / "seats"))
    worker = AgentSessionWorker(
        seat=seat,
        seat_spec=SeatSpec(
            agent=config.seat,
            labels={"work_folder": config.folder_id, "dispatcher": "fleet-graph"},
        ),
        seat_key=derive_seat_key(thread_id, "worker"),
        turn_timeout_seconds=config.turn_timeout_seconds,
    )

    inbox: Any = Inbox(BusClient(), config.inbox_alias) if config.inbox_alias else _NullInbox()

    deps = LineDeps(
        coordinator=coordinator,
        worker=worker,
        inbox=inbox,
        artifacts=artifacts,
        guards=LineGuards(
            bounds=LineBounds(
                max_rounds=config.max_rounds,
                noop_limit=config.noop_limit,
                timeout_limit=config.timeout_limit,
            )
        ),
        folder_id=config.folder_id,
        persist_coord_input=lambda round_no, payload: write_json_durable(
            config.run_root / "coord" / f"round-{round_no}-input.json", payload
        ),
        # Always constructed: an undeclared spec yields the explicit
        # `not_declared` fact instead of a silently absent step.
        acceptance=AcceptanceRunner(config.acceptance),
    )
    return build_goal_line_graph(deps), deps


class _NullInbox:
    """A line with no bus alias still has to hand the coordinator a list."""

    def drain_then_ack(self, persist: Any) -> tuple[list[Any], list[str]]:
        persist([])
        return [], []


def resume_start(compiled: Any, invoke_config: dict[str, Any]) -> dict[str, Any] | None:
    """The input that continues this thread rather than replaying it.

    Measured against langgraph 1.2.11 with a real on-disk SqliteSaver
    (tests/test_line_restart.py), because the two options genuinely diverge:

    - ``invoke(None)`` on a thread whose checkpoint has pending work
      (``snapshot.next`` non-empty) resumes exactly there: a line killed
      during round 3's coordinator turn re-enters coordinator_turn with
      ``round_no == 3`` and its recorded rounds intact.
    - ``invoke({"round_no": 1})`` on that same thread *replays from round 1*:
      the coordinator is called again for rounds it already completed and
      ``rounds_recorded`` double-counts. With a stable thread_id that replay
      is exactly the duplicate-dispatch hazard re-adopt exists to prevent.

    So: pending checkpoint -> ``None`` (resume in place); anything else --
    including a brand-new thread (``snapshot.next == ()``, ``created_at is
    None``) -- gets a fresh round 1. A thread that already terminated also
    lands in the fresh branch; its carried-over ``terminal`` routes the graph
    straight to finalise, and starting a genuinely new attempt is the
    scheduler's job via a new generation.
    """
    snapshot = compiled.get_state(invoke_config)
    if snapshot.next:
        return None
    return {"round_no": 1}


def run_line(config: LineConfig, *, run_id: str | None = None) -> dict[str, Any]:
    graph, _deps = build_line(config, run_id=run_id)
    invoke_config: dict[str, Any] = {
        "configurable": {"thread_id": config.thread_id},
        # Each round is several graph steps; the bounds check is the
        # real limit, this is only a runaway backstop.
        "recursion_limit": config.max_rounds * 8 + 20,
    }

    with SqliteSaver.from_conn_string(config.resolved_checkpoint_path) as saver:
        compiled = graph.compile(checkpointer=saver)
        state = compiled.invoke(resume_start(compiled, invoke_config), config=invoke_config)
    return {
        "folder_id": config.folder_id,
        "terminal": state.get("terminal"),
        "terminal_reason": state.get("terminal_reason"),
        "rounds": state.get("rounds_recorded", 0),
        "run_root": str(config.run_root),
    }


__all__ = ["LineConfig", "build_line", "resume_start", "run_line"]
