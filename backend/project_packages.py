"""Portable project packages: complete asset history, integrity checks, all-or-nothing import.

A package is a ZIP archive with a JSON manifest and one normalized PNG per asset.
Exporting a selected asset always includes its whole upstream history so the
imported copy can continue editing with the original provenance. Import validates
every record and image first, stages the files, and only then commits the records
in one JSON write. A damaged package therefore never leaves a partial library.
"""
import hashlib
import io
import json
import os
import re
import tempfile
import threading
import warnings
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from PIL import Image, UnidentifiedImageError
from .errors import MissingRecord, StudioError
from .storage import new_id, utc_now
from .validation import (
    IDENTIFIER,
    boolean_value,
    choice_value,
    dimension_guard,
    identifier,
    list_value,
    number_value,
    text_value,
)

PACKAGE_FORMAT = "guangyu.image-studio.project"
SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
APP_INFO = {"name": "光屿图像工作室", "version": "1.0.0"}
ASSET_KINDS = ["original", "result", "sample"]
SHA256 = re.compile(r"^[a-f0-9]{64}$")
RECORD_FIELDS = {
    "id", "name", "kind", "width", "height", "mode", "format", "bytes",
    "sha256", "has_alpha", "parent_id", "root_id", "steps", "favorite",
    "tags", "metadata", "created_at", "updated_at",
}
ENTRY_FIELDS = {"record", "file", "sha256", "bytes"}
MANIFEST_FIELDS = {
    "package_format", "schema_version", "created_at", "app", "selection",
    "root_ids", "asset_count", "assets",
}


def _invalid(reason, **details):
    return StudioError(
        f"项目包校验失败，已整包拒绝：{reason}",
        code="invalid_project_package",
        status=400,
        details=details,
    )


