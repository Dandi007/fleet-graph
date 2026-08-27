"""The in-process control plane behind the dev-dispatch MCP surface.

The supervision plane struck the separate graph-API tier (:5611): the MCP
service *is* the control plane. This module is what the service calls -- no
HTTP hop, no second engine. Its state model is the one the user ruled for R1
(wf-13ff9e plan.md §1 R1-a):

    state = the git ancestry chain (authoritative commits + receipts)
          + the durable checkpoint (in-flight graph state)
          + the run artifacts (events, results, launches)

There is deliberately **no database**. `status.json` under a development's
directory is a *rebuildable cache* for list/get fast paths: `rebuild_status`
recomputes it wholesale from the sources above, and a test proves the rebuilt
copy equals the cached one, so losing the file loses nothing.

**Admission is server-side derivation** (R1-b). `create` takes exactly a repo
path, a target base, and the spec -- everything else (development id, H0
handoff, root digest, bootstrap commit, durable ref, acceptance argv) is
derived here and written down where it can be independently re-derived:

- the development id is a digest over (repo, spec digest, target base), so
  `create` is idempotent by construction;
- the H0 handoff is canonical JSON whose digest seeds the receipt chain, and
  both the object and its digest are recomputable from the record;
- the target base is committed by bootstrap and read back by the run through
  `committed_target_base` (the §25 lesson: the two commands must compose on
  their defaults, with the introducing commit as the tamper anchor);
- the acceptance argv is read out of the **spec itself** (a ```dd-acceptance
  fenced block), because the spec is frozen and digest-bound at bootstrap --
  the graded party cannot edit the exam after admission.

**Starting is a transient systemd unit** (same isolation argument as
scheduler/launcher.py): the control plane can restart without killing runs in
flight. The thread identity is derived from the development id alone
(`{development_id}:g1` via DevelopmentConfig), and the checkpoint lives at a
path derived from the development id, so a kill-restart re-enters the same
thread and re-adopts the agent runs in flight.

**The gate carries no verdict.** `gate` reports the pending question note and
offers `resume`, which re-enters the suspended thread with no input at all --
the graph re-reads the board itself. Verdicts travel only as `work.decision.v1`
on the bus, published by a human; this module has no way to publish one.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fleet_graph.dd.bootstrap import (
    DEVELOPMENT_PATH,
    IdentityChanged,
    build_attempt_context,
    canonical_bytes,
    committed_target_base,
    digest_of,
)
from fleet_graph.dd.git import run_git
from fleet_graph.dd.upstream_constants import ATTEMPT_CONTEXT_CONTRACT_VERSION
from fleet_graph.graphs.dd_runner import EVENTS_FILE, RESULT_FILE
from fleet_graph.state.run_artifacts import iso, write_json_durable

DEFAULT_DD_ROOT = Path("/data/fleet-graph/dd")
DEFAULT_PLUGIN_BINDING = Path("/data/fleet-graph/dd/plugin-binding.json")
DEFAULT_WORKING_DIRECTORY = "/data/apps/fleet-graph/current"
DEFAULT_EXECUTABLE = "/data/apps/fleet-graph/current/.venv/bin/fleet-graph"
#: Fail-closed admission whitelist: a repo outside these roots is refused.
#: /tmp is admitted for throwaway acceptance repos (the §24 precedent).
DEFAULT_WORKTREE_ROOTS: tuple[str, ...] = ("/data/worktrees", "/tmp")

RECORD_FILE = "record.json"
STATUS_FILE = "status.json"
H0_FILE = "h0-handoff.json"
LAUNCHES_FILE = "launches.jsonl"
CHECKPOINT_FILE = "checkpoint.sqlite3"
LOG_FILE = "dd.log"

UNIT_PREFIX = "fleet-graph-dd"

GATE_DECISION_PATH = ".dev-dispatch/gate/decision-g1.json"
MERGE_RESULT_PATH = ".dev-dispatch/merge/result-g1.json"
ACCEPTANCE_RECORD_PATH = ".dd-evidence/acceptance.json"

#: The spec's own acceptance declaration: one argv per non-empty line inside a
#: ```dd-acceptance fenced block. Declared in the spec so it is frozen and
#: digest-bound at bootstrap; multiple blocks concatenate in order.
ACCEPTANCE_FENCE = re.compile(r"^```dd-acceptance[ \t]*\n(.*?)^```[ \t]*$", re.M | re.S)

STATE_CREATED = "created"
STATE_RUNNING = "running"
STATE_AWAITING_GATE = "awaiting_gate"
STATE_INTERRUPTED = "interrupted"
# Terminal states are the pipeline's own vocabulary, passed through:
# complete / failed / refused / bounds / fault.

_HEX40 = re.compile(r"^[0-9a-f]{40}$")


class ControlPlaneError(RuntimeError):
    """A refusal with one cause per code, and an honest retryability bit."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = message
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.detail, "retryable": self.retryable}


def derive_development_id(repo: Path, spec_digest: str, target_base_commit: str) -> str:
    """Deterministic, so `create` is idempotent: same admission, same identity."""
    seed = f"{repo}\x1f{spec_digest}\x1f{target_base_commit}".encode()
    return "dev-fg-" + hashlib.sha256(seed).hexdigest()[:12]


