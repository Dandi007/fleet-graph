"""The goal-driven MCP surface: ``fleet-graph goal serve`` on loopback.

The goal-driven MCP surface is its own service, not a guest of the
dev-dispatch MCP. It serves exactly the goal-driven family -- the
``goal_enroll`` application tool plus the ``goal_list`` / ``goal_status`` /
``goal_withdraw`` view tools, the U4 supervisor-only ``goal_admit`` release
tool, the U2 supervisor-only ``goal_reject`` decision tool, the versioned
``goal-open`` briefing prompt, and the
``fleet-graph://goal-open/briefing`` resource -- on its own port (:5611),
registered as ``fleet-graph-goal``. dd (:5610) carries no goal-driven
registrations any more.

``goal_enroll`` is an *application*, not an ignition: a passing submission
lands in the pending queue (``enroll-queue.jsonl``), where the supervisory
face sees it (read-model ``/v1/enrollments`` + E8 + the best-effort board
question note) and decides. ``goal_admit`` is the one supervisor-only release
edge this surface offers: it marks a decided application ``admitted`` with the
supervisor release verdict's ``decision_ref`` (the queue's existing
``mark_admitted`` primitive -- no state-machine rewrite), and refuses every
non-supervisor identity (``GOAL_ENROLL_NOT_SUPERVISOR``) so the callable
capability never broadens the authorization boundary. ``goal_reject`` is the
mirror-image supervisor-only rejection edge: it marks a *pending* application
``rejected`` with the supervisor verdict's ``decision_ref`` (the queue's
existing ``mark_rejected`` primitive) under the exact same authority boundary,
and stays distinct from ``goal_withdraw`` (which never produces a rejected
status). Seat finalization and roster writes still stay on the supervisory
roster-PR path.

The enrollment queue lives in an **independent queue home** (default
``/data/fleet-graph/goal/``), deliberately separate from the work-folder-root
that owns goal folders: goal enrollment reads/writes only this queue home and
never pollutes or consumes another governance warehouse. The state read-model
(:7494 ``/v1/enrollments``) defaults to the same home so it observes the
actual enrollment queue.

Fail-fast root binding: ``goal serve`` refuses to start when neither
``--work-folder-root`` nor ``FLEET_GRAPH_WORK_FOLDER_ROOT`` is set. A goal MCP
without a bound goal-folder root would only discover ``GOAL_ENROLL_SOURCE_UNBOUND``
on the first real call -- the registered-but-unbound family bug this service
refuses to ship. Startup-time visible failure replaces the runtime half-broken
service.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from typing import Any

from fleet_graph.goal_enroll.briefing import BRIEFING_TEXT, goal_open_prompt_text
from fleet_graph.goal_enroll.contract import (
    BRIEFING_RESOURCE_URI,
    BRIEFING_VERSION,
    GOAL_OPEN_PROMPT_NAME,
    GoalEnrollError,
)
from fleet_graph.goal_enroll.queue import EnrollQueue, migrate_queue_home
from fleet_graph.goal_enroll.roster import RealRosterReader
from fleet_graph.goal_enroll.service import GoalEnrollService
from fleet_graph.goal_enroll.source import governed_goal_folder_store
from fleet_graph.goal_enroll.validator import GoalEnrollValidator

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5611

#: The FastMCP registration name of this surface (what a client sees in
#: tools/list's server name, distinct from the dev-dispatch server).
MCP_SERVER_NAME = "fleet-graph-goal"

#: The directory that owns one governed goal-folder repository per folder id.
#: ``goal serve`` binds the concrete ``goal_enroll`` source from this root (or
#: the ``--work-folder-root`` flag); a service without it refuses to start.
WORK_FOLDER_ROOT_ENV = "FLEET_GRAPH_WORK_FOLDER_ROOT"

#: The goal service's *independent* queue home: the directory that owns the
#: enrollment pending queue (``enroll-queue.jsonl``) and rejection history
#: (``enroll-rejections.jsonl``). It is deliberately separate from the
#: work-folder-root (the governance warehouse that owns goal folders), so goal
#: enrollment reads/writes only this queue home and never pollutes or consumes
#: another governance warehouse. Overridable via the env var or the
#: ``--goal-queue-home`` flag; the state read-model (:7494 /v1/enrollments)
#: defaults to the same home.
DEFAULT_GOAL_QUEUE_HOME = "/data/fleet-graph/goal"
GOAL_QUEUE_HOME_ENV = "FLEET_GRAPH_GOAL_QUEUE_HOME"


def port_is_available(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    """Bind-test the selected loopback port before FastMCP tries to serve it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def _bind_board() -> Any | None:
    """The best-effort board writer (B.3), or None when no bus credential.

    The application's question note needs an agent-bus credential; without one
    the entry records ``board_notify: failed`` and E8 is the fallback
    visibility -- a goal service must never depend on the bus to submit.
    """
    try:
        from fleet_graph.bus.board import Board
        from fleet_graph.bus.client import BusClient, load_token

        base_url = os.environ.get("FLEET_GRAPH_BUS_URL", "http://127.0.0.1:7490")
        return Board(BusClient(base_url=base_url, token=load_token()))
    except Exception as exc:  # no credential / bus misconfigured: degrade, not block
        logger.debug("goal serve: board question note unavailable: %s", exc)
        return None


