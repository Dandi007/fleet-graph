"""B3 work-folder residue reconciliation: classify, plan, adopt -- never guess.

The incident this corrects (parent ``wf-a87b04``): an append to a governed
bookkeeping file (``progress.md``) outlived its transaction, the whole-commit
clean guard classified it as un-attributable (``WORKTREE_DIRTY``), and the
intended human exit -- an MCP tool named ``wf_reconcile`` -- was never
registered, so a real invocation came back ``Unknown tool: wf_reconcile``.

This module is the mechanical core of the correction. It is deterministic and
self-contained: it never touches a physical path, never runs git, and never
invents a claim it cannot back. It works over a *source seam* that yields the
folder's governed base and working bytes for each logical filename:

- ``inspect(folder_id) -> tuple[InspectedFile, ...]`` is the read side. The
  physical data repository stays behind the seam -- only the opaque
  ``folder_id`` and logical filenames ever reach this module, so no public
  payload can leak a data-repository root.
- ``adopt(folder_id, entries)`` is the write side, invoked exactly once per
  confirmation. The source commits the appended bytes atomically and returns a
  receipt fragment.

The two-step flow mirrors ``HumanRecoveryExit``/``AdoptionLedger`` (B2):

- ``plan`` is the dry-run. It classifies the residue, refuses closed when any
  file is not a safe append, and otherwise returns a stable plan carrying
  opaque ``folder_id``, logical filenames, classifications, content/base/appended
  digests, and a confirmation token bound to that exact base by digest (a CAS
  binding). It makes no mutation.
- ``confirm`` is the governed exit. It refuses unless the supplied token binds
  to the *current* base and bytes, then adopts the exact appended bytes through
  the source seam, seals a receipt, and is idempotent: re-confirming the same
  token replays the same receipt without adopting twice or forking history.

Refusals are closed in the other direction too: deletion, replacement (which
includes prepend and any mid-file edit), binary/unreadable diffs, untracked
residue, conflict markers, changes to files outside the allowed bookkeeping
set (cross-folder), dirty governance control files, and any ambiguous/mixed
residue all refuse without mutation.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from fleet_graph.dd.upstream_constants import compute_digest, compute_json_digest

#: What produced a reconciliation record. Stored on the record itself (and bound
#: by its digest) so a downstream evidence link can assert the artifact was
#: produced by this exact mechanism rather than re-typing the name at the site.
RECONCILE_MECHANISM = "WorkFolderReconciler.adopt"

#: Classification verdicts. ``adoptable`` and ``clean`` are the only non-refusals.
CLS_ADOPTABLE = "adoptable"
CLS_CLEAN = "clean"
CLS_REWRITE = "rewrite"
CLS_DELETION = "deletion"
CLS_CONFLICT = "conflict"
CLS_UNTRACKED = "untracked"
CLS_BINARY = "binary"
CLS_CROSS_FOLDER = "cross_folder"
CLS_DIRTY_CONTROL = "dirty_control"
CLS_AMBIGUOUS = "ambiguous"

#: Verdicts that refuse closed: anything that is not a clean, safe append.
_REFUSED_CLASSES = frozenset(
    {
        CLS_REWRITE,
        CLS_DELETION,
        CLS_CONFLICT,
        CLS_UNTRACKED,
        CLS_BINARY,
        CLS_CROSS_FOLDER,
        CLS_DIRTY_CONTROL,
        CLS_AMBIGUOUS,
    }
)

#: The bookkeeping files a governed work folder may safely append to. ``progress.md``
#: is the incident's file; the rest are the durable files a work folder already
#: carries (goal/plan/design/findings/context) so a live folder has a stable,
#: reviewable surface rather than one fixed to a single filename.
ALLOWED_BOOKKEEPING_FILES = frozenset(
    {"progress.md", "findings.md", "goal.md", "plan.md", "design.md", "context.md"}
)

#: Governance control files. A tracked change here is never bookkeeping and never
#: adoptable: it is a dirty governance control file and refuses.
GOVERNANCE_CONTROL_FILES = frozenset({"manifest.json", "control.json", "status.json"})

_CONFLICT_MARKERS = (b"<<<<<<<", b"=======", b">>>>>>>")

#: The three adopted facts a source commits per file: (filename, base, appended).
AppendItem = tuple[str, bytes, bytes]


class ReconcileError(RuntimeError):
    """A work-folder residue cannot be reconciled. Refuse; do not guess."""


@dataclass(frozen=True)
class InspectedFile:
    """One logical file's governed base and working bytes.

    ``base`` is the committed bytes (``None`` when the file has no governed
    history); ``current`` is the working bytes (``None`` when the file was
    deleted). ``tracked`` is whether the file has governed history at all, so a
    brand-new file is an *untracked* residue, not an append to history.
    """

    filename: str
    base: bytes | None
    current: bytes | None
    tracked: bool = True


@dataclass(frozen=True)
class ReconciliationEntry:
    """The mechanical classification of one file's residue, sealed by digest."""

    filename: str
    classification: str
    base_digest: str
    content_digest: str
    appended_digest: str = ""
    appended_size: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "classification": self.classification,
            "base_digest": self.base_digest,
            "content_digest": self.content_digest,
            "appended_digest": self.appended_digest,
            "appended_size": self.appended_size,
        }


