"""Wiring the cost-observability data plane into the dev-dispatch lifecycle.

The data plane (``fleet_graph.cost_obs``) is producer-shaped: it emits the
labelled source facts the five ``cost-observability`` recording rules consume.
This module is the DD-side half of that wiring -- the half that knows where the
rendered exposition belongs and how a dev-dispatch run obtains a data plane:

- ``FLEET_GRAPH_COST_OBS_DIR`` names the node_exporter textfile collector
  directory the run should render ``cost-obs.prom`` into. The scrape side of
  that path lives in ``deploy/prometheus/cost-observability.yml``, so the
  producer and the scraper agree on the same directory by construction.
- ``build_cost_plane`` constructs the data plane, or returns ``None`` when no
  directory is configured. A dev-dispatch run that is not wired to a scrape
  path therefore simply does not collect -- it does not fail.

The lifecycle facts themselves are emitted by the components that own them:
``graphs/dd_actors.py`` (launch + review), ``graphs/dd_scripts.py``
(promotion), and the pipeline walker (settlement). This module deliberately
does not restate those mappings a second time.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from fleet_graph.cost_obs import CostDataPlane

#: The environment variable naming the node_exporter textfile collector
#: directory the dev-dispatch run writes its ``cost-obs.prom`` exposition into.
COST_OBS_DIR_ENV = "FLEET_GRAPH_COST_OBS_DIR"


def cost_obs_exposition_dir(env: Mapping[str, str] | None = None) -> Path | None:
    """The textfile directory to render into, or ``None`` when not configured.

    An explicit empty string means "not configured", so a run that is not wired
    to a scrape path declines collection rather than inventing a directory.
    """
    source = os.environ if env is None else env
    configured = source.get(COST_OBS_DIR_ENV)
    if not configured:
        return None
    return Path(str(configured))


def build_cost_plane(
    exposition_dir: Path | str | None = None, env: Mapping[str, str] | None = None
) -> CostDataPlane | None:
    """Build the data plane for one dev-dispatch run, or ``None`` when unwired.

    An explicit `exposition_dir` wins; otherwise the environment variable is
    consulted. Where neither names a directory the run is not collecting, so
    ``None`` is returned and the dispatch proceeds without the data plane.
    """
    directory = Path(exposition_dir) if exposition_dir else cost_obs_exposition_dir(env)
    if directory is None:
        return None
    return CostDataPlane(exposition_dir=directory)


__all__ = [
    "COST_OBS_DIR_ENV",
    "build_cost_plane",
    "cost_obs_exposition_dir",
]
