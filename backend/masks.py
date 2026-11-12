"""Reusable masks that limit an adjustment to part of an image."""
import hashlib
import io
import os
import threading
from pathlib import Path
import numpy as np
from PIL import Image, ImageFilter, ImageOps
from .errors import StudioError, Conflict
from .imaging import decode_image, smoothstep
from .storage import new_id
from .validation import boolean_value, choice_value, identifier, number_value, text_value

MAX_FEATHER = 250

BUILTINS = [
    {"key": "radial", "name": "径向渐变", "description": "中心完全覆盖，向四周均匀淡出。"},
    {"key": "linear", "name": "线性渐变", "description": "左侧完全覆盖，向右均匀淡出。"},
    {"key": "circle", "name": "柔和圆形", "description": "中央圆形区域，边缘柔和过渡。"},
    {"key": "vignette", "name": "四周暗角框", "description": "四角与边缘覆盖，中心保持原样。"},
]


def builtin_id(key):
    """Deterministic 32-hex id, so catalog defaults can reference builtins."""
    return hashlib.md5(f"builtin-mask:{key}".encode("utf-8")).hexdigest()


def render_builtin(key, width, height):
    """Procedural grayscale coverage; white is fully covered."""
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    center_x, center_y = (width - 1) / 2, (height - 1) / 2
    distance = np.hypot(xx - center_x, yy - center_y) * 2 / min(width, height)
    if key == "radial":
        values = np.clip(1.15 - distance, 0, 1)
    elif key == "linear":
        values = 1 - xx / max(width - 1, 1)
    elif key == "circle":
        values = 1 - smoothstep(0.5, 0.8, distance)
    elif key == "vignette":
        values = smoothstep(0.55, 1.15, distance)
    else:
        raise StudioError("未知的内置蒙版", details={"key": key})
    return Image.fromarray((values * 255).astype(np.uint8), "L")


