# dd 控制面 implement 阶段模型回落 pro

## 目标

监督面已裁决（wf-9b5931 goal.md 2026-09-02 18:5x「🔴 更正」+ 17:00 APPROVE）：
停用 `--stage-model implement=deepseek-v4-flash` 覆盖，让 implement 阶段回落到
`implementer` 角色的出厂座位 `deepseek-v4-pro@opencode/gw`。

真因（权威源 = New API 网关 logs 表，请求级，非 dd attempt 账）：deepseek-v4-flash
首 token 延迟过高（TTFT P90 67.7s / P99 444.7s，对比 pro P90 4.5s / P99 11.9s），
一次 implement attempt 的期望等待远超 3600s/7200s/9000s 运行栅，导致 implement 阶段
持续 PROVIDER_UNAVAILABLE。两条腿请求成功率均 ~100%，问题是延迟不是失败——故不修
「换腿 fallback」，只撤掉这个慢腿覆盖。

## 改动

仅改 `deploy/systemd/fleet-graph-dd-mcp.service`：

1. 删除 ExecStart 里的 `    --stage-model implement=deepseek-v4-flash \` 一行。
2. 同步更新上方注释（当前写「flash=写码、pro=智能。implement 写码走
   deepseek-v4-flash」），改为：implement 回落 role registry 出厂座位
   `deepseek-v4-pro`（经 agent-run chain 解析到网关腿），两段 review 保持
   `deepseek-v4-pro`。

两段 review 的 `--stage-model continuous_review=deepseek-v4-pro` 与
`--stage-model final_review=deepseek-v4-pro` 保持不动。

## 边界（硬线）

- 不改任何 Python/产品代码，不新增角色，不动 dd serve 的其余参数。
- 不动 `profiles/roles/implementer.yaml` 的出厂 selector
  （`deepseek-v4-pro@opencode/gw`）——那是回落目标，保持不变。
- 不涉及部署：把新 unit 模板落到 `~/.config/systemd/user/` 并 restart
  `fleet-graph-dd-mcp.service` 属监督面部署动作，不在本单范围。

## 判据（机器可判）

① `deploy/systemd/fleet-graph-dd-mcp.service` 全文不再含
   `--stage-model implement=deepseek-v4-flash`；
② `continuous_review=deepseek-v4-pro` 与 `final_review=deepseek-v4-pro` 两行仍在；
③ `uv run pytest -q tests/test_deploy_unit.py` 通过（unit 仍是合法 systemd 单元）。

```dd-acceptance
bash -c '! grep -q -- "--stage-model implement=deepseek-v4-flash" deploy/systemd/fleet-graph-dd-mcp.service'
bash -c 'grep -q -- "continuous_review=deepseek-v4-pro" deploy/systemd/fleet-graph-dd-mcp.service'
bash -c 'grep -q -- "final_review=deepseek-v4-pro" deploy/systemd/fleet-graph-dd-mcp.service'
uv run pytest -q tests/test_deploy_unit.py
```
