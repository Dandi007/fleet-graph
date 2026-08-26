"""The four upstream values the vendored plugin adapter needs.

`plugin_adapter.py` reaches into three loop-engine modules for what amounts to
two constants, an enum with two members, and eight lines of canonical-JSON
digesting. Vendoring `config.py` (380 lines), `handoff.py` (2171) and
`models.py` (1111) to get them would be absurd; so would editing the vendored
file to inline them, because then it stops diffing cleanly against upstream.

So they live here, restated, with a test that reads them back out of the
upstream sources and fails if either side moves. That test is the point: a
restated constant that nobody checks is a constant waiting to drift.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

# loop_engine_development_mcp/handoff.py
HANDOFF_CONTRACT_VERSION = 1

# loop_engine_development_mcp/models.py
ATTEMPT_CONTEXT_CONTRACT_VERSION = "dev-dispatch.attempt-context/v1"


class ReviewPhase(StrEnum):
    """loop_engine_development_mcp/models.py"""

    CONTINUOUS = "continuous"
    FINAL = "final"


def canonical_json(obj: Any) -> str:
    """RFC 8785 / JCS-equivalent canonical JSON -- upstream config.py, verbatim.

    Every digest computed against a plugin bundle routes through this, so the
    serialisation has to match upstream byte for byte: sorted keys, no
    insignificant whitespace, UTF-8 preserved, minimal separators. A digest
    computed over differently-spelled JSON is a digest of something else.
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_digest(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def compute_json_digest(obj: Any) -> str:
    return compute_digest(canonical_json(obj))


__all__ = [
    "ATTEMPT_CONTEXT_CONTRACT_VERSION",
    "HANDOFF_CONTRACT_VERSION",
    "ReviewPhase",
    "canonical_json",
    "compute_digest",
    "compute_json_digest",
]
