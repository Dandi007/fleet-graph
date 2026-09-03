"""Sealing a stage: the plugin's materializer, invoked as upstream invokes it.

`dd/dispatch.py` builds the twelve-field StageDispatch; this wraps it in the
materialization request the plugin's `materialize-handoff` script consumes and
reads back what it produced. Everything security-relevant on this path already
exists upstream and is reused rather than reimplemented: the vendored
`invoke_*_materializer` verifies the pinned plugin capability before it runs
anything, and refuses a result that is neither a receipt nor an exact failure.

Three things this module is careful about.

**The request is frozen, and freezing must be reproducible.** The commit
metadata uses the attempt's own start time, carried on the dispatch, rather
than the clock at materialize time. A retry of the same attempt therefore
builds a byte-identical request -- same canonical JSON, same digest, same
derived intent id -- which is what makes re-sealing idempotent instead of
producing a second, differently-timestamped commit for the same work.

**A non-applied receipt is not a failure.** DISPUTED and BLOCKED carry no
`output_commit` at all, because a no-op produces no commit. Upstream
terminalises them rather than reworking, and so does this: `StageRefused`,
which the walker records as a refusal rather than a fault. Nothing broke.

**Failure codes come back untranslated.** The plugin returns codes from the
contract's own taxonomy along with its own `retryable` flag; inventing a code
here, or overriding that flag, would decide retry policy on behalf of a
contract that already states it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from fleet_graph.dd import chain_rules
from fleet_graph.dd.capability import CONTRACTS_DIR
from fleet_graph.dd.dispatch import (
    DispatchError,
    StageDispatchBuilder,
    derive_attempt_id,
    read_committed_refs,
)
from fleet_graph.dd.lifecycle import Lifecycle, Stage
from fleet_graph.dd.mutation import (
    CHECKED_ITEMS_FIELD,
    VERIFIED_ITEMS_FIELD,
)
from fleet_graph.dd.vendor import plugin_adapter
from fleet_graph.graphs.dd_actors import (
    implement_stage,
    review_stages,
)
from fleet_graph.graphs.dd_pipeline import Dispatch, Sealed, StageOutcome, StageRefused
from fleet_graph.state.run_artifacts import write_json_durable

# Upstream's identity for the commits the sealer writes. Deliberately not a
# routable address, and deliberately not the operator's: these commits are
# written by a machine on a contract's behalf.
AUTHOR_NAME = "Dev Dispatch"
AUTHOR_EMAIL = "dev-dispatch@example.invalid"

# `acceptance` appears in the dispatch schema's stage enum, so "served by the
# sealer" is *not* the same set as "appears in a dispatch". The plugin ships
# exactly two materializers; which stages own their outputs comes from the
# contract, via the helpers in dd_actors.

# Where the plugin's sealer writes the Implement receipt, relative to the
# attempt's receipts directory under the state root.
IMPLEMENT_RECEIPT_FILE = "implement-receipt.json"

# The engine-owned sidecar the review materializer writes beside the sealed
# receipt, same directory. The plugin's review-result schema is pinned
# externally with ``additionalProperties: false``, so the reviewer's mandatory
# checklist (S12.5) and the final_review mutation receipt (S12.3) physically
# cannot travel inside the sealed review.json -- they travel here instead,
# written by the engine the moment the stage seals, so the durable record of
# what was checked (and what fell red) survives past the process. The spec's
# own words: "落 review.json 或其旁挂工件" -- this is the sidecar artifact.
REVIEW_SIDECAR_FILE = {
    "continuous": "continuous-review-sidecar.json",
    "final": "final-review-sidecar.json",
}

# Whose sealed receipt each stage's dispatch must name as its parent. The
# digest is over the file's bytes, not over an equivalent object: the sealer
# re-reads exactly those bytes. Implement has no predecessor, so its parent is
# the chain root the caller supplied.
PARENT_RECEIPT_FILE = {
    "continuous_review": IMPLEMENT_RECEIPT_FILE,
    "final_review": "continuous-review-receipt.json",
}

APPLIED = "APPLIED"
NON_APPLIED_OUTCOMES = ("DISPUTED", "BLOCKED")
NON_APPLIED_DETAIL_FIELD = {"DISPUTED": "rebuttal", "BLOCKED": "blocker"}

# The fields `implement.output.schema.json` admits. It sets
# `additionalProperties: false`, so anything else the role happens to return --
# `effects`, say -- has to be dropped rather than forwarded.
IMPLEMENT_ACTOR_FIELDS = (
    "actor_job_id",
    "input_commit",
    "outcome",
    "work_head_commit",
    "rebuttal",
    "blocker",
    "verification_record",
)
# What the plugin requires regardless of outcome. `outcome` is deliberately not
# here: the vendored adapter defaults a legacy three-field result to APPLIED,
# so demanding it earlier refuses a result that bridge exists to accept.
IMPLEMENT_REQUIRED_FIELDS = ("actor_job_id", "input_commit")
APPLIED_EXTRA_FIELDS = ("work_head_commit", "verification_record")


class MaterializationFailed(RuntimeError):
    """The sealer refused or could not finish. The code is the contract's."""

    def __init__(self, failure_code: str, detail: str, *, retryable: bool = False) -> None:
        super().__init__(f"{failure_code}: {detail}")
        self.failure_code = failure_code
        self.detail = detail
        self.retryable = retryable


