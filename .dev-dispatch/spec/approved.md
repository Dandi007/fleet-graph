# 修 verify-lim.sh 判据 11/12 —— 测量 bug 修复，不改判据标准

## 背景（用户 2026-09-03 全授权，修测量 bug 不改判据）

`scripts/verify-lim.sh` 是 wf-8d9737 线 16 项验收判据的机械化脚本，断言对象是**已部署的生产事实**（/data/fleet-graph/dd/、agent-bus :7490、decision MCP :5614 等），不是本工作树源码。当前两个判据存在测量 bug，永远报红或靠运气跳过，须修复测量方式，**不得改动判据标准本身**。

## 交付一：判据 11 `dd-gate-by-dispatching-line`（当前 L317-351 区域）

### Bug
判据 11 读 `record.json` 的 `.scope_verdict.decided_by`（L329/333），但引擎从不往 `scope_verdict` 里写 `decided_by`/`principal`。核实过真实用途（须照此写注释）：

- `record.json.scope_verdict` 是**准入时（development_create）的 B1/B3 边界裁决**：`DdControlPlane._require_scope(spec)` 的产物，只有 `admitted`/`rule_id`/`rationale`（等）字段，由 `_admit` 在 L735 写入，`_read_scope_evidence`（L2420-2429）只消费 `admitted`/`rule_id` —— 它是「这单有没有越 scope 边界被准入」的证据，**不是闸裁决署名**。闸裁决署名在别处（见下）。

### 真实裁决署名（判据 11 应改读的）
按优先级依序探测，任一命中即得 `decided_by`：
1. **`<repo_path>/.dev-dispatch/gate/decision-g<generation>.json`** 的 `.decided_by`（`DdControlPlane.gate()` 的 `_committed_gate_decision` 亲写的闸裁决文件；repo_path 在 record.json 里，generation 在 `record.json.generation` 或 `status.json.generation`，默认 1；文件可能不存在=该单未过闸）。
2. **board `work.decision.v1` 的 `decided_by`**：对 `status.json.awaiting.question_note_id`（或 gen result.json 的 awaiting）投递的裁决消息，agent-bus :7490 读 `board:work-notes` 频道按 `refs[].target_entity == question_note_id` 且 `payload.decided_by` 非空识别（Bearer token 取 `/data/agent-bus/tokens/fleet-graph.token`，只读 GET，禁止 publish）。

### 前缀规范化（必须）
真机存在两种署名写法：`wf-6475fd` 与 `wf-6475fd (goal line, self-adjudication)`。比较 `decided_by` 与 `record.json.dispatched_by` 时须先归一：**取 `decided_by` 第一个空白分隔 token**（`printf '%s' "$decided_by" | awk '{print $1}'`）再与 `dispatched_by` 全等比较。`decided_by` 为空或两源都无 → 该单不计数（不算 match 也不算 gate）。

### 判定标准（不变，只换测量）
窗口（默认 86400s，按 record.json mtime）内：有 dd 单、其中**过闸（有真实裁决署名）的单里 decided_by（归一后）== dispatched_by 的张数 > 0** → PASS，报出「窗口内 N 张 dd 单，M 张闸裁决 decided_by==dispatched_by（自判张数）」；窗口内无单 → FAIL 原文保留；有单但 0 张过闸 → FAIL（如实报 0，不再归因 scope_verdict）。样例字段照旧展示真实署名。

## 交付二：判据 12 `foreign-delivery-refused`（当前 L353-370 区域）

### Bug
现探针从 DD_ROOT 挑一张**真实 awaiting_gate 单**当靶子投 REJECT —— 有副作用风险（若身份校验失效，REJECT 会真的毙掉别人的单），且靶子靠运气（无 awaiting 单时跳过）。不得退回旧坑：用不存在的 id 撞 `DEVELOPMENT_NOT_FOUND`/`DD_NOT_FOUND` 提前返回，那样到不了身份校验分支，什么都证明不了。

