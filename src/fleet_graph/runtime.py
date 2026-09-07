"""异步 agent-runtime 边界：耐久启动意图、保守恢复与原始 Session 查询。"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


class RuntimeErrorContract(RuntimeError):
    """运行协议或配置错误，不是业务 verdict。"""


@dataclass(frozen=True)
class RoleConfig:
    runtime: str
    model: str
    harness: str
    write: bool = False
    timeout: int = 900
    hooks: str = "isolate"
    session_policy: str = "resume"
    compact_after: int = 0
    system_prompt_file: str | None = None

    def __post_init__(self) -> None:
        if self.system_prompt_file and not Path(self.system_prompt_file).is_absolute():
            raise ValueError("system_prompt_file 必须为绝对路径")
        if not self.runtime or not self.model or not self.harness:
            raise ValueError("runtime、model、harness 必须显式配置")
        if self.compact_after < 0:
            raise ValueError("compact_after 不能为负数")
        if self.timeout <= 0 or self.session_policy not in {"fresh", "resume", "compact"}:
            raise ValueError("非法 timeout 或 session_policy")
        if self.hooks not in {"isolate", "inherit"}:
            raise ValueError("hooks 必须为 isolate 或 inherit")


def _json(path: Path) -> Any:
    return json.loads(path.read_text())


def _atomic(path: Path, value: Any) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    temporary = Path(name)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Runtime:
    """路径均由本组配置提供，不隐式读取生产 current 或共享运行目录。"""

    def __init__(
        self,
        *,
        agent_run: str,
        agent_session: str,
        state_root: str,
        session_root: str,
        roles: dict[str, RoleConfig | dict],
    ) -> None:
        for value in (agent_run, agent_session, state_root, session_root):
            if not Path(value).is_absolute():
                raise ValueError("Runtime 路径必须显式配置为绝对路径")
        self.agent_run = agent_run
        self.agent_session = agent_session
        self.state_root = Path(state_root)
        self.session_root = session_root
        self.roles = {
            name: value if isinstance(value, RoleConfig) else RoleConfig(**value)
            for name, value in roles.items()
        }
        for name, config in self.roles.items():
            if name in {"scribe", "书记员"} and config.write:
                raise ValueError(f"{name} 必须使用只读 harness")

    async def _command(self, argv: list[str]) -> Any:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("FLEET_GRAPH_DECISION_")
            and key not in {"OPENCODE_DB", "OPENCODE_HOST", "OPENCODE_PORT", "OPENCODE_SKIP_START"}
        }
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            env=environment,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        try:
            value = json.loads(stdout)
        except (ValueError, UnicodeDecodeError) as error:
            raise RuntimeErrorContract(
                f"runtime CLI 返回非 JSON：{stderr.decode(errors='replace')[-2000:]}"
            ) from error
        # status 对 unknown 返回非零，但其结构化状态仍须恢复判断。
        unknown = (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(x, dict) and x.get("state") == "unknown" for x in value)
        )
        unknown = unknown or (isinstance(value, dict) and value.get("state") == "unknown")
        if process.returncode and not unknown:
            raise RuntimeErrorContract(f"runtime CLI 失败：{value!r}")
        return value

    def _path(self, run_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", run_id) or run_id in {".", ".."}:
            raise ValueError("非法 run_id")
        return self.state_root / run_id

    async def start(
        self,
        run_id: str,
        role: str,
        workspace: str,
        prompt: dict,
        output_schema: dict,
        session_scope: str,
    ) -> dict:
        config = self.roles[role]
        Draft202012Validator.check_schema(output_schema)
        directory = self._path(run_id)
        request = dict(
            role=role,
            workspace=workspace,
            prompt=prompt,
            output_schema=output_schema,
            session_scope=session_scope,
            config=asdict(config),
        )
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
        lock = await asyncio.to_thread((directory / "lock").open, "a")
        try:
            while True:
                try:
                    await asyncio.to_thread(fcntl.flock, lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.01)
            intent_path = directory / "intent.json"
            if await asyncio.to_thread(intent_path.exists):
                intent = await asyncio.to_thread(_json, intent_path)
                if intent["fingerprint"] != fingerprint:
                    raise RuntimeErrorContract("同一 run_id 不能绑定不同请求")
                # 即使进程在 spawn 与 ticket 写盘之间崩溃，也绝不盲目再派。
                if intent["stage"] != "prepared":
                    return dict(run_id=run_id, session_root=self.session_root, adopted=True)
            else:
                intent = dict(fingerprint=fingerprint, stage="prepared", request=request)
                await asyncio.to_thread(_atomic, intent_path, intent)
            await asyncio.to_thread(_atomic, directory / "prompt.json", prompt)
            await asyncio.to_thread(_atomic, directory / "schema.json", output_schema)
            argv = [
                self.agent_run,
                "--detach",
                "--run-id",
                run_id,
                "--session-root",
                self.session_root,
                "--json",
                "--runtime",
                config.runtime,
                "--model",
                config.model,
                "--harness",
                config.harness,
                "--timeout",
                str(config.timeout),
                "--cwd",
                workspace,
                "--prompt-file",
                str(directory / "prompt.json"),
                "--output-schema",
                str(directory / "schema.json"),
                "--structured",
                "--hooks",
                config.hooks,
                "--label",
                f"fleet_role={role}",
            ]
            if config.system_prompt_file:
                argv.extend(["--system-prompt-file", config.system_prompt_file])
            if config.write:
                argv.append("--write")
            if config.session_policy != "fresh":
                previous = await self._previous_session(session_scope)
                if previous:
                    argv.extend(["--resume", previous])
                    if config.session_policy == "compact":
                        argv.append("--compact")
                        if config.compact_after:
                            argv.extend(["--compact-after", str(config.compact_after)])
            await asyncio.to_thread(_atomic, directory / "argv.json", argv)
            intent["stage"] = "dispatching"
            await asyncio.to_thread(_atomic, intent_path, intent)
            result = await self._command(argv)
            if not isinstance(result, dict) or result.get("run_id") != run_id:
                raise RuntimeErrorContract("启动回执缺少匹配的 run_id")
            intent.update(stage="started", launch=result)
            await asyncio.to_thread(_atomic, intent_path, intent)
            return dict(run_id=run_id, session_root=self.session_root, adopted=False)
        finally:
            await asyncio.to_thread(lock.close)

    async def _previous_session(self, scope: str) -> str | None:
        path = self.state_root / "scopes" / hashlib.sha256(scope.encode()).hexdigest()
        if await asyncio.to_thread(path.exists):
            return (await asyncio.to_thread(_json, path)).get("run_dir")
        return None

    async def recover(self, run_id: str) -> dict | None:
        directory = self._path(run_id)
        if await asyncio.to_thread(directory.is_dir):
            return dict(run_id=run_id, session_root=self.session_root, adopted=True)
        return None

    async def inspect(self, ticket: dict) -> dict:
        run_id = ticket["run_id"]
        directory = self._path(run_id)
        value = await self._command(
            [
                self.agent_run,
                "status",
                run_id,
                "--session-root",
                self.session_root,
                "--json",
            ]
        )
        status = value[0] if isinstance(value, list) and len(value) == 1 else value
        if not isinstance(status, dict) or status.get("run_id") != run_id:
            raise RuntimeErrorContract("status 回执与 run_id 不匹配")
        state = status.get("state")
        if state in {"unknown", "lost"}:
            return dict(
                status="lost",
                output=None,
                session_ref=run_id,
                error="launch_uncertain",
                recovery="inspect_before_explicit_retry",
            )
        if state not in {"running", "succeeded", "failed", "lost"}:
            raise RuntimeErrorContract("非法 runtime state")
        result = status.get("result") or {}
        output = result.get("structured_result")
        protocol_error = None
        if state in {"succeeded", "failed"}:
            intent = await asyncio.to_thread(_json, directory / "intent.json")
            if state == "succeeded":
                if output is None or result.get("contract_error"):
                    state, protocol_error, output = "failed", "protocol_error", None
                else:
                    validator = Draft202012Validator(intent["request"]["output_schema"])
                    errors = await asyncio.to_thread(lambda: list(validator.iter_errors(output)))
                    if errors:
                        state, protocol_error, output = "failed", "protocol_error", None
            else:
                output = None
            # 失败轮次仍保留会话连续性，协议错误的原始输出只能经 result/Session 读取。
            if result.get("run_dir"):
                scope = intent["request"]["session_scope"]
                path = self.state_root / "scopes" / hashlib.sha256(scope.encode()).hexdigest()
                await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(_atomic, path, {"run_dir": result["run_dir"]})
        return dict(
            status=state,
            output=output,
            session_ref=run_id,
            result=result,
            error=protocol_error or result.get("contract_error"),
        )

    async def stop(self, ticket: dict) -> dict:
        self._path(ticket["run_id"])
        return await self._command(
            [
                self.agent_run,
                "stop",
                ticket["run_id"],
                "--session-root",
                self.session_root,
                "--json",
            ]
        )

    async def session(self, ticket: dict, offset: int = 0, limit: int = 100) -> dict:
        self._path(ticket["run_id"])
        if offset < 0 or not 1 <= limit <= 1000:
            raise ValueError("非法 Session 分页范围")
        return await self._command(
            [
                self.agent_run,
                "history",
                ticket["run_id"],
                "--session-root",
                self.session_root,
                "--offset",
                str(offset),
                "--limit",
                str(limit),
            ]
        )
