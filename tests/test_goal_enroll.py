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
    CODE_FOLDER_NOT_FOUND,
    CODE_GOLDEN_ORDER_EMPTY,
    CODE_NO_ACCEPTANCE_COMMAND,
    CODE_NOT_A_GOAL_LINE,
    CODE_SOURCE_UNBOUND,
    CODE_SPEC_LINT_BAN,
    GOAL_ENROLL_MECHANISM,
    LINT_WARNING_PINNED_SHA,
    GoalEnrollError,
    GoalRosterEntry,
)
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


class TestServiceAndMCP:
    def test_service_seals_the_engine_versioned_roster_entry(self, tmp_path: Path) -> None:
        _folder(tmp_path, "wf-1", GOAL_MD_OK, GOLDEN_ORDER_OK)
        service = GoalEnrollService(
            GoalEnrollValidator(_source(tmp_path)), roster=GoalEnrollRoster(str(tmp_path / "store"))
        )
        admitted = service.enroll("wf-1")
        assert admitted["already_admitted"] is False
        assert admitted["briefing_version"] == BRIEFING_VERSION
        assert admitted["mechanism"] == GOAL_ENROLL_MECHANISM

    def test_the_goal_open_prompt_and_briefing_are_versioned(self) -> None:
        text = goal_open_prompt_text()
        assert GOAL_OPEN_PROMPT_NAME in text
        assert BRIEFING_VERSION in text
        assert BRIEFING_VERSION in BRIEFING_TEXT
        # The briefing carries the recorded constraints verbatim.
        for constraint in ("never merges to main directly", "dd-evidence", ".dev-dispatch"):
            assert constraint in BRIEFING_TEXT

    def test_the_tool_is_registered_on_the_mcp_surface(self) -> None:
        from fleet_graph.dd.service import build_mcp_server
        from test_dd_service import FakeControlPlane

        server = build_mcp_server(FakeControlPlane())
        tools = asyncio.run(server.list_tools())
        assert "goal_enroll" in {tool.name for tool in tools}
        prompts = asyncio.run(server.list_prompts())
        assert GOAL_OPEN_PROMPT_NAME in {prompt.name for prompt in prompts}
        resources = asyncio.run(server.list_resources())
        assert str(BRIEFING_RESOURCE_URI) in {str(res.uri) for res in resources}

    def test_the_refusal_reaches_the_client_machine_readably(self, tmp_path: Path) -> None:
        from fastmcp import Client
        from fastmcp.exceptions import ToolError

        from fleet_graph.dd.service import build_mcp_server
        from test_dd_service import FakeControlPlane, running_server

        _folder(tmp_path, "wf-1", "# no acceptance\n", GOLDEN_ORDER_OK)
        source = _source(tmp_path)
        server = build_mcp_server(FakeControlPlane(), goal_folders=source)

        async def call(url: str) -> str:
            async with Client(url) as client:
                with pytest.raises(ToolError) as excinfo:
                    await client.call_tool("goal_enroll", {"folder_id": "wf-1"})
                return str(excinfo.value)

        with running_server(server) as url:
            message = asyncio.run(call(url))

        payload = json.loads(message[message.index("{") : message.rindex("}") + 1])
        assert payload["code"] == CODE_NO_ACCEPTANCE_COMMAND
        assert payload["tool"] == "goal_enroll"

    def test_an_unbound_server_refuses_goal_enroll_explicitly(self) -> None:
        from fastmcp import Client
        from fastmcp.exceptions import ToolError

        from fleet_graph.dd.service import build_mcp_server
        from test_dd_service import FakeControlPlane, running_server

        server = build_mcp_server(FakeControlPlane(), goal_folders=None)

        async def call(url: str) -> str:
            async with Client(url) as client:
                with pytest.raises(ToolError) as excinfo:
                    await client.call_tool("goal_enroll", {"folder_id": "wf-1"})
                return str(excinfo.value)

        with running_server(server) as url:
            message = asyncio.run(call(url))

        payload = json.loads(message[message.index("{") : message.rindex("}") + 1])
        assert payload["code"] == CODE_SOURCE_UNBOUND

    def test_a_valid_admission_over_the_wire(self, tmp_path: Path) -> None:
        from fastmcp import Client

        from fleet_graph.dd.service import build_mcp_server
        from test_dd_service import FakeControlPlane, running_server

        _folder(tmp_path, "wf-1", GOAL_MD_OK, GOLDEN_ORDER_OK)
        source = _source(tmp_path)
        roster = GoalEnrollRoster(str(tmp_path / "store"))
        server = build_mcp_server(FakeControlPlane(), goal_folders=source, goal_roster=roster)

        async def call(url: str) -> dict[str, Any]:
            async with Client(url) as client:
                result = await client.call_tool("goal_enroll", {"folder_id": "wf-1"})
                return _payload(result)

        with running_server(server) as url:
            admitted = asyncio.run(call(url))

        assert admitted["already_admitted"] is False
        assert admitted["briefing_version"] == BRIEFING_VERSION
        assert admitted["acceptance_argv"] == [["python3", "-c", "print('ok')"]]


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
