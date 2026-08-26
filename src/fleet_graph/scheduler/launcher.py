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

import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_UNIT_PREFIX = "fleet-graph-line"


@dataclass(frozen=True)
class LaunchSpec:
    folder_id: str
    seat: str
    generation: int = 1
    max_rounds: int = 10
    run_root: Path | None = None
    log_path: Path | None = None
    unit_prefix: str = DEFAULT_UNIT_PREFIX
    working_directory: str = "/data/apps/fleet-graph/current"
    executable: str = "/data/apps/fleet-graph/current/.venv/bin/fleet-graph"
    environment: dict[str, str] = field(default_factory=dict)

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
