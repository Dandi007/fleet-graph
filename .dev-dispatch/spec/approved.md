# ⑯ spec-d16 — 引擎韧性：egress 失败分层与 stage 产物存活（定稿）

- 状态：**定稿 2026-09-04T05:25Z**（草案 2026-09-04T04:50Z；本轮依三采样探测史与 05:16Z 双复活实测收紧，下列交付面与红靶均为约束性条款）
- 实现专用 worktree：`/data/worktrees/fleet-graph-wf-8d9737-d16-egress-resilience-20260904`（detached @ `7f20b340a69bb8e2ed29964c9abff5a54419cd09` = origin/release/wf-8d9737 头，与远端 ls-remote 亲证一致；建库时 clean）
- 背景：2026-09-04 ~02:29–03:14Z github egress 事故窗全线冻结（board seq 2818 解除并立派单前置探测协议）；本线三采样：04:48:25Z / 04:58:25Z 读探 exit=128（GnuTLS TLS 握手非正常终止，逐字同文），05:08:25–05:09:17Z 6/6 读写双绿，随后 05:16Z ⑮-b g4 / ⑫-b g2 configure-push 过墙（6220e33f / b484fb69）——**egress 间歇抖动、绿窗可用但不可依赖**，引擎必须把传输层失败建模为一等公民，而非让它冒充业务失败终结整单。证据链：`preflight-probe-evidence-20260904T0448Z.md` / `…-round2-…T0458Z.md` / `…-round3-…T0508Z.md`。

## 交付面（五条，验收必达）

1. **远端探测指数退避重试**：所有触达远端的 git 操作（ls-remote / fetch / push）包一层重试。约束值：base 2s、factor 2、单次上限 60s、每 stage 最多 5 次尝试、加 ±20% 抖动、退避总时长不超过所在 stage 的 run fence。仅传输类失败触发重试；每次尝试落一条证据行 `{attempt, at, exit, stderr_tail}`（与 seq 2818 探针协议同构，可直接入卷）。
2. **传输层与业务层失败分离处置**：传输类触发枚举闭集——DNS 解析失败、TCP 连接失败/重置、TLS 握手失败（含 GnuTLS `The TLS connection was non-properly terminated`）、HTTP 5xx、超时（exit 124 家族）。传输类**自身永不构成 terminal**：重试耗尽后落为 retryable fault 并保留续跑权。业务类（验收不通过、评审否决、scope 拒收）维持既有 terminal / 治理语义不变。
3. **失败码分层**：`PROVIDER_UNAVAILABLE` 细分为可区分的三类根因——`transport`（网络 egress）/ `execution`（命令已运行但执行环境失败）/ `business`（业务语义拒绝），结构化码 + 处置映射：transport → 退避重试；execution → R1-c reconfigure 通道；business → 治理/人闸。旧平铺码保留为 transport 的合法别名（存量事件兼容）；events.jsonl 与 status.json 的 failure 结构必须携带 `root_cause` 字段。
4. **git 段失败区分传输层与仓库层**：分类器将 git stderr/exit 映射为 `egress_transport` / `repo_rejected` / `repo_conflict` 三类。本日实测样本入 fixture：`fatal: unable to access '…': GnuTLS, handshake failed: The TLS connection was non-properly terminated.` → egress_transport；`! [remote rejected]` → repo_rejected；`non-fast-forward / fetch first` → repo_conflict。仅仓库层结局可终结整单；冲突走既有 rebase/重试逻辑；传输层进入第 1 条退避重试。
5. **fault 不丢弃已成功 stage 产物、可从最后成功 stage 续跑**：发生 fault 时 checkpoint 完整保留已成功（sealed/succeeded）stage 的产物与回执（events.jsonl 不截断、stages/ 不清理）；恢复从第一个非成功 stage 进入，任何已成功 stage 零重跑；与 generation resume 语义对齐（同 thread id 续跑，不 fork 历史）。

## 红靶（五条可断言条款）

注入点：development setup / 远端探测时点注入**恰好一次** ls-remote 传输层失败（fixture 用第 4 条 GnuTLS 逐字样本）：

1. 整单**不**进入 terminal fault（最终态非 fault/failed 终态）；
2. 该 stage 观察到 ≥2 次尝试，且相邻尝试间隔呈指数退避（2s、4s… 量级，容差 ±50%）；
3. 重试后运行继续并完成（到 awaiting_gate 或 complete）；
4. 失败记录携带 `root_cause=transport`（第 3 条分层码可读）；
5.（对应交付面 5）人为 fault 后恢复：已成功 stage 不重跑——事件序列中不出现重复 output_commit 的 success 行。

## Non-Goals 与安全

- 不引入新的轮询循环；重试在既有 stage 执行路径内完成。
- 不改 dd 单元环境契约（proxy 变量保持 unset；不为"修好 egress"私加代理）。
- 不触碰生产主 checkout；全部 git 操作在专用 worktree 内。

## dd-acceptance

```dd-acceptance
uv sync --frozen
make verify
uv run pytest tests/test_egress_resilience.py -q -k 'backoff_retry or transport_not_terminal or failure_code_layering or artifact_survival'
```

测试文件组织可由实现侧调整，但红靶断言 1–5 为约束性条款，逐条可机械验证。