# 最小系统 —— 输入输出协议（agent 起草，GO-12 授权）

> 状态：v1（2026-09-06 10:5x）。§0.2 由 GO-13 定、§10 与 §11 由 GO-16 / GO-14 确认、§6 按 GO-15 改；其余字段级细节仍是 agent 起草，用户可改任意字段。
> 依据 design.md 已定内容；本文件只定「字段与规则」，不改流程。
> 每个 agent 的输入是一个 JSON 对象，输出必须是**恰好一个** JSON 对象，无 prose、无代码围栏。

## 0. 通用规则

1. **公共信封**。每个输出对象必含 `schema` 与 `stop` 两个键；`schema` 是字面量字符串（如 `"goal.turn/1"`），`stop` 是该协议允许的枚举值之一。缺任一键、`schema` 不匹配、`stop` 不在枚举内，一律判**无效输出**。
2. **输出符合 schema 是 agent-runtime cli 的基础能力，不是引擎的**〔GO-13〕。引擎调 `agent-run` 时传入本文件对应的输出 schema（`--output-schema <json>`）；runtime 负责让 agent 的最后一条输出符合该 schema（校验、把错误喂回 agent 重试，次数与策略由 runtime 定），做不到就以非零退出并在 stdout 给出 `{"schema":"runtime.error/1","stop":"invalid_output","detail":"…"}`。引擎侧只有一条规则：**runtime 非零退出 = 该 agent 失败**，写 event `agent.failed`，按类型收尾：Goal Agent 失败 → goal 置 blocked；DD 内 agent 失败 → 该 DD 以 `failed` 结束交回 Goal Agent。引擎不重跑。各协议输入里的 `error` 字段因此删除。
3. **文件路径一律相对 workspace 根**；commit 用完整 40 位 sha。
4. **消息字段**（`message` / `detail` / `findings[].detail`）是给下一个 agent 读的自然语言，不限长度，但引擎只透传不解析。
5. 引擎对每个 agent 的调用都带 `run_id`（引擎生成），输出里不需要回显。
6. 字段命名 snake_case；未列出的键引擎忽略但原样落 event。
7. **两层输入：本次交接注入，历史只给路径**〔GO-17〕。每个 agent 的输入只直接包含「这一步为什么来找你」的内容（reject 消息、review 意见、验收失败输出、上一张 DD 的结论），写清楚、作为 prompt 注入。其余一切历史（全部 event、之前各轮 review、之前的 DD、MCP 送入的消息）**不注入**，只给一个 `history` 句柄：`{ "events": "<goal_run_root>/events.jsonl", "dd_dir": "<goal_run_root>/dd/", "note": "所有历史都在这里，需要就自己读" }`。agent 想看就 cat，引擎不替它挑。下面各节的输入按此原则精简：`dd_history` 只留一行摘要、`prior_reviews` 只留本轮、`feedback` 只留本轮回到 impl 的原因。
8. **session 策略按角色可配：resume 或 fresh**〔GO-23〕。每个角色一个 session 作用域，`resume` 模式下同一作用域内的每次调用都续用同一 session（agent-run `--resume <run_dir>`），配 `compact_at`（context 用量比例，超过就 compact 再续），`fresh` 模式每次新开。本次交接内容（§0.7）**两种模式都注入**；`history` 句柄在 resume 下是补充、在 fresh 与 compact 之后是主要入口。默认策略〔推荐〕：

| 角色 | 作用域 | 默认 | compact_at |
|---|---|---|---|
| Goal Agent（turn 与 review 共用） | 一个 goal 一个 session | resume | 0.7 |
| Impl | 一张 DD 一个 session（跨轮续用） | resume | 0.7 |
| CR / FR | 一张 DD 各一个 session（跨轮续用，能看到上一轮自己提的意见） | resume | 0.8 |
| Merge Agent | 每次合并 | fresh | – |
| 书记员 | 一个 goal 一个 session | resume | 0.6 |

