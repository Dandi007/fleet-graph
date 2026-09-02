"""M3 收割反应器：harvest ReAct 子图（supervisor 进程内）。

输入是 E5 `approved_unharvested` 事件（payload: development_id / head_commit /
stage）。一条开发过了 gate 但产品 commit 尚未落默认分支，这里把它收割进默认分支
并部署。

SOP（spec 交付 B）逐节点实现，全部是 script 节点（机械判定，不采信任何自述）：

1. `intake`       —— 解析 E5 payload，解析目标 repo（dd 准入 record -> repo_path）。
2. `gate`         —— allowlist 判定写目标（repo_path / 目标分支 / 部署命令）。
                   拒绝 -> outcome=refused，记录留痕，**不执行任何写**（交付 A.2/A.3）。
3. `fetch`        —— fetch dd ref（refs/heads/dd/<development_id>）。
4. `cherry`       —— cherry 判重：产品 commit 是否已 cherry 等价进默认分支。
                   已等价 -> outcome=already_harvested，无写动作。
5. `worktree`     —— 独立 worktree cherry-pick 产品 commit，冲突消解（即兴），
                    并洗掉 `.dev-dispatch/` / `.dd-evidence/` 两棵 dd 协议子树
                    得到干净产品树 tip（harvest_tip）。
6. `verify`       —— 在 worktree 跑全量套件。verify argv 按目标仓解析（交付
                   A.1：根目录 Makefile 含 verify 目标 -> make verify；无 Makefile
                   但 pyproject.toml / uv.lock -> repo-canonical 全量套件
                   uv run pytest -q），解析不到可执行指令 -> ok:false + 机器可读
                   detail（no resolvable verify command）-> escalated（交付 A.2）。
7. `cleanup_worktree` —— verify 之后移除一次性 worktree（harvest_ops 成功路径
   保留 worktree 供 verify 使用，见 rc-702098ab）。
8. `pr_merge`     —— 用干净产品树 tip（harvest_tip，已剔除 .dev-dispatch/.dd-evidence）
                   建 harvest 分支 -> PR -> squash merge（H5）。
9. `pull`         —— 先机器可读检测本地 HEAD 与 origin 是否分叉；分叉/无法判定 ->
                   立即 outcome=escalated 并直接走 receipt，绝不 ff_only_pull 带病
                   继续（H3）；未分叉才 ff-only pull 默认分支。
10. `deploy`      —— 运行 allowlist 允许的部署命令。
11. `verify_real` —— 真机 verify，记 exit code。
12. `evidence`    —— evidence note 挂卡。
13. `postconditions` —— 代码核验（交付 B.3）：PR merged + verify 零退出 +
   evidence note 存在，三缺任一 -> outcome=escalated（失败/升报）。
14. `receipt`     —— 结果落 supervisor 自己的 state root。

**生成-验证分离**（交付 B.4）：所有写动作都落在 allowlist 圈定目标——写原语
（git/部署执行）全部被 gate 节点与逐写步骤的 authorize 判定包住；编排层不直接
执行任何 git/部署命令，机械操作委托给 `supervise/harvest_ops.DefaultHarvestOps`
（AST 守卫 Guard D 钉死编排层每个含写原语的函数必须先调用 allowlist gate）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from fleet_graph.bus.board import Board
from fleet_graph.bus.client import BusClient
from fleet_graph.dd.control_plane import DEFAULT_DD_ROOT, RECORD_FILE
from fleet_graph.state.run_artifacts import iso, write_json_durable
from fleet_graph.supervise.events import (
    EVENT_APPROVED_UNHARVESTED,
    SupervisorEvent,
    validate_event,
)
from fleet_graph.supervise.harvest_allowlist import (
    HarvestAllowlist,
    HarvestAuthorization,
)
from fleet_graph.supervise.harvest_ops import EXIT_HEAD_MISMATCH, EXIT_NOT_FOUND

#: harvest 终态词汇（outcome）。REFUSED / ALREADY_HARVESTED 都是无写动作的合法
#: 终止；HARVESTED 要求后置条件三要素齐全；ESCALATED = 失败/升报。
OUTCOME_REFUSED = "refused"
OUTCOME_ALREADY_HARVESTED = "already_harvested"
OUTCOME_HARVESTED = "harvested"
OUTCOME_ESCALATED = "escalated"

#: 默认分支名（spec 用词）。可用 config 覆盖。
DEFAULT_BRANCH = "main"

#: 历史硬编码默认 verify 指令。交付 A 后不再是全局默认——verify_argv 走按目标仓
#: 解析（`HarvestOps.resolve_verify_argv`：Makefile 含 verify 目标 -> make verify；
#: pyproject.toml / uv.lock -> uv run pytest -q）；仅当显式配置且不同于本默认值时
#: 作为覆盖。supervisor 默认透传的 legacy ["make","verify"] 也被视为「未配置」。
DEFAULT_VERIFY_ARGV = ["make", "verify"]

#: 写步骤名单（H7 写前闸）：worktree_cherry_pick / run_verify 任一判红后，这些
#: 写步骤绝不允许执行——pr_squash_merge（push/merge 进默认分支）、ff_only_pull
#: （pull）、deploy（部署）即 spec D1 明确列出的写动作。escalate 收尾时回执用
#: `writes_skipped` 机器可读地记录「哪些写步骤被显式跳过」。
WRITE_STEPS = ("pr_squash_merge", "ff_only_pull", "deploy")

#: H8 动树前 occupancy 门：目标树被另一张在飞单绑定时，intake 立即拒绝+escalate
#: 的机器可读 escalate 码（spec 交付 B.3）。写前闸（H7）与本门不互斥——本门在
#: 更早节点拦（intake 早退 -> receipt），拦不到时 H7 仍在位。
ESCALATE_TREE_OCCUPIED = "HARVEST_TREE_OCCUPIED_BY_INFLIGHT"

#: M3 分支占用 refuse+escalate 码（与 `harvest_ops.pr_squash_merge` 返回的
#: `escalate` 值一致）：本地 `harvest/<development_id>` 分支被任一残留 worktree
#: 检出 -> 占用前置判红 refuse+escalate。编排层据此立即 outcome=escalated +
#: writes_skipped，绝不落「远端已合并却报未合并」的半态，也不触碰 pull/deploy/
#: verify_real 任何写步（spec 交付 A.2：走既有 escalate 收尾）。
ESCALATE_BRANCH_OCCUPIED = "HARVEST_BRANCH_OCCUPIED"

#: SOP 步骤名的封闭枚举——测试据此断言「编排步骤齐全」。
SOP_STEPS = (
    "fetch_dd_ref",
    "cherry_check",
    "worktree_cherry_pick",
    "run_verify",
    "cleanup_worktree",
    "pr_squash_merge",
    "ff_only_pull",
    "deploy",
    "verify_real",
    "evidence_note",
)


class HarvestOps(Protocol):
    """机械操作层接口。编排层只调用这些方法；测试注入 fake。

    默认实现见 `supervise/harvest_ops.DefaultHarvestOps`。所有方法都是「执行
    一件事并返回机械事实」，编排层据此记录 steps 与后置条件。
    """

    def fetch_dd_ref(
        self, repo: Path, development_id: str, remote_url: str | None = None
    ) -> dict[str, Any]: ...
    def resolve_canonical_repo(
        self,
        record_repo_path: str,
        record_remote_url: str | None,
        allowlist_repo_paths: list[str],
    ) -> tuple[Path | None, str]: ...
    def resolve_canonical_repo_unfiltered(
        self,
        record_repo_path: str,
        record_remote_url: str | None,
        candidate_repo_paths: list[str] | None = None,
    ) -> tuple[Path | None, str]: ...
    def cherry_equivalent(self, repo: Path, head_commit: str, default_branch: str) -> bool: ...
    def worktree_cherry_pick(
        self, repo: Path, head_commit: str, default_branch: str, worktree_root: Path
    ) -> dict[str, Any]: ...
    def build_harvest_tip(
        self,
        repo: Path,
        head_commit: str,
        default_branch: str,
        worktree_root: Path,
    ) -> dict[str, Any]: ...
    def remove_worktree(self, repo: Path, worktree_root: Path) -> dict[str, Any]: ...
    def run_verify(self, worktree: Path, argv: list[str]) -> int: ...
    def resolve_verify_argv(self, worktree: Path) -> tuple[list[str] | None, str]: ...
    def board_card_entity_id(self, development_id: str, dd_root: Path) -> str | None: ...
    def detect_inflight_binding(
        self, tree_path: Path, dd_root: Path, current_development_id: str | None = None
    ) -> dict[str, Any]: ...
    def pr_squash_merge(
        self, repo: Path, development_id: str, head_commit: str, default_branch: str
    ) -> dict[str, Any]: ...
    def detect_divergence(self, repo: Path, default_branch: str) -> dict[str, Any]: ...
    def ff_only_pull(self, repo: Path, default_branch: str) -> dict[str, Any]: ...
    def deploy(self, command: list[str], repo: Path) -> int: ...
    def verify_real(self, argv: list[str], repo: Path, expected_head: str | None) -> int: ...


class HarvestState(TypedDict, total=False):
    event: dict[str, Any]
    development_id: str
    head_commit: str
    harvest_tip: str
    stage: str
    repo_path: str
    record_worktree: str
    remote_url: str
    would_resolve_canonical: str
    would_do: list[str]
    default_branch: str
    deploy_command: list[str]
    allowlist_auth: dict[str, Any]
    steps: list[dict[str, Any]]
    pr_merged: bool
    pr_url: str
    verify_exit_code: int
    verify_real_exit_code: int
    deploy_exit_code: int
    merged_head: str
    evidence_note_id: str
    outcome: str
    receipt_path: str
    writes_skipped: list[str]
    _gaps: list[str]
    wiki: Any


@dataclass
class HarvestDeps:
    """harvest 子图对外只依赖这几个端口，全部注入以便测试替换。"""

    allowlist: HarvestAllowlist
    state_root: Path
    run_root: Path
    thread_id: str
    dd_root: Path = DEFAULT_DD_ROOT
    repo: Path | None = None
    default_branch: str = DEFAULT_BRANCH
    deploy_command: list[str] = field(default_factory=list)
    verify_argv: list[str] | None = None
    verify_real_argv: list[str] = field(default_factory=lambda: list(DEFAULT_VERIFY_ARGV))
    ops: HarvestOps | None = None
    bus: BusClient | None = None
    publish_notes: bool = True
    #: katana-wiki-mcp 客户端（可选）。终局 HARVESTED 时追加「生产晋级」分节；
    #: None -> 不汇报（默认）。wiki 是 telemetry，追加失败绝不翻转 outcome /
    #: escalate / 重跑收割（best-effort，见 receipt 节点）。
    wiki: Any | None = None

    def thread_dir(self, key: str) -> Path:
        return self.state_root / "threads" / key


def _event_of(state: HarvestState) -> SupervisorEvent:
    return validate_event(state.get("event") or {})


def _resolve_repo(
    development_id: str,
    dd_root: Path,
    ops: HarvestOps,
    allowlist_repo_paths: list[str],
) -> tuple[Path | None, list[str], str]:
    """dd 准入 record -> canonical 目标仓，机械解析（与 supervisor 同款链）。

    读 record 的 `repo_path`（原始 worktree 路径）+ `remote_url`（纯 JSON 读，
    Guard D 安全），把 canonical 解析委托给 ops 读口 `resolve_canonical_repo`
    （Guard D 豁免的机械层）。返回 `(canonical_path | None, gaps, remote_url)`；
    解析不到可命中 allowlist 的 canonical -> None + 机器可读留痕理由，交由
    intake/gate 走既有 escalated/refused 路径，绝不 fallback 到 record 原始
    worktree 路径去授权或写。`remote_url` 在 `resolve_canonical_repo` 同一处
    读出并原样透传（供 fetch 步取 dd ref，不引入第二解析）。
    """
    record_path = dd_root / development_id / RECORD_FILE
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, [f"dd record 不可读（{record_path}）: {type(exc).__name__}: {exc}"[:300]], ""
    if not isinstance(record, dict):
        return None, [f"dd record 顶层不是 JSON 对象（{record_path}）"], ""
    repo_path = str(record.get("repo_path") or "")
    if not repo_path:
        return None, [f"dd record 缺 repo_path 字段（{record_path}）"], ""
    remote_url = record.get("remote_url")
    if remote_url is not None and not isinstance(remote_url, str):
        remote_url = str(remote_url)
    canonical, reason = ops.resolve_canonical_repo(repo_path, remote_url, allowlist_repo_paths)
    if canonical is None:
        message = reason or (f"record repo_path 无法解析到任何白名单 canonical 仓（{record_path}）")
        return None, [message], str(remote_url or "")
    return canonical, [], str(remote_url or "")


def _record_repo_path(development_id: str, dd_root: Path) -> str:
    """读 dd 准入 record 的 `repo_path`（工作树路径），occupancy 归属锚点。

    H8 交付 B.1：intake 解析 canonical 的同时把 record 的 `repo_path`（原始
    worktree 路径）保留进 `HarvestState.record_worktree`——这是该链要消费的那棵
    树的归属锚点，不能丢。纯 JSON 读（Guard D 安全），不可读/缺字段 -> 返回空串。
    """
    record_path = dd_root / development_id / RECORD_FILE
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(record, dict):
        return ""
    return str(record.get("repo_path") or "")


def _detect_occupied_tree(
    deps: HarvestDeps,
    *,
    development_id: str,
    record_worktree: str,
    canonical: Path,
    worktree_root: Path,
) -> dict[str, Any] | None:
    """H8 交付 B.2/B.3：动树前对链上每棵将被消费的树做 occupancy 探测（纯读口）。

    对 record 的 `repo_path`（record_worktree）、解析出的 canonical `repo`、以及
    本次一次性 `worktree_root`（`deps.thread_dir(...)/worktree`）各调用一次
    `deps.ops.detect_inflight_binding`——凡是要去 rmtree / worktree add / worktree
    remove / pull / deploy 的树，动之前都过一遍。

    任一调用返回 `in_flight=True` 且 `bound_development_id != 当前 development_id`
    -> 立即返回机器可读占用事实 `{"escalate": ..., "bound_development_id": ...,
    "repo_path": ..., "detail": ...}`；否则（无绑定 / 仅终态绑定 / 绑定为本单）->
    None，走既有链。探测读口异常 -> 保守按占用 escalate（fail-closed，绝不静默
    放行）。`repo_path` 与 `detail` 均不得为空（H-C：机器可读理由必须落进
    intake step 与 receipt）。
    """
    probes: list[tuple[str, Path | None]] = [
        ("record_worktree", Path(record_worktree) if record_worktree else None),
        ("canonical", canonical),
        ("worktree_root", worktree_root),
    ]
    for name, tree in probes:
        if tree is None:
            continue
        try:
            # rc-3d12fbbe：传入 current_development_id，让 ops 层跳过本单自身在飞
            # 绑定并继续扫描（防止本单 dev id 排序靠前时遮蔽更靠后的外来在飞单）。
            binding = deps.ops.detect_inflight_binding(
                tree, deps.dd_root, current_development_id=development_id
            )
        except Exception as exc:
            return {
                "escalate": ESCALATE_TREE_OCCUPIED,
                "bound_development_id": None,
                "repo_path": str(tree),
                "detail": f"detect_inflight_binding({name}) 异常，保守 escalate: {repr(exc)[:300]}",
            }
        if binding.get("in_flight") and binding.get("bound_development_id") != development_id:
            return {
                "escalate": ESCALATE_TREE_OCCUPIED,
                "bound_development_id": binding.get("bound_development_id"),
                "repo_path": binding.get("repo_path") or str(tree),
                "detail": binding.get("detail") or f"{name} 被另一张在飞单绑定",
            }
    return None


def authorize_harvest_write(
    allowlist: HarvestAllowlist,
    *,
    repo_path: str,
    branch: str,
    deploy: tuple[str, ...],
) -> HarvestAuthorization:
    """机械写判定，唯一的写门（Guard D 钉死的 gate 名）。

    写权限唯一来源 = 命中白名单条目；命中不了 -> 拒绝 + 留痕。拒绝是结果不是
    异常：调用方把它记进 steps/evidence，并跳过该写动作。
    """
    return allowlist.authorize(repo_path=repo_path, branch=branch, deploy=deploy)


# --- helpers ----------------------------------------------------------------


def _branch_ref(default_branch: str) -> str:
    return f"refs/heads/{default_branch}"


def _record_step(state: HarvestState, step: str, **facts: Any) -> list[dict[str, Any]]:
    steps = list(state.get("steps") or [])
    steps.append({"step": step, **facts})
    return steps


def _record_auth(
    state: HarvestState, auth: HarvestAuthorization, step: str
) -> list[dict[str, Any]]:
    return _record_step(
        state,
        step,
        ok=auth.granted,
        evidence={"granted": auth.granted, "reasons": list(auth.reasons)},
    )


# --- the graph --------------------------------------------------------------


def build_harvest_graph(deps: HarvestDeps) -> StateGraph:
    def intake(state: HarvestState) -> HarvestState:
        event = _event_of(state)
        payload = event.payload or {}
        development_id = str(payload.get("development_id") or "")
        head_commit = str(payload.get("head_commit") or "")
        stage = str(payload.get("stage") or "")
        gaps: list[str] = []
        if not development_id or not head_commit:
            gaps.append("E5 payload 缺 development_id 或 head_commit——事件不完整")
        repo = deps.repo
        record_worktree = ""
        remote_url = ""
        if repo is None and development_id:
            allowlist_repo_paths = [entry.repo_path for entry in deps.allowlist.entries]
            repo, resolve_gaps, remote_url = _resolve_repo(
                development_id, deps.dd_root, deps.ops, allowlist_repo_paths
            )
            gaps.extend(resolve_gaps)
            # H8 交付 B.1：record 的 repo_path（工作树路径）是这棵树的归属锚点，
            # 解析 canonical 后一并保留进 state，供动树前 occupancy 探测消费。
            record_worktree = _record_repo_path(development_id, deps.dd_root)
        elif repo is not None:
            record_worktree = str(repo)
        # 案A改写③：归属解析与 allowlist 授权判定解耦——record 归属的 canonical
        # 不在 allowlist（`resolve_canonical_repo` 解析不到 -> None）时，用纯读
        # unfiltered 口解析「本会归属的 canonical 仓」+「本会执行的写步骤」，把
        # dry-run 留痕写进 e5 报告（would_resolve_canonical / would_do）——
        # 不授予任何写权限，writes_skipped 覆盖全部写步、真机零写（不进入任何
        # 写节点）。授权与否由 gate 另行判定，这里只做观测。
        would_resolve_canonical = ""
        would_do: list[str] = []
        if repo is None and record_worktree:
            try:
                would_canonical, _reason = deps.ops.resolve_canonical_repo_unfiltered(
                    record_worktree, remote_url or None
                )
            except Exception:
                would_canonical = None
            if would_canonical is not None:
                would_resolve_canonical = str(would_canonical)
            # 本会执行的写步骤 = 收割链的写步骤名单（与 writes_skipped 同源，
            # 先观测后授权：只留痕，不执行）。
            would_do = list(WRITE_STEPS)
        # H8 交付 B.2/B.3：_resolve_repo 成功之后、进入 gate 之前，对链上每棵
        # 将被消费的树（record_worktree / canonical / 本次 worktree_root）做
        # occupancy 探测。任一在飞且非本单 -> 立即拒绝+escalate，走既有
        # after_intake（intake 早退 -> receipt），不进入 gate 及其后任何写节点。
        occupied: dict[str, Any] | None = None
        if repo is not None:
            worktree_root = deps.thread_dir(event.key) / "worktree"
            occupied = _detect_occupied_tree(
                deps,
                development_id=development_id,
                record_worktree=record_worktree,
                canonical=repo,
                worktree_root=worktree_root,
            )
        intake_facts: dict[str, Any] = {
            "ok": not gaps and occupied is None,
            "development_id": development_id,
            "head_commit": head_commit,
            "stage": stage,
            "repo_path": str(repo) if repo is not None else "",
            "record_worktree": record_worktree,
            "remote_url": remote_url,
        }
        if occupied is not None:
            # H-C：_detect_occupied_tree 返回的 repo_path（本次判定的树的规范化路径）/
            # detail / bound_development_id 原样落进 intake step，不被上面基础字段吞掉。
            intake_facts.update(occupied)
        steps = _record_step(state, "intake", **intake_facts)
        outcome = None
        if gaps or occupied is not None:
            outcome = OUTCOME_ESCALATED
        return {
            "development_id": development_id,
            "head_commit": head_commit,
            "stage": stage,
            "repo_path": str(repo) if repo is not None else "",
            "record_worktree": record_worktree,
            "remote_url": remote_url,
            "default_branch": deps.default_branch,
            "deploy_command": list(deps.deploy_command),
            "steps": steps,
            "_gaps": gaps,
            "outcome": outcome,
            "writes_skipped": list(WRITE_STEPS) if occupied is not None else None,
        }

    def gate(state: HarvestState) -> HarvestState:
        auth = authorize_harvest_write(
            deps.allowlist,
            repo_path=state.get("repo_path") or "",
            branch=_branch_ref(state.get("default_branch") or DEFAULT_BRANCH),
            deploy=tuple(state.get("deploy_command") or ()),
        )
        steps = _record_auth(state, auth, "gate")
        if not auth.granted:
            return {
                "allowlist_auth": auth.as_dict(),
                "steps": steps,
                "outcome": OUTCOME_REFUSED,
            }
        return {"allowlist_auth": auth.as_dict(), "steps": steps}

    def fetch(state: HarvestState) -> HarvestState:
        auth = authorize_harvest_write(
            deps.allowlist,
            repo_path=state.get("repo_path") or "",
            branch=_branch_ref(state.get("default_branch") or DEFAULT_BRANCH),
            deploy=(),
        )
        if not auth.granted:
            return {
                "steps": _record_auth(state, auth, "fetch_dd_ref"),
                "outcome": OUTCOME_REFUSED,
            }
        repo = Path(state.get("repo_path") or "")
        try:
            result = deps.ops.fetch_dd_ref(
                repo,
                state.get("development_id") or "",
                state.get("remote_url") or None,
            )
        except Exception as exc:
            return {"steps": _record_step(state, "fetch_dd_ref", ok=False, detail=repr(exc)[:300])}
        step = {**result, "ok": bool(result.get("ok"))}
        return {"steps": _record_step(state, "fetch_dd_ref", **step)}

    def cherry(state: HarvestState) -> HarvestState:
        repo = Path(state.get("repo_path") or "")
        equivalent = False
        try:
            equivalent = bool(
                deps.ops.cherry_equivalent(
                    repo, state.get("head_commit") or "", state.get("default_branch") or ""
                )
            )
        except Exception as exc:
            return {"steps": _record_step(state, "cherry_check", ok=False, detail=repr(exc)[:300])}
        steps = _record_step(state, "cherry_check", ok=True, already_harvested=equivalent)
        if equivalent:
            return {"steps": steps, "outcome": OUTCOME_ALREADY_HARVESTED}
        return {"steps": steps}

    def worktree(state: HarvestState) -> HarvestState:
        auth = authorize_harvest_write(
            deps.allowlist,
            repo_path=state.get("repo_path") or "",
            branch=_branch_ref(state.get("default_branch") or DEFAULT_BRANCH),
            deploy=(),
        )
        if not auth.granted:
            return {
                "steps": _record_auth(state, auth, "worktree_cherry_pick"),
                "outcome": OUTCOME_REFUSED,
            }
        repo = Path(state.get("repo_path") or "")
        worktree_root = deps.thread_dir(_event_of(state).key) / "worktree"
        try:
            result = deps.ops.worktree_cherry_pick(
                repo,
                state.get("head_commit") or "",
                state.get("default_branch") or "",
                worktree_root,
            )
        except Exception as exc:
            return {
                "steps": _record_step(
                    state, "worktree_cherry_pick", ok=False, detail=repr(exc)[:300]
                ),
                "outcome": OUTCOME_ESCALATED,
                "writes_skipped": list(WRITE_STEPS),
            }
        steps = _record_step(state, "worktree_cherry_pick", **result)
        if not result.get("ok"):
            # H7 写前闸：cherry-pick 判红 -> 立即停止链（见 after_worktree），
            # 不执行任何写步骤；escalate 收尾时回执显式记录 writes_skipped。
            return {
                "steps": steps,
                "outcome": OUTCOME_ESCALATED,
                "writes_skipped": list(WRITE_STEPS),
            }
        # 干净产品树 tip（worktree_cherry_pick 已剔除 .dev-dispatch/.dd-evidence）。
        harvest_tip = str(result.get("harvest_tip") or "")
        return {"steps": steps, "harvest_tip": harvest_tip}

    def verify(state: HarvestState) -> HarvestState:
        auth = authorize_harvest_write(
            deps.allowlist,
            repo_path=state.get("repo_path") or "",
            branch=_branch_ref(state.get("default_branch") or DEFAULT_BRANCH),
            deploy=(),
        )
        if not auth.granted:
            return {
                "steps": _record_auth(state, auth, "run_verify"),
                "outcome": OUTCOME_REFUSED,
            }
        worktree = deps.thread_dir(_event_of(state).key) / "worktree"
        # 交付 A.1：verify argv 按目标仓解析，不再全局硬编码 make verify。
        # 显式配置且非历史硬编码默认 -> 直接覆盖（测试/运维注入）；否则（含
        # supervisor 默认透传的 legacy ["make","verify"]）走 ops 机械口按目标仓
        # 自身声明解析——根目录 Makefile 含 verify 目标 -> make verify；无 Makefile
        # 但 pyproject.toml / uv.lock -> repo-canonical 全量套件（uv run pytest -q）。
        configured = deps.verify_argv
        if configured is not None and list(configured) != list(DEFAULT_VERIFY_ARGV):
            argv = list(configured)
            detail = ""
        else:
            try:
                argv, detail = deps.ops.resolve_verify_argv(worktree)
            except Exception as exc:
                return {
                    "steps": _record_step(state, "run_verify", ok=False, detail=repr(exc)[:300]),
                    "verify_exit_code": EXIT_NOT_FOUND,
                    "outcome": OUTCOME_ESCALATED,
                    "writes_skipped": list(WRITE_STEPS),
                }
            if argv is None:
                # 交付 A.2：解析不到可执行 verify 指令 -> 如实 ok:false + 机器可读
                # detail（no resolvable verify command）-> escalated；绝不硬跑
                # make verify 制造误导性 127。
                steps = _record_step(
                    state,
                    "run_verify",
                    ok=False,
                    detail=detail or "no resolvable verify command",
                    argv=None,
                )
                return {
                    "steps": steps,
                    "verify_exit_code": EXIT_NOT_FOUND,
                    "outcome": OUTCOME_ESCALATED,
                    "writes_skipped": list(WRITE_STEPS),
                }
        try:
            exit_code = int(deps.ops.run_verify(worktree, argv))
        except Exception as exc:
            return {
                "steps": _record_step(state, "run_verify", ok=False, detail=repr(exc)[:300]),
                "verify_exit_code": EXIT_NOT_FOUND,
                "outcome": OUTCOME_ESCALATED,
                "writes_skipped": list(WRITE_STEPS),
            }
        steps = _record_step(state, "run_verify", ok=exit_code == 0, exit_code=exit_code, argv=argv)
        if exit_code != 0:
            # H7 写前闸：verify 判红 -> 立即停止链（见 after_verify），不执行
            # 任何写步骤；escalate 收尾时回执显式记录 writes_skipped。
            return {
                "steps": steps,
                "verify_exit_code": exit_code,
                "outcome": OUTCOME_ESCALATED,
                "writes_skipped": list(WRITE_STEPS),
            }
        return {"steps": steps, "verify_exit_code": exit_code}

    def cleanup_worktree(state: HarvestState) -> HarvestState:
        """verify 之后移除一次性 worktree。

        harvest_ops 成功路径保留 worktree 供 `run_verify` 在真实目录上跑全量套件
        （rc-702098ab：finally 提前删除会导致 verify 永远 127）；这里在 verify
        完成之后清理。仍是写动作，走同一个 allowlist 门（拒绝 -> refused）。
        """
        auth = authorize_harvest_write(
            deps.allowlist,
            repo_path=state.get("repo_path") or "",
            branch=_branch_ref(state.get("default_branch") or DEFAULT_BRANCH),
            deploy=(),
        )
        if not auth.granted:
            return {
                "steps": _record_auth(state, auth, "cleanup_worktree"),
                "outcome": OUTCOME_REFUSED,
            }
        repo = Path(state.get("repo_path") or "")
        worktree_root = deps.thread_dir(_event_of(state).key) / "worktree"
        try:
            result = deps.ops.remove_worktree(repo, worktree_root)
        except Exception as exc:
            return {
                "steps": _record_step(state, "cleanup_worktree", ok=False, detail=repr(exc)[:300])
            }
        steps = _record_step(state, "cleanup_worktree", **result)
        return {"steps": steps}

    def pr_merge(state: HarvestState) -> HarvestState:
        auth = authorize_harvest_write(
            deps.allowlist,
            repo_path=state.get("repo_path") or "",
            branch=_branch_ref(state.get("default_branch") or DEFAULT_BRANCH),
            deploy=(),
        )
        if not auth.granted:
            return {
                "steps": _record_auth(state, auth, "pr_squash_merge"),
                "outcome": OUTCOME_REFUSED,
            }
        repo = Path(state.get("repo_path") or "")
        harvest_tip = state.get("harvest_tip") or state.get("head_commit") or ""
        try:
            result = deps.ops.pr_squash_merge(
                repo,
                state.get("development_id") or "",
                harvest_tip,
                state.get("default_branch") or "",
            )
        except Exception as exc:
            return {
                "steps": _record_step(state, "pr_squash_merge", ok=False, detail=repr(exc)[:300])
            }
        merged = bool(result.get("merged"))
        pr_url = str(result.get("pr_url") or "")
        steps = _record_step(state, "pr_squash_merge", ok=merged, commit=harvest_tip, **result)
        # M3 分支占用 refuse+escalate：pr_squash_merge 返回 refused+escalate ->
        # 立即 outcome=escalated + writes_skipped，绝不落半态、绝不触碰任何写步
        # （spec 交付 A.2：走既有 escalate 收尾，不执行 gh pr merge / 后续写步）。
        if result.get("refused") and result.get("escalate") == ESCALATE_BRANCH_OCCUPIED:
            return {
                "steps": steps,
                "pr_merged": merged,
                "pr_url": pr_url,
                "outcome": OUTCOME_ESCALATED,
                "writes_skipped": list(WRITE_STEPS),
            }
        return {"steps": steps, "pr_merged": merged, "pr_url": pr_url}

    def pull(state: HarvestState) -> HarvestState:
        auth = authorize_harvest_write(
            deps.allowlist,
            repo_path=state.get("repo_path") or "",
            branch=_branch_ref(state.get("default_branch") or DEFAULT_BRANCH),
            deploy=(),
        )
        if not auth.granted:
            return {
                "steps": _record_auth(state, auth, "ff_only_pull"),
                "outcome": OUTCOME_REFUSED,
            }
        repo = Path(state.get("repo_path") or "")
        default_branch = state.get("default_branch") or ""
        # H3：pull 前先机器可读判定「本地 HEAD vs origin」是否分叉。分叉/无法判定
        # -> 立即 escalate，绝不带病 ff_only_pull（其必然报 Diverging branches
        # 且链上无恢复路径），也不跑 deploy/verify_real。纯读口，零写原语。
        try:
            divergence = deps.ops.detect_divergence(repo, default_branch)
        except Exception as exc:
            divergence = {
                "diverged": True,
                "local_head": None,
                "origin_head": None,
                "detail": f"detect_divergence 异常，按无法判定保守 escalate: {repr(exc)[:300]}",
            }
        if divergence.get("diverged"):
            return {
                "steps": _record_step(
                    state,
                    "ff_only_pull",
                    ok=False,
                    escalate="HARVEST_DIVERGED_LOCAL_VS_ORIGIN",
                    local_head=divergence.get("local_head"),
                    origin_head=divergence.get("origin_head"),
                    detail=divergence.get("detail") or "local 与 origin 分叉",
                ),
                "outcome": OUTCOME_ESCALATED,
            }
        try:
            result = deps.ops.ff_only_pull(repo, default_branch)
        except Exception as exc:
            return {"steps": _record_step(state, "ff_only_pull", ok=False, detail=repr(exc)[:300])}
        step = {**result, "ok": bool(result.get("ok"))}
        steps = _record_step(state, "ff_only_pull", **step)
        # 已合并 commit 的唯一机械来源：pull 成功后 ops 返回的 HEAD，绝不另造。
        return {"steps": steps, "merged_head": result.get("head")}

    def deploy(state: HarvestState) -> HarvestState:
        command = list(state.get("deploy_command") or ())
        auth = authorize_harvest_write(
            deps.allowlist,
            repo_path=state.get("repo_path") or "",
            branch=_branch_ref(state.get("default_branch") or DEFAULT_BRANCH),
            deploy=tuple(command),
        )
        if not auth.granted:
            return {
                "steps": _record_auth(state, auth, "deploy"),
                "outcome": OUTCOME_REFUSED,
            }
        steps = _record_step(state, "deploy", command=command)
        if not command:
            return {"steps": steps, "deploy_exit_code": 0}
        repo = Path(state.get("repo_path") or "")
        try:
            exit_code = int(deps.ops.deploy(command, repo))
        except Exception as exc:
            return {"steps": _record_step(state, "deploy", ok=False, detail=repr(exc)[:300])}
        steps = _record_step(state, "deploy", ok=exit_code == 0, exit_code=exit_code)
        return {"steps": steps, "deploy_exit_code": exit_code}

    def verify_real(state: HarvestState) -> HarvestState:
        auth = authorize_harvest_write(
            deps.allowlist,
            repo_path=state.get("repo_path") or "",
            branch=_branch_ref(state.get("default_branch") or DEFAULT_BRANCH),
            deploy=(),
        )
        if not auth.granted:
            return {
                "steps": _record_auth(state, auth, "verify_real"),
                "outcome": OUTCOME_REFUSED,
            }
        repo = Path(state.get("repo_path") or "")
        merged_head = state.get("merged_head")
        # H9 交付：verify_real 与 run_verify 共用同一机械口
        # `HarvestOps.resolve_verify_argv`（解析规则一字不改），不再全局硬编码
        # make verify——uv 管仓（pyproject.toml/uv.lock、无 Makefile）真机 deploy
        # 后用 legacy make verify 退出 2 制造误导性红。
        # 显式配置且非历史硬编码默认 -> 直接覆盖（测试/运维注入，行为不变）；
        # 否则（含 supervisor 默认透传的 legacy ["make","verify"]）按目标仓自身
        # 声明解析（`repo` = canonical 目标仓，pull 后已位于 merged head；纯读）。
        configured = deps.verify_real_argv
        if configured is not None and list(configured) != list(DEFAULT_VERIFY_ARGV):
            argv = list(configured)
        else:
            try:
                argv, detail = deps.ops.resolve_verify_argv(repo)
            except Exception as exc:
                return {
                    "steps": _record_step(state, "verify_real", ok=False, detail=repr(exc)[:300]),
                    "verify_real_exit_code": EXIT_NOT_FOUND,
                    "outcome": OUTCOME_ESCALATED,
                }
            if argv is None:
                # 解析不到可执行 verify 指令 -> 如实 ok:false + 机器可读 detail
                # （no resolvable verify command）-> escalated；绝不硬跑
                # make verify 制造误导性退出码。
                steps = _record_step(
                    state,
                    "verify_real",
                    ok=False,
                    detail=detail or "no resolvable verify command",
                )
                return {
                    "steps": steps,
                    "verify_real_exit_code": EXIT_NOT_FOUND,
                    "outcome": OUTCOME_ESCALATED,
                }
        try:
            exit_code = int(deps.ops.verify_real(argv, repo, merged_head))
        except Exception as exc:
            return {
                "steps": _record_step(state, "verify_real", ok=False, detail=repr(exc)[:300]),
                "verify_real_exit_code": EXIT_NOT_FOUND,
                "outcome": OUTCOME_ESCALATED,
            }
        facts: dict[str, Any] = {
            "ok": exit_code == 0,
            "exit_code": exit_code,
            "argv": argv,
        }
        if exit_code == EXIT_HEAD_MISMATCH:
            facts["detail"] = "HEAD 与已合并 commit 不一致——拒绝在陈旧树上报绿"
        steps = _record_step(state, "verify_real", **facts)
        return {"steps": steps, "verify_real_exit_code": exit_code}

    def evidence(state: HarvestState) -> HarvestState:
        event = _event_of(state)
        if not deps.publish_notes or deps.bus is None:
            return {
                "steps": _record_step(
                    state, "evidence_note", ok=False, detail="无 bus 凭证——note 未挂卡"
                )
            }
        development_id = state.get("development_id") or ""
        head_commit = state.get("head_commit") or ""
        card_entity_id = deps.ops.board_card_entity_id(development_id, deps.dd_root)
        note = (
            f"harvest {event.type} {event.key}: {state.get('outcome') or 'in_progress'}\n"
            f"development={development_id} head_commit={head_commit}\n"
            f"steps: {[s.get('step') for s in state.get('steps') or []]}"
        )
        if not card_entity_id:
            # 该 development 尚无 goal-line board card（dd record 缺 card_entity_id）。
            # best-effort：如实 skip，绝不把 development_id 当 ref 目标伪造。
            return {
                "steps": _record_step(
                    state,
                    "evidence_note",
                    ok=False,
                    detail="card_entity_id 缺失——note 未挂卡（best-effort）",
                )
            }
        try:
            published = Board(deps.bus).evidence(
                card_entity_id=card_entity_id,
                text=note,
                idempotency_key=f"harvest:{event.key}",
            )
        except Exception as exc:
            return {
                "steps": _record_step(
                    state, "evidence_note", ok=False, detail=f"board note 被拒: {repr(exc)[:300]}"
                )
            }
        steps = _record_step(state, "evidence_note", ok=True, evidence_note_id=published.message_id)
        return {"steps": steps, "evidence_note_id": published.message_id}

    def postconditions(state: HarvestState) -> HarvestState:
        """后置条件代码核验（交付 B.3）：不采信子图自述，只看机械事实。

        PR merged（且 PR 链接非空）+ verify 命令零退出 + evidence note 存在，
        三缺任一 -> escalated。此外扫描 `state["steps"]`：任一 step 的 `ok` 为
        假（机械事实，非自述）也计缺失 -> escalated——「收割链里非零退出必须
        停下来交人工」落进代码，任何中途 step 的 ok:false 都不再被静默吞掉
        （H2 终局语义缺口修复）。
        """
        missing: list[str] = []
        if not state.get("pr_merged"):
            missing.append("PR merged 未达成")
        if not state.get("pr_url"):
            missing.append("PR merged 链接缺失（无真实 forge PR 链接）")
        if state.get("verify_exit_code") != 0:
            missing.append(f"verify 命令退出码 {state.get('verify_exit_code')!r} != 0")
        if not state.get("evidence_note_id"):
            missing.append("evidence note 不存在（未挂卡）")
        for step in state.get("steps") or []:
            if step.get("ok") is False:
                name = str(step.get("step") or "?")
                facts: list[str] = []
                if step.get("detail") is not None:
                    facts.append(f"detail={step['detail']!r}")
                if step.get("exit_code") is not None:
                    facts.append(f"exit_code={step['exit_code']!r}")
                missing.append(f"step {name} ok:false（{' '.join(facts)}）")
        steps = _record_step(state, "postconditions", ok=not missing, missing=missing)
        outcome = OUTCOME_HARVESTED if not missing else OUTCOME_ESCALATED
        return {"steps": steps, "outcome": outcome}

    def receipt(state: HarvestState) -> HarvestState:
        event = _event_of(state)
        outcome = state.get("outcome")
        # 交付 A：harvest 生产晋级分节接线。best-effort——wiki 是 telemetry，
        # 追加失败只记 wiki_report step ok:false + detail，绝不翻转 outcome /
        # escalate / 重跑收割。守卫 `outcome == OUTCOME_HARVESTED` 是阴性守卫的
        # 锚点：去掉后未收割成功的单也会被写成已上线（telemetry 可以失败、
        # 不可以撒谎）。
        if deps.wiki is not None and state.get("outcome") == OUTCOME_HARVESTED:
            try:
                from fleet_graph.supervise.wiki_report import record_production_promotion

                commit = state.get("merged_head") or state.get("harvest_tip") or ""
                evidence: tuple[str, ...] = tuple(
                    p
                    for p in (
                        state.get("pr_url") or "",
                        f"commit {commit}".strip() if commit else "",
                        f"event {event.key}",
                    )
                    if p
                )
                record_production_promotion(
                    deps.wiki,
                    development_name=state.get("development_id") or "",
                    background=(
                        f"development {state.get('development_id') or ''} 通过 gate 后由 "
                        "harvest 反应器收割进默认分支。"
                    ),
                    delivery=(
                        f"harvest outcome={outcome}：产品 commit 已 squash merge + "
                        "ff-only pull 落默认分支，verify 零退出。"
                    ),
                    evidence=evidence,
                    at=iso(time.time()),
                    skeleton="# 舰队开发阶段性成果报告\n\n按「报告更新约定」追加分节。\n",
                )
            except Exception as exc:  # telemetry must not bite
                steps = list(state.get("steps") or [])
                steps.append(
                    {
                        "step": "wiki_report",
                        "ok": False,
                        "detail": f"wiki 追加失败: {repr(exc)[:200]}",
                    }
                )
                state = {**state, "steps": steps}
        path = write_json_durable(
            deps.state_root / "reports" / f"{event.key}.json",
            {
                "event": event.as_dict(),
                "thread_id": deps.thread_id,
                "development_id": state.get("development_id"),
                "head_commit": state.get("head_commit"),
                "harvest_tip": state.get("harvest_tip"),
                "stage": state.get("stage"),
                "repo_path": state.get("repo_path"),
                "record_worktree": state.get("record_worktree"),
                "default_branch": state.get("default_branch"),
                "allowlist_auth": state.get("allowlist_auth") or {},
                "steps": state.get("steps") or [],
                "pr_merged": state.get("pr_merged"),
                "pr_url": state.get("pr_url"),
                "verify_exit_code": state.get("verify_exit_code"),
                "verify_real_exit_code": state.get("verify_real_exit_code"),
                "evidence_note_id": state.get("evidence_note_id"),
                "outcome": state.get("outcome"),
                "writes_skipped": state.get("writes_skipped") or [],
            },
        )
        return {"receipt_path": str(path), "steps": state.get("steps") or []}

    def after_gate(state: HarvestState) -> str:
        return "fetch" if state.get("outcome") is None else "receipt"

    def after_cherry(state: HarvestState) -> str:
        return "worktree" if state.get("outcome") is None else "receipt"

    def after_pull(state: HarvestState) -> str:
        # H3：pull 前分叉检测命中 -> outcome 已设（escalated）-> 直接 receipt，
        # 不再跑 deploy/verify_real；未分叉 -> 既有链。
        return "deploy" if state.get("outcome") is None else "receipt"

    def after_pr_merge(state: HarvestState) -> str:
        # M3 分支占用 refuse+escalate：pr_squash_merge 返回 refused+escalate ->
        # outcome 已设（escalated）-> 绝不进入 pull/deploy/verify_real 任何写步，
        # 直接走既有 escalate 收尾（postconditions -> receipt）；outcome=refused
        # （per-write 门拒绝）-> 直接 receipt。未判红 -> 既有链进 pull。
        if state.get("outcome") is None:
            return "pull"
        if state.get("outcome") == OUTCOME_REFUSED:
            return "receipt"
        return "postconditions"

    def after_worktree(state: HarvestState) -> str:
        # H7 写前闸：worktree_cherry_pick 判红 -> outcome=escalated -> 直接 escalate
        # 收尾（postconditions 只读记缺失 -> receipt），绝不进入
        # verify/cleanup/pr_merge/pull/deploy 等后续步骤（无一写动作执行）。
        # outcome=refused（per-write 门拒绝）-> 直接 receipt，不经过 postconditions
        # （refused 是独立合法终态，不得被 postconditions 覆盖成 escalated）。
        if state.get("outcome") is None:
            return "verify"
        if state.get("outcome") == OUTCOME_REFUSED:
            return "receipt"
        return "postconditions"

    def after_verify(state: HarvestState) -> str:
        # H7 写前闸：run_verify 判红 -> 仍走 cleanup_worktree 收掉一次性 worktree
        # （housekeeping：清理失败路径遗留的临时 worktree，避免下一次收割被陈旧
        # 注册卡死；cleanup 不是 push/merge/部署类写步骤，spec 写前闸不禁止），
        # 但绝不进入 pr_merge/pull/deploy/verify_real——真正的写闸在
        # after_cleanup：outcome 已设 -> 直接 postconditions escalate 收尾。
        # outcome=refused -> 直接 receipt（语义同上）。
        if state.get("outcome") is None:
            return "cleanup_worktree"
        if state.get("outcome") == OUTCOME_REFUSED:
            return "receipt"
        return "cleanup_worktree"

    def after_cleanup(state: HarvestState) -> str:
        # H7 写前闸（真正的闸口）：verify 判红后 cleanup 已完成 housekeeping，
        # 这里 outcome 已设（escalated）-> 绝不进入 pr_merge（写默认分支），
        # 直接 postconditions escalate 收尾；未判红 -> 既有链进 pr_merge。
        if state.get("outcome") is None:
            return "pr_merge"
        if state.get("outcome") == OUTCOME_REFUSED:
            return "receipt"
        return "postconditions"

    def after_intake(state: HarvestState) -> str:
        return "gate" if state.get("outcome") is None else "receipt"

    graph: StateGraph = StateGraph(HarvestState)
    graph.add_node("intake", intake)
    graph.add_node("gate", gate)
    graph.add_node("fetch", fetch)
    graph.add_node("cherry", cherry)
    graph.add_node("worktree", worktree)
    graph.add_node("verify", verify)
    graph.add_node("cleanup_worktree", cleanup_worktree)
    graph.add_node("pr_merge", pr_merge)
    graph.add_node("pull", pull)
    graph.add_node("deploy", deploy)
    graph.add_node("verify_real", verify_real)
    graph.add_node("evidence", evidence)
    graph.add_node("postconditions", postconditions)
    graph.add_node("receipt", receipt)

    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", after_intake, {"gate", "receipt"})
    graph.add_conditional_edges("gate", after_gate, {"fetch", "receipt"})
    graph.add_edge("fetch", "cherry")
    graph.add_conditional_edges("cherry", after_cherry, {"worktree", "receipt"})
    graph.add_conditional_edges("worktree", after_worktree, {"verify", "postconditions", "receipt"})
    graph.add_conditional_edges("verify", after_verify, {"cleanup_worktree", "receipt"})
    graph.add_conditional_edges(
        "cleanup_worktree", after_cleanup, {"pr_merge", "postconditions", "receipt"}
    )
    graph.add_conditional_edges("pr_merge", after_pr_merge, {"pull", "postconditions", "receipt"})
    graph.add_conditional_edges("pull", after_pull, {"deploy", "receipt"})
    graph.add_edge("deploy", "verify_real")
    graph.add_edge("verify_real", "evidence")
    graph.add_edge("evidence", "postconditions")
    graph.add_edge("postconditions", "receipt")
    graph.add_edge("receipt", END)
    return graph


# --- assembly ---------------------------------------------------------------


@dataclass
class HarvestRunConfig:
    event: dict[str, Any]
    state_root: Path = Path("/data/fleet-graph/supervisor")
    run_root: Path = Path("/data/fleet-graph/runs")
    checkpoint_path: str | None = None
    dd_root: Path = DEFAULT_DD_ROOT
    repo: Path | None = None
    default_branch: str = DEFAULT_BRANCH
    deploy_command: list[str] = field(default_factory=list)
    verify_argv: list[str] | None = None
    verify_real_argv: list[str] = field(default_factory=lambda: list(DEFAULT_VERIFY_ARGV))
    allowlist: HarvestAllowlist = field(default_factory=HarvestAllowlist.default)
    ops: HarvestOps | None = None
    bus: BusClient | None = None
    publish_notes: bool = True
    #: katana-wiki-mcp 客户端（可选）。终局 HARVESTED 时追加「生产晋级」分节；
    #: None -> 不汇报（默认）。wiki 是 telemetry，追加失败绝不翻转 outcome。
    wiki: Any | None = None

    @property
    def resolved_checkpoint_path(self) -> str:
        return self.checkpoint_path or str(self.state_root / "checkpoint.sqlite3")


def build_harvest(config: HarvestRunConfig) -> tuple[Any, HarvestDeps, SupervisorEvent]:
    from fleet_graph.supervise.harvest_ops import DefaultHarvestOps

    event = validate_event(config.event)
    deps = HarvestDeps(
        allowlist=config.allowlist,
        state_root=config.state_root,
        run_root=config.run_root,
        thread_id=event.thread_id,
        dd_root=config.dd_root,
        repo=config.repo,
        default_branch=config.default_branch,
        deploy_command=list(config.deploy_command),
        verify_argv=None if config.verify_argv is None else list(config.verify_argv),
        verify_real_argv=list(config.verify_real_argv),
        ops=config.ops or DefaultHarvestOps(),
        bus=config.bus,
        publish_notes=config.publish_notes,
        wiki=config.wiki,
    )
    return build_harvest_graph(deps), deps, event


def run_harvest(config: HarvestRunConfig) -> dict[str, Any]:
    """跑一次 harvest 收割到 receipt，线程已终局则 no-op（与 run_supervisor 同语义）。"""
    from langgraph.checkpoint.sqlite import SqliteSaver

    graph, _deps, event = build_harvest(config)
    invoke_config: dict[str, Any] = {
        "configurable": {"thread_id": event.thread_id},
        "recursion_limit": 50,
    }

    checkpoint = config.resolved_checkpoint_path
    if checkpoint != ":memory:":
        Path(checkpoint).parent.mkdir(parents=True, exist_ok=True)

    with SqliteSaver.from_conn_string(checkpoint) as saver:
        compiled = graph.compile(checkpointer=saver)
        snapshot = compiled.get_state(invoke_config)
        if snapshot.next:
            start: dict[str, Any] | None = None  # resume in place
        elif snapshot.values and snapshot.values.get("receipt_path"):
            return {
                "event": event.as_dict(),
                "thread_id": event.thread_id,
                "outcome": snapshot.values.get("outcome"),
                "receipt_path": snapshot.values.get("receipt_path"),
                "resumed": "already_complete",
            }
        else:
            start = {"event": event.as_dict()}
        state = compiled.invoke(start, config=invoke_config)

    return {
        "event": event.as_dict(),
        "thread_id": event.thread_id,
        "outcome": state.get("outcome"),
        "steps": state.get("steps"),
        "receipt_path": state.get("receipt_path"),
    }


__all__ = [
    "DEFAULT_BRANCH",
    "DEFAULT_VERIFY_ARGV",
    "ESCALATE_TREE_OCCUPIED",
    "EVENT_APPROVED_UNHARVESTED",
    "OUTCOME_ALREADY_HARVESTED",
    "OUTCOME_ESCALATED",
    "OUTCOME_HARVESTED",
    "OUTCOME_REFUSED",
    "SOP_STEPS",
    "HarvestDeps",
    "HarvestOps",
    "HarvestRunConfig",
    "HarvestState",
    "authorize_harvest_write",
    "build_harvest",
    "build_harvest_graph",
    "run_harvest",
]
