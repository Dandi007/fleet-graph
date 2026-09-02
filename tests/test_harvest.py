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
import shutil
from pathlib import Path
from typing import Any

from conftest import git, head
from fleet_graph.supervise.events import approved_unharvested_event, validate_event
from fleet_graph.supervise.harvest import (
    ESCALATE_BRANCH_OCCUPIED,
    OUTCOME_ALREADY_HARVESTED,
    OUTCOME_ESCALATED,
    OUTCOME_HARVESTED,
    OUTCOME_REFUSED,
    SOP_STEPS,
    WRITE_STEPS,
    HarvestDeps,
    HarvestRunConfig,
    _resolve_repo,
    authorize_harvest_write,
    build_harvest_graph,
    run_harvest,
)
from fleet_graph.supervise.harvest_allowlist import HarvestAllowlist, parse_harvest_allowlist
from fleet_graph.supervise.harvest_ops import EXIT_HEAD_MISMATCH, DefaultHarvestOps


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
    pull_head: str = "f" * 40,
    resolve_canonical: Path | None = None,
    divergence: dict[str, Any] | None = None,
    harvest_tip: str = "b" * 40,
    inflight_binding: dict[str, Any] | None = None,
    resolve_verify_argv: tuple[list[str] | None, str] | None = None,
    pr_merge_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A recording fake ops: every write/execute is recorded, results scripted.

    `pull_head` 模拟 `ff_only_pull` 成功后返回的已合并 commit（`head` 字段）；
    `pull_ok=False` 时 head 为 None。`verify_real` 按机械契约拒绝 `expected_head
    is None`（返回 EXIT_HEAD_MISMATCH），与 DefaultHarvestOps 行为一致。
    `inflight_binding` 非 None 时 `detect_inflight_binding` 返回该绑定事实
    （脚本化，用于 H8 occupancy 用例）；None 时返回「无在飞绑定」（零回归）。
    `resolve_verify_argv` 脚本化按目标仓解析结果（交付 A）：None 时默认返回
    `(["make","verify"], "")`（模拟 Makefile 含 verify 目标）；显式传
    `(None, "no resolvable verify command")` 等可脚本化解析失败路径。
    `pr_merge_result` 非 None 时 `pr_squash_merge` 返回该完整结果（脚本化，
    用于 M3 分支占用 refuse+escalate 编排短路口用例）；None 时返回默认
    `{"merged": ..., "pr_url": ...}`。
    """
    calls: list[str] = []
    deploy_repos: list[Path] = []
    verify_real_repos: list[Path] = []
    verify_real_heads: list[str | None] = []
    binding_probes: list[dict[str, Any]] = []
    fetch_remote_urls: list[str | None] = []
    if divergence is None:
        divergence = {
            "diverged": False,
            "local_head": "a" * 40,
            "origin_head": "b" * 40,
            "detail": "local 是 origin 祖先（未分叉）",
        }
    pr_merge_args: list[dict[str, Any]] = []

    class Ops:
        def fetch_dd_ref(
            self, repo: Path, development_id: str, remote_url: str | None = None
        ) -> dict[str, Any]:
            calls.append("fetch_dd_ref")
            fetch_remote_urls.append(remote_url)
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

        def detect_inflight_binding(
            self, tree_path: Path, dd_root: Path, current_development_id: str | None = None
        ) -> dict[str, Any]:
            # 纯读口：不构成写原语，不入 calls；调用面单独记录（H8 只读判据）。
            binding_probes.append(
                {
                    "tree_path": str(tree_path),
                    "dd_root": str(dd_root),
                    "current_development_id": current_development_id,
                }
            )
            if inflight_binding is not None:
                return dict(inflight_binding)
            return {"bound_development_id": None, "in_flight": False, "detail": ""}

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

        def resolve_verify_argv(self, worktree: Path) -> tuple[list[str] | None, str]:
            # 机械读口：不入 calls（calls 只记录写/执行动作）。
            if resolve_verify_argv is not None:
                return resolve_verify_argv[0], resolve_verify_argv[1]
            return ["make", "verify"], ""

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
            if pr_merge_result is not None:
                return dict(pr_merge_result)
            return {"merged": merged, "pr_url": pr_url, "method": "gh-pr-squash-merge"}

        def ff_only_pull(self, repo: Path, default_branch: str) -> dict[str, Any]:
            calls.append("ff_only_pull")
            if not pull_ok:
                return {"ok": False, "head": None}
            return {"ok": True, "head": pull_head}

        def detect_divergence(self, repo: Path, default_branch: str) -> dict[str, Any]:
            # 纯读口：不构成写原语，不入 calls（calls 只记录写/执行动作）。
            return dict(divergence)

        def deploy(self, command: list[str], repo: Path) -> int:
            calls.append("deploy")
            deploy_repos.append(repo)
            return deploy_exit

        def verify_real(self, argv: list[str], repo: Path, expected_head: str | None) -> int:
            calls.append("verify_real")
            verify_real_repos.append(repo)
            verify_real_heads.append(expected_head)
            if expected_head is None:
                return EXIT_HEAD_MISMATCH
            return verify_real_exit

    return {
        "ops": Ops(),
        "calls": calls,
        "deploy_repos": deploy_repos,
        "verify_real_repos": verify_real_repos,
        "verify_real_heads": verify_real_heads,
        "pr_merge_args": pr_merge_args,
        "binding_probes": binding_probes,
        "fetch_remote_urls": fetch_remote_urls,
    }


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


class TestHarvestCanonicalCwdAndHead:
    """交付 C：deploy/verify_real 以 canonical 仓为 cwd + verify_real 先断言 HEAD。

    1. **cwd 断言**：fake ops 的 `deploy`/`verify_real` 收到 `repo` == 解析出的
       canonical 仓（`state["repo_path"]`）；`verify_real` 收到 `expected_head`
       == fake `ff_only_pull` 返回的 `head`。
    2. **HEAD 断言（正向）**：pull 成功 + fake `head` 非 None -> `verify_real`
       执行、exit 0。
    3. **HEAD 断言（负向）**：`pull_ok=False`（fake 返回 `head=None`）->
       `verify_real` 收到 `expected_head=None` -> 该步 exit 非 0/ok:false。
    """

    def test_deploy_and_verify_real_get_canonical_repo(self, tmp_path: Path) -> None:
        fake = fake_ops()
        config, _ = config_for(
            tmp_path,
            ops=fake,
            bus=FakeBus(),
            deploy_command=["make", "deploy"],
        )
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_HARVESTED
        canonical = str(config.dd_root.parent / "repos" / "fleet-graph")
        assert fake["deploy_repos"] and all(str(r) == canonical for r in fake["deploy_repos"])
        assert fake["verify_real_repos"] and all(
            str(r) == canonical for r in fake["verify_real_repos"]
        )

    def test_verify_real_gets_pull_head_as_expected_head(self, tmp_path: Path) -> None:
        fake = fake_ops(pull_head="a" * 40)
        config, _ = config_for(
            tmp_path,
            ops=fake,
            bus=FakeBus(),
            deploy_command=["make", "deploy"],
        )
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_HARVESTED
        assert fake["verify_real_heads"] == ["a" * 40]

    def test_verify_real_executes_when_pull_head_present(self, tmp_path: Path) -> None:
        fake = fake_ops(pull_ok=True, pull_head="a" * 40, verify_real_exit=0)
        config, _ = config_for(
            tmp_path,
            ops=fake,
            bus=FakeBus(),
            deploy_command=["make", "deploy"],
        )
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_HARVESTED
        assert fake["verify_real_heads"] == ["a" * 40]
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        verify_real = next(s for s in receipt["steps"] if s["step"] == "verify_real")
        assert verify_real["exit_code"] == 0
        assert verify_real["ok"] is True

    def test_verify_real_refuses_when_pull_failed(self, tmp_path: Path) -> None:
        """负向（不可省略）：pull_ok=False -> head=None -> verify_real 拒绝执行。

        fake 按机械契约：`expected_head=None` -> 返回 EXIT_HEAD_MISMATCH；
        该步 exit 非 0 / ok:false。
        """
        fake = fake_ops(pull_ok=False)
        config, _ = config_for(
            tmp_path,
            ops=fake,
            bus=FakeBus(),
            deploy_command=["make", "deploy"],
        )
        result = run_harvest(config)
        assert fake["verify_real_heads"] == [None]
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        verify_real = next(s for s in receipt["steps"] if s["step"] == "verify_real")
        assert verify_real["exit_code"] != 0
        assert verify_real["ok"] is False
        assert verify_real["exit_code"] == EXIT_HEAD_MISMATCH
        assert "拒绝在陈旧树上报绿" in verify_real["detail"]
        # ff_only_pull 失败也留痕（ok:false + head None）。
        pull = next(s for s in receipt["steps"] if s["step"] == "ff_only_pull")
        assert pull["ok"] is False
        assert pull["head"] is None

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


def _missing_of(receipt: dict[str, Any]) -> list[str]:
    return [item for s in receipt["steps"] for item in (s.get("missing") or [])]


class TestPostconditionsStepScan:
    """H2：任一 step ok:false -> postconditions 必红、outcome 不得为 harvested。

    交付 B.2/B.3 阴性：`ff_only_pull` / `deploy` / `verify_real` 任一机械
    失败（fake ops 注入）都必须让 outcome 离开 harvested，即使四要素（PR
    merged + verify 零退出 + evidence note）都齐。postconditions 扫描 steps
    机械事实，不采信任何子图自述。
    """

    def test_pull_failure_escalates_with_step_facts(self, tmp_path: Path) -> None:
        fake = fake_ops(pull_ok=False)
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        assert result["outcome"] != OUTCOME_HARVESTED
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        missing = _missing_of(receipt)
        assert any("ff_only_pull" in item for item in missing), missing
        post = next(s for s in receipt["steps"] if s["step"] == "postconditions")
        assert post["ok"] is False
        pull_step = next(s for s in receipt["steps"] if s["step"] == "ff_only_pull")
        assert pull_step["ok"] is False

    def test_deploy_failure_escalates(self, tmp_path: Path) -> None:
        fake = fake_ops(deploy_exit=1)
        config, _ = config_for(tmp_path, ops=fake, deploy_command=["make", "deploy"], bus=FakeBus())
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        assert result["outcome"] != OUTCOME_HARVESTED
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert any("deploy" in item for item in _missing_of(receipt))

    def test_verify_real_failure_escalates(self, tmp_path: Path) -> None:
        fake = fake_ops(verify_real_exit=1)
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        assert result["outcome"] != OUTCOME_HARVESTED
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert any("verify_real" in item for item in _missing_of(receipt))

    def test_all_ok_still_harvests(self, tmp_path: Path) -> None:
        fake = fake_ops()
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_HARVESTED
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        post = next(s for s in receipt["steps"] if s["step"] == "postconditions")
        assert post["ok"] is True
        assert _missing_of(receipt) == []


class TestCherryDedup:
    def test_already_harvested_is_a_no_op(self, tmp_path: Path) -> None:
        fake = fake_ops(cherry_equivalent=True)
        config, _ = config_for(tmp_path, ops=fake)
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ALREADY_HARVESTED
        # SOP 顺序：fetch dd ref -> cherry 判重。判重命中后没有任何写动作
        # （worktree/verify/merge/pull/deploy/verify_real/evidence 都不跑）。
        assert fake["calls"] == ["fetch_dd_ref", "cherry_equivalent"], fake["calls"]


class TestPullDivergenceEscalation:
    """H3 交付 B/C：pull 前分叉检测 -> 立即 escalate，绝不带病继续。

    fake ops 注入 `detect_divergence`：
    1. 分叉 fixture -> outcome==escalated、`ff_only_pull` step ok:false + escalate
       字段、calls 里没有任何 reset/checkout 类写动作（deploy/verify_real 不跑）。
    2. 未分叉 fixture -> 走正常链、outcome==harvested（正向回归）。
    3. 实现方不得引入任何自动 reset 路径（fake 无 reset/checkout 方法，天然约束）。
    """

    def test_diverged_local_vs_origin_escalates_without_writes(self, tmp_path: Path) -> None:
        fake = fake_ops(
            divergence={
                "diverged": True,
                "local_head": "l" * 40,
                "origin_head": "o" * 40,
                "detail": "local 与 origin 双向非祖先 → 1:1 分叉",
            },
            pull_ok=True,
        )
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        pull_step = next(s for s in result["steps"] if s["step"] == "ff_only_pull")
        assert pull_step["ok"] is False
        assert pull_step["escalate"] == "HARVEST_DIVERGED_LOCAL_VS_ORIGIN"
        assert pull_step["local_head"] == "l" * 40
        assert pull_step["origin_head"] == "o" * 40
        assert pull_step["detail"]
        # 分叉 -> 立即走 receipt：不跑 ff_only_pull（分叉检测已命中）、不跑
        # deploy/verify_real，也没有任何 reset/checkout 类写动作。
        assert "ff_only_pull" not in fake["calls"], fake["calls"]
        assert "deploy" not in fake["calls"], fake["calls"]
        assert "verify_real" not in fake["calls"], fake["calls"]
        assert not any("reset" in c or "checkout" in c for c in fake["calls"])
        # 立即走 receipt：postconditions 都不跑。
        assert "postconditions" not in [s.get("step") for s in result["steps"]]

    def test_not_diverged_pull_proceeds_to_normal_chain(self, tmp_path: Path) -> None:
        fake = fake_ops(divergence={"diverged": False, "detail": "未分叉"}, pull_ok=True)
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_HARVESTED
        # 未分叉 -> 走既有链：ff_only_pull 执行，deploy/verify_real/evidence/
        # postconditions 都跑（正向回归）。
        assert "ff_only_pull" in fake["calls"]
        assert "verify_real" in fake["calls"]
        ran_steps = [s["step"] for s in result["steps"]]
        for step in ("ff_only_pull", "deploy", "verify_real", "evidence_note", "postconditions"):
            assert step in ran_steps, f"{step} missing from {ran_steps}"
        assert not any("reset" in c or "checkout" in c for c in fake["calls"])

    def test_indeterminate_divergence_escalates_conservatively(self, tmp_path: Path) -> None:
        """无法判定分叉（如无 origin）-> 保守 escalate，不留分叉漏检。"""
        fake = fake_ops(
            divergence={
                "diverged": True,
                "local_head": "l" * 40,
                "origin_head": None,
                "detail": "读取 origin/main 失败（可能无 origin）",
            }
        )
        config, _ = config_for(tmp_path, ops=fake)
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        assert "deploy" not in fake["calls"]
        assert "verify_real" not in fake["calls"]


class TestDefaultHarvestOpsDetectDivergence:
    """H3 交付 A：真实 git 上 `DefaultHarvestOps.detect_divergence` 的机械读判。

    单仓本地合成（HEAD + `git update-ref refs/remotes/origin/main` 构造远端 tip），
    禁触真网/生产 checkout。断言的判据：
    - 未分叉（local 是 origin 祖先 / origin 是 local 祖先）-> diverged False；
    - 1:1 分叉（双向非祖先）-> diverged True；
    - 无 origin ref / 无本地 HEAD -> 保守 diverged True（不留分叉漏检）。
    """

    def _repo(self, tmp_path: Path, name: str = "repo") -> Path:
        repo = tmp_path / name
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "test@example.invalid")
        git(repo, "config", "user.name", "test")
        (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "seed")
        return repo

    def _set_origin(self, repo: Path, ref_target: str) -> None:
        git(repo, "update-ref", "refs/remotes/origin/main", ref_target)

    def _commit(self, repo: Path, filename: str, content: str) -> str:
        (repo / filename).write_text(content, encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"{filename}: {content}")
        return head(repo)

    def test_local_behind_origin_is_not_diverged(self, tmp_path: Path) -> None:
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo = self._repo(tmp_path)
        origin_tip = self._commit(repo, "advance.txt", "advance")
        self._set_origin(repo, origin_tip)
        result = DefaultHarvestOps().detect_divergence(repo, "main")
        assert result["diverged"] is False, result
        assert result["local_head"] and result["origin_head"] == origin_tip

    def test_identical_heads_are_not_diverged(self, tmp_path: Path) -> None:
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo = self._repo(tmp_path)
        tip = head(repo)
        self._set_origin(repo, tip)
        result = DefaultHarvestOps().detect_divergence(repo, "main")
        assert result["diverged"] is False, result
        assert result["local_head"] == result["origin_head"] == tip

    def test_diverged_local_vs_origin_detected(self, tmp_path: Path) -> None:
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo = self._repo(tmp_path)
        base = head(repo)
        # 本地侧从 base 长出一个 commit（模拟 bb026e3「本地假合并」残骸）。
        local_tip = self._commit(repo, "local-only.txt", "local")
        assert head(repo) == local_tip
        # 回到 base，origin 侧再从 base 长出一个不同 commit -> 与 local 1:1 分叉。
        git(repo, "reset", "-q", "--hard", base)
        origin_tip = self._commit(repo, "origin-advance.txt", "origin")
        git(repo, "reset", "-q", "--hard", local_tip)
        self._set_origin(repo, origin_tip)
        result = DefaultHarvestOps().detect_divergence(repo, "main")
        assert result["diverged"] is True, result
        assert result["local_head"] == local_tip
        assert result["origin_head"] == origin_tip

    def test_missing_origin_ref_is_conservative(self, tmp_path: Path) -> None:
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo = self._repo(tmp_path)
        result = DefaultHarvestOps().detect_divergence(repo, "main")
        assert result["diverged"] is True, result
        assert result["origin_head"] is None

    def test_missing_local_head_is_conservative(self, tmp_path: Path) -> None:
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo = tmp_path / "empty"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        result = DefaultHarvestOps().detect_divergence(repo, "main")
        assert result["diverged"] is True, result
        assert result["local_head"] is None


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
        """冲突路径行为不变：worktree add 失败/冲突都如实报告，不强行覆盖。

        H6 后 -X theirs 能解开纯内容冲突（重试收口），这里改用 **modify/delete
        冲突**（默认分支删文件、工单分支改同文件）——git 连 -X theirs 都无法自动
        收口，必须诚实 escalate（ok:false / conflicts:true）并就地清理 worktree，
        绝不把冲突吞成 ok:true。
        """
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
        git(repo, "rm", "-q", "shared.txt")
        git(repo, "commit", "-q", "-m", "main deletes")

        ops = DefaultHarvestOps()
        worktree_root = tmp_path / "harvest-worktree"
        result = ops.worktree_cherry_pick(repo, product_commit, "main", worktree_root)
        assert result.get("conflicts") is True or result["ok"] is False
        assert not worktree_root.exists(), "failed worktree left behind"

    def test_conflict_retry_closes_via_x_theirs(self, tmp_path: Path) -> None:
        """H6：纯内容冲突清场后 -X theirs 重试必须收口（ok:true + 真实 harvest_tip）。

        双分支同文件改法（沿用 test_conflict_path_still_reports_conflicts 旧 fixture）：
        首败冲突后 worktree 索引残留 unmerged files；本实现先 cherry-pick --abort
        清场再 -X theirs 重试，`-X theirs` 以默认分支为主解得开 -> ok:true 且
        harvest_tip 非空、成功路径 worktree 保留供 run_verify（rc-702098ab 语义）。
        """
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
        assert result["ok"] is True, result
        assert result.get("harvest_tip"), result
        # harvest_tip 必须是真实可解析 commit（不凭空造）。
        git(repo, "cat-file", "-e", f"{result['harvest_tip']}^{{commit}}")
        # 成功路径 worktree 保留供 run_verify（rc-702098ab）。
        assert worktree_root.is_dir(), "closed conflict worktree must survive for run_verify"
        verify_exit = ops.run_verify(worktree_root, ["/bin/true"])
        assert verify_exit == 0
        ops.remove_worktree(repo, worktree_root)
        assert not worktree_root.exists()

    def test_conflict_retry_never_fabricates_harvest_tip(self, tmp_path: Path) -> None:
        """H6 关键负例：不得自报 harvested——失败绝不报 ok:true，也绝不凭空 harvest_tip。

        modify/delete 冲突连 -X theirs 都收不了口 -> 必须诚实 ok:false +
        conflicts:true（escalate 语义），绝不「失败却报 ok:true / 凭空 harvest_tip」。
        """
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
        git(repo, "rm", "-q", "shared.txt")
        git(repo, "commit", "-q", "-m", "main deletes")

        ops = DefaultHarvestOps()
        worktree_root = tmp_path / "harvest-worktree"
        result = ops.worktree_cherry_pick(repo, product_commit, "main", worktree_root)
        assert result["ok"] is False, result
        assert result.get("conflicts") is True, result
        assert not worktree_root.exists(), "escalated conflict worktree left behind"


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
        resolved, gaps, _remote_url = _resolve_repo(
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
        resolved, gaps, _remote_url = _resolve_repo(
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
        resolved, gaps, _remote_url = _resolve_repo(
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
        resolved, gaps, _remote_url = _resolve_repo(
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
        resolved, gaps, _remote_url = _resolve_repo(
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


class TestDefaultHarvestOpsVerifyRealHead:
    """交付 A：DefaultHarvestOps.verify_real 以 canonical 仓为 cwd + 先断言 HEAD。

    真 git 合成仓（禁触真网/生产 checkout）：expected_head 缺失或与当前 HEAD
    不一致时**不执行** verify 命令并返回 EXIT_HEAD_MISMATCH；相等时才在 repo
    cwd 下执行。
    """

    def test_refuses_when_expected_head_none(self, tmp_path: Path) -> None:
        from fleet_graph.supervise.harvest_ops import EXIT_HEAD_MISMATCH, DefaultHarvestOps

        repo = tmp_path / "canon"
        _init_git_repo(repo)
        marker = repo / "ran.txt"
        ops = DefaultHarvestOps()
        exit_code = ops.verify_real(["touch", "ran.txt"], repo, None)
        assert exit_code == EXIT_HEAD_MISMATCH
        assert not marker.exists(), "expected_head=None 时仍执行了 verify 命令"

    def test_refuses_when_head_mismatches(self, tmp_path: Path) -> None:
        from fleet_graph.supervise.harvest_ops import EXIT_HEAD_MISMATCH, DefaultHarvestOps

        repo = tmp_path / "canon"
        _init_git_repo(repo)
        marker = repo / "ran.txt"
        ops = DefaultHarvestOps()
        exit_code = ops.verify_real(["touch", "ran.txt"], repo, "0" * 40)
        assert exit_code == EXIT_HEAD_MISMATCH
        assert not marker.exists(), "HEAD 与已合并 commit 不一致时仍执行了 verify"

    def test_executes_in_repo_cwd_when_head_matches(self, tmp_path: Path) -> None:
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo = tmp_path / "canon"
        _init_git_repo(repo)
        current_head = head(repo)
        marker = repo / "ran.txt"
        ops = DefaultHarvestOps()
        exit_code = ops.verify_real(["touch", "ran.txt"], repo, current_head)
        assert exit_code == 0
        assert marker.exists(), "HEAD 相等时应以 repo 为 cwd 执行 verify"


class TestDefaultHarvestOpsFfOnlyPullHead:
    """交付 A：ff_only_pull 成功时返回 pull 后的 HEAD（字段名 `head`），失败为 None。"""

    def test_returns_head_on_success(self, tmp_path: Path) -> None:
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        canon = tmp_path / "canon"
        _init_git_repo(canon)
        origin = tmp_path / "origin.git"
        git(canon, "clone", "--bare", "-q", ".", str(origin))
        git(canon, "remote", "add", "origin", str(origin))
        ops = DefaultHarvestOps()
        result = ops.ff_only_pull(canon, "main")
        assert result["ok"] is True, result
        assert result["head"] == head(canon), "成功时 head 应为 pull 后的 HEAD"

    def test_head_none_on_failure(self, tmp_path: Path) -> None:
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        canon = tmp_path / "canon"
        _init_git_repo(canon)
        git(canon, "remote", "add", "origin", str(tmp_path / "missing-origin.git"))
        ops = DefaultHarvestOps()
        result = ops.ff_only_pull(canon, "main")
        assert result["ok"] is False, result
        assert result["head"] is None, "失败时 head 应为 None"


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
        """正向回归：不含这两棵子树的普通工单 commit——洗树 no-op（washed=False）。

        cherry-pick 落地的是**新 commit**（固定收割身份 `_commit_env()`，身份
        不再是 repo 本地 user config 的巧合），但洗树步骤没有 dd 子树可剔——
        `washed` 必须为 False，harvest_tip 是真实可解析 commit（不凭空造）。
        """
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
        assert result["harvest_tip"], result
        git(repo, "cat-file", "-e", f"{result['harvest_tip']}^{{commit}}")


class TestHarvestGitPlumbingNegatives:
    """交付 C：M3 收割反应器两处原生 git 缺陷的阴性测试（合成本地仓，禁真网）。

    1. 阴性 A（identity）：合成**无全局 identity 环境**（repo 无 user.name/
       user.email config、`safe_git_environment` 会清空 GIT_AUTHOR/COMMITTER 并
       禁用 global config）→ `worktree_cherry_pick` 返回 ok:true、`harvest_tip`
       非空；对照未修复时必然 `Committer identity unknown`（cherry-pick 落地
       commit 无 committer identity）。
    2. 阴性 B（remote_url）：合成 record 其 `remote_url` 为**本地路径**仓、dd
       ref 只推在该本地仓、`origin` 故意指向不含该 dd ref 的远端 → `fetch_dd_ref`
       ok:true；对照未修复时 `couldn't find remote ref`（origin 与 remote_url
       不同源）。
    3. 反向不抖动：URL remote_url 且 `origin` 同源 → 行为不变（既有路径零回归）；
       有 identity 环境 → cherry-pick/洗树/冲突重试路径不变。
    """

    def test_cherry_pick_lands_commit_without_global_identity(self, tmp_path: Path) -> None:
        """阴性 A（identity）：无全局 identity 下 cherry-pick 仍能落地 commit。

        repo 创建时**不写任何 user.name/user.email config**（conftest `git()`
        每命令 `-c user.email/name` 仅当次生效、不落库）；`run_git` 走
        `safe_git_environment()`（清空 GIT_*、禁用 global/system config）——
        未修复时 `git cherry-pick` 必然 `Committer identity unknown`，修复后
        `_commit_env()` 提供固定收割身份 -> ok:true。
        """
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "seed")
        git(repo, "checkout", "-q", "-b", "feature")
        (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "product change")
        product_commit = head(repo)
        git(repo, "checkout", "-q", "main")

        # 无全局 identity：repo 本地 config 无 user.name/user.email。
        local_config = git(repo, "config", "--local", "--list")
        assert "user.email" not in local_config
        assert "user.name" not in local_config

        ops = DefaultHarvestOps()
        worktree_root = tmp_path / "harvest-wt"
        picked = ops.worktree_cherry_pick(repo, product_commit, "main", worktree_root)
        assert picked["ok"] is True, picked
        assert picked["harvest_tip"], picked
        # harvest_tip 必须是真实可解析 commit（不凭空造）。
        git(repo, "cat-file", "-e", f"{picked['harvest_tip']}^{{commit}}")
        # 成功路径 worktree 保留供 run_verify（rc-702098ab）。
        assert worktree_root.is_dir()
        ops.remove_worktree(repo, worktree_root)
        assert not worktree_root.exists()

    def test_cherry_pick_theirs_retry_lands_commit_without_global_identity(
        self, tmp_path: Path
    ) -> None:
        """阴性 A（identity）+ H6 冲突重试：无全局 identity 下 -X theirs 重试也落地 commit。

        纯内容冲突清场后 `-X theirs` 重试同样要新建 commit；未修复时重试的
        cherry-pick 同样 `Committer identity unknown`。修复后 ok:true。
        """
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
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

        local_config = git(repo, "config", "--local", "--list")
        assert "user.email" not in local_config
        assert "user.name" not in local_config

        ops = DefaultHarvestOps()
        worktree_root = tmp_path / "harvest-wt"
        result = ops.worktree_cherry_pick(repo, product_commit, "main", worktree_root)
        assert result["ok"] is True, result
        assert result.get("harvest_tip"), result
        git(repo, "cat-file", "-e", f"{result['harvest_tip']}^{{commit}}")
        ops.remove_worktree(repo, worktree_root)

    def test_fetch_dd_ref_uses_local_path_remote_url(self, tmp_path: Path) -> None:
        """阴性 B（remote_url）：dd ref 只推在本地路径 remote_url 仓、origin 无它。

        `origin` 指向另一个**不含**该 dd ref 的 bare 远端（对照未修复时
        `git fetch origin <dd_ref>` 报 `couldn't find remote ref`）；`remote_url`
        是本地路径仓且持有 `refs/heads/dd/<dev>`。修复后 fetch 走 remote_url ->
        ok:true。
        """
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        canon = tmp_path / "canon"
        _init_git_repo(canon)
        (canon / "product.txt").write_text("product change\n", encoding="utf-8")
        git(canon, "add", "-A")
        git(canon, "commit", "-q", "-m", "product change")
        product = head(canon)

        # remote_url：本地路径 bare 仓，持有 dd ref。
        local_remote = tmp_path / "local-remote.git"
        git(canon, "clone", "--bare", "-q", ".", str(local_remote))
        git(local_remote, "update-ref", "refs/heads/dd/dev-x", product)

        # origin：另一个 bare 仓，不含该 dd ref（未修复时 fetch 会找不到）。
        origin = tmp_path / "origin.git"
        git(canon, "clone", "--bare", "-q", ".", str(origin))
        git(canon, "remote", "add", "origin", str(origin))

        ops = DefaultHarvestOps()
        result = ops.fetch_dd_ref(canon, "dev-x", str(local_remote))
        assert result["ok"] is True, result
        assert result["ref"] == "refs/heads/dd/dev-x"

    def test_fetch_dd_ref_url_remote_url_matches_origin(self, tmp_path: Path) -> None:
        """反向不抖动：URL remote_url 且 origin 同源 -> 行为不变（既有路径零回归）。

        `remote_url` 与 `origin` 指向同一本地 bare 仓（URL remote 时
        remote_url==origin 等价），dd ref 就在该仓 -> fetch ok:true，不因本次
        修复而回归。
        """
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        canon = tmp_path / "canon"
        _init_git_repo(canon)
        (canon / "product.txt").write_text("product change\n", encoding="utf-8")
        git(canon, "add", "-A")
        git(canon, "commit", "-q", "-m", "product change")
        product = head(canon)

        origin = tmp_path / "origin.git"
        git(canon, "clone", "--bare", "-q", ".", str(origin))
        git(canon, "remote", "add", "origin", str(origin))
        git(origin, "update-ref", "refs/heads/dd/dev-x", product)

        ops = DefaultHarvestOps()
        result = ops.fetch_dd_ref(canon, "dev-x", str(origin))
        assert result["ok"] is True, result
        assert result["ref"] == "refs/heads/dd/dev-x"

    def test_fetch_dd_ref_local_path_remote_url_works(self, tmp_path: Path) -> None:
        """阴性 B（本地路径 remote_url，非 bare 仓）：从本地路径取 dd ref -> ok:true。

        本地路径 remote_url 仓（普通仓，非 bare）持有 `refs/heads/dd/<dev>`；
        `git fetch <本地路径> <dd_ref>`（或直接解析本地 ref）都能取到 -> ok:true，
        不依赖 origin、不触真网。
        """
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        canon = tmp_path / "canon"
        _init_git_repo(canon)
        local_remote = tmp_path / "local-remote"
        _init_git_repo(local_remote)
        (local_remote / "product.txt").write_text("product change\n", encoding="utf-8")
        git(local_remote, "add", "-A")
        git(local_remote, "commit", "-q", "-m", "product change")
        product = head(local_remote)
        git(local_remote, "update-ref", "refs/heads/dd/dev-x", product)

        ops = DefaultHarvestOps()
        result = ops.fetch_dd_ref(canon, "dev-x", str(local_remote))
        assert result["ok"] is True, result
        assert result["ref"] == "refs/heads/dd/dev-x"

    def test_fetch_dd_ref_without_remote_url_is_fail_closed(self, tmp_path: Path) -> None:
        """反向不抖动：缺 remote_url 时绝不 fallback origin 猜源 -> ok:false + detail。"""
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        canon = tmp_path / "canon"
        _init_git_repo(canon)
        ops = DefaultHarvestOps()
        result = ops.fetch_dd_ref(canon, "dev-x", None)
        assert result["ok"] is False, result
        assert "remote_url" in result["detail"]

    def test_orchestration_passes_record_remote_url_to_fetch(self, tmp_path: Path) -> None:
        """编排层把 record 的 remote_url 透传给 ops.fetch_dd_ref（交付 B.1）。"""
        fake = fake_ops()
        repo = repo_path_for(tmp_path)
        remote_url = str(tmp_path / "remote.git")
        config = HarvestRunConfig(
            event=e5_event(),
            state_root=tmp_path / "supervisor",
            run_root=tmp_path / "runs",
            dd_root=dd_record_root_with_remote_url(tmp_path, repo, remote_url),
            deploy_command=[],
            allowlist=full_allowlist(repo),
            ops=fake["ops"],
            publish_notes=False,
        )
        run_harvest(config)
        assert fake["fetch_remote_urls"] == [remote_url]


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


class TestHarvestWriteGate:
    """H7 写前闸：worktree_cherry_pick / run_verify 任一判红 -> 立即停链，无任何写动作。

    spec 阴性测试 1/2：run_verify 非零 或 worktree_cherry_pick ok:false 的 fixture ->
    没有任何 PR 被 merge、默认分支 HEAD 与运行前逐字节相同、回执 outcome=escalated
    且写步骤被显式记为跳过（writes_skipped）。fake ops 记录所有写原语调用，零调用
    即机械证明「未执行任何写动作」；真实 git fixture 逐字节断言默认分支 HEAD 不变。
    """

    def test_verify_failure_blocks_all_writes(self, tmp_path: Path) -> None:
        fake = fake_ops(verify_exit=1)
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        # 没有任何 PR 被 merge：fake 记录的写原语零调用。
        assert "pr_squash_merge" not in fake["calls"], fake["calls"]
        assert "ff_only_pull" not in fake["calls"], fake["calls"]
        assert "deploy" not in fake["calls"], fake["calls"]
        assert "verify_real" not in fake["calls"], fake["calls"]
        # housekeeping cleanup 仍会跑（收掉一次性 worktree），不算写默认分支。
        assert "remove_worktree" in fake["calls"], fake["calls"]
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["outcome"] == OUTCOME_ESCALATED
        assert receipt["writes_skipped"] == list(WRITE_STEPS)
        # 写步骤本身不进回执（没执行）。
        assert "pr_squash_merge" not in [s.get("step") for s in receipt["steps"]]

    def test_cherry_pick_failure_blocks_all_writes(self, tmp_path: Path) -> None:
        fake = fake_ops(worktree_ok=False)
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        # cherry-pick 失败 -> 无任何 PR 被 merge（D2：fallback 不存在，钉死无 merge）。
        assert "pr_squash_merge" not in fake["calls"], fake["calls"]
        assert "ff_only_pull" not in fake["calls"], fake["calls"]
        assert "deploy" not in fake["calls"], fake["calls"]
        assert "verify_real" not in fake["calls"], fake["calls"]
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["outcome"] == OUTCOME_ESCALATED
        assert receipt["writes_skipped"] == list(WRITE_STEPS)
        assert "pr_squash_merge" not in [s.get("step") for s in receipt["steps"]]

    def test_verify_failure_leaves_default_branch_head_untouched(self, tmp_path: Path) -> None:
        """真实 git：run_verify 判红 -> 默认分支 HEAD 与运行前逐字节相同。"""
        repo, product = _real_repo_with_origin(tmp_path)
        before = head(repo)
        config = HarvestRunConfig(
            event=approved_unharvested_event(
                development_id="dev-x", head_commit=product, stage="implement"
            ).as_dict(),
            state_root=tmp_path / "supervisor",
            run_root=tmp_path / "runs",
            dd_root=dd_record_root_with_remote_url(
                tmp_path, str(repo), str(tmp_path / "origin.git")
            ),
            deploy_command=[],
            allowlist=full_allowlist(str(repo)),
            ops=DefaultHarvestOps(),
            verify_argv=["false"],
            verify_real_argv=["false"],
            publish_notes=False,
        )
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        assert head(repo) == before, "verify 判红后默认分支 HEAD 必须逐字节不变"
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["writes_skipped"] == list(WRITE_STEPS)

    def test_cherry_pick_failure_leaves_default_branch_head_untouched(self, tmp_path: Path) -> None:
        """真实 git：worktree_cherry_pick 判红（modify/delete 冲突）-> HEAD 不变。"""
        repo = tmp_path / "canon"
        _init_git_repo(repo)
        (repo / "shared.txt").write_text("base\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "add shared")
        git(repo, "checkout", "-q", "-b", "feature")
        (repo / "shared.txt").write_text("feature\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "product change")
        product = head(repo)
        git(repo, "checkout", "-q", "main")
        git(repo, "rm", "-q", "shared.txt")
        git(repo, "commit", "-q", "-m", "main deletes")
        before = head(repo)
        _add_origin_with_dd_ref(repo, tmp_path / "origin.git", product)
        config = HarvestRunConfig(
            event=approved_unharvested_event(
                development_id="dev-x", head_commit=product, stage="implement"
            ).as_dict(),
            state_root=tmp_path / "supervisor",
            run_root=tmp_path / "runs",
            dd_root=dd_record_root_with_remote_url(
                tmp_path, str(repo), str(tmp_path / "origin.git")
            ),
            deploy_command=[],
            allowlist=full_allowlist(str(repo)),
            ops=DefaultHarvestOps(),
            verify_argv=["true"],
            verify_real_argv=["true"],
            publish_notes=False,
        )
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        assert head(repo) == before, "cherry-pick 判红后默认分支 HEAD 必须逐字节不变"
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["writes_skipped"] == list(WRITE_STEPS)

    def test_all_ok_still_harvests_and_merges(self, tmp_path: Path) -> None:
        """正向回归：全链每步 ok:true 仍正常 harvested，PR 正常 merge（不是永不写）。"""
        fake = fake_ops()
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_HARVESTED
        assert "pr_squash_merge" in fake["calls"], fake["calls"]
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["pr_merged"] is True
        assert receipt["writes_skipped"] == []


class TestHarvestBranchOccupiedEscalateShortCircuit:
    """交付 A.2（rc-6c2e9473 复审修复）：pr_squash_merge 返回 refused+escalate
    HARVEST_BRANCH_OCCUPIED -> 编排层立即 outcome=escalated + writes_skipped，
    绝不进入 pull/deploy/verify_real 任何写步（不再在 postconditions 前对 canonical
    checkout 跑 ff_only_pull / deploy / verify_real），也不落「远端已合并却报未合并」
    的半态。
    """

    def test_occupied_refusal_escalates_without_running_pull_deploy_verify_real(
        self, tmp_path: Path
    ) -> None:
        fake = fake_ops(
            pr_merge_result={
                "merged": False,
                "refused": True,
                "escalate": ESCALATE_BRANCH_OCCUPIED,
                "worktree_paths": ["/data/worktrees/residual-wt"],
                "detail": "harvest 分支 harvest/dev-x 被残留 worktree 检出占用",
            }
        )
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        # 占用判红后，后续写步零调用（ff_only_pull / deploy / verify_real 都不执行）。
        assert "pr_squash_merge" in fake["calls"], fake["calls"]
        assert "ff_only_pull" not in fake["calls"], fake["calls"]
        assert "deploy" not in fake["calls"], fake["calls"]
        assert "verify_real" not in fake["calls"], fake["calls"]
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["outcome"] == OUTCOME_ESCALATED
        assert receipt["pr_merged"] is False
        assert receipt["writes_skipped"] == list(WRITE_STEPS)
        # 机器可读占用诊断进步骤留痕。
        merge_step = next(s for s in receipt["steps"] if s["step"] == "pr_squash_merge")
        assert merge_step["refused"] is True
        assert merge_step["escalate"] == ESCALATE_BRANCH_OCCUPIED

    def test_occupied_refusal_does_not_produce_half_merged_state(self, tmp_path: Path) -> None:
        """绝不落半态：merged=false 且无 pr_url（远端未合并、也无 forge 链接）。"""
        fake = fake_ops(
            pr_merge_result={
                "merged": False,
                "refused": True,
                "escalate": ESCALATE_BRANCH_OCCUPIED,
                "worktree_paths": ["/data/worktrees/residual-wt"],
                "detail": "harvest 分支 harvest/dev-x 被残留 worktree 检出占用",
            }
        )
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["pr_merged"] is False
        assert receipt["pr_url"] == ""
        # 占用分支明确拒绝，绝不替人删残留 worktree（无 remove_worktree 之外的任何动作）。
        assert "pr_squash_merge" in [s.get("step") for s in receipt["steps"]]


def _real_repo_with_origin(tmp_path: Path) -> tuple[Path, str]:
    """真实可收割仓：main + feature 产品 commit + origin + dd ref（本地合成，禁真网）。"""
    repo = tmp_path / "canon"
    _init_git_repo(repo)
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "product.txt").write_text("product change\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "product change")
    product = head(repo)
    git(repo, "checkout", "-q", "main")
    _add_origin_with_dd_ref(repo, tmp_path / "origin.git", product)
    return repo, product


def _add_origin_with_dd_ref(repo: Path, origin: Path, dd_target: str) -> None:
    """把 repo 做成 bare origin，并建立 `refs/heads/dd/<dev>` 指向产品 commit。"""
    git(repo, "clone", "--bare", "-q", ".", str(origin))
    git(repo, "remote", "add", "origin", str(origin))
    git(origin, "update-ref", "refs/heads/dd/dev-x", dd_target)


#: H8 阴性 fixture 的哨兵文件字节串（内容已知，逐字节比对动树前后）。
H8_SENTINEL_BYTES = b"H8-OCCUPANCY-SENTINEL\x00\x01\x02\n"


def _h8_target_fixture(
    tmp_path: Path,
    *,
    other_dev: str = "dev-fg-OTHER",
    subject_dev: str = "dev-fg-SUBJECT",
    other_terminal: str = "",
    subject_terminal: str = "complete",
) -> tuple[Path, Path, bytes, Path]:
    """真实 git 合成仓：canonical + 目标工作树 `<target>`（linked worktree）。

    在 `<target>` 里放一个未提交的哨兵文件（已知字节串），构造 `dd_root`：
    `other_dev/record.json`（repo_path=<target>）+ status.json（terminal 可配，
    默认在飞）+ harness 自己的 `subject_dev/record.json`（repo_path=<target>，
    本次 E5 要收割的单）。全部本地合成，禁触真网/生产 checkout。

    dev id 可配：rc-3d12fbbe 回归需要构造「本单 id 在 dd_root 枚举中排序靠前」
    的形态（真实 incident 里 dev-fg-644942a367ae 先于 dev-fg-cfe509fa9c23）。
    """
    canonical = tmp_path / "canon"
    _init_git_repo(canonical)
    target = tmp_path / "target"
    git(canonical, "worktree", "add", "--detach", str(target), "main")
    (target / "sentinel.bin").write_bytes(H8_SENTINEL_BYTES)

    dd_root = tmp_path / "dd"
    for dev, terminal in ((other_dev, other_terminal), (subject_dev, subject_terminal)):
        dev_dir = dd_root / dev
        dev_dir.mkdir(parents=True)
        (dev_dir / "record.json").write_text(
            json.dumps(
                {
                    "development_id": dev,
                    "repo_path": str(target),
                    "remote_url": str(tmp_path / "local-remote.git"),
                }
            ),
            encoding="utf-8",
        )
        (dev_dir / "status.json").write_text(
            json.dumps(
                {"development_id": dev, "state": terminal or "running", "terminal": terminal}
            ),
            encoding="utf-8",
        )
    return canonical, target, H8_SENTINEL_BYTES, dd_root


def _h8_config(
    tmp_path: Path,
    *,
    canonical: Path,
    dd_root: Path,
    ops: Any,
    development_id: str = "dev-fg-SUBJECT",
) -> HarvestRunConfig:
    return HarvestRunConfig(
        event=approved_unharvested_event(
            development_id=development_id, head_commit="a" * 40, stage="implement"
        ).as_dict(),
        state_root=tmp_path / "supervisor",
        run_root=tmp_path / "runs",
        dd_root=dd_root,
        deploy_command=[],
        allowlist=full_allowlist(str(canonical)),
        ops=ops,
        publish_notes=False,
    )


class TestHarvestTreeOccupancy:
    """H8 交付 C：动树前 occupancy 门——目标树被在飞单绑定 -> 拒绝+escalate 且一字不动。

    阴性 fixture（关键，goal.md 判据）用真实 git 合成仓：目标工作树 `<target>`
    里的未提交哨兵文件逐字节、`git rev-parse HEAD`、`git status --porcelain` 都
    必须与运行前完全一致，目录与文件都仍在（未被 rmtree / worktree remove）。
    正例：终态绑定 / 无绑定 -> 放行走既有链（零回归）。只读判据：occupancy 探测
    只发生 open/read 类读操作与 `git rev-parse`，没有任何写文件/建目录/登记动作。
    """

    def test_inflight_foreign_binding_escalates_and_target_untouched(self, tmp_path: Path) -> None:
        """阴性 fixture（不可省略）：dev-fg-OTHER 在飞绑定 <target> -> intake 立即
        escalate，写步骤一个没跑，<target> 一字未动（HEAD/status/哨兵字节/目录）。"""
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        canonical, target, sentinel, dd_root = _h8_target_fixture(
            tmp_path, other_terminal="", subject_terminal="complete"
        )
        head_before = head(target)
        status_before = git(target, "status", "--porcelain")

        config = _h8_config(tmp_path, canonical=canonical, dd_root=dd_root, ops=DefaultHarvestOps())
        result = run_harvest(config)
        # a. 立即 escalate。
        assert result["outcome"] == OUTCOME_ESCALATED
        # b. intake step ok:false + escalate 码 + bound_development_id 机器可读。
        intake_step = next(s for s in result["steps"] if s["step"] == "intake")
        assert intake_step["ok"] is False
        assert intake_step["escalate"] == "HARVEST_TREE_OCCUPIED_BY_INFLIGHT"
        assert intake_step["bound_development_id"] == "dev-fg-OTHER"
        assert intake_step["detail"]
        # c. 写步骤一个没跑：writes_skipped 含三个写步骤，steps 里没有任何
        #    worktree_cherry_pick / pr_squash_merge / ff_only_pull ok:true。
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert set(receipt["writes_skipped"]) >= set(WRITE_STEPS)
        assert not any(
            s.get("step") in ("worktree_cherry_pick", "pr_squash_merge", "ff_only_pull")
            and s.get("ok") is True
            for s in receipt["steps"]
        )
        # d. `<target>` 一字未动：HEAD / porcelain / 哨兵字节 / 目录都仍在。
        assert head(target) == head_before
        assert git(target, "status", "--porcelain") == status_before
        assert (target / "sentinel.bin").read_bytes() == sentinel
        assert target.is_dir() and (target / "sentinel.bin").is_file()

    def test_fake_ops_escalate_facts_are_machine_readable(self, tmp_path: Path) -> None:
        """fake ops 脚本化在飞绑定 -> intake step 字段精确（escalate/bound/detail）。"""
        fake = fake_ops(
            inflight_binding={
                "bound_development_id": "dev-fg-OTHER",
                "in_flight": True,
                "detail": "<target> 被在飞 development dev-fg-OTHER 绑定",
            }
        )
        config, _ = config_for(tmp_path, ops=fake)
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        intake_step = next(s for s in result["steps"] if s["step"] == "intake")
        assert intake_step["ok"] is False
        assert intake_step["escalate"] == "HARVEST_TREE_OCCUPIED_BY_INFLIGHT"
        assert intake_step["bound_development_id"] == "dev-fg-OTHER"
        assert intake_step["detail"]
        # 零写动作：写原语零调用，直接走 receipt（postconditions 都不跑）。
        assert "worktree_cherry_pick" not in fake["calls"], fake["calls"]
        assert "pr_squash_merge" not in fake["calls"], fake["calls"]
        assert "ff_only_pull" not in fake["calls"], fake["calls"]
        assert "deploy" not in fake["calls"], fake["calls"]
        assert "postconditions" not in [s.get("step") for s in result["steps"]]
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert set(receipt["writes_skipped"]) >= set(WRITE_STEPS)

    def test_terminal_binding_is_released_not_inflight(self, tmp_path: Path) -> None:
        """终态绑定（区分在飞）：OTHER terminal="complete" -> in_flight=False。"""
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        _canonical, target, _sentinel, dd_root = _h8_target_fixture(
            tmp_path, other_terminal="complete", subject_terminal="complete"
        )
        binding = DefaultHarvestOps().detect_inflight_binding(target, dd_root)
        assert binding["in_flight"] is False
        assert binding["bound_development_id"] is None
        # 编排层放行：脚本化 in_flight=False -> 走既有链正常收割。
        fake = fake_ops(
            inflight_binding={
                "bound_development_id": "dev-fg-OTHER",
                "in_flight": False,
                "detail": "complete",
            }
        )
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_HARVESTED
        assert "pr_squash_merge" in fake["calls"], fake["calls"]

    def test_no_binding_is_released(self, tmp_path: Path) -> None:
        """无绑定：dd_root 无任何外部 record 指向 <target> -> in_flight=False，正常收割。"""
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        _canonical, target, _sentinel, _dd_root = _h8_target_fixture(
            tmp_path, other_terminal="", subject_terminal="complete"
        )
        dd_root = tmp_path / "dd-empty"
        dd_root.mkdir(parents=True)
        binding = DefaultHarvestOps().detect_inflight_binding(target, dd_root)
        assert binding["in_flight"] is False
        assert binding["bound_development_id"] is None
        # 编排层放行：无绑定脚本 -> 走既有链正常收割。
        fake = fake_ops()
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_HARVESTED
        assert "pr_squash_merge" in fake["calls"], fake["calls"]

    def test_detect_inflight_binding_is_read_only(self, tmp_path: Path, monkeypatch: Any) -> None:
        """只读判据（不另造账本）：occupancy 探测只 open/read JSON（纯路径比较，
        H8 case 2 收敛后连 `git rev-parse` 读口都不再需要），不写文件/不建目录/
        不登记。dd_root 目录树逐字节相同、`<target>` 一字未动。"""
        from fleet_graph.supervise import harvest_ops
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        _canonical, target, sentinel, dd_root = _h8_target_fixture(
            tmp_path, other_terminal="", subject_terminal="complete"
        )
        dd_snapshot_before = sorted(
            (str(p.relative_to(dd_root)), p.read_bytes())
            for p in sorted(dd_root.rglob("*"))
            if p.is_file()
        )
        head_before = head(target)
        status_before = git(target, "status", "--porcelain")

        git_calls: list[tuple[str, list[str]]] = []
        real_run_git = harvest_ops.run_git

        def recording_run_git(repo, *args, **kwargs):
            git_calls.append((str(repo), list(args)))
            return real_run_git(repo, *args, **kwargs)

        monkeypatch.setattr(harvest_ops, "run_git", recording_run_git)
        binding = DefaultHarvestOps().detect_inflight_binding(target, dd_root)
        assert binding["in_flight"] is True
        assert binding["bound_development_id"] == "dev-fg-OTHER"

        # 没有任何 git 调用，更没有任何写类 git 命令——纯 JSON 读 + 路径比较。
        assert git_calls == [], f"detect_inflight_binding 不应触发任何 git 命令: {git_calls}"
        # dd_root 目录树逐字节相同（没有新建/改动任何账本文件）。
        dd_snapshot_after = sorted(
            (str(p.relative_to(dd_root)), p.read_bytes())
            for p in sorted(dd_root.rglob("*"))
            if p.is_file()
        )
        assert dd_snapshot_before == dd_snapshot_after
        # `<target>` 一字未动。
        assert head(target) == head_before
        assert git(target, "status", "--porcelain") == status_before
        assert (target / "sentinel.bin").read_bytes() == sentinel

    def test_self_binding_inflight_does_not_block(self, tmp_path: Path) -> None:
        """本单自身归属绑定且在飞 -> 不拒绝（自身归属解析命中走 False 侧）。"""
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        canonical, _target, _sentinel, dd_root = _h8_target_fixture(
            tmp_path, other_terminal="complete", subject_terminal=""
        )
        # 只有本单（dev-fg-SUBJECT）绑定 target 且在飞：intake 探测 record_worktree
        # 时 detect 返回 bound=dev-fg-SUBJECT == 当前 development_id -> 放行。
        config = _h8_config(tmp_path, canonical=canonical, dd_root=dd_root, ops=DefaultHarvestOps())
        result = run_harvest(config)
        intake_step = next(s for s in result["steps"] if s["step"] == "intake")
        assert intake_step["ok"] is True

    def test_self_sorts_first_does_not_mask_foreign_inflight(self, tmp_path: Path) -> None:
        """rc-3d12fbbe 阻塞项回归：本单 dev id 排序靠前时不得遮蔽更靠后的外来在飞单。

        本单 dev-fg-SUBJECT 与外来 dev-fg-ZED-OTHER 同在飞且都直接绑定 `<target>`
        （`record.repo_path` 直接等于本次要消费的树路径，case 1 命中），而本单 id
        在 `<dd_root>/` 枚举顺序（sorted）中先于外来单。旧实现 detect 返回第一条
        在飞绑定（=本单），`_detect_occupied_tree` 判定 bound==本单即丢弃、不继续
        扫 -> 外来在飞占用被静默漏检，树仍被 rmtree/pull/deploy。修复后 detect
        跳过本单自身绑定继续扫描，必须返回外来单并 escalate（bound=dev-fg-ZED-OTHER）。
        """
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        # 本单 SUBJECT 排序在 ZED-OTHER 之前（'S' < 'Z'），且两单都在飞直接绑定同一棵树。
        canonical, target, sentinel, dd_root = _h8_target_fixture(
            tmp_path,
            other_dev="dev-fg-ZED-OTHER",
            subject_dev="dev-fg-SUBJECT",
            other_terminal="",
            subject_terminal="",
        )
        # ops 层先验证：跳过本单、返回外来单（排序遮蔽被修复）。
        binding = DefaultHarvestOps().detect_inflight_binding(
            target, dd_root, current_development_id="dev-fg-SUBJECT"
        )
        assert binding["in_flight"] is True
        assert binding["bound_development_id"] == "dev-fg-ZED-OTHER", binding

        head_before = head(target)
        status_before = git(target, "status", "--porcelain")
        config = _h8_config(tmp_path, canonical=canonical, dd_root=dd_root, ops=DefaultHarvestOps())
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        intake_step = next(s for s in result["steps"] if s["step"] == "intake")
        assert intake_step["ok"] is False
        assert intake_step["escalate"] == "HARVEST_TREE_OCCUPIED_BY_INFLIGHT"
        assert intake_step["bound_development_id"] == "dev-fg-ZED-OTHER"
        assert intake_step["detail"]
        # 写步骤一个没跑，`<target>` 一字未动。
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert set(receipt["writes_skipped"]) >= set(WRITE_STEPS)
        assert not any(
            s.get("step") in ("worktree_cherry_pick", "pr_squash_merge", "ff_only_pull")
            and s.get("ok") is True
            for s in receipt["steps"]
        )
        assert head(target) == head_before
        assert git(target, "status", "--porcelain") == status_before
        assert (target / "sentinel.bin").read_bytes() == sentinel
        assert target.is_dir() and (target / "sentinel.bin").is_file()

    def test_self_inflight_plus_terminal_other_is_released(self, tmp_path: Path) -> None:
        """rc-3d12fbbe 相邻回归：本单在飞 + 外来已终态 -> 放行（外来不构成在飞占用）。"""
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        # 本单排序靠前、在飞；外来排序靠后、终态（complete）。detect 跳过本单后
        # 只看到终态外来 -> in_flight=False -> 编排层放行走既有链。
        canonical, _target, _sentinel, dd_root = _h8_target_fixture(
            tmp_path,
            other_dev="dev-fg-ZED-OTHER",
            subject_dev="dev-fg-SUBJECT",
            other_terminal="complete",
            subject_terminal="",
        )
        binding = DefaultHarvestOps().detect_inflight_binding(
            canonical, dd_root, current_development_id="dev-fg-SUBJECT"
        )
        assert binding["in_flight"] is False, binding
        assert binding["bound_development_id"] is None, binding

    def test_unrelated_dev_missing_record_json_with_terminal_result_proceeds(
        self, tmp_path: Path
    ) -> None:
        """①——无关单缺 record.json 且 result 终态 -> 收割照常进行（H-A + H-B）。

        构造 dd_root 含一个与本次要收割的树**无关**的发展目录：缺 record.json
        （也无 status.json），但带终态 result.json（terminal 非空）。未修复时
        detect 因该无关目录进全局 `indeterminate` -> 恒 in_flight=True 误 escalate；
        修复后必须 out of scope 跳过 -> in_flight=False、收割照常进行。
        """
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        canonical = tmp_path / "canon"
        _init_git_repo(canonical)
        target = tmp_path / "target"
        git(canonical, "worktree", "add", "--detach", str(target), "main")

        dd_root = tmp_path / "dd"
        # 无关目录：缺 record.json（也无 status.json），带终态 result.json（fault）。
        stray = dd_root / "dev-fg-STRAY"
        stray.mkdir(parents=True)
        (stray / "result.json").write_text(
            json.dumps({"development_id": "dev-fg-STRAY", "terminal": "fault"}),
            encoding="utf-8",
        )
        # 本次要收割的树归属：subject 单 record 绑定 target，终态。
        subject = dd_root / "dev-fg-SUBJECT"
        subject.mkdir(parents=True)
        (subject / "record.json").write_text(
            json.dumps(
                {
                    "development_id": "dev-fg-SUBJECT",
                    "repo_path": str(target),
                    "remote_url": str(tmp_path / "local-remote.git"),
                }
            ),
            encoding="utf-8",
        )
        (subject / "status.json").write_text(
            json.dumps({"development_id": "dev-fg-SUBJECT", "terminal": "complete"}),
            encoding="utf-8",
        )

        # H-A + H-B：缺 record.json 的无关目录 out of scope -> detect 放行。
        binding = DefaultHarvestOps().detect_inflight_binding(target, dd_root)
        assert binding["in_flight"] is False, binding
        assert binding["bound_development_id"] is None, binding

        # 编排层照常进行：occupancy 放行 -> outcome 不是 escalated、intake ok True。
        fake = fake_ops()
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_HARVESTED
        intake_step = next(s for s in result["steps"] if s["step"] == "intake")
        assert intake_step["ok"] is True
        assert intake_step.get("escalate") != "HARVEST_TREE_OCCUPIED_BY_INFLIGHT"

    def test_inflight_foreign_binding_escalates_with_machine_readable_reason(
        self, tmp_path: Path
    ) -> None:
        """②——本次要动的树确被另一在飞单绑定 -> refuse+escalate 且 detail 含单 id 与树路径（H-C）。

        构造 dev-fg-OTHER 的 record repo_path 绑定 <target> 且在飞（status.json
        terminal 空 + 无终态 result.json）。断言 detect 返回 in_flight=True、
        bound=dev-fg-OTHER、`repo_path` 非空、`detail` 同时含 dev id 与树路径；
        编排层 outcome=escalated、intake step escalate 码正确、写步骤一个没跑、
        `<target>` 一字未动。
        """
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        canonical, target, sentinel, dd_root = _h8_target_fixture(
            tmp_path, other_terminal="", subject_terminal="complete"
        )
        # H-C：detect 返回体必带非空 repo_path + 同时含 dev id 与树路径的 detail。
        binding = DefaultHarvestOps().detect_inflight_binding(target, dd_root)
        assert binding["in_flight"] is True, binding
        assert binding["bound_development_id"] == "dev-fg-OTHER", binding
        assert binding["repo_path"], "detect 返回体 repo_path 不得为空"
        assert binding["detail"] and "dev-fg-OTHER" in binding["detail"]
        assert binding["detail"] and str(target.resolve()) in binding["detail"]

        # 编排层：outcome=escalated + intake step 原样落进 repo_path/detail/bound。
        config = _h8_config(tmp_path, canonical=canonical, dd_root=dd_root, ops=DefaultHarvestOps())
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        intake_step = next(s for s in result["steps"] if s["step"] == "intake")
        assert intake_step["ok"] is False
        assert intake_step["escalate"] == "HARVEST_TREE_OCCUPIED_BY_INFLIGHT"
        assert intake_step["bound_development_id"] == "dev-fg-OTHER"
        assert intake_step["repo_path"], "intake step repo_path 不得为空"
        assert intake_step["detail"] and "dev-fg-OTHER" in intake_step["detail"]
        assert intake_step["detail"] and str(target.resolve()) in intake_step["detail"]
        # 写步骤一个没跑 + `<target>` 一字未动（HEAD/porcelain/哨兵字节/目录）。
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert set(receipt["writes_skipped"]) >= set(WRITE_STEPS)
        assert not any(
            s.get("step") in ("worktree_cherry_pick", "pr_squash_merge", "ff_only_pull")
            and s.get("ok") is True
            for s in receipt["steps"]
        )
        assert (target / "sentinel.bin").read_bytes() == sentinel
        assert target.is_dir()

    def test_other_linked_worktree_inflight_does_not_lock_canonical(self, tmp_path: Path) -> None:
        """H8 case 2 收敛阴性（修复前必红）：同仓另一棵 linked worktree 在飞不再锁 canonical。

        canonical + 两棵 linked worktree：dev-fg-OTHER 在飞绑定 `wt-other`
        （record.repo_path = canonical 的**另一棵** linked worktree），本次收割单
        dev-fg-SUBJECT 绑定 `wt-target`（终态）。修复前 case 2 把 `wt-other`
        归属到 canonical -> `detect_inflight_binding(canonical)` 恒 in_flight=True
        误锁整仓；修复后 canonical 与本次要消费的树都 in_flight=False，且
        `run_harvest` 照常 harvested（不「等在飞单跑完再收」）。
        """
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        canonical = tmp_path / "canon"
        _init_git_repo(canonical)
        wt_other = tmp_path / "wt-other"
        git(canonical, "worktree", "add", "--detach", str(wt_other), "main")
        wt_target = tmp_path / "wt-target"
        git(canonical, "worktree", "add", "--detach", str(wt_target), "main")
        (wt_target / "sentinel.bin").write_bytes(H8_SENTINEL_BYTES)

        dd_root = tmp_path / "dd"
        for dev, repo_path, terminal in (
            ("dev-fg-OTHER", str(wt_other), ""),
            ("dev-fg-SUBJECT", str(wt_target), "complete"),
        ):
            dev_dir = dd_root / dev
            dev_dir.mkdir(parents=True)
            (dev_dir / "record.json").write_text(
                json.dumps(
                    {
                        "development_id": dev,
                        "repo_path": repo_path,
                        "remote_url": str(tmp_path / "local-remote.git"),
                    }
                ),
                encoding="utf-8",
            )
            (dev_dir / "status.json").write_text(
                json.dumps(
                    {"development_id": dev, "state": terminal or "running", "terminal": terminal}
                ),
                encoding="utf-8",
            )

        ops = DefaultHarvestOps()
        # 阴性：canonical 与本次要消费的树都不再被同仓另一棵 linked worktree 锁住。
        assert ops.detect_inflight_binding(canonical, dd_root)["in_flight"] is False
        assert ops.detect_inflight_binding(wt_target, dd_root)["in_flight"] is False

        # 编排层照常收割（harvested）：occupancy 用真实（修复后）判定，其余机械步
        # 走 fake，全程不触真网/生产 checkout。
        fake = fake_ops(resolve_canonical=canonical)

        class _RealDetectFakeRest:
            """detect_inflight_binding 走真实 DefaultHarvestOps，其余方法委托 fake。"""

            def __getattr__(self, name):
                return getattr(fake["ops"], name)

            def detect_inflight_binding(
                self, tree_path: Path, dd_root: Path, current_development_id: str | None = None
            ) -> dict[str, Any]:
                return ops.detect_inflight_binding(tree_path, dd_root, current_development_id)

        config = HarvestRunConfig(
            event=approved_unharvested_event(
                development_id="dev-fg-SUBJECT", head_commit="a" * 40, stage="implement"
            ).as_dict(),
            state_root=tmp_path / "supervisor",
            run_root=tmp_path / "runs",
            dd_root=dd_root,
            deploy_command=[],
            allowlist=full_allowlist(str(canonical)),
            ops=_RealDetectFakeRest(),
            bus=FakeBus(),
            publish_notes=True,
        )
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_HARVESTED, result
        intake_step = next(s for s in result["steps"] if s["step"] == "intake")
        assert intake_step["ok"] is True
        assert intake_step.get("escalate") != "HARVEST_TREE_OCCUPIED_BY_INFLIGHT"
        # 本次要消费的树未被 in_flight 单动到（sentinel 仍在，目录仍在）。
        assert (wt_target / "sentinel.bin").read_bytes() == H8_SENTINEL_BYTES
        assert wt_target.is_dir()


class TestResolveVerifyArgv:
    """交付 A/B：verify argv 按目标仓解析（机械口 + 编排层），不再全局硬编码 make verify。

    - 阴性（可红）：合成目标仓无 Makefile（仅任意文件）-> `DefaultHarvestOps
      .resolve_verify_argv` 返回 `(None, "no resolvable verify command")`；未修复
      时恒 `argv==["make","verify"] exit 127`，修复后编排层 escalated 且 detail=
      `no resolvable verify command`，绝不硬跑 make verify 制造误导性 127。
    - 反向不抖动：合成目标仓含 Makefile + verify 目标 -> 仍 `["make","verify"]`
      且 `make verify` exit 0（行为不变）。
    - repo-canonical：无 Makefile 但 pyproject.toml / uv.lock -> uv run pytest -q。
    """

    def test_no_makefile_only_arbitrary_file_resolves_none(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "arbitrary.txt").write_text("x\n", encoding="utf-8")
        argv, detail = DefaultHarvestOps().resolve_verify_argv(worktree)
        assert argv is None
        assert detail == "no resolvable verify command"

    def test_makefile_with_verify_target_resolves_make(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "Makefile").write_text("verify:\n\t@true\n", encoding="utf-8")
        argv, detail = DefaultHarvestOps().resolve_verify_argv(worktree)
        assert argv == ["make", "verify"]
        assert detail == ""

    def test_makefile_without_verify_but_pyproject_resolves_uv(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "Makefile").write_text("test:\n\t@true\n", encoding="utf-8")
        (worktree / "pyproject.toml").write_text("", encoding="utf-8")
        argv, detail = DefaultHarvestOps().resolve_verify_argv(worktree)
        assert argv == ["uv", "run", "pytest", "-q"]
        assert detail == ""

    def test_pyproject_only_resolves_uv(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "pyproject.toml").write_text("", encoding="utf-8")
        argv, _ = DefaultHarvestOps().resolve_verify_argv(worktree)
        assert argv == ["uv", "run", "pytest", "-q"]

    def test_uv_lock_only_resolves_uv(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "uv.lock").write_text("", encoding="utf-8")
        argv, _ = DefaultHarvestOps().resolve_verify_argv(worktree)
        assert argv == ["uv", "run", "pytest", "-q"]

    def test_makefile_verify_target_runs_and_exits_zero(self, tmp_path: Path) -> None:
        """反向不抖动：Makefile 含 verify 目标 -> 仍 make verify 且 exit 0（真跑）。"""
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "Makefile").write_text("verify:\n\t@true\n", encoding="utf-8")
        argv, _ = DefaultHarvestOps().resolve_verify_argv(worktree)
        assert argv == ["make", "verify"]
        exit_code = DefaultHarvestOps().run_verify(worktree, argv)
        assert exit_code == 0

    def test_unresolvable_verify_escalates_with_detail_and_no_run(self, tmp_path: Path) -> None:
        """阴性（编排层）：解析不到 -> run_verify step ok:false + detail，绝无 make 硬跑。"""
        fake = fake_ops(resolve_verify_argv=(None, "no resolvable verify command"))
        config, _ = config_for(tmp_path, ops=fake)
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        rv = next(s for s in result["steps"] if s["step"] == "run_verify")
        assert rv["ok"] is False
        assert rv["detail"] == "no resolvable verify command"
        assert rv["argv"] is None
        # 绝不硬跑 make verify：run_verify 从未被调用。
        assert "run_verify" not in fake["calls"], fake["calls"]
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["writes_skipped"] == list(WRITE_STEPS)

    def test_makefile_repo_still_runs_make_verify_and_harvests(self, tmp_path: Path) -> None:
        """反向不抖动（编排层）：Makefile 仓 -> argv 仍 make verify 且 exit 0 -> harvested。"""
        fake = fake_ops(resolve_verify_argv=(["make", "verify"], ""))
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_HARVESTED
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        rv = next(s for s in receipt["steps"] if s["step"] == "run_verify")
        assert rv["argv"] == ["make", "verify"]
        assert rv["exit_code"] == 0

    def test_real_no_makefile_repo_escalates_no_resolvable(self, tmp_path: Path) -> None:
        """真实 git：无 Makefile 目标仓 -> verify step escalated + detail，HEAD 不变。"""
        repo, product = _real_repo_with_origin(tmp_path)
        before = head(repo)
        config = HarvestRunConfig(
            event=approved_unharvested_event(
                development_id="dev-x", head_commit=product, stage="implement"
            ).as_dict(),
            state_root=tmp_path / "supervisor",
            run_root=tmp_path / "runs",
            dd_root=dd_record_root(tmp_path, str(repo)),
            deploy_command=[],
            allowlist=full_allowlist(str(repo)),
            ops=DefaultHarvestOps(),
            publish_notes=False,
        )
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_ESCALATED
        rv = next(s for s in result["steps"] if s["step"] == "run_verify")
        assert rv["ok"] is False
        assert rv["detail"] == "no resolvable verify command"
        assert rv["argv"] is None
        assert head(repo) == before, "escalated 后默认分支 HEAD 必须逐字节不变"
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["writes_skipped"] == list(WRITE_STEPS)

    def test_explicit_verify_argv_override_still_wins(self, tmp_path: Path) -> None:
        """显式配置（非历史默认）仍是覆盖：resolve 被跳过，直接用配置 argv。"""
        fake = fake_ops(resolve_verify_argv=(["uv", "run", "pytest", "-q"], ""))
        config, _ = config_for(tmp_path, ops=fake, bus=FakeBus())
        config.verify_argv = ["custom", "verify-cmd"]
        result = run_harvest(config)
        assert result["outcome"] == OUTCOME_HARVESTED
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        rv = next(s for s in receipt["steps"] if s["step"] == "run_verify")
        assert rv["argv"] == ["custom", "verify-cmd"]


class TestHarvestWorktreeReclaimGuard:
    """交付 A/B/C/D：主 worktree 判别 + rmtree 护栏 + 真实状态 + 后置校验。

    spec 交付 D：
    1. 阴性（不可省）：主 worktree 检出 `harvest/<id>`（`.git` 为目录）->
       回收/清理步 ok:false 且仓完好（生产 checkout 文件与 `.git` 一字未动、
       `is-inside-work-tree=true`）。未修复时（无判别、rmtree 无护栏）主树被
       清空 = 失败。
    2. 反向不抖动：合法 linked worktree 持有 `harvest/<id>` -> 正常 remove+prune、
       ok:true，主树不受影响。
    3. 不恒返 ok:true：后置断言（`_assert_repo_valid`）识别被破坏的仓 -> 如实报
       ok:false；非 git 目标（普通目录）绝不被 rmtree。
    """

    def test_remove_worktree_refuses_primary_checkout(self, tmp_path: Path) -> None:
        """阴性：目标是主 checkout（`.git` 是目录）且持 `harvest/<id>` 分支 ->
        ok:false + 机器可读 detail，生产 checkout 与 `.git` 一字未动。"""
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo = tmp_path / "canon"
        _init_git_repo(repo)
        git(repo, "checkout", "-q", "-b", "harvest/dev-x")
        sentinel = repo / "sentinel.bin"
        sentinel.write_bytes(b"PRIMARY-INTACT\x00\x01")
        head_before = head(repo)

        ops = DefaultHarvestOps()
        result = ops.remove_worktree(repo, repo)
        assert result["ok"] is False, result
        assert "primary checkout is not reclaimable" in result["detail"], result
        # 仓完好：文件、`.git` 目录、HEAD、is-inside-work-tree 全部未动。
        assert sentinel.read_bytes() == b"PRIMARY-INTACT\x00\x01"
        assert (repo / ".git").is_dir()
        assert head(repo) == head_before
        assert git(repo, "rev-parse", "--is-inside-work-tree") == "true"

    def test_worktree_cherry_pick_preclean_refuses_primary(self, tmp_path: Path) -> None:
        """阴性：worktree_cherry_pick 前置清树把主 checkout 当 worktree_root ->
        护栏拒绝（ok:false），主 checkout 一字未动、`.git` 未被 rmtree。"""
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo = tmp_path / "canon"
        _init_git_repo(repo)
        git(repo, "checkout", "-q", "-b", "harvest/dev-x")
        sentinel = repo / "sentinel.bin"
        sentinel.write_bytes(b"PRIMARY-INTACT\x00\x01")
        head_before = head(repo)

        ops = DefaultHarvestOps()
        result = ops.worktree_cherry_pick(repo, head_before, "main", repo)
        assert result["ok"] is False, result
        assert "primary checkout is not reclaimable" in result["detail"], result
        assert sentinel.read_bytes() == b"PRIMARY-INTACT\x00\x01"
        assert (repo / ".git").is_dir()
        assert head(repo) == head_before

    def test_build_harvest_tip_preclean_refuses_primary(self, tmp_path: Path) -> None:
        """阴性：build_harvest_tip 前置清树同过护栏 -> ok:false，主 checkout 完好。"""
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo = tmp_path / "canon"
        _init_git_repo(repo)
        git(repo, "checkout", "-q", "-b", "harvest/dev-x")
        sentinel = repo / "sentinel.bin"
        sentinel.write_bytes(b"PRIMARY-INTACT\x00\x01")
        head_before = head(repo)

        ops = DefaultHarvestOps()
        result = ops.build_harvest_tip(repo, head_before, "main", repo)
        assert result["ok"] is False, result
        assert "primary checkout is not reclaimable" in result["detail"], result
        assert sentinel.read_bytes() == b"PRIMARY-INTACT\x00\x01"
        assert (repo / ".git").is_dir()
        assert head(repo) == head_before

    def test_linked_worktree_holding_harvest_branch_is_reclaimed(self, tmp_path: Path) -> None:
        """反向不抖动：合法 linked worktree（`.git` 是 gitfile）持有 `harvest/<id>`
        -> 正常 remove+prune、ok:true，主树不受影响。"""
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo = tmp_path / "canon"
        _init_git_repo(repo)
        worktree = tmp_path / "linked-wt"
        git(repo, "worktree", "add", "-b", "harvest/dev-x", str(worktree), "main")
        main_head_before = head(repo)
        assert (worktree / ".git").is_file(), "linked worktree 的 .git 必须是 gitfile"

        ops = DefaultHarvestOps()
        result = ops.remove_worktree(repo, worktree)
        assert result["ok"] is True, result
        assert not worktree.exists(), "linked worktree 应被正常回收"
        assert head(repo) == main_head_before
        assert (repo / ".git").is_dir()

    def test_remove_worktree_refuses_non_git_target(self, tmp_path: Path) -> None:
        """护栏：非 git 工作树目标（普通目录，非主树非 linked）-> 拒绝清理、ok:false，
        目录不被 rmtree。"""
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo = tmp_path / "canon"
        _init_git_repo(repo)
        plain = tmp_path / "plain-dir"
        plain.mkdir()
        ops = DefaultHarvestOps()
        result = ops.remove_worktree(repo, plain)
        assert result["ok"] is False, result
        assert plain.is_dir(), "非 git 目标绝不能被 rmtree"

    def test_remove_worktree_reports_false_when_repo_broken(self, tmp_path: Path) -> None:
        """不恒返 ok:true：主仓 `.git` 被破坏（假绿场景）-> remove_worktree 如实
        ok:false + detail，绝不报成功。"""
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo = tmp_path / "canon"
        _init_git_repo(repo)
        worktree = tmp_path / "linked-wt"
        git(repo, "worktree", "add", "--detach", str(worktree), "main")
        # 模拟「被删主树的假绿」：主仓 .git 目录被破坏。
        shutil.rmtree(repo / ".git")

        ops = DefaultHarvestOps()
        result = ops.remove_worktree(repo, worktree)
        assert result["ok"] is False, result
        assert result["detail"], result

    def test_assert_repo_valid_detects_corruption(self, tmp_path: Path) -> None:
        """交付 C：后置校验能识别被破坏的仓（.git 缺失 / 非 git / HEAD 不可解析）。"""
        from fleet_graph.supervise.harvest_ops import _assert_repo_valid

        repo = tmp_path / "canon"
        _init_git_repo(repo)
        assert _assert_repo_valid(repo) == (True, "")
        shutil.rmtree(repo / ".git")
        ok, detail = _assert_repo_valid(repo)
        assert ok is False
        assert detail


class TestHarvestPrSquashMergeBranchOccupied:
    """M3 分支占用前置检测：pr_squash_merge 在 merge 前只读判占用，占用即
    refuse+escalate，绝不执行 push / gh pr create / gh pr merge（合成本地仓，
    禁触真网/生产 checkout）。

    1. 阴性（本单判据，未修复必红）：残留 worktree 检出 `harvest/<id>` ->
       `pr_squash_merge` 返回 refused=true / escalate=HARVEST_BRANCH_OCCUPIED /
       merged=false，且 gh 零调用（fake 命令计数 0）、git push 不执行、占用者
       worktree 原样保留（占用只 report 不替人删）。
    2. 反向不抖动：无占用 -> 走原路径，merged=true、产出 pr_url（gh 走 fake，
       不触真网）。
    """

    def _repo_with_origin(self, tmp_path: Path) -> Path:
        repo = tmp_path / "canon"
        _init_git_repo(repo)
        origin = tmp_path / "origin.git"
        git(repo, "clone", "--bare", "-q", ".", str(origin))
        git(repo, "remote", "add", "origin", str(origin))
        return repo

    def test_occupied_branch_refuses_without_gh_or_push(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """阴性 fixture：残留 worktree 检出 harvest/dev-x -> refused+escalate。

        未修复时必然走进 `gh pr merge --delete-branch` 报原始 `used by
        worktree`、merged=false 且无 refused/escalate 码；修复后 refused=true +
        escalate=HARVEST_BRANCH_OCCUPIED + gh 零调用（fake 命令计数 0）。
        """
        from fleet_graph.supervise import harvest_ops
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo = self._repo_with_origin(tmp_path)
        head_commit = head(repo)
        # 残留 worktree 检出 harvest/dev-x（真实 git linked worktree）。
        git(repo, "checkout", "-q", "-b", "harvest/dev-x")
        git(repo, "checkout", "-q", "main")
        worktree = tmp_path / "residual-wt"
        git(repo, "worktree", "add", str(worktree), "harvest/dev-x")

        gh_calls: list[list[str]] = []
        real_run = harvest_ops._run

        def recording_run(argv: list[str], cwd: Path | None = None) -> dict[str, Any]:
            gh_calls.append(argv)
            return real_run(argv, cwd=cwd)

        monkeypatch.setattr(harvest_ops, "_run", recording_run)

        result = DefaultHarvestOps().pr_squash_merge(repo, "dev-x", head_commit, "main")
        assert result["merged"] is False, result
        assert result["refused"] is True, result
        assert result["escalate"] == "HARVEST_BRANCH_OCCUPIED", result
        assert str(worktree) in result["detail"], result
        # gh 零调用：merge/create 都不执行（fake 命令计数 0）。
        assert gh_calls == [], f"gh 被调用: {gh_calls}"
        # git push 不执行：origin 上不应出现 harvest/dev-x ref。
        assert git(repo, "ls-remote", "--heads", "origin", "harvest/dev-x") == ""
        # 占用者 worktree 与分支原样保留（占用只 report，绝不替人删）。
        assert worktree.is_dir()
        assert (worktree / "seed.txt").is_file()

    def test_unoccupied_proceeds_to_gh_merge_and_produces_pr_url(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """反向不抖动：无占用 -> 走原路径，merged=true、产出 pr_url（gh 走 fake）。"""
        from fleet_graph.supervise import harvest_ops
        from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

        repo = self._repo_with_origin(tmp_path)
        head_commit = head(repo)
        pr_url = "https://github.com/Dandi007/fleet-harvest-sandbox/pull/7"
        gh_calls: list[list[str]] = []

        def fake_run(argv: list[str], cwd: Path | None = None) -> dict[str, Any]:
            gh_calls.append(argv)
            if argv[:3] == ["gh", "pr", "create"]:
                return {
                    "ok": True,
                    "exit_code": 0,
                    "stdout_tail": pr_url + "\n",
                    "stderr_tail": "",
                }
            return {"ok": True, "exit_code": 0, "stdout_tail": "", "stderr_tail": ""}

        monkeypatch.setattr(harvest_ops, "_run", fake_run)

        result = DefaultHarvestOps().pr_squash_merge(repo, "dev-x", head_commit, "main")
        assert result["merged"] is True, result
        assert result["pr_url"] == pr_url, result
        gh_argv = [argv for argv in gh_calls if argv and argv[0] == "gh"]
        assert any(argv[:3] == ["gh", "pr", "create"] for argv in gh_argv), gh_argv
        assert any(argv[:3] == ["gh", "pr", "merge"] for argv in gh_argv), gh_argv
        # push 已执行：origin 上存在 harvest/dev-x ref。
        assert git(repo, "ls-remote", "--heads", "origin", "harvest/dev-x") != ""
