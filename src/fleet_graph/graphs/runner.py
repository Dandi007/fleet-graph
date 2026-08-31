"""Assemble and run one ronin line.

Everything above this is a part; this is where the parts become a line. Kept
separate from the graph itself so that goal_line.py stays testable without any
of the real collaborators.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from fleet_graph.acceptance import AcceptanceRunner, AcceptanceSpec
from fleet_graph.bus.client import BusClient
from fleet_graph.bus.inbox import Inbox
from fleet_graph.bus.tokens import resolve_line_token
from fleet_graph.executors.agent_run import AgentRunLauncher
from fleet_graph.executors.agent_session import (
    AgentSessionSeat,
    SeatSpec,
    derive_seat_key,
)
from fleet_graph.goal_interrupt.contract import DecisionInput
from fleet_graph.goal_interrupt.runtime import LineInterruptPort, resume_line
from fleet_graph.goal_interrupt.store import GoalInterruptStore
from fleet_graph.graphs.adapters import AgentRunCoordinator, AgentSessionWorker
from fleet_graph.graphs.goal_line import LineDeps, build_goal_line_graph
from fleet_graph.graphs.guards import LineBounds, LineGuards
from fleet_graph.state.run_artifacts import RunArtifacts, iso, write_json_durable


@dataclass
class LineConfig:
    folder_id: str
    seat: str
    run_root: Path
    max_rounds: int = 10
    noop_limit: int = 3
    timeout_limit: int = 2
    turn_timeout_seconds: int = 3000
    # Mirrors the CLI default; see cli.py --coordinator-timeout for why 5400
    # (agent-run divides the budget across route-chain legs).
    coordinator_timeout_seconds: int = 5400
    alias: str | None = None
    write: bool = False
    generation: int = 1
    #: The board card entity id the scheduler's escalation already materialised
    #: (stall-state ``board_card_entity_id``). Empty means no known card yet and
    #: the interrupt runtime falls back to publishing through the shared
    #: constructor + shared key on its first ask.
    board_card_entity_id: str = ""
    #: None means durable: run_root / "checkpoint.sqlite3". ":memory:" stays
    #: available for tests that want a throwaway thread.
    checkpoint_path: str | None = None
    #: Test seam: the kill-restart contract test points this at a fake binary.
    #: Production leaves it None and gets DEFAULT_AGENT_RUN_BIN.
    agent_run_bin: str | None = None
    #: What the roster declared for the acceptance step (R0d). None still gets
    #: the step -- it states `not_declared` rather than staying silent.
    acceptance: AcceptanceSpec | None = None
    #: Where this line's stdout/stderr lands (defaults under
    #: /data/fleet-graph/logs in RunArtifacts). Recorded in the heartbeat and
    #: terminal so the run root names its own log.
    log_path: Path | None = None
    #: The mechanical wf_resume verification facts captured at generation start
    #: by the orchestration layer, injected into every coordinator input. None
    #: means none were captured (the field is then absent from the envelope).
    resume_verification: dict[str, Any] | None = None
    #: The prior generation's terminal.json content, injected into the round-1
    #: coordinator input when present. None lets build_line read the terminal
    #: left on disk under run_root, which at generation start is the previous
    #: generation's.
    prior_terminal: dict[str, Any] | None = None
    #: The per-process launch identity (D4). None lets build_line mint one from
    #: a wall-clock start timestamp, so a process restart is a new launch and a
    #: re-adopted run keeps the first dispatch's label (the launcher never
    #: rewrites argv.json for an adopted session root).
    launch_id: str | None = None

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


def _read_prior_terminal(run_root: Path) -> dict[str, Any] | None:
    """Read the terminal.json already on disk, which at generation start is the
    previous generation's. Absent or unparseable -> None, so round 1 simply
    carries no prior_terminal rather than guessing."""
    try:
        data = json.loads((run_root / "terminal.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def mint_launch_id(folder_id: str, generation: int, start_ts: int) -> str:
    """Mint the per-process launch identity (D4).

    Stable and readable within one process: ``launch-<folder>-g<generation>-<start-ts>``.
    The wall-clock start timestamp is what makes a process restart a new
    launch -- the same generation re-built in a fresh process stamps a
    different second, so a new launch id. A re-adopted agent run keeps the
    label it was first dispatched with: the launcher is idempotent per run id
    and never rewrites argv.json for an adopted session root.
    """
    return f"launch-{folder_id}-g{generation}-{start_ts}"


def build_line(config: LineConfig, *, run_id: str | None = None) -> tuple[Any, LineDeps]:
    """Wire a line. Returns the compiled graph and the deps it holds."""
    # run_id names this process's RunArtifacts (heartbeat/terminal attribution)
    # and nothing else. It must never leak into thread_id: a fresh uuid there
    # re-randomised every derived agent-run id on restart and broke re-adopt.
    run_id = run_id or str(uuid.uuid4())
    thread_id = config.thread_id

    artifacts = RunArtifacts(
        config.run_root,
        run_id=run_id,
        folder_id=config.folder_id,
        log_path=config.log_path,
    )

    launcher_kwargs: dict[str, Any] = {"state_root": str(config.run_root / "agent-runs")}
    if config.agent_run_bin:
        launcher_kwargs["bin_path"] = config.agent_run_bin
    launcher = AgentRunLauncher(**launcher_kwargs)
    launch_id = config.launch_id or mint_launch_id(
        config.folder_id, config.generation, int(time.time())
    )
    coordinator = AgentRunCoordinator(
        launcher=launcher,
        folder_id=config.folder_id,
        thread_id=thread_id,
        run_root=config.run_root,
        timeout_seconds=config.coordinator_timeout_seconds,
        launch_id=launch_id,
    )

    seat = AgentSessionSeat(state_root=str(config.run_root / "seats"))
    seat_labels: dict[str, str] = {
        "work_folder": config.folder_id,
        "dispatcher": "fleet-graph",
        "role": "worker",
        "goal": config.folder_id,
    }
    if launch_id:
        seat_labels["launch"] = launch_id
    worker = AgentSessionWorker(
        seat=seat,
        seat_spec=SeatSpec(
            agent=config.seat,
            labels=seat_labels,
        ),
        seat_key=derive_seat_key(thread_id, "worker"),
        turn_timeout_seconds=config.turn_timeout_seconds,
    )

    inbox: Any = _build_line_inbox(config)

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
        resume_verification=config.resume_verification,
        prior_terminal=config.prior_terminal
        if config.prior_terminal is not None
        else _read_prior_terminal(config.run_root),
        # The E2 in-graph interrupt port. Wired here so a human-decision wait
        # on a real line routes through the durable interrupt instead of the
        # legacy parking terminal (spec: "replace the normal goal-line parking
        # path with a durable graph interrupt"). A line whose store cannot be
        # opened still starts, but loses the interrupt routing rather than the
        # whole run -- parking remains the fallback.
        interrupt=_build_interrupt(config, run_id=run_id),
        run_id=run_id,
    )
    return build_goal_line_graph(deps), deps


def _build_interrupt(config: LineConfig, *, run_id: str = "") -> LineInterruptPort | None:
    """The production E2 interrupt port for one line.

    Opens the line's durable ``GoalInterruptStore`` (under ``run_root``) and, when
    a bus credential is present, a ``Board`` for materialising the question and
    card. The line's ``run_id`` is threaded through so ``ask`` reuses the
    scheduler's escalation question idempotency key (``parked:<folder>:<run_id>``)
    -- the one question a human answers, resuming the same interrupt. A missing
    credential degrades to a deterministic question id rather than failing the
    line start; a store that cannot be opened degrades to ``None`` (legacy
    parking stays the path) rather than bricking the run.
    """
    try:
        store = GoalInterruptStore(config.run_root).open()
    except Exception:
        return None
    board = None
    try:
        from fleet_graph.bus.board import Board
        from fleet_graph.bus.client import BusClient

        board = Board(BusClient())
    except Exception:
        board = None
    return LineInterruptPort(
        folder_id=config.folder_id,
        generation=config.generation,
        store=store,
        board=board,
        # The scheduler's card, threaded through so the runtime reuses it
        # instead of publishing a second one (E2 card pass-through).
        card_entity_id=config.board_card_entity_id,
        run_id=run_id,
        # The scheduler's stall-state file for this line
        # (``<run_root>/.scheduler/<folder_id>.json``). Threaded so the E2
        # interrupt's ``persist`` mirrors its question note into the stall
        # state, letting the decision bridge map a ``work.decision.v1``
        # answering it back to the parked line.
        stall_state_path=config.run_root.parent / ".scheduler" / f"{config.folder_id}.json",
    )


class _NullInbox:
    """A line with no bus alias still has to hand the coordinator a list."""

    def drain_then_ack(self, persist: Any) -> tuple[list[Any], list[str]]:
        persist([])
        return [], []


class _DegradedInbox:
    """A line with an alias but no usable credential: drain degrades in place.

    The line still runs and the coordinator still receives a well-formed empty
    ``inbox_messages``; the reason is recorded durably under the run root, so a
    missing line token is never silently mistaken for an empty inbox. Nothing
    is acked (there was nothing to read) and the line is never faulted solely
    because an inbox credential is absent (fail-open, E1 gap #4).
    """

    def __init__(self, alias: str, reason: str, record_path: Path) -> None:
        self.alias = alias
        self.reason = reason
        self.record_path = record_path

    def drain_then_ack(self, persist: Any) -> tuple[list[Any], list[str]]:
        persist([])
        try:
            self.record_path.parent.mkdir(parents=True, exist_ok=True)
            self.record_path.write_text(
                json.dumps(
                    {
                        "alias": self.alias,
                        "reason": self.reason,
                        "recorded_at": iso(time.time()),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
        return [], []


def _build_line_inbox(config: LineConfig) -> Any:
    """The line's inbox: a real ``Inbox`` authenticated as the line itself.

    The ``agent:{alias}`` channel is owner-only and the owner is the line's
    pump, so the client must present the line's own mirrored token (the same
    credential family the scheduler's wake probe uses, resolved by the shared
    ``resolve_line_token`` helper) -- never the fleet-graph service token,
    which the channel ACL structurally 403s. A line with no alias gets the
    null inbox; a line whose token cannot be resolved degrades explicitly
    instead of faulting the line (fail-open).
    """
    alias = config.inbox_alias
    if not alias:
        return _NullInbox()
    resolution = resolve_line_token(alias)
    if not resolution.present:
        return _DegradedInbox(
            alias=alias,
            reason=resolution.status,
            record_path=config.run_root / "inbox-degraded.json",
        )
    return Inbox(BusClient(token=resolution.token), alias)


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
    graph, deps = build_line(config, run_id=run_id)
    invoke_config: dict[str, Any] = {
        "configurable": {"thread_id": config.thread_id},
        # Each round is several graph steps; the bounds check is the
        # real limit, this is only a runaway backstop.
        "recursion_limit": config.max_rounds * 8 + 20,
    }

    try:
        with SqliteSaver.from_conn_string(config.resolved_checkpoint_path) as saver:
            compiled = graph.compile(checkpointer=saver)
            state = compiled.invoke(resume_start(compiled, invoke_config), config=invoke_config)
    except Exception as exc:
        # The exception boundary. `finalise` only runs on a well-formed
        # terminal, so an unexpected node exception would otherwise leave a
        # stale previous-generation terminal.json as the freshest signal.
        # Write a `fault` terminal so the crash is visible, then let the
        # exception propagate to a non-zero exit.
        deps.artifacts.write_fault_terminal(exception=exc)
        raise
    return {
        "folder_id": config.folder_id,
        "terminal": state.get("terminal"),
        "terminal_reason": state.get("terminal_reason"),
        "rounds": state.get("rounds_recorded", 0),
        "run_root": str(config.run_root),
    }


def resume_goal_line(config: LineConfig, decision: DecisionInput) -> tuple[dict[str, Any], str]:
    """Re-enter a suspended goal line's interrupt with a validated decision.

    The production twin of ``resume_line``: it rebuilds the line's graph from the
    *same* ``LineConfig`` that first suspended it (so thread_id, checkpoint path,
    coordinator/worker wiring and the interrupt store all match), then injects
    the validated ``DecisionInput`` through the resume key. This is what the
    resident ``goal-interrupt`` bridge calls for each recovered decision, so a
    human verdict in production actually resumes the same generation and
    continuation instead of parking.
    """
    store = GoalInterruptStore(config.run_root).open()
    try:
        graph, _deps = build_line(config)
        invoke_config: dict[str, Any] = {
            "configurable": {"thread_id": config.thread_id},
            "recursion_limit": config.max_rounds * 8 + 20,
        }
        with SqliteSaver.from_conn_string(config.resolved_checkpoint_path) as saver:
            compiled = graph.compile(checkpointer=saver)
            return resume_line(compiled, config=invoke_config, decision=decision, store=store)
    finally:
        store.close()


__all__ = [
    "LineConfig",
    "build_line",
    "resume_goal_line",
    "resume_start",
    "run_line",
]
