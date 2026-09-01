"""E5 goal_enroll: every gate, every refusal code, and the MCP surface.

Pins the fail-closed contract: ``goal_enroll`` admits a goal line only when
every gate passes, and refuses otherwise with exactly one stable machine-
readable code naming the failing clause. There is no partial admission, no
warning-as-admission, no deferred acceptance -- and the admitted roster entry
records the briefing version id so a line is auditable against the briefing
that opened it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from fleet_graph.goal_enroll.briefing import (
    BRIEFING_TEXT,
    GOAL_OPEN_PROMPT_NAME,
    goal_open_prompt_text,
)
from fleet_graph.goal_enroll.contract import (
    BRIEFING_RESOURCE_URI,
    BRIEFING_VERSION,
    CODE_ACCEPTANCE_ARGV_UNEXECUTABLE,
    CODE_ACCEPTANCE_DECLARATION_INVALID,
    CODE_ALIAS_CONFLICT,
    CODE_ALIAS_TOKEN_MISSING,
    CODE_DECISION_REF_REQUIRED,
    CODE_FOLDER_NOT_FOUND,
    CODE_GOLDEN_ORDER_EMPTY,
    CODE_NO_ACCEPTANCE_COMMAND,
    CODE_NOT_A_GOAL_LINE,
    CODE_NOT_PENDING,
    CODE_NOT_SUPERVISOR,
    CODE_SOURCE_UNBOUND,
    CODE_SPEC_LINT_BAN,
    GOAL_ENROLL_MECHANISM,
    LINT_WARNING_PINNED_SHA,
    QUEUE_STATUS_ADMITTED,
    QUEUE_STATUS_PENDING,
    QUEUE_STATUS_REJECTED,
    QUEUE_STATUS_WITHDRAWN,
    U4_CLOSEOUT_DECISION_REF,
    GoalEnrollError,
    GoalRosterEntry,
)
from fleet_graph.goal_enroll.queue import EnrollQueue, migrate_queue_home
from fleet_graph.goal_enroll.roster import RealRosterReader
from fleet_graph.goal_enroll.service import GoalEnrollService
from fleet_graph.goal_enroll.source import governed_goal_folder_store
from fleet_graph.goal_enroll.store import GoalEnrollRoster
from fleet_graph.goal_enroll.validator import (
    GoalEnrollValidator,
    liveness_probe,
    spec_lint,
)

GOAL_MD_OK = """# A goal line

## Acceptance

```dd-acceptance
python3 -c "print('ok')"
```
"""

GOLDEN_ORDER_OK = """# Golden order