def derive_acceptance_commands(spec: bytes) -> list[list[str]]:
    """The argv lists the spec itself declares. Absent block means zero commands."""
    commands: list[list[str]] = []
    for block in ACCEPTANCE_FENCE.findall(spec.decode("utf-8", errors="replace")):
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                argv = shlex.split(line)
            except ValueError as exc:
                raise ControlPlaneError(
                    "ACCEPTANCE_DECLARATION_INVALID",
                    f"cannot parse acceptance line {line!r}: {exc}",
                ) from exc
            if argv:
                commands.append(argv)
    return commands


def build_h0_handoff(
    *, development_id: str, spec_digest: str, target_base_commit: str, remote_url: str
) -> dict[str, Any]:
    """The chain-root handoff. Canonical JSON; its digest seeds the receipt chain.

    Every field is anchored elsewhere (the spec digest and target base in the
    bootstrap commit, the remote in the record), so the digest is
    independently recomputable -- nothing here is invented at read time.
    """
    return {
        "contract_version": ATTEMPT_CONTEXT_CONTRACT_VERSION,
        "development_id": development_id,
        "kind": "root-handoff",
        "remote_url": remote_url,
        "spec_digest": spec_digest,
        "target_base_commit": target_base_commit,
    }


def _inherited_environment() -> dict[str, str]:
    """What a launched run inherits from the control plane's own environment.

    - PATH: transient units start from the user manager's environment, not
      this process's, and agent-run is a `#!/usr/bin/env bun` script -- the
      same lesson scheduler/daemon.py:line_environment already carries
      (measured again here: `env: 'bun': No such file or directory`).
    - FLEET_GRAPH_BUS_TOKEN_FILE: the gate needs a credential, and a *path*
      in a transient unit's argv points at a 0600 file rather than being one.
      A raw FLEET_GRAPH_BUS_TOKEN value is deliberately never forwarded:
      `--setenv` travels through argv, and a token in argv is a token in
      `/proc`. Production runs on the token file (findings §26).
    """
    import os

    env = {"PATH": os.environ.get("PATH", "")}
    token_file = os.environ.get("FLEET_GRAPH_BUS_TOKEN_FILE")
    if token_file:
        env["FLEET_GRAPH_BUS_TOKEN_FILE"] = token_file
    return env


def _systemd_unit_is_active(unit: str) -> bool:
    proc = subprocess.run(
        ["systemctl", "--user", "is-active", unit], capture_output=True, text=True, check=False
    )
    return proc.stdout.strip() in {"active", "activating"}


@dataclass(frozen=True)
class DdLaunchSpec:
    """The argv for one detached `dd run`, shaped for TransientLauncher.

    Duck-typed against scheduler/launcher.py's LaunchSpec: the launcher only
    reads `argv()`, `log_file` and `unit_name`. The credential discipline is
    the launcher's too -- only the token *file path* crosses into the unit's
    environment, never a token value, and never through argv.
    """

    development_id: str
    dev_root: Path
    workspace: Path
    plugin_binding: Path
    remote_url: str
    remote_ref: str
    root_digest: str
    acceptance_commands: list[list[str]] = field(default_factory=list)
    board_card: str = ""
    resume: bool = False
    launch_seq: int = 1
    #: Server-side policy, not client vocabulary: per-stage model overrides
    #: (the roles' own selectors stay the default). The §24 precedent runs
    #: review stages on deepseek-v4-pro.
    stage_models: dict[str, str] = field(default_factory=dict)
    working_directory: str = DEFAULT_WORKING_DIRECTORY
    executable: str = DEFAULT_EXECUTABLE
    environment: dict[str, str] = field(default_factory=dict)

    @property
    def unit_name(self) -> str:
        """The sequence keeps a relaunch from colliding with a unit systemd is
        still tearing down; the thread identity underneath stays derived from
        the development id alone."""
        return f"{UNIT_PREFIX}-{self.development_id}-r{self.launch_seq}"

    @property
    def log_file(self) -> Path:
        return self.dev_root / LOG_FILE

    def argv(self) -> list[str]:
        argv = [
            "systemd-run",
            "--user",
            "--collect",
            "--unit",
            self.unit_name,
            f"--working-directory={self.working_directory}",
        ]
        for key, value in sorted(self.environment.items()):
            argv += [f"--setenv={key}={value}"]
        argv += [
            f"--property=StandardOutput=append:{self.log_file}",
            f"--property=StandardError=append:{self.log_file}",
            self.executable,
            "dd",
            "run",
            "--development",
            self.development_id,
            "--workspace",
            str(self.workspace),
            "--plugin-binding",
            str(self.plugin_binding),
            "--remote-url",
            self.remote_url,
            "--remote-ref",
            self.remote_ref,
            "--root-digest",
            self.root_digest,
            "--run-root",
            str(self.dev_root),
            # On disk and derived from the development id: the contract that
            # makes a kill-restart re-enter the same thread instead of
            # re-dispatching sealed stages.
            "--checkpoint",
            str(self.dev_root / CHECKPOINT_FILE),
            # The durable MR is the goal; the merge stage still runs only
            # after the gate lets it.
            "--publish-merge",
        ]
        for command in self.acceptance_commands:
            argv += ["--accept", shlex.join(command)]
        for stage, model in sorted(self.stage_models.items()):
            argv += ["--stage-model", f"{stage}={model}"]
        if self.board_card:
            argv += ["--board-card", self.board_card]
        if self.resume:
            # Deliberately valueless: the gate re-reads the board on resume,
            # so whoever relaunches the unit cannot cast the verdict by it.
            argv += ["--resume"]
        return argv