可在 enroll 的 `sessions` 字段按 goal 覆盖（§1）。恢复（§11）时 resume 模式的丢失 run 也重起同一步骤，但带上该作用域的 session 目录续用，而不是新开。
9. **协议输入怎么变成 prompt**〔GO-24〕。两层对应两种 prompt：
   - **system prompt**，一个 session 只给一次（fresh 起时；resume 时已在 session 里，不重发）：角色与做事框架（role persona）、本协议的输出 schema 约束、`history` 句柄（历史都在哪、需要自己读）、harness 边界说明。
   - **user prompt**，每次调用一条：就是该轮的 `*.in/1` 输入对象（本次交接内容，如 reviewer 打回 implementer 时这一轮的 review 意见）。**只含最新**，不重复历史。
   - 因此 §2 到 §6、§12 里的 `history` 字段在 resume 模式下可省（已在 system prompt），fresh 模式下随首条 user prompt 一并给。引擎侧 adapter（§9）按此渲染。
10. **机械化原则：能机械化确定的都由引擎确定并核对，agent 只做自由裁量**〔GO-25〕。
   - **git 上下文由引擎填、由引擎核**。§4 到 §6 输入里的 `workspace` / `branch` / `base_commit` / `head_commit` 是引擎按分支规则算出来的，不是 agent 报的；agent 输出里凡引用 commit 的字段（`impl/1.commit`、`merge/1.merged_commit` / `new_head`），引擎都拿 git 核一遍，对不上判**无效输出**（与 §0.1 同级），写 event `agent.invalid_output`，按 §0.2 收尾（DD 内 → 该 DD `failed` 交回 Goal Agent）。
   - **分支规则全部程序化**：release 分支 `release/<goal_id>`，从目标分支 head 开；单的分支 `dd/<goal_id>/<dd_id>`，从 release 当前 head 开；worktree 路径 `<goal_run_root>/worktrees/<dd_id>/`；rebased 之后 review 的 base / head 按 §6。agent 不选分支、不选目录，只在给定 worktree 里干活。
   - **每个 agent 节点前后引擎核对什么**：

| 节点 | 调用前核对 | Stop 后核对 |
|---|---|---|
| Impl | worktree 存在且在 `branch` 上；HEAD == 上一轮 head（首轮 == `base_commit`）；工作树干净 | `commit` 在 `branch` 上且是 tip；工作树干净 |
| CR / FR | HEAD == `head_commit`；工作树干净 | 工作树与 tip 均未变（review 不改代码，改了判无效） |
| Merge Agent | `source_head` / `target_head` 与两分支 tip 一致 | `merged`：`merged_commit` == 目标分支 tip 且包含 `source_head`；`rebased`：`new_head` 是源分支 tip、目标分支 tip 未变；`failed`：两分支 tip 均未变 |
| Goal Agent | `goal_version` 是最新 | `dispatch` 的 spec 非空；`done` 时无未合并的 DD |

   - **通过即 merge 的归属**〔推荐〕（采纳则修订 GO-15）：Goal approve 后先由引擎试无冲突合并（`git merge --ff-only`，或干净的 `--no-ff`），成功且验收命令在结果上过就直接 `merged`；只在冲突或验收失败时才调 Merge Agent，让它只处理需要裁量的冲突。目前按 GO-15 仍全部交 Merge Agent。

## 1. goal enroll 请求（MCP 入口，校验节点的对象）

```json
{
  "schema": "goal.enroll/1",
  "goal_id": "g-7f3a2c",              // 可选；缺省由 MCP 生成
  "work_folder": "wf-ab12cd",          // 一等公民〔GO-19〕：有就传 folder_id；传 null 则 MCP 经 katana-work-folder-mcp 新建，topic 用 title
  "title": "把 X 功能做出来",
  "goal_text": "……自然语言目标，含完成定义……",   // 或 "goal_path": "goal.md"，指该 WF 内文件，二选一
  "repo": { "path": "/data/code/self/foo", "target_branch": "main" },
  "acceptance": [ "make test" ],       // 至少一条；进入 CR 前必须全部 exit 0
  "models": {                          // 可选；缺省用引擎默认表
    "goal": "claude-opus-5", "impl": "glm-5.3", "cr": "glm-5.3",
    "fr": "claude-opus-5", "merge": "glm-5.3"
  },
  "warn": { "turns": 30, "dd_rounds": 6 },  // 可选；warning 线，越线只告警
  "sessions": {                        // 可选〔GO-23〕；缺省用 §0.8 的默认表
    "goal": { "mode": "resume", "compact_at": 0.7 },
    "impl": { "mode": "resume", "compact_at": 0.7 },
    "cr":   { "mode": "fresh" } }
}
```

