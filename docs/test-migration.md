# 旧测试逐文件迁移审计

基线 ff8a15c1117bfa506c84031814a07d2b26c096f8。旧实现和测试完整保留在 Git 历史中；下表逐文件列出处理理由。本轮没有先运行旧测试再删除失败断言，也不以旧测试数量衡量新契约覆盖。全量旧模块下线后，绑定旧 API 的测试不能继续导入；关键行为以明确的新协议重新测试。新测试的执行结果和联合验收缺口见交付文档。

| 原文件 | test 函数数 | 处理与理由 | 替代覆盖 |
|---|---:|---|---|
| `tests/conftest.py` | 0 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/fakes/fake_agent_run.py` | 0 | 替换：生命周期移到统一 runtime bridge；重建恢复、身份、schema 与 Session 连续性测试。 | tests/test_runtime.py、tests/test_commands.py |
| `tests/fakes/fake_agent_session.py` | 0 | 替换：生命周期移到统一 runtime bridge；重建恢复、身份、schema 与 Session 连续性测试。 | tests/test_runtime.py、tests/test_commands.py |
| `tests/fakes/fake_slow_coordinator_run.py` | 0 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/fakes/fake_supervisor_audit.py` | 0 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/source_tools.py` | 0 | 替换：旧审批/物理 WF/line 入口退役，改验 opaque WF、直接 enroll、持久队列与控制语义。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_15b_gate_reject_source_binding.py` | 8 | 替换：旧审批/物理 WF/line 入口退役，改验 opaque WF、直接 enroll、持久队列与控制语义。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_a2_escalation_targets.py` | 13 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_acceptance.py` | 15 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_action_result_handoff.py` | 6 | 替换：保留交接与版本安全意图，旧 receipt/多 gate 格式已退役；新测试核对真实 Git/PR adapter 契约。 | tests/test_git_ops.py、tests/test_engine.py |
| `tests/test_adapters.py` | 24 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_adoption.py` | 8 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_agent_session.py` | 21 | 替换：生命周期移到统一 runtime bridge；重建恢复、身份、schema 与 Session 连续性测试。 | tests/test_runtime.py、tests/test_commands.py |
| `tests/test_arbiter.py` | 27 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_arbiter_managed_path.py` | 17 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_bootstrap.py` | 21 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_bus.py` | 39 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_bus_alias_precision.py` | 7 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_capability.py` | 12 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_cli.py` | 24 | 替换：旧审批/物理 WF/line 入口退役，改验 opaque WF、直接 enroll、持久队列与控制语义。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_cli_gateway_prober.py` | 15 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_configure_remote_switch.py` | 1 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_contract_provenance.py` | 4 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_coordinator_contract.py` | 9 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_cost_obs_acceptance.py` | 2 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_cost_obs_integration.py` | 23 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_cost_observability.py` | 22 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_credential_separation.py` | 4 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_d12b_audit_note_targets.py` | 19 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_dd_actors.py` | 40 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_dd_auto_resume.py` | 11 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_dd_b2_recovery.py` | 13 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_dd_cancel.py` | 3 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_dd_control_plane.py` | 70 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_dd_git.py` | 7 | 替换：保留交接与版本安全意图，旧 receipt/多 gate 格式已退役；新测试核对真实 Git/PR adapter 契约。 | tests/test_git_ops.py、tests/test_engine.py |
| `tests/test_dd_materializer.py` | 39 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_dd_operation_lock.py` | 1 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_dd_pipeline.py` | 40 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_dd_replay.py` | 37 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_dd_runner.py` | 22 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_dd_sandbox.py` | 2 | 替换：生命周期移到统一 runtime bridge；重建恢复、身份、schema 与 Session 连续性测试。 | tests/test_runtime.py、tests/test_commands.py |
| `tests/test_dd_scripts.py` | 26 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_dd_service.py` | 13 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_dd_stale_running.py` | 9 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_dd_vendor.py` | 15 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_decision_bridge.py` | 55 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_decision_mcp.py` | 36 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_decision_publisher.py` | 9 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_decisions_reconciliation.py` | 11 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_deploy_unit.py` | 56 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_dispatch.py` | 28 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_e6_stop.py` | 15 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_egress_resilience.py` | 14 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_engine_gate_delivery.py` | 8 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_enroll_queue_atomic.py` | 6 | 替换：旧审批/物理 WF/line 入口退役，改验 opaque WF、直接 enroll、持久队列与控制语义。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_envelope_facts.py` | 14 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_evidence_chain.py` | 15 | 替换：保留交接与版本安全意图，旧 receipt/多 gate 格式已退役；新测试核对真实 Git/PR adapter 契约。 | tests/test_git_ops.py、tests/test_engine.py |
| `tests/test_fault_boundary.py` | 3 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_fleet_state_readmodel.py` | 32 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_gate_authority_text.py` | 3 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_gate_repair_boundaries.py` | 4 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_gate_verdict_normalization.py` | 10 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_goal_enroll.py` | 77 | 替换：旧审批/物理 WF/line 入口退役，改验 opaque WF、直接 enroll、持久队列与控制语义。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_goal_interrupt.py` | 37 | 替换：旧审批/物理 WF/line 入口退役，改验 opaque WF、直接 enroll、持久队列与控制语义。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_goal_line.py` | 50 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_goal_line_card.py` | 12 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_guards.py` | 26 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_harvest.py` | 132 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_harvest_allowlist.py` | 19 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_hello_graph.py` | 4 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_human_recovery.py` | 8 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_inbox.py` | 30 | 替换：旧审批/物理 WF/line 入口退役，改验 opaque WF、直接 enroll、持久队列与控制语义。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_inbox_content_path.py` | 11 | 替换：旧审批/物理 WF/line 入口退役，改验 opaque WF、直接 enroll、持久队列与控制语义。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_launch_integrity.py` | 2 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_launcher.py` | 22 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_layout.py` | 1 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_lifecycle.py` | 29 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_line_dd_binding.py` | 3 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_line_message_ack_evidence.py` | 10 | 替换：保留交接与版本安全意图，旧 receipt/多 gate 格式已退役；新测试核对真实 Git/PR adapter 契约。 | tests/test_git_ops.py、tests/test_engine.py |
| `tests/test_line_metrics.py` | 12 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_line_registration.py` | 9 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_line_restart.py` | 14 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_line_revive.py` | 36 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_m1_line_state_mcp.py` | 22 | 替换：旧审批/物理 WF/line 入口退役，改验 opaque WF、直接 enroll、持久队列与控制语义。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_m1_waiting_park.py` | 30 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_m2_dd_gate_delivery.py` | 11 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_m3_engine_defects.py` | 21 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_m3_line_selfgate.py` | 52 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_m4_availability.py` | 10 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_m4_line_message_seats.py` | 41 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_mcp_only_scaffold.py` | 3 | 替换：旧审批/物理 WF/line 入口退役，改验 opaque WF、直接 enroll、持久队列与控制语义。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_parking.py` | 62 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_preauth.py` | 27 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_probe_conformance.py` | 3 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_prompt.py` | 35 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_protocol_entry_normalization.py` | 25 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_r0_verify_rebuild.py` | 11 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_r1_testenv.py` | 17 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_r2_graph_unification.py` | 17 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_r3_stop_response.py` | 15 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_r4_release_branch.py` | 28 | 替换：保留交接与版本安全意图，旧 receipt/多 gate 格式已退役；新测试核对真实 Git/PR adapter 契约。 | tests/test_git_ops.py、tests/test_engine.py |
| `tests/test_r5_outer_gate_mcp.py` | 39 | 替换：旧审批/物理 WF/line 入口退役，改验 opaque WF、直接 enroll、持久队列与控制语义。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_r6_legacy_removal.py` | 15 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_re_adopt.py` | 23 | 替换：生命周期移到统一 runtime bridge；重建恢复、身份、schema 与 Session 连续性测试。 | tests/test_runtime.py、tests/test_commands.py |
| `tests/test_reconcile_a2_live_bus.py` | 14 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_release_script.py` | 4 | 替换：保留交接与版本安全意图，旧 receipt/多 gate 格式已退役；新测试核对真实 Git/PR adapter 契约。 | tests/test_git_ops.py、tests/test_engine.py |
| `tests/test_repair_runtime.py` | 7 | 替换：生命周期移到统一 runtime bridge；重建恢复、身份、schema 与 Session 连续性测试。 | tests/test_runtime.py、tests/test_commands.py |
| `tests/test_research.py` | 39 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_research_anchor.py` | 13 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_research_bus.py` | 14 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_research_coldstart.py` | 26 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_research_entry.py` | 20 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_research_preflight.py` | 18 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_research_sources.py` | 10 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_rework_contract.py` | 10 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_ronin_lines_config.py` | 16 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_run_artifacts.py` | 49 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_runtime_roster.py` | 4 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_scheduler.py` | 44 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_scheduler_daemon.py` | 102 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_scope_isolation.py` | 15 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_seat_override.py` | 34 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_smoke.py` | 3 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_standard_gate_policy.py` | 13 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_supervise_audit.py` | 40 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_supervise_inbox.py` | 6 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_supervisor_conformance.py` | 39 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_supervisor_events.py` | 62 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_supervisor_graph.py` | 47 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_terminal_derived_view.py` | 16 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_text_node.py` | 19 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_turn_timeout_two_tracks.py` | 26 | 替换：生命周期移到统一 runtime bridge；重建恢复、身份、schema 与 Session 连续性测试。 | tests/test_runtime.py、tests/test_commands.py |
| `tests/test_turn_timeout_variables.py` | 16 | 替换：生命周期移到统一 runtime bridge；重建恢复、身份、schema 与 Session 连续性测试。 | tests/test_runtime.py、tests/test_commands.py |
| `tests/test_wake_fact_decision.py` | 22 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_wiki_report.py` | 11 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_work_folder.py` | 17 | 替换：旧审批/物理 WF/line 入口退役，改验 opaque WF、直接 enroll、持久队列与控制语义。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_work_folder_reconcile.py` | 29 | 替换：旧审批/物理 WF/line 入口退役，改验 opaque WF、直接 enroll、持久队列与控制语义。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_work_folder_write_gate.py` | 2 | 替换：旧审批/物理 WF/line 入口退役，改验 opaque WF、直接 enroll、持久队列与控制语义。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_work_report.py` | 29 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_work_report_conformance.py` | 8 | 退役：旧研究/外围监督/seat/多 gate 或旧通信投影不属于最小 Goal 系统；不迁移其业务假设。事件与查询的必要能力改由统一 L0/L1 覆盖。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_x1_line_message_baseurl.py` | 6 | 替换：旧审批/物理 WF/line 入口退役，改验 opaque WF、直接 enroll、持久队列与控制语义。 | tests/test_control.py、tests/test_engine.py |
| `tests/test_x4_fault_classification.py` | 30 | 替换：旧图节点、调度器与 DD 结构退役；保留相关流程正确性目标，以新线性 DD/异步 Goal 契约重新验证。 | tests/test_engine.py、tests/test_control.py |
| `tests/test_x6_work_folder_timeout.py` | 3 | 替换：生命周期移到统一 runtime bridge；重建恢复、身份、schema 与 Session 连续性测试。 | tests/test_runtime.py、tests/test_commands.py |

辅助 fakes、fixtures、conftest 与已退役测试共同删除；不再保留可启动旧 scheduler/生产探针的默认测试入口。

# References
- 本组冻结 inputs/design.md、goal.md。
- Git 基线及新 tests/。
