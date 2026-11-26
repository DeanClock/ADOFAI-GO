from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Mapping
from xml.sax.saxutils import escape

from .child_process import spawn_detached
from .config import Settings, base_dir

logger = logging.getLogger(__name__)

UMM_FOLDER_NAME = "UnityModManagerInstaller"
GAME_FOLDER_NAME = "A Dance of Fire and Ice"
GAME_EXE = "A Dance of Fire and Ice.exe"
MODS_DIR_NAME = "Mods"

# Steam 版 ADOFAI 的 AppID。Steam 安装时改由 Steam 拉起游戏，否则 DLC 与
# 创意工坊不可用；steam:// 协议在 Steam 未运行时会自动启动客户端并排队启动。
STEAM_APP_ID = "977950"
STEAM_RUN_GAME_URL = f"steam://rungameid/{STEAM_APP_ID}"
REFERENCE_INSTALLER_DIR = r"C:\Users\DeanClock\DeskBox\文件\box\Tools\UnityModManagerInstaller"

UMM_REPOSITORY_URL = (
    "https://raw.githubusercontent.com/newman55/unity-mod-manager/master/"
    "Repository_beta_13.json"
)
UMM_MIRRORS = {
    "官方直连（GitHub）": "",
    "gh-proxy.com": "https://gh-proxy.com/",
    "ghfast.top": "https://ghfast.top/",
    "ghproxy.net": "https://ghproxy.net/",
}
DEFAULT_UMM_MIRROR = "官方直连（GitHub）"

UMM_SOURCE_FILES = (
    "0Harmony.dll",
    "dnlib.dll",
    "Ionic.Zip.dll",
    "Newtonsoft.Json.dll",
    "System.Xml.dll",
    "UnityModManager.dll",
    "UnityModManager.exe",
    "UnityModManager.xml",
    "UnityModManagerConfig.xml",
    "winhttp_x64.dll",
    "winhttp_x86.dll",
)

# DoorstopProxy 注入：winhttp.dll 劫持游戏进程加载，doorstop_config.ini 指定要加载的
# UMM 程序集。游戏位数决定用哪份 winhttp 代理。
UMM_DOORSTOP_FILENAME = "winhttp.dll"
UMM_DOORSTOP_CONFIG = "doorstop_config.ini"
UMM_ARCH_FILES = {"x64": "winhttp_x64.dll", "x86": "winhttp_x86.dll"}

# UMM 运行库要放到 <游戏>_Data\Managed\UnityModManager\ 下，doorstop 才会加载到它。
UMM_MANAGER_DIR_NAME = "UnityModManager"

# UMM 真正的模组启用开关：UnityModManager.dll 同目录的 Params.xml（UMM 0.33 源码里
# Param.filepath = Path.GetDirectoryName(typeof(Param).Assembly.Location) + "Params.xml"）。
# 文件里每个 <Mod Id="..." Enabled="false" /> 会被 ReadModParams 写回 ModEntry，
# 未列出的模组默认启用。UMM 加载模组时不看目录名后缀，改文件夹名完全无效。
UMM_PARAMS_FILENAME = "Params.xml"
UMM_PARAMS_ROOT = "Param"
UMM_PARAMS_MODS = "ModParams"
UMM_PARAMS_MOD = "Mod"
UMM_PARAMS_ID_ATTR = "Id"
UMM_PARAMS_ENABLED_ATTR = "Enabled"
# fork（Thunderstore UMM_ADOFAI / 0.32.4）布局：运行库带 Ionic.Zip 与 Newtonsoft.Json。
UMM_MANAGER_LIBRARIES = (
    "dnlib.dll",
    "System.Xml.dll",
    "UnityModManager.dll",
    "UnityModManager.xml",
    "Ionic.Zip.dll",
    "Newtonsoft.Json.dll",
)

# 官方 0.33.x 安装器只往 Managed\UnityModManager 放这些（0Harmony 单独处理；
# System.Xml.dll 仅在游戏 Managed 里没有时才带）。Ionic.Zip / Newtonsoft.Json
# 官方运行时用不到，官方安装器也不复制。
UMM_OFFICIAL_MANAGER_LIBRARIES = (
    "dnlib.dll",
    "System.Xml.dll",
    "UnityModManager.dll",
    "UnityModManager.xml",
)

UMM_INSTALLED_MARKERS = (
    "UnityModManager.exe",
    "UnityModManager.dll",
    "0Harmony.dll",
)

# Assembly 注入：UMM 官方安装器不走 doorstop 时，直接给入口程序集打补丁，注入
# UnityModManagerNet.Injection.UnityModManagerStarter（0.12 旧式是
# UnityModManager）。这种安装方式不会在游戏根目录留下 winhttp.dll /
# doorstop_config.ini，只能靠入口程序集里的类型名判断。
# 具体 patch 哪个程序集取决于 UMM 版本（安装器 Form.cs 的逻辑）：
#   - UMM 0.33.x 用运行时 Config.xml 的 EntryPoint，ADOFAI 上是
#     <Managed>\UnityEngine.CoreModule.dll；
#   - 旧版 / 其它游戏可能用 StartingPoint（ADOFAI 的 Assembly-CSharp.dll）。
# 所以检测与还原都要遍历两个挂点指向的程序集，不能只看其中一个。
# 打补丁前安装器会把原文件备份成 <入口程序集>.original_。
UMM_ENTRY_ASSEMBLY_MARKERS = (
    b"UnityModManagerStarter",
    b"UnityModManagerNet.Injection",
    b"UnityModManager",
)
UMM_ENTRY_ASSEMBLY_FALLBACK = "Assembly-CSharp.dll"
UMM_BACKUP_SUFFIX = ".original_"

# UMM 运行时读取自身目录下的 Config.xml。优先使用安装器自带的 ADOFAI 配置；
# 下面两份只是安装器里找不到时（例如在线更新包被裁剪过）的兜底：
#   - fork 模板抄自 Thunderstore 的 ADOFAI 专用 UMM 包
#     ADOFAI_Modding-UMM_ADOFAI-1.1.0（基于 UMM 0.32.4）的 UMM/Core/Config.xml；
#   - 官方模板抄自官方 UMM 0.33.3 安装包 UnityModManagerConfig.xml 的 ADOFAI
#     GameInfo（newman55/unity-mod-manager）。
# 兜底配置写盘时会打 WARNING「需真机确认」。
#
# 两种运行库认的 mod 目录字段完全不同，不能混用：
#   - 0.32.4 fork 优先 <RelativeModsDirectory>，相对 UMM 程序集所在目录
#     （<游戏>_Data\Managed\UnityModManager）解析；它的 ModsDirectory 是 XML
#     属性，写成子元素不生效。默认值 "../Mods" 会指向 Managed\Mods，必须按
#     实际目录改写（通常 ..\..\..\Mods），并删掉无效的 ModsDirectory 子元素。
#   - 官方 0.33.x 没有 RelativeModsDirectory，只认 <ModsDirectory>，且相对
#     游戏工作目录（游戏根目录）解析。0.33.3 读不到该字段时
#     Path.Combine(cwd, null) 会抛 ArgumentNullException（path2），游戏启动即
#     崩溃——旧版把 fork 的 Rel 字段写进官方配置就是这么崩的。
ADOFAI_RELATIVE_MODS_DIRECTORY_PLACEHOLDER = "{relative_mods_directory}"
ADOFAI_MOD_INFO = "Info.json"
ADOFAI_RUNTIME_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<Config xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" Name="A Dance of Fire and Ice">
  <Folder>A Dance of Fire and Ice</Folder>
  <RelativeModsDirectory>{relative_mods_directory}</RelativeModsDirectory>
  <ModInfo>Info.json</ModInfo>
  <GameExe>A Dance of Fire and Ice.exe</GameExe>
  <EntryPoint>[UnityEngine.CoreModule.dll]UnityEngine.Application.cctor:Before</EntryPoint>
  <StartingPoint>[Assembly-CSharp.dll]ADOStartup.Startup:Before</StartingPoint>
  <UIStartingPoint>[Assembly-CSharp.dll]ADOStartup.Startup:After</UIStartingPoint>
</Config>
"""
ADOFAI_OFFICIAL_RUNTIME_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<Config xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" Name="A Dance of Fire and Ice">
  <Folder>ADOFAI</Folder>
  <ModsDirectory>Mods</ModsDirectory>
  <ModInfo>Info.json</ModInfo>
  <GameExe>A Dance of Fire and Ice.exe</GameExe>
  <EntryPoint>[UnityEngine.CoreModule.dll]UnityEngine.MonoBehaviour.cctor:Before</EntryPoint>
  <StartingPoint>[Assembly-CSharp.dll]ADOStartup.Startup:Before</StartingPoint>
  <UIStartingPoint>[Assembly-CSharp.dll]ADOStartup.Startup:After</UIStartingPoint>
  <MinimalManagerVersion>0.22.14</MinimalManagerVersion>
  <Comment>Required minimum game version 2.7.0</Comment>
</Config>
"""

# 运行时 GameInfo 的字段（UnityModManager/Config.cs 的 GameInfo 类），顺序按
# 官方 0.33.3 的 UnityModManagerConfig.xml。ModsDirectory（官方 0.33.x）与
# RelativeModsDirectory（0.32.4 fork）是互斥的两种风格，见 _config_style。
RUNTIME_CONFIG_FIELDS = (
    "Folder",
    "ModsDirectory",
    "RelativeModsDirectory",
    "ModInfo",
    "GameExe",
    "EntryPoint",
    "StartingPoint",
    "UIStartingPoint",
    "TextureReplacingPoint",
    "SessionStartPoint",
    "SessionStopPoint",
    "GameVersionPoint",
    "MinimalManagerVersion",
    "Comment",
)

# 运行时配置风格：fork 用 RelativeModsDirectory，官方 0.33.x 用 ModsDirectory。
CONFIG_STYLE_FORK = "fork"
CONFIG_STYLE_OFFICIAL = "official"
# 官方 UMM 从 0.33 起去掉了 RelativeModsDirectory（0.33.3 只认 ModsDirectory）。
UMM_OFFICIAL_MIN_VERSION = (0, 33)

DEFAULT_SETTINGS_KEYS = ("umm_game_dir", "umm_installer_dir")

# 「一键安装 UMM」自动下载安装包失败时的消息前缀；界面据此把错误标题区分为
# 「下载失败」而不是「安装失败」。
UMM_DOWNLOAD_ERROR_PREFIX = "下载 UMM 安装包失败"

# 备用下载源：UMM 官方更新包托管在 Dropbox，国内常被拒绝（WinError 10061），
# ghfast.top 之类的 GitHub 镜像也不转发 Dropbox。Thunderstore 的 ADOFAI 专用
# 包 ADOFAI_Modding/UMM_ADOFAI 直连可下，是基于 UMM 的 ADOFAI 预配置版。
THUNDERSTORE_UMM_PACKAGE_URL = (
    "https://thunderstore.io/api/experimental/package/ADOFAI_Modding/UMM_ADOFAI/"
)
THUNDERSTORE_SOURCE_LABEL = "Thunderstore"

# Thunderstore 包内是「UMM/Core/* + 根目录 winhttp.dll」布局，安装器目录要的是
# 平铺结构，这几样 Core 文件落到安装器根目录，winhttp.dll 改名成 doorstop 代理
# 找的 winhttp_x64.dll，其余文件（.so、run_umm.sh、manifest.json 等）全部丢弃。
_THUNDERSTORE_CORE_FILES = (
    "UnityModManager.dll",
    "UnityModManager.xml",
    "0Harmony.dll",
    "dnlib.dll",
    "Config.xml",
)
_THUNDERSTORE_DOORSTOP_SOURCE = "winhttp.dll"
_THUNDERSTORE_DOORSTOP_TARGET = "winhttp_x64.dll"

