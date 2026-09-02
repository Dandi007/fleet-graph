# 闸27 名册 line 生命周期 RETIRED 集合语义——闭卷审计退役线从 CONVERGED 迁出至独立 RETIRED 集合

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：`config/ronin-lines.json` + `tests/test_ronin_lines_config.py`（名册 line 生命周期状态机）。
- 类别：语义修正（rebase）。监督面 08:5x 直写 goal：去掉监督面 #225 加进 `CONVERGED` 的那三条线（wf-7cd0a7 / wf-c106b9 / wf-e7b0dd），采纳本线引入的 `RETIRED` 集合语义（closed-by-supervisor 是「历史使命结束」，退役是「DoD 逐条核验后不复活」，二者不可混）。

## 根因（已实读 main 151507a 源，非推断）

`tests/test_ronin_lines_config.py`：
- `CONVERGED = {wf-a8c7b5, wf-a08949, wf-40fa8d, wf-7bc4d1, wf-7cd0a7, wf-c106b9, wf-e7b0dd}`（L57-68）——监督面 #225 把「闭卷审计退役」三条塞进了 CONVERGED；
- 但同一文件 `ENROLLED` 集合仍含 wf-7cd0a7（L83）、wf-c106b9（L114）、wf-e7b0dd（L128）；
- `CLOSED_BY_SUPERVISOR = {wf-3f87f3}`（L137-139）是「历史使命结束/由监督面收线」的独立集合。

语义污染：CONVERGED 原义=「使命完成收敛」（enabled=false 留名册），与「闭卷审计退役、判定不复活」是两种生命终点；把退役线混进 CONVERGED 会污染 `test_exactly_the_current_batch_is_switched_on` 的 `(…)-CONVERGED` 语义与「迁移做完了没有」的可回答性。应引入独立 `RETIRED` 集合承载这三条。

## 交付 A：引入 `RETIRED` 集合并迁出三条线（`tests/test_ronin_lines_config.py`）

1. 新增 `RETIRED = {"wf-7cd0a7", "wf-c106b9", "wf-e7b0dd"}`，注释注明「2026-09-02 监督面闭卷审计退役（#225），DoD 逐条真机核验后不复活，enabled=false 留名册」。
2. 从 `CONVERGED` 删除这三条（CONVERGED 回归 {wf-a8c7b5, wf-a08949, wf-40fa8d, wf-7bc4d1}）。
3. `test_the_real_loader_accepts_it` 全集断言更新为 `MIGRATED | OPENED | ENROLLED | REVIVED`（RETIRED 是 history 集合、不要求 loader 逐条可载，语义同 CONVERGED/CLOSED_BY_SUPERVISOR 不进全集）。
4. `test_exactly_the_current_batch_is_switched_on` 期望更新为 `(BATCH_TWO | OPENED | ENROLLED | REVIVED) - CONVERGED - CLOSED_BY_SUPERVISOR`，并新增「RETIRED 三条 enabled=false」断言（退役线绝不点火）。

## 交付 B：`config/ronin-lines.json` 语义对齐（不删行，只加/调标记）

1. 三条退役线的 `_provenance`/`_retirement` 标记保留并补「RETIRED 集合」语义（名册是编成史实，退役靠 enabled=false 不靠删行）。
2. 不改变任何 `enabled` 现值（wf-7cd0a7/wf-c106b9/wf-e7b0dd 已 enabled=false）；不动其他 15 条 enabled=true 线。

## 交付 C：测试（`tests/test_ronin_lines_config.py`）

1. **阴性守卫（本单判据）**：任一条 RETIRED 线被塞回 CONVERGED → `test_the_real_loader_accepts_it`/`test_exactly_the_current_batch_is_switched_on` 必红（未修复时三条同时存在于两集合，语义污染可被断言捕获）。
2. 反向不抖动：CONVERGED 移除三条后全集/开关断言仍绿、`make verify` 全绿、`test_ronin_lines_config.py` 零回归。

## 可复现验收

```dd-acceptance
make verify
```

## 量化判据

1. `make verify` 通过。
2. RETIRED 三条同时不再属于 CONVERGED；`wf-7cd0a7`/`wf-c106b9`/`wf-e7b0dd` 仅存在于 ENROLLED 与 RETIRED（历史入编 + 退役史实），enabled=false 断言成立。
3. 破坏样本（任一退役线塞回 CONVERGED）→ 断言红。

## 铁律

- 只改 `config/ronin-lines.json` + `tests/test_ronin_lines_config.py`；不触 `src/`、不改判据、不改 enabled 现值、不删名册行。
- 一切改动走 PR（本 development worktree），生产主 checkout 仅 ff-only pull，禁 checkout/switch/reset/detach/建分支/验证。
- 不触 `harvest-allowlist.json`、不改名册 loader 语义（`SchedulerConfig.from_json` 不变）。