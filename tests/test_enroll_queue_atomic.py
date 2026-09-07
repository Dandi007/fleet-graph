"""独立验证入编队列跨进程不丢写、失败不截断，以及读写不共享可变引用。"""

import multiprocessing

import pytest

from fleet_graph.goal_enroll.contract import GoalEnrollError
from fleet_graph.goal_enroll.queue import QUEUE_FILE, EnrollQueue


def _submit_after_barrier(root, barrier, number):
    queue = EnrollQueue(root)
    barrier.wait(timeout=10)
    queue.submit({"folder_id": f"wf-{number}", "alias": f"line-{number}"})


def test_parallel_processes_do_not_lose_distinct_submissions(tmp_path):
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(6)
    workers = [
        context.Process(target=_submit_after_barrier, args=(tmp_path, barrier, i)) for i in range(6)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0
    assert len(EnrollQueue(tmp_path)) == 6


def test_stale_instance_reloads_before_transition_and_read(tmp_path):
    first, second = EnrollQueue(tmp_path), EnrollQueue(tmp_path)
    first.submit({"folder_id": "wf-one"})
    second.mark_admitted("wf-one", decided_by="supervisor", decision_ref="decision-one")
    assert first.get("wf-one")["status"] == "admitted"
    with pytest.raises(GoalEnrollError):
        first.withdraw("wf-one", by="other")
    assert EnrollQueue(tmp_path).get("wf-one")["status"] == "admitted"


def test_failed_replace_preserves_queue_and_local_state(tmp_path, monkeypatch):
    queue = EnrollQueue(tmp_path)
    queue.submit({"folder_id": "wf-before"})
    original = (tmp_path / QUEUE_FILE).read_bytes()
    import fleet_graph.goal_enroll.queue as module

    def fail_replace(*args):
        raise OSError("模拟原子提交之前中断")

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "replace", fail_replace)
        with pytest.raises(OSError):
            queue.submit({"folder_id": "wf-after"})
    assert (tmp_path / QUEUE_FILE).read_bytes() == original
    assert queue.get("wf-after") is None
    assert not list(tmp_path.glob(".enroll-*"))
    assert not queue.submit({"folder_id": "wf-after"})["already_pending"]


def test_rejection_history_reloads_without_duplicate_or_lost_entries(tmp_path):
    first, second = EnrollQueue(tmp_path), EnrollQueue(tmp_path)
    first.record_rejection("wf", code="one", detail="第一条")
    second.record_rejection("wf", code="two", detail="第二条")
    assert [entry["code"] for entry in first.rejections("wf")] == ["one", "two"]
    assert len(first.rejections("wf")) == 2


def test_corrupt_persisted_queue_is_not_silently_dropped(tmp_path):
    path = tmp_path / QUEUE_FILE
    path.write_text('{"folder_id": "wf-good"}\n{"folder_id":')
    original = path.read_bytes()
    with pytest.raises(ValueError):
        EnrollQueue(tmp_path)
    assert path.read_bytes() == original


def test_read_result_cannot_mutate_in_memory_queue():
    queue = EnrollQueue()
    queue.submit({"folder_id": "wf"})
    queue.get("wf")["status"] = "admitted"
    assert queue.get("wf")["status"] == "pending"
