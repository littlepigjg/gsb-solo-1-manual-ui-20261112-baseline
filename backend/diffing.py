"""Alpha-aware visual diffing between two aligned studio assets.

The comparison models what is actually displayed: RGB is premultiplied by
alpha before measuring distance, so a fully transparent pixel never reports
a fake colour difference. Placement always produces a union canvas, which
keeps coverage changes (areas present in only one image) inside the same
mask and statistics used by every visualization.
"""
import io
import json
import os
import zipfile
import numpy as np
from PIL import Image, ImageDraw
from .errors import StudioError
from .storage import new_id
from .validation import object_value, reject_unknown, number_value, choice_value, identifier


ALIGNMENTS = ("center", "first", "second")
VIEWS = ("heatmap", "overlay", "mask")
MAX_RECORDS = 40
ONLY_FIRST_COLOR = (255, 122, 69)
ONLY_SECOND_COLOR = (74, 153, 226)
BBOX_COLOR = (255, 232, 92)
HEATMAP_BACKGROUND = (24, 32, 28)
HEATMAP_STOPS = (
    (0.0, (38, 70, 170)),
    (0.4, (52, 180, 190)),
    (0.7, (236, 210, 90)),
    (1.0, (224, 82, 60)),
)
OVERLAY_TINT = (201, 76, 56)


