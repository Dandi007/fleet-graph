# 安装与运行指南

以下服务启动与联合验收命令是交付方案，本开发阶段没有执行。运行前须等待双方开发完成并取得联合阶段安排。所有路径指本组副本，禁止改固定 driver、共享 current 或生产 main。

## 依赖与静态开发检查

- Python 3.11、uv；版本由 `pyproject.toml` / `uv.lock` 固定。
- Bun、Git；GitHub 使用 gh，GitLab 使用 glab，平台认证沿各工具配置。
- 本组 agent-runtime `release/fleet-compare-codex`，精确交付 HEAD 见 WF delivery.md。
- 本组 Katana 保持固定基线；WF 通过现有 Work Folder MCP 访问，无物理路径挂载。
- Git 自动 merge 需要 repo 本地 `user.name` / `user.email`；adapter 不继承全局 Git 重写配置。

```bash
cd /data/code/fleet-comparison/codex/agent-runtime
bun install --frozen-lockfile
bun bin/agent-run validate
cd /data/code/fleet-comparison/codex/fleet-graph
uv sync --frozen
make verify
uv run fleet-graph check-config --config config/codex.json
```

`config/codex.json` 显式配置 agent-run、agent-session、角色、模型、harness、系统说明、超时、容量与书记员周期。Goal/Impl/CR/FR 默认 resume，书记员独立 scope。compact 由 runtime 实际归档和摘要再 fresh，原始 Session 链保留；配置支持范围以本组 runtime 文档为准。角色 prompt 文件不能替代 sandbox，书记员使用 exec deny 的只读 harness。

## 启动与停止（待联合阶段执行）

建议状态根 `/data/code/fleet-comparison/state/codex/joint-validation`，MCP `127.0.0.1:15611/mcp`。Goal 子进程无额外服务端口。该根与固定开发 driver 的状态分离。

```bash
cd /data/code/fleet-comparison/codex/fleet-graph
uv run fleet-graph serve \
  --config config/codex.json \
  --root /data/code/fleet-comparison/state/codex/joint-validation \
  --host 127.0.0.1 --port 15611
```

也可 `--transport stdio`，其余参数相同。停止单 Goal 用 `goal_stop(goal_id, immediate=false)`；需立即停止用 `immediate=true`。确认 `goal_status` 为 stopped 且运行不再活跃后再退出 MCP 主进程。停止 MCP 进程本身不会停止已 detach 的 Goal 引擎，不能用“退出服务”代替逐 Goal stop。

崩溃后查询 `goal_status`、`goal_events` 和 runtime 原始 Session，再调用 `goal_resume(goal_id)`。resume 核对现场，不用重复 enroll 来恢复。不可删除 events.sqlite3、runtime intent 或 Session 记录以绕过异常。

启动回执丢失且进程缺席无法由现有 receipt 确认时，先外部核查，再显式 `goal_resume(confirm_launch_absent=true)`；工具仍拒绝活跃 launcher 或已占用引擎锁。对于 `run.uncertain`，持续只读核对期间同一 Goal/角色不会另起执行；外部确证缺席后可传 `confirmed_absent_runs=[run_id]` 和 `absence_evidence`。真实迟到结果优先，不用缺席声明覆盖已完成输出。这是异常恢复入口，不是日常重试开关。

## MCP 请求示例

以下为格式模板，必须使用 MCP 返回的真实 WF ID、专用验收 repo 与分支。不得把模板直接指向生产 main。

```json
{
  "schema": "goal.enroll/2",
  "request_id": "joint-codex-case-01",
  "work_folder": "由 Work Folder MCP 返回的 ID",
  "title": "联合验收专用目标",
  "source_branch": "release/joint-codex-case-01",
  "repos": [{
    "path": "/data/code/fleet-comparison/codex/validation-repo",
    "remote": "origin",
    "target_branch": "validation/codex-target",
    "acceptance": ["make verify"]
  }],
  "takeover": false
}
```

将对象作为 `goal_enroll(request=...)` 提交。release 已有且确认接管才用 takeover=true。相同 request_id 内容须一致；不同内容拒绝。返回 goal_id、状态、repo 与运行句柄。源 Goal 正文从 WF goal.md 读取并冻结，编辑 WF 不静默影响运行；用 goal_steer(body,expected_version,request_id) 显式更新版本。

`goal_message` 要求外部 caller（kind=human/agent、id）、reply_to、独立 request_id 和 text。默认回复经 `goal_replies` 的耐久 mailbox 消费；配置外部 reply_mcp 时，接收工具须接受 request_id/caller/reply_to/text/idempotency_key 并去重。分页空结果可能是当前事件页没有回复，仍按 next 继续，直至 next 不变。

## 联合测试驱动（待执行）

准备包含上述 request 的 JSON 文件及真实验证目标后，运行：

```bash
uv run python scripts/joint_validation.py \
  --url http://127.0.0.1:15611/mcp \
  --request /data/code/fleet-comparison/state/codex/joint-request.json \
  --output /data/code/fleet-comparison/state/codex/joint-evidence \
  --timeout 7200
```

脚本保存登记结果、完整分页事件、最终状态；未完成则申请 graceful stop 并非零退出。它只驱动一个真实目标，不替代恢复/并发/版本变化等联合场景清单。每场景分别保存运行 ID、HEAD/PR、验收命令日志和 Session 原始证据，不能把某个 case 的 done 当成整套 E2E 验收通过。

# References
- `config/codex.json`、`scripts/joint_validation.py`、`src/fleet_graph/service.py`。
- 本组 WF goal.md 与 delivery.md。
