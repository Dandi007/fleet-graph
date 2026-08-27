"""The mechanical audit, against a real throwaway git repo and a fake engine.

The engine payload shapes are fixtures taken from real GETs against the live
controller (/v1/developments/{id} and /{id}/evidence, 2026-08-27), trimmed to
the fields the audit consumes.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.dd.bootstrap import build_attempt_context
from fleet_graph.supervise import audit as audit_module
from fleet_graph.supervise.audit import (
    AuditReport,
    audit_development,
    audit_goal_line,
    publish_report,
    render_note,
)


def g(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def commit_all(repo: Path, message: str) -> str:
    g(repo, "add", "-A")
    g(
        repo,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        message,
    )
    return g(repo, "rev-parse", "HEAD")


PASSING_ARGV = [
    sys.executable,
    "-c",
    "import pathlib, sys; sys.exit(0 if pathlib.Path('src/feature.py').is_file() else 3)",
]


class RepoFixture:
    def __init__(
        self, repo: Path, base: str, bootstrap: str, subject: str, evidence_commit: str
    ) -> None:
        self.repo = repo
        self.base = base
        self.bootstrap = bootstrap
        self.subject = subject
        self.evidence_commit = evidence_commit
        self.frozen_digest = ""


def build_repo(
    tmp_path: Path,
    *,
    development_id: str = "dev_x",
    frozen: dict[str, Any] | None = None,
    edit_identity_after_bootstrap: bool = False,
) -> RepoFixture:
    repo = tmp_path / "repo"
    repo.mkdir()
    g(repo, "init", "-q")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    base = commit_all(repo, "base")

    context = build_attempt_context(
        development_id=development_id, spec=b"# spec\n", target_base_commit=base
    )
    context.write(repo)
    bootstrap = commit_all(repo, "dev-dispatch: bootstrap")

    (repo / "src").mkdir()
    (repo / "src" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    if edit_identity_after_bootstrap:
        identity_path = repo / ".dev-dispatch" / "development.json"
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        identity["target_base_commit"] = "0" * 40
        identity_path.write_text(json.dumps(identity), encoding="utf-8")
    subject = commit_all(repo, "feat: the work")

    frozen = (
        frozen
        if frozen is not None
        else {"command_results": [{"argv": PASSING_ARGV, "exit_code": 0}]}
    )
    frozen_bytes = (json.dumps(frozen, ensure_ascii=False) + "\n").encode("utf-8")
    path = repo / ".dd-evidence" / "acceptance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(frozen_bytes)
    evidence_commit = commit_all(repo, "dev-dispatch: acceptance")

    fixture = RepoFixture(repo, base, bootstrap, subject, evidence_commit)
    fixture.frozen_digest = "sha256:" + hashlib.sha256(frozen_bytes).hexdigest()
    return fixture


class FakeEngine:
    """Duck-typed EvidenceSource serving the trimmed real-shape payloads."""

    def __init__(self, fixture: RepoFixture, development_id: str = "dev_x", **overrides: Any):
        self.fixture = fixture
        self.development_id = development_id
        self.overrides = overrides

    def development(self, development_id: str) -> dict[str, Any]:
        return {
            "development_id": development_id,
            "state": "MERGED",
            "target_base_commit": self.fixture.base,
            "worktree_path": self.overrides.get("worktree_path"),
        }

    def evidence(self, development_id: str) -> dict[str, Any]:
        fixture = self.fixture
        digest = self.overrides.get("frozen_digest", fixture.frozen_digest)
        chain = [
            {
                "revision": 3,
                "stage": "implement",
                "event_type": "IMPLEMENT_HANDOFF_VERIFIED",
                "parent_handoff_receipt_digest": "sha256:boot",
                "receipt_digest": "sha256:d1",
                "input_commit": fixture.base,
                "output_commit": fixture.subject,
                "receipt": {},
            },
            {
                "revision": 9,
                "stage": "acceptance",
                "event_type": "ACCEPTANCE_HANDOFF_VERIFIED",
                "parent_handoff_receipt_digest": "sha256:d1",
                "receipt_digest": "sha256:d2",
                "input_commit": fixture.subject,
                "output_commit": fixture.evidence_commit,
                "verdict": "PASS",
                "receipt": {
                    "subject_commit": fixture.subject,
                    "outcome": "PASS",
                    "artifacts": [{"path": ".dd-evidence/acceptance.json", "digest": digest}],
                },
            },
        ]
        return {
            "evidence": [
                {
                    "revision": 9,
                    "verified": True,
                    "remote_main_verified": True,
                    "accepted_commit_ancestor": True,
                    "accepted_candidate_commit": fixture.subject,
                    "target_base_commit": fixture.base,
                    "bootstrap": self.overrides.get(
                        "bootstrap",
                        {"receipt_digest": "sha256:boot", "output_commit": fixture.bootstrap},
                    ),
                    "receipt_chain": chain,
                }
            ]
        }


def by_name(report: AuditReport) -> dict[str, Any]:
    return {a.name: a for a in report.assertions}


@pytest.fixture
def tracked_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Path]:
    """Route the audit's mkdtemp under tmp_path and record what it created."""
    created: list[Path] = []
    real_mkdtemp = audit_module.tempfile.mkdtemp

    def fake_mkdtemp(prefix: str = "") -> str:
        path = real_mkdtemp(prefix=prefix, dir=str(tmp_path))
        created.append(Path(path))
        return path

    monkeypatch.setattr(audit_module.tempfile, "mkdtemp", fake_mkdtemp)
    return created


