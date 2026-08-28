"""Writing the attempt context a development starts from.

dd calls this "the deterministic bootstrap primitive", and until now I had been
hand-writing its output into a fixture -- which is how three of the last four
contract refusals were found the expensive way. An operator should not be
hand-writing it either.

Four files, in the worktree, committed:

- `.dev-dispatch/spec/approved.md` -- the approved spec, immutable from here on
- `.dev-dispatch/spec/manifest.json` -- binds that path, digest and size
- `.dev-dispatch/feedback/index.json` -- empty to begin with; the only
  feedback history the chain will admit
- `.dev-dispatch/development.json` -- the identity every later stage is
  checked against

**Canonical bytes, not merely equivalent JSON.** The sealer re-serialises what
it reads and compares byte for byte, so key order, separators and the trailing
newline are part of the contract rather than formatting. The field sets are
exact in both directions -- a missing field and an extra one fail the same
way -- which is why they are pinned against the plugin's own constants by test
instead of being remembered.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fleet_graph.dd.upstream_constants import ATTEMPT_CONTEXT_CONTRACT_VERSION

SPEC_PATH = ".dev-dispatch/spec/approved.md"
SPEC_MANIFEST_PATH = ".dev-dispatch/spec/manifest.json"
INDEX_PATH = ".dev-dispatch/feedback/index.json"
# The append-only cross-generation feedback archive. The live index is scoped to
# the current generation's attempt chain (what the carrier's generation-unaware
# ordering rule validates); entries that belong to an older generation are moved
# here, never dropped, so the complete feedback history stays readable.
HISTORY_PATH = ".dev-dispatch/feedback/history.json"
DEVELOPMENT_PATH = ".dev-dispatch/development.json"

DEVELOPMENT_FIELDS = frozenset(
    {
        "contract_version",
        "development_id",
        "feedback_index_path",
        "spec_digest",
        "spec_manifest_path",
        "spec_path",
        "target_base_commit",
    }
)
SPEC_MANIFEST_FIELDS = frozenset(
    {"contract_version", "development_id", "spec_digest", "spec_path", "spec_size_bytes"}
)
INDEX_FIELDS = frozenset({"contract_version", "development_id", "entries"})


class BootstrapError(RuntimeError):
    """The attempt context cannot be written as asked."""


class IdentityChanged(RuntimeError):
    """The committed development identity is not the one bootstrap wrote."""


def committed_target_base(worktree: Path, *, revision: str = "HEAD") -> str | None:
    """The `target_base_commit` the development already committed, if any.

    Bootstrap freezes the commit the spec was approved against; by the time a
    run starts, HEAD has moved past it (the bootstrap commit itself, at
    least). A run that re-derived this from HEAD would hand the review sealer
    a base the committed identity never named, and it refuses that with
    BINDING_MISMATCH -- correctly. So this is read, not guessed.

    **And it is read only if nobody has edited it since.** The identity lives
    in the worktree, and the implementer's role grants `write:
    [worktree_path]`. If a run derived its dispatch from an edited identity,
    the sealer's check -- committed identity equals dispatch -- would compare
    the agent's file against itself: always true, worth nothing. That is the
    same shape as the `expected_remote_head` mistake in findings §21a.

    Raises `IdentityChanged` rather than returning the edited value. The
    operator can still say `--target-base` explicitly; what is refused is
    *inferring* it from something the graded party can rewrite.
    """
    from fleet_graph.dd.git import run_git

    found = run_git(worktree, "show", f"{revision}:{DEVELOPMENT_PATH}")
    if found.returncode != 0:
        return None
    _refuse_if_edited_since_bootstrap(worktree, found.stdout)
    try:
        identity = json.loads(found.stdout)
    except ValueError:
        return None
    base = identity.get("target_base_commit")
    return base if isinstance(base, str) and base else None


def _refuse_if_edited_since_bootstrap(worktree: Path, current: str) -> None:
    """Compare the identity at HEAD with the one in the commit that added it.

    The introducing commit is the anchor because an agent cannot change it
    after the fact: rewriting history would change every descendant hash, and
    the chain the sealer verifies is built on those hashes.
    """
    from fleet_graph.dd.git import run_git

    history = run_git(worktree, "log", "--diff-filter=A", "--format=%H", "--", DEVELOPMENT_PATH)
    introduced = [line for line in history.stdout.split() if line]
    if not introduced:
        return
    original = run_git(worktree, "show", f"{introduced[-1]}:{DEVELOPMENT_PATH}")
    if original.returncode != 0:
        return
    if original.stdout != current:
        raise IdentityChanged(
            f"{DEVELOPMENT_PATH} has been edited since bootstrap ({introduced[-1][:12]}); "
            "refusing to derive the target base from it. Pass --target-base explicitly "
            "if this change is intended"
        )


def canonical_bytes(value: Any) -> bytes:
    """Exactly what the plugin re-serialises with, trailing newline included."""
    data = (
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        + "\n"
    )
    return data.encode("utf-8")


def digest_of(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class AttemptContext:
    """The four files, as bytes, ready to be written and committed."""

    files: dict[str, bytes]

    @property
    def spec_digest(self) -> str:
        return digest_of(self.files[SPEC_PATH])

    def write(self, worktree: Path) -> list[Path]:
        written = []
        for relative, payload in sorted(self.files.items()):
            path = worktree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            written.append(path)
        return written


def build_attempt_context(
    *, development_id: str, spec: bytes, target_base_commit: str
) -> AttemptContext:
    if not development_id:
        raise BootstrapError("a development needs an id")
    if not spec.strip():
        raise BootstrapError("the approved spec is empty")
    if len(target_base_commit) != 40 or not all(
        c in "0123456789abcdef" for c in target_base_commit
    ):
        raise BootstrapError(
            f"target_base_commit must be one full lowercase 40-hex id, got {target_base_commit!r}"
        )

    spec_digest = digest_of(spec)
    manifest = {
        "contract_version": ATTEMPT_CONTEXT_CONTRACT_VERSION,
        "development_id": development_id,
        "spec_digest": spec_digest,
        "spec_path": SPEC_PATH,
        "spec_size_bytes": len(spec),
    }
    index = {
        "contract_version": ATTEMPT_CONTEXT_CONTRACT_VERSION,
        "development_id": development_id,
        "entries": [],
    }
    development = {
        "contract_version": ATTEMPT_CONTEXT_CONTRACT_VERSION,
        "development_id": development_id,
        "feedback_index_path": INDEX_PATH,
        "spec_digest": spec_digest,
        "spec_manifest_path": SPEC_MANIFEST_PATH,
        "spec_path": SPEC_PATH,
        "target_base_commit": target_base_commit,
    }

    for value, fields, label in (
        (manifest, SPEC_MANIFEST_FIELDS, "spec manifest"),
        (index, INDEX_FIELDS, "feedback index"),
        (development, DEVELOPMENT_FIELDS, "development identity"),
    ):
        # Exact in both directions: a missing field and an extra one fail the
        # sealer the same way, so neither is allowed to leave here.
        if set(value) != set(fields):
            raise BootstrapError(f"{label} field set is wrong: {sorted(set(value))}")

    return AttemptContext(
        files={
            SPEC_PATH: spec,
            SPEC_MANIFEST_PATH: canonical_bytes(manifest),
            INDEX_PATH: canonical_bytes(index),
            DEVELOPMENT_PATH: canonical_bytes(development),
        }
    )


__all__ = [
    "DEVELOPMENT_FIELDS",
    "DEVELOPMENT_PATH",
    "HISTORY_PATH",
    "INDEX_FIELDS",
    "INDEX_PATH",
    "SPEC_MANIFEST_FIELDS",
    "SPEC_MANIFEST_PATH",
    "SPEC_PATH",
    "AttemptContext",
    "BootstrapError",
    "IdentityChanged",
    "build_attempt_context",
    "canonical_bytes",
    "committed_target_base",
    "digest_of",
]
