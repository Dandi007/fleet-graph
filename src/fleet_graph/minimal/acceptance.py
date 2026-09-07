"""Minimal acceptance runner: fail-fast execution of a goal's acceptance commands.

Every goal carries programmatic acceptance commands (GO-6.1); the engine runs
them after the Impl commit and they must all exit 0 before the DD enters CR
(docs/specs/minimal/design.md §3.2). This module is that execution layer:

- ``combine_acceptance`` (protocol §1/§2): the goal's ``acceptance`` plus the
  DD's ``acceptance_extra``, order-preserving dedup, empty entries skipped.
- ``run_acceptance`` (fail-fast): commands run in order; the first non-zero
  or timed-out command stops the batch -- later commands are neither executed
  nor recorded.
- ``acceptance_results`` / ``acceptance_event_payload``: projections into the
  ``[{cmd, exit, tail}]`` review-input shape (protocol §5/§7) and the
  ``dd.acceptance`` event payload (protocol §8).

No events are written, no git is run, no retry policy lives here: whether a
failed batch bounces the DD back to Impl is another DD's state machine.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Protocol

# The tail budget each command's output is squeezed into. Chars, not bytes:
# tails end up inside JSON envelopes, where the char count is what bounds the
# line size. 4000 chars keeps even a noisy command readable in an event
# payload without letting a failing build dump its whole log into the log.
TAIL_LIMIT = 4000

# What the truncation marker counts: the chars dropped from the front of the
# original text (never negative, even when the marker pushes a short remainder
# a few chars over the line boundary). The trailing newline puts the marker on
# its own line, so the content below it always starts at a line start.
_TRUNCATION_MARKER = "…[truncated {n} chars]\n"


def tail_of(text: str, limit: int = TAIL_LIMIT) -> str:
    """The last ``limit`` chars of ``text``, cut at a line boundary.

    Truncation is the exception, not the rule: short output is returned
    verbatim. When cutting is needed the cut lands on the first line boundary
    inside the budget (a leading partial line would start mid-sentence and
    carry no context). A truncation marker naming the number of dropped chars
    is prepended on its own line, so the boundary between marker and content
    survives any later diffing. Length is bounded: the kept tail never exceeds
    ``limit`` chars, plus the marker line; when even the last line alone
    exceeds the budget, that line is cut at the char limit and the marker
    names the total number of dropped chars.
    """

    if limit < 0:
        raise ValueError("limit must be >= 0")
    if len(text) <= limit:
        return text

    # At least `start` chars must go; cut at the first line boundary from
    # there on, so the kept tail is the largest one that still fits the
    # budget and every kept line is whole.
    start = len(text) - limit
    nl = text.find("\n", start - 1)
    if nl == -1:
        # No line boundary in the tail region: the last line alone exceeds
        # the budget, so cut it at the char limit (still bounded, still a tail).
        return _TRUNCATION_MARKER.format(n=start) + text[start:]
    head_len = nl + 1
    return _TRUNCATION_MARKER.format(n=head_len) + text[head_len:]


def combine_acceptance(base: list[str] | None, extra: list[str] | None) -> list[str]:
    """Goal ``acceptance`` plus DD ``acceptance_extra``, order-preserving dedup.

    Protocol §1 requires at least one goal acceptance command, so a ``base``
    with no non-empty entries (None, [], or only empty strings) raises
    ``ValueError`` -- an acceptance batch with nothing to run must never be
    silently green. Empty-string entries are skipped, first occurrence wins,
    and the goal's commands keep their order ahead of the DD extras.
    """

    if not base or not any(base):
        raise ValueError("goal acceptance must contain at least one command (protocol §1)")
    combined: list[str] = []
    for cmd in [*base, *(extra or [])]:
        if not cmd:
            continue
        if cmd not in combined:
            combined.append(cmd)
    return combined


@dataclass(frozen=True)
class Completed:
    """One executed command: merged output plus its verdict."""

    exit_code: int
    output: str
    timed_out: bool


class Runner(Protocol):
    """Every command funnels through this; injectable like gitgate's GitRunner."""

    def run(self, cmd: str, cwd: str, timeout_s: int, env: dict[str, str] | None) -> Completed:
        """Run ``cmd`` in ``cwd``; ``env=None`` means inherit the parent env."""
        ...  # pragma: no cover - protocol body


