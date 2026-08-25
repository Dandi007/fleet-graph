"""Thin CLI entrypoint.

`version` and `hello` only; the scheduler and graph servers land in P6.
"""

from __future__ import annotations

import argparse
import json
import sys

from fleet_graph import __version__


def _hello(args: argparse.Namespace) -> int:
    """Run hello-graph for real, through the gateway."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    from fleet_graph.executors.text_node import TextNode
    from fleet_graph.graphs.hello import HelloConfig, build_hello_graph

    node = TextNode()
    graph = build_hello_graph(node, HelloConfig(drafter=args.drafter, critic=args.critic))

    with SqliteSaver.from_conn_string(args.checkpoint) as saver:
        compiled = graph.compile(checkpointer=saver)
        state = compiled.invoke(
            {"topic": args.topic},
            config={"configurable": {"thread_id": args.thread}},
        )

    json.dump(
        {
            "topic": state.get("topic"),
            "draft": state.get("draft"),
            "critique": state.get("critique"),
            "usage": state.get("usage", []),
        },
        sys.stdout,
        ensure_ascii=False,
        indent=1,
    )
    sys.stdout.write("\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet-graph")
    parser.add_argument("--version", action="version", version=__version__)
    parser.set_defaults(func=lambda _: 0)

    subparsers = parser.add_subparsers()
    hello = subparsers.add_parser("hello", help="run hello-graph through the gateway")
    hello.add_argument("--topic", default="what a work-stealing scheduler is")
    hello.add_argument("--drafter", default="deepseek-v4-flash")
    hello.add_argument("--critic", default="glm-4.6")
    hello.add_argument("--thread", default="hello-1")
    hello.add_argument("--checkpoint", default=":memory:")
    hello.set_defaults(func=_hello)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
