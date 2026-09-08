# Docker E2E 交付与检查证据

本线 `wf-613744` 从 Fleet Graph main `a8d21ab51bfbfa8faf6364114a2e6949950a7ccb` 创建独立分支 `feat/docker-e2e-contracts`，交付 [PR #283](https://github.com/Dandi007/fleet-graph/pull/283)。关联 Codex 重构线 `wf-53a584`、自举重构线 `wf-2bf703` 与共同设计 `wf-cce72d`。产品比较分支、固定 driver、生产 main 和共享服务均未修改。

## 完整 E2E 通过

`make test-end-to-end CANDIDATE=codex CASE=single-repo` 在干净实现 HEAD `75d3f4766a49d84ad8012441814e2be7f1a936c2` 启动，运行 `fg-75a88c70368c` 真实退出 0。最终 `execution.json` 为 `status=e2e_passed`、`e2e_passed=true`、`evidence_exported=true`、`cleanup=removed`，无二进制凭证命中。此后分支仅补充交付文档。

| 交付事实 | 实际证据 |
|---|---|
| Goal / DD | `41aa444ba2be8e761bbcacf1` / `57881389ea2b5455d5c34957`，均完成；10 个 run 全部 finished / succeeded |
| 独立 SPEC commit | `8f5bdbf28d65e9ccea26cf3f41586929290fef22`，早于实现 |
| Impl 与最终 target commit | `606de77638788dbbf4d9ba139c3d7e91629fc503` |
| 真实 GitHub 交付 | [DD PR #7](https://github.com/Dandi007/fleet-graph-e2e-fixtures/pull/7) 与 [整线 PR #8](https://github.com/Dandi007/fleet-graph-e2e-fixtures/pull/8) 均已合并到本次专用分支 |
| 终局 Scribe | `196356891050348a2046cbbf` 成功，观察范围 `[1,68]` 覆盖 `goal.done` seq 66，观察回执 seq 73 |
| 独立 verifier | 8/8 通过：原始证据一致性、完整生命周期、终局 Scribe、runtime bus、review/approve 绑定、程序验收产物、Git/PR、17 项独立功能行为与 CLI |
| 测试驱动反馈 | 本轮触发 0 次；有界反馈策略有单元测试，未据本轮宣称真实恢复通过 |
| 导出与清理 | 原始公开记录、Session、实际命令日志、WF/search/bus/Git/工作目录完整导出；最终审计 13 次构建/运行，均无残留容器、网络或数据卷，镜像缓存与远端测试 PR 保留 |

成功运行的证据根目录为 `.runtime/e2e/runs/fg-75a88c70368c/`：`execution.json` 记录源码及镜像、`verification/verification.json` 记录独立判定、`artifacts/fg-75a88c70368c/raw/` 保存公开原始证据。控制台日志为 `.runtime/e2e/e2e-console-7.log`，最终清理审计为 `.runtime/e2e/cleanup-audit-final.json`。原始运行产物保留在本机，不提交进 Git。

## 冻结输入

| 源码 | 本次候选 commit |
|---|---|
| Fleet Graph | `5e601d8d256c5d4d855e3d8b87556fb19c36c824` |
| agent-runtime | `1813ff1e3435031f8fbb30f1776676a25ce56b54` |
| katana / Work Folder MCP | `1c90073fc057b9246ffd5b0f1f0935fa8f456c45` |
| agent-bus | `febbc2cc6ae11709ff448a8f0139bfa46e934c99` |
| agent-knowledge | `7c40cc76fddbdf6df55dd504da68113e5d0cfb10` |

镜像中的实际工具版本为 Python 3.11.13、Bun 1.3.14、OpenCode 1.17.13、Git 2.49.0、gh 2.76.2。模型通过宿主网关调用 `deepseek-v4-pro`。候选 manifest 记录原始模型配置、实际 bare model、路由、生成配置哈希和追加的 JSON 输出要求。

短 case 使用原生 `scribe_interval=0`，只在终局观察一次，且必须真实成功。它规避本 case 中的连续观察自引用，不代表该产品缺陷已修复。没有修改冻结源码或放宽输出 schema。

## 需求覆盖

| 要求 | 交付实现与证据 |
|---|---|
| 独立 main 分支及可复用入口 | `make docker-build`、`make test-docker`、`make test-end-to-end CANDIDATE=codex CASE=single-repo`；候选以完整 SHA manifest 切换 |
| 所有运行依赖独立部署 | 本次 Docker project 的 WF、keyword 搜索/索引器、bus、Git remote、workspace、Session 与数据卷；真实读写、搜索、Git push、bus consume/ack 探针 |
| 仅网关与 GitHub 对外 | internal network、固定网关转发、GitHub CONNECT allowlist；实际代理拒绝和直接出网拒绝检查 |
| 固定输入及输出契约 | slugify-v1、公共 schema、逐字段原始 JSON Pointer；通过公开 MCP、GitHub API 与 Git 验证，不读取产品私有数据库补证据 |
| 完整真实业务链 | Goal、独立 SPEC commit、DD、Impl、程序验收、CR、FR、Goal approve、两次 PR 合并与真实终局 Scribe |
| 独立功能验收 | 无网络、无凭证、无 capabilities、输入与 rootfs 只读的 verifier；可信父进程逐例判定 17 项函数行为及 CLI |
| 失败可追溯与清理 | 保留非零退出及该阶段实际产生的 Session/事件/停止回执；停止服务后导出，成功导出后删除本次容器/网络/数据卷 |
| 两线隔离与关联 | 仅本组冻结候选已运行；另一线保持待接入，不读取其内部实现，不将本轮结果推广为双方通过 |

## 开发检查

- `make verify`：在实现 HEAD `75d3f4766a49d84ad8012441814e2be7f1a936c2` 实际通过，3,262 passed、1 skipped、55 subtests；lint 和三项 conformance 检查通过。原始日志 `.runtime/e2e/verify-final.log`。期间只有 README 文本措辞调整，没有实现修改。
- 同一实现 HEAD 的 [CI verify](https://github.com/Dandi007/fleet-graph/actions/runs/34186391631/job/101935463787) 通过。
- 上一次本地回归的旧测试 `test_mkrepo_idempotent` 遇端口 25612 被占用而失败；随后端口已空闲，单项与完整回归均通过。没有终止未知监听者或修改固定 driver；原始失败日志 `.runtime/e2e/verify-feedback.log` 与单项复查日志 `.runtime/e2e/verify-port-rerun.log` 保留。
- 独立 Docker smoke `fg-8e0caa67b529` 通过，包含真实新写 UUID 的搜索命中，证据导出完成且资源已清理；不能将 smoke 当成业务 E2E。
- verifier 的负例覆盖提前打印成功标记、缺少函数、过期 snapshot、缺失分页、错误审批绑定、终局 Scribe 缺失/失败；另有 Git 失败 stderr 与只读权限准备的回归检查。

## 保留的失败与复验

| 运行 | 实际结果与处理 |
|---|---|
| `fg-2595647a91b5` | 构建失败：useradd 路径；修复为绝对路径 |
| `fg-0ba7521a4132` | smoke 失败：镜像文件权限；修复构建可读权限。首次自动导出失败保留卷，随后恢复导出并清理，原回执与 `cleanup-recovery.json` 均保留 |
| `fg-c9449460d674` | E2E 启动失败：模型名称重复追加 runtime 后缀；生成配置改用 bare model，原值和有效值入 manifest |
| `fg-46f36728807e` | E2E 失败：缺少搜索服务、Impl 输出契约失败；完整保留实际实现与 [PR #1](https://github.com/Dandi007/fleet-graph-e2e-fixtures/pull/1)，补齐真实搜索并收口清理 |
| `fg-f0093dca5a05` | smoke 失败：搜索缓存进入受治理 WF Git repo；迁至本次独立 search volume 后验证 |
| `fg-3c42620a7957` | 主链与 [DD PR #2](https://github.com/Dandi007/fleet-graph-e2e-fixtures/pull/2)、[整线 PR #3](https://github.com/Dandi007/fleet-graph-e2e-fixtures/pull/3) 完成；连续 Scribe 自引用失败。修复验收权限后独立重验 7/8，唯一失败为终局 Scribe；不能计作完整通过 |
| `fg-ed6ba65407da` | Goal 最终 JSON 前附解释，runtime exit 91；测试自动报错、导出并清理 |
| `fg-ed504e5c2047` | 主链与真实终局 Scribe 成功，原 Makefile 因 Git objects 权限导致 verifier 失败；在派生副本上以 `a2dbbb3` 修复版重验 8/8 通过，原始内容 SHA 未变。原始 Makefile 失败回执仍保留 |
| `fg-bffdbdaaefbf` | Impl 的解释/围栏及纠正 Goal 的中途 text 触发 exit 91；冻结解析器只检查首条 text。整轮失败、完整导出并清理；启动约束进一步明确为整轮工具调用加最后一次 JSON |

第三轮、第五轮的派生复验报告分别在对应 run 的 `revalidation/contract-review/verification/verification.json`。`invocation.json` 记录源码 HEAD、容器镜像和实际命令；同目录的 `*-permissions.json`、`original-{before,after}.json`、`workspace-original-{before,after}.json` 记录权限调整及内容 hash。复验没有重写原始 bundle 或 workspace。

## 尚未覆盖

native subscription、其他 runtime 认证、vector 检索、另一条重构候选、多 repo、冲突/崩溃恢复和长期连续监督均未据此验证。真实模型仍可能违反严格 JSON 契约；本次结果不构成重复运行必然成功的承诺。完整说明见 [已知边界与候选问题](KNOWN_LIMITATIONS.md)。

# References

- [入口与隔离说明](README.md)、[公共证据契约](contract/README.md)、[候选 manifest](candidates/codex.json)。
- `.runtime/e2e/runs/` 下各运行的 `execution.json`、原始公开记录、`verification.json` 与恢复/复验回执。
- 工作线 `wf-613744` 的 goal.md、design.md、findings.md。
