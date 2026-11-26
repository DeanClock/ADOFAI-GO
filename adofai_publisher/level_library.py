from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .ard_parser import (
    ARDProfile,
    NO_CHART_CONTENT_ERROR,
    default_chart_member,
    parse_ard,
    parse_chart_member,
)
from .config import base_dir
from .level_paths import category_for_path, classify_difficulty, normalize_collection_name
from .models import LevelInfo

logger = logging.getLogger(__name__)

# 谱面库文件格式版本。
# 1 = 中旋按「非 0 角度」统计的旧数据；
# 2 = 中旋按 angleData 里的 999 统计；
# 3 = 条目带 parse_error（解析失败要能在界面上看见）；
# 4 = 本机条目带 auto_tile_count / playable_hits / variant / meta_missing。
LIBRARY_SCHEMA_VERSION = 4
# 这些来源的条目是本机解析出来的，可以重解析修正；download 等来源必须保留服务端数据。
_REPARSABLE_SOURCES = frozenset({"scan", "analyze"})

ARCHIVE_SUFFIXES = (".zip", ".adozip")
CHART_SUFFIXES = (".ard", ".adofai", *ARCHIVE_SUFFIXES)
BACKUP_RE = re.compile(r"^backup\d*$", re.IGNORECASE)
AUXILIARY_RE = re.compile(
    r"^(credit|effects?|noeffects?|non-?effects?|camera.*|sub\d+|low[_ ]?effects?)$",
    re.IGNORECASE,
)

# 显式写了 tuf 的编号可以短（tuf #94），裸 # 编号和括号编号要求 4 位以上，
# 避免把 (2023) 这类年份误认成 TUF 编号。
_TUF_EXPLICIT_RE = re.compile(r"\btuf\s*#\s*(\d+)\b", re.IGNORECASE)
_TUF_HASH_RE = re.compile(r"(?<![\w#])#\s*(\d{4,})\b")
_TUF_PAREN_RE = re.compile(r"\((\d{4,})\)")

_GENERIC_CHART_STEMS = frozenset(
    {"level", "main", "chart", "backup", "sub", "credit", "effect", "effects", "camera"}
)

_META_SOURCE_FIELDS = (("song", "song"), ("artist", "artist"), ("author", "charter"))

_LOCATION_FIELDS = (
    "key",
    "path",
    "filename",
    "folder",
    "category",
    "band",
    "collection",
    "game_difficulty",
    "source",
    "size",
    "added_at",
    "variant",
)

