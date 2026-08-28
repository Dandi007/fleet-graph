"""B2/B3 wiring: automatic adoption and MCP human recovery on the control plane.

Two review findings are pinned here:

- ``DdControlPlane.adopt`` must be *automatic* (discovery->adopt via
  ``AdoptionLedger.discover``) and idempotent on the file-backed trail: a
  replayed batch appends nothing.
- ``DdControlPlane.recover`` must authenticate against the board's human
  decision and resume only from the recorded decision, with the sealed trail
  resumable from disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conftest import git, head
from fleet_graph.bus.board import Decision
from fleet_graph.dd.adoption import ADOPTION_MECHANISM
from fleet_graph.dd.control_plane import ControlPlaneError, DdControlPlane
from fleet_graph.dd.recovery import RECOVERY_MECHANISM
from fleet_graph.scheduler.launcher import LaunchResult

SPEC = """# SPEC: greet

Make greet() personal.

```dd-acceptance
python3 -m pytest -q
```
"""


class BoardDouble:
    """publish_card for admission plus decide_for for the recovery gate."""

    def __init__(self) -> None:
        self.cards: list[str] = []
        self.decision: Decision | None = None

    def publish_card(self, payload: dict[str, Any], idempotency_key: str) -> Any:
        self.cards.append(idempotency_key)

        class Result:
            entity_id = "ent-dd-card"

        return Result()

    def decision_for(self, ticket: Any) -> Decision | None:
        return self.decision


class Recorder:
    dry_run = False

    def __init__(self, *, started: bool = True) -> None:
        self.specs: list[Any] = []
        self.started = started

    def launch(self, spec: Any) -> LaunchResult:
        self.specs.append(spec)
        return LaunchResult(spec.unit_name, self.started, "recorded")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "greet.py").write_text('def greet():\n    return "hello"\n', encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "seed")
    bare = tmp_path / "origin.git"
    git(work, "init", "-q", "--bare", str(bare))
    git(work, "remote", "add", "origin", str(bare))
    return work


def make_plane(
    tmp_path: Path,
    board: BoardDouble,
    *,
    launcher: Any = None,
    unit_probe: Any = None,
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
        launcher=launcher if launcher is not None else Recorder(),
        unit_probe=unit_probe if unit_probe is not None else (lambda unit: False),
        board_factory=lambda: board,
        clock=lambda: 1_700_000_000.0,
    )


class TestAutomaticAdoption:
    def test_a_batch_adopts_only_the_not_yet_adopted_and_replays_idempotently(
        self, repo: Path, tmp_path: Path
    ) -> None:
        board = BoardDouble()
        plane = make_plane(tmp_path, board)
        dev = plane.create(str(repo), spec_text=SPEC)["development_id"]

        batch = [
            {
                "signature": "dev-1:g1",
                "kind": "in_flight",
                "source": "runner",
                "target_ref": head(repo),
            }
        ]
        first = plane.adopt(dev, batch)
        assert [record["signature"] for record in first["adopted"]] == ["dev-1:g1"]
        assert first["skipped"] == []

        replay = plane.adopt(dev, batch)
        assert replay["adopted"] == []
        assert replay["skipped"] == ["dev-1:g1"]

        # The trail is file-backed: one sealed line, not two, after a replay.
        trail = plane._adoption_path(dev).read_text(encoding="utf-8")
        records = trail.splitlines()
        assert len(records) == 1
        assert records[0]
        assert '"mechanism": "AdoptionLedger.adopt"' in records[0]

        restored = plane.adoptions(dev)["adoptions"]
        assert len(restored) == 1
        assert restored[0]["mechanism"] == ADOPTION_MECHANISM

    def test_discover_filters_already_adopted_work_in_a_mixed_batch(
        self, repo: Path, tmp_path: Path
    ) -> None:
        board = BoardDouble()
        plane = make_plane(tmp_path, board)
        dev = plane.create(str(repo), spec_text=SPEC)["development_id"]
        target = head(repo)

        plane.adopt(
            dev,
            [{"signature": "a", "kind": "in_flight", "source": "", "target_ref": target}],
        )
        mixed = plane.adopt(
            dev,
            [
                {"signature": "a", "kind": "in_flight", "source": "", "target_ref": target},
                {"signature": "b", "kind": "recoverable", "source": "", "target_ref": target},
            ],
        )
        assert [record["signature"] for record in mixed["adopted"]] == ["b"]
        assert mixed["skipped"] == ["a"]

    def test_mcp_adoption_replays_idempotently_over_the_surface(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """B2's idempotent replay, exercised through the registered tool itself
        -- the governed surface, not the control-plane stub."""
        import asyncio
        import json as _json

        from fastmcp import Client

        from fleet_graph.dd.service import build_mcp_server
        from test_dd_service import running_server

        board = BoardDouble()
        plane = make_plane(tmp_path, board)
        dev = plane.create(str(repo), spec_text=SPEC)["development_id"]
        batch = [
            {
                "signature": "dev-1:g1",
                "kind": "in_flight",
                "source": "runner",
                "target_ref": head(repo),
            }
        ]

        async def adopt(url: str) -> list[dict[str, Any]]:
            async with Client(url) as client:
                results: list[dict[str, Any]] = []
                for _ in range(2):
                    result = await client.call_tool(
                        "development_adopt",
                        {"development_id": dev, "discoveries": batch},
                    )
                    data = getattr(result, "structured_content", None) or getattr(
                        result, "data", None
                    )
                    if isinstance(data, dict):
                        results.append(data)
                        continue
                    content = getattr(result, "content", None)
                    if content:
                        results.append(_json.loads(getattr(content[0], "text", None)))
                    else:
                        results.append(result)  # type: ignore[arg-type]
                return results

        with running_server(build_mcp_server(plane)) as url:
            first, replay = asyncio.run(adopt(url))

        assert [record["signature"] for record in first["adopted"]] == ["dev-1:g1"]
        assert replay["adopted"] == []
        assert replay["skipped"] == ["dev-1:g1"]

        trail = plane._adoption_path(dev).read_text(encoding="utf-8")
        assert len(trail.splitlines()) == 1
        assert len(plane.adoptions(dev)["adoptions"]) == 1


class TestMCPHumanRecovery:
    def test_recovery_requires_the_board_decision_and_resumes_only_then(
        self, repo: Path, tmp_path: Path
    ) -> None:
        launcher = Recorder()
        board = BoardDouble()
        plane = make_plane(tmp_path, board, launcher=launcher)
        dev = plane.create(str(repo), spec_text=SPEC)["development_id"]
        target = head(repo)

        # No human decision on the board -> the exit refuses, no launch, no bypass.
        with pytest.raises(ControlPlaneError) as refused:
            plane.recover(dev, target_ref=target, question_note_id="note-1")
        assert refused.value.code == "HUMAN_DECISION_MISSING"
        assert launcher.specs == []

        board.decision = Decision(
            message_id="msg-1",
            decision="resume",
            decided_by="alice",
            question="",
            rationale="",
            card_entity_id="ent-dd-card",
            raw={},
        )
        recovered = plane.recover(dev, target_ref=target, question_note_id="note-1")
        assert recovered["resume"]["resumed"] is True
        assert recovered["resume"]["launched"] is True
        assert recovered["resume"]["thread_id"] == f"{dev}:g1"
        assert recovered["recovery"]["target_ref"] == target
        argv = launcher.specs[-1].argv()
        assert "--resume" in argv

        trail = plane._recoveries_path(dev).read_text(encoding="utf-8")
        assert len(trail.splitlines()) == 1
        restored = plane.recoveries(dev)["recoveries"]
        assert len(restored) == 1
        assert restored[0]["mechanism"] == RECOVERY_MECHANISM
        assert restored[0]["target_ref"] == target

    def test_a_recovery_without_a_decision_text_is_still_authenticated_by_the_board(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """The exit reads the decision off the board; a decision naming no actor
        cannot authenticate, so nothing is recorded."""
        board = BoardDouble()
        plane = make_plane(tmp_path, board)
        dev = plane.create(str(repo), spec_text=SPEC)["development_id"]
        target = head(repo)

        board.decision = Decision(
            message_id="msg-1",
            decision="resume",
            decided_by="",
            question="",
            rationale="",
            card_entity_id="ent-dd-card",
            raw={},
        )
        with pytest.raises(ControlPlaneError) as refused:
            plane.recover(dev, target_ref=target, question_note_id="note-1")
        assert refused.value.code == "RECOVERY_REFUSED"
        assert not plane._recoveries_path(dev).exists()

    def test_suspended_to_resumed_over_the_mcp_surface(self, repo: Path, tmp_path: Path) -> None:
        """B3's mandated suspended-to-resumed MCP human-recovery case: the
        recovery travels through the registered tool, not a stubbed module."""
        import asyncio

        from fastmcp import Client

        from fleet_graph.dd.service import build_mcp_server
        from test_dd_service import running_server

        board = BoardDouble()
        board.decision = Decision(
            message_id="msg-1",
            decision="resume",
            decided_by="alice",
            question="",
            rationale="",
            card_entity_id="ent-dd-card",
            raw={},
        )
        plane = make_plane(tmp_path, board)
        dev = plane.create(str(repo), spec_text=SPEC)["development_id"]
        target = head(repo)

        async def recover(url: str) -> dict[str, Any]:
            async with Client(url) as client:
                result = await client.call_tool(
                    "development_recover",
                    {"development_id": dev, "target_ref": target, "question_note_id": "note-1"},
                )
            data = getattr(result, "structured_content", None) or getattr(result, "data", None)
            if isinstance(data, dict):
                return data
            content = getattr(result, "content", None)
            if content:
                import json as _json

                return _json.loads(getattr(content[0], "text", None))
            return result

        with running_server(build_mcp_server(plane)) as url:
            payload = asyncio.run(recover(url))

        assert payload["resume"]["resumed"] is True
        assert payload["recovery"]["target_ref"] == target
        assert len(plane.recoveries(dev)["recoveries"]) == 1


def _alice_decision() -> Decision:
    return Decision(
        message_id="msg-1",
        decision="resume",
        decided_by="alice",
        question="",
        rationale="",
        card_entity_id="ent-dd-card",
        raw={},
    )


class TestRecoveryReentry:
    """The real recovery entrypoint: `recover` must actually relaunch/re-enter
    the suspended thread, gate the launch on the record, and never fabricate
    success."""

    def test_a_suspended_thread_is_actually_relaunched(self, repo: Path, tmp_path: Path) -> None:
        board = BoardDouble()
        board.decision = _alice_decision()
        launcher = Recorder()
        plane = make_plane(tmp_path, board, launcher=launcher)
        dev = plane.create(str(repo), spec_text=SPEC)["development_id"]
        target = head(repo)

        recovered = plane.recover(dev, target_ref=target, question_note_id="note-1")

        resume = recovered["resume"]
        assert resume["resumed"] is True
        assert resume["launched"] is True
        assert resume["already_running"] is False
        assert resume["thread_id"] == f"{dev}:g1"
        assert resume["generation"] == 1
        assert resume["unit"]
        argv = launcher.specs[-1].argv()
        assert "--resume" in argv
        assert len(plane.recoveries(dev)["recoveries"]) == 1

    def test_missing_record_does_not_launch(self, repo: Path, tmp_path: Path) -> None:
        launcher = Recorder()
        board = BoardDouble()  # no decision on the board -> no record can be sealed
        plane = make_plane(tmp_path, board, launcher=launcher)
        dev = plane.create(str(repo), spec_text=SPEC)["development_id"]

        with pytest.raises(ControlPlaneError) as refused:
            plane.recover(dev, target_ref=head(repo), question_note_id="note-1")
        assert refused.value.code == "HUMAN_DECISION_MISSING"
        assert launcher.specs == []
        assert not plane._recoveries_path(dev).exists()

    def test_duplicate_invocation_is_idempotent_and_does_not_duplicate_the_thread(
        self, repo: Path, tmp_path: Path
    ) -> None:
        board = BoardDouble()
        board.decision = _alice_decision()
        launcher = Recorder()
        active: set[str] = set()
        plane = make_plane(
            tmp_path, board, launcher=launcher, unit_probe=lambda unit: unit in active
        )
        dev = plane.create(str(repo), spec_text=SPEC)["development_id"]
        target = head(repo)

        first = plane.recover(dev, target_ref=target, question_note_id="note-1")
        assert first["resume"]["launched"] is True
        active.add(first["resume"]["unit"])

        second = plane.recover(dev, target_ref=target, question_note_id="note-1")
        assert second["resume"]["already_running"] is True
        assert second["resume"]["launched"] is False

        assert len(launcher.specs) == 1
        assert len(plane.recoveries(dev)["recoveries"]) == 1

    def test_launch_failure_is_surfaced_as_failure_not_resumed(
        self, repo: Path, tmp_path: Path
    ) -> None:
        board = BoardDouble()
        board.decision = _alice_decision()
        launcher = Recorder(started=False)
        plane = make_plane(tmp_path, board, launcher=launcher)
        dev = plane.create(str(repo), spec_text=SPEC)["development_id"]
        target = head(repo)

        recovered = plane.recover(dev, target_ref=target, question_note_id="note-1")
        resume = recovered["resume"]
        assert resume["resumed"] is False
        assert resume["launched"] is False
        assert resume["launch_failure"]


class TestB3EvidenceChainIsBoundToTheTrail:
    def test_the_chain_is_assembled_from_the_real_artifacts(
        self, repo: Path, tmp_path: Path
    ) -> None:
        board = BoardDouble()
        board.decision = Decision(
            message_id="msg-1",
            decision="resume",
            decided_by="alice",
            question="",
            rationale="",
            card_entity_id="ent-dd-card",
            raw={},
        )
        plane = make_plane(tmp_path, board)
        dev = plane.create(str(repo), spec_text=SPEC)["development_id"]
        target = head(repo)

        plane.adopt(
            dev,
            [{"signature": "a", "kind": "in_flight", "source": "", "target_ref": target}],
        )
        plane.recover(dev, target_ref=target, question_note_id="note-1")

        chain = plane.b3_evidence_chain(dev)
        assert chain["valid"] is True, chain["reasons"]
        kinds = [link["kind"] for link in chain["links"]]
        assert "scope" in kinds
        assert "adoption" in kinds
        assert "human_recovery" in kinds

        adoption = next(link for link in chain["links"] if link["kind"] == "adoption")
        recovery = next(link for link in chain["links"] if link["kind"] == "human_recovery")
        assert adoption["evidence_mechanism"] == ADOPTION_MECHANISM
        assert recovery["evidence_mechanism"] == RECOVERY_MECHANISM
        assert adoption["subject_ref"] == target
        assert recovery["subject_ref"] == target

    def test_evidence_returns_the_chain_alongside_the_receipt_entries(
        self, repo: Path, tmp_path: Path
    ) -> None:
        board = BoardDouble()
        plane = make_plane(tmp_path, board)
        dev = plane.create(str(repo), spec_text=SPEC)["development_id"]

        chain = plane.evidence(dev)["b3_evidence_chain"]
        # A freshly-created development has only the scope link -- the adoption
        # and human-recovery links are required and their absence is reported.
        assert chain["valid"] is False
        assert any(link["kind"] == "scope" for link in chain["links"])
        assert "required link missing: adoption" in chain["reasons"]
        assert "required link missing: human_recovery" in chain["reasons"]


class TestScopeQuarantinesTheHandoffPath:
    def test_an_active_crossing_in_a_handoff_body_is_attributed(
        self, repo: Path, tmp_path: Path
    ) -> None:
        board = BoardDouble()
        plane = make_plane(tmp_path, board)
        assert plane._scope_crossings("implement B4 as the next phase")
        assert not plane._scope_crossings("B4 is explicitly deferred")
