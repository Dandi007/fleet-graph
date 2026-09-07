"""fleet_graph.minimal — the LoopX minimal-system package.

Each DD adds its own module here; modules from different DDs are intentionally
not imported by one another. The mechanically-checked git gates for agent
handoffs (``gitgate``) live in this package and are importable without any
sibling module being present.
"""
