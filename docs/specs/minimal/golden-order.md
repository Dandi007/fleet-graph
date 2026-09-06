> 用户（青林）原话，逐条按时间序记录，不改写、不概括。派生结论一律放 design.md，不进本文件。
> 来源：2026-09-05 21:5x–22:1x 主 session 对话（承接 fleet-graph 现状汇报页之后）。

## 1. 问题定性与方法论（约 21:50）
> 现在的问题是 我已经完全失去了对fleet graph的细节的掌控了——我觉得这问题很大，我觉得我没办法再去做技术细节的决策了，现在的系统过于复杂，我觉得，应该从第一性原理，出发，less is more，先最小化系统，然后或许，一点一点加

## 2. 对 agent 建议的定位（约 21:55）
> 我觉得你的很多建议也不多，矫枉过正了，我们先不说这些，或者说起码不要把你的建议当作决策的base，我们先一起过一下，这个系统有哪些模块 和部分

## 3. 系统一定需要的东西（约 22:00）
> 现在存在的东西 都未必是系统需要的东西
> 我觉得 系统一定需要的
>
> 1 程序化校验节点——确认输入的请求符合协议
> 2 Goal Agent——一个ReAct Loop的Agent，给他指定的输入，他用agent runtime去跑一个loop，权限比较宽泛，虽然有写代码的权限，但是写代码需要用prompt约束他只走DD，这个Agent的输出，需要协议化，可以解析为sheme json，然后被外层引擎执行——比如 给DD派工作，验收DD，汇报done or waiting or blocked 等等
> 3 Impl Agent
> 4. Continus Reviewer Agent
> 5 Final Reviewer Agent
>
> 每个Agent的输入都是有限协议，输出都是需要sheme

## 4. Goal Agent 的形态（约 22:03）
> 1 我觉得其实 一个goal agent就可以了——它的第一性原理 就是判断工作是否完成了
> 2 我觉得就是一个react loop，也就是一个turn，它多次调用LLM API，直到给出Stop，Stop的输出需要协议化
> 3 我没太理解你在说啥
> 4 本质就是 goal agent 跑，然后到Stop，给出一个response，可以是——对dd的操作（本质就是waiting dd），汇报done，汇报blocked，我觉得就是三个状态，只不过第一个状态还需要操作DD

## 5. DD 的验收与合并（约 22:06）
> 我觉得是这样 DD的FR报通过了，就给Goal Agent了，它需要输出类似 "approve"，or "reject（需要给出消息）"——in this case，DD会继续工作。对于AP，可以让外部引擎执行merge，或者 even better，a merge agent，要求merge agent stop的时候，都处理好了，如果处理不好有问题，比如冲突，则需要它处理完rebase 解决，然后再给cr and fr，有问题则回到impl

## 6. 验收命令、rebase 后流向、分支模型、循环上限（约 22:09）
> 1 可以每个goal 都要求好一个程序化验收的 然后需要进入cr之前，类似于 make test，必须pass——但是这个只是基本的，最终的重要的验收，应该是FR去做，它可以部署 测试 之类的，然后goal agent也可以验收
> 2 你推荐一个吧
> 3 我觉得是这样 开发过程中 都是merge到release分支，然后都开发完了，release分支再merge回一开始它的目标分支——比如说 main
> 4 一开始先不需要上限，可以设置一个warning线，如果没有无意义转，就还好

（第 2 条用户授权 agent 推荐；agent 推荐为「rebase 后 CR/FR 再过即直接推，不回 Goal Agent；impl 每跑一次 approve 清零」，用户未明确否决，记为暂定，见 design.md）

## 7. 引擎生命周期、请求来源、可观测性（约 22:12）
> 引擎一直是活着的，只不过每个时刻跑的东西不一样，goal agent跑，然后Stop（也就是退出了），然后引擎根据他的协议，调度别的东西，但是引擎应该一直活着的
> 2 请求？就是MCP来的——goal enroll，是我理解错了么？
> 3 引擎可以有event 日志——我们有要求过可观测性，可观测性一定是最最最最最重要的，这样外部可以观测，然后发现问题，改进

## 8. 记录纪律（约 22:13）
> ——对了 我给你的输入 都要创建WF 记录golden order## 9. 对 design.md §7 待定项的回答（约 22:18）
> 1 我还没太想好 2 一个 goal 一个进程可以 3 FR 就是类似一个 Claudecode 他自然是有的 就是 agent runtime cli

