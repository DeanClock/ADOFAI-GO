from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .models import LevelInfo

SERIES = ("P", "G", "U")

CATEGORY_LABELS = {
    "P": "P 系列",
    "G": "G 系列",
    "U": "U 系列",
    "Unranked": "未评级",
    "Censored": "Censored",
    "Impossible": "Impossible",
    "Collection": "合集",
}

CATEGORY_COLORS = {
    "P": "#60A5FA",
    "G": "#34D399",
    "U": "#A78BFA",
    "Unranked": "#FBBF24",
    "Censored": "#94A3B8",
    "Impossible": "#F87171",
    "Collection": "#F472B6",
}

UNRANKED_ALIASES = {
    "unranked",
    "未评级",
    "未匹配",
    "-2and未评级",
    "2and未评级",
    "p0",
    "q0",
}

UNRANKED_FOLDER_NAMES = {
    "unranked",
    "未评级",
    "未匹配",
    "-2and未评级",
    "2and未评级",
}

SPECIAL_CATEGORIES = {
    "censored": "Censored",
    "impossible": "Impossible",
}

_BAND_RE = re.compile(r"^([PGU])(\d{1,2})$", re.IGNORECASE)

_INVALID_CHARS = '<>:"/\\|?*'
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _clean_name(value: str, limit: int = 60) -> str:
    name = "".join("_" if ch in _INVALID_CHARS or ord(ch) < 32 else ch for ch in (value or ""))
    name = name.strip(" .")
    if len(name) > limit:
        name = name[:limit].rstrip(" .")
    return name


def normalize_collection_name(value: str) -> str:
    """校验并规范化合集名称，空字符串表示移出合集。"""
    name = " ".join((value or "").split()).strip(" .")
    if not name:
        return ""
    if len(name) > 60:
        raise ValueError("合集名称最多 60 个字符")
    if any(ch in _INVALID_CHARS or ord(ch) < 32 for ch in name):
        raise ValueError('合集名称不能包含 < > : " / \\ | ? * 等字符')
    if name.split(".")[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise ValueError("合集名称不能使用 Windows 保留名")
    return name


def classify_difficulty(difficulty: str) -> tuple[str, str]:
    """把难度字符串拆成（分类目录, 等级目录）。"""
    text = (difficulty or "").strip()
    if not text:
        return "Unranked", ""
    lowered = text.lower()
    if lowered in SPECIAL_CATEGORIES:
        return SPECIAL_CATEGORIES[lowered], ""
    if lowered in UNRANKED_ALIASES:
        return "Unranked", ""
    match = _BAND_RE.match(text)
    if match:
        return match.group(1).upper(), text.upper()
    return "Unranked", ""


def category_for_path(parts: list[str]) -> tuple[str, str, str]:
    """根据相对目录推断（分类, 等级, 合集名）。"""
    if not parts:
        return "Unranked", "", ""
    head = parts[0].strip()
    upper = head.upper()
    if upper in SERIES:
        band = parts[1].upper() if len(parts) > 1 and _BAND_RE.match(parts[1]) else ""
        return upper, band, ""
    lowered = head.lower()
    if lowered in SPECIAL_CATEGORIES:
        return SPECIAL_CATEGORIES[lowered], "", ""
    if lowered in UNRANKED_ALIASES:
        return "Unranked", "", ""
    return "Collection", "", head


def chart_folder(root: Path, level: LevelInfo) -> Path:
    """返回某个谱面应落盘的文件夹（不含文件名）。"""
    category, band = classify_difficulty(level.difficulty)
    folder = target_chart_folder(root, level, category, band)
    if folder.exists() and not _folder_matches(folder, level):
        base = folder.parent
        song = folder.name
        charter = _clean_name(level.charter or "未知谱师", 40)
        suffix = f" ({level.tuf_id})" if level.tuf_id else ""
        folder = base / _clean_name(f"{song} - {charter}{suffix}", 80)
    return folder


def target_chart_folder(
    root: Path,
    level: LevelInfo,
    category: str,
    band: str = "",
    collection: str = "",
) -> Path:
    """按指定分类返回规范目标目录，不处理同名冲突。"""
    if category == "Collection":
        name = normalize_collection_name(collection)
        if not name:
            raise ValueError("合集名称为空，无法生成目标目录")
        if name.upper() in SERIES or name.casefold() in {
            "unranked",
            "未评级",
            "未匹配",
            "censored",
            "impossible",
        }:
            raise ValueError(f"合集名「{name}」与分类目录冲突")
        base = root / _clean_name(name)
    elif category in SERIES:
        base = root / category
        clean_band = _clean_name(band or "", 16)
        if clean_band:
            base = base / clean_band
    elif category == "Unranked":
        base = _unranked_root(root)
    elif category in ("Censored", "Impossible"):
        base = root / category
    else:
        base = root / "Unranked"
    return base / _clean_name(level.song or "未命名谱面")


def _unranked_root(root: Path) -> Path:
    try:
        for item in root.iterdir():
            if item.is_dir() and item.name.lower() in UNRANKED_FOLDER_NAMES:
                return item
    except OSError:
        pass
    return root / "Unranked"


def _folder_matches(folder: Path, level: LevelInfo) -> bool:
    marker = f"({level.tuf_id})" if level.tuf_id else ""
    try:
        names = [item.name for item in folder.iterdir()]
    except OSError:
        return False
    if not marker:
        return not names
    return any(marker in name for name in names)


def entry_category(entry: dict[str, Any]) -> str:
    value = str(entry.get("category") or "").strip()
    if value:
        return value
    return classify_difficulty(str(entry.get("difficulty") or ""))[0]


def entry_band(entry: dict[str, Any]) -> str:
    value = str(entry.get("band") or "").strip()
    if value:
        return value
    return classify_difficulty(str(entry.get("difficulty") or ""))[1]


def category_color(entry: dict[str, Any]) -> str:
    return CATEGORY_COLORS.get(entry_category(entry), "")
