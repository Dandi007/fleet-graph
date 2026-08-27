"""The mechanical acceptance step: declared argv in, exit codes and tails out.

Three running lines spent weeks writing "NOT RUN: no process-execution
primitive" into every ritual round: acceptance commands were declared, but no
role could execute them. The ruling that fixes it draws the line on the same
counts-versus-prose boundary the scheduler lives by -- **execution belongs to
the orchestration layer, judgement stays with the coordinator**. This module
runs argv lists and reports facts; it never decides what they mean.

Where the declaration lives matters as much as who runs it. The commands come
from the scheduler's roster config -- a PR-reviewed file in this repo -- and
never from goal.md or the work folder: anything an agent can write is an
improper control input for what gets executed on this host (wf-13ff9e
findings §31c). The trust anchor is the roster's PR review; the argv being
visible in `systemctl --user` output is acceptable precisely because nothing
secret and nothing agent-authored is in it.

Two absences are stated out loud rather than skipped, because "no acceptance
was declared" and "acceptance passed" being confusable is exactly the failure
this step exists to end:

- no commands declared        -> ``{"status": "not_declared"}``
- commands but no cwd         -> ``{"status": "skipped:no_cwd"}`` -- where a
  command runs is part of the reviewed declaration, and inheriting the
  engine's own working directory would smuggle ambient state into it.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any

#: Synthetic exit codes for commands that never got to return one, following
#: the same shell convention supervise/audit.py uses: 124 timeout, 127 not
#: found.
EXIT_TIMEOUT = 124
EXIT_NOT_FOUND = 127

#: Kept per stream (stdout and stderr each), matching audit.py's TAIL.
TAIL = 2000

DEFAULT_TIMEOUT_SECONDS = 300

#: The only environment a declared command inherits. An explicit whitelist,
#: extended only by editing this constant in a PR -- the scheduler's own
#: environment (bus tokens, gateway credentials) must never leak into a
#: command an agent's work is graded by.
ENV_KEEP = ("PATH", "HOME")

STATUS_NOT_DECLARED = "not_declared"
STATUS_SKIPPED_NO_CWD = "skipped:no_cwd"
STATUS_RAN = "ran"
STATUS_ERROR = "acceptance_error"


@dataclass(frozen=True)
class AcceptanceSpec:
    """What the roster declared: which commands, where, and how long each gets."""

    argvs: tuple[tuple[str, ...], ...] = ()
    cwd: str | None = None
    #: Per command, not for the batch.
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    def to_cli_json(self) -> str:
        """The one-argument JSON the launcher passes as `--acceptance-json`.

        One argument rather than repeated flags so the declaration crosses the
        systemd-run boundary as a single opaque token: no quoting rules, no
        argv-splitting reimplementation on the far side.
        """
        return json.dumps(
            {
                "argvs": [list(argv) for argv in self.argvs],
                "cwd": self.cwd,
                "timeout_seconds": self.timeout_seconds,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_cli_json(cls, text: str) -> AcceptanceSpec:
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("--acceptance-json must hold an object")
        argvs = tuple(tuple(str(part) for part in argv) for argv in raw.get("argvs") or [] if argv)
        cwd = raw.get("cwd")
        return cls(
            argvs=argvs,
            cwd=str(cwd) if cwd is not None else None,
            timeout_seconds=int(raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        )


def acceptance_environment() -> dict[str, str]:
    return {key: os.environ[key] for key in ENV_KEEP if os.environ.get(key)}


def run_acceptance(
    spec: AcceptanceSpec | None,
    *,
    run: Any = subprocess.run,
    clock: Any = time.monotonic,
) -> dict[str, Any]:
    """Run the declared commands in order and report the facts.

    Every command runs, whatever the previous one returned -- a red first
    command hiding the second's result would make the report less than the
    declaration. No `[ -f ] &&` guards, no interpretation, no verdict.
    """
    if spec is None or not spec.argvs:
        return {"status": STATUS_NOT_DECLARED}
    if not spec.cwd:
        return {"status": STATUS_SKIPPED_NO_CWD, "commands": len(spec.argvs)}

    results: list[dict[str, Any]] = []
    for argv in spec.argvs:
        command = list(argv)
        started = clock()
        try:
            proc = run(
                command,
                cwd=spec.cwd,
                env=acceptance_environment(),
                capture_output=True,
                text=True,
                timeout=spec.timeout_seconds,
            )
            exit_code = proc.returncode
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
        except FileNotFoundError as exc:
            exit_code, stdout, stderr = EXIT_NOT_FOUND, "", f"command not found: {exc}"
        except subprocess.TimeoutExpired:
            exit_code, stdout, stderr = (
                EXIT_TIMEOUT,
                "",
                f"timed out after {spec.timeout_seconds}s",
            )
        results.append(
            {
                "command": command,
                "exit_code": exit_code,
                "duration_s": round(clock() - started, 3),
                "tail": {"stdout": stdout[-TAIL:], "stderr": stderr[-TAIL:]},
            }
        )
    return {"status": STATUS_RAN, "results": results}


class AcceptanceRunner:
    """The AcceptancePort the goal-line graph holds. One spec, run per round."""

    def __init__(self, spec: AcceptanceSpec | None) -> None:
        self.spec = spec

    def run(self) -> dict[str, Any]:
        return run_acceptance(self.spec)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "ENV_KEEP",
    "EXIT_NOT_FOUND",
    "EXIT_TIMEOUT",
    "STATUS_ERROR",
    "STATUS_NOT_DECLARED",
    "STATUS_RAN",
    "STATUS_SKIPPED_NO_CWD",
    "TAIL",
    "AcceptanceRunner",
    "AcceptanceSpec",
    "acceptance_environment",
    "run_acceptance",
]
