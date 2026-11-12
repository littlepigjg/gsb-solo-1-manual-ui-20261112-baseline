import base64
import io
import re
from PIL import Image

from backend.app import create_app


def _upload(client, size, color, name, alpha=255):
    image = Image.new("RGBA", size, color + (alpha,))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    response = client.post("/api/assets", data={"file": (buffer, name)})
    assert response.status_code == 201, response.json
    return response.json["data"]


def _decode_data_url(data_url):
    match = re.fullmatch(r"data:image/png;base64,(.+)", data_url)
    assert match
    return Image.open(io.BytesIO(base64.b64decode(match.group(1))))


def test_contact_sheet_preview_layout(client, uploaded):
    response = client.post("/api/contact-sheet/preview", json={
        "asset_ids": [uploaded["id"]],
        "columns": 2,
        "cell_width": 200,
        "cell_height": 160,
        "gap": 20,
        "padding": 30,
        "background": "#112233",
    })
    assert response.status_code == 200, response.json
    data = response.json["data"]
    # padding*2 + 2 columns * cell + 1 gap  =>  60 + 400 + 20 = 480
    assert data["width"] == 480
    # padding*2 + 1 row * cell (no inter-row gap)  =>  60 + 160 = 220
    assert data["height"] == 220
    assert data["rows"] == 1
    assert data["columns"] == 2
    assert data["cells"] == 2
    preview = _decode_data_url(data["image"])
    assert preview.format == "PNG"
    assert max(preview.size) <= 1600


def test_contact_sheet_keeps_aspect_ratio_and_blank_tail(client):
    landscape = _upload(client, (400, 200), (200, 40, 40), "横图.png")
    portrait = _upload(client, (100, 300), (40, 60, 200), "竖图.png")
    preview_response = client.post("/api/contact-sheet/preview", json={
        "asset_ids": [landscape["id"], portrait["id"]],
        "columns": 2,
        "cell_width": 200,
        "cell_height": 200,
        "gap": 0,
        "padding": 0,
        "background": "#ffffff",
    })
    assert preview_response.json["data"]["width"] == 400
    assert preview_response.json["data"]["height"] == 200
    # Pixel checks run against the full-resolution download, not the scaled preview.
    record = client.post("/api/contact-sheet", json={
        "asset_ids": [landscape["id"], portrait["id"]],
        "columns": 2,
        "cell_width": 200,
        "cell_height": 200,
        "gap": 0,
        "padding": 0,
        "background": "#ffffff",
    }).json["data"]
    sheet = Image.open(io.BytesIO(client.get(record["url"]).data)).convert("RGBA")
    assert sheet.size == (400, 200)
    # Landscape 400x200 fits cell 200x200 -> 200x100, centered vertically:
    # the cell corners above and below the image stay background white.
    assert sheet.getpixel((5, 5))[:3] == (255, 255, 255)
    assert sheet.getpixel((100, 150))[:3] == (255, 255, 255)
    assert sheet.getpixel((100, 125))[:3] == (200, 40, 40)
    # Portrait 100x300 fits -> ~67x200, centered horizontally in second cell:
    # far-right of the second cell remains blank background (last-row blanks too).
    assert sheet.getpixel((395, 100))[:3] == (255, 255, 255)
    assert sheet.getpixel((300, 100))[:3] == (40, 60, 200)
    # A third column in a 2-item sheet must stay entirely blank.
    extra = client.post("/api/contact-sheet", json={
        "asset_ids": [landscape["id"], portrait["id"]],
        "columns": 3,
        "cell_width": 100,
        "cell_height": 100,
        "gap": 0,
        "padding": 0,
        "background": "#ffffff",
    }).json["data"]
    assert extra["layout"]["cells"] == 3
    wider = Image.open(io.BytesIO(client.get(extra["url"]).data)).convert("RGBA")
    assert wider.getpixel((250, 50))[:3] == (255, 255, 255)


def test_contact_sheet_repeated_ids_keep_order(client, uploaded):
    second = _upload(client, (64, 64), (10, 200, 10), "第二张.png")
    response = client.post("/api/contact-sheet/preview", json={
        "asset_ids": [uploaded["id"], second["id"], uploaded["id"]],
        "columns": 3,
        "cell_width": 120,
        "cell_height": 120,
        "gap": 0,
        "padding": 0,
    })
    assert response.status_code == 200, response.json
    data = response.json["data"]
    assert data["rows"] == 1 and data["columns"] == 3
    preview = _decode_data_url(data["image"])
    # Cells 1 and 3 come from the same source and must be identical.
    first = preview.crop((0, 0, 120, 120))
    third = preview.crop((240, 0, 360, 120))
    assert first.tobytes() == third.tobytes()


