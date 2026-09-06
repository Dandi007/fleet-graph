"""Minimal enroll validation node (goal.enroll/2).

Programmatic gate for MCP enroll requests: validate the payload shape per
docs/specs/minimal/golden-order.md GO-26..33 (which supersede protocol.md §1's
stale goal.enroll/1). No state is written, no process spawned, no branch
created here — GO-34: state belongs to the engine.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any, Protocol

from fleet_graph.dd.git import git_argv

SCHEMA = "goal.enroll/2"

# GO-27 removed goal_path (default WF convention); GO-27 also dropped
# sessions/warn (engine defaults, less is more); GO-29 moved PR/worktree down
# to DD level. The single `repo` field is superseded by GO-26's repos[].
REMOVED_FIELDS: dict[str, str] = {
    "goal_path": "removed by GO-27 (goal files follow the default WF convention)",
    "sessions": "removed by GO-27 (session policy is an engine default)",
    "warn": "removed by GO-27 (warning thresholds are an engine default)",
    "repo": "superseded by GO-26/GO-29 repos[] (a goal spans multiple repos)",
}

TOP_LEVEL_KEYS = frozenset(
    {"schema", "work_folder", "title", "goal_text", "source_branch", "repos"}
)

REPO_KEYS = frozenset({"path", "remote", "target_branch", "acceptance"})

_WORK_FOLDER_PREFIX = "wf-"
_GOAL_ID_PREFIX = "g-"
_GOAL_ID_HEX_LEN = 6


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def is_valid_git_branch_name(name: str) -> bool:
    """Mechanical git ref-name check: no spaces, no leading '-', no '..',
    no trailing '/', no ASCII control chars, no leading/trailing whitespace."""

    if name == "":
        return False
    if name.startswith("-"):
        return False
    if ".." in name:
        return False
    if name.endswith("/"):
        return False
    for ch in name:
        if ch.isspace():
            return False
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            return False
    if "~^:?*[\\" in name:
        return False
    if name.endswith("."):
        return False
    return not name.endswith(".lock")


@dataclass
class RepoSpec:
    path: str
    remote: str
    target_branch: str
    acceptance: list[str] = field(default_factory=list)


@dataclass
class EnrollRequest:
    work_folder: str | None
    title: str
    goal_text: str
    source_branch: str
    repos: list[RepoSpec] = field(default_factory=list)


@dataclass
class EnrollValidation:
    ok: bool
    errors: list[str] = field(default_factory=list)


class GitProbe(Protocol):
    """Duck-typed probe for all checks that would touch the filesystem or git.
    Tests inject a fake; production uses SubprocessProbe."""

    def is_worktree(self, path: str) -> bool: ...

    def branch_exists(self, path: str, branch: str) -> bool: ...

    def bash_parses(self, command: str) -> bool: ...


class SubprocessProbe:
    """Default probe: real `git` / `bash -n` via subprocess. Every call carries a
    timeout and never uses shell=True with user-controlled input. The git calls
    reuse fleet_graph.dd.git's hardened argv (repo-local fsmonitor/hooks and
    ext:: transports disabled): the probed worktrees come from enroll requests,
    and .git/config there is untrusted input."""

    timeout_seconds: float = 30.0

    def _run(self, argv: list[str]) -> bool:
        import subprocess

        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        return proc.returncode == 0

    def is_worktree(self, path: str) -> bool:
        return self._run(git_argv(path, "rev-parse", "--is-inside-work-tree"))

    def branch_exists(self, path: str, branch: str) -> bool:
        return self._run(git_argv(path, "rev-parse", "--verify", f"refs/heads/{branch}"))

    def bash_parses(self, command: str) -> bool:
        return self._run(["bash", "-n", "-c", command])


def _default_probe() -> GitProbe:
    return SubprocessProbe()


def validate_enroll(payload: dict, *, git_probe: GitProbe | None = None) -> EnrollValidation:
    """Validate a goal.enroll/2 payload. Purely mechanical; returns field-level
    errors naming the exact location (e.g. ``repos[1].remote: missing``)."""

    errors: list[str] = []
    probe = git_probe if git_probe is not None else _default_probe()

    def fail(location: str, message: str) -> None:
        errors.append(f"{location}: {message}")

    if not isinstance(payload, dict):
        return EnrollValidation(ok=False, errors=["payload: must be a JSON object"])

    if "schema" not in payload:
        fail("schema", "missing")
    elif payload["schema"] != SCHEMA:
        fail("schema", f"expected {SCHEMA!r}, got {payload['schema']!r}")

    for key in sorted(set(payload) - TOP_LEVEL_KEYS):
        if key in REMOVED_FIELDS:
            fail(key, f"removed field ({REMOVED_FIELDS[key]})")
        else:
            fail(
                key,
                "unknown top-level key (allowed: schema, work_folder, title, goal_text, "
                "source_branch, repos)",
            )

    if "work_folder" in payload:
        work_folder = payload["work_folder"]
        if work_folder is not None and not _is_nonempty_str(work_folder):
            fail("work_folder", "must be null (to be created) or a non-empty string")

    for key in ("title", "goal_text"):
        if key not in payload:
            fail(key, "missing")
        elif not _is_nonempty_str(payload[key]):
            fail(key, "must be a non-empty string")

    if "source_branch" not in payload:
        fail("source_branch", "missing")
    else:
        source_branch = payload["source_branch"]
        if not _is_nonempty_str(source_branch):
            fail("source_branch", "must be a non-empty string")
        elif not is_valid_git_branch_name(source_branch):
            fail("source_branch", f"{source_branch!r} is not a valid git branch name")

    if "repos" not in payload:
        fail("repos", "missing")
        repos: Any = []
    else:
        repos = payload["repos"]
        if not isinstance(repos, list):
            fail("repos", "must be an array")
            repos = []
        elif repos == []:
            fail("repos", "must be a non-empty array")

    if isinstance(repos, list):
        seen_paths: dict[str, int] = {}
        for i, repo in enumerate(repos):
            if not isinstance(repo, dict):
                fail(f"repos[{i}]", "must be an object")
                continue
            for key in sorted(set(repo) - REPO_KEYS):
                fail(
                    f"repos[{i}].{key}",
                    "unknown key in repo entry (allowed: path, remote, target_branch, acceptance)",
                )
            if "path" not in repo:
                fail(f"repos[{i}].path", "missing")
            else:
                path = repo["path"]
                if not _is_nonempty_str(path):
                    fail(f"repos[{i}].path", "must be a non-empty string")
                elif not probe.is_worktree(path):
                    fail(
                        f"repos[{i}].path",
                        f"{path!r} is not a git worktree (GO-27: path must be the worktree path)",
                    )
                if _is_nonempty_str(path):
                    if path in seen_paths:
                        fail(
                            f"repos[{i}].path",
                            f"duplicate path already used by repos[{seen_paths[path]}]",
                        )
                    else:
                        seen_paths[path] = i

            if "remote" not in repo:
                fail(f"repos[{i}].remote", "missing (GO-32: every repo must have a remote)")
            elif not _is_nonempty_str(repo["remote"]):
                fail(f"repos[{i}].remote", "must be a non-empty string (GO-32)")

            if "target_branch" not in repo:
                fail(f"repos[{i}].target_branch", "missing")
            else:
                target = repo["target_branch"]
                if not _is_nonempty_str(target):
                    fail(f"repos[{i}].target_branch", "must be a non-empty string")
                elif not is_valid_git_branch_name(target):
                    fail(f"repos[{i}].target_branch", f"{target!r} is not a valid git branch name")
                elif _is_nonempty_str(repo.get("path")) and not probe.branch_exists(
                    repo["path"], target
                ):
                    fail(f"repos[{i}].target_branch", f"branch {target!r} not found in repo")

            if "acceptance" not in repo:
                fail(f"repos[{i}].acceptance", "missing")
            else:
                acceptance = repo["acceptance"]
                if not isinstance(acceptance, list):
                    fail(f"repos[{i}].acceptance", "must be an array")
                elif acceptance == []:
                    fail(f"repos[{i}].acceptance", "must be a non-empty array")
                else:
                    for j, command in enumerate(acceptance):
                        if not _is_nonempty_str(command):
                            fail(f"repos[{i}].acceptance[{j}]", "must be a non-empty string")
                        elif not probe.bash_parses(command):
                            fail(
                                f"repos[{i}].acceptance[{j}]",
                                f"command does not parse (bash -n): {command!r}",
                            )

    return EnrollValidation(ok=not errors, errors=errors)


def _generate_goal_id() -> str:
    return _GOAL_ID_PREFIX + secrets.token_hex(_GOAL_ID_HEX_LEN // 2)


def normalize_enroll(
    payload: dict, goal_id: str | None = None, *, git_probe: GitProbe | None = None
) -> dict:
    """Normalize a validated payload into the canonical object (fixed field
    order, goal_id filled in). Raises ValueError when the payload does not
    validate. Persists nothing — GO-34: state belongs to the engine."""

    validation = validate_enroll(payload, git_probe=git_probe)
    if not validation.ok:
        raise ValueError(f"invalid goal.enroll/2 payload: {'; '.join(validation.errors)}")

    return {
        "schema": SCHEMA,
        "goal_id": goal_id if goal_id is not None else _generate_goal_id(),
        "work_folder": payload["work_folder"],
        "title": payload["title"],
        "goal_text": payload["goal_text"],
        "source_branch": payload["source_branch"],
        "repos": [
            {
                "path": repo["path"],
                "remote": repo["remote"],
                "target_branch": repo["target_branch"],
                "acceptance": list(repo["acceptance"]),
            }
            for repo in payload["repos"]
        ],
    }
