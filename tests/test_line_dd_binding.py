"""真实启动入口必须绑定DD；图单元测试的注入不能替代部署接线。"""

from pathlib import Path

from fleet_graph.graphs.dd_subgraph import ControlPlaneGateway
from fleet_graph.graphs.runner import LineConfig, bind_dd_dependencies, build_line


def test_production_line_has_both_action_consumers(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_GRAPH_DD_ROOT", str(tmp_path / "isolated-dd"))
    config = bind_dd_dependencies(LineConfig(folder_id="wf-canary", seat="test", run_root=tmp_path))
    assert isinstance(config.dd_gateway, ControlPlaneGateway)
    assert config.dd_gateway.plane is config.dd_gate_plane
    assert config.dd_gate_plane.root == tmp_path / "isolated-dd"
    assert config.dd_gate_plane.plugin_binding == tmp_path / "isolated-dd/plugin-binding.json"
    assert Path(config.dd_gate_plane.executable).is_absolute()
    _, deps = build_line(config)
    assert deps.dd is not None
    assert deps.gate is not None


def test_explicit_root_and_gateway_are_preserved(tmp_path, monkeypatch):
    from fleet_graph.dd.control_plane import DdControlPlane

    monkeypatch.setenv("FLEET_GRAPH_DD_ROOT", str(tmp_path / "wrong"))
    plane = DdControlPlane(root=tmp_path / "explicit")
    gateway = ControlPlaneGateway(plane)
    config = bind_dd_dependencies(
        LineConfig(
            folder_id="wf-canary",
            seat="test",
            run_root=tmp_path,
            dd_root=plane.root,
            dd_gateway=gateway,
        )
    )
    assert config.dd_gate_plane is plane
    assert config.dd_gateway is gateway
    assert config.dd_root == plane.root


def test_plugin_binding_override_remains_isolated(tmp_path, monkeypatch):
    binding = tmp_path / "config/plugin-binding.json"
    monkeypatch.setenv("FLEET_GRAPH_DD_PLUGIN_BINDING", str(binding))
    config = bind_dd_dependencies(LineConfig(folder_id="wf-canary", seat="test", run_root=tmp_path))
    assert config.dd_gate_plane.plugin_binding == binding
