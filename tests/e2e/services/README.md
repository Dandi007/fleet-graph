# 真实依赖服务

三个 Dockerfile 均使用本仓库根目录作为 build context，由根 Makefile 统一构建。
源码必须经 `git archive` 导出；不复制服务运行目录、宿主 HOME、凭据或生产数据。

| 源码 | 固定 commit | build context 内目录 |
|---|---|---|
| katana | `1c90073fc057b9246ffd5b0f1f0935fa8f456c45` | `.runtime/e2e/build/katana/mcp/{shared,kernel,work-folder}` |
| agent-bus | `febbc2cc6ae11709ff448a8f0139bfa46e934c99` | `.runtime/e2e/build/agent-bus` |
| agent-knowledge | `7c40cc76fddbdf6df55dd504da68113e5d0cfb10` | `.runtime/e2e/build/agent-knowledge` |

Work Folder 安装上述三个真实 Python package，固定 FastMCP `3.2.4`；其他依赖由 pip
解析，当前不宣称完整 Python 依赖锁定。agent-bus 使用源码自带 `uv.lock`，通过
`uv sync --frozen --no-dev --no-editable` 安装。

| Compose 服务名 | 端口与 API | 本次 named volume 目标 | 配置 |
|---|---|---|---|
| `git-remote` | `9418`，Git daemon | `/data/git` | 自动创建 `work-folder.git` bare 仓 |
| `work-folder` | `5602/mcp`，真实 Streamable HTTP MCP | `/data/work-folder` | 等待 git-remote healthy 后启动 |
| `agent-bus` | `7470`，真实 HTTP API | `/data/agent-bus` | 本次独立 `BUS_ADMIN_TOKEN`、`BUS_GATEWAY_TOKEN` |

这些服务只接入本次 Compose 内部网络，不发布宿主端口。Git daemon 的 receive-pack
只允许用于该隔离网络中的测试数据仓。bus 的两个 token 均至少 32 字符且互不相同，
不得使用宿主现有 token。入口优先读取 `/run/secrets/bus_admin_token` 与
`/run/secrets/bus_gateway_token`；未挂载时读取同名大写环境变量。bus 自身仍监听
容器 loopback `7471`，socat 转发容器 `7470`。

Work Folder 入口先从内部 remote clone；空仓按真实服务要求生成 flat-layout canary、
tombstone ledger、空 legacy manifest inventory 和源码生成的 INDEX，再提交初始化数据。
随后调用真实 `server.configure()` 和 `server.mcp.run()`，不替换工具实现。
已有数据卷原样校验并继续运行，不清空、不重建、不覆盖。

`GET /metrics` 只能证明 Work Folder HTTP 进程存活。镜像 healthcheck 使用真实
MCP `wf_list`；bus 使用 `/healthz` 和 `/readyz`，分别覆盖 SQLite 与 bootstrap。

容器内命令如下，输出 JSON 不含 token：

```sh
python /opt/e2e/probe.py work-folder-read-write
python /opt/e2e/probe.py bus-read-write
python /opt/e2e/probe.py git-sync
```

前两项可从装有对应 Python 依赖的本次 runner 或服务容器执行；`git-sync` 必须在
work-folder 容器执行。Work Folder 探针经过真实 `wf_create → fs_create → fs_read_bytes`，
检查回读字节与 commit。bus 探针经过真实注册、未授权反例、publish、consume、ack，
检查内容以及 ack 后无重复交付。每次读写探针生成新标识，保留结果供验收核对。

当前固定版本的 Work Folder kernel 负责本地 Git commit，没有自动 push。
`git-sync` 是明确的额外验收动作，推送本次数据至内部 remote 并比较 SHA；该结果
不能解释为 Work Folder 自带异步同步或自动 push 能力。

## Work Folder 搜索依赖

冻结 Work Folder 的 `wf_search` 调用本机 `127.0.0.1:18082/search`。同一容器内
启动固定 commit 的真实 `agent-knowledge` FastAPI 服务与原生
`watch_and_index.py`；索引由唯一源 `build_lancedb_index.py` 生成。
通过原生 `VAULT_SEARCH_DISABLE_VECTOR=1` 运行 keyword 模式，不安装向量后端，
不访问外部 embedding。该测试覆盖真实关键词检索，**不覆盖向量检索质量**。

索引只扫描 `/data/work-folder`，来源 ID 由真实 indexer 对该根路径生成，和 WF
传入的 source filter 一致。缓存与搜索日志放在该 named volume 的
`.katana/runtime/search`，通过容器自身缓存路径的 symlink 接入原服务默认配置；
不挂载宿主 HOME。原生 watcher 的更新等待为 0.5 秒 quiet / 2 秒 max-wait。

healthcheck 真正调用 `wf_search` 和底层 `/search`，要求 backend mode 为 keyword。
读写探针还要求刚写入的唯一标识在 20 秒内通过 `wf_search` 返回，匹配 folder ID、
filename 和 snippet。搜索服务或 watcher 退出时，入口停止 WF，避免数据面残缺仍假健康。

## Runtime 生命周期接入

真实 bus 的 bootstrap 只内置 message/chat/envelope，不会创建 `board:agent-runs`
或注册 `agent.run.started/exited.v1/v2/v3`。仅有服务健康和普通消息往返不能证明
runtime 生命周期接入已经生效。

candidate 在生成配置后、启动 Fleet 前执行 `bun /opt/e2e/bootstrap_runtime_bus.ts`。
脚本通过绝对 import 调用冻结 runtime 的 `registerBusProtocols()`，使用它原有的
`PROTOCOL_DESCRIPTORS` 与 role schema，并创建公共 fanout 频道 `board:agent-runs`。
任何注册漂移、鉴权或发布失败都会阻止 candidate 启动。

本次 `BUS_GATEWAY_TOKEN` 对应真实 bus 身份 `mcp-gateway`，源码允许它向公共频道
发布；因此继续使用已有 secret，不额外创建身份。脚本用该 token 发布并回读一条
普通 `message`，将结果写入 `/state/runtime-bus-bootstrap.json`。这条 bootstrap
消息不属于生命周期证据，报告明确保留 `lifecycle_verified: false`。

runner 终局调用 `runtime_bus_messages()` 获取完整频道分页并保存原件，然后调用
`verify_runtime_lifecycle(evidence, status)`。后者要求公开 status 中成功的 Goal 和
Impl ticket 分别在 bus 里找到真实的、同版本的 started/exited 对，核对 sender、
run ID、序号先后和成功退出码。缺少任一角色记录就失败，不能用独立消息探针替代。

# References

- katana 固定版本源码：`mcp/work-folder/katana_work_folder_mcp/server.py`、`reindex.py`、`fs_tools.py`。
- katana 固定版本源码：`mcp/kernel/katana_kernel/gitops.py`。
- agent-bus 固定版本源码：`agent_bus/http_server.py`、`agent_bus/config.py`、`scripts/e2e-smoke.sh`、`uv.lock`。
- agent-bus 固定版本源码：`agent_bus/auth.py` 的 `can_publish_to_channel` 与 `ensure_gateway_seed`。
- agent-runtime 固定版本源码：`src/agent-bus.ts` 的 `registerBusProtocols`、`PROTOCOL_DESCRIPTORS` 和 `BUS_CHANNEL`。
- agent-knowledge 固定版本源码：`service/app.py`、`service/search.py`、`scripts/build_lancedb_index.py`、`scripts/watch_and_index.py`。
