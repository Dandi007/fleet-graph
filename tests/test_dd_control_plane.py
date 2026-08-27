"""The in-process control plane: admission derivation, launch, and read side.

Three rulings are pinned here, each by a test that fails if the property is
walked back:

- **Admission is server-side derivation** -- create takes a repo, a base and
  a spec, and every derived fact it returns (id, digests, H0, acceptance
  argv) is independently recomputable from git plus the record.
- **State is git + checkpoint + run artifacts, no database** -- status.json
  is a cache, and `rebuild_status` reproduces it wholesale after deletion.
- **A kill-restart re-enters the same thread** -- the checkpoint path and
  thread identity are derived from the development id, and a restart launches
  with `--resume` against the same checkpoint.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import git, head
from fleet_graph.dd.bootstrap import (
    DEVELOPMENT_PATH,
    SPEC_PATH,
    canonical_bytes,
    digest_of,
)
from fleet_graph.dd.control_plane import (
    CHECKPOINT_FILE,
    RECORD_FILE,
    RESULT_FILE,
    STATUS_FILE,
    ControlPlaneError,
    DdControlPlane,
    derive_acceptance_commands,
    derive_development_id,
)
from fleet_graph.scheduler.launcher import LaunchResult

SPEC = """# SPEC: add a name parameter to greet()

Make `greet(name)` return a personalised greeting.

