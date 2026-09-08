# mcpserver.py：常驻控制面 MCP（mcptools 九工具的传输层 + enroll/resume spawn 引擎进程）

## 背景
GO-7.2 / GO-10 / GO-11 / GO-16：MCP 是这套系统**唯一常驻的服务**，goal 从它进来（enroll 校验通过后由它 spawn 该 goal 的引擎进程），几个 goal 在跑、每个的状态、对 goal 的操作接口都从它出去。DD-16 已经把 protocol §10/§12 的 9 个工具落成声明表 + 读写 handler（`mcptools.py`），并显式**不绑传输层**；DD-02/DD-11 已有 `enroll.validate_*` 与 `runroot.prepare_*`。现在缺的就是那层传输 + spawn。

## 要改什么
新增 `src/fleet_graph/minimal/mcpserver.py` 与 `tests/test_minimal_mcpserver.py`。

1. **传输**：JSON-RPC 2.0 over stdio，实现 `initialize`、`tools/list`、`tools/call` 三个方法。**不新增第三方依赖**——若 `mcp` 包已经是本仓依赖则可以用它，否则手写 stdio 循环（逐行读 JSON，逐行写 JSON）。把「读一行 → 处理 → 返回一个 dict」的核心抽成纯函数 `handle_request(req: dict, ctx) -> dict`，stdio 循环只是它的薄壳，测试全打在纯函数上。
2. **tools/list** 直接由 `mcptools` 的声明表生成（名字、描述、inputSchema），不要在这里重抄一份工具表。
3. **tools/call** 参数先过 `mcptools.validate_tool_call`，错误按 JSON-RPC error 返回字段级信息；通过后分派到 `mcptools` 的对应 handler。
4. **spawn**：`goal_enroll` 与 `goal_resume` 的 handler 返回 `mcptools.SpawnPlan`；本模块负责真正 spawn，argv **必须逐字**是：
   `[sys.executable, "-m", "fleet_graph.minimal.engine", "--goal-id", <goal_id>, "--engine-root", <engine_root>]`
   （这组 flag 由并行的 engine.py DD 定义；即使那张 DD 还没合，也照这个 argv 发，测试用注入的 stub spawner，不真起进程。）spawn 用 `start_new_session=True` 脱离 MCP 的进程组，stdout/stderr 重定向到 `<goal_run_root>/engine.log`。spawner 是可注入 seam，默认实现用 `subprocess.Popen`。
5. **崩溃不自动 resume**（GO-16 已确认）：`goal_list` 里把 pid 不在了的 goal 标出来，但服务器自己绝不重起它，只有显式 `goal_resume` 才 spawn。

## 不要改什么
- 不改 `mcptools.py`（工具表与 handler 都已定稿；确需补的话只允许加导出，不改语义）。
- 不改 `engine.py`（另一张 DD 在写；只按上面的 argv 契约调用它）。
- 不注册 systemd unit、不改 `docs/specs/**`、不新增依赖。

## 怎么验收
`tests/test_minimal_mcpserver.py` 至少覆盖：
1. `initialize` 返回合法的 JSON-RPC 结果；
2. `tools/list` 列出的工具名集合 == `mcptools` 声明表的工具名集合（用集合断言，防漂移）；
3. `tools/call` 参数非法 → JSON-RPC error 且带字段级信息，且**没有**发生 spawn；
4. 合法的 `goal_enroll` → 注入的 stub spawner 收到的 argv **逐字**等于上面那串；
5. 读类工具（`goal_list` / `goal_status` / `goal_events`）在 tmp_path 造的引擎根上返回和直接调 `mcptools` 同名函数一致的结果；
6. 一段多行 stdio 输入喂进循环，输出是逐行合法 JSON（含一条非法 JSON 行 → 返回 parse error 且循环不崩）。

`make verify` 必须全绿。