_MOD_DISPLAY_ORDER = [
    "ADOFAIGOBridge",
    "AdofaiTweaks",
    "ADOFAISkins",
    "CreplayMod",
    "LevelLibrary",
    "Quartz",
    "TogetherBootstrap",
    "TUFPlay",
    "YouTubeStream",
]

_MOD_DESCRIPTIONS = {
    "ADOFAIGOBridge": "ADOFAI GO 桌面端桥接，用于在游戏编辑器内直接载入本地谱面。",
    "AdofaiTweaks": "功能增强与优化设置。",
    "ADOFAISkins": "自定义谱面皮肤。",
    "CreplayMod": "谱面回放与练习辅助。",
    "LevelLibrary": "关卡库管理，本地谱面查询。",
    "Quartz": "高难度谱面实时渲染辅助。",
    "TogetherBootstrap": "多人联机引导。",
    "TUFPlay": "TUF 在线谱面播放与下载。",
    "YouTubeStream": "YouTube 直播辅助。",
}

_SKIPPED_FOLDER_PREFIXES = ("__", ".")


class UmmError(Exception):
    """UMM 相关操作失败。"""


class UmmCancelled(Exception):
    """UMM 在线更新被取消。"""


def _first_existing(*paths: Path) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def steam_path() -> Path | None:
    """从注册表读取 Steam 安装根目录。"""
    import winreg

    candidates: list[Path] = []
    for hive, key in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
    ):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, _ = winreg.QueryValueEx(handle, "SteamPath")
                candidates.append(Path(value))
        except OSError:
            pass
    return _first_existing(*candidates)


def _parse_libraryfolders(path: Path) -> list[Path]:
    """解析 libraryfolders.vdf，返回所有游戏库根目录。"""
    libraries: list[Path] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return libraries
    pattern = re.compile(r'"path"\s+"([^"]+)"')
    for match in pattern.finditer(text):
        candidate = Path(match.group(1))
        if candidate.exists():
            libraries.append(candidate)
    return libraries


def _steam_library_roots() -> list[Path]:
    """返回 Steam 根目录以及 libraryfolders.vdf 中登记的所有库目录。"""
    root = steam_path()
    if root is None:
        return []
    roots = [root]
    for library in _parse_libraryfolders(root / "steamapps" / "libraryfolders.vdf"):
        if library not in roots:
            roots.append(library)
    return roots


def _steam_game_path(library: Path) -> Path:
    return library / "steamapps" / "common" / GAME_FOLDER_NAME


def _is_steam_game_dir(game: Path) -> bool:
    """判断 game 是否为某个 Steam 库里的 ADOFAI 安装目录。

    路径先做 realpath 归一化再按不区分大小写比较；读取失败时按「不是」处理。
    """
    try:
        resolved = os.path.normcase(os.path.realpath(str(game)))
    except OSError:
        return False
    resolved_path = Path(resolved)
    if (
        resolved_path.name.casefold() != GAME_FOLDER_NAME.casefold()
        or resolved_path.parent.name.casefold() != "common"
        or resolved_path.parent.parent.name.casefold() != "steamapps"
    ):
        return False
    for library in _steam_library_roots():
        try:
            candidate = os.path.normcase(os.path.realpath(str(_steam_game_path(library))))
        except OSError:
            continue
        if resolved == candidate:
            return True
    return False


def default_game_dir() -> Path | None:
    """自动探测 ADOFAI 游戏目录（Steam 注册表 + libraryfolders.vdf）。"""
    root = steam_path()
    if root is None:
        return None
    direct = root / "steamapps" / "common" / GAME_FOLDER_NAME
    if direct.is_dir():
        return direct
    folders = _parse_libraryfolders(root / "steamapps" / "libraryfolders.vdf")
    for library in folders:
        candidate = library / "steamapps" / "common" / GAME_FOLDER_NAME
        if candidate.is_dir():
            return candidate
    return None


def is_valid_game_dir(directory: Path) -> bool:
    return directory.is_dir() and (directory / GAME_EXE).is_file()


def installer_dir() -> Path:
    return base_dir() / UMM_FOLDER_NAME


def umm_installer_source(settings: Settings | None = None) -> Path | None:
    """定位 UMM 安装器目录。

    优先级：设置里的 ``umm_installer_dir`` → 工具数据目录下的
    ``UnityModManagerInstaller`` → 旧版参考目录（仅当它确实存在时作为兼容回退）。
    目录都不存在时返回 None。
    """
    candidates: list[Path] = []
    if settings is not None:
        configured = str(settings.get("umm_installer_dir") or "").strip()
        if configured:
            candidates.append(Path(configured))
    candidates.append(installer_dir())
    reference = Path(REFERENCE_INSTALLER_DIR)
    if reference.is_dir():
        candidates.append(reference)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _http_get(
    url: str,
    *,
    timeout: float = 20.0,
    stream: bool = False,
) -> Any:
    """带超时的 GET 请求；stream=True 时返回原始响应对象。"""
    import requests

    return requests.get(url, timeout=timeout, stream=stream)


def _proxy_url(mirror: str | None, url: str) -> str:
    """把下载地址包上镜像前缀；官方直连返回原地址。"""
    prefix = (UMM_MIRRORS.get(mirror or "") or "").strip()
    if not prefix:
        return url
    return prefix.rstrip("/") + "/" + url.lstrip("/")


def umm_installer_ready(source: Path | None) -> bool:
    """安装器目录里是否具备安装所需的关键文件。"""
    if source is None:
        return False
    return (
        _installer_file(source, "UnityModManager.dll") is not None
        and _doorstop_source(source, "x64") is not None
    )


def _is_managed_installer_dir(source: Path) -> bool:
    """source 是否就是工具数据目录下由本工具维护的安装器目录。"""
    try:
        return source.resolve() == installer_dir().resolve()
    except OSError:
        return False


def umm_installer_needs_download(settings: Settings | None = None) -> bool:
    """「一键安装 UMM」是否需要先下载安装包。

    没有安装器，或工具数据目录里的安装器不完整（例如被清空、下载中断）
    都算需要下载；用户自己指定的完整安装器目录不会被替换。
    """
    source = umm_installer_source(settings)
    if source is None:
        return True
    if umm_installer_ready(source):
        return False
    return _is_managed_installer_dir(source)


def fetch_umm_update(
    mirror: str | None = None,
    *,
    progress=None,
    cancel=None,
) -> tuple[str, Path]:
    """在线拉取 UMM 最新更新包并解压到工具数据目录，返回 (版本号, 安装器目录)。

    官方仓库 JSON 走镜像加速；更新包在 Dropbox（国内一般不可达），会依次尝试
    官方直连与各镜像转发，全部失败时抛出带手动兜底提示的 UmmError。
    """
    import requests

    cancel = cancel or (lambda: False)
    progress = progress or (lambda *_: None)
    errors: list[str] = []
    mirrors = [mirror] if mirror in UMM_MIRRORS else [DEFAULT_UMM_MIRROR]
    all_mirrors = list(dict.fromkeys([*mirrors, *UMM_MIRRORS.values()]))
    repository_errors: list[str] = []
    repository_url = None
    download_url = None
    version = "未知"
    for candidate in all_mirrors:
        if cancel():
            raise UmmCancelled("已取消在线更新")
        try:
            progress("正在读取官方版本信息…", 0)
            response = _http_get(_proxy_url(candidate, UMM_REPOSITORY_URL), timeout=20.0)
            response.raise_for_status()
            payload = response.json()
            releases = payload.get("Releases") or []
            entry = next(
                (item for item in releases if item.get("Id") == "UnityModManager"),
                None,
            )
            if entry is None or not entry.get("DownloadUrl"):
                raise UmmError("官方更新信息里没有 UMM 下载地址")
            version = str(entry.get("Version") or "未知")
            download_url = str(entry["DownloadUrl"])
            repository_url = _proxy_url(candidate, UMM_REPOSITORY_URL)
            break
        except Exception as exc:
            repository_errors.append(f"{candidate}：{exc}")
    if download_url is None:
        raise UmmError("无法读取官方版本信息：\n" + "\n".join(repository_errors))
    download_errors: list[str] = []
    download_sources = [download_url] + [
        _proxy_url(name, download_url) for name in all_mirrors
    ]
    for source in dict.fromkeys(download_sources):
        if cancel():
            raise UmmCancelled("已取消在线更新")
        try:
            progress(f"正在下载 UMM v{version}…", 5)
            archive = _download_umm_zip(source, progress, cancel)
            target = installer_dir()
            target.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive) as zf:
                _extract_zip_safe(archive, target)
            try:
                archive.unlink(missing_ok=True)
            except OSError:
                pass
            logger.info("UMM 在线更新完成 v%s（%s）", version, target)
            return version, target
        except UmmCancelled:
            raise
        except Exception as exc:
            download_errors.append(f"{source}：{exc}")
            continue
    raise UmmError(
        "在线更新失败：官方更新包托管在 Dropbox（国内网络通常无法直连），"
        "镜像也无法转发。\n\n"
        "请选择：\n"
        "1. 使用能访问 Dropbox 的网络重试；\n"
        "2. 手动下载 umm_update.zip 后放到：\n"
        f"{installer_dir()}\\umm_update.zip\n"
        "   然后在页面点「从本地更新包安装」。\n\n"
        "详细错误：\n" + "\n".join(download_errors)
    )


def _download_umm_zip(
    url: str,
    progress,
    cancel,
    *,
    source_label: str = "",
) -> Path:
    """流式下载安装包到缓存目录；source_label 用于在进度里标明下载源。"""
    import requests

    label = (
        f"正在从 {source_label} 下载 UMM" if source_label else "正在下载 UMM"
    )
    response = _http_get(url, timeout=60.0, stream=True)
    response.raise_for_status()
    total = 0
    try:
        total = int(response.headers.get("content-length") or 0)
    except (TypeError, ValueError):
        total = 0
    cache_dir = base_dir() / "cache" / "umm"
    cache_dir.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(suffix=".zip", dir=cache_dir)
    os.close(fd)
    path = Path(raw_path)
    received = 0
    try:
        with path.open("wb") as out:
            for chunk in response.iter_content(chunk_size=65536):
                if cancel():
                    raise UmmCancelled("已取消在线更新")
                if not chunk:
                    continue
                out.write(chunk)
                received += len(chunk)
                if total:
                    percent = 5 + int(received * 90 / total)
                    progress(f"{label}… {received / 1024 / 1024:.1f} MB / "
                            f"{total / 1024 / 1024:.1f} MB", percent)
        if total and received < total:
            raise UmmError(f"下载不完整：{received} / {total} 字节")
        progress("下载完成，正在解压…", 95)
        return path
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _strip_zip_prefix(name: str) -> str:
    name = name.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    return name


def _thunderstore_layout_plan(names) -> list[tuple[str, str]] | None:
    """识别 Thunderstore 的 ADOFAI 专用 UMM 包布局，返回归一化计划。

    返回 ``[(压缩包条目, 安装器目录下的文件名), ...]``；不是这种布局时返回 None。
    目标文件名只取自本文件写死的白名单，压缩包里的其它条目一律丢弃，因此不会
    把任意路径写出安装器目录。
    """
    entries: dict[str, str] = {}
    for raw in names:
        name = _strip_zip_prefix(str(raw))
        if not name or name.endswith("/"):
            continue
        entries.setdefault(name.lower(), str(raw))
    marker = "umm/core/unitymodmanager.dll"
    core_prefix = None
    for key in entries:
        if key == marker or key.endswith("/" + marker):
            # 去掉文件名后是 "<包根>/umm/core/"。
            core_prefix = key[: -len("unitymodmanager.dll")]
            break
    if core_prefix is None:
        return None
    package_root = core_prefix[: -len("umm/core/")]
    plan: list[tuple[str, str]] = []
    for name in _THUNDERSTORE_CORE_FILES:
        member = entries.get(core_prefix + name.lower())
        if member is not None:
            plan.append((member, name))
    doorstop = entries.get(package_root + _THUNDERSTORE_DOORSTOP_SOURCE)
    if doorstop is not None:
        plan.append((doorstop, _THUNDERSTORE_DOORSTOP_TARGET))
    return plan


