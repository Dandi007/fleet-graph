"""Thin CLI entrypoint.

`version`, `hello`, and `line run`. The scheduler daemon lands in P6.
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


def _line_run(args: argparse.Namespace) -> int:
    """Run one ronin line to termination, printing its terminal record."""
    import pathlib

    from fleet_graph.graphs.runner import LineConfig, run_line

    config = LineConfig(
        folder_id=args.folder,
        seat=args.seat,
        run_root=pathlib.Path(args.run_root or f"/data/fleet-graph/runs/{args.folder}"),
        max_rounds=args.max_rounds,
        noop_limit=args.noop_limit,
        timeout_limit=args.timeout_limit,
        turn_timeout_seconds=args.turn_timeout,
        coordinator_timeout_seconds=args.coordinator_timeout,
        alias=args.alias,
        checkpoint_path=args.checkpoint or ":memory:",
    )
    result = run_line(config, run_id=args.run_id)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
    # A line that ended `done` is not the same as a line that ended well; the
    # exit code reports termination, and acceptance stays a human's job.
    return 0 if result.get("terminal") in {"done", "blocked", "bounds"} else 1


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

    line = subparsers.add_parser("line", help="run a ronin line")
    line_sub = line.add_subparsers()
    run = line_sub.add_parser("run", help="run one line to termination")
    run.add_argument("--folder", required=True, help="work folder id")
    run.add_argument("--seat", required=True, help="worker seat from agents.yaml")
    run.add_argument("--run-root", default=None)
    run.add_argument("--max-rounds", type=int, default=10)
    run.add_argument("--noop-limit", type=int, default=3)
    run.add_argument("--timeout-limit", type=int, default=2)
    run.add_argument("--turn-timeout", type=int, default=3000)
    run.add_argument("--coordinator-timeout", type=int, default=2700)
    run.add_argument("--alias", default=None, help="agent-bus inbox alias")
    run.add_argument("--checkpoint", default=None)
    run.add_argument("--run-id", default=None)
    run.set_defaults(func=_line_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
