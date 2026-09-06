"""fleet_graph.minimal — the LoopX minimal-system protocol layer.

Holds the Stop schema table and envelope validator the rebuilt engine's nodes
reuse to grade every agent output as exactly one valid JSON object, plus the
mechanically-checked git gates for agent handoffs (``gitgate``). Each DD adds
its own module here; modules from different DDs are intentionally not
imported by one another.
"""

from fleet_graph.minimal.protocol import (
    ENVELOPE_KEYS,
    SCHEMA_GOAL_REVIEW,
    SCHEMA_GOAL_TURN,
    SCHEMA_IMPL,
    SCHEMA_MERGE,
    SCHEMA_NAMES,
    SCHEMA_REVIEW,
    SCHEMA_RUNTIME_ERROR,
    SCHEMA_SCRIBE,
    ValidationResult,
    describe_schema,
    extract_protocol_object,
    validate,
)

__all__ = [
    "ENVELOPE_KEYS",
    "SCHEMA_GOAL_REVIEW",
    "SCHEMA_GOAL_TURN",
    "SCHEMA_IMPL",
    "SCHEMA_MERGE",
    "SCHEMA_NAMES",
    "SCHEMA_REVIEW",
    "SCHEMA_RUNTIME_ERROR",
    "SCHEMA_SCRIBE",
    "ValidationResult",
    "describe_schema",
    "extract_protocol_object",
    "validate",
]