class ReconcileSource(Protocol):
    """The seam between the reconciler and the governed work-folder store."""

    def inspect(self, folder_id: str) -> tuple[InspectedFile, ...]: ...

    def adopt(self, folder_id: str, entries: tuple[AppendItem, ...]) -> dict[str, Any]: ...


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data


def _has_conflict_markers(data: bytes) -> bool:
    return any(marker in data for marker in _CONFLICT_MARKERS)


def classify_file(
    filename: str,
    *,
    base: bytes | None,
    current: bytes | None,
    tracked: bool,
    allowed: frozenset[str],
    control: frozenset[str],
) -> str:
    """Classify one file's residue. ``clean``/``adoptable`` are safe, the rest refuse.

    Only a tracked file whose working bytes are a strict extension of its
    governed bytes -- a pure append -- is adoptable. Everything else is named
    precisely so a refusal can say *why* a folder is not safely adoptable.
    """
    if tracked:
        if current is None:
            return CLS_DELETION
        if base is None:
            return CLS_AMBIGUOUS
        if base == current:
            return CLS_CLEAN
        if filename in control:
            return CLS_DIRTY_CONTROL
        if filename not in allowed:
            return CLS_CROSS_FOLDER
        if _is_binary(base) or _is_binary(current):
            return CLS_BINARY
        if _has_conflict_markers(current):
            return CLS_CONFLICT
        if current.startswith(base) and len(current) > len(base):
            return CLS_ADOPTABLE
        return CLS_REWRITE
    if current is not None:
        return CLS_UNTRACKED
    return CLS_CLEAN


def _entries(
    folder_id: str,
    files: Iterable[InspectedFile],
    *,
    allowed: frozenset[str],
    control: frozenset[str],
) -> tuple[ReconciliationEntry, ...]:
    ordered = sorted(files, key=lambda item: item.filename)
    result: list[ReconciliationEntry] = []
    for item in ordered:
        classification = classify_file(
            item.filename,
            base=item.base,
            current=item.current,
            tracked=item.tracked,
            allowed=allowed,
            control=control,
        )
        if classification == CLS_CLEAN:
            continue
        base = item.base if item.base is not None else b""
        current = item.current if item.current is not None else b""
        appended_digest = ""
        appended_size = 0
        if classification == CLS_ADOPTABLE:
            appended = current[len(base) :]
            appended_digest = compute_digest(appended)
            appended_size = len(appended)
        result.append(
            ReconciliationEntry(
                filename=item.filename,
                classification=classification,
                base_digest=compute_digest(base),
                content_digest=compute_digest(current),
                appended_digest=appended_digest,
                appended_size=appended_size,
            )
        )
    return tuple(result)


def _binding(folder_id: str, entries: tuple[ReconciliationEntry, ...]) -> dict[str, Any]:
    return {
        "folder_id": folder_id,
        "entries": [
            {
                "filename": entry.filename,
                "classification": entry.classification,
                "base_digest": entry.base_digest,
                "content_digest": entry.content_digest,
            }
            for entry in entries
        ],
    }


def reconcile_token(folder_id: str, entries: tuple[ReconciliationEntry, ...]) -> str:
    """The confirmation token: a CAS binding over the exact base and bytes.

    A changed base, changed bytes, a changed classification, or a different
    ``folder_id`` all produce a different token, so a stale or mismatched
    confirmation is refused by construction rather than by a side-channel check.
    """
    return compute_json_digest(_binding(folder_id, entries))


def _refusal_text(entries: tuple[ReconciliationEntry, ...]) -> str:
    return ", ".join(f"{e.classification}:{e.filename}" for e in entries)


