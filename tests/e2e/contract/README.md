# Docker E2E 公共契约 v1

本目录是与实现独立的固定输入、公共证据格式和验收器。输入任务见 `goal.md`；只把 `fixture/` 内容复制到候选可写的 Git 仓库。`verifier.py`、schema、harness 与证据目录不得挂载到 agent 的可写路径。

Runner 通过公开 MCP 查询 status、完整 events、Session 与 artifact，并通过 GitHub 查询 PR。原始返回值保存在 `bundle/raw/`。适配器只能重命名字段、选择已有记录；不得补出成功状态、commit、Session、review_ref。无法观察的字段应缺失，让验收失败并报告证据缺口。公开字段不足属于可观察性缺口，不能用内部 SQLite 或私有 graph 状态补齐。

`manifest.json` 由可信 harness 建立，冻结 run_id、候选及其依赖源码的完整 commit、fixture、测试 repo、seed commit、goal_id、source/target。它不是候选输出。`snapshot.json` 满足 `schema.json`：每条 record 含 `values` 和 `sources`，每一个值都带 `{file,pointer}` 原始 JSON Pointer；验收器逐字段比对，禁止转换值。两份输入通过 JSON Schema 验证。

| 记录 | 必填 values |
|---|---|
| goal | goal_id、status、commit、pr_url、done_seq |
| dds | dd_id、commit、spec_commit、spec_path、pr_url |
| runs | run_id、role、status、session_id；Impl/CR/FR 另需 dd_id |
| reviews | dd_id、role、commit、verdict、run_id；FR 另需 review_ref |
| acceptances | dd_id、commit、status、run_id、workspace、results（原始命令记录，逐条检查 exit_code=0） |
| approvals | dd_id、commit、review_ref、goal_run_id、decision、applied_review_ref |
| prs | url、base、head、head_sha、merge_sha、state、repository |

events 是指向完整原始事件数组的引用。Runner 必须从起始游标读到日志末尾，保留空页与 next 的推进，不能只存最后一页。role 使用 goal/impl/cr/fr/scribe。成功值可为 pass/passed/succeeded/success/done/approved，原始值仍须保留；Goal 最终必须为 done，PR 必须为 merged（大小写不限）。此词表只解决公共表示差异，不允许把运行结束当业务通过。

events 必须从 0 或 1 开始连续，汇总与原始分页逐条一致；本组公开 API 的 seq 从 1 开始。Session 每页必须绑定同一 run_id，total 与实际项数一致、next_offset 连续且末页为 null。批准证据必须来自成功 Goal run 的真实 Stop approve 动作，且 review_ref 已应用到当前 DD；只有 DD.approved 字段不能单独充当 Goal 审单证据。

功能验证的 17 个用例由可信父进程逐例提交并核对实际返回类型、值和 TypeError。待测子进程只返回当前请求的数据，不负责声明检查是否通过或执行了多少项；CLI 语义另行核对。

Scribe 必须有终局成功证据：run.intent 的原始 prompt.final=true，event_range 覆盖 Goal done_seq，并有之后同一 run_id 的 scribe.observed 与非空 observations。适配器把这些原始字段映射到 Scribe run 的 final、event_range、observed_seq、observed_run_id、observations。早期 Scribe 成功不能替代终局失败或缺失的观察。

验收检查角色成功 Session、同 DD 的 CR/FR/程序验收/Goal approval、review_ref 与完整 commit 绑定、先 SPEC 后实现、DD/整线真实 PR 的 source/target 与 Git ancestry、最终 tree 无未审查改动，以及外部运行固定功能用例。程序验收还直接核对 `raw/artifacts/commands/RUN/input.json` 与 `result.json`：命令必须等于登记的固定 make verify，workspace、状态、结果、日志引用与同一个 run 一致。任何 `raw/collection-errors.json` 记录都使验收失败。最终 repo 必须由可信 harness 从 target checkout；不得由候选选一个工作树冒充交付。

```sh
python tests/e2e/contract/verifier.py --bundle /artifacts/RUN --repo /workspace/fixture --output /verification/verification.json
python -m unittest discover -s tests/e2e/contract -p 'test_*.py' -v
```

验收器需要 Python、git、jsonschema；它应运行在独立 Docker 容器，repo 与 bundle 只读挂载，无 GitHub/model 凭证、无网络，只有报告输出可写。待测功能在 verifier 内执行，持有写入凭证的 runner 负责采集公开证据与准备 checkout。报告统一包含 status、checks、evidence、run_id、candidate commits。返回 0 仅表示所有固定检查通过；失败返回 1，不能以 Goal done 替代。

单测的 `unit.synthetic` 事件和本地 Git 历史是拒绝条件测试用例，不能计入真实运行。公共 v1 只覆盖单 repo 正常交付，不宣称覆盖冲突、崩溃恢复、多 repo 或持续监督。

`raw/runtime-bus.json` 保存公共 agent-bus lifecycle 原始消息与所有分页。外部验收器要求真实成功 Goal/Impl run_id 的 started/exited 配对，普通探针消息或其他 run 的上报均不计入通过。

# References

- work folder `wf-53a584`，`inputs/protocol.md` §6–9、`delivery.md`，经 MCP 只读读取。
- 本组公开 API：`/data/code/fleet-comparison/codex/fleet-graph/src/fleet_graph/service.py`（self repo）。
