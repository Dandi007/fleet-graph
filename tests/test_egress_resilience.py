"""spec-d16 red targets: egress failure layering and stage-artifact survival.

The 2026-09-04 incident window (board seq 2818) measured egress as
intermittently degraded: three read probes failing with the identical GnuTLS
handshake error, then a green window where everything passed. The five red
targets below are the spec's binding clauses, each mechanically asserted:

1. one injected transport failure must not terminalise the order;
2. the stage observes >=2 attempts with exponential backoff between them
   (2s, 4s ... magnitude, +-50% tolerance);
3. the run continues and completes after the retry;
4. the failure record carries ``root_cause=transport``;
5. after a fault and recovery, succeeded stages never re-run -- no duplicate
   output_commit success lines in the recovery's event sequence.

Timing runs on a virtual clock, so the backoff intervals are measured exactly
without the test (or `make verify`) ever sleeping.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.dd import egress
from fleet_graph.dd.control_plane import classify_failure
from fleet_graph.dd.egress import (
    EGRESS_TRANSPORT,
    REPO_CONFLICT,
    REPO_REJECTED,
    EgressPolicy,
    EgressRepoError,
    RemoteResult,
    TransportExhausted,
    backoff_delay,
    classify_git_failure,
    layer_failure,
    retry_remote,
)
from fleet_graph.dd.lifecycle import Lifecycle
from fleet_graph.graphs import dd_scripts
from fleet_graph.graphs.dd_pipeline import (
    FAILURE_EVENT,
    SPINE_EVENT,
    TERMINAL_COMPLETE,
    TERMINAL_FAULT,
    Dispatch,
    PipelineFault,
    Replayed,
    Sealed,
    StageOutcome,
    build_dd_pipeline_graph,
    initial_state,
)
from fleet_graph.graphs.dd_scripts import WorkspaceSealer
from fleet_graph.state.run_artifacts import iso
from test_dd_pipeline import ContractActor, make_deps, run

#: The measured incident fixture, verbatim (spec 交付面 4 / seq 2818 probes).
GNUTLS_SAMPLE = (
    "fatal: unable to access 'https://git.example.invalid/team/repo.git': "
    "GnuTLS, handshake failed: The TLS connection was non-properly terminated."
)

APPROVES = {"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]}


class VirtualClock:
    """A clock that only moves when the retry executor sleeps on it."""

    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def iso_now(self) -> str:
        return iso(self.now)


class RecordingSealer:
    """Seals fake commits per stage, keeping each stage's receipt."""

    def __init__(self) -> None:
        self.commits: list[str] = []
        self.chain: dict[str, dict[str, Any]] = {}

    def materialize(self, stage: Any, dispatch: Dispatch, outcome: StageOutcome) -> Sealed:
        commit = f"{dispatch['stage']}-g{dispatch['generation']}-a{dispatch['attempt']}"
        self.commits.append(commit)
        receipt = dict(outcome.receipt or {})
        receipt["output_commit"] = commit
        self.chain[stage.id] = receipt
        return Sealed(commit=commit, receipt=receipt)


def _invoke(deps: Any, repo: Path) -> dict[str, Any]:
    """Walk the whole contract from configure, over a real git repo."""
    graph = build_dd_pipeline_graph(deps).compile()
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return graph.invoke(
        initial_state(
            development_id="dev-fg-egress",
            stage="configure",
            head_commit=head,
            artifacts={"spec": head},
        ),
        config={"recursion_limit": 200},
    )


def _bare_remote(repo: Path, tmp_path: Path, name: str) -> Path:
    bare = tmp_path / name
    subprocess.run(
        ["git", "init", "-q", "--bare", str(bare)], capture_output=True, text=True, check=True
    )
    return bare


def _flaky_run_git(monkeypatch: Any, stderr: str, exit_code: int, times: int) -> dict[str, int]:
    """Serve the failure for the first `times` push attempts, then the real git."""
    real = dd_scripts.run_git
    served = {"count": 0}

    def flaky(repo: Path, *args: str, **kwargs: Any):
        if args and args[0] == "push" and served["count"] < times:
            served["count"] += 1
            return subprocess.CompletedProcess(
                ["git", "push", *args[1:]], exit_code, stdout="", stderr=stderr
            )
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(dd_scripts, "run_git", flaky)
    return served


