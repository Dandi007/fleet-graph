"""Guards the package layout the architecture doc promises."""

import importlib

import pytest

EXPECTED_PACKAGES = [
    "fleet_graph.executors",
    "fleet_graph.bus",
    "fleet_graph.state",
    "fleet_graph.graphs",
]


@pytest.mark.parametrize("name", EXPECTED_PACKAGES)
def test_package_importable(name: str) -> None:
    assert importlib.import_module(name)
