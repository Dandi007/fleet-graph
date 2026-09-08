## 阶段
GO-26~36 已回写进 design.md / protocol.md 正文（2026-09-08，dd-24），正文与 golden-order 一致；golden-order 仍是最高优先级，后续新段落照旧先落 golden-order、再回写正文。三项待用户拍板仍开放（见下），拍板后只改对应字段。

## 已实现（GO-26~36，release 分支上的落地模块）
- enroll `goal.enroll/2` 六字段（schema / work_folder / title / goal_text / source_branch / repos[]，repo 四键 path / remote / target_branch / acceptance）与字段级校验节点，REMOVED_FIELDS 显式拒绝 goal_path / sessions / warn / 单 repo：`src/fleet_graph/minimal/enroll.py`。
- enroll 后程序化准备（建引擎根、原样写 `goal.enroll.json` + 首条 event、每 repo fetch、release 分支缺失时从 target 切出 push 的纯数据计划）：`runroot.py`（`prepare_plan` / `git_argv_for`）。
- 状态归引擎根（`/data/fleet/goals/<goal_id>/`：events / control / sessions / worktrees / dd / goal.enroll.json；WF 只放人读的 goal / design / progress / findings）：`runroot.py`（`GoalRunRoot`）。
- 交接规则（每个 agent stage 调用前后核「已 push、HEAD == remote tip、工作树干净」，cr / fr 另要求 remote tip 不变；sha 不进核对）：`gitgate.py`（`check_handoff`）+ `stagerunner.py`（gate→prompt→agent→validate→gate）。
- dispatch 即 DD 启动协议（字段级校验、repos 转换、GO-36 五条机械核、核不过 `goal.dispatch_rejected` 打回）：`protocol.py`（`_check_dispatch`）、`dispatch.py`、`gitgate.py`（`check_dd_ready`）、`goalgraph.py`。
- 开 PR、基线验收、DD 循环（dd_ready → open_pr → baseline → impl → 验收 → CR → FR → Goal 审单 → merge → cleanup；基线红则 `dd.failed` stage=baseline，不起 Impl）：`ddgraph.py`。
- DD 生命周期收尾（merged → PR 已合并即收，failed → close 不合并；删 worktree、删远端 dd 分支）：`prlifecycle.py`（`open_pr` / `close_pr` / `remove_worktree` / `delete_remote_branch`）+ `ddgraph.py`（cleanup 节点）。
- approve 后先看平台 mergeable（MERGEABLE → 引擎平台合并；CONFLICTING / UNKNOWN → Merge Agent，UNKNOWN 不当 mergeable 猜；线尾 release → 各 repo 自己 target_branch 的合并计划）：`mergegate.py`（`decide` / `platform_merge` / `verify_merge_output` / `final_merge_plan`）。
- 支撑层：事件日志与回放 `events.py`、输入对象与 prompt 渲染 `prompts.py`、goal 版本 / steer 投射 `steer.py`。

## 待用户拍板（仍待人拍，未替拍）
1. 加 repo 走 dispatch（Goal Agent 列新 repo 带 remote+target_branch，引擎按 enroll 规则核过即加、版本+1、goal.steered 来源 goal_agent）〔推荐〕，还是只允许人经 goal_steer。
2. 多 repo 一张 DD 的 spec 放一处（spec.repo 指明）还是每 repo 各一份。
3. `models` 留可选还是去掉（当前 enroll 校验不收该键，见 protocol.md §1）。
拍板后的动作：只改 protocol §1 / §2 对应字段；另有 build.py review 框、重建 viz（当前页只到 GO-25，已过时）仍待做。

## 仍待过目的旧〔推荐〕
§0.8 session 默认表、§9 harness 边界、§12 书记员；§0.10 末条 ff 合并归属已按 GO-36 回复落盘（approve 后先看 mergeable，正文见 protocol.md §0.10）。暂不派发（GO-16）。
