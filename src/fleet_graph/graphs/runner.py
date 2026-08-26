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
    checkpoint_path: str = ":memory:"

    @property
    def inbox_alias(self) -> str | None:
        return self.alias


def build_line(config: LineConfig, *, run_id: str | None = None) -> tuple[Any, LineDeps]:
    """Wire a line. Returns the compiled graph and the deps it holds."""
    run_id = run_id or str(uuid.uuid4())
    thread_id = f"{config.folder_id}:{run_id}"

    artifacts = RunArtifacts(config.run_root, run_id=run_id, folder_id=config.folder_id)

    launcher = AgentRunLauncher(state_root=str(config.run_root / "agent-runs"))
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
    )
    return build_goal_line_graph(deps), deps


class _NullInbox:
    """A line with no bus alias still has to hand the coordinator a list."""

    def drain_then_ack(self, persist: Any) -> tuple[list[Any], list[str]]:
        persist([])
        return [], []


def run_line(config: LineConfig, *, run_id: str | None = None) -> dict[str, Any]:
    graph, _deps = build_line(config, run_id=run_id)
    thread_id = f"{config.folder_id}:{run_id or 'default'}"

    with SqliteSaver.from_conn_string(config.checkpoint_path) as saver:
        compiled = graph.compile(checkpointer=saver)
        state = compiled.invoke(
            {"round_no": 1},
            config={
                "configurable": {"thread_id": thread_id},
                # Each round is several graph steps; the bounds check is the
                # real limit, this is only a runaway backstop.
                "recursion_limit": config.max_rounds * 8 + 20,
            },
        )
    return {
        "folder_id": config.folder_id,
        "terminal": state.get("terminal"),
        "terminal_reason": state.get("terminal_reason"),
        "rounds": state.get("rounds_recorded", 0),
        "run_root": str(config.run_root),
    }


__all__ = ["LineConfig", "build_line", "run_line"]