```dd-acceptance
# comment lines and blanks are ignored
python3 -m pytest -q
sh -c "echo 'quoted argument'"
```
"""


class RecordingLauncher:
    """Stands in for TransientLauncher; records the specs it was handed."""

    dry_run = False

    def __init__(self, *, started: bool = True) -> None:
        self.specs: list[Any] = []
        self.started = started

    def launch(self, spec: Any) -> LaunchResult:
        self.specs.append(spec)
        return LaunchResult(spec.unit_name, self.started, "recorded")


class FakeBoard:
    """publish_card only -- the control plane must not need anything more."""

    def __init__(self) -> None:
        self.cards: list[tuple[dict[str, Any], str]] = []

    def publish_card(self, payload: dict[str, Any], idempotency_key: str) -> Any:
        self.cards.append((payload, idempotency_key))

        class Result:
            entity_id = "ent-dd-card"

        return Result()


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    """A dedicated worktree with a local bare origin -- the §24 shape."""
    repo = tmp_path / "work"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "greet.py").write_text('def greet():\n    return "hello"\n', encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "seed")
    bare = tmp_path / "origin.git"
    git(repo, "init", "-q", "--bare", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    return repo


def make_plane(
    tmp_path: Path,
    *,
    launcher: Any = None,
    unit_probe: Any = None,
    board: Any = None,
) -> DdControlPlane:
    binding = tmp_path / "plugin-binding.json"
    if not binding.exists():
        binding.write_text('{"plugin_producer": {}}', encoding="utf-8")
    return DdControlPlane(
        root=tmp_path / "dd",
        plugin_binding=binding,
        worktree_roots=(str(tmp_path),),
        working_directory=str(tmp_path),
        executable="/usr/local/bin/fleet-graph",
        launcher=launcher if launcher is not None else RecordingLauncher(),
        unit_probe=unit_probe if unit_probe is not None else (lambda unit: False),
        board_factory=lambda: board,
        clock=lambda: 1_700_000_000.0,
    )


class TestAdmissionDerivation:
    def test_create_derives_everything_and_each_fact_is_recomputable(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        base = head(scratch)
        plane = make_plane(tmp_path, board=FakeBoard())
        created = plane.create(str(scratch), spec_text=SPEC)

        dev = created["development_id"]
        spec_digest = digest_of(SPEC.encode("utf-8"))
        # The id is a digest over the admission inputs -- recomputable.
        assert dev == derive_development_id(scratch, spec_digest, base)

        # Bootstrap froze the spec bytes and the base into the repo.
        boot = created["bootstrap"]
        assert boot["target_base_commit"] == base
        assert boot["spec_digest"] == spec_digest
        identity = json.loads(git(scratch, "show", f"HEAD:{DEVELOPMENT_PATH}"))
        assert identity["development_id"] == dev
        assert identity["target_base_commit"] == base
        assert identity["spec_digest"] == spec_digest
        assert identity["spec_path"] == SPEC_PATH

        # The H0 handoff is on disk in canonical bytes and its digest is the
        # chain root the record names.
        h0 = json.loads((plane.root / dev / "h0-handoff.json").read_text())
        assert digest_of(canonical_bytes(h0)) == boot["root_handoff_digest"]

        # The acceptance argv came out of the spec, quoting intact.
        assert created["acceptance_commands"] == [
            ["python3", "-m", "pytest", "-q"],
            ["sh", "-c", "echo 'quoted argument'"],
        ]

        # The durable ref is derived, and the board card was published.
        assert created["remote"]["ref"] == f"refs/heads/dd/{dev}"
        assert created["card_entity_id"] == "ent-dd-card"
        assert created["gate_enabled"] is True

    def test_create_is_idempotent_for_the_same_admission(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        plane = make_plane(tmp_path)
        first = plane.create(str(scratch), spec_text=SPEC)
        again = plane.create(str(scratch), spec_text=SPEC)
        assert again["development_id"] == first["development_id"]
        assert again["already_admitted"] is True
        assert first["already_admitted"] is False

    def test_a_second_spec_cannot_reuse_a_bound_worktree(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        plane = make_plane(tmp_path)
        plane.create(str(scratch), spec_text=SPEC)
        with pytest.raises(ControlPlaneError) as refused:
            plane.create(str(scratch), spec_text="# a different spec entirely\n")
        assert refused.value.code == "REPO_BOUND_TO_OTHER_DEVELOPMENT"

    def test_a_dirty_worktree_is_refused(self, scratch: Path, tmp_path: Path) -> None:
        (scratch / "untracked.txt").write_text("x", encoding="utf-8")
        with pytest.raises(ControlPlaneError) as refused:
            make_plane(tmp_path).create(str(scratch), spec_text=SPEC)
        assert refused.value.code == "WORKTREE_DIRTY"

    def test_a_repo_outside_the_whitelist_is_refused(self, scratch: Path, tmp_path: Path) -> None:
        plane = make_plane(tmp_path)
        plane.worktree_roots = ("/nonexistent-root",)
        with pytest.raises(ControlPlaneError) as refused:
            plane.create(str(scratch), spec_text=SPEC)
        assert refused.value.code == "WORKTREE_ROOT_NOT_ALLOWED"

    def test_spec_text_and_spec_path_are_mutually_exclusive(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        plane = make_plane(tmp_path)
        for kwargs in ({}, {"spec_text": SPEC, "spec_path": "/tmp/x"}):
            with pytest.raises(ControlPlaneError) as refused:
                plane.create(str(scratch), **kwargs)
            assert refused.value.code == "SPEC_INPUT_INVALID"

    def test_a_repo_with_no_origin_is_refused(self, tmp_path: Path) -> None:
        repo = tmp_path / "loner"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        (repo / "a.txt").write_text("a", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "seed")
        with pytest.raises(ControlPlaneError) as refused:
            make_plane(tmp_path).create(str(repo), spec_text=SPEC)
        assert refused.value.code == "REPO_HAS_NO_ORIGIN"


class TestAcceptanceDerivation:
    def test_lines_parse_with_shell_quoting(self) -> None:
        spec = b"```dd-acceptance\nmake verify\npytest -k 'not slow'\n```\n"
        assert derive_acceptance_commands(spec) == [
            ["make", "verify"],
            ["pytest", "-k", "not slow"],
        ]

    def test_a_spec_without_a_block_declares_nothing(self) -> None:
        assert derive_acceptance_commands(b"# spec\n") == []

    def test_unparseable_quoting_is_refused_not_guessed(self) -> None:
        with pytest.raises(ControlPlaneError) as refused:
            derive_acceptance_commands(b'```dd-acceptance\necho "unclosed\n```\n')
        assert refused.value.code == "ACCEPTANCE_DECLARATION_INVALID"


class TestStartAndReAdopt:
    def test_start_launches_a_detached_run_with_the_derived_identity(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher=launcher)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        started = plane.start(dev)

        assert started["started"] is True
        assert started["thread_id"] == f"{dev}:g1"
        argv = launcher.specs[0].argv()
        assert argv[0] == "systemd-run"
        assert "--resume" not in argv
        checkpoint = argv[argv.index("--checkpoint") + 1]
        assert checkpoint == str(plane.root / dev / CHECKPOINT_FILE)
        # The acceptance argv survives the systemd boundary with quoting:
        # shlex.join here, shlex.split on the dd-run side.
        import shlex

        accepted = [argv[i + 1] for i, a in enumerate(argv) if a == "--accept"]
        assert accepted == [
            shlex.join(["python3", "-m", "pytest", "-q"]),
            shlex.join(["sh", "-c", "echo 'quoted argument'"]),
        ]
        assert [shlex.split(a) for a in accepted] == [
            ["python3", "-m", "pytest", "-q"],
            ["sh", "-c", "echo 'quoted argument'"],
        ]

    def test_a_restart_after_a_kill_resumes_the_same_thread(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        """The re-adopt pin: same checkpoint, same derived identity, and the
        relaunch says --resume so sealed stages are not re-dispatched."""
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher=launcher)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        plane.start(dev)
        # The run wrote its durable checkpoint, then the unit was killed.
        (plane.root / dev / CHECKPOINT_FILE).touch()

        second = plane.start(dev)
        assert second["mode"] == "resume"
        first_argv, second_argv = launcher.specs[0].argv(), launcher.specs[1].argv()
        assert "--resume" in second_argv
        index = first_argv.index("--checkpoint")
        assert second_argv[second_argv.index("--checkpoint") + 1] == first_argv[index + 1]
        assert second["thread_id"] == f"{dev}:g1"
        # A fresh unit name (systemd may still be tearing the old one down),
        # but the identity underneath did not move.
        assert launcher.specs[0].unit_name != launcher.specs[1].unit_name

    def test_starting_a_running_development_is_a_visible_noop(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        launcher = RecordingLauncher()
        active: set[str] = set()
        plane = make_plane(tmp_path, launcher=launcher, unit_probe=lambda unit: unit in active)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        first = plane.start(dev)
        active.add(first["unit"])
        second = plane.start(dev)
        assert second["already_running"] is True
        assert len(launcher.specs) == 1


class TestGate:
    def _suspended(self, plane: DdControlPlane, scratch: Path) -> str:
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        dev_root = plane.root / dev
        dev_root.mkdir(parents=True, exist_ok=True)
        (dev_root / RESULT_FILE).write_text(
            json.dumps(
                {
                    "development_id": dev,
                    "terminal": None,
                    "stage": "human_gate",
                    "head_commit": head(scratch),
                    "awaiting": {
                        "question_note_id": "msg_question_1",
                        "card_entity_id": "ent-dd-card",
                    },
                    "history": [],
                }
            ),
            encoding="utf-8",
        )
        (dev_root / CHECKPOINT_FILE).touch()
        return dev

    def test_the_gate_reports_the_pending_question_note(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        plane = make_plane(tmp_path)
        dev = self._suspended(plane, scratch)
        report = plane.gate(dev)
        assert report["pending"] is True
        assert report["awaiting"]["question_note_id"] == "msg_question_1"
        assert report["state"] == "awaiting_gate"
        assert "work.decision.v1" in report["ruling"]

    def test_resume_relaunches_the_thread_and_carries_no_verdict(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher=launcher)
        dev = self._suspended(plane, scratch)
        report = plane.gate(dev, resume=True)
        assert report["resume"]["mode"] == "resume"
        argv = launcher.specs[-1].argv()
        assert "--resume" in argv
        # No verdict can travel this path: nothing in the argv looks like one.
        for forbidden in ("APPROVE", "REJECT", "--decision", "--verdict"):
            assert forbidden not in argv

    def test_resume_without_a_checkpoint_is_refused(self, scratch: Path, tmp_path: Path) -> None:
        plane = make_plane(tmp_path)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        with pytest.raises(ControlPlaneError) as refused:
            plane.gate(dev, resume=True)
        assert refused.value.code == "CHECKPOINT_MISSING"


class TestStatusCacheIsACache:
    def test_rebuild_reproduces_the_cache_from_the_authoritative_sources(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        plane = make_plane(tmp_path)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        plane.start(dev)
        dev_root = plane.root / dev
        (dev_root / RESULT_FILE).write_text(
            json.dumps(
                {
                    "development_id": dev,
                    "terminal": "complete",
                    "terminal_reason": "merger is the last declared stage",
                    "stage": "merger",
                    "head_commit": head(scratch),
                    "awaiting": None,
                    "history": [],
                }
            ),
            encoding="utf-8",
        )
        before = plane.rebuild_status(dev)
        (dev_root / STATUS_FILE).unlink()
        assert plane.rebuild_status(dev) == before
        assert json.loads((dev_root / STATUS_FILE).read_text()) == before
        assert before["state"] == "complete"

    def test_list_survives_a_corrupted_cache_by_rebuilding(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        plane = make_plane(tmp_path)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        (plane.root / dev / STATUS_FILE).write_text("not json", encoding="utf-8")
        listed = plane.list()
        assert [row["development_id"] for row in listed["developments"]] == [dev]
        assert listed["developments"][0]["state"] == "created"


class TestEvents:
    def test_events_page_by_event_id(self, scratch: Path, tmp_path: Path) -> None:
        plane = make_plane(tmp_path)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        events_path = plane.root / dev / "events.jsonl"
        with events_path.open("w", encoding="utf-8") as handle:
            for stage in ("configure", "implement", "continuous_review"):
                handle.write(json.dumps({"stage": stage, "event": "success"}) + "\n")
        page = plane.events(dev, after="e1", limit=1)
        assert [e["event_id"] for e in page["events"]] == ["e2"]
        assert page["events"][0]["stage"] == "implement"
        assert page["head_event_id"] == "e3"

    def test_a_bad_cursor_is_refused(self, scratch: Path, tmp_path: Path) -> None:
        plane = make_plane(tmp_path)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        with pytest.raises(ControlPlaneError) as refused:
            plane.events(dev, after="latest")
        assert refused.value.code == "EVENT_CURSOR_INVALID"


def _complete_run(plane: DdControlPlane, scratch: Path) -> tuple[str, str]:
    """A development whose acceptance ran and whose chain reached the remote."""
    created = plane.create(str(scratch), spec_text=SPEC)
    dev = created["development_id"]
    record = json.loads((plane.root / dev / RECORD_FILE).read_text())

    acceptance_record = {
        "development_id": dev,
        "attempt": 1,
        "passed": True,
        "results": [{"command": ["true"], "exit_code": 0, "stdout_tail": "", "stderr_tail": ""}],
    }
    path = scratch / ".dd-evidence" / "acceptance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(acceptance_record), encoding="utf-8")
    git(scratch, "add", "-A")
    git(scratch, "commit", "-q", "-m", "dev-dispatch: acceptance")
    accepted = head(scratch)
    git(scratch, "push", "-q", "origin", f"HEAD:{record['remote_ref']}")

    (plane.root / dev / RESULT_FILE).write_text(
        json.dumps(
            {
                "development_id": dev,
                "terminal": "complete",
                "terminal_reason": "merger is the last declared stage",
                "stage": "merger",
                "head_commit": accepted,
                "awaiting": None,
                "history": [
                    {
                        "stage": "configure",
                        "event": "success",
                        "attempt": 1,
                        "output_commit": record["bootstrap_commit"],
                    },
                    {
                        "stage": "acceptance",
                        "event": "success",
                        "attempt": 1,
                        "output_commit": accepted,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return dev, accepted


class TestEvidence:
    def test_the_entry_is_assembled_live_and_every_digest_recomputes(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        plane = make_plane(tmp_path)
        dev, accepted = _complete_run(plane, scratch)
        payload = plane.evidence(dev)
        entry = payload["evidence"][0]

        assert entry["accepted_commit_ancestor"] is True
        assert entry["remote_main_verified"] is True
        assert entry["verified"] is True
        assert entry["bootstrap"]["receipt_digest"].startswith("sha256:")
        assert entry["bootstrap"]["h0"]["digest_recomputed"] == entry["bootstrap"]["receipt_digest"]

        chain = entry["receipt_chain"]
        assert [r["stage"] for r in chain] == ["configure", "acceptance"]
        # The chain-root link closes on the H0 digest.
        assert chain[0]["parent_handoff_receipt_digest"] == entry["bootstrap"]["receipt_digest"]
        assert chain[1]["parent_handoff_receipt_digest"] == chain[0]["receipt_digest"]
        # Honesty bit: none of these links carries a plugin attestation.
        assert {r["parent_source"] for r in chain} == {"derived"}

        acceptance = chain[1]
        assert acceptance["receipt"]["subject_commit"] == accepted
        artifact = acceptance["receipt"]["artifacts"][0]
        assert artifact["path"] == ".dd-evidence/acceptance.json"
        raw = git(scratch, "show", f"{accepted}:.dd-evidence/acceptance.json")
        import hashlib

        assert artifact["digest"] == "sha256:" + hashlib.sha256(raw.encode()).hexdigest()

    def test_the_supervision_audit_consumes_the_new_engine_directly(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        """audit_development runs against GraphEngineSource with no old engine
        anywhere: identity anchored in git, frozen argv re-run in a throwaway
        worktree, all green."""
        from fleet_graph.supervise.audit import GraphEngineSource, audit_development

        plane = make_plane(tmp_path)
        dev, _accepted = _complete_run(plane, scratch)
        report = audit_development(dev, engine=GraphEngineSource(plane), repo=scratch)

        by_name = {a.name: a for a in report.assertions}
        for name in (
            "evidence_present",
            "acceptance_receipt_present",
            "accepted_commit_in_git",
            "target_base_recomputed",
            "identity_binding",
            "target_base_is_ancestor",
            "frozen_acceptance_digest",
            "acceptance_no_skips",
            "acceptance_rerun",
        ):
            assert by_name[name].ok, f"{name}: {by_name[name].detail}"


class TestCredentialDiscipline:
    def test_only_the_token_file_path_is_forwarded_to_the_unit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A raw token value must never reach --setenv (argv is /proc-public);
        the 0600 token file's *path* is what crosses the boundary."""
        monkeypatch.setenv("FLEET_GRAPH_BUS_TOKEN", "secret-value")
        monkeypatch.setenv("FLEET_GRAPH_BUS_TOKEN_FILE", "/data/agent-bus/tokens/fleet-graph.token")
        plane = DdControlPlane(root=tmp_path / "dd", board_factory=lambda: None)
        assert (
            plane.environment["FLEET_GRAPH_BUS_TOKEN_FILE"]
            == "/data/agent-bus/tokens/fleet-graph.token"
        )
        assert "secret-value" not in json.dumps(plane.environment)
        # PATH rides along: agent-run is a bun script and transient units do
        # not inherit this process's PATH (scheduler line_environment lesson).
        assert plane.environment["PATH"]
        monkeypatch.delenv("FLEET_GRAPH_BUS_TOKEN_FILE")
        bare = DdControlPlane(root=tmp_path / "dd", board_factory=lambda: None)
        assert set(bare.environment) == {"PATH"}, "a raw token value is never forwarded"


