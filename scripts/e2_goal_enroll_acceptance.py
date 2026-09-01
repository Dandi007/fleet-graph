#!/usr/bin/env python3
"""goal-driven 入册流水线 acceptance: isolated drills over the real MCP surface.

Each scenario builds a real ``build_goal_mcp_server`` (the same surface
``fleet-graph goal serve`` serves, :5611), runs it over loopback HTTP, and
drives the goal tools through a real fastmcp ``Client`` -- no fakes at the
surface. The only scratch state is a throwaway goal-folder root, pending-queue
root, real-roster file and alias-token dir under a temp dir.

- ``no-acceptance-goal-fail-closed`` -- the negative-sample assertion: a goal
  whose ``goal.md`` declares no executable acceptance command is refused with
  ``NO_ACCEPTANCE_COMMAND``. The drill itself exits 0 because proving the
  refusal *is* the pass criterion.
- ``submit-queue-and-withdraw-end-to-end`` -- one throwaway application is
  submitted through the MCP: a goal whose ``goal.md`` declares an executable
  acceptance command and whose ``golden-order.md`` is non-empty passes the
  gates, lands as a ``pending`` entry in ``enroll-queue.jsonl``, is visible on
  the state read-model's ``/v1/enrollments``, and withdraws to ``withdrawn``
  leaving the row in place (失败留痕原则).
- ``alias-token-missing-reject`` -- the gate-6 negative: an alias whose bus
  token file is absent is refused with ``GOAL_ENROLL_ALIAS_TOKEN_MISSING``.
- ``gate6-token-ownership`` -- gate-6 ownership: a supervision-plane token, an
  other-line token, and a symlink-alias token are each refused with
  ``GOAL_ENROLL_ALIAS_TOKEN_MISSING``, while a genuinely owned token passes the
  gate. The check is the real ownership validator (realpath-canonicalized over
  a scratch secrets root and supervision root), not an existence lambda.
- ``queue-home-isolation`` -- the default goal queue home is
  ``/data/fleet-graph/goal/``; both queue files (``enroll-queue.jsonl`` and
  ``enroll-rejections.jsonl``) land in the injected queue home, never in the
  work-records (goal-folder) root, and ``/v1/enrollments`` observes the same
  queue file.
- ``e8-observable-enrollment`` -- a valid enrollment through the aligned queue
  home is visible on ``/v1/enrollments`` and the supervisor observer emits an
  ``enrollment_pending`` (E8) event for it.
- ``admit-end-to-end`` -- the U4 supervisor release edge: the live MCP surface
  exposes ``goal_admit`` (tools/list), refuses a non-supervisor identity with
  ``GOAL_ENROLL_NOT_SUPERVISOR``, admits a pending application with the real
  U4 closeout ``decision_ref``, reports ``status='admitted'`` with that exact
  ``decision_ref`` through the queue and ``/v1/enrollments``, keeps the
  original history rows and appends the admission transition, and is
  idempotent for the same-decision re-admit.

Evidence is one JSON object per scenario on stdout; the process exits non-zero
when the scenario does not pass.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import socket
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastmcp import Client

from fleet_graph.goal_enroll.contract import (
    BRIEFING_VERSION,
    CODE_ALIAS_TOKEN_MISSING,
    CODE_NO_ACCEPTANCE_COMMAND,
    CODE_NOT_SUPERVISOR,
    GOAL_ENROLL_MECHANISM,
    QUEUE_STATUS_ADMITTED,
    QUEUE_STATUS_PENDING,
    QUEUE_STATUS_WITHDRAWN,
    U4_CLOSEOUT_DECISION_REF,
)
from fleet_graph.goal_enroll.queue import QUEUE_FILE, REJECTIONS_FILE, EnrollQueue
from fleet_graph.goal_enroll.roster import RealRosterReader
from fleet_graph.goal_enroll.source import governed_goal_folder_store

GOAL_MD_OK = """# A throwaway drill line

## Acceptance

```dd-acceptance
python3 -c "print('enroll drill line starts')"
```
"""

GOLDEN_ORDER_OK = """# Golden order (throwaway drill)

