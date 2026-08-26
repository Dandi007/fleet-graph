"""hello-graph wiring, with a stub TextNode (no gateway traffic)."""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from fleet_graph.executors.text_node import TextResult, TextSpec
from fleet_graph.graphs.hello import HelloConfig, build_hello_graph


class StubNode:
    def __init__(self) -> None:
        self.calls: list[tuple[TextSpec, str]] = []

    def complete(self, spec: TextSpec, prompt: str) -> TextResult:
        self.calls.append((spec, prompt))
        return TextResult(
            text=f"[{spec.model}] says something",
            model=spec.model,
            finish_reason="stop",
            usage={"total_tokens": 10},
        )


def run(node: Any, config: HelloConfig | None = None) -> dict[str, Any]:
    graph = build_hello_graph(node, config)
    compiled = graph.compile(checkpointer=InMemorySaver())
    return compiled.invoke({"topic": "backpressure"}, config={"configurable": {"thread_id": "t1"}})


def test_both_nodes_run_and_state_flows_forward() -> None:
    node = StubNode()
    state = run(node)
    assert "draft" in state
    assert "critique" in state
    # The critic must actually see the draft, not just the topic.
    assert state["draft"] in node.calls[1][1]


def test_each_node_uses_its_own_model() -> None:
    """Heterogeneous per-node models are the reason for the framework."""
    node = StubNode()
    run(node, HelloConfig(drafter="deepseek-v4-flash", critic="glm-4.6"))
    assert [spec.model for spec, _ in node.calls] == ["deepseek-v4-flash", "glm-4.6"]


def test_usage_accumulates_across_nodes() -> None:
    node = StubNode()
    state = run(node)
    assert len(state["usage"]) == 2
    assert {u["model"] for u in state["usage"]} == {"deepseek-v4-flash", "glm-4.6"}


def test_default_budget_leaves_room_for_reasoning_tokens() -> None:
    """512 was not enough: glm-4.6 emitted no visible text at all."""
    assert HelloConfig().max_tokens >= 2048
