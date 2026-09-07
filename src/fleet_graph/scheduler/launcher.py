"""Start a line in its own transient systemd unit.

Why transient units rather than plain subprocesses: the babysitter learned it
the hard way. A line started as a child of the scheduler shares its cgroup, so
when the scheduler is stopped or restarted, systemd takes the whole cgroup with
it and every running line dies at once. `systemd-run --user` gives each line
its own unit and its own cgroup, so the scheduler can be restarted, upgraded or
killed without touching work already in flight.

That property is the same one the re-adopt primitive depends on, from the other
direction: executors survive because they are detached, and lines survive
because they are isolated.

This module builds the command and hands it over. It deliberately does not
decide *whether* to start anything -- that is scheduler/ignition.py, kept
separate so the policy stays reviewable on its own.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_UNIT_PREFIX = "fleet-graph-line"


@dataclass(frozen=True)
class LaunchSpec:
    folder_id: str
    seat: str
    generation: int = 1
    #: The line's agent-bus inbox alias. None means the line has no inbox and
    #: runs the null inbox. Forwarded as `--alias` so the line process builds
    #: a real `Inbox` over `agent:{alias}` instead of a structurally empty one.
    alias: str | None = None
    max_rounds: int = 10
    run_root: Path | None = None
    log_path: Path | None = None
    unit_prefix: str = DEFAULT_UNIT_PREFIX
    working_directory: str = "/data/apps/fleet-graph/current"
    executable: str = "/data/apps/fleet-graph/current/.venv/bin/fleet-graph"
    environment: dict[str, str] = field(default_factory=dict)
    #: The roster's acceptance declaration, already serialised by
    #: AcceptanceSpec.to_cli_json. One JSON argument so it crosses the
    #: systemd-run boundary without quoting rules; visible in argv by design,
    #: because the trust anchor is the roster config's PR review, not secrecy.
    #: None means the roster declared nothing and the line records that fact.
    acceptance_json: str | None = None
    #: The line's streak breakers, forwarded as CLI bounds only when the roster
    #: declared them. None means "absent" -- the line keeps its own defaults --
    #: so a scheduler that never heard of a bound must not pass the runner's
    #: defaults down as if someone had reviewed them.
    noop_limit: int | None = None
    timeout_limit: int | None = None
    #: The board card the scheduler's escalation already materialised for this
    #: line (stall-state ``board_card_entity_id``). Threaded into the line so the
    #: E2 interrupt runtime reuses the existing card instead of publishing a
    #: second one. Empty means the line starts with no known card and the
    #: runtime falls back to the shared constructor.
    board_card_entity_id: str = ""
    #: M5: the revival envelope (who/basis/generation/reason) for a line whose
    #: `done` terminal a valid revoke overturned. One JSON argument, like
    #: acceptance_json, so it crosses the systemd-run boundary without quoting
    #: rules. Absent (None) for a normal launch -- the line then carries no
    #: `revival` fact into its round-1 coordinator input.
    revival: dict[str, Any] | None = None
    #: M3: the development a ``dd_awaiting_gate`` wake names, forwarded as
    #: ``--dd-awaiting-gate`` so the woken line self-delivers that single's
    #: gate decision (six mechanically produced evidence obligations, then
    #: ``decision_deliver``). Empty means an ordinary run: the launch never
    #: invents a wake identity the scheduler did not observe.
    dd_awaiting_gate_development_id: str = ""

    @property
    def log_file(self) -> Path:
        return self.log_path or Path(f"/data/fleet-graph/logs/{self.folder_id}.log")

    @property
    def unit_name(self) -> str:
        """Generation keeps restarts from colliding with a unit systemd is
        still tearing down."""
        return f"{self.unit_prefix}-{self.folder_id}-g{self.generation}"

    def argv(self) -> list[str]:
        run_root = self.run_root or Path(f"/data/fleet-graph/runs/{self.folder_id}")
        log_path = self.log_file

        argv = [
            "systemd-run",
            "--user",
            # --collect: a failed unit is garbage-collected instead of sitting
            # in the failed state and blocking the next launch of the same name.
            "--collect",
            "--unit",
            self.unit_name,
            f"--working-directory={self.working_directory}",
        ]
        for key, value in sorted(self.environment.items()):
            argv += [f"--setenv={key}={value}"]
        argv += [
            # `--property=K=V` as one token, not `-p V` as one token. There is
            # no shell here: execve hands systemd-run whatever string this is,
            # and "-p StandardOutput=..." arrives as a single argument whose
            # value begins with a space -- systemd-run answers
            # "Unknown assignment:  StandardOutput=...". It looks like a typo
            # in the property name and is not.
            f"--property=StandardOutput=append:{log_path}",
            f"--property=StandardError=append:{log_path}",
            self.executable,
            "line",
            "run",
            "--folder",
            self.folder_id,
            "--seat",
            self.seat,
            "--max-rounds",
            str(self.max_rounds),
            "--run-root",
            str(run_root),
            # The line derives every agent-run id from folder:g{generation};
            # passing the generation down is what keeps that identity stable
            # across a kill-restart, so in-flight runs are re-adopted.
            "--generation",
            str(self.generation),
            # Explicit even though it matches the line's own default: the
            # checkpoint being on disk is a contract the scheduler relies on
            # (resume after restart), not an implementation detail to inherit.
            "--checkpoint",
            str(run_root / "checkpoint.sqlite3"),
        ]
        if self.acceptance_json:
            argv += ["--acceptance-json", self.acceptance_json]
        if self.alias:
            argv += ["--alias", self.alias]
        if self.noop_limit is not None:
            argv += ["--noop-limit", str(self.noop_limit)]
        if self.timeout_limit is not None:
            argv += ["--timeout-limit", str(self.timeout_limit)]
        if self.board_card_entity_id:
            argv += ["--board-card", self.board_card_entity_id]
        if self.revival is not None:
            argv += ["--revival", json.dumps(self.revival, ensure_ascii=False)]
        if self.dd_awaiting_gate_development_id:
            argv += [
                "--dd-awaiting-gate",
                self.dd_awaiting_gate_development_id,
            ]
        return argv


@dataclass(frozen=True)
class LaunchResult:
    unit_name: str
    started: bool
    detail: str


class TransientLauncher:
    def __init__(self, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    def launch(self, spec: LaunchSpec) -> LaunchResult:
        argv = spec.argv()
        if self.dry_run:
            return LaunchResult(spec.unit_name, False, shlex.join(argv))

        # `append:` does not create the directory; systemd fails the unit and
        # the line never starts. /data/fleet-graph/logs did not exist on the
        # first real launch, waiting directly behind the property bug above.
        try:
            spec.log_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return LaunchResult(spec.unit_name, False, f"cannot create log directory: {exc}")

        completed = subprocess.run(argv, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            return LaunchResult(
                spec.unit_name,
                False,
                f"systemd-run exited {completed.returncode}: {completed.stderr.strip()[:300]}",
            )
        return LaunchResult(spec.unit_name, True, completed.stdout.strip()[:300])


__all__ = ["DEFAULT_UNIT_PREFIX", "LaunchResult", "LaunchSpec", "TransientLauncher"]
