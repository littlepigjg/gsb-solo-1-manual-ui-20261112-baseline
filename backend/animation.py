"""Frame sequencing, coordinated palette quantization and animated GIF encoding.

GIF only supports 256 palette colors and 1-bit transparency. Every frame shares
one palette built from all frames so the loop stays color-coordinated, and
subtle color differences are preserved as well as the format allows through
optional Floyd-Steinberg dithering. Mismatched frame sizes are placed on one
common canvas, repeated frames and transparent assets are reported explicitly
instead of being dropped silently, and per-frame durations are rounded to the
GIF 10 ms grid so downloaded timing matches the arranged timeline.
"""
import hashlib
import io
import json
import os
from urllib.parse import quote

import numpy as np
from PIL import Image, ImageColor

from .imaging import flatten
from .storage import new_id
from .validation import (
    boolean_value,
    choice_value,
    color_value,
    dimension_guard,
    identifier,
    list_value,
    number_value,
    object_value,
    reject_unknown,
)

MIN_DURATION_MS = 20
MAX_DURATION_MS = 10000
MAX_CANVAS_EDGE = 1920
PREVIEW_EDGE = 480
SAMPLE_EDGE = 320
MAX_SAMPLE_PIXELS = 800_000
ALPHA_THRESHOLD = 128
FIT_MODES = ("contain", "cover", "stretch", "center")
FIT_NAMES = {
    "contain": "适应留白",
    "cover": "铺满裁切",
    "stretch": "拉伸铺满",
    "center": "原始尺寸居中",
}
ALPHA_MODES = ("keep", "flatten")
CANVAS_MODES = ("max", "first", "custom")

try:
    _DITHER_FLOYD = Image.Dither.FLOYDSTEINBERG
    _DITHER_NONE = Image.Dither.NONE
except AttributeError:  # pragma: no cover - legacy Pillow fallbacks
    _DITHER_FLOYD = Image.FLOYDSTEINBERG
    _DITHER_NONE = Image.NONE
DITHERS = {"floyd": _DITHER_FLOYD, "none": _DITHER_NONE}

try:
    _MEDIANCUT = Image.Quantize.MEDIANCUT
except AttributeError:  # pragma: no cover - legacy Pillow fallback
    _MEDIANCUT = Image.MEDIANCUT