class WorkFolderReconciler:
    """Classifies residue, plans a dry-run, and adopts only a matching confirmation.

    The ledger keys completed records by token, so a replayed confirmation is
    the same record: nothing is adopted twice and history never forks.
    """

    def __init__(
        self,
        *,
        records: Iterable[dict[str, Any]] = (),
        allowed: frozenset[str] = ALLOWED_BOOKKEEPING_FILES,
        control: frozenset[str] = GOVERNANCE_CONTROL_FILES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._allowed = allowed
        self._control = control
        self._clock = clock
        self._by_token: dict[str, dict[str, Any]] = {}
        for record in records:
            self._by_token[record["token"]] = record

    def _plan_entries(
        self, folder_id: str, files: Iterable[InspectedFile]
    ) -> tuple[ReconciliationEntry, ...]:
        return _entries(folder_id, files, allowed=self._allowed, control=self._control)

    def plan(self, folder_id: str, files: Iterable[InspectedFile]) -> dict[str, Any]:
        """Dry-run: classify and return a stable plan, or refuse. No mutation."""
        entries = self._plan_entries(folder_id, files)
        refusals = tuple(entry for entry in entries if entry.classification in _REFUSED_CLASSES)
        if refusals:
            raise ReconcileError(
                f"work folder {folder_id!r} residue is not safely adoptable "
                f"({_refusal_text(refusals)}); refusing closed"
            )
        return {
            "folder_id": folder_id,
            "token": reconcile_token(folder_id, entries),
            "clean": not entries,
            "entries": [entry.as_dict() for entry in entries],
        }

    def confirm(
        self,
        folder_id: str,
        token: str,
        files: Iterable[InspectedFile],
        *,
        adopt: Callable[[str, tuple[AppendItem, ...]], dict[str, Any]],
    ) -> dict[str, Any]:
        """Governed adoption, bound to the exact dry-run plan, idempotent on replay."""
        existing = self._by_token.get(token)
        if existing is not None:
            return existing
        entries = self._plan_entries(folder_id, files)
        if token != reconcile_token(folder_id, entries):
            raise ReconcileError(
                "confirmation token does not bind to the current base or bytes; "
                "refusing without mutation (stale confirmation, changed base, or wrong folder)"
            )
        refusals = tuple(entry for entry in entries if entry.classification in _REFUSED_CLASSES)
        if refusals:
            raise ReconcileError(
                f"work folder {folder_id!r} residue is not safely adoptable "
                f"({_refusal_text(refusals)}); refusing without mutation"
            )
        if not entries:
            return self._seal(folder_id, token, (), {})

        by_name = {item.filename: item for item in files}
        append_items: list[AppendItem] = []
        for entry in entries:
            if entry.classification != CLS_ADOPTABLE:
                continue
            item = by_name[entry.filename]
            base = item.base if item.base is not None else b""
            current = item.current if item.current is not None else b""
            append_items.append((entry.filename, base, current[len(base) :]))
        receipt = adopt(folder_id, tuple(append_items))
        return self._seal(folder_id, token, tuple(append_items), receipt)

    def _seal(
        self,
        folder_id: str,
        token: str,
        append_items: tuple[AppendItem, ...],
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        adopted = [
            {
                "filename": filename,
                "base_digest": compute_digest(base),
                "appended_digest": compute_digest(appended),
                "appended_utf8_bytes": len(appended),
            }
            for filename, base, appended in append_items
        ]
        record: dict[str, Any] = {
            "folder_id": folder_id,
            "token": token,
            "mechanism": RECONCILE_MECHANISM,
            "digest": compute_json_digest(
                {"folder_id": folder_id, "token": token, "mechanism": RECONCILE_MECHANISM}
            ),
            "adopted": adopted,
            "receipt": receipt,
            "at": _iso_timestamp(self._clock()),
        }
        self._by_token[token] = record
        return record

    def records(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._by_token.values())


def _iso_timestamp(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


__all__ = [
    "ALLOWED_BOOKKEEPING_FILES",
    "CLS_ADOPTABLE",
    "CLS_AMBIGUOUS",
    "CLS_BINARY",
    "CLS_CLEAN",
    "CLS_CONFLICT",
    "CLS_CROSS_FOLDER",
    "CLS_DELETION",
    "CLS_DIRTY_CONTROL",
    "CLS_REWRITE",
    "CLS_UNTRACKED",
    "GOVERNANCE_CONTROL_FILES",
    "RECONCILE_MECHANISM",
    "AppendItem",
    "InspectedFile",
    "ReconcileError",
    "ReconcileSource",
    "ReconciliationEntry",
    "WorkFolderReconciler",
    "classify_file",
    "reconcile_token",
]
