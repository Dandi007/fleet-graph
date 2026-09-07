# DD 启动数据完整性修复

## 问题与行为

入单记录的 seats 必须在 dd run 入口进入 DevelopmentConfig.models，最终传到 AgentRunStageActor。不能只保存配置而静默运行 registry 默认模型。显式提供的记录文件不可读、身份不符或 seats 形状错误时，在启动任何角色前拒绝；未提供记录的独立 CLI 调用保留现有默认。

systemd-run 不得提前展开验收和 setup 脚本中的环境变量。变量由最终执行脚本的 shell 按验收环境解释。启动端关闭 systemd 环境展开，保留 argv 字节。

## 验证

CLI 入单记录到配置的有效与错误输入测试；控制面 argv 保留脚本文本的测试；systemd-run 真机对照验证 `${VAR}` 原文保持。完整仓库验收为 make verify。

# References

- WF wf-fdb41f；wf-6d1cea X-12；wf-3a5732 A-10。

## 运行、审单与恢复

DD 的实现与审查进程使用 bubblewrap 文件与 PID 隔离。实现只写工作树、对应 Git 元数据和 Session 目录；审查工作树只读。验收同样隔离，缺 bubblewrap 明确失败。验收/setup/env 从冻结记录读取，父进程 argv 不再携带脚本。宿主 OpenCode 会话变量不进入子调用。

新 DD 默认冻结 `dd-standard-v1`：验收命令三方一致、当代 Impl→CR→FR 回执与 checkpoint canonical digest 一致且 review APPROVE、实际重跑验收通过且 subject 未变化、零测试删除。SPEC 可显式选择 `legacy-six-v1`；旧记录保持原 policy，不自动降低判据。四项是通用开发交付的机械约束，不代表引擎能证明任意需求语义，Goal 仍承担业务验收。

Goal 的 `dd.gate_release.v1` 必须绑定 dispatcher、generation 对应请求及幂等 action。APPROVE 全部证据通过才可发布；REJECT 可带失败证据，但必须给出问题、建议答案、不回答的代价。先持久化裁决，再 Git 封存其 ID 与证据，最后恢复；任何不完整窗口均不得由自动恢复器越过。拒绝后同一 DD 新一代回 Impl，再经 CR/FR/验收。控制操作跨进程按 DD 互斥，避免取消、启动、审单互相覆盖。

程序 sealer 只提交 `.dev-dispatch` 和 `.dd-evidence`，不将验收产生的缓存当产品。重试 reset 前为旧 HEAD 与未提交产物建立 recovery ref。失败回执保留真实错误；缺验收命令返回结构化 127，超时返回 124。回执审计跳过不移动 HEAD 的失败事件，保留真实坏链与跨代输入差异。

## 等待、消息与观测

运行中且尚无 result 的 DD 由当前 unit 活性证明等待成立；已死 unit 产生可恢复事实。等待 DD 不累计无进展惩罚。角色调用期间持续更新 phase heartbeat，总调用预算仍生效。消息降级原因进入 Goal 本轮输入，驻停期间重新探测来源恢复。

消息的 channel 由 agent-bus alias 解析结果确定；旧服务不存在解析入口时兼容同名 channel。消息时间保留毫秒，避免同秒消息丢失唤醒。必须先持久化输入再 ack，Session 与引擎事件分别保留。

## 发布契约

`deploy/release.sh --no-flip` 从干净 commit 冻结依赖构建候选。完成标记写在构建成功之后；失败清理，残缺目录不得复用。`--activate` 要求有回滚版本，切换 current 后先重启八个服务，再启动 scheduler，核验九个实际 PID cwd 收敛并稳定；失败恢复旧版本并复核。已运行的短期 DD 保留其原版本，不为版本统一强杀在途工作。

## 验收证据

完整入口：`NO_PROXY='*' no_proxy='*' make verify`。隔离真实 DD `dev-fg-f1895e4ce3a9`：g1 因程序缓存污染被真实 Goal REJECT，g2 清理后 Impl/CR/FR/acceptance 全通过，真实 Goal APPROVE，程序合并完成。裸远端交付树仅 `clamp.py` 和原 `test_clamp.py`。消息真机探针覆盖 alias 与 agent ID 不同、发送、毫秒唤醒、持久化、ack、清空。

外部驱动说明：该集成验收由监督脚本提交/消费 Goal 动作并启动拒绝后的新代，没有手改结果或封存文件；不能等同于已证明整个 WF scheduler 零监督自主完成。

## 开线数据契约

`goal_prepare_line(alias, decided_by)` 是监督面入口。新 agent 的线凭证经 whoami 校验身份与非监督权限后，以 0600 原子落位；已有 alias 不重绑、不 rotate。缺凭证明确失败，不借用监督 token。

`goal_enroll` 通过七道门后持久化申请；`goal_admit` 验证监督身份，未提供外部 decision_ref 时生成内生审计引用。裁决先封存，再把线写到服务名册；重投可恢复中断的名册写入。`FLEET_GRAPH_RUNTIME_ROSTER` 默认 `/data/fleet-graph/goal/roster.json`，已有代码名册作种子，新增线不需要改代码。scheduler、状态面和裁决发现每轮合并读取；alias 不可与种子或运行线冲突。入编队列跨进程串行、锁内重读、原子持久化，不能用旧实例覆盖其他申请。

`verify-rebuild.sh` 的旧重构专项判据保留原语义，仅修复缺少 `VRB_MCP_STATE` 默认值导致的脚本早退。其中退役资产清理项与主动合成请求不能替代本 SPEC 的交付验收，也不能直接对生产盲跑整份脚本。

## 完整 Goal 启动接线

正常 line run 与中断恢复必须绑定同一真实 DD 控制面，同时提供 Stop dispatch 和 gate 消费者；不得只在测试注入时可用。scheduler 的 dd_root 是子进程派单与唤醒共同的数据根，经 FLEET_GRAPH_DD_ROOT 透传。DD 执行程序钉住启动 Goal 的实际发布路径，避免运行中 current 切换造成跨版本执行。
