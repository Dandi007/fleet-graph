"""Git operations for isolated workspace management."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any

from fleet_graph.dd.vendor import external_ops

_FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_DURABLE_HEAD_REF_RE = re.compile(r"^refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*$")
_ATTEMPT_REMOTE_REF = "refs/remotes/origin/attempt-context-input"
_GH_CREDENTIAL_HELPER = "!/usr/bin/gh auth git-credential"

logger = logging.getLogger(__name__)


def _gh_credential_config_args() -> list[str]:
    """Reset inherited helpers, then admit only the fixed gh credential protocol."""

    return [
        "-c",
        "credential.helper=",
        "-c",
        f"credential.helper={_GH_CREDENTIAL_HELPER}",
    ]


class ExactWorkspaceError(RuntimeError):
    """A fail-closed exact-input Git preparation failure.

    ``expected_head`` and ``subject_head`` are PURELY DIAGNOSTIC. They are
    never read by an admission check, a CAS comparison, or any publish
    decision; the strict compare-and-swap semantics of the exact-workspace
    protocol are unchanged by their presence. They exist so a caller can tell
    a *deterministic* "the world has moved past the frozen precondition"
    failure apart from a *transient* race, and so an operator-facing report can
    name all three commits (frozen expectation, observed remote, intended
    subject) without having to re-derive them.

    - ``expected_head``: the frozen head this operation was authorized against.
    - ``subject_head``: the exact commit this operation intended to publish.
    - ``remote_head``: the head actually observed on the remote.
    """

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        local_head: str | None = None,
        remote_head: str | None = None,
        expected_head: str | None = None,
        subject_head: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.local_head = local_head
        self.remote_head = remote_head
        self.expected_head = expected_head
        self.subject_head = subject_head


@dataclass(frozen=True, kw_only=True)
class ExactWorkspaceIdentity:
    """The four-way Git identity proven immediately before actor dispatch."""

    workspace_path: str
    durable_ref: str
    input_commit: str
    remote_head: str
    local_head: str


@dataclass(frozen=True, kw_only=True)
class TargetUpdateResult:
    """Exact controller materialization of one target-head merge-forward."""

    input_commit: str
    observed_target_head_commit: str
    output_commit: str
    tree_sha: str
    artifact_path: str
    artifact_blob_oid: str
    artifact_digest: str


@dataclass(frozen=True, kw_only=True)
class TargetMergeResult:
    """CAS settlement of the target ref to the current durable handoff."""

    target_ref: str
    target_head_before: str
    target_head_after: str


def safe_git_environment() -> dict[str, str]:
    """Return an environment isolated from caller-controlled Git settings."""

    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _safe_git(
    *args: str,
    worktree: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        "git",
        *_gh_credential_config_args(),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "protocol.ext.allow=never",
    ]
    if worktree is not None:
        command.extend(
            [
                "-c",
                f"core.worktree={os.path.realpath(worktree)}",
                "-c",
                "core.bare=false",
                "-c",
                "core.sparseCheckout=false",
                "-c",
                "core.sparseCheckoutCone=false",
            ]
        )
        command.extend(["-C", worktree])
    command.extend(args)
    try:
        return external_ops.run_process(
            command,
            cwd=os.path.dirname(os.devnull) if worktree is None else None,
            text=True,
            env=safe_git_environment(),
            kind="git",
        )
    except OSError as exc:
        raise ExactWorkspaceError(
            "INPUT_COMMIT_MISMATCH",
            f"Git verification failed: {exc}",
        ) from exc


def _command_text(
    *args: str,
    worktree: str | None = None,
    code: str = "INPUT_COMMIT_MISMATCH",
) -> str:
    proc = _safe_git(*args, worktree=worktree)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:500]
        raise ExactWorkspaceError(
            code,
            f"git {args[0]} failed: {detail or f'exit {proc.returncode}'}",
        )
    return proc.stdout.strip()


def _validate_exact_workspace_inputs(
    remote_url: str,
    durable_ref: str,
    input_commit: str,
) -> None:
    if (
        not isinstance(remote_url, str)
        or not remote_url
        or remote_url.startswith(("-", "ext::"))
        or any(ord(char) < 32 or ord(char) == 127 for char in remote_url)
    ):
        raise ExactWorkspaceError(
            "MR_IDENTITY_MISMATCH",
            "remote URL is not a safe Git transport identity",
        )
    if _DURABLE_HEAD_REF_RE.fullmatch(durable_ref) is None or ".." in durable_ref or "//" in durable_ref or durable_ref.endswith((".", "/")):
        raise ExactWorkspaceError(
            "MR_IDENTITY_MISMATCH",
            "durable ref must be one canonical refs/heads/... identity",
        )
    if _FULL_COMMIT_RE.fullmatch(input_commit) is None:
        raise ExactWorkspaceError(
            "INPUT_COMMIT_MISMATCH",
            "input commit must be one full lowercase 40-hex object ID",
        )


def resolve_remote_ref(
    remote_url: str,
    durable_ref: str,
) -> str:
    """Resolve exactly one durable ref without loading a repository config."""

    _validate_exact_workspace_inputs(remote_url, durable_ref, "0" * 40)
    proc = _safe_git(
        "ls-remote",
        "--exit-code",
        "--refs",
        remote_url,
        durable_ref,
    )
    records = [line.split("\t", 1) for line in proc.stdout.splitlines() if line.strip()]
    if proc.returncode != 0 or len(records) != 1:
        raise ExactWorkspaceError(
            "INPUT_COMMIT_MISMATCH",
            f"durable ref {durable_ref!r} is missing or ambiguous",
            remote_head=None,
        )
    remote_head, observed_ref = records[0]
    if observed_ref != durable_ref or _FULL_COMMIT_RE.fullmatch(remote_head) is None:
        raise ExactWorkspaceError(
            "INPUT_COMMIT_MISMATCH",
            f"durable ref {durable_ref!r} returned malformed identity",
            remote_head=remote_head or None,
        )
    return remote_head


def _assert_remote_input(
    remote_url: str,
    durable_ref: str,
    input_commit: str,
) -> str:
    remote_head = resolve_remote_ref(remote_url, durable_ref)
    if remote_head != input_commit:
        raise ExactWorkspaceError(
            "INPUT_COMMIT_MISMATCH",
            f"remote durable ref head {remote_head} != input commit {input_commit}",
            remote_head=remote_head,
        )
    return remote_head


def _verify_local_exact_workspace(
    workspace_path: str,
    *,
    remote_url: str,
    durable_ref: str,
    input_commit: str,
    remote_head: str,
) -> ExactWorkspaceIdentity:
    if (
        not os.path.isdir(workspace_path)
        or os.path.islink(workspace_path)
        or not os.path.isdir(os.path.join(workspace_path, ".git"))
        or os.path.islink(os.path.join(workspace_path, ".git"))
    ):
        raise ExactWorkspaceError(
            "INVALID_WORKSPACE",
            f"workspace {workspace_path!r} is not one standalone Git directory",
            remote_head=remote_head,
        )
    observed_url = _command_text(
        "remote",
        "get-url",
        "origin",
        worktree=workspace_path,
        code="MR_IDENTITY_MISMATCH",
    )
    if observed_url != remote_url:
        raise ExactWorkspaceError(
            "MR_IDENTITY_MISMATCH",
            f"workspace origin {observed_url!r} != durable remote {remote_url!r}",
            remote_head=remote_head,
        )
    _verify_local_git_config(
        workspace_path,
        remote_url=remote_url,
        durable_ref=durable_ref,
        remote_head=remote_head,
    )

    head_proc = _safe_git(
        "rev-parse",
        "HEAD",
        worktree=workspace_path,
    )
    local_head = head_proc.stdout.strip() if head_proc.returncode == 0 else None
    if local_head != input_commit:
        raise ExactWorkspaceError(
            "INPUT_COMMIT_MISMATCH",
            f"local HEAD {local_head!r} != input commit {input_commit}",
            local_head=local_head,
            remote_head=remote_head,
        )
    object_proc = _safe_git(
        "cat-file",
        "-e",
        f"{input_commit}^{{commit}}",
        worktree=workspace_path,
    )
    if object_proc.returncode != 0:
        raise ExactWorkspaceError(
            "INPUT_COMMIT_MISMATCH",
            f"input commit object {input_commit} is missing from local workspace",
            local_head=local_head,
            remote_head=remote_head,
        )
    symbolic = _safe_git(
        "symbolic-ref",
        "-q",
        "HEAD",
        worktree=workspace_path,
    )
    if symbolic.returncode == 0:
        raise ExactWorkspaceError(
            "INVALID_WORKSPACE",
            "attempt workspace HEAD must be detached from branch semantics",
            local_head=local_head,
            remote_head=remote_head,
        )
    local_branches = _command_text(
        "for-each-ref",
        "--format=%(refname)",
        "refs/heads",
        worktree=workspace_path,
    )
    if local_branches:
        raise ExactWorkspaceError(
            "INVALID_WORKSPACE",
            "attempt workspace must not contain a candidate branch",
            local_head=local_head,
            remote_head=remote_head,
        )
    remote_refs = _command_text(
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        "refs/remotes",
        worktree=workspace_path,
    ).splitlines()
    if remote_refs != [f"{_ATTEMPT_REMOTE_REF} {input_commit}"]:
        raise ExactWorkspaceError(
            "INPUT_COMMIT_MISMATCH",
            (f"workspace does not contain exactly the fetched durable input ref: {remote_refs!r}"),
            local_head=local_head,
            remote_head=remote_head,
        )
    status = _command_text(
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored=matching",
        worktree=workspace_path,
        code="DIRTY_WORKTREE",
    )
    if status:
        raise ExactWorkspaceError(
            "DIRTY_WORKTREE",
            "attempt workspace is not clean",
            local_head=local_head,
            remote_head=remote_head,
        )

    # Close the fetch/checkout race immediately before the caller can submit an
    # actor. A ref move after the first fetch must not be hidden by FETCH_HEAD.
    final_remote_head = _assert_remote_input(
        remote_url,
        durable_ref,
        input_commit,
    )
    return ExactWorkspaceIdentity(
        workspace_path=workspace_path,
        durable_ref=durable_ref,
        input_commit=input_commit,
        remote_head=final_remote_head,
        local_head=local_head,
    )


def sanitize_ignored_dirt(workspace_path: str) -> bool:
    status = _safe_git(
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored=matching",
        worktree=workspace_path,
    )
    if status.returncode != 0:
        logger.warning("sanitize_ignored_dirt: git status failed for %s", workspace_path)
        return False
    lines = status.stdout.strip().splitlines()
    if not lines:
        return True
    if not all(line.startswith("!!") for line in lines):
        logger.warning(
            "sanitize_ignored_dirt: non-ignored dirt in %s, falling through to rebuild",
            workspace_path,
        )
        return False
    clean = _safe_git(
        "clean", "-fdX",
        worktree=workspace_path,
    )
    if clean.returncode != 0:
        logger.warning("sanitize_ignored_dirt: git clean failed for %s", workspace_path)
        return False
    status2 = _safe_git(
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored=matching",
        worktree=workspace_path,
    )
    if status2.returncode != 0:
        logger.warning("sanitize_ignored_dirt: re-verify status failed for %s", workspace_path)
        return False
    cleaned = not status2.stdout.strip()
    if cleaned:
        logger.info(
            "sanitize_ignored_dirt: removed ignored-only artifacts from %s, verdict accepted",
            workspace_path,
        )
    return cleaned


def _late_stage_required_config(remote_url: str) -> dict[str, list[str]]:
    """The exact local config contract of one late-stage (attached) workspace.

    Single source of truth shared by create_attached_exact_workspace() and
    verify_late_stage_workspace(). A late-stage workspace deliberately carries
    no remote.origin.fetch: its refspec is passed on the fetch command line
    (with --refmap=) so no implicit refspec can widen the fetch. Keeping the
    creator and the verifier on one definition is what stops ACCEPTANCE from
    rejecting the very workspace the reconciler just created.
    """

    return {
        "core.repositoryformatversion": ["0"],
        "core.bare": ["false"],
        "core.logallrefupdates": ["true"],
        "remote.origin.url": [remote_url],
        "user.email": ["loop-engine@localhost"],
        "user.name": ["Loop Engine"],
    }


def _verify_local_git_config(
    workspace_path: str,
    *,
    remote_url: str,
    durable_ref: str,
    remote_head: str,
    required: dict[str, list[str]] | None = None,
    kind: str = "attempt",
) -> None:
    raw = _command_text(
        "config",
        "--local",
        "--null",
        "--list",
        "--no-includes",
        worktree=workspace_path,
        code="INVALID_WORKSPACE",
    )
    entries: dict[str, list[str]] = {}
    for record in raw.split("\0"):
        if not record:
            continue
        if "\n" not in record:
            raise ExactWorkspaceError(
                "INVALID_WORKSPACE",
                "workspace local Git config contains a malformed entry",
                remote_head=remote_head,
            )
        key, value = record.split("\n", 1)
        entries.setdefault(key, []).append(value)

    if required is None:
        required = {
            "core.repositoryformatversion": ["0"],
            "core.bare": ["false"],
            "core.logallrefupdates": ["true"],
            "remote.origin.url": [remote_url],
            "remote.origin.fetch": [f"+{durable_ref}:{_ATTEMPT_REMOTE_REF}"],
            "user.email": ["loop-engine@localhost"],
            "user.name": ["Loop Engine"],
        }
    optional = {
        "core.filemode": {"true", "false"},
        "core.ignorecase": {"true", "false"},
        "core.precomposeunicode": {"true", "false"},
    }
    unknown = sorted(set(entries) - set(required) - set(optional))
    if unknown:
        raise ExactWorkspaceError(
            "INVALID_WORKSPACE",
            f"{kind} workspace local Git config contains unapproved keys: {unknown}",
            remote_head=remote_head,
        )
    for key, expected in required.items():
        observed = entries.get(key)
        if observed != expected:
            raise ExactWorkspaceError(
                "INVALID_WORKSPACE",
                f"{kind} workspace local Git config {key!r} expected "
                f"{expected!r} but observed {observed!r}",
                remote_head=remote_head,
            )
    for key, allowed in optional.items():
        values = entries.get(key)
        if values is not None and (len(values) != 1 or values[0] not in allowed):
            raise ExactWorkspaceError(
                "INVALID_WORKSPACE",
                f"workspace local Git config {key!r} is invalid",
                remote_head=remote_head,
            )


def create_exact_input_workspace(
    *,
    remote_url: str,
    durable_ref: str,
    input_commit: str,
    workspace_path: str,
) -> ExactWorkspaceIdentity:
    """Fetch one durable ref and detach a new clean workspace at exact input."""

    _validate_exact_workspace_inputs(remote_url, durable_ref, input_commit)
    remote_head = _assert_remote_input(remote_url, durable_ref, input_commit)
    if os.path.lexists(workspace_path):
        raise ExactWorkspaceError(
            "INVALID_WORKSPACE",
            f"new workspace path already exists: {workspace_path}",
            remote_head=remote_head,
        )
    parent = os.path.dirname(workspace_path)
    os.makedirs(parent, exist_ok=True)
    init = _safe_git("init", "--quiet", workspace_path)
    if init.returncode != 0:
        detail = (init.stderr or init.stdout or "").strip()[:500]
        raise ExactWorkspaceError(
            "INVALID_WORKSPACE",
            f"could not initialize exact-input workspace: {detail}",
            remote_head=remote_head,
        )
    _command_text(
        "config",
        "remote.origin.url",
        remote_url,
        worktree=workspace_path,
        code="MR_IDENTITY_MISMATCH",
    )
    _command_text(
        "config",
        "remote.origin.fetch",
        f"+{durable_ref}:{_ATTEMPT_REMOTE_REF}",
        worktree=workspace_path,
        code="MR_IDENTITY_MISMATCH",
    )
    _command_text(
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        "--refmap=",
        "origin",
        f"+{durable_ref}:{_ATTEMPT_REMOTE_REF}",
        worktree=workspace_path,
    )
    fetched = _command_text(
        "rev-parse",
        f"{_ATTEMPT_REMOTE_REF}^{{commit}}",
        worktree=workspace_path,
    )
    if fetched != input_commit:
        raise ExactWorkspaceError(
            "INPUT_COMMIT_MISMATCH",
            f"fetched durable ref {fetched!r} != input commit {input_commit}",
            remote_head=remote_head,
        )
    _command_text(
        "checkout",
        "--detach",
        "--force",
        input_commit,
        worktree=workspace_path,
    )
    _command_text(
        "config",
        "user.email",
        "loop-engine@localhost",
        worktree=workspace_path,
    )
    _command_text(
        "config",
        "user.name",
        "Loop Engine",
        worktree=workspace_path,
    )
    return _verify_local_exact_workspace(
        workspace_path,
        remote_url=remote_url,
        durable_ref=durable_ref,
        input_commit=input_commit,
        remote_head=remote_head,
    )


def verify_exact_input_workspace(
    *,
    remote_url: str,
    durable_ref: str,
    input_commit: str,
    workspace_path: str,
) -> ExactWorkspaceIdentity:
    """Re-verify an idempotently reused attempt workspace without repairing it."""

    _validate_exact_workspace_inputs(remote_url, durable_ref, input_commit)
    remote_head = _assert_remote_input(remote_url, durable_ref, input_commit)
    return _verify_local_exact_workspace(
        workspace_path,
        remote_url=remote_url,
        durable_ref=durable_ref,
        input_commit=input_commit,
        remote_head=remote_head,
    )


def create_attached_exact_workspace(
    *,
    remote_url: str,
    durable_ref: str,
    input_commit: str,
    workspace_path: str,
) -> ExactWorkspaceIdentity:
    """Create one controller-only workspace attached to the durable head ref."""

    _validate_exact_workspace_inputs(remote_url, durable_ref, input_commit)
    remote_head = _assert_remote_input(remote_url, durable_ref, input_commit)
    if os.path.lexists(workspace_path):
        raise ExactWorkspaceError(
            "INVALID_WORKSPACE",
            f"new late-stage workspace already exists: {workspace_path}",
            remote_head=remote_head,
        )
    branch = durable_ref.removeprefix("refs/heads/")
    parent = os.path.dirname(workspace_path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    init = _safe_git("init", "--quiet", workspace_path)
    if init.returncode != 0:
        raise ExactWorkspaceError(
            "INVALID_WORKSPACE",
            "could not initialize late-stage workspace",
            remote_head=remote_head,
        )
    # Write exactly the keys the late-stage verifier will assert. core.* keys are
    # produced by `git init` itself, so they are asserted but not written here.
    _init_written = ("core.repositoryformatversion", "core.bare", "core.logallrefupdates")
    for key, values in _late_stage_required_config(remote_url).items():
        if key in _init_written:
            continue
        _command_text(
            "config",
            key,
            values[0],
            worktree=workspace_path,
            code="INVALID_WORKSPACE",
        )
    _command_text(
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        "--refmap=",
        "origin",
        f"+{durable_ref}:refs/remotes/origin/attempt-context-late-input",
        worktree=workspace_path,
    )
    _command_text(
        "checkout",
        "--force",
        "-b",
        branch,
        input_commit,
        worktree=workspace_path,
    )
    local_head = _command_text(
        "rev-parse",
        "HEAD",
        worktree=workspace_path,
    )
    symbolic = _command_text(
        "symbolic-ref",
        "HEAD",
        worktree=workspace_path,
    )
    status = _command_text(
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        worktree=workspace_path,
        code="DIRTY_WORKTREE",
    )
    if local_head != input_commit or symbolic != durable_ref or status or _assert_remote_input(remote_url, durable_ref, input_commit) != input_commit:
        raise ExactWorkspaceError(
            "INPUT_COMMIT_MISMATCH",
            "late-stage workspace does not bind the exact durable input",
            local_head=local_head,
            remote_head=remote_head,
        )
    return ExactWorkspaceIdentity(
        workspace_path=workspace_path,
        durable_ref=durable_ref,
        input_commit=input_commit,
        remote_head=remote_head,
        local_head=local_head,
    )


def verify_late_stage_workspace(
    *,
    remote_url: str,
    durable_ref: str,
    input_commit: str,
    workspace_path: str,
) -> ExactWorkspaceIdentity:
    """Re-verify a reused late-stage workspace against its creation contract.

    The reconciler creates a late-stage workspace once and re-verifies it on
    every later reconcile pass. Verifying it with the *attempt* (detached)
    contract is a permanent failure: a late-stage workspace carries no
    remote.origin.fetch and is attached to durable_ref rather than detached.
    That mismatch made ACCEPTANCE retry INVALID_WORKSPACE forever.
    """

    _validate_exact_workspace_inputs(remote_url, durable_ref, input_commit)
    remote_head = _assert_remote_input(remote_url, durable_ref, input_commit)
    if (
        not os.path.isdir(workspace_path)
        or os.path.islink(workspace_path)
        or not os.path.isdir(os.path.join(workspace_path, ".git"))
        or os.path.islink(os.path.join(workspace_path, ".git"))
    ):
        raise ExactWorkspaceError(
            "INVALID_WORKSPACE",
            f"late-stage workspace {workspace_path!r} is not one standalone Git directory",
            remote_head=remote_head,
        )
    _verify_local_git_config(
        workspace_path,
        remote_url=remote_url,
        durable_ref=durable_ref,
        remote_head=remote_head,
        required=_late_stage_required_config(remote_url),
        kind="late-stage",
    )
    local_head = _command_text("rev-parse", "HEAD", worktree=workspace_path)
    symbolic = _command_text("symbolic-ref", "HEAD", worktree=workspace_path)
    status = _command_text(
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        worktree=workspace_path,
        code="DIRTY_WORKTREE",
    )
    if local_head != input_commit or symbolic != durable_ref or status:
        raise ExactWorkspaceError(
            "INPUT_COMMIT_MISMATCH",
            "late-stage workspace does not bind the exact durable input",
            local_head=local_head,
            remote_head=remote_head,
        )
    return ExactWorkspaceIdentity(
        workspace_path=workspace_path,
        durable_ref=durable_ref,
        input_commit=input_commit,
        remote_head=remote_head,
        local_head=local_head,
    )


@dataclass(frozen=True, kw_only=True)
class SyntheticIntegrationIdentity:
    """Identity of the controller-built synthetic integration checkout.

    Spec §R3: the synthetic integration checkout combines the exact candidate
    (``input_commit``) with the current target head
    (``observed_target_head_commit``) into one clean synthesized integration
    commit (``integration_commit``). Its tree is the clean merge of the two
    parents; its HEAD is the synthesized commit, not the candidate input
    commit.
    """

    workspace_path: str
    durable_ref: str
    input_commit: str
    observed_target_head_commit: str
    integration_commit: str
    integration_tree: str


def _synthetic_integration_tree(
    workspace_path: str,
    *,
    input_commit: str,
    observed_target_head_commit: str,
) -> str:
    return _merge_tree_resolve_reserved(
        workspace_path,
        input_commit=input_commit,
        target_commit=observed_target_head_commit,
        reserved_winner="input",
        error_code="SYNTHETIC_INTEGRATION_CONFLICT",
        error_detail="candidate cannot be merged cleanly with the current target head",
    )


# Spec P5b §4: `.dev-dispatch/` must stay reserved (B-group controller plane).
# `.dd-evidence/` is retained as well even though acceptance no longer writes
# it: legacy trees admitted before P5b still carry the path, the H0
# acceptance-cleanup allowlist in handoff.py still consumes it, and
# _merge_tree_resolve_reserved deterministically resolves any leftover
# conflict on it to the durable side. Removing it would weaken none of today's
# protections, but keeping it costs nothing and avoids reopening a path that a
# pre-P5b tree may still reference.
_RESERVED_PREFIXES = (".dev-dispatch/", ".dd-evidence/")


def _merge_tree_resolve_reserved(
    workspace_path: str,
    *,
    input_commit: str,
    target_commit: str,
    reserved_winner: str,
    error_code: str,
    error_detail: str,
) -> str:
    merge_tree_result = _git_with_environment(
        workspace_path,
        [
            "merge-tree",
            "--write-tree",
            input_commit,
            target_commit,
        ],
    )
    merge_lines = merge_tree_result.stdout.decode(
        "utf-8",
        "replace",
    ).splitlines()
    if not merge_lines or _FULL_COMMIT_RE.fullmatch(merge_lines[0]) is None:
        raise ExactWorkspaceError(
            error_code,
            error_detail,
        )
    merged_tree = merge_lines[0]
    if merge_tree_result.returncode == 0:
        return merged_tree

    conflicted_paths: set[str] = set()
    for line in merge_lines[1:]:
        if not line:
            continue
        if "\t" not in line:
            continue
        metadata, path = line.split("\t", 1)
        metadata_parts = metadata.split()
        if len(metadata_parts) != 3:
            continue
        mode, obj, stage = metadata_parts
        if not _FULL_COMMIT_RE.fullmatch(obj):
            continue
        if stage not in ("1", "2", "3"):
            continue
        if mode not in ("100644", "100755", "120000", "160000"):
            continue
        conflicted_paths.add(path)

    if not all(
        p.startswith(_RESERVED_PREFIXES) for p in conflicted_paths
    ):
        raise ExactWorkspaceError(
            error_code,
            error_detail,
        )

    winner_commit = input_commit if reserved_winner == "input" else target_commit

    with tempfile.TemporaryDirectory(prefix="merge-resolve-index-") as index_root:
        index_path = os.path.join(index_root, "index")
        index_env = {"GIT_INDEX_FILE": index_path}
        _git_bytes_ok(
            workspace_path,
            ["read-tree", merged_tree],
            env_updates=index_env,
            code=error_code,
        )
        for path in conflicted_paths:
            _git_bytes_ok(
                workspace_path,
                ["rm", "--cached", "--force", "--ignore-unmatch", "--", path],
                env_updates=index_env,
                code=error_code,
            )
            winner_entry_out = _git_bytes_ok(
                workspace_path,
                ["ls-tree", "--full-tree", winner_commit, "--", path],
                code=error_code,
            ).decode().strip()
            if not winner_entry_out:
                raise ExactWorkspaceError(
                    error_code,
                    f"reserved-conflict path {path!r} not found in winning commit, "
                    "cannot resolve deterministically",
                )
            matched = False
            for we in winner_entry_out.splitlines():
                we = we.strip()
                if not we:
                    continue
                if "\t" not in we:
                    continue
                meta, entry_path = we.split("\t", 1)
                if entry_path != path:
                    continue
                entries = meta.split()
                if len(entries) < 3:
                    continue
                mode = entries[0]
                obj = entries[2]
                _git_bytes_ok(
                    workspace_path,
                    [
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        f"{mode},{obj},{path}",
                    ],
                    env_updates=index_env,
                    code=error_code,
                )
                matched = True
                break
            if not matched:
                raise ExactWorkspaceError(
                    error_code,
                    f"reserved-conflict path {path!r} entry in winning commit "
                    "could not be parsed, cannot resolve deterministically",
                )
        final_tree = _git_bytes_ok(
            workspace_path,
            ["write-tree"],
            env_updates=index_env,
            code=error_code,
        ).decode().strip()
    if _FULL_COMMIT_RE.fullmatch(final_tree) is None:
        raise ExactWorkspaceError(
            error_code,
            "reserved-path conflict resolution produced an invalid tree",
        )
    return final_tree


def _materialize_synthetic_integration_commit(
    workspace_path: str,
    *,
    input_commit: str,
    observed_target_head_commit: str,
    integration_tree: str,
) -> str:
    commit_env = {
        "GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z",
        "GIT_AUTHOR_EMAIL": "dev-dispatch@localhost",
        "GIT_AUTHOR_NAME": "Dev Dispatch",
        "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z",
        "GIT_COMMITTER_EMAIL": "dev-dispatch@localhost",
        "GIT_COMMITTER_NAME": "Dev Dispatch",
    }
    output_commit = (
        _git_bytes_ok(
            workspace_path,
            [
                "commit-tree",
                integration_tree,
                "-p",
                input_commit,
                "-p",
                observed_target_head_commit,
            ],
            env_updates=commit_env,
            input_bytes=b"dev-dispatch: synthetic integration of candidate and target head\n",
        )
        .decode()
        .strip()
    )
    if _FULL_COMMIT_RE.fullmatch(output_commit) is None:
        raise ExactWorkspaceError(
            "SYNTHETIC_INTEGRATION_FAILED",
            "synthetic integration commit is not one full object ID",
        )
    return output_commit


def create_synthetic_integration_workspace(
    *,
    remote_url: str,
    durable_ref: str,
    input_commit: str,
    workspace_path: str,
    target_ref: str,
) -> SyntheticIntegrationIdentity:
    """Build one clean synthetic integration checkout.

    Spec §R3: the checkout combines the exact candidate (``input_commit``,
    still the durable ref head) with the CURRENT target head
    (``target_ref``). It must not fall back to the candidate checkout. The
    resulting HEAD is the synthesized integration commit (a clean merge of
    the candidate and the target head); the working tree is reset to that
    commit and must be clean.
    """

    _validate_exact_workspace_inputs(remote_url, durable_ref, input_commit)
    _validate_exact_workspace_inputs(remote_url, target_ref, "0" * 40)
    remote_head = _assert_remote_input(remote_url, durable_ref, input_commit)
    observed_target_head_commit = resolve_remote_ref(remote_url, target_ref)
    if observed_target_head_commit == input_commit:
        raise ExactWorkspaceError(
            "SYNTHETIC_INTEGRATION_CONFLICT",
            "target head equals the candidate input; no integration is defined",
        )
    if os.path.lexists(workspace_path):
        raise ExactWorkspaceError(
            "INVALID_WORKSPACE",
            f"new synthetic integration workspace already exists: {workspace_path}",
            remote_head=remote_head,
        )
    branch = durable_ref.removeprefix("refs/heads/")
    parent = os.path.dirname(workspace_path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    init = _safe_git("init", "--quiet", workspace_path)
    if init.returncode != 0:
        raise ExactWorkspaceError(
            "INVALID_WORKSPACE",
            "could not initialize synthetic integration workspace",
            remote_head=remote_head,
        )
    for key, value in (
        ("remote.origin.url", remote_url),
        ("user.email", "loop-engine@localhost"),
        ("user.name", "Loop Engine"),
    ):
        _command_text(
            "config",
            key,
            value,
            worktree=workspace_path,
            code="INVALID_WORKSPACE",
        )
    _command_text(
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        "--refmap=",
        "origin",
        f"+{durable_ref}:refs/remotes/origin/attempt-context-synthetic-candidate",
        worktree=workspace_path,
    )
    _command_text(
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        "--refmap=",
        "origin",
        f"+{target_ref}:refs/remotes/origin/attempt-context-synthetic-target",
        worktree=workspace_path,
    )
    fetched_candidate = (
        _git_bytes_ok(
            workspace_path,
            [
                "rev-parse",
                "refs/remotes/origin/attempt-context-synthetic-candidate^{commit}",
            ],
        )
        .decode()
        .strip()
    )
    fetched_target = (
        _git_bytes_ok(
            workspace_path,
            [
                "rev-parse",
                "refs/remotes/origin/attempt-context-synthetic-target^{commit}",
            ],
        )
        .decode()
        .strip()
    )
    if fetched_candidate != input_commit:
        raise ExactWorkspaceError(
            "INPUT_COMMIT_MISMATCH",
            "fetched candidate does not equal the durable input commit",
            remote_head=remote_head,
        )
    if fetched_target != observed_target_head_commit:
        raise ExactWorkspaceError(
            "TARGET_HEAD_CONFLICT",
            "fetched target does not equal the frozen target head",
            remote_head=remote_head,
        )
    integration_tree = _synthetic_integration_tree(
        workspace_path,
        input_commit=input_commit,
        observed_target_head_commit=observed_target_head_commit,
    )
    integration_commit = _materialize_synthetic_integration_commit(
        workspace_path,
        input_commit=input_commit,
        observed_target_head_commit=observed_target_head_commit,
        integration_tree=integration_tree,
    )
    _command_text(
        "checkout",
        "--force",
        "-b",
        branch,
        integration_commit,
        worktree=workspace_path,
    )
    return _verify_synthetic_integration_workspace(
        workspace_path,
        remote_url=remote_url,
        durable_ref=durable_ref,
        input_commit=input_commit,
        observed_target_head_commit=observed_target_head_commit,
        integration_commit=integration_commit,
        integration_tree=integration_tree,
    )


def _verify_synthetic_integration_workspace(
    workspace_path: str,
    *,
    remote_url: str,
    durable_ref: str,
    input_commit: str,
    observed_target_head_commit: str,
    integration_commit: str,
    integration_tree: str,
) -> SyntheticIntegrationIdentity:
    if (
        not os.path.isdir(workspace_path)
        or os.path.islink(workspace_path)
        or not os.path.isdir(os.path.join(workspace_path, ".git"))
        or os.path.islink(os.path.join(workspace_path, ".git"))
    ):
        raise ExactWorkspaceError(
            "INVALID_WORKSPACE",
            f"workspace {workspace_path!r} is not one standalone Git directory",
        )
    observed_url = _command_text(
        "remote",
        "get-url",
        "origin",
        worktree=workspace_path,
        code="MR_IDENTITY_MISMATCH",
    )
    if observed_url != remote_url:
        raise ExactWorkspaceError(
            "MR_IDENTITY_MISMATCH",
            f"workspace origin {observed_url!r} != durable remote {remote_url!r}",
        )
    local_head = _command_text(
        "rev-parse",
        "HEAD",
        worktree=workspace_path,
    )
    if local_head != integration_commit:
        raise ExactWorkspaceError(
            "INPUT_COMMIT_MISMATCH",
            f"local HEAD {local_head!r} != synthetic integration commit "
            f"{integration_commit!r}",
            local_head=local_head,
        )
    head_tree = _command_text(
        "rev-parse",
        "HEAD^{tree}",
        worktree=workspace_path,
    )
    if head_tree != integration_tree:
        raise ExactWorkspaceError(
            "INPUT_COMMIT_MISMATCH",
            "synthetic integration HEAD tree does not match the synthesized tree",
            local_head=local_head,
        )
    parents = _command_text(
        "rev-list",
        "--no-walk",
        "--parents",
        "HEAD",
        worktree=workspace_path,
    ).split()
    if (
        len(parents) != 3
        or parents[1] != input_commit
        or parents[2] != observed_target_head_commit
    ):
        raise ExactWorkspaceError(
            "INPUT_COMMIT_MISMATCH",
            "synthetic integration commit parents do not bind candidate and target",
            local_head=local_head,
        )
    status = _command_text(
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored=matching",
        worktree=workspace_path,
        code="DIRTY_WORKTREE",
    )
    if status:
        raise ExactWorkspaceError(
            "DIRTY_WORKTREE",
            "synthetic integration workspace is not clean",
            local_head=local_head,
        )
    return SyntheticIntegrationIdentity(
        workspace_path=workspace_path,
        durable_ref=durable_ref,
        input_commit=input_commit,
        observed_target_head_commit=observed_target_head_commit,
        integration_commit=integration_commit,
        integration_tree=integration_tree,
    )


def verify_synthetic_integration_workspace(
    *,
    remote_url: str,
    durable_ref: str,
    input_commit: str,
    workspace_path: str,
    target_ref: str,
) -> SyntheticIntegrationIdentity:
    """Re-verify an idempotently reused synthetic integration checkout.

    Spec §R3: the checkout must remain clean and must represent the expected
    synthesized tree/commit for the CURRENT candidate and target head. The
    durable ref must still point at ``input_commit``; the target head is
    re-resolved and the synthesized integration commit/tree are recomputed
    and compared against the on-disk HEAD.
    """

    _validate_exact_workspace_inputs(remote_url, durable_ref, input_commit)
    _validate_exact_workspace_inputs(remote_url, target_ref, "0" * 40)
    _assert_remote_input(remote_url, durable_ref, input_commit)
    observed_target_head_commit = resolve_remote_ref(remote_url, target_ref)
    if observed_target_head_commit == input_commit:
        raise ExactWorkspaceError(
            "SYNTHETIC_INTEGRATION_CONFLICT",
            "target head equals the candidate input; no integration is defined",
        )
    _command_text(
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        "--refmap=",
        "origin",
        f"+{durable_ref}:refs/remotes/origin/attempt-context-synthetic-candidate",
        worktree=workspace_path,
    )
    _command_text(
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        "--refmap=",
        "origin",
        f"+{target_ref}:refs/remotes/origin/attempt-context-synthetic-target",
        worktree=workspace_path,
    )
    fetched_target = (
        _git_bytes_ok(
            workspace_path,
            [
                "rev-parse",
                "refs/remotes/origin/attempt-context-synthetic-target^{commit}",
            ],
        )
        .decode()
        .strip()
    )
    if fetched_target != observed_target_head_commit:
        raise ExactWorkspaceError(
            "TARGET_HEAD_CONFLICT",
            "fetched target does not equal the frozen target head",
        )
    integration_tree = _synthetic_integration_tree(
        workspace_path,
        input_commit=input_commit,
        observed_target_head_commit=observed_target_head_commit,
    )
    integration_commit = _materialize_synthetic_integration_commit(
        workspace_path,
        input_commit=input_commit,
        observed_target_head_commit=observed_target_head_commit,
        integration_tree=integration_tree,
    )
    return _verify_synthetic_integration_workspace(
        workspace_path,
        remote_url=remote_url,
        durable_ref=durable_ref,
        input_commit=input_commit,
        observed_target_head_commit=observed_target_head_commit,
        integration_commit=integration_commit,
        integration_tree=integration_tree,
    )


def exact_commit_identity(
    workspace_path: str,
    commit: str,
) -> dict[str, Any]:
    """Read exact commit/tree/parent identities without checkout mutation."""

    if _FULL_COMMIT_RE.fullmatch(commit) is None:
        raise ExactWorkspaceError(
            "INPUT_COMMIT_MISMATCH",
            "commit identity must be one full object ID",
        )
    tree = _command_text(
        "rev-parse",
        f"{commit}^{{tree}}",
        worktree=workspace_path,
    )
    parents_line = _command_text(
        "rev-list",
        "--parents",
        "-n",
        "1",
        commit,
        worktree=workspace_path,
    ).split()
    if not parents_line or parents_line[0] != commit:
        raise ExactWorkspaceError(
            "INPUT_COMMIT_MISMATCH",
            "commit parent identity cannot be read",
        )
    return {
        "sha": commit,
        "tree_sha": tree,
        "parents": parents_line[1:],
    }


def exact_artifact_identity(
    workspace_path: str,
    commit: str,
    path: str,
) -> dict[str, str]:
    """Resolve a regular tracked blob and digest from one exact commit."""

    if not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/"):
        raise ExactWorkspaceError(
            "INVALID_ARTIFACT_PATH",
            "artifact path is not one normalized relative path",
        )
    listing = _command_text(
        "ls-tree",
        commit,
        "--",
        path,
        worktree=workspace_path,
        code="INVALID_ARTIFACT_PATH",
    ).splitlines()
    if len(listing) != 1 or "\t" not in listing[0]:
        raise ExactWorkspaceError(
            "INVALID_ARTIFACT_PATH",
            f"artifact {path!r} is missing or ambiguous",
        )
    metadata, observed_path = listing[0].split("\t", 1)
    fields = metadata.split()
    if observed_path != path or len(fields) != 3 or fields[0] != "100644" or fields[1] != "blob" or _FULL_COMMIT_RE.fullmatch(fields[2]) is None:
        raise ExactWorkspaceError(
            "INVALID_ARTIFACT_PATH",
            f"artifact {path!r} is not one regular blob",
        )
    blob = _git_with_environment(
        workspace_path,
        ["cat-file", "blob", fields[2]],
    )
    if blob.returncode != 0:
        raise ExactWorkspaceError(
            "INVALID_ARTIFACT_PATH",
            f"artifact {path!r} bytes cannot be read",
        )
    payload = bytes(blob.stdout)
    return {
        "blob_oid": fields[2],
        "digest": f"sha256:{hashlib.sha256(payload).hexdigest()}",
        "path": path,
    }


def _git_with_environment(
    workspace_path: str,
    args: list[str],
    *,
    env_updates: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    env = safe_git_environment()
    if env_updates:
        env.update(env_updates)
    command = [
        "git",
        *_gh_credential_config_args(),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "protocol.ext.allow=never",
        "-C",
        workspace_path,
        *args,
    ]
    try:
        return external_ops.run_process(
            command,
            env=env,
            input=input_bytes,
            kind="git",
        )
    except OSError as exc:
        raise ExactWorkspaceError(
            "TARGET_UPDATE_FAILED",
            f"target-update Git operation failed: {exc}",
        ) from exc


def _git_bytes_ok(
    workspace_path: str,
    args: list[str],
    *,
    env_updates: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
    code: str = "TARGET_UPDATE_FAILED",
) -> bytes:
    result = _git_with_environment(
        workspace_path,
        args,
        env_updates=env_updates,
        input_bytes=input_bytes,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()[:500]
        raise ExactWorkspaceError(
            code,
            detail or f"git {args[0]} exited {result.returncode}",
        )
    return bytes(result.stdout)


def materialize_target_update(
    *,
    workspace_path: str,
    remote_url: str,
    durable_ref: str,
    target_ref: str,
    input_commit: str,
    observed_target_head_commit: str,
    artifact_path: str,
    artifact_bytes: bytes,
    commit_message: str,
    author_name: str,
    author_email: str,
    author_timestamp: str,
    committer_name: str,
    committer_email: str,
    committer_timestamp: str,
) -> TargetUpdateResult:
    """Merge a frozen target head into the handoff and CAS-publish one commit."""

    _validate_exact_workspace_inputs(remote_url, durable_ref, input_commit)
    _validate_exact_workspace_inputs(
        remote_url,
        target_ref,
        observed_target_head_commit,
    )
    if not artifact_path.startswith(".dev-dispatch/target-updates/") or ".." in artifact_path.split("/"):
        raise ExactWorkspaceError(
            "INVALID_ARTIFACT_PATH",
            "target-update receipt path is not canonical",
        )
    observed_remote_head = resolve_remote_ref(remote_url, target_ref)
    if observed_remote_head != (observed_target_head_commit):
        # R5: the CAS predicate itself is unchanged and stays strict. The extra
        # fields are diagnostics only, so the caller can classify this as a
        # deterministic (never self-healing) failure instead of retrying it
        # forever.
        raise ExactWorkspaceError(
            "TARGET_HEAD_CONFLICT",
            "target ref moved after the update was frozen",
            remote_head=observed_remote_head,
            expected_head=observed_target_head_commit,
            subject_head=input_commit,
        )

    _git_bytes_ok(
        workspace_path,
        [
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "--refmap=",
            "origin",
            (f"+{target_ref}:refs/remotes/origin/attempt-context-target"),
        ],
    )
    fetched_target = (
        _git_bytes_ok(
            workspace_path,
            [
                "rev-parse",
                "refs/remotes/origin/attempt-context-target^{commit}",
            ],
        )
        .decode()
        .strip()
    )
    if fetched_target != observed_target_head_commit:
        raise ExactWorkspaceError(
            "TARGET_HEAD_CONFLICT",
            "fetched target does not equal the frozen target head",
        )
    merged_tree = _merge_tree_resolve_reserved(
        workspace_path,
        input_commit=input_commit,
        target_commit=observed_target_head_commit,
        reserved_winner="input",
        error_code="TARGET_MERGE_CONFLICT",
        error_detail="frozen target cannot be merged cleanly into the current handoff",
    )

    with tempfile.TemporaryDirectory(prefix="attempt-context-target-index-") as index_root:
        index_path = os.path.join(index_root, "index")
        index_env = {"GIT_INDEX_FILE": index_path}
        _git_bytes_ok(
            workspace_path,
            ["read-tree", merged_tree],
            env_updates=index_env,
        )
        artifact_blob = (
            _git_bytes_ok(
                workspace_path,
                ["hash-object", "-w", "--stdin"],
                input_bytes=artifact_bytes,
            )
            .decode()
            .strip()
        )
        if _FULL_COMMIT_RE.fullmatch(artifact_blob) is None:
            raise ExactWorkspaceError(
                "TARGET_UPDATE_FAILED",
                "artifact hash is not a full object ID",
            )
        _git_bytes_ok(
            workspace_path,
            [
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{artifact_blob},{artifact_path}",
            ],
            env_updates=index_env,
        )
        final_tree = (
            _git_bytes_ok(
                workspace_path,
                ["write-tree"],
                env_updates=index_env,
            )
            .decode()
            .strip()
        )

    commit_env = {
        "GIT_AUTHOR_DATE": author_timestamp,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_AUTHOR_NAME": author_name,
        "GIT_COMMITTER_DATE": committer_timestamp,
        "GIT_COMMITTER_EMAIL": committer_email,
        "GIT_COMMITTER_NAME": committer_name,
    }
    output_commit = (
        _git_bytes_ok(
            workspace_path,
            [
                "commit-tree",
                final_tree,
                "-p",
                input_commit,
                "-p",
                observed_target_head_commit,
            ],
            env_updates=commit_env,
            input_bytes=(commit_message.rstrip("\n") + "\n").encode(),
        )
        .decode()
        .strip()
    )
    if _FULL_COMMIT_RE.fullmatch(output_commit) is None:
        raise ExactWorkspaceError(
            "TARGET_UPDATE_FAILED",
            "target-update commit is not one full object ID",
        )

    remote_before = resolve_remote_ref(remote_url, durable_ref)
    if remote_before not in {input_commit, output_commit}:
        raise ExactWorkspaceError(
            "REMOTE_HEAD_CONFLICT",
            "durable ref moved during target-update materialization",
            remote_head=remote_before,
        )
    if remote_before == input_commit:
        push = _safe_git(
            "push",
            "--no-verify",
            "origin",
            f"{output_commit}:{durable_ref}",
            worktree=workspace_path,
        )
        if push.returncode != 0 and resolve_remote_ref(remote_url, durable_ref) != output_commit:
            raise ExactWorkspaceError(
                "REMOTE_HEAD_CONFLICT",
                "target-update CAS publication was rejected",
            )
    if resolve_remote_ref(remote_url, durable_ref) != output_commit:
        raise ExactWorkspaceError(
            "REMOTE_HEAD_CONFLICT",
            "durable ref did not converge to target-update output",
        )
    _command_text(
        "reset",
        "--hard",
        output_commit,
        worktree=workspace_path,
        code="TARGET_UPDATE_FAILED",
    )
    return TargetUpdateResult(
        input_commit=input_commit,
        observed_target_head_commit=observed_target_head_commit,
        output_commit=output_commit,
        tree_sha=final_tree,
        artifact_path=artifact_path,
        artifact_blob_oid=artifact_blob,
        artifact_digest=("sha256:" + hashlib.sha256(artifact_bytes).hexdigest()),
    )


def cas_fast_forward_target(
    *,
    workspace_path: str,
    remote_url: str,
    target_ref: str,
    expected_target_head_commit: str,
    handoff_commit: str,
) -> TargetMergeResult:
    """CAS fast-forward the target ref to an already integrated handoff."""

    _validate_exact_workspace_inputs(
        remote_url,
        target_ref,
        expected_target_head_commit,
    )
    if _FULL_COMMIT_RE.fullmatch(handoff_commit) is None:
        raise ExactWorkspaceError(
            "MERGE_SUBJECT_INCOMPLETE",
            "handoff commit is not one full object ID",
        )
    current = resolve_remote_ref(remote_url, target_ref)
    if current == handoff_commit:
        return TargetMergeResult(
            target_ref=target_ref,
            target_head_before=expected_target_head_commit,
            target_head_after=handoff_commit,
        )
    if current != expected_target_head_commit:
        # R5: predicate unchanged (strict CAS). Diagnostics only.
        raise ExactWorkspaceError(
            "TARGET_HEAD_CONFLICT",
            "target ref moved after merge settlement was frozen",
            remote_head=current,
            expected_head=expected_target_head_commit,
            subject_head=handoff_commit,
        )
    _git_bytes_ok(
        workspace_path,
        [
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "--refmap=",
            "origin",
            f"+{target_ref}:refs/remotes/origin/attempt-context-merge-target",
        ],
    )
    ancestry = _git_with_environment(
        workspace_path,
        [
            "merge-base",
            "--is-ancestor",
            expected_target_head_commit,
            handoff_commit,
        ],
    )
    if ancestry.returncode != 0:
        raise ExactWorkspaceError(
            "TARGET_UPDATE_REQUIRED",
            "current handoff does not contain the frozen target head",
        )
    push = _safe_git(
        "push",
        "--no-verify",
        "origin",
        f"{handoff_commit}:{target_ref}",
        worktree=workspace_path,
    )
    if push.returncode != 0 and resolve_remote_ref(remote_url, target_ref) != handoff_commit:
        raise ExactWorkspaceError(
            "TARGET_HEAD_CONFLICT",
            "target CAS fast-forward was rejected",
        )
    if resolve_remote_ref(remote_url, target_ref) != handoff_commit:
        raise ExactWorkspaceError(
            "TARGET_HEAD_CONFLICT",
            "target ref did not converge to the exact handoff",
        )
    return TargetMergeResult(
        target_ref=target_ref,
        target_head_before=expected_target_head_commit,
        target_head_after=handoff_commit,
    )
