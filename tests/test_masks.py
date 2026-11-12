import io
import time
import numpy as np
import pytest
from PIL import Image
from backend.masks import BUILTINS, MAX_FEATHER, builtin_id
from backend.registry import Registry


@pytest.fixture(scope="module")
def registry():
    return Registry()


def mask_payload(width, height, fill):
    """Build a mask PNG upload body; fill(x, y) returns an (r, g, b, a) tuple."""
    xx, yy = np.meshgrid(np.arange(width), np.arange(height))
    pixels = np.zeros((height, width, 4), dtype=np.uint8)
    pixels[..., 0], pixels[..., 1], pixels[..., 2], pixels[..., 3] = fill(xx, yy)
    buffer = io.BytesIO()
    Image.fromarray(pixels).save(buffer, "PNG")
    buffer.seek(0)
    return buffer


def upload_mask(client, width, height, fill, name="蒙版.png"):
    buffer = mask_payload(width, height, fill)
    response = client.post("/api/masks", data={"file": (buffer, name)})
    assert response.status_code == 201, response.json
    return response.json["data"]


def solid_image(width, height, color):
    buffer = io.BytesIO()
    Image.new("RGBA", (width, height), color).save(buffer, "PNG")
    buffer.seek(0)
    return buffer


def half_mask(x, y):
    """Left half uncovered (black, transparent), right half covered (white, opaque)."""
    covered = x >= 60
    value = np.where(covered, 255, 0)
    return value, value, value, value


def brightness_step(mask_id, **overrides):
    params = {
        "mask_id": mask_id,
        "mode": "luminance",
        "invert": False,
        "feather": 0,
        "strength": 1.0,
        "adjustment": {"operation": "color_brightness", "params": {"factor": 1.5}},
    }
    params.update(overrides)
    return {"operation": "mask_local_adjust", "params": params}


def test_builtin_masks_seeded(client):
    masks = client.get("/api/masks").json["data"]
    builtins = [mask for mask in masks if mask["kind"] == "builtin"]
    assert len(builtins) == len(BUILTINS)
    assert all(mask["adaptive"] for mask in builtins)
    assert {mask["id"] for mask in builtins} == {builtin_id(item["key"]) for item in BUILTINS}
    first = builtins[0]
    assert client.get(first["url"]).mimetype == "image/png"
    assert client.get(first["thumbnail_url"]).status_code == 200
    again = client.get("/api/masks").json["data"]
    assert [mask["id"] for mask in again] == [mask["id"] for mask in masks]


def test_catalog_default_mask_matches_builtin(registry):
    from backend.catalog.mask import OPERATIONS as MASK_OPS
    spec = MASK_OPS[0]
    fields = {field["name"]: field for field in spec["parameters"]}
    assert fields["mask_id"]["default"] == builtin_id("radial")
    assert fields["feather"]["max"] == MAX_FEATHER
    assert set(fields["adjustment"]["operations"]) == {
        operation["id"] for operation in registry.specs.values() if operation["category"] == "color"
    }
    assert registry.validate_step(spec["example"])["operation"] == "mask_local_adjust"


def test_upload_mask_round_trip(client):
    mask = upload_mask(client, 64, 48, lambda x, y: (x % 256, y % 256, 128, 255))
    assert mask["width"] == 64 and mask["height"] == 48
    assert mask["kind"] == "upload" and not mask["adaptive"]
    assert client.get(mask["url"]).mimetype == "image/png"
    assert client.get(mask["thumbnail_url"]).status_code == 200
    listed = client.get("/api/masks").json["data"]
    assert any(item["id"] == mask["id"] for item in listed)
    assert client.delete(f"/api/masks/{mask['id']}").status_code == 200
    assert client.get(f"/api/masks/{mask['id']}").status_code == 404


def test_delete_builtin_mask_rejected(client):
    response = client.delete(f"/api/masks/{builtin_id('radial')}")
    assert response.status_code == 400


def test_invalid_mask_upload_rejected(client):
    response = client.post("/api/masks", data={"file": (io.BytesIO(b"junk"), "mask.png")})
    assert response.status_code == 400
    assert len(client.get("/api/masks").json["data"]) == len(BUILTINS)


def test_local_adjust_only_changes_covered_region(client):
    source = client.post("/api/assets", data={"file": (solid_image(120, 80, (100, 100, 100, 255)), "灰图.png")}).json["data"]
    mask = upload_mask(client, 120, 80, half_mask)
    response = client.post("/api/process", json={"asset_id": source["id"], "steps": [brightness_step(mask["id"])]})
    assert response.status_code == 201, response.json
    result = response.json["data"]
    assert result["parent_id"] == source["id"]
    output = np.asarray(Image.open(io.BytesIO(client.get(result["url"]).data)).convert("RGBA"))
    assert np.array_equal(output[:, :60, :3], np.full((80, 60, 3), 100, dtype=np.uint8))
    assert np.all(output[:, 60:, :3] == 150)