The golden order outranks the spec. This line is a disposable acceptance drill.
"""


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def evidence(scenario: str, passed: bool, **facts: Any) -> dict[str, Any]:
    return {
        "scenario": scenario,
        "utc_timestamp": utc_now(),
        "pass": passed,
        "exit_code": 0 if passed else 1,
        **facts,
    }


def _payload(result: Any) -> dict[str, Any]:
    data = getattr(result, "structured_content", None) or getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    content = getattr(result, "content", None)
    if content:
        for item in content:
            text = getattr(item, "text", None)
            if text:
                try:
                    return json.loads(text)
                except ValueError:
                    continue
    return {}


@contextlib.contextmanager
def running_server(server: Any) -> Iterator[str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    app = server.http_app(path="/mcp", transport="streamable-http")
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        for _ in range(100):
            try:
                httpx.get(url, timeout=0.1)
            except httpx.HTTPError:
                time.sleep(0.01)
            else:
                break
        else:
            raise RuntimeError("MCP endpoint did not become reachable")
        yield url
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=5)


def _goal_folder(root: Path, folder_id: str) -> Path:
    folder = root / folder_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "goal.md").write_text(GOAL_MD_OK, encoding="utf-8")
    (folder / "golden-order.md").write_text(GOLDEN_ORDER_OK, encoding="utf-8")
    return folder


def _submit(server: Any, folder_id: str, alias: str, **extra: Any) -> dict[str, Any]:
    """Submit one enrollment through the live MCP surface; never raises."""
    from fastmcp.exceptions import ToolError

    async def call(url: str) -> dict[str, Any]:
        async with Client(url) as client:
            try:
                result = await client.call_tool(
                    "goal_enroll", {"folder_id": folder_id, "alias": alias, **extra}
                )
                return {"refused": False, "code": None, "payload": _payload(result)}
            except ToolError as exc:
                message = str(exc)
                payload = json.loads(message[message.index("{") : message.rindex("}") + 1])
                return {"refused": True, "code": payload.get("code"), "payload": payload}

    with running_server(server) as url:
        return asyncio.run(call(url))


def _state_read_model(
    work_dir: Path, roster_file: Path, enroll_queue_path: Path
) -> tuple[Any, int]:
    """Start the state read-model over the given queue file; (server, port)."""
    from fleet_graph.state.fleet_state import FleetStateConfig, FleetStateHTTPServer

    state_config = FleetStateConfig(
        host="127.0.0.1",
        port=0,
        run_root=work_dir / "runs",
        dd_root=work_dir / "dd",
        lines_config=roster_file,
        bridge_state_dir=work_dir / "bridge",
        enroll_queue_path=enroll_queue_path,
    )
    state_server = FleetStateHTTPServer(state_config)
    state_thread = threading.Thread(target=state_server.serve_forever, daemon=True)
    state_thread.start()
    return state_server, state_server.server_address[1]


def _fetch_enrollments(state_port: int) -> dict[str, Any]:
    resp = httpx.get(f"http://127.0.0.1:{state_port}/v1/enrollments", timeout=5)
    body = resp.json()
    return {
        "status_code": resp.status_code,
        "schema_version": body.get("schema_version"),
        "folder_ids": [e.get("folder_id") for e in body.get("enrollments", [])],
    }


def scenario_no_acceptance_goal_fail_closed(work_dir: Path) -> dict[str, Any]:
    """The negative sample: no acceptance command -> NO_ACCEPTANCE_COMMAND."""
    from fleet_graph.goal.service import build_goal_mcp_server

    folder_root = work_dir / "folders"
    (folder_root / "wf-1").mkdir(parents=True, exist_ok=True)
    (folder_root / "wf-1" / "goal.md").write_text("# no acceptance here\n", encoding="utf-8")
    (folder_root / "wf-1" / "golden-order.md").write_text(GOLDEN_ORDER_OK, encoding="utf-8")

    server = build_goal_mcp_server(
        goal_folders=governed_goal_folder_store(str(folder_root)),
        goal_queue=EnrollQueue(str(work_dir / "queue")),
        real_roster=RealRosterReader(work_dir / "ronin-lines.json"),
        alias_token_check=lambda alias: True,
    )

    async def call(url: str) -> dict[str, Any]:
        from fastmcp.exceptions import ToolError

        async with Client(url) as client:
            try:
                await client.call_tool("goal_enroll", {"folder_id": "wf-1", "alias": "ronin-noacc"})
                return {"refused": False, "code": None}
            except ToolError as exc:
                message = str(exc)
                payload = json.loads(message[message.index("{") : message.rindex("}") + 1])
                return {"refused": True, "code": payload.get("code")}

    with running_server(server) as url:
        outcome = asyncio.run(call(url))

    passed = outcome["refused"] is True and outcome["code"] == CODE_NO_ACCEPTANCE_COMMAND
    return evidence(
        "no-acceptance-goal-fail-closed",
        passed,
        refused=outcome["refused"],
        refusal_code=outcome["code"],
    )


def scenario_submit_queue_and_withdraw_end_to_end(work_dir: Path) -> dict[str, Any]:
    """Submit -> queue row -> /v1/enrollments visible -> withdraw leaves trace."""
    from fleet_graph.goal.service import build_goal_mcp_server
    from fleet_graph.state.fleet_state import FleetStateConfig, FleetStateHTTPServer

    folder_root = work_dir / "folders"
    queue_root = work_dir / "queue"
    roster_file = work_dir / "ronin-lines.json"
    roster_file.write_text(
        json.dumps({"run_root": "/data/fleet-graph/runs", "lines": []}), encoding="utf-8"
    )
    secrets_dir = work_dir / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / "ronin-drill.token").write_text("drill-token", encoding="utf-8")

    (folder_root / "wf-1").mkdir(parents=True, exist_ok=True)
    (folder_root / "wf-1" / "goal.md").write_text(GOAL_MD_OK, encoding="utf-8")
    (folder_root / "wf-1" / "golden-order.md").write_text(GOLDEN_ORDER_OK, encoding="utf-8")

    def alias_token_check(alias: str) -> bool:
        return (secrets_dir / f"{alias}.token").is_file()

    queue = EnrollQueue(str(queue_root))
    server = build_goal_mcp_server(
        goal_folders=governed_goal_folder_store(str(folder_root)),
        goal_queue=queue,
        real_roster=RealRosterReader(roster_file),
        board=None,
        alias_token_check=alias_token_check,
    )

    async def call(url: str) -> dict[str, Any]:
        async with Client(url) as client:
            result = await client.call_tool(
                "goal_enroll",
                {
                    "folder_id": "wf-1",
                    "alias": "ronin-drill",
                    "seat_hint": "opencode-gpt-sol",
                    "max_rounds": 9999,
                    "note": "drill application",
                },
            )
            return _payload(result)

    with running_server(server) as url:
        submitted = asyncio.run(call(url))

    persisted = queue.get("wf-1") or {}

    # /v1/enrollments visibility: spin the read-model over the same queue file.
    state_config = FleetStateConfig(
        host="127.0.0.1",
        port=0,
        run_root=work_dir / "runs",
        dd_root=work_dir / "dd",
        lines_config=roster_file,
        bridge_state_dir=work_dir / "bridge",
        enroll_queue_path=queue_root / "enroll-queue.jsonl",
    )
    state_server = FleetStateHTTPServer(state_config)
    state_thread = threading.Thread(target=state_server.serve_forever, daemon=True)
    state_thread.start()
    state_port = state_server.server_address[1]
    visible = {}
    try:
        resp = httpx.get(f"http://127.0.0.1:{state_port}/v1/enrollments", timeout=5)
        body = resp.json()
        visible = {
            "status_code": resp.status_code,
            "schema_version": body.get("schema_version"),
            "folder_ids": [e.get("folder_id") for e in body.get("enrollments", [])],
        }
    finally:
        state_server.shutdown()
        state_server.server_close()

    # Withdraw leaves the row as `withdrawn`.
    async def withdraw(url: str) -> dict[str, Any]:
        async with Client(url) as client:
            result = await client.call_tool("goal_withdraw", {"folder_id": "wf-1"})
            return _payload(result)

    with running_server(server) as url:
        withdrawn = asyncio.run(withdraw(url))
    after_withdraw = queue.get("wf-1") or {}

    passed = bool(
        submitted.get("status") == QUEUE_STATUS_PENDING
        and submitted.get("already_pending") is False
        and submitted.get("briefing_version") == BRIEFING_VERSION
        and submitted.get("mechanism") == GOAL_ENROLL_MECHANISM
        and persisted.get("status") == QUEUE_STATUS_PENDING
        and persisted.get("alias") == "ronin-drill"
        and visible.get("status_code") == 200
        and visible.get("schema_version") is not None
        and "wf-1" in visible.get("folder_ids", [])
        and withdrawn.get("status") == QUEUE_STATUS_WITHDRAWN
        and after_withdraw.get("status") == QUEUE_STATUS_WITHDRAWN  # 留痕不删行
    )
    return evidence(
        "submit-queue-and-withdraw-end-to-end",
        passed,
        submitted=submitted,
        persisted_status=persisted.get("status"),
        enrollments_visible=visible,
        withdrawn=withdrawn,
        after_withdraw_status=after_withdraw.get("status"),
    )


def scenario_alias_token_missing_reject(work_dir: Path) -> dict[str, Any]:
    """Gate 6 negative: an alias whose token file is absent is refused."""
    from fleet_graph.goal.service import build_goal_mcp_server

    folder_root = work_dir / "folders"
    queue_root = work_dir / "queue2"
    roster_file = work_dir / "ronin-lines.json"
    secrets_dir = work_dir / "secrets2"
    secrets_dir.mkdir(parents=True, exist_ok=True)  # empty: no tokens at all

    (folder_root / "wf-1").mkdir(parents=True, exist_ok=True)
    (folder_root / "wf-1" / "goal.md").write_text(GOAL_MD_OK, encoding="utf-8")
    (folder_root / "wf-1" / "golden-order.md").write_text(GOLDEN_ORDER_OK, encoding="utf-8")

    def alias_token_check(alias: str) -> bool:
        return (secrets_dir / f"{alias}.token").is_file()

    server = build_goal_mcp_server(
        goal_folders=governed_goal_folder_store(str(folder_root)),
        goal_queue=EnrollQueue(str(queue_root)),
        real_roster=RealRosterReader(roster_file),
        board=None,
        alias_token_check=alias_token_check,
    )

    async def call(url: str) -> dict[str, Any]:
        from fastmcp.exceptions import ToolError

        async with Client(url) as client:
            try:
                await client.call_tool(
                    "goal_enroll", {"folder_id": "wf-1", "alias": "ronin-no-token"}
                )
                return {"refused": False, "code": None}
            except ToolError as exc:
                message = str(exc)
                payload = json.loads(message[message.index("{") : message.rindex("}") + 1])
                return {"refused": True, "code": payload.get("code")}

    with running_server(server) as url:
        outcome = asyncio.run(call(url))

    queue = EnrollQueue(str(queue_root))
    passed = outcome["refused"] is True and outcome["code"] == CODE_ALIAS_TOKEN_MISSING
    return evidence(
        "alias-token-missing-reject",
        passed,
        refused=outcome["refused"],
        refusal_code=outcome["code"],
        queue_empty=len(queue) == 0,
    )


def scenario_gate6_token_ownership(work_dir: Path) -> dict[str, Any]:
    """Gate-6 ownership: supervision-plane / other-line / symlink-alias tokens
    are refused; a genuinely owned token passes."""
    from fleet_graph.bus.tokens import build_line_token_ownership_check
    from fleet_graph.goal.service import build_goal_mcp_server

    folder_root = work_dir / "folders"
    queue_root = work_dir / "queue3"
    secrets_dir = work_dir / "secrets"
    supervision_dir = work_dir / "supervision"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    supervision_dir.mkdir(parents=True, exist_ok=True)

    # Supervision-plane token: realpath resolves into the supervision dir.
    (supervision_dir / "supervisor.token").write_text("supervisor-token", encoding="utf-8")
    (secrets_dir / "ronin-sup.token").symlink_to(supervision_dir / "supervisor.token")

    # Other-line token: realpath resolves to another line's token file.
    (secrets_dir / "ronin-other.token").write_text("other-token", encoding="utf-8")
    (secrets_dir / "ronin-x.token").symlink_to(secrets_dir / "ronin-other.token")

    # Symlink alias: a symlink masquerading as the line's own token (resolves
    # within the secrets boundary to a same-named file under a subdirectory).
    (secrets_dir / "real").mkdir()
    (secrets_dir / "real" / "ronin-link.token").write_text("real-token", encoding="utf-8")
    (secrets_dir / "ronin-link.token").symlink_to(secrets_dir / "real" / "ronin-link.token")

    # Genuinely owned token: a plain regular file at the canonical path.
    (secrets_dir / "ronin-owned.token").write_text("owned-token", encoding="utf-8")

    _goal_folder(folder_root, "wf-1")
    alias_token_check = build_line_token_ownership_check(
        template=str(secrets_dir / "{alias}.token"),
        secrets_root=secrets_dir,
        supervision_roots=(supervision_dir,),
    )
    server = build_goal_mcp_server(
        goal_folders=governed_goal_folder_store(str(folder_root)),
        goal_queue=EnrollQueue(str(queue_root)),
        real_roster=RealRosterReader(work_dir / "ronin-lines.json"),
        board=None,
        alias_token_check=alias_token_check,
    )

    supervision = _submit(server, "wf-1", "ronin-sup")
    other_line = _submit(server, "wf-1", "ronin-x")
    symlink_alias = _submit(server, "wf-1", "ronin-link")
    owned = _submit(server, "wf-1", "ronin-owned")

    passed = bool(
        supervision["refused"]
        and supervision["code"] == CODE_ALIAS_TOKEN_MISSING
        and other_line["refused"]
        and other_line["code"] == CODE_ALIAS_TOKEN_MISSING
        and symlink_alias["refused"]
        and symlink_alias["code"] == CODE_ALIAS_TOKEN_MISSING
        and not owned["refused"]
        and owned["payload"].get("status") == QUEUE_STATUS_PENDING
    )
    return evidence(
        "gate6-token-ownership",
        passed,
        supervision=supervision,
        other_line=other_line,
        symlink_alias=symlink_alias,
        owned=owned,
    )


def scenario_queue_home_isolation(work_dir: Path) -> dict[str, Any]:
    """Default queue home is /data/fleet-graph/goal/; both queue files land in
    the injected queue home, never in work-records; /v1/enrollments sees the
    same queue."""
    from fleet_graph.goal.service import DEFAULT_GOAL_QUEUE_HOME, build_goal_mcp_server

    folder_root = work_dir / "work-records"  # the governance warehouse (goal folders)
    queue_home = work_dir / "goal"  # test-isolated equivalent of the queue home
    roster_file = work_dir / "ronin-lines.json"
    roster_file.write_text(
        json.dumps({"run_root": "/data/fleet-graph/runs", "lines": []}), encoding="utf-8"
    )
    secrets_dir = work_dir / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / "ronin-owned.token").write_text("owned-token", encoding="utf-8")

    _goal_folder(folder_root, "wf-1")
    queue = EnrollQueue(str(queue_home))
    server = build_goal_mcp_server(
        goal_folders=governed_goal_folder_store(str(folder_root)),
        goal_queue=queue,
        real_roster=RealRosterReader(roster_file),
        board=None,
        alias_token_check=lambda alias: (secrets_dir / f"{alias}.token").is_file(),
    )

    # A valid enrollment lands pending in the queue home.
    submitted = _submit(server, "wf-1", "ronin-owned", note="queue home drill")
    # A failing submission (unknown folder -> gate refusal) records a rejection
    # into the same queue home, proving the rejections file also lives outside
    # work-records.
    _submit(server, "wf-reject", "ronin-no-token")

    queue_home_files = sorted(p.name for p in queue_home.iterdir())
    # Nothing queue-shaped may appear in the work-records root.
    work_records_queue_files = sorted(
        p.name for p in folder_root.iterdir() if p.name in (QUEUE_FILE, REJECTIONS_FILE)
    )

    state_server, state_port = _state_read_model(work_dir, roster_file, queue_home / QUEUE_FILE)
    visible = {}
    try:
        visible = _fetch_enrollments(state_port)
    finally:
        state_server.shutdown()
        state_server.server_close()

    passed = bool(
        DEFAULT_GOAL_QUEUE_HOME == "/data/fleet-graph/goal"
        and submitted.get("payload", {}).get("status") == QUEUE_STATUS_PENDING
        and QUEUE_FILE in queue_home_files
        and REJECTIONS_FILE in queue_home_files
        and work_records_queue_files == []
        and visible.get("status_code") == 200
        and "wf-1" in visible.get("folder_ids", [])
    )
    return evidence(
        "queue-home-isolation",
        passed,
        default_goal_queue_home=DEFAULT_GOAL_QUEUE_HOME,
        submitted_status=submitted.get("payload", {}).get("status"),
        queue_home_files=queue_home_files,
        work_records_queue_files=work_records_queue_files,
        enrollments_visible=visible,
    )


def scenario_e8_observable_enrollment(work_dir: Path) -> dict[str, Any]:
    """A valid enrollment through the aligned queue home is visible on
    /v1/enrollments and the supervisor observer emits an E8
    ``enrollment_pending`` event for it."""
    from fleet_graph.goal.service import build_goal_mcp_server
    from fleet_graph.scheduler.supervisor_events import ObserverConfig, SupervisorObserver
    from fleet_graph.supervise.events import EVENT_ENROLLMENT_PENDING

    folder_root = work_dir / "folders"
    queue_home = work_dir / "goal"
    roster_file = work_dir / "ronin-lines.json"
    roster_file.write_text(
        json.dumps({"run_root": "/data/fleet-graph/runs", "lines": []}), encoding="utf-8"
    )
    secrets_dir = work_dir / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / "ronin-owned.token").write_text("owned-token", encoding="utf-8")

    _goal_folder(folder_root, "wf-1")
    server = build_goal_mcp_server(
        goal_folders=governed_goal_folder_store(str(folder_root)),
        goal_queue=EnrollQueue(str(queue_home)),
        real_roster=RealRosterReader(roster_file),
        board=None,
        alias_token_check=lambda alias: (secrets_dir / f"{alias}.token").is_file(),
    )
    submitted = _submit(server, "wf-1", "ronin-owned")

    state_server, state_port = _state_read_model(work_dir, roster_file, queue_home / QUEUE_FILE)

    class RecordingLauncher:
        def __init__(self) -> None:
            self.specs: list[Any] = []

        def launch(self, spec: Any):
            self.specs.append(spec)

            class _Result:
                unit_name = spec.unit_name
                started = True
                detail = "recorded"

            return _Result()

        def events(self) -> list[dict[str, Any]]:
            parsed = []
            for spec in self.specs:
                argv = spec.argv()
                parsed.append(json.loads(argv[argv.index("--event-json") + 1]))
            return parsed

    def read_model(path: str) -> dict[str, Any] | None:
        if path == "/v1/enrollments":
            return httpx.get(f"http://127.0.0.1:{state_port}/v1/enrollments", timeout=5).json()
        if path == "/v1/lines":
            return {"schema_version": "1", "lines": []}
        if path == "/v1/decisions":
            return {"schema_version": "1", "decisions": []}
        if path == "/v1/harvestable":
            return {"schema_version": "1", "developments": []}
        return None

    launcher = RecordingLauncher()
    observer = SupervisorObserver(
        ObserverConfig(
            run_root=work_dir / "runs",
            supervisor_state_root=work_dir / "supervisor",
        ),
        launcher=launcher,  # type: ignore[arg-type]
        read_model=read_model,
    )
    try:
        observer.after_tick(
            now=1_000_000.0, folder_ids=[], terminal_reader=lambda folder: None, tick_results=[]
        )
        events = launcher.events()
        pending = [e for e in events if e.get("type") == EVENT_ENROLLMENT_PENDING]
        passed = bool(
            submitted.get("payload", {}).get("status") == QUEUE_STATUS_PENDING
            and pending
            and pending[0]["key"] == "enroll-wf-1"
            and pending[0]["payload"].get("folder_id") == "wf-1"
        )
        return evidence(
            "e8-observable-enrollment",
            passed,
            submitted_status=submitted.get("payload", {}).get("status"),
            event_types=[e.get("type") for e in events],
            e8_key=pending[0]["key"] if pending else None,
            e8_payload=pending[0]["payload"] if pending else None,
        )
    finally:
        state_server.shutdown()
        state_server.server_close()


def scenario_submit_admit_end_to_end(work_dir: Path) -> dict[str, Any]:
    """U4: submit -> goal_admit (supervisor-only) -> /v1/enrollments admitted.

    Drives the real MCP surface: tools/list exposes ``goal_admit``, a
    non-supervisor identity is refused with ``GOAL_ENROLL_NOT_SUPERVISOR``, a
    supervisor identity admits a pending application with the real U4 closeout
    ``decision_ref``, the queue entry and ``/v1/enrollments`` report
    ``status='admitted'`` with that exact ``decision_ref``, history keeps the
    original pending row and appends the admission transition, and the same-
    decision re-admit is idempotent.
    """
    from fleet_graph.goal.service import build_goal_mcp_server

    folder_root = work_dir / "folders"
    queue_root = work_dir / "queue-admit"
    roster_file = work_dir / "ronin-lines.json"
    roster_file.write_text(
        json.dumps({"run_root": "/data/fleet-graph/runs", "lines": []}), encoding="utf-8"
    )
    secrets_dir = work_dir / "secrets-admit"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / "ronin-owned.token").write_text("owned-token", encoding="utf-8")

    _goal_folder(folder_root, "wf-1")
    queue = EnrollQueue(str(queue_root))
    server = build_goal_mcp_server(
        goal_folders=governed_goal_folder_store(str(folder_root)),
        goal_queue=queue,
        real_roster=RealRosterReader(roster_file),
        board=None,
        alias_token_check=lambda alias: (secrets_dir / f"{alias}.token").is_file(),
        supervisor_identity_check=lambda identity: identity == "supervisor",
    )

    async def call(url: str) -> dict[str, Any]:
        from fastmcp.exceptions import ToolError

        async with Client(url) as client:
            tools = await client.list_tools()
            tool_names = {getattr(t, "name", None) for t in tools}
            await client.call_tool("goal_enroll", {"folder_id": "wf-1", "alias": "ronin-owned"})
            non_supervisor = {"refused": False, "code": None}
            try:
                await client.call_tool(
                    "goal_admit",
                    {
                        "folder_id": "wf-1",
                        "decision_ref": U4_CLOSEOUT_DECISION_REF,
                        "decided_by": "ronin-owned",
                    },
                )
            except ToolError as exc:
                message = str(exc)
                payload = json.loads(message[message.index("{") : message.rindex("}") + 1])
                non_supervisor = {"refused": True, "code": payload.get("code")}
            first = _payload(
                await client.call_tool(
                    "goal_admit",
                    {
                        "folder_id": "wf-1",
                        "decision_ref": U4_CLOSEOUT_DECISION_REF,
                        "decided_by": "supervisor",
                    },
                )
            )
            second = _payload(
                await client.call_tool(
                    "goal_admit",
                    {
                        "folder_id": "wf-1",
                        "decision_ref": U4_CLOSEOUT_DECISION_REF,
                        "decided_by": "supervisor",
                    },
                )
            )
            return {
                "tools": sorted(tool_names),
                "non_supervisor": non_supervisor,
                "first": first,
                "second": second,
            }

    with running_server(server) as url:
        outcome = asyncio.run(call(url))

    persisted = queue.get("wf-1") or {}
    state_server, state_port = _state_read_model(work_dir, roster_file, queue_root / QUEUE_FILE)
    visible = {}
    try:
        resp = httpx.get(f"http://127.0.0.1:{state_port}/v1/enrollments", timeout=5)
        body = resp.json()
        entry = next((e for e in body.get("enrollments", []) if e.get("folder_id") == "wf-1"), {})
        visible = {
            "status_code": resp.status_code,
            "entry": entry,
        }
    finally:
        state_server.shutdown()
        state_server.server_close()

    passed = bool(
        "goal_admit" in outcome["tools"]
        and outcome["non_supervisor"]["refused"]
        and outcome["non_supervisor"]["code"] == CODE_NOT_SUPERVISOR
        and outcome["first"].get("status") == QUEUE_STATUS_ADMITTED
        and outcome["first"].get("decision_ref") == U4_CLOSEOUT_DECISION_REF
        and outcome["second"].get("already_admitted") is True
        and persisted.get("status") == QUEUE_STATUS_ADMITTED
        and persisted.get("decision_ref") == U4_CLOSEOUT_DECISION_REF
        and [h.get("status") for h in persisted.get("history", [])]
        == [QUEUE_STATUS_PENDING, QUEUE_STATUS_ADMITTED]
        and visible.get("status_code") == 200
        and visible["entry"].get("status") == QUEUE_STATUS_ADMITTED
        and visible["entry"].get("decision_ref") == U4_CLOSEOUT_DECISION_REF
    )
    return evidence(
        "admit-end-to-end",
        passed,
        tools=outcome["tools"],
        non_supervisor=outcome["non_supervisor"],
        first_admit=outcome["first"],
        second_admit=outcome["second"],
        persisted_status=persisted.get("status"),
        persisted_decision_ref=persisted.get("decision_ref"),
        persisted_history=[h.get("status") for h in persisted.get("history", [])],
        enrollments_visible=visible,
    )


# --- cli ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        default=None,
        choices=[
            "no-acceptance-goal-fail-closed",
            "submit-queue-and-withdraw-end-to-end",
            "alias-token-missing-reject",
            "gate6-token-ownership",
            "queue-home-isolation",
            "e8-observable-enrollment",
            "admit-end-to-end",
        ],
        help="run one scenario only; default runs all (the full acceptance)",
    )
    parser.add_argument("--work-dir", default=None, help="scratch dir (default: a fresh temp dir)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    work_dir = (
        Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="e2-goal-enroll-"))
    )
    work_dir.mkdir(parents=True, exist_ok=True)

    if args.scenario is not None:
        scenarios = [args.scenario]
    else:
        scenarios = [
            "no-acceptance-goal-fail-closed",
            "submit-queue-and-withdraw-end-to-end",
            "alias-token-missing-reject",
            "gate6-token-ownership",
            "queue-home-isolation",
            "e8-observable-enrollment",
            "admit-end-to-end",
        ]

    results: list[dict[str, Any]] = []
    for scenario in scenarios:
        if scenario == "no-acceptance-goal-fail-closed":
            result = scenario_no_acceptance_goal_fail_closed(work_dir)
        elif scenario == "submit-queue-and-withdraw-end-to-end":
            result = scenario_submit_queue_and_withdraw_end_to_end(work_dir)
        elif scenario == "alias-token-missing-reject":
            result = scenario_alias_token_missing_reject(work_dir)
        elif scenario == "gate6-token-ownership":
            result = scenario_gate6_token_ownership(work_dir)
        elif scenario == "queue-home-isolation":
            result = scenario_queue_home_isolation(work_dir)
        elif scenario == "admit-end-to-end":
            result = scenario_submit_admit_end_to_end(work_dir)
        else:
            result = scenario_e8_observable_enrollment(work_dir)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))

    return 0 if all(result.get("pass") for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
