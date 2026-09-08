# Candidate 容器契约

该镜像启动冻结 Fleet CLI 和真实 agent-runtime/OpenCode。`/health` 通过上游 MCP `tools/list` 判定就绪，不代表模型请求、GitHub 操作或业务 E2E 通过。

构建 context 为本仓库根，Dockerfile 是 `tests/e2e/candidate/Dockerfile`。构建输入由主入口用 `git archive` 准备：

| 输入 | 内容 |
|---|---|
| `.runtime/e2e/build/fleet/` | manifest 指定的 Fleet commit，经 `git archive` 导出 |
| `.runtime/e2e/build/agent-runtime/` | manifest 指定的 runtime commit，经 `git archive` 导出 |
| `.runtime/e2e/build/source-manifest.json` | 必须含 `fleet_commit`、`runtime_commit`；所有 `*_commit` 均须为完整 40 位十六进制值，额外来源字段原样留存 |
| `tests/e2e/candidate/` | 容器配置生成器、启动入口与 relay |

启动适配器不锁定某个候选 commit。候选来源由构建入口验证并归档，容器校验 manifest 格式，不会将 manifest 自述当作源码内容校验。当前候选的冻结 commit 另存于 `fixtures/codex-source-manifest.json`，仅作为可选 fixture。

manifest 的 `config_path` 可指定 Fleet 源码内相对路径，默认 `config/codex.json`；`prompt_dir` 默认配置文件旁的 `prompts/`，也可显式指定源码内相对目录。这些字段只调整启动配置定位，不修改候选业务实现；配置文件仍需符合当前 launch adapter 的 roles 配置契约。不能将不同 CLI/schema 的实现宣称为自动兼容，需要显式提供相应 launch adapter 并保持外部验收接口一致。

工具版本固定为 Python 3.11.13、Bun 1.3.14、OpenCode 1.17.13、Git 2.49.0、gh 2.76.2。Git/gh 由固定版本官方发行包安装；镜像 build args 可显式修改工具版本，实际输出记录在 manifest。冻结 runtime 没有提交 Bun lock，因此镜像构建会生成解析后的 lock。manifest 如实标明该依赖复现限制，不宣称所有间接依赖均已锁定。

| 运行路径或环境 | 契约 |
|---|---|
| `/state` | UID 10001 可写；保存 Fleet 状态、生成配置、`logs/fleet.log`、`candidate-manifest.json` |
| `/workspace` | UID 10001 可写；仅放测试专用 repo 和 worktree |
| `/run/secrets/gateway_token` | 只读文件；值注入 `NEW_API_GATEWAY_TOKEN_OPENAI`，可由同名 `_FILE` 环境变量指定其他文件 |
| `/run/secrets/gh_token` | 只读测试专用 token；值注入 `GH_TOKEN`，可由 `GH_TOKEN_FILE` 指定其他文件 |
| `/run/secrets/agent_bus_token` | 独立 agent-bus token，runtime 原生读取；启动前注册缺失或失败时阻止 Fleet 启动 |
| `AGENT_BUS_URL` | 默认 `http://agent-bus:7470`，可由 Compose 覆盖 |
| `HTTP_PROXY`、`HTTPS_PROXY` | Compose 设置 `http://egress:3128`，仅允许测试授权的 GitHub 外发 |
| `NO_PROXY` | 至少含 `candidate,work-folder,gateway,agent-bus,localhost,127.0.0.1` |

只接 Compose 内部网络，不挂宿主 HOME、Docker socket 或共享服务目录。模型仅有 `deepseek-v4-pro@opencode/gw` 一个 static route，经 `http://gateway:15722/v1` 调用，没有 native subscription 或 fallback。OpenCode 所需 `@ai-sdk/openai-compatible` 已由该版本的 `BUNDLED_PROVIDERS` 内置；镜像设置 `OPENCODE_DISABLE_MODELS_FETCH=1`、`OPENCODE_DISABLE_AUTOUPDATE=1`，运行阶段不需要下载模型目录或更新 CLI。

该测试验证的是显式容器配置覆盖后的候选行为。它不验证候选默认宿主配置、native subscription、其他模型/runtime 或自动发现宿主凭证的能力；这些范围不应计入通过项。

配置生成后先执行 `/opt/e2e/bootstrap_runtime_bus.ts`，通过冻结 runtime 的协议描述和注册函数，在独立 agent-bus 初始化协议与 `board:agent-runs`。脚本非零退出或超过 60 秒时阻止 Fleet 启动；原始输出写入 `/state/logs/runtime-bus-bootstrap.log`，退出码保存到 manifest 的 `agent_bus.bootstrap`。该步骤不替代后续真实生命周期消息的验收。

外部调用 `http://candidate:15611/mcp`。原 Fleet CLI 仍监听 `127.0.0.1:15612`，relay 仅替换 Host 并双向转发字节，保留 MCP session、SSE 与 chunked 传输。`GET /candidate-manifest` 返回 commit、实际工具版本、生成配置哈希和明确的覆盖说明。

覆盖只涉及测试配置：将 runtime `profiles` 接到 `/state/config/runtime-profiles`；仅保留默认网关 route、candidate 与 work-folder MCP；原 fleet harness 保持不变；原角色 prompt 附加本次 Docker E2E 授权。冻结 Fleet/runtime 的 `src/` 文件没有改写。原角色 prompt 的职责和真实业务返回协议仍由 candidate 执行。

# References

- 被测 Fleet：`config/codex.json`、`config/prompts/`、`src/fleet_graph/cli.py`、`src/fleet_graph/runtime.py`。
- 被测 runtime：`src/recipes/opencode.ts`、`src/agent-bus.ts`、`profiles/harness/fleet-*.yaml`、`profiles/routes.yaml`。
- [OpenCode v1.17.13 内置 Provider](https://github.com/anomalyco/opencode/blob/v1.17.13/packages/opencode/src/provider/provider.ts)。
- [OpenCode v1.17.13 环境开关](https://github.com/anomalyco/opencode/blob/v1.17.13/packages/core/src/flag/flag.ts)。