The golden order outranks the spec.
"""


def _folder(root: Path, folder_id: str, goal_md: str, golden_order: str) -> Path:
    folder = root / folder_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "goal.md").write_text(goal_md, encoding="utf-8")
    (folder / "golden-order.md").write_text(golden_order, encoding="utf-8")
    return folder


def _source(root: Path) -> Any:
    return governed_goal_folder_store(str(root))


class TestSpecLint:
    def test_a_merge_instruction_to_main_is_a_ban(self) -> None:
        bans, warnings = spec_lint("After the gate, run `git merge origin main`.")
        assert any(ban.clause == "merge_or_push_to_main" for ban in bans)
        assert not warnings

    def test_a_push_instruction_to_main_is_a_ban(self) -> None:
        bans, _ = spec_lint("deliver by `git push origin main`")
        assert any(ban.clause == "merge_or_push_to_main" for ban in bans)

    def test_a_reserved_path_reference_is_a_ban(self) -> None:
        bans, _ = spec_lint("tests must never touch .dev-dispatch/feedback/index.json")
        assert any(ban.clause == "reserved_path:.dev-dispatch" for ban in bans)

    def test_a_dd_evidence_reference_is_a_ban(self) -> None:
        bans, _ = spec_lint("read the result from .dd-evidence/acceptance.json")
        assert any(ban.clause == "reserved_path:.dd-evidence" for ban in bans)

    def test_clean_text_has_no_bans_and_no_warnings(self) -> None:
        assert spec_lint(GOAL_MD_OK) == ((), ())

    def test_a_pinned_sha_in_a_critical_path_table_is_a_warning_not_a_ban(self) -> None:
        sha = "abcdef0123456789abcdef0123456789abcdef01"
        text = f"| step | commit |\n| --- | --- |\n| bootstrap | `{sha}` |"
        bans, warnings = spec_lint(text)
        assert not bans
        assert warnings == (LINT_WARNING_PINNED_SHA,)


class TestLivenessProbe:
    def test_a_missing_command_does_not_start(self) -> None:
        result = liveness_probe(["goal-enroll-no-such-command-xyz"])
        assert result["started"] is False
        assert result["exit_code"] == 127

    def test_a_real_command_starts(self) -> None:
        result = liveness_probe(["python3", "-c", "print('ok')"])
        assert result["started"] is True
        assert result["exit_code"] == 0


class TestValidatorGates:
    def test_an_unbound_source_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(GoalEnrollError) as refused:
            GoalEnrollValidator(None).validate("wf-1")
        assert refused.value.code == CODE_SOURCE_UNBOUND

    def test_a_missing_folder_refuses(self, tmp_path: Path) -> None:
        source = _source(tmp_path)
        with pytest.raises(GoalEnrollError) as refused:
            GoalEnrollValidator(source).validate("wf-missing")
        assert refused.value.code == CODE_FOLDER_NOT_FOUND

    def test_a_folder_without_a_goal_line_layout_refuses(self, tmp_path: Path) -> None:
        folder = tmp_path / "wf-1"
        folder.mkdir()
        (folder / "goal.md").write_text("# only goal\n", encoding="utf-8")
        with pytest.raises(GoalEnrollError) as refused:
            GoalEnrollValidator(_source(tmp_path)).validate("wf-1")
        assert refused.value.code == CODE_NOT_A_GOAL_LINE

    def test_a_goal_without_an_acceptance_command_refuses(self, tmp_path: Path) -> None:
        _folder(tmp_path, "wf-1", "# no acceptance\n", GOLDEN_ORDER_OK)
        with pytest.raises(GoalEnrollError) as refused:
            GoalEnrollValidator(_source(tmp_path)).validate("wf-1")
        assert refused.value.code == CODE_NO_ACCEPTANCE_COMMAND

    def test_a_malformed_acceptance_declaration_refuses(self, tmp_path: Path) -> None:
        _folder(
            tmp_path,
            "wf-1",
            '```dd-acceptance\necho "unclosed\n```\n',
            GOLDEN_ORDER_OK,
        )
        with pytest.raises(GoalEnrollError) as refused:
            GoalEnrollValidator(_source(tmp_path)).validate("wf-1")
        assert refused.value.code == CODE_ACCEPTANCE_DECLARATION_INVALID

    def test_an_empty_golden_order_refuses(self, tmp_path: Path) -> None:
        _folder(tmp_path, "wf-1", GOAL_MD_OK, "   \n")
        with pytest.raises(GoalEnrollError) as refused:
            GoalEnrollValidator(_source(tmp_path)).validate("wf-1")
        assert refused.value.code == CODE_GOLDEN_ORDER_EMPTY

    def test_a_spec_lint_ban_refuses_admission(self, tmp_path: Path) -> None:
        _folder(
            tmp_path,
            "wf-1",
            GOAL_MD_OK + "\n## Delivery\nRun `git push origin main` after acceptance.\n",
            GOLDEN_ORDER_OK,
        )
        with pytest.raises(GoalEnrollError) as refused:
            GoalEnrollValidator(_source(tmp_path)).validate("wf-1")
        assert refused.value.code == CODE_SPEC_LINT_BAN
        assert "merge_or_push_to_main" in refused.value.detail

    def test_an_unexecutable_acceptance_argv_refuses(self, tmp_path: Path) -> None:
        _folder(
            tmp_path,
            "wf-1",
            "```dd-acceptance\ngoal-enroll-no-such-command-xyz\n```\n",
            GOLDEN_ORDER_OK,
        )
        with pytest.raises(GoalEnrollError) as refused:
            GoalEnrollValidator(_source(tmp_path)).validate("wf-1")
        assert refused.value.code == CODE_ACCEPTANCE_ARGV_UNEXECUTABLE

    def test_a_valid_goal_admits_with_the_briefing_version(self, tmp_path: Path) -> None:
        _folder(tmp_path, "wf-1", GOAL_MD_OK, GOLDEN_ORDER_OK)
        facts = GoalEnrollValidator(_source(tmp_path)).validate("wf-1")
        assert facts["briefing_version"] == BRIEFING_VERSION
        assert facts["acceptance_argv"] == (("python3", "-c", "print('ok')"),)
        assert facts["liveness"][0]["started"] is True
        assert facts["mechanism"] == GOAL_ENROLL_MECHANISM

    def test_a_pinned_sha_warning_is_recorded_not_refused(self, tmp_path: Path) -> None:
        sha = "abcdef0123456789abcdef0123456789abcdef01"
        goal = GOAL_MD_OK + f"\n| step | commit |\n| --- | --- |\n| bootstrap | `{sha}` |\n"
        _folder(tmp_path, "wf-1", goal, GOLDEN_ORDER_OK)
        facts = GoalEnrollValidator(_source(tmp_path)).validate("wf-1")
        assert facts["lint_warnings"] == (LINT_WARNING_PINNED_SHA,)


class TestRoster:
    def test_admission_is_idempotent_per_folder(self, tmp_path: Path) -> None:
        roster = GoalEnrollRoster(str(tmp_path / "store"))
        entry = GoalRosterEntry(
            folder_id="wf-1",
            briefing_version=BRIEFING_VERSION,
            acceptance_argv=(("python3", "-c", "print('ok')"),),
            liveness=(),
            lint_warnings=(),
            mechanism=GOAL_ENROLL_MECHANISM,
            admitted_at="2026-08-29T00:00:00Z",
        )
        first = roster.admit(entry)
        second = roster.admit(entry)
        assert first["already_admitted"] is False
        assert second["already_admitted"] is True
        assert len(roster) == 1
        assert roster.get("wf-1")["briefing_version"] == BRIEFING_VERSION


class TestEnrollQueueStateMachine:
    """queue 状态机：pending -> admitted | rejected | withdrawn，终态带
    decided_by/decision_ref；withdraw 留痕不删行（失败留痕原则）。"""

    def test_a_submission_lands_pending_and_is_idempotent(self, tmp_path: Path) -> None:
        queue = EnrollQueue(str(tmp_path / "queue"))
        first = queue.submit(
            {
                "folder_id": "wf-1",
                "alias": "ronin-drill",
                "seat_hint": "opencode-gpt-sol",
                "max_rounds": 9999,
                "briefing_version": BRIEFING_VERSION,
                "submitted_by": "drill",
                "submitted_at": "2026-08-31T00:00:00Z",
            }
        )
        second = queue.submit(
            {
                "folder_id": "wf-1",
                "alias": "ronin-drill",
                "seat_hint": "opencode-gpt-sol",
                "max_rounds": 9999,
                "briefing_version": BRIEFING_VERSION,
                "submitted_by": "drill",
                "submitted_at": "2026-08-31T00:00:00Z",
            }
        )
        assert first["status"] == QUEUE_STATUS_PENDING
        assert first["already_pending"] is False
        assert second["already_pending"] is True
        assert len(queue) == 1

    def test_withdraw_only_moves_a_pending_entry_and_keeps_the_row(self, tmp_path: Path) -> None:
        queue = EnrollQueue(str(tmp_path / "queue"))
        queue.submit(
            {
                "folder_id": "wf-1",
                "alias": "ronin-drill",
                "briefing_version": BRIEFING_VERSION,
                "submitted_by": "drill",
                "submitted_at": "2026-08-31T00:00:00Z",
            }
        )
        withdrawn = queue.withdraw("wf-1", by="drill")
        assert withdrawn["status"] == QUEUE_STATUS_WITHDRAWN
        assert queue.get("wf-1")["status"] == QUEUE_STATUS_WITHDRAWN  # 留痕不删行
        with pytest.raises(GoalEnrollError) as refused:
            queue.withdraw("wf-1", by="drill")
        assert refused.value.code == CODE_NOT_PENDING

    def test_admit_and_reject_carry_a_decision_pointer(self, tmp_path: Path) -> None:
        queue = EnrollQueue(str(tmp_path / "queue"))
        for folder in ("wf-a", "wf-b"):
            queue.submit(
                {
                    "folder_id": folder,
                    "alias": f"ronin-{folder}",
                    "briefing_version": BRIEFING_VERSION,
                    "submitted_by": "drill",
                    "submitted_at": "2026-08-31T00:00:00Z",
                }
            )
        admitted = queue.mark_admitted("wf-a", decided_by="supervisor", decision_ref="ref-1")
        rejected = queue.mark_rejected("wf-b", decided_by="supervisor", decision_ref="ref-2")
        assert admitted["status"] == QUEUE_STATUS_ADMITTED
        assert admitted["decided_by"] == "supervisor"
        assert admitted["decision_ref"] == "ref-1"
        assert rejected["status"] == QUEUE_STATUS_REJECTED
        assert rejected["decision_ref"] == "ref-2"

    def test_terminal_transitions_require_a_pending_entry(self, tmp_path: Path) -> None:
        queue = EnrollQueue(str(tmp_path / "queue"))
        with pytest.raises(GoalEnrollError) as refused:
            queue.mark_admitted("wf-absent", decided_by="x", decision_ref="r")
        assert refused.value.code == CODE_NOT_PENDING

    def test_rejections_are_recorded_for_the_rejection_history(self, tmp_path: Path) -> None:
        queue = EnrollQueue(str(tmp_path / "queue"))
        queue.record_rejection("wf-1", code=CODE_ALIAS_TOKEN_MISSING, detail="no token")
        queue.record_rejection("wf-1", code=CODE_ALIAS_CONFLICT, detail="claimed")
        assert [r["code"] for r in queue.rejections("wf-1")] == [
            CODE_ALIAS_TOKEN_MISSING,
            CODE_ALIAS_CONFLICT,
        ]

    def test_the_queue_survives_a_reload(self, tmp_path: Path) -> None:
        root = str(tmp_path / "queue")
        EnrollQueue(root).submit(
            {
                "folder_id": "wf-1",
                "alias": "ronin-drill",
                "briefing_version": BRIEFING_VERSION,
                "submitted_by": "drill",
                "submitted_at": "2026-08-31T00:00:00Z",
            }
        )
        reloaded = EnrollQueue(root)
        assert reloaded.get("wf-1")["status"] == QUEUE_STATUS_PENDING
        assert len(reloaded) == 1


class TestRealRosterReader:
    def _roster(self, tmp_path: Path) -> RealRosterReader:
        config = tmp_path / "ronin-lines.json"
        config.write_text(
            json.dumps(
                {
                    "run_root": "/data/fleet-graph/runs",
                    "lines": [
                        {
                            "folder_id": "wf-a",
                            "seat": "opencode-dsv4pro",
                            "alias": "ronin-a",
                            "max_rounds": 9999,
                            "enabled": True,
                        },
                        {
                            "folder_id": "wf-b",
                            "seat": "opencode-gpt-sol",
                            "alias": "ronin-b",
                            "max_rounds": 1,
                            "enabled": False,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        return RealRosterReader(config)

    def test_it_reads_the_real_roster_lines(self, tmp_path: Path) -> None:
        reader = self._roster(tmp_path)
        assert {e["folder_id"] for e in reader.entries()} == {"wf-a", "wf-b"}
        assert reader.get("wf-a")["alias"] == "ronin-a"
        assert reader.has("wf-a") is True
        assert reader.has("wf-nope") is False
        assert reader.aliases() == {"ronin-a", "ronin-b"}

    def test_a_missing_roster_degrades_to_empty(self, tmp_path: Path) -> None:
        reader = RealRosterReader(tmp_path / "absent.json")
        assert reader.entries() == ()
        assert reader.aliases() == set()
        assert reader.has("wf-a") is False


class TestValidatorGates6And7:
    def test_a_missing_alias_token_refuses(self, tmp_path: Path) -> None:
        _folder(tmp_path, "wf-1", GOAL_MD_OK, GOLDEN_ORDER_OK)
        validator = GoalEnrollValidator(_source(tmp_path), alias_token_check=lambda alias: False)
        with pytest.raises(GoalEnrollError) as refused:
            validator.validate("wf-1", alias="ronin-nope")
        assert refused.value.code == CODE_ALIAS_TOKEN_MISSING

    def test_a_claimed_alias_refuses(self, tmp_path: Path) -> None:
        _folder(tmp_path, "wf-1", GOAL_MD_OK, GOLDEN_ORDER_OK)
        validator = GoalEnrollValidator(
            _source(tmp_path),
            alias_token_check=lambda alias: True,
            alias_conflict_check=lambda alias: "wf-a" if alias == "ronin-taken" else None,
        )
        with pytest.raises(GoalEnrollError) as refused:
            validator.validate("wf-1", alias="ronin-taken")
        assert refused.value.code == CODE_ALIAS_CONFLICT
        assert "wf-a" in refused.value.detail

    def test_a_free_alias_with_a_token_passes_gates_6_and_7(self, tmp_path: Path) -> None:
        _folder(tmp_path, "wf-1", GOAL_MD_OK, GOLDEN_ORDER_OK)
        validator = GoalEnrollValidator(
            _source(tmp_path),
            alias_token_check=lambda alias: True,
            alias_conflict_check=lambda alias: None,
        )
        facts = validator.validate("wf-1", alias="ronin-fresh", seat_hint="opencode-gpt-sol")
        assert facts["alias"] == "ronin-fresh"
        assert facts["seat_hint"] == "opencode-gpt-sol"
        assert facts["briefing_version"] == BRIEFING_VERSION

    def test_a_supervision_plane_token_is_not_owned(self, tmp_path: Path) -> None:
        """Gate 6 ownership: a token whose realpath resolves into the
        supervision plane (control-plane credential root) is not owned."""
        from fleet_graph.bus.tokens import resolve_line_token_ownership

        secrets = tmp_path / "secrets"
        supervision = tmp_path / "supervision"
        secrets.mkdir()
        supervision.mkdir()
        (supervision / "supervisor.token").write_text("control", encoding="utf-8")
        (secrets / "ronin-sup.token").symlink_to(supervision / "supervisor.token")
        ownership = resolve_line_token_ownership(
            "ronin-sup",
            template=str(secrets / "{alias}.token"),
            secrets_root=secrets,
            supervision_roots=(supervision,),
        )
        assert not ownership.owned
        assert ownership.status == "supervision_plane"

    def test_an_other_line_token_is_not_owned(self, tmp_path: Path) -> None:
        """Gate 6 ownership: a token whose realpath is another line's token
        (a different basename) is not owned."""
        from fleet_graph.bus.tokens import resolve_line_token_ownership

        secrets = tmp_path / "secrets"
        secrets.mkdir()
        (secrets / "ronin-other.token").write_text("other", encoding="utf-8")
        (secrets / "ronin-x.token").symlink_to(secrets / "ronin-other.token")
        ownership = resolve_line_token_ownership(
            "ronin-x",
            template=str(secrets / "{alias}.token"),
            secrets_root=secrets,
        )
        assert not ownership.owned
        assert ownership.status == "other_line"

    def test_a_symlink_alias_token_is_not_owned(self, tmp_path: Path) -> None:
        """Gate 6 ownership: a symlink masquerading as the line's own token is
        not owned even when it resolves within the secrets boundary."""
        from fleet_graph.bus.tokens import resolve_line_token_ownership

        secrets = tmp_path / "secrets"
        secrets.mkdir()
        (secrets / "real").mkdir()
        (secrets / "real" / "ronin-link.token").write_text("real", encoding="utf-8")
        (secrets / "ronin-link.token").symlink_to(secrets / "real" / "ronin-link.token")
        ownership = resolve_line_token_ownership(
            "ronin-link",
            template=str(secrets / "{alias}.token"),
            secrets_root=secrets,
        )
        assert not ownership.owned
        assert ownership.status == "symlink_alias"

    def test_a_missing_token_is_not_owned(self, tmp_path: Path) -> None:
        from fleet_graph.bus.tokens import resolve_line_token_ownership

        secrets = tmp_path / "secrets"
        secrets.mkdir()
        ownership = resolve_line_token_ownership(
            "ronin-nope",
            template=str(secrets / "{alias}.token"),
            secrets_root=secrets,
        )
        assert not ownership.owned
        assert ownership.status == "missing"

    def test_a_regular_owned_token_is_owned(self, tmp_path: Path) -> None:
        """Gate 6 ownership: a plain regular file at the canonical path is
        genuinely owned (positive case)."""
        from fleet_graph.bus.tokens import resolve_line_token_ownership

        secrets = tmp_path / "secrets"
        secrets.mkdir()
        (secrets / "ronin-owned.token").write_text("owned", encoding="utf-8")
        ownership = resolve_line_token_ownership(
            "ronin-owned",
            template=str(secrets / "{alias}.token"),
            secrets_root=secrets,
        )
        assert ownership.owned
        assert ownership.status == "owned"


class TestQueueHomeIsolation:
    """U4 defect 1: goal serve's queue lives in an independent queue home
    (default /data/fleet-graph/goal/), never inside the work-folder-root."""

    def test_default_goal_queue_home_is_the_fleet_goal_dir(self) -> None:
        from fleet_graph.goal.service import DEFAULT_GOAL_QUEUE_HOME, GOAL_QUEUE_HOME_ENV

        assert DEFAULT_GOAL_QUEUE_HOME == "/data/fleet-graph/goal"
        assert GOAL_QUEUE_HOME_ENV == "FLEET_GRAPH_GOAL_QUEUE_HOME"

    def test_migrate_queue_home_moves_legacy_files_out_of_work_records(
        self, tmp_path: Path
    ) -> None:
        """Legacy queue files under the work-folder-root are relocated into the
        goal queue home, deterministically and idempotently."""
        legacy = tmp_path / "work-records"
        home = tmp_path / "goal"
        legacy.mkdir(parents=True)
        (legacy / "enroll-queue.jsonl").write_text("legacy-queue\n", encoding="utf-8")
        (legacy / "enroll-rejections.jsonl").write_text("legacy-rej\n", encoding="utf-8")

        assert migrate_queue_home(legacy, home) == (
            "enroll-queue.jsonl",
            "enroll-rejections.jsonl",
        )
        assert (home / "enroll-queue.jsonl").read_text(encoding="utf-8") == "legacy-queue\n"
        assert (home / "enroll-rejections.jsonl").read_text(encoding="utf-8") == "legacy-rej\n"
        assert not (legacy / "enroll-queue.jsonl").exists()
        # Idempotent: re-running is a no-op.
        assert migrate_queue_home(legacy, home) == ()

    def test_migrate_queue_home_never_overwrites_existing_queue_files(self, tmp_path: Path) -> None:
        """When both locations hold a file, the queue home is authoritative:
        the legacy copy is left untouched (retain data, never overwrite)."""
        legacy = tmp_path / "work-records"
        home = tmp_path / "goal"
        legacy.mkdir(parents=True)
        home.mkdir(parents=True)
        (legacy / "enroll-queue.jsonl").write_text("legacy\n", encoding="utf-8")
        (home / "enroll-queue.jsonl").write_text("authoritative\n", encoding="utf-8")
        assert migrate_queue_home(legacy, home) == ()
        assert (home / "enroll-queue.jsonl").read_text(encoding="utf-8") == "authoritative\n"

    def test_goal_serve_refuses_to_start_without_a_queue_migration_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """serve() keeps its fail-fast root binding; the queue home is separate
        but never replaces the required work-folder-root."""
        from fleet_graph.goal.service import serve

        monkeypatch.delenv("FLEET_GRAPH_WORK_FOLDER_ROOT", raising=False)
        with pytest.raises(RuntimeError, match="GOAL_ENROLL_SOURCE_UNBOUND"):
            serve(host="127.0.0.1", port=0, work_folder_root=None, goal_queue_home="/tmp/q")


class TestServiceAndMCP:
    def test_submit_lands_a_pending_application_without_touching_the_roster(
        self, tmp_path: Path
    ) -> None:
        _folder(tmp_path, "wf-1", GOAL_MD_OK, GOLDEN_ORDER_OK)
        queue = EnrollQueue(str(tmp_path / "queue"))
        service = GoalEnrollService(
            GoalEnrollValidator(_source(tmp_path), alias_token_check=lambda alias: True),
            queue=queue,
            roster=RealRosterReader(tmp_path / "absent.json"),
        )
        submitted = service.submit(
            "wf-1", "ronin-fresh", seat_hint="opencode-gpt-sol", max_rounds=9999
        )
        assert submitted["status"] == QUEUE_STATUS_PENDING
        assert submitted["already_pending"] is False
        assert submitted["briefing_version"] == BRIEFING_VERSION
        assert submitted["mechanism"] == GOAL_ENROLL_MECHANISM
        assert submitted["board_notify"].startswith("failed:")  # no board bound
        assert queue.get("wf-1")["status"] == QUEUE_STATUS_PENDING

    def test_a_repeated_pending_submit_answers_already_pending(self, tmp_path: Path) -> None:
        _folder(tmp_path, "wf-1", GOAL_MD_OK, GOLDEN_ORDER_OK)
        queue = EnrollQueue(str(tmp_path / "queue"))
        service = GoalEnrollService(
            GoalEnrollValidator(_source(tmp_path), alias_token_check=lambda alias: True),
            queue=queue,
            roster=RealRosterReader(tmp_path / "absent.json"),
        )
        service.submit("wf-1", "ronin-fresh")
        again = service.submit("wf-1", "ronin-fresh")
        assert again["already_pending"] is True

    def test_a_folder_already_in_the_real_roster_answers_already_enrolled(
        self, tmp_path: Path
    ) -> None:
        _folder(tmp_path, "wf-1", GOAL_MD_OK, GOLDEN_ORDER_OK)
        config = tmp_path / "ronin-lines.json"
        config.write_text(
            json.dumps(
                {
                    "lines": [
                        {
                            "folder_id": "wf-1",
                            "seat": "opencode-dsv4pro",
                            "alias": "ronin-a",
                            "enabled": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        queue = EnrollQueue(str(tmp_path / "queue"))
        service = GoalEnrollService(
            GoalEnrollValidator(_source(tmp_path), alias_token_check=lambda alias: True),
            queue=queue,
            roster=RealRosterReader(config),
        )
        result = service.submit("wf-1", "ronin-a")
        assert result["already_enrolled"] is True
        assert len(queue) == 0  # nothing new queued

    def test_goal_withdraw_only_moves_a_pending_entry(self, tmp_path: Path) -> None:
        _folder(tmp_path, "wf-1", GOAL_MD_OK, GOLDEN_ORDER_OK)
        queue = EnrollQueue(str(tmp_path / "queue"))
        service = GoalEnrollService(
            GoalEnrollValidator(_source(tmp_path), alias_token_check=lambda alias: True),
            queue=queue,
            roster=RealRosterReader(tmp_path / "absent.json"),
        )
        service.submit("wf-1", "ronin-fresh")
        withdrawn = service.withdraw("wf-1", by="drill")
        assert withdrawn["status"] == QUEUE_STATUS_WITHDRAWN
        with pytest.raises(GoalEnrollError) as refused:
            service.withdraw("wf-1", by="drill")
        assert refused.value.code == CODE_NOT_PENDING

    def test_goal_status_returns_the_application_and_its_rejection_history(
        self, tmp_path: Path
    ) -> None:
        _folder(tmp_path, "wf-1", GOAL_MD_OK, GOLDEN_ORDER_OK)
        queue = EnrollQueue(str(tmp_path / "queue"))
        service = GoalEnrollService(
            GoalEnrollValidator(_source(tmp_path), alias_token_check=lambda alias: True),
            queue=queue,
            roster=RealRosterReader(tmp_path / "absent.json"),
        )
        service.submit("wf-1", "ronin-fresh")
        detail = service.status("wf-1")
        assert detail["queue"]["folder_id"] == "wf-1"
        assert detail["queue"]["status"] == QUEUE_STATUS_PENDING
        assert detail["roster"] is None
        assert detail["rejections"] == []

    def test_goal_list_merges_roster_and_queue_with_origins(self, tmp_path: Path) -> None:
        _folder(tmp_path, "wf-1", GOAL_MD_OK, GOLDEN_ORDER_OK)
        config = tmp_path / "ronin-lines.json"
        config.write_text(
            json.dumps(
                {
                    "lines": [
                        {
                            "folder_id": "wf-a",
                            "seat": "opencode-dsv4pro",
                            "alias": "ronin-a",
                            "enabled": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        queue = EnrollQueue(str(tmp_path / "queue"))
        service = GoalEnrollService(
            GoalEnrollValidator(_source(tmp_path), alias_token_check=lambda alias: True),
            queue=queue,
            roster=RealRosterReader(config),
        )
        service.submit("wf-1", "ronin-fresh")
        view = service.list_all()
        origins = {entry["folder_id"]: entry["origin"] for entry in view["entries"]}
        assert origins["wf-a"] == "roster"
        assert origins["wf-1"] == "pending"

    def test_goal_list_marks_the_two_reconciliation_drifts(self, tmp_path: Path) -> None:
        config = tmp_path / "ronin-lines.json"
        config.write_text(
            json.dumps(
                {
                    "lines": [
                        {
                            "folder_id": "wf-a",
                            "seat": "opencode-dsv4pro",
                            "alias": "ronin-a",
                            "enabled": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        queue = EnrollQueue(str(tmp_path / "queue"))
        # Drift 1: queue admitted but the roster has no such line.
        queue.submit(
            {
                "folder_id": "wf-admitted",
                "alias": "ronin-admitted",
                "briefing_version": BRIEFING_VERSION,
                "submitted_by": "drill",
                "submitted_at": "2026-08-31T00:00:00Z",
            }
        )
        queue.mark_admitted("wf-admitted", decided_by="supervisor", decision_ref="ref-1")
        # Drift 2: the roster has the line but the queue still marks pending.
        queue.submit(
            {
                "folder_id": "wf-a",
                "alias": "ronin-a",
                "briefing_version": BRIEFING_VERSION,
                "submitted_by": "drill",
                "submitted_at": "2026-08-31T00:00:00Z",
            }
        )
        service = GoalEnrollService(
            GoalEnrollValidator(None), queue=queue, roster=RealRosterReader(config)
        )
        by_folder = {entry["folder_id"]: entry for entry in service.list_all()["entries"]}
        assert by_folder["wf-admitted"]["drift"] == "admitted_missing_from_roster"
        assert by_folder["wf-a"]["drift"] == "roster_but_pending"

    def test_the_goal_open_prompt_and_briefing_are_versioned(self) -> None:
        text = goal_open_prompt_text()
        assert GOAL_OPEN_PROMPT_NAME in text
        assert BRIEFING_VERSION in text
        assert BRIEFING_VERSION in BRIEFING_TEXT
        # The briefing carries the recorded constraints verbatim.
        for constraint in ("never merges to main directly", "dd-evidence", ".dev-dispatch"):
            assert constraint in BRIEFING_TEXT

    def test_the_tool_family_is_registered_on_the_goal_mcp_surface(self) -> None:
        from fleet_graph.goal.service import build_goal_mcp_server

        server = build_goal_mcp_server()
        tools = asyncio.run(server.list_tools())
        names = {tool.name for tool in tools}
        assert {
            "goal_enroll",
            "goal_list",
            "goal_status",
            "goal_withdraw",
            "goal_admit",
        } <= names
        prompts = asyncio.run(server.list_prompts())
        assert GOAL_OPEN_PROMPT_NAME in {prompt.name for prompt in prompts}
        resources = asyncio.run(server.list_resources())
        assert str(BRIEFING_RESOURCE_URI) in {str(res.uri) for res in resources}

    def test_the_goal_admit_tool_lists_its_required_arguments(self) -> None:
        """U4: tools/list exposes the admit capability with its required args."""
        from fleet_graph.goal.service import build_goal_mcp_server

        server = build_goal_mcp_server()
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        admit = tools["goal_admit"]
        params = set(admit.parameters["properties"])
        assert {"folder_id", "decision_ref", "decided_by"} <= params
        required = set(admit.parameters.get("required") or params)
        assert {"folder_id", "decision_ref", "decided_by"} <= required

    def test_the_goal_surface_is_not_on_the_dd_face(self) -> None:
        """The goal-driven split: dd carries no goal_enroll / goal-open /
        briefing; those live on the standalone goal serve surface (:5611)."""
        from fleet_graph.dd.service import build_mcp_server
        from test_dd_service import FakeControlPlane

        server = build_mcp_server(FakeControlPlane())
        tools = asyncio.run(server.list_tools())
        assert "goal_enroll" not in {tool.name for tool in tools}
        assert "goal_list" not in {tool.name for tool in tools}
        prompts = asyncio.run(server.list_prompts())
        assert GOAL_OPEN_PROMPT_NAME not in {prompt.name for prompt in prompts}
        resources = asyncio.run(server.list_resources())
        assert str(BRIEFING_RESOURCE_URI) not in {str(res.uri) for res in resources}

    def test_the_refusal_reaches_the_client_machine_readably(self, tmp_path: Path) -> None:
        from fastmcp import Client
        from fastmcp.exceptions import ToolError

        from fleet_graph.goal.service import build_goal_mcp_server
        from test_dd_service import running_server

        _folder(tmp_path, "wf-1", "# no acceptance\n", GOLDEN_ORDER_OK)
        source = _source(tmp_path)
        server = build_goal_mcp_server(goal_folders=source)

        async def call(url: str) -> str:
            async with Client(url) as client:
                with pytest.raises(ToolError) as excinfo:
                    await client.call_tool("goal_enroll", {"folder_id": "wf-1", "alias": "ronin-x"})
                return str(excinfo.value)

        with running_server(server) as url:
            message = asyncio.run(call(url))

        payload = json.loads(message[message.index("{") : message.rindex("}") + 1])
        assert payload["code"] == CODE_NO_ACCEPTANCE_COMMAND
        assert payload["tool"] == "goal_enroll"

    def test_an_unbound_server_refuses_goal_enroll_explicitly(self) -> None:
        from fastmcp import Client
        from fastmcp.exceptions import ToolError

        from fleet_graph.goal.service import build_goal_mcp_server
        from test_dd_service import running_server

        server = build_goal_mcp_server(goal_folders=None)

        async def call(url: str) -> str:
            async with Client(url) as client:
                with pytest.raises(ToolError) as excinfo:
                    await client.call_tool("goal_enroll", {"folder_id": "wf-1", "alias": "ronin-x"})
                return str(excinfo.value)

        with running_server(server) as url:
            message = asyncio.run(call(url))

        payload = json.loads(message[message.index("{") : message.rindex("}") + 1])
        assert payload["code"] == CODE_SOURCE_UNBOUND

    def test_goal_serve_refuses_to_start_without_a_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Binding fail-fast: no --work-folder-root and no env root means the
        goal service refuses to start -- a runtime GOAL_ENROLL_SOURCE_UNBOUND
        half-broken service is not allowed to exist.
        """
        from fleet_graph.goal.service import serve

        monkeypatch.delenv("FLEET_GRAPH_WORK_FOLDER_ROOT", raising=False)
        with pytest.raises(RuntimeError, match="GOAL_ENROLL_SOURCE_UNBOUND"):
            serve(host="127.0.0.1", port=0, work_folder_root=None)

    def test_a_valid_submission_over_the_wire(self, tmp_path: Path) -> None:
        from fastmcp import Client

        from fleet_graph.goal.service import build_goal_mcp_server
        from test_dd_service import running_server

        _folder(tmp_path, "wf-1", GOAL_MD_OK, GOLDEN_ORDER_OK)
        source = _source(tmp_path)
        queue = EnrollQueue(str(tmp_path / "queue"))
        server = build_goal_mcp_server(
            goal_folders=source,
            goal_queue=queue,
            real_roster=RealRosterReader(tmp_path / "absent.json"),
            board=None,
            alias_token_check=lambda alias: True,
        )

        async def call(url: str) -> dict[str, Any]:
            async with Client(url) as client:
                result = await client.call_tool(
                    "goal_enroll",
                    {"folder_id": "wf-1", "alias": "ronin-fresh", "seat_hint": "opencode-gpt-sol"},
                )
                return _payload(result)

        with running_server(server) as url:
            submitted = asyncio.run(call(url))

        assert submitted["already_pending"] is False
        assert submitted["status"] == QUEUE_STATUS_PENDING
        assert submitted["briefing_version"] == BRIEFING_VERSION
        assert submitted["acceptance_argv"] == [["python3", "-c", "print('ok')"]]

    def test_alias_token_missing_refusal_reaches_the_client(self, tmp_path: Path) -> None:
        from fastmcp import Client
        from fastmcp.exceptions import ToolError

        from fleet_graph.goal.service import build_goal_mcp_server
        from test_dd_service import running_server

        _folder(tmp_path, "wf-1", GOAL_MD_OK, GOLDEN_ORDER_OK)
        source = _source(tmp_path)
        server = build_goal_mcp_server(
            goal_folders=source,
            goal_queue=EnrollQueue(str(tmp_path / "queue")),
            real_roster=RealRosterReader(tmp_path / "absent.json"),
        )

        async def call(url: str) -> str:
            async with Client(url) as client:
                with pytest.raises(ToolError) as excinfo:
                    await client.call_tool(
                        "goal_enroll", {"folder_id": "wf-1", "alias": "ronin-no-token"}
                    )
                return str(excinfo.value)

        with running_server(server) as url:
            message = asyncio.run(call(url))

        payload = json.loads(message[message.index("{") : message.rindex("}") + 1])
        assert payload["code"] == CODE_ALIAS_TOKEN_MISSING


