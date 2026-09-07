"""Tests for fleet_graph.minimal.runroot: the engine state root + post-enroll prep.

No git, no network, no processes: ``create_run_root``/``write_enroll`` land on a
``tmp_path`` engine_root and ``prepare_plan`` takes an injected ``release_exists``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet_graph.minimal.runroot import (
    DEFAULT_ENGINE_ROOT,
    PreparePlan,
    RepoPrep,
    RunRootConflict,
    create_run_root,
    dd_branch,
    git_argv_for,
    goal_run_root,
    prepare_plan,
    read_enroll,
    release_branch,
    worktree_path,
    write_enroll,
)


def base_enroll():
    return {
        "schema": "goal.enroll/2",
        "goal_id": "g-7f3a2c",
        "work_folder": "wf-ab12cd",
        "title": "重做最小系统",
        "goal_text": "把 fleet-graph 重建为最小系统。",
        "source_branch": "release/loopx-minimal",
        "repos": [
            {
                "path": "/data/wt/alpha",
                "remote": "origin",
                "target_branch": "main",
                "acceptance": ["make verify"],
            },
            {
                "path": "/data/wt/beta",
                "remote": "upstream",
                "target_branch": "release/prod",
                "acceptance": ["make test"],
            },
        ],
    }


# --- goal_run_root ---------------------------------------------------------


class TestGoalIdShape:
    @pytest.mark.parametrize(
        "bad",
        [
            "g-7f3a2c/..",
            "g/7f3a2c",
            "g-..",
            "g-ABCDEF",
            "g-7f3a2",
            "g-7f3a2c9",
            "g-",
            "xyz-7f3a2c",
            "g-7f3a!c",
            "",
            None,
            42,
        ],
    )
    def test_invalid_goal_id_rejected(self, bad):
        with pytest.raises(ValueError):
            goal_run_root(bad, engine_root="/tmp/engine")

    @pytest.mark.parametrize("good", ["g-000000", "g-abcdef", "g-7f3a2c", "g-123456"])
    def test_valid_goal_id_accepted(self, good):
        root = goal_run_root(good, engine_root="/tmp/engine")
        assert root.goal_id == good
        assert root.root == Path("/tmp/engine") / good


class TestPathProperties:
    def test_all_path_properties(self, tmp_path):
        run_root = goal_run_root("g-7f3a2c", engine_root=tmp_path)
        assert run_root.root == tmp_path / "g-7f3a2c"
        assert run_root.events_path == tmp_path / "g-7f3a2c" / "events.jsonl"
        assert run_root.control_path == tmp_path / "g-7f3a2c" / "control.jsonl"
        assert run_root.sessions_dir == tmp_path / "g-7f3a2c" / "sessions"
        assert run_root.worktrees_dir == tmp_path / "g-7f3a2c" / "worktrees"
        assert run_root.dd_dir == tmp_path / "g-7f3a2c" / "dd"
        assert run_root.enroll_path == tmp_path / "g-7f3a2c" / "goal.enroll.json"
        assert run_root.observations_path == tmp_path / "g-7f3a2c" / "observations.jsonl"

    def test_default_engine_root(self):
        run_root = goal_run_root("g-7f3a2c")
        assert run_root.root == Path(DEFAULT_ENGINE_ROOT) / "g-7f3a2c"


# --- dd_branch / worktree_path / release_branch ----------------------------


class TestDdBranch:
    @pytest.mark.parametrize("good", ["dd-01", "dd_01", "dd.01", "DD-01", "1", "_", "a-b.c_d"])
    def test_branch_name(self, good):
        assert dd_branch("g-7f3a2c", good) == f"dd/g-7f3a2c/{good}"

    @pytest.mark.parametrize("bad", ["a/b", "a b", "a@b", "", None, "a:b", "a\tb", 42])
    def test_invalid_dd_id_rejected(self, bad):
        with pytest.raises(ValueError):
            dd_branch("g-7f3a2c", bad)


class TestWorktreePath:
    def test_worktree_path(self, tmp_path):
        run_root = goal_run_root("g-7f3a2c", engine_root=tmp_path)
        assert worktree_path(run_root, "dd-01") == tmp_path / "g-7f3a2c" / "worktrees" / "dd-01"

    @pytest.mark.parametrize("bad", ["a/b", "a b", "", None, "a@b", 42])
    def test_invalid_dd_id_rejected(self, tmp_path, bad):
        run_root = goal_run_root("g-7f3a2c", engine_root=tmp_path)
        with pytest.raises(ValueError):
            worktree_path(run_root, bad)


class TestReleaseBranch:
    def test_reads_source_branch(self):
        assert release_branch(base_enroll()) == "release/loopx-minimal"

    @pytest.mark.parametrize("bad", [{}, {"source_branch": ""}, {"source_branch": None}])
    def test_missing_or_empty_source_branch_raises(self, bad):
        with pytest.raises(ValueError):
            release_branch(bad)


# --- create_run_root / write_enroll / read_enroll --------------------------


class TestCreateRunRoot:
    def test_creates_all_dirs(self, tmp_path):
        run_root = goal_run_root("g-7f3a2c", engine_root=tmp_path)
        create_run_root(run_root)
        for d in (run_root.root, run_root.sessions_dir, run_root.worktrees_dir, run_root.dd_dir):
            assert d.is_dir()

    def test_idempotent(self, tmp_path):
        run_root = goal_run_root("g-7f3a2c", engine_root=tmp_path)
        create_run_root(run_root)
        create_run_root(run_root)
        assert run_root.root.is_dir()


class TestWriteEnroll:
    def test_round_trip_preserves_non_ascii(self, tmp_path):
        run_root = goal_run_root("g-7f3a2c", engine_root=tmp_path)
        enroll = base_enroll()
        write_enroll(run_root, enroll)
        assert read_enroll(run_root) == enroll
        raw = run_root.enroll_path.read_bytes()
        assert raw == json.dumps(enroll, ensure_ascii=False, indent=2).encode("utf-8")

    def test_same_content_rewrite_is_silent(self, tmp_path):
        run_root = goal_run_root("g-7f3a2c", engine_root=tmp_path)
        write_enroll(run_root, base_enroll())
        write_enroll(run_root, base_enroll())  # replay-safe no-op

    def test_different_content_raises_conflict(self, tmp_path):
        run_root = goal_run_root("g-7f3a2c", engine_root=tmp_path)
        write_enroll(run_root, base_enroll())
        other = base_enroll()
        other["title"] = "另一个标题"
        with pytest.raises(RunRootConflict):
            write_enroll(run_root, other)


# --- prepare_plan / git_argv_for -------------------------------------------


def _plan(probe_fn, engine_root):
    return prepare_plan(base_enroll(), "g-7f3a2c", engine_root=engine_root, release_exists=probe_fn)


class TestPreparePlan:
    def test_plan_shape(self, tmp_path):
        plan = _plan(lambda path, remote, branch: True, tmp_path)
        assert isinstance(plan, PreparePlan)
        assert plan.release_branch == "release/loopx-minimal"
        assert plan.run_root.root == tmp_path / "g-7f3a2c"
        assert len(plan.repos) == 2

    def test_all_exist_no_push(self, tmp_path):
        plan = _plan(lambda path, remote, branch: True, tmp_path)
        assert [r.needs_release_push for r in plan.repos] == [False, False]

    def test_all_missing_push(self, tmp_path):
        plan = _plan(lambda path, remote, branch: False, tmp_path)
        assert [r.needs_release_push for r in plan.repos] == [True, True]

    def test_mixed_per_repo(self, tmp_path):
        existing = {"/data/wt/beta"}

        def probe(path, remote, branch):
            return path in existing

        plan = _plan(probe, tmp_path)
        by_path = {r.path: r for r in plan.repos}
        assert by_path["/data/wt/alpha"].needs_release_push is True
        assert by_path["/data/wt/beta"].needs_release_push is False

    def test_release_branch_carried_to_every_repo(self, tmp_path):
        plan = _plan(lambda p, r, b: True, tmp_path)
        assert {r.release_branch for r in plan.repos} == {"release/loopx-minimal"}
        assert {r.target_branch for r in plan.repos} == {"main", "release/prod"}


class TestGitArgvFor:
    def test_fetch_only_argv(self, tmp_path):
        prep = RepoPrep(
            path="/data/wt/alpha",
            remote="origin",
            target_branch="main",
            release_branch="release/loopx-minimal",
            needs_release_push=False,
        )
        argv = git_argv_for(prep)
        assert len(argv) == 1
        fetch = argv[0]
        assert fetch[0] == "git"
        assert fetch[1] == "-c"  # guards directly follow the git token
        assert fetch.index("-C") > fetch.index("protocol.ext.allow=never")  # guards before -C
        assert fetch[fetch.index("-C") + 1] == "/data/wt/alpha"
        assert fetch[-2:] == ["fetch", "origin"]

    def test_fetch_and_push_argv(self, tmp_path):
        prep = RepoPrep(
            path="/data/wt/alpha",
            remote="origin",
            target_branch="main",
            release_branch="release/loopx-minimal",
            needs_release_push=True,
        )
        argv = git_argv_for(prep)
        assert len(argv) == 2
        _fetch, push = argv
        assert push[0] == "git"
        assert push[1] == "-c"  # guards directly follow the git token
        assert push.index("-C") > push.index("protocol.ext.allow=never")  # guards before -C
        idx = push.index("-C")
        assert push[idx + 1] == "/data/wt/alpha"
        assert push[-2] == "origin"
        assert push[-1] == "origin/main:refs/heads/release/loopx-minimal"

    @pytest.mark.parametrize("guards", [False, True])
    def test_every_argv_is_list_of_str_with_guards(self, tmp_path, guards):
        prep = RepoPrep(
            path="/data/wt/alpha",
            remote="origin",
            target_branch="main",
            release_branch="release/loopx-minimal",
            needs_release_push=guards,
        )
        for argv in git_argv_for(prep):
            assert isinstance(argv, list)
            assert all(isinstance(part, str) for part in argv)
            assert "core.fsmonitor=false" in argv
            assert "core.hooksPath=/dev/null" in argv
            assert "protocol.ext.allow=never" in argv
