# Harvest 反应器两缺陷修复 spec（pr_squash_merge 真 forge + evidence 真实板卡实体）

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：监督面 wf-216dc3 worker 取证（harvest_reactor 潜伏缺陷的合流修复，e6-evidence-note-ref-fix-spec.md 明示留待后轮的同一族缺陷 + pr_squash_merge 本地假合并）。
- 类别：缺陷修复，非判据变更。goal.md 不改。
- 依赖：main@6ba94d3（#199 已含 e6_stop.py 同族修复，本单复用其 evidence 规范路径与 board_card 读口模式）。

## 根因（已实读，非推断）

### 缺陷 (a)：pr_squash_merge 本地假合并，不 forge/push PR、不返回 MERGED PR 链接
`src/fleet_graph/supervise/harvest_ops.py::DefaultHarvestOps.pr_squash_merge`（L160-175）只做本地
`git merge --squash <head_commit>` + `git commit`，把「PR merged」机械地降级成「本地 squash merge 落进
默认分支」，且 commit 直接落在目标 repo 的生产 checkout 默认分支上。编排层
`supervise/harvest.py::pr_merge`（L386-409）只读 `result.get("merged")`，receipt 从不落 PR 链接。
真机实证（fleet-harvest-sandbox 历史 commit `bb026e3 harvest: squash-merge bce312a81bd6`）即本地假合并：
它从未经过 GitHub forge，产出不了 MERGED PR 链接。判据②（真实 MERGED PR 链接）因此永远无法达成。

真实收割必须走远端 forge：推 `harvest/` 前缀分支 → 建 PR → squash merge，返回 merged PR 的 html URL。

### 缺陷 (b)：evidence 把 development_id 当 refs.target_entity
`supervise/harvest.py::evidence`（L480-510）绕过 `Board.note`，裸调 `deps.bus.publish` 把
`payload.card_entity_id` 与 `refs[].target_entity` 都填成 raw `development_id`。agent-bus 的 refs 解析要求
`target_entity` 是**已注册板实体 id**（`msg_01M…`），development_id 不是实体 → 422 DERIVATION_ERROR。
这是 e6-evidence-note-ref-fix-spec.md 明示的同一族潜伏缺陷（E6 已在 #172 修复，harvest 因 deny-all /
从未带权走证据步而从未暴露）。

harvest 的正确 ref 目标 = 该 development 的 goal-line board card 实体 id，持久化于 dd admission record
`<dd_root>/<development_id>/record.json` 的 `card_entity_id` 字段（`control_plane._publish_card` →
`record["card_entity_id"]`，`harvest.py::_resolve_repo` 已读同文件，可复用读取模式）。缺卡（字段 null/缺失）
时必须 best-effort skip，绝不把 development_id 当 ref 伪造。

## 交付 A：HarvestOps 增读口 + pr_squash_merge 契约升级（supervise/harvest_ops.py）

1. `HarvestOps` 协议与 `DefaultHarvestOps` 各加方法
   `board_card_entity_id(self, development_id: str, dd_root: Path) -> str | None`：
   读 `<dd_root>/<development_id>/record.json` 的 `card_entity_id`（空/null/缺失/坏档 → None），
   复用 `harvest.py::_resolve_repo` 同文件的读取模式。不改 gate/allowlist 语义。

2. `pr_squash_merge(self, repo, development_id, head_commit, default_branch) -> dict[str, Any]`
   签名增 `development_id`（分支命名/幂等需要），返回契约变为 `{"merged": bool, "pr_url": str, ...}`。
   实作（真实 forge，绝不本地伪装合并）：
   - 取 repo 的 origin url；推送 `head_commit` 到 `refs/heads/harvest/<development_id>`
     （前缀在 allowlist `refs/heads/harvest/` 内，`refs/heads/<default_branch>` 为合并落点）。
   - `gh pr create --repo <origin> --base <default_branch> --head harvest/<development_id> --title … --body …`
     （subprocess 调用，cwd=repo，argv 数组无 shell），capture stdout 解析 PR html url。
   - `gh pr merge <pr-number> --squash --delete-branch`。
   - 成功 → `{"merged": True, "pr_url": "<PR html url>", ...}`；任一步非零退出 / 缺 gh / 缺 origin →
     `{"merged": False, "detail": <stderr/stdout tail[:400]>}`，绝不降级回本地 merge --squash、
     绝不直接 commit 生产 checkout 默认分支。
   - git 子调用沿用 `fleet_graph.dd.git` 的守卫纪律（`core.fsmonitor=false` / `hooksPath=/dev/null` /
     `protocol.ext.allow=never`）；gh 不经 git 守卫浸泡，独立 argv 数组 subprocess。