def _write_thunderstore_files(
    archive: Path,
    target: Path,
    plan: list[tuple[str, str]],
    *,
    cancel=None,
) -> None:
    """按计划把 Thunderstore 包里的文件写到安装器目录（平铺结构）。"""
    cancel = cancel or (lambda: False)
    destinations = {dest for _source, dest in plan}
    if "UnityModManager.dll" not in destinations:
        raise UmmError(
            "Thunderstore 安装包里缺少 UMM/Core/UnityModManager.dll，无法安装 UMM"
        )
    if _THUNDERSTORE_DOORSTOP_TARGET not in destinations:
        raise UmmError(
            "Thunderstore 安装包里缺少 winhttp.dll（doorstop 代理），无法安装 UMM"
        )
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    with zipfile.ZipFile(archive) as zf:
        for source_name, dest_name in plan:
            if cancel():
                raise UmmCancelled("已取消在线更新")
            dest = (target / dest_name).resolve()
            if not dest.is_relative_to(root):
                raise zipfile.BadZipFile(f"压缩包条目越界：{source_name}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(source_name) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)


def _install_umm_archive(archive: Path, target: Path, *, cancel=None) -> bool:
    """把 UMM 更新包解压到安装器目录，返回是否按 Thunderstore 布局归一化。

    Thunderstore 的 ADOFAI 包（``UMM/Core/*`` + 根目录 ``winhttp.dll``）先归一化
    成安装器目录结构；UMM 官方 ``umm_update.zip`` 等其它布局按原样安全解压。
    """
    with zipfile.ZipFile(archive) as zf:
        plan = _thunderstore_layout_plan(zf.namelist())
    if plan is None:
        _extract_zip_safe(archive, target)
        return False
    _write_thunderstore_files(archive, target, plan, cancel=cancel)
    return True


def fetch_umm_thunderstore(
    *,
    progress=None,
    cancel=None,
) -> tuple[str, Path]:
    """从 Thunderstore 下载 ADOFAI 专用 UMM 包，归一化后返回 (版本号, 安装器目录)。

    作为 ``fetch_umm_update`` 失败后的第二级备用源：Thunderstore 可直连下载，
    包内布局会归一化成安装器目录结构。版本号带来源标识，例如 ``1.1.0 (Thunderstore)``。
    """
    cancel = cancel or (lambda: False)
    progress = progress or (lambda *_: None)
    if cancel():
        raise UmmCancelled("已取消在线更新")
    progress("正在从 Thunderstore 读取 UMM 版本信息…", 0)
    try:
        response = _http_get(THUNDERSTORE_UMM_PACKAGE_URL, timeout=20.0)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise UmmError(f"无法读取 Thunderstore 版本信息：{exc}") from exc
    latest = payload.get("latest") if isinstance(payload, dict) else None
    if not isinstance(latest, dict):
        latest = {}
    version = str(latest.get("version_number") or "").strip() or "未知"
    download_url = str(latest.get("download_url") or "").strip()
    if not download_url:
        raise UmmError("Thunderstore 版本信息里没有 UMM 下载地址")
    if cancel():
        raise UmmCancelled("已取消在线更新")
    progress(f"正在从 Thunderstore 下载 UMM v{version}…", 5)
    archive = _download_umm_zip(
        download_url,
        progress,
        cancel,
        source_label=THUNDERSTORE_SOURCE_LABEL,
    )
    target = installer_dir()
    try:
        _install_umm_archive(archive, target, cancel=cancel)
    except UmmCancelled:
        raise
    except UmmError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise UmmError(f"解压 Thunderstore 安装包失败：{exc}") from exc
    finally:
        _remove_quietly(archive)
    progress("下载完成，正在解压…", 95)
    label = f"{version} ({THUNDERSTORE_SOURCE_LABEL})"
    logger.info("已从 Thunderstore 下载 UMM v%s 到 %s", version, target)
    return label, target


def install_local_update() -> Path:
    """从数据目录里的 umm_update.zip 安装到安装器目录，返回安装器目录。

    本地包可以是 UMM 官方更新包，也可以是 Thunderstore 的 UMM_ADOFAI 包
    （``UMM/Core/*`` 布局），两种布局都会归一化成安装器目录结构。
    """
    archive = installer_dir() / "umm_update.zip"
    if not archive.is_file():
        raise UmmError(
            "未找到本地更新包。\n\n"
            f"请把 umm_update.zip 放到：\n{installer_dir()}\\umm_update.zip"
        )
    try:
        _install_umm_archive(archive, installer_dir())
    except UmmError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise UmmError(f"解压本地更新包失败：{exc}") from exc
    logger.info("已从本地更新包安装 UMM 到 %s", installer_dir())
    return installer_dir()


def _as_path(value: str | Path | None) -> Path | None:
    if not value:
        return None
    return Path(value)


def _game_dir(settings: Settings) -> Path | None:
    configured = _as_path(settings.get("umm_game_dir"))
    if configured and configured.is_dir():
        return configured
    return default_game_dir()


def _open_steam_url(url: str) -> None:
    """交给系统打开 steam:// URL；Steam 未运行时由协议处理程序自动拉起。"""
    os.startfile(url)  # type: ignore[attr-defined]


def launch_game(settings: Settings) -> Path:
    """启动 ADOFAI，返回实际启动的可执行文件路径。

    Steam 安装的游戏改由 Steam 拉起，这样 DLC 与创意工坊可用；非 Steam
    安装、或 Steam 不可用时仍直接启动 exe（UMM 的 doorstop 依赖 exe 从
    游戏目录运行，Steam 启动同样满足这一点）。
    """
    game = _game_dir(settings)
    if game is None:
        raise UmmError(
            "未找到 ADOFAI 游戏目录，请先在「05 模组」页设置游戏目录"
        )
    if not is_valid_game_dir(game):
        raise UmmError(f"游戏目录不正确：{game}\n缺少 {GAME_EXE}")
    exe = game / GAME_EXE
    if _is_steam_game_dir(game):
        try:
            # 不等 Steam 客户端就绪，也不轮询进程：Steam 会自行排队启动游戏。
            _open_steam_url(STEAM_RUN_GAME_URL)
        except OSError as exc:
            logger.warning("通过 Steam 启动失败，改为直接启动：%s", exc)
        else:
            logger.info("已通过 Steam 启动 ADOFAI：%s", STEAM_RUN_GAME_URL)
            return exe
    try:
        # 用干净的环境启动，避免游戏从本工具的解包目录加载运行库、锁住临时目录。
        spawn_detached(exe, cwd=game)
    except OSError as exc:
        raise UmmError(f"启动游戏失败：{exc}") from exc
    logger.info("已启动 ADOFAI：%s", exe)
    return exe


def _mods_dir(settings: Settings) -> Path:
    game = _game_dir(settings)
    if game is None:
        raise UmmError("未找到 ADOFAI 游戏目录，请先在页面里指定游戏目录")
    return game / MODS_DIR_NAME


def _managed_dir(game: Path | None) -> Path | None:
    """定位 <游戏>_Data\\Managed；目录名按实际存在的 ``*_Data`` 匹配。"""
    if game is None:
        return None
    preferred = game / f"{GAME_FOLDER_NAME}_Data" / "Managed"
    if preferred.is_dir():
        return preferred
    try:
        candidates = sorted(game.glob("*_Data"))
    except OSError:
        return None
    for data_dir in candidates:
        managed = data_dir / "Managed"
        if managed.is_dir():
            return managed
    return None


def _manager_config_path(managed: Path) -> Path:
    return managed / UMM_MANAGER_DIR_NAME / "Config.xml"


def _umm_params_path(managed: Path) -> Path:
    """UMM 参数文件位置：与 UnityModManager.dll 同在 Managed\\UnityModManager。"""
    return managed / UMM_MANAGER_DIR_NAME / UMM_PARAMS_FILENAME


def _params_mod_container(root: ET.Element) -> ET.Element | None:
    """找到 ``Param/ModParams``；UMM 的 Params.xml 没有命名空间。"""
    if root.tag == UMM_PARAMS_MODS:
        return root
    return root.find(UMM_PARAMS_MODS)


def _find_params_mod(container: ET.Element, mod_id: str) -> ET.Element | None:
    """按 Id 找模组条目：先精确匹配，再大小写不敏感兜底。"""
    nodes = container.findall(UMM_PARAMS_MOD)
    for node in nodes:
        if node.get(UMM_PARAMS_ID_ATTR) == mod_id:
            return node
    lowered = mod_id.casefold()
    for node in nodes:
        if (node.get(UMM_PARAMS_ID_ATTR) or "").casefold() == lowered:
            return node
    return None


def _parse_params_enabled(value: str | None) -> bool:
    """XmlSerializer 的 bool 解析：缺属性或无法识别时按默认 True。"""
    text = (value or "").strip().casefold()
    if text in ("false", "0", "no", "off"):
        return False
    return True


def _read_umm_mod_states(managed: Path | None) -> dict[str, bool]:
    """读取 Params.xml 中已记录的模组开关（Id 原样作为键）。

    缺文件、缺 ``ModParams`` 都返回空映射；文件损坏只记 warning 并返回空映射，
    读取状态不能让页面崩掉。没有记录的模组在调用方按「启用」处理。
    """
    if managed is None:
        return {}
    path = _umm_params_path(managed)
    if not path.is_file():
        return {}
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, ET.ParseError) as exc:
        logger.warning("读取 UMM 参数文件失败，模组状态按默认启用处理：%s（%s）", path, exc)
        return {}
    container = _params_mod_container(root)
    if container is None:
        return {}
    states: dict[str, bool] = {}
    for node in container.findall(UMM_PARAMS_MOD):
        mod_id = (node.get(UMM_PARAMS_ID_ATTR) or "").strip()
        if mod_id:
            states[mod_id] = _parse_params_enabled(node.get(UMM_PARAMS_ENABLED_ATTR))
    return states


def _mod_state_enabled(states: Mapping[str, bool], mod_id: str) -> bool:
    """查 Params 映射：精确 Id → 大小写不敏感 → 缺条目视为启用。"""
    if mod_id in states:
        return states[mod_id]
    lowered = mod_id.casefold()
    for key, value in states.items():
        if key.casefold() == lowered:
            return value
    return True


def _write_params_file(path: Path, root: ET.Element) -> None:
    """把 Params.xml 原子写回：同目录临时文件 + os.replace，UTF-8 带声明。"""
    ET.indent(root)
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_temp = tempfile.mkstemp(
            prefix=path.stem + "-", suffix=".tmp", dir=path.parent
        )
    except OSError as exc:
        raise UmmError(f"写入 {path} 失败：{exc}") from exc
    temp = Path(raw_temp)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(temp, path)
    except OSError as exc:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise UmmError(f"写入 {path} 失败：{exc}") from exc
    logger.info("已更新 UMM 参数文件：%s", path)


