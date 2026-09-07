"""程序验收独立进程、耐久身份与完整日志；中断不伪装成业务失败。"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def identity(pid):
    try:
        fields = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"{boot}:{fields[19]}"
    except (OSError, IndexError, ValueError, TypeError):
        return None


def write_json(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(name).unlink(missing_ok=True)


def _read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _signal_group(process, sig):
    if process and process.get("identity") and identity(process["pid"]) == process["identity"]:
        try:
            if os.getpgid(process["pid"]) == process["pid"]:
                os.killpg(process["pid"], sig)
                return True
        except ProcessLookupError:
            pass
    return False


class Commands:
    def __init__(self, root, timeout=900):
        self.root = Path(root).resolve()
        if timeout <= 0:
            raise ValueError("验收 timeout 必须为正数")
        self.timeout = timeout

    def folder(self, run_id):
        if (
            not isinstance(run_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", run_id)
            or run_id in {".", ".."}
        ):
            raise ValueError("非法验收 run_id")
        return self.root / run_id

    async def start(self, run_id, workspace, commands):
        return await asyncio.to_thread(self._start, run_id, workspace, commands)

    def _start(self, run_id, workspace, commands):
        folder = self.folder(run_id)
        if not commands or any(
            not isinstance(command, str) or not command.strip() for command in commands
        ):
            raise ValueError("验收命令不能为空")
        self.root.mkdir(parents=True, exist_ok=True)
        config = {"workspace": workspace, "commands": commands, "timeout": self.timeout}
        with (self.root / "launch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ticket = {"run_id": run_id, "folder": str(folder), "kind": "command"}
            if folder.exists():
                existing = _read(folder / "input.json")
                if existing is not None and existing != config:
                    raise ValueError("同一验收 run_id 已绑定不同请求")
                # 无 input 的 mkdir 崩溃窗口保持不确定，不盲目重派。
                return ticket
            folder.mkdir()
            write_json(folder / "input.json", config)
            try:
                with (folder / "worker.log").open("ab") as log:
                    process = subprocess.Popen(
                        [sys.executable, "-m", "fleet_graph.commands", str(folder)],
                        stdout=log,
                        stderr=log,
                        stdin=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                write_json(
                    folder / "launcher.json",
                    {"pid": process.pid, "identity": identity(process.pid)},
                )
            except OSError as exc:
                write_json(
                    folder / "result.json",
                    {
                        "status": "lost",
                        "error": f"spawn_failed: {exc}",
                        "evidence_ref": str(folder),
                    },
                )
            return ticket

    async def recover(self, run_id):
        folder = self.folder(run_id)
        if await asyncio.to_thread(folder.is_dir):
            return {"run_id": run_id, "folder": str(folder), "kind": "command"}
        return None

    async def inspect(self, ticket):
        return await asyncio.to_thread(self._inspect, ticket)

    def _inspect(self, ticket):
        folder = self.folder(ticket["run_id"])
        result = _read(folder / "result.json")
        if result is not None:
            return result
        for name in ("process.json", "launcher.json"):
            data = _read(folder / name)
            if data and data.get("identity") and identity(data["pid"]) == data["identity"]:
                return {"status": "running"}
        return {
            "status": "lost",
            "error": "launch_uncertain",
            "detail": "验收进程中断或启动状态不明",
            "evidence_ref": str(folder),
        }

    async def stop(self, ticket):
        return await asyncio.to_thread(self._stop, ticket)

    def _stop(self, ticket):
        folder = self.folder(ticket["run_id"])
        process = _read(folder / "process.json") or _read(folder / "launcher.json")
        # worker 先收到停止标记，子命令单独进程组也必须被停止。
        sent = _signal_group(process, signal.SIGTERM)
        _signal_group(_read(folder / "child.json"), signal.SIGKILL)
        if sent:
            deadline = time.monotonic() + 2
            while identity(process["pid"]) == process["identity"] and time.monotonic() < deadline:
                time.sleep(0.02)
            _signal_group(_read(folder / "child.json"), signal.SIGKILL)
            _signal_group(process, signal.SIGKILL)
        return {"stopped": sent}


def worker(folder):
    folder = Path(folder)
    interrupted = False

    def terminate(signum, frame):
        nonlocal interrupted
        interrupted = True

    previous_handler = signal.signal(signal.SIGTERM, terminate)
    write_json(folder / "process.json", {"pid": os.getpid(), "identity": identity(os.getpid())})
    results = []
    child = None
    try:
        config = json.loads((folder / "input.json").read_text())
        for i, command in enumerate(config["commands"]):
            if interrupted:
                raise InterruptedError("验收被停止")
            with (folder / f"{i}.log").open("wb") as log:
                child = subprocess.Popen(
                    command,
                    cwd=config["workspace"],
                    shell=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
                write_json(
                    folder / "child.json", {"pid": child.pid, "identity": identity(child.pid)}
                )
                deadline = time.monotonic() + config["timeout"]
                while True:
                    if interrupted or time.monotonic() >= deadline:
                        # 此 child 是当前进程刚派出的句柄，即使shell退出也清理整个组。
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
                        if interrupted:
                            raise InterruptedError("验收被停止")
                        code = 124
                        break
                    try:
                        code = child.wait(timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            results.append({"command": command, "exit_code": code, "log": str(folder / f"{i}.log")})
            # 已退出 shell 可能留下后台后代，验收完成也不允许遗留执行。
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child = None
            if code:
                break
        if interrupted:
            raise InterruptedError("验收被停止")
        write_json(
            folder / "result.json",
            {
                "status": "succeeded" if all(r["exit_code"] == 0 for r in results) else "failed",
                "results": results,
                "evidence_ref": str(folder),
            },
        )
    except BaseException as exc:
        if child:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        write_json(
            folder / "result.json",
            {"status": "lost", "error": str(exc), "results": results, "evidence_ref": str(folder)},
        )
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    worker(sys.argv[1])
