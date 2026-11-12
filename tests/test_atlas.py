import io
import json
import zipfile
import numpy as np
import pytest
from PIL import Image

from backend.packer import pack


def upload(client, width, height, alpha_box=None, name="sprite.png", color=(200, 60, 60)):
    pixels = np.zeros((height, width, 4), dtype=np.uint8)
    if alpha_box is not None:
        x0, y0, x1, y1 = alpha_box
        pixels[y0:y1, x0:x1, 0] = color[0]
        pixels[y0:y1, x0:x1, 1] = color[1]
        pixels[y0:y1, x0:x1, 2] = color[2]
        pixels[y0:y1, x0:x1, 3] = 255
    buffer = io.BytesIO()
    Image.fromarray(pixels).save(buffer, "PNG")
    buffer.seek(0)
    response = client.post("/api/assets", data={"file": (buffer, name)})
    assert response.status_code == 201, response.json
    return response.json["data"]


def restore(atlas_image, sprite, assets_service):
    frame = sprite["frame"]
    piece = atlas_image.crop((frame["x"], frame["y"], frame["x"] + frame["w"], frame["y"] + frame["h"]))
    if sprite["rotated"]:
        piece = piece.rotate(-90, expand=True)
    restored = Image.new("RGBA", (sprite["source_size"]["w"], sprite["source_size"]["h"]), (0, 0, 0, 0))
    box = sprite["trim_box"]
    restored.alpha_composite(piece, (box["x"], box["y"]))
    original, _ = assets_service.load(sprite["asset_id"])
    return restored, original


class TestPacker:
    def test_packs_without_overlap_and_respects_padding(self):
        placements, unpacked = pack([(str(i), 20, 20) for i in range(9)], 100, 100, padding=2)
        assert unpacked == []
        assert len(placements) == 9
        inflated = [(p.x - 2, p.y - 2, p.x + p.width + 2, p.y + p.height + 2) for p in placements]

        def overlap(a, b):
            return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])

        assert not any(overlap(a, b) for i, a in enumerate(inflated) for b in inflated[i + 1:])
        assert all(p.x >= 2 and p.y >= 2 for p in placements)

    def test_reports_unpacked_instead_of_silently_dropping(self):
        placements, unpacked = pack([("big", 80, 80), ("small", 10, 10)], 40, 40, padding=0)
        assert [p.key for p in placements] == ["small"]
        assert unpacked == ["big"]

    def test_rotation_only_when_allowed_and_helpful(self):
        # 56x18 fits a 40x64 canvas only when rotated to 18x56.
        without, unpacked = pack([("w", 56, 18)], 40, 64, allow_rotation=False)
        assert without == [] and unpacked == ["w"]
        with_rotation, unpacked = pack([("w", 56, 18)], 40, 64, allow_rotation=True)
        assert unpacked == []
        assert with_rotation[0].rotated is True
        assert (with_rotation[0].x, with_rotation[0].y) == (0, 0)

    def test_square_sprite_is_never_marked_rotated(self):
        placements, _ = pack([("s", 20, 20)], 64, 64, allow_rotation=True)
        assert placements[0].rotated is False

    def test_deterministic_regardless_of_input_order(self):
        items = [(str(i), i % 7 + 5, (i * 3) % 9 + 5) for i in range(20)]
        first, _ = pack(items, 200, 200, padding=1, allow_rotation=True)
        second, _ = pack(list(reversed(items)), 200, 200, padding=1, allow_rotation=True)
        signature = lambda seq: [(p.key, p.x, p.y, p.rotated) for p in seq]
        assert signature(first) == signature(second)