def test_transparency_composites_on_background(client):
    image = Image.new("RGBA", (80, 80), (255, 0, 0, 128))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    asset = client.post("/api/assets", data={"file": (buffer, "半透明.png")}).json["data"]
    response = client.post("/api/contact-sheet/preview", json={
        "asset_ids": [asset["id"]],
        "columns": 1,
        "cell_width": 80,
        "cell_height": 80,
        "gap": 0,
        "padding": 0,
        "background": "#000000",
    })
    preview = _decode_data_url(response.json["data"]["image"])
    # 50% red over black -> ~(128, 0, 0)
    pixel = preview.getpixel((40, 40))
    assert abs(pixel[0] - 128) <= 2 and pixel[1] == 0 and pixel[2] == 0


def test_create_returns_downloadable_png_and_records_export(client, uploaded):
    before_assets = client.get("/api/assets").json["data"]["total"]
    response = client.post("/api/contact-sheet", json={
        "asset_ids": [uploaded["id"], uploaded["id"]],
        "columns": 1,
        "cell_width": 120,
        "cell_height": 100,
        "gap": 10,
        "padding": 10,
        "background": "#f7f8f4",
    })
    assert response.status_code == 201, response.json
    record = response.json["data"]
    assert record["kind"] == "contact_sheet"
    assert record["download_name"].endswith(".png")
    download = client.get(record["url"])
    assert download.status_code == 200
    assert "attachment" in download.headers["Content-Disposition"]
    with Image.open(io.BytesIO(download.data)) as sheet:
        assert sheet.format == "PNG"
        assert sheet.size == (record["layout"]["width"], record["layout"]["height"])
    # Original assets, their files and history remain untouched.
    assert client.get("/api/assets").json["data"]["total"] == before_assets
    assert client.get(uploaded["url"]).status_code == 200
    assert len(client.get(f"/api/assets/{uploaded['id']}/history").json["data"]) == 1


def test_preview_is_ephemeral(client, uploaded):
    response = client.post("/api/contact-sheet/preview", json={"asset_ids": [uploaded["id"]]})
    assert response.status_code == 200
    # No export record is created by a preview.
    assert client.get("/api/backup").json["collections"]["exports"]["records"] == {}


def test_missing_asset_is_rejected_without_side_effects(client, uploaded):
    response = client.post("/api/contact-sheet/preview", json={
        "asset_ids": [uploaded["id"], "0" * 32],
    })
    assert response.status_code == 404
    assert response.json["ok"] is False
    assert client.get("/api/backup").json["collections"]["exports"]["records"] == {}


def test_oversized_canvas_rejected_before_render(client, uploaded):
    # 8 rows of 1200 px cells + 7 gaps of 200 + 600 padding => 11600 px tall.
    response = client.post("/api/contact-sheet/preview", json={
        "asset_ids": [uploaded["id"]] * 8,
        "columns": 1,
        "cell_width": 1200,
        "cell_height": 1200,
        "gap": 200,
        "padding": 300,
    })
    assert response.status_code == 400
    assert response.json["ok"] is False
    assert "8192" in response.json["error"]["message"] or "像素" in response.json["error"]["message"]


def test_validation_rejects_bad_payloads(client, uploaded):
    base = {"asset_ids": [uploaded["id"]]}
    bad_payloads = [
        {},
        {"asset_ids": []},
        {"asset_ids": [uploaded["id"]], "columns": 0},
        {"asset_ids": [uploaded["id"]], "columns": 25},
        {"asset_ids": [uploaded["id"]], "cell_width": 10},
        {"asset_ids": [uploaded["id"]], "cell_height": 5000},
        {"asset_ids": [uploaded["id"]], "gap": -1},
        {"asset_ids": [uploaded["id"]], "padding": 9999},
        {"asset_ids": [uploaded["id"]], "background": "white"},
        {"asset_ids": ["not-an-id"]},
        {"asset_ids": [uploaded["id"]], "unknown": 1},
    ]
    for payload in bad_payloads:
        response = client.post("/api/contact-sheet/preview", json=payload)
        assert 400 <= response.status_code < 500, payload
        assert response.json["ok"] is False


def test_missing_file_on_disk_reports_410(client, uploaded, app):
    settings = app.extensions["studio"]["settings"]
    (settings.media / f"{uploaded['id']}.png").unlink()
    response = client.post("/api/contact-sheet/preview", json={"asset_ids": [uploaded["id"]]})
    assert response.status_code == 410
    assert client.get("/api/backup").json["collections"]["exports"]["records"] == {}
