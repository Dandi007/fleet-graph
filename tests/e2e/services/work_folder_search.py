"""启动 canonical keyword 搜索服务及保鲜进程，存储只属于本次容器。"""

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


@contextlib.contextmanager
def running_search(root):
    source = Path("/opt/src/agent-knowledge")
    cache = Path.home() / ".cache/agent-knowledge/Zettelkasten"
    storage = root / ".katana/runtime/search"
    storage.mkdir(parents=True, exist_ok=True)
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.is_symlink():
        if cache.resolve() != storage.resolve():
            raise RuntimeError("搜索缓存不能指向本次数据卷以外的路径")
    elif cache.exists():
        raise RuntimeError("拒绝接管已存在的搜索缓存")
    else:
        cache.symlink_to(storage, target_is_directory=True)
    env = {**os.environ, "VAULT_SEARCH_DISABLE_VECTOR": "1", "PYTHONPATH": str(source)}
    processes = []
    logs = []
    stopping = threading.Event()
    try:
        # 先用原始 CLI 构建，保证初次查询已有真实索引与来源过滤字段。
        subprocess.run(
            [
                sys.executable,
                str(source / "scripts/build_lancedb_index.py"),
                "--root",
                str(root),
                "--scope",
                ".",
            ],
            env=env,
            cwd=source,
            check=True,
            timeout=60,
        )
        commands = [
            (
                "indexer",
                [
                    sys.executable,
                    str(source / "scripts/watch_and_index.py"),
                    "--root",
                    str(root),
                    "--scope",
                    ".",
                    "--quiet",
                    "0.5",
                    "--max-wait",
                    "2",
                ],
            ),
            ("search", [sys.executable, "-m", "service.app"]),
        ]
        for name, command in commands:
            log = (storage / f"{name}.log").open("ab", buffering=0)
            logs.append(log)
            processes.append(subprocess.Popen(command, env=env, cwd=source, stdout=log, stderr=log))
        deadline = time.monotonic() + 30
        while True:
            if any(process.poll() is not None for process in processes):
                raise RuntimeError("真实搜索服务或 indexer 启动失败，见数据卷 search 日志")
            try:
                with urllib.request.urlopen("http://127.0.0.1:18082/health", timeout=2) as response:
                    health = json.load(response)
                if health == {"ok": True, "embedding": "disabled"}:
                    break
            except (OSError, urllib.error.URLError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("真实搜索服务未在期限内就绪")
            time.sleep(0.2)

        def monitor():
            while not stopping.wait(1):
                if any(process.poll() is not None for process in processes):
                    print("搜索依赖退出，停止 Work Folder 以避免假健康", flush=True)
                    os.kill(os.getpid(), signal.SIGTERM)
                    return

        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()
        yield
    finally:
        stopping.set()
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()
