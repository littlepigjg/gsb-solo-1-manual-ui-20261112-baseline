"""调色文件（.cube LUT）解析、插值、强度混合与 HTTP 行为测试。"""
import io
import numpy as np
import pytest
from PIL import Image

from backend.luts import parse_cube, MAX_3D_SIZE
from backend.errors import StudioError
from backend.registry import Registry, OperationContext
from backend.settings import Settings


@pytest.fixture(scope="module")
def registry():
    return Registry()


def make_3d_cube(size, function, domain_min=(0, 0, 0), domain_max=(1, 1, 1), title=""):
    """按蓝、绿、红（红变化最快）的标准顺序生成 .cube 文本。"""
    lines = []
    if title:
        lines.append(f'TITLE "{title}"')
    lines.append(f"LUT_3D_SIZE {size}")
    if domain_min != (0, 0, 0):
        lines.append("DOMAIN_MIN " + " ".join(str(v) for v in domain_min))
    if domain_max != (1, 1, 1):
        lines.append("DOMAIN_MAX " + " ".join(str(v) for v in domain_max))
    for b in range(size):
        for g in range(size):
            for r in range(size):
                rgb = (r / (size - 1), g / (size - 1), b / (size - 1))
                lines.append(" ".join(str(round(v, 6)) for v in function(rgb)))
    return "\n".join(lines) + "\n"


def make_1d_cube(table, title=""):
    lines = [f'TITLE "{title}"'] if title else []
    lines.append(f"LUT_1D_SIZE {len(table)}")
    lines.extend(" ".join(str(v) for _ in range(3)) if isinstance(v, (int, float))
                 else " ".join(str(v) for v in v) for v in table)
    return "\n".join(lines) + "\n"


def solid_image(rgb, alpha=255, size=(6, 5)):
    image = Image.new("RGBA", size, rgb + (alpha,))
    return image


# ---------- 解析与元数据 ----------

def test_parse_identity_metadata():
    lut = parse_cube(make_3d_cube(3, lambda rgb: rgb, title="中性表"))
    assert lut.kind == "3d"
    assert lut.size == 3
    assert lut.points == 27
    assert lut.title == "中性表"
    assert lut.output_range() == {"red": [0.0, 1.0], "green": [0.0, 1.0], "blue": [0.0, 1.0]}


def test_parse_1d_and_output_range():
    lut = parse_cube(make_1d_cube([(0.1, 0.2, 0.3), (0.8, 0.9, 1.0)]))
    assert lut.kind == "1d"
    assert lut.size == 2
    assert lut.output_range()["red"] == [0.1, 0.8]
    assert lut.output_range()["blue"] == [0.3, 1.0]


def test_comments_blank_lines_and_bom_are_ignored():
    text = "﻿# 顶部注释\n\n   LUT_3D_SIZE 2\r\n# 行内说明\r\n" + \
        "\n".join("0 0 0" if i == 0 else "1 1 1" for i in range(8)) + "\r\n"
    lut = parse_cube(text)
    assert lut.size == 2
    assert lut.points == 8


@pytest.mark.parametrize("name,content", [
    ("空文件", ""),
    ("只有注释", "# nothing here\n"),
    ("缺少尺寸声明", "0 0 0\n1 1 1\n"),
    ("重复尺寸声明", "LUT_3D_SIZE 2\nLUT_3D_SIZE 2\n" + "\n".join("0 0 0" for _ in range(8))),
    ("重复 DOMAIN 声明", "LUT_3D_SIZE 2\nDOMAIN_MIN 0 0 0\nDOMAIN_MIN 0 0 0\n"
     + "\n".join("0 0 0" for _ in range(8))),
    ("数据行数不足", "LUT_3D_SIZE 2\n0 0 0\n1 1 1\n"),
    ("数据行数过多", "LUT_3D_SIZE 2\n" + "\n".join("0 0 0" for _ in range(9))),
    ("数值无法解析", "LUT_3D_SIZE 2\nx 0 0\n" + "\n".join("0 0 0" for _ in range(7))),
    ("数值不是有限数", "LUT_3D_SIZE 2\nnan 0 0\n" + "\n".join("0 0 0" for _ in range(7))),
    ("一维三维同时声明", "LUT_1D_SIZE 2\nLUT_3D_SIZE 2\n" + "\n".join("0 0 0" for _ in range(8))),
    ("尺寸小于 2", "LUT_3D_SIZE 1\n0 0 0\n"),
    ("尺寸超出上限", f"LUT_3D_SIZE {MAX_3D_SIZE + 1}\n"),
    ("定义域反转", "LUT_3D_SIZE 2\nDOMAIN_MIN 0.8 0 0\nDOMAIN_MAX 0.2 1 1\n"
     + "\n".join("0 0 0" for _ in range(8))),
    ("无法识别的行", "LUT_3D_SIZE 2\nGARBAGE 1\n" + "\n".join("0 0 0" for _ in range(8))),
    ("每行数值数量错误", "LUT_3D_SIZE 2\n0 0\n" + "\n".join("0 0 0" for _ in range(7))),
])
def test_invalid_cube_files_are_rejected_with_reason(name, content):
    with pytest.raises(StudioError) as exc:
        parse_cube(content)
    assert exc.value.message  # 每个错误都必须带可读原因


