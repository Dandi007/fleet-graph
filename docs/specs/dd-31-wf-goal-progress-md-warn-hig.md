# WF 回写：goal 级 progress.md 与书记员 warn/high 的 findings.md（GO-19 一等公民落地）

## 背景

GO-19 原话：「goal-enroll 里 WF 应该是一等公民」。GO-34 修正的是**运行时状态**的存放位置（events / control / sessions / worktrees 归引擎根），**没有**取消 WF 的人读正本职责。protocol §1「WF 与运行时状态的分工」明文要求：

> WF 只放人读的正本：`goal.md`、`spec.md`、`progress.md`、`findings.md`。引擎在每个 goal 级 event（turn 结束、DD 结束、done / blocked）后经 work-folder MCP 追加一行 progress；Goal Agent Stop `done` / `blocked` 时把 summary 同步写进 `progress.md`。

protocol §12「存放」段同样要求：`severity ∈ {warn, high}` 的 observation 经 work-folder MCP 追加进 WF `findings.md`，「让 WF 的 findings 天然是 L1 的高严重度子集」。

**现状：整仓零实现。** grep `work_folder` 全仓只有三类用途——enroll 校验字段、steer 禁改键、`prompts.history_handle` 里把 id 塞进句柄。`scribe.py:240` 的 docstring 明说「追加进 WF findings.md 是调用方的事」，而没有任何调用方。`scribe.py:46` 已经定义好 `severity ∈ {warn, high}` 的筛选常量，等的就是这一步。

## 要改什么

1. 新建 `src/fleet_graph/minimal/workfolder.py`：
   - `class WorkFolderWriter(Protocol)`：`append_progress(work_folder, line) -> None`、`append_findings(work_folder, lines) -> None`。
   - 一个默认实现，经 work-folder MCP 追加（进程调用 seam 与 `prlifecycle` 调 gh 的写法保持一致：argv 白名单、`shell=False`、超时）。**不要**直接往 WF 目录写文件——协议说的是「经 work-folder MCP」。
   - `class NullWorkFolderWriter`：什么都不做，用于测试与「WF 不可达」的降级。
   - 纯函数 `progress_line(kind, payload) -> str`：把一个 goal 级 event 渲染成一行人读文本（带 ts、turn / dd 号、一句话结论）。这个函数零 IO，单独可测。
2. `goalgraph.py`：`GoalDeps` 增 `wf_writer: WorkFolderWriter | None = None`（默认 None = 不写，既有测试与图行为字节级不变）。在四个 goal 级边界追加一行 progress：turn 结束、DD 结束（merged / failed）、`goal.done`、`goal.blocked`。`run_scribe` 里 observation 过完证据闸之后，把 `warn` / `high` 的那批交 `append_findings`。
3. `engine.py`：`build_deps` 里用 enroll 对象的 `work_folder` 构造真实 writer 并传进 `GoalDeps`；`work_folder` 为 None 时用 `NullWorkFolderWriter`。
4. `docs/minimal-runbook.md`：第 4 节（引擎根布局）后面补一小节，说明 WF 侧有哪四个人读文件、谁写、什么时候写。
5. `docs/specs/minimal/context.md`「已实现」段补一行。

## 不改什么

- **WF 写入永不阻塞主流程**。这是本 DD 最重要的约束：work-folder MCP 不可达、超时、返回错误，一律吞掉异常，只落一条 `goal.warning`（kind 已在 `events.py` 注册，不要新增 kind），goal 循环照常往下跑。任何让 WF 故障能拖垮 goal 的实现都是错的。
- 不把任何**运行时状态**写进 WF：events.jsonl / control.jsonl / sessions / worktrees / goal.enroll.json 一律留在引擎根（GO-34 明文修正过一次，不要走回头路）。WF 只收人读的 progress / findings 文本行。
- 不改 `scribe.py` 的证据闸与 `ObservationLog`：`observations.jsonl` 仍是 L1 的正本，WF findings 只是它的高严重度**子集镜像**，不是搬家。
- 不改 enroll 校验、不改 `steer` 的 `work_folder` 禁改语义。
- 不碰 `mcptools` / `mcpserver` 的九工具声明表——本 DD 不加第十个工具。

## 怎么验收

- `make verify` 全绿。
- 新增 `tests/test_minimal_workfolder.py`：`progress_line` 的纯函数用例；注入假 writer 断言四个 goal 级边界各追加一行、内容含 turn / dd 标识；断言只有 `warn` / `high` 进 findings，`info` 不进。
- 关键回归用例：假 writer 每次调用都抛异常时，goal 仍以 `done` 正常收尾，只多出 `goal.warning`——证明 WF 故障不阻塞。
- `GoalDeps.wf_writer` 默认 None 时，既有 goalgraph 测试行为不变。
