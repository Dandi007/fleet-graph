# 除颤：test_result_landing_during_the_liveness_check_is_not_called_lost 竞态假红

## 背景（生产实证 2026-08-31，监督面立案）

dev-fg-4999f5b29aeb（wf-66300e R1 重派单）acceptance 被该测试单独炸红
（2002 过 1 红），整单 refused。监督面亲验：同 worktree 隔离复跑 3/3 全绿；
该单 diff（research_pipeline/research_runner/research_bus）与 re_adopt/executors
零交集；红发生时本机并发 4 张 dd 单 + make verify（重负载放大竞态窗口）。
定性：**测试自身的时序竞态假红**，非产品缺陷。失败原文：

```
FAILED tests/test_re_adopt.py::TestFailureModes::test_result_landing_during_the_liveness_check_is_not_called_lost
E       AssertionError: a completed run was reported as lost
E       assert 'running' == 'succeeded'
```

一个 flaky 测试在本舰队的伤害被 gate 纪律放大：每张 dd 单的 acceptance、
监督面每次收割复跑、每次 make verify 都要掷一次骰子。

## 要求

1. **使该测试确定性**：消除对真实时间窗（sleep/轮询间隔）的依赖——用可控
   时钟/事件钩子/显式同步点重写该测试的竞态编排，使「result 在 liveness
   check 进行中落地」这一时序**由测试构造保证**而非碰运气。
2. **语义零放宽**：该测试钉住的产品性质不变——result 在 liveness 检查窗口内
   落地的 run 绝不得被判 lost。修完后把该性质的**已知阴性**（在本单前引入
   一个故意把这种 run 误判 lost 的产品侧变异，或引用 git 历史上的缺陷复现
   方式）在测试注释中注明可复现路径——测试必须还能失败。
3. **压力复证**：单测循环 ≥50 次（可用 pytest-repeat 或裸循环脚本，脚本随单
   入 repo scripts/ 或 tests/ 辅助）全绿，作为除颤完成的机械判据；修前同法
   须能观测到至少一次假红或给出为何本机无法稳定复现的如实说明（负载依赖）。
4. 只改测试与测试辅助；**产品代码（executors/agent_run.py 等）零改动**。
   若除颤过程中发现产品侧真缺陷，如实记 findings 升报，不在本单顺手修。

## 可复现验收

```dd-acceptance
make verify
bash -lc 'for i in $(seq 1 20); do uv run pytest tests/test_re_adopt.py -q -x >/dev/null 2>&1 || { echo "iter $i FAILED"; exit 1; }; done; echo "20 iters green"'
```

（循环式压力验收已真机空跑验证可执行；pytest-repeat 不在依赖内，勿引入。）

## 边界

- 只改 fleet-graph 仓 tests/（及必要的测试辅助/dev 依赖声明）；不动 src/。
- 遵循仓 AGENTS.md。
