"""Animated GIF sequencing, palette, timing and edge-case handling."""
import io

import numpy as np
import pytest
from PIL import Image


def _upload(client, name, size, color=(120, 60, 200), alpha=None):
    image = Image.new("RGBA", size, color + (255,))
    if alpha is not None:
        array = np.array(image)
        array[..., 3] = alpha
        image = Image.fromarray(array)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    response = client.post("/api/assets", data={"file": (buffer, name)})
    assert response.status_code == 201, response.json
    return response.json["data"]


def _base_payload(assets, durations=None, **overrides):
    durations = durations or [300] * len(assets)
    payload = {
        "frames": [
            {"asset_id": asset["id"], "duration_ms": duration}
            for asset, duration in zip(assets, durations)
        ],
        "canvas_mode": "max",
        "fit": "contain",
        "background": "#ffffff",
        "loop": True,
        "alpha_mode": "keep",
        "colors": 256,
        "dither": "floyd",
    }
    payload.update(overrides)
    return payload


def _download_gif(client, record):
    response = client.get(record["url"])
    assert response.status_code == 200
    assert response.mimetype == "image/gif"
    assert "attachment" in response.headers["Content-Disposition"]
    return Image.open(io.BytesIO(response.data))


def test_animation_order_duration_and_loop(client):
    first = _upload(client, "第一帧.png", (64, 48), (220, 40, 40))
    second = _upload(client, "第二帧.png", (64, 48), (40, 200, 90))
    third = _upload(client, "第三帧.png", (64, 48), (40, 80, 220))
    payload = _base_payload([first, second, third], [700, 133, 250])
    response = client.post("/api/animations", json=payload)
    assert response.status_code == 201, response.json
    record = response.json["data"]
    assert record["kind"] == "animation"
    assert record["asset_ids"] == [first["id"], second["id"], third["id"]]
    meta = record["animation"]
    assert meta["frame_count"] == 3
    assert meta["durations_ms"] == [700, 130, 250]
    assert meta["total_duration_ms"] == 1080
    with _download_gif(client, record) as image:
        assert image.format == "GIF"
        assert image.n_frames == 3
        assert image.info["loop"] == 0
        durations = []
        for index in range(image.n_frames):
            image.seek(index)
            durations.append(image.info["duration"])
        assert durations == [700, 130, 250]


def test_preview_is_smaller_ephemeral_and_carries_headers(client):
    assets = [_upload(client, f"p{i}.png", (300, 260), (i * 30, 80, 160)) for i in range(1, 3)]
    before = len(client.get("/api/exports").json["data"]) if False else None
    response = client.post("/api/animations/preview", json=_base_payload(assets))
    assert response.status_code == 200
    assert response.mimetype == "image/gif"
    assert response.headers["X-Frame-Count"] == "2"
    assert response.headers["X-Total-Duration-Ms"] == "600"
    width, height = response.headers["X-Canvas-Size"].split("x")
    assert int(width) <= 480 and int(height) <= 480
    with Image.open(io.BytesIO(response.data)) as image:
        assert image.n_frames == 2
    exports = client.get("/api/stats").json["data"]["exports"]
    assert exports == 0


def test_mismatched_sizes_share_canvas_with_explained_warning(client):
    large = _upload(client, "大.png", (120, 80), (120, 60, 200))
    small = _upload(client, "小.png", (40, 60), (10, 200, 90))
    payload = _base_payload([large, small], [200, 200], fit="contain", background="#102030")
    record = client.post("/api/animations", json=payload).json["data"]
    assert (record["animation"]["width"], record["animation"]["height"]) == (120, 80)
    warning = " ".join(record["animation"]["warnings"])
    assert "尺寸不一致" in warning and "120×80" in warning and "40×60" in warning
    with _download_gif(client, record) as image:
        assert image.size == (120, 80)
        # Transparent padding around the centered smaller frame is fully transparent.
        image.seek(1)
        assert image.getpixel((2, 2))[3] == 0
        assert image.getpixel((60, 40))[3] == 255

    # With transparency flattened, the padded corner is the background color instead.
    flat = client.post("/api/animations", json={**payload, "alpha_mode": "flatten", "dither": "none"}).json["data"]
    with _download_gif(client, flat) as image:
        image.seek(1)
        frame = image.convert("RGBA")
        assert frame.getpixel((2, 2))[:3] == (16, 32, 48)
        assert frame.getpixel((60, 40))[:3] == (10, 200, 90)


def test_duplicate_frames_are_kept_and_explained(client):
    one = _upload(client, "a.png", (48, 48), (90, 90, 90))
    two = _upload(client, "b.png", (48, 48), (200, 10, 10))
    payload = _base_payload([one, one, two], [100, 200, 300])
    record = client.post("/api/animations", json=payload).json["data"]
    assert record["animation"]["frame_count"] == 3
    assert record["animation"]["durations_ms"] == [100, 200, 300]
    assert any("重复帧" in message and "第 1 帧" in message for message in record["animation"]["warnings"])


