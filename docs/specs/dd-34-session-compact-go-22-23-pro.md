# session 续用 + compact 阈值真正接进生产接线（GO-22/23，protocol §0.8/§9）

## 背景
GO-23（golden-order §23）要求 Goal Agent / Impl / Reviewer 等角色默认 **resume 同一 session**（配 compact 阈值），不是每次 fresh；protocol.md §9 要求引擎调 agent-run 时带 `--compact-at <ratio>`。

现状（已核）：`src/fleet_graph/minimal/agentrun.py` 有 `SessionPolicy` / `DEFAULT_SESSION_POLICIES`（:109）/ `resolve_session_policy`（:139）/ `AgentCall.resume_dir`（:186），`build_argv`（:194）在 `resume_dir` 非 None 时补 `--resume`。但全仓 grep `resume_dir` 只命中 `agentrun.py` 与 `stagerunner.py` 的**字段定义**（stagerunner.py:91、:189）——`goalgraph.py` / `ddgraph.py` / `engine.py` 从不计算它。也就是说生产接线里每个 agent stage 都是 fresh session，GO-23 完全没落地；compact 阈值也从未进 argv。

## 要改什么
1. `agentrun.AgentCall` 加 `compact_at: float | None = None`；`build_argv` 在 `--resume` 之后、`--model` 之前插 `--compact-at <ratio>`（仅当非 None）。`compact_at` 非 None 而 `resume_dir` 为 None 时抛 `ValueError`（fresh 不带阈值，与 `_validate_policy` 的既有规则一致）。ratio 的字符串化保持短形式（`0.75` 不要变成 `0.750000`）。
2. 新增纯函数：从 events 折出「该 goal 该 role 上一次 agent run 的 run_id」（放 `agentrun.py` 或 `events.py`，自选其一并在 docstring 说明）。实现前先读 `stagerunner.py` 落 `agent.exited` / `*.stage.finished` / `goal.turn.finished` 时 payload 的真实形状，以代码为准取 `run_id` 与 role/stage；无历史返回 None。它必须**只从 events 派生**（protocol §11：events.jsonl 是唯一状态来源），不许新增状态文件。
3. `stagerunner.StageRequest` 加 `compact_at`，透传进 `AgentCall`（`resume_dir` 已有字段，只需被调用方填）。
4. `goalgraph.py` / `ddgraph.py` 每处构造 `StageRequest` 的地方：按 role 调 `agentrun.resolve_session_policy(role, deps.session_policies)`；`mode == "resume"` 且上一次 run_id 存在 → `resume_dir=<session_root>/<last_run_id>`、`compact_at=policy.compact_at`；否则两者为 None（首跑 fresh）。`session_policies` 作为 `GoalDeps` / `DDDeps` 的可选 seam，默认 None = 用 `DEFAULT_SESSION_POLICIES`。
5. `docs/specs/minimal/context.md` 的「已实现」加一行：GO-22/23 落地（per-role resume + compact 阈值透传）。

## 不改什么
- 不改 `DEFAULT_SESSION_POLICIES` 的默认取值（protocol §0.8 的表仍是待用户过目的〔推荐〕）。
- 不改 `docs/specs/minimal/{design,protocol,golden-order}.md`。
- 不实现 compact 本身（那是 agent-runtime 侧的能力，引擎只传参数）。
- 不写 `agent.compacted` event（§9 明说 compact 是 runtime 内部动作、引擎不感知）；不动 `events.py` 的 kind 全集。
- 不改 `mcptools` / `mcpserver` / `mergegate` / `prlifecycle`。

## 怎么验收（make verify 之外）
新增/扩展测试须覆盖：(a) `build_argv` 带 `--compact-at` 的 token 顺序，以及 `compact_at` + `resume_dir=None` 报 ValueError；(b) 从 events 折 last run_id：无历史 → None、多轮取最后一次、role 之间互不串；(c) 用 fake AgentInvoker 断言同一 goal 第二次调同一 role 时 `AgentCall.resume_dir` 指向第一次的 run dir 且携带 compact_at，首次为 None。
