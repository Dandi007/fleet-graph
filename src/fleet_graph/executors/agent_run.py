"""Detached dispatch of `bin/agent-run`, with the re-adopt guarantee.

The problem this solves
-----------------------
loop-engine's jobd kept worker processes alive across a daemon restart via
``KillMode=process``, and re-attached to them afterwards. fleet-graph has to
provide the same property, because the orchestration process is expected to
restart (deploys, crashes, OOM) while agent runs that cost real money and real
minutes are in flight. Losing track of one means either dropping its result or
dispatching it twice.

How it works
------------
Two decisions make re-adopt fall out almost for free:

1. **The run id is derived, not random.** ``derive_run_id`` is a uuid5 over
   ``(thread_id, node, attempt)``, so a restarted graph computes *the same* id
   for the same logical step. Nothing needs to have survived in memory.
2. **Each run gets its own session root**, named after that id. So the run
   directory is discoverable from the id alone -- no registry to keep in sync,
   and no window where a crash orphans a run because the bookkeeping write
   landed after the spawn.

``launch`` is therefore idempotent: handed an id whose session root already
holds a live process or a finished ``result.json``, it adopts instead of
spawning. That is the whole contract, and ``tests/test_re_adopt.py`` pins it.

Invariant 4 (durable state is work folder + git) still holds: everything here
is in-flight, and the checkpointer copy of a ticket is a cache. Throw it away
and the derived id rebuilds it.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

# Stable namespace for derived run ids. Changing it re-randomises every id and
# breaks re-adopt for runs in flight at the moment of the change.
RUN_ID_NAMESPACE = uuid.UUID("6f6c3c8e-2b6a-5f21-9c47-1f0f5a4d8e10")

DEFAULT_AGENT_RUN_BIN = "/data/code/self/agent-runtime/bin/agent-run"
DEFAULT_STATE_ROOT = "/data/fleet-graph/runs"

RunState = Literal["running", "succeeded", "failed", "lost"]


class RunWaitTimeout(TimeoutError):
    """`wait` gave up while the run was still going.

    Deliberately not a RunState. "I stopped waiting" and "the run is gone" are
    different facts, and collapsing them is how you get a duplicate dispatch:
    a caller told `lost` will reasonably retry, and the original run is still
    out there burning tokens. The ticket rides along so the caller can drop it
    into a checkpoint and re-adopt later.
    """

    def __init__(self, ticket: RunTicket, waited_seconds: float) -> None:
        super().__init__(
            f"run {ticket.run_id} still running after {waited_seconds:.1f}s; "
            "not terminal -- re-adopt it rather than re-dispatching"
        )
        self.ticket = ticket
        self.waited_seconds = waited_seconds


def derive_run_id(thread_id: str, node: str, attempt: int = 1) -> str:
    """Derive the run id for one logical step of one graph thread.

    Deterministic on purpose -- see the module docstring. `attempt` is the
    escape hatch for a *deliberate* retry: bump it and you get a genuinely new
    run instead of re-adopting the old one.
    """
    if attempt < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    return str(uuid.uuid5(RUN_ID_NAMESPACE, f"{thread_id}\x1f{node}\x1f{attempt}"))


@dataclass(frozen=True)
class AgentRunSpec:
    """What to ask of agent-run. Mirrors the CLI rather than abstracting it.

    Invariant 2: the executor is a process boundary, and `runtime` stays a
    plain parameter so swapping harnesses is a config change.
    """

    prompt: str
    role: str | None = None
    agent: str | None = None
    runtime: str | None = None
    route: str | None = None
    model: str | None = None
    isolation: str = "full"
    write: bool = False
    timeout_seconds: int = 900
    cwd: str | None = None
    structured: bool = False
    labels: dict[str, str] = field(default_factory=dict)
    mcp_allow: tuple[str, ...] = ()

    def argv(self, *, bin_path: str, run_id: str, session_root: str) -> list[str]:
        argv = [bin_path]
        if self.agent:
            argv += ["--agent", self.agent]
        if self.role:
            argv += ["--role", self.role]
        if self.runtime:
            argv += ["--runtime", self.runtime]
        if self.route:
            argv += ["--route", self.route]
        if self.model:
            argv += ["--model", self.model]
        argv += ["--isolation", self.isolation]
        argv += ["--timeout", str(self.timeout_seconds)]
        argv += ["--run-id", run_id]
        argv += ["--session-root", session_root]
        argv += ["--json"]
        if self.write:
            argv += ["--write"]
        if self.structured:
            argv += ["--structured"]
        if self.cwd:
            argv += ["--cwd", self.cwd]
        for server in self.mcp_allow:
            argv += ["--mcp-allow", server]
        for key, value in sorted(self.labels.items()):
            argv += ["--label", f"{key}={value}"]
        argv += ["--", self.prompt]
        return argv


@dataclass(frozen=True)
class RunTicket:
    """The in-flight handle. Safe to checkpoint, cheap to rebuild."""

    run_id: str
    session_root: str
    pid: int | None = None
    adopted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunTicket:
        return cls(
            run_id=data["run_id"],
            session_root=data["session_root"],
            pid=data.get("pid"),
            adopted=bool(data.get("adopted", False)),
        )


@dataclass(frozen=True)
class RunStatus:
    state: RunState
    result: dict[str, Any] | None = None

    @property
    def terminal(self) -> bool:
        return self.state != "running"

    @property
    def ok(self) -> bool:
        return self.state == "succeeded"


def _pid_is_our_run(pid: int, run_id: str) -> bool:
    """True when `pid` is alive *and* is the agent-run for `run_id`.

    The cmdline check matters: after a reboot or a long outage a recycled pid
    could otherwise be mistaken for a live run, and we would wait forever on a
    process that has nothing to do with us.
    """
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    if _is_zombie(pid):
        # Exited, just not reaped yet. os.kill(pid, 0) still succeeds for these,
        # so liveness alone would keep us waiting on a run that is already over.
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        # No procfs to confirm with; liveness alone is the best we can do.
        return True
    if not cmdline:
        # An empty cmdline is not evidence of a *different* process. The kernel
        # blanks it for the width of an execve, so a run we launched moments
        # ago reads as empty while the shell hands off to the real binary.
        # Reading that as "not ours" made poll() report `lost` for a perfectly
        # healthy run -- and `lost` invites a duplicate dispatch. Zombies are
        # already excluded above, so an empty cmdline here means mid-exec.
        return True
    return run_id.encode() in cmdline


def _is_zombie(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    # The comm field is parenthesised and may itself contain spaces, so split
    # on the last ')' rather than on whitespace.
    _, _, rest = stat.rpartition(")")
    fields = rest.split()
    return bool(fields) and fields[0] == "Z"


def find_result(session_root: str | Path) -> dict[str, Any] | None:
    """Return the parsed result.json under a session root, if agent-run wrote one."""
    root = Path(session_root)
    if not root.is_dir():
        return None
    for candidate in sorted(root.glob("*/result.json")):
        try:
            return json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError):
            continue
    return None


def find_run_dir(session_root: str | Path) -> str | None:
    root = Path(session_root)
    if not root.is_dir():
        return None
    children = sorted(p for p in root.iterdir() if p.is_dir())
    return str(children[0]) if children else None


class AgentRunLauncher:
    """Launches agent-run detached, and re-adopts what is already running.

    Deliberately not a daemon and not a pool: one method to start work, one to
    ask how it is going. Everything that needs to outlive the process lives on
    disk under `state_root`.
    """

    def __init__(
        self,
        *,
        bin_path: str = DEFAULT_AGENT_RUN_BIN,
        state_root: str = DEFAULT_STATE_ROOT,
    ) -> None:
        self.bin_path = bin_path
        self.state_root = Path(state_root)

    def session_root_for(self, run_id: str) -> Path:
        return self.state_root / run_id

    def launch(self, spec: AgentRunSpec, run_id: str) -> RunTicket:
        """Start `spec` under `run_id`, or adopt it if it is already going.

        Idempotent by construction. Calling this twice with the same run_id --
        including across a process restart -- dispatches once.
        """
        session_root = self.session_root_for(run_id)
        pidfile = session_root / "launcher.pid"

        if session_root.exists():
            if find_result(session_root) is not None:
                return RunTicket(run_id, str(session_root), _read_pid(pidfile), adopted=True)
            pid = _read_pid(pidfile)
            if pid is not None and _pid_is_our_run(pid, run_id):
                return RunTicket(run_id, str(session_root), pid, adopted=True)
            # Root exists but nothing is running and nothing finished: the
            # previous attempt died between mkdir and exec. Fall through and
            # spawn -- there is no run to lose.

        session_root.mkdir(parents=True, exist_ok=True)
        argv = spec.argv(bin_path=self.bin_path, run_id=run_id, session_root=str(session_root))
        (session_root / "argv.json").write_text(json.dumps(argv, ensure_ascii=False, indent=1))

        stdout_path = session_root / "launcher.stdout"
        stderr_path = session_root / "launcher.stderr"
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            # argv is a built list, never shell-parsed.
            proc = subprocess.Popen(
                argv,
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                # Detach: a new session means the child is not in our process
                # group, so it survives us being killed. This is the
                # KillMode=process property, moved into the launcher.
                start_new_session=True,
                cwd=spec.cwd or None,
            )
        pidfile.write_text(str(proc.pid))
        return RunTicket(run_id, str(session_root), proc.pid, adopted=False)

    def poll(self, ticket: RunTicket) -> RunStatus:
        result = find_result(ticket.session_root)
        if result is not None:
            return _classify(result)

        pid = (
            ticket.pid
            if ticket.pid is not None
            else _read_pid(Path(ticket.session_root) / "launcher.pid")
        )
        if pid is not None and _pid_is_our_run(pid, ticket.run_id):
            return RunStatus("running")

        # The process is gone and we saw no result -- but those two checks are
        # not atomic. A run that finished in between would look identical to a
        # run that died, and calling it lost would throw away a real result
        # (and invite a duplicate dispatch). Re-read before declaring death.
        result = find_result(ticket.session_root)
        if result is not None:
            return _classify(result)

        # Genuinely died without writing a result. Surface it rather than
        # hanging, and let the caller retry with a bumped attempt.
        return RunStatus("lost")

    def wait(
        self,
        ticket: RunTicket,
        *,
        poll_interval: float = 2.0,
        deadline_seconds: float | None = None,
    ) -> RunStatus:
        """Block until the run is terminal.

        Raises RunWaitTimeout if `deadline_seconds` passes first -- see that
        class for why this is not a return value.
        """
        started = time.monotonic()
        while True:
            status = self.poll(ticket)
            if status.terminal:
                return status
            waited = time.monotonic() - started
            if deadline_seconds is not None and waited > deadline_seconds:
                raise RunWaitTimeout(ticket, waited)
            time.sleep(poll_interval)

    def describe(self, ticket: RunTicket) -> str:
        argv_path = Path(ticket.session_root) / "argv.json"
        try:
            argv = json.loads(argv_path.read_text())
        except (OSError, json.JSONDecodeError):
            return ticket.run_id
        return shlex.join(argv)


def _classify(result: dict[str, Any]) -> RunStatus:
    succeeded = result.get("state") == "succeeded" and int(result.get("exit_code") or 0) == 0
    return RunStatus("succeeded" if succeeded else "failed", result)


def _read_pid(pidfile: Path) -> int | None:
    try:
        return int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return None