**校验节点规则**（全部程序化，任一不过即拒绝 enroll，不 spawn）：
- `goal_text` 非空；`repo.path` 存在且是 git 仓；`target_branch` 在该仓存在。
- `acceptance` 非空，每条能被 `bash -lc` 解析（dry-run：`bash -n`）。
- `models` 里的每个值在 agent-runtime 可用模型表里。
- 同一 `goal_id` 不重复 enroll。
- `work_folder` 非 null 时必须能 `wf_resume`；`goal_path` 给了则该文件必须存在于 WF。

MCP 通过后：绑定或新建 WF，把 enroll 对象原样写进 WF 的 `goal.enroll.json`；建 release 分支 `release/<goal_id>`（自 `target_branch` 切出）；spawn 引擎进程；返回 `goal_id` 与 `work_folder`。

**WF 是一等公民**〔GO-19〕的含义（〔推荐〕，字段级请用户过目）：
- goal 的正本在 WF：`goal.md`（= goal_text）、`spec.md`（可选）、`progress.md`、`findings.md`。引擎在每个 goal 级 event（turn 结束、DD 结束、done / blocked）后经 work-folder MCP 追加一行 progress。
- `<goal_run_root>` = 该 WF 内的 `runs/<goal_id>/`（events.jsonl、dd/、control.jsonl 都在这里），因此 `history` 句柄同时带 `work_folder` id，agent 经 work-folder MCP 或直接路径读历史都行。
- Goal Agent 的 Stop 输出 `done` / `blocked` 时，引擎把 summary 同步写进 WF `progress.md`，wf_save 一次。

## 2. Goal Agent · turn

**输入** `goal.turn.in/1`
```json
{
  "schema": "goal.turn.in/1",
  "goal": { /* §1 的 enroll 对象，含 steer 后的当前值 */ },
  "goal_version": 3,                   // enroll 为 1，每次 goal_steer +1〔GO-20〕
  "steer_diff": [                      // 自上一个 turn 以来的 steer，注入（本次交接内容）；没有则为空数组
    { "version": 3, "ts": "…", "changed": { "acceptance": ["make test", "make e2e"] }, "added": { "deadline": "…" }, "note": "操作者附言" } ],
  "turn_no": 4,
  "release_branch": "release/g-7f3a2c",
  "release_head": "<sha>",
  "dd_summary": "3 张 DD：2 merged，1 failed（dd-02：…一句话…）",   // 一行，不是整表〔GO-17〕
  "last_dd": { /* 上一张 DD 的结果对象，见 §7 —— 这是本次交接的核心内容，注入 */ },
  "last_stop": { /* 上一个 turn 的输出对象原样；首轮为 null */ },
  "messages": [ { "ts": "…", "from": "mcp", "text": "…" } ],   // 本 turn 新到的 MCP 消息，读后即清；旧消息在 history 里
  "warnings": [ "turns>=30" ],
  "history": { "work_folder": "wf-ab12cd", "events": "<goal_run_root>/events.jsonl", "dd_dir": "<goal_run_root>/dd/",
               "note": "全部历史（每张 DD 的 spec / review / 验收输出、所有 event、旧消息、WF 的 progress / findings）都在这里，需要就自己读" }
}
```

