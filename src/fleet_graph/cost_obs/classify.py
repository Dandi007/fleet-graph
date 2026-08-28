"""Token attribution classification for the cost-observability data plane.

Attribution is a three-way distinction, and the three ways are deliberately
not the same thing:

- **known**: the token spend maps to a recorded lifecycle class
  (`management`, `launch`, `review`, `promotion`, `settlement`).
- **unknown**: tokens flowed, but nothing attributes them to any class. They
  are kept as an explicit bucket -- never silently relabelled as a known
  class, because that would turn "we could not attribute this" into "this was
  X", which is a lie in the other direction.
- **missing**: a lifecycle class whose source-fact *producer* emitted nothing
  at all. That is absence of a fact, not absence of attribution, and it is
  accounted for separately (a bounded zero-compatible presence series) rather
  than folded into `unknown`.

The classifier only ever decides the first line: a token record either names a
known class or it is unknown. Missing is a producer-side property and lives in
the data plane (`data_plane.py`), not here -- a classifier cannot tell absent
from unattributed without pretending to know what never arrived.
"""

from __future__ import annotations

from dataclasses import dataclass

MANAGEMENT = "management"
LAUNCH = "launch"
REVIEW = "review"
PROMOTION = "promotion"
SETTLEMENT = "settlement"

#: The lifecycle classes a token spend can be attributed to.
KNOWN_CLASSES = frozenset({MANAGEMENT, LAUNCH, REVIEW, PROMOTION, SETTLEMENT})

#: The explicit bucket for tokens that genuinely lack attribution.
UNKNOWN = "unknown"


@dataclass(frozen=True)
class TokenRecord:
    """One token spend event: how many tokens, and which class -- or none."""

    tokens: float
    attribution: str | None = None


def classify_tokens(records: list[TokenRecord]) -> dict[str, float]:
    """Bucket token spend into per-class totals plus an explicit unknown bucket.

    An `attribution` of ``None`` -- and, defensively, any value not in
    `KNOWN_CLASSES` -- lands in `unknown`. The result always carries every
    known class key and `unknown`, at zero when nothing contributed, so a
    consumer can always tell "zero known" from "bucket absent from the model".
    """
    buckets: dict[str, float] = {klass: 0.0 for klass in sorted(KNOWN_CLASSES)}
    buckets[UNKNOWN] = 0.0
    for record in records:
        if record.attribution in KNOWN_CLASSES:
            buckets[record.attribution] += record.tokens
        else:
            buckets[UNKNOWN] += record.tokens
    return buckets


__all__ = [
    "KNOWN_CLASSES",
    "LAUNCH",
    "MANAGEMENT",
    "PROMOTION",
    "REVIEW",
    "SETTLEMENT",
    "UNKNOWN",
    "TokenRecord",
    "classify_tokens",
]