def test_transparency_is_retained_and_flatten_mode_removes_it(client):
    gradient = np.linspace(0, 255, 64, dtype=np.uint8)[None, :].repeat(48, 0)
    transparent = _upload(client, "透明.png", (64, 48), (30, 120, 200), alpha=gradient)
    opaque = _upload(client, "不透明.png", (64, 48), (200, 200, 60))
    kept = client.post("/api/animations", json=_base_payload(
        [transparent, opaque], alpha_mode="keep", background="#ff00ff")).json["data"]
    assert kept["animation"]["transparency"] is True
    with _download_gif(client, kept) as image:
        image.seek(0)
        assert "transparency" in image.info
        assert image.disposal_method == 2
    flat = client.post("/api/animations", json=_base_payload(
        [transparent, opaque], alpha_mode="flatten", background="#ff00ff", dither="none")).json["data"]
    assert flat["animation"]["transparency"] is False
    with _download_gif(client, flat) as image:
        image.seek(0)
        assert "transparency" not in image.info
        palette = list(image.getpalette())
        index = image.getpixel((0, 0))
        actual = tuple(palette[index * 3:index * 3 + 3])
        # Fully transparent source pixels are composited onto the chosen background color.
        assert all(abs(a - b) <= 6 for a, b in zip(actual, (255, 0, 255)))


def test_shared_palette_keeps_subtle_color_differences(client):
    shades = []
    for step in range(4):
        value = 60 + step * 12
        shades.append(_upload(client, f"微差{step}.png", (40, 40), (value, value, value)))
    record = client.post("/api/animations", json=_base_payload(
        shades, [120] * 4, colors=256, dither="none")).json["data"]
    with _download_gif(client, record) as image:
        palette = image.getpalette()[:256 * 3]
        grays = sorted({
            palette[index]
            for index in range(0, len(palette), 3)
            if palette[index] == palette[index + 1] == palette[index + 2]
        })
        # The four subtly different grays must survive quantization as distinct entries.
        assert {60, 72, 84, 96}.issubset(set(grays))


def test_non_looping_gif_has_no_loop_extension(client):
    assets = [_upload(client, f"n{i}.png", (32, 32), (i * 40, 30, 90)) for i in range(1, 3)]
    record = client.post("/api/animations", json=_base_payload(assets, [200, 200], loop=False)).json["data"]
    assert record["animation"]["loop"] is False
    with _download_gif(client, record) as image:
        assert "loop" not in image.info


def test_custom_canvas_and_cover_fit(client):
    assets = [_upload(client, f"c{i}.png", (50, 100), (i * 60, 70, 120)) for i in range(1, 3)]
    payload = _base_payload(assets, [200, 200], canvas_mode="custom", width=200, height=150, fit="cover")
    record = client.post("/api/animations", json=payload).json["data"]
    assert (record["animation"]["width"], record["animation"]["height"]) == (200, 150)
    with _download_gif(client, record) as image:
        assert image.size == (200, 150)


@pytest.mark.parametrize("payload, expected", [
    ({"frames": []}, "帧序列数量"),
    ({"frames": [{"duration_ms": 200}, {"duration_ms": 200}]}, "素材格式"),
    ({}, "必须是数组"),
])
def test_invalid_animation_payloads(client, payload, expected):
    response = client.post("/api/animations", json=payload)
    assert response.status_code == 400
    assert expected in response.json["error"]["message"]


def test_duration_bounds_and_unknown_fields_rejected(client, uploaded):
    other = _upload(client, "other.png", (32, 32))
    too_short = client.post("/api/animations", json=_base_payload(
        [uploaded, other], [10, 200]))
    assert too_short.status_code == 400
    assert "停留时间" in too_short.json["error"]["message"]
    unknown = client.post("/api/animations", json={**_base_payload([uploaded, other]), "surprise": 1})
    assert unknown.status_code == 400
    assert "未知字段" in unknown.json["error"]["message"]
    missing_asset = client.post("/api/animations", json={
        "frames": [
            {"asset_id": uploaded["id"], "duration_ms": 200},
            {"asset_id": "0" * 32, "duration_ms": 200},
        ]
    })
    assert missing_asset.status_code == 404


def test_color_count_bounds(client, uploaded):
    other = _upload(client, "o.png", (24, 24))
    response = client.post("/api/animations", json=_base_payload([uploaded, other], colors=1))
    assert response.status_code == 400
    assert "调色板颜色数" in response.json["error"]["message"]
