"""真实图汇流须把本次副作用交给下一次 Goal 调用，并保留当前 DD 代际。"""

from types import SimpleNamespace

import pytest

from fleet_graph.graphs.dd_subgraph import ControlPlaneGateway
from test_r3_stop_response import FakeDdPort, dispatch_action, gate_action, make_deps, run_graph


def test_observation_budget_keeps_latest_generation_and_stage():
    plane = SimpleNamespace(
        get=lambda _: {"state": "running", "generation": 4, "stage": "implement"}
    )
    result = ControlPlaneGateway(plane, max_observations=1).observe(
        {"development_id": "dev-x", "generation": 1}, line_folder="wf-x"
    )
    assert result["generation"] == 4
    assert result["stage"] == "implement"
    assert result["state"] == "in_flight"


def test_parallel_results_reach_coordinator_once_with_actual_round():
    dd = FakeDdPort()
    dd.answers = [
        {"dd_result": {"development_id": f"dev-{i}", "state": "in_flight", "generation": 3}}
        for i in range(2)
    ]
    deps = make_deps(
        [
            {"verdict": "continue", "next_prompt": "先检查当前设计"},
            {
                "verdict": "continue",
                "next_prompt": "再检查验收边界",
                "actions": [dispatch_action("a"), dispatch_action("b")],
            },
            {"verdict": "continue", "next_prompt": "最后检查发布配置"},
            {"verdict": "done"},
        ],
        dd=dd,
    )
    run_graph(deps)
    inputs = deps.coordinator.calls
    results = inputs[2]["action_results"]
    assert len(dd.payloads) == 2
    assert len(results) == 2
    assert all(r["receipt"]["status"] == "consumed" for r in results), results
    assert {r["round"] for r in results} == {2}
    assert {r["receipt"]["development_id"] for r in results} == {"dev-0", "dev-1"}
    assert all(r["dd_result"]["generation"] == 3 for r in results)
    assert "action_results" not in inputs[3]


def test_dispatch_failure_reaches_next_goal_input():
    class BrokenPort:
        def invoke(self, *args, **kwargs):
            raise RuntimeError("launch unavailable")

    deps = make_deps(
        [
            {"verdict": "continue", "next_prompt": "检查失败原因", "actions": [dispatch_action()]},
            {"verdict": "done"},
        ],
        dd=BrokenPort(),
    )
    run_graph(deps)
    receipt = deps.coordinator.calls[1]["action_results"][0]["receipt"]
    assert receipt["status"] == "failed"
    assert receipt["reason"] == "dispatch_fault"
    assert "launch unavailable" in receipt["detail"]


def test_unwired_action_refusal_reaches_next_goal_input():
    deps = make_deps(
        [
            {
                "verdict": "continue",
                "next_prompt": "检查未接线错误",
                "actions": [dispatch_action()],
            },
            {"verdict": "done"},
        ]
    )
    run_graph(deps)
    receipt = deps.coordinator.calls[1]["action_results"][0]["receipt"]
    assert receipt["status"] == "failed"
    assert receipt["reason"] == "consumer_unwired"


@pytest.mark.parametrize("with_dispatch", [False, True])
def test_gate_join_continues_once_and_delivers_mixed_results(with_dispatch):
    calls = []

    def consume(action, **kwargs):
        calls.append(action)
        return {
            "status": "consumed",
            "kind": action["kind"],
            "idempotency_key": action["idempotency_key"],
        }

    dd = FakeDdPort()
    dd.answers = [{"dd_result": {"development_id": "dev-new", "state": "in_flight"}}]
    actions = [gate_action("gate-a"), gate_action("gate-b")]
    if with_dispatch:
        actions.append(dispatch_action("dispatch-c"))
    deps = make_deps(
        [
            {"verdict": "continue", "next_prompt": "根据本轮回执继续检查", "actions": actions},
            {"verdict": "done"},
        ],
        dd=dd,
        gate=SimpleNamespace(consume=consume),
    )
    state = run_graph(deps)
    assert state["terminal"] == "done"
    assert len(calls) == 2
    assert len(dd.payloads) == int(with_dispatch)
    assert len(deps.coordinator.calls) == 2
    assert len(deps.coordinator.calls[1]["action_results"]) == len(actions)


def test_checkpoint_resume_at_join_preserves_results_without_redispatch():
    from langgraph.checkpoint.memory import InMemorySaver

    from fleet_graph.graphs.goal_line import build_goal_line_graph

    dd = FakeDdPort()
    dd.answers = [
        {"dd_result": {"development_id": "dev-resume", "generation": 2, "state": "in_flight"}}
    ]
    deps = make_deps(
        [
            {"verdict": "continue", "next_prompt": "等待交接结果", "actions": [dispatch_action()]},
            {"verdict": "done"},
        ],
        dd=dd,
    )
    graph = build_goal_line_graph(deps).compile(
        checkpointer=InMemorySaver(), interrupt_before=["action_join"]
    )
    config = {"configurable": {"thread_id": "resume-handoff"}}
    graph.invoke({"round_no": 1}, config=config)
    assert len(dd.payloads) == 1
    assert graph.get_state(config).next == ("action_join",)
    state = graph.invoke(None, config=config)
    assert state["terminal"] == "done"
    assert len(dd.payloads) == 1
    result = deps.coordinator.calls[1]["action_results"][0]
    assert result["dd_result"]["generation"] == 2
