# Delivery: 版本绑定的线性 DD 生命周期与同身份返工 (L1-L7)

- development_id: `dev-fg-4053bbf02dcf`
- line / work volume: `wf-2bf703` (`fleet-comparison/self`)
- target deliverable: 单 repo Fleet Graph DD 生命周期 L1-L7

## ready_for_joint_validation

本交付是**本组开发阶段**交付：需求到实现到测试的映射、代码、契约与
`tests/test_dd_lifecycle_contract.py` 的合约测试、以及开发阶段验收
（`python3 -m compileall -q src` 与 `python3 -m pytest -q
tests/test_dd_lifecycle_contract.py`）均已完成并记录。

以下四项**尚未执行**，统一留待双方开发完成后进行联合验证：

- 联合运行（joint run）——**未执行**；
- 集成（integration）——**未执行**；
- 部署（deployment）——**未执行**；
- 端到端验收（E2E）——**未执行**。

`ready_for_joint_validation` 不宣称上述任何一项已经通过，也不以“未开放的运行
阶段”为由拒绝合格的开发交付；新引擎进程、真实模型、daemon、共享服务、live
Git/PR 副作用与新引擎部署一律留待联合验证阶段。

## Requirement -> implementation -> test mapping

| 需求 (SPEC) | 实现 | 测试 |
| --- | --- | --- |
| L1 严格线性顺序：Impl -> acceptance -> continuous review -> final review -> Goal gate -> typed merge authorization -> merge；configure/PREPARED 不完成；acceptance 失败阻挡 CR；顺序在 contract/executor/materializer/replay/恢复一致 | `src/fleet_graph/dd/contracts/development-lifecycle.json`（contract_version 3，7 stages、10 transitions）；`src/fleet_graph/dd/lifecycle.py`（由契约派生 spine/binding/terminal）；`src/fleet_graph/graphs/dd_pipeline.py`、`dd_scripts.py`、`dd_runner.py`、`dd_replay.py`（同序执行） | `tests/test_dd_lifecycle_contract.py::TestStrictLinearOrder`、`TestAcceptanceBlocksReview` |
| L2 五类拒绝（acceptance/CR/FR/goal gate/需改码 merge feedback）同身份返工；移除 6 次/40 步业务上限；per-call 超时与不可恢复基础设施错误有界分型 | `src/fleet_graph/graphs/dd_pipeline.py`（unbounded business rework + 技术 backstop）；`src/fleet_graph/graphs/dd_scripts.py`（per-call timeout=exit 124）；`src/fleet_graph/dd/control_plane.py::classify_failure` | `tests/test_dd_lifecycle_contract.py::TestSameIdentityRework`、`TestFiveRejectionKinds` |
| L3 validity key：绑定 product revision/tree、SPEC digest、acceptance-context revision、target identity、PR identity；任一变化失效重验；记账-only 不重审；SPEC 改是新 DD；完整 validity key（target/PR 必须绑定，未知即拒绝封存） | `src/fleet_graph/dd/validity.py`（纯原语）；`src/fleet_graph/dd/validity_binding.py`（真实 git 事实测量与绑定）；接入 `dd/dispatch.py::StageDispatchBuilder.validity_key`（未知 target/PR → `DispatchError`，不封存未知事实）、`dd_materializer.py`（seal validity，target_ref/audit_ref 缺失 → `VALIDITY_BINDING_FAILED`）、`dd_gate.py`（bind+verify 入 sealed decision）、`dd/recovery.py`（validity_digest 入 decision digest） | `tests/test_dd_lifecycle_contract.py::TestValidityKey`、`TestValidityBindingIsWired`（`test_the_dispatch_builder_refuses_to_seal_an_unknown_target_or_pr`、`test_the_materializer_refuses_to_seal_with_an_unknown_target_or_pr`）；`tests/test_dd_materializer.py::TestTheValidityKeyIsPersistedFailClosed::test_a_seal_with_an_unknown_target_or_pr_is_refused` |
| L4 replay 验证新顺序封存前缀；先询外部效果再决定 collect/reuse/rework/unknown；不丢 dirty、不重建 PR、不 reset 分支、不重发效果；legacy 契约解释或安全拒绝；validity 证据缺失/损坏/不可测时产生可追踪安全拒绝（非静默 `return False`） | `src/fleet_graph/graphs/dd_replay.py`（每个 receipt 的 digest 链 + ancestor + product-drift 封闭；`_prepare` 在 HEAD≠tip 时 fail-closed 拒绝、绝不 reset/trim，dirty/上方效果保留；`_clear_stale_run_config_residue` 仅清除 controller 所有的 run-config 残留、其余交 reserved-path guard 拒绝；`_acceptance_context_changed` 上下文变更失效审查；`_sealed_validity`/`_validity_allows` 按 L3 validity key 重验、校验封存 digest 与 fields 一致（`binding_key_from_fields` 被篡改/损坏的 digest→`VALIDITY_EVIDENCE_CORRUPT`）、使受影响 stage 失效重跑，并对 `VALIDITY_EVIDENCE_MISSING`/`VALIDITY_EVIDENCE_CORRUPT`/`VALIDITY_FACTS_UNMEASURABLE` 记录可追踪拒绝并写入 observe/raw-event 边界）；`src/fleet_graph/dd/lifecycle.py`（prefix/未知→fault） | `tests/test_dd_replay.py::TestReplayFailsClosed`（`test_product_drift_above_the_tip_refuses_the_whole_replay`、`test_an_uncommitted_product_change_refuses_the_trim_and_is_preserved`）、`TestAChangedValidityKeyInvalidatesTheSealedStages`、`TestAReviewedChainContinuesThroughItsReviews::test_a_reconfigured_context_invalidates_the_sealed_reviews`、`TestAMissingOrCorruptedValidityKeyRefusesReplay`（含 `test_a_validity_key_whose_digest_was_tampered_is_not_replayed`）、`TestAStaleRunConfigResidueAtTheReplayTipIsRemoved`；`tests/test_dd_lifecycle_contract.py::TestReplayPrefix`、`TestLegacyContractsAreExplainedOrRefused`、`TestValidityBindingIsWired::test_replay_records_a_traceable_refusal_when_validity_evidence_is_lost` |
| L5 Goal gate 绑定同一已验收已审查版本与完整 validity key；缺 FR/反馈/版本一致不批准；过期 verdict 无效；合法 reject 走共同返工 | `src/fleet_graph/graphs/dd_runner.py`（gate stage）；`src/fleet_graph/graphs/dd_gate.py::_validity_binding`（measure+bind+verify 入 decision 文件，`previous` sealed key 对照而非自比较，`expired` 判定 → `CODE_EXPIRED_VERDICT`）；`src/fleet_graph/dd/validity_binding.py::binding_key_from_fields`（重构并校验封存 digest 与 fields 一致，篡改→拒绝）；`src/fleet_graph/dd/control_plane.py::classify_failure`（GATE_REJECTED→rework） | `tests/test_dd_lifecycle_contract.py::TestGoalGateBinding`、`TestValidityBindingIsWired`（`test_the_gate_verifies_against_the_sealed_key_and_flags_expiry`、`test_a_bookkeeping_only_advance_does_not_expire_the_verdict`、`test_the_gate_refuses_a_tampered_previous_digest`、`test_binding_key_from_fields_refuses_a_digest_that_does_not_match`）；`tests/test_m2_dd_gate_delivery.py::test_a_drifted_version_refuses_the_gate_as_expired` |
| L6 typed merge feedback：target 竞争/内容冲突/transport+unknown/already-merged/PREPARED-only/measured MERGED；target 竞争不误报冲突；transport 不改业务 verdict；PREPARED 非成功 | `src/fleet_graph/dd/merge_feedback.py`（分类 + merge_event）；接入 `dd_scripts.py::MergeStage.act`（typed event：MERGED/PREPARED 为 terminal，内容冲突 RAISE→REJECT 返工）；契约 `merger MERGED->complete`、`PREPARED->prepared`、`REJECT->implement`；`dd_pipeline.py`（terminal 类型流经） | `tests/test_dd_lifecycle_contract.py::TestTypedMergeFeedback`、`TestTypedMergeFeedbackIsWiredIntoTheLifecycle` |
| L7 raw-event 边界可靠记录 DD/stage/attempt/validity/opaque runtime refs；journal 写失败不吞错误迁移状态；恢复保留引用；L1 clerk 无批准/阻塞/返工/合并权限 | `src/fleet_graph/graphs/dd_pipeline.py`（history/observe sink 分离 + sealed validity 记入事件）；`src/fleet_graph/graphs/dd_runner.py::persist_event`（events.jsonl 写失败抛 `EventPersistenceError`，不吞） | `tests/test_dd_lifecycle_contract.py::TestRawEventBoundary::test_a_failing_observability_sink_is_not_swallowed`；`tests/test_dd_runner.py::TestTheRunLeavesArtifactsBehind::test_a_failed_event_write_fails_the_run_loudly` |
| 生命周期契约与其 JSON schema 一致（contract_version、transitions 数目与边集合） | `src/fleet_graph/dd/contracts/development-lifecycle.schema.json`（contract_version 3、10 transitions） | `tests/test_dd_lifecycle_contract.py::TestLifecycleSchemaConsistency` |