class TestStageModelPolicy:
    def test_server_side_stage_models_reach_the_launched_run(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        """Model overrides are deploy-level policy on the control plane, not
        client vocabulary -- create's schema has no model parameter."""
        launcher = RecordingLauncher()
        binding = tmp_path / "plugin-binding.json"
        binding.write_text('{"plugin_producer": {}}', encoding="utf-8")
        plane = DdControlPlane(
            root=tmp_path / "dd",
            plugin_binding=binding,
            worktree_roots=(str(tmp_path),),
            launcher=launcher,
            unit_probe=lambda unit: False,
            board_factory=lambda: None,
            stage_models={"continuous_review": "deepseek-v4-pro"},
        )
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        plane.start(dev)
        argv = launcher.specs[0].argv()
        assert argv[argv.index("--stage-model") + 1] == "continuous_review=deepseek-v4-pro"


class TestListDoesNotServeStaleLiveness:
    def test_a_cached_running_row_is_recomputed_once_the_unit_is_gone(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        """Measured on the real machine: a run that failed after the cache was
        written kept listing as running. Terminal cache rows are immutable and
        trusted; non-terminal rows are recomputed on every list."""
        plane = make_plane(tmp_path)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        dev_root = plane.root / dev
        stale = {
            "development_id": dev,
            "state": "running",
            "stage": "",
            "terminal": "",
            "terminal_reason": "",
            "head_commit": "",
            "awaiting": None,
            "active_unit": "fleet-graph-dd-gone-r1",
            "launches": 1,
        }
        (dev_root / STATUS_FILE).write_text(json.dumps(stale), encoding="utf-8")
        (dev_root / RESULT_FILE).write_text(
            json.dumps(
                {
                    "development_id": dev,
                    "terminal": "failed",
                    "terminal_reason": "implement failed (PROVIDER_UNAVAILABLE)",
                    "stage": "implement",
                    "head_commit": "",
                    "awaiting": None,
                    "history": [],
                }
            ),
            encoding="utf-8",
        )
        listed = plane.list()
        assert listed["developments"][0]["state"] == "failed"