**输出** `goal.turn/1`，`stop` ∈ `dispatch | done | blocked`
```json
{ "schema": "goal.turn/1", "stop": "dispatch",
  "summary": "为什么派这张单、它在目标里的位置",
  "dispatch": {
    "spec_text": "……给 Impl 的完整任务书，自含验收期望……",
    "acceptance_extra": [ "pytest tests/test_x.py" ]   // 可选，叠加在 goal.acceptance 之后
  } }
{ "schema": "goal.turn/1", "stop": "done",
  "summary": "目标已完成的依据（引用 dd_history / release_head 上的事实）" }
{ "schema": "goal.turn/1", "stop": "blocked",
  "summary": "…", "blocked": { "kind": "needs_human | external | contradiction", "detail": "…" } }
```
- 一个 turn 只派**一张** DD（GO-4.4「派 DD 本质就是 waiting DD」）。
- `dispatch.spec_text` 非空是硬校验。

## 3. Goal Agent · review（DD 过 FR 后）

**输入** `goal.review.in/1`：`goal`、`dd`（§7 的 DD 结果对象，此时 `outcome` 为 `awaiting_approval`，这是本次交接内容，注入）、`release_head`、`history`（同 §2，历史自己读）〔GO-17〕。

**输出** `goal.review/1`，`stop` ∈ `approve | reject`
```json
{ "schema": "goal.review/1", "stop": "approve", "summary": "…" }
{ "schema": "goal.review/1", "stop": "reject",  "message": "必填：要 Impl 改什么" }
```
`reject` 缺 `message` 判无效。

## 4. Impl

**输入** `impl.in/1`
```json
{ "schema": "impl.in/1",
  "dd_id": "dd-03", "round": 2,
  "workspace": "/data/worktrees/g-7f3a2c/dd-03",   // 已 checkout 到单的分支
  "branch": "dd/g-7f3a2c/dd-03", "base_commit": "<release_head>",
  "spec_text": "…", "acceptance": [ "make test", "pytest tests/test_x.py" ],
  "feedback": {                        // 本轮为什么回到 impl，只这一条，注入〔GO-17〕；首轮为 null
    "from": "acceptance | cr | fr | goal | merge", "detail": "…写清楚要改什么…", "findings": [ /* §5 格式，可空 */ ] },
  "history": { "events": "…/events.jsonl", "dd_dir": "…/dd/<dd_id>/",
               "note": "之前各轮的 review、验收输出、commit 都在这里，需要就自己读" } }
```
**输出** `impl/1`，`stop` ∈ `committed | failed`
```json
{ "schema": "impl/1", "stop": "committed", "commit": "<sha>", "summary": "改了什么、为什么" }
{ "schema": "impl/1", "stop": "failed", "detail": "为什么做不了（缺依赖、spec 矛盾……）" }
```
- `committed` 时引擎校验：`commit` 存在于 `branch` 且工作树干净；否则判无效。
- `failed` 直接结束该 DD（`outcome: failed`），不重试，交回 Goal Agent。
- `workspace` / `branch` / `base_commit` 由引擎填写，调用前后按 §0.10 表核对；agent 不改分支、不切目录〔GO-25〕。

## 5. CR 与 FR（同一协议，`role` 区分）

**输入** `review.in/1`
```json
{ "schema": "review.in/1", "role": "cr | fr",
  "dd_id": "dd-03", "round": 2,
  "workspace": "…", "branch": "…", "base_commit": "<sha>", "head_commit": "<sha>",
  "spec_text": "…",
  "acceptance_results": [ { "cmd": "make test", "exit": 0, "tail": "…" } ],   // 本轮的，注入
  "cr_result": { "stop": "pass", "summary": "…", "findings": [] },   // 仅 role=fr 时有，且只是本轮 CR 的结论〔GO-17〕
  "history": { "events": "…/events.jsonl", "dd_dir": "…/dd/<dd_id>/",
               "note": "之前各轮的 review 与 impl 摘要都在这里，需要就自己读" }
}
```
**输出** `review/1`，`stop` ∈ `pass | fail`
```json
{ "schema": "review/1", "stop": "pass", "summary": "…",
  "findings": [ { "severity": "note", "file": "a.py", "line": 12, "detail": "…" } ],
  "evidence": [ { "cmd": "…", "exit": 0, "summary": "…" } ] }
{ "schema": "review/1", "stop": "fail", "summary": "…",
  "findings": [ { "severity": "blocker | major | minor | note", "file": "…", "line": 0, "detail": "…" } ] }
```
- `fail` 时 `findings` 至少一条 `blocker` 或 `major`，否则判无效（防止无理由打回）。
- FR 可以部署、跑任何东西（GO-6.1, GO-9）；做了什么写进 `evidence`，引擎只落 event。
- CR 与 FR 的区别只在 prompt 与模型，协议相同。
- `head_commit` 由引擎填、引擎核（§0.10）；review 不改工作树，改了判无效〔GO-25〕。

