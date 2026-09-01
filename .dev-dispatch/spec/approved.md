# H2 任一 step ok:false → postconditions 必红、outcome 不得为 harvested

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：监督面 10:1x 立案（goal.md 顶部 🔴块 H2「终局语义」）。不是判据变更，是缺陷修复。
- 类别：缺陷修复（终局语义缺口），不改 deny-all、不改 allowlist 配置文件、不改判据。

## 根因（已实读，非推断）

M3 e2e 第三轮回执 `reports/e5-dev-fg-a324ae06f67c.json` 里 `ff_only_pull ok:false`
（`Diverging branches can't be fast-forwarded`）与 `deploy ok:false`（exit 127）**两处
ok:false 都没有被终局计入**，`postconditions` 仍判 `ok:true missing:[]` → outcome=harvested。

代码 `src/fleet_graph/supervise/harvest.py::postconditions`（L554-571）只核四要素：
PR merged！pr_url 非空、verify_exit_code==0、evidence_note_id 非空；**不扫描 steps**。
于是任一中途 step 的 ok:false 都被静默吞掉——「收割链里非零退出必须停下来交人工」这条 SOP
只活在操作纪律里、没进代码。

## 交付 A：postconditions 计准入任一 step 的 ok:false

`src/fleet_graph/supervise/harvest.py::postconditions`：

1. 在既有四要素之外，扫描 `state["steps"]`：任一 step 的 `ok` 为假（`step.get("ok")
   is False`）→ 记入 `missing`（逐条列出 `step` 名 + `detail`/`exit_code` 等机械事实），
   postconditions 因此 `ok:false`，outcome=escalated。
2. 保持「不采信子图自述、只看机械事实」语义：steps 是各节点用 ops 机械返回值记录的
   事实，不是自述。
3. outcome 判定公式不变：`missing` 非空 → `OUTCOME_ESCALATED`；否则 `OUTCOME_HARVESTED`。

## 交付 B：阴性测试（必须，不可省略）

`tests/test_harvest.py`（fake ops 注入）：

1. **pull 失败 fixture**：`fake_ops(pull_ok=False)` 跑完整图 → 断言
   `outcome == OUTCOME_ESCALATED`（≠ harvested），且 receipt 里 postconditions step
   `missing` 含 `ff_only_pull`、该 step `ok` 为 False。
2. **deploy 失败 fixture**：`fake_ops(deploy_exit=1)` → `outcome != harvested`。
3. **verify_real 失败 fixture**：`fake_ops(verify_real_exit=1)` → `outcome != harvested`。
4. **正向回归**：全 ok → `outcome == OUTCOME_HARVESTED`（与既有 `test_full_sop_runs_all_steps_and_harvests` 一致，不回归）。
5. 断言 helper 可复用既有 `missing` 提取写法（`[item for s in receipt["steps"] for item in (s.get("missing") or [])]`）。

## 可复现验收

```dd-acceptance
make verify
```

## 铁律

- 只改 `src/fleet_graph/supervise/harvest.py::postconditions` + `tests/test_harvest.py`；
  不触 allowlist 配置文件、不改 deny-all、不改判据、不扩大 SOP 步骤集合（SOP_STEPS 枚举不动）。
- 一切改动走本 development worktree + PR，不直改 main；生产主 checkout 仅读。
- 修复落地后由监督面再造沙箱低风险单重跑 e2e；本单只交付修复。