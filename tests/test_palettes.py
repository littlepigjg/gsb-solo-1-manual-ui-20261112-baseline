import io
import json
import numpy as np
import pytest
from PIL import Image
from backend.app import create_app


GPL_SAMPLE = (
    "GIMP Palette\n"
    "Name: 测试调色板\n"
    "Columns: 2\n"
    "# 这是注释\n"
    "255   0   0\t纯红\n"
    "0 255 0 绿色\n"
    "0\t0\t255\n"
    "128 128 128\n"
)


def import_gpl(client, content=GPL_SAMPLE, filename="sample.gpl"):
    payload = content.encode("utf-8") if isinstance(content, str) else content
    return client.post("/api/palettes/import", data={"file": (io.BytesIO(payload), filename)})


def palette_count(client):
    return len(client.get("/api/palettes").json["data"])


@pytest.fixture
def palette(client):
    response = client.post("/api/palettes", json={
        "name": "基础三色",
        "colors": [
            {"hex": "#FF0000", "name": "红"},
            {"hex": "#00ff00", "name": "绿"},
            {"hex": "#0000ff", "name": "蓝"},
        ],
    })
    assert response.status_code == 201, response.json
    return response.json["data"]


def test_import_gpl_success(client):
    response = import_gpl(client)
    assert response.status_code == 201, response.json
    record = response.json["data"]
    assert record["name"] == "测试调色板"
    assert record["source"] == "gpl"
    assert record["columns"] == 2
    assert [color["hex"] for color in record["colors"]] == ["#ff0000", "#00ff00", "#0000ff", "#808080"]
    assert record["colors"][0]["name"] == "纯红"
    assert record["colors"][1]["name"] == "绿色"
    assert record["colors"][3]["name"] == ""
    assert palette_count(client) == 1


@pytest.mark.parametrize("content,fragment", [
    ("", "为空"),
    ("Not A Palette\n255 0 0\n", "文件头"),
    ("GIMP Palette\n255 0\n", "第 2 行"),
    ("GIMP Palette\n255 x 0\n", "不是整数"),
    ("GIMP Palette\n300 0 0\n", "超出 0–255 范围"),
    ("GIMP Palette\n-1 0 0\n", "超出 0–255 范围"),
    ("GIMP Palette\n25.5 0 0\n", "不是整数"),
    ("GIMP Palette\nColumns: abc\n255 0 0\n", "Columns"),
    ("GIMP Palette\nColumns: 0\n255 0 0\n", "Columns"),
    ("GIMP Palette\nName: 只有名字\n# 没有颜色\n", "不包含任何颜色"),
    ("GIMP Palette\n未知内容行\n", "第 2 行"),
])
def test_import_gpl_corruption_rejected(client, content, fragment):
    response = import_gpl(client, content)
    assert response.status_code == 400
    assert fragment in response.json["error"]["message"]
    assert palette_count(client) == 0


def test_import_gpl_invalid_encoding_rejected(client):
    response = import_gpl(client, b"GIMP Palette\n\xff\xfe\x00bad\n")
    assert response.status_code == 400
    assert palette_count(client) == 0


def test_import_gpl_too_many_colors(client):
    lines = ["GIMP Palette"] + [f"{i % 256} 0 0" for i in range(257)]
    response = import_gpl(client, "\n".join(lines))
    assert response.status_code == 400
    assert "上限" in response.json["error"]["message"]
    assert palette_count(client) == 0


def test_import_gpl_without_name_uses_filename(client):
    response = import_gpl(client, "GIMP Palette\n1 2 3\n", "秋日印象.gpl")
    assert response.status_code == 201
    assert response.json["data"]["name"] == "秋日印象"
    assert response.json["data"]["colors"][0]["hex"] == "#010203"


def test_create_palette_validation(client):
    assert client.post("/api/palettes", json={"name": "x", "colors": [], "extra": 1}).status_code == 400
    assert client.post("/api/palettes", json={"name": "x", "colors": []}).status_code == 400
    assert client.post("/api/palettes", json={"name": "x", "colors": [{"hex": "red"}]}).status_code == 400
    assert client.post("/api/palettes", json={"name": "x", "colors": [{"hex": "#010203", "name": "a\nb"}]}).status_code == 400
    assert client.post("/api/palettes", json={"name": "换\n行", "colors": [{"hex": "#010203"}]}).status_code == 400
    assert palette_count(client) == 0


