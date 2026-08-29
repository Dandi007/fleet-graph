# Step 7: fleet-graph 线级换座操作面（line set-seat，覆盖层 override + 防漂移 C1-C4）

## Authority and target

Direct-child development for `wf-9b5931` step 7（座位级 runtime×model 自由切换），授权依据 goal.md 2026-08-29 16:2x 步 7 增量、golden-order 分层原则（同 runtime=session resume 通道 / 跨 runtime=协议化交接通道）、以及监督面 2026-08-29 board:work-notes seq 539 的 Q1 裁决（落盘形态裁 A 覆盖层，附 C1-C4 四条防漂移约束）。

Target base：fleet-graph `main` @ `4dcdd3c62d3941cf6e43ad9e15d98bd9bb591511`（fleet-graph 无 `integration/model-switch` 分支，其活跃集成面即 main，现有全部 fleet-graph dd 单均以 main tip 为 target base——本单同）。不手写 MR、不绕过 dd 管线；product commit 不得引入 `.dev-dispatch/` 或 `.dd-evidence/` 身份区（由引擎管理）。

前置：agent-runtime 步 2/3/5 已合入（caller-wins resume + agent-session set-model + goal-agent 线级 set-model 透传）；fleet-graph 已有 roster（`config/ronin-lines.json`，编成 SSoT）与 scheduler/daemon 的 seat 解析面（`src/fleet_graph/scheduler/daemon.py` LineSpec.seat、probe 探活）。

## Frozen scope

实现「线级换座」操作面：对一条在跑/驻停的 goal line 换 runtime×model 座位，落盘形态 = **覆盖层 override**（不直写 roster）。

1. **操作面**（`fleet-graph line set-seat <folder_id> <seat>` 或等价 CLI/MCP 工具面，实现自选并落 docs 记用法）：
   - 停当前 generation → 写座位 override 到 scheduler 持久面 → 新 generation 以 override 座冷启动、经 wf_resume 续上下文。
   - override 与 roster 分离：roster 只经 git/PR/review/deploy 流水变更，运行时换座绝不改写 `config/ronin-lines.json`。
2. **C1（审计字段）**：每条 override 记录必带 `who / when / from→to / reason` 四字段，落 scheduler 持久面并可在 board evidence note 引用。
3. **C2（临时态语义）**：override 是运行时临时态；永久化仍走 PR 改 roster，合入部署后清对应 override（override 与 roster 相等时 reconcile 自动清亦可）。
4. **C3（reconcile/lint 巡检面）**：status 与 scheduler 启动时响亮列出「roster ≠ 生效座位」的 override 清单，长期漂移不许静默。
5. **C4（三元可观测）**：线状态可观测输出「roster 座位 / override 座位 / 生效座位」三元；换座前探活预检照既有 spec 不降级（probe healthy 才切；跨 runtime 过 OAuth native-only 宪法与座位 mcp 声明）。
6. **治理**：换座是 B 类生产变更留审计痕；批量换座 = 逐线换座原语编排，不另设旁路。

不做：改动 agent-runtime / goal-agent / loop-engine / subagent-mcp；改动 roster 编成格式；新增第二调度器或旁路。

## Required validation（验收先行）

先写红测试再实现变绿（`make verify` 会跑 `uv run pytest`，新测试进 `tests/`）：

1. **set-seat 操作面**：对测试线 `wf-XXXX` 下发 set-seat → override 落持久面（C1 四字段齐全）→ 新 generation 以 override 座冷启动（session resume 或协议交接按 runtime 是否变化判定）。
2. **C1**：override 记录可断言含 who/when/from→to/reason 四字段；缺任一字段拒绝写入。
3. **C2**：override 是临时态——与 roster 相等时 reconcile 自动清；永久化路径（PR 改 roster）不属本单运行时面，但 override 清理逻辑必须有测试。
4. **C3**：reconcile/lint 面在 roster≠生效座位 时响亮列出 override 清单（含 diff 事实），漂移不静默；零漂移时干净退出。
5. **C4**：status 输出「roster 座位 / override 座位 / 生效座位」三元；换座前探活预检照 spec（probe 不健康拒绝切换并报因）。
6. **回归**：既有 `make verify`（lint + pytest + conformance）不被破坏；roster 编成 SSoT 的既有读取/校验路径不变。

```dd-acceptance
uv sync --frozen
make verify
```
