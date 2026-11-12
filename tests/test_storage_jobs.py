import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from backend.jobs import JobService, TERMINAL
from backend.storage import JsonStore
from backend.app import create_app


class StubAssets:
    def __init__(self):
        self.lock = threading.RLock()

    def get(self, asset_id):
        return {"id": asset_id}


class StubProcessing:
    def __init__(self, hook=None):
        self.hook = hook

    def validate(self, steps):
        return steps

    def process(self, asset_id, steps, progress=None, cancelled=None):
        if self.hook:
            self.hook(asset_id)
        return {"id": f"result-{asset_id}", "name": f"结果 {asset_id}"}


def make_jobs(tmp_path, processing=None):
    settings = SimpleNamespace(workers=2, max_batch=24)
    return JobService(settings, JsonStore(tmp_path), StubAssets(), processing or StubProcessing())


def wait_for_terminal(jobs, job_id, timeout=10):
    deadline = time.monotonic() + timeout
    record = jobs.get(job_id)
    while record["status"] not in TERMINAL and time.monotonic() < deadline:
        time.sleep(0.01)
        record = jobs.get(job_id)
    assert record["status"] in TERMINAL, f"任务未结束：{record['status']}"
    return record


ASSET_A = "a" * 32
ASSET_B = "b" * 32


def test_atomic_concurrent_writes(tmp_path):
    store = JsonStore(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: store.insert("events", {"index": index}), range(40)))
    records = store.all("events")
    assert len(records) == 40
    assert {item["index"] for item in records} == set(range(40))
    assert not list(tmp_path.glob("*.tmp"))


def test_recovers_last_valid_backup(tmp_path):
    store = JsonStore(tmp_path)
    first = store.insert("events", {"message": "first"})
    store.insert("events", {"message": "second"})
    (tmp_path / "events.json").write_text("broken json", encoding="utf-8")
    recovered = JsonStore(tmp_path)
    assert recovered.get("events", first["id"])["message"] == "first"
    assert len(recovered.all("events")) == 1


def test_restart_marks_job_interrupted(tmp_path):
    root = tmp_path / "data"
    app = create_app(root, testing=True)
    store = app.extensions["studio"]["store"]
    job = store.insert("jobs", {"status": "running", "asset_ids": [], "results": []})
    app.extensions["studio"]["jobs"].shutdown()
    second = create_app(root, testing=True)
    try:
        record = second.extensions["studio"]["store"].get("jobs", job["id"])
        assert record["status"] == "interrupted"
    finally:
        second.extensions["studio"]["jobs"].shutdown()


def test_restart_finalizes_interrupted_job(tmp_path):
    root = tmp_path / "data"
    app = create_app(root, testing=True)
    store = app.extensions["studio"]["store"]
    job = store.insert("jobs", {
        "status": "cancelling",
        "asset_ids": [ASSET_A, ASSET_B, "c" * 32],
        "total": 3,
        "done": 1,
        "current_asset_id": ASSET_B,
        "results": [{"source_id": ASSET_A, "asset_id": "r1", "name": "r1"}],
        "errors": [],
    })
    app.extensions["studio"]["jobs"].shutdown()
    second = create_app(root, testing=True)
    try:
        record = second.extensions["studio"]["store"].get("jobs", job["id"])
        assert record["status"] == "interrupted"
        assert record["current_asset_id"] is None
        assert record["finished_at"] is not None
        assert "已保存 1 张结果" in record["message"]
        assert "共 3 张" in record["message"]
        events = second.extensions["studio"]["store"].all("events")
        assert any(event["action"] == "batch" and event["details"].get("job_id") == job["id"] for event in events)
    finally:
        second.extensions["studio"]["jobs"].shutdown()


def test_cancel_waiting_job(app, client, uploaded):
    processor = app.extensions["studio"]["processing"]
    processor.compute_lock.acquire()
    try:
        response = client.post("/api/jobs", json={"asset_ids": [uploaded["id"]], "steps": [{"operation": "color_invert", "params": {}}]})
        job_id = response.json["data"]["id"]
        cancelled = client.post(f"/api/jobs/{job_id}/cancel", json={})
        assert cancelled.status_code == 200
    finally:
        processor.compute_lock.release()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json["data"]
        if job["status"] == "cancelled":
            break
        time.sleep(0.02)
    assert job["status"] == "cancelled"
    assert job["results"] == []


