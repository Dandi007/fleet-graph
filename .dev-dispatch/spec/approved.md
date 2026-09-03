# Spec M5（wf-8d9737）· release/<line-id> 分支模型（D6）

> 状态：**落卷待批；派单序钉在 M3 合入之后**（base 取派单时 main 头，吸收 M1/M2/M3；M4 可并行或先行，无硬依赖但同仓注意合并序）。判据锚：goal.md §二 M5（含「本线自己的 DD 从 M5 上线后一律走 release/wf-8d9737」）、design.md D6、§6.4 分支模型表、§8 行「DD 只碰线分支」「派单前 rebase」。与 design.md/golden-order 冲突以后两者为准。

## 要交付的行为（全部在 fleet-graph 仓）

1. **三层分支模型落地**：
   - 目标分支（main）：只在 goal 级验收放行时由上线器合入，一条线一次；DD 不碰。
   - 线分支 `release/<line-id>`：一条线触及几个仓就有几条；本线每张 DD 经 gate 后 merge 进来。
   - 单分支 `dd/<dev-id>` worktree：implementer 单内用（现状保留）。
2. **派单前 rebase 是 configure 段固定第一步**：把 `release/<line-id>` rebase 到目标分支当前头；无冲突直接继续；有冲突 → implement 段第一件事解冲突并跑全量套件，解不了走 rework 有界重试后 `failed(REBASE_CONFLICT)` 升报线。
3. **target_base_commit 冻结为 rebase 之后的 `release/<line-id>` 头**（不是 main 头）；record.json 记 `line_ref` 与 rebase 记录。
4. **merger 推 `release/<line-id>`**（不再推完 dd/<id> 即了事：merge 段产物进线分支；harvest allowlist 语义改为「圈 `release/<line-id>` 可写仓」——S7 分工，收割反应器已由 M3 改接 merge 段之后，本单补 allowlist 新语义）。
5. **`state_line.release_behind` 进状态面**：每条 release 分支落后目标分支的提交数；超阈值告警（判定口与指标归本线，告警规则本体归 wf-6475fd——只暴露指标，规则不在本单）。
6. **本线切换**：M5 上线后本线自己的 DD 一律合 `release/wf-8d9737`（goal §五）；goal 级验收一次放行 release → main。

## 判据（正/负双向，goal.md §二 M5 与 design.md §8 两行原文内联）

- 阳性「DD 只碰线分支」：对某线的任一 DD 读 `record.json.target_base_commit` 与 `remote_ref` → base == 派单时 `release/<line-id>` 头；merger 推的是 `release/<line-id>`；`main` 无该单直接提交（git log origin/main 可查）。
- 阳性「派单前 rebase」：在目标分支人为前进一个提交后派单，读 configure 段日志 → 出现 rebase 记录；`state_line.release_behind` 回到 0。
- 阴性（越分支）：构造一张试图以 main 头为 base 或推 main 的单 → 拒绝码（结构化），单不建立/推送被拒。
- 阴性（rebase 缺失）：删掉 configure 首步 rebase 的发射 → 「人为前进目标分支后派单，record.target_base_commit 不含新提交」的用例必须红（不得靠事后补偿兜住）。
- 阴性（落后告警）：release 落后超阈值 → `release_behind` 指标超阈可查（判定口必须有，规则消费归 wf-6475fd）。

## 测试与验收

- 新增 `tests/test_m5_release_branch_model.py`：上述正/负用例逐条（三层模型、configure 首步 rebase、base 冻结、merger 推线分支、main 无直接提交、越分支拒绝、rebase 缺失红、release_behind 指标）。**零测试删除**；既有 dd 流水线断言更新到新真值不算删除。
- 历史反例素材（写进测试注释）：design §6.4「落后 160 提交搁浅 54 个的死分支」。

## 边界

- 只动 fleet-graph 仓（dd configure/merge 段、record schema、harvest allowlist、状态面指标）；不做 goal 级上线器本体（release→main 放行流程维持现状收口）；不做 M6 状态 MCP 面（release_behind 先进现有 :7494 读模型）；agent-runtime 仓的 release 分支仅在涉及该仓的单里生效，不在本单预铺。

```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_m5_release_branch_model.py'
bash -lc 'make verify'
```
