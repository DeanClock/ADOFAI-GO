from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STRICT_VALUES = {"strict", "1", "true", "judge_strict"}


@dataclass
class SavCheck:
    path: Path | None
    found: bool
    strict: bool | None
    raw_value: Any
    message: str


def default_save_paths() -> list[Path]:
    local_low = os.environ.get("USERPROFILE") or str(Path.home())
    return [
        Path(local_low) / "AppData" / "LocalLow" / "7thBeat" / "A Dance of Fire and Ice" / "data.sav",
        Path(local_low) / "AppData" / "LocalLow" / "7thBeat" / "A Dance of Fire and Ice" / "saves" / "data.sav",
    ]


def find_sav(override: str | None = None) -> Path | None:
    if override:
        path = Path(override)
        if path.is_file():
            return path
    for path in default_save_paths():
        if path.is_file():
            return path
    return None


def _extract_judge_mode(data: Any) -> Any:
    if not isinstance(data, dict):
        return None
    for key in ("judgeMode", "judge", "judgementMode", "judgmentMode", "perfectMode"):
        if key in data:
            return data[key]
    for value in data.values():
        if isinstance(value, dict):
            found = _extract_judge_mode(value)
            if found is not None:
                return found
    return None


def check_strict(override: str | None = None) -> SavCheck:
    path = find_sav(override)
    if path is None:
        return SavCheck(None, False, None, None, "没有找到 data.sav，请在设置里手动指定路径")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return SavCheck(path, True, None, None, f"读取 data.sav 失败：{exc}")
    value = _extract_judge_mode(data)
    if value is None:
        return SavCheck(path, True, None, None, "已读到 data.sav，但没找到判定模式字段")
    strict = str(value).strip().lower() in STRICT_VALUES
    message = "判定模式为严格" if strict else f"当前判定模式不是严格（{value}）"
    return SavCheck(path, True, strict, value, message)
