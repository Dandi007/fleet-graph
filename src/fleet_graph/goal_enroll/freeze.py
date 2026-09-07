"""The M4 acceptance-command freeze face (design.md §6.1 / §8).

A goal line's acceptance commands are pinned at enlistment: the goal
carrier's ```dd-acceptance block is hashed, the digest is recorded on the
roster entry, and from then on *changing the acceptance commands means
re-enlisting*. Two readers consume the pin:

- ``goal_status`` exposes ``acceptance`` / ``acceptance_digest`` per
  enlisted goal, recomputes the carrier digest, and reports the structured
  code ``ACCEPTANCE_DIGEST_MISMATCH`` when they disagree.
- the scheduler refuses to ignite a line whose carrier digest drifted
  (``Refusal.ACCEPTANCE_DIGEST_MISMATCH``) -- the executed commands stay
  the roster's declared argv (R0d); the freeze is the drift tripwire that
  says "your carrier no longer says what was enlisted".

A goal enlisted before the pin existed has no pinned digest: the freeze
fails open for it (grandfathered lines keep running), because a missing pin
can never *mismatch* and a freeze that locks every pre-M4 line shut would
be a fleet-wide outage disguised as discipline.
"""

from __future__ import annotations

import hashlib

from fleet_graph.dd.control_plane import ACCEPTANCE_FENCE

#: The structured code both readers report on a carrier/pin disagreement.
ACCEPTANCE_DIGEST_MISMATCH = "ACCEPTANCE_DIGEST_MISMATCH"


def acceptance_block(goal_md: str) -> str | None:
    """The exact ```dd-acceptance fenced block text, or None when absent.

    The digest hashes the block *verbatim* (fences included), so any edit --
    a new command, a changed flag, reformatting -- moves it. Whitespace
    outside the block is irrelevant, whitespace inside is content.
    """
    match = ACCEPTANCE_FENCE.search(str(goal_md or ""))
    if match is None:
        return None
    return match.group(0)


def acceptance_block_digest(goal_md: str) -> str | None:
    """``sha256:<hex>`` of the carrier's dd-acceptance block, or None.

    None means "no block to pin" -- an unpinnable carrier fails open at
    every consumer, never locks a line shut.
    """
    block = acceptance_block(goal_md)
    if block is None:
        return None
    digest = hashlib.sha256(block.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


__all__ = [
    "ACCEPTANCE_DIGEST_MISMATCH",
    "acceptance_block",
    "acceptance_block_digest",
]
