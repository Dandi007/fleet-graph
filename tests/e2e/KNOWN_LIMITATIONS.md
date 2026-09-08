# Docker E2E 已知边界与候选问题

## Scribe 连续观察的输入自引用

固定候选 Fleet Graph `5e601d8d256c5d4d855e3d8b87556fb19c36c824` 在运行 `fg-3c42620a7957` 中暴露了连续观察缺陷。Scribe 的 `run.intent` 保存完整 prompt；下一次观察虽然计算了排除 Scribe 事件的 `meaningful`，实际输入仍使用完整 `page["events"]`。失败后 cursor 不推进，后续输入重复包含之前的 Scribe 输入并持续膨胀。

公开 `goal_session` 的实测证据，Goal `9e06f56a38d44f91fc4ee770`：

| Scribe run | 当前 user prompt 字符数 | 实际结果 |
|---|---:|---|
| `15d50b45a66eaf0936084a2a` | 977,560 | HTTP 400，输入长度超过上游限制 |
| `8c53041a80be25939746049f` | 1,961,896 | HTTP 400，请求体超过 6,291,456 bytes |
| `b0a6ef927c6d67d5fe280f0d` | 3,930,393 | HTTP 400，相同请求体超限 |

另一次 Scribe `63cb21143859199246e263f8` 因最终 JSON 包含非法 `\s` 转义而 exit 91。这与请求体膨胀是两个问题。上游长度错误标记 `isRetryable:false`，原样恢复同一会话无法解决。

本测试短 case 采用候选原生配置 `scribe_interval=0`，只在 Goal 完成时运行真实终局 Scribe；配置原值、有效值和模式写入 candidate manifest，独立 verifier 要求终局 Scribe 成功。它减少本 case 的中途观察，不修复连续观察缺陷。没有修改冻结候选源码、截断产品输入或伪造角色输出。

后续产品修复应在对应开发线处理输入事件过滤、失败 cursor 与上下文容量，再用连续观察 case 验证。仅将 `roles.scribe.session_policy` 改为 `fresh` 无法解决单轮 prompt 自嵌套。

## 其他未覆盖事项

- native subscription 和宿主登录态复用尚未支持；首版只验证 OpenCode static gateway。
- 搜索使用真实 agent-knowledge 的 keyword 索引；vector 与 embedding 服务不在本 case 中。
- 另一条重构线尚未用本测试执行；共用契约不代表所有候选已通过，启动适配和公开输出不一致时须明确报告。
- 冻结 agent-runtime 未提交 Bun lock；证据保存本次解析结果及工具版本，不保证以后重新构建获得相同依赖。
- GitHub 域名代理不提供仓库级授权隔离，继承的 token 可能拥有更大权限；测试驱动固定专用仓库与运行分支。
- 功能 verifier 针对错误实现与提前退出作独立断言，不是任意恶意 Python 代码的证明系统。候选功能执行容器无网络、无凭证、输入只读。

# References

- 冻结候选 `src/fleet_graph/engine.py`：`observe()` 的事件筛选和 prompt 构造；`_launch()` 的 `run.intent`；Scribe 结果收集的 cursor 更新。
- 冻结候选 `src/fleet_graph/service.py`、`src/fleet_graph/cli.py`：`scribe_interval` 配置与终局等待。
- `.runtime/e2e/runs/fg-3c42620a7957/`：公开 Session、运行状态和导出证据。
- 工作线 `wf-613744`；候选开发线 `wf-53a584`。
