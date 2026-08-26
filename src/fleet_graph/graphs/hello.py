"""hello-graph: the smallest thing that proves the spine works end to end.

plan.md P1 wants one graph that really runs through the New API gateway. This
is it, and it is deliberately the shape the real lines will have rather than a
toy: two nodes, two *different* models, state carried in a typed dict, and a
SQLite checkpointer.

Heterogeneous per-node models are the point. The old stack could only do this
with per-adapter patching; here it is a field on the node spec, because model
choice resolves at the gateway (invariant 3).

This module is one of the few allowed to import langgraph -- invariant 1 keeps
the framework inside graphs/ and executors/, and out of anything that lands in
a work folder.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from fleet_graph.executors.text_node import TextNode, TextSpec


class HelloState(TypedDict, total=False):
    topic: str
    draft: str
    critique: str
    usage: list[dict[str, Any]]


@dataclass(frozen=True)
class HelloConfig:
    """Which logical model plays which part. Names only -- never keys."""

    drafter: str = "deepseek-v4-flash"
    critic: str = "glm-4.6"
    # Generous on purpose: reasoning models spend most of the budget on
    # reasoning tokens, and at 512 glm-4.6 emitted no visible text at all.
    max_tokens: int = 2048


def build_hello_graph(node: TextNode, config: HelloConfig | None = None) -> StateGraph:
    config = config or HelloConfig()

    def draft(state: HelloState) -> HelloState:
        result = node.complete(
            TextSpec(
                model=config.drafter,
                system="You are terse. Answer in at most two sentences.",
                max_tokens=config.max_tokens,
            ),
            f"Explain: {state['topic']}",
        )
        return {"draft": result.text, "usage": [*state.get("usage", []), _usage(result)]}

    def critique(state: HelloState) -> HelloState:
        result = node.complete(
            TextSpec(
                model=config.critic,
                system="You are a terse critic. One sentence. Say what is missing.",
                max_tokens=config.max_tokens,
            ),
            f"Critique this explanation of {state['topic']}:\n\n{state['draft']}",
        )
        return {
            "critique": result.text,
            "usage": [*state.get("usage", []), _usage(result)],
        }

    graph: StateGraph = StateGraph(HelloState)
    graph.add_node("draft", draft)
    graph.add_node("critique", critique)
    graph.add_edge(START, "draft")
    graph.add_edge("draft", "critique")
    graph.add_edge("critique", END)
    return graph


def _usage(result: Any) -> dict[str, Any]:
    return {"model": result.model, "usage": result.usage}


__all__ = ["HelloConfig", "HelloState", "build_hello_graph"]
