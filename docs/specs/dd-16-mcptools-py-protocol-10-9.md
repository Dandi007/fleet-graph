# mcptools.py：protocol §10 的 9 个工具（声明表 + 读写处理器，不绑传输层）

## 背景

protocol.md §10（GO-13 授权、GO-16 已确认）定死了 MCP 这个唯一常驻服务对外的 8 个工具，§12 末行再加一个 `goal_observations`，共 **9 个**。铁律两条：**读**只读 events.jsonl；**写**只做两件事 —— spawn 引擎进程，或往该 goal 的 `control.jsonl` 追加一行。MCP 与引擎之间没有别的通道。

现状缺口：`runroot`（状态根布局）、`control`（control.jsonl 写读 + `goal_status_view` / `goal_list_row`）、`events`（fold / resume_point）都齐了，但**没有工具层**。

## 要改什么

只新增 `src/fleet_graph/minimal/mcptools.py`，**不绑任何 MCP 框架或传输层**（保持可纯测；传输层留给后面的 DD）：

1. `TOOLS`：9 个工具的声明式表（name、输入字段与必填、read/write 归类）：`goal_enroll` `goal_list` `goal_status` `goal_events` `goal_message` `goal_steer` `goal_stop` `goal_resume` `goal_observations`。风格照 `protocol.py` 的 `SCHEMA_SPECS`（声明表而非一堆 if）。
2. `validate_tool_call(name, args) -> list[str]`：未知工具、缺必填、类型不对都给字段级错误。
3. 读处理器（注入 `engine_root`）：`goal_list`（扫 `<engine_root>/g-*/`，复用 `control.goal_list_row`）、`goal_status`（复用 `control.goal_status_view`）、`goal_events(goal_id, since_seq)`、`goal_observations(goal_id, since_ts, severity)`。`goal_observations` 的读取用**注入的 reader**（默认一个本地行解析器），这样本 DD 不依赖 scribe.py 那张 DD 是否已合。
4. 写处理器：只走两条路 —— 经 `control.ControlLog` 往 control.jsonl **追加恰好一行**（message / steer / stop-graceful / resume），或返回一个 `SpawnPlan` dataclass（enroll / resume 用）。**本模块不 `subprocess`、不 `os.kill`、不发信号**，只产计划。
5. `crashed` 派生：state 是 running 但 pid 不在 → 报 `crashed`，且**绝不自动 resume**（§10 末段 + GO-16：崩溃后自动重启会掩盖问题，与 GO-7.3 让外部发现问题相反），只在 `goal_list` 里标出来。

## 不要改什么

- 不绑传输层/框架、不 `import langgraph`、不 spawn 进程、不发信号、不跑 git、不写 events.jsonl。
- `goal_steer` 的 patch 校验**直接调 `control.validate_control`**，不要另写一份规则。
- 不改 `control.py` / `events.py` / `runroot.py` 的公开签名。
- `__init__.py` 只在 docstring 模块清单加 `mcptools`。

## 验收要点

未知工具与缺必填被拒；tmp engine_root 下两个 goal 的 `goal_list`；假 alive_probe 下 crashed 标记正确且没有自动 resume；`goal_message` / `goal_steer` 各只追加一行 control 且不产生别的副作用；steer 碰不可变字段被拒；源码级断言本模块不 import subprocess / signal / os.kill。
