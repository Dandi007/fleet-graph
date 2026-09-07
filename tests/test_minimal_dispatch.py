"""Dispatch launch protocol: field validation + DDRepoRef conversion + ready gate.

The validation half exercises ``protocol.validate`` on ``goal.turn/1`` dispatch
objects (the DD launch protocol, GO-36); the conversion half checks
``dispatch.dd_repo_refs`` / ``dd_acceptance`` / ``check_dispatch_ready`` against
a fake git runner so no real git, network, or filesystem is touched.
"""

from __future__ import annotations

from fleet_graph.minimal.dispatch import (
    check_dispatch_ready,
    dd_acceptance,
    dd_repo_refs,
)
from fleet_graph.minimal.gitgate import CompletedResult, FailureCode
from fleet_graph.minimal.protocol import SCHEMA_GOAL_TURN, validate

_SHA = "a" * 40

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _repo(
    path: str = "/wt/foo",
    remote: str = "origin",
    branch: str = "feature-x",
    spec_path: str = "docs/specs/101-foo.md",
) -> dict:
    return {"path": path, "remote": remote, "branch": branch, "spec_path": spec_path}


def _dispatch(**overrides) -> dict:
    dispatch = {"spec_text": "do it", "repos": [_repo()]}
    dispatch.update(overrides)
    return dispatch


def _obj(dispatch: dict) -> dict:
    return {
        "schema": SCHEMA_GOAL_TURN,
        "stop": "dispatch",
        "summary": "派一张单",
        "dispatch": dispatch,
    }


def _errors(dispatch: dict) -> list[str]:
    return validate(_obj(dispatch), SCHEMA_GOAL_TURN).errors


class FakeGitRunner:
    """Answers the read-only queries ``check_dd_ready`` performs."""

    def __init__(self, *, spec_exit: int = 0, spec_path: str = "docs/specs/101-foo.md") -> None:
        self.spec_exit = spec_exit
        self.spec_path = spec_path
        self.calls: list[tuple[list[str], str]] = []

    def run(self, args: list[str], *, cwd: str) -> CompletedResult:
        self.calls.append((list(args), cwd))
        if "ls-remote" in args:
            return CompletedResult(0, f"{_SHA}\trefs/heads/feature-x\n", "")
        if "rev-parse" in args and "--abbrev-ref" in args:
            return CompletedResult(0, "feature-x\n", "")
        if "rev-parse" in args:
            return CompletedResult(0, f"{_SHA}\n", "")
        if "status" in args:
            return CompletedResult(0, "", "")
        if "cat-file" in args:
            assert self.spec_path in " ".join(args[args.index("-C") + 2 :])
            return CompletedResult(self.spec_exit, "", "")
        if "merge-base" in args:
            return CompletedResult(1, "", "")
        raise AssertionError(f"unrecognized git argv: {args!r}")


# ---------------------------------------------------------------------------
# protocol.validate: dispatch field-level checks
# ---------------------------------------------------------------------------


