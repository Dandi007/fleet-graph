# dd implement 重试自动 re-prepare：前任遗骸不得挡住返工

## 背景（生产实证 2026-08-31，dev-fg-82373b544898 g1 全程取证）

implement run fad31fe6 把活干完（worktree 留下已提交未交付的
d9f13c602d26，父=input_commit）但以 exit 97 contract_violation
（no structured output found in stdout，#480-A 诚实性护栏）终局失败。
下一次 implement 尝试的 actor 按 exact-commit 契约发现
HEAD(d9f13c6) != input_commit(ae698a7)，声明 BLOCKED、执行单次
sanctioned reset 复位、拒绝继续 → 整单 terminal=refused，
监督面人工破障（手核 worktree + development_start g2）才恢复。

actor 的拒绝是正确诊断（契约就该这么写）；缺陷在 stage 层：
**failed implement 之后、下一 attempt 之前，没有人把 worktree
恢复到 attempt 前置条件要求的状态**。「做完没报告」的失败模式下，
遗骸必然存在，重试必然被挡，每次都要人来破障。

## 要求

1. **重试前自动 re-prepare**：implement（及同通道 stage）的 attempt 以
   failed/contract_violation 终局后，若 worktree 状态不满足下一 attempt
   的前置（HEAD != 该 attempt 的 input_commit，或树不净），stage 在派下一
   attempt 前自动恢复（reset --hard 到 input_commit + clean，等价于 actor
   合同里那次 sanctioned reset，由引擎代做）。遗骸 commit 无须保留
   （git reflog 天然留档），但 re-prepare 动作必须落 events.jsonl
   （event=re_prepare，含被清理的 HEAD sha），可审计。
2. **不误伤领养路径**：RunWaitTimeout 后 re-adopt 在飞 run（#167 语义）
   时**绝不** re-prepare——run 还活着，它的工作区状态是它的；只有 run 真正
   终局失败后、确需重派新 attempt 时才恢复。区分依据写测试钉死。
3. refused 状态的既有语义不变：actor 侧 exact-commit 检查保留（纵深防御，
   引擎 re-prepare 失效时它仍是最后一道闸）。

## 回归测试（判据随单冻结）

- 模拟 failed implement 留下遗骸 commit → 下一 attempt 启动时 HEAD ==
  input_commit 且树净、events 含 re_prepare 记录、attempt 正常执行；
- 已知阴性：本单前 main 上同场景第二 attempt 被 BLOCKED/refused
  （测试注明复现，即 dev-fg-82373b544898 g1 实录的机械化）；
- re-adopt 在飞 run 场景：不触发 re-prepare（工作区原样保留）；
- 干净 worktree 正常重试：行为逐字节不变（零回归对照）；
- 存量套件零回归（make verify）。

## 边界

- 只改 fleet-graph 仓 graphs/dd_actors.py、graphs/dd_runner.py（如需）、
  tests/；不动 executors/agent_run.py 的领养语义、不动 roles/prompts、
  不动 supervise/、state/。
- 遵循仓 AGENTS.md 与引擎本体加严条款。

```dd-acceptance
make verify
```