class UnservedStage(MaterializationFailed):
    """This sealer does not serve this stage. Refuse rather than pass through."""

    def __init__(self, stage_id: str) -> None:
        super().__init__(
            "PLUGIN_CONTRACT_MISMATCH",
            f"the plugin sealer does not serve stage {stage_id!r}",
        )


@dataclass(frozen=True)
class MaterializationTarget:
    """Where the sealer writes, and from which workspace."""

    remote_url: str
    remote_ref: str
    worktree: str
    state_root: str


@lru_cache(maxsize=1)
def review_result_fields() -> frozenset[str]:
    """The keys `review-result.schema.json` admits, read from the schema itself.

    It sets `additionalProperties: false`, and the reviewer's persona tells it
    to answer "with `effects: []`" -- agent-runtime's envelope convention, which
    ends up inside the result object and which the plugin does not admit. Read
    from the contract rather than listed here, so a contract that grows a field
    does not need this to be remembered.
    """
    schema = json.loads((CONTRACTS_DIR / "review-result.schema.json").read_text(encoding="utf-8"))
    return frozenset(schema["properties"])


def receipt_digest(state_root: str, attempt_id: str, filename: str, *, label: str) -> str:
    """sha256 of a sealed receipt's bytes, as the sealer re-reads them."""
    path = Path(state_root) / "receipts" / attempt_id / filename
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise MaterializationFailed(
            "HANDOFF_CHAIN_MISMATCH", f"no sealed {label} at {path}: {exc}"
        ) from exc
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def review_actor_result(declared: dict[str, Any]) -> dict[str, Any]:
    """The reviewer's declared result, narrowed to what the plugin admits.

    The narrowing exists because the plugin enforces it (a result with an
    unadmitted key is a ``SCHEMA_VIOLATION`` at the seal, not a repairable
    nit) -- which is also why the checklist the narrowing drops is persisted
    engine-side by :func:`write_review_sidecar` instead of forwarded.
    """
    admitted = review_result_fields()
    return {key: value for key, value in declared.items() if key in admitted}


def review_checklist(declared: dict[str, Any]) -> dict[str, list[str]]:
    """The reviewer's mandatory checklist, normalized to two string lists.

    ``checked_items`` is the schema-level name and ``verified_items`` its
    alias; whichever the reviewer answered with lands under both keys of the
    sidecar, so a reader never has to know which spelling a phase used.
    """
    items = declared.get(CHECKED_ITEMS_FIELD)
    alias = declared.get(VERIFIED_ITEMS_FIELD)
    effective = items if isinstance(items, list) and items else alias
    if not isinstance(effective, list):
        effective = []
    strings = [str(item) for item in effective if isinstance(item, str) and item.strip()]
    return {CHECKED_ITEMS_FIELD: list(strings), VERIFIED_ITEMS_FIELD: list(strings)}