class TestDispatchValidation:
    def test_valid_multi_repo_dispatch_passes(self) -> None:
        dispatch = _dispatch(
            repos=[
                _repo("/wt/a", branch="dd/g-000000/dd-01"),
                _repo("/wt/b", branch="dd/g-000000/dd-02", spec_path="docs/specs/102-bar.md"),
            ]
        )
        result = validate(_obj(dispatch), SCHEMA_GOAL_TURN)
        assert result.ok, result.errors

    def test_missing_spec_text_is_invalid(self) -> None:
        dispatch = _dispatch()
        dispatch.pop("spec_text")
        result = validate(_obj(dispatch), SCHEMA_GOAL_TURN)
        assert not result.ok
        assert any("dispatch.spec_text" in e for e in result.errors)

    def test_empty_repos_is_invalid(self) -> None:
        assert _errors(_dispatch(repos=[])) and any(
            "dispatch.repos" in e for e in _errors(_dispatch(repos=[]))
        )

    def test_repos_not_a_list_is_invalid(self) -> None:
        result = validate(_obj(_dispatch(repos="nope")), SCHEMA_GOAL_TURN)
        assert not result.ok
        assert any("dispatch.repos" in e for e in result.errors)

    def test_repo_item_missing_branch(self) -> None:
        repo = _repo()
        repo.pop("branch")
        result = validate(_obj(_dispatch(repos=[repo])), SCHEMA_GOAL_TURN)
        assert not result.ok
        assert any("dispatch.repos[0].branch" in e for e in result.errors)

    def test_spec_path_absolute_is_invalid(self) -> None:
        result = validate(
            _obj(_dispatch(repos=[_repo(spec_path="/abs/spec.md")])), SCHEMA_GOAL_TURN
        )
        assert not result.ok
        assert any("repos[0].spec_path" in e for e in result.errors)

    def test_spec_path_with_dotdot_is_invalid(self) -> None:
        result = validate(_obj(_dispatch(repos=[_repo(spec_path="a/../b.md")])), SCHEMA_GOAL_TURN)
        assert not result.ok
        assert any("repos[0].spec_path" in e for e in result.errors)

    def test_path_relative_is_invalid(self) -> None:
        result = validate(_obj(_dispatch(repos=[_repo(path="relative/path")])), SCHEMA_GOAL_TURN)
        assert not result.ok
        assert any("repos[0].path" in e and "absolute" in e for e in result.errors)

    def test_path_duplicate_is_invalid_with_index(self) -> None:
        result = validate(
            _obj(
                _dispatch(
                    repos=[
                        _repo("/wt/same", branch="dd/g-000000/dd-01"),
                        _repo("/wt/same", branch="dd/g-000000/dd-02"),
                    ]
                )
            ),
            SCHEMA_GOAL_TURN,
        )
        assert not result.ok
        assert any("repos[1].path" in e for e in result.errors)

    def test_pair_duplicate_is_invalid(self) -> None:
        result = validate(
            _obj(
                _dispatch(
                    repos=[
                        _repo("/wt/same"),
                        _repo("/wt/same"),
                    ]
                )
            ),
            SCHEMA_GOAL_TURN,
        )
        assert not result.ok
        assert any("repos[1]" in e for e in result.errors)

    def test_branch_with_space_is_invalid(self) -> None:
        result = validate(_obj(_dispatch(repos=[_repo(branch="bad branch")])), SCHEMA_GOAL_TURN)
        assert not result.ok
        assert any("repos[0].branch" in e for e in result.errors)

    def test_acceptance_extra_with_empty_entry_is_invalid(self) -> None:
        result = validate(_obj(_dispatch(acceptance_extra=["make test", ""])), SCHEMA_GOAL_TURN)
        assert not result.ok
        assert any("acceptance_extra" in e and "[" in e for e in result.errors)

    def test_acceptance_extra_not_a_list_is_invalid(self) -> None:
        result = validate(_obj(_dispatch(acceptance_extra="make test")), SCHEMA_GOAL_TURN)
        assert not result.ok
        assert any("acceptance_extra" in e for e in result.errors)


# ---------------------------------------------------------------------------
# dd_repo_refs
# ---------------------------------------------------------------------------


class TestDDRepoRefs:
    def test_fields_map_one_to_one(self) -> None:
        refs = dd_repo_refs(
            _dispatch(
                repos=[
                    _repo(
                        "/wt/foo",
                        remote="upstream",
                        branch="dd/g-000000/dd-01",
                        spec_path="docs/specs/101-foo.md",
                    )
                ]
            )
        )
        assert len(refs) == 1
        ref = refs[0]
        assert ref.worktree == "/wt/foo"
        assert ref.remote == "upstream"
        assert ref.branch == "dd/g-000000/dd-01"
        assert ref.spec_path == "docs/specs/101-foo.md"
        assert ref.label == "foo"

    def test_label_is_the_path_basename(self) -> None:
        refs = dd_repo_refs(_dispatch(repos=[_repo("/data/code/self/foo")]))
        assert refs[0].label == "foo"

    def test_invalid_repo_raises_value_error_with_index(self) -> None:
        repo = _repo()
        repo.pop("branch")
        try:
            dd_repo_refs(_dispatch(repos=[_repo(), repo]))
        except ValueError as exc:
            assert "repos[1].branch" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected ValueError")


# ---------------------------------------------------------------------------
# dd_acceptance
# ---------------------------------------------------------------------------


class TestDDAcceptance:
    def test_goal_acceptance_plus_extra_deduped_in_order(self) -> None:
        dispatch = _dispatch(acceptance_extra=["make test", "pytest tests/test_x.py"])
        assert dd_acceptance(dispatch, ["make test", "make lint"]) == [
            "make test",
            "make lint",
            "pytest tests/test_x.py",
        ]

    def test_no_extra_keeps_goal_acceptance(self) -> None:
        assert dd_acceptance(_dispatch(), ["make test"]) == ["make test"]


# ---------------------------------------------------------------------------
# check_dispatch_ready (five mechanical checks via a fake runner)
# ---------------------------------------------------------------------------


class TestCheckDispatchReady:
    def test_all_checks_pass(self) -> None:
        result = check_dispatch_ready(
            _dispatch(), goal_acceptance=["make test"], runner=FakeGitRunner(spec_exit=0)
        )
        assert result.ok is True
        assert result.failures == []

    def test_spec_missing_in_head_commit(self) -> None:
        result = check_dispatch_ready(
            _dispatch(), goal_acceptance=["make test"], runner=FakeGitRunner(spec_exit=1)
        )
        assert result.ok is False
        assert [f.code for f in result.failures] == [FailureCode.SPEC_MISSING]
