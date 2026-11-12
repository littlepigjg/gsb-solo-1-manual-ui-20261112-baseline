"""Editable color palettes: GIMP import, image extraction, mapping and exchange export.

Color matching is deterministic: squared Euclidean distance in sRGB, ties are
won by the color that appears earlier in the palette. Applying a palette keeps
the source alpha channel untouched and always saves a new asset copy, so the
original image is never modified. Every write validates the complete palette
first and persists it with one atomic JSON replacement — a failed import or
update never leaves half a palette behind.
"""
import io
import json
import os
import numpy as np
from PIL import Image
from .errors import StudioError
from .storage import new_id
from .validation import (
    object_value, reject_unknown, identifier, text_value, number_value,
    color_value, list_value, choice_value,
)


MAX_COLORS = 256
MAX_PALETTES = 100
MAX_GPL_BYTES = 1024 * 1024
EXTRACT_MAX = 32
DEFAULT_COLUMNS = 8
EXPORT_FORMATS = {"gpl": ("gpl", "text/plain"), "css": ("css", "text/css"),
                  "json": ("json", "application/json"), "txt": ("txt", "text/plain")}
ALPHA_VISIBLE = 128


def _palette_text(value, label, maximum, minimum=0):
    """Palette names must survive one-color-per-line exchange formats."""
    value = text_value(value, label, maximum, minimum)
    if "\n" in value or "\t" in value:
        raise StudioError(f"{label}不能包含换行或制表符")
    return value


def validate_colors(value, maximum=MAX_COLORS):
    items = list_value(value, "颜色列表", minimum=1, maximum=maximum)
    colors = []
    for item in items:
        object_value(item, "颜色")
        reject_unknown(item, {"hex", "name"}, "颜色")
        colors.append({
            "hex": color_value(item.get("hex"), "颜色值"),
            "name": _palette_text(item.get("name", ""), "颜色名称", 80),
        })
    return colors


def parse_gpl(payload):
    """Parse a GIMP palette. Any defect raises before a record exists."""
    if not payload:
        raise StudioError("调色板文件为空")
    if len(payload) > MAX_GPL_BYTES:
        raise StudioError("调色板文件过大", status=413)
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise StudioError("调色板文件不是有效的 UTF-8 文本，文件可能已损坏") from error
    lines = text.splitlines()
    if not lines or lines[0].strip() != "GIMP Palette":
        raise StudioError("不是有效的 GIMP 调色板：缺少 “GIMP Palette” 文件头")
    name = None
    columns = None
    colors = []
    for lineno, raw in enumerate(lines[1:], start=2):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("Name:"):
            name = line[len("Name:"):].strip()
            continue
        if line.startswith("Columns:"):
            token = line[len("Columns:"):].strip()
            try:
                columns = int(token)
            except ValueError as error:
                raise StudioError(f"第 {lineno} 行：Columns 必须是整数") from error
            if not 1 <= columns <= MAX_COLORS:
                raise StudioError(f"第 {lineno} 行：Columns 必须在 1–{MAX_COLORS} 之间")
            continue
        parts = line.split(None, 3)
        if len(parts) < 3:
            raise StudioError(f"第 {lineno} 行：颜色行需要 3 个通道值，无法识别")
        channels = []
        for token in parts[:3]:
            try:
                channel = int(token)
            except ValueError as error:
                raise StudioError(f"第 {lineno} 行：颜色通道 “{token}” 不是整数") from error
            if not 0 <= channel <= 255:
                raise StudioError(f"第 {lineno} 行：颜色值 {channel} 超出 0–255 范围")
            channels.append(channel)
        colors.append({
            "hex": "#" + "".join(f"{channel:02x}" for channel in channels),
            "name": parts[3].strip() if len(parts) > 3 else "",
        })
        if len(colors) > MAX_COLORS:
            raise StudioError(f"调色板颜色超过 {MAX_COLORS} 个上限（第 {lineno} 行）")
    if not colors:
        raise StudioError("调色板不包含任何颜色")
    return name, columns, colors


