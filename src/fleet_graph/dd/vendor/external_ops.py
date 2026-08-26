"""Unified boundary for every out-of-process / out-of-host call.

Spec "外部调用统一超时 + 可区分的超时失败码": every Git subprocess, every
``gh`` CLI invocation, and every jobd/controller/agent-bus HTTP call in the
controller must enter the outside world through exactly one wrapper that
**always** applies a configurable timeout. No call point may pass its own
literal ``timeout`` (that would silently bypass the configuration and make it
dead code), so ``run_process`` / ``http_request`` here expose **no** timeout
parameter: the timeout is resolved from the controller configuration
(``external_call.timeout_seconds``) which ``config.load_config`` installs into
this module.

When a configured timeout fires, the wrapper raises :class:`ExternalCallTimeout`
which carries ``code == EXTERNAL_CALL_TIMEOUT`` and inherits **only** from
``Exception`` -- deliberately NOT from ``subprocess.TimeoutExpired`` or
``OSError`` -- so no legacy ``except (OSError, subprocess.TimeoutExpired)``
branch anywhere in the product can collapse it back into a generic/legacy
failure code. The named code instead survives to the reconcile failure
pipeline, is persisted, is treated as transient (it is not in
``DETERMINISTIC_FAILURE_CODES``), and is served by
``GET /v1/developments/{id}``.
"""

from __future__ import annotations

import subprocess
from typing import Any

import httpx

# The single named, distinguishable failure code produced by any external-call
# timeout. Used as the ``code`` carried by ExternalCallTimeout so the reconcile
# failure pipeline persists it verbatim.
EXTERNAL_CALL_TIMEOUT = "EXTERNAL_CALL_TIMEOUT"

# Network-type external operations should never hang the controller forever; the
# spec asks for a sane default in the 60-120s band. Individual deployments can
# override it via ``external_call.timeout_seconds`` in the config file.
DEFAULT_EXTERNAL_CALL_TIMEOUT_SECONDS = 90.0


class ExternalCallTimeout(Exception):
    """A configured external-call timeout fired.

    Distinguishable on purpose: it is a plain ``Exception`` (not
    ``subprocess.TimeoutExpired`` / ``OSError``) so that no existing
    ``except`` branch that catches those types can absorb it. ``code`` is the
    named, persisted, GET-readable failure code.
    """

    code = EXTERNAL_CALL_TIMEOUT

    def __init__(self, kind: str, detail: str = "") -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind} external call timed out: {detail}")


_timeout_seconds: float = DEFAULT_EXTERNAL_CALL_TIMEOUT_SECONDS


def set_timeout_seconds(seconds: float) -> None:
    """Install the configured timeout from the controller configuration."""
    global _timeout_seconds
    _timeout_seconds = float(seconds)


def timeout_seconds() -> float:
    """The currently configured external-call timeout in seconds."""
    return _timeout_seconds


def run_process(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    text: bool = False,
    input: str | bytes | None = None,
    capture_output: bool = True,
    check: bool = False,
    kind: str = "subprocess",
) -> subprocess.CompletedProcess[Any]:
    """Run one subprocess under the configured timeout.

    This is the only sanctioned way to spawn a controller-internal child process
    (Git, ``gh``, and any other external binary whose hang would block the
    controller). The timeout always comes from
    ``external_call.timeout_seconds``; no call point may supply its own. On
    timeout it raises :class:`ExternalCallTimeout` instead of letting
    ``subprocess.TimeoutExpired`` leak out.
    """
    try:
        return subprocess.run(
            argv,
            env=env,
            cwd=cwd,
            text=text,
            input=input,
            capture_output=capture_output,
            timeout=_timeout_seconds,
            check=check,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExternalCallTimeout(kind, str(exc)) from exc


def run_bounded_command(
    argv: list[str],
    *,
    timeout: float,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    text: bool = False,
    input: str | bytes | None = None,
    capture_output: bool = True,
    check: bool = False,
    kind: str = "command",
) -> subprocess.CompletedProcess[Any]:
    """Run one *declared-timeout* child process (acceptance / plugin commands).

    This is deliberately a separate primitive from :func:`run_process`. The
    acceptance command executor and the plugin materialization scripts run the
    development's own declared commands, each of which carries its own explicit
    ``timeout_seconds`` bound (e.g. a 900s ``uv sync``). Those bounds are a
    per-command contract, not the controller external-call timeout, and must not
    be silently shrunk to the external-call default. Both entry points live in
    this one module so the structural coverage check (no bare ``subprocess.run``
    in the package) still holds. On timeout it raises
    :class:`ExternalCallTimeout` too, so the caller can translate it to the same
    failure it already maps ``subprocess.TimeoutExpired`` to.
    """
    try:
        return subprocess.run(
            argv,
            env=env,
            cwd=cwd,
            text=text,
            input=input,
            capture_output=capture_output,
            timeout=timeout,
            check=check,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExternalCallTimeout(kind, str(exc)) from exc


def http_request(
    method: str,
    url: str,
    *,
    json: Any = None,
    headers: dict[str, str] | None = None,
    params: Any = None,
    kind: str = "http",
) -> httpx.Response:
    """Issue one HTTP request under the configured timeout.

    This is the only sanctioned way to make an out-of-process HTTP call (jobd,
    the in-process controller from the MCP gateway, the agent-bus projector).
    On timeout it raises :class:`ExternalCallTimeout` instead of letting an
    ``httpx`` timeout exception leak out.
    """
    try:
        return httpx.request(
            method,
            url,
            json=json,
            headers=headers,
            params=params,
            timeout=_timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        raise ExternalCallTimeout(kind, str(exc)) from exc
