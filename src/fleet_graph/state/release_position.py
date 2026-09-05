"""R4（一线一分支）: the per-line branch-position readings on the state face.

Two first-class readings, both explicitly marked when there is no sample or
the numbers are unavailable -- never a fabricated 0:

- ``release_behind`` -- how many commits the line branch head
  (``refs/heads/release/<line-id>``) trails its origin counterpart
  (``refs/remotes/origin/release/<line-id>``). This is the dispatch-side
  staleness view check 14's probe reads: after a dispatch's configure rebase
  the local branch is synced to the origin head, so the reading returns to 0.

- ``deploy_behind`` -- how many commits the execution position (the
  ``released_commit`` the line's latest completed order actually pushed onto
  the line branch) trails the line branch head. This is the D8 冻结代价
  reading: when the branch has advanced past what the line last deployed, the
  rework chain would break silently unless the supervision face can see it.

Computation source and cache discipline (spec 开放点 4): both readings are
computed from **local refs only** -- ``git rev-parse``/``rev-list`` against
the local line branch and its remote-tracking counterpart, in the repo the
line's latest dispatched order was admitted against. No network, no writes:
the read model never fetches. The remote-tracking ref is the cache, refreshed
by whoever legitimately runs a fetch (configure's first step fetches origin
before rebasing), so the reading's staleness is exactly the staleness of the
last dispatch-side fetch -- consistent with the read model's pull-per-request
refresh cadence. When any input is missing (no dispatched order, no repo, no
branch, no merge artifact) the reading is ``None`` with an explicit
``*_basis`` reason instead of a default 0.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fleet_graph.dd.git import run_git

#: Only a record durable on a release branch counts as a line-branch sample.
RELEASE_REF_PREFIX = "refs/heads/release/"

#: Where the merger freezes the commit it actually pushed onto the line
#: branch (graphs.dd_scripts.MERGE_PATH, generation-formatted).
MERGE_RESULT_PATH = ".dev-dispatch/merge/result-g{generation}.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    """One artifact, or None on missing/unreadable/unparseable (never raises)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _development_generation(dd_root: Path, development_id: str) -> int:
    """The development's current generation from its admission record; 1 fail-soft."""
    record = _read_json(dd_root / development_id / "record.json")
    try:
        return max(1, int((record or {}).get("generation") or 1))
    except (TypeError, ValueError):
        return 1


def _generation_result_path(dd_root: Path, development_id: str, generation: int) -> Path:
    """Where one generation's ``result.json`` lives (g1 at the dev root)."""
    dev_root = dd_root / development_id
    if generation and generation <= 1:
        return dev_root / "result.json"
    return dev_root / f"g{generation}" / "result.json"


def _git_output(repo: Path, *args: str) -> str:
    """One guarded read-only git query's stdout, or "" on any failure."""
    proc = run_git(repo, *args)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _rev_list_count(repo: Path, older: str, newer: str) -> int | None:
    """Commits reachable from ``newer`` but not ``older``; None on any error."""
    out = _git_output(repo, "rev-list", "--count", f"{older}..{newer}")
    if not out.isdigit():
        return None
    return int(out)


def _latest_release_record(dd_root: Path, folder_id: str) -> dict[str, Any] | None:
    """The latest admission record this line dispatched onto a release branch."""
    if not dd_root.is_dir():
        return None
    try:
        entries = sorted(dd_root.iterdir(), key=lambda p: p.name)
    except OSError:
        return None
    latest: dict[str, Any] | None = None
    for entry in entries:
        if not entry.is_dir():
            continue
        record = _read_json(entry / "record.json")
        if record is None:
            continue
        if str(record.get("dispatched_by") or "") != folder_id:
            continue
        if not str(record.get("remote_ref") or "").startswith(RELEASE_REF_PREFIX):
            continue
        if latest is None or str(record.get("created_at") or "") >= str(
            latest.get("created_at") or ""
        ):
            latest = record
    return latest


def _released_commit(record: dict[str, Any], dd_root: Path) -> str:
    """The commit the line's latest order actually pushed onto the branch.

    Read from the generation's authority artifacts: the run result names the
    head commit and generation, and the committed merge result names the
    released commit. Anything missing degrades to "" -- the reading is then
    marked unavailable, never guessed.
    """
    development_id = str(record.get("development_id") or "")
    if not development_id:
        return ""
    generation = _development_generation(dd_root, development_id)
    result = _read_json(_generation_result_path(dd_root, development_id, generation))
    if result is None:
        return ""
    head_commit = str(result.get("head_commit") or "")
    if not head_commit:
        return ""
    repo = Path(str(record.get("repo_path") or ""))
    if not repo.is_dir():
        return ""
    merge_path = MERGE_RESULT_PATH.format(generation=generation)
    proc = run_git(repo, "show", f"{head_commit}:{merge_path}")
    if proc.returncode != 0:
        return ""
    try:
        merge = json.loads(proc.stdout)
    except ValueError:
        return ""
    released = str(merge.get("released_commit") or "")
    return released if released else ""


def release_position(dd_root: Path, folder_id: str) -> dict[str, Any]:
    """Both branch-position readings for one line, with honest bases.

    Every returned dict always carries the four keys (the field surface is
    stable); ``None`` readings carry a ``*_basis`` reason instead of a number.
    """
    surface: dict[str, Any] = {
        "release_ref": "",
        "release_behind": None,
        "deploy_behind": None,
        "release_behind_basis": "",
        "deploy_behind_basis": "",
    }
    record = _latest_release_record(dd_root, folder_id)
    if record is None:
        surface["release_behind_basis"] = "no_line_branch_dispatch"
        surface["deploy_behind_basis"] = "no_line_branch_dispatch"
        return surface
    repo = Path(str(record.get("repo_path") or ""))
    ref = str(record.get("remote_ref") or "")
    surface["release_ref"] = ref
    if not repo.is_dir():
        surface["release_behind_basis"] = "repo_unavailable"
        surface["deploy_behind_basis"] = "repo_unavailable"
        return surface
    short = ref.removeprefix("refs/heads/")
    local = _git_output(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{short}^{{commit}}")
    tracking = _git_output(
        repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{short}^{{commit}}"
    )
    if not local:
        surface["release_behind_basis"] = "local_branch_missing"
    elif not tracking:
        surface["release_behind_basis"] = "origin_branch_unfetched"
    else:
        behind = _rev_list_count(repo, local, tracking)
        if behind is None:
            surface["release_behind_basis"] = "unreadable"
        else:
            surface["release_behind"] = behind
            surface["release_behind_basis"] = "measured"
    released = _released_commit(record, dd_root)
    if not local:
        surface["deploy_behind_basis"] = "local_branch_missing"
    elif not released:
        surface["deploy_behind_basis"] = "no_released_commit"
    else:
        behind = _rev_list_count(repo, released, local)
        if behind is None:
            surface["deploy_behind_basis"] = "unreadable"
        else:
            surface["deploy_behind"] = behind
            surface["deploy_behind_basis"] = "measured"
    return surface


__all__ = ["MERGE_RESULT_PATH", "RELEASE_REF_PREFIX", "release_position"]
