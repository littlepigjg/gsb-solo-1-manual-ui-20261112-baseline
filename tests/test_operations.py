import numpy as np
import pytest
from PIL import Image
from backend.catalog import OPERATIONS
from backend.errors import StudioError
from backend.registry import Registry, OperationContext
from backend.settings import Settings


@pytest.fixture(scope="module")
def registry():
    return Registry()


@pytest.mark.parametrize("spec", OPERATIONS, ids=[spec["id"] for spec in OPERATIONS])
def test_every_operation_runs(spec, registry, sample_image, tmp_path):
    context = OperationContext(Settings(root=tmp_path))
    output = registry.apply(sample_image, {"operation": spec["id"], "params": {}}, context)
    assert isinstance(output, Image.Image)
    assert output.mode == "RGBA"
    assert output.width > 0 and output.height > 0
    if spec["preserves_dimensions"]:
        assert output.size == sample_image.size
    if spec["category"] in {"color", "enhance", "edge"}:
        assert np.array_equal(np.asarray(output.getchannel("A")), np.asarray(sample_image.getchannel("A")))


def test_resize_exact_dimensions(registry, sample_image, tmp_path):
    output = registry.apply(sample_image, {"operation": "resize_exact", "params": {"width": 111, "height": 73}}, OperationContext(Settings(tmp_path)))
    assert output.size == (111, 73)


def test_rotate_exact_pixels(registry, sample_image, tmp_path):
    output = registry.apply(sample_image, {"operation": "transform_quarter", "params": {"angle": "90"}}, OperationContext(Settings(tmp_path)))
    assert np.array_equal(np.asarray(output), np.rot90(np.asarray(sample_image)))


def test_crop_correct_pixels(registry, sample_image, tmp_path):
    output = registry.apply(sample_image, {"operation": "crop_rect", "params": {"left": 30, "top": 40, "width": 70, "height": 80}}, OperationContext(Settings(tmp_path)))
    assert np.array_equal(np.asarray(output), np.asarray(sample_image)[40:120, 30:100])


def test_invert_is_involution(registry, sample_image, tmp_path):
    context = OperationContext(Settings(tmp_path))
    step = {"operation": "color_invert", "params": {}}
    output = registry.apply(registry.apply(sample_image, step, context), step, context)
    assert np.array_equal(np.asarray(output), np.asarray(sample_image))


def test_gamma_identity(registry, sample_image, tmp_path):
    output = registry.apply(sample_image, {"operation": "color_gamma", "params": {"gamma": 1}}, OperationContext(Settings(tmp_path)))
    assert np.max(np.abs(np.asarray(output).astype(float) - np.asarray(sample_image))) <= 1


def test_color_key_transparency(registry, tmp_path):
    image = Image.new("RGBA", (20, 20), (0, 255, 0, 255))
    image.putpixel((10, 10), (255, 0, 0, 255))
    output = registry.apply(image, {"operation": "format_color_key", "params": {}}, OperationContext(Settings(tmp_path)))
    assert output.getpixel((0, 0))[3] == 0
    assert output.getpixel((10, 10))[3] == 255


def test_crop_rejects_out_of_bounds(registry, sample_image, tmp_path):
    with pytest.raises(StudioError):
        registry.apply(sample_image, {"operation": "crop_rect", "params": {"width": 8000}}, OperationContext(Settings(tmp_path)))


@pytest.mark.parametrize("params", [{"factor": True}, {"factor": "1.2"}, {"factor": float("nan")}, {"factor": 5}, {"unexpected": 1}])
def test_strict_parameter_validation(registry, params):
    with pytest.raises(StudioError):
        registry.validate_step({"operation": "color_brightness", "params": params})


