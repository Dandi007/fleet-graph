# 收割 squash-merge 提交溯源 trailer 机制（用户拍板 2026-09-02 12:4x）

## Goal

收割反应器 `pr_squash_merge`（`supervise/harvest_ops.py`）用 `gh pr merge --squash`
把产品 commit 压进默认分支。当前 gh 的 `--squash` 默认拿 PR title/body 当压出来的
commit message，没有稳定携带「这次合入来自哪个 development、从哪个 durable ref
HEAD 压的、以哪次板上裁决为准」的机器可核溯源。用户拍板把 squash merge 做成机制：

- 压出来的 squash commit message 的**叙事半**（subject/正文）由 LLM 写；
- **trailer 半**一律机械生成（不依赖 LLM 输出）；
- trailer 作为 `pr_squash_merge` 之前的一个独立节点产出，merge 写前闸校验。

本 spec 只写「要什么」与「凭什么判红」，不含实现与任何解决过程。

## 要什么：溯源 trailer + 非空 subject

squash 合入后默认分支上新增的那个 squash commit 的 commit message 必须满足：

1. 含 `Development-Id: <development_id>` trailer，`<development_id>` == 该
   development `record.json` 的 `development_id`；
2. 含 `Squashed-From: <full 40-hex>` trailer，`<full 40-hex>` == 该 development 的
   durable ref `refs/heads/dd/<development_id>` 的 HEAD 完整 sha（`git rev-parse
   --verify refs/heads/dd/<development_id>` 的值）；
3. commit subject（首个非空标题行）非空，且不得因溯源失败而缺失。

## trailer 取值源（机械事实，严禁从 LLM 输出解析）

trailer 取值只能来自结构化产物，酌情取用其一或组合：

- `record.json`（development_id 字段 — Development-Id 的唯一来源）；
- `acceptance.json` / review receipt（验收与评审回执）；
- 板上 `work.decision.v1` 的 message_id（人类裁决溯源，如 `Decision-Id` 类
  trailer，若有）。

任何从 LLM 输出里解析/注入的 trailer 值都不得通过判据——telemetry/溯源可以缺、
不可以撒。LLM 的职责被限定在「叙事半」（subject/正文），trailer 半恒机械。

## 节点位置与降级

- 新节点在 harvest 子图 `pr_squash_merge` 之前：先机械取 trailer 值 + 让 LLM 写
  叙事，再组装 commit message，再做 merge 写前闸。
- LLM 不可用/抛错 → 降级为模板叙事（subject 仍非空），trailer 照常完整生成，
  合并仍发生（fail-open，不因 LLM 挂而拒绝合入或丢溯源）。

## 验收标准（逐条译成判红）

### 阳性
产出 squash commit message：`Development-Id: <dev_id>` 存在且 == record.development_id，
且 `Squashed-From: <sha>` 存在且 == durable ref HEAD 的完整 40-hex（可 `git show
-s --format=%B <squash_commit>` 查）。未实现（gh 默认 title 无 trailer / 值不符）必红。

### 阴性①（取值源换成「从 LLM 输出解析」必红）
存在 pin 测试：把 trailer 值的取值源由机械事实换成「从 LLM 输出解析」→ 断言必红。
trailer 值只认机械事实，凡由 LLM 输出注入/替代的 trailer 值不得通过判据。

### 阴性②（缺 Development-Id 写前闸必须拦截、不得合并）
构造缺 `Development-Id` trailer 的 commit/PR → merge 写前闸必须 refuse（不执行
`gh pr merge`、不落 squash commit、不把无溯源 commit 写进默认分支）。未实现必红。

### 阴性③（LLM 不可用仍合：trailer 完整 + subject 非空 + 合并发生）
伪造 LLM/subject 生成失败（不可用/抛错）→ 两条 trailer 仍完整且值正确（不依赖
LLM）、subject 非空（模板兜底）、合并仍发生。未实现（LLM 挂导致 trailer 缺失或整单
拒绝合并）必红。

## 判据锚点（利于实现交付测试钉住，非解决方案）

- 机械事实源：record 的 `development_id` + `git rev-parse --verify
  refs/heads/dd/<development_id>`（durable ref HEAD）。
- 写前闸 = merge 之前拦下「溯源不完整」的 commit，refuse 而非合并；与既有
  H7/H8/M3 写前闸语义并列，不互相替代。

```dd-acceptance
uv sync --frozen
make verify
```

## 交付约束

- 只改 `src/fleet_graph/supervise/harvest_ops.py`（+ 如需要 `harvest.py` 与
  `tests/test_harvest.py`）；本 spec 不指定实现路径。
- 不触 `harvest-allowlist.json`、不改 E5/E6/E7 词表、不改 H7/H8/M3 分支占用语义。
- 一切改动走 PR（development worktree），生产主 checkout 仅 ff-only pull，禁
  checkout/switch/reset/detach/建分支/验证。