def _payload(result: Any) -> dict[str, Any]:
    data = getattr(result, "structured_content", None) or getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    content = getattr(result, "content", None)
    if content:
        for item in content:
            text = getattr(item, "text", None)
            if text:
                try:
                    return json.loads(text)
                except ValueError:
                    continue
    return {}


class TestGoalAdmitSupervisorSurface:
    """U4 closeout: the supervisor admission edge (pending -> admitted).

    Pins the supervisor-only release path: ``goal_admit`` is the one MCP tool
    that marks a *pending* application ``admitted`` with the supervisor release
    verdict's ``decision_ref`` (reusing the queue's ``mark_admitted`` -- no
    state-machine rewrite), refuses every non-supervisor identity, and is
    idempotent for the already-admitted-same-decision case without duplicating
    or destructively rewriting history.
    """

    def _service(
        self, tmp_path: Path, *, supervisor_check: Any | None = None
    ) -> tuple[GoalEnrollService, EnrollQueue, Path]:
        _folder(tmp_path, "wf-1", GOAL_MD_OK, GOLDEN_ORDER_OK)
        queue = EnrollQueue(str(tmp_path / "queue"))
        service = GoalEnrollService(
            GoalEnrollValidator(_source(tmp_path), alias_token_check=lambda alias: True),
            queue=queue,
            roster=RealRosterReader(tmp_path / "absent.json"),
            supervisor_identity_check=supervisor_check
            or (lambda identity: identity == "supervisor"),
        )
        return service, queue, tmp_path

    def test_a_non_supervisor_identity_cannot_invoke_admission(self, tmp_path: Path) -> None:
        """The authorization boundary holds: only a supervisor-plane principal
        may admit; a non-supervisor identity refuses with a stable code."""
        service, queue, _ = self._service(tmp_path)
        service.submit("wf-1", "ronin-fresh")
        with pytest.raises(GoalEnrollError) as refused:
            service.admit("wf-1", U4_CLOSEOUT_DECISION_REF, decided_by="ronin-fresh")
        assert refused.value.code == CODE_NOT_SUPERVISOR
        # Nothing changed: the entry stays pending.
        assert queue.get("wf-1")["status"] == QUEUE_STATUS_PENDING

    def test_admission_of_an_already_rejected_enrollment_is_refused(self, tmp_path: Path) -> None:
        service, queue, _ = self._service(tmp_path)
        service.submit("wf-1", "ronin-fresh")
        queue.mark_rejected("wf-1", decided_by="supervisor", decision_ref="ref-reject")
        with pytest.raises(GoalEnrollError) as refused:
            service.admit("wf-1", U4_CLOSEOUT_DECISION_REF, decided_by="supervisor")
        assert refused.value.code == CODE_NOT_PENDING
        assert queue.get("wf-1")["status"] == QUEUE_STATUS_REJECTED

    def test_admission_of_an_already_withdrawn_enrollment_is_refused(self, tmp_path: Path) -> None:
        service, queue, _ = self._service(tmp_path)
        service.submit("wf-1", "ronin-fresh")
        queue.withdraw("wf-1", by="ronin-fresh")
        with pytest.raises(GoalEnrollError) as refused:
            service.admit("wf-1", U4_CLOSEOUT_DECISION_REF, decided_by="supervisor")
        assert refused.value.code == CODE_NOT_PENDING
        assert queue.get("wf-1")["status"] == QUEUE_STATUS_WITHDRAWN

    def test_successful_admission_writes_status_and_decision_ref(self, tmp_path: Path) -> None:
        service, queue, _ = self._service(tmp_path)
        service.submit("wf-1", "ronin-fresh")
        admitted = service.admit("wf-1", U4_CLOSEOUT_DECISION_REF, decided_by="supervisor")
        assert admitted["status"] == QUEUE_STATUS_ADMITTED
        assert admitted["decision_ref"] == U4_CLOSEOUT_DECISION_REF
        assert admitted["decided_by"] == "supervisor"
        persisted = queue.get("wf-1")
        assert persisted["status"] == QUEUE_STATUS_ADMITTED
        assert persisted["decision_ref"] == U4_CLOSEOUT_DECISION_REF
        # History retained the original pending row and appended the admission.
        assert [h["status"] for h in persisted["history"]] == [
            QUEUE_STATUS_PENDING,
            QUEUE_STATUS_ADMITTED,
        ]

    def test_repeated_admission_is_idempotent_and_does_not_rewrite_history(
        self, tmp_path: Path
    ) -> None:
        service, queue, _ = self._service(tmp_path)
        service.submit("wf-1", "ronin-fresh")
        service.admit("wf-1", U4_CLOSEOUT_DECISION_REF, decided_by="supervisor")
        again = service.admit("wf-1", U4_CLOSEOUT_DECISION_REF, decided_by="supervisor")
        assert again["already_admitted"] is True
        assert again["status"] == QUEUE_STATUS_ADMITTED
        assert again["decision_ref"] == U4_CLOSEOUT_DECISION_REF
        # History is neither duplicated nor destructively rewritten.
        persisted = queue.get("wf-1")
        assert [h["status"] for h in persisted["history"]] == [
            QUEUE_STATUS_PENDING,
            QUEUE_STATUS_ADMITTED,
        ]
        assert len(persisted["history"]) == 2

    def test_admission_requires_a_decision_reference(self, tmp_path: Path) -> None:
        service, queue, _ = self._service(tmp_path)
        service.submit("wf-1", "ronin-fresh")
        with pytest.raises(GoalEnrollError) as refused:
            service.admit("wf-1", "", decided_by="supervisor")
        assert refused.value.code == CODE_DECISION_REF_REQUIRED
        assert queue.get("wf-1")["status"] == QUEUE_STATUS_PENDING

    def test_an_already_admitted_enrollment_with_a_different_decision_refuses(
        self, tmp_path: Path
    ) -> None:
        service, _, _ = self._service(tmp_path)
        service.submit("wf-1", "ronin-fresh")
        service.admit("wf-1", "ref-old", decided_by="supervisor")
        with pytest.raises(GoalEnrollError) as refused:
            service.admit("wf-1", U4_CLOSEOUT_DECISION_REF, decided_by="supervisor")
        assert refused.value.code == CODE_NOT_PENDING

    def test_successful_admission_exposes_admitted_and_decision_ref_on_the_read_model(
        self, tmp_path: Path
    ) -> None:
        """/v1/enrollments reports the admitted status and exact decision_ref."""
        service, _, tmp_path = self._service(tmp_path)
        service.submit("wf-1", "ronin-fresh")
        service.admit("wf-1", U4_CLOSEOUT_DECISION_REF, decided_by="supervisor")

        from fleet_graph.state.fleet_state import FleetStateConfig, FleetStateView

        view = FleetStateView(
            FleetStateConfig(
                host="127.0.0.1",
                port=0,
                run_root=tmp_path / "runs",
                dd_root=tmp_path / "dd",
                lines_config=tmp_path / "missing.json",
                bridge_state_dir=tmp_path / "bridge",
                enroll_queue_path=tmp_path / "queue" / "enroll-queue.jsonl",
            )
        )
        payload = view.enrollments()
        entry = next(e for e in payload["enrollments"] if e["folder_id"] == "wf-1")
        assert entry["status"] == QUEUE_STATUS_ADMITTED
        assert entry["decision_ref"] == U4_CLOSEOUT_DECISION_REF

    def test_goal_admit_over_the_wire_refuses_non_supervisor(self, tmp_path: Path) -> None:
        from fastmcp import Client
        from fastmcp.exceptions import ToolError

        from fleet_graph.goal.service import build_goal_mcp_server
        from test_dd_service import running_server

        _folder(tmp_path, "wf-1", GOAL_MD_OK, GOLDEN_ORDER_OK)
        queue = EnrollQueue(str(tmp_path / "queue"))
        server = build_goal_mcp_server(
            goal_folders=_source(tmp_path),
            goal_queue=queue,
            real_roster=RealRosterReader(tmp_path / "absent.json"),
            board=None,
            alias_token_check=lambda alias: True,
            supervisor_identity_check=lambda identity: identity == "supervisor",
        )

        async def submit_and_admit(url: str) -> dict[str, Any]:
            async with Client(url) as client:
                await client.call_tool("goal_enroll", {"folder_id": "wf-1", "alias": "ronin-fresh"})
                try:
                    await client.call_tool(
                        "goal_admit",
                        {
                            "folder_id": "wf-1",
                            "decision_ref": U4_CLOSEOUT_DECISION_REF,
                            "decided_by": "ronin-fresh",
                        },
                    )
                    return {"refused": False, "code": None}
                except ToolError as exc:
                    message = str(exc)
                    payload = json.loads(message[message.index("{") : message.rindex("}") + 1])
                    return {"refused": True, "code": payload.get("code")}

        with running_server(server) as url:
            outcome = asyncio.run(submit_and_admit(url))

        assert outcome["refused"] is True
        assert outcome["code"] == CODE_NOT_SUPERVISOR
        assert queue.get("wf-1")["status"] == QUEUE_STATUS_PENDING

    def test_goal_admit_over_the_wire_admits_and_is_idempotent(self, tmp_path: Path) -> None:
        from fastmcp import Client

        from fleet_graph.goal.service import build_goal_mcp_server
        from test_dd_service import running_server

        _folder(tmp_path, "wf-1", GOAL_MD_OK, GOLDEN_ORDER_OK)
        queue = EnrollQueue(str(tmp_path / "queue"))
        server = build_goal_mcp_server(
            goal_folders=_source(tmp_path),
            goal_queue=queue,
            real_roster=RealRosterReader(tmp_path / "absent.json"),
            board=None,
            alias_token_check=lambda alias: True,
            supervisor_identity_check=lambda identity: identity == "supervisor",
        )

        async def call(url: str) -> dict[str, Any]:
            async with Client(url) as client:
                await client.call_tool("goal_enroll", {"folder_id": "wf-1", "alias": "ronin-fresh"})
                first = _payload(
                    await client.call_tool(
                        "goal_admit",
                        {
                            "folder_id": "wf-1",
                            "decision_ref": U4_CLOSEOUT_DECISION_REF,
                            "decided_by": "supervisor",
                        },
                    )
                )
                second = _payload(
                    await client.call_tool(
                        "goal_admit",
                        {
                            "folder_id": "wf-1",
                            "decision_ref": U4_CLOSEOUT_DECISION_REF,
                            "decided_by": "supervisor",
                        },
                    )
                )
                return {"first": first, "second": second}

        with running_server(server) as url:
            result = asyncio.run(call(url))

        assert result["first"]["status"] == QUEUE_STATUS_ADMITTED
        assert result["first"]["decision_ref"] == U4_CLOSEOUT_DECISION_REF
        assert result["second"]["already_admitted"] is True
        assert result["second"]["status"] == QUEUE_STATUS_ADMITTED
        persisted = queue.get("wf-1")
        assert [h["status"] for h in persisted["history"]] == [
            QUEUE_STATUS_PENDING,
            QUEUE_STATUS_ADMITTED,
        ]
