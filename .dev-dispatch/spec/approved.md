# Spec dev-x6-m1：work-folder MCP 调用超时界真正全程覆盖 + goal_revision fail-open（X-6 M1）

- 单号：M1 第一张改动单（本线开线以来首单）
- 日期：2026-09-07
- 作者：ronin-sched-timeout 线 worker（opencode-glm53）
- 目标仓：/data/code/self/fleet-graph（GitHub Dandi007/fleet-graph）
- 目标基座：refs/heads/release/wf-610189 头 c53769e8981c（origin 实读确认）
- 验收命令（原样，goal.md §可复现验收）：
  - `bash -lc 'cd /data/code/self/fleet-graph && uv run pytest -q tests/test_x6_work_folder_timeout.py'`（在你的 worktree 内以同名命令执行）
  - `bash -lc 'cd /data/code/self/fleet-graph && env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy make verify'`（同上，在 worktree 内）

## 0. 必读前提（开工前先读完这一节，避免做成功劳）

目标基座 c53769e 上「超时管线」**已经部分在位**，这是本单与普通「加 timeout 入参」单的根本区别：

- `src/fleet_graph/state/work_folder.py`：`FastMCPCaller.__init__(url, timeout=None)` 已有 timeout 入参；`_call()` 经 `httpx_client_factory=factory` 注入，factory 里 `kwargs["timeout"] = self.timeout`（仅当 timeout 非 None）。缺省 `None`。
- `src/fleet_graph/scheduler/wake.py`：`WAKE_TIMEOUT_SECONDS = 5.0`；`LiveWakeSignals.__init__(timeout=WAKE_TIMEOUT_SECONDS)`；`goal_revision()` 构造 `FastMCPCaller(timeout=self.timeout)`。
- `src/fleet_graph/cli.py:895`：scheduler 装配 `wake=LiveWakeSignals()`。
- 依赖锁定：`fastmcp==3.4.7`、`httpx==0.28.1`（pyproject.toml）。

**而 X-6 的两次生产复发（2026-09-06 04:24 与 20:39，py-spy 栈行号与该版源码逐行吻合）正发生在同含此管线的已部署快照上。** 也就是说：5 秒超时已经传到了 httpx，却没能封住那两次永久等待。所以本单**第一要求是诊断**，不是盲目再加一层超时。

### 诊断假设（必须先证实或证伪，写进 PR/commit 说明与测试注释）

fastmcp 3.4.7 的 `StreamableHttpTransport`（`.venv/lib/python3.11/site-packages/fastmcp/client/transports/http.py`）对 `httpx_client_factory` 的调用约定是：`connect_session()` 在**仅当** `session_kwargs.read_timeout_seconds` 非空时才组装 `timeout` 形参并传给 factory；且即便传了，形状是 `httpx.Timeout(30.0, read=read_timeout_seconds)`——即 **connect 界 30 秒、read 界才受控**。而我们这边的 factory 用 `kwargs["timeout"] = self.timeout`（标量 5.0）覆盖：

1. httpx 0.28 的标量 timeout 等价 `Timeout(5.0, 5.0, 5.0, 5.0)`，理论上 connect/read/write/pool 全 5 秒界——**但** streamable-http 会话是长连接流式读（SSE/chunked），httpx 的 read timeout 只约束「单次网络读返回数据的等待」，而 fastmcp 的会话建立含多次请求-响应往返（initialize → notifications → tool call），每次往返若各自 <5s 而对端挂住的是「应答字节永不出现」，则 read 超时本应触发——**为什么 0906 没触发？** 你的任务就是回答这个问题并修掉它。候选方向（不预设结论）：
   - factory 覆盖路径上，fastmcp 3.4.7 实际把 factory 的 kwargs 集合以 `**({"timeout": timeout} if timeout else {})` 传入——注意**当 `read_timeout_seconds` 未设置时它根本不传 `timeout` 键**，此时 factory 里 `kwargs["timeout"]=5.0` 生效吗？逐行读 `transports/http.py` 的 `connect_session` 与 `client.py` 的 `call_tool` 路径，确认超时最终落在 `httpx.AsyncClient(timeout=...)` 的哪一层（连接级/请求级/流级）。
   - `asyncio.run(...)` 包住整个 `client.__aenter__ → call_tool → __aexit__` 会话：即使每次 HTTP 读有 5s 界，会话握手若在「等待服务端事件流头一个字节」处无限挂（fastmcp 内部 `anyio`/`httpx_sse` 的读循环），标量 timeout 可能只作用于请求发出阶段，流式响应的迭代读不在其界内。验证方法：写一个只 accept 不应答的本地 socket，观察现版本 5s 超时下到底多久返回、在哪一层抛（或永不抛）。
