"""Building the plugin's StageDispatch, validated against the plugin's schema."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import DEVELOPMENT_ID, INDEX_PATH, SPEC_PATH, git, head, write_index
from fleet_graph.dd.dispatch import (
    DISPATCH_SCHEMA_PATH,
    DevelopmentChain,
    DispatchError,
    StageDispatchBuilder,
    derive_attempt_id,
    derive_intent_id,
    read_committed_refs,
)
from fleet_graph.dd.upstream_constants import (
    compute_json_digest,
)


def make_builder(repo: Path) -> StageDispatchBuilder:
    return StageDispatchBuilder(
        DevelopmentChain(
            development_id=DEVELOPMENT_ID,
            workspace_path=str(repo),
            target_base_commit="b" * 40,
            root_handoff_digest="sha256:" + "c" * 64,
        )
    )


def walker_dispatch(
    repo: Path,
    *,
    stage: str = "implement",
    mode: str = "initial",
    generation: int = 1,
    attempt: int = 1,
) -> dict[str, Any]:
    return {
        "stage": stage,
        "mode": mode,
        "generation": generation,
        "attempt": attempt,
        "input_commit": head(repo),
    }


def validator() -> Any:
    """A validator that can follow the schema's relative $refs."""
    import jsonschema
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    contracts = DISPATCH_SCHEMA_PATH.parent
    registry: Registry = Registry()
    for path in contracts.glob("*.schema.json"):
        resource = Resource(
            contents=json.loads(path.read_text(encoding="utf-8")), specification=DRAFT202012
        )
        registry = registry.with_resource(uri=path.name, resource=resource)
    schema = json.loads(DISPATCH_SCHEMA_PATH.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema, registry=registry)


class TestTheBuiltDispatchSatisfiesThePluginsOwnSchema:
    """The authority is the schema shipped in contracts/, not my reading of it."""

    def test_it_validates(self, repo: Path) -> None:
        built = make_builder(repo).build(walker_dispatch(repo))
        validator().validate(built)

    def test_the_validator_is_not_vacuous(self, repo: Path) -> None:
        """A validator that follows no $ref would pass anything shaped roughly
        right. Prove it rejects a deep violation before trusting the positives.
        """
        import jsonschema

        built = make_builder(repo).build(walker_dispatch(repo))
        built["spec_ref"]["blob_oid"] = "not-a-blob-oid"
        with pytest.raises(jsonschema.ValidationError):
            validator().validate(built)

        built = make_builder(repo).build(walker_dispatch(repo))
        built["spec_ref"]["path"] = ".dev-dispatch/spec/somewhere-else.md"
        with pytest.raises(jsonschema.ValidationError):
            validator().validate(built)

        built = make_builder(repo).build(walker_dispatch(repo))
        del built["attempt_id"]
        with pytest.raises(jsonschema.ValidationError):
            validator().validate(built)

    def test_it_validates_for_every_stage_the_sealer_serves(self, repo: Path) -> None:
        builder = make_builder(repo)
        for stage in sorted(builder.allowed_stages):
            built = builder.build(walker_dispatch(repo, stage=stage))
            validator().validate(built)

    def test_it_validates_in_rework_mode(self, repo: Path) -> None:
        built = make_builder(repo).build(walker_dispatch(repo, mode="rework", attempt=2))
        validator().validate(built)

    def test_the_refs_are_the_committed_blobs(self, repo: Path) -> None:
        built = make_builder(repo).build(walker_dispatch(repo))
        assert built["spec_ref"]["blob_oid"] == git(repo, "rev-parse", f"HEAD:{SPEC_PATH}")
        assert built["feedback_ref"]["blob_oid"] == git(repo, "rev-parse", f"HEAD:{INDEX_PATH}")

    def test_the_entry_count_is_read_not_assumed(self, repo: Path) -> None:
        write_index(repo, entries=[{"a": 1}, {"b": 2}, {"c": 3}])
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "feedback")

        built = make_builder(repo).build(walker_dispatch(repo))
        assert built["feedback_ref"]["entry_count"] == 3


