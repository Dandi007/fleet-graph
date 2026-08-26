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

from dataclasses import dataclass
from typing import Any

from fleet_graph.dd.dispatch import DispatchError, StageDispatchBuilder
from fleet_graph.dd.lifecycle import Stage
from fleet_graph.dd.vendor import plugin_adapter
from fleet_graph.graphs.dd_pipeline import Dispatch, Sealed, StageOutcome, StageRefused

# Upstream's identity for the commits the sealer writes. Deliberately not a
# routable address, and deliberately not the operator's: these commits are
# written by a machine on a contract's behalf.
AUTHOR_NAME = "Dev Dispatch"
AUTHOR_EMAIL = "dev-dispatch@example.invalid"

APPLIED = "APPLIED"
NON_APPLIED_OUTCOMES = ("DISPUTED", "BLOCKED")
NON_APPLIED_DETAIL_FIELD = {"DISPUTED": "rebuttal", "BLOCKED": "blocker"}

# Restated from upstream `parse_implement_actor_result`. Kept minimal on
# purpose: the plugin validates the request against its own schema, so this
# only has to stop a request that is obviously not one.
IMPLEMENT_ACTOR_BASE_FIELDS = ("actor_job_id", "input_commit", "outcome")
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


def implement_actor_result(receipt: dict[str, Any]) -> dict[str, Any]:
    """The actor's declared result, checked for the fields its outcome owes."""
    missing = [f for f in IMPLEMENT_ACTOR_BASE_FIELDS if not receipt.get(f)]
    if missing:
        raise MaterializationFailed(
            "INVALID_HANDOFF_SCHEMA",
            f"implement actor result is missing {sorted(missing)}",
        )
    outcome = str(receipt["outcome"])
    result = {field: receipt[field] for field in IMPLEMENT_ACTOR_BASE_FIELDS}

    if outcome == APPLIED:
        absent = [f for f in APPLIED_EXTRA_FIELDS if receipt.get(f) is None]
        if absent:
            raise MaterializationFailed(
                "INVALID_HANDOFF_SCHEMA",
                f"an APPLIED implement result must carry {sorted(absent)}",
            )
        result.update({field: receipt[field] for field in APPLIED_EXTRA_FIELDS})
        return result

    if outcome in NON_APPLIED_OUTCOMES:
        field = NON_APPLIED_DETAIL_FIELD[outcome]
        if receipt.get(field) is None:
            raise MaterializationFailed(
                "INVALID_HANDOFF_SCHEMA", f"a {outcome} implement result must carry {field!r}"
            )
        result[field] = receipt[field]
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
    verify_worktree_head: bool = True

    def serves(self, stage: Stage) -> bool:
        return self.builder.serves(stage.id)

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

        if self._is_implement(stage_dispatch):
            request["actor_result"] = implement_actor_result(declared)
        else:
            review_result = declared.get("review_result")
            if not isinstance(review_result, dict):
                raise MaterializationFailed(
                    "INVALID_HANDOFF_SCHEMA",
                    "a review actor result must declare a review_result object",
                )
            request["review_result"] = review_result
            parent_digest = declared.get("implementation_handoff_receipt_digest")
            if not isinstance(parent_digest, str) or not parent_digest:
                raise MaterializationFailed(
                    "HANDOFF_CHAIN_MISMATCH",
                    "a review materialization must name the implement receipt it reviews",
                )
            request["implementation_handoff_receipt_digest"] = parent_digest
        return request

    def materialize(self, stage: Stage, dispatch: Dispatch, outcome: StageOutcome) -> Sealed:
        request = self.request(stage, dispatch, outcome)
        invoke = (
            plugin_adapter.invoke_implement_materializer
            if self._is_implement(request["dispatch"])
            else plugin_adapter.invoke_review_materializer
        )
        result = invoke(self.binding, request, verify_worktree_head=self.verify_worktree_head)
        return self._read(stage, result)

    @staticmethod
    def _is_implement(stage_dispatch: dict[str, Any]) -> bool:
        return stage_dispatch.get("stage") == "implement"

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
]