## 6. Merge Agent（所有合并的唯一执行者，GO-15）

一个协议覆盖两种合并：`kind: "dd"`（单的分支 → 本线 release）与 `kind: "release"`（release → goal 的目标分支）。引擎自己不做 merge、不做 fast-forward。

**输入** `merge.in/1`
```json
{ "schema": "merge.in/1",
  "kind": "dd | release",
  "dd_id": "dd-03",                    // kind=release 时为 null
  "workspace": "…",
  "source_branch": "dd/g-7f3a2c/dd-03", "source_head": "<sha>",
  "target_branch": "release/g-7f3a2c", "target_head": "<sha>",
  "acceptance": [ "make test" ] }
```
**输出** `merge/1`，`stop` ∈ `merged | rebased | failed`
```json
{ "schema": "merge/1", "stop": "merged",  "merged_commit": "<target 新 head>", "summary": "…" }
{ "schema": "merge/1", "stop": "rebased", "new_head": "<source 新 head>", "summary": "解决了哪些冲突、改了哪些文件" }
{ "schema": "merge/1", "stop": "failed",  "detail": "…" }
```
- `merged`：Stop 时目标分支已包含改动、验收命令已在合并结果上跑过且过（GO-5「stop 的时候都处理好了」）。引擎校验 `merged_commit` 是目标分支当前 head。
- `rebased`：rebase 动了代码，**不合并**。源分支停在 `new_head`，引擎对它**完整再走 CR → FR → Goal Agent 审单**（design §3.6 / §2.4），Goal approve 后再调一次 Merge Agent。kind=release 时 review 的 `base_commit` 是目标分支 head、`head_commit` 是 `new_head`，`spec_text` 用 goal_text。
- `failed`：kind=dd → DD 回 Impl，approve 清零，`feedback.from = "merge"`；kind=release → goal 置 blocked（`kind: merge_failed`），不猜。
- `source_head` / `target_head` 由引擎填写；Stop 后引擎按 §0.10 表核对三种结果各自的 git 状态，对不上判无效输出〔GO-25〕。

## 7. DD 结果对象（引擎产出，喂给 Goal Agent）

```json
{ "dd_id": "dd-03", "spec_text": "…", "spec_digest": "sha256:…",
  "outcome": "merged | failed | awaiting_approval",
  "rounds": 2, "branch": "…", "head_commit": "<sha>", "merged_commit": "<sha 或 null>",
  "acceptance_results": [ … ], "reviews": [ { "role": "cr", … }, { "role": "fr", … } ],
  "impl_summary": "…", "failure": { "stage": "impl | acceptance | cr | fr | merge", "detail": "…" },
  "usage": { "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "wall_seconds": 0 } }
```

## 8. Event 日志（可观测性，GO-7.3）

一行一个 JSON，append-only，文件 `<goal_run_root>/events.jsonl`；MCP 的 list / status 只读这个文件。
```json
{ "ts": "2026-09-05T22:47:13+08:00", "goal_id": "g-7f3a2c", "dd_id": "dd-03",
  "kind": "dd.stage.finished", "seq": 418,
  "payload": { "stage": "fr", "stop": "pass", "run_id": "…", "usage": { … } } }
```
`kind` 全集（引擎每次状态变化恰好一条）：
- goal：`goal.enrolled` `goal.turn.started` `goal.turn.finished`(payload=输出对象) `goal.done` `goal.blocked` `goal.warning` `goal.message`(MCP 送入) `goal.steered`(payload: version, diff, note) `goal.merged_to_target`(Merge Agent kind=release 成功后) `goal.dispatch_rejected`(dispatch 未过引擎 GO-36 核对被打回：字段级错误，作为下一 turn 交接内容)
- dd：`dd.dispatched` `dd.stage.started` `dd.stage.finished` `dd.pr_opened`(每个 repo 开 PR 一条) `dd.acceptance`(每条命令一条) `dd.review_requested` `dd.approved` `dd.rejected` `dd.merged` `dd.failed`
- agent：`agent.spawned` `agent.exited`(exit code, usage) `agent.failed`(runtime 非零退出，含 invalid_output / timeout 等 detail) `agent.invalid_output`(引擎判无效输出：解不出 / 校验不过 / 前后置闸不过 / commit 对不上，§0.1 / §0.10)
- engine：`engine.started` `engine.resumed`(from_seq) `engine.exiting`(reason: done | blocked | stop | crash)
- control：`control.received`(MCP 送入的每条操作，payload=操作对象)

