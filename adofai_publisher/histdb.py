from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import records_path


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class HistoryDB:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else records_path()
        self._records: list[dict[str, Any]] = []
        self.load()

    def load(self) -> list[dict[str, Any]]:
        self._records = []
        if not self.path.is_file():
            return self._records
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._records
        if isinstance(raw, dict):
            raw = raw.get("records") or raw.get("items") or []
        if isinstance(raw, dict):
            raw = [raw]
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    self._records.append(self._normalize(item))
        return self._records

    def _normalize(self, item: dict[str, Any]) -> dict[str, Any]:
        record = dict(item)
        record.setdefault("id", uuid.uuid4().hex[:12])
        record.setdefault("created_at", record.get("updated_at") or _now())
        record.setdefault("updated_at", record.get("created_at"))
        record.setdefault("status", "draft")
        return record

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        payload = {"version": 2, "records": self._records}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def all(self) -> list[dict[str, Any]]:
        return sorted(self._records, key=lambda item: item.get("created_at", ""), reverse=True)

    def get(self, record_id: str) -> dict[str, Any] | None:
        for record in self._records:
            if record.get("id") == record_id:
                return record
        return None

    def add(self, data: dict[str, Any]) -> dict[str, Any]:
        record = self._normalize(dict(data))
        record["id"] = record.get("id") or uuid.uuid4().hex[:12]
        record["created_at"] = _now()
        record["updated_at"] = record["created_at"]
        self._records.append(record)
        self.save()
        return record

    def update(self, record_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
        record = self.get(record_id)
        if record is None:
            return None
        record.update(values)
        record["updated_at"] = _now()
        self.save()
        return record

    def upsert(self, data: dict[str, Any]) -> dict[str, Any]:
        record_id = data.get("id")
        if record_id and self.get(record_id):
            updated = self.update(record_id, data)
            return updated or data
        return self.add(data)

    def delete(self, record_id: str) -> bool:
        before = len(self._records)
        self._records = [item for item in self._records if item.get("id") != record_id]
        changed = len(self._records) != before
        if changed:
            self.save()
        return changed

    def clear(self) -> None:
        self._records = []
        self.save()