class AnimationService:
    """Builds looping GIFs from ordered studio assets."""

    def __init__(self, settings, store, assets, processing):
        self.settings = settings
        self.store = store
        self.assets = assets
        self.processing = processing

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def validate(self, options):
        object_value(options, "动图参数")
        reject_unknown(options, {
            "frames", "canvas_mode", "width", "height", "fit", "background",
            "loop", "alpha_mode", "colors", "dither",
        }, "动图参数")
        raw_frames = list_value(
            options.get("frames"), "帧序列",
            minimum=2, maximum=self.settings.max_batch,
        )
        frames = []
        for index, item in enumerate(raw_frames):
            object_value(item, f"第 {index + 1} 帧")
            reject_unknown(item, {"asset_id", "duration_ms"}, f"第 {index + 1} 帧")
            frames.append({
                "asset_id": identifier(item.get("asset_id"), f"第 {index + 1} 帧素材"),
                "duration_ms": number_value(
                    item.get("duration_ms", 800),
                    f"第 {index + 1} 帧停留时间",
                    MIN_DURATION_MS, MAX_DURATION_MS, integer=True,
                ),
            })
        canvas_mode = choice_value(options.get("canvas_mode", "max"), CANVAS_MODES, "画布模式")
        width = height = None
        if canvas_mode == "custom":
            width = number_value(options.get("width"), "画布宽度", 16, MAX_CANVAS_EDGE, integer=True)
            height = number_value(options.get("height"), "画布高度", 16, MAX_CANVAS_EDGE, integer=True)
            dimension_guard(width, height, self.settings)
        return {
            "frames": frames,
            "canvas_mode": canvas_mode,
            "width": width,
            "height": height,
            "fit": choice_value(options.get("fit", "contain"), FIT_MODES, "尺寸适应方式"),
            "background": color_value(options.get("background", "#ffffff")),
            "loop": boolean_value(options.get("loop", True), "循环播放"),
            "alpha_mode": choice_value(options.get("alpha_mode", "keep"), ALPHA_MODES, "透明素材处理"),
            "colors": number_value(options.get("colors", 256), "调色板颜色数", 2, 256, integer=True),
            "dither": choice_value(options.get("dither", "floyd"), ("floyd", "none"), "抖动方式"),
        }

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def build(self, options, preview=False):
        options = self.validate(options)
        with self.processing.compute_lock:
            loaded = []
            for index, frame in enumerate(options["frames"]):
                image, asset = self.assets.load(frame["asset_id"])
                loaded.append({"image": image, "asset": asset, "frame": frame, "index": index})
            warnings = []
            warnings.extend(self._size_warnings(loaded, options))
            warnings.extend(self._duplicate_warnings(loaded))
            canvas = self._canvas_size(loaded, options)
            if preview:
                scale = min(1.0, PREVIEW_EDGE / max(canvas))
                canvas = (
                    max(1, round(canvas[0] * scale)),
                    max(1, round(canvas[1] * scale)),
                )
            prepared = [self._place_frame(item["image"], canvas, options) for item in loaded]
            has_transparency = options["alpha_mode"] == "keep" and any(
                frame.getchannel("A").getextrema()[0] < 255 for frame in prepared
            )
            if has_transparency:
                warnings.append(
                    "已保留透明通道；GIF 仅支持 1 位透明，半透明边缘会先与背景色融合，"
                    "再按透明度阈值转为全透明或全不透明。"
                )
            palette_image, transparent_index = self._build_palette(prepared, options, has_transparency)
            gif_frames = self._quantize_frames(
                prepared, palette_image, transparent_index, options, has_transparency,
            )
            raw_durations = [frame["duration_ms"] for frame in options["frames"]]
            durations, aligned = self._round_durations(raw_durations)
            if aligned:
                warnings.append("GIF 的时间粒度为 10 ms，停留时间已按四舍五入对齐到最近的 10 ms。")
            buffer = io.BytesIO()
            save_kwargs = {
                "format": "GIF",
                "save_all": True,
                "append_images": gif_frames[1:],
                "duration": durations,
                "optimize": False,
                "include_color_table": False,
            }
            if options["loop"]:
                save_kwargs["loop"] = 0
            if has_transparency:
                save_kwargs["transparency"] = transparent_index
                save_kwargs["disposal"] = 2
            gif_frames[0].save(buffer, **save_kwargs)
        meta = {
            "width": canvas[0],
            "height": canvas[1],
            "frame_count": len(gif_frames),
            "durations_ms": durations,
            "requested_durations_ms": raw_durations,
            "total_duration_ms": sum(durations),
            "loop": options["loop"],
            "transparency": has_transparency,
            "colors": options["colors"],
            "fit": options["fit"],
            "canvas_mode": options["canvas_mode"],
            "background": options["background"],
            "warnings": warnings,
            "preview": preview,
            "first_name": loaded[0]["asset"]["name"],
        }
        return buffer, meta

    def create(self, options):
        buffer, meta = self.build(options, preview=False)
        return self._save(buffer.getvalue(), meta, options)

    def _canvas_size(self, loaded, options):
        if options["canvas_mode"] == "custom":
            return options["width"], options["height"]
        if options["canvas_mode"] == "first":
            return loaded[0]["image"].size
        return (
            max(item["image"].width for item in loaded),
            max(item["image"].height for item in loaded),
        )

    def _place_frame(self, image, canvas, options):
        canvas_w, canvas_h = canvas
        keep_alpha = options["alpha_mode"] == "keep"
        if keep_alpha:
            frame = Image.new("RGBA", canvas, (0, 0, 0, 0))
        else:
            frame = Image.new("RGBA", canvas, ImageColor.getrgb(options["background"]) + (255,))
        image = image.convert("RGBA")
        fit = options["fit"]
        if fit == "stretch":
            self._composite(frame, image.resize(canvas, Image.Resampling.LANCZOS), 0, 0)
            return frame
        if fit == "center":
            x = (canvas_w - image.width) // 2
            y = (canvas_h - image.height) // 2
            self._composite(frame, image, x, y)
            return frame
        ratio = (min if fit == "contain" else max)(canvas_w / image.width, canvas_h / image.height)
        new_size = (
            max(1, round(image.width * ratio)),
            max(1, round(image.height * ratio)),
        )
        scaled = image.resize(new_size, Image.Resampling.LANCZOS)
        self._composite(frame, scaled, (canvas_w - new_size[0]) // 2, (canvas_h - new_size[1]) // 2)
        return frame

    @staticmethod
    def _composite(frame, image, x, y):
        """Alpha composite with clipping, so negative or overflowing offsets work."""
        fw, fh = frame.size
        iw, ih = image.size
        left, top = max(0, x), max(0, y)
        right, bottom = min(fw, x + iw), min(fh, y + ih)
        if right <= left or bottom <= top:
            return
        region = image.crop((left - x, top - y, right - x, bottom - y))
        frame.alpha_composite(region, (left, top))

    def _build_palette(self, frames, options, has_transparency):
        """One median-cut palette sampled from every frame, shared by the loop."""
        pixels = self._sample_pixels(frames)
        sample_image = Image.fromarray(pixels.reshape(-1, 1, 3), mode="RGB")
        target_colors = options["colors"]
        transparent_index = None
        if has_transparency:
            target_colors = max(2, options["colors"] - 1)
            transparent_index = target_colors - 1
        palette_image = sample_image.quantize(
            colors=target_colors,
            method=_MEDIANCUT,
            dither=_DITHER_NONE,
        )
        if has_transparency:
            palette = palette_image.getpalette()
            palette[transparent_index * 3:transparent_index * 3 + 3] = ImageColor.getrgb(options["background"])
            palette_image.putpalette(palette)
        return palette_image, transparent_index

    def _sample_pixels(self, frames):
        chunks = []
        for frame in frames:
            sample = frame.convert("RGB")
            edge = max(sample.size)
            if edge > SAMPLE_EDGE:
                ratio = SAMPLE_EDGE / edge
                sample = sample.resize(
                    (max(1, round(sample.width * ratio)), max(1, round(sample.height * ratio))),
                    Image.Resampling.BILINEAR,
                )
            chunks.append(np.asarray(sample, dtype=np.uint8).reshape(-1, 3))
        pixels = np.ascontiguousarray(np.concatenate(chunks, axis=0))
        if len(pixels) > MAX_SAMPLE_PIXELS:
            generator = np.random.default_rng(42)
            indices = generator.choice(len(pixels), size=MAX_SAMPLE_PIXELS, replace=False)
            pixels = pixels[indices]
        return np.ascontiguousarray(pixels)

    def _quantize_frames(self, frames, palette_image, transparent_index, options, has_transparency):
        dither = DITHERS[options["dither"]]
        gif_frames = []
        for frame in frames:
            rgb = flatten(frame, options["background"])
            quantized = rgb.quantize(palette=palette_image, dither=dither)
            if has_transparency:
                mask = frame.getchannel("A").point(
                    lambda value: 255 if value < ALPHA_THRESHOLD else 0
                )
                quantized.paste(transparent_index, mask=mask)
            gif_frames.append(quantized)
        return gif_frames

    @staticmethod
    def _round_durations(raw_durations):
        durations = [
            max(MIN_DURATION_MS, min(MAX_DURATION_MS, int(round(value / 10.0)) * 10))
            for value in raw_durations
        ]
        return durations, any(actual != requested for actual, requested in zip(durations, raw_durations))

    # ------------------------------------------------------------------
    # Explanations for the arranged timeline
    # ------------------------------------------------------------------
    def _size_warnings(self, loaded, options):
        sizes = {item["image"].size for item in loaded}
        if len(sizes) == 1:
            return []
        details = "；".join(
            f"第 {item['index'] + 1} 帧 {item['image'].width}×{item['image'].height}"
            for item in loaded
        )
        return [
            f"各帧尺寸不一致，已统一放置到同一画布，并按「{FIT_NAMES[options['fit']]}」"
            f"处理（{options['background']} 填补留白）：{details}。"
        ]

    def _duplicate_warnings(self, loaded):
        groups = {}
        for item in loaded:
            digest = hashlib.sha256(item["image"].tobytes()).hexdigest()
            groups.setdefault(digest, []).append(item["index"])
        repeated = [group for group in groups.values() if len(group) > 1]
        if not repeated:
            return []
        texts = ["、".join(f"第 {index + 1} 帧" for index in group) for group in repeated]
        return [
            f"检测到画面完全相同的重复帧（{'；'.join(texts)}），已分别保留，"
            "按各自停留时间重复写入，未自动删除。"
        ]

    # ------------------------------------------------------------------
    # Persistence into the existing exports collection
    # ------------------------------------------------------------------
    def _save(self, payload, meta, options):
        export_id = new_id()
        filename = f"{export_id}.gif"
        path = self.settings.exports / filename
        temporary = path.with_suffix(".tmp")
        record_options = dict(options)
        record_options["format"] = "GIF"
        base_name = meta["first_name"].rsplit(".", 1)[0][:100]
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, path)
            record = self.store.insert("exports", {
                "id": export_id,
                "filename": filename,
                "download_name": f"{base_name}_循环动图.gif",
                "bytes": len(payload),
                "asset_ids": [frame["asset_id"] for frame in options["frames"]],
                "options": record_options,
                "kind": "animation",
                "animation": {
                    "width": meta["width"],
                    "height": meta["height"],
                    "frame_count": meta["frame_count"],
                    "durations_ms": meta["durations_ms"],
                    "total_duration_ms": meta["total_duration_ms"],
                    "loop": meta["loop"],
                    "transparency": meta["transparency"],
                    "colors": meta["colors"],
                    "warnings": meta["warnings"],
                },
            })
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
        record["url"] = f"/api/exports/{export_id}/download"
        self.store.event("animation", f"生成循环动图 {record['download_name']}", {
            "export_id": export_id,
            "frame_count": meta["frame_count"],
            "total_duration_ms": meta["total_duration_ms"],
        })
        return record


def preview_headers(meta):
    """Custom response headers carrying timing and warnings for the ephemeral preview."""
    return {
        "X-Frame-Count": str(meta["frame_count"]),
        "X-Total-Duration-Ms": str(meta["total_duration_ms"]),
        "X-Canvas-Size": f"{meta['width']}x{meta['height']}",
        "X-Animation-Transparency": "1" if meta["transparency"] else "0",
        "X-Animation-Warnings": quote(json.dumps(meta["warnings"], ensure_ascii=False)),
    }