（对应 §7 第 1 项恢复源：未定；第 2 项进程粒度：一个 goal 一个进程；第 3 项 FR 执行环境：FR 就是 agent runtime cli 起的一个类 Claude Code 的 agent，天然有执行环境）## 10. 起子与收尾合并（约 22:22）
> enroll 的 MCP 自己 spawn 就行，release 合回 main 引擎自动做## 11. MCP 的接口面（约 22:26）
> MCP提供该有的借口 类似 几个goal在跑 每个的状态 操作接口 等等## 12. 协议起草授权（约 22:30）
> 引擎的输入输出协议 其实不算我特别在意的事情 我完全可以交给你 你写一下吧

（授权范围：design.md §7.5 各 agent 输入协议与输出 schema、goal enroll 请求协议、event 日志格式。产物 protocol.md，标〔推荐〕，用户可改。）## 13. 对 protocol.md 自定规则的回复 + 两项授权（约 22:55）
> 2的话 其实我觉得需要放在agent runtime cli来保证这个基础能力——就是agent的协议输出 MCP操作接口你也可以定 我来看 恢复你来定吧先

（解读：protocol.md §0.2「无效输出重跑一次」不由引擎做，agent 输出符合 schema 是 agent-runtime cli 的基础能力；MCP 操作接口清单授权 agent 定、用户审；引擎恢复源授权 agent 先定。规则 1、3 未否决。）## 14. 对「还有哪些需要我决定」的回复（2026-09-06 00:0x）
> A 1 LGTM 2 LGTM 4 LGTM 其他的放VIZ页面 全部，我去读一下
> B 5 旧的继续吧，开release分支 大规模简化重构 6 新的写好了的话 重复的全都下线 7 goal-agent去做

（解读：A1 rebase 后直推 + impl 重跑清 approve、A2 一 turn 一 DD + review fail 必带 blocker/major、A4 event 回放恢复 → 已确认。A3 MCP 8 工具 + 崩溃不自动 resume → 用户读 viz 页后再审。B5 落地路径：旧引擎继续跑，在 fleet-graph 仓开 release 分支做大规模简化重构，不另起新仓。B6 新系统写好后，与之重复的旧部件全部下线。B7 第一版由 goal-agent（舰队线）来做。要求：design.md + protocol.md 全部内容放 viz 页面。）

## 15. 合并统一化（2026-09-06 10:3x，读 viz 页后）
> 引擎自动：release 合回目标分支（main） —— 我想了一下 本质上所有merge行为 都可以是 源分支->目标分支，然后交给Merger Agent，有问题就打回，或者，rebase之后，再给回对应的人，让他判断是否还OK——比如CR->FR->Goal的路径，如果没问题，那么理应pass，多一次agent review wont hurt

（解读：① 所有合并（单→release、release→目标分支）都是「源分支→目标分支」交 Merge Agent，引擎不再自己做 merge/ff，只建分支。② 覆盖 GO-14 已确认的 A1 前半「rebase 后 CR/FR 再过直接推」：rebase 动了代码后回到 CR → FR → Goal Agent 审单的完整路径，Goal 再 approve 一次才合。A1 后半「impl 重跑 approve 清零」不变。③ release→目标分支的合并若 rebase 动了代码，同样走 CR → FR → Goal 审 rebased 的 release，再合。）

## 16. 最后三项（2026-09-06 10:5x）
> 1 MCP接口 OK 2 langraph编排能力和框架还是很好的 肯定是基于它去做的 3先不派发

（解读：① protocol.md §10 的 8 个工具与「崩溃不自动 resume」确认。② LangGraph 保留，新引擎基于 LangGraph 编排实现；与 GO-14 已确认的「events.jsonl 是唯一状态来源」并存的方式：LangGraph checkpointer 只作可删缓存，不是真相。③ 给 goal-agent 的 goal 先不派发，起草可以但不 enroll。design.md 至此无待定项，定稿 v1。）

## 17. agent 间通信的核心原则（2026-09-06 11:0x）
> 一个核心原则就是 每个agent和下一个agent通信 交流 如果是类似于 你的工作有问题 需要把这次通信的内容写清楚 直接作为prompt注入，但是，历史信息也都可以让每个agent主动可以去查询 但是不主动注入 类似于"所有消息都在XX 你需要可以自己去读"

