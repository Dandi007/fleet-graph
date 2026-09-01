"""M3 交付 B：harvest ReAct 子图单测。

覆盖 spec 交付 D.2：

1. E5 approved_unharvested 事件 -> 编排步骤齐全（SOP_STEPS 全走一遍）。
2. 后置条件三要素（PR merged + verify 零退出 + evidence note 存在）缺一即
   escalated（失败/升报），绝不采信子图自述。
3. allowlist 拒绝路径：非白名单 repo / 分支 / 部署脚本 -> refused + 留痕，
   不执行任何写（fake ops 记录零次调用）。

所有 git/部署操作都是注入的 fake ops，绝不触碰真实网络或生产 checkout。
回归 rc-702098ab：DefaultHarvestOps 成功路径必须保留 worktree 供 run_verify
使用，verify 之后才由 remove_worktree 清理。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import git, head
from fleet_graph.supervise.events import approved_unharvested_event, validate_event
from fleet_graph.supervise.harvest import (
    OUTCOME_ALREADY_HARVESTED,
    OUTCOME_ESCALATED,
    OUTCOME_HARVESTED,
    OUTCOME_REFUSED,
    SOP_STEPS,
    HarvestDeps,
    HarvestRunConfig,
    _resolve_repo,
    authorize_harvest_write,
    build_harvest_graph,
    run_harvest,
)
from fleet_graph.supervise.harvest_allowlist import HarvestAllowlist, parse_harvest_allowlist


def fake_ops(
    *,
    cherry_equivalent: bool = False,
    merged: bool = True,
    pr_url: str = "https://github.com/Dandi007/fleet-harvest-sandbox/pull/1",
    board_card_entity_id: str | None = "card-xyz",
    worktree_ok: bool = True,
    verify_exit: int = 0,
    verify_real_exit: int = 0,
    deploy_exit: int = 0,
    fetch_ok: bool = True,
    pull_ok: bool = True,
    resolve_canonical: Path | None = None,
    harvest_tip: str = "b" * 40,
) -> dict[str, Any]:
    """A recording fake ops: every write/execute is recorded, results scripted."""
    calls: list[str] = []
    pr_merge_args: list[dict[str, Any]] = []

    class Ops:
        def fetch_dd_ref(self, repo: Path, development_id: str) -> dict[str, Any]:
            calls.append("fetch_dd_ref")
            return {"ok": fetch_ok}

        def resolve_canonical_repo(
            self,
            record_repo_path: str,
            record_remote_url: str | None,
            allowlist_repo_paths: list[str],
        ) -> tuple[Path | None, str]:
            # 读口：不构成写原语，不入 calls（calls 只记录写/执行动作）。
            if resolve_canonical is not None:
                return resolve_canonical, ""
            return Path(record_repo_path), ""

        def cherry_equivalent(self, repo: Path, head_commit: str, default_branch: str) -> bool:
            calls.append("cherry_equivalent")
            return cherry_equivalent

        def worktree_cherry_pick(
            self, repo: Path, head_commit: str, default_branch: str, worktree_root: Path
        ) -> dict[str, Any]:
            calls.append("worktree_cherry_pick")
            return {"ok": worktree_ok, "method": "cherry-pick", "harvest_tip": harvest_tip}

        def build_harvest_tip(
            self,
            repo: Path,
            head_commit: str,
            default_branch: str,
            worktree_root: Path,
        ) -> dict[str, Any]:
            calls.append("build_harvest_tip")
            return {"ok": worktree_ok, "harvest_tip": harvest_tip}

        def remove_worktree(self, repo: Path, worktree_root: Path) -> dict[str, Any]:
            calls.append("remove_worktree")
            return {"ok": True}

        def run_verify(self, worktree: Path, argv: list[str]) -> int:
            calls.append("run_verify")
            return verify_exit

        def board_card_entity_id(self, development_id: str, dd_root: Path) -> str | None:
            calls.append("board_card_entity_id")
            return board_card_entity_id

        def pr_squash_merge(
            self, repo: Path, development_id: str, head_commit: str, default_branch: str
        ) -> dict[str, Any]:
            calls.append("pr_squash_merge")
            pr_merge_args.append(
                {
                    "development_id": development_id,
                    "head_commit": head_commit,
                    "default_branch": default_branch,
                }
            )
            return {"merged": merged, "pr_url": pr_url, "method": "gh-pr-squash-merge"}

        def ff_only_pull(self, repo: Path, default_branch: str) -> dict[str, Any]:
            calls.append("ff_only_pull")
            return {"ok": pull_ok}

        def deploy(self, command: list[str]) -> int:
            calls.append("deploy")
            return deploy_exit

        def verify_real(self, argv: list[str]) -> int:
            calls.append("verify_real")
            return verify_real_exit

    return {"ops": Ops(), "calls": calls, "pr_merge_args": pr_merge_args}


def full_allowlist(
    repo_path: str = "/data/code/self/fleet-graph", **overrides: Any
) -> HarvestAllowlist:
    raw: dict[str, Any] = {
        "entries": [
            {
                "repo_path": repo_path,
                "allowed_branches": ["refs/heads/main"],
                "allowed_deploy": [["make", "deploy"]],
            }
        ]
    }
    raw.update(overrides)
    return parse_harvest_allowlist(raw)


def repo_path_for(tmp_path: Path, name: str = "repos/fleet-graph") -> str:
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    return str(repo)


def dd_record_root(tmp_path: Path, repo_path: str, card_entity_id: str | None = None) -> Path:
    dd_root = tmp_path / "dd"
    dev_dir = dd_root / "dev-x"
    dev_dir.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {"development_id": "dev-x", "repo_path": repo_path}
    if card_entity_id is not None:
        record["card_entity_id"] = card_entity_id
    (dev_dir / "record.json").write_text(json.dumps(record), encoding="utf-8")
    return dd_root


def dd_record_root_with_remote_url(tmp_path: Path, repo_path: str, remote_url: str) -> Path:
    """与 dd_record_root 同构，但 record 显式携带 remote_url（spec 交付 A 新读字段）。"""
    dd_root = tmp_path / "dd"
    dev_dir = dd_root / "dev-x"
    dev_dir.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "development_id": "dev-x",
        "repo_path": repo_path,
        "remote_url": remote_url,
    }
    (dev_dir / "record.json").write_text(json.dumps(record), encoding="utf-8")
    return dd_root


def e5_event() -> dict[str, Any]:
    return approved_unharvested_event(
        development_id="dev-x", head_commit="a" * 40, stage="implement"
    ).as_dict()


def config_for(
    tmp_path: Path,
    *,
    allowlist: HarvestAllowlist | None = None,
    repo_path: str | None = None,
    deploy_command: list[str] | None = None,
    ops: Any | None = None,
    bus: Any | None = None,
    publish_notes: bool = True,
    **overrides: Any,
) -> tuple[HarvestRunConfig, dict[str, Any]]:
    fake = ops or fake_ops()
    repo = repo_path or repo_path_for(tmp_path)
    config = HarvestRunConfig(
        event=e5_event(),
        state_root=tmp_path / "supervisor",
        run_root=tmp_path / "runs",
        dd_root=dd_record_root(tmp_path, repo),
        deploy_command=deploy_command or [],
        allowlist=allowlist or full_allowlist(repo),
        ops=fake["ops"],
        bus=bus,
        publish_notes=publish_notes,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config, fake


class FakeBus:
    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.published: list[dict[str, Any]] = []

    def publish(self, channel, kind, payload, idempotency_key, *, refs=None, **_kw):
        if self.refuse:
            raise RuntimeError("HTTP 503: bus down")

        class _Result:
            message_id = f"msg_harvest_{len(self.published)}"

        self.published.append(
            {
                "channel": channel,
                "kind": kind,
                "payload": payload,
                "idempotency_key": idempotency_key,
                "refs": refs or [],
            }
        )
        return _Result()


class TestHarvestAllowlistRefusal:
    """交付 D.1 + 铁律：非白名单 -> 拒绝 + 留痕，不执行任何写。"""

    def test_repo_outside_allowlist_refuses_and_writes_nothing(self, tmp_path: Path) -> None:
        fake = fake_ops()
        repo = repo_path_for(tmp_path, "repos/other")
        other = repo_path_for(tmp_path, "repos/fleet-graph")
        config, _ = config_for(
            tmp_path,
            repo_path=repo,
            allowlist=full_allowlist(other),
            ops=fake,
        )
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_REFUSED
        assert fake["calls"] == [], f"write primitives executed: {fake['calls']}"
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["allowlist_auth"]["granted"] is False
        assert any("不在收割写白名单" in r for r in receipt["allowlist_auth"]["reasons"])
        # 拒绝也留痕：gate 步骤记录 evidence。
        assert any(
            step["step"] == "gate" and step.get("evidence", {}).get("granted") is False
            for step in receipt["steps"]
        )

    def test_default_deny_all_refuses_everything(self, tmp_path: Path) -> None:
        fake = fake_ops()
        config, _ = config_for(tmp_path, allowlist=HarvestAllowlist.default(), ops=fake)
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_REFUSED
        assert fake["calls"] == []

    def test_deploy_outside_allowlist_refuses_without_executing(self, tmp_path: Path) -> None:
        fake = fake_ops()
        config, _ = config_for(tmp_path, deploy_command=["/bin/rm", "-rf", "/"], ops=fake)
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_REFUSED
        assert fake["calls"] == []

    def test_per_write_gate_still_holds_after_the_main_gate(self, tmp_path: Path) -> None:
        """belt-and-braces：主 gate 通过后，每个写步骤仍各自校验 allowlist。"""
        # main branch allowed, but the deploy command is not: the main gate sees
        # a non-empty deploy command and refuses before any write step.
        fake = fake_ops()
        allowlist = full_allowlist(
            entries=[
                {
                    "repo_path": "/data/code/self/fleet-graph",
                    "allowed_branches": ["refs/heads/main"],
                    "allowed_deploy": [["make", "deploy"]],
                }
            ]
        )
        config, _ = config_for(
            tmp_path,
            allowlist=allowlist,
            deploy_command=["something", "else"],
            ops=fake,
        )
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_REFUSED
        assert fake["calls"] == []


class TestHarvestOrchestration:
    """交付 D.2：E5 事件 -> 编排步骤齐全。"""

    def test_full_sop_runs_all_steps_and_harvests(self, tmp_path: Path) -> None:
        fake = fake_ops()
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_HARVESTED
        assert result["steps"]
        ran_steps = [s["step"] for s in result["steps"]]
        for step in SOP_STEPS:
            assert step in ran_steps, f"{step} missing from {ran_steps}"
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["pr_merged"] is True
        assert receipt["verify_exit_code"] == 0

    def test_steps_are_recorded_in_receipt_with_mechanical_facts(self, tmp_path: Path) -> None:
        fake = fake_ops(verify_exit=0)
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        run_harvest(config)
        receipt = json.loads(Path(config.state_root / "reports" / "e5-dev-x.json").read_text())
        run_verify = next(s for s in receipt["steps"] if s["step"] == "run_verify")
        assert run_verify["exit_code"] == 0
        assert run_verify["argv"] == ["make", "verify"]

    def test_e5_resolves_repo_from_dd_record(self, tmp_path: Path) -> None:
        fake = fake_ops()
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_HARVESTED
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["repo_path"] == str(config.dd_root.parent / "repos" / "fleet-graph")

    def test_finished_event_is_a_no_op_on_rerun(self, tmp_path: Path) -> None:
        fake = fake_ops()
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        first = run_harvest(config)
        second = run_harvest(config)
        assert second["resumed"] == "already_complete"
        assert second["receipt_path"] == first["receipt_path"]
        assert fake["calls"].count("fetch_dd_ref") == 1, "re-run re-executed a write"


class TestPostconditions:
    """交付 D.2：后置条件三要素缺一即失败/升报。"""

    def test_unmerged_pr_escalates(self, tmp_path: Path) -> None:
        fake = fake_ops(merged=False)
        config, _ = config_for(tmp_path, ops=fake)
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        missing = [item for s in receipt["steps"] for item in (s.get("missing") or [])]
        assert any("PR merged 未达成" in item for item in missing)

    def test_failed_verify_escalates(self, tmp_path: Path) -> None:
        fake = fake_ops(verify_exit=1)
        config, _ = config_for(tmp_path, ops=fake)
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        missing = [item for s in receipt["steps"] for item in (s.get("missing") or [])]
        assert any("verify 命令退出码" in item for item in missing)

    def test_missing_evidence_note_escalates(self, tmp_path: Path) -> None:
        fake = fake_ops()
        config, _ = config_for(tmp_path, ops=fake, publish_notes=False, bus=None)
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        missing = [item for s in receipt["steps"] for item in (s.get("missing") or [])]
        assert any("evidence note 不存在" in item for item in missing)

    def test_refused_evidence_publish_escalates(self, tmp_path: Path) -> None:
        fake = fake_ops()
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus(refuse=True))
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED

    def test_evidence_note_is_published_on_success(self, tmp_path: Path) -> None:
        fake = fake_ops()
        bus = FakeBus()
        config, _ = config_for(tmp_path, ops=fake, bus=bus)
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_HARVESTED
        [note] = bus.published
        assert note["payload"]["note_type"] == "evidence"
        assert note["refs"] == [{"target_entity": "card-xyz"}]

    def test_pr_url_lands_on_receipt_and_state(self, tmp_path: Path) -> None:
        fake = fake_ops(pr_url="https://github.com/Dandi007/fleet-harvest-sandbox/pull/1")
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_HARVESTED
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["pr_url"] == "https://github.com/Dandi007/fleet-harvest-sandbox/pull/1"
        pr_step = next(s for s in receipt["steps"] if s["step"] == "pr_squash_merge")
        assert pr_step["pr_url"] == "https://github.com/Dandi007/fleet-harvest-sandbox/pull/1"

    def test_missing_pr_url_escalates(self, tmp_path: Path) -> None:
        """fake ops 返回 merged=True 但 pr_url 为空 -> escalated（链接缺失）。"""
        fake = fake_ops(merged=True, pr_url="")
        config, _ = config_for(tmp_path, ops=fake)
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        missing = [item for s in receipt["steps"] for item in (s.get("missing") or [])]
        assert any("PR merged 链接缺失" in item for item in missing)

    def test_pr_merged_false_still_escalates(self, tmp_path: Path) -> None:
        """旧 negative 零回归：merged=False -> escalated。"""
        fake = fake_ops(merged=False, pr_url="")
        config, _ = config_for(tmp_path, ops=fake)
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        missing = [item for s in receipt["steps"] for item in (s.get("missing") or [])]
        assert any("PR merged 未达成" in item for item in missing)

    def test_evidence_note_targets_real_board_card_entity(self, tmp_path: Path) -> None:
        """evidence 用真实板卡实体 id：payload 与 refs 都填 card-xyz，绝不填 dev-x。"""
        fake = fake_ops(board_card_entity_id="card-xyz")
        bus = FakeBus()
        config, _ = config_for(tmp_path, ops=fake, bus=bus)
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_HARVESTED
        assert len(bus.published) == 1, f"published: {bus.published}"
        note = bus.published[0]
        assert note["payload"]["card_entity_id"] == "card-xyz"
        assert note["refs"] == [{"target_entity": "card-xyz"}]
        targets = {ref["target_entity"] for ref in note["refs"]}
        assert "dev-x" not in targets

    def test_evidence_note_skips_when_no_board_card(self, tmp_path: Path) -> None:
        """无卡（fake ops -> None）：零发布 + evidence_note ok=False + detail 含「缺失」。

        无卡 -> evidence 步 best-effort skip（零发布）；postconditions 因缺
        evidence_note_id 自然 escalated。
        """
        fake = fake_ops(board_card_entity_id=None)
        bus = FakeBus()
        config, _ = config_for(tmp_path, ops=fake, bus=bus)
        result = run_harvest(config)
        assert bus.published == [], f"published: {bus.published}"
        evidence = next(s for s in result["steps"] if s["step"] == "evidence_note")
        assert evidence["ok"] is False
        assert "缺失" in evidence["detail"]
        assert "未挂卡" in evidence["detail"]


class TestCherryDedup:
    def test_already_harvested_is_a_no_op(self, tmp_path: Path) -> None:
        fake = fake_ops(cherry_equivalent=True)
        config, _ = config_for(tmp_path, ops=fake)
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ALREADY_HARVESTED
        # SOP 顺序：fetch dd ref -> cherry 判重。判重命中后没有任何写动作
        # （worktree/verify/merge/pull/deploy/verify_real/evidence 都不跑）。
        assert fake["calls"] == ["fetch_dd_ref", "cherry_equivalent"], fake["calls"]


class TestUnresolvableEvent:
    def test_missing_repo_record_escalates_without_writes(self, tmp_path: Path) -> None:
        fake = fake_ops()
        config, _ = config_for(tmp_path, ops=fake)
        config.dd_root = tmp_path / "absent"
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        assert fake["calls"] == []

    def test_missing_head_commit_escalates(self, tmp_path: Path) -> None:
        fake = fake_ops()
        config, _ = config_for(tmp_path, ops=fake)
        config.event = {
            **e5_event(),
            "payload": {"development_id": "dev-x", "head_commit": "", "stage": "implement"},
        }
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        assert fake["calls"] == []


class TestGraphDispatch:
    def test_build_harvest_graph_returns_a_compilable_graph(self, tmp_path: Path) -> None:
        fake = fake_ops()
        config, _ = config_for(tmp_path, ops=fake)
        graph = build_harvest_graph(
            HarvestDeps(
                allowlist=config.allowlist,
                state_root=config.state_root,
                run_root=config.run_root,
                thread_id=validate_event(config.event).thread_id,
                ops=fake["ops"],
            )
        )
        assert graph is not None


class TestDefaultHarvestOpsBoardCardRead:
    """交付 A：DefaultHarvestOps.board_card_entity_id 读 dd 准入 record 的 card_entity_id。"""

    def test_reads_card_entity_id_from_dd_record(self, tmp_path: Path) -> None:
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        dd_root = dd_record_root(tmp_path, "/data/code/self/fleet-graph", card_entity_id="card-xyz")
        ops = DefaultHarvestOps()
        assert ops.board_card_entity_id("dev-x", dd_root) == "card-xyz"

    def test_missing_or_empty_card_entity_id_returns_none(self, tmp_path: Path) -> None:
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo = repo_path_for(tmp_path)
        dd_root = tmp_path / "dd"
        dev_a = dd_root / "dev-a"
        dev_a.mkdir(parents=True)
        (dev_a / "record.json").write_text(
            json.dumps({"development_id": "dev-a", "repo_path": repo}), encoding="utf-8"
        )
        dev_b = dd_root / "dev-b"
        dev_b.mkdir(parents=True)
        (dev_b / "record.json").write_text(
            json.dumps({"development_id": "dev-b", "repo_path": repo, "card_entity_id": None}),
            encoding="utf-8",
        )
        dev_c = dd_root / "dev-c"
        dev_c.mkdir(parents=True)
        (dev_c / "record.json").write_text(
            json.dumps({"development_id": "dev-c", "repo_path": repo, "card_entity_id": ""}),
            encoding="utf-8",
        )
        dev_bad = dd_root / "dev-bad"
        dev_bad.mkdir(parents=True)
        (dev_bad / "record.json").write_text("not json", encoding="utf-8")
        ops = DefaultHarvestOps()
        assert ops.board_card_entity_id("dev-a", dd_root) is None
        assert ops.board_card_entity_id("dev-b", dd_root) is None
        assert ops.board_card_entity_id("dev-c", dd_root) is None
        assert ops.board_card_entity_id("dev-bad", dd_root) is None
        assert ops.board_card_entity_id("dev-missing", dd_root) is None


class TestDefaultHarvestOpsWorktreeLifecycle:
    """回归 rc-702098ab：真实 git 上，成功路径的 worktree 必须活到 run_verify。

    rc-702098ab：`worktree_cherry_pick` 的 finally 无条件删除 worktree，但
    `run_verify` 要在同一路径跑 make verify -> 必然 127，子图永远到不了
    HARVESTED。这里用真实 git 验证：cherry-pick 成功后 worktree 仍在，
    run_verify 对同一目录能 0 退出，之后 remove_worktree 才清理。
    """

    def test_worktree_survives_cherry_pick_through_verify_then_is_removed(
        self, tmp_path: Path
    ) -> None:
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "test@example.invalid")
        git(repo, "config", "user.name", "test")
        (repo / "base.txt").write_text("base\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "seed")

        git(repo, "checkout", "-q", "-b", "feature")
        (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "product change")
        product_commit = head(repo)
        git(repo, "checkout", "-q", "main")

        ops = DefaultHarvestOps()
        worktree_root = tmp_path / "harvest-worktree"

        picked = ops.worktree_cherry_pick(repo, product_commit, "main", worktree_root)
        assert picked["ok"] is True, picked
        assert worktree_root.is_dir(), "worktree removed before run_verify (rc-702098ab)"
        assert (worktree_root / "feature.txt").read_text().strip() == "feature"

        verify_exit = ops.run_verify(worktree_root, ["/bin/true"])
        assert verify_exit == 0, "run_verify failed against a surviving worktree"

        removed = ops.remove_worktree(repo, worktree_root)
        assert removed["ok"] is True, removed
        assert not worktree_root.exists(), "worktree not cleaned up after verify"

    def test_conflict_path_still_reports_conflicts(self, tmp_path: Path) -> None:
        """冲突路径行为不变：worktree add 失败/冲突都如实报告，不强行覆盖。"""
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "test@example.invalid")
        git(repo, "config", "user.name", "test")
        (repo / "shared.txt").write_text("base\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "seed")

        git(repo, "checkout", "-q", "-b", "feature")
        (repo / "shared.txt").write_text("feature\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "product change")
        product_commit = head(repo)
        git(repo, "checkout", "-q", "main")
        (repo / "shared.txt").write_text("main changed\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "main change")

        ops = DefaultHarvestOps()
        worktree_root = tmp_path / "harvest-worktree"
        result = ops.worktree_cherry_pick(repo, product_commit, "main", worktree_root)
        assert result.get("conflicts") is True or result["ok"] is False
        assert not worktree_root.exists(), "failed worktree left behind"


def _init_git_repo(repo: Path) -> None:
    """一个可收割的真 git 仓（含 origin remote），全部本地合成，禁触真网。"""
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "test")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "seed")


def _canonical_allowlist(canonical: Path) -> HarvestAllowlist:
    return full_allowlist(str(canonical))


class TestCanonicalRepoResolution:
    """M3 第二轮根因修复：harvest 解析 canonical 目标仓（spec 交付 A/C）。

    真机回执 granted=false 的根因是 record.repo_path 是每单一次的 linked
    worktree，而 allowlist 按 canonical 仓签发。这里用真实 git worktree +
    本地合成仓验证：`_resolve_repo` 把 worktree 解析成 canonical 主 checkout，
    授权 granted=True；阴性（非白名单 canonical / 无 origin / worktree 路径
    本身）仍 deny/refused，绝不拉宽 deny-all。全程禁触真网/生产 checkout。
    """

    def test_linked_worktree_resolves_to_canonical_main_checkout(self, tmp_path: Path) -> None:
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        canonical = tmp_path / "canon"
        _init_git_repo(canonical)
        git(canonical, "remote", "add", "origin", "https://example.invalid/x.git")
        worktree = tmp_path / "linked-wt"
        git(canonical, "worktree", "add", "--detach", str(worktree), "main")

        allowlist = _canonical_allowlist(canonical)
        resolved, gaps = _resolve_repo(
            "dev-x",
            dd_record_root(tmp_path, str(worktree)),
            DefaultHarvestOps(),
            [e.repo_path for e in allowlist.entries],
        )
        assert gaps == []
        assert resolved == canonical, f"resolved {resolved!r} != canonical {canonical!r}"
        auth = authorize_harvest_write(
            allowlist,
            repo_path=str(resolved),
            branch="refs/heads/main",
            deploy=(),
        )
        assert auth.granted is True, auth.reasons

    def test_origin_url_mapping_hits_canonical(self, tmp_path: Path) -> None:
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        canonical = tmp_path / "canon"
        _init_git_repo(canonical)
        git(canonical, "remote", "add", "origin", "https://example.invalid/x.git")
        other = tmp_path / "other"
        _init_git_repo(other)
        git(other, "remote", "add", "origin", "https://example.invalid/x.git")

        allowlist = _canonical_allowlist(canonical)
        resolved, gaps = _resolve_repo(
            "dev-x",
            dd_record_root(tmp_path, str(other), None),
            DefaultHarvestOps(),
            [e.repo_path for e in allowlist.entries],
        )
        assert gaps == []
        assert resolved == canonical, f"origin mapping resolved {resolved!r}"
        auth = authorize_harvest_write(
            allowlist,
            repo_path=str(resolved),
            branch="refs/heads/main",
            deploy=(),
        )
        assert auth.granted is True

    def test_origin_url_mapping_uses_record_remote_url(self, tmp_path: Path) -> None:
        """record 显式 remote_url 时优先用该字段做 origin URL 映射。"""
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        canonical = tmp_path / "canon"
        _init_git_repo(canonical)
        git(canonical, "remote", "add", "origin", "https://example.invalid/x.git")
        standalone = tmp_path / "standalone"
        _init_git_repo(standalone)

        allowlist = _canonical_allowlist(canonical)
        resolved, gaps = _resolve_repo(
            "dev-x",
            dd_record_root_with_remote_url(
                tmp_path, str(standalone), "https://example.invalid/x.git"
            ),
            DefaultHarvestOps(),
            [e.repo_path for e in allowlist.entries],
        )
        assert gaps == []
        assert resolved == canonical, f"record remote_url mapping resolved {resolved!r}"
        auth = authorize_harvest_write(
            allowlist,
            repo_path=str(resolved),
            branch="refs/heads/main",
            deploy=(),
        )
        assert auth.granted is True

    def test_intake_stores_canonical_and_gate_grants(self, tmp_path: Path) -> None:
        """record 指向 worktree，intake 存 canonical，gate 对 canonical 授权。"""
        canonical = tmp_path / "canon"
        _init_git_repo(canonical)
        worktree = tmp_path / "linked-wt"
        git(canonical, "worktree", "add", "--detach", str(worktree), "main")

        fake = fake_ops(resolve_canonical=canonical)
        allowlist = _canonical_allowlist(canonical)
        config, _ = config_for(
            tmp_path,
            allowlist=allowlist,
            repo_path=str(worktree),
            ops=fake,
            bus=FakeBus(),
        )
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_HARVESTED
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["repo_path"] == str(canonical)
        assert receipt["allowlist_auth"]["granted"] is True

    def test_worktree_path_itself_is_still_denied(self, tmp_path: Path) -> None:
        """回归：直接拿 record 原始 worktree 路径授权 -> 仍 deny（不拉宽 deny-all）。"""
        canonical = tmp_path / "canon"
        _init_git_repo(canonical)
        worktree = tmp_path / "linked-wt"
        git(canonical, "worktree", "add", "--detach", str(worktree), "main")

        allowlist = _canonical_allowlist(canonical)
        auth = authorize_harvest_write(
            allowlist,
            repo_path=str(worktree),
            branch="refs/heads/main",
            deploy=(),
        )
        assert auth.granted is False
        assert any("不在收割写白名单" in r for r in auth.reasons)

    def test_non_allowlist_canonical_denies(self, tmp_path: Path) -> None:
        """解析出的 canonical 仓不在 allowlist -> 拒绝（authorize 语义不变）。"""
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        canonical = tmp_path / "canon"
        _init_git_repo(canonical)
        worktree = tmp_path / "linked-wt"
        git(canonical, "worktree", "add", "--detach", str(worktree), "main")
        other_canon = tmp_path / "other-canon"
        _init_git_repo(other_canon)

        allowlist = _canonical_allowlist(other_canon)
        resolved, gaps = _resolve_repo(
            "dev-x",
            dd_record_root(tmp_path, str(worktree)),
            DefaultHarvestOps(),
            [e.repo_path for e in allowlist.entries],
        )
        assert resolved is None
        assert gaps and any("无法解析" in g for g in gaps)

    def test_no_origin_refuses(self, tmp_path: Path) -> None:
        """无 origin 且不在 allowlist -> 解析不到 canonical -> 拒绝。"""
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        lone = tmp_path / "lone"
        _init_git_repo(lone)
        allowlist = _canonical_allowlist(tmp_path / "other")
        resolved, gaps = _resolve_repo(
            "dev-x",
            dd_record_root(tmp_path, str(lone)),
            DefaultHarvestOps(),
            [e.repo_path for e in allowlist.entries],
        )
        assert resolved is None
        assert gaps and any("无法解析" in g for g in gaps)

    def test_unresolvable_escalates_without_writes(self, tmp_path: Path) -> None:
        """解析不到任何 canonical -> intake 留 gaps -> escalated，零写动作。"""
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        lone = tmp_path / "lone"
        _init_git_repo(lone)
        allowlist = _canonical_allowlist(tmp_path / "other")
        fake = fake_ops()
        config, _ = config_for(
            tmp_path,
            allowlist=allowlist,
            repo_path=str(lone),
            ops=fake,
        )
        config.ops = DefaultHarvestOps()
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        assert fake["calls"] == []


def _ticket_repo_with_dd_artifacts(tmp_path: Path) -> tuple[Path, str]:
    """真 git 合成仓构造一张「工单 commit」：产品改动 + 两棵 dd 协议子树。

    `.dev-dispatch/`（development.json / spec/approved.md）与 `.dd-evidence/
    acceptance.json` 随产品改动一起进同一张工单 commit（dd 协议要求工单分支提交
    它们）。工单 commit 落在独立 feature 分支（模拟 dd 链，不在默认分支上——
    否则 cherry-pick 到默认分支会空提交）。全部本地合成，禁触真网/生产 checkout。
    """
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    git(repo, "checkout", "-q", "-b", "feature")

    dev_dispatch = repo / ".dev-dispatch"
    dev_dispatch.mkdir()
    (dev_dispatch / "development.json").write_text(
        '{"development_id": "dev-x"}\n', encoding="utf-8"
    )
    spec_dir = dev_dispatch / "spec"
    spec_dir.mkdir()
    (spec_dir / "approved.md").write_text("# approved spec\n", encoding="utf-8")

    evidence = repo / ".dd-evidence"
    evidence.mkdir()
    (evidence / "acceptance.json").write_text('{"ok": true}\n', encoding="utf-8")

    (repo / "product.txt").write_text("product change\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "ticket with dd artifacts")
    ticket = head(repo)
    git(repo, "checkout", "-q", "main")
    return repo, ticket


class TestHarvestCleanTipExcludesDdArtifacts:
    """交付 C.1：真实 git 合成仓上，洗树 tip 不含 dd 协议子树、产品改动保留。"""

    def test_build_harvest_tip_excludes_dd_subtrees(self, tmp_path: Path) -> None:
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo, ticket = _ticket_repo_with_dd_artifacts(tmp_path)
        ops = DefaultHarvestOps()
        worktree_root = tmp_path / "harvest-wt"

        result = ops.build_harvest_tip(repo, ticket, "main", worktree_root)
        assert result["ok"] is True, result
        tip = result["harvest_tip"]
        assert tip and tip != ticket

        paths = git(repo, "ls-tree", "-r", tip, "--name-only").splitlines()
        assert not any(p.startswith(".dev-dispatch/") or p == ".dev-dispatch" for p in paths), paths
        assert not any(p.startswith(".dd-evidence/") or p == ".dd-evidence" for p in paths), paths
        # 产品改动仍保留。
        assert "product.txt" in paths
        assert "seed.txt" in paths
        assert git(repo, "show", f"{tip}:product.txt").strip() == "product change"

    def test_worktree_cherry_pick_returns_clean_tip(self, tmp_path: Path) -> None:
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo, ticket = _ticket_repo_with_dd_artifacts(tmp_path)
        ops = DefaultHarvestOps()
        worktree_root = tmp_path / "harvest-wt"

        picked = ops.worktree_cherry_pick(repo, ticket, "main", worktree_root)
        assert picked["ok"] is True, picked
        tip = picked["harvest_tip"]
        assert tip and tip != ticket

        paths = git(repo, "ls-tree", "-r", tip, "--name-only").splitlines()
        assert not any(p.startswith(".dev-dispatch/") or p == ".dev-dispatch" for p in paths), paths
        assert not any(p.startswith(".dd-evidence/") or p == ".dd-evidence" for p in paths), paths
        assert "product.txt" in paths
        assert git(repo, "show", f"{tip}:product.txt").strip() == "product change"
        # worktree 保留供 verify 用（rc-702098ab 语义不变）。
        assert worktree_root.is_dir()
        assert (worktree_root / "product.txt").read_text().strip() == "product change"
        ops.remove_worktree(repo, worktree_root)

    def test_wash_is_noop_without_dd_subtrees(self, tmp_path: Path) -> None:
        """正向回归：不含这两棵子树的普通工单 commit——洗树 no-op，tip 不变。"""
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo = tmp_path / "repo"
        _init_git_repo(repo)
        git(repo, "checkout", "-q", "-b", "feature")
        (repo / "product.txt").write_text("product change\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "plain ticket")
        ticket = head(repo)
        git(repo, "checkout", "-q", "main")

        ops = DefaultHarvestOps()
        result = ops.build_harvest_tip(repo, ticket, "main", tmp_path / "harvest-wt")
        assert result["ok"] is True, result
        assert result["washed"] is False, result
        assert result["harvest_tip"] == ticket


class TestHarvestPrMergeUsesCleanTip:
    """交付 C.2：编排层 pr_merge 推干净 tip，而非裸 head_commit（fake 记录实参）。"""

    def test_pr_squash_merge_receives_clean_tip_not_raw_head(self, tmp_path: Path) -> None:
        fake = fake_ops()
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_HARVESTED
        assert fake["pr_merge_args"], "pr_squash_merge 未被编排层调用"
        (merge_args,) = fake["pr_merge_args"]
        # e5 事件 head_commit 是 "a"*40；fake 干净 tip 是 "b"*40。编排层必须
        # 把 worktree 步返回的干净 tip 传给 pr_squash_merge，而不是裸 head_commit。
        assert merge_args["head_commit"] == "b" * 40
        assert merge_args["head_commit"] != e5_event()["payload"]["head_commit"]

    def test_worktree_step_records_harvest_tip(self, tmp_path: Path) -> None:
        fake = fake_ops()
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        run_harvest(config)
        receipt = json.loads(Path(config.state_root / "reports" / "e5-dev-x.json").read_text())
        worktree_step = next(s for s in receipt["steps"] if s["step"] == "worktree_cherry_pick")
        assert worktree_step["harvest_tip"] == "b" * 40
        merge_step = next(s for s in receipt["steps"] if s["step"] == "pr_squash_merge")
        assert merge_step["commit"] == "b" * 40
        assert receipt["harvest_tip"] == "b" * 40
