"""Contact sheet composition.

The service only reads existing assets and (for the create route) appends one
record to the exports collection. It never writes assets, processing history or
the asset library. Repeated asset ids are kept in selection order, every image
is scaled to fit its cell without cropping so landscape and portrait frames keep
their full aspect ratio, and transparent pixels are alpha-composited onto the
configured background. Slots beyond the last image stay blank.
"""
import base64
import io
import os
from PIL import Image, ImageColor
from .imaging import dimension_guard
from .storage import new_id
from .validation import (
    color_value,
    identifier,
    list_value,
    number_value,
    object_value,
    reject_unknown,
)


PREVIEW_MAX_EDGE = 1600
ALLOWED_FIELDS = {"asset_ids", "columns", "cell_width", "cell_height", "gap", "padding", "background"}


class ContactSheetService:
    def __init__(self, settings, store, assets, processing):
        self.settings = settings
        self.store = store
        self.assets = assets
        self.processing = processing

    def validate(self, data):
        object_value(data, "联系印样参数")
        reject_unknown(data, ALLOWED_FIELDS, "联系印样参数")
        asset_ids = list_value(data.get("asset_ids"), "素材列表", minimum=1, maximum=self.settings.max_batch)
        asset_ids = [identifier(value, "素材 ID") for value in asset_ids]
        columns = number_value(data.get("columns", 4), "每行数量", 1, self.settings.max_batch, integer=True)
        cell_width = number_value(data.get("cell_width", 320), "单元格宽度", 60, 1200, integer=True)
        cell_height = number_value(data.get("cell_height", 320), "单元格高度", 60, 1200, integer=True)
        gap = number_value(data.get("gap", 24), "单元格间距", 0, 200, integer=True)
        padding = number_value(data.get("padding", 36), "画布留白", 0, 300, integer=True)
        background = color_value(data.get("background", "#f7f8f4"), "背景颜色")
        return {
            "asset_ids": asset_ids,
            "columns": columns,
            "cell_width": cell_width,
            "cell_height": cell_height,
            "gap": gap,
            "padding": padding,
            "background": background,
        }

    def _measure(self, options):
        count = len(options["asset_ids"])
        columns = options["columns"]
        rows = (count + columns - 1) // columns
        width = options["padding"] * 2 + columns * options["cell_width"] + (columns - 1) * options["gap"]
        height = options["padding"] * 2 + rows * options["cell_height"] + (rows - 1) * options["gap"]
        dimension_guard(width, height, self.settings)
        return rows, columns, (width, height)

    def render(self, options):
        """Render the full-resolution sheet. Raises before any write on error."""
        rows, columns, (width, height) = self._measure(options)
        background = ImageColor.getrgb(options["background"]) + (255,)
        sheet = Image.new("RGBA", (width, height), background)
        cell_w = options["cell_width"]
        cell_h = options["cell_height"]
        with self.processing.compute_lock:
            for index, asset_id in enumerate(options["asset_ids"]):
                image, _record = self.assets.load(asset_id)
                fitted = image.copy()
                fitted.thumbnail((cell_w, cell_h), Image.Resampling.LANCZOS)
                row, column = divmod(index, columns)
                left = options["padding"] + column * (cell_w + options["gap"])
                top = options["padding"] + row * (cell_h + options["gap"])
                offset_x = left + (cell_w - fitted.width) // 2
                offset_y = top + (cell_h - fitted.height) // 2
                sheet.alpha_composite(fitted, (offset_x, offset_y))
        return sheet, {"rows": rows, "columns": columns, "width": width, "height": height, "cells": rows * columns}

    def preview(self, options):
        """Ephemeral preview: no JSON record and no file are created."""
        sheet, meta = self.render(options)
        displayed = sheet.copy()
        displayed.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        displayed.save(buffer, format="PNG", optimize=True)
        payload = base64.b64encode(buffer.getvalue()).decode("ascii")
        meta["preview_width"] = displayed.width
        meta["preview_height"] = displayed.height
        meta["image"] = "data:image/png;base64," + payload
        return meta

    def create(self, options):
        options = self.validate(options)
        sheet, meta = self.render(options)
        buffer = io.BytesIO()
        sheet.save(buffer, format="PNG", optimize=True, compress_level=6)
        payload = buffer.getvalue()
        export_id = new_id()
        filename = f"{export_id}.png"
        path = self.settings.exports / filename
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, path)
            record = self.store.insert("exports", {
                "id": export_id,
                "filename": filename,
                "download_name": "光屿_联系印样.png",
                "bytes": len(payload),
                "asset_ids": options["asset_ids"],
                "options": {**options, "format": "PNG"},
                "kind": "contact_sheet",
                "layout": meta,
            })
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
        record["url"] = f"/api/exports/{export_id}/download"
        self.store.event("contact_sheet", f"生成联系印样 · {len(options['asset_ids'])} 张素材", {
            "export_id": export_id,
            "width": meta["width"],
            "height": meta["height"],
        })
        return record
