"""Scoping the committed feedback index for a new generation.

The pinned carrier applies a flat, generation-unaware ordering rule to
`.dev-dispatch/feedback/index.json` at the review seal: a fresh continuous
review is a brand-new attempt, legal only as the chain's very first entry or
the entry right after a REJECT. A new generation's fresh attempt would
otherwise be misread as "the next attempt of the previous generation's chain",
so a valid later generation's continuous review is refused with the reported
ORDER_VIOLATION.

The fix is mechanical and preserves history. At the start of a new generation
the engine splits the committed index into the current generation's entries and
the inherited older-generation entries, moves the inherited entries into an
append-only archive (`.dev-dispatch/feedback/history.json`), and re-seeds the
live index with the empty chain. The archive is the durable feedback history
the spec requires be preserved; the live index is the chain the carrier
validates.

Membership is keyed on each entry's durable `attempt_id` -- the uuid5 the
engine itself derived from `(development_id, generation, attempt)` -- so it is
a bounded forward search over attempts, never a reverse of the derivation, and
never a guess from prose.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fleet_graph.dd import chain_rules
from fleet_graph.dd.bootstrap import HISTORY_PATH, INDEX_PATH, canonical_bytes
from fleet_graph.dd.git import run_git
from fleet_graph.dd.upstream_constants import ATTEMPT_CONTEXT_CONTRACT_VERSION


def _committed_object(workspace: Path, path: str) -> dict[str, Any] | None:
    """The committed JSON object at `path` on HEAD, or None."""
    proc = run_git(workspace, "show", f"HEAD:{path}")
    if proc.returncode != 0:
        return None
    try:
        value = json.loads(proc.stdout)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _dedupe(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable, idempotent: one entry per `review_id`, first occurrence wins."""
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for entry in entries:
        key = entry.get("review_id")
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return result


def scope_index_for_generation(workspace: Path, *, generation: int, development_id: str) -> bool:
    """Scope the committed feedback index to `generation`, archiving the rest.

    Returns True when the worktree was changed (the archive grew and the live
    index was re-seeded empty), False when there is nothing to do: generation 1
    already carries the bootstrap's empty index, and a later generation whose
    committed index holds no older-generation entries is already scoped.

    The two writes use the plugin's canonical serialisation (``canonical_bytes``)
    because the carrier re-reads the index and re-checks it byte-for-byte; a
    non-canonical re-seed would fail BINDING_MISMATCH on the next review seal.
    """
    if generation <= 1:
        return False
    index = _committed_object(workspace, INDEX_PATH)
    if index is None:
        return False
    entries = index.get("entries")
    if not isinstance(entries, list):
        return False
    _own, inherited = chain_rules.split_entries(
        entries, generation=generation, development_id=development_id
    )
    if not inherited:
        return False

    history = _committed_object(workspace, HISTORY_PATH)
    existing = history.get("entries") if history is not None else None
    merged = _dedupe([*(existing if isinstance(existing, list) else []), *inherited])
    history_payload = {
        "contract_version": ATTEMPT_CONTEXT_CONTRACT_VERSION,
        "development_id": development_id,
        "entries": merged,
    }
    (workspace / HISTORY_PATH).parent.mkdir(parents=True, exist_ok=True)
    (workspace / HISTORY_PATH).write_bytes(canonical_bytes(history_payload))

    index_payload = {
        "contract_version": ATTEMPT_CONTEXT_CONTRACT_VERSION,
        "development_id": development_id,
        "entries": [],
    }
    (workspace / INDEX_PATH).parent.mkdir(parents=True, exist_ok=True)
    (workspace / INDEX_PATH).write_bytes(canonical_bytes(index_payload))
    return True


__all__ = ["scope_index_for_generation"]
