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
    GOAL_ENROLL_MECHANISM,
    QUEUE_STATUS_PENDING,
    QUEUE_STATUS_WITHDRAWN,
)
from fleet_graph.goal_enroll.queue import EnrollQueue
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
        ]

    results: list[dict[str, Any]] = []
    for scenario in scenarios:
        if scenario == "no-acceptance-goal-fail-closed":
            result = scenario_no_acceptance_goal_fail_closed(work_dir)
        elif scenario == "submit-queue-and-withdraw-end-to-end":
            result = scenario_submit_queue_and_withdraw_end_to_end(work_dir)
        else:
            result = scenario_alias_token_missing_reject(work_dir)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))

    return 0 if all(result.get("pass") for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