`seq` 单调递增，每个 goal 独立计数；文件是该 goal 的**唯一状态来源**（§11）。

## 9. 对 agent-runtime 的需求清单（GO-13, GO-22）

所有 agent 相关的东西都封装在 `agent-run` 里，引擎只调它。引擎每次调用固定带：`--role <r> --harness <r> --session-root <goal_run_root>/sessions --run-id <run_id> --output-schema <json> --isolation full --timeout <n> --cwd <workspace>`。

| 需求 | 现状（agent-run --help 与 profiles/harness/ 核对，2026-09-06） | 缺口 |
|---|---|---|
| 输出符合 schema〔GO-13〕 | 有 `--structured`，无 `--output-schema` | **新增** `--output-schema`：runtime 负责校验、喂错重试、失败非零退出（§0.2） |
| Harness 可配置：能读哪些 MCP、各自 read / read-write〔GO-22.1〕 | 有：`--harness <profile>`，profile 里 `capability.mcp: [{server, access}]`、`tools`、`subagent`、`network`、`data.write_roots`、`permission_mode`、`memory.injection` | 每个流程 agent 一份 profile（见下表），今天只有 building-agent 一份 |
| Harness 可配置：包含哪些 hook〔GO-22.1〕 | profile **没有 `hooks` 字段**；`--isolation full` 是否已把宿主 hook（含 claude-mem 的 observation 写入 hook）全部隔离，待核 | **新增** `hooks: { allow: [...] }`（缺省空 = 不挂任何宿主 hook），compile 时兑现到各 runtime 的隔离原语；纯自动化场景默认**不**挂 claude-mem 之类写入型 hook |
| Session 保存位置可指定〔GO-22.2〕 | 有：`--session-root <dir>`（默认 /data/agent-runtime/runs） | 引擎传 `<goal_run_root>/sessions/`，runtime 在其下落 `<run_id>/`，即 §12 书记员读的 L0 |
| Session 续用与 compact〔GO-23〕 | 有：`--resume <run_dir>`；**无** compact 阈值参数 | **新增** `--compact-at <ratio>`（或 profile 里 `session.compact_at`）：resume 时若 context 用量超阈值先 compact 再续；compact 本身是 runtime 内部动作，引擎不感知，只落一条 `agent.compacted` event（kind 补入 §8） |
| 结构化输出与 role contract | 现有 role contract 字段与本文件不同 | 引擎侧薄 adapter：system prompt = role persona + schema 约束 + history 句柄（一个 session 一次）；user prompt = 该轮 `*.in/1` 对象（§0.9）；agent 最后一条 JSON → 本文件输出；role contract 不改 |

**六份 harness profile 的建议边界**〔推荐〕：
| agent | MCP | 写 | exec / network | hooks |
|---|---|---|---|---|
| Goal Agent | wiki read、memory read、work-folder read-write（只写本 goal 的 WF） | 否（改代码只经 DD） | exec 否 / network 否 | 无 |
| Impl | wiki read、memory read | 是（限 workspace） | exec 是 / network 按 repo 需要 | 无 |
| CR | 无 | 否 | exec 是（跑测试）/ network 否 | 无 |
| FR | wiki read | 否（可部署到测试环境，限 write_roots） | exec 是 / network 是 | 无 |
| Merge Agent | 无 | 是（限 workspace 与目标分支 push） | exec 是 / network 是（push） | 无 |
| 书记员 | work-folder read-write（只写 findings） | 否 | exec 否 / network 否 | 无 |

