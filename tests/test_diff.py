import io
import json
import zipfile
import numpy as np
import pytest
from PIL import Image


def upload_image(client, image, name="对比素材.png"):
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    response = client.post("/api/assets", data={"file": (buffer, name)})
    assert response.status_code == 201, response.json
    return response.json["data"]


def solid(width, height, color):
    pixels = np.zeros((height, width, 4), dtype=np.uint8)
    pixels[..., :] = color
    return Image.fromarray(pixels)


@pytest.fixture
def pair(client):
    first = solid(64, 48, (40, 90, 160, 255))
    second_array = np.asarray(first).copy()
    second_array[10:14, 10:14] = (200, 60, 40, 255)
    second = Image.fromarray(second_array)
    return upload_image(client, first, "第一版.png"), upload_image(client, second, "第二版.png")


def create_diff(client, first, second, **extra):
    payload = {"first_id": first["id"], "second_id": second["id"], **extra}
    response = client.post("/api/diffs", json=payload)
    assert response.status_code == 201, response.json
    return response.json["data"]


def fetch_image(client, url):
    response = client.get(url)
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    return Image.open(io.BytesIO(response.data)).convert("RGBA")


def test_identical_images_have_no_changes(client, uploaded):
    result = create_diff(client, uploaded, uploaded)
    stats = result["stats"]
    assert stats["identical"] is True
    assert stats["total_changed_pixels"] == 0
    assert stats["changed_pixels"] == 0
    assert stats["only_first_pixels"] == 0
    assert stats["only_second_pixels"] == 0
    assert stats["bbox"] is None
    assert stats["full_resolution"] is True
    assert stats["canvas_size"] == [384, 320]


def test_change_region_and_bbox(client, pair):
    first, second = pair
    result = create_diff(client, first, second)
    stats = result["stats"]
    assert stats["identical"] is False
    assert stats["changed_pixels"] == 16
    assert stats["total_changed_pixels"] == 16
    assert stats["bbox"] == {"x": 10, "y": 10, "width": 4, "height": 4}
    assert stats["max_distance"] > 0
    mask = np.asarray(fetch_image(client, result["urls"]["mask"]))
    assert int((mask[..., 0] == 255).sum()) == stats["changed_pixels"]
    assert int((mask[..., 1] == 255).sum()) == stats["only_first_pixels"]
    assert int((mask[..., 2] == 255).sum()) == stats["only_second_pixels"]


def test_transparent_hidden_colors_do_not_create_fake_diff(client):
    first = solid(32, 32, (255, 0, 0, 0))
    second = solid(32, 32, (0, 0, 255, 0))
    first_asset = upload_image(client, first, "隐藏色甲.png")
    second_asset = upload_image(client, second, "隐藏色乙.png")
    result = create_diff(client, first_asset, second_asset, sensitivity=100)
    stats = result["stats"]
    assert stats["identical"] is True
    assert stats["changed_pixels"] == 0
    assert stats["max_distance"] == 0


def test_alpha_difference_is_detected(client):
    first = solid(32, 32, (255, 0, 0, 0))
    second = solid(32, 32, (255, 0, 0, 255))
    first_asset = upload_image(client, first)
    second_asset = upload_image(client, second)
    result = create_diff(client, first_asset, second_asset)
    stats = result["stats"]
    assert stats["changed_pixels"] == 32 * 32
    assert stats["identical"] is False


def test_coverage_outside_overlap_is_counted(client):
    first = upload_image(client, solid(64, 48, (10, 20, 30, 255)))
    second = upload_image(client, solid(32, 48, (10, 20, 30, 255)))
    result = create_diff(client, first, second, alignment="first")
    stats = result["stats"]
    assert stats["canvas_size"] == [64, 48]
    assert stats["overlap_pixels"] == 32 * 48
    assert stats["only_first_pixels"] == 32 * 48
    assert stats["only_second_pixels"] == 0
    assert stats["changed_pixels"] == 0
    assert stats["total_changed_pixels"] == 32 * 48
    assert stats["coverage_percent"] == 50.0
    assert stats["identical"] is False


