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
| L4 replay 验证新顺序封存前缀；先询外部效果再决定 collect/reuse/rework/unknown；不丢 dirty、不重建 PR、不 reset 分支、不重发效果；legacy 契约解释或安全拒绝；validity 证据缺失/损坏/不可测时产生可追踪安全拒绝（非静默 `return False`） | `src/fleet_graph/graphs/dd_replay.py`（每个 receipt 的 digest 链 + ancestor + product-drift 封闭；`_prepare` 在 HEAD≠tip 时 fail-closed 拒绝、绝不 reset/trim，dirty/上方效果保留；`_clear_stale_run_config_residue` 仅清除 controller 所有的 run-config 残留、其余交 reserved-path guard 拒绝；`_acceptance_context_changed` 上下文变更失效审查；`_sealed_validity`/`_validity_allows` 按 L3 validity key 重验、使受影响 stage 失效重跑，并对 `VALIDITY_EVIDENCE_MISSING`/`VALIDITY_EVIDENCE_CORRUPT`/`VALIDITY_FACTS_UNMEASURABLE` 记录可追踪拒绝并写入 observe/raw-event 边界）；`src/fleet_graph/dd/lifecycle.py`（prefix/未知→fault） | `tests/test_dd_replay.py::TestReplayFailsClosed`（`test_product_drift_above_the_tip_refuses_the_whole_replay`、`test_an_uncommitted_product_change_refuses_the_trim_and_is_preserved`）、`TestAChangedValidityKeyInvalidatesTheSealedStages`、`TestAReviewedChainContinuesThroughItsReviews::test_a_reconfigured_context_invalidates_the_sealed_reviews`、`TestAMissingOrCorruptedValidityKeyRefusesReplay`、`TestAStaleRunConfigResidueAtTheReplayTipIsRemoved`；`tests/test_dd_lifecycle_contract.py::TestReplayPrefix`、`TestLegacyContractsAreExplainedOrRefused`、`TestValidityBindingIsWired::test_replay_records_a_traceable_refusal_when_validity_evidence_is_lost` |
| L5 Goal gate 绑定同一已验收已审查版本与完整 validity key；缺 FR/反馈/版本一致不批准；过期 verdict 无效；合法 reject 走共同返工 | `src/fleet_graph/graphs/dd_runner.py`（gate stage）；`src/fleet_graph/graphs/dd_gate.py::_validity_binding`（measure+bind+verify 入 decision 文件，`previous` sealed key 对照而非自比较，`expired` 判定 → `CODE_EXPIRED_VERDICT`）；`src/fleet_graph/dd/validity_binding.py::binding_key_from_fields`；`src/fleet_graph/dd/control_plane.py::classify_failure`（GATE_REJECTED→rework） | `tests/test_dd_lifecycle_contract.py::TestGoalGateBinding`、`TestValidityBindingIsWired`（`test_the_gate_verifies_against_the_sealed_key_and_flags_expiry`、`test_a_bookkeeping_only_advance_does_not_expire_the_verdict`）；`tests/test_m2_dd_gate_delivery.py::test_a_drifted_version_refuses_the_gate_as_expired` |
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
- 产品候选 commit（本交付覆盖的产品内容精确 HEAD，已存在、可验证的完整对象 ID）:
  `b688d698844f7640b46958ad5fea509f5ba12bf4`
  （本次返工针对 rf-bfe89f96 两个 major 的真实产品改动：L3 完整 validity key——
  未知 target/PR 拒绝封存；L4/L5 replay——validity 证据缺失/损坏/不可测的可追踪
  安全拒绝。覆盖 `src/fleet_graph/dd/dispatch.py`、
  `src/fleet_graph/graphs/{dd_materializer,dd_replay,dd_runner}.py`、
  `tests/test_dd_lifecycle_contract.py`、`tests/test_dd_materializer.py`、
  `tests/test_dd_runner.py`、`tests/test_rework_contract.py`）。
- 本 `delivery.md` 与产品候选的关系：本文件是纯文档提交，落在产品候选
  `b688d698…` 之上——`git merge-base --is-ancestor b688d698… <最终HEAD>` 成立，
  且 `git diff --name-only b688d698… <最终HEAD> -- . ':(exclude).dev-dispatch'`
  仅含 `delivery.md`（`b688d698…` 之后的最终 HEAD 无任何 `src/`/`tests/` 改动）。
- 产品变更沿链关系（机械证据）：`b688d698…` 直接落在 rf-bfe89f96 返工输入
  `c5c1a7e2…` 之后（`git merge-base --is-ancestor c5c1a7e2… b688d698…` 成立），
  中间仅本次 8 个 `src/`/`tests/` 文件改动；再上溯，
  `git diff --name-only ac67c859… b688d698… -- . ':(exclude).dev-dispatch'`
  仅含本次返工的 `src/`/`tests/` 文件与 `delivery.md`，即 `ac67c859…` 之后的
  `.dev-dispatch/**` 记账提交不含产品代码变更。
- 最终交付 HEAD（`work_head_commit`，即携带本文件定稿的 commit 对象 ID）由
  controller 在 Implement handoff receipt（`.dev-dispatch`）内机械封存于本文档
  之外；本文件不得把文档自引用 SHA 冒充产品 SHA。
- 输入 commit（本次 attempt 的精确起点，generation 4 重新 configure 后的基线）:
  `4ae34b73e572bc408072ae959ebef9365ef5c7a8`
- generation 4 说明：本 attempt 重新 configure（feedback index 归档至 `history.json`
  并清空、`run-config.json` generation 2→4），SPEC 与产品代码均未变，产品候选仍为
  `b688d698…`；本 attempt 仅重验开发阶段 acceptance（`compileall` 与
  `pytest tests/test_dd_lifecycle_contract.py`）并刷新交付身份，不新增 `src/`/
  `tests/` 改动。
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