# 开发验证与联合验收边界

本轮执行编译、静态检查和不干扰共享服务的本地单元测试。单元测试通过可注入 fake runtime、Git/PR、WF/MCP 与子进程接口验证行为；没有启动新 Goal 引擎进程、MCP server、真实模型、集成/E2E 或生产部署。最终命令输出与依赖 HEAD 记录在本组 WF delivery.md。

## 需求覆盖

| 需求 | 实现 | 已提供开发检查 |
|---|---|---|
| MCP 控制面、每 Goal 一个 LangGraph | service.py、cli.py、engine.py | test_control.py 注册/登记/进程锁；test_engine.py 图节点与调度 |
| Goal 多 repo、DD 单 repo/SPEC/PR | protocol.py、engine.py、git_ops.py | test_protocol.py、test_git_ops.py handoff/SPEC/PR；test_engine.py 派单与多 repo |
| 单 Goal 串行、DD 并行、一请求一 prompt | engine.py、store.py | requests_remain_distinct_one_goal_call_many_dds；队列等待与状态控制 |
| Stop 动作 List、部分失败、reply | protocol.py、engine.py | 列表组合约束、action_failure_becomes_separate_goal_request、mailbox_reply_replay_emits_one_delivery |
| Impl→验收→CR→FR→Goal→合并 | engine.py | baseline、changed_head、Goal 版本变化、review 与 merge 重放 |
| 返工复用身份、needs_goal 保留 | engine.py | impl_needs_goal_does_not_require_clean_handoff、revise_action_replay_does_not_increment_input_twice |
| 代码变更重验重审、非 FF 不等于冲突 | engine.py、git_ops.py | changed_head_invalidates_all_review_evidence、merge_non_ff_requires_review_after_updating_source、merge_conflicts_preserved_for_impl |
| 程序合并、目标竞争、逐 repo 才 done | git_ops.py、engine.py | exact lease、merge 恢复、多 repo 已完成项复核与部分失败重试 |
| stop/resume、目标版本、崩溃恢复 | service.py、commands.py、engine.py、runtime.py | stop 不启动步骤、进程身份、启动不明保守处理、版本 CAS、事务折叠回滚 |
| L0 完整、可翻页、工件路径边界 | store.py、service.py、runtime.py、ports.py | 原始 Session 页、工件字节与越界、WF 正文完整分页与 revision |
| 异步只读 L1、不阻塞 DD | engine.py、runtime.py、fleet-scribe harness | 书记员只读配置、失败隔离与游标；runtime recipe 权限检查 |
| runtime schema/session/hooks/compact | 本组 agent-runtime、runtime.py | 通用数组 schema、系统 prompt、harness、resume/fresh、完整归档摘要压缩、停止与历史分页 |
| 退役旧重复流程与测试审计 | 新唯一 CLI、删除旧源模块/服务/探针 | docs/test-migration.md 逐文件登记；make verify 只含新协议测试 |

## 仍须联合运行验证

1. 用真实 agent-runtime、模型网关与 MCP 完成从 WF enroll 到多 DD、审单、逐 repo 收尾的完整闭环；核对 PR 和 Git 平台最终状态。
2. 在 A 审单期间同时到达 B 审单与外部消息，核实顺序独立调用、当前 prompt、reply 的关联与真实接收者读取。
3. 验收失败、CR/FR fail、Goal reject、冲突及 target 竞争反复返工，核对同 DD/SPEC/PR、版本与批准失效。
4. 启动、结果采集、PR 创建、target push、清理、reply 投递各副作用边界注入崩溃，再 resume，核对无重复副作用。
5. graceful/immediate stop 与同时到达消息、steer、在途验收/agent；检查全部子进程、代码保留、恢复后正确续接。
6. 多 repo 部分成功后失败再恢复，检查已经成功项未误重复、后来 target/source 漂移被识别，全部成功才 done。
7. 真实 Session 的模型输出、工具调用、Stop、resume 与 compact 前后完整分页；确认归档链无丢失、系统说明正确且当前 prompt 不重复注入历史。
8. 书记员退出、延迟、失败及恢复，确认 DD 不被阻塞，L1 证据指向完整 L0，查询不需要数据库物理挂载到 WF。
9. GitHub/GitLab 两个平台认证、目标保护规则、远端 CAS 与 PR 自动/显式关闭行为；验证清理不删除新版本或未终结工作。
10. 端口、服务生命周期、资源用量、长期事件量及生产迁移方案。部署权限与正式目标分支另行安排，本次没有部署。

`scripts/joint_validation.py` 提供一个真实 MCP 目标的可执行驱动，其余场景须按此清单安排故障注入与平台核对。不能把该脚本本身存在或单元测试全绿称为 E2E 已通过。

# References
- 本组 WF goal.md、inputs/design.md §12、inputs/protocol.md P1～P8。
- `tests/`、`scripts/joint_validation.py`、`docs/test-migration.md`。
