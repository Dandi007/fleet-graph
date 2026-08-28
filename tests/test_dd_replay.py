"""Replaying a restarted generation from its sealed receipts (F4).

The scenario is dev-fg-4628ef887564: generation n sealed implement, a later
stage failed, and generation n+1 used to re-dispatch a fresh implement actor
against a tree that already contains the work -- which honestly reports
BLOCKED, and the line jams. These tests fabricate the two generations on a
real git repo and prove the receipt prefix replays mechanically, the chain
breaks re-run for real, and REJECT is never replayed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import DEVELOPMENT_ID, git, head
from fleet_graph.dd import chain_rules
from fleet_graph.dd.dispatch import derive_attempt_id
from fleet_graph.dd.lifecycle import Lifecycle
from fleet_graph.dd.upstream_constants import (
    ATTEMPT_CONTEXT_CONTRACT_VERSION,
    compute_json_digest,
)
from fleet_graph.dd.vendor import plugin_adapter
from fleet_graph.graphs.dd_pipeline import (
    MODE_INITIAL,
    MODE_REWORK,
    TERMINAL_COMPLETE,
    build_dd_pipeline_graph,
    initial_state,
)
from fleet_graph.graphs.dd_replay import (
    ReceiptReplayer,
    byte_digest,
    prior_generation_state_roots,
)
from fleet_graph.graphs.dd_scripts import RUN_CONFIG_PATH, AcceptanceStage
from test_dd_pipeline import ContractActor, make_deps

LIFECYCLE = Lifecycle.load()


def commit_file(repo: Path, relative: str, content: str, message: str = "seal") -> str:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return head(repo)


def implement_receipt(seed: str, configure: str, implement: str) -> dict[str, Any]:
    """A complete applied implement receipt, chained to its configure link."""
    receipt = {
        "actor_job_id": "job-1",
        "artifacts": [],
        "attempt_id": derive_attempt_id(DEVELOPMENT_ID, 1, 1),
        "contract_version": ATTEMPT_CONTEXT_CONTRACT_VERSION,
        "development_id": DEVELOPMENT_ID,
        "feedback_digest": "sha256:" + "0" * 64,
        "input_commit": configure,
        "materialization_intent_id": "intent-implement",
        "output_commit": implement,
        "parent_handoff_receipt_digest": compute_json_digest(
            {"stage": "configure", "input_commit": seed, "output_commit": configure}
        ),
        "spec_digest": "sha256:" + "1" * 64,
        "verification_record": {"verification_commands": []},
        "work_head_commit": implement,
    }
    assert set(receipt) == plugin_adapter.IMPLEMENT_RECEIPT_FIELDS
    return receipt


def review_receipt(
    *, parent_digest: str, subject: str, output: str, verdict: str, phase: str = "continuous"
) -> dict[str, Any]:
    receipt = {
        "attempt_id": derive_attempt_id(DEVELOPMENT_ID, 1, 1),
        "contract_version": ATTEMPT_CONTEXT_CONTRACT_VERSION,
        "development_id": DEVELOPMENT_ID,
        "feedback_index": {"entries": []},
        "implementation_handoff_receipt_digest": parent_digest,
        "implementation_subject_commit": subject,
        "input_commit": subject,
        "materialization_intent_id": f"intent-{phase}",
        "output_commit": output,
        "parent_handoff_receipt_digest": parent_digest,
        "review_artifact": {"path": "x"},
        "review_id": f"R-{phase}",
        "review_phase": phase,
        "reviewer_job_id": "rev-1",
        "subject_commit": subject,
        "verdict": verdict,
    }
    assert set(receipt) == plugin_adapter.REVIEW_RECEIPT_FIELDS
    return receipt


def write_receipt(
    state_root: Path, generation: int, attempt: int, filename: str, receipt: dict[str, Any]
) -> bytes:
    raw = json.dumps(receipt, sort_keys=True).encode("utf-8")
    directory = state_root / "receipts" / derive_attempt_id(DEVELOPMENT_ID, generation, attempt)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_bytes(raw)
    return raw


def write_intent(state_root: Path, receipt: dict[str, Any]) -> bytes:
    """The frozen materialization intent a receipt was sealed with, at the
    plugin's `intents/<intent_id>.json` layout."""
    intent_id = str(receipt.get("materialization_intent_id") or "")
    assert intent_id
    raw = json.dumps(
        {"materialization_intent_id": intent_id, "kind": "review_materialization_intent"},
        sort_keys=True,
    ).encode("utf-8")
    directory = state_root / "intents"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{intent_id}.json").write_bytes(raw)
    return raw


class G1:
    """One previous generation: configure and implement sealed on a real repo."""

    def __init__(self, repo: Path, tmp_path: Path) -> None:
        self.repo = repo
        self.dev_root = tmp_path / "dd"
        self.state_root = self.dev_root / "state"
        self.seed = head(repo)
        self.configure = commit_file(repo, ".dev-dispatch/run-config.json", '{"generation": 1}')
        self.implement = commit_file(repo, "product.py", "print('done')\n")
        self.receipt = implement_receipt(self.seed, self.configure, self.implement)
        self.raw = write_receipt(self.state_root, 1, 1, "implement-receipt.json", self.receipt)

    def junk_configure(self) -> str:
        """The pre-F4 restart's dead weight: a fresh configure re-seal."""
        return commit_file(self.repo, ".dev-dispatch/run-config.json", '{"generation": 2}')

    def replayer(self) -> ReceiptReplayer:
        return ReceiptReplayer(
            workspace=self.repo,
            state_root=self.dev_root / "g2" / "state",
            prior_state_roots=((1, self.state_root),),
            development_id=DEVELOPMENT_ID,
            generation=2,
            lifecycle=LIFECYCLE,
        )

    def installed(self, filename: str) -> Path:
        # Under the receipt's own sealed identity (g1 attempt 1), never a
        # re-derived g2 one: the plugin reads the parent receipt at the
        # dispatch's attempt id and refuses an embedded identity that
        # differs, so the install path and the pinned dispatch id must both
        # be the receipt's own.
        attempt_id = derive_attempt_id(DEVELOPMENT_ID, 1, 1)
        return self.dev_root / "g2" / "state" / "receipts" / attempt_id / filename

    def installed_intent(self, intent_id: str) -> Path:
        return self.dev_root / "g2" / "state" / "intents" / f"{intent_id}.json"


def run_generation_two(deps: Any, head_commit: str) -> dict[str, Any]:
    graph = build_dd_pipeline_graph(deps).compile()
    start = initial_state(
        development_id=DEVELOPMENT_ID,
        stage="configure",
        head_commit=head_commit,
        artifacts={"spec": head_commit},
        generation=2,
    )
    return graph.invoke(start, config={"recursion_limit": 200})


