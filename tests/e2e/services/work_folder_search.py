"""启动 canonical keyword 搜索服务及保鲜进程，存储只属于本次容器。"""

import contextlib
import hashlib
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


def validate_index(root, storage):
    """用文件集合、内容摘要和来源字段验证真实索引，没有索引不能当作空结果成功。"""
    root, storage = Path(root).resolve(), Path(storage)
    manifest = json.loads((storage / "index-manifest.json").read_text())
    records = [
        json.loads(line)
        for line in (storage / "lancedb/chunks.jsonl").read_text().splitlines()
        if line.strip()
    ]
    source_id = hashlib.sha256(str(root).encode()).hexdigest()
    sources = manifest.get("sources", [])
    if (
        len(sources) != 1
        or sources[0].get("root") != str(root)
        or sources[0].get("source_id") != source_id
    ):
        raise RuntimeError("搜索索引来源与 Work Folder 不一致")
    files = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*.md")
        if not any(part.startswith(".") for part in path.relative_to(root).parts)
    }
    indexed = manifest.get("files", {})
    if not files or set(files) != set(indexed):
        raise RuntimeError("搜索索引文件集合未覆盖当前 Work Folder")
    if any(indexed[name].get("sha256") != digest for name, digest in files.items()):
        raise RuntimeError("搜索索引内容落后于 Work Folder 当前文件")
    if not records or len(records) != manifest.get("chunk_count"):
        raise RuntimeError("搜索 chunks 缺失、为空或尚未完整写入")
    counts = {name: 0 for name in files}
    for record in records:
        name = record.get("relative_path")
        if (
            name not in files
            or record.get("sha256") != files[name]
            or record.get("source_root") != str(root)
            or record.get("source_id") != source_id
        ):
            raise RuntimeError("搜索 chunk 来源或内容摘要不一致")
        counts[name] += 1
    if any(counts[name] != indexed[name].get("chunks") for name in files):
        raise RuntimeError("搜索 chunk 数量与文件 manifest 不一致")
    return {"indexed_files": len(files), "chunks": len(records), "source_id": source_id}


@contextlib.contextmanager
def running_search(root):
    source = Path("/opt/src/agent-knowledge")
    cache = Path.home() / ".cache/agent-knowledge/Zettelkasten"
    storage = Path("/data/search")
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
    failures = []
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
            invalid_since = None
            while not stopping.wait(1):
                if any(process.poll() is not None for process in processes):
                    failures.append("搜索服务或 indexer 进程退出")
                else:
                    try:
                        validate_index(root, storage)
                        invalid_since = None
                    except Exception as error:
                        if invalid_since is None:
                            invalid_since = time.monotonic()
                        if time.monotonic() - invalid_since >= 20:
                            failures.append(f"搜索索引持续20秒无效或未保鲜：{error}")
                if failures:
                    print(f"{failures[-1]}；停止 Work Folder 以避免假健康", flush=True)
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
        if failures:
            raise RuntimeError(failures[-1])