# ---------- 像素正确性：插值、边界、强度与透明度 ----------

def test_identity_lut_keeps_rgb_and_alpha():
    lut = parse_cube(make_3d_cube(4, lambda rgb: rgb))
    image = solid_image((137, 92, 210), alpha=73)
    output = np.asarray(lut.apply(image, 1.0))
    source = np.asarray(image)
    # 恒等 LUT 的 RGB 只允许 8 位定点量化误差，透明通道必须逐像素不变
    assert np.max(np.abs(output[..., :3].astype(int) - source[..., :3].astype(int))) <= 2
    assert np.array_equal(output[..., 3], source[..., 3])


def test_trilinear_interpolation_between_samples():
    # 每个通道输出 = 输入 / 2，晶格只有 0 和 1 两个采样点
    lut = parse_cube(make_3d_cube(2, lambda rgb: tuple(v / 2 for v in rgb)))
    output = lut.apply(solid_image((128, 128, 128)), 1.0)
    # 0.5 落在两个采样点正中间，线性插值结果 0.25 → 64
    assert output.getpixel((0, 0))[:3] == (64, 64, 64)
    quarter = lut.apply(solid_image((64, 64, 64)), 1.0).getpixel((0, 0))[:3]
    # 0.251 → 0.1255 → 32
    assert quarter == (32, 32, 32)


def test_out_of_domain_colors_clamp_to_boundary():
    lut = parse_cube(make_3d_cube(
        2, lambda rgb: tuple(v / 2 for v in rgb),
        domain_min=(0.25, 0.25, 0.25), domain_max=(0.75, 0.75, 0.75),
    ))
    dark = lut.apply(solid_image((0, 0, 0)), 1.0).getpixel((0, 0))[:3]
    bright = lut.apply(solid_image((255, 255, 255)), 1.0).getpixel((0, 0))[:3]
    assert dark == (0, 0, 0)        # 低于定义域 → 钳到首节点
    assert bright == (128, 128, 128)  # 高于定义域 → 钳到末节点（0.5 * 255）


def test_zero_strength_is_exact_original_and_full_strength_applies_lut():
    lut = parse_cube(make_3d_cube(2, lambda rgb: tuple(v / 2 for v in rgb)))
    image = solid_image((100, 150, 200), alpha=40)
    assert np.array_equal(np.asarray(lut.apply(image, 0.0)), np.asarray(image))
    full = np.asarray(lut.apply(image, 1.0))
    blended = np.asarray(lut.apply(image, 0.5))
    expected = (np.asarray(image).astype(np.float32) + full.astype(np.float32)) / 2
    assert np.max(np.abs(blended.astype(np.float32) - np.rint(expected))) <= 1
    assert np.array_equal(blended[..., 3], np.asarray(image)[..., 3])


def test_alpha_channel_is_never_changed():
    rng = np.random.default_rng(7)
    pixels = np.zeros((12, 9, 4), dtype=np.uint8)
    pixels[..., :3] = rng.integers(0, 256, size=(12, 9, 3))
    pixels[..., 3] = np.tile(np.arange(9), (12, 1)) * 28
    image = Image.fromarray(pixels)
    lut = parse_cube(make_3d_cube(3, lambda rgb: ((rgb[0] ** 2, rgb[1] * 0.7, 1 - rgb[2]))))
    for strength in (0.0, 0.25, 0.5, 1.0):
        output = np.asarray(lut.apply(image, strength))
        assert np.array_equal(output[..., 3], pixels[..., 3])