## 10. MCP 操作接口（agent 起草，GO-13 授权，GO-16 已确认）

MCP 是唯一常驻服务，对外 8 个工具。**读**只读 events.jsonl；**写**只做两件事：spawn 引擎进程，或往该 goal 的 `control.jsonl` 追加一行。引擎在每个步骤边界（agent 起跑前、验收命令前、git 操作前）读一次 control.jsonl 的新行，落 `control.received` event 后执行。MCP 与引擎之间没有别的通道。

| 工具 | 输入 | 做什么 | 对引擎的语义 |
|---|---|---|---|
| `goal_enroll` | §1 请求对象 | 校验节点 → 建 release 分支 → spawn 引擎 | 新进程，`engine.started` |
| `goal_list` | 无 | 每个 goal 一行：`goal_id / title / state / step / turn_no / dd_count / last_event_ts / warnings / pid` | 只读 |
| `goal_status` | `goal_id`, `tail`(默认 20) | §11 的派生状态 + 最近 N 条 event | 只读 |
| `goal_events` | `goal_id`, `since_seq` | 原始 event 流，给观测面拉 | 只读 |
| `goal_message` | `goal_id`, `text` | 写 control `{op:"message"}` | 进 Goal Agent **下一个 turn** 的 `messages`；不打断当前 agent |
| `goal_steer` | `goal_id`, `patch`（可改或**新增**任意 goal 字段，`goal_id` / `work_folder` / `repo.path` 除外） | 写 control `{op:"steer"}`；`goal_version` +1 | 下一个步骤边界起生效，落 `goal.steered` event（含 diff 与新版本号）；**Goal Agent 下一个 turn 的输入注入 `goal_version` 与 `steer_diff`**〔GO-20〕 |
| `goal_stop` | `goal_id`, `mode`: `graceful` \| `kill` | graceful 写 control `{op:"stop"}`；kill 直接 SIGTERM 进程组 | graceful：当前 agent 跑完即 `engine.exiting(stop)`，不起下一个；kill：立即退出，在跑的 agent run 视为丢失（§11） |
| `goal_resume` | `goal_id` | 对 state ∈ {stopped, blocked, crashed} 的 goal 重新 spawn | `engine.resumed`，按 §11 续跑；blocked 的 goal 通常先 `goal_message` 再 resume |

`state` 枚举（由 events 派生）：`running | stopped | blocked | done | crashed`。`crashed` = 最后一条 event 不是终态且进程不在。
MCP 自身重启时：扫所有 goal，`running` 但进程不在的按 `crashed` 处理，**不自动 resume**，只在 `goal_list` 里标出来，等人或外部观测面调 `goal_resume`。（这是 agent 的选择：崩溃后自动重启会掩盖问题，与 GO-7.3「让外部发现问题」相反。）

## 11. 恢复（agent 起草，GO-13 授权「先定」）

**决定：events.jsonl 是唯一状态来源，恢复 = 回放。** 不另存 checkpoint、不存 state.json。

引擎启动时从头 fold events.jsonl 得到派生状态：goal 处于哪一步、当前 DD 及其轮次、approve 是否有效、turn_no、dd_history。然后按最后一条 event 决定续跑点：

| 最后一条 event | 续跑 |
|---|---|
| `goal.turn.started` / `dd.stage.started` / `agent.spawned`（无对应 finished/exited） | 该 agent run 视为丢失：写 `agent.failed(detail: lost_on_restart)`，**重新起同一个步骤**。Impl 重跑会产新 commit，无害；review / merge 重跑无副作用；Goal Agent 重跑等价于再问一次 |
| `dd.acceptance` 中途 | 重跑该轮全部验收命令 |
| `*.finished` / `dd.merged` / `dd.failed` 等边界 | 从下一个步骤继续 |
| `goal.done` / `goal.blocked` / `engine.exiting(stop)` | 不续跑，退出 |