class MaskService:
    def __init__(self, settings, store, assets):
        self.settings = settings
        self.store = store
        self.assets = assets
        self.lock = threading.RLock()
        self.settings.masks.mkdir(parents=True, exist_ok=True)
        self.ensure_builtins()

    def _file(self, mask_id, thumbnail=False):
        identifier(mask_id, "蒙版ID")
        suffix = ".thumb.png" if thumbnail else ".png"
        return self.settings.masks / (mask_id + suffix)

    def public(self, record):
        result = dict(record)
        result["url"] = f"/api/masks/{record['id']}/file"
        result["thumbnail_url"] = f"/api/masks/{record['id']}/thumbnail"
        return result

    def ensure_builtins(self):
        with self.lock:
            existing = {item.get("builtin_key") for item in self.store.all("masks")}
            for definition in BUILTINS:
                if definition["key"] in existing:
                    continue
                mask_id = builtin_id(definition["key"])
                thumbnail = render_builtin(definition["key"], 320, 240)
                thumbnail.save(self._file(mask_id, True), format="PNG")
                self.store.insert("masks", {
                    "id": mask_id,
                    "name": definition["name"],
                    "kind": "builtin",
                    "builtin_key": definition["key"],
                    "description": definition["description"],
                    "adaptive": True,
                    "width": None,
                    "height": None,
                    "has_alpha": True,
                    "bytes": 0,
                })

    def list(self):
        records = self.store.all("masks")
        records.sort(key=lambda item: (item["kind"] != "builtin", item["created_at"]))
        return [self.public(item) for item in records]

    def get(self, mask_id):
        identifier(mask_id, "蒙版ID")
        return self.public(self.store.get("masks", mask_id))

    def upload(self, payload, name):
        image, original_format, frames = decode_image(payload, self.settings)
        name = Path(str(name).replace("\\", "/")).name
        name = text_value(name or "未命名蒙版", "文件名", 180, 1)
        mask_id = new_id()
        path = self._file(mask_id)
        thumbnail_path = self._file(mask_id, True)
        temporary = path.with_suffix(".tmp")
        image = image.convert("RGBA")
        image.info.clear()
        with self.lock:
            try:
                image.save(temporary, format="PNG", compress_level=4)
                os.replace(temporary, path)
                thumbnail = image.copy()
                thumbnail.thumbnail((320, 240), Image.Resampling.LANCZOS)
                thumbnail.save(thumbnail_path, format="PNG")
                record = self.store.insert("masks", {
                    "id": mask_id,
                    "name": name,
                    "kind": "upload",
                    "builtin_key": None,
                    "description": "",
                    "adaptive": False,
                    "width": image.width,
                    "height": image.height,
                    "has_alpha": image.getchannel("A").getextrema()[0] < 255,
                    "bytes": path.stat().st_size,
                    "source_format": original_format,
                })
            except Exception:
                path.unlink(missing_ok=True)
                thumbnail_path.unlink(missing_ok=True)
                raise
            finally:
                temporary.unlink(missing_ok=True)
        self.store.event("mask", f"导入蒙版 {name}", {"mask_id": mask_id})
        return self.public(record)

    def delete(self, mask_id):
        record = self.get(mask_id)
        if record["kind"] == "builtin":
            raise StudioError("内置蒙版不能删除")
        for preset in self.store.all("presets"):
            for step in preset.get("steps", []):
                params = step.get("params", {})
                if step.get("operation") == "mask_local_adjust" and params.get("mask_id") == mask_id:
                    raise Conflict(f"预设「{preset['name']}」正在使用该蒙版，请先修改预设")
        with self.lock:
            self.store.delete("masks", mask_id)
            self._file(mask_id).unlink(missing_ok=True)
            self._file(mask_id, True).unlink(missing_ok=True)
        self.store.event("mask", f"删除蒙版 {record['name']}", {"mask_id": mask_id})

    def file(self, mask_id, thumbnail=False):
        record = self.get(mask_id)
        path = self._file(mask_id, thumbnail)
        if record["kind"] == "builtin" and not path.exists():
            size = (320, 240) if thumbnail else (800, 600)
            render_builtin(record["builtin_key"], *size).save(path, format="PNG")
        if not path.exists():
            raise StudioError("蒙版文件丢失", status=410)
        return path, record

    def coverage(self, mask_id, size, mode, invert, feather):
        """Float coverage in [0, 1] at the target size; raises before any save."""
        mode = choice_value(mode, ("alpha", "luminance"), "覆盖依据")
        invert = boolean_value(invert, "反转蒙版")
        feather = number_value(feather, "柔化边缘", 0, MAX_FEATHER, integer=True)
        width, height = int(size[0]), int(size[1])
        record = self.get(mask_id)
        if record["kind"] == "builtin":
            gray = render_builtin(record["builtin_key"], width, height)
            alpha = gray
        else:
            path = self._file(mask_id)
            if not path.exists():
                raise StudioError("蒙版文件丢失，请重新导入", code="missing_file", status=410)
            with Image.open(path) as opened:
                opened.load()
                mask = opened.convert("RGBA")
            if mask.size != (width, height):
                raise StudioError(
                    "蒙版尺寸与当前图片不一致，请选择尺寸相同的蒙版",
                    code="mask_size_mismatch",
                    details={
                        "mask_width": mask.width,
                        "mask_height": mask.height,
                        "image_width": width,
                        "image_height": height,
                    },
                )
            gray = mask.convert("L")
            alpha = mask.getchannel("A")
        base = alpha if mode == "alpha" else gray
        if invert:
            base = ImageOps.invert(base)
        if feather:
            base = base.filter(ImageFilter.GaussianBlur(feather))
        return np.asarray(base, dtype=np.float32) / 255

    def render_coverage_preview(self, asset_id, mask_id, mode, invert, feather):
        """Ephemeral grayscale coverage overlay for the asset's dimensions."""
        asset = self.assets.get(asset_id)
        coverage = self.coverage(mask_id, (asset["width"], asset["height"]), mode, invert, feather)
        image = Image.fromarray((coverage * 255).astype(np.uint8), "L")
        image.thumbnail((1200, 1000), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        buffer.seek(0)
        return buffer
