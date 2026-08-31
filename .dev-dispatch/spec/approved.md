# U3 舰队入口总表

## Scope

仅修改 `docs/operating.md`，新增标题明确为「舰队入口总表」的章节。按 wf-c106b9 `goal.md` U3 节与 `findings.md`「① 统一入口——现状盘点（六类）」登记现存入口和归宿。表格每行必须包含以下五列：入口、协议/端点、准入者、归宿（统一|保留）、理由。

六类入口缺一不可，条目名称必须逐字包含以下机械验收标记：

1. `goal 提交面`
2. `dd 派单面`
3. `放行入口`
4. `裁决入口`
5. `收割入口`
6. `带外手工入口`

## Required content

- goal 提交面：协议/端点写明 `:5611` 与 `goal_enroll`；准入者为任何 agent；归宿为统一；理由说明它是唯一对外提交入口。
- dd 派单面：协议/端点写明 `:5610` 与 `development_*`；准入者限定 roster 线与监督面；归宿为保留；理由说明它是内部执行入口且当前健康。
- 放行入口：协议/端点写明 `roster PR`、`release`、`restart`；准入者为监督面；归宿为保留；理由说明放行权不下放，U2 后 queue admitted 留 decision 指针。
- 裁决入口：协议/端点写明 `board question` 到 `work.decision.v1`；准入者为有权作出人类裁决者/监督面；归宿为保留；理由说明治理裁决路径不变，并如实注明 decision-bridge 缺陷已由 wf-216dc3 另案处理，桥修复后 E7 goal 直写退役。
- 收割入口：协议/端点写明 `supervisor harvest` 与 `allowlist`；准入者为监督面；归宿为保留；理由说明 allowlist 已激活且仍由监督面管控。
- 带外手工入口：协议/端点覆盖手工 token 铸造、手工 roster 直编、spec 挂他卷、E7 goal 直写；准入者写明监督面/历史带外操作者；归宿为统一；理由说明 U2 后提交一律收敛到 goal 提交面，但 token 铸造显式保留为放行 SOP 步骤并在提交期由闸 6 校验；不得把所有带外动作笼统保留。

文档应清楚区分「统一提交」与「保留点火/执行/裁决/收割」，不得改 scheduler、roster、代码、unit 或其他文档。

## Frozen acceptance

在 development acceptance 中原样、可复现执行并回显：

```dd-acceptance
make verify
bash -lc 'set -euo pipefail; f=docs/operating.md; grep -F -- "舰队入口总表" "$f"; for entry in "goal 提交面" "dd 派单面" "放行入口" "裁决入口" "收割入口" "带外手工入口"; do grep -F -- "$entry" "$f"; done'
```

成功条件：两条命令均退出 0，且第二条回显章节标题和六类入口命中行。验收判据冻结，不得替换、删减或放宽。