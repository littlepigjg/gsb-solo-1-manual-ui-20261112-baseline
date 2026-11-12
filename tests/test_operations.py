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


def _fill(registry, context, image, width, height, anchor_x=50, anchor_y=50):
    return registry.apply(image, {"operation": "resize_fill", "params": {
        "width": width, "height": height,
        "anchor_x": anchor_x, "anchor_y": anchor_y,
    }}, context)


def test_resize_fill_never_pads_crop_out_of_bounds(registry, tmp_path):
    context = OperationContext(Settings(root=tmp_path))
    image = Image.new("RGBA", (7, 7), (255, 255, 255, 255))
    for anchor_x, anchor_y in [(0, 0), (0, 100), (100, 0), (100, 100), (50, 50)]:
        output = _fill(registry, context, image, 7, 14, anchor_x, anchor_y)
        pixels = np.asarray(output)
        assert output.size == (7, 14)
        assert (pixels[..., 3] == 255).all()
        assert not (pixels[..., :3] == 0).all(axis=-1).any()


def test_resize_fill_anchor_moves_continuously(registry, tmp_path):
    context = OperationContext(Settings(root=tmp_path))
    image = Image.fromarray(np.random.default_rng(1).integers(0, 256, (53, 37, 4), np.uint8))
    for target_width, target_height in [(3, 7), (31, 5), (100, 3), (3, 100), (1, 1)]:
        scale = max(target_width / image.width, target_height / image.height)
        crop_width = min(image.width, max(1, round(target_width / scale)))
        crop_height = min(image.height, max(1, round(target_height / scale)))
        lefts = [min(max(round((image.width - crop_width) * ax / 100), 0),
                     image.width - crop_width) for ax in range(101)]
        tops = [min(max(round((image.height - crop_height) * ay / 100), 0),
                    image.height - crop_height) for ay in range(101)]
        assert lefts == sorted(lefts) and tops == sorted(tops)
        assert all(step <= 1 for step in np.diff(lefts)) and all(step <= 1 for step in np.diff(tops))
        assert lefts[0] == 0 and lefts[-1] == image.width - crop_width
        assert tops[0] == 0 and tops[-1] == image.height - crop_height
        for anchor_x, anchor_y in [(0, 0), (100, 100), (37, 63)]:
            assert _fill(registry, context, image, target_width, target_height,
                         anchor_x, anchor_y).size == (target_width, target_height)


def test_resize_fill_keeps_square_aspect(registry, tmp_path):
    context = OperationContext(Settings(root=tmp_path))
    image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    for y in range(30, 70):
        for x in range(30, 70):
            image.putpixel((x, y), (255, 255, 255, 255))
    output = _fill(registry, context, image, 50, 100)
    mask = np.asarray(output.getchannel("A")) > 0
    ys, xs = np.where(mask)
    assert abs((xs.max() - xs.min() + 1) - (ys.max() - ys.min() + 1)) <= 1


def test_resize_fill_transparent_rgb_cannot_fringe_edges(registry, tmp_path):
    context = OperationContext(Settings(root=tmp_path))
    image = Image.new("RGBA", (7, 7), (0, 0, 0, 0))
    for y in range(2, 5):
        for x in range(2, 5):
            image.putpixel((x, y), (255, 0, 0, 255))
    image.putpixel((2, 2), (255, 0, 0, 128))
    output = _fill(registry, context, image, 21, 21)
    pixels = np.asarray(output).astype(int)
    semi = pixels[(pixels[..., 3] > 0) & (pixels[..., 3] < 255)]
    assert semi.size > 0
    assert (semi[..., 0] - np.maximum(semi[..., 1], semi[..., 2]) >= 240).all()
    transparent = pixels[pixels[..., 3] == 0]
    assert (transparent[..., :3] == 0).all()
    opaque = pixels[pixels[..., 3] == 255]
    assert (opaque[..., 0] >= 250).all() and (opaque[..., 1] <= 5).all()


def test_resize_fill_same_size_returns_identical_pixels(registry, tmp_path):
    context = OperationContext(Settings(root=tmp_path))
    image = Image.fromarray(np.random.default_rng(2).integers(0, 256, (19, 23, 4), np.uint8))
    for anchor_x, anchor_y in [(50, 50), (0, 100), (100, 0)]:
        output = _fill(registry, context, image, image.width, image.height, anchor_x, anchor_y)
        assert np.array_equal(np.asarray(output), np.asarray(image))


def test_resize_fill_odd_and_extreme_dimensions(registry, tmp_path):
    context = OperationContext(Settings(root=tmp_path))
    image = Image.new("RGBA", (37, 53), (10, 20, 30, 255))
    for width, height in [(1, 1), (1, 500), (500, 1), (3, 3), (99, 101)]:
        assert _fill(registry, context, image, width, height).size == (width, height)


def test_resize_fill_still_enforces_size_limits(registry, sample_image, tmp_path):
    context = OperationContext(Settings(root=tmp_path))
    for width, height in [(8192, 8192), (4001, 4000), (0, 10), (10, 0)]:
        with pytest.raises(StudioError):
            _fill(registry, context, sample_image, width, height)


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
