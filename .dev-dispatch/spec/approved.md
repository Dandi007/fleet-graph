# E5 harvestable 语义修复（rework）—— 收割机械信号改「收割回执 / 首跑基线」 spec

- 目标仓：`/data/code/self/fleet-graph`（本 development 在独立 worktree）。
- 归属：M2 修复。🔴P1 生产缺陷。类别：缺陷修复，非判据变更；goal.md 不改。
- 上游：REFUSED `dev-fg-9bb86bfee5a2` 的 rework。本单携新信息（监督面真机抽检 147 条 + 结构性根因 + 方向 A/B），故为新 development，不复用已 refused id。

## 根因（真机证据，非推断）

上单把「未收割」判据实现为 `landed_in_default_branch()` 对 `status.head_commit` 做 tree/content 等价「未落默认分支」。对本舰队收割模式**结构性不成立**：
1. 协议目录被 strip：durable 分支 merger tip 的 tree 含 `.dev-dispatch/`、`.dd-evidence/` 协议目录，默认分支合并后已 strip 协议目录 —— tree 永不等价。
2. 监督面按内容收割会跨 base 重组 tree，产品 commit 的 tree 也测不出「已收割」。

真机复现：`FleetStateView(repo_path=/data/code/self/fleet-graph).harvestable()` 报 147 条，**全部历史 complete 在列**，含今日已收割部署的 `ece1ede28358` / `d62232e1193b` / `e25fcb288648`。refused 排除已正确（`677b33a80fc3` 出列）。

## 目标语义（supervisor REJECT rationale 给定，照此实现）

E5 `approved_unharvested` ⇔ `terminal == "complete"` **且** 卡上无「收割回执」；`refused` / `fault` / 任何非 complete 终态 / 在途单 **永不入列**。

- **收割回执（harvest receipt，方向 A，主信号）**：监督面收割部署后，在单卡固定挂 `note_type=evidence` 且 `idempotency_key` 以 `evidence-` 开头的回执（今日已收割三单皆有，board seq 936/943 可验）。E5=complete 且卡上无收割回执。
- **首跑基线豁免（方向 B，照抄 E7 首跑基线先例）**：首次采存量 complete 集合为基线、一次性出清 147 条历史；此后只报新增 complete，叠加方向 A 判后续收割。

## 交付（代码与评审全委 dev-dispatch）

1. 修正 `harvestable()` 谓词：`terminal == "complete"` 且 `has_harvest_receipt(card) == False` 才入列；`refused`/`fault`/在途一律排除。
2. `has_harvest_receipt(card) -> bool` 判定：卡上存在 `note_type=evidence` 且 `idempotency_key` 前缀为 `evidence-` 的回执。实现为**可注入**；任何读取失败降级为「未收割」或整条 absent，绝不 5xx 全链、绝不崩溃。
3. 首跑基线豁免：首次采存量 complete 集合为基线（照抄 E7 `e7_baseline` 水位先例），此后只审新增 complete。
4. 保留上单正确部分：`refused`/`fault`/在途排除；降级不 5xx；只读、不获写权限、不调可写 git/MCP。

## 交付 D：测试（合成 dd 工件正反例）

在 `tests/test_fleet_state_readmodel.py` 的 `TestHarvestableView` 内补正反例（注入假 `has_harvest_receipt` / 合成 status.json 与 card note 工件）：

1. **refused 排除**：`terminal="refused"` → 不入列。
2. **fault 排除**：`terminal="fault"` → 不入列。
3. **complete 已收割排除**：`terminal="complete"` 且卡上有收割回执（`note_type=evidence` + `idempotency_key` 前缀 `evidence-`）→ 不入列。
4. **complete 未收割入列**：`terminal="complete"` 且卡上无收割回执 → 入列（构造一张新 complete 无回执单必须入列）。
5. **首跑基线豁免**：合成 147 条历史 complete（无回执）→ 首跑全部出清不在列；此后新增一条 complete 无回执 → 该条入列。
6. `make verify`（lint+test+conformance）通过；M2 既有 E5/E6/E7 词表/负例零回归。

## 可复现验收

```dd-acceptance
make verify
```

## 落地后真机抽检（监督面/worker，不并入本 dd acceptance）

- 修复合入 + 重新部署 read-model 后，`GET /v1/harvestable` 真机断言：
  1. 147 条历史 complete 不在列（尤其已收割 `ece1ede28358`/`d62232e1193b`/`e25fcb288648`）。
  2. 构造一张新 `complete` 且无收割回执的单，必须在列。
  3. `dev-fg-677b33a80fc3`（refused）仍不在列。

## 铁律

- 只读：read-model 不写被观察工件、不写 git、不获写权限。
- 一切改动走 PR（本 development worktree），不直改 main；生产主 checkout 仅 ff-only pull。判据只有用户能改。