def test_bilateral_has_no_band_seams(registry, tmp_path):
    # A smooth vertical gradient must stay smooth after bilateral filtering:
    # the old stripe implementation produced fixed-pitch horizontal seams.
    height, width = 130, 24
    ramp = np.linspace(0, 255, height, dtype=np.float32)[:, None, None]
    rgb = np.repeat(np.repeat(ramp, width, axis=1), 3, axis=2).astype(np.uint8)
    image = Image.fromarray(
        np.dstack([rgb, np.full((height, width), 255, dtype=np.uint8)]), "RGBA"
    )
    output = registry.apply(
        image, {"operation": "enhance_bilateral", "params": {"radius": 2, "sigma": 30.0}},
        OperationContext(Settings(root=tmp_path)),
    )
    channel = np.asarray(output.convert("RGB"), dtype=np.float32)[:, width // 2, 0]
    step = np.abs(np.diff(channel))
    assert step.max() <= step.mean() + 3 * step.std()


def test_bilateral_uses_shared_color_weight(registry, tmp_path):
    # A blue pixel surrounded by strongly colored neighbors must not gain an
    # invented hue from per-channel neighbor mixing.
    image = Image.new("RGBA", (9, 9), (90, 90, 90, 255))
    image.putpixel((4, 4), (10, 10, 200, 255))
    image.putpixel((4, 5), (240, 240, 20, 255))
    image.putpixel((3, 4), (200, 30, 30, 255))
    image.putpixel((5, 4), (30, 200, 60, 255))
    output = registry.apply(
        image, {"operation": "enhance_bilateral", "params": {"radius": 2, "sigma": 60.0}},
        OperationContext(Settings(root=tmp_path)),
    )
    red, green, blue = output.getpixel((4, 4))[:3]
    # The center stays in the blue family instead of drifting toward gray.
    assert blue > red and blue > green


@pytest.mark.parametrize("size", [(1, 40), (40, 1), (1, 1), (3, 2), (2, 3), (65, 5)])
def test_bilateral_handles_narrow_and_single_axis_images(registry, tmp_path, size):
    pixels = np.random.default_rng(7).integers(0, 256, size=(*size, 4)).astype(np.uint8)
    image = Image.fromarray(pixels, "RGBA")
    output = registry.apply(
        image, {"operation": "enhance_bilateral", "params": {"radius": 4, "sigma": 30.0}},
        OperationContext(Settings(root=tmp_path)),
    )
    assert output.size == image.size and output.mode == "RGBA"
    assert np.array_equal(np.asarray(output.getchannel("A")), pixels[..., 3])


def test_bilateral_is_continuous_across_band_boundaries(registry, tmp_path):
    # Rows on either side of an internal processing-band boundary must carry
    # the same smooth statistics as rows well inside a band: the old stripe
    # implementation created discontinuities every 32 rows.
    rng = np.random.default_rng(11)
    height, width = 128, 48
    base = np.linspace(0, 255, height, dtype=np.float32)[:, None, None]
    noise = rng.normal(0, 4, size=(height, width, 1))
    rgb = np.clip(np.repeat(base, width, axis=1) + noise, 0, 255)
    rgb = np.repeat(rgb, 3, axis=2).astype(np.uint8)
    image = Image.fromarray(
        np.dstack([rgb, np.full((height, width), 255, dtype=np.uint8)]), "RGBA"
    )
    output = np.asarray(
        registry.apply(
            image,
            {"operation": "enhance_bilateral", "params": {"radius": 4, "sigma": 25.0}},
            OperationContext(Settings(root=tmp_path)),
        ).convert("RGB"),
        dtype=np.float32,
    )
    step = np.abs(np.diff(output[:, width // 2, 0]))
    interior = np.concatenate([step[8:56], step[72:120]])
    boundary = step[60:68]
    assert boundary.max() <= interior.mean() + 3 * interior.std()


def test_odd_window_validation(registry):
    with pytest.raises(StudioError):
        registry.validate_step({"operation": "enhance_median", "params": {"size": 4}})


def test_all_builtin_examples_validate(registry):
    for spec in OPERATIONS:
        assert registry.validate_step(spec["example"])["operation"] == spec["id"]


def test_deterministic_grain(registry, sample_image, tmp_path):
    context = OperationContext(Settings(tmp_path))
    step = {"operation": "filter_grain", "params": {"seed": 14}}
    assert np.array_equal(np.asarray(registry.apply(sample_image, step, context)), np.asarray(registry.apply(sample_image, step, context)))


def test_memory_dimension_guard(registry, sample_image, tmp_path):
    with pytest.raises(StudioError):
        registry.apply(sample_image, {"operation": "resize_exact", "params": {"width": 8192, "height": 8192}}, OperationContext(Settings(tmp_path)))
