from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .umm import UmmError, _game_dir, _mods_dir, launch_game, resolve_mod_dir

BRIDGE_ID = "ADOFAIGOBridge"
BRIDGE_VERSION = "0.2.1"
BRIDGE_DLL_NAME = "ADOFAIGOBridge.dll"
GAME_PROCESS_NAME = "A Dance of Fire and Ice.exe"
REQUEST_NAME = "request.txt"
STATUS_NAME = "status.txt"
HEARTBEAT_NAME = "heartbeat.txt"
HEARTBEAT_STALE_SECONDS = 8.0

# 游戏已在运行：模组内部最多等 20 秒兜底，再留出写状态文件与轮询的余量。
GAME_RUNNING_WAIT_SECONDS = 25.0
# 游戏由工具启动：还要等 Unity 初始化、UMM 加载模组。
GAME_LAUNCH_WAIT_SECONDS = 60.0


class GameBridgeError(Exception):
    """无法把谱面交给游戏载入。"""


@dataclass(frozen=True)
class GameOpenResult:
    chart_path: Path
    mod_dir: Path
    launched: bool
    message: str


def bridge_source_dir() -> Path:
    """返回仓库/打包资源里的桥接 Mod 源目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "bridge_mod"
    return Path(__file__).resolve().parent.parent / "bridge_mod"


def chart_file_from_entry(entry: Mapping[str, Any]) -> Path:
    """从本地库条目中找出可交给 scnEditor 的 .adofai 文件。

    条目录过「谱面变体」时优先打开选中的那一份，否则沿用 main.adofai /
    按名称排序的第一个谱面。
    """
    raw = str(entry.get("path") or "").strip()
    folder = str(entry.get("folder") or "").strip()
    variant = str(entry.get("variant") or "").strip()
    raw_is_archive = False
    if raw:
        try:
            raw_is_archive = Path(raw).is_file() and zipfile.is_zipfile(raw)
        except OSError:
            raw_is_archive = False
    candidates: list[Path] = []
    if folder and variant and not raw_is_archive:
        base = Path(folder)
        chosen = base / PurePosixPath(variant.replace("\\", "/"))
        try:
            chosen.resolve().relative_to(base.resolve())
        except (OSError, ValueError):
            chosen = None
        if (
            chosen is not None
            and chosen.is_file()
            and chosen.suffix.lower() == ".adofai"
        ):
            return chosen
    if raw:
        candidates.append(Path(raw))
    if folder:
        candidates.append(Path(folder))

    for candidate in candidates:
        if candidate.is_file() and candidate.suffix.lower() == ".adofai":
            return candidate
        if candidate.is_dir():
            main = candidate / "main.adofai"
            if main.is_file():
                return main
            try:
                children = list(candidate.iterdir())
            except OSError:
                continue
            charts = sorted(
                (
                    path
                    for path in children
                    if path.is_file() and path.suffix.lower() == ".adofai"
                ),
                key=lambda path: path.name.casefold(),
            )
            if charts:
                return charts[0]

    if raw:
        suffix = Path(raw).suffix.lower()
        if suffix in (".ard", ".zip", ".adozip"):
            raise GameBridgeError(
                "这条记录还是压缩包，游戏编辑器不能直接打开。\n"
                "请先在「磁盘整理」里解压成谱面文件夹，再试一次。"
            )
    raise GameBridgeError(
        "没有找到可打开的 .adofai 文件。\n"
        f"谱面目录：{folder or raw or '（空）'}"
    )


def ensure_bridge_mod(settings: Any) -> Path:
    """把桥接 Mod 安装/更新到游戏的 Mods 目录，返回其目录。"""
    return install_bridge_mod(settings, force=False)


def repair_bridge_mod(settings: Any) -> Path:
    """强制重新安装桥接 Mod，用于修复文件缺失或版本不一致。"""
    return install_bridge_mod(settings, force=True)


def install_bridge_mod(settings: Any, *, force: bool = False) -> Path:
    """一键安装/更新桥接 Mod；force=True 时重新覆盖现有文件。"""
    game = _game_dir(settings)
    if game is None:
        raise GameBridgeError(
            "未找到 ADOFAI 游戏目录，请先在「05 模组」页设置游戏目录"
        )
    source = bridge_source_dir()
    source_dll = source / BRIDGE_DLL_NAME
    source_info = source / "Info.json"
    if not source_dll.is_file() or not source_info.is_file():
        raise GameBridgeError(
            "缺少桥接 Mod 文件，请先运行：\n"
            f"powershell -File {source / 'build.ps1'}"
        )

    # 旧版禁用实现可能把目录改名成 ADOFAIGOBridge.disabled；原地更新那一份，
    # 不能再建第二个同 Id 的目录（UMM 会加载两份 Bridge）。
    mod_dir = resolve_mod_dir(settings, BRIDGE_ID) or (_mods_dir(settings) / BRIDGE_ID)
    target_dll = mod_dir / BRIDGE_DLL_NAME
    target_info = mod_dir / "Info.json"
    # Info.json 也要比对：版本号和 DLL 都没变、但署名/描述更新过时，
    # 已安装的旧副本同样要覆盖（例如作者署名发生变化）。
    if (
        not force
        and target_dll.is_file()
        and target_info.is_file()
        and _installed_version(mod_dir) == BRIDGE_VERSION
        and _sha256(target_dll) == _sha256(source_dll)
        and _sha256(target_info) == _sha256(source_info)
    ):
        return mod_dir
    try:
        mod_dir.mkdir(parents=True, exist_ok=True)
        _copy_if_changed(source_info, target_info)
        _copy_if_changed(source_dll, target_dll)
    except OSError as exc:
        raise GameBridgeError(
            "安装桥接 Mod 失败，游戏可能正在占用文件。\n"
            "请关闭 ADOFAI 后重试。\n\n"
            f"{exc}"
        ) from exc
    return mod_dir


def uninstall_bridge_mod(settings: Any) -> None:
    """删除游戏 Mods 目录里的桥接 Mod，不影响其他模组。"""
    mod_dir = resolve_mod_dir(settings, BRIDGE_ID)
    if mod_dir is None:
        raise GameBridgeError("ADOFAI GO Bridge 尚未安装")
    try:
        shutil.rmtree(mod_dir)
    except OSError as exc:
        raise GameBridgeError(
            "卸载 Bridge 失败，游戏可能正在占用文件。\n"
            "请完全关闭 ADOFAI 后重试。\n\n"
            f"{exc}"
        ) from exc


def get_bridge_status(settings: Any) -> dict[str, Any]:
    """返回桥接 Mod 的安装、在线与日志状态，供「05 模组」页展示。"""
    game = _game_dir(settings)
    if game is None:
        return {
            "game_dir": "",
            "mod_dir": "",
            "installed": False,
            "installed_version": "",
            "expected_version": BRIDGE_VERSION,
            "version_ok": False,
            "running": False,
            "online": False,
            "state": "",
            "log_path": "",
            "status_text": "未检测到游戏目录",
            "status_level": "muted",
        }

    # 兼容旧版 `.disabled` 目录：Bridge 实际已安装时不能报告「未安装」。
    mod_dir = resolve_mod_dir(settings, BRIDGE_ID)
    display_dir = mod_dir or (_mods_dir(settings) / BRIDGE_ID)
    installed = bool(
        mod_dir is not None
        and (mod_dir / BRIDGE_DLL_NAME).is_file()
        and (mod_dir / "Info.json").is_file()
    )
    installed_version = _installed_version(mod_dir) if installed else ""
    version_ok = installed_version == BRIDGE_VERSION
    running = is_game_running()
    heartbeat = _read_heartbeat_file(mod_dir) if installed else None
    online = bool(running and heartbeat)
    state = str((heartbeat or {}).get("state") or "")
    state_label = {
        "idle": "空闲",
        "pending": "正在载入",
        "loaded": "已加载",
    }.get(state, state)

    if not installed:
        text, level = "未安装", "warn"
    elif not version_ok:
        text, level = f"版本不一致 · 已装 {installed_version or '未知'}", "warn"
    elif online:
        text, level = f"在线 · {state_label or '空闲'}", "ok"
    elif running:
        text, level = "游戏已启动，但 Bridge 未响应", "warn"
    else:
        text, level = "已安装 · 游戏未启动", "muted"

    return {
        "game_dir": str(game),
        "mod_dir": str(display_dir),
        "installed": installed,
        "installed_version": installed_version,
        "expected_version": BRIDGE_VERSION,
        "version_ok": version_ok,
        "running": running,
        "online": online,
        "state": state,
        "log_path": str(display_dir / "bridge.log"),
        "status_text": text,
        "status_level": level,
    }


def is_game_running() -> bool:
    """不依赖第三方库，判断 ADOFAI 是否正在运行。"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [
                "tasklist",
                "/FI",
                f"IMAGENAME eq {GAME_PROCESS_NAME}",
                "/NH",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # tasklist truncates long image names (e.g. "A Dance of Fire and Ice.e"),
    # so match the stable prefix instead of the full .exe name.
    return "a dance of fire and ice" in (result.stdout or "").casefold()


