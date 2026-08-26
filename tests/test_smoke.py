"""P0 smoke tests: package imports and the CLI contract holds."""

import pytest

import fleet_graph
from fleet_graph.cli import build_parser, main


def test_version_is_exported() -> None:
    assert fleet_graph.__version__


def test_parser_knows_version() -> None:
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["--version"])
    assert excinfo.value.code == 0


def test_main_returns_zero() -> None:
    assert main([]) == 0
