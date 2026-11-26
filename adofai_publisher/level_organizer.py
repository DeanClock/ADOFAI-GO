from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .level_library import LevelLibraryDB
from .level_paths import entry_band, entry_category, target_chart_folder
from .models import LevelInfo

STATUS_LABELS = {
    "move": "可移动",
    "same": "已就位",
    "conflict": "冲突",
    "skip": "跳过",
}

_LAYOUT_FOLDER_NAMES = {
    "p",
    "g",
    "u",
    "unranked",
    "censored",
    "impossible",
    "未评级",
    "未匹配",
    "-2and未评级",
    "2and未评级",
}
_BAND_FOLDER_RE = re.compile(r"^[pgu]\d{1,2}$", re.IGNORECASE)


@dataclass
class OrganizeItem:
    key: str
    song: str
    source_folder: str
    target_folder: str
    source_path: str
    target_path: str
    old_category: str
    old_band: str
    old_collection: str
    status: str
    message: str = ""
    new_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "song": self.song,
            "source_folder": self.source_folder,
            "target_folder": self.target_folder,
            "source_path": self.source_path,
            "target_path": self.target_path,
            "old_category": self.old_category,
            "old_band": self.old_band,
            "old_collection": self.old_collection,
            "status": self.status,
            "message": self.message,
            "new_key": self.new_key,
        }


@dataclass
class OrganizePlan:
    root: str
    items: list[OrganizeItem] = field(default_factory=list)

    @property
    def move_items(self) -> list[OrganizeItem]:
        return [item for item in self.items if item.status == "move"]

    def counts(self) -> dict[str, int]:
        result = {key: 0 for key in STATUS_LABELS}
        for item in self.items:
            result[item.status] = result.get(item.status, 0) + 1
        return result


@dataclass
class OrganizeResult:
    mode: str
    moved: int = 0
    failed: int = 0
    conflicts: int = 0
    skipped: int = 0
    manifest_path: str = ""
    backup_path: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "moved": self.moved,
            "failed": self.failed,
            "conflicts": self.conflicts,
            "skipped": self.skipped,
            "manifest_path": self.manifest_path,
            "backup_path": self.backup_path,
            "errors": list(self.errors),
        }


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _is_layout_folder(name: str) -> bool:
    text = (name or "").strip()
    return text.casefold() in _LAYOUT_FOLDER_NAMES or bool(
        _BAND_FOLDER_RE.match(text)
    )


def infer_organize_root(entries: list[dict[str, Any]]) -> Path | None:
    folders: list[str] = []
    for entry in entries:
        folder = str(entry.get("folder") or "").strip()
        if not folder:
            path = str(entry.get("path") or "").strip()
            folder = str(Path(path).parent) if path else ""
        if folder and Path(folder).is_dir():
            folders.append(str(Path(folder)))
    if len(folders) < 2:
        return None
    home = Path.home().resolve()
    counts: dict[Path, int] = {}
    for folder in folders:
        try:
            current = Path(folder).resolve()
        except OSError:
            continue
        while current != current.parent:
            if current == home:
                break
            if (
                current not in home.parents
                and not _is_layout_folder(current.name)
            ):
                counts[current] = counts.get(current, 0) + 1
            current = current.parent
    if not counts:
        return None
    highest = max(counts.values())
    if highest < 2:
        return None
    best = {path for path, count in counts.items() if count == highest}
    deepest = [
        path
        for path in best
        if not any(path != other and path in other.parents for other in best)
    ]
    if len(deepest) != 1:
        return None
    result = deepest[0]
    if result == home or result in home.parents:
        return None
    return result


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(path)