旧测试的替换与逐项映射（保留身份/receipt 链/操作超时覆盖，删除者均有等价新增）：
`tests/test_dd_pipeline.py`、`tests/test_dd_replay.py`、`tests/test_dd_runner.py`、
`tests/test_dd_scripts.py`、`tests/test_lifecycle.py`、
`tests/test_m3_engine_defects.py`。

## HEAD / PR / dependency config

- baseline commit（本单基线，禁止偏离）: `5a0c392f45a5cf2b66231e61061a27201469edc6`
- 目标交付 ref（唯一交付分支）: `refs/heads/release/fleet-compare-self`
- 开发分支: `dd/fleet-compare-self/dev-fg-4053bbf02dcf`
- 产品代码祖先（本交付产品代码的既有根，已存在、可机械验证）:
  `b688d698844f7640b46958ad5fea509f5ba12bf4`
  （该提交完成 L3 完整 validity key——未知 target/PR 拒绝封存，与 L4/L5 replay/gate
  的 validity 证据缺失/损坏/不可测之可追踪安全拒绝（rf-bfe89f96 FR 的代码改动）。）
- 本次 attempt 的产品代码改动（rework 针对 rf-8d5a8e16 Final review 的 major finding
  ——replay/gate 未校验封存 validity digest，`binding_key_from_fields` 忽略持久化
  的 `digest` 而仅从 `fields` 重算，导致被篡改/损坏的 digest 仍可被 replay/gate
  放行）。本 attempt 使其 fail-closed：`binding_key_from_fields` 现在要求持久化
  `digest` 与 `fields` 重算的 digest 严格相等，否则判为 `VALIDITY_EVIDENCE_CORRUPT`
  可追踪拒绝。改动文件：
  `src/fleet_graph/dd/validity_binding.py`、
  `src/fleet_graph/graphs/dd_replay.py`、`src/fleet_graph/graphs/dd_gate.py`、
  `tests/test_dd_replay.py`、`tests/test_dd_lifecycle_contract.py`。