def _write_umm_mod_enabled(managed: Path, mod_id: str, enabled: bool) -> None:
    """把某个模组的 Enabled 写进 Params.xml，保留其它模组与未知字段。

    文件不存在时创建最小合法 ``<Param><ModParams>…</ModParams></Param>``；
    文件损坏时抛 UmmError 且不覆盖（否则会丢掉用户所有模组设置）。
    """
    path = _umm_params_path(managed)
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            raise UmmError(f"读取 {path} 失败：{exc}") from exc
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise UmmError(
                f"UMM 参数文件无法解析，已停止写入以免覆盖：{path}\n"
                f"{exc}\n\n请先修复或删除该文件后重试（删除后所有模组恢复默认启用）。"
            ) from exc
    else:
        root = ET.Element(UMM_PARAMS_ROOT)
        ET.SubElement(root, UMM_PARAMS_MODS)
    container = _params_mod_container(root)
    if container is None:
        container = ET.SubElement(root, UMM_PARAMS_MODS)
    node = _find_params_mod(container, mod_id)
    if node is None:
        node = ET.SubElement(container, UMM_PARAMS_MOD)
        node.set(UMM_PARAMS_ID_ATTR, mod_id)
    node.set(UMM_PARAMS_ENABLED_ATTR, "true" if enabled else "false")
    _write_params_file(path, root)


def _remove_umm_mod_entry(managed: Path | None, mod_id: str) -> None:
    """卸载模组后删掉 Params.xml 里对应条目；失败只记 warning，不影响卸载。"""
    if managed is None:
        return
    path = _umm_params_path(managed)
    if not path.is_file():
        return
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, ET.ParseError) as exc:
        logger.warning("无法清理 UMM 参数文件里的模组条目：%s（%s）", path, exc)
        return
    container = _params_mod_container(root)
    if container is None:
        return
    lowered = mod_id.casefold()
    removed = False
    for node in list(container.findall(UMM_PARAMS_MOD)):
        node_id = (node.get(UMM_PARAMS_ID_ATTR) or "").strip()
        if node_id == mod_id or node_id.casefold() == lowered:
            container.remove(node)
            removed = True
    if not removed:
        return
    try:
        _write_params_file(path, root)
    except UmmError as exc:
        logger.warning("删除 UMM 参数条目失败：%s", exc)


def _assembly_name_from_point(value: str | None) -> str | None:
    """从 UMM 挂点字符串里取出程序集文件名。

    运行时 Config.xml 的挂点格式是 ``[程序集.dll]类型.方法:时机``（也兼容直接写
    ``程序集.dll``）。带路径的写法一律拒绝，保证只在 Managed 目录里找文件。
    """
    text = (value or "").strip()
    if not text:
        return None
    match = re.match(r"\[([^\]]+)\]", text)
    name = (match.group(1) if match else text).strip()
    if not name.lower().endswith(".dll"):
        return None
    if Path(name).name != name:
        return None
    return name


def _entry_assembly_candidates(managed: Path) -> list[Path]:
    """入口程序集候选：EntryPoint → StartingPoint → Assembly-CSharp.dll。

    UMM 0.33.x 的安装器在 Assembly 模式下 patch 的是 EntryPoint 指向的程序集
    （ADOFAI 上是 ``UnityEngine.CoreModule.dll``），旧版或其它游戏可能 patch
    StartingPoint 指向的程序集（ADOFAI 上是 ``Assembly-CSharp.dll``）。两个挂点
    都来自运行时 Config.xml，全部收集去重后追加默认入口程序集兜底；检测与还原
    必须遍历全部候选，不能只命中第一个。读配置失败只记日志，不影响默认回退。
    """
    names: list[str] = []
    config = _manager_config_path(managed)
    if config.is_file():
        try:
            # 官方安装器写出的 XML 可能带 BOM，用 utf-8-sig 兼容两种写法。
            root = ET.fromstring(
                config.read_text(encoding="utf-8-sig", errors="replace")
            )
        except (OSError, ET.ParseError) as exc:
            logger.warning("读取 UMM 运行时配置失败，入口程序集回退默认：%s（%s）", config, exc)
        else:
            # EntryPoint 在前：0.33.x 实际 patch 的就是它。
            for tag in ("EntryPoint", "StartingPoint"):
                name = _assembly_name_from_point(root.findtext(tag))
                if name:
                    names.append(name)
    names.append(UMM_ENTRY_ASSEMBLY_FALLBACK)
    paths: list[Path] = []
    for name in names:
        path = managed / name
        if path not in paths:
            paths.append(path)
    return paths


# 入口程序集标记扫描缓存：{规范化路径: (mtime_ns, size, 是否含标记)}。
# UnityEngine.CoreModule.dll 有几 MB，状态页每次刷新都整文件读一遍不划算；
# 文件内容变化时 mtime 或大小随之改变，键对不上就重新扫描。
_ENTRY_MARKER_CACHE: dict[str, tuple[int, int, bool]] = {}


def _entry_marker_cache_key(path: Path) -> str:
    return os.path.normcase(str(path))


def _invalidate_entry_marker(path: Path) -> None:
    _ENTRY_MARKER_CACHE.pop(_entry_marker_cache_key(path), None)


def _read_entry_assembly_bytes(path: Path) -> bytes:
    """读入口程序集全部字节；单独一层便于测试统计读盘次数。"""
    return path.read_bytes()


def _assembly_marker_state(path: Path) -> bool | None:
    """入口程序集是否含 UMM 注入的类型名；None 表示文件读不到。

    扫描结果按 (mtime_ns, size) 缓存，同一文件没变就不再读盘。
    """
    try:
        info = path.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("读取入口程序集失败：%s（%s）", path, exc)
        return None
    key = _entry_marker_cache_key(path)
    cached = _ENTRY_MARKER_CACHE.get(key)
    if cached is not None and cached[0] == info.st_mtime_ns and cached[1] == info.st_size:
        return cached[2]
    try:
        data = _read_entry_assembly_bytes(path)
    except OSError as exc:
        logger.warning("读取入口程序集失败：%s（%s）", path, exc)
        return None
    state = any(marker in data for marker in UMM_ENTRY_ASSEMBLY_MARKERS)
    _ENTRY_MARKER_CACHE[key] = (info.st_mtime_ns, info.st_size, state)
    return state


def _assembly_has_marker(path: Path) -> bool:
    """读不到时按未注入处理，仅用于状态展示。"""
    return _assembly_marker_state(path) is True


def _detect_assembly_injection(managed: Path | None) -> tuple[bool, Path | None]:
    if managed is None:
        return False, None
    for path in _entry_assembly_candidates(managed):
        if _assembly_has_marker(path):
            return True, path
    return False, None


def _restore_entry_assemblies(managed: Path) -> list[Path]:
    """把 Assembly 注入还原成原版入口程序集，返回还原的文件。

    遍历 EntryPoint / StartingPoint / 默认入口程序集全部候选（UMM 0.33 还原的是
    ``UnityEngine.CoreModule.dll``，旧版可能是 ``Assembly-CSharp.dll``），
    只有「该程序集确实含注入标记」且「同名 .original_ 备份存在」时才动文件；
    备份缺失时抛 UmmError —— 此时删掉 UMM 文件会留下打过补丁的入口程序集，
    游戏启动会报错。
    """
    restored: list[Path] = []
    for path in _entry_assembly_candidates(managed):
        backup = path.with_name(path.name + UMM_BACKUP_SUFFIX)
        state = _assembly_marker_state(path)
        if state is None:
            # 读不到就无法判断是否打过补丁；有备份时宁可停下也不误删。
            if backup.is_file():
                raise UmmError(
                    f"无法确认入口程序集是否被 UMM 补丁过：{path}\n"
                    "可能是游戏正在运行。请关闭游戏后重试。"
                )
            continue
        if not state:
            continue
        if not backup.is_file():
            raise UmmError(
                "检测到 UMM 以 Assembly 方式注入了入口程序集，但找不到原文件备份：\n"
                f"{backup}\n\n"
                "为避免游戏启动报错，已停止操作。请先用 UMM 安装器（UnityModManager.exe）"
                "的 Restore 恢复，或在 Steam 里「验证游戏文件完整性」后再重试。"
            )
        try:
            # 同卷原子替换：备份变成正式文件，旧的补丁文件被覆盖。
            os.replace(backup, path)
        except OSError as exc:
            raise UmmError(
                f"还原入口程序集失败：{path}\n{exc}\n\n请关闭游戏后重试。"
            ) from exc
        # 万一文件系统时间戳没往后走，显式让缓存失效。
        _invalidate_entry_marker(path)
        logger.info("已还原 Assembly 注入的入口程序集：%s", path)
        restored.append(path)
    return restored


def _remove_entry_backups(managed: Path) -> list[str]:
    """删除入口程序集旁残留的 .original_ 备份（还原后一般已不存在）。"""
    removed: list[str] = []
    for path in _entry_assembly_candidates(managed):
        backup = path.with_name(path.name + UMM_BACKUP_SUFFIX)
        if not backup.is_file():
            continue
        if _assembly_marker_state(path) is None:
            # 入口程序集读不到时保留备份，避免删掉唯一的还原依据。
            continue
        try:
            backup.unlink()
        except OSError as exc:
            raise UmmError(f"删除备份 {backup.name} 失败：{exc}") from exc
        removed.append(backup.name)
    return removed


def _game_architecture(game: Path) -> str:
    """读 PE 头判断游戏是 32 位还是 64 位，读不到时按 64 位处理。"""
    for name in (GAME_EXE, "UnityPlayer.dll"):
        path = game / name
        try:
            with path.open("rb") as handle:
                header = handle.read(0x40)
                if len(header) < 0x40 or header[:2] != b"MZ":
                    continue
                pe_offset = int.from_bytes(header[0x3C:0x40], "little")
                handle.seek(pe_offset)
                signature = handle.read(6)
                if len(signature) < 6 or signature[:4] != b"PE\0\0":
                    continue
                machine = int.from_bytes(signature[4:6], "little")
                if machine == 0x014C:
                    return "x86"
                if machine == 0x8664:
                    return "x64"
        except OSError:
            continue
    return "x64"


def _installer_file(source: Path, name: str) -> Path | None:
    """安装器文件位置：发布包解压根目录，或仓库源码布局的 ``lib\\`` 下。"""
    return _first_existing(source / name, source / "lib" / name)


def _harmony_source(source: Path) -> Path | None:
    """0Harmony 来源：优先安装器根目录那份，与 UMM 官方安装器一致。

    官方 0.33.3 的 ADOFAI GameInfo 没有 HarmonyVersion，安装器 Form.cs 因此不会
    取 Harmony\\2.2 子目录，而是用根目录的完整 0Harmony.dll（2.2.x）。只有根目录
    缺失时才退到 Harmony\\2.2；Harmony\\1.2 只服务 MinimalManagerVersion < 0.22
    的老游戏，ADOFAI 用不到。
    """
    return _first_existing(
        source / "0Harmony.dll",
        source / "lib" / "0Harmony.dll",
        source / "Harmony" / "2.2" / "0Harmony.dll",
        source / "lib" / "Harmony" / "2.2" / "0Harmony.dll",
    )


def _doorstop_source(source: Path, arch: str) -> tuple[str, Path] | None:
    """按位数选 winhttp 代理；首选缺失时退到另一份。"""
    order = [arch] + [key for key in UMM_ARCH_FILES if key != arch]
    for key in order:
        candidate = _installer_file(source, UMM_ARCH_FILES[key])
        if candidate is not None:
            return key, candidate
    return None


def _is_adofai_config(text: str) -> bool:
    return "a dance of fire and ice" in text.lower()


