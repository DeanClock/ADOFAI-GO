"""启动外部子进程时的环境清洗与 DLL 搜索路径处理。

背景（PyInstaller onefile）：onefile 模式运行时，bootloader 会把自己解包到
``%TEMP%\\_MEIxxxxxx``，外部程序（OBS、ADOFAI 本体）如果从那里加载运行库，父进程
退出时 bootloader 就删不掉自己的临时目录，弹出「Failed to remove temporary
directory: ...\\_MEIxxxxxx」。

牵连外部程序的机制有两种：

1. 环境变量：PyInstaller 会注入 ``PYTHONHOME`` / ``PYTHONPATH``，历史上也会把
   解包目录塞进 ``PATH``。这里复制一份环境，剔除 ``_MEI`` 相关路径和这些变量，
   让子进程的环境保持干净。
2. DLL 搜索目录（v0.17.4 仍会复现的真正原因）：PyInstaller 6 的引导器在每个进程里
   都调用 ``SetDllDirectoryW(_MEI)``，而 Windows 会把这项设置**随 CreateProcess
   传给子进程**（PyInstaller 引导器源码 ``pyi_main.c`` 的注释写明该修改
   "is inherited by child processes"）。搜索顺序里这项目录排在 System32 之前，
   于是 obs64.exe 调用 LoadLibrary 拿 VCRUNTIME140.dll 时会先命中 ``_MEI`` 里的副本，
   把它映射进自己的地址空间，锁住本工具的临时目录。仅清洗 PATH 对此无效。
   因此 ``spawn_detached()`` 在创建子进程前把该目录清空（``SetDllDirectoryW(NULL)``），
   创建完立即恢复，保证本进程后续仍能从 ``_MEI`` 加载自己的运行库。

源码运行时 ``sys._MEIPASS`` 不存在、``SetDllDirectoryW`` 也没有被设置，两处清洗都是
无操作，不会影响开发调试。
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Mapping

logger = logging.getLogger(__name__)

# PyInstaller 会注入、但不应该传给外部程序的变量。
_DROPPED_VARIABLES = ("PYTHONHOME", "PYTHONPATH", "_MEIPASS2")
# 外部程序（OBS）依赖、而系统可能缺失的 VC 运行库；缺失时用 _MEI 里的副本兜底。
_RUNTIME_DLL_NAMES = ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll")
# PyInstaller 解包目录的前缀，例如 _MEI00004c842。
_MEI_PREFIX = "_mei"
# SetDllDirectoryW/GetDllDirectoryW 的路径缓冲；Windows 路径上限 32767 个字符。
_DLL_DIRECTORY_BUFFER = 32768
# 清空 DLL 搜索目录是进程级状态，用锁避免多线程同时改。
_DLL_DIRECTORY_LOCK = threading.Lock()


def _meipass_dir() -> str | None:
    """返回当前 PyInstaller 解包目录；源码运行时返回 None。"""
    value = getattr(sys, "_MEIPASS", None)
    return str(value) if value else None


def _normalize(entry: str) -> str:
    """统一路径写法，便于做大小写不敏感、忽略结尾分隔符的比较。"""
    text = entry.strip().strip('"').replace("/", "\\")
    return text.rstrip("\\").casefold()


def _is_mei_entry(entry: str, meipass: str) -> bool:
    """判断 PATH 条目是否指向 PyInstaller 的 _MEI 解包目录（或其子目录）。"""
    normalized = _normalize(entry)
    if not normalized:
        return False
    target = _normalize(meipass)
    if target and (normalized == target or normalized.startswith(target + "\\")):
        return True
    name = normalized.rsplit("\\", 1)[-1]
    return name.startswith(_MEI_PREFIX) and len(name) > len(_MEI_PREFIX)


def _find_path_key(env: Mapping[str, str]) -> str | None:
    """大小写不敏感地找到 PATH 的键名（Windows 下常见 Path / PATH 混用）。"""
    for key in env:
        if key.casefold() == "path":
            return key
    return None


def _split_path(value: str) -> list[str]:
    return [entry for entry in value.split(os.pathsep) if entry.strip()]


def clean_child_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """返回适合传给外部程序的环境变量副本。

    - 剔除 PATH 里等于 / 位于 ``sys._MEIPASS`` 下、以及任何 ``_MEI`` 临时目录的条目；
    - 丢弃 ``PYTHONHOME`` / ``PYTHONPATH`` / ``_MEIPASS2``；
    - 源码运行（没有 ``sys._MEIPASS``）时 PATH 原样保留，不会报错。
    """
    source = dict(os.environ if env is None else env)

    dropped = {name.casefold() for name in _DROPPED_VARIABLES}
    for key in [key for key in source if key.casefold() in dropped]:
        del source[key]

    meipass = _meipass_dir()
    if meipass:
        path_key = _find_path_key(source)
        if path_key is not None:
            kept = [
                entry
                for entry in _split_path(source[path_key])
                if not _is_mei_entry(entry, meipass)
            ]
            source[path_key] = os.pathsep.join(kept)
    return source


def _kernel32():
    """取 kernel32 句柄；非 Windows 或取不到时返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        return ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return None


