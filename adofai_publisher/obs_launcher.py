from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, TypeVar

from .child_process import spawn_detached

logger = logging.getLogger(__name__)

OBS_PROCESS_NAME = "obs64.exe"
OBS_INSTALL_RELATIVE = Path("obs-studio") / "bin" / "64bit" / OBS_PROCESS_NAME
OBS_BIN_RELATIVE = Path("bin") / "64bit" / OBS_PROCESS_NAME
STEAM_OBS_RELATIVE = (
    Path("steamapps") / "common" / "OBS Studio" / "bin" / "64bit" / OBS_PROCESS_NAME
)
# 「找不到 OBS」时展示给用户，说明自动查找覆盖过哪些位置。
OBS_SEARCH_LOCATIONS = "PATH、Program Files、注册表、Steam 库、用户目录"

_UNINSTALL_SUBKEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
_UNINSTALL_SUBKEY_WOW = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"

_T = TypeVar("_T")


def _safe(builder: Callable[[], _T], default: _T) -> _T:
    """调用某个候选来源；任何异常都只跳过，绝不中断整体查找。"""
    try:
        return builder()
    except Exception:  # noqa: BLE001 - 单个来源失败不应让 OBS 查找整体失败
        logger.debug("查找 OBS 时跳过出错的来源", exc_info=True)
        return default


def _winreg_value(key, name: str) -> str:
    """读取注册表字符串值；读不到就返回空串。"""
    import winreg

    try:
        value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return str(value or "").strip()


def _registry_obs_entries() -> list[tuple[str, str]]:
    """从卸载表里找出 DisplayName 含 OBS 的项，返回 (InstallLocation, DisplayIcon)。

    Steam 版、装在其它盘的版本都会在这里留下记录。权限不足或键不存在时
    只是跳过该键，绝不抛异常。
    """
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:  # 非 Windows 兜底
        return []

    entries: list[tuple[str, str]] = []
    hives = (
        (winreg.HKEY_LOCAL_MACHINE, _UNINSTALL_SUBKEY),
        (winreg.HKEY_LOCAL_MACHINE, _UNINSTALL_SUBKEY_WOW),
        (winreg.HKEY_CURRENT_USER, _UNINSTALL_SUBKEY),
    )
    for hive, subkey in hives:
        try:
            root = winreg.OpenKey(hive, subkey)
        except OSError:
            continue
        try:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    item = winreg.OpenKey(root, name)
                except OSError:
                    continue
                try:
                    display_name = _winreg_value(item, "DisplayName")
                    if "obs" not in display_name.casefold():
                        continue
                    entries.append(
                        (
                            _winreg_value(item, "InstallLocation"),
                            _winreg_value(item, "DisplayIcon"),
                        )
                    )
                except OSError:
                    continue
                finally:
                    item.Close()
        except OSError:
            continue
        finally:
            root.Close()
    return entries


