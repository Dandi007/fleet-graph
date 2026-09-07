# minimal 控制面：control.jsonl 写读 + goal_list/goal_status 投射

## 背景
MCP 是唯一常驻服务，对引擎的写只有两件事：spawn 引擎进程，或往该 goal 的 `control.jsonl` 追加一行；引擎在步骤边界读新行、落 `control.received` event 后执行（protocol.md §10）。读侧的 `goal_list` / `goal_status` 全部从 events.jsonl 派生，`state ∈ running|stopped|blocked|done|crashed`，`crashed` = 最后一条 event 非终态且进程不在，且**崩溃不自动 resume**（GO-16）。已合并的 `events.py` 提供 `EventLog.read` / `fold` / `DerivedState`。本张 DD 只做控制面这一层的纯数据代码，不做 MCP server。

## 要改什么
新增 `src/fleet_graph/minimal/control.py` 与 `tests/test_minimal_control.py`：

1. `CONTROL_OPS = (message, steer, stop, resume)`；`validate_control(op_obj) -> list[str]`（返回字段级错误列表，空列表即通过）：`op` 必须在枚举内；`message` 必须带非空 `text`；`steer` 必须带 dict 型 `patch`，且 patch 不得含 `goal_id` / `work_folder` / `repo.path`（protocol §10 明文禁改）、patch 非空；`stop` 的 `mode ∈ graceful|kill`；`resume` 无额外字段。
2. `ControlLog` 类，落 `<goal_run_root>/control.jsonl`：`append(op_obj) -> dict`（先 `validate_control`，不过抛 `ValueError`；补 `ts`（UTC ISO）与自增 `seq`，**append + fsync**，键顺序固定、`ensure_ascii=False`、紧凑分隔符）；`read_new(since_seq) -> Iterator[dict]`；打开已有文件时 seq 从最后一条可解析行续下去；最后一行截断（半行）视为未写完并忽略，中间行损坏则抛错 —— 与 `events.py` 的 `EventLog` 行为对齐，但**独立实现，不改 events.py**。
3. `goal_status_view(events, *, alive: bool, tail_events: list|None = None) -> dict`：基于 `events.fold` 产出 `{goal_id, state, step, turn_no, dd_count, goal_version, last_event_ts, last_seq, warnings, current_dd}`；`state` 按上面五值枚举计算，非终态且 `alive=False` → `crashed`；`step` 是人读的一句（如 `dd-03/cr`、`goal.turn#4`）。
4. `goal_list_row(goal_run_root, *, alive_probe) -> dict`：读该目录的 events.jsonl，产出 protocol §10 `goal_list` 那一行需要的键（`goal_id / title? / state / step / turn_no / dd_count / last_event_ts / warnings / pid`）；`alive_probe` 是注入的 `Callable[[int|None], bool]`（默认实现可用 `os.kill(pid, 0)`，但测试一律注入，不真探进程）；pid 从最后一条 `engine.started` / `engine.resumed` 的 payload 取，取不到为 None 且视为不活。
5. `never_auto_resume` 语义写进模块 docstring：本模块只报 `crashed`，绝不产生 resume 动作（GO-16）。

## 不改什么
- 不动 `src/fleet_graph/minimal/__init__.py`；不改 `events.py`（只 import 其 `Event` / `EventLog` / `fold`）。
- 不实现 MCP server、不注册工具、不 spawn 任何进程、不发信号（只做注入式 probe）。
- 不 import `agentrun` / `acceptance` / `ddflow` / `gitgate` / `enroll`；不加第三方依赖。

## 怎么验收
`make verify` 全绿。`tests/test_minimal_control.py` 至少覆盖：四种 op 的合法与各条非法（含 steer 改禁改字段、空 patch、stop 非法 mode）；`ControlLog` 的 seq 续接、fsync 后可被 `read_new(since_seq)` 增量读到、半行尾部被忽略、中间损坏行抛错；`goal_status_view` 五种 state（running / stopped / blocked / done / crashed）各一例；`goal_list_row` 在 tmp_path 造的 events.jsonl 上取到 pid 与 warnings，pid 缺失时 state 为 crashed。
