#!/usr/bin/env python3
"""E5 goal-enroll acceptance: two isolated drills over the real MCP surface.

Each scenario builds a real ``build_goal_mcp_server`` (the same surface
``fleet-graph goal serve`` serves, :5611), runs it over loopback HTTP, and
drives ``goal_enroll`` through a real fastmcp ``Client`` -- no fakes at the
surface. The only scratch state is a throwaway goal-folder root and roster
store under a temp dir.

- ``no-acceptance-goal-fail-closed`` -- the negative-sample assertion: a goal
  whose ``goal.md`` declares no executable acceptance command is refused with
  ``NO_ACCEPTANCE_COMMAND``. The drill itself exits 0 because proving the
  refusal *is* the pass criterion.
- ``enroll-drill-line-end-to-end`` -- enrolls one throwaway drill line through
  the MCP: a goal whose ``goal.md`` declares an executable acceptance command
  and whose ``golden-order.md`` is non-empty is admitted, the roster records the
  engine-versioned entry with the briefing version id, and the declared
  acceptance argv starts (liveness probe exit code reachable).

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
    CODE_NO_ACCEPTANCE_COMMAND,
    GOAL_ENROLL_MECHANISM,
)
from fleet_graph.goal_enroll.source import governed_goal_folder_store
from fleet_graph.goal_enroll.store import GoalEnrollRoster

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

    server = build_goal_mcp_server(goal_folders=governed_goal_folder_store(str(folder_root)))

    async def call(url: str) -> dict[str, Any]:
        from fastmcp.exceptions import ToolError

        async with Client(url) as client:
            try:
                await client.call_tool("goal_enroll", {"folder_id": "wf-1"})
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


def scenario_enroll_drill_line_end_to_end(work_dir: Path) -> dict[str, Any]:
    """One throwaway drill line is enrolled through the MCP and starts."""
    from fleet_graph.goal.service import build_goal_mcp_server

    folder_root = work_dir / "folders"
    roster_root = work_dir / "roster"
    (folder_root / "wf-1").mkdir(parents=True, exist_ok=True)
    (folder_root / "wf-1" / "goal.md").write_text(GOAL_MD_OK, encoding="utf-8")
    (folder_root / "wf-1" / "golden-order.md").write_text(GOLDEN_ORDER_OK, encoding="utf-8")

    roster = GoalEnrollRoster(str(roster_root))
    server = build_goal_mcp_server(
        goal_folders=governed_goal_folder_store(str(folder_root)),
        goal_roster=roster,
    )

    async def call(url: str) -> dict[str, Any]:
        async with Client(url) as client:
            result = await client.call_tool("goal_enroll", {"folder_id": "wf-1"})
            return _payload(result)

    with running_server(server) as url:
        admitted = asyncio.run(call(url))

    persisted = roster.get("wf-1") or {}
    starts = bool(admitted.get("liveness")) and all(
        item.get("started") is True for item in admitted.get("liveness", [])
    )
    passed = bool(
        admitted.get("already_admitted") is False
        and admitted.get("briefing_version") == BRIEFING_VERSION
        and admitted.get("mechanism") == GOAL_ENROLL_MECHANISM
        and starts
        and persisted.get("briefing_version") == BRIEFING_VERSION
    )
    return evidence(
        "enroll-drill-line-end-to-end",
        passed,
        admitted=admitted,
        persisted_briefing_version=persisted.get("briefing_version"),
        line_starts=starts,
    )


# --- cli ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        default=None,
        choices=[
            "no-acceptance-goal-fail-closed",
            "enroll-drill-line-end-to-end",
        ],
        help="run one scenario only; default runs both (the full acceptance)",
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
        scenarios = ["no-acceptance-goal-fail-closed", "enroll-drill-line-end-to-end"]

    results: list[dict[str, Any]] = []
    for scenario in scenarios:
        if scenario == "no-acceptance-goal-fail-closed":
            result = scenario_no_acceptance_goal_fail_closed(work_dir)
        else:
            result = scenario_enroll_drill_line_end_to_end(work_dir)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))

    return 0 if all(result.get("pass") for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
