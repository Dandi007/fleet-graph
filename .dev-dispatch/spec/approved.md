# 回收 wf-6475fd 两张 gate 单裁决 rationale 进判据（goal.md 16:4x G3）

来源：监督面直写 goal.md 2026-09-02 16:4x 交付物 G3。
监督面 16:4x 已把 `wf-6475fd` 名下两张在闸单就地交给它自己判
（`dev-fg-e760435f2a6d` 账本 outcome / `dev-fg-977e5280d628` metrics 零 I/O），
并在其 goal 写明了裁决义务（不采信自述 / 亲跑验收 / 变异两枪 / 回显写 rationale /
REJECT 时逐字写返工指令）。现场实验实际结果——是否漏判、是否走过场——是 G1 判据的一手输入。

## 要什么

把 wf-6475fd 两张 self-adjudication 裁决的 rationale 形态回收成**可机读判据**，
使「派单线自己判」不退化成一言 APPROVE：

1. 一条合法 self-adjudication APPROVE/REJECT 的 `rationale` 必须携带机械回显：
   三方验收逐字相等（spec dd-acceptance == run-config == record acceptance_commands）、
   产品 diff 未越 spec 边界、既有测试未删除（`LC_ALL=C comm -23` 逐名比对）、
   亲跑验收退出码与尾部回显。
2. REJECT 的 `rationale` 必须含逐字返工指令（非空，且指名返工点）。

## 判据（两方向可红）

### 阳性
一条携带上述全部机械回显字段的裁决 → 判据通过（作为合法 self-adjudication）。

### 阴性（可红）
构造一条只有 `decision: APPROVE`、rationale 为空的裁决 → 判据不得通过；
构造一条 REJECT 但 rationale 无返工指令的裁决 → 判据不得通过。
变异：把判定改成「decision 字段非空即通过」→ 必须有用例转红。

## 交付约束

- 只改 `scripts/check_supervisor_conformance.py`（或同仓等价 conformance 检查器）
  的裁决判据扩展，加 `tests/test_supervisor_conformance.py`（或对应测试）与 fixtures，
  fixtures 硬编码 wf-6475fd 两票 rationale 的形态样本（含一正一反样本）；
- 不触 human_gate 主体判定（那是 G1 的活）；不触 decision_publisher v2 唯一发布点；
  不部署不重启。

```dd-acceptance
uv sync --frozen
make verify
```