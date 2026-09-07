# minimal agent 调用适配层：session 策略 + agent-run argv + Stop 解析

## 背景
引擎自己不做任何模型调用；六个角色（goal/impl/cr/fr/merge/scribe）一律经 `agent-run` 调起，见 docs/specs/minimal/protocol.md §0.8（session 策略表）、§0.9（system/user prompt 两层）、§9（引擎每次调用固定带的参数）。已合并的 `src/fleet_graph/minimal/protocol.py` 已提供 `validate(obj, expected_schema)` 与 `extract_protocol_object(text, schema_prefix)`。本张 DD 只做「怎么调起、怎么把 stdout 变成协议对象」这一层纯适配代码，不碰引擎编排。

## 要改什么
新增 `src/fleet_graph/minimal/agentrun.py` 与 `tests/test_minimal_agentrun.py`：

1. 角色与 schema 映射：`ROLES = (goal, impl, cr, fr, merge, scribe)`；`schema_for(role, call_kind)`，其中 goal 有两种 call_kind（`turn` → `goal.turn/1`，`review` → `goal.review/1`），impl → `impl/1`，cr/fr → `review/1`，merge → `merge/1`，scribe → `scribe/1`。未知 role / call_kind 抛 `ValueError`。
2. `SessionPolicy` frozen dataclass(`mode`: `resume|fresh`, `compact_at`: `float|None`)；`DEFAULT_SESSION_POLICIES` 照抄 protocol §0.8 的表（goal resume 0.7、impl resume 0.7、cr/fr resume 0.8、merge fresh None、scribe resume 0.6）；`resolve_session_policy(role, overrides)` 接受 enroll 的 `sessions` 字段（键即 role 名）做逐字段覆盖；非法 mode、`fresh` 却带 compact_at、compact_at 不在 (0,1] 一律抛 `ValueError`。
3. `AgentCall` frozen dataclass：`role`、`call_kind`、`run_id`、`cwd`（workspace 绝对路径）、`session_root`、`harness`（缺省等于 role）、`timeout_s`、`output_schema_json`（字符串）、`resume_dir`（`str|None`）、`model`（`str|None`）。
4. `build_argv(call) -> list[str]`：固定形状与固定顺序，便于逐项断言 —— `agent-run --role R --harness H --session-root S --run-id ID --output-schema JSON --isolation full --timeout N --cwd W` 后按需追加 `--resume DIR`、`--model M`。`role`/`harness`/`run_id` 必须匹配白名单 `[A-Za-z0-9._-]+`，否则抛 `ValueError`（防注入）；`timeout_s` 必须为正整数。永不经 shell 拼字符串。
5. `AgentResult` frozen dataclass(`ok`、`stop`、`obj`、`failure_code`、`detail`、`argv`、`exit_code`)；`FailureCode` 常量类：`nonzero_exit` / `invalid_output` / `no_object` / `schema_mismatch` / `timeout`。
6. `parse_stop(stdout, expected_schema, exit_code) -> AgentResult`：
   - `exit_code != 0`：若 stdout 里能提出 `runtime.error/1` 对象，`detail` 用它的 `detail`，`failure_code` 取 `invalid_output` 或 `timeout`（按其 `stop` 值），否则 `nonzero_exit`（protocol §0.2：runtime 非零退出 = 该 agent 失败，引擎不重跑）。
   - `exit_code == 0`：用 `extract_protocol_object` 取 stdout 里最后一个匹配 `expected_schema` 的对象；取不到 → `no_object`；取到但 `validate` 不通过 → `invalid_output`，`detail` 带字段级错误全文；通过 → `ok=True`，`stop` 取对象的 `stop`。
7. `run_agent(call, *, runner) -> AgentResult`：`runner` 是 `Protocol`（`run(argv: list[str], cwd: str, timeout_s: int) -> Completed(exit_code, stdout, stderr)`），并给一个 `SubprocessRunner` 默认实现（`subprocess.run`，`shell=False`，超时按 `timeout_s`、超时映射为 `FailureCode.timeout`）。风格照 `gitgate.py` 的注入式 runner。

## 不改什么
- 不动 `src/fleet_graph/minimal/__init__.py`（跨 DD 冲突源，dd-03 已踩过）。
- 不 import `events` / `enroll` / `gitgate`；只允许 import `fleet_graph.minimal.protocol`。
- 不实现引擎图、不引入 LangGraph、不写 event、不落盘、不做 prompt 渲染（下一批）。
- 不改 `protocol.py`；不加第三方依赖；测试里绝不真的 spawn `agent-run`。

## 怎么验收
`make verify` 全绿（ruff clean + 全量 pytest + conformance）。`tests/test_minimal_agentrun.py` 至少覆盖：六个角色的默认 session 策略与非法覆盖；goal 两种 call_kind 的 schema；`build_argv` 的 fresh/resume 两种形状逐项断言 + 注入字符被拒；非零退出且 stdout 带 `runtime.error/1`；stdout 混 prose 与多个对象时只取最后一个合法对象；schema 名不匹配；`review/1` `fail` 缺 blocker/major 时由 `validate` 报错并落 `invalid_output`；超时路径。
