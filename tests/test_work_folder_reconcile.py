"""B3 work-folder residue reconciliation: classify, plan, adopt, refuse.

Pins the correction for the ``wf-a87b04`` incident: a governed work folder whose
only residue is a tracked, pure append to an allowed bookkeeping file is
classifiable as adoptable and adopted by an exact confirmed plan; every other
residue shape refuses closed and stays byte-for-byte unchanged; the plan is a
dry-run with no mutation; a stale or mismatched confirmation refuses without
mutation; and ``wf_reconcile`` is a real, registered MCP tool that a client can
list and drive dry-run -> confirm over the wire, never leaking a physical path.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from fleet_graph.dd.reconcile import (
    CLS_ADOPTABLE,
    CLS_BINARY,
    CLS_CONFLICT,
    CLS_CROSS_FOLDER,
    CLS_DELETION,
    CLS_DIRTY_CONTROL,
    CLS_REWRITE,
    CLS_UNTRACKED,
    RECONCILE_MECHANISM,
    InspectedFile,
    ReconcileError,
    WorkFolderReconciler,
    classify_file,
)
from fleet_graph.dd.upstream_constants import compute_digest

PROGRESS = b"# Progress\n- first line\n"
APPEND = b"- resolved: adopt the residue\n"


def tracked(filename: str, base: bytes, current: bytes) -> InspectedFile:
    return InspectedFile(filename=filename, base=base, current=current, tracked=True)


def untracked(filename: str, current: bytes) -> InspectedFile:
    return InspectedFile(filename=filename, base=None, current=current, tracked=False)


class FakeSource:
    """In-memory governed work-folder store behind the reconcile seam."""

    def __init__(self, files: list[InspectedFile]) -> None:
        self._files = list(files)
        self.adopt_calls: list[tuple[str, tuple[tuple[str, bytes, bytes], ...]]] = []

    def inspect(self, folder_id: str) -> tuple[InspectedFile, ...]:
        return tuple(self._files)

    def adopt(
        self, folder_id: str, entries: tuple[tuple[str, bytes, bytes], ...]
    ) -> dict[str, Any]:
        self.adopt_calls.append((folder_id, entries))
        for filename, base, appended in entries:
            committed = base + appended
            for index, item in enumerate(self._files):
                if item.filename == filename:
                    self._files[index] = InspectedFile(
                        filename=filename, base=committed, current=committed, tracked=True
                    )
                    break
        return {"store": "fake", "committed_files": len(entries)}

    def file(self, filename: str) -> InspectedFile | None:
        return next((item for item in self._files if item.filename == filename), None)


class TestClassification:
    def test_pure_append_is_adoptable(self) -> None:
        assert (
            classify_file(
                "progress.md",
                base=PROGRESS,
                current=PROGRESS + APPEND,
                tracked=True,
                allowed=frozenset({"progress.md"}),
                control=frozenset(),
            )
            == CLS_ADOPTABLE
        )

    def test_replacement_is_rewrite(self) -> None:
        assert (
            classify_file(
                "progress.md",
                base=PROGRESS,
                current=b"# Progress\n- replaced\n",
                tracked=True,
                allowed=frozenset({"progress.md"}),
                control=frozenset(),
            )
            == CLS_REWRITE
        )

    def test_prepend_is_rewrite(self) -> None:
        assert (
            classify_file(
                "progress.md",
                base=PROGRESS,
                current=b"# Preamble\n" + PROGRESS,
                tracked=True,
                allowed=frozenset({"progress.md"}),
                control=frozenset(),
            )
            == CLS_REWRITE
        )

    def test_mid_file_edit_is_rewrite(self) -> None:
        assert (
            classify_file(
                "progress.md",
                base=PROGRESS,
                current=PROGRESS.replace(b"first", b"EDITED"),
                tracked=True,
                allowed=frozenset({"progress.md"}),
                control=frozenset(),
            )
            == CLS_REWRITE
        )

    def test_conflict_marker_is_conflict(self) -> None:
        assert (
            classify_file(
                "progress.md",
                base=PROGRESS,
                current=PROGRESS + b"<<<<<<< ours\n",
                tracked=True,
                allowed=frozenset({"progress.md"}),
                control=frozenset(),
            )
            == CLS_CONFLICT
        )

    def test_deletion_is_deletion(self) -> None:
        assert (
            classify_file(
                "progress.md",
                base=PROGRESS,
                current=None,
                tracked=True,
                allowed=frozenset({"progress.md"}),
                control=frozenset(),
            )
            == CLS_DELETION
        )

    def test_untracked_file_is_untracked(self) -> None:
        assert (
            classify_file(
                "new.md",
                base=None,
                current=b"brand new\n",
                tracked=False,
                allowed=frozenset({"progress.md"}),
                control=frozenset(),
            )
            == CLS_UNTRACKED
        )

    def test_binary_diff_is_binary(self) -> None:
        assert (
            classify_file(
                "progress.md",
                base=PROGRESS,
                current=PROGRESS + b"\x00\x01\x02",
                tracked=True,
                allowed=frozenset({"progress.md"}),
                control=frozenset(),
            )
            == CLS_BINARY
        )

    def test_cross_folder_change_is_cross_folder(self) -> None:
        assert (
            classify_file(
                "src/impl.py",
                base=b"x = 1\n",
                current=b"x = 1\nx = 2\n",
                tracked=True,
                allowed=frozenset({"progress.md"}),
                control=frozenset(),
            )
            == CLS_CROSS_FOLDER
        )

    def test_dirty_control_file_is_dirty_control(self) -> None:
        assert (
            classify_file(
                "manifest.json",
                base=b"{}\n",
                current=b"{}\n{}\n",
                tracked=True,
                allowed=frozenset({"progress.md"}),
                control=frozenset({"manifest.json"}),
            )
            == CLS_DIRTY_CONTROL
        )

    def test_unchanged_is_clean(self) -> None:
        assert (
            classify_file(
                "progress.md",
                base=PROGRESS,
                current=PROGRESS,
                tracked=True,
                allowed=frozenset({"progress.md"}),
                control=frozenset(),
            )
            == "clean"
        )


class TestDryRunAndAdoption:
    def test_dry_run_classifies_and_mutates_nothing(self) -> None:
        source = FakeSource([tracked("progress.md", PROGRESS, PROGRESS + APPEND)])
        reconciler = WorkFolderReconciler(clock=lambda: 1_700_000_000.0)

        plan = reconciler.plan("wf-a87b04", source.inspect("wf-a87b04"))

        assert plan["folder_id"] == "wf-a87b04"
        assert plan["clean"] is False
        assert plan["entries"][0]["classification"] == CLS_ADOPTABLE
        assert plan["entries"][0]["appended_size"] == len(APPEND)
        assert plan["entries"][0]["appended_digest"] == compute_digest(APPEND)
        assert plan["token"].startswith("sha256:")
        assert source.adopt_calls == []  # dry-run made no mutation
        assert source.file("progress.md").current == PROGRESS + APPEND  # unchanged

    def test_confirmed_execution_adopts_exact_bytes_and_records_evidence(self) -> None:
        source = FakeSource([tracked("progress.md", PROGRESS, PROGRESS + APPEND)])
        reconciler = WorkFolderReconciler(clock=lambda: 1_700_000_000.0)
        plan = reconciler.plan("wf-a87b04", source.inspect("wf-a87b04"))

        result = reconciler.confirm(
            "wf-a87b04", plan["token"], source.inspect("wf-a87b04"), adopt=source.adopt
        )

        # The exact appended bytes, nothing more, nothing less.
        assert source.adopt_calls == [("wf-a87b04", (("progress.md", PROGRESS, APPEND),))]
        # An immutable receipt, sealed and bound to the mechanism.
        assert result["mechanism"] == RECONCILE_MECHANISM
        assert result["digest"].startswith("sha256:")
        assert result["adopted"][0]["filename"] == "progress.md"
        assert result["adopted"][0]["appended_utf8_bytes"] == len(APPEND)
        assert result["adopted"][0]["appended_digest"] == compute_digest(APPEND)
        # The folder is now clean: base absorbed the adopted append.
        adopted_file = source.file("progress.md")
        assert adopted_file.base == adopted_file.current

    def test_replay_is_idempotent_and_never_forks(self) -> None:
        source = FakeSource([tracked("progress.md", PROGRESS, PROGRESS + APPEND)])
        reconciler = WorkFolderReconciler()
        plan = reconciler.plan("wf-a87b04", source.inspect("wf-a87b04"))

        first = reconciler.confirm(
            "wf-a87b04", plan["token"], source.inspect("wf-a87b04"), adopt=source.adopt
        )
        second = reconciler.confirm(
            "wf-a87b04", plan["token"], source.inspect("wf-a87b04"), adopt=source.adopt
        )

        assert second == first
        assert len(source.adopt_calls) == 1  # adopted exactly once

    def test_a_clean_folder_plans_clean_and_adopts_nothing(self) -> None:
        source = FakeSource([tracked("progress.md", PROGRESS, PROGRESS)])
        reconciler = WorkFolderReconciler()

        plan = reconciler.plan("wf-a87b04", source.inspect("wf-a87b04"))
        assert plan["clean"] is True
        assert plan["entries"] == []

        result = reconciler.confirm(
            "wf-a87b04", plan["token"], source.inspect("wf-a87b04"), adopt=source.adopt
        )
        assert result["adopted"] == []
        assert source.adopt_calls == []


class TestRefusals:
    @pytest.mark.parametrize(
        ("label", "files", "classification"),
        [
            ("deletion", [tracked("progress.md", PROGRESS, None)], CLS_DELETION),
            ("rewrite", [tracked("progress.md", PROGRESS, b"# replaced\n")], CLS_REWRITE),
            (
                "conflict",
                [tracked("progress.md", PROGRESS, PROGRESS + b"<<<<<<< ours\n")],
                CLS_CONFLICT,
            ),
            ("untracked", [untracked("scratch.md", b"junk\n")], CLS_UNTRACKED),
            ("binary", [tracked("progress.md", PROGRESS, PROGRESS + b"\x00")], CLS_BINARY),
            (
                "cross_folder",
                [tracked("src/impl.py", b"x = 1\n", b"x = 1\nx = 2\n")],
                CLS_CROSS_FOLDER,
            ),
            ("dirty_control", [tracked("manifest.json", b"{}\n", b"{}\n{}\n")], CLS_DIRTY_CONTROL),
        ],
    )
    def test_residue_refuses_closed_and_remains_byte_for_byte_unchanged(
        self, label: str, files: list[InspectedFile], classification: str
    ) -> None:
        source = FakeSource(files)
        reconciler = WorkFolderReconciler()
        before = source.inspect("wf-a87b04")

        with pytest.raises(ReconcileError, match=classification):
            reconciler.plan("wf-a87b04", source.inspect("wf-a87b04"))

        # Nothing changed: no adopt effect fired, bytes are the same references.
        assert source.adopt_calls == []
        assert source.inspect("wf-a87b04") == before

    def test_stale_or_changed_confirmation_refuses_without_mutation(self) -> None:
        source = FakeSource([tracked("progress.md", PROGRESS, PROGRESS + APPEND)])
        reconciler = WorkFolderReconciler()
        plan = reconciler.plan("wf-a87b04", source.inspect("wf-a87b04"))

        # The base moves after the dry-run: the token no longer binds.
        changed = [tracked("progress.md", PROGRESS + APPEND, PROGRESS + APPEND + b"- more\n")]
        with pytest.raises(ReconcileError, match="does not bind"):
            reconciler.confirm("wf-a87b04", plan["token"], changed, adopt=source.adopt)
        assert source.adopt_calls == []

    def test_wrong_folder_refuses_without_mutation(self) -> None:
        source = FakeSource([tracked("progress.md", PROGRESS, PROGRESS + APPEND)])
        reconciler = WorkFolderReconciler()
        plan = reconciler.plan("wf-a87b04", source.inspect("wf-a87b04"))

        with pytest.raises(ReconcileError, match="does not bind"):
            reconciler.confirm(
                "wf-somewhere-else",
                plan["token"],
                source.inspect("wf-a87b04"),
                adopt=source.adopt,
            )
        assert source.adopt_calls == []


def _payload(result: Any) -> dict[str, Any]:
    data = getattr(result, "structured_content", None) or getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    content = getattr(result, "content", None)
    if content:
        return json.loads(getattr(content[0], "text", None))
    raise AssertionError(f"unexpected tool result: {result!r}")


class TestMCPRegisteredTool:
    def test_wf_reconcile_is_listed_and_drives_dry_run_then_confirm(self) -> None:
        from fastmcp import Client

        from fleet_graph.dd.service import build_mcp_server
        from test_dd_service import FakeControlPlane, running_server

        source = FakeSource([tracked("progress.md", PROGRESS, PROGRESS + APPEND)])
        server = build_mcp_server(FakeControlPlane(), work_folders=source)

        tools = asyncio.run(server.list_tools())
        assert "wf_reconcile" in {tool.name for tool in tools}

        async def exercise(url: str) -> tuple[dict[str, Any], dict[str, Any]]:
            async with Client(url) as client:
                dry = await client.call_tool("wf_reconcile", {"folder_id": "wf-a87b04"})
                plan = _payload(dry)
                confirmed = await client.call_tool(
                    "wf_reconcile", {"folder_id": "wf-a87b04", "token": plan["token"]}
                )
                return plan, _payload(confirmed)

        with running_server(server) as url:
            plan, adopted = asyncio.run(exercise(url))

        assert plan["entries"][0]["classification"] == CLS_ADOPTABLE
        assert plan["token"]
        assert adopted["mechanism"] == RECONCILE_MECHANISM
        assert [entry["filename"] for entry in adopted["adopted"]] == ["progress.md"]

        # No physical data-repository path leaks into any public payload.
        for payload in (plan, adopted):
            for forbidden in ("/data", "/worktrees", "/code/self"):
                assert forbidden not in json.dumps(payload, sort_keys=True)

    def test_refusal_over_the_wire_carries_no_physical_path(self) -> None:
        from fastmcp import Client
        from fastmcp.exceptions import ToolError

        from fleet_graph.dd.service import build_mcp_server
        from test_dd_service import FakeControlPlane, running_server

        source = FakeSource([tracked("progress.md", PROGRESS, b"# replaced\n")])
        server = build_mcp_server(FakeControlPlane(), work_folders=source)

        async def call(url: str) -> str:
            async with Client(url) as client:
                with pytest.raises(ToolError) as excinfo:
                    await client.call_tool("wf_reconcile", {"folder_id": "wf-a87b04"})
                return str(excinfo.value)

        with running_server(server) as url:
            message = asyncio.run(call(url))

        payload = json.loads(message[message.index("{") : message.rindex("}") + 1])
        assert payload["code"] == "RECONCILE_REFUSED"
        for forbidden in ("/data", "/worktrees", "/code/self"):
            assert forbidden not in message

    def test_an_unbound_server_refuses_explicitly(self) -> None:
        from fastmcp import Client
        from fastmcp.exceptions import ToolError

        from fleet_graph.dd.service import build_mcp_server
        from test_dd_service import FakeControlPlane, running_server

        server = build_mcp_server(FakeControlPlane(), work_folders=None)

        async def call(url: str) -> str:
            async with Client(url) as client:
                with pytest.raises(ToolError) as excinfo:
                    await client.call_tool("wf_reconcile", {"folder_id": "wf-a87b04"})
                return str(excinfo.value)

        with running_server(server) as url:
            message = asyncio.run(call(url))

        assert "RECONCILE_SOURCE_UNBOUND" in message


def test_b3_findings_document_closes_the_chain_with_anchors() -> None:
    from pathlib import Path

    doc_path = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "findings"
        / ("work-folder-residue-reconciliation.md")
    )
    text = doc_path.read_text(encoding="utf-8")
    for heading in ("## Phenomenon", "## Mechanism", "## Evidence"):
        assert heading in text
    for anchor in ("wf_reconcile", "test_work_folder_reconcile", "WorkFolderReconciler"):
        assert anchor in text
