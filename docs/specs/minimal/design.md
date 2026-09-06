# fleet-graph 最小系统 —— 设计正本（草稿，随 golden-order 增量更新）

> 状态：**v1 定稿（2026-09-06 10:5x，GO-16）**，无待定项。后续改动走新的 golden-order 段落并在 §7 追加记录。每条标注来源：〔GO-n〕= golden-order.md 第 n 段用户原话；〔已确认 GO-n〕= agent 起草、用户已 LGTM；〔推荐〕= agent 起草、用户尚未过目；〔待定〕= 尚未拍板。
> 纪律：agent 的建议不作为决策 base〔GO-2〕。本文件只合成用户已定的内容，派生推论单独标出。
> 更新：2026-09-06 12:4x（GO-17~25 增补）

## 1. 组成：一个引擎、一个校验节点、五个流程 agent、一个书记员

**引擎**是常驻进程〔GO-7〕，**一个 goal 一个进程**〔GO-9〕。全是程序，不含模型调用：
- 收 MCP 来的 goal enroll〔GO-7〕，跑校验节点〔GO-3.1〕
- 按 Goal Agent 的 Stop 输出调度下一个要跑的东西〔GO-7〕
- 跑程序化验收命令（每个 goal 必带，类似 make test）〔GO-6.1〕
- git：只建分支（release 分支、单的分支）。**引擎自己不做任何 merge**，所有合并都是「源分支 → 目标分支」交 Merge Agent〔GO-15〕
- 数 turn 数与 DD 轮数，越 warning 线只告警不停〔GO-6.4〕
- 写 event 日志，可观测性最高优先〔GO-7.3〕
- **填写并核对每轮输入里的 git 上下文**（worktree 在哪、分支、当前 commit id），对不上就打回，不靠 agent 自述〔GO-25〕

**校验节点**：程序化，确认输入请求符合协议〔GO-3.1〕。

**机械化原则**〔GO-25〕：能机械化确定的都机械化——脚本化，或由外层框架（LangGraph 图）调度程序节点处理；只有需要自由裁量的才给 agent。分支规则（worktree 开在哪、分支叫什么、从哪个 commit 开）全部由引擎程序化决定，agent 只在给定 worktree 里干活；每轮输入必带 worktree 路径与 commit id，由引擎填、由引擎核，有问题就打回，这与协议化输出的约束是同一件事。系统的框架图、交互图、流程图必须清晰确定：每个节点是程序还是 agent、每条边由什么输出触发，都能画出来。节点清单如下，字段级核对规则见 protocol.md §0.10。

| 节点 | 类型 | 进入条件（上一节点的输出） | 产出 |
|---|---|---|---|
| 校验节点 | 程序 | MCP 收到 enroll | 通过 → spawn 引擎；不通过 → 拒绝并给字段级错误 |
| 建分支 / 开 worktree | 程序 | enroll 通过；Goal Agent `dispatch` | release 分支；单的分支 + worktree，从 release head 开 |
| Goal Agent turn | agent | 线开始；上一张 DD 结束 | dispatch / done / blocked |
| Impl | agent | dispatch；任何一步不过 | committed / failed |
| 验收命令 | 程序 | Impl committed | pass → CR；fail → Impl |
| CR | agent | 验收 pass；Merge rebased | pass / fail |
| FR | agent | CR pass | pass / fail |
| Goal Agent 审单 | agent | FR pass | approve / reject |
| Merge Agent | agent | approve；线 done | merged / rebased / failed |
| git 上下文核对 | 程序 | 每个 agent 调用前后 | 对得上放行；对不上判无效输出、打回 |
| 计数与 warning 线 | 程序 | 每次 turn / 每轮 DD | event |
| 书记员 | agent（只读） | goal 级边界 | L1 observation |

**MCP（控制面，唯一常驻服务）**〔GO-7, GO-10, GO-11〕：对外提供该有的接口，至少包括 goal enroll（校验后 spawn 该 goal 的引擎进程）、几个 goal 在跑、每个 goal 的状态、对 goal 的操作接口。状态来源是各引擎的 event 日志（§6）。操作接口具体有哪几个见 §7 待定。