def test_source_count_over_5000():
    from scripts.count_backend import count_file, ROOT
    records = [count_file(path) for path in (ROOT / "backend").rglob("*.py")]
    assert sum(item["code_lines_excluding_docstrings"] for item in records) > 5000


def test_cancel_queued_job_never_flaps_or_completes(tmp_path):
    jobs = make_jobs(tmp_path)
    try:
        # Hold the job lock so the worker cannot write "running" before the
        # cancellation lands; both calls re-enter the same RLock.
        with jobs.lock:
            record = jobs.submit([ASSET_A, ASSET_B], [{"operation": "op"}], "测试")
            cancelled = jobs.cancel(record["id"])
            assert cancelled["status"] == "cancelling"
        final = wait_for_terminal(jobs, record["id"])
        assert final["status"] == "cancelled"
        assert final["results"] == []
        assert final["done"] == 0
        assert final["started_at"] is None
    finally:
        jobs.shutdown()


def test_cancel_after_last_item_keeps_results(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def hook(asset_id):
        entered.set()
        assert release.wait(10)

    jobs = make_jobs(tmp_path, StubProcessing(hook))
    try:
        record = jobs.submit([ASSET_A], [{"operation": "op"}], "测试")
        assert entered.wait(10)
        # The item is saved and the worker is about to finalize; the cancel
        # must win over the late completion while the saved result is kept.
        with jobs.lock:
            release.set()
            cancelled = jobs.cancel(record["id"])
            assert cancelled["status"] == "cancelling"
        final = wait_for_terminal(jobs, record["id"])
        assert final["status"] == "cancelled"
        assert [item["asset_id"] for item in final["results"]] == [f"result-{ASSET_A}"]
        assert final["done"] == 1
        assert final["progress"] == 100
        assert "已保存 1 张结果" in final["message"]
    finally:
        release.set()
        jobs.shutdown()


def test_cancel_after_completion_is_noop(tmp_path):
    jobs = make_jobs(tmp_path)
    try:
        record = jobs.submit([ASSET_A], [{"operation": "op"}], "测试")
        final = wait_for_terminal(jobs, record["id"])
        assert final["status"] == "completed"
        again = jobs.cancel(record["id"])
        assert again["status"] == "completed"
        assert jobs.get(record["id"])["status"] == "completed"
    finally:
        jobs.shutdown()


def test_repeated_cancel_is_idempotent(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def hook(asset_id):
        entered.set()
        assert release.wait(10)

    jobs = make_jobs(tmp_path, StubProcessing(hook))
    try:
        record = jobs.submit([ASSET_A], [{"operation": "op"}], "测试")
        assert entered.wait(10)
        first = jobs.cancel(record["id"])
        assert first["status"] == "cancelling"
        second = jobs.cancel(record["id"])
        assert second["status"] == "cancelling"
        assert second["updated_at"] == first["updated_at"]
        release.set()
        final = wait_for_terminal(jobs, record["id"])
        assert final["status"] == "cancelled"
        third = jobs.cancel(record["id"])
        assert third["status"] == "cancelled"
        assert third["updated_at"] == final["updated_at"]
        events = [event for event in jobs.store.all("events") if event["action"] == "batch"]
        assert len(events) == 1
    finally:
        release.set()
        jobs.shutdown()


def test_shutdown_marks_active_job_interrupted(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def hook(asset_id):
        entered.set()
        assert release.wait(10)

    jobs = make_jobs(tmp_path, StubProcessing(hook))
    record = jobs.submit([ASSET_A], [{"operation": "op"}], "测试")
    assert entered.wait(10)
    closer = threading.Thread(target=jobs.shutdown)
    closer.start()
    deadline = time.monotonic() + 10
    while not jobs.cancellations[record["id"]].is_set():
        assert time.monotonic() < deadline
        time.sleep(0.005)
    release.set()
    closer.join(10)
    assert not closer.is_alive()
    final = jobs.get(record["id"])
    assert final["status"] == "interrupted"
    assert "中断" in final["message"]
    assert final["done"] == 1
    assert len(final["results"]) == 1
    assert final["finished_at"] is not None
