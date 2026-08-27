"""The stages that are scripts, kept as small as they can honestly be.

Three of dd's seven stages are plain work: write a config, run the acceptance
commands, record the merge. They do not need the ceremony the plugin path
needs -- there is no authoritative commit to protect and no agent to distrust
-- so they are written the short way: put a file in the worktree, commit it,
say what happened.

The one place that stays strict is the gate. `BoardGate` is wired when a board
is supplied and the stage simply refuses otherwise, because a default that
approved on its own would be an agent casting a human's verdict. A caller who
wants a different policy registers their own actor for the stage; that is an
explicit choice rather than a default.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fleet_graph.dd.git import run_git
from fleet_graph.dd.vendor import git_ops
from fleet_graph.graphs.dd_pipeline import (
    Dispatch,
    PipelineFault,
    Sealed,
    StageOutcome,
    StageRefused,
)

AUTHOR_NAME = "Dev Dispatch"
AUTHOR_EMAIL = "dev-dispatch@example.invalid"

RUN_CONFIG_PATH = ".dev-dispatch/run-config.json"
ACCEPTANCE_PATH = ".dd-evidence/acceptance.json"
MERGE_PATH = ".dev-dispatch/merge/result-g{generation}.json"
GATE_PATH = ".dev-dispatch/gate/decision-g{generation}.json"

PREPARED = "PREPARED"
MERGED = "MERGED"


def write_json(repo: Path, relative: str, payload: Any) -> Path:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


@dataclass
class WorkspaceSealer:
    """Commits whatever the stage left in the worktree.

    Deliberately not a second materialization protocol: the script wrote its
    files, this records them, and the receipt says which commit resulted. The
    plugin path is where attestation has to be earned.
    """

    repo: Path
    author_name: str = AUTHOR_NAME
    author_email: str = AUTHOR_EMAIL
    # The development's own durable ref. Publishing to it is not the merge --
    # it is what makes the chain a chain: the plugin sealer verifies the
    # remote head equals the commit it was handed, so a stage that seals
    # locally and never publishes severs the link before the next stage runs.
    remote_url: str = ""
    remote_ref: str = ""

    def materialize(self, stage: Any, dispatch: Dispatch, outcome: StageOutcome) -> Sealed:
        stamp = str(dispatch.get("attempt_started_at") or "")
        env = git_ops.safe_git_environment()
        if stamp:
            env["GIT_AUTHOR_DATE"] = stamp
            env["GIT_COMMITTER_DATE"] = stamp

        self._git(["add", "-A"], env)
        self._git(
            [
                "-c",
                f"user.name={self.author_name}",
                "-c",
                f"user.email={self.author_email}",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                f"dev-dispatch: {stage.id}",
            ],
            env,
        )
        commit = self._git(["rev-parse", "HEAD"], env).strip()
        if self.remote_url and self.remote_ref:
            self._git(["push", "--quiet", self.remote_url, f"HEAD:{self.remote_ref}"], env)

        receipt: dict[str, Any] = {
            "stage": stage.id,
            "input_commit": dispatch.get("input_commit", ""),
            "output_commit": commit,
        }
        declared = (outcome.receipt or {}).get("verdict")
        if declared:
            receipt["verdict"] = declared
        return Sealed(commit=commit, receipt=receipt)

    def _git(self, args: list[str], env: dict[str, str]) -> str:
        # Guarded: this runs on a worktree an agent wrote, and a repo-local
        # `core.fsmonitor` there executes on `add`. See dd/git.py.
        proc = run_git(self.repo, *args, env=env)
        if proc.returncode != 0:
            raise PipelineFault(
                f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()[:400]}"
            )
        return proc.stdout


@dataclass
class ConfigureStage:
    """Writes the run config the later stages read."""

    repo: Path
    run_config: dict[str, Any] = field(default_factory=dict)

    def act(self, stage: Any, dispatch: Dispatch) -> StageOutcome:
        write_json(
            self.repo,
            RUN_CONFIG_PATH,
            {
                "development_id": dispatch.get("development_id", ""),
                "generation": dispatch.get("generation", 1),
                **self.run_config,
            },
        )
        return StageOutcome(produced=tuple(stage.produced_artifacts))


@dataclass
class AcceptanceStage:
    """Runs the declared acceptance commands and records what they did.

    A failing command refuses the pipeline rather than faulting it: the run
    happened, the answer was no. The contract declares no failure edge out of
    acceptance, and inventing one to express "the tests failed" would be
    reading meaning the contract does not carry.

    **The operator's declaration is the authority, not the file in the
    worktree.** The implementer's role grants it `write: [worktree_path]`, so
    it can rewrite `run-config.json` -- the very commands that decide whether
    its work passed. Measured: the same repo with a failing test went from
    refused to `passed: true` once the acceptance command was replaced with
    `true`. The thing being graded must not be able to edit the exam.

    The committed file is still read, because a disagreement between it and
    the declaration is worth refusing loudly rather than quietly overriding.
    """

    repo: Path
    # What the operator asked for. Compared against the worktree's copy; the
    # comparison is what makes tampering visible.
    declared: list[list[str]] = field(default_factory=list)
    # Setup runs before the acceptance commands, in the same environment: the
    # R1-c environment/contract exit exists so a missing `node_modules` (the
    # m-8f70cc shape) is fixed by declaring setup, not by editing the spec.
    setup: list[list[str]] = field(default_factory=list)
    # Environment overlaid on both setup and acceptance commands. Same
    # authority rule as the commands: the declaration wins, the tree's copy is
    # only checked against it.
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 1800

    def commands(self) -> list[list[str]]:
        path = self.repo / RUN_CONFIG_PATH
        if not path.is_file():
            raise StageRefused(
                f"{RUN_CONFIG_PATH} is missing; configure did not run",
                code="RUN_CONFIG_MISSING",
            )
        config = json.loads(path.read_text(encoding="utf-8"))
        for key, wanted_value in (
            ("acceptance_commands", [list(c) for c in self.declared if c]),
            ("setup_commands", [list(c) for c in self.setup if c]),
            ("acceptance_env", dict(self.env)),
        ):
            in_tree = config.get(key) or ([] if key != "acceptance_env" else {})
            if key != "acceptance_env":
                in_tree = [list(command) for command in in_tree if command]
            if in_tree != wanted_value:
                raise StageRefused(
                    f"{RUN_CONFIG_PATH} declares {key}={in_tree!r} but this run was "
                    f"configured with {wanted_value!r}; refusing to accept against "
                    f"a {key} nobody declared",
                    code="ACCEPTANCE_DECLARATION_MISMATCH",
                )
        return [list(c) for c in self.declared if c]

    def _run(self, command: list[str]) -> dict[str, Any]:
        import os

        proc = subprocess.run(
            command,
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            env={**os.environ, **self.env} if self.env else None,
        )
        return {
            "command": command,
            "exit_code": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-2000:],
            "stderr_tail": (proc.stderr or "")[-2000:],
        }

    def act(self, stage: Any, dispatch: Dispatch) -> StageOutcome:
        declared = self.commands()

        setup_results = []
        for command in [list(c) for c in self.setup if c]:
            entry = self._run(command)
            setup_results.append(entry)
            if entry["exit_code"] != 0:
                # A failing setup is an environment fact, not a verdict on the
                # work: recorded under its own code so the control plane's
                # failure exits can tell it from a failing test.
                write_json(
                    self.repo,
                    ACCEPTANCE_PATH,
                    {
                        "development_id": dispatch.get("development_id", ""),
                        "attempt": dispatch.get("attempt", 1),
                        "passed": False,
                        "setup_results": setup_results,
                        "results": [],
                    },
                )
                raise StageRefused(f"setup failed: {command}", code="SETUP_FAILED")

        results = []
        failed = []
        for command in declared:
            entry = self._run(command)
            results.append(entry)
            if entry["exit_code"] != 0:
                failed.append(command)

        write_json(
            self.repo,
            ACCEPTANCE_PATH,
            {
                "development_id": dispatch.get("development_id", ""),
                "attempt": dispatch.get("attempt", 1),
                "passed": not failed,
                **({"setup_results": setup_results} if setup_results else {}),
                "results": results,
            },
        )
        if failed:
            raise StageRefused(f"acceptance failed: {failed}", code="ACCEPTANCE_FAILED")
        return StageOutcome(produced=tuple(stage.produced_artifacts))


@dataclass
class MergeStage:
    """Records the merge, and performs it only when told to.

    `PREPARED` is a first-class result in the artifact contract, not a fudge:
    the work is sealed and ready and the publish is a separate decision. The
    default stays there because pushing to a durable ref is the one thing on
    this path that is not undoable, and it should be opted into.
    """

    repo: Path
    remote_url: str
    target_ref: str
    publish: bool = False

    def act(self, stage: Any, dispatch: Dispatch) -> StageOutcome:
        result = MERGED if self.publish else PREPARED
        detail: dict[str, Any] = {}
        if self.publish:
            detail = self._fast_forward(dispatch)

        write_json(
            self.repo,
            MERGE_PATH.format(generation=dispatch.get("generation", 1)),
            {
                "development_id": dispatch.get("development_id", ""),
                "result": result,
                "subject_commit": dispatch.get("input_commit", ""),
                "target_ref": self.target_ref,
                **detail,
            },
        )
        return StageOutcome(produced=tuple(stage.produced_artifacts))

    def _fast_forward(self, dispatch: Dispatch) -> dict[str, Any]:
        subject = str(dispatch.get("input_commit", ""))
        try:
            observed = git_ops.resolve_remote_ref(self.remote_url, self.target_ref)
            git_ops.cas_fast_forward_target(
                workspace_path=str(self.repo),
                remote_url=self.remote_url,
                target_ref=self.target_ref,
                expected_target_head_commit=observed,
                handoff_commit=subject,
            )
        except git_ops.ExactWorkspaceError as exc:
            raise StageRefused(f"merge refused: {exc}", code="MERGE_REFUSED") from exc
        return {"previous_target_head": observed}


__all__ = [
    "ACCEPTANCE_PATH",
    "GATE_PATH",
    "MERGED",
    "MERGE_PATH",
    "PREPARED",
    "RUN_CONFIG_PATH",
    "AcceptanceStage",
    "ConfigureStage",
    "MergeStage",
    "WorkspaceSealer",
    "write_json",
]
