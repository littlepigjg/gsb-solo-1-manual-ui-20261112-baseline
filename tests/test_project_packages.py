import io
import json
import zipfile
from pathlib import Path
import pytest
from PIL import Image
from backend.app import create_app


def make_chain(client):
    first = client.post("/api/process", json={
        "asset_id": client.uploaded_id,
        "steps": [{"operation": "color_invert", "params": {}}],
    }).json["data"]
    second = client.post("/api/process", json={
        "asset_id": first["id"],
        "steps": [{"operation": "filter_sepia", "params": {}}],
    }).json["data"]
    return first, second


@pytest.fixture
def studio(tmp_path):
    source = create_app(data_dir=tmp_path / "source", testing=True)
    target = create_app(data_dir=tmp_path / "target", testing=True)
    try:
        yield source, target
    finally:
        source.extensions["studio"]["jobs"].shutdown()
        target.extensions["studio"]["jobs"].shutdown()


@pytest.fixture
def source_client(studio):
    return studio[0].test_client()


@pytest.fixture
def target_client(studio):
    return studio[1].test_client()


@pytest.fixture
def uploaded_source(source_client, sample_image):
    buffer = io.BytesIO()
    sample_image.save(buffer, "PNG")
    buffer.seek(0)
    response = source_client.post("/api/assets", data={"file": (buffer, "原片.png")})
    assert response.status_code == 201, response.json
    source_client.uploaded_id = response.json["data"]["id"]
    return response.json["data"]


def export_project(client, asset_ids):
    response = client.post("/api/project-packages/export", json={"asset_ids": asset_ids})
    assert response.status_code == 201, response.json
    record = response.json["data"]
    payload = client.get(record["url"]).data
    return record, payload


def import_project(client, payload, name="迁移项目包.zip"):
    return client.post(
        "/api/project-packages/import",
        data={"file": (io.BytesIO(payload), name)},
    )


def rebuild_zip(payload, mutate):
    with zipfile.ZipFile(io.BytesIO(payload)) as source:
        entries = {name: source.read(name) for name in source.namelist()}
    mutate(entries)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for name, data in entries.items():
            if data is not None:
                target.writestr(name, data)
    return buffer.getvalue()


def test_export_contains_selected_asset_and_all_upstream_history(source_client, uploaded_source):
    first, second = make_chain(source_client)
    record, payload = export_project(source_client, [second["id"]])
    assert record["kind"] == "project"
    assert record["included_count"] == 3
    assert record["selection_count"] == 1

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.testzip() is None
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["package_format"] == "guangyu.image-studio.project"
        assert manifest["selection"] == [second["id"]]
        assert manifest["root_ids"] == [uploaded_source["id"]]
        ids = [entry["record"]["id"] for entry in manifest["assets"]]
        assert ids == [uploaded_source["id"], first["id"], second["id"]]
        result_entry = manifest["assets"][-1]
        assert result_entry["record"]["name"] == second["name"]
        assert result_entry["record"]["steps"][0]["operation"] == "filter_sepia"
        assert result_entry["record"]["metadata"]["elapsed_ms"] > 0
        root_entry = manifest["assets"][0]
        assert root_entry["record"]["parent_id"] is None
        assert root_entry["record"]["root_id"] == uploaded_source["id"]
        assert archive.read(result_entry["file"]) == source_client.get(second["url"]).data


def test_import_restores_originals_names_processing_info_and_can_continue(
    source_client, target_client, uploaded_source
):
    first, second = make_chain(source_client)
    _, payload = export_project(source_client, [second["id"]])

    response = import_project(target_client, payload)
    assert response.status_code == 201, response.json
    result = response.json["data"]
    assert result["imported"] == 3
    assert result["id_conflicts"] == 0
    assert result["primary_id"] == result["selection_ids"][0]

    imported = {item["id"]: item for item in result["records"]}
    original_root = imported[result["root_ids"][0]]
    original_result = imported[result["primary_id"]]
    assert original_root["name"] == "原片.png"
    assert original_result["name"] == second["name"]
    assert original_result["steps"][0]["operation"] == "filter_sepia"
    assert original_result["parent_id"] in imported
    assert original_result["root_id"] == original_root["id"]
    assert target_client.get(original_root["url"]).data == source_client.get(uploaded_source["url"]).data
    assert target_client.get(original_result["thumbnail_url"]).status_code == 200

    history = target_client.get(f"/api/assets/{original_result['id']}/history").json["data"]
    assert [item["name"] for item in history] == [
        uploaded_source["name"], first["name"], second["name"]
    ]
    assert history[0]["created_at"] == uploaded_source["created_at"]
    assert history[2]["created_at"] == second["created_at"]
    assert history[0]["metadata"]["project_import"]["source_asset_id"] == uploaded_source["id"]

    continued = target_client.post("/api/process", json={
        "asset_id": original_result["id"],
        "steps": [{"operation": "color_grayscale", "params": {}}],
    })
    assert continued.status_code == 201, continued.json
    new_asset = continued.json["data"]
    assert new_asset["parent_id"] == original_result["id"]
    assert new_asset["root_id"] == original_root["id"]
    diagnostics = target_client.get("/api/diagnostics").json["data"]
    assert diagnostics["healthy"] is True


