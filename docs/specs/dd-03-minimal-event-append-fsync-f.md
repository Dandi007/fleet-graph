# minimal event 日志：append+fsync 写入与 fold 回放派生状态

# DD-103 · minimal event 日志：写入与回放

## 背景（自含）

本仓 `release/loopx-minimal` 正在按 `docs/specs/minimal/` 重建最小系统。这套系统有一条压倒性的纪律（golden-order GO-7.3「可观测性一定是最最最最最重要的」+ GO-14 已确认）：

> **`events.jsonl` 是唯一状态来源，恢复 = 回放**，不另存 checkpoint、不存 state.json。LangGraph 的 checkpointer 只是可删缓存，不是真相（GO-16）。

必读：`docs/specs/minimal/protocol.md` §8（event 日志格式与 kind 全集）、§11（恢复表）、`docs/specs/minimal/design.md` §6（两层可观测性）、`docs/specs/minimal/golden-order.md` GO-34（**状态归引擎：events/control/sessions/worktrees 落在引擎根如 `/data/fleet/goals/<goal_id>/`，不放 work folder**）。

今天这条纪律在代码里没有落点。本单只做**纯粹的日志层与 fold 层**，不做引擎调度。

## 要改什么

在包 `src/fleet_graph/minimal/`（若同批其它 DD 尚未落地则自行建 `__init__.py`，**不要 import 同批其它 DD 的模块**）新建 `events.py`：

1. **`Event` dataclass**：`ts`（ISO8601 带时区）、`goal_id`、`dd_id`（可 None）、`kind`、`seq`（int）、`payload`（dict）。序列化为一行 JSON（`ensure_ascii=False`，键顺序固定，`separators` 紧凑）。

2. **`KIND` 全集常量**，逐条照抄 protocol §8，不增不减：
   - goal：`goal.enrolled` `goal.turn.started` `goal.turn.finished` `goal.done` `goal.blocked` `goal.warning` `goal.message` `goal.steered` `goal.merged_to_target`
   - dd：`dd.dispatched` `dd.stage.started` `dd.stage.finished` `dd.acceptance` `dd.review_requested` `dd.approved` `dd.rejected` `dd.merged` `dd.failed`
   - agent：`agent.spawned` `agent.exited` `agent.failed` `agent.compacted`（§9 补入的那条）
   - engine：`engine.started` `engine.resumed` `engine.exiting`
   - control：`control.received`
   写入未知 kind 必须直接报错（防止 event 面被随手扩散）。

3. **`EventLog` 类**，构造参数是 goal 的引擎根目录（`goal_run_root`，形如 `/data/fleet/goals/<goal_id>/`，**由调用方给，不要在模块里硬编码路径**；日志文件是它下面的 `events.jsonl`）：
   - `append(kind, payload, *, dd_id=None) -> Event`：`seq` 由日志自身单调递增分配（每个 goal 独立计数，从 1 开始；打开已有文件时从末行的 seq 续），写入必须 **append + flush + `os.fsync`**（protocol §11 明确要求：写完 event 再执行副作用）。并发安全不是本单目标，但同一进程内多次 append 的 seq 不得重复。
   - `read(since_seq=0) -> Iterator[Event]`：流式读，**必须容忍最后一行是半截**（崩溃时可能写了一半）——遇到不能解析的尾行就丢弃并停止，不抛异常；中间行不能解析则要报错（那是真的损坏）。

4. **`fold(events) -> DerivedState`**：从头折叠出派生状态，字段至少包括：`state` ∈ `running | stopped | blocked | done | crashed`（`crashed` 由调用方结合进程存活判断，fold 只负责给出「最后一条 event 是否终态」，用一个 `terminal: bool` 表达）、`turn_no`、`current_dd`（dd_id 与其 round）、`dd_history`（每张 DD 的 outcome 摘要）、`goal_version`（`goal.steered` 每条 +1，enroll 为 1）、`last_seq`、`warnings`。

5. **`resume_point(events) -> ResumePoint`**：实现 protocol §11 的续跑表——
   - 最后一条是 `goal.turn.started` / `dd.stage.started` / `agent.spawned` 且无对应的 finished/exited → 该 run 判丢失，动作 `restart_step`（并要求调用方补写 `agent.failed(detail: lost_on_restart)`）；
   - 最后一条是 `dd.acceptance` 中途 → 动作 `rerun_acceptance`（重跑该轮全部验收命令）；
   - 最后一条是 `*.finished` / `dd.merged` / `dd.failed` 等边界 → 动作 `next_step`；
   - 最后一条是 `goal.done` / `goal.blocked` / `engine.exiting(stop)` → 动作 `exit`。
   返回值要带上足够信息让调用方知道「重起哪个步骤」（stage 名、dd_id）。

## 不改什么

- 不碰 `src/fleet_graph/` 下任何旧模块（尤其不要动 `state/`、`bus/`、`scheduler/`）；新代码只在 `src/fleet_graph/minimal/` 下。
- 不写 work folder、不调任何 MCP、不起 agent、不碰 git、不碰 LangGraph。
- 不实现 control.jsonl 的读取与 MCP 工具（后续批次）。
- 不实现书记员/observations.jsonl（后续批次）。
- 不引入新运行时依赖；不改 Makefile；不改 `docs/specs/minimal/`。

## 怎么验收

`make verify` 绿（ruff line-length=100，写完 `make fmt`）。

另建 `tests/test_minimal_events.py`，全部用 `tmp_path`，至少覆盖：
- append 三条后 read 回来 seq 为 1/2/3 且内容一致；
- 重新构造 EventLog 指向同一目录后 append，seq 从 4 续，不重置；
- 写入未知 kind → 抛错；
- 手工往文件尾部追加半截 JSON 后 read → 前面的 event 全部正常返回，不抛异常；中间插入损坏行 → 抛错；
- fold：enroll→turn.started→turn.finished→dd.dispatched→…→dd.merged 的序列能得出正确 turn_no 与 dd_history；两条 `goal.steered` 后 goal_version 为 3；
- resume_point 四种情形各一条用例（丢失的 agent run / acceptance 中途 / 边界 / 终态），断言动作与携带的 stage、dd_id 正确。