def _config_field_value(root: ET.Element, tag: str) -> str:
    """取 GameInfo / Config 的字段值：先子元素，再 XML 属性。

    fork 的 GameInfo 把 ModsDirectory 声明成属性，官方和运行时配置用子元素；
    两种写法都要能识别。
    """
    value = (root.findtext(tag) or "").strip()
    if value:
        return value
    return (root.get(tag) or "").strip()


def _config_style(root: ET.Element) -> str | None:
    """判断一份 GameInfo / Config 用的是哪种 mod 目录字段。

    ``RelativeModsDirectory`` 是 0.32.4 fork 的扩展字段，有它就是 fork 风格；
    官方 0.33.x 只有 ``ModsDirectory``。两者都没有时返回 None（由运行库决定）。
    """
    if _config_field_value(root, "RelativeModsDirectory"):
        return CONFIG_STYLE_FORK
    if _config_field_value(root, "ModsDirectory"):
        return CONFIG_STYLE_OFFICIAL
    return None


def _manager_dll_version(path: Path | None) -> tuple[int, ...] | None:
    """读 UnityModManager.dll 的 PE FileVersion（如 ``(0, 33, 3, 0)``）。

    用 Windows 版本资源 API，不执行 DLL；读不到（非 PE、缺资源、非 Windows）
    一律返回 None，由调用方退回内容扫描或保持旧行为。
    """
    if path is None:
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:  # pragma: no cover - 本项目只跑 Windows
        return None

    class _FixedFileInfo(ctypes.Structure):
        _fields_ = [
            ("dwSignature", wintypes.DWORD),
            ("dwStrucVersion", wintypes.DWORD),
            ("dwFileVersionMS", wintypes.DWORD),
            ("dwFileVersionLS", wintypes.DWORD),
            ("dwProductVersionMS", wintypes.DWORD),
            ("dwProductVersionLS", wintypes.DWORD),
            ("dwFileFlagsMask", wintypes.DWORD),
            ("dwFileFlags", wintypes.DWORD),
            ("dwFileOS", wintypes.DWORD),
            ("dwFileType", wintypes.DWORD),
            ("dwFileSubtype", wintypes.DWORD),
            ("dwFileDateMS", wintypes.DWORD),
            ("dwFileDateLS", wintypes.DWORD),
        ]

    try:
        version_api = ctypes.WinDLL("version", use_last_error=True)
        size = version_api.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not version_api.GetFileVersionInfoW(str(path), 0, size, buffer):
            return None
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version_api.VerQueryValueW(
            buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)
        ):
            return None
        if not pointer.value or length.value < ctypes.sizeof(_FixedFileInfo):
            return None
        info = _FixedFileInfo.from_address(pointer.value)
        if info.dwSignature != 0xFEEF04BD:
            return None
        return (
            info.dwFileVersionMS >> 16,
            info.dwFileVersionMS & 0xFFFF,
            info.dwFileVersionLS >> 16,
            info.dwFileVersionLS & 0xFFFF,
        )
    except (OSError, AttributeError, ValueError):
        return None


def _dll_has_relative_mods_directory(path: Path | None) -> bool | None:
    """DLL 里是否含 RelativeModsDirectory 字符串；读不到返回 None。

    这是判断 fork 能力最直接的依据：fork（含 0.32.4）二进制里有这个字段名，
    官方 0.33.3 没有。
    """
    if path is None:
        return None
    try:
        return b"RelativeModsDirectory" in path.read_bytes()
    except OSError:
        return None


def _installer_dll_style(source: Path) -> str | None:
    """按运行库自身判断配置风格：能力（是否含 Rel 字段）优先，版本兜底。"""
    dll = _installer_file(source, "UnityModManager.dll")
    if dll is None:
        return None
    has_relative = _dll_has_relative_mods_directory(dll)
    if has_relative is True:
        return CONFIG_STYLE_FORK
    version = _manager_dll_version(dll)
    if version is not None:
        return (
            CONFIG_STYLE_OFFICIAL
            if version >= UMM_OFFICIAL_MIN_VERSION
            else CONFIG_STYLE_FORK
        )
    if has_relative is False:
        return CONFIG_STYLE_OFFICIAL
    return None


def _installer_config_style(source: Path) -> str | None:
    """按安装包自带的 ADOFAI 配置判断风格：运行时 Config.xml → GameInfo。"""
    direct = _first_existing(
        source / "Config.xml",
        source / "Managed" / UMM_MANAGER_DIR_NAME / "Config.xml",
    )
    if direct is not None:
        try:
            root = ET.fromstring(
                direct.read_text(encoding="utf-8", errors="replace")
            )
        except (OSError, ET.ParseError):
            root = None
        if root is not None and _is_adofai_config(root.get("Name", "")):
            style = _config_style(root)
            if style is not None:
                return style
    catalog = _first_existing(
        source / "UnityModManagerConfig.xml",
        source / "lib" / "UnityModManagerConfig.xml",
    )
    if catalog is not None:
        try:
            root = ET.fromstring(
                catalog.read_text(encoding="utf-8", errors="replace")
            )
        except (OSError, ET.ParseError):
            root = None
        if root is not None:
            for info in root.iter("GameInfo"):
                if not _is_adofai_config(info.get("Name", "")):
                    continue
                style = _config_style(info)
                if style is not None:
                    return style
                break
    return None


def _installer_runtime_style(source: Path) -> str | None:
    """运行时配置该用哪种风格。

    以运行库能力为准（它决定 Config.xml 能否被解析），安装包自带的配置样式
    用来交叉验证；两者都判断不出时返回 None，由调用方按 fork 兜底。
    """
    config_style = _installer_config_style(source)
    dll_style = _installer_dll_style(source)
    if dll_style is not None:
        if config_style is not None and config_style != dll_style:
            logger.warning(
                "安装包配置与运行库风格不一致（配置=%s，运行库=%s），按运行库处理：%s",
                config_style,
                dll_style,
                source,
            )
        return dll_style
    return config_style


def _is_official_installer(source: Path) -> bool:
    """是否为 UMM 官方安装器目录（0.33.x 及以后）。

    官方包根目录有 UnityModManager.exe / UnityModManagerConfig.xml；
    Thunderstore 的 ADOFAI 专用 fork 包归一化后只有运行库与 Config.xml。
    """
    return (
        (source / "UnityModManager.exe").is_file()
        or (source / "UnityModManagerConfig.xml").is_file()
        or (source / "lib" / "UnityModManagerConfig.xml").is_file()
    )


def _relative_mods_directory(manager_dir: Path, mods_directory: Path) -> str:
    """算出 UMM 程序集目录到 ``<游戏根目录>\\Mods`` 的相对路径。

    UMM 的 fork 用 ``Path.Combine(<程序集所在目录>, RelativeModsDirectory)`` 解析
    mod 目录，程序集在 ``<_Data>\\Managed\\UnityModManager``，所以按实际目录
    算出的值通常是 ``..\\..\\..\\Mods``；不写死，随游戏目录结构变化。
    """
    return os.path.relpath(
        os.path.abspath(str(mods_directory)),
        start=os.path.abspath(str(manager_dir)),
    )


def _installer_runtime_config(
    source: Path,
    mods_directory: Path,
    manager_dir: Path,
    style: str,
) -> str | None:
    """从安装器里找 ADOFAI 运行时配置，并按运行库风格写出。

    优先级：
    1. 安装器直接带的运行时 ``Config.xml``（存在且 Name 是 ADOFAI 时使用，
       其它字段原样保留，只按 style 改写 mod 目录字段）；
    2. 安装器的 ``UnityModManagerConfig.xml`` 里 ADOFAI 的 ``GameInfo``，按官方
       安装器 ``ExportToGame()`` 的规则转成运行时 ``<Config>``；
    3. 都没有时返回 None，由调用方写内置兜底配置。
    """
    relative_mods = _relative_mods_directory(manager_dir, mods_directory)
    direct = _first_existing(
        source / "Config.xml",
        source / "Managed" / UMM_MANAGER_DIR_NAME / "Config.xml",
    )
    if direct is not None:
        try:
            text = direct.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if text:
            try:
                root = ET.fromstring(text)
            except ET.ParseError:
                root = None
            if root is not None and _is_adofai_config(root.get("Name", "")):
                if style == CONFIG_STYLE_OFFICIAL:
                    _apply_official_paths(root, force_mod_info=True)
                else:
                    _apply_fork_paths(root, relative_mods, force_mod_info=True)
                logger.info(
                    "使用安装器自带的运行时配置（%s 风格）：%s", style, direct
                )
                return _serialize_config_root(root)

    catalog = _first_existing(
        source / "UnityModManagerConfig.xml",
        source / "lib" / "UnityModManagerConfig.xml",
    )
    if catalog is None:
        return None
    try:
        root = ET.fromstring(catalog.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ET.ParseError):
        return None
    for info in root.iter("GameInfo"):
        if not _is_adofai_config(info.get("Name", "")):
            continue
        fields = {
            child.tag: (child.text or "").strip()
            for child in info
            if child.tag in RUNTIME_CONFIG_FIELDS
        }
        # fork 的 GameInfo 把 ModsDirectory 声明成属性；官方是子元素。
        if "ModsDirectory" not in fields:
            attribute = (info.get("ModsDirectory") or "").strip()
            if attribute:
                fields["ModsDirectory"] = attribute
        # 缺启动挂点的 GameInfo 装上去 UMM 也不会出现，退回内置兜底配置。
        required = ("EntryPoint", "StartingPoint", "UIStartingPoint")
        if not all(fields.get(key) for key in required):
            logger.warning("安装器里的 ADOFAI GameInfo 缺少启动挂点，忽略：%s", catalog)
            continue
        if style == CONFIG_STYLE_OFFICIAL:
            # 官方 0.33.x 只认 ModsDirectory（相对游戏根目录），不写 Rel。
            fields.pop("RelativeModsDirectory", None)
            fields["ModsDirectory"] = (
                (fields.get("ModsDirectory") or "").strip() or MODS_DIR_NAME
            )
        else:
            # fork 按实际目录算 Rel，并丢掉不生效的 ModsDirectory。
            fields.pop("ModsDirectory", None)
            fields["RelativeModsDirectory"] = relative_mods
        fields["ModInfo"] = ADOFAI_MOD_INFO
        logger.info(
            "使用安装器 UnityModManagerConfig.xml 里的 ADOFAI GameInfo（%s 风格）：%s",
            style,
            catalog,
        )
        return _serialize_runtime_config(info.get("Name") or GAME_FOLDER_NAME, fields)
    return None


def _serialize_runtime_config(name: str, fields: dict[str, str]) -> str:
    """把安装器的 GameInfo 字段序列化成运行时 ``<Config>``。"""
    root = ET.Element("Config", {"Name": name})
    for field in RUNTIME_CONFIG_FIELDS:
        value = fields.get(field)
        if value:
            ET.SubElement(root, field).text = value
    return _serialize_config_root(root)


def _serialize_config_root(root: ET.Element) -> str:
    """把运行时配置元素序列化成带 XML 声明的文本。"""
    ET.indent(root)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        + ET.tostring(root, encoding="unicode")
        + "\n"
    )


def _set_runtime_field(root: ET.Element, tag: str, value: str) -> None:
    """设置运行时配置字段；字段缺失时按 GameInfo 的字段顺序插入。"""
    node = root.find(tag)
    if node is None:
        node = ET.Element(tag)
        order = list(RUNTIME_CONFIG_FIELDS)
        index = order.index(tag) if tag in order else len(order)
        position = len(root)
        for offset, child in enumerate(list(root)):
            if child.tag in order and order.index(child.tag) < index:
                position = offset + 1
        root.insert(position, node)
    node.text = value