def read_status(mod_dir: Path) -> dict[str, str] | None:
    path = mod_dir / STATUS_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    values = _parse_key_values(text)
    return values or None


def wait_for_heartbeat(mod_dir: Path, timeout: float) -> dict[str, str] | None:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        heartbeat = _read_heartbeat_file(mod_dir)
        if heartbeat is not None:
            return heartbeat
        time.sleep(0.2)
    return None


def open_chart_in_game(
    settings: Any,
    chart_path: Path | str,
    *,
    timeout: float | None = None,
    poll_interval: float = 0.2,
    auto_install: bool = True,
) -> GameOpenResult:
    """写请求给游戏内 Mod，并等待它确认载入。"""
    chart = Path(chart_path)
    if not chart.is_file() or chart.suffix.lower() != ".adofai":
        raise GameBridgeError(f"不是可打开的 .adofai 文件：{chart}")

    if auto_install:
        mod_dir = ensure_bridge_mod(settings)
    else:
        try:
            mod_dir = resolve_mod_dir(settings, BRIDGE_ID)
        except UmmError as exc:
            raise GameBridgeError(str(exc)) from exc
        if (
            mod_dir is None
            or not (mod_dir / BRIDGE_DLL_NAME).is_file()
            or not (mod_dir / "Info.json").is_file()
        ):
            raise GameBridgeError(
                "ADOFAI GO Bridge 尚未安装。\n\n"
                "可到「05 模组」页点「一键安装」，或再次点「在游戏中打开」选择安装。"
            )
        if _installed_version(mod_dir) != BRIDGE_VERSION:
            raise GameBridgeError(
                "ADOFAI GO Bridge 版本不一致，请先到「05 模组」页点「修复安装」。"
            )
    running = is_game_running()
    if running and wait_for_heartbeat(mod_dir, 3.0) is None:
        raise GameBridgeError(
            "ADOFAI 正在运行，但 ADOFAI GO Bridge Mod 没有响应。\n\n"
            "这通常是因为 Mod 是在游戏启动后才安装的。\n"
            "请先完全关闭游戏，再点一次「在游戏中打开」，软件会自动启动它。"
        )

    request_id = uuid.uuid4().hex
    _write_request(mod_dir, request_id, chart)
    launched = False
    if not running:
        try:
            launch_game(settings)
            launched = True
        except UmmError as exc:
            _remove_quietly(mod_dir / REQUEST_NAME)
            raise GameBridgeError(str(exc)) from exc

    wait_limit = timeout
    if wait_limit is None:
        wait_limit = (
            GAME_LAUNCH_WAIT_SECONDS if launched else GAME_RUNNING_WAIT_SECONDS
        )
    deadline = time.monotonic() + wait_limit
    while time.monotonic() < deadline:
        status = read_status(mod_dir)
        if status and status.get("id") == request_id:
            if status.get("ok") == "1":
                return GameOpenResult(
                    chart_path=chart,
                    mod_dir=mod_dir,
                    launched=launched,
                    message=status.get("message") or "已交给游戏载入",
                )
            raise GameBridgeError(
                status.get("message") or "游戏内 Bridge Mod 拒绝了这次载入"
            )
        time.sleep(poll_interval)

    raise GameBridgeError(
        f"已把请求交给游戏，但 {wait_limit:.0f} 秒内没有收到确认。\n\n"
        "请检查：\n"
        "1. 游戏是否卡在加载界面；\n"
        "2. 游戏内是否能看到 ADOFAI GO Bridge（UMM 模组页）；\n"
        f"3. 日志：{mod_dir / 'bridge.log'}"
    )


