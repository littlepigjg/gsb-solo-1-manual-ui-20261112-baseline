"""Adobe .cube 调色文件（LUT）：严格解析、持久化存储与可分离插值应用。

调色表只有有限的晶格采样点，应用时先在 8 位输入空间预计算 256^3 整数查找表，
对落在采样点之间的颜色做线性插值；输入超出定义域时钳制到边界晶格；
透明通道与零强度结果原样保留。
"""
import hashlib
import math
import os
import threading
from pathlib import Path

import numpy as np
from PIL import Image

from .errors import Conflict, MissingRecord, StudioError
from .storage import new_id
from .validation import identifier, text_value

MAX_3D_SIZE = 128
MAX_1D_SIZE = 4096
KEYWORDS = {"TITLE", "LUT_1D_SIZE", "LUT_3D_SIZE", "DOMAIN_MIN", "DOMAIN_MAX"}
DATA_START = frozenset("0123456789+-.")


def _floats(tokens, count, lineno, label):
    if len(tokens) != count:
        raise StudioError(
            f"调色文件第 {lineno} 行的{label}需要 {count} 个数值，实际为 {len(tokens)} 个",
            code="lut_invalid",
        )
    try:
        values = [float(token) for token in tokens]
    except ValueError as error:
        raise StudioError(f"调色文件第 {lineno} 行包含无法解析的数值", code="lut_invalid") from error
    if not all(math.isfinite(value) for value in values):
        raise StudioError(f"调色文件第 {lineno} 行包含非有限数值", code="lut_invalid")
    return values


def _size_value(tokens, lineno):
    value = _floats(tokens, 1, lineno, "采样尺寸")[0]
    if value != int(value):
        raise StudioError(f"调色文件第 {lineno} 行的采样尺寸必须是整数", code="lut_invalid")
    return int(value)


def parse_cube(text):
    """解析 .cube 文本，所有结构问题都抛出带原因的 StudioError。"""
    if text.startswith("﻿"):
        text = text[1:]
    if not text.strip():
        raise StudioError("调色文件为空，未找到任何配置或颜色数据", code="lut_invalid")
    title = ""
    size_1d = None
    size_3d = None
    domain_min = (0.0, 0.0, 0.0)
    domain_max = (1.0, 1.0, 1.0)
    seen = set()
    rows = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        tokens = line.split()
        key = tokens[0].upper()
        if key in KEYWORDS:
            if key in seen:
                raise StudioError(
                    f"调色文件配置重复：{tokens[0]} 只能声明一次",
                    code="lut_duplicate",
                )
            seen.add(key)
            if key == "TITLE":
                title = " ".join(tokens[1:]).strip().strip('"').strip("'")[:120]
            elif key == "LUT_1D_SIZE":
                size_1d = _size_value(tokens[1:], lineno)
            elif key == "LUT_3D_SIZE":
                size_3d = _size_value(tokens[1:], lineno)
            elif key == "DOMAIN_MIN":
                domain_min = tuple(_floats(tokens[1:], 3, lineno, "DOMAIN_MIN"))
            else:  # DOMAIN_MAX
                domain_max = tuple(_floats(tokens[1:], 3, lineno, "DOMAIN_MAX"))
        elif tokens[0][:1] in DATA_START:
            rows.append(_floats(tokens, 3, lineno, "颜色数据"))
        else:
            raise StudioError(
                f"调色文件第 {lineno} 行包含无法识别的内容：{tokens[0]}",
                code="lut_invalid",
            )
    if size_1d is not None and size_3d is not None:
        raise StudioError("调色文件同时声明了一维与三维表，暂不支持此类组合", code="lut_invalid")
    if size_1d is None and size_3d is None:
        raise StudioError(
            "调色文件缺少 LUT_3D_SIZE 或 LUT_1D_SIZE 声明，无法确定采样点数量",
            code="lut_missing",
        )
    kind = "3d" if size_3d is not None else "1d"
    size = size_3d if size_3d is not None else size_1d
    limit = MAX_3D_SIZE if kind == "3d" else MAX_1D_SIZE
    if not 2 <= size <= limit:
        raise StudioError(
            f"采样尺寸 {size} 超出支持范围（2 至 {limit}）",
            code="lut_invalid",
        )
    for channel in range(3):
        if domain_max[channel] <= domain_min[channel]:
            raise StudioError(
                f"DOMAIN_MAX 必须大于 DOMAIN_MIN（第 {('R', 'G', 'B')[channel]} 通道）",
                code="lut_invalid",
            )
    expected = size ** 3 if kind == "3d" else size
    layout = f"{size}×{size}×{size} 三维表" if kind == "3d" else f"{size} 级一维表"
    if len(rows) < expected:
        raise StudioError(
            f"调色文件内容不完整：声明 {layout}、共 {expected} 行颜色数据，实际只有 {len(rows)} 行",
            code="lut_corrupt",
        )
    if len(rows) > expected:
        raise StudioError(
            f"调色文件内容损坏：颜色数据比声明的 {expected} 行多出 {len(rows) - expected} 行",
            code="lut_corrupt",
        )
    table = np.asarray(rows, dtype=np.float32)
    if kind == "3d":
        table = table.reshape(size, size, size, 3)  # 轴顺序：蓝、绿、红（红变化最快）
    return CubeLUT(kind, size, domain_min, domain_max, table, title)


