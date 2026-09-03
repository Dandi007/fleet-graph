# Spec M4（wf-8d9737）· line_message + 回执义务 + stage_models 座位单一来源

> 状态：**落卷待批；派单序钉在 M3 合入之后**（base 取派单时 release/wf-8d9737 头 a53f93c，吸收 M1/M2/M3；M3.1 六缺陷单 d90fb60ce56e 并行在闸，收割次序归监督面）。判据锚：goal.md §二 M4、§7 S2/S3 裁决（M4 内容据此收紧）、design.md D7、§6.1（E1 验收冻结）、§6.2（line_message）、§6.3（消息义务）、§6.4（座位由派单方给出）、§8 行「消息必达必回」「消息不能冒充裁决」「座位单一来源」「验收命令冻结」。与 design.md/golden-order 冲突以后两者为准。

## 要交付的行为（全部在 fleet-graph 仓）

1. **`line_message(line, text, kind ∈ instruction|info)`** 上 MCP（goal 面 :5611）：监督者专用（校验 principal）；落线的 inbox，是唤醒事实 `inbox_message`（M1 词表）；下一代 round 输入原文携带（现状 `coord/round-N-input.json` 的 `inbox_messages` 位置）。
2. **回执义务**：线下一轮必回执 `ack(message_id, 执行 | 拒绝 + 理由)`，落 progress 与状态面；`kind=instruction` 未执行也未拒绝 → 计入 R8 空转计数（缺省先把计数口径接上，告警规则归 wf-6475fd）。
3. **消息≠裁决**：`line_message` 不承载裁决语义——对 `waiting_decision` 的线只发 `line_message("APPROVE")` 文本，线可被唤醒但驻停不解除；裁决只走 `decision_deliver`（M2 路径）。结构上禁止：消息 payload 无 decision 字段、解除驻停的代码路径不得读 inbox。
4. **stage_models 成为座位唯一来源（S2.3/S3 收尾）**：
   - `development_create` 加 `stage_models` 形参（dict stage→seat）；缺省取 role registry 出厂值；
   - `control_plane.py`（原 :1243 处 `stage_models=dict(self.stage_models)`）改为**从 record 取值**，服务 argv 全局不再注入每单；
   - **删除 dd-mcp unit 的 `--stage-model continuous_review / final_review` 两键**（S3 裁定的收尾动作，与本形参同批上线、同批断言）+ CLI `--stage-model` 参数去功能化（保留解析则断言拒绝，或直接移除并更新部署单元模板）；
   - record.json 冻结每单 `seats`（含来源：line-explicit / registry-default），launches.jsonl 实测 argv 与 record 一致。
5. **消费方就位**：本线后续派单用 `stage_models={"implement":"glm-5.3-flash", "continuous_review":"glm-5.3", "final_review":"glm-5.3"}`（链 `glm-5.3-flash@opencode/gw` 已在 M0-b 落地）；registry 允许的座位集合校验（D7 例：监督者一条消息后线可改 implement 座位——本单只做座位参数通道，动态改座的流程消费归后续单）。M4 到位前实现座位用全舰默认并记 launches.jsonl 实际生效座位。
6. **验收命令冻结面（design.md §6.1/§8 行「验收命令冻结」——审计补锚 2026-09-03，原 spec 家族漏此行）**：`goal_status` 为每个已入编目标暴露 `acceptance`/`acceptance_digest`；目标载体（goal.md 的 dd-acceptance 块）摘要与入编时钉的不一致 → 调度器拒绝点火并报结构化码 `ACCEPTANCE_DIGEST_MISMATCH`（改验收命令等于重新入编）。

## 判据（正/负双向，goal.md §二 M4 与 design.md §8 三行原文内联）

- 阳性「消息必达必回」：`line_message(line, "…", instruction)` → 下一代 round 输入含该消息；progress 出现 ack；之后该线每张新单 `record.json.seats.implement` == 消息指定的座位；**dd-mcp argv 不再需要 `--stage-model`**（/proc/<MainPID>/cmdline 无该串）。
- 阳性「座位单一来源」：`cat /proc/$(systemctl --user show fleet-graph-dd-mcp -p MainPID --value)/cmdline | tr '\0' ' '` 不含 `--stage-model`；传 `stage_models` 的单 launches.jsonl 实测座位 == record.seats（未被任何全局覆盖）。
- 阳性「验收命令冻结」：`goal_status` 逐目标暴露 `acceptance_digest`；改目标载体 acceptance 后调 `goal_status` → 报 `ACCEPTANCE_DIGEST_MISMATCH`，线不点火（§8 行原文）。
- 阴性（误拒）：摘要一致的目标正常点火——任何「一致也拒」的实现必须红（对抗用例）。
- 阴性「消息不能冒充裁决」：对 `waiting_decision` 的线只发 `line_message("APPROVE")` → 线被唤醒但驻停不解除，回执写明「消息不是裁决」——用例必须红在「驻停被解除」上。
- 阴性（座位越权）：`stage_models` 给 registry 不允许的座位 → `development_create` 拒绝（结构化拒绝码），单不建立。
- 阴性（越权消息）：非监督者 principal 调 `line_message` → 拒绝码，inbox 不落。

## 测试与验收

- 新增 `tests/test_m4_line_message_seats.py`：上述正/负用例逐条（消息必达、回执落档、kind 语义、消息不解除驻停、stage_models 形参/record 冻结/argv 一致、删键断言、越权拒绝）。**零测试删除**；被 S2/S3 改写语义的既有断言更新到新真值不算删除。
- 部署单元模板与 `tests/test_deploy_unit.py` 同步（删两键后 unit 断言更新）。

## 边界

- 只动 fleet-graph 仓（goal MCP 面、dd service/control_plane/cli、调度器 inbox 唤醒接线、unit 模板）；agent-runtime 侧 registry 收口归 wf-9b5931（S7）；不做 R8 空转停线本体（计数口径即可）；不做 M5 release 分支。

```dd-acceptance
bash -lc 'uv sync --frozen && uv run pytest -q tests/test_m4_line_message_seats.py'
bash -lc 'make verify'
```
