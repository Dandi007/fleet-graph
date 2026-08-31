# arbiter A2 分页窗修复：频道破千后 fetch 截断不得使 triage 永久失效

## 背景（生产实证 2026-08-31，监督面立案；#178 同族）

fleet-graph-arbiter.service（A2 只读仲裁员，timer 驱动）自 board:work-notes
频道超过消息上限窗口后，每次触发即摔：

```
a2.py:288 collect_subjects
RuntimeError: board:work-notes fetch truncated; refusing to triage a partial board
```

「拒绝在残缺板面上 triage」这个响亮拒绝语义是**正确的**（不许静默跳过）；
缺陷在于 fetch 用了裸窗口读法，频道破千后**必然**截断 ⇒ 服务持续 failed，
A2 分诊能力实际归零。PR #178 已为 board.decision_for 修过同款（bus 消息
分页为**升序**，裸 limit=N 读到的是最老窗口）：先 learn head_seq，再以
after_seq=head-N 读尾窗。本单把 arbiter 的板面读取修到同一口径，或改为
全量翻页聚合（实现者论证后择一；无论哪种，读全所需窗口后 triage 必须给出
与破千前一致的结果）。

## 要求

1. arbiter 的 board:work-notes 读取在频道任意长度下不再截断摔死：
   要么尾窗读法（先学 head_seq，再读需要的尾部窗口），要么顺序翻页聚合；
   选型理由写进代码注释。
2. 「残缺板面拒绝 triage」的响亮拒绝语义**保留**——仅当读取机制真实失败
   （HTTP 错误、翻页中断）时触发，不再因频道长度触发。
3. **判据必须包含「频道 >1000 条时仍能正确 triage」，而不只是「不再 crash」**
   （方向权 2026-08-31 预定；修成「截断后静默跳过」= 缺陷族第九式，拒收）。

## 回归测试（判据随单冻结）

- fake bus 频道 >1000 条消息（含新旧混排的 question/decision）：
  collect_subjects 返回的 subjects 与全量口径一致（新消息不丢、不因截断少报）；
- fake bus 频道 <100 条：行为与现行逐字节一致（存量零回归）；
- fake bus 读取中途真实失败（HTTP 5xx）：仍响亮拒绝，不静默跳过；
- 已知阴性：本单前的 main 在 >1000 条 fake 频道上必须复现截断拒绝
  （脚本或测试注明）。
- 存量套件零回归（make verify）。

## 边界

- 只改 fleet-graph 仓 arbiter 侧（src/fleet_graph/arbiter/、必要时
  bus/client.py 复用 #178 的尾窗原语、tests/）；不改 bus 服务端、
  不动 supervise/、state/、dd/。
- 遵循仓 AGENTS.md 与引擎本体加严条款。

```dd-acceptance
make verify
```