def build_organize_plan(db: LevelLibraryDB, root: Path | str) -> OrganizePlan:
    root_path = Path(root)
    items: list[OrganizeItem] = []
    for entry in db.all():
        key = str(entry.get("key") or "")
        category = entry_category(entry)
        band = entry_band(entry)
        collection = str(entry.get("collection") or "").strip()
        song = str(entry.get("song") or "").strip()
        path_text = str(entry.get("path") or "").strip()
        source_folder = Path(
            str(entry.get("folder") or (Path(path_text).parent if path_text else ""))
        )
        if not song:
            song = source_folder.name or "(未命名)"

        source_path = Path(path_text) if path_text else source_folder / "(未知谱面)"
        target_folder = source_folder
        target_path = source_path
        status = "move"
        message = ""

        if not key:
            status = "skip"
            message = "条目缺少内部编号"
        elif not path_text:
            status = "skip"
            message = "条目缺少主谱面路径"
        elif not str(source_folder):
            status = "skip"
            message = "条目缺少所在目录"
        elif source_folder.resolve() == root_path.resolve():
            status = "skip"
            message = "不能移动谱面根目录"
        elif _within(root_path, source_folder):
            status = "skip"
            message = "源目录包含谱面根目录，不能移动"
        elif not source_folder.is_dir():
            status = "skip"
            message = "源目录不存在"
        else:
            level = LevelInfo.from_dict(entry)
            if level is None:
                status = "skip"
                message = "无法读取谱面信息"
            else:
                level.song = song
                try:
                    target_folder = target_chart_folder(
                        root_path,
                        level,
                        category,
                        band,
                        collection,
                    )
                    try:
                        relative = source_path.resolve().relative_to(
                            source_folder.resolve()
                        )
                    except ValueError:
                        relative = Path(source_path.name)
                    target_path = target_folder / relative
                    if target_folder.resolve() == source_folder.resolve():
                        status = "same"
                        message = "已在目标目录"
                    elif target_folder.exists():
                        status = "conflict"
                        message = "目标目录已存在"
                except (OSError, ValueError) as exc:
                    status = "conflict"
                    message = str(exc)

        items.append(
            OrganizeItem(
                key=key,
                song=song,
                source_folder=str(source_folder) if str(source_folder) else "",
                target_folder=str(target_folder) if str(target_folder) else "",
                source_path=str(source_path) if path_text else "",
                target_path=str(target_path) if path_text else "",
                old_category=category,
                old_band=band,
                old_collection=collection,
                status=status,
                message=message,
            )
        )

    source_groups: dict[str, list[OrganizeItem]] = {}
    target_groups: dict[str, list[OrganizeItem]] = {}
    for item in items:
        if item.source_folder:
            source_groups.setdefault(item.source_folder.casefold(), []).append(item)
        if item.status == "move" and item.target_folder:
            target_groups.setdefault(item.target_folder.casefold(), []).append(item)
    for group in source_groups.values():
        if len(group) > 1:
            for item in group:
                item.status = "conflict"
                item.message = "多个条目共用同一源目录"
    for group in target_groups.values():
        if len(group) > 1:
            for item in group:
                item.status = "conflict"
                item.message = "多个条目会落到同一目标目录"

    return OrganizePlan(root=str(root_path), items=items)


def _manifest_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def apply_organize_plan(
    db: LevelLibraryDB,
    plan: OrganizePlan,
    history_dir: Path | str,
    selected_keys: set[str] | None = None,
) -> OrganizeResult:
    root = Path(plan.root)
    history = Path(history_dir)
    history.mkdir(parents=True, exist_ok=True)
    stamp = _manifest_stamp()
    manifest_path = history / f"organize-{stamp}.json"
    backup_path = history / f"level_library-{stamp}.json"
    if db.path.is_file():
        shutil.copy2(db.path, backup_path)

    moves = plan.move_items
    if selected_keys is not None:
        moves = [item for item in moves if item.key in selected_keys]
    manifest: dict[str, Any] = {
        "version": 1,
        "mode": "organize",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "root": str(root),
        "backup_path": str(backup_path) if backup_path.is_file() else "",
        "status": "started",
        "items": [
            {
                **item.to_dict(),
                "status": "pending",
                "error": "",
            }
            for item in moves
        ],
    }
    _write_json(manifest_path, manifest)

    result = OrganizeResult(mode="organize")
    counts = plan.counts()
    result.conflicts = counts.get("conflict", 0)
    result.skipped = counts.get("skip", 0)
    result.manifest_path = str(manifest_path)
    result.backup_path = manifest["backup_path"]

    for index, item in enumerate(moves):
        row = manifest["items"][index]
        source = Path(item.source_folder)
        target = Path(item.target_folder)
        target_path = Path(item.target_path)
        try:
            if not _within(target, root):
                raise ValueError("目标目录超出谱面根目录")
            if source.resolve() == root.resolve() or _within(root, source):
                raise ValueError("源目录包含谱面根目录")
            if not source.is_dir():
                raise ValueError("源目录已不存在")
            if target.exists():
                raise ValueError("目标目录已存在")
            new_key = db.location_key(item.key, target, target_path)
            if new_key != item.key and db.get(new_key) is not None:
                raise ValueError(f"目标条目编号 {new_key} 已被占用")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            try:
                entry = db.move_entry(
                    item.key,
                    target,
                    target_path,
                    category=item.old_category,
                    band=item.old_band,
                    collection=item.old_collection,
                )
            except Exception as exc:
                try:
                    shutil.move(str(target), str(source))
                except Exception as rollback_exc:
                    raise RuntimeError(
                        f"数据库更新失败，且回滚失败：{exc}；{rollback_exc}"
                    ) from exc
                raise
            row["status"] = "moved"
            row["new_key"] = str(entry.get("key") or "")
            result.moved += 1
        except Exception as exc:
            row["status"] = "failed"
            row["error"] = str(exc)
            result.failed += 1
            result.errors.append(f"{item.song}: {exc}")
        _write_json(manifest_path, manifest)

    manifest["status"] = (
        "completed"
        if result.failed == 0
        else ("partial" if result.moved else "failed")
    )
    manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
    _write_json(manifest_path, manifest)
    return result


def latest_manifest(history_dir: Path | str) -> Path | None:
    history = Path(history_dir)
    if not history.is_dir():
        return None
    manifests = sorted(history.glob("organize-*.json"), reverse=True)
    for path in manifests:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items = payload.get("items")
        if isinstance(items, list) and any(
            isinstance(item, dict) and item.get("status") == "moved"
            for item in items
        ):
            return path
    return None