class TestTheForwardChain:
    def test_the_parent_digest_is_the_previous_receipt(self, repo: Path) -> None:
        receipt = {"stage": "implement", "output_commit": "d" * 40}
        built = make_builder(repo).build(walker_dispatch(repo), parent_receipt=receipt)
        assert built["parent_handoff_receipt_digest"] == compute_json_digest(receipt)

    def test_the_first_stage_uses_the_chain_root(self, repo: Path) -> None:
        built = make_builder(repo).build(walker_dispatch(repo))
        assert built["parent_handoff_receipt_digest"] == "sha256:" + "c" * 64

    def test_the_digest_does_not_depend_on_key_order(self, repo: Path) -> None:
        builder = make_builder(repo)
        one = builder.parent_digest({"a": 1, "b": 2})
        two = builder.parent_digest({"b": 2, "a": 1})
        assert one == two

    def test_expected_remote_head_is_the_input_commit(self, repo: Path) -> None:
        """Upstream's choice, and not an oversight: the remote is checked
        against this field, so reading the remote here would compare it to
        itself."""
        built = make_builder(repo).build(walker_dispatch(repo))
        assert built["expected_remote_head"] == built["input_commit"] == head(repo)


class TestDerivedIdsAreStable:
    def test_the_same_attempt_derives_the_same_ids(self, repo: Path) -> None:
        builder = make_builder(repo)
        first = builder.build(walker_dispatch(repo))
        second = builder.build(walker_dispatch(repo))
        assert first == second

    def test_a_retry_freezes_the_same_intent(self) -> None:
        """A random intent id would fork a second materialization per retry."""
        args = (DEVELOPMENT_ID, "implement", 1, 1, "e" * 40)
        assert derive_intent_id(*args) == derive_intent_id(*args)

    def test_a_different_attempt_derives_a_different_id(self) -> None:
        assert derive_attempt_id(DEVELOPMENT_ID, 1, 1) != derive_attempt_id(DEVELOPMENT_ID, 1, 2)
        assert derive_attempt_id(DEVELOPMENT_ID, 1, 1) != derive_attempt_id(DEVELOPMENT_ID, 2, 1)

    def test_a_pinned_attempt_identity_wins_over_derivation(self, repo: Path) -> None:
        """A replayed prefix pins the identity its receipts were sealed under;
        the built dispatch must carry it, and it still validates against the
        plugin's own schema."""
        pinned = derive_attempt_id(DEVELOPMENT_ID, 2, 1)
        dispatch = walker_dispatch(repo, generation=4)
        dispatch["pinned_attempt_id"] = pinned
        built = make_builder(repo).build(dispatch)
        assert built["attempt_id"] == pinned
        validator().validate(built)

    def test_an_empty_pin_falls_back_to_derivation(self, repo: Path) -> None:
        dispatch = walker_dispatch(repo, generation=4)
        dispatch["pinned_attempt_id"] = ""
        built = make_builder(repo).build(dispatch)
        assert built["attempt_id"] == derive_attempt_id(DEVELOPMENT_ID, 4, 1)

    def test_a_different_input_commit_derives_a_different_intent(self) -> None:
        assert derive_intent_id(DEVELOPMENT_ID, "implement", 1, 1, "e" * 40) != derive_intent_id(
            DEVELOPMENT_ID, "implement", 1, 1, "f" * 40
        )


