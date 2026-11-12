"""Inspector statistics must share one visible-pixel basis and be block-independent."""
import io
import numpy as np
import pytest
from PIL import Image


def upload(client, image):
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    response = client.post("/api/assets", data={"file": (buffer, "image.png")})
    assert response.status_code == 201, response.json
    return response.json["data"]


def solid_rgba(size, color, alpha=255):
    width, height = size
    pixels = np.zeros((height, width, 4), dtype=np.uint8)
    pixels[..., :3] = color
    pixels[..., 3] = alpha
    return Image.fromarray(pixels)


def analyze(client, image):
    asset = upload(client, image)
    return client.get(f"/api/assets/{asset['id']}/analysis").json["data"]


def test_fully_transparent_image_reports_no_statistics(client):
    analysis = analyze(client, solid_rgba((200, 200), (12, 34, 56), alpha=0))

    assert analysis["analyzed_pixels"] == 0
    assert analysis["transparent_percent"] == 100.0
    assert analysis["brightness"] is None
    assert analysis["contrast"] is None
    assert analysis["entropy_bits"] is None
    assert analysis["mean_rgb"] is None
    assert analysis["std_rgb"] is None
    assert analysis["laplacian_variance"] is None
    assert analysis["dark_clipping_percent"] is None
    assert all(value is None for value in analysis["percentiles"].values())
    assert analysis["palette"] == []
    for channel in ("red", "green", "blue", "luminance"):
        assert analysis["histograms"][channel] == [0] * 256
    assert len(analysis["suggestions"]) == 1
    assert analysis["suggestions"][0]["operation"] is None


def test_solid_color_image_collapses_to_one_suggestion(client):
    analysis = analyze(client, solid_rgba((200, 200), (30, 60, 90)))

    assert analysis["analyzed_pixels"] == 200 * 200
    assert analysis["contrast"] == 0.0
    assert analysis["entropy_bits"] == 0.0
    assert analysis["laplacian_variance"] == 0.0
    assert analysis["percentiles"]["50"] == analysis["brightness"]
    assert len(analysis["palette"]) == 1
    assert analysis["palette"][0]["percentage"] == 100.0
    assert analysis["palette"][0]["rgb"] == [30, 60, 90]
    # Only the solid-color hint; brightness/contrast/sharpen rules must not stack.
    assert len(analysis["suggestions"]) == 1
    assert analysis["suggestions"][0]["operation"] == "filter_vintage"
    # All visible pixels share one luma value, so clipping counts are either 0 or 100.
    assert (analysis["dark_clipping_percent"],
            analysis["light_clipping_percent"]) in {(0.0, 0.0), (100.0, 0.0), (0.0, 100.0)}


def test_transparent_pixels_are_excluded_from_every_metric(client):
    width = height = 200
    pixels = np.zeros((height, width, 4), dtype=np.uint8)
    pixels[: height // 2, :, :3] = (10, 20, 30)
    pixels[height // 2:, :, :3] = (200, 210, 220)
    pixels[: height // 2, :, 3] = 255
    pixels[height // 2:, :, 3] = 0
    image = Image.fromarray(pixels)
    analysis = analyze(client, image)

    visible = width * (height // 2)
    assert analysis["analyzed_pixels"] == visible
    assert analysis["transparent_percent"] == 50.0
    assert analysis["mean_rgb"] == [10.0, 20.0, 30.0]
    assert analysis["std_rgb"] == [0.0, 0.0, 0.0]
    # Palette shares are relative to visible pixels only (rounding to 0.1).
    assert abs(sum(swatch["percentage"] for swatch in analysis["palette"]) - 100.0) <= 0.6
    # Histogram mass equals the visible pixel count, not the full thumbnail area.
    assert sum(analysis["histograms"]["red"]) == visible
    assert sum(analysis["histograms"]["luminance"]) == visible
    # No visible pixel is dark/light clipped at the extremes used here.
    assert analysis["dark_clipping_percent"] == 0.0
    assert analysis["light_clipping_percent"] == 0.0


def test_statistics_do_not_depend_on_pixel_partitioning(client):
    width = height = 200
    rng = np.random.default_rng(42)
    base = np.zeros((height, width, 4), dtype=np.uint8)
    base[..., 0] = rng.integers(0, 256, size=(height, width), dtype=np.int64)
    base[..., 1] = rng.integers(0, 256, size=(height, width), dtype=np.int64)
    base[..., 2] = rng.integers(0, 256, size=(height, width), dtype=np.int64)
    base[..., 3] = 255
    base[::2, :, 3] = 0

    # Same visible-pixel multiset, rearranged spatially (with alpha moved in
    # lockstep): distribution statistics must not depend on array split points.
    shuffled = base.reshape(-1, 4).copy()
    rng.shuffle(shuffled)
    shuffled = shuffled.reshape(height, width, 4)

    first = analyze(client, Image.fromarray(base))
    second = analyze(client, Image.fromarray(shuffled))

    assert first["analyzed_pixels"] == second["analyzed_pixels"]
    assert first["histograms"] == second["histograms"]
    assert first["percentiles"] == second["percentiles"]
    assert first["entropy_bits"] == second["entropy_bits"]
    assert first["dark_clipping_percent"] == second["dark_clipping_percent"]
    assert first["light_clipping_percent"] == second["light_clipping_percent"]
    for left, right in zip(first["mean_rgb"], second["mean_rgb"]):
        assert left == pytest.approx(right, abs=1e-10)
    for left, right in zip(first["std_rgb"], second["std_rgb"]):
        assert left == pytest.approx(right, abs=1e-10)
    assert first["brightness"] == pytest.approx(second["brightness"], abs=1e-10)
    assert first["contrast"] == pytest.approx(second["contrast"], abs=1e-10)


def test_percentiles_are_ordered_and_bounded(client, uploaded):
    analysis = client.get(f"/api/assets/{uploaded['id']}/analysis").json["data"]
    values = [analysis["percentiles"][str(level)]
              for level in (1, 5, 25, 50, 75, 95, 99)]
    assert values == sorted(values)
    assert 0 <= values[0] <= values[-1] <= 255
    assert analysis["analyzed_pixels"] > 0