**五个 agent**每次调用都是一个 run：起、跑一个 turn（内部多次调 LLM）直到 Stop、退出；但 run 之间的 session 默认**续用**（resume + compact），不是每次 fresh〔GO-23〕。输入有限协议，输出 schema〔GO-3, GO-4.2〕。所有 agent 相关的东西都封装在 agent-run 里，引擎只调它；agent-run 必须支持 harness 可配置（MCP 可见性与读写、hook 开关）与 session 保存位置可指定〔GO-22〕。

| agent | 输入 | Stop 输出 | 来源 |
|---|---|---|---|
| Goal Agent | goal + 上一个 DD 的结果 | 派 DD / done / blocked | GO-4.4 |
| Goal Agent（审单） | FR 通过的单 | approve / reject（带消息） | GO-5 |
| Impl | 单的 spec + reject 消息或 review 意见 | commit | GO-3.3 |
| CR（Continuous Reviewer） | commit | 过 / 不过带意见 | GO-3.4 |
| FR（Final Reviewer） | commit；可部署、可测试，做最终验收 | 过 / 不过带意见 | GO-3.5, GO-6.1 |
| Merge Agent | 源分支 + 目标分支（单→release，或 release→main） | merged / rebased（动了代码，回 CR→FR→Goal 再审） / failed | GO-5, GO-15 |
| **书记员（Scribe）** | 自上次观测以来的 L0：新 event、agent session 文件、Stop 输出 | 一批结构化 observation（L1），每条带 L0 证据指针；**只读，不改任何状态、不参与流程** | GO-21 |

Goal Agent 只有一个，第一性原理是判断工作是否完成〔GO-4.1〕；有写码权限但 prompt 约束只走 DD〔GO-3.2〕。

**agent 间通信原则**〔GO-17〕：本次交接的内容（「你的工作有问题，问题是……」）写清楚、直接注入 prompt；历史信息不主动注入，只告诉 agent「所有消息都在 XX，需要自己去读」。协议里每个输入因此是「本次 + history 句柄」两层，见 protocol.md §0.7。「整个 goal 线程内所有 a2a 通信互相全可见」记为备选，暂不做〔GO-18〕。

**WF 是 goal-enroll 的一等公民**〔GO-19〕：每个 goal 绑定一个 work folder（enroll 时传 id 或由 MCP 新建）；goal / spec / progress / findings 的正本在 WF，运行产物（events.jsonl、dd/）落在 WF 内的 `runs/<goal_id>/`，history 句柄带 WF id。字段见 protocol.md §1〔推荐〕。

## 2. 线的循环
1. 请求经 MCP goal enroll 进入，过校验节点。
2. Goal Agent 跑一个 turn，Stop 输出三选一：派 DD / done / blocked〔GO-4.4〕。
3. 派 DD → 引擎执行 DD（§3），结束后结果进下一个 turn，回 2。
4. done → 引擎调 Merge Agent 做 release → 目标分支（如 main）的合并〔GO-6.3, GO-15〕；rebase 动了代码则对 rebased 的 release 走一遍 CR → FR → Goal 审，过了再合〔GO-15〕。blocked → 写 event。

## 3. DD 的循环
1. Impl 产出 commit。
2. 引擎跑程序化验收命令，必须 pass 才进 CR〔GO-6.1〕；不过回 1。
3. CR；不过回 1。
4. FR，做真正的验收（可部署、测试）〔GO-6.1〕；不过回 1。
5. Goal Agent 审单：approve / reject 带消息〔GO-5〕。reject 回 1，DD 继续。
6. Merge Agent 做「单的分支 → 本线 release 分支」，冲突自行 rebase，Stop 时必须已处理完〔GO-5, GO-15〕。
   - rebase 改了代码 → 回 3，**完整再走 CR → FR → Goal Agent 审单**，Goal 再 approve 一次才合〔GO-15，覆盖 GO-14 的「直接推」〕。多一次 review 不亏。
   - 任何一步不过 → 回 1，且 **impl 每跑一次 approve 清零**〔已确认 GO-14〕。