class BashRunner:
    """Default ``Runner``: ``bash -lc <cmd>`` with whole-process-group kill.

    Each command runs as ``bash -lc <cmd>`` in ``cwd`` (the worktree) with
    stdout and stderr merged into one stream, in its own session
    (``start_new_session=True``). On timeout the *entire process group* is
    killed with SIGKILL (bash -l spawns children a plain process.kill() would
    orphan), and the verdict is ``timed_out=True`` with ``exit_code=-9``: the
    negative signal number of the kill, as subprocess reports it. A timeout
    must read as failure to the fail-fast loop, and -9 != 0 does exactly that.
    """

    def run(self, cmd: str, cwd: str, timeout_s: int, env: dict[str, str] | None) -> Completed:
        with subprocess.Popen(
            ["bash", "-lc", cmd],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,
        ) as proc:
            try:
                output, _ = proc.communicate(timeout=timeout_s)
                return Completed(
                    exit_code=proc.returncode if proc.returncode is not None else -1,
                    output=output or "",
                    timed_out=False,
                )
            except subprocess.TimeoutExpired:
                # Kill the whole process group bash -l spawned (its children
                # are not Popen's children); SIGKILL so a stuck test cannot
                # trap/ignore its way past the timeout.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                output, _ = proc.communicate()
                return Completed(
                    exit_code=proc.returncode if proc.returncode is not None else -9,
                    output=output or "",
                    timed_out=True,
                )


@dataclass(frozen=True)
class CommandResult:
    """One acceptance command's outcome; ``tail`` is the bounded output tail."""

    cmd: str
    exit_code: int
    tail: str
    duration_s: float
    timed_out: bool


@dataclass(frozen=True)
class AcceptanceRun:
    """The verdict of one fail-fast acceptance batch.

    ``results`` holds exactly the commands that ran: after the first failure
    the batch stops, so later commands appear neither here nor anywhere else.
    ``passed`` is True only when at least one command ran and all of them
    exited 0. ``failed_cmd`` names the stopping command, or None on a full
    pass.
    """

    results: tuple[CommandResult, ...]
    passed: bool
    failed_cmd: str | None


def run_acceptance(
    cmds: list[str],
    *,
    cwd: str,
    runner: Runner,
    timeout_s: int = 1800,
    env: dict[str, str] | None = None,
) -> AcceptanceRun:
    """Run ``cmds`` in order, stopping at the first non-zero or timed-out one.

    ``cmds`` must be non-empty (protocol §1: an acceptance batch always has at
    least one command; an empty one raises ``ValueError`` rather than passing
    vacuously). Each command runs through the injected ``runner`` in ``cwd``
    with the per-command ``timeout_s`` and optional ``env``. Fail-fast: the
    first command with a non-zero exit code or a timeout is the last one
    executed; commands after it are neither run nor recorded.
    """

    if not cmds:
        raise ValueError("run_acceptance requires at least one command")
    results: list[CommandResult] = []
    failed_cmd: str | None = None
    for cmd in cmds:
        start = time.monotonic()
        completed = runner.run(cmd, cwd, timeout_s, env)
        duration_s = time.monotonic() - start
        result = CommandResult(
            cmd=cmd,
            exit_code=completed.exit_code,
            tail=tail_of(completed.output),
            duration_s=duration_s,
            timed_out=completed.timed_out,
        )
        results.append(result)
        if completed.exit_code != 0 or completed.timed_out:
            failed_cmd = cmd
            break
    passed = failed_cmd is None and len(results) == len(cmds) and len(results) >= 1
    return AcceptanceRun(results=tuple(results), passed=passed, failed_cmd=failed_cmd)


def acceptance_results(run: AcceptanceRun) -> list[dict]:
    """Project a run into protocol §5/§7's ``[{cmd, exit, tail}]`` shape.

    The exit key is ``exit`` (the protocol field name), not ``exit_code``;
    entries appear in execution order, which fail-fast already guarantees is
    command order.
    """

    return [
        {"cmd": result.cmd, "exit": result.exit_code, "tail": result.tail} for result in run.results
    ]


def acceptance_event_payload(result: CommandResult, *, index: int, total: int) -> dict:
    """The payload of one ``dd.acceptance`` event (protocol §8: one per command).

    ``index`` is the command's 0-based position in the batch and ``total`` the
    number of commands the batch was composed of (post-dedup), so a reader of
    the event log can see both where this command sat and how much of the
    batch fail-fast actually let run.
    """

    return {
        "cmd": result.cmd,
        "exit": result.exit_code,
        "tail": result.tail,
        "duration_s": result.duration_s,
        "timed_out": result.timed_out,
        "index": index,
        "total": total,
    }


__all__ = [
    "TAIL_LIMIT",
    "AcceptanceRun",
    "BashRunner",
    "CommandResult",
    "Completed",
    "Runner",
    "acceptance_event_payload",
    "acceptance_results",
    "combine_acceptance",
    "run_acceptance",
    "tail_of",
]
