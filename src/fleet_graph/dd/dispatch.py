"""Building the StageDispatch the plugin sealer actually consumes.

The walker carries a small dispatch: which stage, which attempt, which commit.
The plugin's `stage-dispatch.schema.json` wants twelve fields, and the seven
it wants that the walker does not track are forward-chain bookkeeping --
`attempt_id`, `target_base_commit`, `expected_remote_head`,
`parent_handoff_receipt_digest`, `spec_ref`, `feedback_ref`,
`materialization_intent_id`. This module is where those come from.

**Where they come from matters more than that they exist.** Each one is either
read out of git at the input commit, derived deterministically, or carried on
the chain -- none is invented:

- `spec_ref` / `feedback_ref` are the committed blobs' own identities, read
  through the vendored `exact_artifact_identity`. The feedback index is also
  checked to bind this development and this protocol, because an index
  belonging to another development would silently re-point the whole chain.
- `expected_remote_head` equals `input_commit`, which is what upstream does.
  It is not a live `ls-remote`: the remote is checked elsewhere, against this
  field, and having the field read the remote would make that check compare
  the remote to itself.
- `parent_handoff_receipt_digest` is the canonical-JSON digest of the previous
  stage's receipt -- the actual forward link. The chain's root value stands in
  for the first stage, which has no predecessor.
- `attempt_id` and `materialization_intent_id` are derived (uuid5), not
  random. A retry of the same attempt therefore freezes the *same* intent
  rather than forking a second one, which is the same reason run ids are
  derived in `executors/agent_run.py`.

**This is the boundary where rewriting beats vendoring.** Upstream's version of
this lives in `handoff.py` (2171 lines) on top of `models.py` (1111), and what
it produces is a dict conforming to a schema this repo already ships. Bringing
3.3k lines across to build a twelve-key dict would be the tail wagging the dog
-- which is exactly the `编排壳重写、领域资产库化` split plan.md called. So the
sixty lines that matter are restated here, and a test validates the result
against the plugin's own schema rather than against my reading of it.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from fleet_graph.dd.capability import CONTRACTS_DIR
from fleet_graph.dd.upstream_constants import (
    ATTEMPT_CONTEXT_CONTRACT_VERSION,
    compute_json_digest,
)
from fleet_graph.dd.vendor import git_ops

DISPATCH_SCHEMA_PATH = CONTRACTS_DIR / "stage-dispatch.schema.json"
ARTIFACTS_PATH = CONTRACTS_DIR / "stage-artifacts.json"

# uuid5 namespaces. Fixed values, so the derivation is stable across processes
# and across restarts -- a namespace that moved would fork every id.
ATTEMPT_ID_NAMESPACE = uuid.UUID("6f2a1c30-9d4b-5e7a-8c11-2b7d4e6f8a90")
INTENT_ID_NAMESPACE = uuid.UUID("1e5c7b42-3a86-5d19-9f04-6c8e2a5b7d31")

FEEDBACK_INDEX_FIELDS = frozenset({"contract_version", "development_id", "entries"})


class DispatchError(RuntimeError):
    """The dispatch cannot be built as declared. Nothing is guessed."""


def derive_attempt_id(development_id: str, generation: int, attempt: int) -> str:
    return str(uuid.uuid5(ATTEMPT_ID_NAMESPACE, f"{development_id}\x1f{generation}\x1f{attempt}"))


def derive_intent_id(
    development_id: str, stage: str, generation: int, attempt: int, input_commit: str
) -> str:
    """Derived so a retry freezes the same intent instead of forking one."""
    return str(
        uuid.uuid5(
            INTENT_ID_NAMESPACE,
            f"{development_id}\x1f{stage}\x1f{generation}\x1f{attempt}\x1f{input_commit}",
        )
    )


@dataclass(frozen=True)
class DevelopmentChain:
    """The per-development facts a single stage dispatch cannot derive itself."""

    development_id: str
    workspace_path: str
    target_base_commit: str
    root_handoff_digest: str


@dataclass(frozen=True)
class CommittedRefs:
    spec: dict[str, Any]
    feedback: dict[str, Any]
    #: The feedback index's committed review entries, in committed order. Kept
    #: separately from `feedback` because `feedback_ref`'s schema admits only
    #: `path/blob_oid/digest/entry_count` (additionalProperties: false); the
    #: generation-aware ordering guard needs the entries themselves.
    entries: tuple[dict[str, Any], ...] = ()


def read_committed_refs(
    workspace_path: str, input_commit: str, development_id: str, *, spec_path: str, index_path: str
) -> CommittedRefs:
    """Read the spec and feedback identities out of the commit itself.

    Restated from upstream `resolve_committed_dispatch_refs`, keeping the two
    bindings that do real work: a feedback index from another protocol or
    another development would otherwise re-point the chain without anything
    noticing.
    """
    try:
        spec = git_ops.exact_artifact_identity(workspace_path, input_commit, spec_path)
        feedback = git_ops.exact_artifact_identity(workspace_path, input_commit, index_path)
        # The private call is deliberate: `_command_text` runs git with the
        # hooks/fsmonitor/ext-protocol guards that make an inherited
        # environment harmless (see test_dd_vendor). Restating that argument
        # list here is how it drifts. A test pins the name.
        raw = git_ops._command_text(
            "cat-file", "blob", feedback["blob_oid"], worktree=workspace_path
        )
    except git_ops.ExactWorkspaceError as exc:
        raise DispatchError(f"cannot read committed refs at {input_commit}: {exc}") from exc

    try:
        index = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DispatchError(f"{index_path} at {input_commit} is not JSON: {exc}") from exc
    if not isinstance(index, dict) or set(index) != FEEDBACK_INDEX_FIELDS:
        raise DispatchError(
            f"{index_path} must carry exactly {sorted(FEEDBACK_INDEX_FIELDS)}, "
            f"got {sorted(index) if isinstance(index, dict) else type(index).__name__}"
        )
    if index["contract_version"] != ATTEMPT_CONTEXT_CONTRACT_VERSION:
        raise DispatchError(f"{index_path} uses protocol {index['contract_version']!r}")
    if index["development_id"] != development_id:
        raise DispatchError(
            f"{index_path} binds development {index['development_id']!r}, not {development_id!r}"
        )
    entries = index["entries"]
    if not isinstance(entries, list):
        raise DispatchError(f"{index_path} entries must be an array")

    return CommittedRefs(
        spec=spec,
        feedback={**feedback, "entry_count": len(entries)},
        entries=tuple(entries),
    )


class StageDispatchBuilder:
    """Turns the walker's dispatch into the plugin's twelve-field object."""

    def __init__(
        self,
        chain: DevelopmentChain,
        *,
        schema_path: Path = DISPATCH_SCHEMA_PATH,
        artifacts_path: Path = ARTIFACTS_PATH,
    ) -> None:
        self.chain = chain
        self._schema_path = schema_path
        self._artifacts_path = artifacts_path

    @cached_property
    def _schema(self) -> dict[str, Any]:
        return json.loads(self._schema_path.read_text(encoding="utf-8"))

    @cached_property
    def required_fields(self) -> frozenset[str]:
        return frozenset(self._schema["required"])

    @cached_property
    def allowed_stages(self) -> frozenset[str]:
        """Read from the schema: the sealer serves fewer stages than the graph has."""
        return frozenset(self._schema["properties"]["stage"]["enum"])

    @cached_property
    def allowed_modes(self) -> frozenset[str]:
        return frozenset(self._schema["properties"]["mode"]["enum"])

    def _resolve(self, ref: str) -> dict[str, Any]:
        """Follow one relative `$ref` into a sibling contract file.

        The dispatch schema does not spell the artifact paths inline; it points
        at `attempt-context.schema.json`. Following the pointer keeps the paths
        coming from the contract rather than from a constant here that would
        have to be kept in step with it.
        """
        filename, _, pointer = ref.partition("#")
        document = json.loads((self._schema_path.parent / filename).read_text(encoding="utf-8"))
        node: Any = document
        for part in [p for p in pointer.split("/") if p]:
            node = node[part.replace("~1", "/").replace("~0", "~")]
        if not isinstance(node, dict):
            raise DispatchError(f"{ref} does not resolve to an object")
        return node

    def _const_path(self, field: str) -> str:
        definition = self._resolve(self._schema["properties"][field]["$ref"])
        return str(definition["properties"]["path"]["const"])

    @cached_property
    def spec_path(self) -> str:
        return self._const_path("spec_ref")

    @cached_property
    def index_path(self) -> str:
        return self._const_path("feedback_ref")

    def serves(self, stage: str) -> bool:
        return stage in self.allowed_stages

    def build(
        self,
        dispatch: dict[str, Any],
        *,
        parent_receipt: dict[str, Any] | None = None,
        parent_digest: str | None = None,
    ) -> dict[str, Any]:
        stage = str(dispatch.get("stage", ""))
        if stage not in self.allowed_stages:
            raise DispatchError(
                f"the sealer serves {sorted(self.allowed_stages)}; {stage!r} is not one of them"
            )
        mode = str(dispatch.get("mode", ""))
        if mode not in self.allowed_modes:
            raise DispatchError(f"mode must be one of {sorted(self.allowed_modes)}, got {mode!r}")
        input_commit = str(dispatch.get("input_commit", ""))
        if git_ops._FULL_COMMIT_RE.fullmatch(input_commit) is None:
            raise DispatchError(
                f"input_commit must be one full 40-hex object id, got {input_commit!r}"
            )

        generation = int(dispatch.get("generation", 1))
        attempt = int(dispatch.get("attempt", 1))
        # The attempt identity: derived from (generation, attempt) as always,
        # unless a replayed prefix pinned the identity its receipts were
        # sealed under. The pin travels on the walker's own state -- it is
        # set only from a replay-verified receipt body, never from an actor's
        # claim -- and every binding the sealer enforces still runs: it reads
        # the parent receipt at exactly this identity and refuses one whose
        # embedded identity differs. What changes is where the *expected*
        # identity comes from, not what is checked against it.
        attempt_id = str(dispatch.get("pinned_attempt_id") or "") or derive_attempt_id(
            self.chain.development_id, generation, attempt
        )
        refs = read_committed_refs(
            self.chain.workspace_path,
            input_commit,
            self.chain.development_id,
            spec_path=self.spec_path,
            index_path=self.index_path,
        )

        payload = {
            "attempt_id": attempt_id,
            "contract_version": ATTEMPT_CONTEXT_CONTRACT_VERSION,
            "development_id": self.chain.development_id,
            # Upstream sets this to the input commit rather than reading the
            # remote. The remote is checked *against* this field elsewhere;
            # reading it here would make that check compare the remote to itself.
            "expected_remote_head": input_commit,
            "feedback_ref": refs.feedback,
            "input_commit": input_commit,
            "materialization_intent_id": derive_intent_id(
                self.chain.development_id, stage, generation, attempt, input_commit
            ),
            "mode": mode,
            "parent_handoff_receipt_digest": parent_digest or self.parent_digest(parent_receipt),
            "spec_ref": refs.spec,
            "stage": stage,
            "target_base_commit": self.chain.target_base_commit,
        }

        missing = self.required_fields - set(payload)
        extra = set(payload) - self.required_fields
        if missing or extra:
            raise DispatchError(
                f"built dispatch does not match the schema's field set: "
                f"missing {sorted(missing)}, unexpected {sorted(extra)}"
            )
        return payload

    def parent_digest(self, parent_receipt: dict[str, Any] | None) -> str:
        """The forward link. The chain root stands in for the first stage."""
        if parent_receipt:
            return compute_json_digest(parent_receipt)
        return self.chain.root_handoff_digest


__all__ = [
    "ARTIFACTS_PATH",
    "DISPATCH_SCHEMA_PATH",
    "CommittedRefs",
    "DevelopmentChain",
    "DispatchError",
    "StageDispatchBuilder",
    "derive_attempt_id",
    "derive_intent_id",
    "read_committed_refs",
]
