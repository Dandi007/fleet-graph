"""M4 acceptance: the MCP-surface functional-availability oracle.

Covers the two-way criterion from wf-525fd4 goal.md M4 without a transport
layer -- every test injects a fake ``McpSurface`` (红管纪律: 可无传输层单测 /
纯判定 / 不触碰生产账本或生产文件):

- 阴性 (no false green): a healthy surface is ``available``, and an explicitly
  ``NOT_SUPPORTED`` historical tool's refusal is correct behaviour -- neither a
  success nor a failure.
- 阳性 (no false clear): an unreachable surface (upstream pointed at nothing)
  and a surface whose read-only tool genuinely fails are both ``unavailable``.
- 不可判定 is marked explicitly (``indeterminate``), never silently asserted.
"""

from __future__ import annotations

import json
from typing import Any

from fleet_graph.mcp_availability import (
    NOT_SUPPORTED_CODE,
    PROBE_ERROR,
    PROBE_NOT_SUPPORTED,
    PROBE_SUCCESS,
    STATUS_AVAILABLE,
    STATUS_INDETERMINATE,
    STATUS_UNAVAILABLE,
    judge_mcp_availability,
)


class FakeSurface:
    """An injected MCP surface: tools/list names + per-tool call behaviour."""

    def __init__(
        self,
        tools: tuple[str, ...] = (),
        *,
        list_error: BaseException | None = None,
        responses: dict[str, Any] | None = None,
    ) -> None:
        self._tools = list(tools)
        self._list_error = list_error
        self._responses = responses if responses is not None else {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self) -> list[str]:
        if self._list_error is not None:
            raise self._list_error
        return list(self._tools)

    def call_tool(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((tool, arguments))
        response = self._responses[tool]
        if isinstance(response, BaseException):
            raise response
        return response


def refused_not_supported(tool: str) -> BaseException:
    """The dd-surface refusal shape: a JSON error carrying ``NOT_SUPPORTED``."""
    return RuntimeError(
        json.dumps(
            {
                "code": NOT_SUPPORTED_CODE,
                "tool": tool,
                "reason": "legacy-only tool, refuses explicitly",
                "ruling": "wf-a08949",
            },
            sort_keys=True,
        )
    )


def probe_outcomes(verdict: Any, tool: str) -> dict[str, Any]:
    for probe in verdict.probes:
        if probe.tool == tool:
            return probe
    raise AssertionError(f"no probe recorded for {tool!r}")


# --- 阴性: 面正常不得火 (available) ----------------------------------------


def test_healthy_surface_is_available() -> None:
    surface = FakeSurface(
        ("development_list", "development_get", "development_evidence"),
        responses={
            "development_list": {"developments": []},
            "development_get": {"development_id": "dev-1"},
            "development_evidence": {},
        },
    )
    verdict = judge_mcp_availability(surface, ("development_list",))

    assert verdict.status == STATUS_AVAILABLE
    assert verdict.tools_listed == [
        "development_list",
        "development_get",
        "development_evidence",
    ]
    probe = probe_outcomes(verdict, "development_list")
    assert probe.outcome == PROBE_SUCCESS


def test_available_only_needs_one_read_only_success() -> None:
    surface = FakeSurface(
        ("development_list", "development_get"),
        responses={
            "development_list": {"developments": []},
            "development_get": RuntimeError("development_id required"),
        },
    )
    verdict = judge_mcp_availability(surface, ("development_list", "development_get"))

    assert verdict.status == STATUS_AVAILABLE
    assert probe_outcomes(verdict, "development_get").outcome == PROBE_ERROR


# --- 阳性: 不可用必须产出不可用 (unavailable) -------------------------------


def test_unreachable_surface_is_unavailable() -> None:
    surface = FakeSurface(list_error=RuntimeError("connection refused"))
    verdict = judge_mcp_availability(surface, ("development_list",))

    assert verdict.status == STATUS_UNAVAILABLE
    assert verdict.list_error == "connection refused"
    assert verdict.tools_listed == []


def test_broken_read_only_tool_is_unavailable() -> None:
    surface = FakeSurface(
        ("development_list",),
        responses={"development_list": RuntimeError("internal error in list")},
    )
    verdict = judge_mcp_availability(surface, ("development_list",))

    assert verdict.status == STATUS_UNAVAILABLE
    assert probe_outcomes(verdict, "development_list").outcome == PROBE_ERROR


# --- NOT_SUPPORTED 不计失败 -------------------------------------------------


def test_not_supported_refusal_is_not_a_failure() -> None:
    surface = FakeSurface(
        ("development_steer", "development_list"),
        responses={
            "development_steer": refused_not_supported("development_steer"),
            "development_list": {"developments": []},
        },
    )
    verdict = judge_mcp_availability(surface, ("development_steer", "development_list"))

    assert verdict.status == STATUS_AVAILABLE
    assert probe_outcomes(verdict, "development_steer").outcome == PROBE_NOT_SUPPORTED
    assert probe_outcomes(verdict, "development_list").outcome == PROBE_SUCCESS


def test_all_not_supported_is_marked_indeterminate_not_unavailable() -> None:
    surface = FakeSurface(
        ("development_steer", "development_relock"),
        responses={
            "development_steer": refused_not_supported("development_steer"),
            "development_relock": refused_not_supported("development_relock"),
        },
    )
    verdict = judge_mcp_availability(surface, ("development_steer", "development_relock"))

    assert verdict.status == STATUS_INDETERMINATE
    assert verdict.probes, "the two refusals must still be recorded as evidence"
    assert all(p.outcome == PROBE_NOT_SUPPORTED for p in verdict.probes)


def test_not_supported_does_not_mask_a_real_error() -> None:
    surface = FakeSurface(
        ("development_steer", "development_list"),
        responses={
            "development_steer": refused_not_supported("development_steer"),
            "development_list": RuntimeError("connection reset during list"),
        },
    )
    verdict = judge_mcp_availability(surface, ("development_steer", "development_list"))

    assert verdict.status == STATUS_UNAVAILABLE
    assert probe_outcomes(verdict, "development_steer").outcome == PROBE_NOT_SUPPORTED
    assert probe_outcomes(verdict, "development_list").outcome == PROBE_ERROR


# --- 不可判定须显式标注 -----------------------------------------------------


def test_no_read_only_tools_is_marked_indeterminate() -> None:
    surface = FakeSurface(("development_list",))
    verdict = judge_mcp_availability(surface, ())

    assert verdict.status == STATUS_INDETERMINATE
    assert "no read-only tools" in verdict.list_error


# --- 判定口是结构化结论 + 传参 ---------------------------------------------


def test_verdict_is_structured_not_a_bool() -> None:
    surface = FakeSurface(("development_list",), responses={"development_list": {}})
    verdict = judge_mcp_availability(surface, ("development_list",))

    assert isinstance(verdict.as_dict(), dict)
    assert verdict.as_dict()["status"] in {
        STATUS_AVAILABLE,
        STATUS_UNAVAILABLE,
        STATUS_INDETERMINATE,
    }
    assert set(verdict.as_dict()) == {"status", "tools_listed", "probes", "list_error"}


def test_probe_arguments_are_passed_through() -> None:
    surface = FakeSurface(("development_list",), responses={"development_list": {}})
    judge_mcp_availability(
        surface,
        ("development_list",),
        arguments={"state": "running", "limit": 4},
    )

    assert surface.calls == [("development_list", {"state": "running", "limit": 4})]