2. 无论诊断结论是哪一条，**修法判据只有一条：超时界必须真正覆盖 connect 到流读完的全程**——即从 `FastMCPCaller.call()` 进入到返回/抛出，最坏耗时被入参 timeout 有界（允许 ≥timeout 的有限超支，例如 asyncio 外层兜底唤醒；不允许无限等待）。

## 1. 改动面（建议路径，细节可自决但判据不可降）

### 1.1 `src/fleet_graph/state/work_folder.py` — FastMCPCaller 全程有界

- 超时界覆盖「connect 到流读完」全程的机制自选：候选包括（a）`asyncio.wait_for(self._call(...), timeout=...)` 外层兜底（注意 `asyncio.run` 顶层无法直接 wait_for 内层会话——需要把会话也搬进协程内，或用 `asyncio.timeout` 上下文）；（b）向 `StreamableHttpTransport` 传 `read_timeout_seconds`/session kwargs，使 fastmcp 自己把 read 界收紧到我们的值；（c）factory 内显式构造 `httpx.Timeout(connect=..., read=..., write=..., pool=...)` 并核对 fastmcp 对 factory kwargs 的传递契约。**选哪条以诊断结论为准**，但必须在代码注释与 commit message 里写清「为什么这条界能覆盖 0906 的挂点」。
- 超时触发时的异常类型必须仍是（或可被识别为）`WorkFolderError` 家族（现 `except Exception → raise WorkFolderError(...) from exc` 的语义保持），超时不得静默、不得挂死、不得换成语义不明的裸异常逃逸到调用方。
- `timeout=None` 显式传入时保持可构造、行为兼容（无界调用语义不回归），用例③锁死。
- 本单**不要求**给 `WorkFolder`/`FastMCPCaller` 的非调度器调用方（如 pump 的 wf_append_progress 路径）统一加超时——那属可选扩展（goal §二末条），不做不阻塞。

### 1.2 `src/fleet_graph/scheduler/wake.py` — goal_revision 探针 fail-open 语义化

- `LiveWakeSignals.goal_revision()` 在「超时或连接失败」时返回**「尚无事实」语义**（具体形态自决：返回 `None`、或哨兵常量、或专门异常类型——但必须让调度器可机械区分「探到了 revision」与「探针失败无事实」），不得把原始异常抛回调度器 tick 循环。
- 本 tick 视为 goal 未变：调度器（daemon.py `_check_wake` 的 goal_revision 分支）在「尚无事实」时**继续持有 park、继续扫名册**，不 wake、不抛、不中断 tick。注意与既有 fail-open 行为的衔接：现 `_check_wake` 里 `except Exception → woken:probe_failed:...` 是「唤醒」方向的 fail-open（保守放行），goal §二 M1 对本单要求的是「**goal 未变**方向的 fail-open（继续持有）」——两者语义冲突处由你裁决并写明理由：判据是「探针故障不得锁死线（既有语义）」与「探针故障不得假装 goal 变了（本单语义）」同时成立，即既不永久 park 也不误 wake，本 tick 记为「无事实、维持现状、下一 tick 再探」。
- `_establish_park` 路径的 fail-open 语义保持现状（无锚点不 park）。

### 1.3 `tests/test_x6_work_folder_timeout.py` — 三用例（文件名、用例语义不可改）

