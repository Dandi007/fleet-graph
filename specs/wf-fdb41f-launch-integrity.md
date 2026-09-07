# DD 启动数据完整性修复

## 问题与行为

入单记录的 seats 必须在 dd run 入口进入 DevelopmentConfig.models，最终传到 AgentRunStageActor。不能只保存配置而静默运行 registry 默认模型。显式提供的记录文件不可读、身份不符或 seats 形状错误时，在启动任何角色前拒绝；未提供记录的独立 CLI 调用保留现有默认。

systemd-run 不得提前展开验收和 setup 脚本中的环境变量。变量由最终执行脚本的 shell 按验收环境解释。启动端关闭 systemd 环境展开，保留 argv 字节。

## 验证

CLI 入单记录到配置的有效与错误输入测试；控制面 argv 保留脚本文本的测试；systemd-run 真机对照验证 `${VAR}` 原文保持。完整仓库验收为 make verify。

# References

- WF wf-fdb41f；wf-6d1cea X-12；wf-3a5732 A-10。