def test_create_normalizes_hex(client, palette):
    assert palette["colors"][0]["hex"] == "#ff0000"
    assert palette["colors"][0]["name"] == "红"


def test_update_reorder_and_rename(client, palette):
    colors = list(reversed(palette["colors"]))
    response = client.patch(f"/api/palettes/{palette['id']}", json={"name": "颠倒三色", "colors": colors})
    assert response.status_code == 200
    assert response.json["data"]["name"] == "颠倒三色"
    assert [color["hex"] for color in response.json["data"]["colors"]] == ["#0000ff", "#00ff00", "#ff0000"]
    fetched = client.get(f"/api/palettes/{palette['id']}").json["data"]
    assert [color["hex"] for color in fetched["colors"]] == ["#0000ff", "#00ff00", "#ff0000"]


def test_update_invalid_color_keeps_palette_unchanged(client, palette):
    broken = list(palette["colors"]) + [{"hex": "#gg0000", "name": "坏颜色"}]
    response = client.patch(f"/api/palettes/{palette['id']}", json={"colors": broken})
    assert response.status_code == 400
    fetched = client.get(f"/api/palettes/{palette['id']}").json["data"]
    assert [color["hex"] for color in fetched["colors"]] == ["#ff0000", "#00ff00", "#0000ff"]


def test_delete_palette(client, palette):
    assert client.delete(f"/api/palettes/{palette['id']}").status_code == 200
    assert client.get(f"/api/palettes/{palette['id']}").status_code == 404
    assert palette_count(client) == 0


def test_extract_deterministic_and_ignores_transparency(client):
    pixels = np.zeros((64, 64, 4), dtype=np.uint8)
    pixels[:32, :32] = [255, 0, 0, 255]
    pixels[:32, 32:] = [0, 255, 0, 255]
    pixels[32:, :32] = [0, 0, 255, 255]
    pixels[32:, 32:] = [10, 20, 30, 0]
    buffer = io.BytesIO()
    Image.fromarray(pixels).save(buffer, "PNG")
    buffer.seek(0)
    asset = client.post("/api/assets", data={"file": (buffer, "四色图.png")}).json["data"]
    first = client.post("/api/palettes/extract", json={"asset_id": asset["id"], "count": 3})
    assert first.status_code == 201, first.json
    second = client.post("/api/palettes/extract", json={"asset_id": asset["id"], "count": 3})
    first_colors = [color["hex"] for color in first.json["data"]["colors"]]
    second_colors = [color["hex"] for color in second.json["data"]["colors"]]
    assert first_colors == second_colors
    assert first_colors == ["#0000ff", "#00ff00", "#ff0000"]
    assert "#0a141e" not in first_colors
    assert first.json["data"]["source"] == "image"
    assert first.json["data"]["source_asset_id"] == asset["id"]


def test_extract_rejects_fully_transparent_image(client):
    image = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    asset = client.post("/api/assets", data={"file": (buffer, "透明.png")}).json["data"]
    response = client.post("/api/palettes/extract", json={"asset_id": asset["id"], "count": 4})
    assert response.status_code == 400
    assert palette_count(client) == 0