def test_local_adjust_invert_flips_coverage(client):
    source = client.post("/api/assets", data={"file": (solid_image(120, 80, (100, 100, 100, 255)), "灰图.png")}).json["data"]
    mask = upload_mask(client, 120, 80, half_mask)
    step = brightness_step(mask["id"], invert=True)
    result = client.post("/api/process", json={"asset_id": source["id"], "steps": [step]}).json["data"]
    output = np.asarray(Image.open(io.BytesIO(client.get(result["url"]).data)).convert("RGBA"))
    assert np.all(output[:, :60, :3] == 150)
    assert np.all(output[:, 60:, :3] == 100)


def test_local_adjust_strength_scales_effect(client):
    source = client.post("/api/assets", data={"file": (solid_image(120, 80, (100, 100, 100, 255)), "灰图.png")}).json["data"]
    mask = upload_mask(client, 120, 80, half_mask)
    half = client.post("/api/process", json={"asset_id": source["id"], "steps": [brightness_step(mask["id"], strength=0.5)]}).json["data"]
    output = np.asarray(Image.open(io.BytesIO(client.get(half["url"]).data)).convert("RGBA"))
    assert np.all(output[:, 60:, :3] == 125)
    noop = client.post("/api/process", json={"asset_id": source["id"], "steps": [brightness_step(mask["id"], strength=0)]}).json["data"]
    output = np.asarray(Image.open(io.BytesIO(client.get(noop["url"]).data)).convert("RGBA"))
    assert np.all(output[:, :, :3] == 100)


def test_local_adjust_alpha_mode_uses_transparency(client):
    source = client.post("/api/assets", data={"file": (solid_image(120, 80, (100, 100, 100, 255)), "灰图.png")}).json["data"]

    def white_with_alpha_ramp(x, y):
        alpha = np.where(x >= 60, 255, 0)
        return np.full_like(alpha, 255), np.full_like(alpha, 255), np.full_like(alpha, 255), alpha

    mask = upload_mask(client, 120, 80, white_with_alpha_ramp)
    by_alpha = client.post("/api/process", json={"asset_id": source["id"], "steps": [brightness_step(mask["id"], mode="alpha")]}).json["data"]
    output = np.asarray(Image.open(io.BytesIO(client.get(by_alpha["url"]).data)).convert("RGBA"))
    assert np.all(output[:, :60, :3] == 100)
    assert np.all(output[:, 60:, :3] == 150)
    by_luminance = client.post("/api/process", json={"asset_id": source["id"], "steps": [brightness_step(mask["id"], mode="luminance")]}).json["data"]
    output = np.asarray(Image.open(io.BytesIO(client.get(by_luminance["url"]).data)).convert("RGBA"))
    assert np.all(output[:, :, :3] == 150)


def test_local_adjust_feather_softens_transition(client):
    source = client.post("/api/assets", data={"file": (solid_image(120, 80, (100, 100, 100, 255)), "灰图.png")}).json["data"]
    mask = upload_mask(client, 120, 80, half_mask)
    step = brightness_step(mask["id"], feather=8)
    result = client.post("/api/process", json={"asset_id": source["id"], "steps": [step]}).json["data"]
    output = np.asarray(Image.open(io.BytesIO(client.get(result["url"]).data)).convert("RGBA"))
    boundary = output[40, 45:75, 0].astype(int)
    assert boundary.min() >= 100 and boundary.max() <= 150
    assert 100 < boundary[10] < 150 or 100 < boundary[19] < 150
    assert np.all(output[:, :20, :3] == 100)
    assert np.all(output[:, 100:, :3] == 150)


def test_local_adjust_preserves_alpha(client, uploaded):
    mask = upload_mask(client, 384, 320, lambda x, y: (np.full_like(x, 255),) * 4)
    step = brightness_step(mask["id"])
    result = client.post("/api/process", json={"asset_id": uploaded["id"], "steps": [step]}).json["data"]
    output = np.asarray(Image.open(io.BytesIO(client.get(result["url"]).data)).convert("RGBA"))
    original = np.asarray(Image.open(io.BytesIO(client.get(uploaded["url"]).data)).convert("RGBA"))
    assert np.array_equal(output[..., 3], original[..., 3])
    assert output[0, 0, 3] == 0 and output[15, 0, 3] == 120


def test_builtin_mask_fits_any_size(client, uploaded):
    step = brightness_step(builtin_id("radial"), feather=6)
    response = client.post("/api/process", json={"asset_id": uploaded["id"], "steps": [step]})
    assert response.status_code == 201, response.json
    result = response.json["data"]
    assert result["width"] == 384 and result["height"] == 320


def test_mask_size_mismatch_rejected_without_partial_result(client, uploaded):
    mask = upload_mask(client, 100, 100, lambda x, y: (np.full_like(x, 255),) * 4)
    before = client.get("/api/assets").json["data"]["total"]
    response = client.post("/api/process", json={"asset_id": uploaded["id"], "steps": [brightness_step(mask["id"])]})
    assert response.status_code == 400
    assert response.json["error"]["code"] == "mask_size_mismatch"
    assert response.json["error"]["details"]["mask_width"] == 100
    assert client.get("/api/assets").json["data"]["total"] == before