_TUF_ONLY_DEFAULTS: dict[str, Any] = {
    "difficulty_id": None,
    "difficulty_type": "",
    "difficulty_color": "",
    "difficulty_icon": "",
    "difficulty_sort_order": None,
    "base_score": None,
    "video_link": "",
    "dl_link": "",
    "workshop_link": "",
    "file_id": "",
    "tags": list,
    "curation_type_ids": list,
    "clears": 0,
    "unique_clears": 0,
    "likes": 0,
    "download_count": 0,
    "raw": dict,
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalize_location(value: Any) -> str:
    """把路径/目录折叠成可直接比较的形式（大小写、斜杠、相对路径差异）。"""
    if not value:
        return ""
    try:
        raw = os.path.abspath(os.path.expanduser(os.fspath(value)))
    except (TypeError, ValueError, OSError):
        raw = str(value)
    try:
        raw = os.path.realpath(raw)
    except (OSError, ValueError):
        pass
    return os.path.normcase(raw)


def _tuf_id(value: Any) -> int | None:
    try:
        raw = value.get("tuf_id") if isinstance(value, dict) else getattr(value, "tuf_id", None)
        tuf = int(raw)
    except (TypeError, ValueError):
        return None
    return tuf if tuf > 0 else None


def _tuf_compatible(first: int | None, second: int | None) -> bool:
    """TUF 编号相同、或其中一方为空时视为同一条谱面。"""
    if not first or not second:
        return True
    return int(first) == int(second)


def _merge_tags(first: Any, second: Any) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for tag in list(first or []) + list(second or []):
        if not isinstance(tag, dict):
            continue
        marker = (str(tag.get("id") or ""), str(tag.get("name") or "").casefold())
        if not any(marker) or marker in seen:
            continue
        seen.add(marker)
        merged.append(dict(tag))
    return merged


def _tuf_id_from_text(text: str) -> int | None:
    """从文件名/文件夹名里识别 TUF 编号。

    支持 ``tuf #1234``、``tuf#1234``、``#1234``、``(1234)``。显式带 ``tuf``
    前缀时允许编号少于 4 位（实测目录 ``tuf #94``、``tuf #512``）；裸 ``#``
    和括号编号仍要求 4 位以上，避免把年份、版本号当成 TUF 编号。
    """
    if not text:
        return None
    for pattern in (_TUF_EXPLICIT_RE, _TUF_HASH_RE, _TUF_PAREN_RE):
        match = pattern.search(str(text))
        if not match:
            continue
        try:
            value = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _detect_tuf_id(path: Path, folder: Path, root: Path | None = None) -> int | None:
    """按「文件名 → 最近的文件夹 → 更外层文件夹」顺序识别 TUF 编号。"""
    candidates: list[str] = [path.stem]
    parts: list[str] = []
    if root is not None:
        try:
            parts = list(folder.relative_to(root).parts)
        except ValueError:
            parts = []
    if not parts and folder.name:
        parts = [folder.name]
    candidates.extend(reversed(parts))
    for text in candidates:
        value = _tuf_id_from_text(text)
        if value is not None:
            return value
    return None


def _tuf_bound_heuristic(entry: dict[str, Any]) -> bool:
    """旧条目没有 tuf_bound 标记时，按是否带服务端字段推断。"""
    if not _tuf_id(entry):
        return False
    if str(entry.get("source") or "").strip().lower() == "download":
        return True
    return bool(
        entry.get("difficulty_id") is not None
        or entry.get("base_score") is not None
        or entry.get("dl_link")
        or entry.get("raw")
    )


def is_tuf_bound(entry: dict[str, Any] | None) -> bool:
    """条目是否已经绑定过 TUF 数据（而不是只从文件名识别出编号）。"""
    if not isinstance(entry, dict):
        return False
    if "tuf_bound" in entry and entry.get("tuf_bound") is not None:
        return bool(entry.get("tuf_bound"))
    return _tuf_bound_heuristic(entry)


def variant_container(entry: dict[str, Any] | None) -> Path | None:
    """条目对应的「谱面容器」：压缩包本身，或包含 .adofai 的文件夹。"""
    if not isinstance(entry, dict):
        return None
    raw = str(entry.get("path") or "").strip()
    path = Path(raw) if raw else None
    if path is not None and path.suffix.lower() in ARCHIVE_SUFFIXES:
        return path
    folder = str(entry.get("folder") or "").strip()
    if folder:
        folder_path = Path(folder)
        if folder_path.is_dir():
            return folder_path
    if path is not None and path.is_file():
        return path.parent
    return path


def entry_variant(entry: dict[str, Any] | None) -> str:
    """条目当前使用的变体名；没有记录时返回默认成员。"""
    if not isinstance(entry, dict):
        return ""
    stored = str(entry.get("variant") or "").strip()
    if stored:
        return stored
    container = variant_container(entry)
    if container is None:
        return ""
    if container.is_dir():
        raw = str(entry.get("path") or "").strip()
        if raw:
            try:
                return Path(raw).relative_to(container).as_posix()
            except ValueError:
                return Path(raw).name
        return ""
    return default_chart_member(container)


def _default_variant_name(container: Path, path: Path) -> str:
    if container.is_dir():
        try:
            return path.relative_to(container).as_posix()
        except ValueError:
            return path.name
    return default_chart_member(container) or path.name


def _missing_meta(profile: ARDProfile) -> list[str]:
    """谱面 settings 里缺失的曲名/曲师/谱师字段。"""
    missing: list[str] = []
    for source_field, label in _META_SOURCE_FIELDS:
        if not str(getattr(profile, source_field, "") or "").strip():
            missing.append(label)
    return missing


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _local_number_fields(entry: dict[str, Any] | None) -> dict[str, Any]:
    """条目里的本地数值；解析失败或没有可击物量时返回空字典。

    物量口径以本地文件为准：``tilecount`` 是本地总格，``playable_hits`` 已扣除
    中旋与自动砖。绑定 TUF 时只补元数据，不能用 TUF 的 tilecount 覆盖它们。
    """
    if not isinstance(entry, dict):
        return {}
    if str(entry.get("parse_error") or "").strip():
        return {}
    playable = _int_or_none(entry.get("playable_hits"))
    if playable is None:
        return {}
    fields: dict[str, Any] = {"playable_hits": max(playable, 0)}
    for field in ("tilecount", "midspin_count", "auto_tile_count"):
        value = _int_or_none(entry.get(field))
        if value is not None:
            fields[field] = value
    bpm = _float_or_none(entry.get("bpm"))
    if bpm:
        fields["bpm"] = bpm
    duration = _float_or_none(entry.get("duration_ms"))
    if duration:
        fields["duration_ms"] = duration
    return fields


def local_fields_from_profile(profile: ARDProfile) -> dict[str, Any]:
    """把本地解析结果转成谱面库里的数值；没有可用谱面时返回空字典。"""
    if profile.error or not profile.tile_count:
        return {}
    fields: dict[str, Any] = {
        "tilecount": int(profile.tile_count),
        "midspin_count": int(profile.midspin_count or 0),
        "auto_tile_count": int(profile.auto_tile_count or 0),
        "playable_hits": int(profile.playable_hits),
    }
    bpm = _float_or_none(profile.bpm)
    if bpm:
        fields["bpm"] = bpm
    duration = _float_or_none(profile.song_duration_ms)
    if duration:
        fields["duration_ms"] = duration
    return fields


def parse_chart_source(path: Path | str) -> ARDProfile:
    """解析下载/导入得到的谱面：文件按自身，解压目录挑主谱面。"""
    target = Path(path)
    if not target.is_dir():
        return parse_ard(str(target))
    charts = sorted(
        item
        for item in target.rglob("*")
        if item.is_file() and item.suffix.lower() == ".adofai"
    )
    if not charts:
        return ARDProfile(path=str(target), error=NO_CHART_CONTENT_ERROR)
    main = _pick_main_chart(charts)
    try:
        member = main.relative_to(target).as_posix()
    except ValueError:
        member = main.name
    return parse_chart_member(target, member)


def _clean_name_part(text: Any) -> str:
    value = re.sub(r"\s*[\(\[（【]\d{4,}[\)\]）】]\s*$", "", str(text or "")).strip()
    return value.strip(" -_·")


def _split_title(name: str) -> tuple[str, str]:
    """把 ``曲名 - 曲师`` 拆开；没有分隔符时整段当曲名。"""
    cleaned = _clean_name_part(name)
    if " - " in cleaned:
        song, _, artist = cleaned.partition(" - ")
        return song.strip(), artist.strip()
    return cleaned, ""


def _fallback_meta(
    path: Path,
    folder: Path | None,
    member: str = "",
) -> tuple[str, str]:
    """从文件名/文件夹名推断 (曲名, 曲师)，只用于谱面 settings 缺失时兜底。"""
    names: list[str] = []
    if member and member.lower().endswith(".adofai"):
        names.append(PurePosixPath(member.replace("\\", "/")).stem)
    if path.suffix.lower() in ARCHIVE_SUFFIXES:
        names.insert(0, path.stem)
    elif folder is not None and folder.name:
        names.append(folder.name)
    names.append(path.stem)
    seen: set[str] = set()
    for name in names:
        lowered = name.casefold()
        if not name or lowered in seen:
            continue
        seen.add(lowered)
        if lowered in _GENERIC_CHART_STEMS:
            continue
        song, artist = _split_title(name)
        if song:
            return song, artist
    return "", ""


def library_path() -> Path:
    return base_dir() / "level_library.json"


class LevelLibraryDB:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else library_path()
        self._entries: dict[str, dict[str, Any]] = {}
        self._collection_names: set[str] = set()
        self.load()

    def load(self) -> dict[str, dict[str, Any]]:
        self._entries = {}
        self._collection_names = set()
        if not self.path.is_file():
            return self._entries
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._entries
        if not isinstance(raw, dict):
            return self._entries
        schema_version = _schema_version(raw)
        names = raw.get("collections")
        if isinstance(names, list):
            self._collection_names = {
                str(name).strip()
                for name in names
                if str(name).strip()
            }
        rows = raw.get("entries")
        if isinstance(rows, dict):
            for key, entry in rows.items():
                if isinstance(entry, dict):
                    normalized = self._normalize(entry)
                    self._entries[key] = normalized
                    collection = str(normalized.get("collection") or "").strip()
                    if collection:
                        self._collection_names.add(collection)
        self.dedupe_locations()
        if schema_version < LIBRARY_SCHEMA_VERSION:
            self._migrate_entries(schema_version)
            try:
                self.save()
            except OSError:
                logger.warning("谱面库迁移：写入失败，下次启动会重试", exc_info=True)
        return self._entries

    def _migrate_entries(self, from_version: int) -> int:
        """按当前规则重解析旧版谱面库里的本机条目。

        只处理本机解析出来的条目（scan/analyze）；下载 / TUF 条目保留服务端下发的数据。
        v1 → v2 需要修正全部条目的中旋/物量；v2 → v3 只补做「解析失败」相关修复；
        v3 → v4 要为所有本机条目补齐自动砖 / 可击物量 / 变体 / 缺失字段，
        所以 v3 及以后的旧库会逐条重解析，v2 及更早仍沿用「只修坏条目」的策略。
        谱面文件已不存在或解析失败时保留原值，只记日志。
        """
        fixed = 0
        for entry in self._entries.values():
            source = str(entry.get("source") or "").strip().lower()
            if source not in _REPARSABLE_SOURCES:
                continue
            path = str(entry.get("path") or "")
            if not path:
                continue
            if from_version < 3 and from_version >= 2 and not _needs_parse_repair(entry):
                continue
            try:
                if not Path(path).is_file():
                    continue
                profile = self._parse_entry_chart(entry, Path(path))
            except Exception:
                logger.warning("谱面库迁移：解析 %s 失败，保留原值", path, exc_info=True)
                entry["parse_error"] = "解析谱面时发生异常"
                continue
            if profile.error:
                logger.warning(
                    "谱面库迁移：%s 解析失败（%s），保留原值", path, profile.error
                )
                entry["parse_error"] = profile.error
                continue
            _apply_profile(entry, profile)
            container = variant_container(entry) or Path(path)
            if not str(entry.get("variant") or "").strip():
                entry["variant"] = _default_variant_name(container, Path(path))
            entry["auto_tile_count"] = int(profile.auto_tile_count or 0)
            entry["playable_hits"] = int(profile.playable_hits)
            entry["meta_missing"] = (
                [] if is_tuf_bound(entry) else _missing_meta(profile)
            )
            folder_text = str(entry.get("folder") or "").strip()
            folder = Path(folder_text) if folder_text else None
            _apply_name_fallbacks(entry, Path(path), folder, entry["variant"])
            fixed += 1
        if fixed:
            logger.info("谱面库迁移：已修正 %d 条条目", fixed)
        return fixed

    def _parse_entry_chart(self, entry: dict[str, Any], path: Path) -> ARDProfile:
        """按条目保存的 variant 重解析，没有变体时走默认选择。"""
        member = str(entry.get("variant") or "").strip()
        container = variant_container(entry)
        if member and container is not None:
            return parse_chart_member(container, member)
        return parse_ard(str(path))

    def _normalize(self, entry: dict[str, Any]) -> dict[str, Any]:
        item = dict(entry)
        item.setdefault("key", uuid.uuid4().hex[:12])
        item.setdefault("added_at", item.get("updated_at") or _now())
        item.setdefault("updated_at", item.get("added_at"))
        item.setdefault("tuf_id", None)
        item.setdefault("path", "")
        item.setdefault("size", 0)
        item.setdefault("tags", [])
        item.setdefault("curation_type_ids", [])
        item.setdefault("category", "")
        item.setdefault("band", "")
        item.setdefault("collection", "")
        item.setdefault("folder", "")
        item.setdefault("game_difficulty", "")
        item.setdefault("variant", "")
        item.setdefault("meta_missing", [])
        item.setdefault("playable_hits", None)
        if not isinstance(item.get("meta_missing"), list):
            item["meta_missing"] = []
        if "tuf_bound" not in item:
            item["tuf_bound"] = _tuf_bound_heuristic(item)
        error = item.get("parse_error")
        item["parse_error"] = error.strip() if isinstance(error, str) else ""
        return item

    def save(self) -> None:
        payload = {
            "version": 1,
            "schema_version": LIBRARY_SCHEMA_VERSION,
            "collections": sorted(self._collection_names),
            "entries": self._entries,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def all(self) -> list[dict[str, Any]]:
        return sorted(
            self._entries.values(),
            key=lambda entry: str(entry.get("updated_at") or ""),
            reverse=True,
        )

    def get(self, key: str) -> dict[str, Any] | None:
        return self._entries.get(key)

    def _collection_lookup(self) -> dict[str, str]:
        lookup: dict[str, str] = {}
        for name in self._collection_names:
            lookup.setdefault(name.casefold(), name)
        for entry in self._entries.values():
            name = str(entry.get("collection") or "").strip()
            if name:
                lookup.setdefault(name.casefold(), name)
        return lookup

    def _canonical_collection(self, name: str) -> str:
        normalized = normalize_collection_name(name)
        if not normalized:
            return ""
        return self._collection_lookup().get(normalized.casefold(), normalized)

    def collections(self) -> list[tuple[str, int]]:
        counts = Counter(
            str(entry.get("collection") or "").strip()
            for entry in self._entries.values()
            if str(entry.get("collection") or "").strip()
        )
        names = set(self._collection_names) | set(counts)
        return sorted((name, counts.get(name, 0)) for name in names)

    def create_collection(self, name: str) -> str:
        normalized = normalize_collection_name(name)
        if not normalized:
            raise ValueError("合集名称不能为空")
        existing = self._collection_lookup().get(normalized.casefold())
        if existing:
            raise ValueError(f"合集「{existing}」已存在")
        self._collection_names.add(normalized)
        return normalized

    @staticmethod
    def _restore_category(entry: dict[str, Any]) -> None:
        difficulty = str(entry.get("difficulty") or entry.get("band") or "")
        category, band = classify_difficulty(difficulty)
        entry["category"] = category
        if band:
            entry["band"] = band

    def assign_collection(self, key: str, name: str) -> dict[str, Any]:
        entry = self._entries.get(key)
        if entry is None:
            raise KeyError(key)
        normalized = self._canonical_collection(name)
        if normalized:
            self._collection_names.add(normalized)
            entry["collection"] = normalized
            entry["category"] = "Collection"
        else:
            entry["collection"] = ""
            if str(entry.get("category") or "") == "Collection":
                self._restore_category(entry)
        entry["updated_at"] = _now()
        return entry

    def rename_collection(self, old_name: str, new_name: str) -> int:
        old = normalize_collection_name(old_name)
        new = normalize_collection_name(new_name)
        if not old:
            raise ValueError("请选择要改名的合集")
        if not new:
            raise ValueError("新合集名称不能为空")
        if old.casefold() == new.casefold():
            return 0
        canonical_new = self._canonical_collection(new)
        changed = 0
        for entry in self._entries.values():
            current = str(entry.get("collection") or "").strip()
            if current.casefold() == old.casefold():
                entry["collection"] = canonical_new
                entry["category"] = "Collection"
                entry["updated_at"] = _now()
                changed += 1
        for name in list(self._collection_names):
            if name.casefold() == old.casefold():
                self._collection_names.discard(name)
        self._collection_names.add(canonical_new)
        return changed

    def dissolve_collection(self, name: str) -> int:
        wanted = normalize_collection_name(name)
        if not wanted:
            raise ValueError("请选择要解散的合集")
        changed = 0
        for entry in self._entries.values():
            current = str(entry.get("collection") or "").strip()
            if current.casefold() != wanted.casefold():
                continue
            entry["collection"] = ""
            self._restore_category(entry)
            entry["updated_at"] = _now()
            changed += 1
        for current in list(self._collection_names):
            if current.casefold() == wanted.casefold():
                self._collection_names.discard(current)
        return changed

    @staticmethod
    def _entry_key(level: Any, path: Path, folder: str | Path | None = None) -> str:
        tuf = _tuf_id(level)
        if tuf:
            return f"tuf:{tuf}"
        if path.suffix.lower() in ARCHIVE_SUFFIXES:
            return f"file:{str(path).lower()}"
        if folder:
            return f"folder:{str(folder).lower()}"
        return f"file:{str(path).lower()}"

    def _locate_existing_key(
        self,
        level: Any,
        path: Path | str,
        folder: str | Path | None = None,
    ) -> str | None:
        """算出的 key 不存在时，按规范化位置找回已有条目。

        优先按文件路径匹配；路径对不上再按目录兜底。目录兜底只在不会误合并的
        情况下使用：候选条目的 path 为空，或它的父目录就是本次扫描的目录，且
        TUF 编号兼容（相同或一方为空）。
        """
        path = Path(path)
        tuf = _tuf_id(level)

        wanted_path = _normalize_location(path)
        if wanted_path:
            for key, entry in self._entries.items():
                stored = entry.get("path")
                if stored and _normalize_location(stored) == wanted_path:
                    if _tuf_compatible(tuf, _tuf_id(entry)):
                        return key

        folder_norm = _normalize_location(folder) if folder else ""
        if not folder_norm or path.suffix.lower() in ARCHIVE_SUFFIXES:
            return None

        candidates: list[str] = []
        for key, entry in self._entries.items():
            if not _tuf_compatible(tuf, _tuf_id(entry)):
                continue
            stored_path = str(entry.get("path") or "")
            if stored_path:
                if Path(stored_path).suffix.lower() in ARCHIVE_SUFFIXES:
                    # 压缩包按文件区分，不并入同目录的谱面条目。
                    continue
                if _normalize_location(Path(stored_path).parent) == folder_norm:
                    candidates.append(key)
            elif _normalize_location(entry.get("folder")) == folder_norm:
                candidates.append(key)
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda item: (_tuf_id(self._entries[item]) is None, item),
        )

    def _existing_key_for(
        self,
        level: Any,
        path: Path | str,
        folder: str | Path | None = None,
    ) -> str | None:
        key = self._entry_key(level, Path(path), folder)
        if key in self._entries:
            return key
        return self._locate_existing_key(level, path, folder)

    def dedupe_locations(self) -> list[tuple[str, str]]:
        """合并落在同一规范化路径上的重复条目，返回 (保留 key, 删除 key)。"""
        groups: dict[str, list[str]] = {}
        for key, entry in self._entries.items():
            location = _normalize_location(entry.get("path"))
            if location:
                groups.setdefault(location, []).append(key)
        merged: list[tuple[str, str]] = []
        for keys in groups.values():
            if len(keys) < 2:
                continue
            ordered = sorted(
                keys,
                key=lambda item: (_tuf_id(self._entries[item]) is None, item),
            )
            keep_key = ordered[0]
            for drop_key in ordered[1:]:
                keep = self._entries.get(keep_key)
                drop = self._entries.get(drop_key)
                if keep is None or drop is None:
                    continue
                if not _tuf_compatible(_tuf_id(keep), _tuf_id(drop)):
                    continue
                self._absorb_entry(keep_key, drop_key)
                merged.append((keep_key, drop_key))
        return merged

    def _absorb_entry(self, keep_key: str, drop_key: str) -> dict[str, Any]:
        keep = self._entries[keep_key]
        drop = self._entries[drop_key]
        for field, value in drop.items():
            # parse_error 描述的是「本地文件能否解析」，不能从另一条记录借过来。
            if field in ("key", "added_at", "updated_at", "tags", "tuf_id", "parse_error"):
                continue
            current = keep.get(field)
            if current is None or current == "" or current == 0 or current == [] or current == {}:
                keep[field] = value
        keep["tuf_id"] = keep.get("tuf_id") or drop.get("tuf_id")
        keep["tags"] = _merge_tags(keep.get("tags"), drop.get("tags"))
        added = sorted(
            value
            for value in (str(keep.get("added_at") or ""), str(drop.get("added_at") or ""))
            if value
        )
        if added:
            keep["added_at"] = added[0]
        updated = sorted(
            value
            for value in (str(keep.get("updated_at") or ""), str(drop.get("updated_at") or ""))
            if value
        )
        keep["updated_at"] = updated[-1] if updated else _now()
        self._entries[keep_key] = keep
        self._entries.pop(drop_key, None)
        return keep

    def location_key(self, key: str, folder: Path | str, path: Path | str) -> str:
        entry = self._entries.get(key)
        if entry is None:
            raise KeyError(key)
        return self._entry_key(entry, Path(path), folder)

    def find_by_folder(self, folder: Path | str) -> tuple[str, dict[str, Any]] | None:
        wanted = str(folder).casefold()
        for key, entry in self._entries.items():
            if str(entry.get("folder") or "").casefold() == wanted:
                return key, entry
        return None

    def move_entry(
        self,
        key: str,
        folder: Path | str,
        path: Path | str,
        *,
        category: str | None = None,
        band: str | None = None,
        collection: str | None = None,
    ) -> dict[str, Any]:
        entry = self._entries.get(key)
        if entry is None:
            raise KeyError(key)
        folder_path = Path(folder)
        chart_path = Path(path)
        new_key = self._entry_key(entry, chart_path, folder_path)
        if new_key != key and new_key in self._entries:
            raise ValueError(f"目标位置已被条目 {new_key} 占用")
        self._entries.pop(key, None)
        entry["key"] = new_key
        entry["folder"] = str(folder_path)
        entry["path"] = str(chart_path)
        entry["filename"] = chart_path.name
        if category is not None:
            entry["category"] = category
        if band is not None:
            entry["band"] = band
        if collection is not None:
            entry["collection"] = collection
            if collection:
                entry["category"] = "Collection"
                self._collection_names.add(collection)
        try:
            entry["size"] = chart_path.stat().st_size
        except OSError:
            pass
        entry["updated_at"] = _now()
        self._entries[new_key] = entry
        return entry

    def _upsert_bound_entry(
        self,
        existing: dict[str, Any],
        key: str,
        data: dict[str, Any],
        path: Path,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        """已绑定条目重新导入/重扫：只刷新文件信息与本地数值。

        TUF 元数据（曲名、曲师、谱师、难度、VFX、链接、统计等）和未知字段
        （review_*、collection 等）全部保留，避免重扫把已绑定信息覆盖成空。
        """
        entry = dict(existing)
        entry["key"] = key
        entry["path"] = str(path)
        entry["filename"] = path.name
        folder = extra.get("folder")
        if folder:
            entry["folder"] = str(folder)
        try:
            entry["size"] = path.stat().st_size
        except OSError:
            entry["size"] = int(existing.get("size") or 0)
        variant = extra.get("variant")
        if variant is not None:
            entry["variant"] = str(variant)
        parse_error = extra.get("parse_error")
        if parse_error is None:
            parse_error = existing.get("parse_error", "")
        entry["parse_error"] = str(parse_error or "")
        game_difficulty = extra.get("game_difficulty")
        if game_difficulty is not None:
            entry["game_difficulty"] = str(game_difficulty or "")
        # 分类/难度段/合集按路径刷新，语义沿用未绑定条目的 upsert：
        # 非空覆盖，空值保留原值；合集仍归到 Collection 分类。
        category = extra.get("category")
        if category:
            entry["category"] = str(category)
        band = extra.get("band")
        if band:
            entry["band"] = str(band)
        collection = extra.get("collection")
        if collection:
            entry["collection"] = str(collection)
        final_collection = str(entry.get("collection") or "").strip()
        if final_collection:
            self._collection_names.add(final_collection)
            entry["category"] = "Collection"
        if not entry["parse_error"].strip():
            entry.update(_local_number_fields(data))
        entry["meta_missing"] = []
        # 扫描时识别出的编号不能改绑已有 TUF；绑定状态保持为真。
        entry["tuf_id"] = existing.get("tuf_id") or data.get("tuf_id")
        entry["tuf_bound"] = True
        entry["added_at"] = existing.get("added_at") or _now()
        entry["updated_at"] = _now()
        self._entries[key] = entry
        return entry

    def upsert(self, level: Any, path: Path | str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        path = Path(path)
        extra = dict(extra or {})
        data = level.to_dict() if level is not None else {}
        if not isinstance(data, dict):
            data = {}
        key = self._existing_key_for(level, path, extra.get("folder")) or self._entry_key(
            level, path, extra.get("folder")
        )
        existing = self._entries.get(key, {})
        if existing and is_tuf_bound(existing):
            return self._upsert_bound_entry(existing, key, data, path, extra)

        def keep(field: str, default: Any = None) -> Any:
            value = data.get(field)
            if value is None or value == "" or value == 0 or value == [] or value == {}:
                return existing.get(field, default)
            return value

        tuf_id = data.get("tuf_id")
        entry = {
            "key": key,
            "song": data.get("song") or "",
            "artist": data.get("artist") or "",
            "charter": data.get("charter") or "",
            "vfxers": data.get("vfxers") or [],
            "difficulty": data.get("difficulty") or "",
            "difficulty_id": keep("difficulty_id"),
            "difficulty_type": keep("difficulty_type", ""),
            "difficulty_color": keep("difficulty_color", ""),
            "difficulty_icon": keep("difficulty_icon", ""),
            "difficulty_sort_order": keep("difficulty_sort_order"),
            "base_score": keep("base_score"),
            "bpm": data.get("bpm"),
            "duration_ms": data.get("duration_ms"),
            "tilecount": data.get("tilecount"),
            "auto_tile_count": (
                int(data.get("auto_tile_count") or 0)
                if data.get("tuf_id")
                else keep("auto_tile_count", 0)
            ),
            "midspin_count": data.get("midspin_count") or 0,
            # 扫描/绑定都会重新给出这个值；服务端条目没有时保持为空，
            # 让 expected_hits 回到 tilecount - auto_tile_count。
            "playable_hits": data.get("playable_hits"),
            "variant": existing.get("variant", ""),
            "meta_missing": existing.get("meta_missing", []),
            "tuf_id": tuf_id if tuf_id else existing.get("tuf_id"),
            "dl_link": data.get("dl_link") or existing.get("dl_link", ""),
            "workshop_link": data.get("workshop_link") or existing.get("workshop_link", ""),
            "video_link": data.get("video_link") or existing.get("video_link", ""),
            "file_id": data.get("file_id") or existing.get("file_id", ""),
            "tags": data.get("tags") or existing.get("tags", []),
            "curation_type_ids": keep("curation_type_ids", []),
            "clears": keep("clears", 0),
            "unique_clears": keep("unique_clears", 0),
            "likes": keep("likes", 0),
            "download_count": keep("download_count", 0),
            "raw": keep("raw", {}),
            "filename": path.name,
            "path": str(path),
            "folder": extra.get("folder") or existing.get("folder", ""),
            "category": extra.get("category") or existing.get("category", ""),
            "band": extra.get("band") or existing.get("band", ""),
            "collection": extra.get("collection") or existing.get("collection", ""),
            "game_difficulty": extra.get("game_difficulty")
            if extra.get("game_difficulty") is not None
            else existing.get("game_difficulty", ""),
            "parse_error": extra.get("parse_error")
            if extra.get("parse_error") is not None
            else existing.get("parse_error", ""),
        }
        try:
            entry["size"] = path.stat().st_size
        except OSError:
            entry["size"] = int(existing.get("size") or 0)
        merged = {
            k: v
            for k, v in extra.items()
            if v is not None
            and k not in ("folder", "category", "band", "collection", "game_difficulty")
        }
        entry.update(merged)
        if "tuf_bound" in existing:
            entry["tuf_bound"] = bool(existing["tuf_bound"])
        else:
            entry["tuf_bound"] = _tuf_bound_heuristic(entry)
        collection = str(entry.get("collection") or "").strip()
        if collection:
            self._collection_names.add(collection)
            entry["category"] = "Collection"
        entry["added_at"] = existing.get("added_at") or _now()
        entry["updated_at"] = _now()
        self._entries[key] = entry
        return entry

    def delete(self, key: str, delete_file: bool = False) -> bool:
        entry = self._entries.pop(key, None)
        if entry is None:
            return False
        if delete_file and entry.get("path"):
            try:
                Path(entry["path"]).unlink()
            except OSError:
                pass
        return True

    def find_by_tuf(self, tuf_id: int, exclude_key: str = "") -> list[tuple[str, dict[str, Any]]]:
        try:
            wanted = int(tuf_id)
        except (TypeError, ValueError):
            return []
        matches: list[tuple[str, dict[str, Any]]] = []
        for key, entry in self._entries.items():
            if key == exclude_key:
                continue
            try:
                current = int(entry.get("tuf_id") or 0)
            except (TypeError, ValueError):
                current = 0
            if current == wanted:
                matches.append((key, entry))
        return matches

    def bind_tuf(self, key: str, level: LevelInfo) -> dict[str, Any]:
        entry = self._entries.get(key)
        if entry is None:
            raise KeyError(key)
        data = level.to_dict()
        for field in _LOCATION_FIELDS:
            if field == "key":
                continue
            if entry.get(field) not in (None, "", [], {}):
                data[field] = entry.get(field)
        data["key"] = key
        data["tuf_id"] = int(level.tuf_id) if level.tuf_id else None
        # 本地文件本身是否解析失败与 TUF 元数据无关，绑定后仍要保留提示。
        data["parse_error"] = entry.get("parse_error") or ""
        # 本地优先：物量/可击物量/BPM/时长来自本地解析，TUF 只补元数据。
        # 本地文件解析失败时才退回 TUF 的 tilecount / auto / midspin。
        local = _local_number_fields(entry)
        if local:
            data.update(local)
        data["tuf_bound"] = True
        data["meta_missing"] = []
        data["added_at"] = entry.get("added_at") or _now()
        data["updated_at"] = _now()
        self._entries[key] = data
        return data

    def set_variant(self, key: str, member: str) -> dict[str, Any]:
        """把条目切换到容器里的指定谱面变体，并按该成员重解析。

        只改索引里的解析结果，不移动或重命名磁盘文件。变体不存在/解析失败时
        抛 ``ValueError``，条目保持原样，由详情页展示错误。
        """
        entry = self._entries.get(key)
        if entry is None:
            raise KeyError(key)
        container = variant_container(entry)
        if container is None:
            raise ValueError("找不到谱面容器")
        profile = parse_chart_member(container, member)
        if profile.error:
            # 解析失败的成员不写入条目，避免把索引指向一个用不了的变体。
            raise ValueError(profile.error)
        entry["variant"] = member
        entry["updated_at"] = _now()
        path = Path(str(entry.get("path") or ""))
        folder_text = str(entry.get("folder") or "").strip()
        folder = Path(folder_text) if folder_text else path.parent
        _apply_profile(entry, profile)
        entry["meta_missing"] = (
            [] if is_tuf_bound(entry) else _missing_meta(profile)
        )
        if profile.tile_count:
            entry["tilecount"] = int(profile.tile_count)
        bpm = _float_or_none(profile.bpm)
        if bpm:
            entry["bpm"] = bpm
        duration = _float_or_none(profile.song_duration_ms)
        if duration:
            entry["duration_ms"] = duration
        if str(profile.difficulty or "").strip():
            entry["game_difficulty"] = str(profile.difficulty).strip()
        _apply_name_fallbacks(entry, path, folder, member)
        return entry

    def merge_entries(self, keep_key: str, drop_key: str) -> dict[str, Any]:
        keep = self._entries.get(keep_key)
        drop = self._entries.get(drop_key)
        if keep is None or drop is None:
            raise KeyError(keep_key if keep is None else drop_key)
        merged = dict(keep)
        for field, value in drop.items():
            if field in ("key", "added_at", "updated_at", "tags", "parse_error"):
                continue
            current = merged.get(field)
            if current is None or current == "" or current == 0 or current == [] or current == {}:
                merged[field] = value
        merged["key"] = keep_key
        merged["tuf_id"] = keep.get("tuf_id") or drop.get("tuf_id")
        merged["tags"] = _merge_tags(keep.get("tags"), drop.get("tags"))
        added = sorted(
            value
            for value in (str(keep.get("added_at") or ""), str(drop.get("added_at") or ""))
            if value
        )
        merged["added_at"] = added[0] if added else _now()
        merged["updated_at"] = _now()
        self._entries[keep_key] = merged
        self._entries.pop(drop_key, None)
        return merged

    def unbind_tuf(self, key: str) -> dict[str, Any]:
        entry = self._entries.get(key)
        if entry is None:
            raise KeyError(key)
        entry["tuf_id"] = None
        entry["tuf_bound"] = False
        entry["meta_missing"] = []
        for field, default in _TUF_ONLY_DEFAULTS.items():
            entry[field] = default() if callable(default) else default
        band = str(entry.get("band") or "")
        if band:
            entry["difficulty"] = band
        elif str(entry.get("category") or "") in ("Unranked", "Censored", "Impossible"):
            entry["difficulty"] = str(entry["category"])
        entry["updated_at"] = _now()
        return entry

    def scan(self, directory: Path | str) -> ScanResult:
        root = Path(directory)
        if not root.is_dir():
            return ScanResult()
        self.dedupe_locations()
        result = ScanResult()
        files: list[Path] = []
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in CHART_SUFFIXES:
                files.append(path)
        folders = {
            path.parent
            for path in files
            if path.suffix.lower() not in ARCHIVE_SUFFIXES
        }
        for folder in sorted(folders):
            charts = [
                item
                for item in folder.iterdir()
                if item.is_file()
                and item.suffix.lower() in CHART_SUFFIXES
                and item.suffix.lower() not in ARCHIVE_SUFFIXES
            ]
            if not charts:
                continue
            self._scan_chart(
                path=_pick_main_chart(charts),
                folder=folder,
                root=root,
                result=result,
            )
        for archive in sorted(
            path for path in files if path.suffix.lower() in ARCHIVE_SUFFIXES
        ):
            if _archive_has_extracted_copy(archive, folders):
                continue
            self._scan_chart(
                path=archive,
                folder=archive.parent,
                root=root,
                result=result,
            )
        return result

    def _entry_at_path(self, path: Path) -> dict[str, Any] | None:
        """按规范化路径找已有条目，用于读取用户保存的变体选择。"""
        wanted = _normalize_location(path)
        if not wanted:
            return None
        for entry in self._entries.values():
            if _normalize_location(entry.get("path")) == wanted:
                return entry
        return None

    def _entry_at_folder(self, folder: Path) -> dict[str, Any] | None:
        """按文件夹找本机谱面条目（压缩包条目按文件区分，不参与）。"""
        wanted = _normalize_location(folder)
        if not wanted:
            return None
        for entry in self._entries.values():
            if _normalize_location(entry.get("folder")) != wanted:
                continue
            stored = str(entry.get("path") or "")
            if stored and Path(stored).suffix.lower() in ARCHIVE_SUFFIXES:
                continue
            return entry
        return None

    def _level_from_profile(
        self,
        profile: ARDProfile,
        path: Path,
        folder: Path,
        tuf_id: int | None,
        variant: str,
        difficulty: str = "",
    ) -> tuple[LevelInfo, list[str]]:
        """把解析结果转成 LevelInfo，并为缺失字段套用文件名/文件夹名兜底。"""
        song = (profile.song or "").strip()
        artist = (profile.artist or "").strip()
        charter = (profile.author or "").strip()
        fallback_song, fallback_artist = _fallback_meta(path, folder, variant)
        song = song or fallback_song
        artist = artist or fallback_artist
        missing = [] if profile.error else _missing_meta(profile)
        return (
            LevelInfo(
                tuf_id=tuf_id,
                song=song,
                artist=artist,
                charter=charter,
                difficulty=difficulty,
                bpm=_float_or_none(profile.bpm),
                duration_ms=_float_or_none(profile.song_duration_ms),
                tilecount=profile.tile_count or None,
                auto_tile_count=int(profile.auto_tile_count or 0),
                midspin_count=int(profile.midspin_count or 0),
                playable_hits=(
                    int(profile.playable_hits)
                    if profile.tile_count and not profile.error
                    else None
                ),
            ),
            missing,
        )

    def _scan_chart(
        self,
        path: Path,
        folder: Path,
        root: Path,
        result: "ScanResult",
    ) -> None:
        container = path if path.suffix.lower() in ARCHIVE_SUFFIXES else folder
        existing = self._entry_at_path(path)
        if existing is None and path.suffix.lower() not in ARCHIVE_SUFFIXES:
            # 同一个文件夹换主谱面（文件大小变化）时仍能认出旧条目。
            existing = self._entry_at_folder(folder)
        variant = str((existing or {}).get("variant") or "").strip()
        try:
            if variant:
                profile = parse_chart_member(container, variant)
            else:
                profile = parse_ard(str(path))
            parse_error = profile.error
        except Exception as exc:  # 解析器自身出错也要留下痕迹，不能静默
            profile = ARDProfile(path=str(path))
            parse_error = f"解析谱面时发生异常：{exc.__class__.__name__}"
            profile.error = parse_error
        if parse_error == NO_CHART_CONTENT_ERROR:
            # 误导入的安装包/发布包：不入库，并清掉同一路径上以前生成的垃圾条目。
            if existing is not None:
                for existing_key, current in list(self._entries.items()):
                    if current is existing:
                        self._entries.pop(existing_key, None)
                        break
            result.skipped_count += 1
            return
        if not variant:
            variant = _default_variant_name(container, path)
        try:
            relative = folder.relative_to(root).parts
        except ValueError:
            relative = ()
        category, band, collection = category_for_path(list(relative))
        # 已绑定的 TUF 编号优先：后续扫描不能因为文件名/文件夹名而改绑。
        tuf_id = _tuf_id(existing) if existing else None
        if tuf_id is None:
            tuf_id = _detect_tuf_id(path, folder, root)
        difficulty = band or (
            category if category in ("Unranked", "Censored", "Impossible") else ""
        )
        level, missing = self._level_from_profile(
            profile, path, folder, tuf_id, variant, difficulty
        )
        if existing is not None and is_tuf_bound(existing):
            # 已绑定的条目以 TUF 数据补齐字段，本地谱面没写不算缺失。
            missing = []
        existed = self._existing_key_for(level, path, folder) is not None
        extra: dict[str, Any] = {
            "source": "scan",
            "category": category,
            "band": band,
            "collection": collection,
            "folder": str(folder),
            "game_difficulty": getattr(profile, "difficulty", "") or "",
            "parse_error": parse_error or "",
            "variant": variant,
            "meta_missing": missing,
        }
        if not parse_error:
            extra["auto_tile_count"] = int(profile.auto_tile_count or 0)
            extra["playable_hits"] = int(profile.playable_hits)
        entry = self.upsert(level, path, extra)
        if parse_error:
            result.failed_count += 1
        result[entry["key"]] = entry
        if existed:
            result.updated_count += 1
        else:
            result.created_count += 1

    def prune_missing(self) -> list[str]:
        removed: list[str] = []
        for key, entry in list(self._entries.items()):
            folder = entry.get("folder")
            if folder and not Path(folder).is_dir():
                del self._entries[key]
                removed.append(key)
                continue
            if entry.get("path") and not Path(entry["path"]).is_file():
                del self._entries[key]
                removed.append(key)
        return removed


class ScanResult(dict):
    """扫描结果：沿用 key -> entry 的字典契约，另带新增/更新计数。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.created_count = 0
        self.updated_count = 0
        self.failed_count = 0
        # 没有 .adofai 内容的压缩包/目录，扫描时跳过（并清理旧条目）。
        self.skipped_count = 0


def _pick_main_chart(files: list[Path]) -> Path:
    return min(files, key=lambda item: (_chart_rank(item), -_safe_size(item)))


def _archive_has_extracted_copy(archive: Path, chart_folders: set[Path]) -> bool:
    """压缩包旁边已经有解压后的谱面时，不再重复入库。"""
    if archive.parent in chart_folders:
        return True
    extracted = archive.with_name(archive.stem)
    if not extracted.is_dir():
        return False
    return any(
        folder == extracted or extracted in folder.parents
        for folder in chart_folders
    )


def _chart_rank(path: Path) -> int:
    stem = path.stem
    if BACKUP_RE.match(stem):
        return 3
    lowered = stem.lower()
    if lowered.startswith("level") or lowered.startswith("main"):
        return 0
    if AUXILIARY_RE.match(stem):
        return 2
    return 1


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _schema_version(raw: dict[str, Any]) -> int:
    """旧库文件没有 schema_version，按 1 处理。"""
    try:
        version = int(raw.get("schema_version") or 1)
    except (TypeError, ValueError):
        return 1
    return version if version > 0 else 1


def _needs_parse_repair(entry: dict[str, Any]) -> bool:
    """v2 → v3 迁移只挑「可能解析失败」的条目重解析，避免整库重扫。

    旧版本解析失败时不写 parse_error，而是把曲名回退成文件夹名、物量留空，
    所以物量缺失也要算作需要修复（解析成功的空谱面重扫一次无害）。
    """
    if str(entry.get("parse_error") or "").strip():
        return True
    if not str(entry.get("song") or "").strip():
        return True
    try:
        tilecount = int(entry.get("tilecount") or 0)
    except (TypeError, ValueError):
        return True
    return tilecount <= 0


def _apply_profile(entry: dict[str, Any], profile: ARDProfile) -> None:
    """用重解析结果刷新条目的中旋、物量和基础元数据；解析不出的字段保持原值。"""
    entry["parse_error"] = profile.error or ""
    entry["midspin_count"] = int(profile.midspin_count or 0)
    entry["auto_tile_count"] = int(profile.auto_tile_count or 0)
    entry["playable_hits"] = int(profile.playable_hits)
    if profile.tile_count:
        entry["tilecount"] = int(profile.tile_count)
    for field, value in (
        ("song", profile.song),
        ("artist", profile.artist),
        ("charter", profile.author),
        ("game_difficulty", profile.difficulty),
    ):
        text = str(value or "").strip()
        if text:
            entry[field] = text
    bpm = _float_or_none(profile.bpm)
    if bpm:
        entry["bpm"] = bpm
    duration = _float_or_none(profile.song_duration_ms)
    if duration:
        entry["duration_ms"] = duration


def _apply_name_fallbacks(
    entry: dict[str, Any],
    path: Path,
    folder: Path | None,
    member: str = "",
) -> None:
    """曲名/曲师为空时用文件名、文件夹名兜底（不会覆盖已有值）。"""
    if not str(entry.get("song") or "").strip():
        song, _artist = _fallback_meta(path, folder, member)
        if song:
            entry["song"] = song
    if not str(entry.get("artist") or "").strip():
        _song, artist = _fallback_meta(path, folder, member)
        if artist:
            entry["artist"] = artist