class TestAtlasApi:
    def test_preview_trim_and_frame_metadata(self, client):
        asset = upload(client, 40, 30, alpha_box=(5, 5, 35, 25), name="icon.png")
        response = client.post("/api/atlases/preview", json={
            "asset_ids": [asset["id"]], "canvas_width": 128, "canvas_height": 128,
        })
        assert response.status_code == 200, response.json
        plan = response.json["data"]
        assert plan["sprite_count"] == 1
        frame = plan["frames"][0]
        assert frame["trim_box"] == {"x": 5, "y": 5, "w": 30, "h": 20}
        assert frame["source_size"] == {"w": 40, "h": 30}
        assert frame["trimmed"] is True
        assert frame["frame"]["x"] == frame["frame"]["y"] == 2

    def test_trim_disabled_keeps_full_source_size(self, client):
        asset = upload(client, 40, 30, alpha_box=(5, 5, 35, 25))
        response = client.post("/api/atlases/preview", json={
            "asset_ids": [asset["id"]], "canvas_width": 128, "canvas_height": 128,
            "trim_transparent": False,
        })
        frame = response.json["data"]["frames"][0]
        assert frame["trim_box"] == {"x": 0, "y": 0, "w": 40, "h": 30}
        assert frame["trimmed"] is False

    def test_fully_transparent_sprite_is_rejected(self, client):
        asset = upload(client, 12, 12, alpha_box=None, name="blank.png")
        response = client.post("/api/atlases/preview", json={
            "asset_ids": [asset["id"]], "canvas_width": 64, "canvas_height": 64,
        })
        assert response.status_code == 400
        assert response.json["error"]["code"] == "empty_sprite"
        assert response.json["error"]["details"]["empty_assets"] == ["blank.png"]

    def test_fully_transparent_sprite_rejected_even_without_trimming(self, client):
        asset = upload(client, 12, 12, alpha_box=None, name="blank.png")
        response = client.post("/api/atlases/preview", json={
            "asset_ids": [asset["id"]], "canvas_width": 64, "canvas_height": 64,
            "trim_transparent": False,
        })
        assert response.status_code == 400
        assert response.json["error"]["code"] == "empty_sprite"

    def test_overflow_stops_whole_production(self, client):
        big = upload(client, 100, 100, alpha_box=(0, 0, 100, 100), name="big.png")
        small = upload(client, 10, 10, alpha_box=(0, 0, 10, 10), name="small.png")
        response = client.post("/api/atlases/preview", json={
            "asset_ids": [big["id"], small["id"]],
            "canvas_width": 40, "canvas_height": 40,
        })
        assert response.status_code == 400
        error = response.json["error"]
        assert error["code"] == "atlas_overflow"
        assert error["details"]["unpacked_asset_ids"] == [big["id"]]
        assert error["details"]["unpacked_names"] == ["big.png"]

    def test_overflow_creates_no_export_record_or_file(self, client, app):
        big = upload(client, 100, 100, alpha_box=(0, 0, 100, 100))
        before = len(app.extensions["studio"]["store"].all("exports"))
        response = client.post("/api/atlases", json={
            "asset_ids": [big["id"]], "canvas_width": 32, "canvas_height": 32,
        })
        assert response.status_code == 400
        store = app.extensions["studio"]["store"]
        assert len(store.all("exports")) == before
        assert not list(app.extensions["studio"]["settings"].exports.glob("*.zip"))

    def test_duplicate_asset_selection_is_rejected(self, client):
        asset = upload(client, 16, 16, alpha_box=(0, 0, 16, 16))
        response = client.post("/api/atlases/preview", json={
            "asset_ids": [asset["id"], asset["id"]],
            "canvas_width": 64, "canvas_height": 64,
        })
        assert response.status_code == 400
        assert response.json["error"]["code"] == "duplicate_asset"

    @pytest.mark.parametrize("payload", [
        {"asset_ids": [], "canvas_width": 64, "canvas_height": 64},
        {"asset_ids": ["not-an-id"], "canvas_width": 64, "canvas_height": 64},
        {"asset_ids": None, "canvas_width": 64, "canvas_height": 64},
        {"asset_ids": ["x"], "canvas_width": 0, "canvas_height": 64},
        {"asset_ids": ["x"], "canvas_width": 64, "canvas_height": 64, "padding": -1},
        {"asset_ids": ["x"], "canvas_width": 64, "canvas_height": 64, "unknown": 1},
        {"asset_ids": ["x"], "canvas_width": 64, "canvas_height": 64, "trim_transparent": "yes"},
    ])
    def test_invalid_requests_are_rejected(self, client, payload):
        response = client.post("/api/atlases/preview", json=payload)
        assert 400 <= response.status_code < 500
        assert response.json["ok"] is False

    def test_create_zip_and_manifest_round_trip(self, client, app):
        first = upload(client, 40, 30, alpha_box=(5, 5, 35, 25), name="icon.png", color=(200, 60, 60))
        second = upload(client, 20, 20, alpha_box=(0, 0, 20, 20), name="icon.png", color=(40, 120, 200))
        third = upload(client, 56, 18, alpha_box=(0, 0, 56, 18), name="wide.png", color=(20, 160, 120))
        response = client.post("/api/atlases", json={
            "asset_ids": [first["id"], second["id"], third["id"]],
            "canvas_width": 128, "canvas_height": 128,
            "padding": 2, "trim_transparent": True, "allow_rotation": True,
        })
        assert response.status_code == 201, response.json
        data = response.json["data"]
        assert data["manifest"]["sprite_count"] == 3
        download = client.get(data["export"]["url"])
        assert download.status_code == 200
        assert download.mimetype == "application/zip"
        with zipfile.ZipFile(io.BytesIO(download.data)) as archive:
            assert archive.namelist() == ["atlas.png", "atlas-manifest.json"]
            manifest = json.loads(archive.read("atlas-manifest.json"))
            atlas = Image.open(io.BytesIO(archive.read("atlas.png"))).convert("RGBA")
        assert atlas.size == (128, 128)
        # Same-named assets get unique names and an explicit warning.
        warnings = manifest["name_warnings"]
        assert len(warnings) == 1
        unique_names = [sprite["unique_name"] for sprite in manifest["sprites"]]
        assert len(set(unique_names)) == len(unique_names)
        assert all(sprite["asset_id"] for sprite in manifest["sprites"])
        # Every sprite restores pixel-exactly to its original size and orientation.
        assets_service = app.extensions["studio"]["assets"]
        for sprite in manifest["sprites"]:
            restored, original = restore(atlas, sprite, assets_service)
            assert restored.size == original.size
            assert np.array_equal(np.asarray(restored), np.asarray(original))

    def test_manifest_records_rotation_for_reconstruction(self, client, app):
        wide = upload(client, 56, 18, alpha_box=(0, 0, 56, 18), name="wide.png", color=(20, 160, 120))
        response = client.post("/api/atlases", json={
            "asset_ids": [wide["id"]], "canvas_width": 40, "canvas_height": 64,
            "allow_rotation": True, "trim_transparent": True,
        })
        manifest = response.json["data"]["manifest"]
        sprite = manifest["sprites"][0]
        assert sprite["rotated"] is True
        assert sprite["frame"] == {"x": 2, "y": 2, "w": 18, "h": 56}
        download = client.get(response.json["data"]["export"]["url"]).data
        with zipfile.ZipFile(io.BytesIO(download)) as archive:
            atlas = Image.open(io.BytesIO(archive.read("atlas.png"))).convert("RGBA")
        restored, original = restore(atlas, sprite, app.extensions["studio"]["assets"])
        assert np.array_equal(np.asarray(restored), np.asarray(original))

    def test_preview_and_production_share_the_same_layout(self, client):
        assets = [
            upload(client, 30 + i * 4, 24 + i * 3, alpha_box=(2, 2, 28 + i * 4, 20 + i * 3), name=f"s{i}.png")
            for i in range(5)
        ]
        body = {
            "asset_ids": [asset["id"] for asset in assets],
            "canvas_width": 200, "canvas_height": 200,
            "padding": 3, "trim_transparent": True, "allow_rotation": True,
        }
        preview = client.post("/api/atlases/preview", json=body).json["data"]
        produced = client.post("/api/atlases", json=body).json["data"]["manifest"]
        preview_frames = {(f["asset_id"]): f["frame"] for f in preview["frames"]}
        for sprite in produced["sprites"]:
            assert preview_frames[sprite["asset_id"]] == sprite["frame"]