def replayed_stages(state: dict[str, Any]) -> list[str]:
    return [e["stage"] for e in state["history"] if e.get("replayed")]


def committed_index(repo: Path, entries: list[dict[str, Any]], message: str = "seal index") -> str:
    """Commit a feedback index at the carrier's path, returning HEAD.

    The replayer reads the inherited index from ``HEAD:.dev-dispatch/feedback/
    index.json`` to decide whether a partial [configure, implement] prefix would
    be followed by a legal new attempt. This fabricates that committed state.
    """
    index = {
        "contract_version": ATTEMPT_CONTEXT_CONTRACT_VERSION,
        "development_id": DEVELOPMENT_ID,
        "entries": entries,
    }
    return commit_file(
        repo,
        ".dev-dispatch/feedback/index.json",
        json.dumps(index, sort_keys=True),
        message,
    )


def index_entry(generation: int, attempt: int, phase: str, verdict: str) -> dict[str, Any]:
    """A feedback entry carrying the durable attempt identity for one generation.

    The ordering rule is generation-aware: an entry from an older generation is
    immutable history and must not steer the current generation's attempt order,
    which is what the derived ``attempt_id`` lets the replayer recognise.
    """
    return {
        "attempt": attempt,
        "attempt_id": derive_attempt_id(DEVELOPMENT_ID, generation, attempt),
        "review_id": f"r-{phase}-g{generation}-a{attempt}",
        "review_phase": phase,
        "subject_commit": "0" * 40,
        "implementation_subject_commit": "0" * 40,
        "verdict": verdict,
        "artifact_path": ".dev-dispatch/reviews/x.json",
        "artifact_blob_oid": "0" * 40,
        "artifact_digest": "sha256:" + "0" * 64,
    }