def _write_request(mod_dir: Path, request_id: str, chart: Path) -> None:
    text = (
        f"id={request_id}\n"
        f"path={chart}\n"
        f"created={int(time.time())}\n"
    )
    temp = mod_dir / f"{REQUEST_NAME}.tmp"
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, mod_dir / REQUEST_NAME)
    except OSError as exc:
        _remove_quietly(temp)
        raise GameBridgeError(f"写入游戏载入请求失败：{exc}") from exc


def _read_heartbeat_file(mod_dir: Path) -> dict[str, str] | None:
    path = mod_dir / HEARTBEAT_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    values = _parse_key_values(text)
    if values is None:
        return None
    try:
        stamp = float(values.get("time") or 0)
    except ValueError:
        return None
    if time.time() - stamp > HEARTBEAT_STALE_SECONDS:
        return None
    return values


def _parse_key_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in str(text or "").replace("\r", "").split("\n"):
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().casefold()] = value.strip()
    return values


def _copy_if_changed(source: Path, target: Path) -> None:
    if target.is_file() and _sha256(source) == _sha256(target):
        return
    if target.exists():
        target.unlink()
    shutil.copy2(source, target)


def _installed_version(mod_dir: Path) -> str:
    try:
        payload = json.loads((mod_dir / "Info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(payload.get("Version") or "").strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
