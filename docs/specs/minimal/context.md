## 阶段
GO-26 起进入「一件一件定义」模式：先 enroll，再 DD。golden-order 已到第 36 段，全部逐字落盘。design.md / protocol.md / viz 只落到 GO-25；GO-26~36 的内容目前只在 golden-order 与对话里，待三个拍板后一次写进 protocol §1（enroll）、§2（dispatch = DD 启动协议）、§4（DD 生命周期），并修 §0.10（commit 核对改为 push/remote 一致核对）、§1 WF 段（状态归引擎）。

## 已定（GO-26~36 合成）
enroll `goal.enroll/2` 六字段：schema、work_folder、title、source_branch（goal 级 release 分支名，写一次）、repos[]（每项 repo 本地路径、remote URL 必有、target_branch 每 repo 可不同、acceptance）。去掉 goal_path / sessions / warn / 单 repo / PR / worktree。
校验节点 13 条（见 GO-33 回复）；通过后引擎只做：建引擎根（不在 WF）、写 enroll 原样与首条 event、每 repo fetch 并在 release 分支缺失时从 target 切出 push、spawn。
状态归引擎：events/control/sessions/worktrees 在引擎根（如 /data/fleet/goals/<goal_id>/），WF 只放 goal/design/progress/findings，引擎经 MCP 写摘要。history 句柄指引擎根。
交接规则：每次 agent Stop 后引擎 fetch 核「已 push、HEAD == remote tip、工作树干净」，agent 输入不带 sha，只带 branch + PR。
DD：Goal Agent dispatch 输出即 DD 启动协议，可多 repo；Goal Agent Stop 前自己建 dd 分支、worktree、写 docs/specs/N-XX.md 并 push；引擎机械核五条（分支在 remote、worktree 在该分支、HEAD==remote tip、干净、spec 文件存在）→ 开 PR 到 release → 在 base 跑基线验收（红则 failed/baseline）→ 起 Impl。收尾：merged 即 PR merge/close，failed 则 close 不合并；删 worktree、删远端 dd 分支、写 event、结果回 Goal Agent。Goal approve 后先看平台 mergeable，能合就合，冲突才 Merge Agent。

## 待用户拍板
1. 加 repo 走 dispatch（Goal Agent 列新 repo 带 remote+target_branch，引擎按 enroll 规则核过即加、版本+1、goal.steered 来源 goal_agent）〔推荐〕，还是只允许人经 goal_steer。
2. 多 repo 一张 DD 的 spec 放一处（spec.repo 指明）还是每 repo 各一份。
3. `models` 留可选还是去掉。
拍完的动作：一次改 protocol §0.10/§1/§2/§4 + design §1/§7 追加 GO-26~36 决策记录 + 同步本地镜像 + build.py review 框 + 重建 viz（当前页只到 GO-25，已过时）。

## 仍待过目的旧〔推荐〕
§0.8 session 默认表、§9 harness 边界、§12 书记员；§0.10 末条 ff 合并归属已被 GO-36 回复里「approve 后先看 mergeable」覆盖，落盘时合并处理。暂不派发（GO-16）。