class ProjectPackageService:
    def __init__(self, settings, store, assets, registry):
        self.settings = settings
        self.store = store
        self.assets = assets
        self.registry = registry
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- export
    def export(self, asset_ids):
        selection = list_value(asset_ids, "项目包素材", maximum=self.settings.max_batch)
        selection = list(dict.fromkeys(selection))
        for asset_id in selection:
            identifier(asset_id)
        records = self._history_closure(selection)
        if len(records) > self.settings.max_package_assets:
            raise StudioError(
                f"项目包包含的素材版本超过 {self.settings.max_package_assets} 个上限",
                code="package_too_large",
                status=400,
            )

        export_id = new_id()
        filename = f"{export_id}.zip"
        path = self.settings.exports / filename
        temporary = path.with_suffix(".tmp")
        try:
            with self.assets.lock:
                entries = []
                with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                    for record in records:
                        source = self.settings.media / (record["id"] + ".png")
                        if not source.is_file():
                            raise StudioError(
                                f"项目包导出失败：素材“{record['name']}”的图片文件缺失",
                                code="missing_media",
                                status=410,
                            )
                        if source.stat().st_size > self.settings.max_package_member_bytes:
                            raise StudioError(
                                f"项目包导出失败：素材“{record['name']}”的图片文件过大",
                                code="package_too_large",
                                status=400,
                            )
                        payload = source.read_bytes()
                        self._verify_image(payload, record, "项目包导出失败")
                        entry = {
                            "record": self._export_record(record),
                            "file": f"assets/{record['id']}.png",
                            "sha256": record["sha256"],
                            "bytes": record["bytes"],
                        }
                        archive.writestr(entry["file"], payload)
                        entries.append(entry)

                    created_at = utc_now()
                    roots = sorted({record["root_id"] for record in records})
                    manifest = {
                        "package_format": PACKAGE_FORMAT,
                        "schema_version": SCHEMA_VERSION,
                        "created_at": created_at,
                        "app": APP_INFO,
                        "selection": selection,
                        "root_ids": roots,
                        "asset_count": len(entries),
                        "assets": entries,
                    }
                    archive.writestr(
                        MANIFEST_NAME,
                        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"),
                    )
                os.replace(temporary, path)
                download_name = "光屿_项目包_" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + ".zip"
                record = self.store.insert("exports", {
                    "id": export_id,
                    "filename": filename,
                    "download_name": download_name,
                    "bytes": path.stat().st_size,
                    "asset_ids": [item["record"]["id"] for item in entries],
                    "options": {
                        "package_format": PACKAGE_FORMAT,
                        "schema_version": SCHEMA_VERSION,
                        "selection": selection,
                        "asset_count": len(entries),
                    },
                    "kind": "project",
                })
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)

        self.store.event("export_project_package", f"导出项目包 {download_name}", {
            "export_id": export_id,
            "selection": selection,
            "included": len(entries),
        })
        record["url"] = f"/api/exports/{export_id}/download"
        record["included_count"] = len(entries)
        record["selection_count"] = len(selection)
        record["root_ids"] = roots
        return record

    def _history_closure(self, selection):
        """Return selected assets plus every ancestor, parents before children."""
        ordered = []
        seen = set()
        for asset_id in selection:
            chain = []
            visited = set()
            cursor = asset_id
            while cursor:
                if cursor in visited:
                    raise StudioError(
                        "项目包导出失败：素材历史存在循环引用",
                        code="broken_history",
                        status=409,
                    )
                visited.add(cursor)
                try:
                    record = self.store.get("assets", cursor)
                except MissingRecord as error:
                    raise StudioError(
                        f"项目包导出失败：找不到上游素材 {cursor}，无法保留完整历史",
                        code="broken_history",
                        status=410,
                    ) from error
                chain.append(record)
                cursor = record.get("parent_id")
            for record in reversed(chain):
                if record["id"] not in seen:
                    seen.add(record["id"])
                    ordered.append(record)
        return ordered

    @staticmethod
    def _export_record(record):
        result = {field: record.get(field) for field in RECORD_FIELDS}
        result["metadata"] = dict(record.get("metadata") or {})
        return result

    # ---------------------------------------------------------------- import
    def import_package(self, stream, filename=""):
        with self.lock:
            staging = Path(tempfile.mkdtemp(prefix="project-import-", dir=str(self.settings.root)))
            try:
                prepared = self._validate_archive(stream, staging)
                return self._commit(prepared, staging, filename)
            finally:
                for path in staging.glob("*") if staging.exists() else ():
                    path.unlink(missing_ok=True)
                if staging.exists():
                    staging.rmdir()

    def _validate_archive(self, stream, staging):
        try:
            archive = zipfile.ZipFile(stream)
        except (zipfile.BadZipFile, OSError, ValueError) as error:
            raise _invalid("文件不是有效的 ZIP 压缩包", detail=str(error)) from error

        with archive:
            names = archive.namelist()
            if len(names) > self.settings.max_package_assets * 2 + 8:
                raise _invalid("压缩包内的文件数量超过上限")
            if len(names) != len(set(names)):
                raise _invalid("压缩包内存在重复的文件名")
            for name in names:
                self._guard_member_name(name)
            total_size = sum(info.file_size for info in archive.infolist())
            if total_size > self.settings.max_package_bytes:
                raise _invalid("压缩包解压后的总体积超过上限")
            if MANIFEST_NAME not in names:
                raise _invalid("缺少 manifest.json 项目清单")
            manifest_info = archive.getinfo(MANIFEST_NAME)
            if manifest_info.file_size > 4 * 1024 * 1024:
                raise _invalid("项目清单文件过大")
            if manifest_info.flag_bits & 0x1:
                raise _invalid("项目包包含加密文件，无法校验")
            manifest = self._read_manifest(archive)
            selection = self._validate_manifest(manifest)

            records = {}
            entries = []
            for entry in manifest["assets"]:
                if not isinstance(entry, dict):
                    raise _invalid("项目清单中的素材条目必须是 JSON 对象")
                unknown = sorted(set(entry) - ENTRY_FIELDS)
                if unknown:
                    raise _invalid("素材条目包含未知字段", fields=unknown)
                record = self._validate_record(entry.get("record"))
                if record["id"] in records:
                    raise _invalid("项目包中存在重复的素材标识", asset_id=record["id"])
                expected_file = f"assets/{record['id']}.png"
                if entry.get("file") != expected_file:
                    raise _invalid(
                        "素材图片路径与记录标识不匹配",
                        asset_id=record["id"],
                        file=entry.get("file"),
                    )
                if entry.get("sha256") != record["sha256"] or entry.get("bytes") != record["bytes"]:
                    raise _invalid("素材清单中的校验信息不一致", asset_id=record["id"])
                records[record["id"]] = record
                entries.append((record, expected_file))

            self._validate_history(records, selection, manifest)

            for record, file_name in entries:
                if file_name not in names:
                    raise _invalid("项目包缺少素材图片", asset_id=record["id"], file=file_name)
                info = archive.getinfo(file_name)
                if info.is_dir():
                    raise _invalid("素材图片路径不是文件", file=file_name)
                if info.flag_bits & 0x1:
                    raise _invalid("项目包包含加密文件，无法校验", file=file_name)
                if info.file_size > self.settings.max_package_member_bytes:
                    raise _invalid("素材图片文件过大", asset_id=record["id"], file=file_name)
                try:
                    payload = archive.read(info)
                except (zipfile.BadZipFile, OSError, RuntimeError, EOFError) as error:
                    raise _invalid(
                        "素材图片数据损坏，CRC 校验失败",
                        asset_id=record["id"],
                        file=file_name,
                        detail=str(error),
                    ) from error
                if len(payload) != info.file_size:
                    raise _invalid("素材图片数据不完整", asset_id=record["id"], file=file_name)
                image = self._verify_image(payload, record, "项目包校验失败")
                (staging / (record["id"] + ".png")).write_bytes(payload)
                thumbnail = image.copy()
                thumbnail.thumbnail((320, 240), Image.Resampling.LANCZOS)
                thumbnail.save(staging / (record["id"] + ".thumb.png"), format="PNG")
                image.close()
                thumbnail.close()

            return {
                "selection": selection,
                "records": self._history_order(records, selection),
                "manifest": manifest,
            }

    def _read_manifest(self, archive):
        try:
            payload = archive.read(MANIFEST_NAME)
        except (zipfile.BadZipFile, OSError, RuntimeError, EOFError) as error:
            raise _invalid("项目清单数据损坏，CRC 校验失败", detail=str(error)) from error
        try:
            manifest = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise _invalid("项目清单不是有效的 UTF-8 JSON", detail=str(error)) from error
        return manifest

    def _validate_manifest(self, manifest):
        if not isinstance(manifest, dict):
            raise _invalid("项目清单必须是 JSON 对象")
        unknown = sorted(set(manifest) - MANIFEST_FIELDS)
        if unknown:
            raise _invalid("项目清单包含未知字段", fields=unknown)
        if manifest.get("package_format") != PACKAGE_FORMAT:
            raise _invalid("不是受支持的项目包格式", package_format=manifest.get("package_format"))
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise _invalid("项目包版本不受支持", schema_version=manifest.get("schema_version"))
        try:
            created_at = text_value(manifest.get("created_at"), "创建时间", 80, 1)
        except StudioError as error:
            raise _invalid(error.message) from error
        if not isinstance(manifest.get("app"), dict):
            raise _invalid("项目包缺少应用信息")
        assets = manifest.get("assets")
        if not isinstance(assets, list) or not 1 <= len(assets) <= self.settings.max_package_assets:
            raise _invalid("项目包中的素材数量无效")
        if manifest.get("asset_count") != len(assets):
            raise _invalid("素材数量与清单不一致")
        try:
            selection = list_value(manifest.get("selection"), "所选素材", maximum=self.settings.max_batch)
        except StudioError as error:
            raise _invalid(error.message) from error
        selection = list(dict.fromkeys(selection))
        for asset_id in selection:
            if not isinstance(asset_id, str) or not IDENTIFIER.fullmatch(asset_id):
                raise _invalid("所选素材标识格式不正确", asset_id=asset_id)
        if not isinstance(manifest.get("root_ids"), list):
            raise _invalid("项目包缺少根素材清单")
        manifest["_created_at"] = created_at
        return selection

    def _validate_record(self, raw):
        def fail(reason, **details):
            raise _invalid(reason, **details)

        if not isinstance(raw, dict):
            fail("素材记录必须是 JSON 对象")
        unknown = sorted(set(raw) - RECORD_FIELDS)
        if unknown:
            fail("素材记录包含未知字段", fields=unknown)

        asset_id = raw.get("id")
        if not isinstance(asset_id, str) or not IDENTIFIER.fullmatch(asset_id):
            fail("素材标识格式不正确", asset_id=asset_id)
        try:
            name = text_value(raw.get("name"), "素材名称", 180, 1)
            width = number_value(raw.get("width"), "宽度", 1, self.settings.max_dimension, integer=True)
            height = number_value(raw.get("height"), "高度", 1, self.settings.max_dimension, integer=True)
            dimension_guard(width, height, self.settings)
            byte_count = number_value(
                raw.get("bytes"), "文件大小", 0, self.settings.max_package_member_bytes, integer=True
            )
            kind = choice_value(raw.get("kind"), ASSET_KINDS, "素材类型")
            has_alpha = boolean_value(raw.get("has_alpha"), "透明通道")
            favorite = boolean_value(raw.get("favorite"), "收藏状态")
            parent_id = raw.get("parent_id")
            if parent_id is not None:
                identifier(parent_id, "上游素材 ID")
            root_id = identifier(raw.get("root_id"), "根素材 ID")
            created_at = text_value(raw.get("created_at"), "创建时间", 80, 1)
            text_value(raw.get("updated_at"), "更新时间", 80, 1)
        except StudioError as error:
            raise _invalid(error.message, asset_id=raw.get("id")) from error

        if raw.get("mode") != "RGBA" or raw.get("format") != "PNG":
            fail("素材记录不是规范的 RGBA PNG", asset_id=asset_id)
        digest = raw.get("sha256")
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            fail("素材校验值格式不正确", asset_id=asset_id)
        metadata = raw.get("metadata")
        if not isinstance(metadata, dict):
            fail("素材处理信息必须是 JSON 对象", asset_id=asset_id)

        steps = raw.get("steps")
        if not isinstance(steps, list) or len(steps) > self.settings.max_steps:
            fail("素材处理记录必须是数组且不超过步骤上限", asset_id=asset_id)
        if steps:
            try:
                steps = self.registry.validate_pipeline(steps, maximum=self.settings.max_steps)
            except StudioError as error:
                raise _invalid(f"素材处理记录无法识别：{error.message}", asset_id=asset_id) from error

        tags = raw.get("tags")
        if not isinstance(tags, list) or len(tags) > 12:
            fail("素材标签数量无效", asset_id=asset_id)
        normalized_tags = []
        for tag in tags:
            try:
                normalized_tags.append(text_value(tag, "标签", 30, 1))
            except StudioError as error:
                raise _invalid(error.message, asset_id=asset_id) from error
        normalized_tags = list(dict.fromkeys(normalized_tags))

        return {
            "id": asset_id,
            "name": name,
            "kind": kind,
            "width": width,
            "height": height,
            "mode": "RGBA",
            "format": "PNG",
            "bytes": byte_count,
            "sha256": digest,
            "has_alpha": has_alpha,
            "parent_id": parent_id,
            "root_id": root_id,
            "steps": steps,
            "favorite": favorite,
            "tags": normalized_tags,
            "metadata": metadata,
            "created_at": created_at,
            "updated_at": raw.get("updated_at"),
        }

    def _validate_history(self, records, selection, manifest):
        for asset_id in selection:
            if asset_id not in records:
                raise _invalid("所选素材不在项目包中", asset_id=asset_id)

        needed = set()
        for asset_id in selection:
            cursor = records[asset_id]
            visited = set()
            while cursor:
                if cursor["id"] in visited:
                    raise _invalid("素材历史存在循环引用", asset_id=asset_id)
                visited.add(cursor["id"])
                needed.add(cursor["id"])
                parent_id = cursor["parent_id"]
                if parent_id is None:
                    break
                if parent_id not in records:
                    raise _invalid(
                        "项目包缺少所选素材的上游版本，无法沿原历史继续",
                        asset_id=asset_id,
                        missing_asset_id=parent_id,
                    )
                cursor = records[parent_id]
        if needed != set(records):
            extra = sorted(set(records) - needed)
            raise _invalid("项目包包含与所选素材历史无关的记录", asset_ids=extra)

        computed_roots = set()
        for record in records.values():
            visited = set()
            cursor = record
            while cursor["parent_id"] is not None:
                if cursor["id"] in visited:
                    raise _invalid("素材历史存在循环引用", asset_id=record["id"])
                visited.add(cursor["id"])
                cursor = records[cursor["parent_id"]]
            root = cursor
            if root["parent_id"] is not None or root["root_id"] != root["id"]:
                raise _invalid("素材根记录不一致", asset_id=root["id"])
            if record["root_id"] != root["id"]:
                raise _invalid(
                    "素材的根标识与历史不一致",
                    asset_id=record["id"],
                    root_id=record["root_id"],
                    expected=root["id"],
                )
            computed_roots.add(root["id"])
        declared_roots = manifest.get("root_ids")
        if sorted(declared_roots) != sorted(computed_roots):
            raise _invalid("项目包的根素材清单与历史不一致")

    @staticmethod
    def _history_order(records, selection):
        ordered = []
        seen = set()
        for asset_id in selection:
            chain = []
            cursor = records[asset_id]
            while cursor:
                chain.append(cursor)
                cursor = records[cursor["parent_id"]] if cursor["parent_id"] else None
            for record in reversed(chain):
                if record["id"] not in seen:
                    seen.add(record["id"])
                    ordered.append(record)
        return ordered

    def _verify_image(self, payload, record, action):
        def media_error(reason, **details):
            if action.startswith("项目包校验"):
                raise _invalid(reason, asset_id=record["id"], **details)
            raise StudioError(
                f"{action}：素材“{record['name']}”的{reason}",
                code="invalid_media",
                status=400,
                details=details,
            )

        if len(payload) != record["bytes"]:
            media_error("图片大小与记录不一致")
        digest = hashlib.sha256(payload).hexdigest()
        if digest != record["sha256"]:
            media_error("图片校验值不一致")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(payload)) as opened:
                    if opened.format != "PNG":
                        media_error("图片不是 PNG 格式")
                    if getattr(opened, "n_frames", 1) != 1:
                        media_error("图片包含多帧")
                    if opened.size != (record["width"], record["height"]):
                        media_error("图片尺寸与记录不一致")
                    dimension_guard(*opened.size, self.settings)
                    opened.load()
                    image = opened.convert("RGBA")
                    has_alpha = image.getchannel("A").getextrema()[0] < 255
        except StudioError:
            raise
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
                Image.DecompressionBombWarning) as error:
            media_error("图片无法解码", detail=str(error))
        if has_alpha != record["has_alpha"]:
            media_error("透明通道记录与图片不一致")
        return image

    def _commit(self, prepared, staging, filename):
        source_records = prepared["records"]
        selection = prepared["selection"]
        manifest = prepared["manifest"]

        with self.assets.lock:
            existing = {item["id"]: item for item in self.store.all("assets")}
            existing_names = {item["name"] for item in existing.values()}
            existing_files = {path.name for path in self.settings.media.iterdir() if path.is_file()}
            mapping = {}
            id_conflicts = 0
            name_conflicts = 0
            for record in source_records:
                desired = record["id"]
                if desired in existing or f"{desired}.png" in existing_files or f"{desired}.thumb.png" in existing_files:
                    id_conflicts += 1
                    desired = self._fresh_id(existing, existing_files)
                if record["name"] in existing_names:
                    name_conflicts += 1
                mapping[record["id"]] = desired
                existing.setdefault(desired, record)
                existing_files.update({f"{desired}.png", f"{desired}.thumb.png"})
                existing_names.add(record["name"])

            now = utc_now()
            new_records = []
            import_info = {
                "package_format": PACKAGE_FORMAT,
                "package_created_at": manifest["_created_at"],
                "imported_at": now,
                "filename": str(filename or ""),
            }
            for record in source_records:
                new_id = mapping[record["id"]]
                metadata = dict(record["metadata"])
                metadata["project_import"] = dict(import_info, source_asset_id=record["id"])
                new_records.append({
                    "id": new_id,
                    "name": record["name"],
                    "kind": record["kind"],
                    "width": record["width"],
                    "height": record["height"],
                    "mode": "RGBA",
                    "format": "PNG",
                    "bytes": record["bytes"],
                    "sha256": record["sha256"],
                    "has_alpha": record["has_alpha"],
                    "parent_id": mapping.get(record["parent_id"]) if record["parent_id"] else None,
                    "root_id": mapping[record["root_id"]],
                    "steps": record["steps"],
                    "favorite": record["favorite"],
                    "tags": record["tags"],
                    "metadata": metadata,
                    "created_at": record["created_at"],
                    "updated_at": now,
                })

            moved = []
            try:
                for record in source_records:
                    new_id = mapping[record["id"]]
                    for suffix in (".png", ".thumb.png"):
                        source = staging / (record["id"] + suffix)
                        if not source.is_file():
                            raise StudioError(
                                "项目包校验失败，已整包拒绝：暂存素材文件丢失",
                                code="invalid_project_package",
                                status=400,
                            )
                        destination = self.settings.media / (new_id + suffix)
                        if destination.exists():
                            raise StudioError(
                                "项目包导入失败：目标素材文件已存在，已停止导入",
                                code="conflict",
                                status=409,
                            )
                        os.replace(source, destination)
                        moved.append(destination)
                self.store.insert_many("assets", new_records)
            except Exception:
                for path in moved:
                    path.unlink(missing_ok=True)
                raise

        selected_ids = [mapping[asset_id] for asset_id in selection]
        imported_roots = sorted({record["root_id"] for record in new_records})
        self.store.event(
            "import_project_package",
            f"导入项目包 {filename}".strip() if filename else "导入项目包",
            {
                "imported": len(new_records),
                "selection": selection,
                "id_conflicts": id_conflicts,
            },
        )
        return {
            "imported": len(new_records),
            "asset_ids": [record["id"] for record in new_records],
            "root_ids": imported_roots,
            "selection_ids": selected_ids,
            "primary_id": selected_ids[-1] if selected_ids else None,
            "id_conflicts": id_conflicts,
            "name_conflicts": name_conflicts,
            "package_created_at": manifest["_created_at"],
            "records": [self.assets.public(record) for record in new_records],
        }

    @staticmethod
    def _fresh_id(existing, existing_files):
        while True:
            candidate = new_id()
            if candidate not in existing and f"{candidate}.png" not in existing_files:
                return candidate

    @staticmethod
    def _guard_member_name(name):
        if not isinstance(name, str) or not name or "\x00" in name:
            raise _invalid("压缩包包含无效文件名")
        if "\\" in name or name.startswith("/"):
            raise _invalid("压缩包包含不安全的绝对路径", file=name)
        path = PurePosixPath(name)
        if ".." in path.parts:
            raise _invalid("压缩包包含不安全的路径", file=name)
        if not path.parts or str(path) != name:
            raise _invalid("压缩包包含无效文件名", file=name)
