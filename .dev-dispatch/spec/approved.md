# coordinator 信封携带机械事实：resume verdict 与 prior_terminal（E4a 切片）

## 事实（2026-08-28 夜，三次真机复现）

goal 线的新 generation round-1 coordinator 信封（`<run_root>/coord/round-1-input.json`）
当前只含 `folder_id/round/last_turn_output(空)/bounds_remaining/inbox_messages/inbox_framing`
——**不含本代 runner 亲测的 wf_resume 结果，也不含上一代 terminal**。后果已三次真机
复现（wf-d002a6 21:39 与 22:0x 两例、wf-a87b04 21:57 一例）：模型从 progress.md 的
历史 BROKEN 叙事「补位」，把旧 verdict 复读成本轮取证，verdict=BLOCKED 直接烧掉
一个 generation，监督面被迫逐次人工证伪驳回。

runner 在 generation 起点确实执行了 wf_resume（progress changelog 有带时间戳的
「resume | 环境验证」记账，当晚多轮均 4✅1⚠️0❌ 全绿），但该机械事实没有进入信封。

## 要求

1. **信封新增机械字段 `resume_verification`**：line runner 在 generation 起点执行的
   wf_resume 结果（`overall`、逐行 verdict 的紧凑摘要、UTC 时间戳）注入**每一轮**
   coordinator 输入；字段由编排层填写，模型不可伪造来源。
2. **round-1 信封新增 `prior_terminal`**：上一 generation 的 terminal.json 内容
   （存在时），使新代 coordinator 无须从 progress 叙事重建前代状态。
3. **编排层守卫（N7）**：coordinator verdict=BLOCKED 且 reason 声称恢复验证
   BROKEN，而信封 `resume_verification.overall` 非 BROKEN 时，该轮按无效轮处置
   （机械拒收并以明确 code 记录，按既有无效轮/重试语义走，不得进入 park 升报）。
   守卫必须是机械比对，不解读散文之外的语义。
4. 回归测试：信封含新字段；守卫正例（BROKEN 自述 + 信封绿 → 拒收）与负例
   （信封确 BROKEN → 正常 park 升报）；既有 tests/test_goal_line.py 等不回退。

## Non-goals

- 不改 wf_resume 本身与 parking/wake 机制；不动 bus；不改 coordinator persona
  （agent-runtime 侧）——只改编排层信封装配与守卫。

## 边界

一切实现与 review 由 dev-dispatch actor 完成；只在本 worktree 及 dd 子 worktree
内操作；不触碰生产 main checkout 与生产服务。

```dd-acceptance
uv sync --frozen
make verify
```
