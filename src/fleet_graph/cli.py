"""Thin CLI entrypoint.

P0 only exposes `version`; graph/scheduler subcommands land in P2/P6.
"""

from __future__ import annotations

import argparse
import sys

from fleet_graph import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet-graph")
    parser.add_argument("--version", action="version", version=__version__)
    parser.set_defaults(func=lambda _: 0)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
