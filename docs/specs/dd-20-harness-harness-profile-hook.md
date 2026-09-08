# harness 档：六个角色的 harness profile（含 hooks 开关）+ 加载校验

## 背景

GO-22 明确要求 agent-run 的 harness 可配置：能读哪些 MCP、各自读写权限、挂哪些 hook；并点名「像 Cloud Memory 会把 agent 运行内容转成 observation 写进来，对我们这种纯自动化场景，希望它不要加这个观测」。design.md §7.13 把缺口记为「harness profile 没有显式的 `hooks` 字段」。

现状核对（已确认）：`agentrun.build_argv` 已经传 `--harness <name>`，`AgentCall.__post_init__` 在 harness 为 None 时默认取 role 名；但**仓里 `profiles/` 目录根本不存在**，六个角色一份 profile 都没有。引擎真跑起来时 `--harness goal` 会指向一个不存在的档。

## 要做

**1. 六份 profile 文件**：`profiles/harness/minimal-goal`、`-impl`、`-cr`、`-fr`、`-merge`、`-scribe`。

格式：**先查 `pyproject.toml` 的依赖里有没有 PyYAML**。有就用 `.yaml`；**没有就用 `.json`，绝对不要为此新增第三方依赖**（并在模块 docstring 里写明选了哪种及原因）。

每份含这些键（键集固定，未知键校验时报错）：`name`、`role`、`mcp`（数组，每项 `{server, access}`，access ∈ `read` / `read-write`）、`tools`（允许的工具名单）、`hooks`（**显式数组；写入型 hook 一律不列；即使为空也要写 `hooks: []` 而不是省略**）、`network`、`data_roots`、`permission_mode`、`memory_injection`（六份全为 `false`）。

六份的边界按 protocol.md §9〔推荐〕与各角色职责：
- `goal`：最宽——可写 spec 文件、可建分支开 worktree（GO-36 要求 Goal Agent 自己做这些）
- `impl`：可写自己的 worktree
- `cr` / `fr`：只读代码，可跑测试；FR 另需可部署（GO-6.1）
- `merge`：只读代码 + git 写
- `scribe`：**全只读，零写权限**（GO-21：书记员只读、不改任何状态、不参与流程）

**2. `src/fleet_graph/minimal/harness.py`**：
- `PROFILE_NAMES`：六个档名常量
- `profile_for_role(role) -> str`：六个角色（照 `agentrun.ROLES`）到档名的映射，未知 role 抛 ValueError
- `load_profile(name, *, root) -> dict`：从 `profiles/harness/` 读一份
- `validate_profile(obj) -> list[str]`：返回错误列表（空 = 通过）。校验必填键齐全、无未知键、`access` 在枚举内、`memory_injection is False`、`hooks` 存在且是 list、`hooks` 里不含写入型 hook（用模块级显式黑名单常量，如 claude-mem / cloud-memory / memory-write 之类）、scribe 档的所有 `mcp[].access` 必须是 `read` 且 `hooks == []`

## 不改什么

- 不改 `agentrun.py`（它已经会传 `--harness`，本 DD 只补档与加载器，不动 argv 构造）。
- **不要改 `src/fleet_graph/minimal/__init__.py`**（连续五张 DD 的 rebase 冲突源）。
- 不改 `pyproject.toml` 依赖、不加第三方包、不改 `Makefile`。
- 不写任何图、不调 agent、不 spawn 进程、不 import `ddgraph` / `goalgraph` / `mergegate`（同批并行）。
- 不动 agent-runtime 那边的东西（GO-22 的 runtime 侧缺口不归本仓）。

## 验收

`tests/test_minimal_harness.py`：
1. 六份档全部能 `load_profile` 且 `validate_profile` 返回空列表；
2. `profile_for_role` 覆盖 `agentrun.ROLES` 全部六个角色，未知 role 抛 ValueError；
3. scribe 档断言：所有 `mcp[].access == "read"` 且 `hooks == []`（GO-21 只读硬约束）；
4. 六份档的 `hooks` 与黑名单常量求交集必须为空；六份 `memory_injection` 全为 False；
5. 构造坏档各报一条错：缺必填键 / `access` 非法 / 出现未知键 / `hooks` 缺失 / `hooks` 里混入黑名单 hook / scribe 档带 read-write；
6. 断言 `profiles/harness/` 下的文件数正好是六。

`make verify` 全绿，ruff 干净。