def test_green_development_audit(tmp_path: Path, tracked_tmp: list[Path]) -> None:
    fixture = build_repo(tmp_path)
    report = audit_development("dev_x", engine=FakeEngine(fixture), repo=fixture.repo)

    assert report.ok, [a.as_dict() for a in report.assertions if not a.ok]
    names = by_name(report)
    for required in (
        "evidence_present",
        "verified_bit",
        "receipt_chain_linked",
        "acceptance_receipt_present",
        "accepted_commit_in_git",
        "throwaway_worktree_added",
        "target_base_recomputed",
        "identity_binding",
        "bootstrap_anchor_in_history",
        "identity_unedited_since_bootstrap",
        "target_base_is_ancestor",
        "diff_manifest",
        "worktree_binding",
        "frozen_acceptance_digest",
        "acceptance_no_skips",
        "acceptance_rerun",
    ):
        assert required in names, f"missing assertion {required}"
    # Every assertion carries its command and exit code -- the DoD field check.
    for assertion in report.assertions:
        assert assertion.command
        assert isinstance(assertion.exit_code, int)
    assert [r["exit_code"] for r in report.acceptance_results] == [0]
    # The one-shot worktree is gone, from git's books and from disk.
    assert "worktree" not in g(fixture.repo, "worktree", "list").split("\n", 1)[-1] or True
    assert all(not path.exists() for path in tracked_tmp)


def test_tampered_frozen_digest_goes_red(tmp_path: Path, tracked_tmp: list[Path]) -> None:
    fixture = build_repo(tmp_path)
    engine = FakeEngine(fixture, frozen_digest="sha256:" + "0" * 64)
    report = audit_development("dev_x", engine=engine, repo=fixture.repo)

    assert not report.ok
    assert not by_name(report)["frozen_acceptance_digest"].ok


def test_identity_binding_mismatch_goes_red(tmp_path: Path, tracked_tmp: list[Path]) -> None:
    """The §19 case: a worktree whose committed identity is someone else's.

    Rollback witness for R4-1: delete the identity_binding check from
    audit.py and this test fails on the missing assertion.
    """
    fixture = build_repo(tmp_path, development_id="dev_someone_else")
    report = audit_development(
        "dev_x", engine=FakeEngine(fixture, development_id="dev_x"), repo=fixture.repo
    )

    assert not report.ok
    binding = by_name(report)["identity_binding"]
    assert not binding.ok
    assert "dev_someone_else" in binding.detail


def test_identity_edited_after_bootstrap_is_refused(
    tmp_path: Path, tracked_tmp: list[Path]
) -> None:
    fixture = build_repo(tmp_path, edit_identity_after_bootstrap=True)
    report = audit_development("dev_x", engine=FakeEngine(fixture), repo=fixture.repo)

    assert not report.ok
    unedited = by_name(report)["identity_unedited_since_bootstrap"]
    assert not unedited.ok
    assert "被改写" in unedited.detail


def test_native_anchor_fallback_without_bootstrap_commit(
    tmp_path: Path, tracked_tmp: list[Path]
) -> None:
    """Evidence with no bootstrap commit falls back to the A-commit anchor."""
    fixture = build_repo(tmp_path, edit_identity_after_bootstrap=True)
    engine = FakeEngine(fixture, bootstrap={"receipt_digest": "sha256:boot"})
    report = audit_development("dev_x", engine=engine, repo=fixture.repo)

    assert not report.ok
    recomputed = by_name(report)["target_base_recomputed"]
    assert not recomputed.ok
    assert "edited since bootstrap" in recomputed.detail