## 4. 分支模型〔GO-6.3, GO-15〕
单的分支 → 本线 release 分支（开发期间所有单都合到这里） → 线 done 后 release 合回一开始的目标分支（如 main）。**两层合并是同一件事**：源分支 → 目标分支，交 Merge Agent；有问题打回，rebase 动了代码就回给 CR → FR → Goal 再判一次。

## 5. 循环上限〔GO-6.4〕
不设硬上限。引擎按 DD 轮数与线 turn 数各设一条 warning 线，越线只告警。

## 6. 可观测性：两层〔GO-7.3, GO-21〕
- **L0，原始证据**，三种，都是程序写的、逐字可查：① 引擎的 event 日志（每次状态变化一条，done / blocked 也是 event，不另设上报通道）；② 每个 agent run 的 session 文件（agent-runtime 落的，用了什么工具、说了什么，最细最真）；③ 每个 agent 的 Stop 输出（它的最终汇报）。
- **L1，结构化观测**，由**书记员**产出：持续读 L0，把细节抽象成 high level 的 observation，每条指回 L0 证据。书记员只做总结，不裁决、不改流程；外部看问题先读 L1，需要时下钻 L0。类比 claude-mem 的 observation。
- 用途：分层发现系统问题。L0 太细读不过来，L1 是人和其他会话的入口。

## 7. 决策记录（原「待定」，逐项关闭）
1. ~~引擎崩溃/重启从哪恢复~~ → **events.jsonl 是唯一状态来源，恢复 = 回放**，不另存 checkpoint；在跑的 agent run 视为丢失并重起该步骤；git 状态不回放只核对。详见 protocol.md §11〔已确认 GO-14〕。
2. ~~进程粒度~~ → 已定：**一个 goal 一个引擎进程**〔GO-9〕；**enroll 的 MCP 服务自己 spawn 这个进程**〔GO-10〕。常驻进程因此只有两种：enroll MCP（一个）与每 goal 的引擎（N 个）。
3. ~~FR 执行环境~~ → 已定：FR 就是 agent runtime cli 起的一个类 Claude Code 的 agent，天然有执行环境〔GO-9〕。
4. ~~release 合回 main~~ → 已定：线 done 后自动合回目标分支，不留人手步骤〔GO-10〕；执行者是 **Merge Agent 而非引擎**〔GO-15〕。
5. ~~各 agent 的输入协议与输出 schema~~ → 用户授权 agent 起草〔GO-12〕，见 **protocol.md** v0.1（全部〔推荐〕）。其中 agent 自定规则：一个 turn 只派一张 DD；review `fail` 必须带 blocker/major finding〔已确认 GO-14〕。**输出符合 schema 由 agent-runtime cli 保证，引擎不重跑**〔GO-13〕，runtime 需新增 `--output-schema` 能力。
6. ~~MCP 操作接口~~ → 用户授权 agent 定、用户审〔GO-13〕：8 个工具（enroll / list / status / events / message / steer / stop / resume），MCP→引擎只经每 goal 一个 `control.jsonl`，引擎在步骤边界读。详见 protocol.md §10〔推荐〕。agent 自定的一条请用户过目：**崩溃的 goal 不自动 resume**，只在 list 里标出。〔已确认 GO-16〕
7. ~~rebase 后直接推~~ → GO-14 曾确认「CR/FR 再过直接推」，**GO-15 改为回 CR → FR → Goal 完整再审**；同时所有 merge 统一交 Merge Agent，引擎不做 merge〔GO-15〕。
8. **通信两层**：本次交接注入、历史给路径自查〔GO-17〕。protocol.md 的 `dd_history` / `prior_reviews` / 多轮 `feedback` 整表注入随之改为一行摘要或本轮一条，加 `history` 句柄。
9. **a2a 全可见**：备选，暂不做〔GO-18〕。
10. **WF 一等公民**：enroll 必绑 WF，正本与运行产物都在 WF〔GO-19〕；字段级〔推荐〕待用户过目。
11. **goal-steer 与版本号**：MCP 提供 `goal_steer`，可改或新增 goal 字段；每次 steer 版本 +1；Goal Agent 下一个 turn 必看到版本号与 diff〔GO-20〕。原 `goal_update` 并入。
12. **书记员**：第六个 agent，只读观测，产 L1 observation〔GO-21〕。触发时机 / schema / 存放位置见 protocol.md §12〔推荐〕。
13. **agent-run 两项必备**〔GO-22〕：① harness 可配置（能读哪些 MCP、各自读写权限、挂哪些 hook；纯自动化场景不挂 claude-mem 之类写入型 hook）；② session 保存位置可指定，每条线的 session 集中存放。核对现状：agent-run 已有 `--harness` / `--mcp-allow` / `--session-root`，缺 profile 里的 `hooks` 字段与 `--output-schema`；见 protocol.md §9 需求清单与六份 profile 建议边界〔推荐〕。
14. **session 续用与 compact**〔GO-23〕：多数角色（Goal Agent、Impl、Reviewer）resume 同一 session 而非每次 fresh，配 compact 阈值；也可按场景选 fresh；按角色、按 goal 可配。作用域与默认表见 protocol.md §0.8〔推荐〕；agent-runtime 缺 compact 阈值参数。
15. **协议输入 = 每轮 user prompt**〔GO-24〕：system prompt 一个 session 给一次（框架、schema 约束、历史在哪）；每轮 user prompt 只注入最新的交接内容；历史自己读。见 protocol.md §0.9。
16. **机械化原则**〔GO-25〕：能机械化确定的都机械化，自由裁量才给 agent。落法：引擎程序化决定分支与 worktree；每轮输入必带 git 上下文（worktree、分支、commit id），由引擎填、调用前后核对，对不上打回；框架图 / 交互图 / 流程图必须确定（§1 节点清单）。「通过即 merge」目前仍全部交 Merge Agent〔GO-15〕；把无冲突 fast-forward 收回程序、Merge Agent 只处理冲突，作为〔推荐〕待过目，见 protocol.md §0.10 末条。

