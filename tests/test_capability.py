"""Capability lock. Every ambiguous case must refuse, not allow."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from fleet_graph.dd.capability import (
    CONTRACTS_DIR,
    MANIFEST_NAME,
    CapabilityError,
    CapabilityLock,
    file_digest,
)


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """A private copy of the real contract bundle, safe to tamper with."""
    target = tmp_path / "contracts"
    shutil.copytree(CONTRACTS_DIR, target)
    return target


class TestTheRealBundleVerifies:
    def test_the_vendored_contracts_match_their_pins(self) -> None:
        """Evidence the vendoring was faithful: byte-identical to what dd pins."""
        result = CapabilityLock.load().verify()
        assert result.ok
        assert len(result.matched) == 10
        assert not result.mismatched and not result.missing

    def test_manifest_identifies_its_protocol(self) -> None:
        lock = CapabilityLock.load()
        assert lock.protocol == "dev-dispatch.attempt-context/v1"
        assert lock.manifest_version.startswith("dev-dispatch.attempt-context-capability/")


class TestFailClosed:
    """A verifier that allows when confused is a delay, not a control."""

    def test_a_tampered_contract_is_caught(self, bundle: Path) -> None:
        target = bundle / "development-lifecycle.json"
        contract = json.loads(target.read_text())
        contract["transitions"].append(
            {"from": "implement", "on": "success", "to": "merger", "next_mode": "inherit"}
        )
        target.write_text(json.dumps(contract))

        result = CapabilityLock.load(bundle).verify()
        assert not result.ok
        assert "contracts/development-lifecycle.json" in result.mismatched

    def test_even_a_whitespace_change_is_caught(self, bundle: Path) -> None:
        target = bundle / "development-lifecycle.json"
        target.write_text(target.read_text() + "\n")
        assert not CapabilityLock.load(bundle).verify().ok

    def test_a_removed_contract_is_caught(self, bundle: Path) -> None:
        (bundle / "development-lifecycle.json").unlink()
        result = CapabilityLock.load(bundle).verify()
        assert "contracts/development-lifecycle.json" in result.missing
        assert not result.ok

    def test_require_raises_rather_than_returning_false(self, bundle: Path) -> None:
        (bundle / "attempt-context.schema.json").unlink()
        with pytest.raises(CapabilityError, match="refusing to dispatch"):
            CapabilityLock.load(bundle).require()

    def test_an_unreadable_manifest_refuses(self, tmp_path: Path) -> None:
        (tmp_path / MANIFEST_NAME).write_text("{ not json")
        with pytest.raises(CapabilityError, match="cannot read capability manifest"):
            CapabilityLock.load(tmp_path)

    def test_a_missing_manifest_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(CapabilityError, match="cannot read capability manifest"):
            CapabilityLock.load(tmp_path)

    def test_an_empty_allowlist_refuses(self, tmp_path: Path) -> None:
        """Pinning nothing verifies nothing; that must not read as success."""
        (tmp_path / MANIFEST_NAME).write_text(json.dumps({"manifest_version": "x"}))
        with pytest.raises(CapabilityError, match="pins no files"):
            CapabilityLock.load(tmp_path).verify()

    def test_an_unknown_algorithm_refuses(self, bundle: Path) -> None:
        """Not recognising the algorithm means not having verified it."""
        manifest_path = bundle / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text())
        manifest["lifecycle"]["digest"] = "md5:" + "0" * 32
        manifest_path.write_text(json.dumps(manifest))
        with pytest.raises(CapabilityError, match="unsupported algorithm"):
            CapabilityLock.load(bundle).verify()

    def test_file_digest_rejects_unknown_algorithms(self, tmp_path: Path) -> None:
        target = tmp_path / "x"
        target.write_text("hello")
        with pytest.raises(CapabilityError, match="unsupported digest algorithm"):
            file_digest(target, "md5")


class TestResultShape:
    def test_a_result_with_nothing_matched_is_not_ok(self) -> None:
        from fleet_graph.dd.capability import VerificationResult

        assert VerificationResult().ok is False
