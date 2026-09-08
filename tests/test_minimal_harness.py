"""Tests for fleet_graph.minimal.harness: six profiles + loader + validator.

These cover exactly the DD acceptance: the six shipped profiles all load and
validate clean; ``profile_for_role`` spans ``agentrun.ROLES`` and rejects unknown
roles; the scribe profile is strictly read-only; no profile mounts a write hook
or injects memory; each class of broken profile reports an error; and
``profiles/harness/`` holds exactly six files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fleet_graph.minimal.agentrun import ROLES
from fleet_graph.minimal.harness import (
    ACCESS_MODES,
    PROFILE_KEYS,
    PROFILE_NAMES,
    WRITE_HOOK_BLACKLIST,
    load_profile,
    profile_for_role,
    validate_profile,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS_DIR = REPO_ROOT / "profiles" / "harness"


def _profile(**overrides: object) -> dict:
    base: dict[str, object] = {
        "name": "minimal-impl",
        "role": "impl",
        "mcp": [{"server": "wiki", "access": "read"}],
        "tools": ["read", "bash"],
        "hooks": [],
        "network": True,
        "data_roots": ["$WORKSPACE"],
        "permission_mode": "read-write",
        "memory_injection": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. The six shipped profiles all load and validate clean
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", PROFILE_NAMES)
def test_every_profile_loads_and_validates(name: str) -> None:
    obj = load_profile(name, root=REPO_ROOT)
    assert validate_profile(obj) == []


def test_profile_for_role_covers_every_role() -> None:
    assert tuple(f"minimal-{role}" for role in ROLES) == PROFILE_NAMES
    for role in ROLES:
        assert profile_for_role(role) == f"minimal-{role}"


def test_profile_for_role_unknown_raises() -> None:
    with pytest.raises(ValueError):
        profile_for_role("boss")


def test_load_profile_unknown_name_raises() -> None:
    with pytest.raises(ValueError):
        load_profile("minimal-boss", root=REPO_ROOT)


# ---------------------------------------------------------------------------
# 3. scribe is strictly read-only (GO-21)
# ---------------------------------------------------------------------------


def test_scribe_mcp_all_read_and_hooks_empty() -> None:
    obj = load_profile("minimal-scribe", root=REPO_ROOT)
    assert obj["role"] == "scribe"
    assert obj["hooks"] == []
    assert all(entry["access"] == "read" for entry in obj["mcp"])


# ---------------------------------------------------------------------------
# 4. no write hook mounted, no memory injection
# ---------------------------------------------------------------------------


def test_no_profile_mounts_a_write_hook() -> None:
    for name in PROFILE_NAMES:
        obj = load_profile(name, root=REPO_ROOT)
        assert set(obj["hooks"]).isdisjoint(WRITE_HOOK_BLACKLIST)


def test_no_profile_injects_memory() -> None:
    for name in PROFILE_NAMES:
        obj = load_profile(name, root=REPO_ROOT)
        assert obj["memory_injection"] is False


# ---------------------------------------------------------------------------
# 5. each class of broken profile reports an error
# ---------------------------------------------------------------------------


def test_missing_required_key_reports_error() -> None:
    obj = _profile()
    del obj["network"]
    assert any("required key 'network'" in e for e in validate_profile(obj))


def test_invalid_access_reports_error() -> None:
    obj = _profile(mcp=[{"server": "wiki", "access": "write"}])
    assert any("access" in e for e in validate_profile(obj))


def test_unknown_key_reports_error() -> None:
    obj = _profile()
    obj["bogus"] = 1
    assert any("unknown key 'bogus'" in e for e in validate_profile(obj))


def test_missing_hooks_reports_error() -> None:
    obj = _profile()
    del obj["hooks"]
    assert any("'hooks'" in e for e in validate_profile(obj))


def test_hooks_with_blacklisted_write_hook_reports_error() -> None:
    obj = _profile(hooks=["claude-mem"])
    assert any("claude-mem" in e for e in validate_profile(obj))


def test_scribe_with_read_write_mcp_reports_error() -> None:
    obj = _profile(
        name="minimal-scribe",
        role="scribe",
        mcp=[{"server": "work-folder", "access": "read-write"}],
    )
    assert any("must be 'read'" in e for e in validate_profile(obj))


def test_scribe_with_nonempty_hooks_reports_error() -> None:
    obj = _profile(name="minimal-scribe", role="scribe", hooks=["some-read-hook"])
    assert any("'hooks' must be empty" in e for e in validate_profile(obj))


def test_non_object_reports_error() -> None:
    assert validate_profile("nope") == ["profile must be an object, got str"]


# ---------------------------------------------------------------------------
# 6. profiles/harness/ holds exactly six files
# ---------------------------------------------------------------------------


def test_harness_dir_holds_exactly_six_files() -> None:
    files = sorted(p.name for p in HARNESS_DIR.iterdir() if p.is_file())
    assert files == sorted(f"{name}.json" for name in PROFILE_NAMES)


def test_profile_keys_match_the_fixed_set() -> None:
    assert PROFILE_KEYS == (
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
    assert ACCESS_MODES == ("read", "read-write")
