# SPEC: 版本绑定的线性 DD 生命周期与同身份返工

实现 Fleet Graph 单 repo DD 生命周期 L1-L7。目标 repo 为 Fleet Graph，基线必须为 5a0c392f45a5cf2b66231e61061a27201469edc6，交付目标仅为 refs/heads/release/fleet-compare-self，开发分支使用 dd/fleet-compare-self/<development-id>。本单不得重写 DD01 kernel、Goal 请求队列或 Runtime Session/call 接口，不实现 B1 多 repo finalization、B2 异步只读 clerk，也不进行真实模型、daemon、新引擎进程、共享服务、live Git/PR 副作用、集成或 E2E 运行。

L1: 将 DD 阶段固定为 Impl -> 程序 acceptance -> continuous review -> final review -> Goal gate/request -> typed merge authorization -> merge。configure 或 PREPARED 不算完成。程序 acceptance 失败必须阻止 CR；阶段顺序在 contract、executor、materializer、replay 和恢复路径一致。

L2: acceptance、CR、FR、Goal reject，以及需要修改产品代码的 merge feedback，统一返回原 DD 的 Impl，保持同一 DD ID、冻结 SPEC 和 PR；递增 attempt/work version，并使受影响的旧验收、审查和授权失效。移除业务进度的 6 次/40 步硬上限，允许业务返工继续推进；单次调用超时和不可恢复基础设施错误仍需有界且与业务拒绝分型。

L3: 用真实 Git/产品事实建立并校验完整 validity key，至少绑定产品 revision/tree、SPEC digest、acceptance-context revision、target identity 和 PR identity。任一相关输入变化都必须使受影响阶段失效并触发重验重审；纯记账提交不得制造无限重审；SPEC 改动必须是新 DD，不能通过 reconfigure 偷改本单 SPEC；不得信任 agent 自报 SHA。

L4: replay 必须验证新顺序的封存前缀；先查询 Runtime、Git、PR 等外部效果，再决定 collect、reuse、rework 或 unknown；不得丢失 dirty 状态、重建 PR、reset 分支或重发已确认效果。未知效果必须保留并安全恢复；既有持久旧契约必须显式解释或安全拒绝；不重复实现 driver 专属 configure replay 修复。

L5: Goal gate 必须绑定同一已验收、已审查版本及完整 validity key，校验原 dispatcher、请求和反馈证据；缺少 FR 完成、反馈或版本一致性时不得批准，过期 Goal verdict 无效。合法 reject 必须进入同一 DD 的共同返工路径，不终止并另起相同任务。

L6: merge feedback 必须 typed 区分 target 竞争/变化、真实内容冲突、transport/unknown、already-merged、PREPARED-only 和 measured MERGED。target 竞争不得误报真实冲突；transport/unknown 不应改变业务 verdict；PREPARED 不得算 merged；需要代码修改的反馈回 Impl 并使旧审查失效；保留现有 CAS 保护，不扩展真实 PR 平台算法。

L7: 通过现有 raw-event 边界可靠记录 DD、stage、attempt、validity 以及 opaque Runtime run/session/receipt refs。journal 或证据写入失败时不得无可恢复依据地成功迁移状态，不能吞写入错误；恢复和查询必须保留完整可追溯引用；不得另建 Session transcript 或第二个 L0 权威；L1 clerk 无批准、阻塞、返工或合并权限。

新增 tests/test_dd_lifecycle_contract.py，使用纯函数、内存图和注入 fake stage/effect 端口覆盖：严格顺序、acceptance 阻挡 CR、五类拒绝同身份返工、旧业务阈值移除、合法 per-call timeout、code/context/target/PR 变更失效、记账-only、过期 Goal verdict、每个 receipt 边界 replay、legacy contract 解释、安全拒绝、竞争/冲突/transport/unknown、already-merged 核对、PREPARED 非成功、无关 DD 独立推进和证据写失败。按新需求有据替换旧 tests/test_dd_pipeline.py 的旧顺序、业务 bounds、拒绝终止断言；保留身份、receipt 链和操作超时覆盖，并逐项映射 replay/scripts/gate 旧测试，不能删测试掩盖缺陷。

开发阶段 setup 使用 Python 3.11、uv sync --frozen、隔离树内可写 UV_CACHE_DIR、UV_LINK_MODE=copy、UV_OFFLINE=1、PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 且 PYTEST_PLUGINS 为空。DD acceptance 必须执行并记录：python3 -m compileall -q src；python3 -m pytest -q tests/test_dd_lifecycle_contract.py。交付须为原子 commit 并 push 到本组 release，附需求到实现到测试映射、精确 HEAD/PR/依赖配置及 delivery.md；delivery.md 的 ready_for_joint_validation 只表示本组开发阶段交付，必须明确联合运行、集成、部署和 E2E 尚未执行。