def test_apply_maps_colors_and_preserves_alpha_and_original(client, palette):
    pixels = np.array([
        [[250, 10, 10, 255], [10, 250, 10, 120]],
        [[200, 200, 200, 255], [0, 0, 0, 0]],
    ], dtype=np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(pixels).save(buffer, "PNG")
    buffer.seek(0)
    asset = client.post("/api/assets", data={"file": (buffer, "待映射.png")}).json["data"]
    before = client.get(asset["url"]).data
    response = client.post(f"/api/palettes/{palette['id']}/apply", json={"asset_id": asset["id"]})
    assert response.status_code == 201, response.json
    result = response.json["data"]
    assert result["parent_id"] == asset["id"]
    assert result["metadata"]["palette_id"] == palette["id"]
    with Image.open(io.BytesIO(client.get(result["url"]).data)) as mapped:
        mapped = mapped.convert("RGBA")
        assert mapped.getpixel((0, 0)) == (255, 0, 0, 255)
        assert mapped.getpixel((1, 0)) == (0, 255, 0, 120)
        # (200,200,200) 与三色等距，平局时取调色板中靠前的红色
        assert mapped.getpixel((0, 1)) == (255, 0, 0, 255)
        assert mapped.getpixel((1, 1))[3] == 0
    assert client.get(asset["url"]).data == before


def test_apply_tie_breaks_by_palette_order(client):
    response = client.post("/api/palettes", json={
        "name": "并列",
        "colors": [{"hex": "#ff0000", "name": ""}, {"hex": "#00ff00", "name": ""}],
    })
    palette = response.json["data"]
    image = Image.new("RGBA", (1, 1), (128, 128, 0, 255))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    asset = client.post("/api/assets", data={"file": (buffer, "平局.png")}).json["data"]
    result = client.post(f"/api/palettes/{palette['id']}/apply", json={"asset_id": asset["id"]}).json["data"]
    with Image.open(io.BytesIO(client.get(result["url"]).data)) as mapped:
        assert mapped.convert("RGBA").getpixel((0, 0)) == (255, 0, 0, 255)


def test_apply_missing_records(client, palette, uploaded):
    assert client.post(f"/api/palettes/{palette['id']}/apply", json={"asset_id": "0" * 32}).status_code == 404
    assert client.post(f"/api/palettes/{'0' * 32}/apply", json={"asset_id": uploaded["id"]}).status_code == 404


def test_export_formats_and_download(client, palette):
    for format_name, marker in [("gpl", b"GIMP Palette"), ("css", b"--palette-01: #ff0000;"),
                                ("json", b'"#ff0000"'), ("txt", b"#ff0000")]:
        response = client.post(f"/api/palettes/{palette['id']}/export", json={"format": format_name})
        assert response.status_code == 201, response.json
        record = response.json["data"]
        assert record["download_name"].endswith("." + format_name)
        download = client.get(record["url"])
        assert download.status_code == 200
        assert marker in download.data
        assert "attachment" in download.headers["Content-Disposition"]
    assert client.post(f"/api/palettes/{palette['id']}/export", json={"format": "ase"}).status_code == 400


def test_export_gpl_round_trip(client, palette):
    record = client.post(f"/api/palettes/{palette['id']}/export", json={"format": "gpl"}).json["data"]
    payload = client.get(record["url"]).data
    reimported = client.post("/api/palettes/import", data={"file": (io.BytesIO(payload), "roundtrip.gpl")})
    assert reimported.status_code == 201, reimported.json
    assert reimported.json["data"]["name"] == palette["name"]
    assert reimported.json["data"]["colors"] == palette["colors"]


def test_export_json_structure(client, palette):
    record = client.post(f"/api/palettes/{palette['id']}/export", json={"format": "json"}).json["data"]
    payload = json.loads(client.get(record["url"]).data)
    assert payload["name"] == "基础三色"
    assert payload["colors"][0] == {"hex": "#ff0000", "name": "红", "rgb": [255, 0, 0]}


def test_palettes_persist_across_restart(app, palette):
    root = app.extensions["studio"]["settings"].root
    app.extensions["studio"]["jobs"].shutdown()
    second = create_app(root, testing=True)
    try:
        fetched = second.test_client().get(f"/api/palettes/{palette['id']}").json["data"]
        assert fetched["name"] == "基础三色"
        assert len(fetched["colors"]) == 3
    finally:
        second.extensions["studio"]["jobs"].shutdown()


def test_palettes_included_in_backup_and_diagnostics(client, palette):
    backup = json.loads(client.get("/api/backup").data)
    assert palette["id"] in backup["collections"]["palettes"]["records"]
    diagnostics = client.get("/api/diagnostics").json["data"]
    assert diagnostics["collections"]["palettes"]["records"] == 1
    assert diagnostics["healthy"] is True
