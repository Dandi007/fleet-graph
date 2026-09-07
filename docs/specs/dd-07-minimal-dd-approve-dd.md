# minimal DD 循环状态机：阶段转移表 + approve 清零 + DD 结果对象

## 背景
DD 的循环在 design.md §3 定死：Impl → 验收命令 → CR → FR → Goal 审单 → Merge Agent；任何一步不过回 Impl，且 **impl 每跑一次 approve 清零**（GO-14）；Merge `rebased` 动了代码要**完整再走 CR → FR → Goal 审单**，Goal 再 approve 一次才合（GO-15，覆盖了早先的「直接推」）；Merge `failed` 时 kind=dd 回 Impl。喂回 Goal Agent 的 DD 结果对象格式见 protocol.md §7。这套规则现在只存在于文档里，引擎图还没有可调用的实现。本张 DD 把它做成纯函数，零 IO。

## 要改什么
新增 `src/fleet_graph/minimal/ddflow.py` 与 `tests/test_minimal_ddflow.py`：

1. `STAGES = (impl, acceptance, cr, fr, goal_review, merge)`，`TERMINAL_OUTCOMES = (merged, failed)`。
2. `Transition` frozen dataclass：`next_stage: str|None`、`outcome: str|None`、`feedback_from: str|None`、`approve_reset: bool`。
3. `next_stage(stage, stop) -> Transition`，完整转移表：
   - impl `committed` → acceptance；impl `failed` → outcome `failed`（不重试，交回 Goal Agent，protocol §4）。
   - acceptance `pass` → cr；`fail` → impl，`feedback_from=acceptance`。
   - cr `pass` → fr；`fail` → impl，`feedback_from=cr`。
   - fr `pass` → goal_review；`fail` → impl，`feedback_from=fr`。
   - goal_review `approve` → merge；`reject` → impl，`feedback_from=goal`。
   - merge `merged` → outcome `merged`；`rebased` → cr（回到 CR 走完整 cr→fr→goal_review）；`failed` → impl，`feedback_from=merge`。
   - 任何回到 impl 的转移 `approve_reset=True`；`rebased` 也让已有 approve 失效（`approve_reset=True`）。未知 stage/stop 组合抛 `ValueError`（机械化原则：不猜）。
4. `DDState` dataclass + `advance(state, stage, stop) -> DDState`：累计 `round`（每次进入 impl +1，首轮为 1）、`approve_valid: bool`（goal_review approve 置 True，任何 approve_reset 置 False）、`stage`、`outcome`、`history: tuple`（(stage, stop) 序列）。纯函数式，不改入参。
5. `warnings_for(state, *, warn_dd_rounds) -> list[str]`：轮数越线只产告警文本，不改 outcome（GO-6.4，不设硬上限）。
6. `build_dd_result(events, dd_id) -> dict`：从一串 `fleet_graph.minimal.events.Event`（允许 import 该模块，只读）fold 出 protocol §7 的对象：`dd_id`、`spec_text`、`spec_digest`、`outcome`（无终态时 `awaiting_approval` 或 `in_progress`，需在 docstring 写清判定）、`rounds`、`branch`、`head_commit`、`merged_commit`、`acceptance_results`、`reviews`（`[{role, stop, summary, findings}]`）、`impl_summary`、`failure`（`{stage, detail}` 或 None）。缺字段用 None，不编造。

## 不改什么
- 不动 `src/fleet_graph/minimal/__init__.py`；不改 `events.py`（只 import）。
- 不 import `agentrun` / `acceptance` / `gitgate` / `enroll`。
- 不引入 LangGraph、不写 event、不调 agent、不做 git、不做任何 IO。

## 怎么验收
`make verify` 全绿。`tests/test_minimal_ddflow.py` 至少覆盖：转移表逐条（含未知组合抛错）；`rebased` 后走满 cr→fr→goal_review→merge 才 merged；每次回 impl 后 `approve_valid` 为 False 且 `round` 递增；impl `failed` 直接终态 failed；warning 线越线只告警；`build_dd_result` 在三种典型 event 序列（merged / failed on cr→impl→…/ awaiting_approval）上产出的对象字段正确。