def current_dll_directory() -> str | None:
    """读取本进程当前的 DLL 搜索目录（``SetDllDirectoryW`` 设置的值）。

    没有设置、非 Windows、或系统不提供 ``GetDllDirectoryW`` 时返回 None。
    """
    kernel32 = _kernel32()
    if kernel32 is None:
        return None
    getter = getattr(kernel32, "GetDllDirectoryW", None)
    if getter is None:
        return _meipass_dir()
    getter.argtypes = [ctypes.c_uint32, ctypes.c_wchar_p]
    getter.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(_DLL_DIRECTORY_BUFFER)
    length = getter(_DLL_DIRECTORY_BUFFER, buffer)
    if not length:
        return None
    return buffer.value or None


def set_dll_directory(path: str | None) -> bool:
    """设置本进程的 DLL 搜索目录；``None`` 表示清空。成功返回 True。"""
    kernel32 = _kernel32()
    if kernel32 is None:
        return False
    setter = getattr(kernel32, "SetDllDirectoryW", None)
    if setter is None:
        return False
    setter.argtypes = [ctypes.c_wchar_p]
    setter.restype = ctypes.c_int
    try:
        return bool(setter(path))
    except OSError:
        return False


def system_has_vc_runtime() -> bool:
    """系统目录里是否有 x64 版 VC 运行库（OBS 的 obs64.exe 依赖它）。"""
    if sys.platform != "win32":
        return True
    system32 = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32"
    return (system32 / "vcruntime140.dll").is_file()


def vc_runtime_guard_dir(
    meipass: str | Path | None = None,
    target_root: str | Path | None = None,
) -> Path | None:
    """系统缺少 VC 运行库时，把 ``_MEI`` 里的副本复制到稳定目录并返回该目录。

    OBS 这类外部程序（obs64.exe）没有自带 VC 运行库，靠系统目录或搜索路径里的副本。
    以前它们从本工具的 ``_MEI`` 里拿到运行库，这也是临时目录被锁住的根源；清掉 DLL
    搜索目录后，没装 VC++ 运行库的机器上 OBS 会直接起不来。这里把 ``_MEI`` 里的副本
    复制到数据目录下一次（不是临时目录，不会被删），再用它当作子进程的 DLL 搜索目录。
    系统已有运行库、或不是 PyInstaller 运行时返回 None，行为不变。
    """
    if system_has_vc_runtime():
        return None
    source_root: Path | None
    if meipass is not None:
        source_root = Path(meipass)
    else:
        meipass_dir = _meipass_dir()
        source_root = Path(meipass_dir) if meipass_dir else None
    if source_root is None:
        return None
    sources = [
        source_root / name
        for name in _RUNTIME_DLL_NAMES
        if (source_root / name).is_file()
    ]
    if not sources:
        return None
    if target_root is None:
        from .config import base_dir

        target_root = base_dir()
    target = Path(target_root) / "runtime" / "x64"
    try:
        target.mkdir(parents=True, exist_ok=True)
        for source in sources:
            destination = target / source.name
            if (
                not destination.is_file()
                or destination.stat().st_size != source.stat().st_size
            ):
                shutil.copyfile(source, destination)
    except OSError:
        logger.warning("复制 VC 运行库到 %s 失败", target, exc_info=True)
        return None
    logger.info("系统缺少 VC 运行库，已改用 %s 里的副本启动外部程序", target)
    return target


@contextmanager
def clean_dll_search_path() -> Iterator[None]:
    """临时清空本进程的 DLL 搜索目录，让新建子进程不继承 ``_MEI``。

    Windows 会把 ``SetDllDirectoryW`` 的值随 CreateProcess 传给子进程，PyInstaller
    引导器正是用它把 ``_MEI`` 加进搜索路径。清空后再创建子进程，外部程序就只会从
    自己的目录、System32 和 PATH 里找 DLL，不会锁住本工具的临时目录。
    退出上下文时恢复原值，本进程继续正常运行。
    """
    if sys.platform != "win32":
        yield
        return
    with _DLL_DIRECTORY_LOCK:
        previous = current_dll_directory()
        set_dll_directory(None)
        try:
            yield
        finally:
            set_dll_directory(previous)


def spawn_detached(
    exe: str | Path,
    cwd: str | Path | None = None,
    args: Iterable[str] | None = None,
) -> subprocess.Popen:
    """用干净的环境启动外部程序，返回 Popen 对象，不等待其退出。

    ``exe`` 不存在时抛 ``FileNotFoundError``。Windows 上脱离当前进程组，
    避免外部程序被当作本工具的子进程一起收走；同时清掉继承给子进程的
    ``_MEI`` DLL 搜索目录。
    """
    exe_path = Path(exe)
    if not exe_path.is_file():
        raise FileNotFoundError(f"executable not found: {exe_path}")

    command = [str(exe_path)]
    if args:
        command.extend(str(arg) for arg in args)

    kwargs: dict[str, object] = {
        "env": clean_child_env(),
        "close_fds": True,
    }
    if cwd is not None:
        kwargs["cwd"] = str(cwd)
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    with clean_dll_search_path():
        guard = None
        if sys.platform == "win32":
            guard = vc_runtime_guard_dir()
            if guard is not None:
                set_dll_directory(str(guard))
        try:
            return subprocess.Popen(command, **kwargs)
        finally:
            if guard is not None:
                set_dll_directory(None)
