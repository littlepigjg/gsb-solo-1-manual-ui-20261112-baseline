"""Ordering between a concurrent process-save and an asset delete.

Two windows share one process. The rules under test:

* If processing has already started using the source validly, its copy is
  published first, and the existing descendant guard then refuses the delete.
* If the delete succeeds first, processing fails cleanly with
  ``source_deleted`` and never writes a dangling copy.
* Saving must uphold the same rule (guarded insert), while multi-generation
  history, leaf deletion and a normal flip export keep working.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from backend.errors import SourceDeleted, StudioError


STEPS = [{"operation": "transform_flip", "params": {"axis": "horizontal"}}]


@pytest.fixture
def chain(app, client, uploaded):
    """root -> child -> grandchild, one flip per generation."""
    result = client.post("/api/process", json={"asset_id": uploaded["id"], "steps": STEPS}).json["data"]
    grandchild = client.post("/api/process", json={"asset_id": result["id"], "steps": STEPS}).json["data"]
    return [uploaded["id"], result["id"], grandchild["id"]]


def diagnostics_issues(client):
    return client.get("/api/diagnostics").json["data"]["issues"]


def is_leaf(store, asset_id):
    return not any(item.get("parent_id") == asset_id for item in store.all("assets"))


def test_processing_publishes_before_delete_refusal(app, client, uploaded):
    """Delete racing a process: once the copy lands, the guard rejects the delete."""
    assets = app.extensions["studio"]["assets"]
    store = app.extensions["studio"]["store"]
    published = threading.Event()
    release = threading.Event()

    real_save = assets.save

    def gated_save(*args, **kwargs):
        # The copy is committed (visible under the assets lock) before we pause.
        result = real_save(*args, **kwargs)
        published.set()
        release.wait(10)
        return result

    assets.save = gated_save
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            processing = pool.submit(
                app.extensions["studio"]["processing"].process, uploaded["id"], STEPS
            )
            assert published.wait(10), "processing never published its copy"
            deletion = pool.submit(assets.delete, uploaded["id"])
            release.set()
            result = processing.result()
    finally:
        assets.save = real_save
        release.set()

    assert result["parent_id"] == uploaded["id"]
    assert store.get("assets", result["id"])["parent_id"] == uploaded["id"]
    assert not is_leaf(store, uploaded["id"])
    with pytest.raises(StudioError) as refused:
        deletion.result()
    assert refused.value.code == "conflict"
    assert diagnostics_issues(client) == []


def test_delete_wins_and_save_fails_cleanly(app, client, uploaded, monkeypatch):
    """Delete commits at the save moment: no dangling copy, clear error, no media."""
    assets = app.extensions["studio"]["assets"]
    store = app.extensions["studio"]["store"]
    original = store.insert_guarded
    deleted = []

    def delete_first(collection, record, guard):
        # Exactly the losing interleaving: the delete wins, then the guarded
        # insert runs against the already-updated snapshot.
        if collection == "assets" and record.get("parent_id") == uploaded["id"] and not deleted:
            deleted.append(True)
            assets.delete(uploaded["id"])
        return original(collection, record, guard)

    monkeypatch.setattr(store, "insert_guarded", delete_first)
    with pytest.raises(SourceDeleted):
        app.extensions["studio"]["processing"].process(uploaded["id"], STEPS)
    monkeypatch.setattr(store, "insert_guarded", original)

    assert deleted == [True]
    assert store.all("assets") == []
    assert list(app.extensions["studio"]["settings"].media.glob("*.png")) == []
    assert diagnostics_issues(client) == []


def test_save_guard_publishes_nothing_when_source_deleted(app, client, uploaded):
    assets = app.extensions["studio"]["assets"]
    store = app.extensions["studio"]["store"]
    image, parent = assets.load(uploaded["id"])
    assets.delete(uploaded["id"])
    with pytest.raises(SourceDeleted):
        assets.save(image, name="副本.png", parent=parent)
    assert store.all("assets") == []
    assert list(assets.settings.media.glob("*.png")) == []


def test_multi_generation_history_and_leaf_delete(client, chain):
    for asset_id in chain:
        lineage = client.get(f"/api/assets/{asset_id}/history").json["data"]
        assert [item["id"] for item in lineage] == chain[:chain.index(asset_id) + 1]
    root, child, grandchild = chain
    assert client.delete(f"/api/assets/{grandchild}").status_code == 200
    assert client.delete(f"/api/assets/{child}").status_code == 200
    assert client.get(f"/api/assets/{grandchild}").status_code == 404
    assert client.get(f"/api/assets/{root}").status_code == 200
    history = client.get(f"/api/assets/{root}/history").json["data"]
    assert [item["id"] for item in history] == [root]


def test_delete_then_new_generation_is_clean(client, chain):
    root, child, grandchild = chain
    assert client.delete(f"/api/assets/{grandchild}").status_code == 200
    flipped = client.post("/api/process", json={"asset_id": child, "steps": STEPS}).json["data"]
    assert flipped["parent_id"] == child
    assert client.get(f"/api/assets/{flipped['id']}/history").json["data"][-1]["id"] == flipped["id"]
    assert client.get(f"/api/assets/{child}/history").json["data"][-1]["id"] == child


def test_normal_flip_export_unaffected(client, chain, uploaded):
    exported = client.post("/api/exports", json={"asset_id": uploaded["id"], "options": {"format": "PNG"}})
    assert exported.status_code == 201
    download = client.get(exported.json["data"]["url"])
    assert download.status_code == 200
    result = client.post("/api/process", json={"asset_id": uploaded["id"], "steps": STEPS}).json["data"]
    assert result["steps"] == STEPS
    assert client.get(f"/api/assets/{result['id']}/file").status_code == 200
    assert client.delete(f"/api/assets/{uploaded['id']}").status_code == 409