def _apply_fork_paths(
    root: ET.Element,
    relative_mods_directory: str,
    *,
    force_mod_info: bool = False,
) -> None:
    """按 0.32.4 fork 风格写 ``RelativeModsDirectory``，并保证 ``ModInfo`` 存在。

    fork 优先按 ``Path.Combine(<程序集所在目录>, RelativeModsDirectory)`` 找
    mod，程序集在 ``<_Data>\\Managed\\UnityModManager``；这里写入按实际目录算出
    的回游戏根目录 ``Mods`` 的相对路径。fork 里 ``ModsDirectory`` 只是 XML
    属性、且 ``RelativeModsDirectory`` 非空时会被忽略，旧配置里遗留的
    ``<ModsDirectory>`` 子元素一并删掉，避免误导排查。
    """
    for child in list(root):
        if child.tag == "ModsDirectory":
            root.remove(child)
    _set_runtime_field(root, "RelativeModsDirectory", relative_mods_directory)
    info = root.find("ModInfo")
    if force_mod_info or info is None or not (info.text or "").strip():
        _set_runtime_field(root, "ModInfo", ADOFAI_MOD_INFO)


def _apply_official_paths(
    root: ET.Element,
    *,
    force_mod_info: bool = False,
) -> None:
    """按官方 0.33.x 风格补全 ``ModsDirectory``，不写 ``RelativeModsDirectory``。

    官方运行库只认 ``<ModsDirectory>``（相对游戏根目录），完全没有 Rel 字段；
    把 fork 的 Rel 留在配置里不影响官方运行库（XmlSerializer 忽略未知元素），
    但会让排查混淆，转换风格时一并删掉。已有的 ModsDirectory 值保持原样
    （用户可能写了绝对路径，官方 Path.Combine 同样能解析）。
    """
    for child in list(root):
        if child.tag == "RelativeModsDirectory":
            root.remove(child)
    if not (root.findtext("ModsDirectory") or "").strip():
        _set_runtime_field(root, "ModsDirectory", MODS_DIR_NAME)
    info = root.find("ModInfo")
    if force_mod_info or info is None or not (info.text or "").strip():
        _set_runtime_field(root, "ModInfo", ADOFAI_MOD_INFO)


def _write_runtime_config(path: Path, content: str) -> None:
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise UmmError(f"写入 {path.name} 失败：{exc}") from exc


def _builtin_runtime_config(style: str, relative_mods_directory: str = "") -> str:
    """内置兜底配置：按运行库风格选模板。

    fork 模板把占位符换成相对 UMM 程序集目录的 mod 路径；官方模板的
    ``ModsDirectory=Mods`` 相对游戏根目录，不需要改写。
    """
    if style == CONFIG_STYLE_OFFICIAL:
        return ADOFAI_OFFICIAL_RUNTIME_CONFIG
    return ADOFAI_RUNTIME_CONFIG.replace(
        ADOFAI_RELATIVE_MODS_DIRECTORY_PLACEHOLDER,
        escape(relative_mods_directory),
    )


def _ensure_runtime_config(path: Path, source: Path, mods_directory: Path) -> None:
    """写运行时 Config.xml，风格跟随安装包里的 UMM 运行库。

    官方 0.33.x 只认 ``ModsDirectory``（相对游戏根目录）；0.32.4 fork 用
    ``RelativeModsDirectory``（相对 UMM 程序集目录）。已有配置按同一风格修正：
    官方风格不删 ModsDirectory、不硬塞 Rel；fork 风格重算 Rel 并删掉无效的
    ModsDirectory 子元素；配置属于另一种风格时转换过来（保留挂点等字段）。
    没有可用配置时依次用安装器的、内置兜底的。
    """
    manager_dir = path.parent
    relative_mods = _relative_mods_directory(manager_dir, mods_directory)
    style = _installer_runtime_style(source)
    if style is None:
        # 判断不出运行库能力时保持旧行为（fork 的 Rel），避免现有用户回归。
        style = CONFIG_STYLE_FORK
        logger.warning(
            "无法判断 UMM 安装包的配置风格，按 0.32.4 fork（RelativeModsDirectory）处理：%s",
            source,
        )
    if path.is_file():
        try:
            existing = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""
        if _is_adofai_config(existing):
            try:
                root = ET.fromstring(existing)
            except ET.ParseError:
                root = None
            if root is not None:
                if style == CONFIG_STYLE_OFFICIAL:
                    _apply_official_paths(root)
                    logger.info(
                        "已按官方 0.33.x 风格补全现有 UMM 运行时配置"
                        "（ModsDirectory=%s）：%s",
                        root.findtext("ModsDirectory"),
                        path,
                    )
                else:
                    _apply_fork_paths(root, relative_mods)
                    logger.info(
                        "已更新现有 UMM 运行时配置的 RelativeModsDirectory=%s：%s",
                        relative_mods,
                        path,
                    )
                _write_runtime_config(path, _serialize_config_root(root))
                return
            logger.warning("现有 UMM 运行时配置无法解析，改用安装器配置：%s", path)
    installer_config = _installer_runtime_config(
        source, mods_directory, manager_dir, style
    )
    if installer_config is not None:
        _write_runtime_config(path, installer_config)
        return
    logger.warning(
        "UMM 安装器里没有找到 ADOFAI 运行时配置，使用内置合成配置"
        "（%s 风格，挂点需真机确认）：%s",
        "官方 0.33.x" if style == CONFIG_STYLE_OFFICIAL else "0.32.4 fork",
        path,
    )
    _write_runtime_config(path, _builtin_runtime_config(style, relative_mods))


def _remove_stray_managed_mods(game: Path) -> None:
    """清理 UMM 误建在 ``<游戏>_Data\\Managed\\Mods`` 的空目录。

    UMM 用工作目录拼相对路径，没读到根目录的 Mods 时会自建空目录；里面一旦
    有文件（用户或其它工具放的）就不动，失败也不报错。
    """
    managed = _managed_dir(game)
    if managed is None:
        return
    stray = managed / MODS_DIR_NAME
    try:
        if not stray.is_dir() or any(stray.iterdir()):
            return
        stray.rmdir()
    except OSError:
        return
    logger.info("已清理 UMM 误建的空 Mods 目录：%s", stray)


def _remove_legacy_root_files(game: Path) -> list[str]:
    """清理旧版本复制到游戏根目录的 UMM 本体文件（官方布局不再放这些）。

    只删 ``UMM_SOURCE_FILES`` 名单里的文件（旧版按名单整份复制），doorstop 的
    ``winhttp.dll`` / ``doorstop_config.ini`` 不在名单里，绝不会误删。
    """
    removed: list[str] = []
    for name in UMM_SOURCE_FILES:
        path = game / name
        if not path.is_file():
            continue
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("清理根目录遗留文件失败：%s（%s）", path, exc)
            continue
        removed.append(name)
    if removed:
        logger.info("已清理游戏根目录的 UMM 遗留文件：%s", "、".join(removed))
    return removed


def _write_doorstop_config(game: Path, target: Path) -> None:
    """写 doorstop_config.ini；target_assembly 用绝对路径，避免依赖工作目录。

    字段以 UnityDoorstop 4.5.0 为准（UMM master 的 lib/winhttp_x64.dll 版本号
    4.5.0.0）。对应源码：
      - src/windows/config.c 读取 General.enabled / target_assembly /
        redirect_output_log / ignore_disable_switch
      - 同一文件读取 UnityMono.dll_search_path_override / debug_enabled /
        debug_address / debug_suspend
      - assets/windows/doorstop_config.ini 是这组键的官方模板
    Doorstop 3.x 的 ini 与本模板不同；UMM 自带的是 4.x 代理，故不兼容 3.x。
    UMM 官方安装器（UnityModManagerApp/Form.cs::InstallDoorstop）只写
    General.enabled 与 target_assembly，其余键用默认值；这里补全默认值便于排查。
    """
    content = (
        "[General]\r\n"
        "enabled = true\r\n"
        f"target_assembly = {target}\r\n"
        "redirect_output_log = false\r\n"
        "ignore_disable_switch = false\r\n"
        "\r\n"
        "[UnityMono]\r\n"
        "dll_search_path_override =\r\n"
        "debug_enabled = false\r\n"
        "debug_address = 127.0.0.1:10000\r\n"
        "debug_suspend = false\r\n"
    )
    try:
        (game / UMM_DOORSTOP_CONFIG).write_bytes(content.encode("utf-8"))
    except OSError as exc:
        raise UmmError(f"写入 {UMM_DOORSTOP_CONFIG} 失败：{exc}") from exc


def get_umm_status(settings: Settings) -> dict[str, Any]:
    """返回 UMM 安装状态；兼容 doorstop 与 Assembly 两种注入方式。

    - doorstop：根目录有 winhttp.dll + doorstop_config.ini；
    - Assembly：入口程序集字节里含 UMM 注入标记。入口程序集按运行时 Config.xml
      解析：UMM 0.33.x 的 EntryPoint 指向 ``UnityEngine.CoreModule.dll``，旧版
      可能在 StartingPoint 指向的 ``Assembly-CSharp.dll``。

    两者都可能被 UMM 官方安装器使用；只要运行库齐全且任一注入成立就算已安装。
    同时存在时按 doorstop 计（官方 DoorstopProxy 会加载同一套运行库）。
    """
    game = _game_dir(settings)
    if game is None:
        return {
            "game_dir": "",
            "installed": False,
            "injected": False,
            "doorstop_injected": False,
            "assembly_injected": False,
            "manager_installed": False,
            "mode": None,
            "entry_assembly": "",
            "state": "missing",
            "message": "未找到 ADOFAI 游戏目录",
        }
    files_copied = any((game / name).is_file() for name in UMM_INSTALLED_MARKERS)
    managed = _managed_dir(game)
    manager = managed / UMM_MANAGER_DIR_NAME / "UnityModManager.dll" if managed else None
    manager_installed = bool(manager and manager.is_file())
    doorstop_injected = (game / UMM_DOORSTOP_FILENAME).is_file() and (
        game / UMM_DOORSTOP_CONFIG
    ).is_file()
    assembly_injected, entry_assembly = _detect_assembly_injection(managed)
    injected = doorstop_injected or assembly_injected
    # 运行时不依赖根目录的 exe/dll，只要求注入成立与 Managed 下的运行库齐全。
    installed = manager_installed and injected
    mode = None
    if installed:
        state = "installed"
        if doorstop_injected:
            mode = "doorstop"
            message = "UMM 已安装，doorstop 注入完成"
        else:
            mode = "assembly"
            message = "UMM 已安装，Assembly 注入完成"
    elif files_copied or manager_installed or injected:
        state = "partial"
        missing: list[str] = []
        if not manager_installed:
            missing.append(r"Managed\UnityModManager\UnityModManager.dll")
        if not injected:
            missing.append(
                "winhttp.dll / doorstop_config.ini（doorstop）"
                "或入口程序集注入（Assembly）"
            )
        message = "UMM 文件已复制但注入未完成，缺少：" + "、".join(missing)
        logger.warning("UMM 状态不完整（%s）：%s", game, message)
    else:
        state = "none"
        message = "未安装 UMM"
    return {
        "game_dir": str(game),
        "installed": installed,
        "injected": injected,
        "doorstop_injected": doorstop_injected,
        "assembly_injected": assembly_injected,
        "manager_installed": manager_installed,
        "mode": mode,
        "entry_assembly": str(entry_assembly) if entry_assembly else "",
        "state": state,
        "message": message,
    }


