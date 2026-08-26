"""Comparing the vendored contracts against what production is pinned to.

Skipped where the plugin checkout is not present -- CI, for one -- because the
point is to catch divergence on the machine that does the vendoring, not to
require the plugin repo to build this one.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from fleet_graph.dd.capability import CONTRACTS_DIR

DD_CONFIG = Path.home() / ".config/loop-engine-development-mcp/config.yaml"
DIGEST_LINE = re.compile(r"^\s+(?:contracts/)?([\w.-]+\.json):\s+(sha256:[0-9a-f]{64})\s*$")
NAMED = {
    "lifecycle_digest": "development-lifecycle.json",
    "artifact_digest": "stage-artifacts.json",
}


def pinned_digests() -> dict[str, str]:
    """The digests production's binding pins, read out of the live config."""
    found: dict[str, str] = {}
    for line in DD_CONFIG.read_text(encoding="utf-8").splitlines():
        match = DIGEST_LINE.match(line)
        if match:
            found[match.group(1)] = match.group(2)
            continue
        named = re.match(r"^\s+(\w+_digest):\s+(sha256:[0-9a-f]{64})\s*$", line)
        if named and named.group(1) in NAMED:
            found[NAMED[named.group(1)]] = named.group(2)
    return found


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


needs_live_config = pytest.mark.skipif(
    not DD_CONFIG.is_file(), reason="production dd config is not on this machine"
)


@needs_live_config
def test_every_pinned_contract_matches_the_vendored_copy() -> None:
    """The machine this repo walks must be the machine production walks."""
    pinned = pinned_digests()
    assert pinned, "no digests parsed out of the live config"

    differing = []
    for name, expected in sorted(pinned.items()):
        local = CONTRACTS_DIR / name
        if not local.is_file():
            differing.append(f"{name}: not vendored")
        elif digest(local) != expected:
            differing.append(f"{name}: {digest(local)} != pinned {expected}")
    assert not differing, differing


@needs_live_config
def test_the_pinned_set_covers_what_the_pipeline_validates_against() -> None:
    pinned = set(pinned_digests())
    for required in (
        "stage-dispatch.schema.json",
        "attempt-context.schema.json",
        "development-lifecycle.json",
        "stage-artifacts.json",
    ):
        assert required in pinned, required


def test_the_vendored_bundle_is_self_consistent() -> None:
    """Runs everywhere: this is the check a dispatch is actually gated on."""
    from fleet_graph.dd.capability import CapabilityLock

    assert CapabilityLock.load().require().ok


def test_the_manifest_names_the_protocol_the_contracts_declare() -> None:
    manifest = json.loads(
        (CONTRACTS_DIR / "attempt-context-capability.json").read_text(encoding="utf-8")
    )
    lifecycle = json.loads(
        (CONTRACTS_DIR / "development-lifecycle.json").read_text(encoding="utf-8")
    )
    assert manifest["protocol"] == lifecycle["capabilities"]["attempt_context"]["protocol"]
