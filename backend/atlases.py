"""Sprite atlas production: trimming, rotation-aware packing and ZIP delivery.

The service deliberately separates *planning* (load images, trim transparent
borders and run the deterministic packer) from *writing* any output file.
When one or more sprites cannot fit the requested canvas, planning fails with
a complete list of offenders and no atlas image, export record or JSON event
is produced — a failed production can never look like a partial success.
"""
import io
import json
import os
import zipfile
from PIL import Image
from .errors import StudioError
from .packer import pack
from .storage import new_id
from .validation import (
    boolean_value, list_value, number_value, object_value, reject_unknown,
)


class AtlasService:
    def __init__(self, settings, store, assets):
        self.settings = settings
        self.store = store
        self.assets = assets

    def validate(self, data):
        object_value(data, "图集请求")
        reject_unknown(data, {
            "asset_ids", "canvas_width", "canvas_height", "padding",
            "trim_transparent", "allow_rotation",
        }, "图集请求")
        asset_ids = list_value(data.get("asset_ids"), "图集素材", minimum=1, maximum=self.settings.max_atlas_assets)
        seen = set()
        unique_ids = []
        for asset_id in asset_ids:
            if not isinstance(asset_id, str):
                raise StudioError("图集素材必须是素材 ID 数组")
            if asset_id in seen:
                raise StudioError("素材列表中存在重复选择，请去掉重复项后重试",
                                  code="duplicate_asset", details={"asset_id": asset_id})
            seen.add(asset_id)
            unique_ids.append(asset_id)
        canvas_width = number_value(data.get("canvas_width", 1024), "画布宽度", 1, self.settings.max_dimension, integer=True)
        canvas_height = number_value(data.get("canvas_height", 1024), "画布高度", 1, self.settings.max_dimension, integer=True)
        if canvas_width * canvas_height > self.settings.max_pixels:
            raise StudioError(f"图集画布不能超过 {self.settings.max_pixels:,} 像素")
        padding = number_value(data.get("padding", 2), "图块间距", 0, 256, integer=True)
        return {
            "asset_ids": unique_ids,
            "canvas_width": canvas_width,
            "canvas_height": canvas_height,
            "padding": padding,
            "trim_transparent": boolean_value(data.get("trim_transparent", True), "去除透明空边"),
            "allow_rotation": boolean_value(data.get("allow_rotation", False), "允许旋转"),
        }

    def _prepare(self, options):
        """Load every source and derive the trimmed content rectangles."""
        prepared = []
        empty_names = []
        for asset_id in options["asset_ids"]:
            image, record = self.assets.load(asset_id)
            width, height = image.size
            trim_box = (0, 0, width, height)
            # Fully transparent sprites are rejected regardless of the trim
            # toggle: they carry no reconstructable content and must never be
            # silently mixed into a delivery.
            bbox = image.getchannel("A").getbbox()
            if bbox is None:
                empty_names.append(record["name"])
            elif options["trim_transparent"]:
                trim_box = bbox
            prepared.append({
                "asset_id": asset_id,
                "name": record["name"],
                "image": image,
                "source_width": width,
                "source_height": height,
                "trim_box": trim_box,
            })
        if empty_names:
            raise StudioError(
                "存在完全透明、没有任何可见像素的素材，无法制作图集；请移除后重试",
                code="empty_sprite",
                details={"empty_assets": empty_names},
            )
        return prepared

    def plan(self, data):
        options = self.validate(data)
        prepared = self._prepare(options)
        sprites = [
            (item["asset_id"], item["trim_box"][2] - item["trim_box"][0],
             item["trim_box"][3] - item["trim_box"][1])
            for item in prepared
        ]
        placements, unpacked = pack(
            sprites,
            options["canvas_width"],
            options["canvas_height"],
            allow_rotation=options["allow_rotation"],
            padding=options["padding"],
        )
        if unpacked:
            names = [item["name"] for item in prepared if item["asset_id"] in set(unpacked)]
            raise StudioError(
                "有素材无法放入目标画布，整次图集制作已停止；请加大画布、缩小素材或允许旋转",
                code="atlas_overflow",
                details={"unpacked_asset_ids": unpacked, "unpacked_names": names},
            )
        placement_map = {item.key: item for item in placements}
        frames = []
        used_width = used_height = 0
        for item in prepared:
            placement = placement_map[item["asset_id"]]
            left, top, right, bottom = item["trim_box"]
            content_width = right - left
            content_height = bottom - top
            frame_width = content_height if placement.rotated else content_width
            frame_height = content_width if placement.rotated else content_height
            used_width = max(used_width, placement.x + frame_width + options["padding"])
            used_height = max(used_height, placement.y + frame_height + options["padding"])
            frames.append({
                "asset_id": item["asset_id"],
                "name": item["name"],
                "frame": {"x": placement.x, "y": placement.y, "w": frame_width, "h": frame_height},
                "rotated": placement.rotated,
                "trimmed": options["trim_transparent"] and (left, top, right, bottom) != (
                    0, 0, item["source_width"], item["source_height"]),
                "source_size": {"w": item["source_width"], "h": item["source_height"]},
                "trim_box": {"x": left, "y": top, "w": content_width, "h": content_height},
                "thumbnail_url": f"/api/assets/{item['asset_id']}/thumbnail",
            })
        return options, prepared, frames, {"width": used_width, "height": used_height}

    def preview(self, data):
        options, _prepared, frames, used = self.plan(data)
        return {
            "canvas": {"width": options["canvas_width"], "height": options["canvas_height"]},
            "used": used,
            "padding": options["padding"],
            "trim_transparent": options["trim_transparent"],
            "allow_rotation": options["allow_rotation"],
            "sprite_count": len(frames),
            "frames": frames,
        }

    def _unique_names(self, frames):
        used = set()
        warnings = []
        for frame in frames:
            base = frame["name"].rsplit(".", 1)[0]
            candidate = base
            if candidate in used:
                candidate = f"{base}_{frame['asset_id'][:8]}"
                suffix = 1
                while candidate in used:
                    suffix += 1
                    candidate = f"{base}_{frame['asset_id'][:8]}_{suffix}"
                warnings.append({"asset_id": frame["asset_id"], "name": frame["name"], "unique_name": candidate})
            used.add(candidate)
            frame["unique_name"] = candidate
        return warnings

    def build_atlas(self, options, prepared, frames):
        canvas = Image.new("RGBA", (options["canvas_width"], options["canvas_height"]), (0, 0, 0, 0))
        by_id = {item["asset_id"]: item for item in prepared}
        for frame in frames:
            source = by_id[frame["asset_id"]]["image"]
            box = frame["trim_box"]
            content = source.crop((box["x"], box["y"], box["x"] + box["w"], box["y"] + box["h"]))
            if frame["rotated"]:
                content = content.transpose(Image.Transpose.ROTATE_90)
            canvas.alpha_composite(content, (frame["frame"]["x"], frame["frame"]["y"]))
        return canvas

    def create(self, data):
        options, prepared, frames, used = self.plan(data)
        name_warnings = self._unique_names(frames)
        canvas = self.build_atlas(options, prepared, frames)
        manifest = {
            "format": "guangyu-atlas/1",
            "atlas": {"width": options["canvas_width"], "height": options["canvas_height"]},
            "options": {
                "padding": options["padding"],
                "trim_transparent": options["trim_transparent"],
                "allow_rotation": options["allow_rotation"],
            },
            "restoration_guide": (
                "对每个 sprites 条目：按 frame.x/y/w/h 从图集取出图块；"
                "若 rotated 为 true，图块在制作时顺时针旋转了 90°，还原时逆时针旋转 90°（Pillow 可用 Image.rotate(-90, expand=True)）；"
                "再新建 source_size.w × source_size.h 的透明画布，把内容按 trim_box.x/trim_box.y 贴回，"
                "即恢复每张素材原来的大小、方向与全部像素。"
            ),
            "sprite_count": len(frames),
            "sprites": [
                {
                    "asset_id": frame["asset_id"],
                    "name": frame["name"],
                    "unique_name": frame["unique_name"],
                    "frame": frame["frame"],
                    "rotated": frame["rotated"],
                    "trimmed": frame["trimmed"],
                    "source_size": frame["source_size"],
                    "trim_box": frame["trim_box"],
                }
                for frame in frames
            ],
            "name_warnings": name_warnings,
        }
        image_buffer = io.BytesIO()
        canvas.save(image_buffer, format="PNG", optimize=True, compress_level=6)
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("atlas.png", image_buffer.getvalue())
            archive.writestr("atlas-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        payload = zip_buffer.getvalue()

        export_id = new_id()
        filename = f"{export_id}.zip"
        path = self.settings.exports / filename
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, path)
            record = self.store.insert("exports", {
                "id": export_id,
                "filename": filename,
                "download_name": "光屿_图集.zip",
                "bytes": len(payload),
                "asset_ids": options["asset_ids"],
                "options": {
                    "kind": "atlas",
                    "canvas_width": options["canvas_width"],
                    "canvas_height": options["canvas_height"],
                    "padding": options["padding"],
                    "trim_transparent": options["trim_transparent"],
                    "allow_rotation": options["allow_rotation"],
                },
                "kind": "atlas",
            })
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
        record["url"] = f"/api/exports/{export_id}/download"
        self.store.event("atlas", f"制作图集 · {len(frames)} 张素材", {
            "export_id": export_id,
            "canvas": [options["canvas_width"], options["canvas_height"]],
            "renamed": len(name_warnings),
        })
        return record, manifest, used
