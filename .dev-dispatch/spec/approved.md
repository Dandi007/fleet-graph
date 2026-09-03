# Spec 缺陷⑫（wf-8d9737，监督面 02:20 更正窄版）· arbiter needs_human 布尔拆为具名升级目标

## 背景
A2 arbiter（read-only triage/suggestion 角色）产出 schema 只有一个 `needs_human` 布尔（生产面：`src/fleet_graph/arbiter/a2.py`——dataclass L85、解析 L133、SYSTEM_PROMPT L183、渲染 L469 `needs_human: true/false`、note_type 判定 L518）。布尔无法区分三种截然不同的去向，渲染层把一切 needs_human=true 都写成『交人/escalate to a human maintainer』——对已过 acceptance 的 dd 单，D5 常态下该指引找**派单线自判**，不是等人。⚠️ `src/fleet_graph/arbiter/managed_path.py:224` 区域是 `_ManagedFakeReasoner` **测试夹具**，非生产路径——生产 schema 以 `arbiter/a2.py:183` SYSTEM_PROMPT 为准。

## 交付（全部在 fleet-graph 仓，base=86a2492）
1. **schema 拆分（生产面 a2.py）**：`needs_human: bool` 拆为具名升级目标字段（建议名 `escalation_target`，封闭枚举三值）：
   - `dispatching_line`——该主体是已过 acceptance/在闸的 dd 单，常态归派单线自判（D5：闸由派单线判，人不在闸上）；
   - `supervisor_escalation`——B 类口径的升报，须监督者作答（方向/生产动作类裁决）；
   - `needs_evidence`——无人可判或证据不足，回取证（指明缺什么证据）。
   向后兼容：旧 payload 的 `needs_human: true/false` 解析为对应目标（true→按主体形态路由的默认目标；false→无升级），不炸旧读者。
2. **渲染语同步（a2.py L469/L518 一带）**：note 渲染按目标出指引——`dispatching_line` 渲染为『指引找派单线（decided_by 应为该单 dispatched_by）自判』，**不得**再渲染成『人仍拍板/escalate to a human maintainer』；`supervisor_escalation` 保留『须监督者答升报』；`needs_evidence` 渲染『回取证：缺 XX』。note_type 判定随之按目标定。
3. **SYSTEM_PROMPT 更新（L183）**：五键约束改为含具名目标字段，禁止 decision/verdict/approve/reject/gate_release 词汇的红线不动。
4. **用例**（新增 `tests/test_a2_escalation_targets.py` 或并入既有 A2 测试族）：
   - 已过 acceptance 的 dd 单作 subject → A2 note 指引找派单线自判（断言渲染文本不含『human』拍板指向、含派单线指引）；
   - 三目标各自路由渲染有用例覆盖（dispatching_line / supervisor_escalation / needs_evidence 三条至少各一正例）；
   - 旧 `needs_human` payload 解析兼容用例；
   - 阴性：枚举外值/空目标 → 解析拒绝或降级 needs_evidence 并留痕，用例能红。
5. **零删除既有 arbiter 用例**；受 schema 变更影响的既有断言更新到新真值不算删除（managed_path.py 夹具按新 schema 更新属此类）。

## 边界
- 只动 arbiter 面（a2.py、其渲染/note 发布最小点、夹具、测试）；不动 supervisor.py 的 classify 枚举（那是 supervise 面 R4 语义，另案）；不做 A2→决策路径（A2 仍 read-only、suggestion-not-decision 红线不动）；不碰 M6 MCP 面。

## 验收（dd-acceptance 围栏，逐字冻结）
```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_a2_escalation_targets.py'
bash -lc 'make verify'
```