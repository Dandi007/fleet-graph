# E5 harvestable 语义修复——/v1/harvestable 反转补丁 spec

- 目标仓：`/data/code/self/fleet-graph`（本 development 在独立 worktree）。
- 归属：M2 修复。P1 生产缺陷：E5 语义反转。类别：缺陷修复，非判据变更。

## 根因

`src/fleet_graph/state/fleet_state.py::FleetStateView.harvestable()` 用 `if not head_commit or terminal == "complete": continue` 作「未收割」代理——即把 `terminal != "complete"` 的 development 全部列为 harvestable。结果 refused（GATE_REJECTED）/fault/在途单被误列入，真正 terminal=="complete" 待收割单被排除。E5 现状=「只会误报、永不真报」。

## 目标语义（supervisor 给定，照此实现）

E5 approved_unharvested <=> `terminal == "complete"` 且 产品 commit 未落默认分支；refused / fault / 任何非 complete 终态 永不入列。

## 交付

1. 修正 harvestable() 谓词：仅当 terminal == "complete" 且「产品 commit 未落默认分支」时才入列；refused/fault/空 terminal/在途一律排除。
2. 「产品 commit 未落默认分支」= 该 development 的产品 commit 是否已在默认分支落地，按 content 等价（squash 改写 SHA 也算已落地），不得用字面 SHA 祖先判断。实现为可注入的 landed_in_default_branch(commit) -> bool：
   - 生产默认：只读 git（FleetStateConfig 增 repo 路径 + default branch；只读检查，绝不写）。
   - 读取/查询失败降级（未收割 或 整条 absent），绝不 5xx、绝不崩溃。
3. 绝不因此获得写权限（只读 pull；不调可写 git/MCP）。

## 测试（合成 dd 工件正反例，tests/test_fleet_state_readmodel.py TestHarvestableView）

1. refused 排除：terminal="refused" -> 不入列。
2. fault 排除：terminal="fault" -> 不入列。
3. complete 未收割入列：terminal="complete" 且 landed_in_default_branch False -> 入列。
4. complete 已收割排除：terminal="complete" 且 landed_in_default_branch True -> 不入列。
5. make verify 通过；M2 既有 E5/E6/E7 负例零回归。

## 可复现验收

```dd-acceptance
make verify
```

## 落地后真机复检（监督面/worker，不并入 acceptance）

- 修复合入 + 重新部署 read-model 后，GET /v1/harvestable 不得包含 dev-fg-677b33a80fc3（terminal=refused）。

## 铁律

- 只读：不写被观察工件/git，不获写权限。一切改动走 PR，不直改 main；主 checkout 仅 ff-only pull。判据只有用户能改。