"""保留区域智能构图：一次标记，同版生成多种尺寸的填满画面构图。"""
import math
import numpy as np
from PIL import Image
from .errors import StudioError
from .validation import (
    dimension_guard,
    identifier,
    list_value,
    number_value,
    object_value,
    reject_unknown,
    text_value,
)

ALPHA_THRESHOLD = 8
MAX_REGIONS = 8
MAX_TARGETS = 8
DEFAULT_PADDING = 15


def content_bounds(image):
    """有效内容包围盒，忽略近乎全透明的边缘；完全透明时返回 None。"""
    alpha = np.asarray(image.getchannel("A"))
    visible = alpha > ALPHA_THRESHOLD
    if not visible.any():
        return None
    rows = np.nonzero(visible.any(axis=1))[0]
    columns = np.nonzero(visible.any(axis=0))[0]
    return (int(columns[0]), int(rows[0]), int(columns[-1]) + 1, int(rows[-1]) + 1)


def _union(boxes):
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _clamp(value, low, high):
    return min(max(value, low), high)


def plan_window(image_width, image_height, regions, target_width, target_height,
                padding_percent, anchor=None):
    """为单个目标尺寸计算取景窗口和每个保留区域的落实情况。

    regions 为 [(left, top, right, bottom, importance, index), ...] 像素坐标，
    调用方已按重要度降序排列；anchor 是没有任何区域时参考的内容包围盒。
    窗口向外取整并钳制在画面内，被纳入集合的区域保证完整留在窗口中。
    返回 (window=(left, top, right, bottom), reports)。
    """
    ratio = target_width / target_height
    if image_width / image_height >= ratio:
        max_width = image_height * ratio
        max_height = float(image_height)
    else:
        max_width = float(image_width)
        max_height = image_width / ratio

    included = []
    for region in regions:
        union = _union([item[:4] for item in included] + [region[:4]])
        if (union[2] - union[0] <= max_width + 0.5
                and union[3] - union[1] <= max_height + 0.5):
            included.append(region)

    if included:
        subject = _union([item[:4] for item in included])
    elif regions:
        subject = regions[0][:4]
    elif anchor:
        subject = anchor
    else:
        subject = (0.0, 0.0, float(image_width), float(image_height))

    subject_width = max(subject[2] - subject[0], 1.0)
    subject_height = max(subject[3] - subject[1], 1.0)
    grow = 1 + padding_percent / 100
    want_width = subject_width * grow
    want_height = subject_height * grow
    if want_width / want_height < ratio:
        want_width = want_height * ratio
    else:
        want_height = want_width / ratio
    scale = min(1.0, max_width / want_width, max_height / want_height)
    window_width = want_width * scale
    window_height = want_height * scale

    center_x = (subject[0] + subject[2]) / 2
    center_y = (subject[1] + subject[3]) / 2
    left = _clamp(center_x - window_width / 2, 0.0, image_width - window_width)
    top = _clamp(center_y - window_height / 2, 0.0, image_height - window_height)

    box_left = max(0, int(math.floor(left)))
    box_top = max(0, int(math.floor(top)))
    box_right = min(image_width, int(math.ceil(left + window_width)))
    box_bottom = min(image_height, int(math.ceil(top + window_height)))
    box_right = max(box_right, box_left + 1)
    box_bottom = max(box_bottom, box_top + 1)
    window = (box_left, box_top, box_right, box_bottom)

    reports = []
    for region in regions:
        r_left, r_top, r_right, r_bottom = region[:4]
        overlap_width = max(0, min(r_right, box_right) - max(r_left, box_left))
        overlap_height = max(0, min(r_bottom, box_bottom) - max(r_top, box_top))
        area = max((r_right - r_left) * (r_bottom - r_top), 1e-9)
        coverage = _clamp(overlap_width * overlap_height / area, 0.0, 1.0)
        if coverage >= 0.999:
            status = "kept"
        elif coverage > 0.001:
            status = "partial"
        else:
            status = "dropped"
        reports.append({"index": region[5], "coverage": round(coverage, 4), "status": status})
    reports.sort(key=lambda item: item["index"])
    return window, reports


def validate_regions(value):
    items = list_value(value, "保留区域", minimum=0, maximum=MAX_REGIONS)
    regions = []
    for item in items:
        object_value(item, "保留区域")
        reject_unknown(item, {"x", "y", "width", "height", "importance"}, "保留区域")
        x = number_value(item.get("x"), "区域起点 x", 0, 1)
        y = number_value(item.get("y"), "区域起点 y", 0, 1)
        width = number_value(item.get("width"), "区域宽度", 0.0001, 1)
        height = number_value(item.get("height"), "区域高度", 0.0001, 1)
        if x + width > 1.0001 or y + height > 1.0001:
            raise StudioError("保留区域超出画面范围")
        importance = number_value(item.get("importance", 2), "重要度", 1, 3, integer=True)
        regions.append({"x": x, "y": y, "width": width, "height": height,
                        "importance": importance})
    return regions