class DiffService:
    def __init__(self, settings, store, assets):
        self.settings = settings
        self.store = store
        self.assets = assets

    def validate(self, options):
        object_value(options, "对比参数")
        reject_unknown(options, {"first_id", "second_id", "alignment", "offset_x", "offset_y", "sensitivity"}, "对比参数")
        return {
            "first_id": identifier(options.get("first_id"), "第一张图片 ID"),
            "second_id": identifier(options.get("second_id"), "第二张图片 ID"),
            "alignment": choice_value(options.get("alignment", "center"), ALIGNMENTS, "对齐方式"),
            "offset_x": number_value(options.get("offset_x", 0), "水平偏移", -8192, 8192, integer=True),
            "offset_y": number_value(options.get("offset_y", 0), "垂直偏移", -8192, 8192, integer=True),
            "sensitivity": number_value(options.get("sensitivity", 80), "敏感程度", 1, 100, integer=True),
        }

    def create(self, options):
        options = self.validate(options)
        first_image, first_record = self.assets.load(options["first_id"])
        second_image, second_record = self.assets.load(options["second_id"])
        result = self._compute(first_image, second_image, options)
        diff_id = self._persist(result, options, first_record, second_record)
        self._prune()
        self.store.event("diff", "图像对比", {"diff_id": diff_id, **options})
        return self.public(diff_id)

    def _compute(self, first_image, second_image, options):
        natural = self._placement(first_image.size, second_image.size, options, scale=1.0)
        union_area = natural["width"] * natural["height"]
        work_pixels = self.settings.max_pixels
        scale = min(1.0, float(np.sqrt(work_pixels / max(1, union_area))))
        layout = self._placement(first_image.size, second_image.size, options, scale)
        width, height = layout["width"], layout["height"]

        def resized(image):
            if scale >= 1.0:
                return np.asarray(image, dtype=np.uint8)
            return np.asarray(image.resize((
                max(1, round(image.width * scale)),
                max(1, round(image.height * scale)),
            ), Image.Resampling.LANCZOS), dtype=np.uint8)

        first_rgba = resized(first_image)
        second_rgba = resized(second_image)
        canvas_one = np.zeros((height, width, 4), dtype=np.uint8)
        canvas_two = np.zeros((height, width, 4), dtype=np.uint8)
        present_one = np.zeros((height, width), dtype=bool)
        present_two = np.zeros((height, width), dtype=bool)
        self._paste(canvas_one, present_one, layout["origin_one"], first_rgba)
        self._paste(canvas_two, present_two, layout["origin_two"], second_rgba)

        premult_one = self._premultiplied(canvas_one)
        premult_two = self._premultiplied(canvas_two)
        distance = np.max(
            np.abs(premult_one.astype(np.int16) - premult_two.astype(np.int16)),
            axis=2,
        ).astype(np.uint8)

        only_first = present_one & ~present_two
        only_second = present_two & ~present_one
        overlap = present_one & present_two
        # Premultiplication makes two fully transparent pixels compare equal
        # regardless of the hidden RGB stored under alpha 0.
        threshold = round(255 * (100 - options["sensitivity"]) / 100)
        changed = overlap & (distance > threshold)
        unchanged = overlap & ~changed
        different = changed | only_first | only_second
        bbox = self._bbox(different)

        stats = self._stats({
            "width": width, "height": height, "overlap": overlap,
            "changed": changed, "unchanged": unchanged,
            "only_first": only_first, "only_second": only_second,
            "distance": distance,
        }, options, threshold, scale, bbox, first_image, second_image)

        heatmap = self._heatmap(changed, only_first, only_second, distance, bbox)
        overlay = self._overlay(
            canvas_one, canvas_two, present_one, present_two,
            changed, only_first, only_second, bbox,
        )
        mask = self._mask_image(changed, only_first, only_second)
        return {
            "stats": stats,
            "bbox": bbox,
            "images": {"heatmap": heatmap, "overlay": overlay, "mask": mask},
        }

    def _placement(self, size_one, size_two, options, scale):
        width_one = max(1, round(size_one[0] * scale))
        height_one = max(1, round(size_one[1] * scale))
        width_two = max(1, round(size_two[0] * scale))
        height_two = max(1, round(size_two[1] * scale))
        offset_x = round(options["offset_x"] * scale)
        offset_y = round(options["offset_y"] * scale)
        alignment = options["alignment"]
        if alignment == "first":
            origin_one = (0, 0)
            origin_two = (offset_x, offset_y)
        elif alignment == "second":
            origin_one = (offset_x, offset_y)
            origin_two = (0, 0)
        else:
            origin_one = (0, 0)
            origin_two = (
                round((width_one - width_two) / 2) + offset_x,
                round((height_one - height_two) / 2) + offset_y,
            )
        x_min = min(origin_one[0], origin_two[0])
        y_min = min(origin_one[1], origin_two[1])
        x_max = max(origin_one[0] + width_one, origin_two[0] + width_two)
        y_max = max(origin_one[1] + height_one, origin_two[1] + height_two)
        return {
            "width": x_max - x_min,
            "height": y_max - y_min,
            "origin_one": (origin_one[0] - x_min, origin_one[1] - y_min),
            "origin_two": (origin_two[0] - x_min, origin_two[1] - y_min),
        }

    @staticmethod
    def _paste(canvas, present, origin, rgba):
        x, y = origin
        height, width = rgba.shape[:2]
        canvas[y:y + height, x:x + width, :] = rgba
        present[y:y + height, x:x + width] = True

    @staticmethod
    def _premultiplied(rgba):
        alpha = rgba[..., 3:4].astype(np.uint16)
        rgb = rgba[..., :3].astype(np.uint16)
        blended = ((rgb * alpha) // 255).astype(np.uint8)
        return np.concatenate([blended, rgba[..., 3:4]], axis=2)

    @staticmethod
    def _flatten(rgba):
        alpha = rgba[..., 3:4].astype(np.uint16)
        rgb = rgba[..., :3].astype(np.uint16)
        premult = (rgb * alpha) // 255
        flat = premult + (255 * (255 - alpha)) // 255
        return flat.astype(np.uint8)

    @staticmethod
    def _bbox(mask):
        if not mask.any():
            return None
        rows = np.where(mask.any(axis=1))[0]
        cols = np.where(mask.any(axis=0))[0]
        return {
            "x": int(cols[0]), "y": int(rows[0]),
            "width": int(cols[-1] - cols[0] + 1),
            "height": int(rows[-1] - rows[0] + 1),
        }

    def _stats(self, arrays, options, threshold, scale, bbox, first_image, second_image):
        width, height = arrays["width"], arrays["height"]
        union_total = width * height
        overlap_total = int(arrays["overlap"].sum())
        changed_count = int(arrays["changed"].sum())
        only_first_count = int(arrays["only_first"].sum())
        only_second_count = int(arrays["only_second"].sum())
        total_changed = changed_count + only_first_count + only_second_count
        overlap_distance = arrays["distance"][arrays["overlap"]]
        changed_distance = arrays["distance"][arrays["changed"]]
        return {
            "canvas_size": [width, height],
            "work_scale": round(scale, 4),
            "full_resolution": scale >= 1.0,
            "threshold": threshold,
            "alignment": options["alignment"],
            "offset": [options["offset_x"], options["offset_y"]],
            "sensitivity": options["sensitivity"],
            "union_pixels": union_total,
            "overlap_pixels": overlap_total,
            "unchanged_pixels": int(arrays["unchanged"].sum()),
            "changed_pixels": changed_count,
            "only_first_pixels": only_first_count,
            "only_second_pixels": only_second_count,
            "total_changed_pixels": total_changed,
            "changed_percent_union": round(changed_count / union_total * 100, 3),
            "changed_percent_overlap": round(changed_count / overlap_total * 100, 3) if overlap_total else 0.0,
            "coverage_percent": round((only_first_count + only_second_count) / union_total * 100, 3),
            "total_change_percent": round(total_changed / union_total * 100, 3),
            "max_distance": int(overlap_distance.max()) if overlap_total else 0,
            "mean_distance_overlap": round(float(overlap_distance.mean()), 3) if overlap_total else 0.0,
            "mean_distance_changed": round(float(changed_distance.mean()), 3) if changed_count else 0.0,
            "identical": total_changed == 0,
            "bbox": bbox,
            "first_size": list(first_image.size),
            "second_size": list(second_image.size),
        }

    def _heatmap(self, changed, only_first, only_second, distance, bbox):
        height, width = distance.shape
        image = np.zeros((height, width, 4), dtype=np.uint8)
        image[..., :3] = HEATMAP_BACKGROUND
        image[..., 3] = 255
        image[only_first, :3] = ONLY_FIRST_COLOR
        image[only_second, :3] = ONLY_SECOND_COLOR
        if changed.any():
            image[changed, :3] = self._ramp(distance[changed].astype(np.float32) / 255.0)
        return self._with_bbox(Image.fromarray(image), bbox)

    def _overlay(self, canvas_one, canvas_two, present_one, present_two,
                 changed, only_first, only_second, bbox):
        flat_one = self._flatten(canvas_one)
        flat_two = self._flatten(canvas_two)
        base = np.full(canvas_one.shape[:2] + (3,), 255, dtype=np.uint8)
        base[present_one] = flat_one[present_one]
        base[present_two & ~present_one] = flat_two[present_two & ~present_one]
        base[changed] = self._tint(base[changed], OVERLAY_TINT, 0.45)
        base[only_first] = self._tint(base[only_first], ONLY_FIRST_COLOR, 0.55)
        base[only_second] = self._tint(base[only_second], ONLY_SECOND_COLOR, 0.55)
        rgba = np.dstack([base, np.full(base.shape[:2], 255, dtype=np.uint8)])
        return self._with_bbox(Image.fromarray(rgba), bbox)

    def _mask_image(self, changed, only_first, only_second):
        height, width = changed.shape
        mask = np.zeros((height, width, 4), dtype=np.uint8)
        mask[..., 0] = changed.astype(np.uint8) * 255
        mask[..., 1] = only_first.astype(np.uint8) * 255
        mask[..., 2] = only_second.astype(np.uint8) * 255
        mask[..., 3] = 255
        return Image.fromarray(mask)

    @staticmethod
    def _tint(pixels, color, strength):
        if not len(pixels):
            return pixels
        color_array = np.asarray(color, dtype=np.float32)
        blended = pixels.astype(np.float32) * (1 - strength) + color_array * strength
        return np.clip(np.rint(blended), 0, 255).astype(np.uint8)

    @staticmethod
    def _ramp(values):
        output = np.zeros((len(values), 3), dtype=np.uint8)
        for index in range(len(HEATMAP_STOPS) - 1):
            start, start_color = HEATMAP_STOPS[index]
            end, end_color = HEATMAP_STOPS[index + 1]
            selected = (values >= start) & (values <= end)
            local = np.clip((values[selected] - start) / max(end - start, 1e-6), 0, 1)
            mixed = np.asarray(start_color) * (1 - local[:, None]) + np.asarray(end_color) * local[:, None]
            output[selected] = np.clip(np.rint(mixed), 0, 255).astype(np.uint8)
        return output

    @staticmethod
    def _with_bbox(image, bbox):
        if bbox:
            draw = ImageDraw.Draw(image)
            draw.rectangle(
                (bbox["x"], bbox["y"], bbox["x"] + bbox["width"] - 1, bbox["y"] + bbox["height"] - 1),
                outline=BBOX_COLOR, width=2,
            )
        return image

    def _persist(self, result, options, first_record, second_record):
        diff_id = new_id()
        files = {view: f"{diff_id}.{view}.png" for view in VIEWS}
        payloads = {}
        for view, image in result["images"].items():
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            payloads[view] = buffer.getvalue()
        manifest = {
            "id": diff_id,
            "options": options,
            "stats": result["stats"],
            "bbox": result["bbox"],
            "first": {"id": first_record["id"], "name": first_record["name"], "size": [first_record["width"], first_record["height"]]},
            "second": {"id": second_record["id"], "name": second_record["name"], "size": [second_record["width"], second_record["height"]]},
        }
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for view in VIEWS:
                archive.writestr(f"diff_{view}.png", payloads[view])
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zip_payload = zip_buffer.getvalue()

        written = []
        try:
            for view, filename in files.items():
                path = self.settings.exports / filename
                temporary = path.with_suffix(".tmp")
                temporary.write_bytes(payloads[view])
                os.replace(temporary, path)
                written.append(path)
            zip_path = self.settings.exports / f"{diff_id}.zip"
            zip_temporary = zip_path.with_suffix(".tmp")
            zip_temporary.write_bytes(zip_payload)
            os.replace(zip_temporary, zip_path)
            written.append(zip_path)
            self.store.insert("exports", {
                "id": diff_id,
                "filename": f"{diff_id}.zip",
                "download_name": self._download_name(first_record["name"], second_record["name"]),
                "bytes": len(zip_payload),
                "asset_ids": [options["first_id"], options["second_id"]],
                "options": options,
                "kind": "diff",
                "images": files,
                "stats": result["stats"],
                "bbox": result["bbox"],
            })
        except Exception:
            for path in written:
                path.unlink(missing_ok=True)
            raise
        return diff_id

    def _prune(self):
        records = [item for item in self.store.all("exports") if item.get("kind") == "diff"]
        records.sort(key=lambda item: item["created_at"], reverse=True)
        for stale in records[MAX_RECORDS:]:
            self.store.delete("exports", stale["id"])
            (self.settings.exports / stale["filename"]).unlink(missing_ok=True)
            for filename in stale.get("images", {}).values():
                (self.settings.exports / filename).unlink(missing_ok=True)

    @staticmethod
    def _download_name(first_name, second_name):
        def stem(name):
            return name.rsplit(".", 1)[0][:60] or "图片"
        return f"图像对比_{stem(first_name)}_vs_{stem(second_name)}.zip"

    def file(self, diff_id, view=None):
        identifier(diff_id)
        record = self.store.get("exports", diff_id)
        if record.get("kind") != "diff":
            raise StudioError("对比结果不存在", code="not_found", status=404)
        if view is None:
            path = self.settings.exports / record["filename"]
            if not path.exists():
                raise StudioError("对比文件丢失，请重新生成", status=410)
            return path, record, "application/zip"
        if view not in VIEWS:
            raise StudioError("不支持的对比视图", status=404)
        path = self.settings.exports / record["images"][view]
        if not path.exists():
            raise StudioError("对比图片丢失，请重新生成", status=410)
        return path, record, "image/png"

    def public(self, diff_id):
        record = self.store.get("exports", diff_id)
        return {
            "id": diff_id,
            "stats": record["stats"],
            "bbox": record["bbox"],
            "options": record["options"],
            "download_name": record["download_name"],
            "bytes": record["bytes"],
            "urls": {view: f"/api/diffs/{diff_id}/image/{view}" for view in VIEWS},
            "download_url": f"/api/diffs/{diff_id}/download",
        }
