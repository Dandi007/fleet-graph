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

from fleet_graph.dd.upstream_constants import compute_json_digest

#: The verdict that steers a chain into its rework edge. The lifecycle
#: contract's REJECT transitions are the only edges that re-enter implement,
#: so a link whose predecessor ended REJECT is a rework link by construction.
REJECT = "REJECT"


def is_rework_link(previous_verdict: str) -> bool:
    """Whether the link after a receipt with this verdict is a rework edge."""
    return previous_verdict == REJECT


def rework_link_parent(rejecting_receipt: dict[str, Any]) -> str:
    """The parent digest a rework implement names: the rejecting review's
    canonical-JSON digest, computed over the receipt object itself."""
    return compute_json_digest(dict(rejecting_receipt))


def new_attempt_is_legal(entries: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> bool:
    """Whether a fresh continuous review -- a brand-new attempt -- may follow `entries`.

    Mirrors the pinned plugin's ordering rule (`attempt-context.py:
    check_chain_order` / `protocol_review_attempt`): a fresh continuous entry is
    a *new attempt*, legal only as the very first entry or the entry right after
    a REJECT. Any other predecessor -- in particular a final APPROVE, an accepted
    chain that did not end in REJECT -- makes a fresh continuous review illegal
    (ORDER_VIOLATION "a new attempt requires a prior REJECT").

    The replayer consults this before replaying a prefix that stops at implement:
    such a replay is always followed by a fresh continuous review, so it is only
    safe when that review is a legal new attempt. This is deliberately the same
    single source `supervise/audit.py`'s chain check shares, so the replay side
    and the audit side cannot drift apart.
    """
    if not entries:
        return True
    return entries[-1].get("verdict") == REJECT


__all__ = ["REJECT", "is_rework_link", "new_attempt_is_legal", "rework_link_parent"]
