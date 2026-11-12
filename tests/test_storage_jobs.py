import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from backend.storage import JsonStore
from backend.app import create_app


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


class FailingWriteStream:
    """File-like object that refuses writes, simulating a disk failure."""

    def __init__(self, real_file):
        self._real = real_file

    def write(self, payload):
        raise OSError("模拟磁盘写入失败")

    def flush(self):
        raise OSError("模拟磁盘写入失败")

    def fsync(self):
        raise OSError("模拟磁盘写入失败")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)


def _force_commit_failure(monkeypatch):
    import pathlib
    real_path_open = pathlib.Path.open

    def failing_path_open(self, mode="r", *args, **kwargs):
        stream = real_path_open(self, mode, *args, **kwargs)
        if "w" in mode and self.name.endswith(".tmp"):
            return FailingWriteStream(stream)
        return stream

    monkeypatch.setattr(pathlib.Path, "open", failing_path_open)


def test_failed_write_keeps_last_durable_state_in_cache(tmp_path, monkeypatch):
    store = JsonStore(tmp_path)
    record = store.insert("assets", {"name": "原始名称", "tags": [], "metadata": {}})

    _force_commit_failure(monkeypatch)
    with pytest.raises(OSError):
        store.update("assets", record["id"], {"name": "失败的改名", "tags": ["夹带标签"]})

    # 运行中的页面再次读取，只能看到最后真正保存成功的状态（含嵌套字段）
    current = store.get("assets", record["id"])
    assert current["name"] == "原始名称"
    assert current["tags"] == []
    assert not list(tmp_path.glob("*.tmp"))

    # 磁盘文件同样停留在上一次成功状态，而不是半成品
    on_disk = json.loads((tmp_path / "assets.json").read_text(encoding="utf-8"))
    assert on_disk["records"][record["id"]]["name"] == "原始名称"


def test_later_successful_write_does_not_carry_failed_edit(tmp_path, monkeypatch):
    store = JsonStore(tmp_path)
    first = store.insert("assets", {"name": "第一张", "tags": []})
    second = store.insert("assets", {"name": "第二张", "tags": []})

    _force_commit_failure(monkeypatch)
    with pytest.raises(OSError):
        store.update("assets", first["id"], {"name": "失败改名", "tags": ["失败标签"]})

    monkeypatch.undo()
    store.update("assets", second["id"], {"name": "第二张-已保存"})

    first_after = store.get("assets", first["id"])
    assert first_after["name"] == "第一张"
    assert first_after["tags"] == []
    assert store.get("assets", second["id"])["name"] == "第二张-已保存"

    # 重启后素材库仍然不含失败编辑
    restarted = JsonStore(tmp_path)
    reloaded = restarted.get("assets", first["id"])
    assert reloaded["name"] == "第一张"
    assert reloaded["tags"] == []


def test_failed_insert_and_delete_leave_cache_and_disk_untouched(tmp_path, monkeypatch):
    store = JsonStore(tmp_path)
    record = store.insert("assets", {"name": "唯一素材"})

    _force_commit_failure(monkeypatch)
    with pytest.raises(OSError):
        store.insert("assets", {"name": "不应存在的素材"})
    with pytest.raises(OSError):
        store.delete("assets", record["id"])

    assert len(store.all("assets")) == 1
    assert store.get("assets", record["id"])["name"] == "唯一素材"

    restarted = JsonStore(tmp_path)
    assert [item["id"] for item in restarted.all("assets")] == [record["id"]]


def test_corrupt_primary_is_restored_from_backup_on_next_read(tmp_path):
    store = JsonStore(tmp_path)
    first = store.insert("events", {"message": "first"})
    store.insert("events", {"message": "second"})
    assert (tmp_path / "events.json.bak").exists()

    (tmp_path / "events.json").write_text("broken json", encoding="utf-8")

    restarted = JsonStore(tmp_path)
    assert restarted.get("events", first["id"])["message"] == "first"
    assert len(restarted.all("events")) == 1
    # 主文件已被备份内容修复
    repaired = json.loads((tmp_path / "events.json").read_text(encoding="utf-8"))
    assert repaired["schema_version"] == 1
    assert first["id"] in repaired["records"]


def test_corrupt_primary_without_backup_raises_instead_of_empty_state(tmp_path):
    JsonStore(tmp_path).insert("events", {"message": "only copy"})
    (tmp_path / "events.json").write_text("broken json", encoding="utf-8")
    (tmp_path / "events.json.bak").unlink()

    with pytest.raises(RuntimeError, match="损坏且没有可用备份"):
        JsonStore(tmp_path)


def test_corrupt_primary_and_corrupt_backup_raises_instead_of_cache(tmp_path):
    JsonStore(tmp_path).insert("events", {"message": "only copy"})
    (tmp_path / "events.json").write_text("broken primary", encoding="utf-8")
    (tmp_path / "events.json.bak").write_text("broken backup", encoding="utf-8")

    with pytest.raises(RuntimeError, match="备份同样无法读取"):
        JsonStore(tmp_path)


def test_failed_asset_patch_returns_error_and_state_unchanged(client, uploaded, monkeypatch):
    store = client.application.extensions["studio"]["store"]
    _force_commit_failure(monkeypatch)

    response = client.patch(
        f"/api/assets/{uploaded['id']}",
        json={"name": "保存失败的名称", "tags": ["标签A"]},
    )

    assert response.status_code == 500
    assert response.json["ok"] is False
    # 服务仍在运行，读到的是最后一次成功保存的内容
    fresh = client.get(f"/api/assets/{uploaded['id']}").json["data"]
    assert fresh["name"] == uploaded["name"]
    assert fresh["tags"] == []
    record = store.get("assets", uploaded["id"])
    assert record["name"] == uploaded["name"]
    assert record["tags"] == []