@pytest.mark.parametrize("params", [
    {"mask_id": "not-an-id"},
    {"mask_id": builtin_id("radial"), "mode": "bogus"},
    {"mask_id": builtin_id("radial"), "strength": 2},
    {"mask_id": builtin_id("radial"), "feather": 9999},
    {"mask_id": builtin_id("radial"), "invert": "yes"},
    {"mask_id": builtin_id("radial"), "adjustment": {"operation": "resize_exact", "params": {"width": 10, "height": 10}}},
    {"mask_id": builtin_id("radial"), "adjustment": {"operation": "does_not_exist"}},
    {"mask_id": builtin_id("radial"), "adjustment": {"operation": "color_brightness", "params": {"factor": 99}}},
    {"mask_id": builtin_id("radial"), "adjustment": {"operation": "color_brightness", "params": {"factor": "1.5"}}},
    {"mask_id": "0" * 32},
])
def test_invalid_mask_step_rejected_without_partial_result(client, uploaded, params):
    params = dict(params)
    step = brightness_step(params.pop("mask_id"), **params)
    before = client.get("/api/assets").json["data"]["total"]
    response = client.post("/api/process", json={"asset_id": uploaded["id"], "steps": [step]})
    assert 400 <= response.status_code < 500
    assert response.json["ok"] is False
    assert client.get("/api/assets").json["data"]["total"] == before


def test_preview_with_mask_is_ephemeral(client, uploaded):
    step = brightness_step(builtin_id("circle"))
    response = client.post("/api/preview", json={"asset_id": uploaded["id"], "steps": [step]})
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert client.get("/api/assets").json["data"]["total"] == 1


def test_mask_render_endpoint(client, uploaded):
    response = client.post("/api/masks/render", json={
        "asset_id": uploaded["id"],
        "mask_id": builtin_id("radial"),
        "mode": "luminance",
        "invert": False,
        "feather": 10,
    })
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    with Image.open(io.BytesIO(response.data)) as coverage:
        assert coverage.mode == "L"
        assert coverage.width <= 1200
    assert client.get("/api/assets").json["data"]["total"] == 1
    bad = client.post("/api/masks/render", json={"asset_id": uploaded["id"], "mask_id": "0" * 32})
    assert bad.status_code == 404


def test_mask_render_rejects_size_mismatch(client, uploaded):
    mask = upload_mask(client, 40, 40, lambda x, y: (np.full_like(x, 255),) * 4)
    response = client.post("/api/masks/render", json={"asset_id": uploaded["id"], "mask_id": mask["id"]})
    assert response.status_code == 400
    assert response.json["error"]["code"] == "mask_size_mismatch"


def test_history_records_mask_provenance(client, uploaded):
    mask = upload_mask(client, 384, 320, half_mask, name="右半蒙版.png")
    step = brightness_step(mask["id"], feather=8, strength=0.8)
    result = client.post("/api/process", json={"asset_id": uploaded["id"], "steps": [step]}).json["data"]
    record = client.get(f"/api/assets/{result['id']}").json["data"]
    saved = record["steps"][0]
    assert saved["operation"] == "mask_local_adjust"
    assert saved["params"]["mask_id"] == mask["id"]
    assert saved["params"]["adjustment"]["operation"] == "color_brightness"
    assert saved["params"]["feather"] == 8 and saved["params"]["strength"] == 0.8
    history = client.get(f"/api/assets/{result['id']}/history").json["data"]
    assert [item["id"] for item in history] == [uploaded["id"], result["id"]]


def test_delete_mask_used_by_preset_rejected(client, uploaded):
    mask = upload_mask(client, 384, 320, half_mask)
    preset = client.post("/api/presets", json={
        "name": "局部提亮",
        "description": "",
        "steps": [brightness_step(mask["id"])],
    })
    assert preset.status_code == 201, preset.json
    assert client.delete(f"/api/masks/{mask['id']}").status_code == 409


def test_mask_step_inside_pipeline(client, uploaded):
    steps = [
        {"operation": "color_grayscale", "params": {}},
        brightness_step(builtin_id("vignette"), adjustment={"operation": "color_contrast", "params": {"factor": 0.8}}),
    ]
    response = client.post("/api/process", json={"asset_id": uploaded["id"], "steps": steps})
    assert response.status_code == 201, response.json


def test_batch_job_with_mask_step(client, uploaded):
    response = client.post("/api/jobs", json={
        "asset_ids": [uploaded["id"]],
        "steps": [brightness_step(builtin_id("linear"))],
    })
    assert response.status_code == 202, response.json
    job_id = response.json["data"]["id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json["data"]
        if job["status"] not in {"queued", "running"}:
            break
        time.sleep(0.02)
    assert job["status"] == "completed"
    assert len(job["results"]) == 1
