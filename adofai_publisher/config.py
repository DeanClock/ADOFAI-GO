from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .copy_snippets import default_copy_snippets, normalize_copy_snippets

APP_NAME = "AdofaiTufBridge"

DEFAULT_LEVEL_DIR = ""
DEFAULT_LEVEL_FILENAME_TEMPLATE = "{song} - {charter} ({tuf_id}).ard"


def base_dir() -> Path:
    override = os.environ.get("ADOFAI_TUF_HOME")
    if override:
        return Path(override)
    portable = _portable_root()
    if portable is not None:
        return portable / "ADOFAI GO 数据"
    root = os.environ.get("APPDATA") or str(Path.home())
    return Path(root) / APP_NAME


def _portable_root() -> Path | None:
    """exe 同目录存在 portable.txt 时，把数据放在 exe 旁边，方便内测发整包。"""
    if not getattr(sys, "frozen", False):
        return None
    try:
        root = Path(sys.executable).resolve().parent
    except OSError:
        return None
    return root if (root / "portable.txt").is_file() else None


def settings_path() -> Path:
    return base_dir() / "settings.json"


def records_path() -> Path:
    return base_dir() / "records.json"


def draft_path() -> Path:
    return base_dir() / "draft.json"


def browser_profile_dir() -> Path:
    return base_dir() / "browser_profile"


def level_dir(override: str = "") -> Path:
    path = (override or DEFAULT_LEVEL_DIR).strip()
    return Path(path) if path else base_dir() / "levels"


def preview_dir() -> Path:
    return base_dir() / "cache" / "preview"


def cover_sync_dir() -> Path:
    """4:3 副图由主图居中裁剪生成，固定放这里，避免污染用户的图片目录。"""
    return base_dir() / "cache" / "cover_sync"


def tuf_meta_cache_path() -> Path:
    """TUF 难度 / 标签 / 策展类型列表的磁盘缓存，启动时省掉三个列表请求。"""
    return base_dir() / "cache" / "tuf_meta.json"


def thumbnail_cache_dir() -> Path:
    """在线谱面曲绘缩略图的磁盘缓存，仅用于界面展示。"""
    return base_dir() / "cache" / "thumbnails"


def log_dir() -> Path:
    return base_dir() / "logs"


def organize_history_dir() -> Path:
    return base_dir() / "organize_history"


DEFAULT_TITLE_TEMPLATE = "【冰与火之舞】{song} {difficulty} 通关 {accuracy}"
DEFAULT_DESC_TEMPLATE = (
    "谱面：{song}\n"
    "曲师：{artist}\n"
    "谱师：{charter}\n"
    "难度：{difficulty} / {bpm} BPM / {duration}\n"
    "精准度：{accuracy}\n"
    "判定：-完美 {perfectMinus} · 完美 {perfect} · 完美+ {perfectPlus} · "
    "稍快 {ePerfect} · 稍慢 {lPerfect} · 提前 {earlyDouble} · 太快 {earlySingle} · "
    "太慢 {lateSingle} · 太慢！！ {lateDouble}\n\n"
    "TUF 谱面：{tuf_url}"
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "theme": "dark",
    "browser_channel": "chrome",
    "browser_profile_dir": "",
    "restore_last_draft": True,
    "default_partition": "单机游戏",
    "default_tags": ["冰与火之舞", "ADOFAI", "音游", "TUF"],
    "title_template": DEFAULT_TITLE_TEMPLATE,
    "desc_template": DEFAULT_DESC_TEMPLATE,
    "copy_snippets": default_copy_snippets(),
    "banned_words": ["外挂", "作弊器", "代打", "刷分服务"],
    "adofai_save_path": "",
    "level_dir": "",
    "level_filename_template": DEFAULT_LEVEL_FILENAME_TEMPLATE,
    "organize_levels": True,
    "auto_extract_levels": True,
    "umm_mirror": "官方直连（GitHub）",
    "umm_installer_dir": "",
    "umm_game_dir": "",
    "adofaitools_token": "",
    "bridge_auto_install": False,
    "game_hotkey_enabled": True,
    "game_hotkey": "Alt+X",
    "hotkey_default_version": 2,
    "obs_host": "127.0.0.1",
    "obs_port": 4455,
    "obs_password": "",
    "obs_exe_path": "",
    "update_repo": "DeanClock/ADOFAI-GO",
    "auto_check_updates": True,
    "publish_mode": "dual",
    # 主窗口位置：[x, y, width, height]，启动时校验后再恢复；空表示还没保存过。
    "window_rect": [],
    "window_maximized": False,
}

PUBLISH_MODES = ("dual", "bili_only")


class Settings:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings_path()
        self._data = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self) -> dict[str, Any]:
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data.update(raw)
                    # 旧版本残留或未来版本写入的未知键一律忽略，只保留当前设置项。
                    self._data = {
                        key: value
                        for key, value in self._data.items()
                        if key in DEFAULT_SETTINGS
                    }
                    self._migrate_hotkey_default(raw)
                    self._migrate_publish_mode()
                    self._migrate_copy_snippets(raw)
            except (OSError, ValueError):
                pass
        return self._data

    def _migrate_hotkey_default(self, raw: dict[str, Any]) -> None:
        try:
            version = int(raw.get("hotkey_default_version") or 1)
        except (TypeError, ValueError):
            version = 1
        if version >= 2:
            return
        if self._data.get("game_hotkey") == "Shift+Shift":
            self._data["game_hotkey"] = DEFAULT_SETTINGS["game_hotkey"]
        self._data["hotkey_default_version"] = 2

    def _migrate_publish_mode(self) -> None:
        if self._data.get("publish_mode") not in PUBLISH_MODES:
            self._data["publish_mode"] = DEFAULT_SETTINGS["publish_mode"]

    def _migrate_copy_snippets(self, raw: dict[str, Any]) -> None:
        if "copy_snippets" not in raw:
            self._data["copy_snippets"] = default_copy_snippets()
            return
        self._data["copy_snippets"] = normalize_copy_snippets(
            self._data.get("copy_snippets")
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data["hotkey_default_version"] = 2
        payload = {
            key: value
            for key, value in self._data.items()
            if key in DEFAULT_SETTINGS
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, DEFAULT_SETTINGS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def update(self, values: dict[str, Any]) -> None:
        self._data.update(values)

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)

    def reset(self) -> None:
        self._data = dict(DEFAULT_SETTINGS)