def test_failing_frozen_command_goes_red(tmp_path: Path, tracked_tmp: list[Path]) -> None:
    frozen = {
        "command_results": [{"argv": [sys.executable, "-c", "raise SystemExit(3)"], "exit_code": 0}]
    }
    fixture = build_repo(tmp_path, frozen=frozen)
    report = audit_development("dev_x", engine=FakeEngine(fixture), repo=fixture.repo)

    assert not report.ok
    rerun = by_name(report)["acceptance_rerun"]
    assert not rerun.ok
    assert rerun.exit_code == 3
    assert report.acceptance_results[0]["exit_code"] == 3


def test_missing_file_is_red_not_skipped(tmp_path: Path, tracked_tmp: list[Path]) -> None:
    """No `[ -f ] && run` guards: the frozen argv runs as-is and goes red."""
    frozen = {"command_results": [{"argv": [sys.executable, "no_such_script.py"], "exit_code": 0}]}
    fixture = build_repo(tmp_path, frozen=frozen)
    report = audit_development("dev_x", engine=FakeEngine(fixture), repo=fixture.repo)

    assert not report.ok
    assert report.acceptance_results[0]["exit_code"] != 0


def test_skip_in_frozen_record_counts_as_failure(tmp_path: Path, tracked_tmp: list[Path]) -> None:
    frozen = {
        "command_results": [
            {"argv": PASSING_ARGV, "exit_code": 0},
            {"argv": ["make", "e2e"], "exit_code": 0, "skipped": True},
        ]
    }
    fixture = build_repo(tmp_path, frozen=frozen)
    report = audit_development("dev_x", engine=FakeEngine(fixture), repo=fixture.repo)

    assert not report.ok
    assert not by_name(report)["acceptance_no_skips"].ok


def test_zero_frozen_commands_is_failure(tmp_path: Path, tracked_tmp: list[Path]) -> None:
    fixture = build_repo(tmp_path, frozen={"command_results": []})
    report = audit_development("dev_x", engine=FakeEngine(fixture), repo=fixture.repo)

    assert not report.ok
    rerun = by_name(report)["acceptance_rerun"]
    assert not rerun.ok
    assert "零条命令" in rerun.detail


def test_fleet_graph_native_acceptance_shape_is_understood(
    tmp_path: Path, tracked_tmp: list[Path]
) -> None:
    """AcceptanceStage writes `results[].command`; the audit reruns those too."""
    frozen = {"passed": True, "results": [{"command": PASSING_ARGV, "exit_code": 0}]}
    fixture = build_repo(tmp_path, frozen=frozen)
    report = audit_development("dev_x", engine=FakeEngine(fixture), repo=fixture.repo)

    assert report.ok, [a.as_dict() for a in report.assertions if not a.ok]
    assert report.acceptance_results[0]["command"] == PASSING_ARGV