代码状态以 git 为准，不回放：续跑前引擎核对 events 里记的 `head_commit` / `release_head` 是否仍存在于对应分支，不存在则写 `goal.blocked(kind: state_mismatch)` 停下，不猜。

**每条 event 写入用 append + fsync**，写完再执行副作用；副作用（起 agent、git push）本身幂等或可重做，所以「event 写了、副作用没做」和「副作用做了、event 没写」两种崩溃都收敛到上表的重跑。

选这个而不是另存状态的理由（供用户判断）：只有一份数据要写对；可观测面和恢复面看同一个文件，日志和真相不会分叉；今天 fleet-graph 的 checkpointer 与 work folder 两份状态不一致正是 26 件静默失败里的一类。代价：引擎启动要读完整个文件，一个 goal 的 event 量在千条量级，可忽略。

## 12. 书记员 Scribe（GO-21；触发 / schema / 存放为 agent 起草〔推荐〕）

**定位**：第六个 agent，只读。读 L0，写 L1。不参与 §2 / §3 的任何分支，不能改 goal、不能发 control、输出不进任何流程 agent 的注入层（其他 agent 想看就经 `history` 自己读）。

**L0 三种来源（都已存在，不为书记员另造）**
| 来源 | 位置 | 写者 |
|---|---|---|
| event 日志 | `<goal_run_root>/events.jsonl` | 引擎（§8） |
| agent session 文件 | `<goal_run_root>/sessions/<run_id>/`（agent-runtime 落的 transcript：每次工具调用、每条消息） | agent-runtime |
| Stop 输出 | `goal.turn.finished` / `dd.stage.finished` 等 event 的 payload | 引擎从 agent stdout 原样落 |

**触发**〔推荐〕：引擎在每个 **goal 级** 边界起一次书记员（`goal.turn.finished`、`dd.merged` / `dd.failed`、`goal.done` / `goal.blocked`、`goal.warning`），不在 DD 内每个 stage 起，避免噪声。书记员失败（runtime 非零）只落 `agent.failed`，不影响主流程。

**输入** `scribe.in/1`
```json
{ "schema": "scribe.in/1",
  "goal_id": "g-7f3a2c", "goal_version": 3, "trigger": "dd.merged",
  "since_seq": 380, "until_seq": 418,               // 本次要看的 event 区间，注入的是区间边界不是内容
  "new_runs": [ { "run_id": "…", "role": "impl", "session_dir": "…/sessions/<run_id>/", "stop": { /* 该 run 的 Stop 输出原样 */ } } ],
  "prior_observations": "…/observations.jsonl",   // 之前的 L1，路径，自己读
  "history": { /* 同 §2 */ } }
```
**输出** `scribe/1`，`stop` ∈ `observed`
```json
{ "schema": "scribe/1", "stop": "observed",
  "observations": [
    { "kind": "progress | anomaly | cost | quality | decision | pattern",
      "severity": "info | warn | high",
      "title": "一句话",
      "summary": "两三句 high level 的理解",
      "evidence": [ { "event_seq": 402 }, { "session": "…/sessions/<run_id>/", "line": 118 }, { "stop_of": "<run_id>" } ],
      "tags": [ "impl", "flaky-test" ] } ] }
```
- `evidence` 至少一条，每条必须能定位到 L0 的一个具体位置；引擎校验 `event_seq` 在区间内、`session` 路径存在。缺证据的 observation 整条丢弃并落 `agent.failed(detail: observation_without_evidence)`。
- `observations` 可以为空数组（这段没什么值得记的），这是合法输出。

**存放**〔推荐〕：追加到 `<goal_run_root>/observations.jsonl`（每条 observation 一行，带 `ts`、`trigger`、`seq_range`）；同时经 work-folder MCP 把 `severity ∈ {warn, high}` 的追加进 WF `findings.md`，让 WF 的 findings 天然是 L1 的高严重度子集。

**读**：MCP 增一个只读工具 `goal_observations(goal_id, since_ts, severity)`；`goal_status` 的返回顶部带最近 3 条 L1。这使 §10 从 8 个工具变 9 个。