## 8. 与现有 fleet-graph 的对照（信息，非决策）
- 有对应物：goal_enroll、dd 流水线三个模型阶段、executors、cost_obs、events。
- 因「引擎常驻、进程内等」这一决定而失去存在理由的现有部件：scheduler/wake/parked、decision MCP、decision-bridge、arbiter、看板投票、harvest、supervisor 图、goal_interrupt。
- 无对应物的新部件：Merge Agent（今天合并是程序，冲突即失败）。
- 现有代码盘点见主 session 22:0x 的模块清单（4.8 万行、14 个包、9 个 systemd 单元），必要时补录到 findings。

## 9. 落地路径〔GO-14, GO-16〕
- **旧引擎继续跑**，不停不冻结。
- **在 fleet-graph 仓开一条 release 分支，做大规模简化重构**到本文件的形态；不另起新仓。
- **新系统写好之后，与之重复的旧部件全部下线**（对照 §8 第二条清单）。
- **第一版由 goal-agent 来做**：作为舰队的一条线派单实施，不由人或主 session 手写。
- **LangGraph 保留，新引擎基于它的编排框架实现**〔GO-16〕。与 §7.1 并存的方式：图的 checkpointer 只是可删缓存，events.jsonl 仍是唯一真相；引擎重启按 §7.1 回放，不依赖 checkpointer 续跑。
- **暂不派发**〔GO-16〕：给 goal-agent 的 goal 可以起草进本 WF，但不 enroll。
- 〔下一步〕等用户指令。可做的准备：goal 草案（goal_text、验收命令、release 分支名、模型档位）。