class TestBackoffRetry:
    def test_backoff_retry_ladder_is_exponential_within_the_jitter_band(self) -> None:
        """base 2s, factor 2, single-delay cap 60s, +-20% jitter (交付面 1)."""
        policy = EgressPolicy()
        assert [backoff_delay(n, policy, rand=lambda: 0.5) for n in range(1, 6)] == [
            2.0,
            4.0,
            8.0,
            16.0,
            32.0,
        ]
        # The per-delay cap holds even deep into the ladder.
        assert backoff_delay(12, policy, rand=lambda: 0.5) == 60.0
        # The jitter band: any rand lands within +-20% of the raw delay.
        for attempt, raw in ((1, 2.0), (2, 4.0), (12, 60.0)):
            for rand in (lambda: 0.0, lambda: 1.0, lambda: 0.25, lambda: 0.75):
                delay = backoff_delay(attempt, policy, rand=rand)
                assert abs(delay - raw) <= raw * policy.jitter_fraction + 1e-9

    def test_backoff_retry_stops_at_the_stage_run_fence(self) -> None:
        """Cumulative backoff never exceeds the enclosing stage's fence."""
        calls: list[int] = []

        def failing() -> RemoteResult:
            calls.append(1)
            return RemoteResult(128, GNUTLS_SAMPLE)

        sleeps: list[float] = []

        def record_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        # Fence 5s: one 2s backoff fits; the next 4s delay would push the
        # budget to 6s, so the executor must stop before sleeping it.
        with pytest.raises(TransportExhausted) as exhausted:
            retry_remote(
                failing,
                op_name="push",
                policy=EgressPolicy(),
                fence_seconds=5.0,
                sleep=record_sleep,
                rand=lambda: 0.5,
            )
        assert len(calls) == 2 and sleeps == [2.0]
        assert exhausted.value.attempts == 2
        assert "TLS connection was non-properly terminated" in exhausted.value.last_stderr

    def test_backoff_retry_absorbs_one_injected_transport_failure_and_completes(
        self, repo: Path, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Red targets 1-4 in one walk: one GnuTLS push failure at development
        setup time is absorbed by the bounded backoff; the order completes."""
        bare = _bare_remote(repo, tmp_path, "durable.git")
        served = _flaky_run_git(monkeypatch, GNUTLS_SAMPLE, 128, times=1)
        clock = VirtualClock()
        evidence: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []

        deps = make_deps(
            actor=ContractActor(APPROVES),
            materializer=WorkspaceSealer(
                repo=repo,
                remote_url=str(bare),
                remote_ref="refs/heads/dev-egress",
                sleep=clock.sleep,
                rand=lambda: 0.5,
                now=clock.iso_now,
                evidence=evidence.append,
            ),
        )
        deps.observe = events.append

        state = _invoke(deps, repo)

        # Red target 1 + 3: not a terminal fault; the run completed.
        assert served["count"] == 1, "exactly one transport failure was injected"
        assert state["terminal"] == TERMINAL_COMPLETE
        assert not state.get("fault")

        # Red target 2: >=2 attempts on that stage's push, and the adjacent
        # attempt interval is the exponential backoff magnitude (2s, +-50%).
        push_lines = [line for line in evidence if line["op"] == "push"]
        assert len(push_lines) >= 2
        assert push_lines[0]["exit"] != 0 and push_lines[1]["exit"] == 0
        interval = clock.sleeps[0]
        assert 1.0 <= interval <= 3.0, f"backoff interval {interval} outside 2s +-50%"
        assert interval == 2.0  # jitter rand pinned at 0.5: the raw ladder value

        # Probe-protocol evidence lines: {attempt, at, exit, stderr_tail}.
        first = push_lines[0]
        assert {"attempt", "at", "exit", "stderr_tail"} <= set(first)
        assert first["attempt"] == 1 and first["exit"] == 128
        assert "TLS connection was non-properly terminated" in first["stderr_tail"]
        # Red target 4: the failure record reads root_cause=transport.
        assert first["class"] == EGRESS_TRANSPORT
        assert first["root_cause"] == "transport"

        # The walk itself: every stage walked, each with its own event
        # (success for the spine stages, APPROVE for the two reviews).
        walked = [event for event in events if event.get("event") in ("success", "APPROVE")]
        assert [entry["stage"] for entry in walked] == [
            "configure",
            "implement",
            "continuous_review",
            "final_review",
            "acceptance",
            "human_gate",
            "merger",
        ]

    def test_backoff_retry_retries_transport_never_repo_verdicts(self) -> None:
        """Only the transport closed set retries; the repo's words do not."""
        attempts: list[int] = []

        def repo_rejected() -> RemoteResult:
            attempts.append(1)
            return RemoteResult(
                1,
                "! [remote rejected] HEAD -> refs/heads/dev-1 (pre-receive hook declined)",
            )

        def no_sleep(seconds: float) -> None:
            raise AssertionError(f"retry slept {seconds}s for a repo verdict")

        with pytest.raises(EgressRepoError) as repo_error:
            retry_remote(repo_rejected, op_name="push", sleep=no_sleep, rand=lambda: 0.5)
        assert repo_error.value.classification == REPO_REJECTED
        assert len(attempts) == 1, "a repo verdict must never be retried"


class TestTransportNotTerminal:
    def test_transport_not_terminal_exhaustion_is_retryable_fault_with_resume_rights(
        self,
    ) -> None:
        """Retries exhausted: a retryable failure record, never a fault.

        The stage re-enters through the walker's bounded retry, and even the
        exhausted terminal keeps ``retryable`` true with the reconfigure exit
        open -- the resume rights the control plane's generation restart
        uses. Transport itself never terminalises as a fault.
        """

        class DarkEgressSealer:
            def materialize(self, stage: Any, dispatch: Dispatch, outcome: StageOutcome):
                raise TransportExhausted(
                    f"remote operation 'push' still failing after 5 transport-failed "
                    f"attempts; last: {GNUTLS_SAMPLE}",
                    attempts=5,
                    last_stderr=GNUTLS_SAMPLE,
                )

        state = run(make_deps(materializer=DarkEgressSealer()))
        assert state["terminal"] != TERMINAL_FAULT
        assert not state.get("fault")
        assert state["terminal_code"] == "PROVIDER_UNAVAILABLE"

        failure = classify_failure(
            str(state.get("terminal") or ""),
            str(state.get("terminal_reason") or ""),
            str(state.get("terminal_code") or ""),
            str(state.get("last_failure_detail") or ""),
        )
        assert failure is not None
        assert failure["retryable"] is True
        assert failure["exit"] == "reconfigure"
        assert failure["root_cause"] == "transport"

        # Every failure line in the event history reads the layered cause.
        failed_lines = [entry for entry in state["history"] if entry.get("event") == FAILURE_EVENT]
        assert failed_lines
        assert all(entry["root_cause"] == "transport" for entry in failed_lines)

    def test_transport_not_terminal_repo_layer_rejection_may_still_end_the_order(
        self, repo: Path, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Only repo-layer outcomes may terminate: a remote rejection faults
        the walk on the first attempt, with no backoff, no retries."""
        bare = _bare_remote(repo, tmp_path, "rejected.git")
        rejected = "! [remote rejected] HEAD -> refs/heads/dev-egress (pre-receive hook declined)"
        served = _flaky_run_git(monkeypatch, rejected, 1, times=99)
        clock = VirtualClock()

        deps = make_deps(
            materializer=WorkspaceSealer(
                repo=repo,
                remote_url=str(bare),
                remote_ref="refs/heads/dev-egress",
                sleep=clock.sleep,
                rand=lambda: 0.5,
            )
        )

        state = _invoke(deps, repo)

        assert served["count"] == 1, "a repo-layer verdict is never retried"
        assert clock.sleeps == [], "no backoff for a repo-layer verdict"
        assert state["terminal"] == TERMINAL_FAULT
        assert "remote rejected" in str(state.get("terminal_reason"))

    def test_transport_not_terminal_business_verdicts_keep_governance_semantics(
        self,
    ) -> None:
        """Business refusals are verdicts, not faults: the human gate owns them."""
        for code in ("ACCEPTANCE_FAILED", "GATE_REJECTED", "SCOPE_BOUNDARY_VIOLATION"):
            layered = layer_failure(code)
            assert layered["root_cause"] == "business", code
            assert layered["disposition"] == "human_gate", code

        failure = classify_failure("refused", "gate decision REJECT by operator", "GATE_REJECTED")
        assert failure is not None
        assert failure["class"] == "rejected"
        assert failure["root_cause"] == "business"


class TestFailureCodeLayering:
    def test_failure_code_layering_splits_transport_execution_business(self) -> None:
        """The three root causes, each with its disposition mapping (交付面 3)."""
        # Transport: the GnuTLS incident fixture in the detail.
        assert layer_failure("PROVIDER_UNAVAILABLE", GNUTLS_SAMPLE) == {
            "code": "PROVIDER_UNAVAILABLE",
            "root_cause": "transport",
            "disposition": "backoff_retry",
        }
        # Transport: the timeout family (exit 124).
        assert layer_failure("PROVIDER_UNAVAILABLE", exit_code=124)["root_cause"] == ("transport")
        # Execution: the command ran, its execution environment failed.
        for code in ("SETUP_FAILED", "MATERIALIZATION_FAILED", "INVALID_HANDOFF_SCHEMA"):
            layered = layer_failure(code)
            assert layered["root_cause"] == "execution", code
            assert layered["disposition"] == "reconfigure", code
        # The disposition table itself is total over the three causes.
        assert egress.ROOT_CAUSE_DISPOSITION == {
            "transport": "backoff_retry",
            "execution": "reconfigure",
            "business": "human_gate",
        }

    def test_failure_code_layering_legacy_flat_code_stays_transport_alias(self) -> None:
        """The old flat code keeps reading as transport, evidence or not."""
        assert layer_failure("PROVIDER_UNAVAILABLE")["root_cause"] == "transport"
        assert layer_failure("PROVIDER_UNAVAILABLE", "run ended failed")["root_cause"] == (
            "transport"
        )
        # ...but transport evidence in any code's detail still wins.
        assert layer_failure("MATERIALIZATION_FAILED", GNUTLS_SAMPLE)["root_cause"] == ("transport")

    def test_failure_code_layering_carries_root_cause_into_event_and_status(self) -> None:
        """events.jsonl (history) and status.json (failure record) both read it."""

        class FailingImplement(ContractActor):
            def act(self, stage: Any, dispatch: Dispatch) -> StageOutcome:
                if stage.id == "implement":
                    return StageOutcome(
                        event=FAILURE_EVENT,
                        failure_code="PROVIDER_UNAVAILABLE",
                        detail=f"{stage.id} run did not finish: {GNUTLS_SAMPLE}",
                    )
                return super().act(stage, dispatch)

        state = run(make_deps(actor=FailingImplement(APPROVES)))
        failed_lines = [entry for entry in state["history"] if entry.get("event") == FAILURE_EVENT]
        assert failed_lines
        for entry in failed_lines:
            assert entry["failure_code"] == "PROVIDER_UNAVAILABLE"
            assert entry["root_cause"] == "transport"

        failure = classify_failure(
            str(state.get("terminal") or ""),
            str(state.get("terminal_reason") or ""),
            str(state.get("terminal_code") or ""),
            str(state.get("last_failure_detail") or ""),
        )
        assert failure is not None and failure["root_cause"] == "transport"

    def test_failure_code_layering_git_classes_match_the_measured_fixtures(self) -> None:
        """The three git-layer classes, on the spec's own fixture samples."""
        assert classify_git_failure(GNUTLS_SAMPLE, 128) == EGRESS_TRANSPORT
        # repo_rejected: the remote refused the push outright.
        assert (
            classify_git_failure(
                "! [remote rejected] HEAD -> refs/heads/main (pre-receive hook declined)",
                1,
            )
            == REPO_REJECTED
        )
        # repo_conflict: the remote demands the existing rebase/retry path.
        assert (
            classify_git_failure("! [rejected] HEAD -> main (non-fast-forward)", 1) == REPO_CONFLICT
        )
        assert classify_git_failure("fatal: non-fast-forward, fetch first", 1) == (REPO_CONFLICT)
        # The timeout exit family is transport on its own.
        assert classify_git_failure("", 124) == EGRESS_TRANSPORT


class TestArtifactSurvival:
    def _faulted_walk(
        self,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], RecordingSealer]:
        """A walk that faults at continuous_review, after two stages sealed."""

        class FaultAtReview(ContractActor):
            def act(self, stage: Any, dispatch: Dispatch) -> StageOutcome:
                if stage.id == "continuous_review":
                    raise PipelineFault("injected fault: egress dark, order halted")
                return super().act(stage, dispatch)

        sealer = RecordingSealer()
        events: list[dict[str, Any]] = []
        deps = make_deps(actor=FaultAtReview(APPROVES), materializer=sealer)
        deps.observe = events.append
        state = run(deps)
        return state, events, sealer

    def test_artifact_survival_fault_preserves_succeeded_stage_artifacts(self) -> None:
        """A fault discards nothing: events keep every success line, and the
        sealed commits of succeeded stages stay on the record."""
        state, events, _sealer = self._faulted_walk()

        assert state["terminal"] == TERMINAL_FAULT
        success_lines = [entry for entry in events if entry.get("event") == SPINE_EVENT]
        assert [entry["stage"] for entry in success_lines] == ["configure", "implement"]
        # The artifact map still names each succeeded stage's sealed commit.
        artifacts = state["artifacts"]
        assert artifacts["run_config"] == success_lines[0]["output_commit"]
        assert artifacts["implementation_evidence"] == success_lines[1]["output_commit"]

    def test_artifact_survival_recovery_never_reruns_succeeded_stages(self) -> None:
        """Red target 5: recovery enters at the first non-succeeded stage;
        the recovery's event sequence has no duplicate success commits."""

        class PrefixReplayer:
            """Replays exactly the receipt-sealed prefix; then declines for good."""

            def __init__(self, prefix: list[Replayed]) -> None:
                self._by_stage = {step.receipt["stage"]: step for step in prefix}
                self._declined = False

            def replay(self, stage: Any, dispatch: Dispatch) -> Replayed | None:
                if self._declined:
                    return None
                step = self._by_stage.get(stage.id)
                if step is None:
                    self._declined = True
                    return None
                return step

        faulted, recovery_events_of_faulted, sealer = self._faulted_walk()

        prefix = [
            Replayed(
                event=SPINE_EVENT,
                receipt=dict(sealer.chain[stage_id]),
                output_commit=str(sealer.chain[stage_id]["output_commit"]),
            )
            for stage_id in ("configure", "implement")
        ]

        actor = ContractActor(APPROVES)
        recovery_sealer = RecordingSealer()
        recovery_events: list[dict[str, Any]] = []
        deps = make_deps(actor=actor, materializer=recovery_sealer, replayer=PrefixReplayer(prefix))
        deps.observe = recovery_events.append
        recovered = run(deps)

        assert recovered["terminal"] == TERMINAL_COMPLETE
        # Zero re-run: the succeeded stages never reach the actor again.
        assert [stage for stage, _ in actor.calls] == [
            "continuous_review",
            "final_review",
            "acceptance",
            "human_gate",
            "merger",
        ]
        # No duplicate output_commit success lines in the recovery sequence.
        success_commits = [
            entry["output_commit"] for entry in recovery_events if entry.get("event") == SPINE_EVENT
        ]
        assert len(success_commits) == len(set(success_commits))
        # The recovery continues the same chain, it does not fork it: the
        # replayed prefix carries the faulted walk's own commits forward.
        replayed_lines = [entry for entry in recovery_events if entry.get("replayed") is True]
        assert [entry["stage"] for entry in replayed_lines] == ["configure", "implement"]
        assert replayed_lines[0]["output_commit"] == faulted["artifacts"]["run_config"]
        assert replayed_lines[1]["output_commit"] == faulted["artifacts"]["implementation_evidence"]
        # And the faulted walk's own event record survives untouched.
        faulted_successes = [
            entry for entry in recovery_events_of_faulted if entry.get("event") == SPINE_EVENT
        ]
        assert [entry["stage"] for entry in faulted_successes] == [
            "configure",
            "implement",
        ]


def test_failure_code_layering_lifecycle_is_the_retry_authority() -> None:
    """The retry bound comes from the contract's own taxonomy, not from here."""
    lifecycle = Lifecycle.load()
    assert lifecycle.is_retryable("PROVIDER_UNAVAILABLE") is True
    assert lifecycle.is_retryable("INVALID_HANDOFF_SCHEMA") is False
