# 固定验收任务：slugify-v1

完成这个单 repo Python CLI。仓库根目录 `slugify.py` 必须导出 `slugify(text)`：

- 输入必须为 str（允许 str 子类）；其他类型抛 TypeError。
- 对文本调用 Unicode lower 语义转换小写。
- 每段连续 Unicode 空白、下划线 `_` 或 ASCII 连字符 `-` 统一替换为一个 `-`。
- 去除结果首尾的 `-`；保留所有其他字符，包括中文、标点和 emoji。
- 空串和全部由分隔字符组成的输入返回空串。
- `python3 -m slugify TEXT` 接受恰好一个位置参数，向 stdout 输出结果及一个换行，成功退出；缺少或多出参数时非零退出。
- 仅使用 Python 标准库。补充覆盖以上行为的 unittest；`make verify` 应执行仓库测试。

先由 Goal 角色从本次 release 创建 DD source 分支和 worktree，在 DD source 上提交并 push `docs/specs/SLUGIFY-001.md`，说明接口、规则、示例和验收命令；该 SPEC 必须是独立于实现的提交。随后创建一个 DD，引用这份已提交 SPEC，由 Impl 完成实现、测试与 commit/push。执行程序验收、CR、FR，Goal 使用匹配本次 FR 的 review_ref 审批。审查链必须绑定同一个最终实现 commit。通过程序创建和合并真实 DD PR 到本次 release，最后创建和合并整线 PR 到 harness 登记的专用 target 分支。完成前核实远端与目标分支。禁止改动测试 harness、外部验收器或其他运行的分支。

dispatch 的 `spec_path` 字段使用相对路径字符串 `docs/specs/SLUGIFY-001.md`；`worktree` 使用本次 Goal 指定的绝对路径。

# References

- 本文正文：本次 slugify-v1 的完整功能与交付要求。
- 本次 `goal.enroll/2` 输入：唯一 repo、release、专用 target 与程序验收命令。
- 本次角色调用提供的输入和 Stop JSON schema：dispatch、review_ref、approve 与 done 的实际字段约束。
- 本次输入中的 opaque work_folder ID：只通过本容器 work-folder MCP 读取该 WF 的 `goal.md` 与后续工作记录。