1. **用例①（caller 有界）**：起一个本地 socket server，accept 后**不应答任何字节**（不回 HTTP 响应、不关连接），`FastMCPCaller(url=该socket, timeout=T)` 调 `fs_stat`（或任一工具）必须在有界时间内返回（抛 `WorkFolderError` 即可，耗时断言允许 `T` 的合理超支，例如 `< T + slack`，slack 自定但要防 flaky：建议 T 取 2~5s、slack ≥ T）。**这个用例同时是诊断的取证工具**：先在未修版本上跑它，记录实际行为（挂死？多久？哪层异常？）写进 commit message。
2. **用例②（goal_revision fail-open + tick 完整）**：`LiveWakeSignals`（注入超时/挂死 caller 或指向挂死 socket）下 `goal_revision()` 返回「尚无事实」语义；且调度器一次 tick（或 `_check_wake` 等价调用序列）在探针故障下完整扫完名册、不抛异常、park 维持（或按你在 1.2 的裁决落定）。
3. **用例③（timeout=None 兼容）**：`FastMCPCaller(timeout=None)` 与 `FastMCPCaller()` 均可构造、`timeout` 属性为 `None`（纯构造断言即可，不打真网络）。

### 1.4 阴性靶（变异红）——必须执行并留回执

- 变异：把改动中的超时机制改回「无效」（等价于 `timeout=None` 的行为，例如删掉外层兜底或把 httpx 界改回无限）。在该变异下，用例①与用例②**必须红**（用例①因挂死而超时失败可接受，测试自身要设 pytest 超时保护或用子进程/线程隔离防真挂死 CI；用例②因 fail-open 语义缺失而断言失败）。
- 回执：变异 diff + 红 output 摘要写进 commit message 或随附文件；恢复变异后三用例复绿。
- 零删除既有测试（全仓 pytest 数量不减）。

## 2. 纪律（违反任何一条即 REJECT）

- **主 checkout `/data/code/self/fleet-graph` 只读**。一切 git 写操作（branch/commit/switch/reset）只在 dd 给你的隔离 worktree（`initial_handoff.worktree_path`）内。严禁在主 checkout 建分支/切分支/reset。
- **大文件只 `grep -n` / `sed -n 'A,Bp'` 取片段**，不整读。daemon.py 约 1900 行、wake.py 约 380 行、work_folder.py 约 333 行——都只取相关段。
- **收尾不得 `pkill -f` / `killall` 按模式杀进程**（X-8：dd runner argv 含验收命令原文，模式杀会误杀 runner）。只允许杀自己启动并记录了 PID 的测试子进程。
- 网络代理污染：跑测试/verify 前 `env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy`（goal 验收命令已含）。httpx client 的 `trust_env=False` 现状保持。
- commit 落在你的 worktree 分支；不 push、不碰 main、不碰 release/wf-610189（合流由本线 worker 做）。
- 遵守仓内既有代码风格与注释习惯（该仓注释密度高、docstring 讲「为什么」——新代码照此写）。

## 3. 验收（DoD）

1. `uv run pytest -q tests/test_x6_work_folder_timeout.py` exit 0。
2. `make verify`（含 lint + 全量 test + conformance）exit 0，且既有测试零删除。
3. 诊断结论（为什么 5s 界没封住 0906 挂死）+ 修法依据写进 commit message。
4. 变异红靶回执在卷。
5. 验收命令可重复执行（幂等，不依赖手工清理）。

## 4. 上下文快照（免你再翻史卷）

- X-6 现象原文：wf-4601c8 goal §七 X-6 行；py-spy 栈 `~/.local/state/line-supervisor/fleet-graphd-2702235-20260906-204053-sudo.pyspy`：`daemon tick → park_state → _check_wake → wake.goal_revision → WorkFolder.stat → WorkFolderMcpCaller.call → asyncio.run_until_complete → selectors.select`，挂在对 katana-work-folder-mcp（`DEFAULT_WORK_FOLDER_MCP_URL = http://127.0.0.1:5602/mcp/`）的 `fs_stat goal.md` 永久等待。同一时刻 MCP 服务对新连接正常。
- fastmcp 3.4.7 factory 契约关键行（`.venv/.../fastmcp/client/transports/http.py` `connect_session`）：`if session_kwargs.get("read_timeout_seconds") is not None: timeout = httpx.Timeout(30.0, read=...)`；`http_client = self.httpx_client_factory(headers=..., auth=..., follow_redirects=True, **({"timeout": timeout} if timeout else {}))`——注意 read_timeout_seconds 缺省时 factory 不收 timeout 键。
- 本单不部署、不合 main（B-1 归监督面）；你只交付 worktree 内的 commits + 绿的验收。
