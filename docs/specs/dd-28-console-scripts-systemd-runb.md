# 最小系统的运行入口与运维手册（console scripts + systemd + runbook + verify-minimal）

# 背景（自含）
protocol §10 定「MCP 是唯一常驻服务」，GO-10 定「enroll 的 MCP 自己 spawn 引擎」，代码都在（`mcpserver.py` / `engine.py`），但仓里**没有任何入口**：`pyproject.toml [project.scripts]` 只有 `fleet-graph = fleet_graph.cli:main`；`deploy/systemd/` 下只有旧 dd MCP 单元，没有最小系统的；也没有一页说明「怎么把一个 goal 送进去、怎么观测、怎么 stop / resume」。人要用这套系统时无从下手。

# 要做什么
1. **console scripts**：`pyproject.toml [project.scripts]` 加 `fleet-graph-minimal-engine = "fleet_graph.minimal.engine:main"` 与 `fleet-graph-minimal-mcp = "fleet_graph.minimal.mcpserver:main"`。先读 `mcpserver.py` 确认有没有 `main`；没有就加一个**最薄**的 `main(argv=None) -> int`（argparse 解析 `--engine-root`，调既有 `serve()`，加 `__main__` 保护），**不改 `serve` / `handle_request` 任何语义**。
2. **systemd 单元**：新增 `deploy/systemd/fleet-graph-minimal-mcp.service`，照 `deploy/systemd/` 现有单元的风格（Type=simple、WorkingDirectory=仓 checkout、ExecStart 走 uv、Restart=on-failure、Environment 给引擎根）。只加文件：**不 enable、不 start、不写安装脚本、不动任何既有单元**。
3. **`docs/minimal-runbook.md`**（新文件，中文）：一页操作手册——九个 MCP 工具的名字与入参（从 `mcptools.py` 的声明表抄，不另发明）、一个 `goal.enroll/2` 请求样例（字段以 `docs/specs/minimal/protocol.md` §1 与 `enroll.py` 为准）、引擎根目录布局（events / control / sessions / worktrees / dd / observations / goal.enroll.json）、怎么观测（`goal_status` / `goal_events` / 直接 tail events.jsonl）、graceful stop 与 resume 的语义（含 protocol §10 末段「崩溃不自动 resume，只在 goal_list 里标 crashed」）、最后一节「已知缺口」按 protocol §9 原文列出 agent-runtime 的三项外部缺口（`--output-schema`、profile 的 `hooks`、`--compact-at`），标明属外部依赖、非本仓可修。
4. **`Makefile` 加 `verify-minimal` 目标**：ruff check 限 minimal 相关路径 + `uv run pytest tests -q -k minimal`。**不改既有 `verify` / `test` / `lint` 目标的内容**。
5. 顺手确认 wheel 打包能带上 `profiles/harness/*.json`（看 `harness.py` 怎么定位 profile）：若靠仓内相对路径，就在 runbook 里写清「必须从仓根运行」，**不要为此改 harness.py**。

# 不改什么
不改 `src/fleet_graph/minimal/` 下任何模块语义（唯一例外是 `mcpserver.main` 这层薄壳）；不动 `src/fleet_graph/minimal/__init__.py`；不改 `docs/specs/minimal/` 下任何文件；不改任何旧 deploy 单元、不碰 systemctl；不加运行时依赖。

# 怎么验收
`make verify` 全绿 + 下面额外命令。新文件 `tests/test_minimal_entrypoints.py` 断言：`pyproject.toml` 里两个新 script 的目标对象可 import 且 callable；`deploy/systemd/fleet-graph-minimal-mcp.service` 存在且含 `ExecStart` 与 WorkingDirectory；`docs/minimal-runbook.md` 存在且包含九个工具名（逐个 in 断言）。
