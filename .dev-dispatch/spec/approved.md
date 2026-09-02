# 空收割修复四判据 + 三头对账回执（goal.md 2026-09-03 01:0x「收割事故」复签条件）

来源：监督面直写 goal.md 2026-09-03 01:0x「🔴🔴 收割事故」的四条复签条件；与
2026-09-03 00:4x「监督面交界裁决」一致——**收割反应器接线位置已归 wf-8d9737，本单
只做这四条收割判据 + 回执契约**，不触反应器接线位置、不改 allowlist 四判据、不改
E5/E6/E7 事件词表。

## 事故机械事实（实现据此，取证锚点，不重证）

反应器收 `dev-fg-e0e8eb8c7770`：线放行 commit `a5e07c72`，反应器自造「exclude dd
protocol subtrees」提交 `0299f518`（父系 1c327f4→9182691）并收了那个提交——
`git merge-base --is-ancestor a5e07c72 0299f518` ❌ 非祖先，相对 base 的产品 diff
= 0 文件 → 收 0 文件 PR #153/#155 且宣布收割 + 自动部署 → 生产 fleet-exporter 跑在
无产物 release、`pressure:` 段被覆盖消失。

方法论坑：排除协议目录方向对，**错在做成一个提交再收那个提交**。正确形态是
**收割时从 diff 里排除**：`git diff base..head -- . ':(exclude).dev-dispatch'
':(exclude).dd-evidence'`，产物本身不动。

## 要什么（四条，每条阳性夹具 + 变异枪两方向都要能红）

1. **收割绑定被放行的 exact head**：`approved_head` = E5 事件 `head_commit`（线被
   gate 放行的 exact commit）；收割前判定「放行 commit 是否为待收 head 的祖先」
   （`git merge-base --is-ancestor <approved_head> <待收 head>`），**不是**「分支
   head 是什么就收什么」。非祖先 → escalate，不收。
2. **净产品 diff 为空必须 escalate**：`net_product_files` = `git diff <base>..<待收>`
   排除 `.dev-dispatch` / `.dd-evidence` 后的文件清单；为空 → outcome=escalated，
   **不得记「harvested」**。
3. **回执三头对账**：回执必须记 `approved_head` / `harvested_head` / `net_product_files`；
   `harvested_head` 必须是 `approved_head` 的后代、`net_product_files` 必须等于
   `git diff base..harvested_head` 排除协议目录后的文件清单；三者对不上 → 红（escalate /
   用例红）。
4. **净 diff 为空不得触发部署**：净 diff 为空时 deploy 步绝不执行（writes_skipped 覆盖），
   空收割不得串上自动部署。

## 判据（两方向可红）

- ① 阳性：approved_head 是待收 head 祖先 → 过护栏、正常收割；变异：改成「分支 head
  是什么就收什么」（不验祖先）→ 必有一条用例红。
- ② 阳性：净 diff 非空 → 正常 harvested 且 net_product_files 非空；变异：净 diff 为空
  仍走「已收割」→ 必须红（改判 escalated）。
- ③ 阳性：三头自洽 → 回执绿；变异：net_product_files 或 harvested_head 写错/漏写去对账
  → 红。
- ④ 阳性：净 diff 非空时才部署；变异：净 diff 为空仍触发 deploy → 必须红（写步被跳过、
  无部署发生）。

## 交付约束

- 只改 `src/fleet_graph/supervise/harvest.py`（intake / merge / receipt 与净 diff 判定）、
  `src/fleet_graph/supervise/harvest_ops.py`（exact-head 祖先判定 + 净 diff 排除计算）与
  `tests/test_harvest.py` 及必要 fixture；
- 排除协议子树只能在 diff 计算里做（`:(exclude)` pathspec），**不得再落「exclude 提交」**；
- 不触反应器接线位置（归 wf-8d9737）；不改 allowlist 四判据语义；不判签/不改
  `harvest-allowlist.json`；不部署不重启；写动作仍全部被 allowlist gate 包住（Guard D 不变）。

```dd-acceptance
uv sync --frozen
make verify
```