#!/usr/bin/env python3
"""Reconcile-source binding acceptance: the real MCP surface with a concrete source.

Four end-to-end scenarios, each driving the *real* ``build_mcp_server`` dev-dispatch
MCP surface over HTTP with the concrete ``GitWorkFolderSource`` (never a FakeSource
and never a directly constructed test-only reconciler):

- ``safe-dry-run`` -- a governed work folder with an append-only tracked residue
  classifies ``adoptable`` and returns a plan + confirmation token, mutating nothing.
- ``cas-confirm`` -- confirming that token adopts exactly the appended bytes, seals
  the ``WorkFolderReconciler.adopt`` receipt, and leaves the repository clean.
- ``stale-token-refusal`` -- after the dry-run the working bytes change, so the stale
  token refuses closed and the bytes stay byte-for-byte identical.
- ``unsafe-residue-refusal`` -- a rewrite (non-append) residue refuses closed and the
  bytes stay byte-for-byte identical.

Each isolated fixture is a disposable git repository (a governed work-folder fixture).
Raw request/response JSON for every call is printed, a fresh UTC timestamp is printed
during the run, and no physical backing-store path is ever printed. The process exits
non-zero when any invariant is absent.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

from fleet_graph.dd.control_plane import DdControlPlane
from fleet_graph.dd.reconcile import CLS_ADOPTABLE, RECONCILE_MECHANISM
from fleet_graph.dd.service import build_mcp_server
from fleet_graph.dd.work_folder_store import GitWorkFolderSource

BASE = b"# progress example\n- first\n"
APPEND = b"- adopt the residue\n"
REWRITE = b"# progress example\n- first (rewritten)\n"
BOOKKEEPING = "progress.md"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def scrub_proxy_env() -> None:
    """fastmcp/httpx honours host proxy env even for loopback; strip it."""
    for var in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        os.environ.pop(var, None)


def run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def make_fixture(parent: Path, name: str, *, working: bytes) -> tuple[str, Path]:
    """A disposable governed work-folder fixture: committed base + working bytes."""
    repo = parent / name
    repo.mkdir(parents=True, exist_ok=True)
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.name", "fixture")
    run_git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / BOOKKEEPING).write_bytes(BASE)
    run_git(repo, "add", "--", BOOKKEEPING)
    run_git(repo, "commit", "-q", "-m", "baseline")
    (repo / BOOKKEEPING).write_bytes(working)
    folder_id = f"acct-{name}"
    return folder_id, repo


def resolve_in(root: Path):
    def resolve(folder_id: str) -> Path | None:
        if not folder_id.startswith("acct-"):
            return None
        return root / folder_id[len("acct-") :]

    return resolve


@contextmanager
def running_server(server: Any) -> Iterator[str]:
    import uvicorn

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
        for _ in range(200):
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


def _payload(result: Any) -> dict[str, Any]:
    data = getattr(result, "structured_content", None) or getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    content = getattr(result, "content", None)
    if content:
        return json.loads(getattr(content[0], "text", None))
    raise AssertionError(f"unexpected tool result: {result!r}")


def _error_payload(message: str) -> dict[str, Any]:
    try:
        return json.loads(message[message.index("{") : message.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return {"raw": message}


async def exercise(url: str, folder_id: str, token: str | None = None) -> dict[str, Any]:
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    arguments: dict[str, Any] = {"folder_id": folder_id}
    if token is not None:
        arguments["token"] = token
    request = {"tool": "wf_reconcile", "arguments": arguments}
    async with Client(url) as client:
        try:
            result = await client.call_tool("wf_reconcile", arguments)
        except ToolError as exc:
            return {"request": request, "error": _error_payload(str(exc))}
    return {"request": request, "response": _payload(result)}


def scenario_dry_run(url: str, folder_id: str) -> dict[str, Any]:
    result = asyncio.run(exercise(url, folder_id))
    response = result.get("response", {})
    entries = response.get("entries") or []
    passed = bool(
        "error" not in result
        and entries
        and entries[0].get("classification") == CLS_ADOPTABLE
        and isinstance(response.get("token"), str)
        and response["token"].startswith("sha256:")
    )
    return {"scenario": "safe-dry-run", "pass": passed, **result}


def scenario_cas_confirm(url: str, folder_id: str, repo: Path) -> dict[str, Any]:
    dry = asyncio.run(exercise(url, folder_id))
    token = dry.get("response", {}).get("token")
    confirmed = (
        asyncio.run(exercise(url, folder_id, token=token))
        if token
        else {"error": {"raw": "no token"}}
    )
    head = run_git(repo, "show", f"HEAD:{BOOKKEEPING}")
    status = run_git(repo, "status", "--porcelain")
    adopted = (confirmed.get("response") or {}).get("adopted") or []
    passed = bool(
        token
        and "error" not in confirmed
        and (confirmed.get("response") or {}).get("mechanism") == RECONCILE_MECHANISM
        and any(entry.get("filename") == BOOKKEEPING for entry in adopted)
        and head.encode() == BASE + APPEND
        and status == ""
    )
    return {
        "scenario": "cas-confirm",
        "pass": passed,
        "dry_run_request": dry.get("request"),
        "dry_run_response": dry.get("response"),
        "confirm_request": confirmed.get("request"),
        "confirm_response": confirmed.get("response") or confirmed.get("error"),
        "confirmed_head_matches": head.encode() == BASE + APPEND,
        "repository_clean": status == "",
    }


def scenario_stale_token(url: str, folder_id: str, repo: Path) -> dict[str, Any]:
    dry = asyncio.run(exercise(url, folder_id))
    token = dry.get("response", {}).get("token")
    (repo / BOOKKEEPING).write_bytes(BASE + APPEND + b"- drifted\n")
    before = (repo / BOOKKEEPING).read_bytes()
    confirmed = (
        asyncio.run(exercise(url, folder_id, token=token))
        if token
        else {"error": {"raw": "no token"}}
    )
    after = (repo / BOOKKEEPING).read_bytes()
    error = (confirmed.get("error") or {}).get("code")
    passed = bool(
        token
        and error == "RECONCILE_REFUSED"
        and before == after
        and before == BASE + APPEND + b"- drifted\n"
    )
    return {
        "scenario": "stale-token-refusal",
        "pass": passed,
        "dry_run_request": dry.get("request"),
        "confirm_request": confirmed.get("request"),
        "confirm_error": confirmed.get("error"),
        "bytes_unchanged": before == after,
    }


def scenario_unsafe_residue(url: str, folder_id: str, repo: Path) -> dict[str, Any]:
    before = (repo / BOOKKEEPING).read_bytes()
    result = asyncio.run(exercise(url, folder_id))
    after = (repo / BOOKKEEPING).read_bytes()
    error = (result.get("error") or {}).get("code")
    passed = bool(before == REWRITE and error == "RECONCILE_REFUSED" and before == after)
    return {
        "scenario": "unsafe-residue-refusal",
        "pass": passed,
        "request": result.get("request"),
        "error": result.get("error"),
        "bytes_unchanged": before == after,
    }


def run() -> int:
    scrub_proxy_env()
    root = Path(tempfile.mkdtemp(prefix="reconcile-source-binding-"))
    fixtures: dict[str, tuple[str, Path]] = {}

    fixtures["adoptable"] = make_fixture(root, "adoptable", working=BASE + APPEND)
    fixtures["cas-confirm"] = make_fixture(root, "cas-confirm", working=BASE + APPEND)
    fixtures["stale"] = make_fixture(root, "stale", working=BASE + APPEND)
    fixtures["unsafe"] = make_fixture(root, "unsafe", working=REWRITE)

    source = GitWorkFolderSource(resolve_in(root))
    server = build_mcp_server(DdControlPlane(), work_folders=source)

    results: list[dict[str, Any]] = []
    with running_server(server) as url:
        results.append(scenario_dry_run(url, fixtures["adoptable"][0]))
        results.append(
            scenario_cas_confirm(url, fixtures["cas-confirm"][0], fixtures["cas-confirm"][1])
        )
        results.append(scenario_stale_token(url, fixtures["stale"][0], fixtures["stale"][1]))
        results.append(scenario_unsafe_residue(url, fixtures["unsafe"][0], fixtures["unsafe"][1]))

    envelope = {
        "acceptance": "reconcile-source-binding",
        "utc_timestamp": utc_now(),
        "scenarios": results,
    }
    print(json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(result.get("pass") for result in results) else 1


def main(argv: list[str] | None = None) -> int:
    del argv
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