## 交付 B：harvest.py 编排层接线（supervise/harvest.py）

1. `HarvestState` 增 `pr_url: str`；`pr_merge` 节点：
   - 传 `development_id` 给 `ops.pr_squash_merge`；
   - `merged = bool(result.get("merged"))`，`pr_url = str(result.get("pr_url") or "")`；
   - step 记录 `**result`（含 pr_url），state 回写 `pr_merged` 与 `pr_url`。
2. `postconditions`：PR 判据改为 `pr_merged` **且** `pr_url` 非空；缺失分写
   `"PR merged 未达成"` 与 `"PR merged 链接缺失（无真实 forge PR 链接）"`。
3. `receipt` 落 `pr_url`。
4. `evidence`：
   1. `card_entity_id = deps.ops.board_card_entity_id(state.get("development_id") or "", deps.dd_root)`。
   2. 空/None → `evidence_note` 步 `ok=False`、detail=`"card_entity_id 缺失——note 未挂卡（best-effort）"`，
      **不**发布任何 note、**绝不**发射 `refs=[{target_entity: development_id}]`、不设 `evidence_note_id`。
   3. 非空 → 走规范路径 `Board(deps.bus).evidence(card_entity_id=card_entity_id, text=note,
      idempotency_key=f"harvest:{event.key}")`，`evidence_note_id = published.message_id`。
   - 移除对 `NOTE_KIND`/`WORK_NOTES` 的直接 publish 导入与用法，改从 `fleet_graph.bus.board` import `Board`
     （同 e6_stop.py 已修路径）。启发式不变：evidence 仍是 best-effort，发布失败只落 `ok=False` 不咬反应器终态。

## 交付 C：测试（合成 fake ops/bus，禁触真网/真 gh/真 git push/真 bus）

1. pr_squash_merge 真 forge 成功：fake ops 返回
   `{"merged": True, "pr_url": "https://github.com/Dandi007/fleet-harvest-sandbox/pull/1"}` →
   断言 receipt `pr_url` 非空、`postconditions` 达 `harvested`。
2. fake ops 返回 `{"merged": True, "pr_url": ""}` → 断言 `escalated`（PR merged 链接缺失）。
3. 旧 negative `merged=False` → `escalated` 零回归。
4. evidence 用真实板卡：fake ops `board_card_entity_id -> "card-xyz"`（合法实体）→ 断言发布 note 的
   `payload.card_entity_id == "card-xyz"`、`refs == [{"target_entity": "card-xyz"}]`、
   `"dev-x"`（development_id）不作为任何 target_entity 出现。
5. fake ops `board_card_entity_id -> None`（无卡）→ 断言零发布、`evidence_note` 步 `ok=False` 且 detail 含「缺失」/「未挂卡」。
6. `make verify` 全绿；M1–M4 / supervisor conformance 既有测试零回归。

## 可复现验收

```dd-acceptance
make verify
```

## 铁律

- 只改 `supervise/harvest.py`、`supervise/harvest_ops.py` + 对应测试；不碰判据（goal.md）、
  harvest allowlist 语义与配置文件、`authorize_harvest_write` 门禁语义、E1–E7 词表、生产主 checkout（仅 ff-only pull）。
- pr_squash_merge 绝不直接 commit 目标 repo 生产 checkout 默认分支；真实合并只经 GitHub PR；
  分支推送只在 `refs/heads/harvest/` 前缀与 allowlist 圈定的默认分支落点内。
- evidence 绝不把 development_id 当 refs.target_entity；缺板卡即 best-effort skip。