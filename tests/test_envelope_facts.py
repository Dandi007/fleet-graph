"""E4a envelope facts: resume_verification, prior_terminal, and the N7 guard.

The coordinator envelope must carry two mechanical facts the model neither
writes nor (implicitly) may forge the source of: the wf_resume verification the
line runner executed at generation start, and the previous generation's
terminal. The N7 guard mechanically rejects a BLOCKED verdict whose reason
self-reports a BROKEN recovery verification that the envelope contradicts, so a
coordinator reading an old BROKEN narrative out of progress.md can no longer
burn a generation by re-reading that verdict as this round's evidence.
"""

from __future__ import annotations

import json
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from fleet_graph.graphs.goal_line import (
    N7_INVALID_ROUND_CODE,
    TERMINAL_BLOCKED,
    TERMINAL_DONE,
    LineDeps,
    build_goal_line_graph,
    claims_resume_verification_broken,
    n7_rejects_blocked,
)
from fleet_graph.graphs.guards import LineBounds, LineGuards
from fleet_graph.state.work_folder import resume_verification_from
from fleet_graph.work_report import SCHEMA_VERSION


class FakeCoordinator:
    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def turn(self, round_no: int, coord_input: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(coord_input)
        return self.script.pop(0) if self.script else {"verdict": "done", "reason": "end"}


class FakeWorker:
    def turn(self, prompt: str, round_no: int) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "turn_id": f"t-{round_no}",
            "outcome": "completed",
            "summary": f"did {prompt}",
            "did": [prompt],
            "files": [],
            "self_tests": [],
            "blocker": None,
        }


class FakeInbox:
    def drain_then_ack(self, persist: Any) -> tuple[list[Any], list[str]]:
        persist([])
        return [], []


class FakeArtifacts:
    def __init__(self) -> None:
        self.rounds: list[dict[str, Any]] = []
        self.terminal: dict[str, Any] | None = None

    def heartbeat(self, round_no: int, phase: str, *, force: bool = False) -> bool:
        return True

    def append_round(self, line: dict[str, Any]) -> bool:
        self.rounds.append(line)
        return True

    def write_worker_report(self, round_no: int, report: dict[str, Any]) -> str:
        return "worker-report.json"

    def write_terminal(
        self,
        *,
        terminal: str,
        rounds: int,
        reason: str | None = None,
        pump_fault: bool = False,
        waiting_on: str = "none",
        waiting_on_declared: str | None = None,
        goal_revision: str | None = None,
        dd_development_id: str | None = None,
    ) -> str:
        self.terminal = {
            "terminal": terminal,
            "rounds": rounds,
            "reason": reason,
            "waiting_on": waiting_on,
            "waiting_on_declared": waiting_on_declared,
            "goal_revision": goal_revision,
        }
        return "terminal.json"


def run_line(
    script: list[dict[str, Any]],
    *,
    resume_verification: dict[str, Any] | None = None,
    prior_terminal: dict[str, Any] | None = None,
) -> tuple[FakeArtifacts, LineDeps]:
    artifacts = FakeArtifacts()
    deps = LineDeps(
        coordinator=FakeCoordinator(script),
        worker=FakeWorker(),
        inbox=FakeInbox(),
        artifacts=artifacts,
        guards=LineGuards(bounds=LineBounds()),
        folder_id="wf-e4a",
        resume_verification=resume_verification,
        prior_terminal=prior_terminal,
    )
    compiled = build_goal_line_graph(deps).compile(checkpointer=InMemorySaver())
    compiled.invoke(
        {"round_no": 1}, config={"configurable": {"thread_id": "t"}, "recursion_limit": 100}
    )
    return artifacts, deps


GREEN_FACTS = {"overall": "MATCH", "lines": [], "at": "2026-08-28T22:00:00Z"}
BROKEN_FACTS = {"overall": "BROKEN", "lines": [], "at": "2026-08-28T22:00:00Z"}


class TestEnvelopeFacts:
    def test_resume_verification_reaches_every_round(self) -> None:
        _artifacts, deps = run_line(
            [
                {"verdict": "continue", "next_prompt": "first"},
                {"verdict": "done"},
            ],
            resume_verification=GREEN_FACTS,
        )
        assert deps.coordinator.calls[0]["resume_verification"] == GREEN_FACTS
        assert deps.coordinator.calls[1]["resume_verification"] == GREEN_FACTS

    def test_resume_verification_absent_when_not_captured(self) -> None:
        _artifacts, deps = run_line([{"verdict": "done"}])
        assert "resume_verification" not in deps.coordinator.calls[0]

    def test_prior_terminal_reaches_round_one_only(self) -> None:
        prior = {"terminal": "blocked", "rounds": 3, "reason": "shrunk"}
        _artifacts, deps = run_line(
            [{"verdict": "continue", "next_prompt": "first"}, {"verdict": "done"}],
            prior_terminal=prior,
        )
        assert deps.coordinator.calls[0]["prior_terminal"] == prior
        assert "prior_terminal" not in deps.coordinator.calls[1]

    def test_prior_terminal_absent_when_none(self) -> None:
        _artifacts, deps = run_line([{"verdict": "done"}])
        assert "prior_terminal" not in deps.coordinator.calls[0]


