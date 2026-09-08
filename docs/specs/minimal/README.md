# fleet-graph 最小系统 —— 设计正本（release 分支镜像）

本目录最初是 work folder **wf-cce72d** 的 **2026-09-07 快照**（逐字节复制，未改写）。2026-09-08 起，design.md / protocol.md / context.md 已在本仓 release 分支上按 GO-26~36 回写并继续演进（dd-24），不再与 WF 侧逐字节一致；golden-order.md 仍是用户原话的只增不改记录。它们是「fleet-graph 最小系统」这条线的设计 SSoT。

| 文件 | 内容 |
|---|---|
| `golden-order.md` | 用户（青林）原话，逐段按时间序记录，共 36 段；派生结论不进本文件 |
| `design.md` | 设计正本 v1（GO-16 定稿，增补至 GO-36）：组成、线 / DD 循环、分支模型、可观测性、决策记录、落地路径 |
| `protocol.md` | 输入输出协议 v1（agent 起草，GO-12 授权；GO-26~36 已回写）：公共信封、enroll 请求、各 agent 的 `*.in/1` / 输出 schema、event 日志、MCP 操作接口、恢复、书记员 |
| `context.md` | 阶段说明：GO-26~36 已实现（含落地模块）、待用户拍板项、仍待过目的旧〔推荐〕 |

## 优先级与回写状态

design.md 与 protocol.md 已按 golden-order 第 **26–36** 段回写（2026-09-08，dd-24）：enroll 走 `goal.enroll/2` 六字段（多 repo、必有 remote）、运行时状态归引擎根（WF 只放人读正本）、PR / worktree 以 DD 为粒度、每次交接核「已 push、HEAD == remote tip、工作树干净」、dispatch 输出即 DD 启动协议（Goal Agent 自建分支 / worktree / spec，引擎机械核五条，可跨多 repo）、approve 后先看平台 mergeable。**正文与 golden-order 一致**；如仍有出入，以 golden-order（用户原话，最高优先）为准。仍待用户拍板的三项（加 repo 走 dispatch 还是只经 goal_steer、多 repo spec 放一处还是每 repo 一份、`models` 留不留）见 `context.md`。

## 来源与更新

- golden-order.md 的正本仍在 work folder wf-cce72d（经 katana-work-folder-mcp 访问）；design.md / protocol.md / context.md 回写后的演进正本在本仓 release 分支上，WF 侧保留 2026-09-07 快照。后续 golden-order 新增段落时照旧先落 golden-order，再回写正文。
- 快照日期：2026-09-07；正文回写：2026-09-08（dd-24）。
