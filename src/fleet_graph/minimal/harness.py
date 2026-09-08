"""The six role harness profiles: loading + validation (GO-22, design §7.13).

GO-22 requires the harness to be configurable — which MCPs an agent can read,
each one's read / read-write access, and which hooks get mounted — and calls out
that write hooks such as Cloud Memory (which turn a run's contents into
observations) must *not* be attached in a pure-automation setup. ``agentrun.py``
already forwards ``--harness <name>`` on every call; this module fills in the six
profile documents it points at and the loader + validator the engine uses to
read them. It never touches ``agentrun.build_argv``.

Format: the persistent profiles live in ``profiles/harness/*.json``. The
project's dependencies declare no PyYAML, and this DD must not add a third-party
package, so the documents are JSON — ``json`` is stdlib and parses them with no
dependency. (We pick JSON over YAML for precisely that reason, stated here so
the choice is auditable at a glance.)

Each profile carries exactly this fixed key set (unknown keys are validation
errors): ``name`` (the profile stem), ``role`` (one of ``agentrun.ROLES``),
``mcp`` (a list of ``{server, access}`` with ``access`` in ``read`` /
``read-write``), ``tools`` (the allowed tool list), ``hooks`` (an explicit list;
write hooks are never listed, and the key is written ``[]`` rather than
omitted), ``network`` (bool), ``data_roots`` (the filesystem roots the agent may
write; the symbolic tokens are resolved by the runtime, ``$WORKSPACE`` = the
agent's worktree and ``$DEPLOY_ROOT`` = the FR deploy/test environment),
``permission_mode`` (``read-write``/``read-only`` for the workspace) and
``memory_injection`` (always ``false``).

Role boundaries follow protocol §9 and each role's duties:
- ``goal`` (widest): writes spec files, creates branches and opens worktrees
  (GO-36); work-folder is read-write.
- ``impl``: writes its own worktree.
- ``cr``: read-only code, may run tests.
- ``fr``: read-only code, may run tests and deploy (GO-6.1).
- ``merge``: read-only code plus git write.
- ``scribe``: strictly read-only — zero write permission, ``hooks == []``
  (GO-21: the scribe only observes, never mutates state).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fleet_graph.minimal.agentrun import ROLES

# The six profile stems, in ``agentrun.ROLES`` order, so ``PROFILE_NAMES[i]``
# corresponds to ``ROLES[i]``. They are both a lookup table and the whitelist
# ``load_profile`` accepts, so no name can ever escape ``profiles/harness/``.
PROFILE_NAMES: tuple[str, ...] = tuple(f"minimal-{role}" for role in ROLES)

_PROFILE_BY_ROLE: dict[str, str] = {role: f"minimal-{role}" for role in ROLES}

# The fixed key set of every profile. Anything outside it is an unknown-key
# validation error (protocol §9's flattened harness surface).
PROFILE_KEYS: tuple[str, ...] = (
    "name",
    "role",
    "mcp",
    "tools",
    "hooks",
    "network",
    "data_roots",
    "permission_mode",
    "memory_injection",
)

ACCESS_MODES: tuple[str, ...] = ("read", "read-write")

# Write hooks a pure-automation harness must never mount (GO-22). Any hook here
# in a profile's ``hooks`` list is a validation error; the six shipped profiles
# all use ``[]`` so this stays an explicit module-level blacklist, not an
# unreachable guard.
WRITE_HOOK_BLACKLIST: tuple[str, ...] = ("claude-mem", "cloud-memory", "memory-write")

PROFILE_EXTENSION = ".json"


def _render(values: tuple[str, ...]) -> str:
    return "{" + ", ".join(values) + "}"


def profile_for_role(role: str) -> str:
    """The profile stem for ``role`` (one of ``agentrun.ROLES``).

    An unknown role raises ``ValueError``. The mapping is derived from ``ROLES``
    so it can never drift from the roles ``agentrun`` already knows.
    """
    name = _PROFILE_BY_ROLE.get(role)
    if name is None:
        raise ValueError(f"unknown role {role!r} (expected one of {_render(ROLES)})")
    return name


def load_profile(name: str, *, root: str | Path) -> dict[str, Any]:
    """Read one profile document from ``<root>/profiles/harness/<name>.json``.

    ``name`` is whitelisted to :data:`PROFILE_NAMES` (so a hostile value cannot
    reach outside the profiles directory); a name outside it, or a document that
    is not a JSON object, raises ``ValueError``.
    """
    if name not in PROFILE_NAMES:
        raise ValueError(f"unknown profile {name!r} (expected one of {_render(PROFILE_NAMES)})")
    path = Path(root) / "profiles" / "harness" / f"{name}{PROFILE_EXTENSION}"
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load harness profile {name!r}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"harness profile {name!r} is not a JSON object")
    return data


def validate_profile(obj: Any) -> list[str]:
    """Field-level errors for a loaded profile; an empty list means it passes.

    Checks, per the DD contract: all of :data:`PROFILE_KEYS` present and nothing
    else; ``mcp`` is a list of ``{server, access}`` with ``access`` in
    :data:`ACCESS_MODES`; ``memory_injection is False``; ``hooks`` exists and is
    a list with no :data:`WRITE_HOOK_BLACKLIST` entry; and for the scribe role
    every ``mcp[].access`` is ``read`` and ``hooks == []`` (GO-21 read-only).
    """
    if not isinstance(obj, dict):
        return [f"profile must be an object, got {type(obj).__name__}"]

    errors: list[str] = []

    for key in PROFILE_KEYS:
        if key not in obj:
            errors.append(f"missing required key {key!r}")
    for key in obj:
        if key not in PROFILE_KEYS:
            errors.append(f"unknown key {key!r}")

    if "memory_injection" in obj and obj["memory_injection"] is not False:
        errors.append(f"'memory_injection' must be False, got {obj['memory_injection']!r}")

    hooks = obj.get("hooks")
    if "hooks" in obj:
        if not isinstance(hooks, list):
            errors.append("'hooks' must be a list")
        else:
            for hook in hooks:
                if hook in WRITE_HOOK_BLACKLIST:
                    errors.append(f"'hooks' contains blacklisted write hook {hook!r}")

    mcp = obj.get("mcp")
    if "mcp" in obj:
        if not isinstance(mcp, list):
            errors.append("'mcp' must be a list")
        else:
            for index, entry in enumerate(mcp):
                if not isinstance(entry, dict):
                    errors.append(f"mcp[{index}] must be an object")
                    continue
                access = entry.get("access")
                if access not in ACCESS_MODES:
                    errors.append(f"mcp[{index}].access={access!r} not in {_render(ACCESS_MODES)}")

    # Scribe is the read-only role (GO-21): every MCP is read and no hook fires.
    if obj.get("role") == "scribe":
        if isinstance(hooks, list) and hooks:
            errors.append("scribe profile 'hooks' must be empty (GO-21 read-only)")
        if isinstance(mcp, list):
            for index, entry in enumerate(mcp):
                if isinstance(entry, dict) and entry.get("access") != "read":
                    errors.append(
                        f"scribe-profile mcp[{index}].access must be 'read' (GO-21 read-only)"
                    )

    return errors


__all__ = [
    "ACCESS_MODES",
    "PROFILE_KEYS",
    "PROFILE_NAMES",
    "WRITE_HOOK_BLACKLIST",
    "load_profile",
    "profile_for_role",
    "validate_profile",
]