- 产品代码自 `b688d698…` 起的历史（本 attempt 的机械复验证据）：
  `git diff --name-only b688d698… <work_head_commit> -- . ':(exclude).dev-dispatch'`
  仅含 `delivery.md` 与上述命中 rf-8d5a8e16 的三个 `src/` 文件加两个 `tests/`
  文件；`b688d698…` 之后的提交不涉及其它产品代码。
- 输入 commit（本次 attempt 的精确继承起点 `input_commit`）:
  `1ba9de54cdd8ec88c181a69d475552e36adbf18b`
- 本次 rework 说明：attempt `56fcb138-05d0-5d99-b578-23e64ac7b2c1`（rework mode）
  针对 rf-8d5a8e16 Final review 的 major finding（sealed validity digest 未被
  校验）。SPEC 无改动；`src/`、`tests/` 仅上述 digest 校验相关改动；本 attempt
  重跑开发阶段 acceptance（`compileall` 与
  `pytest tests/test_dd_lifecycle_contract.py`）。
- 最终交付 HEAD（`work_head_commit`）：携带本文件定稿的 commit 对象 ID，其父链以
  input `1ba9de54…` 为继承起点、以产品代码祖先 `b688d698…` 为祖先；本文件不以
  文档自引用 SHA 冒充产品 SHA。该 SHA 由 controller 在 Implement handoff receipt
  （`.dev-dispatch`）内机械封存，交付身份以该封存为准。
- PR：尚未创建（本开发阶段无任何 live PR/远端副作用）；PR 将在 L1「typed merge
  authorization -> merge」阶段对 `release/fleet-compare-self` 创建，其精确
  PR number/URL 届时由 merge 授权记录。
- 运行时：Python 3.11（`requires-python >=3.11,<3.12`）。
- 依赖管理：`uv`；`uv sync --frozen` + `uv.lock`（锁定）；开发树内
  `UV_LINK_MODE=copy`、隔离可写 `UV_CACHE_DIR`、`UV_OFFLINE=1`；
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` 且 `PYTEST_PLUGINS` 为空。
- 运行依赖（`pyproject.toml`）: `langgraph==1.2.11`,
  `langgraph-checkpoint-sqlite==3.1.1`, `httpx==0.28.1`, `fastmcp==3.4.7`。
- 开发依赖（`dependency-groups.dev`）: `jsonschema>=4.26.0`, `pytest==9.1.1`,
  `ruff==0.16.4`。

## 开发阶段 acceptance（已执行并记录）

- `python3 -m compileall -q src`
- `python3 -m pytest -q tests/test_dd_lifecycle_contract.py`