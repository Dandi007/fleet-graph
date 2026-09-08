"""生成容器专属配置；不修改被冻结的 Fleet/runtime 源码。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import yaml

MODEL = "deepseek-v4-pro@opencode"
ROUTE = MODEL + "/gw"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def version(*argv: str) -> str:
    return subprocess.check_output(argv, text=True, stderr=subprocess.STDOUT).strip()


def read_source_manifest(assets: Path) -> dict:
    manifest = json.loads((assets / "source-manifest.json").read_text())
    if not isinstance(manifest, dict):
        raise ValueError("source-manifest 必须为 JSON object")
    for required in ("fleet_commit", "runtime_commit"):
        if required not in manifest:
            raise ValueError(f"source-manifest 缺少 {required}")
    for name, value in manifest.items():
        if name.endswith("_commit") and (
            not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{40}", value) is None
        ):
            raise ValueError(f"source-manifest 的 {name} 必须为完整 40 位十六进制 commit")
    return manifest


def source_path(fleet: Path, value: str, name: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ValueError(f"source-manifest 的 {name} 必须为 Fleet 源码内的相对路径")
    path = (fleet / value).resolve()
    if not path.is_relative_to(fleet.resolve()):
        raise ValueError(f"source-manifest 的 {name} 超出 Fleet 源码范围")
    return path


def configure(state=Path("/state"), fleet=Path("/opt/fleet"), assets=Path("/opt/candidate")):
    source_manifest = read_source_manifest(assets)
    config_source = source_path(
        fleet, source_manifest.get("config_path", "config/codex.json"), "config_path"
    )
    prompts_source = source_path(
        fleet,
        source_manifest.get(
            "prompt_dir", str(config_source.parent.relative_to(fleet.resolve()) / "prompts")
        ),
        "prompt_dir",
    )
    state.mkdir(parents=True, exist_ok=True)
    config_dir = state / "config"
    config_dir.mkdir(exist_ok=True)
    original = assets / "original-profiles"
    profiles = config_dir / "runtime-profiles"
    profiles.mkdir(exist_ok=True)
    for name in ("harness", "roles", "consumers"):
        target = profiles / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir()
    for source in sorted((original / "harness").glob("fleet-*.yaml")):
        shutil.copyfile(source, profiles / "harness" / source.name)
    routes = yaml.safe_load((original / "routes.yaml").read_text())
    route = routes["routes"][ROUTE]
    if route["auth"] != "static" or route["runtimes"] != ["opencode"]:
        raise ValueError("候选默认路由不是预期的 OpenCode static 网关配置")
    route["opencode_provider"]["gateway"]["options"]["baseURL"] = "http://gateway:15722/v1"
    (profiles / "routes.yaml").write_text(
        yaml.safe_dump(
            {
                "routes": {ROUTE: route},
                "chains": {MODEL: {"routes": [ROUTE], "cost_boundary": ROUTE}},
            },
            allow_unicode=True,
            sort_keys=False,
        )
    )
    (profiles / "agents.yaml").write_text(
        yaml.safe_dump(
            {
                "agents": {
                    "candidate-e2e": {"runtime": "opencode", "route": ROUTE},
                }
            }
        )
    )
    servers = {
        "fleet-graph-comparison-codex": {
            "transport": "remote",
            "url": "http://candidate:15611/mcp",
            "runtimes": ["opencode"],
        },
        "katana-work-folder-mcp": {
            "transport": "remote",
            "url": "http://work-folder:5602/mcp",
            "runtimes": ["opencode"],
        },
    }
    (profiles / "mcp.yaml").write_text(yaml.safe_dump({"servers": servers}))
    fleet_config = json.loads(config_source.read_text())
    fleet_config.update(
        agent_run="/opt/agent-runtime/bin/agent-run",
        agent_session="/opt/agent-runtime/bin/agent-session",
        work_folder_mcp="http://work-folder:5602/mcp",
    )
    prompt_dir = config_dir / "prompts"
    prompt_dir.mkdir(exist_ok=True)
    for role, settings in fleet_config["roles"].items():
        if settings["runtime"] != "opencode" or settings["model"] != MODEL:
            raise ValueError("冻结 candidate 的角色默认模型与验收契约不符")
        source = prompts_source / f"{role}.md"
        # 保留原角色职责，附加本次明确授权阶段；覆盖文本与哈希进入 manifest。
        prompt = source.read_text()
        override = (
            "\n\n本次独立 Docker E2E 的阶段授权覆盖上述开发阶段限制："
            "允许在 /workspace 下测试专用仓库、分支和 GitHub PR 执行真实流程与验证。"
            "所有运行只发生在本次容器及专用测试资源中。"
            "禁止访问宿主目录、生产 main、共享服务或其他实验组。"
            "工作记录只经本次 work-folder MCP；"
            "不得模拟工具结果、伪造证据或把未运行检查写成通过。\n"
        )
        target = prompt_dir / f"{role}.md"
        target.write_text(prompt + override)
        settings["system_prompt_file"] = str(target)
    config_path = config_dir / "fleet.json"
    config_path.write_text(json.dumps(fleet_config, ensure_ascii=False, indent=2) + "\n")
    generated = {
        str(p.relative_to(config_dir)): digest(p)
        for p in sorted(config_dir.rglob("*"))
        if p.is_file()
    }
    manifest = {
        "schema_version": 1,
        "source": source_manifest,
        "tools": {
            "python": version("python", "--version"),
            "bun": version("bun", "--version"),
            "opencode": version("opencode", "--version"),
            "git": version("git", "--version"),
            "gh": version("gh", "--version").splitlines()[0],
        },
        "model": MODEL,
        "route": ROUTE,
        "gateway": "http://gateway:15722/v1",
        "fleet_mcp": "http://candidate:15611/mcp",
        "fleet_cli": "http://127.0.0.1:15612/mcp",
        "work_folder_mcp": "http://work-folder:5602/mcp",
        "configuration_sha256": generated,
        "launch_adapter": {
            "config_path": str(config_source.relative_to(fleet.resolve())),
            "prompt_dir": str(prompts_source.relative_to(fleet.resolve())),
        },
        "overrides": [
            "仅保留默认 OpenCode static 网关路由，无 native subscription 或 fallback",
            "runtime profiles 由独立生成目录接入，仅注册 candidate 与 work-folder MCP",
            "原角色 prompt 保留，附加独立 Docker E2E 阶段授权",
            "原 fleet harness 保留，Fleet CLI 保持 loopback，由容器内 TCP relay 转发 HTTP",
            "宿主 HOME、socket、凭证配置未挂入；测试 secrets 只从文件注入环境",
            "agent-bus 通过原生 AGENT_BUS_URL / AGENT_BUS_TOKEN_FILE 接入独立容器；"
            "启动前真实注册协议与 board:agent-runs，失败阻止 Fleet 启动",
        ],
        "dependencies": {
            "python": (assets / "python-packages.txt").read_text().splitlines(),
            "runtime_source_lock": False,
            "runtime_resolved_lock": "/opt/agent-runtime/bun.lock",
            "runtime_resolved_lock_sha256": digest(Path("/opt/agent-runtime/bun.lock"))
            if Path("/opt/agent-runtime/bun.lock").is_file()
            else None,
            "limitation": (
                "冻结 runtime 未提交 lock；构建副本解析依赖并保存 lock，不声称跨构建依赖完全复现"
            ),
        },
        "agent_bus": {
            "bootstrap": {"status": "pending"},
            "url": os.environ.get("AGENT_BUS_URL"),
            "token_file_present": Path(
                os.environ.get("AGENT_BUS_TOKEN_FILE", "/run/secrets/agent_bus_token")
            ).is_file(),
        },
        "health_scope": "health 只验证真实 Fleet MCP tools/list；不代表模型调用或业务 E2E 已通过",
    }
    (state / "candidate-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    return config_path