def test_1d_lut_interpolation():
    lut = parse_cube(make_1d_cube([1.0, 0.0]))  # 反相曲线
    output = lut.apply(solid_image((64, 64, 64)), 1.0).getpixel((0, 0))[:3]
    # 0.251 处 np.interp → 0.749 → 191
    assert output == (191, 191, 191)


# ---------- 通过注册表执行 ----------

def test_registry_validate_step_rules(registry):
    good = {"operation": "color_lut", "params": {"lut_id": "a" * 32, "strength": 0.5}}
    assert registry.validate_step(good)["params"]["strength"] == 0.5
    assert registry.validate_step(
        {"operation": "color_lut", "params": {}})["params"]["lut_id"] == ""
    for bad in (
        {"lut_id": "short"},
        {"lut_id": "a" * 32, "strength": 1.5},
        {"lut_id": "a" * 32, "strength": "0.5"},
        {"lut_id": "a" * 32, "unexpected": 1},
    ):
        with pytest.raises(StudioError):
            registry.validate_step({"operation": "color_lut", "params": bad})


def test_operation_without_lut_selection_returns_unchanged_image(registry, sample_image, tmp_path):
    output = registry.apply(
        sample_image, {"operation": "color_lut", "params": {}},
        OperationContext(Settings(tmp_path)),
    )
    assert np.array_equal(np.asarray(output), np.asarray(sample_image))


# ---------- HTTP 行为 ----------

@pytest.fixture
def lut_service(app):
    return app.extensions["studio"]["luts"]


@pytest.fixture
def uploaded_lut(client):
    response = client.post("/api/luts", data={
        "file": (io.BytesIO(make_3d_cube(2, lambda rgb: tuple(v / 2 for v in rgb)).encode()),
                 "摄影师风格.cube")})
    assert response.status_code == 201, response.json
    return response.json["data"]


def test_lut_upload_list_get_and_delete(client, uploaded_lut):
    assert uploaded_lut["name"] == "摄影师风格.cube"
    assert uploaded_lut["kind"] == "3d"
    assert uploaded_lut["size"] == 2
    assert uploaded_lut["points"] == 8
    assert uploaded_lut["domain_min"] == [0.0, 0.0, 0.0]
    assert uploaded_lut["domain_max"] == [1.0, 1.0, 1.0]
    assert len(uploaded_lut["sha256"]) == 64
    assert client.get("/api/luts").json["data"][0]["id"] == uploaded_lut["id"]
    assert client.get(f"/api/luts/{uploaded_lut['id']}").status_code == 200
    assert client.delete(f"/api/luts/{uploaded_lut['id']}").status_code == 200
    assert client.get("/api/luts").json["data"] == []
    assert client.get(f"/api/luts/{uploaded_lut['id']}").status_code == 404


def test_invalid_upload_leaves_no_record_or_file(client, app, lut_service):
    response = client.post("/api/luts", data={"file": (io.BytesIO(b"broken content"), "bad.cube")})
    assert response.status_code == 400
    assert response.json["error"]["message"]
    assert lut_service.list() == []
    assert list(app.extensions["studio"]["settings"].luts.glob("*.cube")) == []
    assert list(app.extensions["studio"]["settings"].luts.glob("*.tmp")) == []


def test_non_utf8_upload_is_rejected(client):
    response = client.post("/api/luts", data={"file": (io.BytesIO(b"\xff\xfe\x00bad"), "bin.cube")})
    assert response.status_code == 400
    assert "UTF-8" in response.json["error"]["message"]


def test_process_and_preview_with_lut(client, uploaded, uploaded_lut):
    original_bytes = client.get(uploaded["url"]).data
    step = {"operation": "color_lut", "params": {"lut_id": uploaded_lut["id"], "strength": 0.8}}
    response = client.post("/api/process", json={"asset_id": uploaded["id"], "steps": [step]})
    assert response.status_code == 201, response.json
    result = response.json["data"]
    assert result["parent_id"] == uploaded["id"]
    assert result["steps"] == [step]
    # 原片未被改动，预览不产生新素材
    assert client.get(uploaded["url"]).data == original_bytes
    total = client.get("/api/assets").json["data"]["total"]
    preview = client.post("/api/preview", json={"asset_id": uploaded["id"], "steps": [step]})
    assert preview.status_code == 200
    assert preview.mimetype == "image/png"
    assert client.get("/api/assets").json["data"]["total"] == total