def _download_installer_for_install(
    settings: Settings,
    *,
    progress,
    cancel,
) -> None:
    """「一键安装 UMM」缺安装器时自动下载安装包，解压到工具数据目录。

    先试官方源（``fetch_umm_update``），失败后自动改用 Thunderstore 备用源；
    两级都失败才报错，错误里写明两个源各自的原因，并保留手动放包的兜底提示。
    """
    progress("正在下载 UMM 安装包…", 0)
    try:
        version, target = fetch_umm_update(
            settings.get("umm_mirror"),
            progress=progress,
            cancel=cancel,
        )
    except UmmCancelled as exc:
        raise UmmError("已取消下载 UMM 安装包，未安装 UMM。") from exc
    except Exception as official_exc:
        logger.warning(
            "官方源下载 UMM 安装包失败，改用 Thunderstore 备用源：%s", official_exc
        )
        progress("官方源下载失败，正在从 Thunderstore 下载 UMM 安装包…", 0)
        try:
            version, target = fetch_umm_thunderstore(
                progress=progress,
                cancel=cancel,
            )
        except UmmCancelled as exc:
            raise UmmError("已取消下载 UMM 安装包，未安装 UMM。") from exc
        except Exception as fallback_exc:
            raise UmmError(
                f"{UMM_DOWNLOAD_ERROR_PREFIX}，未安装 UMM。\n\n"
                "已依次尝试官方源与 Thunderstore 备用源。\n\n"
                f"官方源失败原因：\n{official_exc}\n\n"
                f"Thunderstore 失败原因：\n{fallback_exc}\n\n"
                "也可以手动下载 umm_update.zip（UMM 官方更新包），或 Thunderstore 的 "
                "UMM_ADOFAI 包改名为 umm_update.zip，放到：\n"
                f"{installer_dir()}\\umm_update.zip\n"
                "然后在本页点「从本地更新包安装」，再点「一键安装 UMM」。"
            ) from fallback_exc
    logger.info("一键安装自动下载 UMM v%s 到 %s", version, target)


def install_umm(
    settings: Settings,
    *,
    progress=None,
    cancel=None,
) -> Path:
    """安装 UMM：复制本体文件，并完成 DoorstopProxy 注入，返回游戏目录。

    本地没有安装器时会先用 ``umm_mirror`` 自动下载安装包再安装，
    让「一键安装 UMM」在新机器上也能直接完成。
    """
    progress = progress or (lambda *_: None)
    cancel = cancel or (lambda: False)
    game = _game_dir(settings)
    if game is None:
        raise UmmError("未找到 ADOFAI 游戏目录，请先在页面里指定游戏目录")
    if not is_valid_game_dir(game):
        raise UmmError(f"游戏目录不正确：{game}\n缺少 {GAME_EXE}")
    source = umm_installer_source(settings)
    if (
        source is not None
        and not umm_installer_ready(source)
        and _is_managed_installer_dir(source)
    ):
        # 数据目录里的安装器被清空 / 下载中断：按缺失处理，重新自动下载。
        logger.info("工具数据目录里的 UMM 安装器不完整，重新下载：%s", source)
        source = None
    if source is None:
        _download_installer_for_install(settings, progress=progress, cancel=cancel)
        source = umm_installer_source(settings)
        if source is None:
            raise UmmError(
                "UMM 安装包已下载，但仍未找到安装器目录。\n"
                f"期望位置：{installer_dir()}\n"
                "可以把安装器解压到该目录，或在页面里指定安装器目录后重试。"
            )
    progress("正在安装 UMM…", 92)
    managed = _managed_dir(game)
    if managed is None:
        raise UmmError(
            f"游戏目录里找不到 Managed 目录：{game}\n"
            "请确认游戏安装完整，应存在 *_Data\\Managed。"
        )
    source_dll = _installer_file(source, "UnityModManager.dll")
    if source_dll is None:
        raise UmmError(
            f"UMM 安装器目录里缺少 UnityModManager.dll：{source}\n"
            "请重新下载完整的 UMM 安装器。"
        )
    doorstop = _doorstop_source(source, _game_architecture(game))
    if doorstop is None:
        raise UmmError(
            f"UMM 安装器目录里缺少 winhttp_x64.dll / winhttp_x86.dll：{source}\n"
            "没有代理 DLL，无法完成 doorstop 注入。"
        )

    # 已有 Assembly 注入时不能再叠加 doorstop，否则 UMM 会双重加载。按官方
    # Restore 的做法先还原入口程序集（EntryPoint / StartingPoint 指向的都查）；
    # 缺备份时在写任何文件之前就报错，不留半套。
    restored = _restore_entry_assemblies(managed)
    if restored:
        logger.info("安装前已切回未注入状态（Assembly → 原版入口程序集）")

    mods_dir = game / MODS_DIR_NAME
    mods_dir.mkdir(parents=True, exist_ok=True)

    # 官方安装器（0.33.x）不往游戏根目录放 UMM 本体，只放 doorstop；fork 包
    # 保持旧布局，根目录复制一份安装器文件（历史行为，不影响运行）。
    official = _is_official_installer(source)
    copied: list[str] = []
    harmony = _harmony_source(source)
    if not official:
        for name in UMM_SOURCE_FILES:
            src = source / name
            if not src.is_file():
                continue
            try:
                shutil.copy2(src, game / name)
            except OSError as exc:
                raise UmmError(f"复制 {name} 失败：{exc}") from exc
            copied.append(name)
        if harmony is not None:
            try:
                shutil.copy2(harmony, game / "0Harmony.dll")
            except OSError as exc:
                raise UmmError(f"复制 0Harmony.dll 失败：{exc}") from exc
        if not copied and not (game / "UnityModManager.exe").exists():
            raise UmmError("UMM 安装器目录里没有找到可复制的文件")

    # 2. 运行库与运行时配置放进 Managed\UnityModManager。
    manager_dir = managed / UMM_MANAGER_DIR_NAME
    try:
        manager_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UmmError(f"创建 {manager_dir} 失败：{exc}") from exc
    manager_copied: list[str] = []
    libraries = (
        UMM_OFFICIAL_MANAGER_LIBRARIES if official else UMM_MANAGER_LIBRARIES
    )
    for name in libraries:
        if name == "System.Xml.dll" and (managed / name).is_file():
            # 游戏 Managed 里已有 System.Xml，UMM 官方安装器也会跳过。
            continue
        src = _installer_file(source, name)
        if src is None:
            continue
        try:
            shutil.copy2(src, manager_dir / name)
        except OSError as exc:
            raise UmmError(f"复制 {name} 到 {manager_dir} 失败：{exc}") from exc
        manager_copied.append(name)
    if harmony is not None:
        try:
            shutil.copy2(harmony, manager_dir / "0Harmony.dll")
        except OSError as exc:
            raise UmmError(f"复制 0Harmony.dll 到 {manager_dir} 失败：{exc}") from exc
        manager_copied.append("0Harmony.dll")
    if not (manager_dir / "UnityModManager.dll").is_file():
        raise UmmError(f"复制 UnityModManager.dll 到 {manager_dir} 失败")
    # 运行时配置按安装包里的运行库风格生成：官方 0.33.x 写 ModsDirectory，
    # 0.32.4 fork 写 RelativeModsDirectory；写错字段游戏启动/读模组都会出问题。
    _ensure_runtime_config(manager_dir / "Config.xml", source, mods_dir)

    # 3. 最后落 doorstop：先配置文件，再放代理 DLL；缺少任意一个都不会影响游戏启动。
    _write_doorstop_config(game, (manager_dir / "UnityModManager.dll").resolve())
    arch, proxy = doorstop
    try:
        shutil.copy2(proxy, game / UMM_DOORSTOP_FILENAME)
    except OSError as exc:
        raise UmmError(f"复制 {UMM_DOORSTOP_FILENAME} 失败：{exc}") from exc

    # 4. 官方布局不往根目录放本体；清掉旧版本复制过去的遗留副本（只动名单里的
    # 文件，不碰用户自己的文件），再清掉旧版 UMM 误建的空 Managed\Mods。
    if official:
        _remove_legacy_root_files(game)
    _remove_stray_managed_mods(game)
    logger.info(
        "UMM 已安装到 %s（%s 布局，根目录 %d 个文件，运行库 %d 个，doorstop=%s）",
        game,
        "官方" if official else "fork",
        len(copied),
        len(manager_copied),
        arch,
    )
    return game


def uninstall_umm(settings: Settings) -> None:
    """删除 UMM 文件与注入（不动 Mods、Managed 下其它文件与游戏本体）。

    Assembly 注入会先还原入口程序集（UMM 0.33 是 EntryPoint 指向的
    ``UnityEngine.CoreModule.dll``，旧版可能是 ``Assembly-CSharp.dll``）：只删
    UnityModManager 目录、留下打过补丁的程序集会让游戏启动报错。还原失败或缺少
    备份时直接报错，不在删了一半文件的状态下收手。
    """
    game = _game_dir(settings)
    if game is None:
        raise UmmError("未找到 ADOFAI 游戏目录")
    managed = _managed_dir(game)
    backups: list[str] = []
    if managed is not None:
        # 还原必须在删运行库之前；备份也要在这里处理，因为候选路径依赖
        # Managed\UnityModManager\Config.xml，删掉目录后就看不到了。
        _restore_entry_assemblies(managed)
        backups = _remove_entry_backups(managed)
    removed: list[str] = []
    for name in dict.fromkeys(
        (*UMM_SOURCE_FILES, UMM_DOORSTOP_FILENAME, UMM_DOORSTOP_CONFIG)
    ):
        target = game / name
        if target.is_file():
            try:
                target.unlink()
            except OSError as exc:
                raise UmmError(f"删除 {name} 失败：{exc}") from exc
            removed.append(name)
    if managed is not None:
        manager_dir = managed / UMM_MANAGER_DIR_NAME
        if manager_dir.is_dir():
            try:
                shutil.rmtree(manager_dir)
            except OSError as exc:
                raise UmmError(f"删除 {UMM_MANAGER_DIR_NAME} 目录失败：{exc}") from exc
            removed.append(UMM_MANAGER_DIR_NAME)
    removed.extend(backups)
    _remove_stray_managed_mods(game)
    logger.info("UMM 已从 %s 卸载（%d 项）", game, len(removed))


