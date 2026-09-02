# dd 闸默认裁决主体改为派单线（goal.md 16:4x G1）

来源：监督面直写 goal.md 2026-09-02 16:4x「🔴 用户拍板」第四节开列的交付物 G1。
用户拍板：DD 开发完了，派单线有全部信息与能力判断是否 APPROVE；gate 裁决主体不应是监督面。

## 背景与现状（机械事实，实现据此，不重证）

监督面 16:3x 真机读源码结论：`human_gate`（七阶段第 6 步，actor=script，即
`src/fleet_graph/graphs/dd_actors.py:BoardGate.act`）消费看板上 `work.decision.v1`，
读的是 `board.decision_for(ticket)` 的 `decided_by` 字段——**只记录、从不校验**；
dd 域内 supervisor-only / 身份白名单 / principal 判定，一个都没有。

## 要什么（改的是「谁来判」，不是「取消判」）

1. `human_gate` 认派单线发出的裁决：`work.decision.v1` 的 `decided_by` 若为该单
   record.json 的 `dispatched_by`（派单线），作为合法裁决主体接受。
2. 监督面覆盖权保留：监督面裁决仍可通过（非必经，但不可被误拒）。
3. 第三方裁决必须拒：`decided_by` 既不是本单 `dispatched_by`、也不是监督面身份的
   第三方发出的裁决 → **必须拒**，且拒绝理由机器可读。
4. ⚠️ 禁止「自动放行」。`ronin-auto-gate`（只看证据链完整性就放行的守护进程）
   于 2026-08-27 已被正确拒掉，那条裁定**仍然有效**：本单不引入任何自动放行形态。

## 判据（两方向可红）

### 阳性
派单线（`decided_by` == `dispatched_by`）发出的合规 APPROVE → 单正常推进到 merger；
监督面身份发出的裁决 → 同样接受（覆盖权）。

### 阴性（不可弱，只做阳性等于把闸拆了）
构造第三方（非派单线、非监督面）发出的 APPROVE 裁决 → human_gate 必须拒，
拒绝理由机器可读（指名 offending `decided_by` 与缺失的授权关系）。
变异：把「派单线合法」判定改成「任何非空 decided_by 都合法」→ 必须有用例转红。

### 反自批
裁决主体判定不得依赖「证据链完整性」而自动放行——不引入任何 auto-gate 信号。

## 交付约束

- 只改 `src/fleet_graph/graphs/dd_actors.py`（BoardGate 的裁决身份校验）、
  必要的 `src/fleet_graph/bus/board.py` 读径（若需把 `dispatched_by` 送进判定）
  与 `tests/test_dd_actors.py` 及必要 fixtures；
- 派单线身份 = record.json `dispatched_by`；监督面身份集按现有生产裁决身份
  （如 `cc-supervisor` / `uther-tui`）机械判定，不造新的授权机构；
- 不触 `decision_publisher.py` 的 v2 唯一发布点；不触 preauth；不部署不重启。

```dd-acceptance
uv sync --frozen
make verify
```