def test_repeated_import_never_overwrites_even_with_same_ids_or_names(
    source_client, target_client, uploaded_source
):
    _, payload = export_project(source_client, [uploaded_source["id"]])
    first = import_project(target_client, payload)
    assert first.status_code == 201, first.json
    first_id = first.json["data"]["asset_ids"][0]
    first_bytes = target_client.get(f"/api/assets/{first_id}/file").data

    second = import_project(target_client, payload)
    assert second.status_code == 201, second.json
    second_data = second.json["data"]
    assert second_data["imported"] == 1
    assert second_data["id_conflicts"] == 1
    assert second_data["name_conflicts"] == 1
    second_id = second_data["asset_ids"][0]
    assert second_id != first_id

    assets = target_client.get("/api/assets?limit=100").json["data"]["items"]
    names = [item["name"] for item in assets]
    assert names.count("原片.png") == 2
    assert len({item["id"] for item in assets}) == 2
    assert target_client.get(f"/api/assets/{first_id}/file").data == first_bytes
    assert target_client.get(f"/api/assets/{second_id}/file").data == first_bytes


def test_corrupt_image_is_rejected_without_partial_import(
    source_client, target_client, uploaded_source
):
    _, payload = export_project(source_client, [uploaded_source["id"]])

    def mutate(entries):
        image_name = next(name for name in entries if name.endswith(".png"))
        entries[image_name] = b"broken image bytes"

    damaged = rebuild_zip(payload, mutate)
    before = target_client.get("/api/assets").json["data"]["total"]
    response = import_project(target_client, damaged)
    assert response.status_code == 400
    assert response.json["ok"] is False
    assert response.json["error"]["code"] == "invalid_project_package"
    assert "已整包拒绝" in response.json["error"]["message"]
    assert target_client.get("/api/assets").json["data"]["total"] == before
    assert not list(target_client.application.extensions["studio"]["settings"].media.glob("*.png"))
    assert not list(Path(target_client.application.extensions["studio"]["settings"].root).glob("project-import-*"))


def test_id_collision_remaps_history_without_touching_existing_asset(
    source_client, target_client, uploaded_source
):
    _, payload = export_project(source_client, [uploaded_source["id"]])
    first = import_project(target_client, payload)
    assert first.status_code == 201, first.json
    original_id = first.json["data"]["asset_ids"][0]
    original_bytes = target_client.get(f"/api/assets/{original_id}/file").data

    # Simulate a target library that already owns the same identifier with a
    # different asset by replacing the imported record and media file in place.
    buffer = io.BytesIO()
    Image.new("RGBA", (12, 12), "purple").save(buffer, "PNG")
    buffer.seek(0)
    collision = target_client.post("/api/assets", data={"file": (buffer, "占用同标识.png")}).json["data"]
    target_media = target_client.application.extensions["studio"]["settings"].media
    target_store = target_client.application.extensions["studio"]["store"]
    replacement = target_store.get("assets", collision["id"])
    target_store.delete("assets", collision["id"])
    target_store.delete("assets", original_id)
    (target_media / (collision["id"] + ".png")).replace(target_media / (original_id + ".png"))
    (target_media / (collision["id"] + ".thumb.png")).replace(target_media / (original_id + ".thumb.png"))
    replacement["id"] = original_id
    replacement["root_id"] = original_id
    target_store.insert("assets", replacement)

    response = import_project(target_client, payload)
    assert response.status_code == 201, response.json
    imported_id = response.json["data"]["asset_ids"][0]
    assert imported_id != original_id
    assert target_client.get(f"/api/assets/{original_id}").json["data"]["name"] == "占用同标识.png"
    assert target_client.get(f"/api/assets/{original_id}/file").data != original_bytes
    imported = target_client.get(f"/api/assets/{imported_id}").json["data"]
    assert imported["name"] == uploaded_source["name"]
    assert target_client.get(f"/api/assets/{imported_id}/file").data == original_bytes


