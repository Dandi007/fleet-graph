"""The vendored domain code, and what the extraction spike measured.

plan.md D1 asks whether reusing dd's domain code as a library costs more than
rewriting it. These tests are the answer, kept executable so the answer stays
true: if the import graph grows a tendril back into loop-engine, this fails.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

VENDOR = Path(__file__).parents[1] / "src" / "fleet_graph" / "dd" / "vendor"
CONTRACTS = Path(__file__).parents[1] / "src" / "fleet_graph" / "dd" / "contracts"


class TestExtractionIsClean:
    def test_it_imports(self) -> None:
        from fleet_graph.dd.vendor import git_ops

        assert git_ops is not None

    def test_the_public_surface_survived(self) -> None:
        from fleet_graph.dd.vendor import git_ops

        for name in (
            "create_exact_input_workspace",
            "verify_exact_input_workspace",
            "create_synthetic_integration_workspace",
            "resolve_remote_ref",
            "safe_git_environment",
        ):
            assert hasattr(git_ops, name), name

    def test_no_import_reaches_back_into_loop_engine(self) -> None:
        """The whole point of vendoring: the dependency is severed, not aliased."""
        for module in VENDOR.glob("*.py"):
            source = module.read_text()
            assert not re.search(r"^\s*(from|import)\s+loop_engine", source, re.M), module.name

    def test_the_closure_is_just_these_two_modules(self) -> None:
        """1748 lines of git discipline came across with one import rewritten.

        If this grows, the D1 calculus changes and the finding needs revisiting.
        """
        assert {m.name for m in VENDOR.glob("*.py")} == {
            "__init__.py",
            "git_ops.py",
            "external_ops.py",
        }


class TestContracts:
    def test_all_sixteen_schemas_came_across(self) -> None:
        assert len(list(CONTRACTS.glob("*.json"))) == 16

    @pytest.mark.parametrize("schema_path", sorted(CONTRACTS.glob("*.json")), ids=lambda p: p.name)
    def test_each_schema_is_itself_valid(self, schema_path: Path) -> None:
        import jsonschema

        jsonschema.Draft202012Validator.check_schema(json.loads(schema_path.read_text()))


class TestSafeGitEnvironmentIsNarrowerThanItsName:
    """A defence-in-depth observation, not a vulnerability.

    `safe_git_environment` strips GIT_* and nothing else, so every credential
    in the caller's environment reaches the git subprocess. What keeps that
    from mattering is elsewhere: `_safe_git` disables hooks, fsmonitor and the
    ext protocol, resets credential helpers, and blocks global and system
    config on *every* call, and the local config is audited separately. The
    paths that would run attacker-controlled code under that environment are
    each closed.

    Pinned here so the mitigation cannot quietly disappear during P3, which
    would turn a naming quibble into a real exposure.
    """

    def test_it_only_strips_git_variables(self) -> None:
        from fleet_graph.dd.vendor import git_ops

        env = git_ops.safe_git_environment()
        assert not [
            k
            for k in env
            if k.startswith("GIT_")
            and k
            not in {
                "GIT_CONFIG_GLOBAL",
                "GIT_CONFIG_NOSYSTEM",
                "GIT_NO_REPLACE_OBJECTS",
                "GIT_TERMINAL_PROMPT",
            }
        ]

    def test_config_injection_is_blocked(self) -> None:
        from fleet_graph.dd.vendor import git_ops

        env = git_ops.safe_git_environment()
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    @pytest.mark.parametrize(
        "guard",
        ["core.hooksPath=/dev/null", "core.fsmonitor=false", "protocol.ext.allow=never"],
    )
    def test_code_execution_guards_are_present(self, guard: str) -> None:
        """These are what make the inherited environment harmless. Losing any
        one of them turns the passthrough into a real exposure."""
        assert guard in (VENDOR / "git_ops.py").read_text()