（解读：协议输入分两层。① 本次交接的内容（这一步为什么来找你：reject 消息、review 意见、验收失败输出、上一张 DD 的结论）写清楚、直接注入 prompt。② 历史（全部 event、之前各轮的 review、之前的 DD、MCP 消息）不注入，只给一个路径或句柄，告诉 agent「都在这里，需要自己读」。protocol.md 相应改：dd_history / prior_reviews 等整表注入改为「本次 + 历史路径」。）

## 18. a2a 全可见（2026-09-06 11:0x，紧接第 17 段）
> 甚至 整个goal thread里，所有a2a的通信 大家都可以互相看到——或许也没能问题，so far可以先不做成这一种状态

（解读：记为备选，不采纳。维持第 17 段的两层：本次注入、历史给路径。注：history 句柄指向的 events.jsonl 本身已含全部 agent 输出，任何 agent 想看都能看，这与「主动查询」一致；「全可见」指的是主动把全部 a2a 通信摊给每个 agent，那个先不做。）

## 19. WF 是 goal-enroll 的一等公民（2026-09-06 11:0x）
> 然后 goal-enroll里 WF应该是一等公民

（解读：goal enroll 请求必须绑定一个 work folder：有就传 folder_id，没有就由 MCP 经 katana-work-folder-mcp 建一个；goal 的 goal / spec / progress / findings 落在该 WF；events.jsonl 与 DD 记录从 WF 可达；Goal Agent 的 history 句柄含 WF id。具体字段见 protocol.md §1〔推荐〕。）

## 20. goal-steer 与 goal 版本号（2026-09-06 11:1x）
> 然后 应该可以有goal-steer的操作，改变 Or 增加字段，每个goal可以有版本号，版本变化了 goal agent在下次被调度起来的时候，需要可以看到

（解读：protocol.md §10 的 `goal_update` 改名 `goal_steer`，允许改或增任意 goal 字段；每次 steer 使 `goal_version` +1，落 `goal.steered` event 记 diff；Goal Agent 下一个 turn 的输入注入 `goal_version` 与自上次 turn 以来的 `steer_diff`（这是本次交接内容，按 GO-17 直接注入，不只给路径）。）

## 21. 书记员角色与两层可观测性（2026-09-06 11:2x）
> 我觉得系统里面应该有这么样一个角色，类似于"书记员"。
>
> 你想一下，整个系统的可观测性应该做得非常干净：
>
> 1. 框架层面：以 event 为基础
> 2. 每个 agent session：其实就是内部的一个 session 文件，发生了什么事也都能读到
> 3. 每个 agent 的 report（即 stop response）：是它给出的一个最终汇报
>
> 这些数据其实都是完全可观测的。
>
> 如果有一个"书记员"的角色，他就可以持续去观测这个系统，记录他的理解和汇报。这有点类似于 claudemem 那样的 observation 形式，把一些很细的信息抽象成 high level 的、结构化的观测信息。最原始的证据依然可以找到，但书记员在这里只做一个总结。
>
> 持续来讲，这能帮助我们分层发现一些系统问题。因为对系统的认知其实分两层：
>
> • L0 层：最细节发生了什么事。比如 agent 用了什么工具，这肯定是最真实的东西，但太细了
> • L1 层：我们需要一个 L1 层的总结，这就是靠书记员来处理的

（解读：新增第六个 agent「书记员」（Scribe），只读、不参与流程、不改任何状态。L0 = events.jsonl + 每个 agent run 的 session 文件 + 每个 Stop 输出，三者都是原始证据。L1 = 书记员产出的结构化 observation，每条带指回 L0 的证据指针。触发时机、输出 schema、存放位置由 agent 起草标〔推荐〕。）

