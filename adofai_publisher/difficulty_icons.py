"""TUF 难度图标。

图标来自 TUF（https://tuforums.com）难度库，属于第三方素材；本文件集中维护
难度名到素材文件的映射，便于开源 / 再分发时统一补充署名与授权说明。
未收录的难度（如 U16、Q2+ 等）返回 None，由 UI 回退到文字徽章。
"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap

from .resources import resource_dir

ICON_SOURCE = "TUF (https://tuforums.com)"

DIFFICULTY_ICON_DIR = resource_dir() / "assets" / "icons" / "difficulty"

# 难度名 -> 素材文件名（与 TUF 原始文件名一致）。
DIFFICULTY_ICONS: dict[str, str] = {
    **{f"P{number}": f"P{number}.png" for number in range(1, 21)},
    # TUF 把 P0 归在 SPECIAL，同样有独立图标。
    "P0": "P0.png",
    **{f"G{number}": f"G{number}.png" for number in range(1, 21)},
    # TUF 的 GQ 系列（“GQ0 (G1~G4)”…“GQ4 (G17~G20)”）各有独立图标；
    # 短名 GQ0~GQ4 一并收录，防止数据只给短名。
    **{f"GQ{index}": f"GQ{index}.png" for index in range(0, 5)},
    **{
        f"GQ{index} (G{index * 4 + 1}~G{index * 4 + 4})": f"GQ{index}.png"
        for index in range(0, 5)
    },
    **{f"U{number}": f"U{number}.png" for number in range(1, 15)},
    **{f"U{number}J": f"U{number}J.png" for number in (1, 5, 6, 7, 9, 11, 13)},
    "U15J": "U15J.png",
    **{f"Q{number}": f"Q{number}.png" for number in range(0, 5)},
    "Qq": "Qq.png",
    "Unranked": "Unranked.png",
    "Censored": "Censored.png",
    "Impossible": "Impossible.png",
    "MA": "MA.png",
    # TUF 把这张图标叫 Marathon，素材文件名是 MA.png。
    "Marathon": "MA.png",
    "Gimmick": "Gimmick.png",
    "ADOFAI": "ADOFAI.png",
}

# TUF 的 UQ 系列（形如 “UQ3 (U13~U16)”）与 Q0~Q4 是同一张图标。
_UQ_RE = re.compile(r"^UQ(\d)\b")

_ICON_CACHE: dict[tuple[str, int], QPixmap | None] = {}


def icon_filename(difficulty: str) -> str | None:
    """返回难度对应的图标文件名；没有素材时返回 None。"""
    name = (difficulty or "").strip()
    filename = DIFFICULTY_ICONS.get(name)
    if filename is not None:
        return filename
    match = _UQ_RE.match(name)
    if match:
        return f"Q{match.group(1)}.png"
    return None


def difficulty_pixmap(difficulty: str, size: int) -> QPixmap | None:
    """按逻辑尺寸取缩放后的图标，结果按（文件名, 尺寸）缓存。"""
    filename = icon_filename(difficulty)
    if filename is None:
        return None
    size = max(1, int(size))
    key = (filename, size)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    pixmap: QPixmap | None = None
    path = DIFFICULTY_ICON_DIR / filename
    if path.is_file():
        loaded = QPixmap(str(path))
        if not loaded.isNull():
            pixmap = loaded.scaled(
                size,
                size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
    _ICON_CACHE[key] = pixmap
    return pixmap


def clear_icon_cache() -> None:
    _ICON_CACHE.clear()