### 修法：专造合成靶单（无副作用）
脚本在跑判据 12 时**现场合成**一张只属于探针的 dd 单（身份=探针自己），让身份校验分支真实走过再断言拒绝码：

1. `PROBE_PRINCIPAL="wf-8d9737-lim-selftest-probe"`（一个不可能是任何真派单线的 principal）。合成单的 `dispatched_by` 必须与 `PROBE_PRINCIPAL` **不同**（例如 `dispatched_by="wf-lim-selftest-synthetic-owner"`），这样探针身份必然是非派单方，触发 `NOT_DISPATCHING_LINE`。
2. 合成单写入 `DD_ROOT/dev-fg-lim-selftest-foreign-probe/`（专用目录名，绝不与 `dev-fg-<真实哈希>` 冲突）：
   - `record.json`：最小字段 `{"development_id":"dev-fg-lim-selftest-foreign-probe","repo_path":"/data/worktrees/fleet-graph-lim-selftest-foreign-probe","dispatched_by":"wf-lim-selftest-synthetic-owner","generation":1}`；
   - `status.json`：`{"development_id":"dev-fg-lim-selftest-foreign-probe","state":"awaiting_gate","generation":1,"dispatched_by":"wf-lim-selftest-synthetic-owner","awaiting":{"question_note_id":"msg_lim_selftest_foreign_probe","card_entity_id":"msg_lim_selftest_foreign_probe"}}`；
   - 空 `repo_path` 目录不必真实存在（身份校验在 workspace 校验**之前**，`DdOwnerSource.dispatched_by`/控制面 get 只读 record.json/status.json 文件即足够走到身份校验分支；若引擎版本要求 workspace 先在，探针 mkdir 一个空目录亦可）。
3. 走 `decision :5614` 的 `decision_deliver`（形态 A `target_kind=dd`+`target_id=合成id`，`principal=""` 或探针身份），断言拒绝码 `NOT_DISPATCHING_LINE`（PASS）或 `accepted`（FAIL 严重红）。
4. **跑完即清**：无论 PASS/FAIL，`rm -rf` 该合成目录 + 清理探针 mkdir 的空目录，并在回显里报告「探针合成单已清理，无真实单被触碰」。清理动作放 emit 之后，保证回显先落。

### 判定标准（不变）
拒绝码含 `NOT_DISPATCHING_LINE` → PASS；被接受 → FAIL（严重红）；其他码 → FAIL。

## 硬边界
- 只改 `scripts/verify-lim.sh` 判据 11/12 两个区块及其必需的辅助函数/变量（如合成单的 mkdir/write/rm、decided_by 归一函数）；判据 1-10、13-16 逐字不动；脚本头部用法注释可加一行说明判据 12 的合成靶单行为。
- 不改任何 `src/` 代码（测量 bug 在脚本，不在引擎）。
- 不得把判据标准改松：判据 11 必须真实报出真机自判张数（不恒 0、不恒绿）；判据 12 必须真实走到身份校验分支（合成单的 dispatched_by 与探针 principal 必须不同）。
- agent-bus 只读（GET），decision MCP 只调 `decision_deliver` 工具；不得 publish 任何消息、不得触碰任何真实 dev-fg-* 目录。

## 验收（dd-acceptance 围栏，逐字冻结）
```dd-acceptance
bash scripts/verify-lim.sh --check 11
bash scripts/verify-lim.sh --check 12
```
含义：判据 11 的回显必须不再出现「0 张带闸裁决（record.json 无 scope_verdict）」这一测量 bug 形态——改后它要么 PASS（真机存在自判单），要么以真实事实 FAIL（窗口内无自判单/无单），红绿由真机事实决定，不由脚本编造；判据 12 探针走完身份校验分支返回结构化码（PASS 时退出 0）。两条命令本身各自跑完退出 0 属于验收通过（脚本单项 FAIL 会使 `--check 11` 退出非 0，此时以回显文本为准人工判定是否测量已修——修复目标就是让 11 报出真机自判张数，而非恒 0）。