## 22. agent-run 的两项必备特性（2026-09-06 11:3x）
> 还有一个点，我们基本把所有 Agent 相关的东西都封装到 Agent run 里面了，它起码要支持两个特性：
>
> 1. Harness 可配置（开启哪些东西）：
> 比如它能读到哪些 MCP、有哪些 MCP 的读写权限，以及包含哪些 Hook 之类的。举个例子，像 Wiki、Memory 这类 MCP，我们可能希望它能读到；但有些写入性质的 Hook，我们可能不希望它加入：比如 Cloud Memory 会把 Agent 运行内容转换成 observation 写进来，而对于我们这种纯自动化的场景，是希望它不要加这个观测的。
> 2. Session 保存位置可指定：
> Agent 运行的一个副作用是，像 Claude Code、OpenCode 这类工具运行都会留下一个 Session 文件。外层需要可以指定它的保存位置，这样每次、每条线的 Session 才能想办法集中存放在一个地方，方便统一管理

（解读：这是对 agent-runtime 的需求，不是引擎的。核对现状：agent-run 已有 `--harness <profile>`（profiles/harness/*.yaml：MCP 白名单与 read/read-write、tools、subagent、network、data roots、permission_mode、memory injection）、`--mcp-allow`、`--write`、`--isolation full`、`--session-root <dir>`。缺口两处：① harness profile 没有显式的 `hooks` 字段，claude-mem 之类写入型 hook 的开关要补进 profile 并在 compile 时兑现；② 引擎要把 `--session-root` 指到 `<goal_run_root>/sessions/`，与 protocol §12 书记员的 L0 路径一致。每个流程 agent 一份 harness profile。）

## 23. session 续用与 compact（2026-09-06 11:4x）
> 另外一个点是，很多角色我们希望去 resume session，而不是每次都开一个新的。比如说 implementer、reviewer，以及我们的 Go Agent，它们其实都是需要去 resume session 才对的。如果每次都开一个新的，其实有点怪，不太符合设计，这样历史也能读到。
>
> 当然，我觉得这中间也是需要 trade off 的：
>
> 1. 有时候可能还希望它们经常 compact 一下（比如类似于 context window 超过多少了就 compact），这样既能读到历史，又不会每次都 fresh 去做事情。
> 2. 但有时我们可能也希望去做 fresh。
>
> 这些都可以根据不同的场景来配置。

（解读：每个角色的 session 策略可配：`resume`（续用同一 session，配 compact 阈值）或 `fresh`。Goal Agent、Impl、Reviewer 默认 resume。核对现状：agent-run 已有 `--resume <run_dir>`；compact 阈值是 agent-runtime 的缺口。session 的作用域与默认值由 agent 起草标〔推荐〕。与 GO-17 的关系：本次交接内容仍注入；resume 时 session 自带历史，history 句柄主要服务 fresh 或 compact 后。）

## 24. 协议输入与 prompt 的对应（2026-09-06 11:5x）
> 还有一点，对 agent 每一轮把它调用起来，肯定都是要先给一个 user prompt，这其实就是每一轮的协议化输入，对吧？
>
> 比如 reviewer 返回 implementer，其实就是这一轮 review 的意见会作为 user prompt 输入。但一开始会给他一个 system prompt，类似于告诉他该以什么样的框架做事，并且会告诉他历史信息可以在哪里拿到。
>
> 不过并不是每次都把历史信息注入为 user prompt，每轮只是注入最新的；历史信息如果他想拿，也是可以去读到的

（解读：确认。协议里每个 `*.in/1` 输入对象 = 该轮的 user prompt；system prompt 在 session 建立时给一次：角色与做事框架、输出 schema 约束、历史在哪里（history 句柄）。resume 时 system prompt 已在 session 里，不重发；每轮 user prompt 只含本次交接内容。与 GO-17 / GO-23 一致，是它们在 prompt 层的落法。）

## 25. 机械化原则：能机械化确定的都机械化，自由裁量才给 agent（2026-09-06 12:4x）
> 一些分支的规则，可以程序化——比如说 worktree 开在哪里，给下一轮输入的时候一定要把 worktree 开在哪里、现在的 commit ID 是多少都写清楚，有问题的话就打回，其实跟协议化输出的约束也是一回事。
>
> 或者比如说如果通过了就可以 merge，不过目前这个是由 agent 处理的。
>
> 总之我们这里面就一个大原则吧：能机械化确定的都尽量机械化去确定，脚本化或者靠外层的框架调度一些机械化节点去处理，那些自由裁量的才给 agent。
>
> 整个系统的框架、交互图和流程图，应该是非常清晰、非常确定的。

（解读：这是一条总原则，与「协议化输出」同源。落法：① 每轮输入对象必带程序化的 git 上下文——worktree 路径、分支、当前 commit id——由引擎填、由引擎核，对不上就打回，不靠 agent 自述；② 分支规则（worktree 开在哪、分支叫什么、从哪个 commit 开）全部由引擎程序化决定，agent 只在给定 worktree 里干活；③ 「通过即 merge」目前仍由 Merge Agent 做，是否把无冲突 fast-forward 收回程序，作为〔推荐〕待过目；④ 系统的框架图、交互图、流程图必须是确定的——每个节点是程序还是 agent、每条边由什么 Stop 输出触发，都能画清楚。）

## 26. 逐件定义清楚；第一件：goal enroll 的 MCP 接口与协议（2026-09-06 12:5x）
> 一件一件事，我觉得我们定义清楚吧
>
> 当我开始要有一个开发goal的时候，我和agent聊好了方案，这个goal大概率涉及到一些repos——甚至，有一些repo，在最开始的时候没想到，现在我们要用这套基建来端到端开发，第一件事，MCP的接口，协议，是什么？

（解读：改变工作方式——不再一次铺全，而是逐件定义。第一件是 enroll：入口场景是「人和 agent 在一个会话里聊好方案，然后由该会话调 MCP 把 goal 送进去」。两个硬事实要进协议：① 一个 goal 涉及多个 repo；② 有的 repo 是开始时没想到、中途才出现的。现行 protocol §1 只有单个 `repo` 字段，不够。）

## 27. 对 enroll 草案的三处纠正（2026-09-06 12:5x）
> path不对——我觉得 需要是worktree的路径，不是原始repo的 goal_path没必要，可以按照默认约定来做
> sessions and warn是啥？有必要么

（解读：① `repos[].path` 应是 worktree 的路径，不是原始 repo 的 checkout——enroll 时会话已经在 worktree 里工作，引擎从 worktree 反推 repo；② 去掉 `goal_path`，goal 文件按默认约定在 WF 里（`goal.md`）；③ `sessions`（GO-23 的按 goal 覆盖 session 策略）与 `warn`（GO-6.4 的 warning 线）被质疑是否需要出现在 enroll 里——按 less is more 应从 enroll 拿掉，作为引擎默认配置。）

## 28. PR 强制 + 交接必 push 且与 remote 一致（2026-09-06 13:0x）
> 还需要PR URL，我们强制，所有开发按照PR来做，并且每个agent之间交接，都必须强制push remote && 和latest一致，commit似乎就不用写清楚了

（解读：① enroll 每个 repo 带 PR URL，所有开发以 PR 为单位；② 每次 agent 之间交接，引擎强制核对：已 push 到 remote，且本地 HEAD == remote 分支 tip（与 latest 一致）；③ 因此第 25 段说的「输入里写清 commit id」不再需要——分支 + PR URL + 「与 remote 一致」这条机械核对已经唯一确定了代码状态。引擎内部 event 仍可记 sha 供观测，但不进 agent 输入协议。）

## 29. enroll 的分支模型定稿：source / target 写清，release 分支名 goal 级约定，PR 与 worktree 下沉到 DD（2026-09-06 13:0x）
> 每个repo的source and target branch，我觉得还是写清楚好
>
> 一个goal，约定release分支名，然后有几个repos不确定，大家的修改都是一个个PR merge到这个release分支，然后完成的时候，release分支merge到它的目标分支（比如 main）
>
> 然后 PR worktree 都不需要在goal的时候写——我意识到了，是每个DD的时候需要去写才对

（解读：① enroll 里每个 repo 显式写 `source_branch`（release 从哪切）与 `target_branch`（release 最后合回哪）；② release 分支名是 goal 级的一个约定名，所有 repo 同名；repo 数量不定，可后加；③ 每张 DD 的修改是一个 PR，合进 release 分支；线 done 时 release 分支合回 target；④ PR URL 与 worktree 路径不属于 enroll，是每张 DD 派单时由引擎建分支、开 worktree、开 PR 后写进该 DD 的输入。第 27 段「worktree 路径」与第 28 段「enroll 带 PR URL」的字段位置由此修正为 DD 级；第 28 段的「交接必 push 且与 remote 一致」不变。）

## 30. source = release 分支（2026-09-06 13:1x）
> source应该是release分支才对 target是main或许没错

（解读：每个 repo 的 `source_branch` 就是这条线的 release 分支——DD 的 PR 合进它、done 时它合回 `target_branch`。goal 级单独的 `release_branch` 字段因此多余，直接写在每个 repo 的 source 里。release 分支不存在时引擎从 target 切出。）

## 31. source 写一次，target 按 repo（2026-09-06 13:1x）
> source branch只需要写一次，target或许每个repo有可能不同

（解读：`source_branch`（release 分支名）回到 goal 级，只写一次，所有 repo 同名；`target_branch` 留在每个 repo 里，可各不相同。）

## 32. repo 必须有 remote（2026-09-06 13:2x）
> repo需要带remote——必须有remote

（解读：每个 repo 条目显式带 `remote`（URL 或 remote 名）；没有 remote 的 repo 不接受 enroll。这是第 28 段「交接必 push 且与 remote 一致」的前提。）

## 33. 问：enroll 之后的程序化检验是什么（2026-09-06 13:2x）
> enroll之后 需要做的程序化检验是什么 告诉我

（解读：要求 agent 列出校验节点对 goal.enroll/2 的全部机械检查项，以及通过后引擎在起 Goal Agent 之前做的程序化准备。agent 的清单标〔推荐〕待拍。）

## 34. 对「通过后的准备」的两条纠正（2026-09-07 00:0x）
> 对于通过后的
> 1 是不是不要在WF里面维护文件系统来维护状态比较好 应该靠engine来通过文件系统 or 数据库维护状态？
> 3 —— 是的 但是每次的worktree应该是以PR为粒度开，应该是DD的过程来维护

（解读：① 修正第 19 段的落法：运行时状态（events.jsonl、control.jsonl、sessions、worktrees、goal.enroll.json）不放 WF，由引擎在自己的根目录或数据库里维护；WF 只放人读的 goal / design / progress / findings，引擎经 work-folder MCP 往里写摘要。history 句柄指向引擎的状态根。② 基线验收要做，但 enroll 时不开临时 worktree；worktree 一律以 PR 为粒度、由 DD 过程开与收，基线验收因此挪到每张 DD 开好 worktree 之后、Impl 起跑之前，在 base 上跑一遍。）

## 35. DD 的 PR 与 worktree 生命周期（2026-09-07 00:1x）
> 对 每个SPEC给到DD 机械性创建PR && worktree，每个PR过review merge，需要close pr and clean worktree

（解读：确认第 34 段的落法。Goal Agent 每派一张 spec，引擎机械建分支、开 worktree、push、开 PR；PR 过 review 并 merge 后，引擎机械关 PR（merge 即 close）、删 worktree、删远端 dd 分支。DD 以 failed 结束时同样关 PR（不合并）并清 worktree。）

## 36. DD 启动协议 = Goal Agent 的 Stop 响应（2026-09-07 00:2x）
> 我觉得 应该是Goal Agent的Stop Response 解析为DD启动的协议——在开启DD的情况，然后协议可以是多个repos的，不限于一个repo
> 然后如果要开DD，要求Goal Agent提前创建好source分支，target分支自然是release分支，work tree也要创建好，然后SPEC要求写到 类似于 docs/specs/N-XX.md 然后把spec的相对路径在DD启动协议里写好

（解读：① Goal Agent `stop: dispatch` 的输出对象就是 DD 启动协议，一张 DD 可跨多个 repo——第 35 段之前悬着的「DD 能否跨 repo」由此定为能。② 开 DD 的前置工作由 Goal Agent 在 Stop 前做完：为每个涉及的 repo 建 dd 分支（DD 的 source）、开 worktree、把 spec 写成仓内文件 `docs/specs/N-XX.md` 并 commit push；DD 的 target 就是 goal 的 release 分支。③ 协议里写每个 repo 的分支、worktree 路径、spec 相对路径。④ 与第 35 段「机械创建 PR && worktree」的关系：建分支与 worktree 从引擎移到 Goal Agent，引擎改为机械核对（分支在 remote、worktree 在该分支上且 HEAD == remote tip、spec 文件在该 commit 里存在、工作树干净），核过后开 PR、跑基线、起 Impl。核不过按第 25 段打回。）