class TestItRefusesRatherThanGuesses:
    def test_a_stage_the_sealer_does_not_serve_is_refused(self, repo: Path) -> None:
        """The graph has seven stages; the sealer's schema enumerates four."""
        builder = make_builder(repo)
        assert not builder.serves("human_gate")
        with pytest.raises(DispatchError, match="not one of them"):
            builder.build(walker_dispatch(repo, stage="human_gate"))

    def test_an_unknown_mode_is_refused(self, repo: Path) -> None:
        with pytest.raises(DispatchError, match="mode must be"):
            make_builder(repo).build(walker_dispatch(repo, mode="normal"))

    def test_a_short_commit_is_refused(self, repo: Path) -> None:
        dispatch = walker_dispatch(repo)
        dispatch["input_commit"] = dispatch["input_commit"][:7]
        with pytest.raises(DispatchError, match="40-hex"):
            make_builder(repo).build(dispatch)

    def test_a_missing_spec_blob_is_refused(self, repo: Path) -> None:
        git(repo, "rm", "-q", SPEC_PATH)
        git(repo, "commit", "-q", "-m", "drop spec")
        with pytest.raises(DispatchError, match="cannot read committed refs"):
            make_builder(repo).build(walker_dispatch(repo))

    def test_a_spec_that_actively_crosses_the_scope_boundary_is_refused(self, repo: Path) -> None:
        """The dispatch path re-checks the committed spec, not just admission:
        a handoff that rewrites the frozen spec to add B4 is refused by name."""
        (repo / SPEC_PATH).write_text("implement B4 as the next phase\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "rewrite spec to add B4")
        with pytest.raises(DispatchError, match="scope boundary refused"):
            make_builder(repo).build(walker_dispatch(repo))

    def test_a_feedback_index_for_another_development_is_refused(self, repo: Path) -> None:
        """Otherwise the whole chain quietly re-points at someone else's work."""
        write_index(repo, entries=[], development_id="dev-someone-else")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "wrong development")
        with pytest.raises(DispatchError, match="binds development"):
            make_builder(repo).build(walker_dispatch(repo))

    def test_a_feedback_index_from_another_protocol_is_refused(self, repo: Path) -> None:
        write_index(repo, entries=[], contract_version="dev-dispatch.attempt-context/v0")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "wrong protocol")
        with pytest.raises(DispatchError, match="uses protocol"):
            make_builder(repo).build(walker_dispatch(repo))

    def test_a_feedback_index_with_extra_keys_is_refused(self, repo: Path) -> None:
        path = repo / INDEX_PATH
        payload = json.loads(path.read_text())
        payload["surprise"] = True
        path.write_text(json.dumps(payload), encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "extra key")
        with pytest.raises(DispatchError, match="must carry exactly"):
            make_builder(repo).build(walker_dispatch(repo))

    def test_a_non_json_feedback_index_is_refused(self, repo: Path) -> None:
        (repo / INDEX_PATH).write_text("not json", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "not json")
        with pytest.raises(DispatchError, match="is not JSON"):
            make_builder(repo).build(walker_dispatch(repo))


class TestTheVendoredHelperItLeansOn:
    def test_the_guarded_git_runner_still_exists(self) -> None:
        """`read_committed_refs` calls a private vendored helper on purpose:
        restating its hooks/fsmonitor/ext-protocol guard list is how that list
        drifts. If a re-vendor renames it, fail here rather than in production.
        """
        from fleet_graph.dd.vendor import git_ops

        assert callable(git_ops._command_text)
        assert callable(git_ops.exact_artifact_identity)
        assert git_ops._FULL_COMMIT_RE.fullmatch("a" * 40)
        assert git_ops._FULL_COMMIT_RE.fullmatch("A" * 40) is None


class TestTheContractsAgreeWithEachOther:
    def test_the_schema_paths_match_the_artifact_contract(self, repo: Path) -> None:
        builder = make_builder(repo)
        artifacts = json.loads(
            (DISPATCH_SCHEMA_PATH.parent / "stage-artifacts.json").read_text(encoding="utf-8")
        )["artifact_kinds"]
        assert builder.spec_path == artifacts["attempt_context_spec"]["path_pattern"]
        assert builder.index_path == artifacts["attempt_context_feedback_index"]["path_pattern"]

    def test_read_committed_refs_needs_both_paths(self, repo: Path) -> None:
        with pytest.raises(DispatchError):
            read_committed_refs(
                str(repo),
                head(repo),
                DEVELOPMENT_ID,
                spec_path=SPEC_PATH,
                index_path=".dev-dispatch/feedback/nope.json",
            )
