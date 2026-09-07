"""真实 Git 回归：切换 origin 后不得消费旧 remote 的 release tracking。"""

from conftest import git, head
from fleet_graph.graphs.dd_scripts import ConfigureStage


def test_missing_line_on_new_origin_prunes_old_remote_tracking(repo, tmp_path):
    base = head(repo)
    old_origin = tmp_path / "old.git"
    new_origin = tmp_path / "new.git"
    git(repo, "init", "--bare", "-q", str(old_origin))
    git(repo, "init", "--bare", "-q", str(new_origin))
    git(repo, "remote", "add", "origin", str(old_origin))
    (repo / "old-remote-product.txt").write_text("只属于旧远端的产品")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "旧远端release产物")
    stale = head(repo)
    line_ref = "refs/heads/release/canary"
    tracking = "refs/remotes/origin/release/canary"
    git(repo, "push", "-q", "origin", f"HEAD:{line_ref}")
    git(repo, "fetch", "--quiet", "origin")
    assert git(repo, "rev-parse", tracking).strip() == stale
    git(repo, "reset", "--hard", base)
    git(repo, "remote", "set-url", "origin", str(new_origin))
    # URL 切换本身不会清理 tracking；测试确实从存在旧引用的状态开始。
    assert git(repo, "rev-parse", tracking).strip() == stale
    result = ConfigureStage(repo, line_ref=line_ref, requested_base=base)._rebase_to_line_head()
    assert result["status"] == "line_branch_absent", result
    assert result["actual_head"] == ""
    assert result["rebased"] is False
    assert head(repo) == base
    assert not (repo / "old-remote-product.txt").exists()
    assert tracking not in git(repo, "for-each-ref", "--format=%(refname)", "refs/remotes/origin")
