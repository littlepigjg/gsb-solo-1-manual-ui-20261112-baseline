"""JSON repository with serialized read/modify/write and atomic replacement.

The local server owns one process. All HTTP and worker threads share this
repository.

Durability rules:

* Every mutation is applied to a deep copy of the last durable state. The
  in-memory cache is swapped only after the new state has been written and
  fsynced to disk, so a failed save never leaves unsaved edits in the cache.
  A later successful save therefore cannot smuggle a failed edit to disk.
* A valid backup (``.json.bak``) is retained before each replacement.
* When the primary file is corrupt or unreadable, the last valid backup is
  restored on the next read. If no valid backup exists, the failure is raised
  explicitly instead of serving an empty document or stale cache data.
"""
import copy
import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from .errors import MissingRecord, Conflict


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id():
    return uuid.uuid4().hex


class JsonStore:
    COLLECTIONS = {"assets", "jobs", "presets", "events", "exports"}

    def __init__(self, directory):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._decoded = {}
        for collection in self.COLLECTIONS:
            with self.lock:
                if not self._path(collection).exists():
                    self._commit(collection, {"schema_version": 1, "records": {}})
                else:
                    self._read(collection)

    def _path(self, collection):
        if collection not in self.COLLECTIONS:
            raise ValueError("Unknown collection")
        return self.directory / f"{collection}.json"

    def _decode(self, path):
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if data.get("schema_version") != 1 or not isinstance(data.get("records"), dict):
            raise ValueError("Unsupported JSON schema")
        return data

    def _recover(self, collection, path):
        """Restore a corrupt primary file from its backup.

        Returns the decoded backup data. Raises ``RuntimeError`` when no valid
        backup is available so callers never mistake an empty store for a
        healthy one.
        """
        backup = path.with_suffix(".json.bak")
        if not backup.exists():
            raise RuntimeError(f"JSON 数据损坏且没有可用备份：{path}")
        try:
            data = self._decode(backup)
        except (ValueError, OSError) as error:
            raise RuntimeError(
                f"JSON 数据损坏，且备份同样无法读取：{path}"
            ) from error
        recovery = path.with_suffix(".json.recovery")
        shutil.copy2(backup, recovery)
        os.replace(recovery, path)
        self._decoded[collection] = data
        return data

    def _read(self, collection):
        cache = self._decoded
        if collection in cache:
            return cache[collection]
        path = self._path(collection)
        try:
            data = self._decode(path)
        except (ValueError, OSError):
            data = self._recover(collection, path)
        cache[collection] = data
        return data

    def _commit(self, collection, new_data):
        """Durably persist ``new_data`` and only then swap the cache.

        The caller must build ``new_data`` from a deep copy of the previously
        durable state. On any disk error the existing cache stays untouched,
        so failed edits never become visible through later reads.
        """
        path = self._path(collection)
        temporary = path.with_suffix(f".{new_id()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(new_data, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if path.exists():
                shutil.copy2(path, path.with_suffix(".json.bak"))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        self._decoded[collection] = new_data
        return new_data

    def all(self, collection):
        with self.lock:
            data = self._read(collection)
            return copy.deepcopy(list(data["records"].values()))

    def get(self, collection, record_id):
        with self.lock:
            data = self._read(collection)
            record = data["records"].get(record_id)
            if record is None:
                raise MissingRecord(collection, record_id)
            return copy.deepcopy(record)

    def insert(self, collection, record):
        with self.lock:
            data = self._read(collection)
            record = copy.deepcopy(record)
            record.setdefault("id", new_id())
            record.setdefault("created_at", utc_now())
            record["updated_at"] = utc_now()
            if record["id"] in data["records"]:
                raise Conflict("记录已存在")
            new_data = copy.deepcopy(data)
            new_data["records"][record["id"]] = record
            self._commit(collection, new_data)
            return copy.deepcopy(record)

    def update(self, collection, record_id, changes):
        with self.lock:
            data = self._read(collection)
            if record_id not in data["records"]:
                raise MissingRecord(collection, record_id)
            changes = copy.deepcopy(changes)
            changes.pop("id", None)
            changes.pop("created_at", None)
            new_data = copy.deepcopy(data)
            record = new_data["records"][record_id]
            record.update(changes)
            record["updated_at"] = utc_now()
            self._commit(collection, new_data)
            return copy.deepcopy(record)

    def delete(self, collection, record_id):
        with self.lock:
            data = self._read(collection)
            if record_id not in data["records"]:
                raise MissingRecord(collection, record_id)
            new_data = copy.deepcopy(data)
            removed = new_data["records"].pop(record_id)
            self._commit(collection, new_data)
            return copy.deepcopy(removed)

    def prune(self, collection, maximum):
        with self.lock:
            data = self._read(collection)
            records = sorted(
                data["records"].values(),
                key=lambda item: item["created_at"],
                reverse=True,
            )
            if len(records) > maximum:
                new_data = copy.deepcopy(data)
                new_data["records"] = {item["id"]: item for item in records[:maximum]}
                self._commit(collection, new_data)

    def event(self, action, message, details=None):
        self.insert("events", {
            "action": action,
            "message": message,
            "details": details or {},
        })
        self.prune("events", 500)

    def snapshot(self):
        with self.lock:
            return {
                "schema_version": 1,
                "created_at": utc_now(),
                "collections": {
                    name: self._read(name)
                    for name in sorted(self.COLLECTIONS)
                },
            }
