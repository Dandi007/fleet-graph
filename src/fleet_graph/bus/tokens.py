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
from collections.abc import Callable
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

#: The supervision/control plane's own credential root. A governed line's
#: token must never resolve (realpath) into it: tokens there belong to the
#: fleet-graph control plane (service / decision publisher), not to a line.
#: Gate 6 refuses a token that masquerades as a control-plane credential.
SUPERVISION_TOKEN_ROOT = Path("/data/agent-bus/tokens")

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


#: Why an alias's token is or is not owned by the governed line (gate 6).
#: The negative statuses name the exact clause that refused admission.
LineTokenOwnershipStatus = Literal[
    "owned",
    "unsafe",
    "missing",
    "non_regular",
    "outside_boundary",
    "supervision_plane",
    "other_line",
    "symlink_alias",
]


@dataclass(frozen=True)
class LineTokenOwnership:
    """Gate 6's ownership verdict for one alias's line token.

    ``owned`` is the single bool a caller should branch on: the token is a
    regular file whose canonical (realpath) path is exactly the governed
    line's own ``<secrets_root>/<alias>.token`` -- it resolves nowhere else
    (no supervision-plane credential, no other line's token, no symlink
    masquerade, no escape from the secrets boundary). Every other status names
    the failing clause so a refusal is explicit, never a silent pass.
    """

    alias: str
    status: LineTokenOwnershipStatus
    path: Path | None = None
    canonical: Path | None = None

    @property
    def owned(self) -> bool:
        return self.status == "owned"


def _is_within(child: Path, root: Path) -> bool:
    """Whether ``child`` is ``root`` or lives under it (both canonicalized)."""
    try:
        child.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_line_token_ownership(
    alias: str,
    *,
    template: str | None = None,
    env: dict[str, str] | None = None,
    secrets_root: Path | None = None,
    supervision_roots: tuple[Path, ...] = (SUPERVISION_TOKEN_ROOT,),
) -> LineTokenOwnership:
    """Gate 6's ownership validation for the alias's line token.

    Ownership is a *positive boundary over canonicalized paths*, never filename
    or token presence: the literal ``<secrets_root>/<alias>.token`` must exist
    as a regular file, and its ``realpath`` must be exactly that canonical
    location. The statuses, in check order:

    - ``unsafe`` -- the alias could traverse out of the secrets directory.
    - ``missing`` -- no token file at the literal path.
    - ``non_regular`` -- the literal path exists but is not a regular file.
    - ``supervision_plane`` -- the realpath resolves into a control-plane
      credential root (``SUPERVISION_TOKEN_ROOT`` or an injected root).
    - ``outside_boundary`` -- the realpath escapes the canonical secrets root.
    - ``other_line`` -- the realpath is a *different* alias's token file (a
      masquerade onto another line's identity).
    - ``symlink_alias`` -- the literal path is itself a symlink (a symlink
      masquerade; the owned token is a plain regular file, not a link).
    - ``owned`` -- a regular file whose realpath is exactly the line's own
      token path, inside the secrets boundary, in no supervision plane.

    ``secrets_root`` defaults to the literal path's parent (the secrets dir);
    ``supervision_roots`` defaults to the fleet's control-plane token root.
    """
    if not _SAFE_ALIAS.match(alias):
        return LineTokenOwnership(alias, "unsafe")
    env = os.environ if env is None else env
    template_text = template or env.get(LINE_TOKEN_PATH_ENV) or LINE_TOKEN_PATH_TEMPLATE
    literal = Path(template_text.format(alias=alias))
    if not literal.exists():
        return LineTokenOwnership(alias, "missing", path=literal)
    if not literal.is_file():
        return LineTokenOwnership(alias, "non_regular", path=literal)
    canonical = Path(os.path.realpath(literal))
    secrets_canonical = (
        Path(os.path.realpath(secrets_root))
        if secrets_root is not None
        else Path(os.path.realpath(literal.parent))
    )
    for root in supervision_roots:
        if _is_within(canonical, Path(os.path.realpath(root))):
            return LineTokenOwnership(alias, "supervision_plane", path=literal, canonical=canonical)
    if not _is_within(canonical, secrets_canonical):
        return LineTokenOwnership(alias, "outside_boundary", path=literal, canonical=canonical)
    if canonical.name != f"{alias}.token":
        return LineTokenOwnership(alias, "other_line", path=literal, canonical=canonical)
    if literal.is_symlink():
        return LineTokenOwnership(alias, "symlink_alias", path=literal, canonical=canonical)
    return LineTokenOwnership(alias, "owned", path=literal, canonical=canonical)