class TestASealedPrefixReplays:
    def test_the_implement_prefix_replays_and_the_review_runs_real(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """The F4 scenario end to end: no fresh implement agent is dispatched
        against a tree that already carries the work."""
        g1 = G1(repo, tmp_path)
        junk = g1.junk_configure()
        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run_generation_two(make_deps(actor=actor, replayer=g1.replayer()), head_commit=junk)

        assert state["terminal"] == TERMINAL_COMPLETE
        assert [stage for stage, _ in actor.calls] == [
            "continuous_review",
            "final_review",
            "acceptance",
            "human_gate",
            "merger",
        ], "neither configure nor implement may be re-dispatched"
        assert replayed_stages(state) == ["configure", "implement"]
        implement_entry = state["history"][1]
        assert implement_entry["output_commit"] == g1.implement

    def test_the_dead_weight_above_the_tip_is_trimmed(self, repo: Path, tmp_path: Path) -> None:
        """The plugin sealer requires remote head == input commit, so the
        junk configure commit of the failed restart must go."""
        g1 = G1(repo, tmp_path)
        junk = g1.junk_configure()
        assert head(repo) == junk
        run_generation_two(make_deps(actor=ContractActor(), replayer=g1.replayer()), junk)
        assert head(repo) == g1.implement

    def test_the_replayed_receipt_bytes_are_installed_for_this_generation(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """A later real seal names its parent by the file's byte digest, read
        from this generation's receipts directory -- so the bytes must be
        there, and must be exactly the sealed bytes."""
        g1 = G1(repo, tmp_path)
        run_generation_two(
            make_deps(actor=ContractActor(), replayer=g1.replayer()), g1.junk_configure()
        )
        installed = g1.installed("implement-receipt.json")
        assert installed.read_bytes() == g1.raw
        assert byte_digest(installed.read_bytes()) == byte_digest(g1.raw)

    def test_an_approved_review_is_replayed_too(self, repo: Path, tmp_path: Path) -> None:
        g1 = G1(repo, tmp_path)
        review_commit = commit_file(
            repo, ".dev-dispatch/reviews/continuous/g1-a1.json", '{"verdict": "APPROVE"}'
        )
        receipt = review_receipt(
            parent_digest=byte_digest(g1.raw),
            subject=g1.implement,
            output=review_commit,
            verdict="APPROVE",
        )
        raw = write_receipt(g1.state_root, 1, 1, "continuous-review-receipt.json", receipt)
        write_intent(g1.state_root, receipt)
        junk = g1.junk_configure()

        actor = ContractActor({"final_review": ["APPROVE"]})
        state = run_generation_two(make_deps(actor=actor, replayer=g1.replayer()), junk)

        assert state["terminal"] == TERMINAL_COMPLETE
        assert replayed_stages(state) == ["configure", "implement", "continuous_review"]
        assert next(stage for stage, _ in actor.calls) == "final_review"
        assert head(repo) == review_commit
        assert g1.installed("continuous-review-receipt.json").read_bytes() == raw

    def test_the_replayed_review_carries_its_frozen_intent(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """The RECEIPT_CONFLICT root fix: the Review sealer re-reads the
        Continuous intent to seal a Final review, so replay must install it
        beside the receipt -- and, here, the whole line still progresses
        through final review to a terminal (and merge) seal."""
        g1 = G1(repo, tmp_path)
        review_commit = commit_file(
            repo, ".dev-dispatch/reviews/continuous/g1-a1.json", '{"verdict": "APPROVE"}'
        )
        receipt = review_receipt(
            parent_digest=byte_digest(g1.raw),
            subject=g1.implement,
            output=review_commit,
            verdict="APPROVE",
        )
        write_receipt(g1.state_root, 1, 1, "continuous-review-receipt.json", receipt)
        intent_raw = write_intent(g1.state_root, receipt)
        junk = g1.junk_configure()

        actor = ContractActor({"final_review": ["APPROVE"]})
        state = run_generation_two(make_deps(actor=actor, replayer=g1.replayer()), junk)

        assert state["terminal"] == TERMINAL_COMPLETE
        assert replayed_stages(state) == ["configure", "implement", "continuous_review"]
        installed = g1.installed_intent(receipt["materialization_intent_id"])
        assert installed.read_bytes() == intent_raw
        assert byte_digest(installed.read_bytes()) == byte_digest(intent_raw)

    def test_a_review_whose_intent_is_missing_reruns_for_real(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """A replayed review whose frozen intent the source generation no
        longer holds is an un-rechargeable link: it re-runs for real rather
        than replaying half a link the next materialization cannot continue."""
        g1 = G1(repo, tmp_path)
        review_commit = commit_file(
            repo, ".dev-dispatch/reviews/continuous/g1-a1.json", '{"verdict": "APPROVE"}'
        )
        write_receipt(
            g1.state_root,
            1,
            1,
            "continuous-review-receipt.json",
            review_receipt(
                parent_digest=byte_digest(g1.raw),
                subject=g1.implement,
                output=review_commit,
                verdict="APPROVE",
            ),
        )
        junk = g1.junk_configure()

        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run_generation_two(make_deps(actor=actor, replayer=g1.replayer()), junk)

        assert state["terminal"] == TERMINAL_COMPLETE
        assert replayed_stages(state) == ["configure", "implement"]
        assert next(stage for stage, _ in actor.calls) == "continuous_review"


class TestABrokenChainRunsRealFromTheBreak:
    def test_a_chain_broken_at_implement_replays_nothing(self, repo: Path, tmp_path: Path) -> None:
        g1 = G1(repo, tmp_path)
        broken = dict(g1.receipt)
        broken["parent_handoff_receipt_digest"] = "sha256:" + "f" * 64
        write_receipt(g1.state_root, 1, 1, "implement-receipt.json", broken)
        junk = g1.junk_configure()

        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run_generation_two(make_deps(actor=actor, replayer=g1.replayer()), junk)

        assert state["terminal"] == TERMINAL_COMPLETE
        assert next(stage for stage, _ in actor.calls) == "configure"
        assert replayed_stages(state) == []
        assert head(repo) == junk, "a refused replay must not touch the tree"

    def test_a_chain_broken_at_the_review_link_reruns_the_review(
        self, repo: Path, tmp_path: Path
    ) -> None:
        g1 = G1(repo, tmp_path)
        write_receipt(
            g1.state_root,
            1,
            1,
            "continuous-review-receipt.json",
            review_receipt(
                parent_digest="sha256:" + "f" * 64,  # names a parent that never sealed
                subject=g1.implement,
                output=g1.implement,
                verdict="APPROVE",
            ),
        )
        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run_generation_two(
            make_deps(actor=actor, replayer=g1.replayer()), g1.junk_configure()
        )

        assert state["terminal"] == TERMINAL_COMPLETE
        assert replayed_stages(state) == ["configure", "implement"]
        assert next(stage for stage, _ in actor.calls) == "continuous_review"


class TestARejectionIsNeverReplayed:
    def test_a_rejected_review_reruns_for_real(self, repo: Path, tmp_path: Path) -> None:
        """The REJECT receipt is complete and chained -- and still not
        replayed: only the success/APPROVE prefix is."""
        g1 = G1(repo, tmp_path)
        review_commit = commit_file(
            repo, ".dev-dispatch/reviews/continuous/g1-a1.json", '{"verdict": "REJECT"}'
        )
        write_receipt(
            g1.state_root,
            1,
            1,
            "continuous-review-receipt.json",
            review_receipt(
                parent_digest=byte_digest(g1.raw),
                subject=g1.implement,
                output=review_commit,
                verdict="REJECT",
            ),
        )
        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run_generation_two(make_deps(actor=actor, replayer=g1.replayer()), head(repo))

        assert state["terminal"] == TERMINAL_COMPLETE
        assert replayed_stages(state) == ["configure", "implement"]
        assert next(stage for stage, _ in actor.calls) == "continuous_review"
        assert head(repo) == g1.implement, "the rejected review's seal is dead weight"


class TestAReviewedChainContinuesThroughItsReviews:
    """The ORDER_VIOLATION lesson: a replayed implementation is not a new
    implementation attempt. A restarted generation that reuses configure and
    implement must carry its sealed reviews along -- replay them, not
    materialise a fresh continuous review the feedback carrier would reject as
    "a new attempt requiring a prior REJECT"."""

    def test_a_fully_reviewed_chain_replays_through_to_acceptance(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """An accepted/reviewed chain is replayed into a later generation: the
        replayed configure and implement are reused, the continuous-review
        handoff materialises alongside them, and the walk reaches acceptance
        with no fresh review -- hence no illegal new attempt."""
        g1 = G1(repo, tmp_path)
        rc = commit_file(
            repo, ".dev-dispatch/reviews/continuous/g1-a1.json", '{"verdict": "APPROVE"}'
        )
        cr = review_receipt(
            parent_digest=byte_digest(g1.raw),
            subject=g1.implement,
            output=rc,
            verdict="APPROVE",
        )
        cr_raw = write_receipt(g1.state_root, 1, 1, "continuous-review-receipt.json", cr)
        write_intent(g1.state_root, cr)
        rf = commit_file(repo, ".dev-dispatch/reviews/final/g1-a1.json", '{"verdict": "APPROVE"}')
        fr = review_receipt(
            parent_digest=byte_digest(cr_raw),
            subject=rc,
            output=rf,
            verdict="APPROVE",
            phase="final",
        )
        write_receipt(g1.state_root, 1, 1, "final-review-receipt.json", fr)
        write_intent(g1.state_root, fr)
        junk = g1.junk_configure()

        actor = ContractActor()
        state = run_generation_two(make_deps(actor=actor, replayer=g1.replayer()), junk)

        assert state["terminal"] == TERMINAL_COMPLETE
        assert replayed_stages(state) == [
            "configure",
            "implement",
            "continuous_review",
            "final_review",
        ]
        assert [stage for stage, _ in actor.calls] == ["acceptance", "human_gate", "merger"]
        # The continuous- and final-review handoffs materialise alongside the
        # replayed configure/implement: their sealed receipt bytes are installed
        # under the identity they were sealed with, so the walk continues into
        # acceptance without a fresh review (no new attempt is created).
        assert g1.installed("continuous-review-receipt.json").read_bytes() == cr_raw
        assert g1.installed("final-review-receipt.json").exists()

    def test_a_final_approve_chain_with_only_a_missing_continuous_intent_replays_through(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """The Continuous intent is re-read only by the Final *sealer*. When the
        Final review is itself replayed, nothing re-reads it, so a missing
        Continuous intent must not truncate the replay to a bare [configure,
        implement] prefix that would then materialise a fresh continuous review
        -- the ORDER_VIOLATION path. The accepted chain continues through its
        reviews to acceptance with no new attempt."""
        g1 = G1(repo, tmp_path)
        rc = commit_file(
            repo, ".dev-dispatch/reviews/continuous/g1-a1.json", '{"verdict": "APPROVE"}'
        )
        cr = review_receipt(
            parent_digest=byte_digest(g1.raw),
            subject=g1.implement,
            output=rc,
            verdict="APPROVE",
        )
        cr_raw = write_receipt(g1.state_root, 1, 1, "continuous-review-receipt.json", cr)
        # Deliberately no write_intent for the continuous review.
        rf = commit_file(repo, ".dev-dispatch/reviews/final/g1-a1.json", '{"verdict": "APPROVE"}')
        fr = review_receipt(
            parent_digest=byte_digest(cr_raw),
            subject=rc,
            output=rf,
            verdict="APPROVE",
            phase="final",
        )
        write_receipt(g1.state_root, 1, 1, "final-review-receipt.json", fr)
        write_intent(g1.state_root, fr)
        junk = g1.junk_configure()

        actor = ContractActor()
        state = run_generation_two(make_deps(actor=actor, replayer=g1.replayer()), junk)

        assert state["terminal"] == TERMINAL_COMPLETE, state.get("terminal_reason")
        assert replayed_stages(state) == [
            "configure",
            "implement",
            "continuous_review",
            "final_review",
        ]
        assert [stage for stage, _ in actor.calls] == ["acceptance", "human_gate", "merger"]

    def test_the_review_bearing_prefix_is_preferred_over_a_partial_one(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """A generation that crashed mid-review sealed only implement. A later
        generation must still replay the reviews the earlier one sealed, not
        materialise a brand-new review of a prefix that stops at implement."""
        g1 = G1(repo, tmp_path)
        rc = commit_file(
            repo, ".dev-dispatch/reviews/continuous/g1-a1.json", '{"verdict": "APPROVE"}'
        )
        cr = review_receipt(
            parent_digest=byte_digest(g1.raw),
            subject=g1.implement,
            output=rc,
            verdict="APPROVE",
        )
        cr_raw = write_receipt(g1.state_root, 1, 1, "continuous-review-receipt.json", cr)
        write_intent(g1.state_root, cr)
        rf = commit_file(repo, ".dev-dispatch/reviews/final/g1-a1.json", '{"verdict": "APPROVE"}')
        fr = review_receipt(
            parent_digest=byte_digest(cr_raw),
            subject=rc,
            output=rf,
            verdict="APPROVE",
            phase="final",
        )
        write_receipt(g1.state_root, 1, 1, "final-review-receipt.json", fr)
        write_intent(g1.state_root, fr)

        g2_state = tmp_path / "dd" / "g2" / "state"
        g2_configure = commit_file(repo, ".dev-dispatch/run-config.json", '{"generation": 2}')
        g2_implement = commit_file(repo, "product.py", "print('g2')\n")
        g2_imp = implement_receipt(rf, g2_configure, g2_implement)
        g2_imp["attempt_id"] = derive_attempt_id(DEVELOPMENT_ID, 2, 1)
        write_receipt(g2_state, 2, 1, "implement-receipt.json", g2_imp)

        replayer = ReceiptReplayer(
            workspace=repo,
            state_root=tmp_path / "dd" / "g3" / "state",
            prior_state_roots=((2, g2_state), (1, g1.state_root)),
            development_id=DEVELOPMENT_ID,
            generation=3,
            lifecycle=LIFECYCLE,
        )
        plans = replayer._candidate_plans()
        assert [step.stage_id for step in plans[0]] == [
            "configure",
            "implement",
            "continuous_review",
            "final_review",
        ]

    def test_a_divergent_unreviewed_generation_refuses_replay(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """Product drift above the reviewed tip refuses replay outright.

        gen 1 sealed a fully reviewed, approved chain. gen 2 then re-ran
        configure + implement and sealed a real (product-touching) implement
        above gen 1's tip before crashing, so its review never sealed. gen 3's
        strongest candidate -- gen 1's four-step prefix -- cannot be prepared
        because `product.py` above its tip is product drift. The replayer must
        not fall back to gen 2's `[configure, implement]` prefix: replaying
        that divergent, unreviewed prefix would hand the feedback carrier a
        fresh continuous review of work whose inherited chain did not end in
        REJECT (ORDER_VIOLATION). It fails closed -- no replay, no trim -- so
        the divergent work is surfaced rather than silently re-reviewed.
        """
        g1 = G1(repo, tmp_path)
        rc = commit_file(
            repo, ".dev-dispatch/reviews/continuous/g1-a1.json", '{"verdict": "APPROVE"}'
        )
        cr = review_receipt(
            parent_digest=byte_digest(g1.raw),
            subject=g1.implement,
            output=rc,
            verdict="APPROVE",
        )
        cr_raw = write_receipt(g1.state_root, 1, 1, "continuous-review-receipt.json", cr)
        write_intent(g1.state_root, cr)
        rf = commit_file(repo, ".dev-dispatch/reviews/final/g1-a1.json", '{"verdict": "APPROVE"}')
        fr = review_receipt(
            parent_digest=byte_digest(cr_raw),
            subject=rc,
            output=rf,
            verdict="APPROVE",
            phase="final",
        )
        write_receipt(g1.state_root, 1, 1, "final-review-receipt.json", fr)
        write_intent(g1.state_root, fr)

        g2_state = tmp_path / "dd" / "g2" / "state"
        g2_configure = commit_file(repo, ".dev-dispatch/run-config.json", '{"generation": 2}')
        g2_implement = commit_file(repo, "product.py", "print('g2')\n")
        g2_imp = implement_receipt(rf, g2_configure, g2_implement)
        g2_imp["attempt_id"] = derive_attempt_id(DEVELOPMENT_ID, 2, 1)
        write_receipt(g2_state, 2, 1, "implement-receipt.json", g2_imp)

        replayer = ReceiptReplayer(
            workspace=repo,
            state_root=tmp_path / "dd" / "g3" / "state",
            prior_state_roots=((2, g2_state), (1, g1.state_root)),
            development_id=DEVELOPMENT_ID,
            generation=3,
            lifecycle=LIFECYCLE,
        )
        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        graph = build_dd_pipeline_graph(make_deps(actor=actor, replayer=replayer)).compile()
        state = graph.invoke(
            initial_state(
                development_id=DEVELOPMENT_ID,
                stage="configure",
                head_commit=g2_implement,
                artifacts={"spec": g2_implement},
                generation=3,
            ),
            config={"recursion_limit": 200},
        )

        assert replayed_stages(state) == []
        assert next(stage for stage, _ in actor.calls) == "configure"
        assert head(repo) == g2_implement

    def test_a_rework_still_returns_a_derived_identity(self, repo: Path, tmp_path: Path) -> None:
        """The complementary boundary: a genuine new attempt (a rework after a
        REJECT) is new work under its own derived identity, not a continuation
        of the replayed prefix. Replaying configure and implement must not
        pretend a rework never happened."""
        g1 = G1(repo, tmp_path)
        seen: list[tuple[str, str]] = []

        class Recorder(ContractActor):
            def act(self, stage: Any, dispatch: dict[str, Any]) -> Any:
                seen.append((stage.id, str(dispatch.get("pinned_attempt_id") or "")))
                return super().act(stage, dispatch)

        actor = Recorder({"continuous_review": ["REJECT", "APPROVE"], "final_review": ["APPROVE"]})
        state = run_generation_two(
            make_deps(actor=actor, replayer=g1.replayer()), g1.junk_configure()
        )

        assert state["terminal"] == TERMINAL_COMPLETE
        sealed_identity = g1.receipt["attempt_id"]
        assert ("continuous_review", sealed_identity) in seen
        assert ("implement", "") in seen, "the rework implement must not inherit the pin"


class TestAPartialPrefixRespectsTheInheritedChain:
    """A replayed prefix that stops at implement is always followed by a fresh
    continuous review -- a brand-new attempt. The replayer reads the inherited
    feedback index and only replays that partial prefix when the fresh review
    would be legal (the inherited chain ends in REJECT). Otherwise it fails
    closed, so the carrier -- not the replayer -- stays the authority that
    rejects a genuine new attempt without a prior REJECT (spec requirements 1,
    2 and 4)."""

    def test_the_new_attempt_rule_mirrors_the_carrier(self) -> None:
        assert chain_rules.new_attempt_is_legal([]) is True
        assert (
            chain_rules.new_attempt_is_legal([{"review_phase": "final", "verdict": "REJECT"}])
            is True
        )
        assert (
            chain_rules.new_attempt_is_legal([{"review_phase": "continuous", "verdict": "APPROVE"}])
            is False
        )
        assert (
            chain_rules.new_attempt_is_legal(
                [
                    {"review_phase": "continuous", "verdict": "APPROVE"},
                    {"review_phase": "final", "verdict": "APPROVE"},
                ]
            )
            is False
        )

    def test_the_rule_is_generation_aware(self) -> None:
        """Historical entries from an older generation do not impose a prior
        REJECT on the current generation's fresh attempt; entries from the same
        generation still do (spec requirements 1, 2 and 3)."""
        accepted_history = [
            index_entry(1, 1, "continuous", "APPROVE"),
            index_entry(1, 1, "final", "APPROVE"),
        ]
        assert (
            chain_rules.new_attempt_is_legal(
                accepted_history, generation=2, development_id=DEVELOPMENT_ID
            )
            is True
        ), "an older generation's accepted history must not block a fresh generation"
        # Same generation: a fresh attempt after an APPROVE chain is illegal.
        assert (
            chain_rules.new_attempt_is_legal(
                [
                    index_entry(2, 1, "continuous", "APPROVE"),
                    index_entry(2, 1, "final", "APPROVE"),
                ],
                generation=2,
                development_id=DEVELOPMENT_ID,
            )
            is False
        )
        assert (
            chain_rules.new_attempt_is_legal(
                [
                    index_entry(2, 1, "continuous", "APPROVE"),
                    index_entry(2, 1, "final", "REJECT"),
                ],
                generation=2,
                development_id=DEVELOPMENT_ID,
            )
            is True
        )

    def test_a_partial_prefix_is_declined_when_the_inherited_chain_did_not_end_in_reject(
        self, repo: Path, tmp_path: Path
    ) -> None:
        g1 = G1(repo, tmp_path)
        committed_index(
            repo,
            [
                index_entry(2, 1, "continuous", "APPROVE"),
                index_entry(2, 1, "final", "APPROVE"),
            ],
        )
        junk = g1.junk_configure()

        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run_generation_two(make_deps(actor=actor, replayer=g1.replayer()), junk)

        assert replayed_stages(state) == []
        assert next(stage for stage, _ in actor.calls) == "configure"
        assert head(repo) == junk, "a refused replay must not touch the tree"

    def test_a_partial_prefix_replays_when_the_inherited_chain_ended_in_reject(
        self, repo: Path, tmp_path: Path
    ) -> None:
        g1 = G1(repo, tmp_path)
        committed_index(
            repo,
            [
                index_entry(2, 1, "continuous", "APPROVE"),
                index_entry(2, 1, "final", "REJECT"),
            ],
        )
        junk = g1.junk_configure()

        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run_generation_two(make_deps(actor=actor, replayer=g1.replayer()), junk)

        assert state["terminal"] == TERMINAL_COMPLETE
        assert replayed_stages(state) == ["configure", "implement"]
        assert next(stage for stage, _ in actor.calls) == "continuous_review"
        assert head(repo) == g1.implement

    def test_a_cross_generation_history_still_materializes_its_continuous_review(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """The reported defect (dev-fg-31b963659d16): generation 1 ran
        attempt 1 (continuous APPROVE, final REJECT) then attempt 2 (continuous
        APPROVE, final APPROVE, accepted), and a later generation inherited that
        committed feedback index but only a [configure, implement] prefix's
        receipts were re-discoverable. The fresh generation's continuous review
        must still materialise -- its attempt is a legal first entry of its own
        chain, not a new attempt inside generation 1's (spec requirement 4)."""
        g1 = G1(repo, tmp_path)
        committed_index(
            repo,
            [
                index_entry(1, 1, "continuous", "APPROVE"),
                index_entry(1, 1, "final", "REJECT"),
                index_entry(1, 2, "continuous", "APPROVE"),
                index_entry(1, 2, "final", "APPROVE"),
            ],
        )
        junk = g1.junk_configure()

        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run_generation_two(make_deps(actor=actor, replayer=g1.replayer()), junk)

        assert state["terminal"] == TERMINAL_COMPLETE
        assert replayed_stages(state) == ["configure", "implement"]
        # The materialised continuous review is the first real stage: the
        # partial prefix replayed, the trim moved the tree to the sealed tip,
        # and no fresh review was declined (structured result, not a shell
        # error's absence).
        assert next(stage for stage, _ in actor.calls) == "continuous_review"
        assert head(repo) == g1.implement


class TestTheReplayedIdentityBindsTheRealReview:
    """The g4 lesson: the review of replayed work must dispatch under the
    identity the implement receipt was sealed with, or the plugin refuses
    with BINDING_MISMATCH "Implement receipt identity does not match Review
    dispatch"."""

    def test_the_first_real_review_dispatch_carries_the_replayed_identity(
        self, repo: Path, tmp_path: Path
    ) -> None:
        g1 = G1(repo, tmp_path)
        seen: list[tuple[str, str]] = []

        class Recorder(ContractActor):
            def act(self, stage: Any, dispatch: dict[str, Any]) -> Any:
                seen.append((stage.id, str(dispatch.get("pinned_attempt_id") or "")))
                return super().act(stage, dispatch)

        actor = Recorder({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run_generation_two(
            make_deps(actor=actor, replayer=g1.replayer()), g1.junk_configure()
        )

        assert state["terminal"] == TERMINAL_COMPLETE
        sealed_identity = g1.receipt["attempt_id"]
        assert sealed_identity == derive_attempt_id(DEVELOPMENT_ID, 1, 1)
        assert ("continuous_review", sealed_identity) in seen
        assert ("final_review", sealed_identity) in seen

    def test_a_rework_returns_to_a_derived_identity(self, repo: Path, tmp_path: Path) -> None:
        """A new attempt is new work: the pin ends where the rework begins."""
        g1 = G1(repo, tmp_path)
        seen: list[tuple[str, int, str]] = []

        class Recorder(ContractActor):
            def act(self, stage: Any, dispatch: dict[str, Any]) -> Any:
                seen.append(
                    (
                        stage.id,
                        int(dispatch.get("attempt", 1)),
                        str(dispatch.get("pinned_attempt_id") or ""),
                    )
                )
                return super().act(stage, dispatch)

        actor = Recorder({"continuous_review": ["REJECT", "APPROVE"], "final_review": ["APPROVE"]})
        state = run_generation_two(
            make_deps(actor=actor, replayer=g1.replayer()), g1.junk_configure()
        )

        assert state["terminal"] == TERMINAL_COMPLETE
        sealed_identity = g1.receipt["attempt_id"]
        assert ("continuous_review", 1, sealed_identity) in seen
        assert ("implement", 2, "") in seen, "the rework implement must not inherit the pin"
        assert ("continuous_review", 2, "") in seen

    def test_a_receipt_claiming_another_identity_is_not_a_link(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """Provenance stays receipt-only: a receipt whose embedded attempt_id
        is not the identity it was sealed under replays nothing."""
        g1 = G1(repo, tmp_path)
        liar = dict(g1.receipt)
        liar["attempt_id"] = derive_attempt_id(DEVELOPMENT_ID, 9, 9)
        write_receipt(g1.state_root, 1, 1, "implement-receipt.json", liar)
        junk = g1.junk_configure()

        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run_generation_two(make_deps(actor=actor, replayer=g1.replayer()), junk)

        assert replayed_stages(state) == []
        assert next(stage for stage, _ in actor.calls) == "configure"
        assert head(repo) == junk


class TestReplayFailsClosed:
    def test_product_drift_above_the_tip_refuses_the_whole_replay(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """Trimming may cut only the reserved namespaces. A product file above
        the sealed tip means the tree is not the sealed chain plus dead
        weight, so nothing is replayed and nothing is cut."""
        g1 = G1(repo, tmp_path)
        drifted = commit_file(repo, "stray-product.py", "print('who wrote this')\n")

        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run_generation_two(make_deps(actor=actor, replayer=g1.replayer()), drifted)

        assert replayed_stages(state) == []
        assert next(stage for stage, _ in actor.calls) == "configure"
        assert head(repo) == drifted

    def test_a_rework_dispatch_is_never_replayed(self, repo: Path, tmp_path: Path) -> None:
        g1 = G1(repo, tmp_path)
        replayer = g1.replayer()
        rework = {"attempt": 2, "retry": 0, "mode": "rework"}
        assert replayer.replay(LIFECYCLE.stages["configure"], rework) is None
        # And the miss is sticky: replay is a prefix, never a hole.
        fresh = {"attempt": 1, "retry": 0, "mode": "initial"}
        assert replayer.replay(LIFECYCLE.stages["configure"], fresh) is None

    def test_a_mid_walk_resume_replays_nothing_and_mutates_nothing(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """A resumed thread consults the replayer from wherever it suspended.
        That is not the prefix, so no replay -- and, critically, no trim."""
        g1 = G1(repo, tmp_path)
        junk = g1.junk_configure()
        replayer = g1.replayer()
        dispatch = {"attempt": 1, "retry": 0, "mode": "initial"}
        assert replayer.replay(LIFECYCLE.stages["implement"], dispatch) is None
        assert head(repo) == junk
        assert not g1.installed("implement-receipt.json").exists()


class TestTheRunnerWiresReplayOnlyWhereItBelongs:
    def test_the_prior_roots_follow_the_control_plane_layout(self) -> None:
        assert prior_generation_state_roots(Path("/dd/dev-x/g3"), 3) == (
            (2, Path("/dd/dev-x/g2/state")),
            (1, Path("/dd/dev-x/state")),
        )

    def test_generation_one_has_nothing_to_replay(self) -> None:
        assert prior_generation_state_roots(Path("/dd/dev-x"), 1) == ()

    def test_an_unrecognized_layout_replays_nothing(self) -> None:
        assert prior_generation_state_roots(Path("/somewhere/custom"), 3) == ()


class TestAReconfiguredContextRewritesTheRunConfig:
    """The reconfigure exit (R1-c) meets replay.

    A legal `development_reconfigure` changes the acceptance context of a
    development; the next generation replays the *sealed* prefix from the
    previous one, and the replayed configure commit carries a stale
    run-config. Acceptance must grade against the reconfigured declaration,
    not the stale file (dev-fg-f8d98b92a6b0 g2: ACCEPTANCE_DECLARATION_MISMATCH
    with an otherwise-correct `acceptance_env`)."""

    def test_a_reconfigured_env_is_written_for_the_fresh_generation(
        self, repo: Path, tmp_path: Path
    ) -> None:
        g1 = G1(repo, tmp_path)
        junk = g1.junk_configure()
        declared = {
            "acceptance_commands": [["true"]],
            "setup_commands": [],
            "acceptance_env": {"PYTHONPATH": "src"},
        }
        replayer = ReceiptReplayer(
            workspace=g1.repo,
            state_root=g1.dev_root / "g2" / "state",
            prior_state_roots=((1, g1.state_root),),
            development_id=DEVELOPMENT_ID,
            generation=2,
            lifecycle=LIFECYCLE,
            run_config=declared,
        )
        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        scripts = {name: actor for name, stage in LIFECYCLE.stages.items() if not stage.is_llm}
        scripts["acceptance"] = AcceptanceStage(
            repo=repo,
            declared=declared["acceptance_commands"],
            setup=declared["setup_commands"],
            env=declared["acceptance_env"],
        )

        state = run_generation_two(make_deps(actor=actor, scripts=scripts, replayer=replayer), junk)

        assert state["terminal"] == TERMINAL_COMPLETE, state.get("terminal_reason")
        # The sealed prefix still replays; configure is not re-dispatched.
        assert replayed_stages(state) == ["configure", "implement"]
        # Configure's output now declares this generation's context, so the
        # acceptance stage graded against the reconfigured env, not g1's empty
        # one.
        config = json.loads((repo / RUN_CONFIG_PATH).read_text(encoding="utf-8"))
        assert config["acceptance_env"] == {"PYTHONPATH": "src"}
        assert config["acceptance_commands"] == [["true"]]
        assert config["generation"] == 2

    def test_an_unchanged_context_leaves_the_replayed_tree_alone(
        self, repo: Path, tmp_path: Path
    ) -> None:
        g1 = G1(repo, tmp_path)
        junk = g1.junk_configure()
        # Same (empty) context as g1 configured: nothing to rewrite.
        replayer = ReceiptReplayer(
            workspace=g1.repo,
            state_root=g1.dev_root / "g2" / "state",
            prior_state_roots=((1, g1.state_root),),
            development_id=DEVELOPMENT_ID,
            generation=2,
            lifecycle=LIFECYCLE,
            run_config={"acceptance_commands": [], "setup_commands": [], "acceptance_env": {}},
        )
        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})

        state = run_generation_two(make_deps(actor=actor, replayer=replayer), junk)

        assert state["terminal"] == TERMINAL_COMPLETE
        assert head(repo) == g1.implement
        # g1's committed run-config still stands: it was not overwritten.
        assert git(repo, "show", f"{g1.configure}:{RUN_CONFIG_PATH}").strip() == '{"generation": 1}'


def write_review_intent(state_root: Path, receipt: dict[str, Any], *, dispatch_mode: str) -> bytes:
    """A review intent that also freezes the `dispatch_mode` the plugin froze,
    so a replayed rework prefix's Continuous intent carries `rework` exactly
    as it would on a real chain."""
    intent_id = str(receipt.get("materialization_intent_id") or "")
    assert intent_id
    raw = json.dumps(
        {
            "materialization_intent_id": intent_id,
            "kind": "review_materialization_intent",
            "dispatch_mode": dispatch_mode,
            "review_phase": receipt.get("review_phase", ""),
        },
        sort_keys=True,
    ).encode("utf-8")
    directory = state_root / "intents"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{intent_id}.json").write_bytes(raw)
    return raw


def implement_receipt_at(
    *, attempt: int, input_commit: str, output_commit: str, parent_digest: str
) -> dict[str, Any]:
    """An applied implement receipt at an arbitrary attempt, for rework chains."""
    receipt = {
        "actor_job_id": "job-1",
        "artifacts": [],
        "attempt_id": derive_attempt_id(DEVELOPMENT_ID, 1, attempt),
        "contract_version": ATTEMPT_CONTEXT_CONTRACT_VERSION,
        "development_id": DEVELOPMENT_ID,
        "feedback_digest": "sha256:" + "0" * 64,
        "input_commit": input_commit,
        "materialization_intent_id": f"intent-implement-a{attempt}",
        "output_commit": output_commit,
        "parent_handoff_receipt_digest": parent_digest,
        "spec_digest": "sha256:" + "1" * 64,
        "verification_record": {"verification_commands": []},
        "work_head_commit": output_commit,
    }
    assert set(receipt) == plugin_adapter.IMPLEMENT_RECEIPT_FIELDS
    return receipt


def review_receipt_at(
    *,
    attempt: int,
    implementation_digest: str,
    parent_digest: str,
    implementation_subject: str,
    subject: str,
    output: str,
    verdict: str,
    phase: str,
    intent_id: str,
) -> dict[str, Any]:
    """A review receipt at an arbitrary attempt, with the two digests a real
    review carries separately (the implement digest and the parent receipt)."""
    receipt = {
        "attempt_id": derive_attempt_id(DEVELOPMENT_ID, 1, attempt),
        "contract_version": ATTEMPT_CONTEXT_CONTRACT_VERSION,
        "development_id": DEVELOPMENT_ID,
        "feedback_index": {"entries": []},
        "implementation_handoff_receipt_digest": implementation_digest,
        "implementation_subject_commit": implementation_subject,
        "input_commit": subject,
        "materialization_intent_id": intent_id,
        "output_commit": output,
        "parent_handoff_receipt_digest": parent_digest,
        "review_artifact": {"path": "x"},
        "review_id": f"R-{phase}-{attempt}",
        "review_phase": phase,
        "reviewer_job_id": "rev-1",
        "subject_commit": subject,
        "verdict": verdict,
    }
    assert set(receipt) == plugin_adapter.REVIEW_RECEIPT_FIELDS
    return receipt


class TestAReworkedPrefixReplaysAsRework:
    """dev-fg-6e4f9345b320 g7: the replayed Continuous intent froze
    `dispatch_mode: "rework"`, while the next generation re-dispatched its real
    final review as `initial` -- BINDING_MISMATCH "persisted Continuous intent
    dispatch_mode does not match its authoritative binding".

    A rework attempts implement + continuous again with mode `rework`; the
    replayed prefix must carry that mode forward so the first real stage (the
    final review) inherits `rework` and the sealer's binding closes."""

    def _reworked_g1(self, repo: Path, tmp_path: Path) -> tuple[G1, str]:
        g1 = G1(repo, tmp_path)
        im1_raw = g1.raw

        # attempt 1 continuous APPROVE
        cr1_commit = commit_file(repo, ".dev-dispatch/reviews/rc-a1.json", '{"verdict": "APPROVE"}')
        cr1 = review_receipt_at(
            attempt=1,
            implementation_digest=byte_digest(im1_raw),
            parent_digest=byte_digest(im1_raw),
            implementation_subject=g1.implement,
            subject=g1.implement,
            output=cr1_commit,
            verdict="APPROVE",
            phase="continuous",
            intent_id="intent-cr-a1",
        )
        cr1_raw = write_receipt(g1.state_root, 1, 1, "continuous-review-receipt.json", cr1)
        write_review_intent(g1.state_root, cr1, dispatch_mode="initial")

        # attempt 1 final REJECT
        fr1_commit = commit_file(repo, ".dev-dispatch/reviews/rf-a1.json", '{"verdict": "REJECT"}')
        fr1 = review_receipt_at(
            attempt=1,
            implementation_digest=byte_digest(im1_raw),
            parent_digest=byte_digest(cr1_raw),
            implementation_subject=g1.implement,
            subject=cr1_commit,
            output=fr1_commit,
            verdict="REJECT",
            phase="final",
            intent_id="intent-fr-a1",
        )
        write_receipt(g1.state_root, 1, 1, "final-review-receipt.json", fr1)

        # attempt 2 rework implement, chained to the rejecting final review
        im2_commit = commit_file(repo, "product.py", "print('reworked')\n")
        im2 = implement_receipt_at(
            attempt=2,
            input_commit=fr1_commit,
            output_commit=im2_commit,
            parent_digest=chain_rules.rework_link_parent(fr1),
        )
        im2_raw = write_receipt(g1.state_root, 1, 2, "implement-receipt.json", im2)

        # attempt 2 rework continuous APPROVE
        cr2_commit = commit_file(repo, ".dev-dispatch/reviews/rc-a2.json", '{"verdict": "APPROVE"}')
        cr2 = review_receipt_at(
            attempt=2,
            implementation_digest=byte_digest(im2_raw),
            parent_digest=byte_digest(im2_raw),
            implementation_subject=im2_commit,
            subject=im2_commit,
            output=cr2_commit,
            verdict="APPROVE",
            phase="continuous",
            intent_id="intent-cr-a2",
        )
        write_receipt(g1.state_root, 1, 2, "continuous-review-receipt.json", cr2)
        write_review_intent(g1.state_root, cr2, dispatch_mode="rework")
        return g1, cr2_commit

    def test_the_final_review_of_a_reworked_prefix_dispatches_rework(
        self, repo: Path, tmp_path: Path
    ) -> None:
        g1, reworked_tip = self._reworked_g1(repo, tmp_path)
        junk = g1.junk_configure()
        modes: list[tuple[str, str]] = []

        class Recorder(ContractActor):
            def act(self, stage: Any, dispatch: dict[str, Any]) -> Any:
                modes.append((stage.id, str(dispatch.get("mode") or "")))
                return super().act(stage, dispatch)

        actor = Recorder({"final_review": ["APPROVE"]})
        state = run_generation_two(make_deps(actor=actor, replayer=g1.replayer()), junk)

        assert state["terminal"] == TERMINAL_COMPLETE, state.get("terminal_reason")
        # The reworked implement + continuous APPROVE prefix replays; the final
        # review is the first real stage and must inherit `rework`.
        assert replayed_stages(state) == ["configure", "implement", "continuous_review"]
        assert modes[:1] == [("final_review", MODE_REWORK)], modes
        assert head(repo) == reworked_tip

    def test_an_initial_prefix_keeps_the_final_review_initial(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """The fix is principled, not a blanket 'replay means rework': an
        ordinary initial prefix replays and the real final review still
        dispatches `initial`."""
        g1 = G1(repo, tmp_path)
        cr_commit = commit_file(repo, ".dev-dispatch/reviews/rc-a1.json", '{"verdict": "APPROVE"}')
        cr = review_receipt_at(
            attempt=1,
            implementation_digest=byte_digest(g1.raw),
            parent_digest=byte_digest(g1.raw),
            implementation_subject=g1.implement,
            subject=g1.implement,
            output=cr_commit,
            verdict="APPROVE",
            phase="continuous",
            intent_id="intent-cr-a1",
        )
        write_receipt(g1.state_root, 1, 1, "continuous-review-receipt.json", cr)
        write_review_intent(g1.state_root, cr, dispatch_mode="initial")
        junk = g1.junk_configure()
        modes: list[tuple[str, str]] = []

        class Recorder(ContractActor):
            def act(self, stage: Any, dispatch: dict[str, Any]) -> Any:
                modes.append((stage.id, str(dispatch.get("mode") or "")))
                return super().act(stage, dispatch)

        actor = Recorder({"final_review": ["APPROVE"]})
        state = run_generation_two(make_deps(actor=actor, replayer=g1.replayer()), junk)

        assert state["terminal"] == TERMINAL_COMPLETE, state.get("terminal_reason")
        assert replayed_stages(state) == ["configure", "implement", "continuous_review"]
        assert modes[:1] == [("final_review", MODE_INITIAL)], modes


class TestANewAttemptWithoutARejectLinkIsNeverReplayed:
    def test_a_rework_that_does_not_name_its_rejecting_review_is_not_a_link(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """The ORDER_VIOLATION-adjacent guard lives on the replay side: a new
        attempt (rework implement) that does not name the rejecting review as
        its parent has no REJECT predecessor, so it must not be replayed as a
        sealed link -- the replayer declines it and the review re-runs for real
        (where the plugin's attempt-context guard may then reject it)."""
        g1 = G1(repo, tmp_path)
        # attempt 1: continuous APPROVE, final REJECT (a real rework steer).
        cr_commit = commit_file(
            repo, ".dev-dispatch/reviews/continuous/g1-a1.json", '{"verdict": "APPROVE"}'
        )
        cr = review_receipt(
            parent_digest=byte_digest(g1.raw),
            subject=g1.implement,
            output=cr_commit,
            verdict="APPROVE",
        )
        write_receipt(g1.state_root, 1, 1, "continuous-review-receipt.json", cr)
        fr_commit = commit_file(
            repo, ".dev-dispatch/reviews/final/g1-a1.json", '{"verdict": "REJECT"}'
        )
        fr = review_receipt(
            parent_digest=byte_digest(
                (
                    g1.state_root
                    / "receipts"
                    / derive_attempt_id(DEVELOPMENT_ID, 1, 1)
                    / "continuous-review-receipt.json"
                ).read_bytes()
            ),
            subject=g1.implement,
            output=fr_commit,
            verdict="REJECT",
            phase="final",
        )
        write_receipt(g1.state_root, 1, 1, "final-review-receipt.json", fr)

        rework_commit = commit_file(repo, ".dd-evidence/rework.json", '{"attempt": 2}')
        # A rework implement that does NOT name the rejecting review: a "new
        # attempt" with no REJECT predecessor, so it is not a rework link.
        rework = {
            "actor_job_id": "job-2",
            "artifacts": [],
            "attempt_id": derive_attempt_id(DEVELOPMENT_ID, 1, 2),
            "contract_version": ATTEMPT_CONTEXT_CONTRACT_VERSION,
            "development_id": DEVELOPMENT_ID,
            "feedback_digest": "sha256:" + "0" * 64,
            "input_commit": fr_commit,
            "materialization_intent_id": "intent-rework",
            "output_commit": rework_commit,
            "parent_handoff_receipt_digest": "sha256:" + "f" * 64,
            "spec_digest": "sha256:" + "1" * 64,
            "verification_record": {"verification_commands": []},
            "work_head_commit": rework_commit,
        }
        assert set(rework) == plugin_adapter.IMPLEMENT_RECEIPT_FIELDS
        write_receipt(g1.state_root, 1, 2, "implement-receipt.json", rework)

        actor = ContractActor({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]})
        state = run_generation_two(make_deps(actor=actor, replayer=g1.replayer()), head(repo))

        # Only the pre-rejection prefix replays: the rework attempt is not a
        # closeable link, so it is declined and the review re-runs for real.
        assert replayed_stages(state) == ["configure", "implement"]
        assert next(stage for stage, _ in actor.calls) == "continuous_review"
