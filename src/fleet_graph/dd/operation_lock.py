"""按开发单串行化控制面操作；跨进程互斥，同线程嵌套可重入。"""

import fcntl
import threading
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

_local = threading.local()


@contextmanager
def operation_lock(path: Path, timeout: float = 30):
    key = str(path.resolve())
    held = getattr(_local, "held", None)
    if held is None:
        held = _local.held = set()
    if key in held:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("开发单控制操作正在执行，请重试") from None
                time.sleep(0.05)
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)
            fcntl.flock(stream, fcntl.LOCK_UN)


def serialized(method):
    @wraps(method)
    def guarded(self, development_id, *args, **kwargs):
        with self.operation_lock(development_id):
            return method(self, development_id, *args, **kwargs)

    return guarded