def build_goal_mcp_server(
    goal_folders: Any | None = None,
    goal_queue: EnrollQueue | None = None,
    real_roster: RealRosterReader | None = None,
    *,
    board: Any | None = None,
    submitted_by: str | None = None,
    alias_token_check: Any | None = None,
    supervisor_identity_check: Any | None = None,
) -> Any:
    """Build the standalone goal-driven MCP surface.

    ``goal_folders`` optionally binds the goal-line enroll exit to a governed
    goal-folder source seam; ``goal_queue`` optionally binds the persistent
    pending-queue store; ``real_roster`` binds the read-only real-roster
    reader. When ``goal_folders`` is ``None`` the tool still exists but
    refuses with ``GOAL_ENROLL_SOURCE_UNBOUND`` (the validator's own
    fail-closed answer) -- and ``goal serve`` itself never starts that way, so
    production cannot reach the unbound route.

    ``board`` is the best-effort question-note writer (B.3); when None (the
    default, and what ``goal serve`` uses when no bus credential exists) the
    application still queues and the entry records ``board_notify: failed``,
    with E8 as the fallback visibility. ``alias_token_check`` is the gate-6
    seam ``(alias) -> bool``; when None the validator uses the production
    default (``/data/ronin/secrets/<alias>.token`` ownership, realpath-
    canonicalized over the secrets boundary, honouring
    ``FLEET_GRAPH_LINE_TOKEN_PATH``). ``supervisor_identity_check`` is the
    U4 admission seam ``(identity) -> bool``; when None the service uses the
    production default (a supervision/control-plane credential in
    ``/data/agent-bus/tokens``), keeping admission authority exclusively with
    the supervisor plane. Drills inject a temp-dir check built over a scratch
    secrets root and supervision root.
    """
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError

    enroll = GoalEnrollService(
        GoalEnrollValidator(goal_folders, alias_token_check=alias_token_check),
        queue=goal_queue if goal_queue is not None else EnrollQueue(),
        roster=real_roster if real_roster is not None else RealRosterReader(),
        board=board,
        submitted_by=submitted_by or os.environ.get("FLEET_GRAPH_SUBMITTED_BY", "goal-mcp"),
        supervisor_identity_check=supervisor_identity_check,
    )
    mcp = FastMCP(MCP_SERVER_NAME)

    def refuse_enroll(tool: str, exc: GoalEnrollError) -> dict[str, Any]:
        """Raise the machine-readable goal_enroll refusal structure."""
        raise ToolError(
            json.dumps(
                {
                    "code": exc.code,
                    "message": exc.detail,
                    "tool": tool,
                    "briefing_version": BRIEFING_VERSION,
                },
                sort_keys=True,
            )
        )

    # The versioned opening briefing (交底), registered as both a prompt and a
    # versioned resource so the handoff is pinned to this engine release. The
    # pending-queue entries `goal_enroll` submits record the same
    # BRIEFING_VERSION.
    @mcp.prompt(name=GOAL_OPEN_PROMPT_NAME)
    def goal_open() -> str:
        """The Phase-0 goal-line opening briefing (交底), engine-versioned."""
        return goal_open_prompt_text()

    @mcp.resource(BRIEFING_RESOURCE_URI)
    def goal_open_briefing() -> str:
        """The versioned briefing text behind the goal-open prompt."""
        return BRIEFING_TEXT

    @mcp.tool()
    def goal_enroll(
        folder_id: str,
        alias: str,
        seat_hint: str | None = None,
        max_rounds: int | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Submit one goal line's enrollment application, fail-closed.

        Validates the candidate goal folder against every gate -- folder is a
        goal line, goal.md declares executable acceptance argv, golden-order.md
        is present and non-empty, spec-lint bans are clean, every declared
        acceptance command starts in a throwaway liveness probe, the alias's
        bus token already exists, and the alias is not already claimed -- and
        only then lands a ``pending`` application in the enrollment queue
        (``enroll-queue.jsonl``) that the supervisory face sees. Admission to
        the real roster is NOT granted here; it happens only via the roster PR
        (supervisory) path. A folder already in the real roster answers
        ``already_enrolled``; a folder already pending answers
        ``already_pending``. A refusal is an explicit, machine-readable error
        with a stable code and the failing clause; there is never a partial
        application.
        """
        try:
            return enroll.submit(
                folder_id, alias, seat_hint=seat_hint, max_rounds=max_rounds, note=note
            )
        except GoalEnrollError as exc:
            enroll.record_rejection(folder_id, code=exc.code, detail=exc.detail, alias=alias)
            return refuse_enroll("goal_enroll", exc)

    @mcp.tool()
    def goal_list() -> dict[str, Any]:
        """Unified enrollment view: the real roster plus the pending queue.

        Every entry carries ``origin`` (``roster`` or ``pending``) and a status.
        The two reconciliation drifts are reported, never fixed: a queue entry
        already ``admitted`` whose line is missing from the real roster, and a
        roster line that still has a ``pending`` queue entry.
        """
        return enroll.list_all()

    @mcp.tool()
    def goal_status(folder_id: str) -> dict[str, Any]:
        """One application's detail: roster/pending entry + rejection history."""
        return enroll.status(folder_id)

    @mcp.tool()
    def goal_withdraw(folder_id: str) -> dict[str, Any]:
        """Withdraw a *pending* enrollment application.

        Only a ``pending`` application can be withdrawn; the row stays in the
        queue with status ``withdrawn`` (失败留痕原则). A decided application
        (admitted/rejected) refuses with ``GOAL_ENROLL_NOT_PENDING``.
        """
        try:
            return enroll.withdraw(folder_id)
        except GoalEnrollError as exc:
            return refuse_enroll("goal_withdraw", exc)

    @mcp.tool()
    def goal_admit(
        folder_id: str,
        decision_ref: str,
        decided_by: str,
    ) -> dict[str, Any]:
        """Admit one *pending* enrollment from a supervisor release verdict.

        Supervisor-only, fail-closed: ``decided_by`` must be a supervisor-plane
        principal (default seam = a supervision/control-plane credential in
        ``/data/agent-bus/tokens``); a non-supervisor identity refuses with
        ``GOAL_ENROLL_NOT_SUPERVISOR`` and nothing changes -- the callable
        capability is created without broadening the authorization boundary.

        On success the queue entry becomes ``status='admitted'`` carrying the
        exact ``decision_ref`` (the supervisor release verdict message id) and
        appends one history row without deleting or replacing existing rows.
        Re-admitting an already-admitted enrollment under the *same* decision
        is idempotent (``already_admitted: True``, no history rewrite); an
        already-admitted enrollment under a *different* decision, and any
        ``rejected`` / ``withdrawn`` enrollment, refuses with
        ``GOAL_ENROLL_NOT_PENDING``. The real roster is NOT written here --
        roster writes stay on the supervisory roster-PR path.
        """
        try:
            return enroll.admit(folder_id, decision_ref, decided_by=decided_by)
        except GoalEnrollError as exc:
            return refuse_enroll("goal_admit", exc)

    @mcp.tool()
    def goal_reject(
        folder_id: str,
        decision_ref: str,
        decided_by: str,
    ) -> dict[str, Any]:
        """Reject one *pending* enrollment from a supervisor verdict.

        Supervisor-only, fail-closed, at the exact same identity guard as
        ``goal_admit``: ``decided_by`` must be a supervisor-plane principal
        (default seam = a supervision/control-plane credential in
        ``/data/agent-bus/tokens``); a non-supervisor identity refuses with
        ``GOAL_ENROLL_NOT_SUPERVISOR`` and nothing changes.

        On success the queue entry becomes ``status='rejected'`` carrying the
        exact ``decision_ref`` and appends one history row without deleting or
        replacing existing rows. Re-rejecting an already-rejected enrollment
        under the *same* decision is idempotent (``already_rejected: True``,
        no history rewrite); an already-rejected enrollment under a *different*
        decision, and any ``admitted`` / ``withdrawn`` enrollment, refuses with
        ``GOAL_ENROLL_NOT_PENDING``. ``goal_withdraw`` stays distinct: it never
        produces a ``rejected`` status. The real roster is NOT written here.
        """
        try:
            return enroll.reject(folder_id, decision_ref, decided_by=decided_by)
        except GoalEnrollError as exc:
            return refuse_enroll("goal_reject", exc)

    return mcp


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    work_folder_root: str | None = None,
    goal_queue_home: str | None = None,
) -> None:
    """Run the standalone goal-driven MCP surface on loopback.

    Two startup refusals, both visible (never a silent half-broken service):

    - **Root unbound**: neither ``--work-folder-root`` nor
      ``FLEET_GRAPH_WORK_FOLDER_ROOT`` is set. This is the structural fix for
      the ``GOAL_ENROLL_SOURCE_UNBOUND`` family: the service refuses to start
      instead of serving a route that fails on first use.
    - **Port taken**: the same ``port_is_available`` discipline the dd surface
      uses -- an occupied :5611 is a visible refusal, not a crash loop.

    The enrollment queue lives in an independent queue home (default
    ``/data/fleet-graph/goal/``, override via ``--goal-queue-home`` or
    ``FLEET_GRAPH_GOAL_QUEUE_HOME``) -- never in the work-folder-root. Legacy
    queue files left under the work-folder-root are relocated into the queue
    home first (deterministic, idempotent, never duplicated or overwritten).
    """
    root = (
        work_folder_root if work_folder_root is not None else os.environ.get(WORK_FOLDER_ROOT_ENV)
    )
    if root in (None, ""):
        raise RuntimeError(
            "no --work-folder-root and no "
            f"{WORK_FOLDER_ROOT_ENV} in the environment; a goal MCP without a bound "
            "goal-folder root would report GOAL_ENROLL_SOURCE_UNBOUND on first use"
        )
    if not port_is_available(host, port):
        raise RuntimeError(f"fleet-graph goal port {host}:{port} is unavailable")
    queue_home = (
        goal_queue_home
        if goal_queue_home is not None
        else os.environ.get(GOAL_QUEUE_HOME_ENV, DEFAULT_GOAL_QUEUE_HOME)
    )
    migrate_queue_home(legacy_root=root, queue_home=queue_home)
    goal_folders = governed_goal_folder_store(root)
    goal_queue = EnrollQueue(queue_home)
    build_goal_mcp_server(
        goal_folders=goal_folders,
        goal_queue=goal_queue,
        real_roster=RealRosterReader(),
        board=_bind_board(),
    ).run(transport="streamable-http", host=host, port=port, path="/mcp")


__all__ = [
    "DEFAULT_GOAL_QUEUE_HOME",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "GOAL_QUEUE_HOME_ENV",
    "MCP_SERVER_NAME",
    "WORK_FOLDER_ROOT_ENV",
    "build_goal_mcp_server",
    "port_is_available",
    "serve",
]
