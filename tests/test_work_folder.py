"""Work-folder client behaviour, against a recording tool caller."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from fleet_graph.state.work_folder import (
    WorkFolder,
    WorkFolderBroken,
    WorkFolderError,
    _unwrap,
)


class RecordingCaller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: list[Any] = []
        self.default: Any = {"ok": True}

    def queue(self, response: Any) -> None:
        self.responses.append(response)

    def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((tool, arguments))
        if self.responses:
            return self.responses.pop(0)
        return self.default


@pytest.fixture
def caller() -> RecordingCaller:
    return RecordingCaller()


@pytest.fixture
def folder(caller: RecordingCaller) -> WorkFolder:
    return WorkFolder("wf-3f30cd", caller)


class TestPassthrough:
    def test_append_progress_sends_the_documented_arguments(
        self, folder: WorkFolder, caller: RecordingCaller
    ) -> None:
        caller.queue({"ok": True, "appended": True})
        folder.append_progress("did a thing", source_session_id="sess", idempotency_key="k")
        tool, args = caller.calls[0]
        assert tool == "wf_append_progress"
        assert args == {
            "folder_id": "wf-3f30cd",
            "entry": "did a thing",
            "source_session_id": "sess",
            "idempotency_key": "k",
        }

    def test_read_returns_content(self, folder: WorkFolder, caller: RecordingCaller) -> None:
        caller.queue({"ok": True, "content": "1\thello"})
        assert folder.read("progress.md") == "1\thello"

    def test_create_and_write_are_distinct_tools(
        self, folder: WorkFolder, caller: RecordingCaller
    ) -> None:
        """fs_write does not create; conflating them silently loses writes."""
        caller.queue({"ok": True})
        folder.create("new.md", "body")
        caller.queue({"ok": True})
        folder.write("existing.md", "body")
        assert [tool for tool, _ in caller.calls] == ["fs_create", "fs_write"]

    def test_list_returns_entries(self, folder: WorkFolder, caller: RecordingCaller) -> None:
        caller.queue({"ok": True, "entries": [{"filename": "plan.md"}]})
        assert folder.list() == [{"filename": "plan.md"}]

    def test_edit_passes_both_strings(self, folder: WorkFolder, caller: RecordingCaller) -> None:
        caller.queue({"ok": True})
        folder.edit("plan.md", "old", "new")
        _, args = caller.calls[0]
        assert args["old_string"] == "old"
        assert args["new_string"] == "new"


class TestFolderIdIsOpaque:
    def test_id_is_passed_through_untouched(self, caller: RecordingCaller) -> None:
        """It is a token, not a path. Never parsed, joined, or rewritten."""
        weird = "wf-ZZ/../not-a-path"
        WorkFolder(weird, caller).read("progress.md")
        _, args = caller.calls[0]
        assert args["folder_id"] == weird


class TestErrors:
    def test_ok_false_raises(self, folder: WorkFolder, caller: RecordingCaller) -> None:
        caller.queue({"ok": False, "error": "nope"})
        with pytest.raises(WorkFolderError, match="fs_read failed"):
            folder.read("progress.md")

    def test_non_object_result_raises(self, folder: WorkFolder, caller: RecordingCaller) -> None:
        caller.queue("just a string")
        with pytest.raises(WorkFolderError, match="expected an object"):
            folder.read("progress.md")


class TestResume:
    def test_healthy_resume_returns_a_report(self, caller: RecordingCaller) -> None:
        caller.queue(
            {
                "ok": True,
                "blocked": False,
                "verification": {"overall": "MATCH"},
                "loaded": {"progress": "# Progress", "context": "# Context"},
            }
        )
        folder, report = WorkFolder.resume("wf-3f30cd", caller)
        assert folder.folder_id == "wf-3f30cd"
        assert report.verification == "MATCH"
        assert report.progress == "# Progress"
        assert report.blocked is False

    @pytest.mark.parametrize(
        "raw",
        [
            {"ok": True, "blocked": True, "verification": {"overall": "MATCH"}},
            {"ok": True, "blocked": False, "verification": {"overall": "BROKEN"}},
        ],
    )
    def test_broken_resume_stops_rather_than_improvising(
        self, caller: RecordingCaller, raw: dict[str, Any]
    ) -> None:
        """House rule: BROKEN means stop and report, not work around it."""
        caller.queue(raw)
        with pytest.raises(WorkFolderBroken):
            WorkFolder.resume("wf-3f30cd", caller)

    def test_broken_is_not_swallowed_by_generic_error_handling(self) -> None:
        assert issubclass(WorkFolderBroken, WorkFolderError)


class TestUnwrap:
    def test_prefers_structured_content(self) -> None:
        result = SimpleNamespace(structured_content={"ok": True, "a": 1})
        assert _unwrap(result) == {"ok": True, "a": 1}

    def test_falls_back_to_json_in_a_text_block(self) -> None:
        result = SimpleNamespace(
            structured_content=None,
            data=None,
            content=[SimpleNamespace(text='{"ok": true, "b": 2}')],
        )
        assert _unwrap(result) == {"ok": True, "b": 2}

    def test_non_json_text_comes_back_as_text(self) -> None:
        result = SimpleNamespace(
            structured_content=None, data=None, content=[SimpleNamespace(text="not json")]
        )
        assert _unwrap(result) == "not json"


class TestLoopbackIsNeverProxied:
    """This host exports a SOCKS proxy; the MCP is on 127.0.0.1.

    fastmcp hit the same trap the bus client did, and it does not merely
    misroute -- it fails to connect, because the socks extra is absent.
    """

    def test_factory_forces_trust_env_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fleet_graph.state.work_folder import _loopback_httpx_client

        monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:7891")
        client = _loopback_httpx_client(headers={"a": "b"}, timeout=5.0)
        assert client.trust_env is False

    def test_factory_tolerates_kwargs_it_has_never_seen(self) -> None:
        """fastmcp's factory contract has changed between versions."""
        from fleet_graph.state.work_folder import _loopback_httpx_client

        client = _loopback_httpx_client(follow_redirects=True, auth=None, timeout=None)
        assert client.trust_env is False

    def test_caller_defaults_to_the_loopback_mcp(self) -> None:
        from fleet_graph.state.work_folder import (
            DEFAULT_WORK_FOLDER_MCP_URL,
            FastMCPCaller,
        )

        assert FastMCPCaller().url == DEFAULT_WORK_FOLDER_MCP_URL
        assert "127.0.0.1" in DEFAULT_WORK_FOLDER_MCP_URL
