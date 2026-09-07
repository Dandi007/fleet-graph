# Fleet Graph

Fleet Graph 接收一个绑定 Work Folder 的开发目标，为每个 Goal 建立独立 LangGraph 引擎。Goal Agent 串行处理异步请求，多个单 repo DD 并行实现、验收、审查并合入 release；所有 repo 收尾成功才记录 done。

本分支是 Codex 对照实验的最小系统重构。当前交付阶段为开发验证，真实模型驱动、集成/E2E、部署和恢复运行演练等待双方开发完成后统一执行，不能把单元测试通过理解为这些验收已通过。

```bash
uv sync --frozen
make verify
uv run fleet-graph check-config --config config/codex.json
```

运行入口、依赖与命令见 [运行指南](docs/operating.md)；职责和恢复协议见 [架构](docs/architecture.md)；需求映射见 [开发验证](docs/validation.md)；旧测试逐文件处理见 [迁移审计](docs/test-migration.md)。

旧 scheduler、enrollment 审批队列、DD 多 gate、外围监督/研究流程及其旧 CLI/service 已从此开发分支移除。历史在基线 Git commit 中可追溯，生产部署与固定实验 driver 未修改。

# References
- 本组 WF `wf-53a584` 的冻结设计、协议与开发阶段目标。
- `src/fleet_graph/` 与 `tests/`。
