# E1 审计 acceptance_rerun 环境供给：非 Python 仓不得因缺依赖假红

## 背景（生产实证 2026-08-31，监督面立案）

E1 审计图（supervise/ 下 board_question 审计）对 dev-fg-acff54d9987f
（calendar-agent，bun 仓）出 recommend_reject（seq 1040），唯一 FAIL 是
acceptance_rerun：它在一次性 detached worktree 里重跑验收，该环境从未装过
web 依赖，报错原文 `vite: command not found` (exit 127)。同一双门在
dd 冻结 receipt、监督面 dd worktree 复跑、收割后 master 三处独立环境全绿
——环境性假阴（人工复裁维持 APPROVE，seq 1042）。

根因：审计的 throwaway worktree 只做 `git worktree add --detach`，不执行任何
环境供给；fleet-graph 自身（uv 仓，make verify 自带 uv sync）恰好自愈，
非 Python 仓一律假红。

## 要求

1. **rerun 前执行该单冻结的环境供给**：E1 acceptance_rerun 在 throwaway
   worktree 里先执行该 development 冻结记录中的 setup_commands
   （run-config.json / record 中已有的那份，逐字执行，不发明新命令），
   再跑 acceptance_commands。
2. **无冻结 setup 时的降级语义**：若该单没有冻结 setup_commands 且 rerun
   失败，该项结论必须降级为 `env_unverified`（advisory，不计入
   recommend_reject 的驱动因子），并在 note 里写明「审计沙箱无环境供给，
   rerun 结果不可判」——不许把环境缺失说成验收失败，也不许静默跳过该项
   （仍要出现在清单里）。
3. rerun 真实失败（供给成功后 acceptance 仍红）继续按现行语义驱动
   recommend_reject，零放宽。

## 回归测试（判据随单冻结）

- fixture：acceptance 依赖 worktree 内一次 setup 步骤才能通过的假仓
  （如 acceptance 脚本检查 setup 生成的文件）——修后 rerun 绿；
- 已知阴性：同 fixture 在本单前 main 上 rerun 红且驱动 recommend_reject
  （测试注明复现）；
- 无冻结 setup + rerun 失败 → 结论为 env_unverified、不驱动 reject、
  note 含声明文案；
- setup 成功但 acceptance 真红 → 仍 recommend_reject（零放宽对照）；
- 存量套件零回归（make verify）。

## 边界

- 只改 fleet-graph 仓 supervise/ 审计侧与 tests/；不动 dd/ 的冻结逻辑、
  不动 arbiter/、state/。
- 遵循仓 AGENTS.md。

```dd-acceptance
make verify
```