class DdControlPlane:
    """Admission, launch, and read-side assembly for dd developments."""

    def __init__(
        self,
        *,
        root: Path = DEFAULT_DD_ROOT,
        plugin_binding: Path = DEFAULT_PLUGIN_BINDING,
        worktree_roots: tuple[str, ...] = DEFAULT_WORKTREE_ROOTS,
        working_directory: str = DEFAULT_WORKING_DIRECTORY,
        executable: str = DEFAULT_EXECUTABLE,
        launcher: Any = None,
        unit_probe: Callable[[str], bool] = _systemd_unit_is_active,
        board_factory: Callable[[], Any] | None = None,
        environment: dict[str, str] | None = None,
        stage_models: dict[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        from fleet_graph.scheduler.launcher import TransientLauncher

        self.root = Path(root)
        self.plugin_binding = Path(plugin_binding)
        self.worktree_roots = tuple(worktree_roots)
        self.working_directory = working_directory
        self.executable = executable
        self.launcher = launcher if launcher is not None else TransientLauncher()
        self.unit_probe = unit_probe
        self._board_factory = board_factory if board_factory is not None else self._default_board
        self.environment = (
            dict(environment) if environment is not None else _inherited_environment()
        )
        self.stage_models = dict(stage_models or {})
        self.clock = clock

    # --- admission -------------------------------------------------------

    def create(
        self,
        repo_path: str,
        target_base: str | None = None,
        spec_text: str | None = None,
        spec_path: str | None = None,
    ) -> dict[str, Any]:
        """Admit one development: derive everything, bootstrap, record.

        Idempotent: the same (repo, spec, base) admission returns the same
        development, with `already_admitted` set instead of a second identity.
        """
        repo = self._admit_repo(repo_path)
        spec = self._read_spec(spec_text, spec_path)
        base = self._default_target_base(repo, target_base)
        spec_digest = digest_of(spec)
        development_id = derive_development_id(repo, spec_digest, base)
        dev_root = self.root / development_id

        existing = self._read_record_if_any(development_id)
        if existing is not None:
            if existing.get("spec_digest") != spec_digest or existing.get("repo_path") != str(repo):
                raise ControlPlaneError(
                    "ADMISSION_RECORD_MISMATCH",
                    f"{development_id} already admitted with a different spec or repo; "
                    "a changed spec is a new development in a fresh worktree",
                )
            if not existing.get("card_entity_id"):
                # The bus was down (or refused) at first admission; the card
                # publish is idempotency-keyed, so healing it here cannot fork.
                card = self._publish_card(
                    development_id, repo, str(existing.get("remote_ref") or "")
                )
                if card:
                    existing["card_entity_id"] = card
                    write_json_durable(dev_root / RECORD_FILE, existing)
            return self._creation_result(existing, already_admitted=True)

        self._refuse_foreign_binding(repo, development_id)
        remote_url = self._origin_url(repo)
        remote_ref = f"refs/heads/dd/{development_id}"
        acceptance_commands = derive_acceptance_commands(spec)

        bootstrap_commit = self._bootstrap(repo, development_id, spec, base)

        h0 = build_h0_handoff(
            development_id=development_id,
            spec_digest=spec_digest,
            target_base_commit=base,
            remote_url=remote_url,
        )
        h0_bytes = canonical_bytes(h0)
        root_handoff_digest = digest_of(h0_bytes)

        dev_root.mkdir(parents=True, exist_ok=True)
        (dev_root / H0_FILE).write_bytes(h0_bytes)

        card_entity_id = self._publish_card(development_id, repo, remote_ref)

        record = {
            "contract_version": ATTEMPT_CONTEXT_CONTRACT_VERSION,
            "development_id": development_id,
            "repo_path": str(repo),
            "remote_url": remote_url,
            "remote_ref": remote_ref,
            "target_base_commit": base,
            "spec_digest": spec_digest,
            "spec_size_bytes": len(spec),
            "bootstrap_commit": bootstrap_commit,
            "root_handoff_digest": root_handoff_digest,
            "acceptance_commands": acceptance_commands,
            "card_entity_id": card_entity_id,
            "plugin_binding_path": str(self.plugin_binding),
            "created_at": iso(self.clock()),
        }
        write_json_durable(dev_root / RECORD_FILE, record)
        self.rebuild_status(development_id)
        return self._creation_result(record, already_admitted=False)

    def _creation_result(self, record: dict[str, Any], *, already_admitted: bool) -> dict[str, Any]:
        return {
            "development_id": record["development_id"],
            "already_admitted": already_admitted,
            "bootstrap": {
                "commit": record["bootstrap_commit"],
                "spec_digest": record["spec_digest"],
                "target_base_commit": record["target_base_commit"],
                "root_handoff_digest": record["root_handoff_digest"],
            },
            "remote": {"url": record["remote_url"], "ref": record["remote_ref"]},
            "acceptance_commands": record["acceptance_commands"],
            "card_entity_id": record["card_entity_id"],
            "gate_enabled": bool(record["card_entity_id"]),
        }

    def _admit_repo(self, repo_path: str) -> Path:
        if not repo_path or not str(repo_path).startswith("/"):
            raise ControlPlaneError(
                "REPO_PATH_INVALID", f"repo_path must be absolute: {repo_path!r}"
            )
        repo = Path(repo_path).resolve()
        if not any(
            repo == Path(root) or Path(root) in repo.parents for root in self.worktree_roots
        ):
            # Fail closed: the whitelist is the safety piece, not a convenience.
            raise ControlPlaneError(
                "WORKTREE_ROOT_NOT_ALLOWED",
                f"{repo} is outside the admitted worktree roots {list(self.worktree_roots)}",
            )
        if not repo.is_dir():
            raise ControlPlaneError("REPO_NOT_FOUND", f"{repo} is not a directory")
        inside = run_git(repo, "rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            raise ControlPlaneError(
                "REPO_NOT_A_GIT_WORKTREE", f"{repo} is not inside a git work tree"
            )
        top = run_git(repo, "rev-parse", "--show-toplevel", check=True).stdout.strip()
        if Path(top) != repo:
            raise ControlPlaneError(
                "REPO_NOT_A_WORKTREE_ROOT", f"{repo} is not the top of its work tree ({top})"
            )
        dirty = run_git(repo, "status", "--porcelain", check=True).stdout.strip()
        if dirty:
            raise ControlPlaneError(
                "WORKTREE_DIRTY",
                f"{repo} has uncommitted changes; admission freezes committed state only",
            )
        return repo

    def _read_spec(self, spec_text: str | None, spec_path: str | None) -> bytes:
        if bool(spec_text) == bool(spec_path):
            raise ControlPlaneError(
                "SPEC_INPUT_INVALID", "pass exactly one of spec_text or spec_path"
            )
        if spec_text is not None:
            spec = spec_text.encode("utf-8")
        else:
            try:
                spec = Path(str(spec_path)).read_bytes()
            except OSError as exc:
                raise ControlPlaneError(
                    "SPEC_PATH_UNREADABLE", f"cannot read spec at {spec_path}: {exc}"
                ) from exc
        if not spec.strip():
            raise ControlPlaneError("SPEC_EMPTY", "the approved spec is empty")
        return spec

    def _default_target_base(self, repo: Path, target_base: str | None) -> str:
        """Explicit base -> committed identity -> HEAD, in that order.

        The §25 composition lesson at admission level: after a bootstrap
        commit, HEAD has moved past the base the spec was approved against.
        A re-admission that defaulted to HEAD would derive a *different*
        development id for the same admission, so the committed identity --
        tamper-anchored by `committed_target_base` -- wins over HEAD.
        """
        if target_base:
            return self._resolve_target_base(repo, target_base)
        try:
            committed = committed_target_base(repo)
        except IdentityChanged as changed:
            raise ControlPlaneError("IDENTITY_EDITED", str(changed)) from changed
        return committed or self._resolve_target_base(repo, None)

    def _resolve_target_base(self, repo: Path, target_base: str | None) -> str:
        ref = target_base or "HEAD"
        resolved = run_git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
        if resolved.returncode != 0:
            raise ControlPlaneError(
                "TARGET_BASE_UNRESOLVED",
                f"cannot resolve {ref!r} in {repo}: {resolved.stderr.strip()[:200]}",
            )
        commit = resolved.stdout.strip()
        if not _HEX40.fullmatch(commit):
            raise ControlPlaneError("TARGET_BASE_UNRESOLVED", f"{ref!r} resolved to {commit!r}")
        return commit

    def _refuse_foreign_binding(self, repo: Path, development_id: str) -> None:
        committed = run_git(repo, "show", f"HEAD:{DEVELOPMENT_PATH}")
        if committed.returncode != 0:
            return
        try:
            bound = str(json.loads(committed.stdout).get("development_id") or "")
        except ValueError:
            bound = ""
        if bound and bound != development_id:
            raise ControlPlaneError(
                "REPO_BOUND_TO_OTHER_DEVELOPMENT",
                f"{repo} already carries the identity of {bound}; "
                "one worktree serves one development",
            )

    def _origin_url(self, repo: Path) -> str:
        origin = run_git(repo, "remote", "get-url", "origin")
        if origin.returncode != 0 or not origin.stdout.strip():
            raise ControlPlaneError(
                "REPO_HAS_NO_ORIGIN",
                f"{repo} has no `origin` remote; the durable ref needs somewhere to live",
            )
        return origin.stdout.strip()

    def _bootstrap(self, repo: Path, development_id: str, spec: bytes, base: str) -> str:
        """Write and commit the attempt context, unless it is already committed."""
        try:
            committed = committed_target_base(repo)
        except IdentityChanged as changed:
            raise ControlPlaneError("IDENTITY_EDITED", str(changed)) from changed
        if committed is not None:
            # Bootstrap already happened for this development (idempotent
            # re-admission after a lost record); the anchor commit stands.
            if committed != base:
                raise ControlPlaneError(
                    "TARGET_BASE_CONFLICT",
                    f"the committed identity freezes base {committed[:12]}, "
                    f"admission asked for {base[:12]}",
                )
            return self._introducing_commit(repo)

        context = build_attempt_context(
            development_id=development_id, spec=spec, target_base_commit=base
        )
        context.write(repo)
        for args in (
            ("add", "--", ".dev-dispatch"),
            (
                "-c",
                "user.name=Dev Dispatch",
                "-c",
                "user.email=dev-dispatch@example.invalid",
                "commit",
                "-q",
                "-m",
                f"dev-dispatch: bootstrap {development_id}",
            ),
        ):
            proc = run_git(repo, *args)
            if proc.returncode != 0:
                raise ControlPlaneError(
                    "BOOTSTRAP_COMMIT_FAILED",
                    f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()[:300]}",
                    retryable=True,
                )
        return run_git(repo, "rev-parse", "HEAD", check=True).stdout.strip()

    def _introducing_commit(self, repo: Path) -> str:
        history = run_git(
            repo, "log", "--diff-filter=A", "--format=%H", "--", DEVELOPMENT_PATH, check=True
        )
        introduced = [line for line in history.stdout.split() if line]
        if not introduced:
            raise ControlPlaneError(
                "BOOTSTRAP_ANCHOR_MISSING", f"{DEVELOPMENT_PATH} has no introducing commit"
            )
        return introduced[-1]

    def _default_board(self) -> Any:
        try:
            from fleet_graph.bus.board import Board
            from fleet_graph.bus.client import BusClient

            return Board(BusClient())
        except Exception:
            # No credential, no bus: admission still works, the gate is then
            # disabled and says so, rather than half-wired.
            return None

    def _publish_card(self, development_id: str, repo: Path, remote_ref: str) -> str:
        board = self._board_factory()
        if board is None:
            return ""
        try:
            # The exact work.card.v1 schema the board enforces: title/status/
            # intent required, additionalProperties false (measured 2026-08-27).
            result = board.publish_card(
                {
                    "title": f"dd {development_id}",
                    "status": "doing",
                    "intent": f"dev-dispatch development in {repo}",
                    "development_id": development_id,
                    "links": [remote_ref],
                },
                idempotency_key=f"dd-card:{development_id}",
            )
        except Exception:
            # Best-effort: admission must survive a downed bus. The gate then
            # stays disabled and the result says so; a later create heals it.
            return ""
        return result.entity_id

    # --- records and status ----------------------------------------------

    def _dev_root(self, development_id: str) -> Path:
        return self.root / development_id

    def _read_record_if_any(self, development_id: str) -> dict[str, Any] | None:
        path = self._dev_root(development_id) / RECORD_FILE
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _record(self, development_id: str) -> dict[str, Any]:
        record = self._read_record_if_any(development_id)
        if record is None:
            raise ControlPlaneError(
                "DEVELOPMENT_NOT_FOUND", f"no admission record for {development_id}"
            )
        return record

    def _launches(self, development_id: str) -> list[dict[str, Any]]:
        path = self._dev_root(development_id) / LAUNCHES_FILE
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def _read_result(self, development_id: str) -> dict[str, Any] | None:
        path = self._dev_root(development_id) / RESULT_FILE
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None

    def _unit_active(self, development_id: str) -> str | None:
        launches = self._launches(development_id)
        if not launches:
            return None
        unit = str(launches[-1].get("unit") or "")
        if unit and self.unit_probe(unit):
            return unit
        return None

    def _checkpoint_state(self, development_id: str) -> dict[str, Any] | None:
        """The latest durable graph state for this development's thread."""
        path = self._dev_root(development_id) / CHECKPOINT_FILE
        if not path.is_file():
            return None
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver

            with SqliteSaver.from_conn_string(str(path)) as saver:
                found = saver.get_tuple({"configurable": {"thread_id": f"{development_id}:g1"}})
        except Exception:
            return None
        if found is None:
            return None
        values = found.checkpoint.get("channel_values") or {}
        return dict(values) if isinstance(values, dict) else None

    def rebuild_status(self, development_id: str) -> dict[str, Any]:
        """Recompute the status cache from git + checkpoint + run artifacts.

        This is the proof the cache is a cache: everything in `status.json`
        comes from here, and nothing reads the file except the list fast path.
        """
        self._record(development_id)  # refuses unknown ids before anything else
        result = self._read_result(development_id)
        active_unit = self._unit_active(development_id)
        checkpoint = self._dev_root(development_id) / CHECKPOINT_FILE

        stage = ""
        terminal = ""
        terminal_reason = ""
        head_commit = ""
        awaiting: dict[str, Any] | None = None
        if result is not None:
            stage = str(result.get("stage") or "")
            terminal = str(result.get("terminal") or "")
            terminal_reason = str(result.get("terminal_reason") or "")
            head_commit = str(result.get("head_commit") or "")
            raw_awaiting = result.get("awaiting")
            awaiting = dict(raw_awaiting) if isinstance(raw_awaiting, dict) else None

        if active_unit:
            state = STATE_RUNNING
        elif awaiting:
            state = STATE_AWAITING_GATE
        elif terminal:
            state = terminal
        elif checkpoint.is_file():
            # A durable thread with no result and no unit: killed mid-run.
            state = STATE_INTERRUPTED
            values = self._checkpoint_state(development_id) or {}
            stage = str(values.get("stage") or stage)
            head_commit = str(values.get("head_commit") or head_commit)
        else:
            state = STATE_CREATED

        status = {
            "development_id": development_id,
            "state": state,
            "stage": stage,
            "terminal": terminal,
            "terminal_reason": terminal_reason,
            "head_commit": head_commit,
            "awaiting": awaiting,
            "active_unit": active_unit or "",
            "launches": len(self._launches(development_id)),
        }
        write_json_durable(self._dev_root(development_id) / STATUS_FILE, status)
        return status

    # --- start / gate ----------------------------------------------------

    def start(self, development_id: str) -> dict[str, Any]:
        """Launch the development detached, resuming its thread if one exists."""
        record = self._record(development_id)
        active = self._unit_active(development_id)
        if active:
            return {
                "development_id": development_id,
                "started": False,
                "already_running": True,
                "unit": active,
            }
        resume = (self._dev_root(development_id) / CHECKPOINT_FILE).is_file()
        return self._launch(record, resume=resume)

    def _launch(self, record: dict[str, Any], *, resume: bool) -> dict[str, Any]:
        development_id = str(record["development_id"])
        dev_root = self._dev_root(development_id)
        if not self.plugin_binding.is_file():
            raise ControlPlaneError(
                "PLUGIN_BINDING_UNREADABLE",
                f"no plugin binding at {self.plugin_binding}; the capability "
                "check is fail-closed and will not be skipped",
            )
        seq = len(self._launches(development_id)) + 1
        spec = DdLaunchSpec(
            development_id=development_id,
            dev_root=dev_root,
            workspace=Path(str(record["repo_path"])),
            plugin_binding=Path(str(record["plugin_binding_path"])),
            remote_url=str(record["remote_url"]),
            remote_ref=str(record["remote_ref"]),
            root_digest=str(record["root_handoff_digest"]),
            acceptance_commands=[list(c) for c in record.get("acceptance_commands") or []],
            board_card=str(record.get("card_entity_id") or ""),
            resume=resume,
            launch_seq=seq,
            stage_models=dict(self.stage_models),
            working_directory=self.working_directory,
            executable=self.executable,
            environment=dict(self.environment),
        )
        launched = self.launcher.launch(spec)
        entry = {
            "seq": seq,
            "unit": spec.unit_name,
            "mode": "resume" if resume else "fresh",
            "at": iso(self.clock()),
            "started": launched.started,
            "detail": launched.detail,
            "argv": spec.argv(),
        }
        with (dev_root / LAUNCHES_FILE).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if not launched.started and not getattr(self.launcher, "dry_run", False):
            raise ControlPlaneError(
                "LAUNCH_FAILED", f"{spec.unit_name}: {launched.detail}", retryable=True
            )
        self.rebuild_status(development_id)
        return {
            "development_id": development_id,
            "started": launched.started,
            "already_running": False,
            "unit": spec.unit_name,
            "mode": entry["mode"],
            "thread_id": f"{development_id}:g1",
            "checkpoint": str(dev_root / CHECKPOINT_FILE),
        }

    def gate(self, development_id: str, resume: bool = False) -> dict[str, Any]:
        """The gate's state, and -- on request -- a valueless resume.

        There is deliberately no decision input anywhere on this path.
        Verdicts travel only as `work.decision.v1` on the board, published by
        a human; on resume the graph re-reads the board itself.
        """
        record = self._record(development_id)
        status = self.rebuild_status(development_id)
        awaiting = status.get("awaiting") or None

        decision = self._committed_gate_decision(record)
        gate_report: dict[str, Any] = {
            "development_id": development_id,
            "state": status["state"],
            "pending": bool(awaiting) and decision is None,
            "awaiting": awaiting,
            "decision": decision,
            "ruling": "decisions travel only as work.decision.v1 on the board; "
            "this tool carries none",
        }
        if awaiting and decision is None:
            gate_report["decision_on_board"] = self._decision_on_board(awaiting)
        if not resume:
            return gate_report

        if status["state"] == STATE_RUNNING:
            raise ControlPlaneError(
                "ALREADY_RUNNING", f"{development_id} is running as {status['active_unit']}"
            )
        if not (self._dev_root(development_id) / CHECKPOINT_FILE).is_file():
            raise ControlPlaneError(
                "CHECKPOINT_MISSING",
                f"{development_id} has no durable checkpoint; there is no thread to resume",
            )
        gate_report["resume"] = self._launch(record, resume=True)
        return gate_report

    def _committed_gate_decision(self, record: dict[str, Any]) -> dict[str, Any] | None:
        found = run_git(Path(str(record["repo_path"])), "show", f"HEAD:{GATE_DECISION_PATH}")
        if found.returncode != 0:
            return None
        try:
            return dict(json.loads(found.stdout))
        except ValueError:
            return None

    def _decision_on_board(self, awaiting: dict[str, Any]) -> bool | None:
        board = self._board_factory()
        if board is None:
            return None
        try:
            from fleet_graph.bus.board import GateTicket

            ticket = GateTicket.from_dict(
                {
                    "question_note_id": str(awaiting.get("question_note_id") or ""),
                    "card_entity_id": str(awaiting.get("card_entity_id") or ""),
                }
            )
            return board.decision_for(ticket) is not None
        except Exception:
            return None

    # --- read side -------------------------------------------------------

    def get(self, development_id: str) -> dict[str, Any]:
        record = self._record(development_id)
        status = self.rebuild_status(development_id)
        return {
            **status,
            "repo_path": record["repo_path"],
            "worktree_path": record["repo_path"],
            "remote_url": record["remote_url"],
            "remote_ref": record["remote_ref"],
            "target_base_commit": record["target_base_commit"],
            "spec_digest": record["spec_digest"],
            "bootstrap_commit": record["bootstrap_commit"],
            "root_handoff_digest": record["root_handoff_digest"],
            "acceptance_commands": record["acceptance_commands"],
            "card_entity_id": record.get("card_entity_id", ""),
            "created_at": record.get("created_at", ""),
        }

    def list(
        self, state: str | None = None, limit: int = 20, cursor: str | None = None
    ) -> dict[str, Any]:
        """O(n) over the development directories -- the ruled-on trade."""
        ids = (
            sorted(
                entry.name
                for entry in self.root.iterdir()
                if entry.is_dir() and (entry / RECORD_FILE).is_file()
            )
            if self.root.is_dir()
            else []
        )
        if cursor:
            ids = [name for name in ids if name > cursor]
        rows: list[dict[str, Any]] = []
        next_cursor = None
        for name in ids:
            status_path = self._dev_root(name) / STATUS_FILE
            status: dict[str, Any] | None = None
            if status_path.is_file():
                try:
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                except ValueError:
                    status = None
            # A terminal state is immutable, so its cache is trustworthy; a
            # cached "running"/"created" row can be stale the moment the unit
            # exits (measured: a failed run listed as running), so anything
            # non-terminal is recomputed rather than served from the file.
            if status is None or not status.get("terminal"):
                status = self.rebuild_status(name)
            if state and status.get("state") != state:
                continue
            rows.append(status)
            if len(rows) >= max(1, limit):
                next_cursor = name
                break
        return {"developments": rows, "cursor": next_cursor}

    def events(
        self, development_id: str, after: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        self._record(development_id)
        path = self._dev_root(development_id) / EVENTS_FILE
        entries: list[dict[str, Any]] = []
        if path.is_file():
            for index, line in enumerate(
                (raw for raw in path.read_text(encoding="utf-8").splitlines() if raw), start=1
            ):
                entries.append({"event_id": f"e{index}", **json.loads(line)})
        threshold = 0
        if after:
            try:
                threshold = int(str(after).lstrip("e"))
            except ValueError as exc:
                raise ControlPlaneError(
                    "EVENT_CURSOR_INVALID", f"after must look like e12, got {after!r}"
                ) from exc
        selected = [e for e in entries if int(e["event_id"].lstrip("e")) > threshold]
        return {
            "development_id": development_id,
            "events": selected[: max(1, limit)],
            "head_event_id": entries[-1]["event_id"] if entries else None,
        }

    # --- evidence --------------------------------------------------------

    def evidence(self, development_id: str) -> dict[str, Any]:
        """Assemble the evidence entry from git + checkpoint + receipts, live.

        Nothing here is a stored summary: the chain digests come from the
        sealed receipt files and the state the checkpoint carries, the
        acceptance and gate records are read out of the commits that carry
        them, and the remote head is resolved now. supervise/audit.py is the
        first consumer, through the same field names it already reads.
        """
        record = self._record(development_id)
        repo = Path(str(record["repo_path"]))
        values = self._checkpoint_state(development_id) or {}
        result = self._read_result(development_id) or {}
        history = list(values.get("history") or result.get("history") or [])
        receipt_digests = dict(values.get("receipt_digests") or {})

        chain = self._receipt_chain(record, repo, history, receipt_digests)
        acceptance = next((r for r in chain if r["stage"] == "acceptance"), None)
        gate = self._committed_gate_decision(record)
        merge = self._committed_json(repo, MERGE_RESULT_PATH)

        head_commit = str(
            (chain[-1]["output_commit"] if chain else "") or values.get("head_commit") or ""
        )
        ancestor_ok = False
        if head_commit:
            ancestor_ok = (
                run_git(
                    repo,
                    "merge-base",
                    "--is-ancestor",
                    str(record["target_base_commit"]),
                    head_commit,
                ).returncode
                == 0
            )
        remote_verified = self._remote_ref_matches(record, head_commit)
        terminal = str(result.get("terminal") or "")

        entry = {
            "revision": len(chain),
            "generation": 1,
            "verified": bool(
                terminal == "complete"
                and acceptance is not None
                and remote_verified
                and ancestor_ok
            ),
            "remote_main_verified": remote_verified,
            "accepted_commit_ancestor": ancestor_ok,
            "target_base_commit": record["target_base_commit"],
            "bootstrap": {
                "output_commit": record["bootstrap_commit"],
                "receipt_digest": record["root_handoff_digest"],
                "spec_digest": record["spec_digest"],
                "h0": self._h0(development_id),
            },
            "receipt_chain": chain,
            "gate": gate,
            "merge": merge,
            "terminal": terminal,
        }
        return {
            "development_id": development_id,
            "state": self.rebuild_status(development_id)["state"],
            "evidence": [entry],
        }

    def _h0(self, development_id: str) -> dict[str, Any] | None:
        path = self._dev_root(development_id) / H0_FILE
        if not path.is_file():
            return None
        h0 = json.loads(path.read_text(encoding="utf-8"))
        return {"payload": h0, "digest_recomputed": digest_of(canonical_bytes(h0))}

    def _committed_json(self, repo: Path, relative: str) -> dict[str, Any] | None:
        found = run_git(repo, "show", f"HEAD:{relative}")
        if found.returncode != 0:
            return None
        try:
            return dict(json.loads(found.stdout))
        except ValueError:
            return None

    def _remote_ref_matches(self, record: dict[str, Any], head_commit: str) -> bool:
        if not head_commit:
            return False
        listed = run_git(
            Path(str(record["repo_path"])),
            "ls-remote",
            str(record["remote_url"]),
            str(record["remote_ref"]),
        )
        if listed.returncode != 0:
            return False
        heads = [line.split()[0] for line in listed.stdout.splitlines() if line.strip()]
        return bool(heads) and heads[0] == head_commit

    def _receipt_chain(
        self,
        record: dict[str, Any],
        repo: Path,
        history: list[dict[str, Any]],
        receipt_digests: dict[str, str],
    ) -> list[dict[str, Any]]:
        from fleet_graph.dd.dispatch import derive_attempt_id
        from fleet_graph.dd.upstream_constants import compute_json_digest

        development_id = str(record["development_id"])
        state_root = self._dev_root(development_id) / "state"
        chain: list[dict[str, Any]] = []
        previous_output = str(record["bootstrap_commit"])
        previous_digest = str(record["root_handoff_digest"])

        sealed = [
            entry
            for entry in history
            if entry.get("output_commit") and entry.get("event") is not None
        ]
        for revision, entry in enumerate(sealed, start=1):
            stage = str(entry.get("stage") or "")
            output_commit = str(entry.get("output_commit") or "")
            attempt_id = derive_attempt_id(development_id, 1, int(entry.get("attempt") or 1))
            receipt, parent_from_receipt, file_digest = self._sealed_receipt(
                state_root, attempt_id, stage, repo, output_commit
            )
            if receipt is None:
                # A script stage with nothing sealed on file reconstructs the
                # WorkspaceSealer receipt it produced -- the exact shape whose
                # canonical digest the next plugin dispatch named as parent.
                receipt = {
                    "stage": stage,
                    "input_commit": previous_output,
                    "output_commit": output_commit,
                }
            # Which digest the *next* link actually names: the sealer re-reads
            # a persisted receipt's exact bytes (dd_materializer.receipt_digest),
            # so a stage with a file on disk chains by its byte digest; a stage
            # with no file chains by the canonical-JSON digest the dispatch
            # builder computes over the in-memory receipt. Measured live on
            # dev-fg-55126095a185: the review receipts name the implement
            # receipt's byte digest, not its canonical one.
            digest = file_digest or receipt_digests.get(stage) or compute_json_digest(receipt)
            chain.append(
                {
                    "revision": revision,
                    "stage": stage,
                    "attempt": int(entry.get("attempt") or 1),
                    "verdict": str(entry.get("event") or ""),
                    "input_commit": previous_output,
                    "output_commit": output_commit,
                    "receipt_digest": digest,
                    "parent_handoff_receipt_digest": parent_from_receipt or previous_digest,
                    # "receipt" means the sealed file attested it;
                    # "derived" means the link is closed by construction and
                    # carries no independent attestation.
                    "parent_source": "receipt" if parent_from_receipt else "derived",
                    "receipt": receipt,
                }
            )
            previous_output = output_commit
            if digest:
                previous_digest = digest
        return chain

    def _sealed_receipt(
        self, state_root: Path, attempt_id: str, stage: str, repo: Path, output_commit: str
    ) -> tuple[dict[str, Any] | None, str, str]:
        """(receipt, its own parent claim, its persisted bytes' digest).

        The byte digest is what a later receipt names as parent -- the sealer
        re-reads exactly those bytes -- so it is returned alongside the parsed
        receipt rather than recomputed from an equivalent object.
        """
        filenames = {
            "implement": "implement-receipt.json",
            "continuous_review": "continuous-review-receipt.json",
            "final_review": "final-review-receipt.json",
        }
        name = filenames.get(stage)
        if name is not None:
            path = state_root / "receipts" / attempt_id / name
            if path.is_file():
                raw = path.read_bytes()
                try:
                    receipt = dict(json.loads(raw.decode("utf-8")))
                except ValueError:
                    receipt = None
                if receipt is not None:
                    return (
                        receipt,
                        str(receipt.get("parent_handoff_receipt_digest") or ""),
                        "sha256:" + hashlib.sha256(raw).hexdigest(),
                    )

        committed = {
            "acceptance": ACCEPTANCE_RECORD_PATH,
            "human_gate": GATE_DECISION_PATH,
            "merger": MERGE_RESULT_PATH,
        }.get(stage)
        if committed and output_commit:
            found = run_git(repo, "show", f"{output_commit}:{committed}")
            if found.returncode == 0:
                try:
                    payload = dict(json.loads(found.stdout))
                except ValueError:
                    payload = None
                if payload is not None:
                    receipt = dict(payload)
                    if stage == "acceptance":
                        # The subject is the tree that carries the frozen
                        # record; the audit checks it out and re-runs exactly
                        # those argvs.
                        receipt["subject_commit"] = output_commit
                        receipt["artifacts"] = [
                            {
                                "path": committed,
                                "digest": "sha256:"
                                + hashlib.sha256(
                                    self._git_show_bytes(repo, f"{output_commit}:{committed}")
                                ).hexdigest(),
                            }
                        ]
                    return receipt, "", ""
        return None, "", ""

    def _git_show_bytes(self, repo: Path, spec: str) -> bytes:
        from fleet_graph.dd.git import git_argv
        from fleet_graph.dd.vendor import git_ops

        proc = subprocess.run(
            git_argv(repo, "show", spec),
            capture_output=True,
            env=git_ops.safe_git_environment(),
            check=False,
        )
        return proc.stdout if proc.returncode == 0 else b""


__all__ = [
    "ACCEPTANCE_FENCE",
    "CHECKPOINT_FILE",
    "DEFAULT_DD_ROOT",
    "DEFAULT_EXECUTABLE",
    "DEFAULT_PLUGIN_BINDING",
    "DEFAULT_WORKING_DIRECTORY",
    "DEFAULT_WORKTREE_ROOTS",
    "EVENTS_FILE",
    "GATE_DECISION_PATH",
    "H0_FILE",
    "LAUNCHES_FILE",
    "RECORD_FILE",
    "RESULT_FILE",
    "STATUS_FILE",
    "UNIT_PREFIX",
    "ControlPlaneError",
    "DdControlPlane",
    "DdLaunchSpec",
    "build_h0_handoff",
    "derive_acceptance_commands",
    "derive_development_id",
]
