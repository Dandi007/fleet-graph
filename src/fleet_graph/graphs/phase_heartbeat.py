"""执行阶段内持续报告引擎活性；调用期限仍由 runtime 管理。"""

from contextlib import contextmanager, suppress
from threading import Event, Thread


@contextmanager
def phase_heartbeat(artifacts, round_no: int, phase: str, interval: float = 20):
    stopped = Event()

    def beat():
        while not stopped.wait(interval):
            with suppress(Exception):
                artifacts.heartbeat(round_no, phase, force=True)

    thread = Thread(target=beat, name="fleet-phase-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1)
