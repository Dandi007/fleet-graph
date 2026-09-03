"""The final_review stage's engine-side mutation experiment (S12 closure).

The spec (goal.md §二 M3 S12, design.md §6.3/§6.3.1) moves mutation testing out
of the gate's hands: the dd engine's **final_review stage** executes it, in a
**one-shot copy**, and the gate only *verifies the receipt* -- it never reruns
the experiment, because a line that shot its own blind spot is exactly the
deviation S12 exists to remove. This module is that execution entry: the
production function the final_review seal path calls, statically reachable in
the review modules' call graph (the D8 equivalence assertion pins the
reachability, never "a process is running").

Three obligations live here.

**Mechanical enumeration.** The targets are this single's new production call
sites, enumerated from the ``base..head`` product diff by
:func:`fleet_graph.dd.self_gate.enumerate_mutation_targets` -- the same
mechanism the gate re-derives. Nobody chooses targets; a diff yields a set.

**One-shot copy.** Each deletion and acceptance rerun happens in a disposable
copy of the subject tree (``git archive <head>`` extracted to a temporary
directory, discarded afterwards). The subject workspace is only ever *read*:
a verification experiment that writes it voids the conclusion (修订一), so
there is no write path to it here at all.

**The receipt artifact.** The experiment emits ``final-review-mutation.json``
beside the sealed review receipt -- the sidecar that carries, per target, its
position (file:line/call) and red/green result, plus the non-empty
``verified_items`` checklist. The sealed plugin receipt's own field set is
pinned upstream, so the engine-side record rides in this sidecar; the gate
merges the view and refuses a verdict whose receipt misses the checklist or
whose target set is short of, or ahead of, the mechanical enumeration (S12.5:
缺该字段 → 回执无效).
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from fleet_graph.dd.git import run_git
from fleet_graph.dd.self_gate import MutationTarget, enumerate_mutation_targets
from fleet_graph.dd.self_gate_evidence import default_rerun, diff_added_lines

#: The final_review stage's mutation record: the sidecar the gate merges into
#: the sealed receipt's view. Written under ``<state_root>/receipts/<attempt>/``.
MUTATION_RECEIPT_FILE = "final-review-mutation.json"

#: The continuous_review stage's checked-items sidecar (S12.5: the rc receipt
#: must also name what was checked, even at findings=0).
CONTINUOUS_CHECKED_FILE = "continuous-review-checked.json"

#: Sidecar names by review stage id -- the engine-side completion of a review
#: receipt's view, merged by the gate's collector.
REVIEW_SIDECAR_FILES = {
    "continuous_review": CONTINUOUS_CHECKED_FILE,
    "final_review": MUTATION_RECEIPT_FILE,
}

#: Checklist keys a review receipt view may carry its 已核验项 under. Either
#: satisfies the schema; both absent (or empty) is an invalid receipt.
CHECKLIST_KEYS = ("checked_items", "verified_items")

#: ``(workspace, argv) -> (echo, exit_code)`` -- the run seam, the same shape
#: the personal rerun uses, pointed at the one-shot copy.
ExperimentRun = Callable[[Path, list[str]], tuple[str, int]]


def _checklist_of(payload: dict[str, Any]) -> list[Any] | None:
    for key in CHECKLIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return None


def checklist_defect(payload: dict[str, Any]) -> str | None:
    """The S12.5 defect a receipt carries when its 已核验项 checklist is missing.

    A review that lists nothing it checked -- even one with ``findings: []`` --
    has not said what its verdict covers, and its receipt is invalid.
    """
    items = _checklist_of(payload)
    if items is None:
        return (
            "review receipt carries no checked/verified_items checklist "
            "(S12: even findings=0 must list what was checked)"
        )
    if not [item for item in items if isinstance(item, str) and item.strip()]:
        return "review receipt's checked/verified_items checklist is empty"
    return None


def _target_defects(targets: Any) -> list[str]:
    if not isinstance(targets, list):
        return ["final review receipt carries no mutation_targets record (S12)"]
    defects: list[str] = []
    for index, entry in enumerate(targets):
        if not isinstance(entry, dict):
            defects.append(f"mutation_targets[{index}] is not an object")
            continue
        if not str(entry.get("file") or "").strip():
            defects.append(f"mutation_targets[{index}] names no file")
        line = entry.get("line")
        if not isinstance(line, int) or isinstance(line, bool) or line <= 0:
            defects.append(f"mutation_targets[{index}] carries no line number")
        if not str(entry.get("call") or "").strip():
            defects.append(f"mutation_targets[{index}] names no call")
        if not isinstance(entry.get("red"), bool):
            defects.append(f"mutation_targets[{index}] carries no red/green result")
    return defects


def review_receipt_defects(payload: dict[str, Any], *, phase: str) -> list[str]:
    """The engine-side schema gate for a review receipt's view (S12.5).

    ``payload`` is the receipt as the engine consumes it -- the sealed receipt
    merged with its engine-side sidecar. Both review phases must carry the
    已核验项 checklist; the final review must additionally carry the
    per-target mutation record with a position and a red/green result for
    every entry. An empty defect list is a valid receipt; anything else is
    回执无效.
    """
    defects: list[str] = []
    checklist = checklist_defect(payload)
    if checklist is not None:
        defects.append(checklist)
    if phase == "final":
        defects.extend(_target_defects(payload.get("mutation_targets")))
    return defects


def declared_review_defects(declared: dict[str, Any]) -> list[str]:
    """The engine-side schema gate for a reviewer's *declared* result.

    The pinned plugin schema cannot grow a field, so the checklist a reviewer
    declares is validated here, before the result is narrowed for the sealer,
    and persisted engine-side in the phase's sidecar. A declaration without
    it fails the seal: the receipt it would produce is invalid by construction.
    """
    checklist = checklist_defect(declared)
    return [checklist] if checklist is not None else []


@contextlib.contextmanager
def one_shot_copy(workspace: Path, head: str, *, copy_root: Path | None = None) -> Iterator[Path]:
    """A disposable copy of the subject tree at ``head``; the workspace stays read-only.

    The tree is materialized with ``git archive`` -- a pure read of the object
    database -- into a temporary directory removed on exit. Nothing in the
    subject workspace is written: no worktree admin entries, no index locks,
    no stray files. A verification experiment that wrote the subject workspace
    would void its own conclusion (修订一), so this is the only substrate the
    experiment ever runs on.
    """
    archive = run_git(workspace, "archive", "--format=tar", head)
    if archive.returncode != 0:
        raise RuntimeError(f"one-shot copy of {head[:8]} failed: {archive.stderr.strip()[:300]}")
    copy_dir = str(copy_root) if copy_root else None
    root = Path(tempfile.mkdtemp(prefix="dd-final-review-copy-", dir=copy_dir))
    try:
        payload = archive.stdout.encode() if isinstance(archive.stdout, str) else archive.stdout
        extract = subprocess.run(
            ["tar", "-x", "-C", str(root)],
            input=payload,
            capture_output=True,
            check=False,
        )
        if extract.returncode != 0:
            raise RuntimeError(
                "one-shot copy extraction failed: "
                + (extract.stderr.decode("utf-8", "replace").strip()[:300] or "tar exited")
            )
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _restore_copy(workspace: Path, head: str, copy: Path) -> None:
    """Reset the copy's tree to ``head`` from the same read-only source.

    Never from the subject workspace's working files: the restore re-reads the
    object database, so the experiment's substrate stays a pure function of
    the reviewed commit.
    """
    archive = run_git(workspace, "archive", "--format=tar", head)
    if archive.returncode != 0:
        raise RuntimeError(f"copy restore from {head[:8]} failed: {archive.stderr.strip()[:300]}")
    payload = archive.stdout.encode() if isinstance(archive.stdout, str) else archive.stdout
    extract = subprocess.run(
        ["tar", "-x", "-C", str(copy)], input=payload, capture_output=True, check=False
    )
    if extract.returncode != 0:
        raise RuntimeError(
            "copy restore extraction failed: "
            + (extract.stderr.decode("utf-8", "replace").strip()[:300] or "tar exited")
        )


def delete_target_line(copy: Path, target: MutationTarget) -> None:
    """The mutation: delete the enumerated call-site line in the copy.

    The unit is the line the diff added -- the same mechanical unit the
    enumeration named. A multi-line call loses its head, which turns the
    module unparseable or the call absent; either way the frozen acceptance
    answers whether anything still covers the call site.
    """
    path = copy / target.file
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if not 1 <= target.line <= len(lines):
        raise RuntimeError(
            f"mutation target {target.file}:{target.line} is outside the copy's file"
        )
    del lines[target.line - 1]
    path.write_text("".join(lines), encoding="utf-8")


def execute_mutation_experiment(
    *,
    workspace: Path,
    base: str,
    head: str,
    acceptance_commands: Sequence[Sequence[str]],
    run: ExperimentRun | None = None,
    copy_root: Path | None = None,
) -> dict[str, Any]:
    """The final_review stage's mutation experiment (S12.3) -- the entry.

    Enumerates this single's new production call sites from the
    ``base..head`` product diff, deletes each in a one-shot copy, reruns the
    frozen acceptance command in that copy, and returns the receipt payload:
    every target's position and red/green result, plus the verified-items
    checklist. A target whose deletion leaves the acceptance green (``red:
    false``) has no test coverage -- the gate refuses the verdict over it.

    Faults (unreadable diff, unextractable tree, no frozen command) raise: the
    caller records a faulted experiment rather than passing silence off as a
    receipt.
    """
    rerun = run or default_rerun
    targets = enumerate_mutation_targets(diff_added_lines(workspace, base, head))
    frozen = [list(command) for command in acceptance_commands]
    if not frozen:
        raise RuntimeError("the stage declares no frozen acceptance command to mutate against")

    results: list[dict[str, Any]] = []
    with one_shot_copy(workspace, head, copy_root=copy_root) as copy:
        for target in targets:
            delete_target_line(copy, target)
            exit_code = 1
            for command in frozen:
                _echo, exit_code = rerun(copy, command)
                if exit_code != 0:
                    break
            results.append(
                {
                    "file": target.file,
                    "line": target.line,
                    "call": target.call,
                    "red": exit_code != 0,
                    "acceptance_exit_code": exit_code,
                }
            )
            _restore_copy(workspace, head, copy)

    return {
        "implementation_subject_commit": head,
        "target_base_commit": base,
        "enumerated_from": f"{base}..{head}",
        "acceptance_commands": frozen,
        "mutation_targets": results,
        "verified_items": [
            "mutation targets mechanically enumerated from the base..head product diff",
            "each target deleted in a one-shot copy of the subject tree",
            "the frozen acceptance command rerun in the copy for every target",
            "the subject workspace left untouched (read-only)",
        ],
    }


def faulted_experiment(detail: str, *, base: str, head: str) -> dict[str, Any]:
    """The receipt payload for an experiment that could not run.

    The record exists -- the stage ran, and this is what it hit -- but it
    verifies nothing: an empty checklist and an empty target set fail the
    gate's schema, so a faulted experiment can never wave a verdict through.
    """
    return {
        "implementation_subject_commit": head,
        "target_base_commit": base,
        "enumerated_from": f"{base}..{head}",
        "fault": detail,
        "mutation_targets": [],
        "verified_items": [],
    }


def write_mutation_receipt(state_root: Path, attempt_id: str, payload: dict[str, Any]) -> Path:
    """Persist the final_review mutation record beside the sealed review receipt."""
    path = Path(state_root) / "receipts" / attempt_id / MUTATION_RECEIPT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return path


def write_checked_items(state_root: Path, attempt_id: str, *, checked_items: list[Any]) -> Path:
    """Persist the continuous_review stage's checklist beside its sealed receipt."""
    path = Path(state_root) / "receipts" / attempt_id / CONTINUOUS_CHECKED_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"checked_items": list(checked_items), "review_phase": "continuous"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "CHECKLIST_KEYS",
    "CONTINUOUS_CHECKED_FILE",
    "MUTATION_RECEIPT_FILE",
    "REVIEW_SIDECAR_FILES",
    "MutationTarget",
    "checklist_defect",
    "declared_review_defects",
    "delete_target_line",
    "execute_mutation_experiment",
    "faulted_experiment",
    "one_shot_copy",
    "review_receipt_defects",
    "write_checked_items",
    "write_mutation_receipt",
]
