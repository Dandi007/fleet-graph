"""Git 边界单元测试；不连接 Git hosting，不运行引擎。"""

from contextlib import nullcontext
from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest

from fleet_graph.git_ops import GitOps, GitOpsError, PRAdapter, safe_git_environment

A = "a" * 40
B = "b" * 40
C = "c" * 40
REPO = {
    "path": "/repo",
    "remote": "origin",
    "remote_url": "git@example.com:org/repo.git",
    "target_branch": "main",
    "platform": "gitlab",
}
DD = {
    "source_branch": "dev/a",
    "target_branch": "release/a",
    "worktree": "/wt",
    "spec_path": "SPEC.md",
}


def ops():
    g = GitOps(runner=Mock())
    g._repo = Mock(return_value=dict(REPO))
    g._branch = Mock()
    g._lock = Mock(side_effect=lambda *args: nullcontext())
    return g


def test_runner_guards_and_environment(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/wrong")
    runner = Mock(return_value=CompletedProcess([], 0, "", ""))
    GitOps(runner)._git("/repo", "status")
    argv = runner.call_args.args[0]
    assert "core.fsmonitor=false" in argv
    assert "core.hooksPath=/dev/null" in argv
    assert "protocol.ext.allow=never" in argv
    assert "GIT_DIR" not in safe_git_environment()


def test_prepare_existing_requires_takeover():
    g = ops()
    g._fetch = Mock(return_value=A)
    g._remote_head = Mock(return_value=B)
    with pytest.raises(GitOpsError, match="显式接管"):
        g.prepare_repo(REPO, "release/a")


def test_prepare_creates_with_absent_ref_cas():
    g = ops()
    g._fetch = Mock(return_value=A)
    g._remote_head = Mock(return_value=None)
    g._push = Mock()
    result = g.prepare_repo(REPO, "release/a")
    assert result["source_head"] == A
    g._push.assert_called_once_with(REPO, "release/a", None, A)


def test_takeover_rejects_wrong_ancestry():
    g = ops()
    g._fetch = Mock(side_effect=[A, B])
    g._remote_head = Mock(return_value=B)
    g._ancestor = Mock(return_value=False)
    with pytest.raises(GitOpsError, match="未包含"):
        g.prepare_repo(REPO, "release/a", takeover=True)


def test_handoff_rejects_other_git_common_dir():
    g = ops()
    g._common = Mock(side_effect=["/foreign", "/repo"])
    with pytest.raises(GitOpsError, match="不属于"):
        g.verify_handoff(REPO, DD)


@pytest.mark.parametrize(
    "values,code",
    [
        (["other"], "WRONG_BRANCH"),
        (["dev/a", " M file"], "DIRTY_WORKTREE"),
        (["dev/a", "", A], "REMOTE_HEAD_CONFLICT"),
    ],
)
def test_handoff_rejects_unready(values, code):
    g = ops()
    g._common = Mock(return_value="/common")
    g._text = Mock(side_effect=values)
    g._remote_head = Mock(return_value=B)
    with pytest.raises(GitOpsError) as exc:
        g.verify_handoff(REPO, DD)
    assert exc.value.code == code


def test_handoff_accepts_committed_spec():
    g = ops()
    g._common = Mock(return_value="/common")
    g._text = Mock(side_effect=["dev/a", "", A, f"100644 blob {B}\tSPEC.md"])
    g._remote_head = Mock(return_value=A)
    assert g.verify_handoff(REPO, DD)["head"] == A


def test_merge_recovery_does_not_repeat_push():
    g = ops()
    g._fetch = Mock(return_value=B)
    g._ancestor = Mock(return_value=True)
    g._push = Mock()
    result = g.merge(REPO, "dev/a", "release/a", A)
    assert result["status"] == "merged" and result["recovered"]
    g._push.assert_not_called()


def test_merge_source_changed_requires_review():
    g = ops()
    g._fetch = Mock(side_effect=[B, C])
    g._ancestor = Mock(return_value=False)
    g._push = Mock()
    assert g.merge(REPO, "dev/a", "release/a", A)["status"] == "review_required"
    g._push.assert_not_called()


def test_merge_ff_cas():
    g = ops()
    g._fetch = Mock(side_effect=[B, A])
    g._ancestor = Mock(side_effect=[False, True])
    g._remote_head = Mock(return_value=A)
    g._push = Mock()
    assert g.merge(REPO, "dev/a", "release/a", A)["status"] == "merged"
    g._push.assert_called_once_with(REPO, "release/a", B, A)


def test_merge_non_ff_requires_review_after_updating_source():
    g = ops()
    g._fetch = Mock(side_effect=[B, A])
    g._ancestor = Mock(return_value=False)
    g._common = Mock(return_value="/common")
    g._text = Mock(side_effect=["dev/a", A, "", C])
    g._git = Mock(return_value=CompletedProcess([], 0, "", ""))
    g._push = Mock()
    result = g.merge(REPO, "dev/a", "release/a", A, worktree="/wt")
    assert result["status"] == "review_required" and result["head"] == C
    g._push.assert_called_once_with(REPO, "dev/a", A, C)


def test_merge_conflicts_preserved_for_impl():
    g = ops()
    g._fetch = Mock(side_effect=[B, A])
    g._ancestor = Mock(return_value=False)
    g._common = Mock(return_value="/common")
    g._text = Mock(side_effect=["dev/a", A, "", "conflicted.py"])
    g._git = Mock(return_value=CompletedProcess([], 1, "", "conflict"))
    g._push = Mock()
    result = g.merge(REPO, "dev/a", "release/a", A, worktree="/wt")
    assert result["status"] == "conflict"
    assert result["files"] == ["conflicted.py"]
    g._push.assert_not_called()
    assert not any("reset" in c.args or "--abort" in c.args for c in g._git.call_args_list)


def test_push_uses_exact_lease_and_checks_remote():
    g = ops()
    g._git = Mock(return_value=CompletedProcess([], 1, "", "network error"))
    g._remote_head = Mock(return_value=B)
    g._push(REPO, "release/a", A, B)
    assert f"--force-with-lease=refs/heads/release/a:{A}" in g._git.call_args.args
    g._remote_head.return_value = C
    with pytest.raises(GitOpsError):
        g._push(REPO, "release/a", A, B)


def test_cleanup_only_terminal_and_protected_branches():
    g = ops()
    assert g.cleanup(REPO, DD)["reason"] == "not_terminal"
    with pytest.raises(GitOpsError, match="目标分支"):
        g.cleanup(REPO, {**DD, "status": "merged", "source_branch": "main"})


def test_cleanup_preserves_dirty_worktree(tmp_path):
    g = ops()
    g._common = Mock(return_value="/common")
    g._text = Mock(side_effect=["dev/a", "!! cache/"])
    g._git = Mock()
    result = g.cleanup(REPO, {**DD, "status": "merged", "worktree": str(tmp_path)})
    assert result["reason"] == "dirty_worktree"
    g._git.assert_not_called()


def test_pr_recovers_by_marker_without_create():
    adapter = PRAdapter(runner=Mock())
    pr = {
        "number": 1,
        "body": "<!-- fleet:1 -->",
        "state": "OPEN",
        "headRefName": DD["source_branch"],
        "baseRefName": DD["target_branch"],
    }
    adapter.list = Mock(return_value=[pr])
    assert adapter.ensure(REPO, DD, "<!-- fleet:1 -->") == pr
    adapter.runner.assert_not_called()


def test_pr_ambiguous_marker_rejected():
    adapter = PRAdapter(runner=Mock())
    pr = {
        "number": 1,
        "body": "marker",
        "state": "OPEN",
        "headRefName": DD["source_branch"],
        "baseRefName": DD["target_branch"],
    }
    adapter.list = Mock(return_value=[pr, {**pr, "number": 2}])
    with pytest.raises(GitOpsError, match="多个 PR"):
        adapter.ensure(REPO, DD, "marker")


@pytest.mark.parametrize("head,expected", [(A, "merged"), (C, "PR_MERGE_UNCONFIRMED")])
def test_platform_merge_recovery_binds_reviewed_head(head, expected):
    g = ops()
    g._fetch = Mock(return_value=B)
    g._ancestor = Mock(side_effect=[False, True])
    g.pr_adapter = Mock()
    g.pr_adapter.get.return_value = {
        "state": "MERGED",
        "mergeCommit": {"oid": B},
        "headRefOid": head,
        "headRefName": "dev/a",
        "baseRefName": "release/a",
    }
    g._push = Mock()
    if expected == "merged":
        result = g.merge(REPO, "dev/a", "release/a", A, pr={"number": 1})
        assert result["status"] == expected
    else:
        with pytest.raises(GitOpsError) as exc:
            g.merge(REPO, "dev/a", "release/a", A, pr={"number": 1})
        assert exc.value.code == expected
    g._push.assert_not_called()


@pytest.mark.parametrize("platform,command", [("github", "gh"), ("gitlab", "glab")])
def test_pr_listing_uses_selected_platform_without_network(platform, command):
    runner = Mock(return_value=CompletedProcess([], 0, "[]", ""))
    assert PRAdapter(runner).list({**REPO, "platform": platform}, "dev/a", "release/a") == []
    args = runner.call_args.args[0]
    assert args[0] == command
    assert "dev/a" in args and "release/a" in args


def test_prepare_operation_recovers_own_created_release_only(tmp_path):
    g = ops()
    repo = {**REPO, "operation_id": "operation"}
    g._repo.return_value = repo
    g._common = Mock(return_value=tmp_path)
    g._fetch = Mock(return_value=A)
    g._remote_head = Mock(return_value=None)
    g._push = Mock()
    assert g.prepare_repo(repo, "release/a")["source_head"] == A
    g._remote_head.return_value = A
    g._ancestor = Mock(return_value=True)
    assert g.prepare_repo(repo, "release/a")["source_head"] == A
    g._push.assert_called_once()
    g._remote_head.return_value = B
    with pytest.raises(GitOpsError, match="显式接管"):
        g.prepare_repo(repo, "release/a")


def test_inspect_refs_does_not_require_release_takeover():
    g = ops()
    g._fetch = Mock(side_effect=[A, B])
    result = g.inspect_refs(REPO, "release/a")
    assert result["source_head"] == A and result["target_head"] == B


def test_cleanup_missing_worktree_continues_pr_and_branch_finalization(tmp_path):
    g = ops()
    g._remote_head = Mock(side_effect=[A, None])
    g._git = Mock(return_value=CompletedProcess([], 0, A, ""))
    g._text = Mock(return_value="")
    g.pr_adapter = Mock()
    g.pr_adapter.close.return_value = {"state": "CLOSED"}
    result = g.cleanup(
        REPO,
        {
            **DD,
            "status": "done",
            "head": A,
            "pr": {"number": 1},
            "worktree": str(tmp_path / "already-removed"),
        },
    )
    assert result["status"] == "cleaned" and result["pr"]["state"] == "CLOSED"
    args = [c.args for c in g._git.call_args_list]
    assert any(
        f"--force-with-lease=refs/heads/dev/a:{A}" in a and ":refs/heads/dev/a" in a for a in args
    )
    assert any(a[1:] == ("update-ref", "-d", "refs/heads/dev/a", A) for a in args)


def test_cleanup_does_not_delete_new_remote_commit(tmp_path):
    g = ops()
    g._remote_head = Mock(return_value=B)
    g._git = Mock()
    result = g.cleanup(
        REPO, {**DD, "status": "done", "head": A, "worktree": str(tmp_path / "missing")}
    )
    assert result["reason"] == "remote_head_changed"
    g._git.assert_not_called()


def test_cleanup_dirty_tree_closes_pr_but_keeps_files_and_refs(tmp_path):
    g = ops()
    g._common = Mock(return_value="/common")
    g._text = Mock(side_effect=["dev/a", "!! cache", A])
    g._remote_head = Mock(return_value=A)
    g._git = Mock(return_value=CompletedProcess([], 0, A, ""))
    g.pr_adapter = Mock()
    g.pr_adapter.close.return_value = {"state": "CLOSED"}
    result = g.cleanup(
        REPO,
        {**DD, "status": "cancelled", "head": A, "worktree": str(tmp_path), "pr": {"number": 1}},
    )
    assert result["reason"] == "dirty_worktree"
    assert result["pr"]["state"] == "CLOSED"
    assert len(g._git.call_args_list) == 1
    assert g._git.call_args.args[1] == "rev-parse"


def test_pr_close_reports_actual_platform_closed_state():
    adapter = PRAdapter(runner=Mock())
    pr = {
        "number": 1,
        "state": "OPEN",
        "headRefOid": A,
        "headRefName": "dev/a",
        "baseRefName": "release/a",
    }
    adapter.get = Mock(side_effect=[pr, {**pr, "state": "CLOSED"}])
    adapter._run = Mock()
    result = adapter.close(REPO, pr, "dev/a", "release/a", expected_head=A, merged=True)
    assert result["state"] == "CLOSED" and result["reason"] == "closed_after_git_merge"
    assert adapter._run.call_args.args[1][:3] == ["glab", "mr", "close"]


def test_pr_close_refuses_new_head():
    adapter = PRAdapter(runner=Mock())
    pr = {
        "number": 1,
        "state": "OPEN",
        "headRefOid": B,
        "headRefName": "dev/a",
        "baseRefName": "release/a",
    }
    adapter.get = Mock(return_value=pr)
    with pytest.raises(GitOpsError, match="HEAD 已变化"):
        adapter.close(REPO, pr, "dev/a", "release/a", expected_head=A)
    adapter.runner.assert_not_called()


def test_recover_merge_never_publishes_when_not_merged():
    g = ops()
    g._fetch = Mock(return_value=B)
    g._ancestor = Mock(return_value=False)
    g._push = Mock()
    assert g.recover_merge(REPO, "dev/a", "release/a", A)["status"] == "review_required"
    g._push.assert_not_called()
