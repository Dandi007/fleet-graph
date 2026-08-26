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

from fleet_graph.dd.capability import CONTRACTS_DIR
from fleet_graph.dd.dispatch import DispatchError, StageDispatchBuilder
from fleet_graph.dd.lifecycle import Lifecycle, Stage
from fleet_graph.dd.vendor import plugin_adapter
from fleet_graph.graphs.dd_actors import (
    implement_stage,
    review_stages,
)
from fleet_graph.graphs.dd_pipeline import Dispatch, Sealed, StageOutcome, StageRefused

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


def review_actor_result(declared: dict[str, Any]) -> dict[str, Any]:
    """The reviewer's declared result, narrowed to what the plugin admits."""
    admitted = review_result_fields()
    return {key: value for key, value in declared.items() if key in admitted}


def implement_actor_result(receipt: dict[str, Any]) -> dict[str, Any]:
    """The actor's declared result, narrowed to what the plugin admits.

    Two jobs, and only two. It drops fields the plugin's
    `additionalProperties: false` would reject, and it refuses a result whose
    *declared* outcome is missing the evidence that outcome owes.

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

    def request(self, stage: Stage, dispatch: Dispatch, outcome: StageOutcome) -> dict[str, Any]:
        """Assemble the frozen materialization request. Deterministic per attempt."""
        if not self.serves(stage):
            raise UnservedStage(stage.id)

        declared = dict(outcome.receipt or {})
        try:
            stage_dispatch = self.builder.build(
                dispatch, parent_receipt=dispatch.get("parent_receipt") or None
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
        # The sealer wrote the stage's artifacts, so it -- not the agent --
        # is what output_verify should be believing.
        return Sealed(
            commit=sealed.commit,
            receipt=sealed.receipt,
            produced=tuple(stage.produced_artifacts),
        )

    def _implement_digest(self, stage_dispatch: dict[str, Any]) -> str:
        """The digest of the sealed Implement receipt's **bytes**.

        Not of the receipt object we hold. The sealer writes the receipt to
        `<state_root>/receipts/<attempt_id>/implement-receipt.json` and the
        review sealer re-reads exactly those bytes and compares
        (`DIGEST_MISMATCH: Implement receipt bytes do not match the requested
        digest`). A canonical-JSON digest of the equivalent dict is a different
        number, because the file carries fields the returned receipt does not.

        And it is not the reviewer's to supply either: an agent handing back
        the digest of the thing it is reviewing would be attesting to its own
        subject.
        """
        attempt_id = str(stage_dispatch.get("attempt_id", ""))
        path = Path(self.target.state_root) / "receipts" / attempt_id / IMPLEMENT_RECEIPT_FILE
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise MaterializationFailed(
                "HANDOFF_CHAIN_MISMATCH",
                f"no sealed Implement receipt for attempt {attempt_id}: {exc}",
            ) from exc
        return "sha256:" + hashlib.sha256(payload).hexdigest()

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
    "MaterializationFailed",
    "MaterializationTarget",
    "PluginMaterializer",
    "StageMaterializers",
    "UnservedStage",
    "implement_actor_result",
    "review_actor_result",
    "review_result_fields",
]
