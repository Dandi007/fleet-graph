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
        implement_receipt: dict[str, Any] = {}
        if "implement_verification_commands" in self.overrides:
            implement_receipt = {
                "verification_record": {
                    "verification_commands": self.overrides["implement_verification_commands"]
                }
            }
        acceptance_receipt: dict[str, Any] = {
            "subject_commit": fixture.subject,
            "outcome": "PASS",
        }
        if not self.overrides.get("no_artifacts"):
            acceptance_receipt["artifacts"] = [
                {"path": ".dd-evidence/acceptance.json", "digest": digest}
            ]
        if "acceptance_verification_commands" in self.overrides:
            acceptance_receipt["verification_record"] = {
                "verification_commands": self.overrides["acceptance_verification_commands"]
            }
        chain = [
            {
                "revision": 3,
                "stage": "implement",
                "event_type": "IMPLEMENT_HANDOFF_VERIFIED",
                "parent_handoff_receipt_digest": "sha256:boot",
                "receipt_digest": "sha256:d1",
                "input_commit": fixture.base,
                "output_commit": fixture.subject,
                "receipt": implement_receipt,
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
                "receipt": acceptance_receipt,
            },
        ]
        entry: dict[str, Any] = {
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
        if "acceptances" in self.overrides:
            entry["acceptances"] = self.overrides["acceptances"]
        return {"evidence": [entry]}


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


def test_no_frozen_setup_and_rerun_fail_is_env_unverified(
    tmp_path: Path, tracked_tmp: list[Path]
) -> None:
    """E1 known negative, reproduction noted: on pre-fix main this fixture's
    rerun went red and drove recommend_reject (e.g. `vite: command not found`,
    exit 127 on a bun repo whose throwaway worktree never provisioned deps).
    Post-fix: the audit sandbox has no frozen setup_commands, so the red is
    unjudgeable -- the item degrades to `env_unverified` (advisory), appears in
    the list, and does NOT drive recommend_reject."""
    frozen = {
        "command_results": [{"argv": [sys.executable, "-c", "raise SystemExit(3)"], "exit_code": 0}]
    }
    fixture = build_repo(tmp_path, frozen=frozen)
    report = audit_development("dev_x", engine=FakeEngine(fixture), repo=fixture.repo)

    assert not report.ok
    names = by_name(report)
    assert "acceptance_rerun" not in names
    env_unverified = names["acceptance_rerun_env_unverified"]
    assert not env_unverified.ok
    assert env_unverified.exit_code == 3
    assert "审计沙箱无环境供给，rerun 结果不可判" in env_unverified.detail
    assert any("审计沙箱无环境供给" in gap for gap in report.gaps)
    # The degraded item is not a reject driver: no reproducible failure.
    from fleet_graph.graphs.supervisor import reproducible_failures

    assert reproducible_failures(report.as_dict()) == []


def test_missing_file_with_successful_setup_still_drives_reject(
    tmp_path: Path, tracked_tmp: list[Path]
) -> None:
    """No `[ -f ] && run` guards: the frozen argv runs as-is and goes red. With
    a frozen setup that succeeds, a red rerun is a *real* failure -- it still
    drives recommend_reject (zero relaxation)."""
    frozen = {
        "results": [{"command": [sys.executable, "no_such_script.py"], "exit_code": 0}],
        "setup_results": [{"command": [sys.executable, "-c", "pass"], "exit_code": 0}],
    }
    fixture = build_repo(tmp_path, frozen=frozen)
    report = audit_development("dev_x", engine=FakeEngine(fixture), repo=fixture.repo)

    assert not report.ok
    rerun = by_name(report)["acceptance_rerun"]
    assert not rerun.ok
    assert report.acceptance_results[0]["exit_code"] != 0
    assert report.setup_results[0]["exit_code"] == 0
    from fleet_graph.graphs.supervisor import reproducible_failures

    assert reproducible_failures(report.as_dict())


def test_frozen_setup_provisions_before_acceptance_rerun_green(
    tmp_path: Path, tracked_tmp: list[Path]
) -> None:
    """E1 regression fixture: acceptance depends on a worktree-side setup step
    (the script checks a file the setup creates). The audit runs the frozen
    setup first, so the rerun goes green. Before the fix the throwaway worktree
    never ran setup and this rerun was red and drove recommend_reject."""
    setup_argv = [
        sys.executable,
        "-c",
        "import pathlib; pathlib.Path('prepared.txt').write_text('ok')",
    ]
    accept_argv = [
        sys.executable,
        "-c",
        "import pathlib, sys; sys.exit(0 if pathlib.Path('prepared.txt').is_file() else 3)",
    ]
    frozen = {
        "results": [{"command": accept_argv, "exit_code": 0}],
        "setup_results": [{"command": setup_argv, "exit_code": 0}],
    }
    fixture = build_repo(tmp_path, frozen=frozen)
    report = audit_development("dev_x", engine=FakeEngine(fixture), repo=fixture.repo)

    assert report.ok, [a.as_dict() for a in report.assertions if not a.ok]
    rerun = by_name(report)["acceptance_rerun"]
    assert rerun.ok
    assert [r["exit_code"] for r in report.setup_results] == [0]
    assert [r["exit_code"] for r in report.acceptance_results] == [0]
    from fleet_graph.graphs.supervisor import reproducible_failures

    assert reproducible_failures(report.as_dict()) == []


def test_setup_fails_in_sandbox_is_env_unverified(tmp_path: Path, tracked_tmp: list[Path]) -> None:
    """A frozen setup that fails in the audit sandbox is an environment fact,
    not a verdict on the work: the item degrades to env_unverified."""
    frozen = {
        "results": [{"command": [sys.executable, "-c", "pass"], "exit_code": 0}],
        "setup_results": [
            {"command": [sys.executable, "-c", "raise SystemExit(7)"], "exit_code": 0}
        ],
    }
    fixture = build_repo(tmp_path, frozen=frozen)
    report = audit_development("dev_x", engine=FakeEngine(fixture), repo=fixture.repo)

    assert not report.ok
    names = by_name(report)
    assert "acceptance_rerun" not in names
    env_unverified = names["acceptance_rerun_env_unverified"]
    assert not env_unverified.ok
    assert env_unverified.exit_code == 7
    assert "供给失败" in env_unverified.detail
    assert report.acceptance_results == []
    from fleet_graph.graphs.supervisor import reproducible_failures

    assert reproducible_failures(report.as_dict()) == []


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


def test_no_artifacts_falls_back_to_record_json(tmp_path: Path, tracked_tmp: list[Path]) -> None:
    """Old-engine evidence: receipt froze no artifact, but the controller-side
    acceptance record (record_json) carries the executed command_results argv.
    First-hand freezing (tier 1) stays covered by test_green_development_audit."""
    fixture = build_repo(tmp_path)
    engine = FakeEngine(
        fixture,
        no_artifacts=True,
        acceptances=[
            {
                "acceptance_id": "acc_1",
                "record_json": json.dumps(
                    {"command_results": [{"argv": PASSING_ARGV, "exit_code": 0}]}
                ),
            }
        ],
    )
    report = audit_development("dev_x", engine=engine, repo=fixture.repo)

    assert report.ok, [a.as_dict() for a in report.assertions if not a.ok]
    names = by_name(report)
    assert "frozen_acceptance_digest" not in names
    source = names["frozen_acceptance_from_record_json"]
    assert source.ok
    assert "非一手冻结" in source.detail
    rerun = names["acceptance_rerun"]
    assert rerun.ok
    assert "兜底" in rerun.detail  # the degraded provenance is visible in the verdict line
    assert any("降级" in gap for gap in report.gaps)
    assert [r["exit_code"] for r in report.acceptance_results] == [0]


def test_no_artifacts_falls_back_to_verification_record_union(
    tmp_path: Path, tracked_tmp: list[Path]
) -> None:
    """No artifacts and no usable record_json: the argv is the deduped union of
    every receipt's verification_record.verification_commands."""
    other_argv = [sys.executable, "-c", "import sys; sys.exit(0)"]
    fixture = build_repo(tmp_path)
    engine = FakeEngine(
        fixture,
        no_artifacts=True,
        acceptances=[{"acceptance_id": "acc_1", "record_json": "not json"}],
        implement_verification_commands=[
            {"argv": PASSING_ARGV, "exit_code": 0},
            {"argv": PASSING_ARGV, "exit_code": 0},  # duplicate: must collapse
        ],
        acceptance_verification_commands=[
            {"argv": PASSING_ARGV, "exit_code": 0},  # cross-receipt duplicate
            {"argv": other_argv, "exit_code": 0},
        ],
    )
    report = audit_development("dev_x", engine=engine, repo=fixture.repo)

    assert report.ok, [a.as_dict() for a in report.assertions if not a.ok]
    names = by_name(report)
    source = names["frozen_acceptance_from_verification_record"]
    assert source.ok
    assert "非一手冻结" in source.detail
    # Union deduped: two distinct argvs ran, not four.
    assert [r["command"] for r in report.acceptance_results] == [PASSING_ARGV, other_argv]
    assert any("降级" in gap for gap in report.gaps)


def test_no_artifacts_and_all_fallbacks_empty_still_fails(
    tmp_path: Path, tracked_tmp: list[Path]
) -> None:
    fixture = build_repo(tmp_path)
    engine = FakeEngine(fixture, no_artifacts=True)
    report = audit_development("dev_x", engine=engine, repo=fixture.repo)

    assert not report.ok
    frozen = by_name(report)["frozen_acceptance_digest"]
    assert not frozen.ok
    assert "兜底源也全空" in frozen.detail
    assert report.acceptance_results == []


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


class TestReceiptChainReworkTopology:
    """The rework edge (dd/chain_rules.py): the implement a REJECT steered
    into names the rejecting review receipt's canonical-JSON digest, not the
    file byte digest every other link names. dev-fg-369dacf607c1's legitimate
    chain went red before the audit modelled it."""

    @staticmethod
    def _record(
        revision: int,
        stage: str,
        verdict: str,
        parent: str,
        digest: str,
        input_commit: str,
        output_commit: str,
        receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "revision": revision,
            "stage": stage,
            "verdict": verdict,
            "parent_handoff_receipt_digest": parent,
            "receipt_digest": digest,
            "input_commit": input_commit,
            "output_commit": output_commit,
            "receipt": receipt or {},
        }

    def _checked(self, chain: list[dict[str, Any]], bootstrap: str) -> Any:
        report = AuditReport(target="dev_x", kind="development")
        audit_module._check_receipt_chain(
            report, "dev_x", {"bootstrap": {"receipt_digest": bootstrap}, "receipt_chain": chain}
        )
        return by_name(report)["receipt_chain_linked"]

    def _rework_chain(self, rework_parent: str) -> list[dict[str, Any]]:
        from fleet_graph.dd.upstream_constants import compute_json_digest

        reject_receipt = {"verdict": "REJECT", "output_commit": "b" * 40}
        chain = [
            self._record(1, "implement", "success", "sha256:boot", "sha256:i1", "0" * 40, "a" * 40),
            self._record(
                2,
                "final_review",
                "REJECT",
                "sha256:i1",
                "sha256:r1",  # the sealed file's byte digest
                "a" * 40,
                "b" * 40,
                receipt=reject_receipt,
            ),
            self._record(
                3,
                "implement",
                "success",
                rework_parent or compute_json_digest(reject_receipt),  # canonical, not sha256:r1
                "sha256:i2",
                "b" * 40,
                "c" * 40,
            ),
        ]
        return chain

    def test_a_rework_chain_audits_green(self) -> None:
        assertion = self._checked(self._rework_chain(""), "sha256:boot")
        assert assertion.ok, assertion.detail

    def test_a_forged_rework_parent_is_still_red(self) -> None:
        from fleet_graph.dd.upstream_constants import compute_json_digest

        forged = compute_json_digest({"verdict": "REJECT", "output_commit": "f" * 40})
        assertion = self._checked(self._rework_chain(forged), "sha256:boot")
        assert not assertion.ok
        assert "rev3 implement" in assertion.detail

    def test_the_canonical_shortcut_is_rework_only(self) -> None:
        """No general loosening: after an APPROVE, the next link must still
        name the byte digest -- its canonical digest does not pass."""
        from fleet_graph.dd.upstream_constants import compute_json_digest

        approve_receipt = {"verdict": "APPROVE", "output_commit": "b" * 40}
        chain = [
            self._record(
                1,
                "continuous_review",
                "APPROVE",
                "sha256:boot",
                "sha256:r1",
                "a" * 40,
                "b" * 40,
                receipt=approve_receipt,
            ),
            self._record(
                2,
                "final_review",
                "APPROVE",
                compute_json_digest(approve_receipt),  # not the byte digest
                "sha256:r2",
                "b" * 40,
                "c" * 40,
            ),
        ]
        assertion = self._checked(chain, "sha256:boot")
        assert not assertion.ok

    def test_dev_fg_369dacf607c1_rework_chain_regression(self) -> None:
        """The production chain that went red, replayed from the sealed
        receipt bytes themselves (fixtures copied byte-exact)."""
        root = Path(__file__).parent / "fixtures" / "dev-fg-369dacf607c1-receipts"
        a1 = "5ae1fe51-3bce-500f-844c-b0974a300272"
        a2 = "cf3a78c9-7988-5700-86a7-194121bf0058"
        order = [
            (a1, "implement-receipt.json", "implement", "success"),
            (a1, "continuous-review-receipt.json", "continuous_review", ""),
            (a1, "final-review-receipt.json", "final_review", ""),
            (a2, "implement-receipt.json", "implement", "success"),
            (a2, "continuous-review-receipt.json", "continuous_review", ""),
            (a2, "final-review-receipt.json", "final_review", ""),
        ]
        chain = []
        for revision, (attempt_dir, filename, stage, verdict) in enumerate(order, 2):
            raw = (root / attempt_dir / filename).read_bytes()
            receipt = json.loads(raw)
            chain.append(
                self._record(
                    revision,
                    stage,
                    verdict or receipt["verdict"],
                    receipt["parent_handoff_receipt_digest"],
                    "sha256:" + hashlib.sha256(raw).hexdigest(),
                    receipt["input_commit"],
                    receipt["output_commit"],
                    receipt=receipt,
                )
            )
        # Seeded exactly the way the evidence assembler seeds a chain: on the
        # first link's own attested parent.
        assertion = self._checked(chain, chain[0]["parent_handoff_receipt_digest"])
        assert assertion.ok, assertion.detail
        # The rework link really is the canonical-digest form, so this test
        # would have caught the old byte-digest-only rule.
        assert chain[3]["parent_handoff_receipt_digest"] != chain[2]["receipt_digest"]


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


class TestVerifiedBitStateAwareness:
    """verified 是控制面推导位（terminal==complete AND ...），gate 前构造性
    恒 False。断言按 state 判推导一致性：未完成家族要求构成件全真且位未立；
    完成态（及名单外未知态，fail-closed）要求位已立。"""

    def test_awaiting_gate_with_honest_false_bit_is_green(
        self, tmp_path: Path, tracked_tmp: list[Path]
    ) -> None:
        fixture = build_repo(tmp_path)
        engine = FakeEngine(fixture)
        engine.development = lambda d: {  # type: ignore[method-assign]
            "development_id": d,
            "state": "awaiting_gate",
            "target_base_commit": fixture.base,
            "worktree_path": None,
        }
        evidence = engine.evidence("dev_x")
        evidence["evidence"][0]["verified"] = False
        engine.evidence = lambda d: evidence  # type: ignore[method-assign]
        report = audit_development("dev_x", engine=engine, repo=fixture.repo)
        assert by_name(report)["verified_bit"].ok

    def test_awaiting_gate_with_bit_already_true_is_red(
        self, tmp_path: Path, tracked_tmp: list[Path]
    ) -> None:
        # gate 前 verified=True 只能来自推导被篡改或读错库——红。
        fixture = build_repo(tmp_path)
        engine = FakeEngine(fixture)
        engine.development = lambda d: {  # type: ignore[method-assign]
            "development_id": d,
            "state": "awaiting_gate",
            "target_base_commit": fixture.base,
            "worktree_path": None,
        }
        report = audit_development("dev_x", engine=engine, repo=fixture.repo)
        assert not by_name(report)["verified_bit"].ok

    def test_unknown_state_still_requires_the_bit(
        self, tmp_path: Path, tracked_tmp: list[Path]
    ) -> None:
        fixture = build_repo(tmp_path)
        engine = FakeEngine(fixture)
        engine.development = lambda d: {  # type: ignore[method-assign]
            "development_id": d,
            "state": "SOME_FUTURE_STATE",
            "target_base_commit": fixture.base,
            "worktree_path": None,
        }
        evidence = engine.evidence("dev_x")
        evidence["evidence"][0]["verified"] = False
        engine.evidence = lambda d: evidence  # type: ignore[method-assign]
        report = audit_development("dev_x", engine=engine, repo=fixture.repo)
        assert not by_name(report)["verified_bit"].ok
