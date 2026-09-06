# fleet-graph 最小系统 —— 设计正本（release 分支镜像）

本目录的四个文件是 work folder **wf-cce72d** 的 **2026-09-07 快照**，逐字节复制（sha256 与 work folder 侧 `content_revision` 一致），未做任何改写。它们是「fleet-graph 最小系统」这条线的设计 SSoT。

| 文件 | 内容 |
|---|---|
| `golden-order.md` | 用户（青林）原话，逐段按时间序记录，共 36 段；派生结论不进本文件 |
| `design.md` | 设计正本 v1（GO-16 定稿，增补至 GO-25）：组成、线 / DD 循环、分支模型、可观测性、决策记录、落地路径 |
| `protocol.md` | 输入输出协议 v1（agent 起草，GO-12 授权）：公共信封、enroll 请求、各 agent 的 `*.in/1` / 输出 schema、event 日志、MCP 操作接口、恢复、书记员 |
| `context.md` | 快照时刻的阶段说明：GO-26~36 已定内容合成、待用户拍板项、仍待过目的旧〔推荐〕 |

## 优先级：golden-order 第 26–36 段高于 design.md / protocol.md

design.md 与 protocol.md 只落到 **GO-25**。golden-order 第 **26–36** 段是最新拍板，尚未回写进这两份文件；凡与它们冲突之处，**以 golden-order 最新段落为准**，实现时照此执行。主要冲突点：

1. **enroll 走 `goal.enroll/2`**（GO-26~33）：六字段 schema / work_folder / title / source_branch（goal 级 release 分支名，写一次）/ repos[]（每项 repo 本地路径、remote URL 必有、target_branch 每 repo 可不同、acceptance）。去掉 protocol §1 里的 goal_path / sessions / warn / 单 `repo` 字段；一个 goal 可涉及多个 repo，repo 必须带 remote。
2. **状态归引擎**（GO-34）：events.jsonl / control.jsonl / sessions / worktrees / goal.enroll.json 不放 WF，由引擎在自己的根目录或数据库维护；WF 只放人读的 goal / design / progress / findings。覆盖 design §1「运行产物落在 WF 内 `runs/<goal_id>/`」与 protocol §1 WF 段。
3. **PR / worktree 以 DD 为粒度**（GO-28、GO-29、GO-34、GO-35）：所有开发以 PR 为单位；PR URL 与 worktree 不属于 enroll，每张 DD 开与收；每次 agent 交接强制核「已 push、HEAD == remote tip、工作树干净」，agent 输入不带 commit sha，只带 branch + PR。覆盖 protocol §0.10 的 commit 核对写法。
4. **DD 启动协议 = Goal Agent 的 Stop Response**（GO-36）：`stop: dispatch` 的输出对象就是 DD 启动协议；Goal Agent 在 Stop 前自己建 dd 分支（DD 的 source）、开 worktree、把 spec 写成仓内 `docs/specs/N-XX.md` 并 push；DD 的 target 是 goal 的 release 分支。引擎改为机械核对（分支在 remote、worktree 在该分支且 HEAD == remote tip、spec 文件存在、工作树干净），核过后开 PR、跑基线验收、起 Impl。覆盖 design §1 节点表「建分支 / 开 worktree 由引擎做」与 protocol §2 dispatch 字段。
5. **可跨多 repo**（GO-36）：一张 DD 可跨多个 repo，协议里写每个 repo 的分支、worktree 路径、spec 相对路径。

其余 GO-26~36 的合成结论见 `context.md`「已定」段；仍待用户拍板的三项（加 repo 走 dispatch 还是只经 goal_steer、多 repo spec 放一处还是每 repo 一份、`models` 留不留）也在那里。

## 来源与更新

- 正本仍在 work folder wf-cce72d（经 katana-work-folder-mcp 访问）；本目录是其 release 分支上的镜像，后续 golden-order 新增段落时应同步重新快照。
- 快照日期：2026-09-07。
