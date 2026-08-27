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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fleet_graph.dd.dispatch import derive_attempt_id
from fleet_graph.dd.git import run_git
from fleet_graph.dd.lifecycle import Lifecycle, Stage
from fleet_graph.dd.upstream_constants import compute_json_digest
from fleet_graph.dd.vendor import plugin_adapter
from fleet_graph.graphs.dd_actors import implement_stage, review_stages
from fleet_graph.graphs.dd_pipeline import MODE_INITIAL, SPINE_EVENT, Dispatch, Replayed

# The sealed receipt file per plugin-sealed stage, under
# `<state_root>/receipts/<attempt_id>/`. The same table
# `control_plane._sealed_receipt` reads; the byte digest of each file is what
# the next receipt names as its parent.
RECEIPT_FILES = {
    "implement": "implement-receipt.json",
    "continuous_review": "continuous-review-receipt.json",
    "final_review": "final-review-receipt.json",
}

APPROVE = "APPROVE"
REJECT = "REJECT"

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


def byte_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


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
    lifecycle: Lifecycle = field(default_factory=Lifecycle.load)

    def __post_init__(self) -> None:
        self._plan: list[_Step] | None = None
        self._index = 0
        self._disabled = False

    # --- the walker's port ------------------------------------------------

    def replay(self, stage: Stage, dispatch: Dispatch) -> Replayed | None:
        if self._disabled:
            return None
        if (
            int(dispatch.get("attempt", 1)) != 1
            or int(dispatch.get("retry", 0)) != 0
            or str(dispatch.get("mode", "")) != MODE_INITIAL
        ):
            # Rework and retries are real work with real feedback; replay
            # serves only the untouched start of a fresh generation.
            self._disabled = True
            return None
        try:
            if self._plan is None:
                self._plan = self._build_plan()
        except Exception:
            self._plan = []
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
        if self._index == 0:
            try:
                prepared = self._prepare(self._plan)
            except Exception:
                prepared = False
            if not prepared:
                self._disabled = True
                return None
        self._index += 1
        return Replayed(
            event=step.event,
            receipt=dict(step.receipt),
            output_commit=step.output_commit,
        )

    # --- plan construction (read-only) ------------------------------------

    def _build_plan(self) -> list[_Step]:
        implement_id = implement_stage(self.lifecycle)
        configure_id = self._spine_predecessor(implement_id)
        if not implement_id or not configure_id:
            return []
        head = self._rev_parse("HEAD")
        if not head:
            return []
        for generation, root in self.prior_state_roots:
            plan = self._walk(int(generation), Path(root), head, configure_id, implement_id)
            if plan:
                return plan
        return []

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
            steps = [
                configure_step,
                _Step(
                    stage_id=implement_id,
                    event=SPINE_EVENT,
                    receipt=imp,
                    output_commit=str(imp["output_commit"]),
                    raw=imp_raw,
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

            steps.append(
                _Step(
                    stage_id=continuous_id,
                    event=APPROVE,
                    receipt=cr,
                    output_commit=str(cr["output_commit"]),
                    raw=cr_raw,
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

            steps.append(
                _Step(
                    stage_id=final_id,
                    event=APPROVE,
                    receipt=fr,
                    output_commit=str(fr["output_commit"]),
                    raw=fr_raw,
                )
            )
            # Acceptance and everything after it always re-runs: acceptance
            # re-measures, the gate re-asks, the merge re-decides.
            return steps
        return []

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
        if receipt.get("parent_handoff_receipt_digest") != compute_json_digest(rejecting_receipt):
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
        return raw, receipt

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
                push = run_git(
                    self.workspace,
                    "push",
                    "--quiet",
                    f"--force-with-lease={self.remote_ref}:{observed}",
                    self.remote_url,
                    f"{tip}:{self.remote_ref}",
                )
                if push.returncode != 0:
                    return False
            reset = run_git(self.workspace, "reset", "--hard", "--quiet", tip)
            if reset.returncode != 0:
                return False

        target = (
            self.state_root
            / "receipts"
            / derive_attempt_id(self.development_id, self.generation, 1)
        )
        for step in plan:
            filename = RECEIPT_FILES.get(step.stage_id)
            if not step.raw or filename is None:
                continue
            target.mkdir(parents=True, exist_ok=True)
            (target / filename).write_bytes(step.raw)
        return True

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
        proc = run_git(self.workspace, "ls-remote", self.remote_url, self.remote_ref)
        if proc.returncode != 0:
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
    "MAX_WALK_ATTEMPTS",
    "RECEIPT_FILES",
    "REJECT",
    "RESERVED_PREFIXES",
    "ReceiptReplayer",
    "byte_digest",
    "prior_generation_state_roots",
]