def list_organize_manifests(history_dir: Path | str) -> list[dict[str, Any]]:
    history = Path(history_dir)
    if not history.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(history.glob("organize-*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items = payload.get("items")
        if not isinstance(items, list):
            continue
        counts: dict[str, int] = {}
        for row in items:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "pending")
            counts[status] = counts.get(status, 0) + 1
        records.append(
            {
                "path": str(path),
                "created_at": str(payload.get("created_at") or ""),
                "finished_at": str(payload.get("finished_at") or ""),
                "undo_finished_at": str(payload.get("undo_finished_at") or ""),
                "status": str(payload.get("status") or ""),
                "root": str(payload.get("root") or ""),
                "backup_path": str(payload.get("backup_path") or ""),
                "total": len(items),
                "moved": counts.get("moved", 0),
                "failed": counts.get("failed", 0),
                "undone": counts.get("undone", 0),
                "pending": counts.get("pending", 0),
                "counts": counts,
                "can_undo": counts.get("moved", 0) > 0,
                "items": [row for row in items if isinstance(row, dict)],
            }
        )
    return records


def undo_manifest(
    db: LevelLibraryDB,
    manifest_path: Path | str,
    history_dir: Path | str,
    selected_keys: set[str] | None = None,
) -> OrganizeResult:
    history = Path(history_dir)
    path = Path(manifest_path)
    try:
        path.resolve().relative_to(history.resolve())
    except (OSError, ValueError) as exc:
        raise ValueError("整理记录不在历史目录内") from exc
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取整理清单：{exc}") from exc
    items = manifest.get("items")
    if not isinstance(items, list):
        raise ValueError("整理清单格式不正确")
    def selected(row: dict[str, Any]) -> bool:
        return selected_keys is None or str(row.get("key") or "") in selected_keys

    if not any(
        isinstance(row, dict)
        and row.get("status") == "moved"
        and selected(row)
        for row in items
    ):
        raise ValueError(
            "没有可撤销的勾选条目"
            if selected_keys is not None
            else "该记录没有可撤销的条目"
        )

    history.mkdir(parents=True, exist_ok=True)
    stamp = _manifest_stamp()
    backup_path = history / f"level_library-undo-{stamp}.json"
    if db.path.is_file():
        shutil.copy2(db.path, backup_path)
    result = OrganizeResult(
        mode="undo",
        manifest_path=str(path),
        backup_path=str(backup_path) if backup_path.is_file() else "",
    )
    root = Path(str(manifest.get("root") or ""))
    for index in range(len(items) - 1, -1, -1):
        row = items[index]
        if (
            not isinstance(row, dict)
            or row.get("status") != "moved"
            or not selected(row)
        ):
            continue
        current = Path(str(row.get("target_folder") or ""))
        original = Path(str(row.get("source_folder") or ""))
        original_path = Path(str(row.get("source_path") or ""))
        try:
            if not current.is_dir():
                raise ValueError("整理后的目录已不存在")
            if original.exists():
                raise ValueError("原目录位置已被占用")
            if current.resolve() == root.resolve() or not _within(current, root):
                raise ValueError("整理后的目录超出谱面根目录")
            found = None
            new_key = str(row.get("new_key") or "")
            if new_key and db.get(new_key) is not None:
                found = (new_key, db.get(new_key) or {})
            if found is None:
                found = db.find_by_folder(current)
            if found is None:
                raise ValueError("谱面库中找不到对应条目")
            current_key, _entry = found
            restored_key = db.location_key(current_key, original, original_path)
            if restored_key != current_key and db.get(restored_key) is not None:
                raise ValueError(f"原条目编号 {restored_key} 已被占用")
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(current), str(original))
            try:
                db.move_entry(
                    current_key,
                    original,
                    original_path,
                    category=str(row.get("old_category") or ""),
                    band=str(row.get("old_band") or ""),
                    collection=str(row.get("old_collection") or ""),
                )
            except Exception as exc:
                try:
                    shutil.move(str(original), str(current))
                except Exception as rollback_exc:
                    raise RuntimeError(
                        f"数据库回滚失败：{exc}；{rollback_exc}"
                    ) from exc
                raise
            row["status"] = "undone"
            row["undo_error"] = ""
            result.moved += 1
        except Exception as exc:
            row["undo_error"] = str(exc)
            result.failed += 1
            result.errors.append(f"{row.get('song') or '谱面'}: {exc}")
        _write_json(path, manifest)

    manifest["undo_finished_at"] = datetime.now().isoformat(timespec="seconds")
    _write_json(path, manifest)
    return result


def undo_last_organize(
    db: LevelLibraryDB,
    history_dir: Path | str,
) -> OrganizeResult:
    manifest_path = latest_manifest(history_dir)
    if manifest_path is None:
        raise FileNotFoundError("没有可撤销的整理记录")
    return undo_manifest(db, manifest_path, history_dir)
