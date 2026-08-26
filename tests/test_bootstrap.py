"""The attempt context a development starts from, and why its bytes matter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import git, head
from fleet_graph.dd.bootstrap import (
    DEVELOPMENT_FIELDS,
    DEVELOPMENT_PATH,
    INDEX_PATH,
    SPEC_MANIFEST_FIELDS,
    SPEC_MANIFEST_PATH,
    SPEC_PATH,
    BootstrapError,
    build_attempt_context,
    canonical_bytes,
    committed_target_base,
    digest_of,
)

PINNED_PLUGIN = Path(
    "/data/code/self/loop-engine-dev-dispatch-plugin-releases"
    "/76c4003bd087890867b411186a0584ea3ba4364b"
)
BASE = "a" * 40
SPEC = b"# spec\n\nDo the thing.\n"


def context() -> object:
    return build_attempt_context(development_id="dev-1", spec=SPEC, target_base_commit=BASE)


class TestTheFilesItWrites:
    def test_all_four(self) -> None:
        assert set(context().files) == {SPEC_PATH, SPEC_MANIFEST_PATH, INDEX_PATH, DEVELOPMENT_PATH}

    def test_the_spec_is_frozen_byte_for_byte(self) -> None:
        assert context().files[SPEC_PATH] == SPEC

    def test_the_manifest_binds_the_spec(self) -> None:
        manifest = json.loads(context().files[SPEC_MANIFEST_PATH])
        assert manifest["spec_digest"] == digest_of(SPEC)
        assert manifest["spec_size_bytes"] == len(SPEC)
        assert manifest["spec_path"] == SPEC_PATH

    def test_the_identity_carries_the_base_the_dispatch_will_claim(self) -> None:
        development = json.loads(context().files[DEVELOPMENT_PATH])
        assert development["target_base_commit"] == BASE
        assert development["spec_digest"] == digest_of(SPEC)

    def test_the_feedback_index_starts_empty(self) -> None:
        assert json.loads(context().files[INDEX_PATH])["entries"] == []

    def test_it_writes_them_into_a_worktree(self, tmp_path: Path) -> None:
        written = context().write(tmp_path)
        assert len(written) == 4
        assert (tmp_path / DEVELOPMENT_PATH).is_file()
        assert (tmp_path / SPEC_PATH).read_bytes() == SPEC


class TestCanonicalBytesAreTheContract:
    def test_the_bytes_round_trip_through_the_same_serialiser(self) -> None:
        """The sealer re-serialises what it reads and compares byte for byte,
        so key order, separators and the trailing newline are contract rather
        than formatting."""
        for path in (SPEC_MANIFEST_PATH, INDEX_PATH, DEVELOPMENT_PATH):
            payload = context().files[path]
            assert canonical_bytes(json.loads(payload)) == payload

    def test_it_matches_the_plugins_own_serialiser(self) -> None:
        if not PINNED_PLUGIN.is_dir():
            pytest.skip("the pinned plugin release is not on this machine")
        source = (PINNED_PLUGIN / "scripts/attempt-context.py").read_text(encoding="utf-8")
        assert 'json.dumps(obj, sort_keys=True, separators=(",", ":"),' in source
        assert 'ensure_ascii=False, allow_nan=False) + "\\n"' in source

    def test_a_trailing_newline_is_not_decoration(self) -> None:
        assert context().files[DEVELOPMENT_PATH].endswith(b"\n")


class TestTheFieldSetsArePinnedToThePlugin:
    """Exact in both directions: a missing field and an extra one fail the
    sealer the same way."""

    def _plugin_set(self, name: str) -> set[str]:
        source = (PINNED_PLUGIN / "scripts/attempt-context.py").read_text(encoding="utf-8")
        block = source.split(f"{name} = {{", 1)[1].split("}", 1)[0]
        return {line.strip().strip('",') for line in block.splitlines() if line.strip().strip('",')}

    def test_development_fields(self) -> None:
        if not PINNED_PLUGIN.is_dir():
            pytest.skip("the pinned plugin release is not on this machine")
        assert set(DEVELOPMENT_FIELDS) == self._plugin_set("DEVELOPMENT_FIELDS")

    def test_spec_manifest_fields(self) -> None:
        if not PINNED_PLUGIN.is_dir():
            pytest.skip("the pinned plugin release is not on this machine")
        assert set(SPEC_MANIFEST_FIELDS) == self._plugin_set("SPEC_MANIFEST_FIELDS")

    def test_the_paths_match_the_plugins_constants(self) -> None:
        if not PINNED_PLUGIN.is_dir():
            pytest.skip("the pinned plugin release is not on this machine")
        source = (PINNED_PLUGIN / "scripts/attempt-context.py").read_text(encoding="utf-8")
        for constant, value in (
            ("DEV_PATH", DEVELOPMENT_PATH),
            ("SPEC_PATH", SPEC_PATH),
            ("SPEC_MANIFEST_PATH", SPEC_MANIFEST_PATH),
            ("INDEX_PATH", INDEX_PATH),
        ):
            assert f'{constant} = "{value}"' in source, constant


class TestItRefusesRatherThanWriteSomethingUnusable:
    def test_an_empty_spec(self) -> None:
        with pytest.raises(BootstrapError, match="empty"):
            build_attempt_context(development_id="d", spec=b"  \n", target_base_commit=BASE)

    def test_no_development_id(self) -> None:
        with pytest.raises(BootstrapError, match="needs an id"):
            build_attempt_context(development_id="", spec=SPEC, target_base_commit=BASE)

    @pytest.mark.parametrize("base", ["", "abc", "A" * 40, "g" * 40, "a" * 39])
    def test_a_base_that_is_not_a_full_lowercase_commit(self, base: str) -> None:
        with pytest.raises(BootstrapError, match="40-hex"):
            build_attempt_context(development_id="d", spec=SPEC, target_base_commit=base)


class TestTheBaseIsReadBackFromWhatWasCommitted:
    """`dd bootstrap` then `dd run` has to compose without an operator
    remembering to repeat the base by hand."""

    def _bootstrap(self, repo: Path, base: str) -> None:
        build_attempt_context(
            development_id="dev-1", spec=b"# spec\n", target_base_commit=base
        ).write(repo)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "dev-dispatch: bootstrap dev-1")

    def test_it_reads_the_base_the_identity_named(self, repo: Path) -> None:
        base = head(repo)
        self._bootstrap(repo, base)

        # HEAD has moved past the base by exactly the bootstrap commit, which
        # is the trap: deriving the base from HEAD here would name a commit
        # the committed identity never claimed.
        assert head(repo) != base
        assert committed_target_base(repo) == base

    def test_a_worktree_that_was_never_bootstrapped_has_none(self, repo: Path) -> None:
        assert committed_target_base(repo) is None

    def test_an_uncommitted_bootstrap_does_not_count(self, repo: Path) -> None:
        """The sealer reads the committed object, so this reads it too."""
        build_attempt_context(
            development_id="dev-1", spec=b"# spec\n", target_base_commit=head(repo)
        ).write(repo)
        assert committed_target_base(repo) is None

    def test_unreadable_json_is_no_answer_rather_than_a_crash(self, repo: Path) -> None:
        (repo / DEVELOPMENT_PATH).parent.mkdir(parents=True, exist_ok=True)
        (repo / DEVELOPMENT_PATH).write_text("not json", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "broken")
        assert committed_target_base(repo) is None