def validate_targets(value, settings):
    items = list_value(value, "目标尺寸", minimum=1, maximum=MAX_TARGETS)
    targets = []
    seen = set()
    for item in items:
        object_value(item, "目标尺寸")
        reject_unknown(item, {"name", "width", "height"}, "目标尺寸")
        width = number_value(item.get("width"), "目标宽度", 1,
                             settings.max_dimension, integer=True)
        height = number_value(item.get("height"), "目标高度", 1,
                              settings.max_dimension, integer=True)
        dimension_guard(width, height, settings)
        if (width, height) in seen:
            raise StudioError("目标尺寸重复", details={"width": width, "height": height})
        seen.add((width, height))
        name = item.get("name")
        if name is None:
            name = f"{width}×{height}"
        else:
            name = text_value(name, "尺寸名称", 40, 1)
        targets.append({"name": name, "width": width, "height": height})
    return targets


class CompositionService:
    """同一份保留区域标记，预览与最终生成共用同一套取景算法。"""

    def __init__(self, settings, store, assets, processing):
        self.settings = settings
        self.store = store
        self.assets = assets
        self.processing = processing

    def _validate(self, data):
        asset_id = identifier(data.get("asset_id"))
        regions = validate_regions(data.get("regions", []))
        targets = validate_targets(data.get("targets"), self.settings)
        padding = number_value(data.get("padding", DEFAULT_PADDING),
                               "留白比例", 0, 100, integer=True)
        return asset_id, regions, targets, padding

    def _pixel_regions(self, regions, width, height):
        ordered = sorted(enumerate(regions),
                         key=lambda item: (-item[1]["importance"], item[0]))
        return [
            (region["x"] * width, region["y"] * height,
             (region["x"] + region["width"]) * width,
             (region["y"] + region["height"]) * height,
             region["importance"], index)
            for index, region in ordered
        ]

    def _plans(self, image, regions, targets, padding):
        anchor = content_bounds(image)
        pixel_regions = self._pixel_regions(regions, image.width, image.height)
        alpha = None
        if image.getchannel("A").getextrema()[0] < 255:
            alpha = np.asarray(image.getchannel("A"))
        plans = []
        for target in targets:
            window, reports = plan_window(
                image.width, image.height, pixel_regions,
                target["width"], target["height"], padding, anchor)
            left, top, right, bottom = window
            transparent = 0.0
            if alpha is not None:
                section = alpha[top:bottom, left:right]
                transparent = round(float((section <= ALPHA_THRESHOLD).mean()) * 100, 1)
            plans.append({
                "target": target,
                "window": {"left": left, "top": top,
                           "width": right - left, "height": bottom - top},
                "output": {"width": target["width"], "height": target["height"]},
                "scale": round(target["width"] / max(right - left, 1), 3),
                "regions": reports,
                "kept": sum(1 for item in reports if item["status"] == "kept"),
                "total": len(reports),
                "transparent_percent": transparent,
            })
        return plans

    def plan(self, data):
        """只读计算每个目标尺寸的取景窗口与取舍报告，不写入任何记录。"""
        asset_id, regions, targets, padding = self._validate(data)
        with self.processing.compute_lock:
            image, record = self.assets.load(asset_id)
            plans = self._plans(image, regions, targets, padding)
        return {
            "asset_id": record["id"],
            "source": {"width": image.width, "height": image.height},
            "plans": plans,
        }

    def apply(self, data):
        """按同一算法重新取景并保存副本；原图与历史保持不变。"""
        asset_id, regions, targets, padding = self._validate(data)
        results = []
        with self.processing.compute_lock:
            image, parent = self.assets.load(asset_id)
            plans = self._plans(image, regions, targets, padding)
            base = parent["name"].rsplit(".", 1)[0][:90]
            for target, plan in zip(targets, plans):
                window = plan["window"]
                section = image.crop((
                    window["left"], window["top"],
                    window["left"] + window["width"], window["top"] + window["height"],
                ))
                size = (target["width"], target["height"])
                planes = [channel.resize(size, Image.Resampling.LANCZOS)
                          for channel in section.split()]
                composed = Image.merge("RGBA", planes)
                result = self.assets.save(
                    composed,
                    name=f"{base} · 智能构图 {target['name']}",
                    parent=parent,
                    steps=[{"operation": "smart_compose", "params": {
                        "width": target["width"],
                        "height": target["height"],
                        "window": window,
                        "regions": len(regions),
                        "padding": padding,
                    }}],
                    metadata={"compose": {
                        "target": target,
                        "window": window,
                        "regions": plan["regions"],
                        "kept": plan["kept"],
                        "total": plan["total"],
                    }},
                )
                results.append(result)
        self.store.event("compose", f"智能构图生成 {len(results)} 个尺寸", {
            "source_id": asset_id,
            "result_ids": [item["id"] for item in results],
        })
        return {"assets": results}
