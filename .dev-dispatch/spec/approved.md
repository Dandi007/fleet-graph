# M3 e2e 第二轮根因修复——harvest 解析 canonical 目标仓 spec

- 目标仓：`/data/code/self/fleet-graph`（本 development 在 `/data/worktrees/` 下独立 worktree）。
- 归属：监督面 04:1x 立案（见 goal.md 顶部监督面答复），M3 e2e 第二轮根因。旁证：e5-dev-fg-1338894333b3 亦同因被拒（已由监督面手工收割）。
- 类别：缺陷修复（canonical 目标仓解析缺失），**不改判据（goal.md 验收断言）**、不改 E1–E8 词表、不改 deliver allowlist 配置文件本体。
- 依赖：main@a53f7e3（#201）已含 harvest 反应器真 forge + evidence 真实板卡实体 + E5 观察器接线与写权旗标透传。

## 根因（已实读，非推断）

真机原始回执 `reports/e5-dev-fg-94276bd735f0.json`：granted=false，理由
`repo_path '/data/worktrees/fleet-harvest-sandbox-m3-e2e-real' 不在收割写白名单（默认 deny-all）`。

代码位置 `src/fleet_graph/supervise/harvest.py::_resolve_repo`（L155-170）直接把 dd 准入
record 的 `repo_path` 当作目标仓返回；而 `repo_path` 是**每单一次的 linked worktree**
（`/data/worktrees/...`），allowlist 却按 **canonical 仓**（`/data/code/self/fleet-harvest-sandbox`）
签发，于是 `gate`/`authorize_harvest_write` 恒 deny。worktree 路径 ephemeral，**绝不进白名单**
（那是 hack）；正确修复是让 harvest 解析出 canonical 目标仓再授权与写。

旁证（已实读，同一根因）：该 worktree 是 canonical 仓的 linked worktree——
`git -C /data/worktrees/fleet-harvest-sandbox-m3-e2e-real rev-parse --git-common-dir`
= `/data/code/self/fleet-harvest-sandbox/.git`；其 `remote get-url origin`
= `https://github.com/Dandi007/fleet-harvest-sandbox.git`（与 canonical 仓 origin 一致）。

## 交付 A：canonical 目标仓解析（新，机械、不猜）

新增一处解析口，从 record `repo_path` 解析出 canonical 目标仓绝对路径。git 读取全部走
`supervise/harvest_ops.py`（ops 机械层，conformance Guard D 豁免）——在 `HarvestOps`
协议与 `DefaultHarvestOps` 增一个读口（如 `resolve_canonical_repo(record_repo_path,
record_remote_url, allowlist_repo_paths) -> Path | None`），**不在 `harvest.py` 编排层
新增任何命中 `HARVEST_WRITE_PRIMITIVES`（git/run_git/subprocess/worktree 等）的裸调用**；
`_resolve_repo` 仅追加读取 record 的 `remote_url` 字段（纯 JSON 读，Guard D 安全）。

解析顺序（优先级从高到低，任一命中即返回，全程机械判定）：

1. **直接命中**：`record_repo_path`（规范化绝对路径）本身是目录且等于某 allowlist 条目的
   `repo_path` → canonical = 它（保留「record 已指向 canonical」的既有正确行为）。
2. **linked worktree 归属**：`git -C <record_repo_path> rev-parse --git-common-dir` 若不等
   `<record_repo_path>/.git`（即该路径是被 canonical 仓注册的 linked worktree），则 common-dir
   指向 `<canonical>/.git`，剥尾段 `.git` 得 canonical 主 checkout；若该目录存在且命中
   allowlist → canonical = 它。
3. **origin 本地路径**：`record_remote_url`（或缺失时 `git -C <record_repo_path> remote get-url
   origin`）是本地绝对路径且该目录命中 allowlist → canonical = 它。
4. **origin URL 映射**：`record_remote_url` 是 forge URL → 对 allowlist 每个 entry 的
   `repo_path`（目录存在）读其 `git remote get-url origin`，**精确字符串匹配**命中者 →
   canonical = 该 entry.repo_path。

解析不到可命中 allowlist 的 canonical → 返回 `None` + 机器可读留痕理由（如
`record repo_path 无法解析到任何白名单 canonical 仓`），交由 intake/gate 走既有
refused/escalated 路径，绝不静默放行、绝不 fallback 到 worktree 路径本身去授权。

## 交付 B：intake / 授权 / 全部写步一律作用于 canonical

`harvest.py`：

1. `_resolve_repo` 改为「读 record.repo_path(原始 worktree) + record.remote_url」并把
   canonical 解析委托给 ops 读口；返回 `(canonical_path | None, gaps)`。
2. `intake` 把 `state["repo_path"]` 存 **canonical**（解析失败存空 + gaps → escalated）。
3. `gate` 与逐写步骤的 `authorize_harvest_write`、以及 fetch dd ref / cherry-pick / worktree
   cherry-pick / push / forge PR / deploy 的 `repo` 目标，全部络 `state["repo_path"]`（canonical），
   **绝不**使用 record 的原始 worktree 路径作为授权对象或写目标（生成-验证分离不变）。

## 交付 C：阴性保留 + conformance + 测试

1. **阴性必须保留（不拉宽 deny-all）**：
   - worktree 路径本身（回归：若直接拿 record 原始路径授权）→ 仍 deny；
   - 解析出的 canonical 仓不在 allowlist → 仍 deny（authorize 语义不变）；
   - 解析不到任何 canonical → escalated/refused 并留痕。
2. conformance（`scripts/check_supervisor_conformance.py`）不回归：Guard D 仍要求 harvest
   编排层每个含写原语的函数先过 allowlist gate；新增解析读口落在 ops 层（豁免），编排层不新增
   裸 git/subprocess。
3. 测试（合成 fake ops + tmp git 仓，禁触真网/生产 checkout）：
   - linked worktree：真实 `git worktree add` 一个 detached worktree → 断言 `_resolve_repo`
     解析成 canonical 主 checkout、授权 granted=True。
   - origin URL 映射：canonical 仓与 worktree 共享同一 origin URL → 断言命中 canonical。
   - 阴性：非白名单 canonical 仓、无 origin、worktree 路径本身 → granted=False / refusal。
   - `make verify` 全绿；`tests/test_harvest.py` / `tests/test_harvest_allowlist.py` 既有用例零回归。

## 可复现验收

```dd-acceptance
make verify
```

## 铁律

- 只改 harvest 的 canonical 解析（`harvest.py` 编排 + `harvest_ops.py` 读口）+ 对应测试；
  不触碰 `/data/fleet-graph/supervisor/harvest-allowlist.json`、不改 deny-all 语义、不改判据。
- 一切改动走本 development worktree + PR，不直改 main；生产主 checkout 仅读。
- 修复落地（合入+release+重启）后，由监督面再造一张沙箱低风险单重跑 e2e；本单只交付修复。