"""Fleet Graph 唯一启动入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from fleet_graph.commands import identity, write_json
from fleet_graph.runtime import RoleConfig
from fleet_graph.service import Control, build_mcp, locked


def load_config(path):
    config = json.loads(Path(path).read_text())
    for field in ("agent_run", "agent_session", "work_folder_mcp", "roles"):
        if not config.get(field):
            raise ValueError(f"缺少配置 {field}")
    for executable in ("agent_run", "agent_session"):
        if not Path(config[executable]).is_absolute() or not Path(config[executable]).is_file():
            raise ValueError(f"runtime 入口必须是现有绝对路径: {executable}")
    for role in ("goal", "impl", "cr", "fr", "scribe"):
        if role not in config["roles"]:
            raise ValueError(f"缺少角色 {role}")
        settings = RoleConfig(**config["roles"][role])
        if role == "scribe" and settings.write:
            raise ValueError("书记员不得获得写权限")
        if settings.system_prompt_file and not Path(settings.system_prompt_file).is_file():
            raise ValueError(f"角色系统说明文件不存在: {role}")
    if config.get("dd_capacity", 8) < 1 or config.get("warning_turns", 20) < 1:
        raise ValueError("容量和 warning 阈值必须为正数")
    return config


def should_exit(state):
    if state["status"] == "stopped":
        return True
    if state["status"] == "done":
        return not any(
            run.get("role") == "scribe" and run["status"] in {"running", "launching", "collected"}
            for run in state["runs"].values()
        )
    return bool(state["stop"]) and not any(
        run["status"] in {"running", "launching", "collected"} for run in state["runs"].values()
    )


async def run_engine(control, goal_id):
    store = control.store(goal_id)
    with locked(store.root / "engine.lock", nonblocking=True):
        write_json(
            store.root / "process.json", {"pid": os.getpid(), "identity": identity(os.getpid())}
        )
        engine = control.engine(store)
        try:
            while True:
                await engine.tick()
                state = store.read()
                if should_exit(state):
                    if any(r["status"] == "uncertain" for r in state["runs"].values()):
                        store.change(
                            "engine.awaiting_reconciliation",
                            {"reason": "保留不确定运行，释放引擎锁供外部核对恢复"},
                        )
                    break
                await asyncio.sleep(control.config.get("poll_interval", 0.2))
        except BaseException as exc:
            store.change(
                "engine.crashed",
                {"error": str(exc)},
                lambda s: s.update(status="stopped", stop="crash"),
            )
            raise
        finally:
            (store.root / "process.json").unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Fleet Graph MCP 与每 Goal 独立引擎")
    parser.add_argument("--version", action="version", version="fleet-graph 2.0.0")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "engine", "check-config"):
        command = sub.add_parser(name)
        command.add_argument("--config", required=True)
        if name != "check-config":
            command.add_argument("--root", required=True)
        if name == "engine":
            command.add_argument("--goal", required=True)
        if name == "serve":
            command.add_argument("--host", default="127.0.0.1")
            command.add_argument("--port", type=int, default=15611)
            command.add_argument("--transport", choices=["stdio", "http"], default="http")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command == "check-config":
        print(json.dumps({"valid": True, "roles": list(config["roles"])}, ensure_ascii=False))
        return
    control = Control(args.root, config)
    if args.command == "engine":
        asyncio.run(run_engine(control, args.goal))
    else:
        mcp = build_mcp(control)
        if args.transport == "stdio":
            mcp.run(transport="stdio")
        else:
            if args.host not in {"127.0.0.1", "::1", "localhost"}:
                raise ValueError("控制面默认仅允许 loopback；远程访问经有认证的代理")
            mcp.run(transport="http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