def test_missing_image_is_rejected(source_client, target_client, uploaded_source):
    _, payload = export_project(source_client, [uploaded_source["id"]])

    def mutate(entries):
        image_name = next(name for name in entries if name.endswith(".png"))
        del entries[image_name]

    response = import_project(target_client, rebuild_zip(payload, mutate))
    assert response.status_code == 400
    assert "缺少素材图片" in response.json["error"]["message"]
    assert target_client.get("/api/assets").json["data"]["total"] == 0


def test_corrupt_manifest_is_rejected(source_client, target_client, uploaded_source):
    _, payload = export_project(source_client, [uploaded_source["id"]])

    def mutate(entries):
        entries["manifest.json"] = b"{not valid json"

    response = import_project(target_client, rebuild_zip(payload, mutate))
    assert response.status_code == 400
    assert "项目清单" in response.json["error"]["message"]
    assert target_client.get("/api/assets").json["data"]["total"] == 0


def test_truncated_history_is_rejected(source_client, target_client, uploaded_source):
    _, payload = export_project(source_client, [uploaded_source["id"]])
    first, _ = make_chain(source_client)
    _, full_payload = export_project(source_client, [first["id"]])

    def mutate(entries):
        manifest = json.loads(entries["manifest.json"])
        root_id = manifest["root_ids"][0]
        manifest["assets"] = [
            entry for entry in manifest["assets"] if entry["record"]["id"] != root_id
        ]
        manifest["asset_count"] = len(manifest["assets"])
        entries["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        del entries[f"assets/{root_id}.png"]

    response = import_project(target_client, rebuild_zip(full_payload, mutate))
    assert response.status_code == 400
    assert "上游版本" in response.json["error"]["message"]
    assert target_client.get("/api/assets").json["data"]["total"] == 0


def test_duplicate_asset_id_is_rejected(source_client, target_client, uploaded_source):
    _, payload = export_project(source_client, [uploaded_source["id"]])

    def mutate(entries):
        manifest = json.loads(entries["manifest.json"])
        manifest["assets"].append(dict(manifest["assets"][0]))
        manifest["asset_count"] = len(manifest["assets"])
        entries["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")

    response = import_project(target_client, rebuild_zip(payload, mutate))
    assert response.status_code == 400
    assert "重复的素材标识" in response.json["error"]["message"]


def test_unsafe_zip_path_is_rejected(source_client, target_client, uploaded_source):
    _, payload = export_project(source_client, [uploaded_source["id"]])

    def mutate(entries):
        entries["../outside.png"] = b"x"

    response = import_project(target_client, rebuild_zip(payload, mutate))
    assert response.status_code == 400
    assert "不安全的路径" in response.json["error"]["message"]


def test_non_zip_or_missing_file_is_rejected(target_client):
    response = target_client.post(
        "/api/project-packages/import",
        data={"file": (io.BytesIO(b"plain text"), "bad.zip")},
    )
    assert response.status_code == 400
    assert response.json["error"]["code"] == "invalid_project_package"

    missing = target_client.post("/api/project-packages/import", data={})
    assert missing.status_code == 400


def test_export_requires_existing_selection(source_client):
    response = source_client.post("/api/project-packages/export", json={"asset_ids": ["0" * 32]})
    assert response.status_code in (400, 404, 410)
    assert response.json["ok"] is False


def test_tampered_media_does_not_stop_later_valid_import(source_client, target_client, uploaded_source):
    record, payload = export_project(source_client, [uploaded_source["id"]])
    media_dir = source_client.application.extensions["studio"]["settings"].media
    media_file = media_dir / (uploaded_source["id"] + ".png")
    original = media_file.read_bytes()
    media_file.write_bytes(b"damaged")
    failed = source_client.post(
        "/api/project-packages/export",
        json={"asset_ids": [uploaded_source["id"]]},
    )
    assert failed.status_code in (400, 410)
    media_file.write_bytes(original)
    response = import_project(target_client, payload)
    assert response.status_code == 201
    assert response.json["data"]["imported"] == 1


def test_export_preserves_tags_favorite_and_original_bytes(source_client, uploaded_source):
    updated = source_client.patch(
        f"/api/assets/{uploaded_source['id']}",
        json={"name": "重命名的原片.png", "favorite": True, "tags": ["迁移", "保留"]},
    ).json["data"]
    _, payload = export_project(source_client, [updated["id"]])
    response = import_project(source_client, payload)
    assert response.status_code == 201, response.json
    imported = response.json["data"]["records"][0]
    assert imported["name"] == "重命名的原片.png"
    assert imported["favorite"] is True
    assert imported["tags"] == ["迁移", "保留"]
    assert imported["sha256"] == updated["sha256"]
    assert imported["bytes"] == updated["bytes"]
