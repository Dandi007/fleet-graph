# Docker 测试 Runner

入口是 `python /harness/runner/run.py --mode smoke|e2e`。Runner 通过 `CANDIDATE_URL`、`WF_URL` 调用本次 Docker 网络内公开 MCP；通过透明网关与 GitHub 访问真实外部依赖。运行模式不会导入 Fleet Graph 的 Python 包，也不读取内部数据库。

| 输入 | 约定 |
|---|---|
| E2E_RUN_ID | 必填，字母/数字/下划线/连字符，最长 80 字符 |
| FIXTURE_REPOSITORY | 仅允许 `owner/fleet-graph-e2e-fixtures` |
| GH_TOKEN_FILE | 本次 GitHub token secret 文件 |
| CANDIDATE_URL | `http://candidate:15611/mcp` |
| WF_URL | `http://work-folder:5602/mcp` |
| E2E_TIMEOUT | 整条 Goal 最大观察秒数，默认 3600 |
| /workspace | 专用可写卷，fixture 与 DD worktree 在这里 |
| /artifacts | 仅 Runner/外部验收器使用，候选不得挂载 |

smoke 执行真实模型请求、公开 MCP 工具发现、WF 写入回读、agent-bus publish/consume/ack，以及代理拒绝和无直连出网检查。它不代表角色链或业务交付通过。

e2e 先运行 smoke，再把固定 fixture 初始化为新 Git 历史，push `e2e/RUN/target`，然后创建容器内 WF 与真实 Goal。已存在 workspace 或 target 一律报错，不接管。Goal 使用 `release/e2e/RUN`，先由 Goal 在 DD source 提交 SPEC，再通过 DD 完成开发与审查，最终 target checkout 交外部验收。

终态等待包括引擎退出，避免异步书记员尚未完成就提前采集。超时调用公开 goal_stop immediate；原始停止返回值仍保留。原始 events 从游标 0 开始读取，Session 与 command artifact 全量分页读取。`adapter.py` 只映射公开返回字段，缺失证据保持缺失，交由公共 verifier 报错。runner-report 的 `collected` 只表示已采集，不是业务 passed；最终通过由独立 verifier 的 verification.json 决定。

独立 verifier 使用无网络、root filesystem 只读的容器，bundle 与最终 repo 只读挂载，报告单独写入 `/verification/verification.json`。可信 Runner 只采集输入，不在带写入凭证的容器里执行待验收功能代码。

终局还从真实 agent-bus 的 `board:agent-runs` 频道完整回读消息，先保存 `raw/runtime-bus.json` 原始页，再核对成功 Goal/Impl 的 run_id、真实 sender、同版本 started/exited、事件顺序与退出码。普通 smoke 消息不能替代 runtime 生命周期消息；缺失或查询失败记入 collection-errors，外部验收器也独立检查原始消息。Goal reply 仍遵循候选公开 mailbox 协议。

若连续两次轮询返回完全相同的 blocked 状态，且所有 run 都明确 finished，Runner 通过公开 goal_stop immediate 提前收口并保存 stop-reason、停止回执与失败证据。有 running、launching、collected、uncertain 或 paused run 时不会触发该规则；状态或新请求发生变化也会重新计数。

另一条失败收口规则是连续两次完全相同的非 done 状态且 engine_alive 明确为 false，覆盖 stopping 与 uncertain/lost 残留。它仍通过公开停止接口保存回执，并继续完整采集，不会把未确定 run 当作成功。启动期间状态或 liveness 发生变化会重置计数；停止接口报错也保留原始状态与错误继续采集。

GitHub token 沿用此次授权凭证，其平台权限可能超出测试 repo。repo basename 与分支约束是 harness 防误写规则；域名代理不提供 GitHub repo 级权限隔离。token 不写入 remote URL、仓库文件或报告。宿主应为更强隔离使用仅授权专用 repo 的 token。

```sh
python -m unittest discover -s tests/e2e/runner -p 'test_*.py' -v
```

# References

- `../contract/README.md`、`../contract/schema.json`：公共验收契约。
- 冻结本组 `service.py`、`engine.py`、`runtime.py` 的公开 API 输出与事件字段，仅用于映射。
