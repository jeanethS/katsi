from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from mnemo_core.models import FileRecord

logger = logging.getLogger(__name__)


class FileRecordStore:
    """JSON-on-disk store for FileRecords, keyed by file id.

    Plain dict {id: FileRecord.model_dump(mode='json')} serialized to one file.
    Suitable for v0.1 — not for huge trees. Test-friendly: tmp_path works."""

    def __init__(self, data_dir: Path) -> None:
        """data_dir is the directory; the file is data_dir / 'file_records.json'."""
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._data_dir / "file_records.json"
        self._cache: dict[str, dict] | None = None

    def _load(self) -> dict[str, dict]:
        if self._cache is not None:
            return self._cache
        if not self._path.exists():
            self._cache = {}
            return self._cache
        try:
            with open(self._path, encoding="utf-8") as f:
                self._cache = json.load(f)
            if not isinstance(self._cache, dict):
                self._cache = {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("FileRecordStore: corrupt file %s: %r", self._path, e)
            self._cache = {}
        return self._cache

    def _flush(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._cache, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self._path)

    def get(self, file_id: str) -> FileRecord | None:
        rec = self._load().get(file_id)
        if rec is None:
            return None
        try:
            return FileRecord(**rec)
        except Exception as e:
            logger.warning("FileRecordStore.get: bad record %s: %r", file_id, e)
            return None

    def put(self, record: FileRecord) -> None:
        data = self._load()
        data[record.id] = record.model_dump(mode="json")
        self._flush()

    def delete(self, file_id: str) -> None:
        data = self._load()
        if file_id in data:
            data.pop(file_id)
            self._flush()

    def list_all(self) -> list[FileRecord]:
        return [FileRecord(**v) for v in self._load().values()]

    def count_by_status(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in self._load().values():
            st = v.get("status", "pending")
            out[st] = out.get(st, 0) + 1
        return out