def build_line_token_ownership_check(
    *,
    template: str | None = None,
    env: dict[str, str] | None = None,
    secrets_root: Path | None = None,
    supervision_roots: tuple[Path, ...] = (SUPERVISION_TOKEN_ROOT,),
) -> Callable[[str], bool]:
    """A gate-6 ownership check ``(alias) -> bool`` over a chosen layout.

    Production binds the fleet's token template, secrets root and supervision
    root; drills bind a scratch secrets dir and a scratch supervision dir so
    the negative ownership cases (supervision-plane, other-line, symlink
    alias) are exercised against the real canonicalization logic.
    """

    def check(alias: str) -> bool:
        return resolve_line_token_ownership(
            alias,
            template=template,
            env=env,
            secrets_root=secrets_root,
            supervision_roots=supervision_roots,
        ).owned

    return check


def resolve_supervisor_identity(
    identity: str,
    *,
    template: str | None = None,
    supervision_root: Path | None = None,
) -> LineTokenOwnership:
    """Whether an identity is a *supervisor-plane* principal (U4 admission).

    The exact mirror of gate 6's line-token ownership: where a line's token
    must NOT resolve into a supervision/control-plane credential root, a
    supervisor identity's credential MUST live there -- the literal
    ``<supervision_root>/<identity>.token`` must exist as a regular file whose
    realpath is exactly that canonical location (inside the supervision
    boundary, not another identity's file, not a symlink masquerade). Any
    other shape degrades to the naming status, never a guess.
    """
    if not _SAFE_ALIAS.match(identity):
        return LineTokenOwnership(identity, "unsafe")
    root = Path(supervision_root) if supervision_root is not None else SUPERVISION_TOKEN_ROOT
    template_text = template or str(root / "{identity}.token")
    literal = Path(template_text.format(identity=identity))
    if not literal.exists():
        return LineTokenOwnership(identity, "missing", path=literal)
    if not literal.is_file():
        return LineTokenOwnership(identity, "non_regular", path=literal)
    canonical = Path(os.path.realpath(literal))
    root_canonical = Path(os.path.realpath(root))
    if not _is_within(canonical, root_canonical):
        return LineTokenOwnership(identity, "outside_boundary", path=literal, canonical=canonical)
    if canonical.name != f"{identity}.token":
        return LineTokenOwnership(identity, "other_line", path=literal, canonical=canonical)
    if literal.is_symlink():
        return LineTokenOwnership(identity, "symlink_alias", path=literal, canonical=canonical)
    return LineTokenOwnership(identity, "owned", path=literal, canonical=canonical)


def build_supervisor_identity_check(
    *,
    template: str | None = None,
    supervision_root: Path | None = None,
) -> Callable[[str], bool]:
    """A supervisor-plane identity check ``(identity) -> bool`` (U4 admission).

    Production binds the fleet's supervision/control-plane credential root
    (``SUPERVISION_TOKEN_ROOT`` = ``/data/agent-bus/tokens``); drills bind a
    scratch supervision dir so the negative cases are exercised against the
    real canonicalization logic. Only an identity whose own credential is a
    regular file inside that root is a supervisor -- the boundary never
    broadens.
    """

    def check(identity: str) -> bool:
        return resolve_supervisor_identity(
            identity, template=template, supervision_root=supervision_root
        ).owned

    return check


__all__ = [
    "LINE_TOKEN_PATH_ENV",
    "LINE_TOKEN_PATH_TEMPLATE",
    "SUPERVISION_TOKEN_ROOT",
    "LineTokenOwnership",
    "LineTokenOwnershipStatus",
    "LineTokenResolution",
    "LineTokenStatus",
    "build_line_token_ownership_check",
    "build_supervisor_identity_check",
    "resolve_line_token",
    "resolve_line_token_ownership",
    "resolve_supervisor_identity",
]
