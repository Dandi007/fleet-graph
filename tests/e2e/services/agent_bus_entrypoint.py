"""保留真实 bus 的 loopback 监听，由容器内 TCP 转发开放服务端口。"""

import os
import signal
import subprocess
import time
from pathlib import Path


def main() -> int:
    for name in ("BUS_ADMIN_TOKEN", "BUS_GATEWAY_TOKEN"):
        secret = Path("/run/secrets", name.lower())
        if secret.is_file():
            os.environ[name] = secret.read_text(encoding="utf-8").strip()
        if len(os.environ.get(name, "")) < 32:
            raise RuntimeError(f"{name} 必须由本次测试注入，且至少 32 个字符")
    if os.environ["BUS_ADMIN_TOKEN"] == os.environ["BUS_GATEWAY_TOKEN"]:
        raise RuntimeError("本次测试的 admin 与 gateway token 必须不同")
    for name in ("state", "tokens"):
        Path("/data/agent-bus", name).mkdir(parents=True, exist_ok=True)
    processes = []
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        for process in processes:
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        processes.append(subprocess.Popen(["agent-bus-server"]))
        processes.append(
            subprocess.Popen(
                [
                    "socat",
                    "TCP-LISTEN:7470,bind=0.0.0.0,reuseaddr,fork",
                    "TCP:127.0.0.1:7471",
                ]
            )
        )
        while not stopping:
            if any(process.poll() is not None for process in processes):
                return 1
            time.sleep(0.2)
        return 0
    finally:
        stop(None, None)
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