def review_sidecar_path(state_root: str | Path, attempt_id: str, review_phase: str) -> Path:
    """Where a review stage's sidecar artifact lives for this attempt."""
    name = REVIEW_SIDECAR_FILE.get(review_phase)
    if name is None:
        raise MaterializationFailed(
            "INVALID_HANDOFF_SCHEMA", f"no review sidecar for phase {review_phase!r}"
        )
    return Path(state_root) / "receipts" / attempt_id / name


def write_review_sidecar(
    state_root: str | Path,
    attempt_id: str,
    review_phase: str,
    checklist: dict[str, list[str]],
    mutation_receipt: dict[str, Any] | None = None,
) -> Path:
    """Persist what the sealed review checked -- the engine's own record.

    The plugin's sealed review.json cannot carry the checklist (pinned schema)
    and never hears about the mutation receipt, so the materializer writes
    both here, durably, the moment the stage seals. Fail-closed: a sidecar
    that cannot be written fails the stage rather than leaving a review whose
    checked-record survives only in process memory.
    """
    payload: dict[str, Any] = {
        "attempt_id": attempt_id,
        "review_phase": review_phase,
        CHECKED_ITEMS_FIELD: list(checklist.get(CHECKED_ITEMS_FIELD) or []),
        VERIFIED_ITEMS_FIELD: list(checklist.get(VERIFIED_ITEMS_FIELD) or []),
        "mutation_receipt": mutation_receipt,
    }
    return write_json_durable(review_sidecar_path(state_root, attempt_id, review_phase), payload)


def load_mutation_receipt(
    state_root: str | Path, attempt_id: str, *, review_phase: str = "final"
) -> dict[str, Any] | None:
    """Read the final_review mutation receipt back from its sidecar artifact.

    This is the production channel the verifying side reads: the gate loads
    the receipt the engine's final_review stage produced and verifies it
    against its own mechanical enumeration -- it never re-runs the gun.
    ``None`` means no receipt was persisted, which duty 5 refuses.
    """
    path = review_sidecar_path(state_root, attempt_id, review_phase)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    receipt = payload.get("mutation_receipt")
    return dict(receipt) if isinstance(receipt, dict) else None


def implement_actor_result(receipt: dict[str, Any]) -> dict[str, Any]:
    """The actor's declared result, narrowed to what the plugin admits.

    Two jobs, and only two. It drops fields the plugin's
    `additionalProperties: false` would reject -- including, for a non-applied
    outcome, an honestly redundant `work_head_commit` that equals the
    `input_commit` -- and it refuses a result whose *declared* outcome is
    missing the evidence that outcome owes (or carries evidence that
    contradicts it).

    What it deliberately does not do is demand more than the plugin does. An
    `implement.result.v1` from agent-runtime carries three fields and no
    `outcome`, and the vendored adapter defaults exactly that shape to APPLIED.
    Requiring `outcome` here refused a result the bridge was written to accept,
    and named a missing field the caller could have done nothing about -- the
    real gap was one field further on.
    """
    missing = [f for f in IMPLEMENT_REQUIRED_FIELDS if not receipt.get(f)]
    if missing:
        raise MaterializationFailed(
            "INVALID_HANDOFF_SCHEMA",
            f"implement actor result is missing {sorted(missing)}",
        )
    result = {f: receipt[f] for f in IMPLEMENT_ACTOR_FIELDS if receipt.get(f) is not None}

    declared = receipt.get("outcome")
    if declared is None:
        return result
    outcome = str(declared)

    if outcome == APPLIED:
        absent = [f for f in APPLIED_EXTRA_FIELDS if receipt.get(f) is None]
        if absent:
            raise MaterializationFailed(
                "INVALID_HANDOFF_SCHEMA",
                f"an APPLIED implement result must carry {sorted(absent)}",
            )
        return result

    if outcome in NON_APPLIED_OUTCOMES:
        field = NON_APPLIED_DETAIL_FIELD[outcome]
        if receipt.get(field) is None:
            raise MaterializationFailed(
                "INVALID_HANDOFF_SCHEMA", f"a {outcome} implement result must carry {field!r}"
            )
        declared_head = receipt.get("work_head_commit")
        if declared_head is not None:
            # An honest no-op reports the head it finished on, which for a
            # no-op equals the input it started from. The plugin's non-applied
            # schema does not admit the field (measured on
            # dev-fg-4628ef887564 g3: INVALID_INPUT "unknown semantic
            # fields"), so a *consistent* value is checked and then dropped
            # rather than forwarded. An inconsistent one is a claim that a
            # no-op moved the head, which is refused, not repaired.
            if str(declared_head) != str(receipt.get("input_commit")):
                raise MaterializationFailed(
                    "INVALID_HANDOFF_SCHEMA",
                    f"a {outcome} implement result claims work_head_commit "
                    f"{declared_head!r} but started from "
                    f"{receipt.get('input_commit')!r}; a no-op that moved the "
                    "head is not a no-op",
                )
            result.pop("work_head_commit", None)
        return result

    raise MaterializationFailed(
        "INVALID_HANDOFF_SCHEMA",
        f"implement outcome {outcome!r} is not one of {sorted((APPLIED, *NON_APPLIED_OUTCOMES))}",
    )