def extract_colors(image, count):
    """Deterministic median cut over the 5-bit histogram of visible pixels.

    Box averages use the original 8-bit pixels, so pure colors survive
    exactly. Identical input always yields identical colors and ordering.
    """
    rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
    pixels = rgba[..., :3][rgba[..., 3] >= ALPHA_VISIBLE]
    if not len(pixels):
        raise StudioError("图片没有可见像素，无法提取配色")
    rgb = pixels.astype(np.uint16)
    keys = (rgb[:, 0] >> 3) * 1024 + (rgb[:, 1] >> 3) * 32 + (rgb[:, 2] >> 3)
    histogram = np.bincount(keys, minlength=32768)
    nonzero = np.flatnonzero(histogram)
    bins = np.stack([nonzero // 1024, nonzero // 32 % 32, nonzero % 32], axis=1)
    weights = histogram[nonzero].astype(np.int64)
    boxes = [(bins, weights)]
    while len(boxes) < count:
        best = None
        for index, (box_bins, box_weights) in enumerate(boxes):
            if len(box_bins) < 2:
                continue
            population = int(box_weights.sum())
            if best is None or population > best[0]:
                best = (population, index)
        if best is None:
            break
        box_bins, box_weights = boxes.pop(best[1])
        ranges = box_bins.max(axis=0).astype(np.int64) - box_bins.min(axis=0).astype(np.int64)
        channel = int(np.argmax(ranges))
        others = [axis for axis in range(3) if axis != channel]
        order = np.lexsort((box_bins[:, others[1]], box_bins[:, others[0]], box_bins[:, channel]))
        box_bins, box_weights = box_bins[order], box_weights[order]
        cumulative = np.cumsum(box_weights)
        split = int(np.searchsorted(cumulative, cumulative[-1] / 2, side="left"))
        split = min(max(split, 1), len(box_bins) - 1)
        boxes.insert(best[1], (box_bins[split:], box_weights[split:]))
        boxes.insert(best[1], (box_bins[:split], box_weights[:split]))
    owners = np.empty(32768, dtype=np.int32)
    for index, (box_bins, box_weights) in enumerate(boxes):
        owners[(box_bins[:, 0].astype(np.int64) * 1024 + box_bins[:, 1] * 32 + box_bins[:, 2])] = index
    assignment = owners[keys]
    populations = np.bincount(assignment, minlength=len(boxes))
    colors = []
    for channel in range(3):
        totals = np.bincount(assignment, weights=rgb[:, channel], minlength=len(boxes))
        colors.append(np.rint(totals / np.maximum(populations, 1)))
    averages = np.stack(colors, axis=1).astype(np.uint8)
    ordering = sorted(range(len(boxes)),
                      key=lambda i: (-int(populations[i]), *averages[i].tolist()))
    result = []
    seen = set()
    for index in ordering:
        hex_value = "#" + "".join(f"{channel:02x}" for channel in averages[index])
        if hex_value in seen:
            continue
        seen.add(hex_value)
        result.append({"hex": hex_value, "name": hex_value})
    return result


def map_to_palette(image, targets):
    """Replace each pixel with its nearest palette color; alpha is preserved."""
    rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
    height, width = rgba.shape[:2]
    alpha = rgba[..., 3]
    pixels = rgba[..., :3].reshape(-1, 3).astype(np.int32)
    targets = np.asarray(targets, dtype=np.int32)
    target_energy = (targets * targets).sum(axis=1)
    mapped = np.empty_like(pixels)
    chunk = 65536
    for start in range(0, len(pixels), chunk):
        block = pixels[start:start + chunk]
        distance = (block * block).sum(axis=1)[:, None] + target_energy[None, :] - 2 * (block @ targets.T)
        nearest = distance.argmin(axis=1)
        mapped[start:start + chunk] = targets[nearest]
    output = Image.fromarray(mapped.reshape(height, width, 3).astype(np.uint8), "RGB").convert("RGBA")
    output.putalpha(Image.fromarray(alpha, "L"))
    return output


def _safe_filename(name):
    cleaned = "".join(char if char not in '\\/:*?"<>|\n\t' else " " for char in name).strip()
    return cleaned[:100] or "palette"


def render_export(record, format_name):
    name = record["name"].replace("\n", " ").replace("\t", " ")
    colors = record["colors"]
    if format_name == "gpl":
        lines = ["GIMP Palette", f"Name: {name}", f"Columns: {record.get('columns') or DEFAULT_COLUMNS}", "#"]
        for color in colors:
            channels = tuple(int(color["hex"][offset:offset + 2], 16) for offset in (1, 3, 5))
            lines.append("{:>3} {:>3} {:>3}\t{}".format(*channels, color["name"] or color["hex"]))
        return ("\n".join(lines) + "\n").encode("utf-8")
    if format_name == "css":
        safe_name = name.replace("*/", "*")
        lines = [f"/* {safe_name} · 光屿图像工作室 */", ":root {"]
        for index, color in enumerate(colors, start=1):
            color_name = color["name"].replace("*/", "*")
            label = f" /* {color_name} */" if color_name else ""
            lines.append(f"  --palette-{index:02d}: {color['hex']};{label}")
        lines.append("}")
        return ("\n".join(lines) + "\n").encode("utf-8")
    if format_name == "json":
        payload = {
            "name": record["name"],
            "columns": record.get("columns") or DEFAULT_COLUMNS,
            "colors": [
                {"hex": color["hex"], "name": color["name"],
                 "rgb": [int(color["hex"][offset:offset + 2], 16) for offset in (1, 3, 5)]}
                for color in colors
            ],
        }
        return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return ("\n".join(color["hex"] for color in colors) + "\n").encode("utf-8")


class PaletteService:
    def __init__(self, settings, store, assets, processing):
        self.settings = settings
        self.store = store
        self.assets = assets
        self.processing = processing

    def list(self):
        records = self.store.all("palettes")
        records.sort(key=lambda item: item["created_at"], reverse=True)
        return records

    def get(self, palette_id):
        identifier(palette_id)
        return self.store.get("palettes", palette_id)

    def _insert(self, record):
        if len(self.store.all("palettes")) >= MAX_PALETTES:
            raise StudioError(f"调色板数量已达 {MAX_PALETTES} 个，请先删除不用的调色板")
        return self.store.insert("palettes", record)

    def create(self, payload):
        object_value(payload)
        reject_unknown(payload, {"name", "colors", "columns"})
        record = {
            "name": _palette_text(payload.get("name"), "调色板名称", 180, 1),
            "colors": validate_colors(payload.get("colors")),
            "columns": number_value(payload.get("columns", DEFAULT_COLUMNS), "列数", 1, MAX_COLORS, integer=True),
            "source": "manual",
            "source_asset_id": None,
        }
        record = self._insert(record)
        self.store.event("palette", f"新建调色板 {record['name']}", {"palette_id": record["id"]})
        return record

    def import_gpl(self, payload, filename):
        from pathlib import Path
        name, columns, colors = parse_gpl(payload)
        fallback = Path(str(filename or "").replace("\\", "/")).stem or "导入调色板"
        record = {
            "name": _palette_text(name or fallback, "调色板名称", 180, 1),
            "colors": validate_colors(colors),
            "columns": columns or DEFAULT_COLUMNS,
            "source": "gpl",
            "source_asset_id": None,
        }
        record = self._insert(record)
        self.store.event("palette", f"导入 GIMP 调色板 {record['name']}", {"palette_id": record["id"]})
        return record

    def extract(self, asset_id, count=8, name=None):
        identifier(asset_id)
        count = number_value(count, "颜色数量", 2, EXTRACT_MAX, integer=True)
        if name is not None:
            name = _palette_text(name, "调色板名称", 180, 1)
        image, asset = self.assets.load(asset_id)
        colors = extract_colors(image, count)
        if name is None:
            base = asset["name"].rsplit(".", 1)[0][:150] or "图片"
            name = f"{base} 的配色"
        record = {
            "name": name,
            "colors": colors,
            "columns": DEFAULT_COLUMNS,
            "source": "image",
            "source_asset_id": asset_id,
        }
        record = self._insert(record)
        self.store.event("palette", f"从 {asset['name']} 提取 {len(colors)} 种颜色", {"palette_id": record["id"]})
        return record

    def update(self, palette_id, payload):
        identifier(palette_id)
        self.store.get("palettes", palette_id)
        object_value(payload)
        reject_unknown(payload, {"name", "colors", "columns"})
        changes = {}
        if "name" in payload:
            changes["name"] = _palette_text(payload["name"], "调色板名称", 180, 1)
        if "colors" in payload:
            changes["colors"] = validate_colors(payload["colors"])
        if "columns" in payload:
            changes["columns"] = number_value(payload["columns"], "列数", 1, MAX_COLORS, integer=True)
        return self.store.update("palettes", palette_id, changes)

    def delete(self, palette_id):
        record = self.get(palette_id)
        self.store.delete("palettes", palette_id)
        self.store.event("palette", f"删除调色板 {record['name']}")

    def apply(self, palette_id, asset_id):
        palette = self.get(palette_id)
        identifier(asset_id)
        targets = [[int(color["hex"][offset:offset + 2], 16) for offset in (1, 3, 5)]
                   for color in palette["colors"]]
        with self.processing.compute_lock:
            image, asset = self.assets.load(asset_id)
            mapped = map_to_palette(image, targets)
            base = asset["name"].rsplit(".", 1)[0][:100]
            result = self.assets.save(
                mapped,
                name=f"{base} · {palette['name']}"[:180],
                parent=asset,
                steps=[],
                metadata={
                    "palette_id": palette["id"],
                    "palette_name": palette["name"],
                    "palette_colors": len(targets),
                    "match": "sRGB 欧氏距离最近色，距离相同时取调色板中靠前的颜色",
                },
            )
        self.store.event("palette", f"套用 {palette['name']} 到 {asset['name']}", {
            "palette_id": palette["id"], "asset_id": asset_id, "result_id": result["id"],
        })
        return result

    def export(self, palette_id, format_name):
        format_name = choice_value(format_name, list(EXPORT_FORMATS), "导出格式")
        record = self.get(palette_id)
        payload = render_export(record, format_name)
        extension, mimetype = EXPORT_FORMATS[format_name]
        export_id = new_id()
        filename = f"{export_id}.{extension}"
        path = self.settings.exports / filename
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, path)
            stored = self.store.insert("exports", {
                "id": export_id,
                "filename": filename,
                "download_name": _safe_filename(record["name"]) + "." + extension,
                "bytes": len(payload),
                "asset_ids": [],
                "options": {"format": format_name},
                "kind": "palette",
                "mimetype": mimetype,
                "palette_id": palette_id,
            })
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
        stored["url"] = f"/api/exports/{export_id}/download"
        self.store.event("export", f"导出调色板 {record['name']}（{format_name}）", {"export_id": export_id})
        return stored
