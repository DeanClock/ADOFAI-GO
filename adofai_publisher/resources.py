from __future__ import annotations

import sys
from pathlib import Path


def resource_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "adofai_publisher"
    return Path(__file__).resolve().parent


def icon_path(name: str = "app-icon.ico") -> Path:
    return resource_dir() / "assets" / "icons" / name