def test_offset_alignment_compensates_shift(client):
    base = np.zeros((48, 64, 4), dtype=np.uint8)
    base[..., 3] = 255
    first_array = base.copy()
    first_array[20:28, 20:28] = (220, 40, 40, 255)
    second_array = base.copy()
    second_array[25:33, 30:38] = (220, 40, 40, 255)
    first = upload_image(client, Image.fromarray(first_array))
    second = upload_image(client, Image.fromarray(second_array))
    misaligned = create_diff(client, first, second, alignment="first")
    assert misaligned["stats"]["changed_pixels"] > 0
    aligned = create_diff(
        client, first, second,
        alignment="first", offset_x=-10, offset_y=-5,
    )
    stats = aligned["stats"]
    assert stats["changed_pixels"] == 0
    assert stats["only_first_pixels"] > 0
    assert stats["only_second_pixels"] > 0
    assert stats["canvas_size"] == [74, 53]


def test_sensitivity_controls_threshold(client):
    first_array = np.zeros((16, 16, 4), dtype=np.uint8)
    first_array[..., :] = (100, 100, 100, 255)
    second_array = first_array.copy()
    second_array[4:8, 4:8, 0] = 110
    first = upload_image(client, Image.fromarray(first_array))
    second = upload_image(client, Image.fromarray(second_array))
    strict = create_diff(client, first, second, sensitivity=99)
    assert strict["stats"]["threshold"] == 3
    assert strict["stats"]["changed_pixels"] == 16
    relaxed = create_diff(client, first, second, sensitivity=90)
    assert relaxed["stats"]["threshold"] == 26
    assert relaxed["stats"]["changed_pixels"] == 0
    assert relaxed["stats"]["identical"] is True


def test_stats_and_visualization_describe_same_changes(client, pair):
    first, second = pair
    result = create_diff(client, first, second)
    stats = result["stats"]
    heatmap = np.asarray(fetch_image(client, result["urls"]["heatmap"]))
    overlay = np.asarray(fetch_image(client, result["urls"]["overlay"]))
    assert heatmap.shape[:2] == (48, 64)
    assert overlay.shape[:2] == (48, 64)
    changed_region = heatmap[10:14, 10:14, :3]
    assert (changed_region != np.array([24, 32, 28])).any()
    overlay_region = overlay[10:14, 10:14, :3]
    assert (overlay_region != np.array([40, 90, 160])).any()
    assert stats["changed_pixels"] == 16


def test_diff_download_zip_bundle(client, pair):
    first, second = pair
    result = create_diff(client, first, second)
    response = client.get(result["download_url"])
    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.data))
    assert sorted(archive.namelist()) == [
        "diff_heatmap.png", "diff_mask.png", "diff_overlay.png", "manifest.json",
    ]
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["stats"]["changed_pixels"] == result["stats"]["changed_pixels"]
    assert manifest["first"]["name"] == "第一版.png"
    for name in ("diff_heatmap.png", "diff_overlay.png", "diff_mask.png"):
        with Image.open(io.BytesIO(archive.read(name))) as decoded:
            assert decoded.size == (64, 48)


def test_diff_validation(client, pair):
    first, second = pair
    base = {"first_id": first["id"], "second_id": second["id"]}
    for extra in ({"sensitivity": 0}, {"sensitivity": 101}, {"offset_x": 9000}, {"unknown": 1}):
        response = client.post("/api/diffs", json={**base, **extra})
        assert response.status_code == 400, extra
    response = client.post("/api/diffs", json={"first_id": "bad", "second_id": second["id"]})
    assert response.status_code == 400
    missing = create_diff(client, first, second)
    assert client.get(f"/api/diffs/{missing['id']}/image/unknown").status_code == 404


def test_diff_records_are_pruned(client, app, pair):
    first, second = pair
    from backend.diffing import MAX_RECORDS
    oldest = create_diff(client, first, second)
    for _ in range(MAX_RECORDS):
        create_diff(client, first, second)
    store = app.extensions["studio"]["store"]
    records = [item for item in store.all("exports") if item.get("kind") == "diff"]
    assert len(records) == MAX_RECORDS
    assert client.get(oldest["urls"]["heatmap"]).status_code == 404
    assert client.get(oldest["download_url"]).status_code == 404