def _display_icon_executable(value: str) -> Path | None:
    """把 DisplayIcon 值还原成 obs64.exe 路径；不是 exe 的图标就返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith('"'):
        closing = text.rfind('"')
        text = text[1:closing] if closing > 0 else text.strip('"')
    else:
        head, sep, tail = text.rpartition(",")
        # 注册表里常写成 "C:\...\obs64.exe,0"，逗号后面是图标序号。
        if sep and tail.strip().isdigit():
            text = head.strip()
    text = text.strip().strip('"').strip()
    if not text:
        return None
    # 在非 Windows 上 Path.name 不认反斜杠，这里手动取文件名。
    file_name = text.replace("/", "\\").rsplit("\\", 1)[-1]
    if file_name.casefold() != OBS_PROCESS_NAME:
        return None
    return Path(text)


def _registry_obs_paths() -> list[Path]:
    """注册表里记录的 OBS 安装位置。"""
    paths: list[Path] = []
    for install_location, display_icon in _registry_obs_entries():
        location = str(install_location or "").strip().strip('"').strip()
        if location:
            paths.append(Path(location) / OBS_BIN_RELATIVE)
        icon = _display_icon_executable(display_icon)
        if icon is not None:
            paths.append(icon)
    return paths


def _registry_steam_path() -> str:
    """Steam 安装根目录，来自 HKCU\\Software\\Valve\\Steam 的 SteamPath。"""
    if sys.platform != "win32":
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
    except OSError:
        return ""
    try:
        return _winreg_value(key, "SteamPath")
    except OSError:
        return ""
    finally:
        key.Close()


def _steam_root() -> Path | None:
    path = _registry_steam_path()
    if path:
        return Path(path)
    fallback = os.environ.get("ProgramFiles(x86)")
    if fallback:
        return Path(fallback) / "Steam"
    return None


def _parse_library_paths(text: str) -> list[Path]:
    """解析 libraryfolders.vdf 里的所有 "path" 值。"""
    paths: list[Path] = []
    for match in re.finditer(r'"path"\s+"([^"]*)"', text or ""):
        raw = match.group(1).replace("\\\\", "\\").strip()
        if raw:
            paths.append(Path(raw))
    return paths


def _steam_obs_paths(steam_root: Path | None = None) -> list[Path]:
    """Steam 根库和 libraryfolders.vdf 里所有库目录下的 OBS Studio。"""
    root = Path(steam_root) if steam_root else _steam_root()
    if root is None:
        return []
    libraries = [root]
    vdf = root / "steamapps" / "libraryfolders.vdf"
    try:
        text = vdf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    libraries.extend(_parse_library_paths(text))
    return [library / STEAM_OBS_RELATIVE for library in libraries]


def _local_appdata_obs_path() -> Path | None:
    """用户级安装（不写入 Program Files 的 OBS）。"""
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        return None
    return Path(root) / "Programs" / OBS_INSTALL_RELATIVE


def _candidate_paths(configured_path: str = "") -> list[Path]:
    """按优先级列出可能存在的 obs64.exe；只列路径，不检查存在性。"""
    candidates: list[Path] = []

    configured = str(configured_path or "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())

    which = shutil.which(OBS_PROCESS_NAME)
    if which:
        candidates.append(Path(which))

    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        root = os.environ.get(env_name)
        if root:
            candidates.append(Path(root) / OBS_INSTALL_RELATIVE)

    candidates.extend(_safe(_registry_obs_paths, []))
    candidates.extend(_safe(_steam_obs_paths, []))
    local = _safe(_local_appdata_obs_path, None)
    if local is not None:
        candidates.append(local)

    # 去重但保留顺序：同一次安装可能同时被注册表和 Steam 库列出来。
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        try:
            key = os.path.normcase(os.path.abspath(str(candidate)))
        except (OSError, ValueError):
            key = os.path.normcase(str(candidate))
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def find_obs_executable(configured_path: str = "") -> Path | None:
    """按设置、PATH、Program Files、注册表、Steam 库、用户目录查找 OBS。"""
    for candidate in _candidate_paths(configured_path):
        try:
            if candidate.is_file():
                logger.info("找到 OBS：%s", candidate)
                return candidate
        except OSError:
            continue
    logger.info("未找到 OBS，已查找：%s", OBS_SEARCH_LOCATIONS)
    return None


def parse_tasklist_output(output: str) -> bool:
    return OBS_PROCESS_NAME in (output or "").casefold()


def is_obs_running() -> bool:
    """不依赖第三方库判断 OBS 是否正在运行。"""
    if sys.platform != "win32":
        return False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [
                "tasklist",
                "/FI",
                f"IMAGENAME eq {OBS_PROCESS_NAME}",
                "/NH",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return parse_tasklist_output(result.stdout or "")


def launch_obs(executable: Path) -> None:
    """启动 OBS；OBS 自己负责读取和保存配置，本工具不修改任何配置。"""
    exe = Path(executable)
    if not exe.is_file():
        raise FileNotFoundError(f"OBS executable not found: {exe}")
    # 用清洗过的环境启动，避免 OBS 从本工具的解包目录加载运行库、锁住临时目录。
    spawn_detached(exe, cwd=exe.parent)
