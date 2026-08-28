"""B2 automatic adoption: idempotent adoption of in-flight/recoverable work.

When eligible in-flight or recoverable work is discovered, the governed
workflow adopts it rather than waiting for a manual bookkeeping intervention.
The one property that makes adoption safe because it makes it mechanical is
*idempotency*: adopting the same discovery twice must not create a second
adopted record, and must not fork the work's history.

Identity is the signature. A discovery is identified by its ``signature`` --
a deterministic string the discoverer computes over the work it found, not a
random id -- so a replayed discovery *is* the same discovery. The ledger keys
its records by that signature, and the record's digest is computed over the
adopted facts, so a replay produces the same record (same digest, same
sequence) and a second ``adopt`` call simply returns it. Nothing is duplicated,
nothing forks.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from fleet_graph.dd.upstream_constants import compute_json_digest

#: The two shapes of eligible work the ledger knows how to adopt.
KIND_IN_FLIGHT = "in_flight"
KIND_RECOVERABLE = "recoverable"
KINDS = frozenset({KIND_IN_FLIGHT, KIND_RECOVERABLE})

#: What produced an adoption record. Stored on the record itself (and bound by
#: its digest) so a downstream evidence link can assert the artifact was produced
#: by this exact mechanism rather than re-typing the name at the assertion site.
ADOPTION_MECHANISM = "AdoptionLedger.adopt"


class AdoptionError(RuntimeError):
    """A discovery cannot be adopted as asked. Nothing is guessed."""


@dataclass(frozen=True)
class Discovery:
    """What was found: eligible in-flight or recoverable work.

    ``signature`` is the whole identity -- deterministic and replay-stable, so a
    replayed discovery is the same discovery. ``source`` is where it was found,
    for the audit trail, not for identity.
    """

    signature: str
    kind: str
    source: str = ""


@dataclass(frozen=True)
class AdoptionRecord:
    """One adopted work item, sealed by digest.

    The digest is computed over (signature, kind, source, target_ref,
    mechanism) -- the adopted facts, not the sequence -- so a replay hashes
    identically. ``mechanism`` names what produced the record; it is stored on
    the artifact itself so an evidence link reads it instead of re-typing it.
    """

    signature: str
    kind: str
    source: str
    target_ref: str
    digest: str
    sequence: int
    mechanism: str = ADOPTION_MECHANISM

    def as_dict(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "kind": self.kind,
            "source": self.source,
            "target_ref": self.target_ref,
            "digest": self.digest,
            "sequence": self.sequence,
            "mechanism": self.mechanism,
        }


def record_digest(
    signature: str, kind: str, source: str, target_ref: str, mechanism: str = ADOPTION_MECHANISM
) -> str:
    return compute_json_digest(
        {
            "signature": signature,
            "kind": kind,
            "source": source,
            "target_ref": target_ref,
            "mechanism": mechanism,
        }
    )


class AdoptionLedger:
    """The governed registry of adopted work, idempotent by signature.

    Deliberately no notion of "close" or "fix up": the ledger only records that
    work was adopted, bound to an immutable target ref and sealed by digest. A
    replay -- discover, adopt, discover again -- sees the same record and does
    nothing new.
    """

    def __init__(self, records: Iterable[AdoptionRecord] = ()) -> None:
        self._by_signature: dict[str, AdoptionRecord] = {}
        self._order: list[str] = []
        for record in records:
            self._by_signature[record.signature] = record
            if record.signature not in self._order:
                self._order.append(record.signature)

    def discover(self, discoveries: Iterable[Discovery]) -> list[Discovery]:
        """The not-yet-adopted subset, in given order. Pure; adopts nothing."""
        return [item for item in discoveries if item.signature not in self._by_signature]

    def adopt(self, discovery: Discovery, target_ref: str) -> AdoptionRecord:
        """Adopt one discovery, or return the existing record on replay."""
        if not discovery.signature:
            raise AdoptionError("a discovery needs a deterministic signature to adopt by")
        if discovery.kind not in KINDS:
            raise AdoptionError(f"kind must be one of {sorted(KINDS)}, got {discovery.kind!r}")
        if not target_ref:
            raise AdoptionError("adoption must bind an immutable target reference")

        existing = self._by_signature.get(discovery.signature)
        if existing is not None:
            return existing

        record = AdoptionRecord(
            signature=discovery.signature,
            kind=discovery.kind,
            source=discovery.source,
            target_ref=target_ref,
            digest=record_digest(
                discovery.signature,
                discovery.kind,
                discovery.source,
                target_ref,
                ADOPTION_MECHANISM,
            ),
            sequence=len(self._order) + 1,
        )
        self._by_signature[record.signature] = record
        self._order.append(record.signature)
        return record

    def records(self) -> tuple[AdoptionRecord, ...]:
        return tuple(self._by_signature[signature] for signature in self._order)

    def is_adopted(self, signature: str) -> bool:
        return signature in self._by_signature

    def __len__(self) -> int:
        return len(self._order)


__all__ = [
    "ADOPTION_MECHANISM",
    "KINDS",
    "KIND_IN_FLIGHT",
    "KIND_RECOVERABLE",
    "AdoptionError",
    "AdoptionLedger",
    "AdoptionRecord",
    "Discovery",
    "record_digest",
]