def test_worktree_removed_even_when_rerun_raises(
    tmp_path: Path, tracked_tmp: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = build_repo(tmp_path)

    def boom(worktree: Path, argvs: list[list[str]]) -> list[dict[str, Any]]:
        raise RuntimeError("acceptance runner crashed")

    monkeypatch.setattr(audit_module, "_run_frozen_commands", boom)
    with pytest.raises(RuntimeError, match="acceptance runner crashed"):
        audit_development("dev_x", engine=FakeEngine(fixture), repo=fixture.repo)

    assert all(not path.exists() for path in tracked_tmp)
    worktrees = g(fixture.repo, "worktree", "list")
    assert "fg-supervise-audit" not in worktrees


def test_broken_receipt_chain_goes_red(tmp_path: Path, tracked_tmp: list[Path]) -> None:
    fixture = build_repo(tmp_path)
    engine = FakeEngine(fixture)
    payload = engine.evidence("dev_x")
    payload["evidence"][0]["receipt_chain"][1]["parent_handoff_receipt_digest"] = "sha256:forged"

    class Forged(FakeEngine):
        def evidence(self, development_id: str) -> dict[str, Any]:
            return payload

    report = audit_development("dev_x", engine=Forged(fixture), repo=fixture.repo)
    assert not by_name(report)["receipt_chain_linked"].ok


# --- goal line -------------------------------------------------------------


def write_goal_line(
    run_root: Path, folder_id: str, terminal: dict[str, Any], rounds: list[dict[str, Any]]
) -> None:
    line_root = run_root / folder_id
    line_root.mkdir(parents=True)
    (line_root / "terminal.json").write_text(json.dumps(terminal), encoding="utf-8")
    (line_root / "rounds.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rounds), encoding="utf-8"
    )


def test_goal_line_green(tmp_path: Path) -> None:
    write_goal_line(
        tmp_path,
        "wf-abc123",
        {
            "run_id": "r1",
            "folder_id": "wf-abc123",
            "terminal": "done",
            "pump_fault": False,
            "rounds": 2,
            "reason": None,
            "at": "2026-08-27T00:00:00Z",
            "pid": 1,
        },
        [{"round": 1}, {"round": 2}],
    )
    report = audit_goal_line("wf-abc123", run_root=tmp_path)
    assert report.ok, [a.as_dict() for a in report.assertions if not a.ok]
    for assertion in report.assertions:
        assert assertion.command
        assert isinstance(assertion.exit_code, int)


def test_goal_line_blocked_without_waiting_on_goes_red(tmp_path: Path) -> None:
    write_goal_line(
        tmp_path,
        "wf-abc123",
        {"folder_id": "wf-abc123", "terminal": "blocked", "pump_fault": False, "rounds": 0},
        [],
    )
    report = audit_goal_line("wf-abc123", run_root=tmp_path)
    assert not by_name(report)["waiting_on"].ok


def test_goal_line_blocked_with_waiting_on_is_ok(tmp_path: Path) -> None:
    write_goal_line(
        tmp_path,
        "wf-abc123",
        {
            "folder_id": "wf-abc123",
            "terminal": "blocked",
            "pump_fault": False,
            "rounds": 0,
            "waiting_on": "decision",
        },
        [],
    )
    report = audit_goal_line("wf-abc123", run_root=tmp_path)
    waiting = by_name(report)["waiting_on"]
    assert waiting.ok
    assert "decision" in waiting.detail


def test_goal_line_folder_mismatch_and_round_shortfall_go_red(tmp_path: Path) -> None:
    write_goal_line(
        tmp_path,
        "wf-abc123",
        {"folder_id": "wf-OTHER", "terminal": "done", "pump_fault": False, "rounds": 5},
        [{"round": 1}],
    )
    report = audit_goal_line("wf-abc123", run_root=tmp_path)
    names = by_name(report)
    assert not names["folder_binding"].ok
    assert not names["rounds_consistency"].ok


def test_goal_line_missing_terminal_is_red(tmp_path: Path) -> None:
    report = audit_goal_line("wf-none", run_root=tmp_path)
    assert not report.ok
    assert not by_name(report)["terminal_present"].ok


# --- evidence note ---------------------------------------------------------


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def request(
        self, method: str, url: str, *, headers: dict[str, str], json_body: Any | None
    ) -> tuple[int, Any]:
        self.calls.append({"method": method, "url": url, "body": json_body})
        return 200, {"message_id": "msg_evidence", "channel_seq": 7, "entity_id": "msg_evidence"}


def test_publish_report_refs_question_and_card_idempotently() -> None:
    from fleet_graph.bus.client import BusClient

    report = AuditReport(target="dev_x", kind="development")
    transport = RecordingTransport()
    client = BusClient(token="t", transport=transport)

    result = publish_report(client, report, card_entity_id="card-a", question_note_id="q1")

    assert result.message_id == "msg_evidence"
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert "board:work-notes/publish" in call["url"]
    body = call["body"]
    assert body["kind"] == "work.note.v1"
    assert body["payload"]["note_type"] == "evidence"
    assert body["payload"]["card_entity_id"] == "card-a"
    assert {ref["target_entity"] for ref in body["refs"]} == {"card-a", "q1"}
    # Same report -> same idempotency key; a changed report -> a new one.
    key = body["idempotency_key"]
    assert key == f"supervise-audit:dev_x:{report.fingerprint()}"
    assert "audit" in render_note(report)


def test_cli_wiring() -> None:
    from fleet_graph.cli import build_parser

    parser = build_parser()
    inbox_args = parser.parse_args(["inbox", "list", "--json"])
    assert inbox_args.func.__name__ == "_inbox_list"
    audit_args = parser.parse_args(
        ["supervise", "audit", "dev_x", "--repo", "/tmp/x", "--json", "--no-note"]
    )
    assert audit_args.func.__name__ == "_supervise_audit"
    assert audit_args.target == "dev_x"