def _read_info_json(directory: Path) -> dict[str, Any]:
    info = directory / "Info.json"
    try:
        raw = json.loads(info.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _version_sort_key(version: str) -> tuple[int, tuple[int, ...], str]:
    """版本号排序键：按数字段逐个比较，空版本排最后。

    例如 ``2.10 > 2.9``、``2.17 > 2.14``、``2.1.1 > 2.0.0-alpha-137``；
    数字段相同时用原字符串做稳定兜底，不依赖 packaging。
    """
    text = str(version or "").strip()
    numbers = tuple(int(part) for part in re.findall(r"\d+", text))
    # 没有数字段的版本（含空串）排在有数字段的版本之后。
    return (1 if not numbers else 0, numbers, text.lower())


def list_mods(settings: Settings) -> list[dict[str, Any]]:
    """扫描安装器目录与游戏 Mods 目录，返回 mod 列表（已安装的与安装器合并）。"""
    source = umm_installer_source(settings)
    mods: list[dict[str, Any]] = []
    seen: set[str] = set()
    if source is not None:
        adofai_root = source / "ADOFAI"
        installer_dirs = (
            sorted(adofai_root.iterdir(), key=lambda p: p.name.lower())
            if adofai_root.is_dir()
            else []
        )
        for mod_dir in installer_dirs:
            if not mod_dir.is_dir() or mod_dir.name.startswith(_SKIPPED_FOLDER_PREFIXES):
                continue
            mod = _mod_from_installer_folder(mod_dir)
            if mod is not None:
                mods.append(mod)
                seen.add(mod["id"])
    game = _game_dir(settings)
    if game is not None:
        # UMM 只认 Params.xml 里的开关；整次扫描只读一次，避免逐个模组重复解析。
        mod_states = _read_umm_mod_states(_managed_dir(game))
        mods_dir = game / MODS_DIR_NAME
        if mods_dir.is_dir():
            for mod_dir in sorted(mods_dir.iterdir(), key=lambda p: p.name.lower()):
                if not mod_dir.is_dir() or mod_dir.name.startswith(_SKIPPED_FOLDER_PREFIXES):
                    continue
                info = _read_info_json(mod_dir)
                mod_id = str(info.get("Id") or mod_dir.name)
                installed_entry = next(
                    (mod for mod in mods if mod["id"] == mod_id), None
                )
                if installed_entry is not None:
                    installed_entry["installed"] = True
                    installed_entry["enabled"] = _mod_enabled(mod_dir, mod_states)
                    installed_entry["source"] = str(mod_dir)
                    # 以实际安装目录的 Info.json 为准，避免沿用安装器旧压缩包的版本。
                    installed_version = str(info.get("Version") or "").strip()
                    if installed_version:
                        installed_entry["version"] = installed_version
                    installed_name = str(info.get("DisplayName") or "").strip()
                    if installed_name:
                        installed_entry["display_name"] = installed_name
                    installed_author = str(info.get("Author") or "").strip()
                    if installed_author:
                        installed_entry["author"] = installed_author
                    continue
                if not info:
                    continue
                mods.append(
                    {
                        "id": mod_id,
                        "display_name": str(info.get("DisplayName") or mod_id),
                        "author": str(info.get("Author") or ""),
                        "version": str(info.get("Version") or ""),
                        "source": str(mod_dir),
                        "installed": True,
                        "enabled": _mod_enabled(mod_dir, mod_states),
                        "has_archive": False,
                        "archives": [],
                        "description": _MOD_DESCRIPTIONS.get(mod_id, ""),
                    }
                )
    mods.sort(key=lambda m: (_MOD_DISPLAY_ORDER.index(m["id"]) if m["id"] in _MOD_DISPLAY_ORDER else 99, m["id"].lower()))
    return mods


def _read_archive_info(archive: Path) -> dict[str, Any]:
    """只读压缩包里的 Info.json，不解压其余内容。"""
    try:
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                if not name.lower().endswith("/info.json"):
                    continue
                try:
                    raw = json.loads(zf.read(name).decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if isinstance(raw, dict):
                    return raw
    except (OSError, zipfile.BadZipFile):
        pass
    return {}


def _mod_from_installer_folder(mod_dir: Path) -> dict[str, Any] | None:
    """读取安装器目录下某个 mod 的所有压缩包，取版本号最新的 Info.json。"""
    archives = sorted(
        (path for path in mod_dir.iterdir() if path.suffix.lower() in {".zip", ".adozip"}),
        key=lambda p: p.name.lower(),
    )
    if not archives:
        return None
    best: dict[str, Any] = {}
    best_key: tuple[int, tuple[int, ...], str] | None = None
    fallback: dict[str, Any] = {}
    for archive in archives:
        candidate = _read_archive_info(archive)
        if not candidate:
            continue
        if not fallback:
            fallback = candidate
        key = _version_sort_key(str(candidate.get("Version") or ""))
        # 没有版本号的包只能兜底，不能挤掉有版本号的包。
        if key[0] != 0:
            continue
        if best_key is None or key > best_key:
            best, best_key = candidate, key
    info = best or fallback
    mod_id = str(info.get("Id") or mod_dir.name)
    return {
        "id": mod_id,
        "display_name": str(info.get("DisplayName") or mod_id),
        "author": str(info.get("Author") or ""),
        "version": str(info.get("Version") or ""),
        "source": str(mod_dir),
        "installed": False,
        "enabled": False,
        "has_archive": True,
        "archives": [str(archive) for archive in archives],
        "description": _MOD_DESCRIPTIONS.get(mod_id, ""),
    }


def _mod_id_from_folder(mod_dir: Path) -> str:
    """模组 Id：优先 Info.json 的 Id；旧版 `.disabled` 目录回退到去掉后缀的目录名。"""
    info = _read_info_json(mod_dir)
    real_id = str(info.get("Id") or "").strip()
    if real_id:
        return real_id
    name = mod_dir.name
    if name.lower().endswith(".disabled"):
        name = name[: -len(".disabled")]
    return name


def _mod_enabled(mod_dir: Path, states: Mapping[str, bool]) -> bool:
    """mod 是否启用：有 Info.json，且 Params.xml 没有把它标成禁用。"""
    if not (mod_dir / "Info.json").is_file():
        return False
    return _mod_state_enabled(states, _mod_id_from_folder(mod_dir))


def install_mod(settings: Settings, mod_id: str, archive_path: str | None = None) -> dict[str, Any]:
    """把某个 mod 的 zip 解压到游戏 Mods 目录，返回安装后的描述。"""
    mod = _find_mod(settings, mod_id)
    if mod is None:
        raise UmmError(f"找不到模组 {mod_id}")
    source_path = Path(archive_path or (mod["archives"][0] if mod["archives"] else "")).resolve()
    result = install_mod_archive(settings, mod_id, source_path)
    result["archives"] = mod["archives"]
    return result


def install_mod_archive(
    settings: Settings,
    mod_id: str,
    source_path: Path,
    *,
    display_name: str = "",
    author: str = "",
    version: str = "",
) -> dict[str, Any]:
    """把任意本地压缩包解压到 Mods\\<mod_id>，返回安装后的描述。"""
    source_path = Path(source_path).resolve()
    if not source_path.is_file():
        raise UmmError(f"找不到模组压缩包：{source_path}")
    mods_dir = _mods_dir(settings)
    mods_dir.mkdir(parents=True, exist_ok=True)
    target = mods_dir / mod_id
    existing = resolve_mod_dir(settings, mod_id)
    if existing is not None:
        raise UmmError(f"模组 {mod_id} 已安装，请先卸载再重新安装")
    try:
        _extract_zip_safe(source_path, target)
    except (OSError, zipfile.BadZipFile) as exc:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        raise UmmError(f"解压 {source_path.name} 失败：{exc}") from exc
    info = _read_info_json(target)
    real_id = str(info.get("Id") or "").strip()
    if real_id and real_id != mod_id and target.is_dir():
        desired = mods_dir / real_id
        if not desired.exists():
            try:
                target.rename(desired)
                target = desired
            except OSError:
                pass
    info = _read_info_json(target)
    final_id = real_id or mod_id
    # 旧版禁用实现可能留下 Enabled=false 的同 Id 条目；新装模组要能直接加载。
    managed = _managed_dir(mods_dir.parent)
    if managed is not None:
        try:
            _write_umm_mod_enabled(managed, final_id, True)
        except UmmError as exc:
            logger.warning("模组 %s 已安装，但未能写入 UMM 开关：%s", final_id, exc)
    return {
        "id": final_id,
        "display_name": str(info.get("DisplayName") or display_name or mod_id),
        "author": str(info.get("Author") or author or ""),
        "version": str(info.get("Version") or version or ""),
        "source": str(target),
        "installed": True,
        "enabled": True,
        "has_archive": True,
        "archives": [],
        "description": _MOD_DESCRIPTIONS.get(final_id, ""),
    }


def resolve_mod_dir(settings: Settings, mod_id: str) -> Path | None:
    """按 Info.json 的 Id 定位真实目录，目录名可能与 Id 不一致。"""
    mods_dir = _mods_dir(settings)
    if not mods_dir.is_dir():
        return None
    direct = mods_dir / mod_id
    if direct.is_dir() and str(_read_info_json(direct).get("Id") or mod_id) == mod_id:
        return direct
    for child in sorted(mods_dir.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        name = child.name
        base = name[: -len(".disabled")] if name.lower().endswith(".disabled") else name
        if base == mod_id:
            return child
        if str(_read_info_json(child).get("Id") or "") == mod_id:
            return child
    return None


def uninstall_mod(settings: Settings, mod_id: str) -> None:
    target = resolve_mod_dir(settings, mod_id)
    if target is None:
        raise UmmError(f"没有找到模组 {mod_id} 的安装目录，可能已经卸载")
    real_id = _mod_id_from_folder(target)
    try:
        shutil.rmtree(target)
    except OSError as exc:
        raise UmmError(f"删除 {target} 失败：{exc}") from exc
    if target.exists():
        raise UmmError(f"删除 {target} 失败，请关闭游戏后重试")
    _remove_umm_mod_entry(_managed_dir(_game_dir(settings)), real_id)


def _normalize_disabled_folder(mod_dir: Path) -> Path:
    """把旧实现留下的 ``<目录>.disabled`` 改回原名；失败只记 warning。

    UMM 不认目录名后缀，留着也能加载；这里只是把用户机器上的历史状态理顺。
    游戏占用目录时 rename 会失败，此时不影响已经写入 Params.xml 的开关。
    """
    if not mod_dir.name.lower().endswith(".disabled"):
        return mod_dir
    target = mod_dir.with_name(mod_dir.name[: -len(".disabled")])
    if target.exists():
        logger.warning("旧禁用目录未归一化，同名目标已存在：%s", target)
        return mod_dir
    try:
        mod_dir.rename(target)
    except OSError as exc:
        logger.warning("旧禁用目录改名失败（不影响 UMM 开关）：%s（%s）", mod_dir, exc)
        return mod_dir
    logger.info("旧禁用目录已改回原名：%s", target)
    return target


def set_mod_enabled(settings: Settings, mod_id: str, enabled: bool) -> None:
    """启用/禁用 mod：写 UMM 的 Params.xml，目录名保持不变。

    UMM 0.33 的 ModManager 遍历 Mods 下所有子目录加载模组，不看 ``.disabled``
    后缀，所以旧实现的改名方案无效；真正的开关是 Params.xml 里的 Enabled。
    """
    current = resolve_mod_dir(settings, mod_id)
    if current is None:
        raise UmmError(f"模组 {mod_id} 未安装")
    game = _game_dir(settings)
    managed = _managed_dir(game) if game is not None else None
    if managed is None:
        raise UmmError(
            "找不到 UMM 运行库目录（<游戏>_Data\\Managed\\UnityModManager），"
            "无法写入模组开关。\n请先安装 UMM 后重试。"
        )
    _write_umm_mod_enabled(managed, _mod_id_from_folder(current), enabled)
    _normalize_disabled_folder(current)


def _find_mod(settings: Settings, mod_id: str) -> dict[str, Any] | None:
    for mod in list_mods(settings):
        if mod["id"] == mod_id:
            return mod
    return None


def _extract_zip_safe(archive: Path, target: Path) -> None:
    """安全解压：拒绝 ..、绝对路径与符号链接条目；顶层单目录且含 Info.json 时拍平。"""
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            name = member.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                raise zipfile.BadZipFile(f"不安全的压缩包条目：{member.filename}")
            dest = (target / name).resolve()
            if not dest.is_relative_to(target.resolve()):
                raise zipfile.BadZipFile(f"压缩包条目越界：{member.filename}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
    _flatten_single_top_dir(target)


def _flatten_single_top_dir(target: Path) -> None:
    """若目标目录里只有一个顶层目录且其中直接含 Info.json，则把内容上移一层。"""
    children = [child for child in target.iterdir() if child.is_dir()]
    if len(children) != 1:
        return
    single = children[0]
    if not (single / "Info.json").is_file():
        return
    for child in list(single.iterdir()):
        try:
            shutil.move(str(child), str(target / child.name))
        except OSError:
            return
    try:
        single.rmdir()
    except OSError:
        pass

