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
    IdentityChanged,
    canonical_bytes,
    committed_target_base,
    digest_of,
)
from fleet_graph.dd.control_plane import (
    CHECKPOINT_FILE,
    RECORD_FILE,
    RESULT_FILE,
    STATUS_FILE,
    ControlPlaneError,
    DdControlPlane,
    DdLaunchSpec,
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


class TestFrozenTargetBaseForwarding:
    """The admitted, persisted `target_base_commit` crosses into the runner's
    argv as an explicit `--target-base`, never re-inferred from the worktree.

    A worktree whose lineage already carries an older metadata commit -- a
    previous development re-writing `.dev-dispatch/development.json` in place
    -- makes `committed_target_base`'s `--diff-filter=A` anchor point at
    someone else's introduction, so an untouched bootstrap blobs reads as
    edited. Forwarding the frozen admission target sidesteps that entirely,
    while a genuine post-bootstrap mutation is still refused.
    """

    def _legacy_metadata_repo(self, tmp_path: Path) -> Path:
        """seed base -> a commit that introduced the identity file without a
        frozen base (the older ancestor metadata commit, an A for the path)."""
        repo = tmp_path / "legacy"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        (repo / "greet.py").write_text('def greet():\n    return "hello"\n', encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "seed")
        (repo / DEVELOPMENT_PATH).parent.mkdir(parents=True, exist_ok=True)
        (repo / DEVELOPMENT_PATH).write_text(
            json.dumps(
                {
                    "contract_version": "dev-dispatch.attempt-context/v1",
                    "target_base_commit": "",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "legacy: metadata with no frozen base")
        bare = tmp_path / "legacy-origin.git"
        git(repo, "init", "-q", "--bare", str(bare))
        git(repo, "remote", "add", "origin", str(bare))
        return repo

    def test_the_recorded_target_base_is_forwarded_as_an_explicit_arg(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        """`start` passes `--target-base <frozen>` into the generated argv, so
        the runner gets the admission target verbatim instead of inferring it."""
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher=launcher)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        record = json.loads((plane.root / dev / RECORD_FILE).read_text())
        frozen = record["target_base_commit"]

        plane.start(dev)
        argv = launcher.specs[0].argv()
        assert argv[argv.index("--target-base") + 1] == frozen
        # The forwarded value is the frozen admission target, not HEAD (which
        # has moved past it by the bootstrap commit).
        assert frozen != head(scratch)

    def test_an_older_ancestor_metadata_commit_does_not_trip_start(self, tmp_path: Path) -> None:
        """The incident shape: a frozen admission target, untouched bootstrap
        metadata, and an older ancestor metadata commit. The `--diff-filter=A`
        anchor misreads the untouched bootstrap as edited -- but `start` still
        forwards the frozen base, so the runner never touches that anchor."""
        repo = self._legacy_metadata_repo(tmp_path)
        freezing = head(repo)  # the seed base, frozen as the admission target

        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher=launcher)
        dev = plane.create(str(repo), target_base=freezing, spec_text=SPEC)["development_id"]
        record = json.loads((plane.root / dev / RECORD_FILE).read_text())
        assert record["target_base_commit"] == freezing

        # Reproduce the false positive the runner used to hit: the identity
        # file is untouched since bootstrap, yet the native anchor sees an edit.
        with pytest.raises(IdentityChanged, match="edited since bootstrap"):
            committed_target_base(repo)

        # The forward sidesteps it: start emits the frozen base as --target-base
        # and no IDENTITY_EDITED refusal appears anywhere in its argv.
        started = plane.start(dev)
        assert started["started"] is True
        argv = launcher.specs[0].argv()
        assert "--target-base" in argv
        assert argv[argv.index("--target-base") + 1] == freezing
        assert all("IDENTITY_EDITED" not in a for a in argv)

    def test_the_launcher_spec_emits_the_recorded_base_verbatim(self, tmp_path: Path) -> None:
        """The argv seam: whatever the recorded admission froze is forwarded
        verbatim, so the exact frozen id survives the systemd-run boundary."""
        spec = DdLaunchSpec(
            development_id="dev-x",
            dev_root=tmp_path / "dd" / "dev-x",
            workspace=tmp_path / "w",
            plugin_binding=tmp_path / "b.json",
            remote_url="u",
            remote_ref="refs/heads/main",
            root_digest="sha256:" + "a" * 64,
            target_base_commit="86f929e8640b2008ae18130ba83ee91df428fc71",
        )
        argv = spec.argv()
        assert argv[argv.index("--target-base") + 1] == "86f929e8640b2008ae18130ba83ee91df428fc71"

    def test_a_genuine_post_bootstrap_edit_is_still_refused(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        """Forwarding must not soften immutability: a worktree whose identity
        the graded party actually rewrote is refused, not guessed around."""
        plane = make_plane(tmp_path)
        plane.create(str(scratch), spec_text=SPEC)

        identity = json.loads((scratch / DEVELOPMENT_PATH).read_text())
        identity["target_base_commit"] = "d" * 40
        (scratch / DEVELOPMENT_PATH).write_text(
            json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        git(scratch, "add", "-A")
        git(scratch, "commit", "-q", "-m", "feat: rewrite the identity")

        # A fresh plane (no admission record to hide behind) re-derives the
        # admission and refuses the edited identity rather than forwarding it.
        fresh = DdControlPlane(
            root=tmp_path / "dd-fresh",
            plugin_binding=tmp_path / "plugin-binding.json",
            worktree_roots=(str(tmp_path),),
            working_directory=str(tmp_path),
            executable="/usr/local/bin/fleet-graph",
            launcher=RecordingLauncher(),
            unit_probe=lambda unit: False,
            board_factory=lambda: None,
            clock=lambda: 1_700_000_000.0,
        )
        with pytest.raises(ControlPlaneError) as refused:
            fresh.create(str(scratch), spec_text=SPEC)
        assert refused.value.code == "IDENTITY_EDITED"


class TestDispatchedByForwarding:
    """The `dispatched_by` provenance the spec (DoD 3) requires reaches the
    runner: recorded at admission, forwarded through the launch argv as
    `--dispatched-by`, and from there into the runner's `DevelopmentConfig`.
    """

    def test_create_records_dispatched_by(self, scratch: Path, tmp_path: Path) -> None:
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher=launcher)
        dev = plane.create(str(scratch), spec_text=SPEC, dispatched_by="ronin-model-switch")[
            "development_id"
        ]
        record = json.loads((plane.root / dev / RECORD_FILE).read_text())
        assert record["dispatched_by"] == "ronin-model-switch"

    def test_start_forwards_dispatched_by_into_the_argv(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher=launcher)
        dev = plane.create(str(scratch), spec_text=SPEC, dispatched_by="ronin-model-switch")[
            "development_id"
        ]
        plane.start(dev)
        argv = launcher.specs[0].argv()
        assert argv[argv.index("--dispatched-by") + 1] == "ronin-model-switch"

    def test_the_spec_emits_no_flag_without_provenance(self, tmp_path: Path) -> None:
        spec = DdLaunchSpec(
            development_id="dev-x",
            dev_root=tmp_path / "dd" / "dev-x",
            workspace=tmp_path / "w",
            plugin_binding=tmp_path / "b.json",
            remote_url="u",
            remote_ref="refs/heads/main",
            root_digest="sha256:" + "a" * 64,
            target_base_commit="86f929e8640b2008ae18130ba83ee91df428fc71",
        )
        assert "--dispatched-by" not in spec.argv()

    def test_the_spec_emits_the_recorded_principal_verbatim(self, tmp_path: Path) -> None:
        spec = DdLaunchSpec(
            development_id="dev-x",
            dev_root=tmp_path / "dd" / "dev-x",
            workspace=tmp_path / "w",
            plugin_binding=tmp_path / "b.json",
            remote_url="u",
            remote_ref="refs/heads/main",
            root_digest="sha256:" + "a" * 64,
            target_base_commit="86f929e8640b2008ae18130ba83ee91df428fc71",
            dispatched_by="wf-goal-line",
        )
        argv = spec.argv()
        assert argv[argv.index("--dispatched-by") + 1] == "wf-goal-line"


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
        # The cost-observability site config is whitelist-forwarded and tested
        # separately (test_cost_observability_site_config_is_forwarded); clear
        # any ambient values so this credential assertion is deterministic.
        monkeypatch.delenv("FLEET_GRAPH_COST_OBS_DIR", raising=False)
        monkeypatch.delenv("FLEET_GRAPH_MANAGEMENT_COST", raising=False)
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

    def test_cost_observability_site_config_is_forwarded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The launched dd run collects only if its env carries the cost-obs
        wiring; the control plane's whitelist is where that site config crosses
        into the transient unit (the launch argv passes no --cost-obs-dir)."""
        monkeypatch.setenv("FLEET_GRAPH_COST_OBS_DIR", "/var/lib/node_exporter/textfile")
        monkeypatch.setenv("FLEET_GRAPH_MANAGEMENT_COST", "0.5")
        plane = DdControlPlane(root=tmp_path / "dd", board_factory=lambda: None)
        assert plane.environment["FLEET_GRAPH_COST_OBS_DIR"] == "/var/lib/node_exporter/textfile"
        assert plane.environment["FLEET_GRAPH_MANAGEMENT_COST"] == "0.5"


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


# --- R1-c: the three failure exits and the rerun generation ---------------


def write_result(
    plane: DdControlPlane, dev: str, payload: dict[str, Any], generation: int = 1
) -> None:
    root = plane.root / dev if generation <= 1 else plane.root / dev / f"g{generation}"
    root.mkdir(parents=True, exist_ok=True)
    (root / RESULT_FILE).write_text(json.dumps(payload), encoding="utf-8")


def failed_result(dev: str, *, code: str, reason: str, detail: str = "") -> dict[str, Any]:
    return {
        "development_id": dev,
        "terminal": "failed",
        "terminal_reason": reason,
        "terminal_code": code,
        "terminal_detail": detail,
        "stage": "implement",
        "head_commit": "",
        "awaiting": None,
        "history": [],
    }


class TestFailureClassification:
    """One failure record per non-complete terminal: cause class, one
    mechanical code, the raw error verbatim, retryability, and which of the
    three exits is open."""

    def test_an_environment_code_opens_the_reconfigure_exit(self) -> None:
        from fleet_graph.dd.control_plane import classify_failure

        failure = classify_failure(
            "failed",
            "acceptance failed: [['make', 'verify']]",
            "ACCEPTANCE_FAILED",
            "tsc: not found",
        )
        assert failure == {
            "class": "environment_contract",
            "code": "ACCEPTANCE_FAILED",
            "raw_error": "acceptance failed: [['make', 'verify']]; tsc: not found",
            "retryable": True,
            "exit": "reconfigure",
        }

    def test_an_implementation_code_points_back_at_rework(self) -> None:
        from fleet_graph.dd.control_plane import classify_failure

        for code in ("GATE_REJECTED", "REWORK_LIMIT_REACHED", "REVIEWER_GIT_MUTATION"):
            failure = classify_failure("failed", f"x failed ({code})", code)
            assert failure is not None
            assert (failure["class"], failure["exit"], failure["retryable"]) == (
                "implementation",
                "rework",
                True,
            ), code

    def test_the_fabrication_family_is_final(self) -> None:
        from fleet_graph.dd.control_plane import FABRICATION_CODES, classify_failure

        assert "UNVERIFIED_TEST_CLAIM" in FABRICATION_CODES
        for code in FABRICATION_CODES:
            failure = classify_failure("failed", f"implement failed ({code})", code)
            assert failure is not None
            assert (failure["class"], failure["exit"], failure["retryable"]) == (
                "fabrication",
                "none",
                False,
            ), code

    def test_a_pre_r1c_result_classifies_by_the_code_in_its_reason(self) -> None:
        """Results written before terminal_code existed carry the code only in
        the synthesized reason text; it is recovered, not guessed."""
        from fleet_graph.dd.control_plane import classify_failure

        failure = classify_failure("failed", "implement failed (PROVIDER_UNAVAILABLE)")
        assert failure is not None
        assert failure["code"] == "PROVIDER_UNAVAILABLE"
        assert failure["class"] == "environment_contract"

    def test_complete_and_no_terminal_classify_as_nothing(self) -> None:
        from fleet_graph.dd.control_plane import classify_failure

        assert classify_failure("complete", "merger is the last declared stage") is None
        assert classify_failure("", "") is None

    def test_the_status_carries_the_failure_record_verbatim(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        plane = make_plane(tmp_path)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        write_result(
            plane,
            dev,
            failed_result(
                dev,
                code="ACCEPTANCE_FAILED",
                reason="acceptance failed: [['python3', '-m', 'pytest', '-q']]",
                detail="sqlite3.OperationalError: unable to open database file",
            ),
        )
        status = plane.rebuild_status(dev)
        assert status["failure"]["class"] == "environment_contract"
        assert status["failure"]["code"] == "ACCEPTANCE_FAILED"
        assert (
            "sqlite3.OperationalError: unable to open database file"
            in status["failure"]["raw_error"]
        )
        assert status["failure"]["retryable"] is True
        assert status["generation"] == 1


class TestReconfigure:
    """The environment/contract exit: acceptance context only, callable in
    FAILED and every non-terminal state, refused where it must be."""

    def _failed_env(self, plane: DdControlPlane, scratch: Path) -> str:
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        write_result(
            plane,
            dev,
            failed_result(dev, code="ACCEPTANCE_FAILED", reason="acceptance failed: [['pytest']]"),
        )
        return dev

    def test_it_changes_only_the_acceptance_context(self, scratch: Path, tmp_path: Path) -> None:
        plane = make_plane(tmp_path)
        dev = self._failed_env(plane, scratch)
        before = json.loads((plane.root / dev / RECORD_FILE).read_text())

        report = plane.reconfigure(
            dev,
            acceptance_argv=["python3 -m pytest -q", "sh -c 'echo done'"],
            setup=["python3 -m venv .venv"],
            acceptance_env={"DATABASE_URL": "sqlite:///tmp/x.db"},
        )
        after = json.loads((plane.root / dev / RECORD_FILE).read_text())

        assert report["reconfigured"] is True
        assert after["acceptance_commands"] == [
            ["python3", "-m", "pytest", "-q"],
            ["sh", "-c", "echo done"],
        ]
        assert after["setup_commands"] == [["python3", "-m", "venv", ".venv"]]
        assert after["acceptance_env"] == {"DATABASE_URL": "sqlite:///tmp/x.db"}
        # The frozen identity did not move: same spec, same base, same chain root.
        for frozen in (
            "spec_digest",
            "target_base_commit",
            "bootstrap_commit",
            "root_handoff_digest",
            "development_id",
        ):
            assert after[frozen] == before[frozen], frozen
        assert after["reconfigures"][0]["changed"] == [
            "acceptance_commands",
            "acceptance_env",
            "setup_commands",
        ]

    def test_it_is_callable_before_any_terminal_too(self, scratch: Path, tmp_path: Path) -> None:
        """The legacy engine's 409 pain must not be replaced by a new gate:
        a created (never started) development reconfigures fine."""
        plane = make_plane(tmp_path)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        report = plane.reconfigure(dev, acceptance_env={"CI": "1"})
        assert report["reconfigured"] is True
        assert report["next_start_generation"] == 1, "nothing ran yet; no bump owed"

    def test_a_complete_development_refuses(self, scratch: Path, tmp_path: Path) -> None:
        plane = make_plane(tmp_path)
        dev, _ = _complete_run(plane, scratch)
        with pytest.raises(ControlPlaneError) as refused:
            plane.reconfigure(dev, acceptance_env={"CI": "1"})
        assert refused.value.code == "DEVELOPMENT_COMPLETE"

    def test_a_fabrication_terminal_refuses_and_names_why(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        plane = make_plane(tmp_path)
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        write_result(
            plane,
            dev,
            failed_result(
                dev,
                code="UNVERIFIED_TEST_CLAIM",
                reason="implement failed (UNVERIFIED_TEST_CLAIM)",
                detail="claimed exit 0 for ['pytest'], measured exit 1",
            ),
        )
        with pytest.raises(ControlPlaneError) as refused:
            plane.reconfigure(dev, acceptance_env={"CI": "1"})
        assert refused.value.code == "FABRICATION_FINAL"
        assert "UNVERIFIED_TEST_CLAIM" in refused.value.detail
        assert "measured exit 1" in refused.value.detail

    def test_an_empty_reconfigure_is_refused(self, scratch: Path, tmp_path: Path) -> None:
        plane = make_plane(tmp_path)
        dev = self._failed_env(plane, scratch)
        with pytest.raises(ControlPlaneError) as refused:
            plane.reconfigure(dev)
        assert refused.value.code == "RECONFIGURE_EMPTY"

    def test_unparseable_quoting_is_refused_not_guessed(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        plane = make_plane(tmp_path)
        dev = self._failed_env(plane, scratch)
        with pytest.raises(ControlPlaneError) as refused:
            plane.reconfigure(dev, acceptance_argv=['echo "unclosed'])
        assert refused.value.code == "ACCEPTANCE_DECLARATION_INVALID"
        with pytest.raises(ControlPlaneError) as refused:
            plane.reconfigure(dev, setup=['sh -c "oops'])
        assert refused.value.code == "SETUP_DECLARATION_INVALID"

    def test_a_malformed_env_is_refused(self, scratch: Path, tmp_path: Path) -> None:
        plane = make_plane(tmp_path)
        dev = self._failed_env(plane, scratch)
        with pytest.raises(ControlPlaneError) as refused:
            plane.reconfigure(dev, acceptance_env={"A=B": "x"})
        assert refused.value.code == "ACCEPTANCE_ENV_INVALID"


class TestGenerationRestart:
    """`start` after a retryable terminal (or a reconfigure) launches the next
    generation fresh -- new thread id, new run root, no identity collision --
    while the fabrication terminal stays final."""

    def _failed_dev(self, plane: DdControlPlane, scratch: Path, *, code: str) -> str:
        dev = plane.create(str(scratch), spec_text=SPEC)["development_id"]
        plane.start(dev)  # g1 launched
        (plane.root / dev / CHECKPOINT_FILE).touch()
        write_result(plane, dev, failed_result(dev, code=code, reason=f"implement failed ({code})"))
        return dev

    def test_a_retryable_failure_starts_the_next_generation_fresh(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher=launcher)
        dev = self._failed_dev(plane, scratch, code="ACCEPTANCE_FAILED")

        second = plane.start(dev)
        assert second["generation"] == 2
        assert second["thread_id"] == f"{dev}:g2"
        assert second["mode"] == "fresh"
        argv = launcher.specs[-1].argv()
        assert argv[argv.index("--generation") + 1] == "2"
        assert argv[argv.index("--run-root") + 1] == str(plane.root / dev / "g2")
        assert "--resume" not in argv, "a new generation must not resume the failed thread"
        # The checkpoint file is shared; the generation inside the thread id
        # is what separates the histories.
        assert argv[argv.index("--checkpoint") + 1] == str(plane.root / dev / CHECKPOINT_FILE)
        record = json.loads((plane.root / dev / RECORD_FILE).read_text())
        assert record["generation"] == 2

    def test_a_reconfigure_feeds_the_next_generation_its_new_context(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher=launcher)
        dev = self._failed_dev(plane, scratch, code="ACCEPTANCE_FAILED")
        plane.reconfigure(
            dev,
            acceptance_argv=["pytest -q"],
            setup=["npm ci"],
            acceptance_env={"CI": "1"},
        )
        plane.start(dev)
        import shlex

        argv = launcher.specs[-1].argv()
        assert [argv[i + 1] for i, a in enumerate(argv) if a == "--accept"] == [
            shlex.join(["pytest", "-q"])
        ]
        assert [argv[i + 1] for i, a in enumerate(argv) if a == "--setup"] == [
            shlex.join(["npm", "ci"])
        ]
        assert [argv[i + 1] for i, a in enumerate(argv) if a == "--accept-env"] == ["CI=1"]
        assert argv[argv.index("--generation") + 1] == "2"

    def test_a_killed_second_generation_resumes_itself(self, scratch: Path, tmp_path: Path) -> None:
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher=launcher)
        dev = self._failed_dev(plane, scratch, code="ACCEPTANCE_FAILED")
        plane.start(dev)  # g2 fresh
        third = plane.start(dev)  # killed; no g2 result, checkpoint present
        assert third["generation"] == 2
        assert third["mode"] == "resume"
        argv = launcher.specs[-1].argv()
        assert "--resume" in argv
        assert argv[argv.index("--generation") + 1] == "2"

    def test_a_fabrication_terminal_does_not_restart(self, scratch: Path, tmp_path: Path) -> None:
        plane = make_plane(tmp_path)
        dev = self._failed_dev(plane, scratch, code="UNVERIFIED_TEST_CLAIM")
        with pytest.raises(ControlPlaneError) as refused:
            plane.start(dev)
        assert refused.value.code == "FABRICATION_FINAL"
        assert refused.value.retryable is False
        record = json.loads((plane.root / dev / RECORD_FILE).read_text())
        assert record.get("generation", 1) == 1, "a refused start must not burn a generation"

    def test_a_complete_development_does_not_restart(self, scratch: Path, tmp_path: Path) -> None:
        plane = make_plane(tmp_path)
        dev, _ = _complete_run(plane, scratch)
        with pytest.raises(ControlPlaneError) as refused:
            plane.start(dev)
        assert refused.value.code == "DEVELOPMENT_COMPLETE"


class TestFullChainFailedReconfigureRestart:
    """The R1-c keystone, end to end on the real pipeline with fake agents:
    g1 dies on a missing acceptance environment piece, reconfigure fixes the
    acceptance context, start launches g2, g2 runs to complete through the
    gate -- one development, two generations, one continuous receipt chain,
    and no identity collision anywhere (the wf-5664e5 shape, healed)."""

    def _plugin_stub(self, scratch: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The same stand-in test_dd_runner.plugin_seals builds, bound to this
        repo: plugin sealers write real commits and persist real receipt
        bytes, the prompt resources come from the bundle stand-in."""
        from fleet_graph.dd.prompt import IMPLEMENT_PERSONA, IMPLEMENT_TEMPLATE
        from fleet_graph.dd.vendor import plugin_adapter
        from fleet_graph.graphs.dd_pipeline import StageOutcome
        from test_dd_runner import RealCommitSealer

        sealer = RealCommitSealer(scratch)

        def write_receipt(request: dict[str, Any], name: str, receipt: dict[str, Any]) -> None:
            path = (
                Path(request["state_root"]) / "receipts" / request["dispatch"]["attempt_id"] / name
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")))

        def implement_seal(binding: Any, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            receipt = sealer.seal("implement", StageOutcome())
            write_receipt(request, "implement-receipt.json", receipt)
            return receipt

        def review_seal(binding: Any, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            stage = request["dispatch"]["stage"]
            receipt = {
                **sealer.seal(stage, StageOutcome()),
                "verdict": request["review_result"]["verdict"],
            }
            if stage == "continuous_review":
                write_receipt(request, "continuous-review-receipt.json", receipt)
            return receipt

        class Resource:
            def __init__(self, path: str, text: str) -> None:
                self.relative_path = path
                self.content = text.encode("utf-8")
                self.digest = "sha256:" + "0" * 64

        monkeypatch.setattr(plugin_adapter, "invoke_implement_materializer", implement_seal)
        monkeypatch.setattr(plugin_adapter, "invoke_review_materializer", review_seal)
        monkeypatch.setattr(
            plugin_adapter,
            "load_implement_stage_resources",
            lambda binding, **kwargs: (
                Resource(IMPLEMENT_PERSONA, "You are the Implementer."),
                Resource(
                    IMPLEMENT_TEMPLATE,
                    "input_commit: {{input_commit}}\nacceptance: {{acceptance_commands}}\n",
                ),
            ),
        )

    def _execute(
        self,
        plane: DdControlPlane,
        scratch: Path,
        dev: str,
        *,
        generation: int,
        board: Any = None,
    ) -> dict[str, Any]:
        """What the transient unit does, run in-process: `dd run` with the
        record's derived context and this generation's identity."""
        from fleet_graph.graphs.dd_runner import DevelopmentConfig, run_pipeline
        from test_dd_runner import AgentRunStub

        record = json.loads((plane.root / dev / RECORD_FILE).read_text())
        run_root = plane.root / dev if generation <= 1 else plane.root / dev / f"g{generation}"
        config = DevelopmentConfig(
            development_id=dev,
            workspace_path=scratch,
            state_root=run_root / "state",
            run_root=run_root,
            remote_url=record["remote_url"],
            remote_ref=record["remote_ref"],
            target_base_commit=record["target_base_commit"],
            root_handoff_digest=record["root_handoff_digest"],
            plugin_binding=object(),
            head_commit=git(scratch, "rev-parse", "HEAD").strip(),
            generation=generation,
            checkpoint_path=str(plane.root / dev / CHECKPOINT_FILE),
            run_config={
                "acceptance_commands": [list(c) for c in record["acceptance_commands"]],
                "setup_commands": [list(c) for c in record.get("setup_commands") or []],
                "acceptance_env": dict(record.get("acceptance_env") or {}),
            },
        )
        return run_pipeline(
            config,
            board=board,
            gate_card_entity_id="card-1" if board is not None else "",
            launcher=AgentRunStub({"continuous_review": ["APPROVE"], "final_review": ["APPROVE"]}),
        )

    def test_failed_then_reconfigure_then_g2_to_complete(
        self, scratch: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fleet_graph.bus.board import Decision
        from test_dd_runner import FakeBoard as GateBoard

        self._plugin_stub(scratch, monkeypatch)
        spec = (
            "# SPEC: greet\n\nMake greet() personal.\n\n"
            "```dd-acceptance\ntest -f prepared.txt\n```\n"
        )
        launcher = RecordingLauncher()
        plane = make_plane(tmp_path, launcher=launcher)
        dev = plane.create(str(scratch), spec_text=spec)["development_id"]

        # g1: the acceptance environment piece is missing; the run dies of it.
        first = self._execute(plane, scratch, dev, generation=1)
        assert first["terminal"] == "refused"
        assert first["terminal_code"] == "ACCEPTANCE_FAILED"

        status = plane.rebuild_status(dev)
        assert status["failure"]["class"] == "environment_contract"
        assert status["failure"]["exit"] == "reconfigure"
        assert "acceptance failed" in status["failure"]["raw_error"]

        # The environment/contract exit: fix the acceptance context, nothing else.
        plane.reconfigure(dev, setup=["touch prepared.txt"])
        started = plane.start(dev)
        assert started["generation"] == 2
        assert started["thread_id"] == f"{dev}:g2"

        # g2, as the launched unit would run it: same spec, fixed context.
        board = GateBoard()
        board.decision = Decision(
            message_id="msg-1",
            decision="APPROVE",
            decided_by="青林",
            question="",
            rationale="",
            card_entity_id="card-1",
            raw={},
        )
        second = self._execute(plane, scratch, dev, generation=2, board=board)
        assert second["terminal"] == "complete", second["terminal_reason"]
        assert second["generation"] == 2

        # No identity collision with g1: the gate's bus idempotency key names g2.
        assert list(board.asked) == [f"dd-gate:{dev}:g2"]

        status = plane.rebuild_status(dev)
        assert status["state"] == "complete"
        assert status["generation"] == 2

        # A finished development stays finished.
        with pytest.raises(ControlPlaneError) as refused:
            plane.start(dev)
        assert refused.value.code == "DEVELOPMENT_COMPLETE"

        # Evidence: both generations, one continuous chain.
        payload = plane.evidence(dev)
        entries = payload["evidence"]
        assert [entry["generation"] for entry in entries] == [1, 2]
        g1, g2 = entries

        assert g1["terminal"] == "refused"
        assert g1["failure"]["class"] == "environment_contract"
        assert g1["bootstrap"]["h0"] is not None
        assert g2["terminal"] == "complete"
        assert g2["failure"] is None

        # g2's chain seeds on g1's tail: commit and digest both continuous.
        assert g2["bootstrap"]["seeded_from_generation"] == 1
        assert g2["bootstrap"]["output_commit"] == g1["receipt_chain"][-1]["output_commit"]
        assert g2["bootstrap"]["receipt_digest"] == g1["receipt_chain"][-1]["receipt_digest"]
        assert g2["receipt_chain"][0]["input_commit"] == g1["receipt_chain"][-1]["output_commit"]
        assert (
            g2["receipt_chain"][0]["parent_handoff_receipt_digest"]
            == g1["receipt_chain"][-1]["receipt_digest"]
        )

        # Revisions number cumulatively across generations.
        revisions = [r["revision"] for entry in entries for r in entry["receipt_chain"]]
        assert revisions == list(range(1, len(revisions) + 1))
        assert g2["revision"] == revisions[-1]

        # The audit's chain check holds on the latest entry, so the existing
        # supervision consumer reads the multi-generation shape unchanged.
        from fleet_graph.supervise.audit import AuditReport, _check_receipt_chain

        report = AuditReport(target=dev, kind="development")
        _check_receipt_chain(report, dev, g2)
        by_name = {a.name: a for a in report.assertions}
        assert by_name["receipt_chain_linked"].ok, by_name["receipt_chain_linked"].detail
