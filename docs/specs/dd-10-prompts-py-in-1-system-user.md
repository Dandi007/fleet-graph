# prompts.py：*.in/1 输入对象 + system/user prompt 渲染（GO-17/GO-24）

## 背景

GO-24（golden-order.md:134-141）与 protocol.md §0.9（:27-30）定死：协议里每个 `*.in/1` 输入对象**就是**该轮的 user prompt；system prompt 在 session 建立时给一次（角色与做事框架、输出 schema 约束、历史在哪），resume 时不重发。GO-17（:72-75）定两层：本次交接内容直接注入，历史不注入、只给一个句柄「所有消息都在 XX，需要自己去读」。

现状缺口：`agentrun.py` 只负责 session 策略、argv 与 Stop 解析（`build_argv` / `parse_stop` / `run_agent`），**没有任何东西构造输入对象或 prompt 文本**。引擎图无法调 agent。

## 要改什么

新文件 `src/fleet_graph/minimal/prompts.py`。纯函数、零 IO、不跑 git、不调 subprocess。只 import `fleet_graph.minimal.protocol`（用 `describe_schema` 与 schema 常量）与 `fleet_graph.minimal.agentrun`（用 `SessionPolicy` / `schema_for`）。

### 1. 输入 schema 常量
`SCHEMA_GOAL_TURN_IN = "goal.turn.in/1"`、`SCHEMA_GOAL_REVIEW_IN = "goal.review.in/1"`、`SCHEMA_IMPL_IN = "impl.in/1"`、`SCHEMA_REVIEW_IN = "review.in/1"`、`SCHEMA_MERGE_IN = "merge.in/1"`、`SCHEMA_SCRIBE_IN = "scribe.in/1"`，外加 `IN_SCHEMAS` 元组。

### 2. history 句柄（GO-17 / protocol.md §0.7）
`history_handle(*, goal_run_root: str, work_folder: str | None = None, dd_id: str | None = None) -> dict`：产 `{"work_folder":…, "events": "<goal_run_root>/events.jsonl", "dd_dir": "<goal_run_root>/dd/" 或 "<goal_run_root>/dd/<dd_id>/", "note": "…都在这里，需要就自己读"}`。`work_folder` 为 None 时不放该键。路径用 `posixpath` 拼，不碰文件系统、不检查存在性。

### 3. 六个 build_*_in
按 protocol.md §2（:84-102）、§3（:123）、§4（:136-144）、§5（:158-167）、§6（:187-194）、§12（:309-315）逐字段产出 dict，签名只收**本次交接内容**（这是硬约束：函数签名里不存在可以塞整表历史的参数）：
- `build_goal_turn_in(*, goal, goal_version, steer_diff, turn_no, release_branch, release_head, dd_summary, last_dd, last_stop, messages, warnings, history)`
- `build_goal_review_in(*, goal, dd, release_head, history)`
- `build_impl_in(*, dd_id, round, workspace, branch, base_commit, spec_text, acceptance, feedback, history)`
- `build_review_in(*, role, dd_id, round, workspace, branch, base_commit, head_commit, spec_text, acceptance_results, cr_result, history)` —— `role` 必须是 `"cr"`/`"fr"`（否则 ValueError）；`cr_result` 仅 `role=="fr"` 允许非 None，`role=="cr"` 传了非 None 即 ValueError。
- `build_merge_in(*, kind, dd_id, workspace, source_branch, source_head, target_branch, target_head, acceptance)` —— `kind` ∈ `dd|release`；`kind=="release"` 时 `dd_id` 必须为 None（protocol.md:190）。
- `build_scribe_in(*, goal_id, goal_version, trigger, since_seq, until_seq, new_runs, prior_observations, history)` —— `until_seq >= since_seq` 否则 ValueError。
每个函数第一个键是 `"schema"`，键顺序与 protocol.md 一致；`None` 的可选字段保留为 `null`（协议里写了 null 的位置，如 `feedback` 首轮、`last_stop` 首轮），不静默丢键。

### 4. prompt 渲染
- `render_user_prompt(in_obj: dict) -> str`：一行固定前言（说明这是本轮的协议化输入、只含本次交接内容）+ `json.dumps(in_obj, ensure_ascii=False, indent=2)`。不夹带历史、不夹带 schema 说明。
- `ROLE_PERSONA: dict[str, str]`：六个角色（goal / impl / cr / fr / merge / scribe）各一段固定文本，写清该角色的第一性原理与边界，据 design.md §1 表（:41-49）与 protocol.md 各节：goal「判断工作是否完成；有写码权限但只走 DD」；impl「只在给定 worktree 干活，不选分支不切目录」；cr/fr「不改工作树，改了判无效；fail 必须带 blocker/major」；merge「Stop 时必须已处理完，冲突自行 rebase」；scribe「只读，不改任何状态、不参与流程，每条 observation 必须带指回 L0 的证据」。
- `render_system_prompt(role: str, *, call_kind: str | None = None, history: dict, harness_note: str | None = None) -> str`：persona + 输出 schema 约束（用 `protocol.describe_schema(agentrun.schema_for(role, call_kind))` 渲染成人读文本：schema 字面量、允许的 stop 枚举、各分支必填字段）+ 「恰好一个 JSON 对象、无 prose、无代码围栏」+ history 句柄（原样 JSON）+ 可选 harness 边界说明。
- `needs_system_prompt(policy: agentrun.SessionPolicy, *, is_first_call: bool) -> bool`：`policy.mode == "fresh"` → True；`resume` 且 `is_first_call` → True；`resume` 且非首次 → False（protocol.md:30）。

### 5. `__init__.py`
照现有风格在 docstring 里列上 prompts 模块，**不** import 它。

## 不改什么
- 不动 `agentrun.py`（不给它加 prompt 参数；prompt 怎么送进 agent-run 是引擎图那张 DD 的事）。
- 不动 `protocol.py`、`events.py`、`control.py`、`ddflow.py`、`gitgate.py`、`acceptance.py`。
- 不写文件、不建 session 目录、不调 agent-run。
- 不改 protocol.md / design.md。

## 验收
`make verify` 全绿，外加下面命令。测试要覆盖：六个 build_* 的键集与 schema 值逐个对上 protocol.md；`role=="cr"` 带 cr_result 报错、`kind=="release"` 带 dd_id 报错、`until_seq < since_seq` 报错；`history_handle` 带/不带 dd_id 与 work_folder 的四种组合；`render_user_prompt` 的输出能被 `json.loads` 还原出原对象（截取 JSON 段即可）且不含 "events.jsonl" 之外的历史正文；`render_system_prompt` 对六个角色都含该角色的 schema 字面量与全部 stop 枚举值；`needs_system_prompt` 三种情形。
