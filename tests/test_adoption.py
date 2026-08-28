"""B2 automatic adoption. Idempotent by signature: replay cannot fork."""

from __future__ import annotations

import pytest

from fleet_graph.dd.adoption import (
    KIND_IN_FLIGHT,
    KIND_RECOVERABLE,
    AdoptionError,
    AdoptionLedger,
    Discovery,
    record_digest,
)


class TestIdempotentAdoption:
    def test_replaying_the_same_discovery_returns_one_record(self) -> None:
        ledger = AdoptionLedger()
        discovery = Discovery(signature="dev-x:g2", kind=KIND_IN_FLIGHT, source="replay-source")
        first = ledger.adopt(discovery, target_ref="abc123")
        again = ledger.adopt(discovery, target_ref="abc123")

        assert again == first
        assert len(ledger) == 1
        assert len(ledger.records()) == 1

    def test_a_replay_does_not_fork_the_digest(self) -> None:
        discovery = Discovery(signature="sig", kind=KIND_RECOVERABLE, source="s")
        assert record_digest(
            discovery.signature, discovery.kind, discovery.source, "ref1"
        ) == record_digest(discovery.signature, discovery.kind, discovery.source, "ref1")

    def test_discover_returns_only_not_yet_adopted_work(self) -> None:
        ledger = AdoptionLedger()
        a = Discovery(signature="a", kind=KIND_IN_FLIGHT)
        b = Discovery(signature="b", kind=KIND_RECOVERABLE)
        ledger.adopt(a, target_ref="t1")

        pending = ledger.discover([a, b])
        assert [item.signature for item in pending] == ["b"]
        assert ledger.is_adopted("a")
        assert not ledger.is_adopted("b")

    def test_distinct_discoveries_adopt_separately_and_in_order(self) -> None:
        ledger = AdoptionLedger()
        a = ledger.adopt(Discovery(signature="a", kind=KIND_IN_FLIGHT), "t1")
        b = ledger.adopt(Discovery(signature="b", kind=KIND_RECOVERABLE), "t2")
        assert [record.sequence for record in ledger.records()] == [1, 2]
        assert ledger.records() == (a, b)


class TestRefusals:
    def test_a_discovery_without_a_signature_refuses(self) -> None:
        with pytest.raises(AdoptionError, match="signature"):
            AdoptionLedger().adopt(Discovery(signature="", kind=KIND_IN_FLIGHT), "t")

    def test_an_unknown_kind_refuses(self) -> None:
        with pytest.raises(AdoptionError, match="kind"):
            AdoptionLedger().adopt(Discovery(signature="s", kind="other"), "t")

    def test_an_adoption_without_a_target_ref_refuses(self) -> None:
        with pytest.raises(AdoptionError, match="target"):
            AdoptionLedger().adopt(Discovery(signature="s", kind=KIND_IN_FLIGHT), "")

    def test_a_ledger_restored_from_records_stays_idempotent(self) -> None:
        discovery = Discovery(signature="s", kind=KIND_IN_FLIGHT, source="s")
        original = AdoptionLedger()
        first = original.adopt(discovery, "t")
        restored = AdoptionLedger(original.records())
        assert restored.adopt(discovery, "t") == first
        assert len(restored) == 1