def test_missing_lut_reference_reports_404(client, uploaded):
    response = client.post("/api/process", json={
        "asset_id": uploaded["id"],
        "steps": [{"operation": "color_lut", "params": {"lut_id": "f" * 32, "strength": 1}}],
    })
    assert response.status_code == 404
    assert response.json["error"]["code"] == "missing_lut"


def test_lut_delete_blocked_while_preset_uses_it(client, uploaded_lut):
    preset = client.post("/api/presets", json={
        "name": "引用 LUT 的预设",
        "description": "",
        "steps": [{"operation": "color_lut",
                   "params": {"lut_id": uploaded_lut["id"], "strength": 1}}],
    })
    assert preset.status_code == 201, preset.json
    blocked = client.delete(f"/api/luts/{uploaded_lut['id']}")
    assert blocked.status_code == 409
    assert client.delete(f"/api/presets/{preset.json['data']['id']}").status_code == 200
    assert client.delete(f"/api/luts/{uploaded_lut['id']}").status_code == 200


def test_lut_collection_included_in_backup(client, uploaded_lut):
    backup = client.get("/api/backup").json
    assert uploaded_lut["id"] in backup["collections"]["luts"]["records"]


def test_lut_renders_inside_multi_step_pipeline(client, uploaded, uploaded_lut):
    response = client.post("/api/process", json={
        "asset_id": uploaded["id"],
        "steps": [
            {"operation": "color_brightness", "params": {"factor": 1.1}},
            {"operation": "color_lut", "params": {"lut_id": uploaded_lut["id"], "strength": 0.4}},
        ],
    })
    assert response.status_code == 201
    assert response.json["data"]["width"] == uploaded["width"]


def test_integer_grid_is_stable_and_indexed_bgr(registry, tmp_path):
    from backend.luts import parse_cube

    # 恒等 LUT 的预计算网格在晶格节点上必须还原输入颜色
    identity = parse_cube(make_3d_cube(4, lambda rgb: rgb))
    grid = identity.integer_grid()
    assert grid.shape == (256, 256, 256, 3) and grid.dtype == np.uint8
    assert tuple(int(value) for value in grid[0, 0, 255]) == (255, 0, 0)
    assert tuple(int(value) for value in grid[255, 0, 0]) == (0, 0, 255)
    assert tuple(int(value) for value in grid[255, 255, 255]) == (255, 255, 255)
    # 重复构建返回同一缓存对象，应用结果与网格直接索引一致
    assert identity.integer_grid() is grid
    image = solid_image((137, 92, 210), alpha=188)
    applied = np.asarray(identity.apply(image, 1.0))
    rgb = np.asarray(image.convert("RGB"))
    assert np.array_equal(applied[..., :3], grid[rgb[..., 2], rgb[..., 1], rgb[..., 0]])
    assert np.array_equal(applied[..., 3], np.asarray(image)[..., 3])


def test_lut_service_lru_evicts_old_grids(app):
    service = app.extensions["studio"]["luts"]

    def cube(seed):
        lines = ["LUT_3D_SIZE 2"]
        for index in range(8):
            lines.append(f"{(index + seed) % 2 / 1:.6f} {index / 8:.6f} 0.5")
        return ("\n".join(lines) + "\n").encode()

    ids = []
    for index in range(3):
        record = service.upload(cube(index), f"lut{index}.cube")
        ids.append(record["id"])
        service.get_lut(record["id"]).integer_grid()
    assert len(service.cache) == service.MAX_CACHED_GRIDS
    assert ids[0] not in service.cache  # 最久未用的整数网格被释放
    assert all(item._grid256 is not None for item in service.cache.values())
    service.get_lut(ids[0]).integer_grid()  # 重新访问会重建并淘汰下一个
    assert ids[1] not in service.cache and ids[0] in service.cache
