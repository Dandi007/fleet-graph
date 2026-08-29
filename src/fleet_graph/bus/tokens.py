"""Shared line-token resolution for the bus credential family.

The ``agent:{alias}`` channel is private and owner-only readable, and the
owner is the line's pump agent -- so every client that talks to a line's own
channel must present the *line's* token, not the fleet-graph service token
(which the channel ACL structurally 403s on ``agent:*``). Two sites
authenticate as the line: the scheduler's inbox wake probe
(``scheduler/wake.py``) and the line process's inbox drain
(``graphs/runner.py``, which builds ``bus/inbox.Inbox``). This module is the
single resolution both use, so a credential change lands in one place and the
two sites can never drift apart again.

The resolution never defaults to a literal token and never leaks one: the
token stays in memory only, never in argv, logs, or error text. A caller that
cannot obtain the line's token receives an explicit status (`missing`,
`empty`, or `unsafe`) so it can degrade loudly instead of silently pretending
it read messages with a credential it never had.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

#: Where a line's own bus credential lives ("{alias}" is substituted). The
#: `agent:{alias}` channel is private, owner-only readable, and the owner is
#: the line's pump agent -- so the inbox probe must present the *line's*
#: token, not the fleet-graph service token (which gets a structural 403).
#: These files mirror the pump tokens (persona §5c). Overridable via the
#: FLEET_GRAPH_LINE_TOKEN_PATH env var or an explicit template.
LINE_TOKEN_PATH_TEMPLATE = "/data/ronin/secrets/{alias}.token"
LINE_TOKEN_PATH_ENV = "FLEET_GRAPH_LINE_TOKEN_PATH"

#: An alias is a path component of the token file; anything outside this set
#: never touches the filesystem.
_SAFE_ALIAS = re.compile(r"^[A-Za-z0-9._-]+$")

#: Why a line's token could not be resolved, so a degraded drain can say which
#: of the three distinct causes it was instead of a silent None.
LineTokenStatus = Literal["ok", "missing", "empty", "unsafe"]


@dataclass(frozen=True)
class LineTokenResolution:
    """One alias's token resolution: the token plus why it is or is not there.

    ``present`` is the single bool a caller should branch on. When false,
    ``status`` names the precise cause for explicit degradation accounting:
    ``missing`` (file absent/unreadable), ``empty`` (file exists but holds no
    token), or ``unsafe`` (the alias could traverse out of the secrets dir and
    was refused before touching the filesystem).
    """

    alias: str
    status: LineTokenStatus
    token: str | None = None

    @property
    def present(self) -> bool:
        return self.token is not None


def resolve_line_token(
    alias: str, *, template: str | None = None, env: dict[str, str] | None = None
) -> LineTokenResolution:
    """Resolve the line's own bus token, or say precisely why it is absent.

    ``template`` is a str with an ``{alias}`` placeholder, used verbatim when
    given. Otherwise the ``FLEET_GRAPH_LINE_TOKEN_PATH`` env var wins over the
    default ``LINE_TOKEN_PATH_TEMPLATE``. An alias that could traverse out of
    the secrets directory is refused before any filesystem access.
    """
    if not _SAFE_ALIAS.match(alias):
        return LineTokenResolution(alias, "unsafe")
    env = os.environ if env is None else env
    template_text = template or env.get(LINE_TOKEN_PATH_ENV) or LINE_TOKEN_PATH_TEMPLATE
    path = Path(template_text.format(alias=alias))
    try:
        token = path.read_text().strip()
    except OSError:
        return LineTokenResolution(alias, "missing")
    if not token:
        return LineTokenResolution(alias, "empty")
    return LineTokenResolution(alias, "ok", token)


__all__ = [
    "LINE_TOKEN_PATH_ENV",
    "LINE_TOKEN_PATH_TEMPLATE",
    "LineTokenResolution",
    "LineTokenStatus",
    "resolve_line_token",
]