@dataclass
class PluginMaterializer:
    """Seals one stage by invoking the pinned plugin materializer."""

    builder: StageDispatchBuilder
    binding: Any
    target: MaterializationTarget
    lifecycle: Lifecycle = field(default_factory=Lifecycle.load)
    verify_worktree_head: bool = True

    @property
    def implement_stage(self) -> str | None:
        return implement_stage(self.lifecycle)

    @property
    def review_stages(self) -> tuple[str, ...]:
        return review_stages(self.lifecycle)

    @property
    def sealed_stages(self) -> frozenset[str]:
        implement = {self.implement_stage} if self.implement_stage else set()
        return frozenset(implement | set(self.review_stages))

    def serves(self, stage: Stage) -> bool:
        return stage.id in self.sealed_stages and self.builder.serves(stage.id)

    def _continuous_ordering_guard(self, stage: Stage) -> bool:
        """Whether `stage` is the fresh continuous review that opens an attempt.

        Both review stages run through this materializer, but only the
        continuous review is a brand-new attempt: the final review always binds
        to a same-attempt continuous APPROVE and is not subject to the
        new-attempt ordering rule (spec requirement 3 / dev-fg-31b963659d16).
        """
        reviews = self.review_stages
        return bool(reviews) and stage.id == reviews[0]

    def _enforce_continuous_order(self, stage: Stage, dispatch: Dispatch) -> None:
        """Enforce the pinned carrier's ordering rule at the materialization
        boundary, as a structured refusal instead of the non-JSON shell error
        the carrier produces when it applies the same rule.

        A fresh continuous review is a new attempt, legal within its own chain
        only as the very first entry or the entry right after a REJECT
        (``attempt-context.py: check_chain_order``). This guard applies exactly
        that flat rule over the committed index the carrier itself reads, so a
        refusal here is a faithful structured mirror of what the carrier would
        otherwise reject as non-JSON output.

        The cross-generation permit is *not* this guard's job: it is delivered
        upstream by configure (``dd.feedback_scope``), which scopes the
        committed index to the current generation's chain and archives the older
        generation's entries as immutable history. By the time a fresh
        generation's continuous review reaches this seal, the index the carrier
        reads is the new chain's empty seed, so the flat rule admits its first
        attempt. The same flat rule still refuses a genuinely new attempt that
        follows an APPROVE-ended chain within the current generation (spec
        requirements 2, 3 and 5 / dev-fg-31b963659d16).
        """
        if not self._continuous_ordering_guard(stage):
            return
        try:
            refs = read_committed_refs(
                self.builder.chain.workspace_path,
                str(dispatch.get("input_commit") or ""),
                self.builder.chain.development_id,
                spec_path=self.builder.spec_path,
                index_path=self.builder.index_path,
            )
        except DispatchError as exc:
            raise MaterializationFailed("INVALID_HANDOFF_SCHEMA", str(exc)) from exc
        if not chain_rules.new_attempt_is_legal(list(refs.entries)):
            raise MaterializationFailed(
                "ORDER_VIOLATION",
                "a fresh continuous review is a new attempt in this chain and "
                "requires a prior REJECT",
            )

    def request(self, stage: Stage, dispatch: Dispatch, outcome: StageOutcome) -> dict[str, Any]:
        """Assemble the frozen materialization request. Deterministic per attempt."""
        if not self.serves(stage):
            raise UnservedStage(stage.id)

        declared = dict(outcome.receipt or {})
        try:
            self._enforce_continuous_order(stage, dispatch)
            stage_dispatch = self.builder.build(
                dispatch,
                parent_receipt=dispatch.get("parent_receipt") or None,
                # The parent receipt lives under the attempt identity the
                # chain is sealed under -- the pinned one where a replayed
                # prefix installed the receipt, the derived one otherwise.
                # Must agree with what the builder puts in the dispatch, or
                # the digest sent would not be the file the sealer re-reads.
                parent_digest=self.parent_digest(
                    stage.id,
                    str(dispatch.get("pinned_attempt_id") or "")
                    or derive_attempt_id(
                        self.builder.chain.development_id,
                        int(dispatch.get("generation", 1)),
                        int(dispatch.get("attempt", 1)),
                    ),
                ),
            )
        except DispatchError as exc:
            raise MaterializationFailed("INVALID_HANDOFF_SCHEMA", str(exc)) from exc

        stamp = str(dispatch.get("attempt_started_at") or "")
        if not stamp:
            # Without it the request is not reproducible, and a retry would
            # write a second commit for the same work.
            raise MaterializationFailed(
                "INVALID_HANDOFF_SCHEMA", "dispatch carries no frozen attempt_started_at"
            )

        request: dict[str, Any] = {
            "commit_message": f"dev-dispatch: materialize {stage.id} "
            f"{stage_dispatch['attempt_id']}\n",
            "commit_metadata": {
                "author_email": AUTHOR_EMAIL,
                "author_name": AUTHOR_NAME,
                "author_time": stamp,
                "committer_email": AUTHOR_EMAIL,
                "committer_name": AUTHOR_NAME,
                "committer_time": stamp,
            },
            "contract_version": stage_dispatch["contract_version"],
            "dispatch": stage_dispatch,
            "remote_ref": self.target.remote_ref,
            "remote_url": self.target.remote_url,
            "state_root": self.target.state_root,
            "worktree": self.target.worktree,
        }

        if stage.id == self.implement_stage:
            request["actor_result"] = implement_actor_result(declared)
        else:
            review_result = declared.get("review_result")
            if not isinstance(review_result, dict):
                raise MaterializationFailed(
                    "INVALID_HANDOFF_SCHEMA",
                    "a review actor result must declare a review_result object",
                )
            request["review_result"] = review_actor_result(review_result)
            request["implementation_handoff_receipt_digest"] = self._implement_digest(
                stage_dispatch
            )
        return request

    def materialize(self, stage: Stage, dispatch: Dispatch, outcome: StageOutcome) -> Sealed:
        request = self.request(stage, dispatch, outcome)
        invoke = (
            plugin_adapter.invoke_implement_materializer
            if stage.id == self.implement_stage
            else plugin_adapter.invoke_review_materializer
        )
        result = invoke(self.binding, request, verify_worktree_head=self.verify_worktree_head)
        sealed = self._read(stage, result)
        if stage.id in self.review_stages:
            # The sealed review.json cannot carry the reviewer's checklist or
            # the mutation receipt (the pinned plugin schema admits neither),
            # so the engine persists them itself, beside the sealed receipt,
            # before the stage is allowed to report success. This is the
            # durable record S12.5 demands and the receipt S12.3's gate
            # verifies -- not whatever stayed in process memory.
            declared = dict(outcome.receipt or {}).get("review_result") or {}
            checklist = review_checklist(declared if isinstance(declared, dict) else {})
            mutation_receipt = dict(outcome.receipt or {}).get("mutation_receipt")
            write_review_sidecar(
                self.target.state_root,
                str(dispatch.get("pinned_attempt_id") or "")
                or derive_attempt_id(
                    self.builder.chain.development_id,
                    int(dispatch.get("generation", 1)),
                    int(dispatch.get("attempt", 1)),
                ),
                "continuous" if stage.id == self.review_stages[0] else "final",
                checklist,
                mutation_receipt if isinstance(mutation_receipt, dict) else None,
            )
        # The sealer wrote the stage's artifacts, so it -- not the agent --
        # is what output_verify should be believing.
        return Sealed(
            commit=sealed.commit,
            receipt=sealed.receipt,
            produced=tuple(stage.produced_artifacts),
        )

    def parent_digest(self, stage_id: str, attempt_id: str) -> str | None:
        """The parent receipt's byte digest, or None where there is no file."""
        name = PARENT_RECEIPT_FILE.get(stage_id)
        if name is None:
            return None
        return receipt_digest(self.target.state_root, attempt_id, name, label="parent receipt")

    def _implement_digest(self, stage_dispatch: dict[str, Any]) -> str:
        """The Implement receipt this review reviews, by its sealed bytes.

        Not the reviewer's word: an agent handing back the digest of the thing
        it is reviewing would be attesting to its own subject.
        """
        return receipt_digest(
            self.target.state_root,
            str(stage_dispatch.get("attempt_id", "")),
            IMPLEMENT_RECEIPT_FILE,
            label="Implement receipt",
        )

    @staticmethod
    def _read(stage: Stage, result: dict[str, Any]) -> Sealed:
        if set(result) == plugin_adapter.IMPLEMENT_FAILURE_FIELDS:
            raise MaterializationFailed(
                str(result.get("failure_code", "")),
                str(result.get("detail", "")),
                retryable=bool(result.get("retryable", False)),
            )

        outcome = result.get("outcome")
        if outcome in NON_APPLIED_OUTCOMES:
            reason = result.get(NON_APPLIED_DETAIL_FIELD[str(outcome)])
            summary = ""
            if isinstance(reason, dict):
                summary = str(reason.get("summary") or reason.get("reason") or "")
            raise StageRefused(f"{stage.id} actor declared {outcome}: {summary}".strip())

        commit = result.get("output_commit")
        if not isinstance(commit, str) or not commit:
            raise MaterializationFailed(
                "PLUGIN_CONTRACT_MISMATCH",
                f"{stage.id} receipt carries no output_commit; keys={sorted(result)}",
            )
        return Sealed(commit=commit, receipt=result)


@dataclass
class StageMaterializers:
    """Routes each stage to the materializer that seals it.

    Missing means refuse, never pass through: silently carrying the previous
    commit forward would report a stage as sealed when nothing was written.
    """

    by_stage: dict[str, Any]

    def materialize(self, stage: Stage, dispatch: Dispatch, outcome: StageOutcome) -> Sealed:
        materializer = self.by_stage.get(stage.id)
        if materializer is None:
            raise UnservedStage(stage.id)
        return materializer.materialize(stage, dispatch, outcome)


__all__ = [
    "AUTHOR_EMAIL",
    "AUTHOR_NAME",
    "REVIEW_SIDECAR_FILE",
    "MaterializationFailed",
    "MaterializationTarget",
    "PluginMaterializer",
    "StageMaterializers",
    "UnservedStage",
    "implement_actor_result",
    "load_mutation_receipt",
    "receipt_digest",
    "review_actor_result",
    "review_checklist",
    "review_result_fields",
    "review_sidecar_path",
    "write_review_sidecar",
]
