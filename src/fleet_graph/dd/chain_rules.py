"""dd receipt 链的成链规则——单一出处，供走链的两侧共用。

链上每一段的 parent digest 规则（生产测得，dev-fg-4628ef887564 /
dev-fg-369dacf607c1）：

- **常规段**认前驱的封存 digest：前驱封了 receipt 文件就是文件字节的
  sha256（sealer 逐字节重读那份文件），script 阶段没有文件则是内存 receipt
  的 canonical-JSON digest（dispatch builder 就是这么算的）。
- **rework 段**——被 REJECT 打回后的重做 implement——认拒绝它的 review
  receipt 的 **canonical-JSON digest**：walker 在 rework 边上携带的是内存
  receipt，没有一份"下一段的文件"可认字节。

这两条规则先在 `graphs/dd_replay.py` 的链走查里实现并测过（#105），后在
`supervise/audit.py` 的链检查里需要同一套。规则写在这里一份，两侧引用，
避免两处各自转述后漂移——audit 曾因未建模 rework 段把合法链判红
（dev-fg-369dacf607c1: "rev5 implement: parent … != expected …"）。
"""

from __future__ import annotations

from typing import Any

from fleet_graph.dd.dispatch import derive_attempt_id
from fleet_graph.dd.upstream_constants import compute_json_digest

#: The verdict that steers a chain into its rework edge. The lifecycle
#: contract's REJECT transitions are the only edges that re-enter implement,
#: so a link whose predecessor ended REJECT is a rework link by construction.
REJECT = "REJECT"

#: The two walker events a chain record's `verdict` can carry for the retry
#: edge: a failed signing (the run ended in the walker's failure event) and
#: the spine event a completed run seals. Local mirrors of
#: `graphs.dd_pipeline.FAILURE_EVENT` / `SPINE_EVENT` -- the same mirroring
#: discipline as REJECT above, so this rule module stays free of graph-layer
#: imports. The X-4 measured path (dev-fg-d9370430e0ce rev4 -> rev5) sealed
#: exactly these two words onto one receipt.
FAILED = "failed"
SUCCESS = "success"

#: Local mirror of `dd_replay.MAX_WALK_ATTEMPTS`. The attempt identity a review
#: entry carries is uuid5 (one-way), so recognising which generation a committed
#: entry was sealed for is a bounded forward search, not a reverse of the
#: derivation.
_MAX_ATTEMPTS = 64


def is_rework_link(previous_verdict: str) -> bool:
    """Whether the link after a receipt with this verdict is a rework edge."""
    return previous_verdict == REJECT


def rework_link_parent(rejecting_receipt: dict[str, Any]) -> str:
    """The parent digest a rework implement names: the rejecting review's
    canonical-JSON digest, computed over the receipt object itself."""
    return compute_json_digest(dict(rejecting_receipt))


def is_retry_link(previous_verdict: str) -> bool:
    """Whether the link after a receipt with this verdict may be a retry edge.

    A failed signing is not a chain break by itself: the engine re-prepares
    the handoff and re-runs the *same* attempt (same attempt_id, same receipt
    identity), so the next record can be the second signing of one receipt
    rather than a new link. The rework edge models a REJECT's re-entry into
    implement; this models a failed attempt's re-run -- the X-4 path the
    audit went red on before it was modelled (dev-fg-d9370430e0ce
    rev4 -> rev5, "parent … != expected …" on a chain that never broke).
    """
    return previous_verdict == FAILED


def retry_link_parent(previous: dict[str, Any]) -> str:
    """The parent digest the retry signing names: the re-prepare handoff
    digest the failed signing already named -- its own parent, unchanged.
    The retry re-enters the same handoff; it does not advance the chain."""
    return str(previous.get("parent_handoff_receipt_digest") or "")


def entry_generation(entry: dict[str, Any], development_id: str, generation: int) -> bool:
    """Whether `entry`'s attempt identity was derived for `generation`.

    A committed feedback entry carries the durable `attempt_id` the engine
    derived from `(development_id, generation, attempt)`. Because that id is a
    uuid5, membership is a bounded forward search over attempts, never a reverse
    of the derivation, and an entry that names no recognisable id (or none at
    all) simply does not belong to `generation`.
    """
    attempt_id = entry.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        return False
    return any(
        derive_attempt_id(development_id, generation, attempt) == attempt_id
        for attempt in range(1, _MAX_ATTEMPTS + 1)
    )


def split_entries(
    entries: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    generation: int,
    development_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition `entries` into (current generation, older generation).

    The durable `attempt_id` each entry carries is the engine's own derivation
    of `(development_id, generation, attempt)`, so an entry either belongs to
    `generation` or it is immutable history from an older generation. This is
    the single source the new-generation index scoping
    (``dd.feedback_scope``) uses to move older entries out of the live index
    without rewriting or discarding them.
    """
    own: list[dict[str, Any]] = []
    inherited: list[dict[str, Any]] = []
    for entry in entries:
        (own if entry_generation(entry, development_id, generation) else inherited).append(entry)
    return own, inherited


def new_attempt_is_legal(
    entries: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    generation: int | None = None,
    development_id: str | None = None,
) -> bool:
    """Whether a fresh continuous review -- a brand-new attempt -- may follow `entries`.

    Mirrors the pinned plugin's ordering rule (`attempt-context.py:
    check_chain_order` / `protocol_review_attempt`): within a single attempt
    chain, a fresh continuous entry is a *new attempt*, legal only as the very
    first entry or the entry right after a REJECT. Any other predecessor -- in
    particular a final APPROVE, an accepted chain that did not end in REJECT --
    makes a fresh continuous review illegal (ORDER_VIOLATION "a new attempt
    requires a prior REJECT").

    The rule is generation-aware. When `generation` and `development_id` are
    both supplied, only the entries whose durable attempt identity was derived
    for *that* generation count; entries from an older generation are immutable
    history and must not be misread as the current generation's attempt order.
    A fresh attempt in a new generation therefore has no same-generation
    predecessor to satisfy, and is legal: the historical records are preserved
    unchanged, and the rejection requirement for a genuinely new attempt still
    holds *within* one chain.

    The replayer consults this before replaying a prefix that stops at implement:
    such a replay is always followed by a fresh continuous review, so it is only
    safe when that review is a legal new attempt. This is deliberately the same
    single source `supervise/audit.py`'s chain check shares, so the replay side
    and the audit side cannot drift apart.
    """
    if not entries:
        return True
    if generation is not None and development_id:
        entries = [
            entry for entry in entries if entry_generation(entry, development_id, generation)
        ]
        if not entries:
            return True
    return entries[-1].get("verdict") == REJECT


__all__ = [
    "FAILED",
    "REJECT",
    "SUCCESS",
    "entry_generation",
    "is_retry_link",
    "is_rework_link",
    "new_attempt_is_legal",
    "retry_link_parent",
    "rework_link_parent",
    "split_entries",
]