class CubeLUT:
    """解析后的调色表：一维曲线或三维晶格，带输入定义域。"""

    def __init__(self, kind, size, domain_min, domain_max, table, title=""):
        self.kind = kind
        self.size = size
        self.domain_min = np.asarray(domain_min, dtype=np.float32)
        self.domain_max = np.asarray(domain_max, dtype=np.float32)
        self.table = table
        self.title = title
        self._grid256 = None  # 惰性构建的 256^3 整数查找表

    @property
    def points(self):
        return self.size ** 3 if self.kind == "3d" else self.size

    def output_range(self):
        names = ("red", "green", "blue")
        return {
            name: [round(float(self.table[..., index].min()), 6),
                   round(float(self.table[..., index].max()), 6)]
            for index, name in enumerate(names)
        }

    def integer_grid(self):
        """在 8 位输入空间预计算整张查找表（256^3×3 字节），应用时只做整数索引。

        构建采用可分离插值：依次沿红、绿、蓝三个轴对相邻晶格做定点混合，
        中间结果保持 uint8，临时数组控制在约 50 MB，避免全量浮点混合的耗时尖峰。
        """
        if self._grid256 is None:
            self._grid256 = self._grid_from_3d() if self.kind == "3d" else self._grid_from_1d()
        return self._grid256

    def _axis_index(self):
        """每个 8 位输入值对应的晶格下界、上界索引与 0–256 定点混合权重。"""
        axis = np.linspace(0.0, 1.0, 256, dtype=np.float32)
        scaled = np.clip(
            (axis[:, None] - self.domain_min) / (self.domain_max - self.domain_min),
            0.0, 1.0,
        ) * (self.size - 1)
        lower = np.minimum(np.floor(scaled).astype(np.int32), self.size - 2)
        weight = np.clip(np.rint((scaled - lower) * 256.0), 0, 256).astype(np.uint16)
        upper = np.minimum(lower + 1, self.size - 1).astype(np.uint8)
        return lower.astype(np.uint8), upper, weight

    @staticmethod
    def _mix(low, high, weight):
        """uint8 线性混合，weight 为 0–256 的定点权重。"""
        weight = np.asarray(weight, dtype=np.uint16)
        result = low.astype(np.uint16) * (256 - weight) + high.astype(np.uint16) * weight
        return (result // 256).astype(np.uint8)

    def _grid_from_3d(self):
        r_lo, r_hi, r_w = (item[:, 0] for item in self._axis_index())
        g_lo, g_hi, g_w = (item[:, 1] for item in self._axis_index())
        b_lo, b_hi, b_w = (item[:, 2] for item in self._axis_index())
        table = np.clip(np.rint(self.table * 255.0), 0, 255).astype(np.uint8)
        # 红轴：对每个（蓝晶格, 绿晶格）组合，在全部 256 个红色输入上做定点插值
        red_stage = np.empty((self.size, self.size, 256, 3), dtype=np.uint8)
        for blue in range(self.size):
            for green in range(self.size):
                red_stage[blue, green] = self._mix(
                    table[blue, green, r_lo], table[blue, green, r_hi], r_w[:, None])
        # 绿轴与蓝轴：在 256×256 的输出平面上依次做定点混合
        grid = np.empty((256, 256, 256, 3), dtype=np.uint8)
        for blue in range(256):
            bl, bh, bw = b_lo[blue], b_hi[blue], b_w[blue]
            plane = np.empty((256, 256, 3), dtype=np.uint8)
            for green in range(256):
                gl, gh, gw = g_lo[green], g_hi[green], g_w[green]
                low = self._mix(red_stage[bl, gl], red_stage[bl, gh], gw)
                high = self._mix(red_stage[bh, gl], red_stage[bh, gh], gw)
                plane[green] = self._mix(low, high, bw)
            grid[blue] = plane
        return grid

    def _grid_from_1d(self):
        lower, upper, weight = self._axis_index()
        table = np.clip(np.rint(self.table * 255.0), 0, 255).astype(np.uint8)
        grid = np.empty((256, 256, 256, 3), dtype=np.uint8)
        for channel in range(3):
            layer = self._mix(table[lower[:, channel], channel],
                              table[upper[:, channel], channel],
                              weight[:, channel])
            view = grid[..., channel]
            view[:] = layer.reshape(256, 1, 1) if channel == 2 else (
                layer.reshape(1, 256, 1) if channel == 1 else layer.reshape(1, 1, 256))
        return grid

    def apply(self, image, strength):
        source = image.convert("RGBA")
        if strength <= 0:
            return source  # 零强度：与原图完全一致，不做任何数值运算
        rgb = np.asarray(source.convert("RGB"))
        grid = self.integer_grid()
        mapped = grid[rgb[..., 2], rgb[..., 1], rgb[..., 0]]  # 表轴顺序：蓝、绿、红
        if strength < 1:
            blended = rgb.astype(np.float32) * (1.0 - strength) + mapped.astype(np.float32) * strength
            mapped = np.clip(np.rint(blended), 0, 255).astype(np.uint8)
        output = Image.fromarray(mapped, "RGB").convert("RGBA")
        output.putalpha(source.getchannel("A"))  # 透明度始终沿用原片
        return output


class LutService:
    """调色文件的上传、查询、删除与解析缓存；原始 .cube 文本独立落盘。"""

    MAX_CACHED_GRIDS = 2  # 每个整数查找表约 50 MB，仅保留最近使用的两张

    def __init__(self, settings, store):
        self.settings = settings
        self.store = store
        self.lock = threading.RLock()
        self.cache = {}  # 按访问顺序排列，队首最久未使用

    def _touch(self, lut_id, lut):
        self.cache.pop(lut_id, None)
        self.cache[lut_id] = lut
        while len(self.cache) > self.MAX_CACHED_GRIDS:
            old_id, old_lut = next(iter(self.cache.items()))
            old_lut._grid256 = None  # 释放整数查找表，原始晶格保留
            del self.cache[old_id]

    def _file(self, lut_id):
        identifier(lut_id)
        return self.settings.luts / f"{lut_id}.cube"

    def list(self):
        records = self.store.all("luts")
        records.sort(key=lambda item: item["created_at"], reverse=True)
        return records

    def get(self, lut_id):
        identifier(lut_id)
        try:
            return self.store.get("luts", lut_id)
        except MissingRecord:
            raise StudioError("调色文件不存在，可能已被删除", code="missing_lut", status=404) from None

    def upload(self, payload, name):
        if not payload:
            raise StudioError("调色文件为空", code="lut_invalid")
        limit = self.settings.max_lut_bytes
        if len(payload) > limit:
            raise StudioError(
                f"调色文件超过 {limit // (1024 * 1024)} MB 大小限制",
                code="lut_too_large",
                status=413,
            )
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise StudioError(
                "调色文件不是有效的 UTF-8 文本，文件可能已损坏",
                code="lut_invalid",
            ) from error
        lut = parse_cube(text)  # 先校验通过，再写盘，避免留下无效文件
        name = Path(str(name).replace("\\", "/")).name
        name = text_value(name or "未命名调色", "文件名", 180, 1)
        lut_id = new_id()
        path = self._file(lut_id)
        temporary = path.with_suffix(".tmp")
        record = {
            "id": lut_id,
            "name": name,
            "title": lut.title or name,
            "kind": lut.kind,
            "size": lut.size,
            "points": lut.points,
            "domain_min": [round(float(value), 6) for value in lut.domain_min],
            "domain_max": [round(float(value), 6) for value in lut.domain_max],
            "output_range": lut.output_range(),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        with self.lock:
            try:
                temporary.write_bytes(payload)
                os.replace(temporary, path)
                record = self.store.insert("luts", record)
            except Exception:
                path.unlink(missing_ok=True)
                raise
            finally:
                temporary.unlink(missing_ok=True)
        self._touch(lut_id, lut)
        self.store.event("lut_upload", f"导入调色文件 {name}", {"lut_id": lut_id})
        return record

    def get_lut(self, lut_id):
        with self.lock:
            if lut_id in self.cache:
                self._touch(lut_id, self.cache[lut_id])
                return self.cache[lut_id]
            record = self.get(lut_id)
            path = self._file(record["id"])
            if not path.exists():
                raise StudioError("调色文件内容丢失，请重新上传", code="missing_lut_file", status=410)
            try:
                lut = parse_cube(path.read_text(encoding="utf-8-sig"))
            except StudioError as error:
                raise StudioError(f"调色文件已损坏：{error.message}", code="lut_corrupt") from error
            self._touch(record["id"], lut)
            return lut

    def delete(self, lut_id):
        record = self.get(lut_id)
        users = list(dict.fromkeys(
            preset["name"]
            for preset in self.store.all("presets")
            for step in preset.get("steps", [])
            if step.get("operation") == "color_lut"
            and step.get("params", {}).get("lut_id") == lut_id
        ))
        if users:
            raise Conflict(f"调色文件正被预设「{users[0]}」使用，请先修改或删除该预设")
        with self.lock:
            self.store.delete("luts", lut_id)
            self._file(lut_id).unlink(missing_ok=True)
            self.cache.pop(lut_id, None)
            self.store.event("lut_delete", f"删除调色文件 {record['name']}")