class TestN7Guard:
    def test_broken_claim_against_green_envelope_rejects_and_retries(self) -> None:
        artifacts, _deps = run_line(
            [
                {"verdict": "blocked", "reason": "恢复验证 BROKEN，无法继续"},
                {"verdict": "done", "reason": "finished"},
            ],
            resume_verification=GREEN_FACTS,
        )
        assert artifacts.terminal is not None
        assert artifacts.terminal["terminal"] == TERMINAL_DONE, "the round must retry, not park"
        invalid = [r for r in artifacts.rounds if r["verdict"] == "invalid"]
        assert invalid and invalid[0]["reason"] == N7_INVALID_ROUND_CODE
        assert invalid[0]["injected"] is False

    def test_broken_envelope_lets_a_broken_claim_park_normally(self) -> None:
        artifacts, _deps = run_line(
            [
                {
                    "verdict": "blocked",
                    "reason": "恢复验证 BROKEN",
                    "waiting_on": "decision",
                }
            ],
            resume_verification=BROKEN_FACTS,
        )
        assert artifacts.terminal is not None
        assert artifacts.terminal["terminal"] == TERMINAL_BLOCKED
        assert artifacts.terminal["waiting_on"] == "decision"
        assert not [r for r in artifacts.rounds if r["verdict"] == "invalid"]

    def test_blocked_without_a_broken_claim_is_not_rejected(self) -> None:
        artifacts, _deps = run_line(
            [{"verdict": "blocked", "reason": "needs a ruling", "waiting_on": "decision"}],
            resume_verification=GREEN_FACTS,
        )
        assert artifacts.terminal is not None
        assert artifacts.terminal["terminal"] == TERMINAL_BLOCKED
        assert not [r for r in artifacts.rounds if r["verdict"] == "invalid"]

    def test_unrelated_broken_reason_is_not_rejected(self) -> None:
        """'the build is broken' is not a recovery-verification claim."""
        artifacts, _deps = run_line(
            [{"verdict": "blocked", "reason": "the build is broken", "waiting_on": "decision"}],
            resume_verification=GREEN_FACTS,
        )
        assert artifacts.terminal is not None
        assert artifacts.terminal["terminal"] == TERMINAL_BLOCKED


class TestGuardPredicates:
    def test_claims_resume_verification_broken(self) -> None:
        assert claims_resume_verification_broken("resume verification BROKEN")
        assert claims_resume_verification_broken("恢复验证 BROKEN")
        assert claims_resume_verification_broken("VERIFICATION broken, stop")

    def test_claims_ignores_unrelated_broken(self) -> None:
        assert not claims_resume_verification_broken("the build is broken")
        assert not claims_resume_verification_broken("all good")

    def test_n7_rejects_only_on_mismatch(self) -> None:
        assert n7_rejects_blocked("resume BROKEN", "MATCH")
        assert not n7_rejects_blocked("resume BROKEN", "BROKEN")
        assert not n7_rejects_blocked("resume BROKEN", "broken")
        assert not n7_rejects_blocked("needs a ruling", "MATCH")


class TestResumeVerificationFrom:
    def test_reduces_overall_lines_and_timestamp(self) -> None:
        facts = resume_verification_from(
            {
                "verification": {
                    "overall": "MATCH",
                    "lines": [
                        {"label": "python", "status": "ok", "detail": "3.11.9"},
                        {"label": "deps", "status": "warn"},
                    ],
                }
            },
            clock=lambda: 0.0,
        )
        assert facts["overall"] == "MATCH"
        assert facts["lines"] == [
            {"label": "python", "verdict": "ok"},
            {"label": "deps", "verdict": "warn"},
        ]
        assert facts["at"] == "1970-01-01T00:00:00Z"

    def test_missing_verification_is_stated_not_guessed(self) -> None:
        facts = resume_verification_from({}, clock=lambda: 0.0)
        assert facts["overall"] == ""
        assert facts["lines"] == []


class TestBuildLinePriorTerminal:
    def test_build_line_carries_prior_terminal_only_by_injection(self, tmp_path: Any) -> None:
        """R2（wf-4601c8 图合一）改写指向新信道：prior_terminal 只经注入进入线，
        线进程不再从盘面 terminal.json 重读（盘面纯持久化，读作事件即违宪）。
        盘面上留着的旧 terminal.json 不再成为事实源。"""
        from fleet_graph.graphs.runner import LineConfig, build_line

        (tmp_path / "terminal.json").write_text(
            json.dumps({"terminal": "blocked", "rounds": 3}), encoding="utf-8"
        )
        _graph, deps = build_line(
            LineConfig(
                folder_id="wf-x",
                seat="s",
                run_root=tmp_path,
                prior_terminal={"terminal": "done", "rounds": 7},
            )
        )
        assert deps.prior_terminal == {"terminal": "done", "rounds": 7}

        _graph, bare = build_line(LineConfig(folder_id="wf-x", seat="s", run_root=tmp_path))
        assert bare.prior_terminal is None
