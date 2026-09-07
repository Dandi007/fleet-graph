"""Mechanical receipt replay for a restarted generation (wf-13ff9e F4).

A fresh generation used to re-run every stage from `configure`. After a
generation whose implement already sealed, the re-bootstrapped tree *contains*
the product commits -- git chain is the state, by design -- so the fresh
implement actor is handed a spec that is already satisfied and honestly
reports BLOCKED. Measured on dev-fg-4628ef887564 g3: the whole line jammed on
an agent telling the truth.

This module is the fix's mechanical half: at generation start, the stages the
previous generation already sealed are replayed from their receipts instead of
re-dispatched. The doctrine is the same one re-adopt and checkpoint resume
follow -- the state lives on the chain, so a re-run must be idempotent.

**The judgment is receipts only.** A stage replays iff:

- its sealed receipt file is on the receipts channel of a previous
  generation's state root, with exactly the field set the vendored adapter
  admits;
- the digest chain closes: the implement receipt's parent digest recomputes
  from its configure predecessor (read out of git), each review names its
  parent receipt's byte digest, and a rework implement names the rejecting
  review's canonical digest;
- its `output_commit` is an ancestor of the current worktree HEAD.

Prose, history summaries and agent claims do not count. A chain broken at a
stage re-runs for real from that stage; a review that ended REJECT (and the
rejected work's reviews) is never replayed -- only the success/APPROVE prefix
is. Rework stays what it always was: the in-graph feedback loop, reached
through real transitions, never through replay.

**A replayed receipt carries its frozen intent too.** The plugin's sealer
freezes each sealed receipt's immutable intent beside the receipt under
`<state_root>/intents/<intent_id>.json`, and the Review sealer re-reads the
Continuous intent when it seals a Final review. Replay therefore re-installs
that intent with its receipt; a review receipt whose intent the source
generation no longer holds is an un-rechargeable link and re-runs for real.

**Replay may trim dead weight, and only dead weight.** A pre-F4 restart left
junk commits above the sealed tip (a fresh generation's `configure` re-seal,
an acceptance record of a run that then failed). The plugin sealer requires
the remote head to equal the input commit, so those commits must go before a
review can seal on the replayed tip. The trim is fail-closed: it happens only
when every commit above the tip touches nothing outside the reserved
`.dev-dispatch/` / `.dd-evidence/` namespaces -- product code above the tip
means no trim and no replay at all.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fleet_graph.dd import chain_rules
from fleet_graph.dd.bootstrap import INDEX_PATH
from fleet_graph.dd.dispatch import derive_attempt_id
from fleet_graph.dd.egress import (
    DEFAULT_EGRESS_POLICY,
    EgressPolicy,
    EgressRepoError,
    TransportExhausted,
    retry_remote,
)
from fleet_graph.dd.git import run_git
from fleet_graph.dd.lifecycle import Lifecycle, Stage
from fleet_graph.dd.upstream_constants import compute_json_digest
from fleet_graph.dd.vendor import plugin_adapter
from fleet_graph.graphs.dd_actors import implement_stage, review_stages
from fleet_graph.graphs.dd_pipeline import (
    MODE_INITIAL,
    MODE_REWORK,
    SPINE_EVENT,
    Dispatch,
    Replayed,
)
from fleet_graph.graphs.dd_scripts import RUN_CONFIG_PATH, write_json

# The sealed receipt file per plugin-sealed stage, under
# `<state_root>/receipts/<attempt_id>/`. The same table
# `control_plane._sealed_receipt` reads; the byte digest of each file is what
# the next receipt names as its parent.
RECEIPT_FILES = {
    "implement": "implement-receipt.json",
    "continuous_review": "continuous-review-receipt.json",
    "final_review": "final-review-receipt.json",
}

# Where the plugin's sealer freezes the immutable materialization intent that
# accompanies each sealed receipt, under `<state_root>/intents/<intent_id>.json`.
# The Review sealer re-reads the Continuous intent when it seals a Final review;
# a replayed Continuous receipt whose intent was not carried over is a chain the
# Final sealer cannot continue (measured: RECEIPT_CONFLICT "Continuous
# materialization intent is unreadable").
INTENTS_DIR = "intents"

APPROVE = "APPROVE"
# The rework-edge rules live in dd/chain_rules.py -- one source, shared with
# supervise/audit.py's chain check, so the topology cannot drift between the
# replayer and the auditor. REJECT is re-exported for existing importers.
REJECT = chain_rules.REJECT

#: The reserved control namespaces. Commits above the sealed tip that touch
#: only these may be trimmed on replay; anything else is product drift and
#: refuses the whole replay.
RESERVED_PREFIXES = (".dev-dispatch/", ".dd-evidence/")

#: Mechanical bound on the within-generation rework walk. The pipeline's own
#: rework bound is single digits; this only stops a pathological directory.
MAX_WALK_ATTEMPTS = 64

_HEX40 = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class _Step:
    """One replayable link: which stage, what its receipt says, its bytes."""

    stage_id: str
    event: str
    receipt: dict[str, Any]
    output_commit: str
    # The sealed file's exact bytes, installed into the current generation's
    # receipts directory so a later real seal chains on the same byte digest.
    # Empty for a reconstructed WorkspaceSealer receipt, which has no file.
    raw: bytes = b""
    # The frozen materialization intent bytes that accompany the receipt, if
    # the receipt names one and the source generation still holds it. Installed
    # alongside the receipt so a later real seal can re-read it. Empty for the
    # reconstructed configure link and for a receipt whose intent is absent.
    intent_raw: bytes = b""
    # The dispatch mode this step was sealed under -- `initial` for attempt 1,
    # `rework` for every later attempt (the lifecycle's REJECT edge always
    # bumps attempt and sets mode together). Carried forward on the Replayed
    # object so the first real stage after a rework prefix dispatches as
    # `rework`, matching the frozen Continuous intent a Final sealer re-reads
    # (against `dispatch["mode"]`). Without it the final review of replayed
    # rework would dispatch `initial` and the sealer raises BINDING_MISMATCH
    # "persisted Continuous intent dispatch_mode does not match its
    # authoritative binding" (dev-fg-6e4f9345b320 g7).
    mode: str = MODE_INITIAL


def byte_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _declared_acceptance_context(config: dict[str, Any]) -> dict[str, Any]:
    """The acceptance-context fields the tamper check actually compares.

    Mirrors ``AcceptanceStage.commands``: empty command lists are dropped, and
    absent keys mean empty declarations -- a pre-R1-c run-config is not a
    mismatch by itself.
    """
    return {
        "acceptance_commands": [
            list(command) for command in (config.get("acceptance_commands") or []) if command
        ],
        "setup_commands": [
            list(command) for command in (config.get("setup_commands") or []) if command
        ],
        "acceptance_env": dict(config.get("acceptance_env") or {}),
    }


@dataclass
class ReceiptReplayer:
    """Replays the receipt-sealed prefix of a previous generation.

    Consulted by the walker once per stage, in walk order. The first miss --
    a chain break, a REJECT, a stage with no receipt -- disables it for the
    rest of the run, so replay is always a prefix. Side effects (the trim and
    the receipt installation) happen exactly once, immediately before the
    first replayed stage is returned, and never on a run that replays
    nothing.
    """

    workspace: Path
    #: The current generation's state root: where replayed receipt bytes are
    #: installed so this generation's own seals can chain on them.
    state_root: Path
    #: (generation, state_root) of every previous generation, newest first.
    prior_state_roots: tuple[tuple[int, Path], ...]
    development_id: str
    generation: int
    remote_url: str = ""
    remote_ref: str = ""
    #: The current generation's declared acceptance context (acceptance
    #: commands, setup commands, environment). The replayed configure commit
    #: carries the *previous* generation's run-config; when the operator
    #: reconfigured the context, this is the authoritative value that configure
    #: would have written, and the replayer re-produces it so the acceptance
    #: stage's tamper check sees agreement rather than a stale mismatch. None
    #: means "leave the replayed tree alone" (the pre-reconfigure behaviour).
    run_config: dict[str, Any] | None = None
    lifecycle: Lifecycle = field(default_factory=Lifecycle.load)
    # Egress resilience: the replay-time remote probe and the trim push retry
    # transport-class failures under the bounded backoff. A probe or push
    # that stays dark past the budget leaves the replay disabled -- the
    # stage re-runs for real, the pre-existing fail-closed path.
    egress_policy: EgressPolicy = field(default_factory=lambda: DEFAULT_EGRESS_POLICY)
    sleep: Callable[[float], None] = time.sleep
    rand: Callable[[], float] = random.random

    def __post_init__(self) -> None:
        self._plan: list[_Step] | None = None
        self._index = 0
        self._disabled = False
        self._pending_run_config: dict[str, Any] | None = None

    # --- the walker's port ------------------------------------------------

    def replay(self, stage: Stage, dispatch: Dispatch) -> Replayed | None:
        if self._pending_run_config is not None and not stage.is_llm:
            # A reconfigured acceptance context is re-produced here, at the
            # first script stage after the replayed reviews -- which is always
            # `acceptance`, the last walker stage that reads the run-config.
            # Deferring it this far keeps the controller-owned
            # `.dev-dispatch/run-config.json` drift out of every review stage's
            # view, so a read-only reviewer is never blamed for it
            # (ACTOR_RESERVED_PATH_CHANGED). The acceptance stage's own sealer
            # commits the rewrite with `git add -A`. See
            # `_apply_pending_run_config`.
            self._apply_pending_run_config()
        if self._disabled:
            return None
        if int(dispatch.get("attempt", 1)) != 1 or int(dispatch.get("retry", 0)) != 0:
            # Rework and retries are real work with real feedback; replay
            # serves only the untouched start of a fresh generation. `mode` is
            # deliberately not checked here: a reworked prefix replays under a
            # later attempt, which has already bumped `attempt`, so it is
            # caught by the attempt guard. And after a rework prefix is
            # replayed, the walker's own mode advances to `rework` for the
            # stages that follow it -- that is the *desired* inheritance, not a
            # fresh-generation start to reject.
            self._disabled = True
            return None
        if self._plan is None:
            try:
                candidates = self._candidate_plans()
            except Exception:
                candidates = []
            self._plan = self._select(candidates, stage)
            if self._plan is None:
                self._disabled = True
                return None
            if self._ends_at_implement(self._plan):
                inherited = self._inherited_entries()
                if inherited is not None and not chain_rules.new_attempt_is_legal(inherited):
                    # Replaying a prefix that stops at implement is always
                    # followed by a fresh continuous review -- a brand-new
                    # attempt. The replayer is deliberately conservative here:
                    # it replays such a partial prefix only when the committed
                    # index that fresh attempt would follow is empty or already
                    # REJECT-terminated, i.e. the flat carrier rule
                    # ``check_chain_order`` accepts it. An APPROVE-ended
                    # inherited index is refused -- in particular a previous
                    # generation's accepted history, because replaying it would
                    # move the tree only to trim that inherited record away,
                    # erasing history the spec requires preserved. The
                    # generation-aware permit of an older generation's inherited
                    # chain is the materializer's job (``dd_materializer``),
                    # which reads the committed entries at the seal -- never the
                    # replayer's, and never by deleting records. (An unreadable
                    # index defers to the materializer.)
                    self._disabled = True
                    return None
            # The one mutation happens exactly once, on exactly the selected
            # candidate, before the first replayed step is returned. A refusal
            # here is fail-closed: nothing else is tried, nothing was moved.
            if not self._prepare(self._plan):
                self._disabled = True
                return None
        if self._index >= len(self._plan):
            self._disabled = True
            return None
        step = self._plan[self._index]
        if step.stage_id != stage.id:
            # A resumed thread mid-walk, or a contract whose order moved:
            # either way this is not the prefix, so nothing is replayed --
            # and, the index still being 0, nothing has been mutated.
            self._disabled = True
            return None
        self._index += 1
        return Replayed(
            event=step.event,
            receipt=dict(step.receipt),
            output_commit=step.output_commit,
            attempt_id=str(step.receipt.get("attempt_id") or ""),
            mode=step.mode,
        )

    # --- plan construction (read-only) ------------------------------------

    def _candidate_plans(self) -> list[list[_Step]]:
        """The replays worth trying, best first.

        A restarted generation replays the receipt-sealed prefix of the
        previous one. A prior generation that crashed mid-review (configure +
        implement sealed, the review never sealed) leaves only a *partial*
        prefix ending at implement; replaying that partial prefix and then
        materialising a fresh continuous review hands the feedback carrier a
        brand-new attempt, whose ordering rule refuses without a preceding
        REJECT (ORDER_VIOLATION). When an earlier generation already holds the
        review-bearing prefix for the same development, it is a stronger
        candidate: the replayed candidate continues through its continuous and
        final review stages instead of opening a new attempt. Candidates are
        therefore ordered review-depth-first, newest generation first within a
        depth. The order is a preference only: a candidate that cannot be
        prepared means no replay at all (see `_select`), never a fall back to a
        *divergent* partial prefix that would surface unreviewed work as a new
        attempt.
        """
        implement_id = implement_stage(self.lifecycle)
        configure_id = self._spine_predecessor(implement_id)
        if not implement_id or not configure_id:
            return []
        head = self._rev_parse("HEAD")
        if not head:
            return []
        plans: list[list[_Step]] = []
        for generation, root in self.prior_state_roots:
            plan = self._walk(int(generation), Path(root), head, configure_id, implement_id)
            if plan:
                plans.append(plan)
        if not plans:
            return []
        review_ids = set(review_stages(self.lifecycle))

        def _review_depth(plan: list[_Step]) -> int:
            return sum(1 for step in plan if step.stage_id in review_ids)

        # `sorted` is stable, so the newest-generation-first walk order is
        # preserved among plans of equal review depth; the key only moves the
        # deeper (more-reviewing) prefixes forward.
        return sorted(plans, key=_review_depth, reverse=True)

    def _select(self, candidates: list[list[_Step]], stage: Stage) -> list[_Step] | None:
        """The strongest candidate that starts at `stage`, chosen read-only.

        `_candidate_plans` already ordered the candidates review-depth-first,
        so the first one that applies is the one to try. Selection itself does
        not mutate: `replay` then `_prepare`s exactly one candidate, so a later
        candidate is never tried against a tree an earlier one already moved
        (the rollback hazard of a prepare-inside-select loop). A candidate
        whose `_prepare` refuses -- product drift above its tip, a vanished
        remote head -- means no replay at all rather than a fall back to a
        less-reviewed prefix: replaying a divergent partial prefix would hand
        the feedback carrier a fresh review of work whose inherited chain did
        not end in REJECT (ORDER_VIOLATION). That is the same fail-closed trim
        the single-generation path already applies.
        """
        for candidate in candidates:
            if not candidate or candidate[0].stage_id != stage.id:
                continue
            return candidate
        return None

    def _ends_at_implement(self, plan: list[_Step]) -> bool:
        """Whether the plan's last link is the implement stage (no reviews)."""
        implement = implement_stage(self.lifecycle)
        return bool(implement) and plan[-1].stage_id == implement

    def _inherited_entries(self) -> list[dict[str, Any]] | None:
        """The committed feedback index's review entries at HEAD, or None.

        The carrier's ordering rule lives in the pinned plugin, but the index it
        enforces is committed in the worktree at this path. Reading it lets the
        replayer recognise -- before moving any tree -- that a partial prefix
        ending at implement would be followed by a fresh continuous review the
        carrier refuses as a new attempt lacking a prior REJECT. An unreadable
        or malformed index returns None, which defers to the carrier rather than
        guessing.
        """
        proc = run_git(self.workspace, "show", f"HEAD:{INDEX_PATH}")
        if proc.returncode != 0:
            return None
        try:
            index = json.loads(proc.stdout)
        except ValueError:
            return None
        entries = index.get("entries") if isinstance(index, dict) else None
        return entries if isinstance(entries, list) else None

    # --- walk one generation's receipts -----------------------------------

    def _walk(
        self,
        source_generation: int,
        root: Path,
        head: str,
        configure_id: str,
        implement_id: str,
    ) -> list[_Step]:
        """The success/APPROVE prefix of one generation, verified link by link."""
        reviews = review_stages(self.lifecycle)
        continuous_id = reviews[0] if reviews else ""
        final_id = reviews[1] if len(reviews) > 1 else ""

        loaded = self._plugin_receipt(root, source_generation, 1, implement_id)
        if loaded is None:
            return []
        imp_raw, imp = loaded
        if not self._valid_implement(imp, head):
            return []
        configure_step = self._configure_step(configure_id, imp)
        if configure_step is None:
            return []

        attempt = 1
        while attempt <= MAX_WALK_ATTEMPTS:
            # The dispatch mode of this attempt's sealed prefix. The lifecycle's
            # REJECT edge bumps attempt and sets mode together, so attempt 1 is
            # `initial` and every later attempt is `rework`. This mirrors the
            # `dispatch_mode` the plugin froze into each sealed intent; carrying
            # it forward is what lets a real Final review of reworked work
            # dispatch as `rework` and close the sealer's binding instead of
            # raising a false dispatch-mode mismatch.
            mode = MODE_REWORK if attempt > 1 else MODE_INITIAL
            # The implement intent is carried over when present but is not
            # required to replay: no downstream sealer re-reads it (the Review
            # sealer binds to the implement receipt and its committed artifact),
            # so treating its absence as a chain break would turn a curable
            # implement replay back into a fresh-agent re-dispatch.
            imp_intent = self._plugin_intent(root, imp)
            steps = [
                configure_step,
                _Step(
                    stage_id=implement_id,
                    event=SPINE_EVENT,
                    receipt=imp,
                    output_commit=str(imp["output_commit"]),
                    raw=imp_raw,
                    intent_raw=imp_intent[0] if imp_intent else b"",
                    mode=mode,
                ),
            ]
            if not continuous_id:
                return steps

            loaded = self._plugin_receipt(root, source_generation, attempt, continuous_id)
            if loaded is None:
                return steps  # chain ends after implement; review runs real
            cr_raw, cr = loaded
            if not self._valid_review(cr, head) or cr.get(
                "parent_handoff_receipt_digest"
            ) != byte_digest(imp_raw):
                return steps  # broken at the review link; re-run from there

            verdict = str(cr.get("verdict") or "")
            if verdict == REJECT:
                # The rejecting review is never replayed. If the rework
                # implement it steered into sealed, the walk moves onto it;
                # otherwise the prefix ends at the (superseded) implement and
                # a real review re-runs.
                advanced = self._rework_implement(
                    root, source_generation, attempt + 1, implement_id, cr, head
                )
                if advanced is None:
                    return steps
                imp_raw, imp = advanced
                attempt += 1
                continue
            if verdict != APPROVE:
                return steps

            cr_intent = self._plugin_intent(root, cr)
            if cr_intent is None and not self._final_replays(
                root, source_generation, attempt, final_id, cr_raw, head
            ):
                # The Review sealer re-reads the Continuous intent only when it
                # *seals* a Final review. When the Final review is itself
                # replayed, nothing re-reads it, so a missing intent is harmless
                # and the walk must continue rather than truncating to a
                # [configure, implement] prefix that would force an illegal fresh
                # continuous review (spec requirement 3). Only when the Final
                # review must run for real is the missing intent an
                # un-rechargeable link, so the walk then re-runs the review.
                return steps

            steps.append(
                _Step(
                    stage_id=continuous_id,
                    event=APPROVE,
                    receipt=cr,
                    output_commit=str(cr["output_commit"]),
                    raw=cr_raw,
                    intent_raw=cr_intent[0] if cr_intent else b"",
                    mode=mode,
                )
            )
            if not final_id:
                return steps

            loaded = self._plugin_receipt(root, source_generation, attempt, final_id)
            if loaded is None:
                return steps
            fr_raw, fr = loaded
            if not self._valid_review(fr, head) or fr.get(
                "parent_handoff_receipt_digest"
            ) != byte_digest(cr_raw):
                return steps

            verdict = str(fr.get("verdict") or "")
            if verdict == REJECT:
                advanced = self._rework_implement(
                    root, source_generation, attempt + 1, implement_id, fr, head
                )
                if advanced is None:
                    return steps
                imp_raw, imp = advanced
                attempt += 1
                continue
            if verdict != APPROVE:
                return steps

            fr_intent = self._plugin_intent(root, fr)
            if fr_intent is None:
                return steps

            steps.append(
                _Step(
                    stage_id=final_id,
                    event=APPROVE,
                    receipt=fr,
                    output_commit=str(fr["output_commit"]),
                    raw=fr_raw,
                    intent_raw=fr_intent[0],
                    mode=mode,
                )
            )
            # Acceptance and everything after it always re-runs: acceptance
            # re-measures, the gate re-asks, the merge re-decides.
            return steps
        return []

    def _final_replays(
        self,
        root: Path,
        source_generation: int,
        attempt: int,
        final_id: str,
        cr_raw: bytes,
        head: str,
    ) -> bool:
        """Whether the final review of `attempt` is itself replayable.

        When the Continuous review's frozen intent is missing, the walk can
        still continue -- replaying the Continuous link with an empty intent --
        iff the Final review is also replayed rather than re-sealed, because
        nothing re-reads the missing intent in that case. This is a read-only
        probe; it does not advance the walk's rework edge (a REJECT final is not
        replayable, so it reports False and the walk re-runs the review for
        real, the conservative pre-existing behaviour).
        """
        if not final_id:
            return False
        loaded = self._plugin_receipt(root, source_generation, attempt, final_id)
        if loaded is None:
            return False
        _fr_raw, fr = loaded
        if not self._valid_review(fr, head) or fr.get(
            "parent_handoff_receipt_digest"
        ) != byte_digest(cr_raw):
            return False
        if str(fr.get("verdict") or "") != APPROVE:
            return False
        return self._plugin_intent(root, fr) is not None

    def _rework_implement(
        self,
        root: Path,
        source_generation: int,
        attempt: int,
        implement_id: str,
        rejecting_receipt: dict[str, Any],
        head: str,
    ) -> tuple[bytes, dict[str, Any]] | None:
        """The implement the REJECT steered into, iff its link closes.

        A rework implement's dispatch named the rejecting review's
        canonical-JSON digest as parent (the in-memory receipt the walker
        carried), so that is the digest checked here.
        """
        loaded = self._plugin_receipt(root, source_generation, attempt, implement_id)
        if loaded is None:
            return None
        raw, receipt = loaded
        if not self._valid_implement(receipt, head):
            return None
        if receipt.get("parent_handoff_receipt_digest") != chain_rules.rework_link_parent(
            rejecting_receipt
        ):
            return None
        return raw, receipt

    def _configure_step(self, configure_id: str, imp: dict[str, Any]) -> _Step | None:
        """The configure link, recomputed rather than believed.

        The first implement's parent digest is the canonical digest of the
        WorkspaceSealer receipt its configure produced: exactly
        ``{"stage", "input_commit", "output_commit"}``. Both commits are on
        the chain, so the receipt is reconstructed from git and the digest
        must recompute -- that closes the implement link back to the
        bootstrap ancestry without trusting anything off-chain.
        """
        output_commit = str(imp.get("input_commit") or "")
        if not _HEX40.fullmatch(output_commit):
            return None
        parents = self._rev_list_parents(output_commit)
        if len(parents) != 1:
            return None
        receipt = {
            "stage": configure_id,
            "input_commit": parents[0],
            "output_commit": output_commit,
        }
        if imp.get("parent_handoff_receipt_digest") != compute_json_digest(receipt):
            return None
        return _Step(
            stage_id=configure_id,
            event=SPINE_EVENT,
            receipt=receipt,
            output_commit=output_commit,
        )

    # --- receipt loading and validation ------------------------------------

    def _plugin_receipt(
        self, root: Path, source_generation: int, attempt: int, stage_id: str
    ) -> tuple[bytes, dict[str, Any]] | None:
        filename = RECEIPT_FILES.get(stage_id)
        if filename is None:
            return None
        attempt_id = derive_attempt_id(self.development_id, source_generation, attempt)
        path = root / "receipts" / attempt_id / filename
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        try:
            receipt = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(receipt, dict):
            return None
        if str(receipt.get("attempt_id") or "") != attempt_id:
            # A receipt claiming an identity other than the one it was
            # sealed under is not a link, whatever else it says. The sealer
            # enforces the same equality; refusing here keeps the pinned
            # identity's provenance receipt-only.
            return None
        return raw, receipt

    def _plugin_intent(
        self, root: Path, receipt: dict[str, Any]
    ) -> tuple[bytes, dict[str, Any]] | None:
        """The frozen materialization intent a receipt was sealed with, if present.

        The plugin freezes each sealed receipt's intent alongside the receipt
        under `<state_root>/intents/<intent_id>.json`. It is read back here --
        and later re-installed -- so a replay carries the whole sealed record,
        not just the receipt a later materialization re-reads. A receipt that
        names an intent the source generation no longer holds, or one whose
        stored identity disagrees with the receipt's, is not a link whose
        intent can be reinstated, so it reads as a miss.
        """
        intent_id = str(receipt.get("materialization_intent_id") or "")
        if not intent_id:
            return None
        path = root / INTENTS_DIR / f"{intent_id}.json"
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        try:
            intent = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(intent, dict):
            return None
        if str(intent.get("materialization_intent_id") or "") != intent_id:
            return None
        return raw, intent

    def _valid_implement(self, receipt: dict[str, Any], head: str) -> bool:
        """An applied implement receipt, complete and on the current chain."""
        if set(receipt) != plugin_adapter.IMPLEMENT_RECEIPT_FIELDS:
            return False
        output = str(receipt.get("output_commit") or "")
        return bool(_HEX40.fullmatch(output)) and self._is_ancestor(output, head)

    def _valid_review(self, receipt: dict[str, Any], head: str) -> bool:
        if set(receipt) != plugin_adapter.REVIEW_RECEIPT_FIELDS:
            return False
        output = str(receipt.get("output_commit") or "")
        return bool(_HEX40.fullmatch(output)) and self._is_ancestor(output, head)

    # --- the one mutation: trim to the tip, install the receipts -----------

    def _prepare(self, plan: list[_Step]) -> bool:
        tip = plan[-1].output_commit
        head = self._rev_parse("HEAD")
        if not head:
            return False
        if head != tip:
            diff = run_git(self.workspace, "diff", "--name-only", tip, head)
            if diff.returncode != 0:
                return False
            names = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
            if not all(name.startswith(RESERVED_PREFIXES) for name in names):
                # Product drift above the sealed tip: refuse rather than cut.
                # (An empty diff is fine -- tree-identical junk commits, e.g.
                # an --allow-empty re-seal, are the safest trim of all.)
                return False
            if self.remote_url and self.remote_ref:
                observed = self._remote_head()
                if observed != head:
                    return False
                try:
                    retry_remote(
                        lambda: run_git(
                            self.workspace,
                            "push",
                            "--quiet",
                            f"--force-with-lease={self.remote_ref}:{observed}",
                            self.remote_url,
                            f"{tip}:{self.remote_ref}",
                        ),
                        op_name="push",
                        policy=self.egress_policy,
                        sleep=self.sleep,
                        rand=self.rand,
                    )
                except (TransportExhausted, EgressRepoError):
                    return False
            reset = run_git(self.workspace, "reset", "--hard", "--quiet", tip)
            if reset.returncode != 0:
                return False
        elif not self._clear_stale_run_config_residue():
            return False

        # Installed under the identity the receipts were sealed with -- their
        # own `attempt_id`, which the walker pins for the rest of the pass.
        # The plugin sealer reads the parent receipt at the *dispatch's*
        # attempt id and refuses a receipt whose embedded identity differs
        # (measured: g4's review at a re-derived id hit BINDING_MISMATCH
        # "Implement receipt identity does not match Review dispatch"), so a
        # re-derived install path would be a chain nobody can continue.
        for step in plan:
            filename = RECEIPT_FILES.get(step.stage_id)
            attempt_id = str(step.receipt.get("attempt_id") or "")
            if not step.raw or filename is None or not attempt_id:
                continue
            target = self.state_root / "receipts" / attempt_id
            target.mkdir(parents=True, exist_ok=True)
            (target / filename).write_bytes(step.raw)
            if step.intent_raw:
                intent_id = str(step.receipt.get("materialization_intent_id") or "")
                if intent_id:
                    intent_dir = self.state_root / INTENTS_DIR
                    intent_dir.mkdir(parents=True, exist_ok=True)
                    (intent_dir / f"{intent_id}.json").write_bytes(step.intent_raw)
        if self.run_config is not None:
            self._pending_run_config = self._reconfigured_run_config(plan[0].output_commit)
        return True

    def _clear_stale_run_config_residue(self) -> bool:
        """Drop a previous generation's controller-owned ``run-config.json`` dirt.

        When ``HEAD`` already equals the replay tip, ``_prepare`` skips its
        ``reset --hard`` branch, so a reconfigured acceptance declaration a
        *previous* failed generation left uncommitted survives into the fresh
        final reviewer and is blamed on the actor (ACTOR_RESERVED_PATH_CHANGED).
        Restore HEAD's committed copy of exactly that one file, and only when
        the residue is provably controller-owned: the sole ``.dev-dispatch/**``
        change is ``run-config.json``, and its worktree copy names this
        development at a previous generation. Anything else -- a write to any
        other reserved path, or a run-config that is not a stale controller
        declaration -- is left for the reserved-path guard to refuse, never
        hidden (fail-closed).
        """
        status = run_git(
            self.workspace,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            ".dev-dispatch",
        )
        if status.returncode != 0:
            # Cannot inspect the tree: let the guard refuse rather than guess.
            return True
        changed = {line[3:] for line in status.stdout.splitlines() if len(line) > 3}
        if changed != {RUN_CONFIG_PATH}:
            # Clean, or a change beyond the one controller-owned file.
            return True
        if not self._is_stale_controller_run_config():
            return True
        restore = run_git(self.workspace, "checkout", "HEAD", "--", RUN_CONFIG_PATH)
        return restore.returncode == 0

    def _is_stale_controller_run_config(self) -> bool:
        """Whether the dirty worktree run-config is a previous generation's.

        The controller's configure/reconfigure always writes ``development_id``
        and ``generation``. A run-config whose ``generation`` is strictly less
        than the current one was written for a generation that already ended,
        so it is stale controller residue rather than this generation's
        declaration (which the replayer defers to acceptance) or an actor's
        legitimate output.
        """
        try:
            raw = (self.workspace / RUN_CONFIG_PATH).read_text(encoding="utf-8")
        except OSError:
            return False
        try:
            config = json.loads(raw)
        except ValueError:
            return False
        if not isinstance(config, dict):
            return False
        if str(config.get("development_id") or "") != self.development_id:
            return False
        generation = config.get("generation")
        return isinstance(generation, int) and generation < self.generation

    def _reconfigured_run_config(self, configure_commit: str) -> dict[str, Any] | None:
        """The run-config this generation must materialise, when reconfigured.

        The replayed configure commit carries the *previous* generation's run
        config. When the operator reconfigured the acceptance context, that
        file is stale: the acceptance stage would refuse the run with
        ACCEPTANCE_DECLARATION_MISMATCH even though the declaration is the
        authoritative value. This returns exactly what a re-run configure
        would have written, so it can be re-produced later without dirtying the
        tree under the review stages. ``None`` means the replayed tree already
        agrees with the declaration and nothing needs to change.
        """
        committed = self._committed_run_config(configure_commit)
        if committed is not None and _declared_acceptance_context(
            committed
        ) == _declared_acceptance_context(self.run_config or {}):
            return None
        return {
            "development_id": self.development_id,
            "generation": self.generation,
            **(self.run_config or {}),
        }

    def _apply_pending_run_config(self) -> None:
        """Write the reconfigured run-config into the working tree, once.

        Invoked lazily, at the first script stage after the replayed reviews --
        always `acceptance`, which is the last stage that reads the file. Doing
        it this late keeps controller-owned `.dev-dispatch/run-config.json`
        drift out of the review stages' view, so a read-only reviewer is never
        blamed for it (ACTOR_RESERVED_PATH_CHANGED). The rewrite stays in the
        working tree and is picked up and committed by the acceptance stage's
        own sealer (``git add -A``), so the final tree still carries the
        reconfigured run-config.
        """
        pending = self._pending_run_config
        if pending is None:
            return
        self._pending_run_config = None
        write_json(self.workspace, RUN_CONFIG_PATH, pending)

    def _committed_run_config(self, commit: str) -> dict[str, Any] | None:
        proc = run_git(self.workspace, "show", f"{commit}:{RUN_CONFIG_PATH}")
        if proc.returncode != 0:
            return None
        try:
            config = json.loads(proc.stdout)
        except ValueError:
            return None
        return config if isinstance(config, dict) else None

    # --- git plumbing ------------------------------------------------------

    def _rev_parse(self, spec: str) -> str:
        proc = run_git(self.workspace, "rev-parse", spec)
        value = proc.stdout.strip()
        return value if proc.returncode == 0 and _HEX40.fullmatch(value) else ""

    def _rev_list_parents(self, commit: str) -> list[str]:
        proc = run_git(self.workspace, "rev-list", "--parents", "-n", "1", commit)
        if proc.returncode != 0:
            return []
        tokens = proc.stdout.split()
        return tokens[1:] if tokens and tokens[0] == commit else []

    def _is_ancestor(self, commit: str, head: str) -> bool:
        return run_git(self.workspace, "merge-base", "--is-ancestor", commit, head).returncode == 0

    def _remote_head(self) -> str:
        try:
            proc = retry_remote(
                lambda: run_git(self.workspace, "ls-remote", self.remote_url, self.remote_ref),
                op_name="ls-remote",
                policy=self.egress_policy,
                sleep=self.sleep,
                rand=self.rand,
            )
        except (TransportExhausted, EgressRepoError):
            return ""
        heads = [line.split()[0] for line in proc.stdout.splitlines() if line.strip()]
        return heads[0] if heads else ""

    def _spine_predecessor(self, stage_id: str | None) -> str | None:
        if not stage_id:
            return None
        sources = [s for s, target in self.lifecycle.spine.items() if target == stage_id]
        return sources[0] if len(sources) == 1 else None


def prior_generation_state_roots(run_root: Path, generation: int) -> tuple[tuple[int, Path], ...]:
    """(generation, state_root) of every previous generation, newest first.

    Knows only the control plane's standard layout -- generation 1 at the
    development root, ``g{n}/`` after that, state under ``state/``. A custom
    layout gets an empty answer, which means no replay and the old fresh-run
    behavior: fail open to correctness, not to cleverness.
    """
    if generation <= 1 or run_root.name != f"g{generation}":
        return ()
    dev_root = run_root.parent
    pairs: list[tuple[int, Path]] = [(1, dev_root / "state")]
    pairs.extend((n, dev_root / f"g{n}" / "state") for n in range(2, generation))
    return tuple(reversed(pairs))


__all__ = [
    "APPROVE",
    "INTENTS_DIR",
    "MAX_WALK_ATTEMPTS",
    "RECEIPT_FILES",
    "REJECT",
    "RESERVED_PREFIXES",
    "ReceiptReplayer",
    "byte_digest",
    "prior_generation_state_roots",
]
