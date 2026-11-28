"""应用内更新：下载安装包、校验 SHA256、退出后由独立 helper 替换 exe。

边界说明：

- 本模块不依赖 Qt，下载与 helper 脚本生成都可以离线单测；
- 下载目录固定在系统临时目录，绝不写用户数据目录（``%APPDATA%`` /
  portable 的 ``ADOFAI GO 数据``）；
- 运行中的 onefile exe 被锁，替换只能等进程退出后由独立 PowerShell
  helper 完成：等旧进程退出 → 备份 ``.old`` → 覆盖 → 失败回滚；
- ``ADOFAI_UPDATE_RELEASE_FILE`` 是本地验收开关（见 tools/update_demo.py），
  只有它指向的 Release 里的本地路径才允许直接读文件，正常更新只认 https。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import requests

from .config import base_dir

USER_AGENT = "ADOFAI-GO-Update-Downloader"
HTTP_TIMEOUT = (10.0, 60.0)
CHUNK_SIZE = 256 * 1024
DOWNLOAD_DIR_NAME = "ADOFAI GO Update"
RELEASE_FILE_ENV = "ADOFAI_UPDATE_RELEASE_FILE"
INSTALL_TARGET_ENV = "ADOFAI_UPDATE_INSTALL_TARGET"
HELPER_WAIT_SECONDS = 600
MAX_CHECKSUM_BYTES = 64 * 1024

_SHA256_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")
_CHECKSUM_SUFFIXES = (".sha256", ".sha256.txt", ".sha256sum")

ProgressCallback = Callable[[int, int], None]


class SelfUpdateError(RuntimeError):
    """更新下载/安装失败，消息可直接展示给用户。"""


class SelfUpdateCancelled(SelfUpdateError):
    """用户取消下载。"""


# -- 资产挑选 ---------------------------------------------------------------


def pick_exe_asset(assets: Iterable[dict[str, Any]] | None) -> dict[str, Any] | None:
    """从 Release assets 里挑出安装包 exe；优先名字带 ADOFAI 的那个。"""
    candidates: list[dict[str, Any]] = []
    for asset in assets or []:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        url = str(asset.get("url") or "")
        if not url or not name.lower().endswith(".exe"):
            continue
        candidates.append(asset)
    if not candidates:
        return None
    for asset in candidates:
        if "adofai" in str(asset.get("name") or "").lower():
            return asset
    return candidates[0]


def find_checksum_asset(
    assets: Iterable[dict[str, Any]] | None,
    exe_asset: dict[str, Any],
) -> dict[str, Any] | None:
    """找与 exe 同名的 ``.sha256`` 资产（兼容大小写与常见后缀）。"""
    exe_name = str(exe_asset.get("name") or "").strip().lower()
    if not exe_name:
        return None
    wanted = {exe_name + suffix for suffix in _CHECKSUM_SUFFIXES}
    for asset in assets or []:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "").strip().lower()
        if name in wanted and str(asset.get("url") or ""):
            return asset
    return None


def parse_checksum(text: str) -> str | None:
    """从 ``<hash>  <filename>`` 一类文本里取出 64 位十六进制摘要。"""
    match = _SHA256_RE.search(text or "")
    return match.group(1).lower() if match else None


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_download_name(asset: dict[str, Any]) -> str:
    name = Path(str(asset.get("name") or "")).name.strip()
    if not name.lower().endswith(".exe"):
        name = "ADOFAI_GO_update.exe"
    return name


def download_dir() -> Path:
    """下载与 helper 脚本都放系统临时目录，不碰用户数据目录。"""
    return Path(tempfile.gettempdir()) / DOWNLOAD_DIR_NAME


# -- 下载 -------------------------------------------------------------------


def local_asset_path(asset: dict[str, Any]) -> Path | None:
    """本地验收资产返回路径；正式更新资产返回 None。"""
    if not asset.get("local"):
        return None
    return Path(str(asset.get("url") or ""))


def _http_get(url: str, *, stream: bool):
    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/octet-stream",
            },
            timeout=HTTP_TIMEOUT,
            stream=stream,
            allow_redirects=True,
        )
    except requests.Timeout as exc:
        raise SelfUpdateError("下载超时，请检查网络后重试") from exc
    except requests.RequestException as exc:
        message = str(exc).splitlines()[0][:160]
        raise SelfUpdateError(f"网络不通，无法下载更新包：{message}") from exc
    if response.status_code == 403:
        _close_quietly(response)
        raise SelfUpdateError(
            "下载被 GitHub 拒绝（403，可能触发限流），请稍后重试或打开发布页手动下载"
        )
    if response.status_code == 404:
        _close_quietly(response)
        raise SelfUpdateError("下载地址不存在（404），发布可能已被替换，请重新检查更新")
    if response.status_code != 200:
        status = response.status_code
        _close_quietly(response)
        raise SelfUpdateError(f"下载失败：服务器返回 HTTP {status}")
    return response


def _close_quietly(response: Any) -> None:
    close = getattr(response, "close", None)
    if close is not None:
        try:
            close()
        except Exception:  # pragma: no cover - 关闭失败不影响用户提示
            pass


def download_file(
    asset: dict[str, Any],
    dest: str | Path,
    *,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> int:
    """把资产下载到 ``dest``（先写 ``.part`` 再原子改名），返回字节数。"""
    url = str(asset.get("url") or "").strip()
    if not url:
        raise SelfUpdateError("更新包下载地址为空")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    part.unlink(missing_ok=True)
    expected_size = max(0, int(asset.get("size") or 0))
    received = 0
    handle = None
    response = None
    try:
        if asset.get("local"):
            source = Path(url)
            if not source.is_file():
                raise SelfUpdateError(f"本地测试更新包不存在：{source}")
            handle = source.open("rb")
            expected_size = source.stat().st_size or expected_size
            chunks = iter(lambda: handle.read(CHUNK_SIZE), b"")
            total = expected_size
        elif url.lower().startswith(("http://", "https://")):
            response = _http_get(url, stream=True)
            try:
                length = int(response.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            if length > 0:
                expected_size = length
            total = expected_size
            chunks = response.iter_content(chunk_size=CHUNK_SIZE)
        else:
            raise SelfUpdateError("更新包下载地址无效（仅支持 https 链接）")

        with part.open("wb") as target:
            for chunk in chunks:
                if cancel_event is not None and cancel_event.is_set():
                    raise SelfUpdateCancelled("下载已取消")
                if not chunk:
                    continue
                target.write(chunk)
                received += len(chunk)
                if progress is not None:
                    progress(received, total)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        if handle is not None:
            handle.close()
        if response is not None:
            _close_quietly(response)

    if cancel_event is not None and cancel_event.is_set():
        # 最后一块写完后才点取消，也要按取消处理，不能当成下载完成。
        part.unlink(missing_ok=True)
        raise SelfUpdateCancelled("下载已取消")
    if received <= 0:
        part.unlink(missing_ok=True)
        raise SelfUpdateError("下载到的更新包为空，请重试")
    if expected_size > 0 and received < expected_size:
        part.unlink(missing_ok=True)
        raise SelfUpdateError(
            f"下载不完整（{received}/{expected_size} 字节），请重试"
        )
    os.replace(part, dest)
    return received


def fetch_checksum(asset: dict[str, Any]) -> str:
    """读取 SHA256 资产并解析出摘要；格式不对时报错。"""
    url = str(asset.get("url") or "").strip()
    if not url:
        raise SelfUpdateError("SHA256 校验文件地址为空")
    if asset.get("local"):
        path = Path(url)
        if not path.is_file():
            raise SelfUpdateError(f"SHA256 校验文件不存在：{path}")
        if path.stat().st_size > MAX_CHECKSUM_BYTES:
            raise SelfUpdateError("SHA256 校验文件过大，已拒绝解析")
        text = path.read_text(encoding="utf-8", errors="replace")
    elif url.lower().startswith(("http://", "https://")):
        response = _http_get(url, stream=False)
        try:
            text = response.text or ""
        finally:
            _close_quietly(response)
        if len(text) > MAX_CHECKSUM_BYTES:
            raise SelfUpdateError("SHA256 校验文件过大，已拒绝解析")
    else:
        raise SelfUpdateError("SHA256 校验文件地址无效")
    digest = parse_checksum(text)
    if digest is None:
        raise SelfUpdateError("SHA256 校验文件格式不正确，缺少 64 位摘要")
    return digest


def download_release_update(
    exe_asset: dict[str, Any],
    checksum_asset: dict[str, Any] | None,
    *,
    dest_dir: str | Path | None = None,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """下载 exe，按需校验 SHA256，返回给界面和替换流程用的结果字典。

    - 校验文件缺失：跳过校验，``checksum_verified`` 为 False（界面警告）；
    - 校验不通过：删除下载文件并抛 ``SelfUpdateError``。
    """
    folder = Path(dest_dir) if dest_dir is not None else download_dir()
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / safe_download_name(exe_asset)
    if on_stage is not None:
        on_stage("download")
    size = download_file(
        exe_asset,
        dest,
        progress=progress,
        cancel_event=cancel_event,
    )
    actual = sha256_file(dest)
    result: dict[str, Any] = {
        "path": str(dest),
        "size": size,
        "sha256": actual,
        "checksum_verified": False,
        "checksum_expected": "",
    }
    if checksum_asset is None:
        return result
    if cancel_event is not None and cancel_event.is_set():
        dest.unlink(missing_ok=True)
        raise SelfUpdateCancelled("下载已取消")
    if on_stage is not None:
        on_stage("checksum")
    try:
        expected = fetch_checksum(checksum_asset)
    except SelfUpdateError:
        # 校验文件读不到/格式不对时也不留下未校验的安装包。
        dest.unlink(missing_ok=True)
        raise
    if expected != actual:
        dest.unlink(missing_ok=True)
        raise SelfUpdateError(
            "SHA256 校验失败，安装包可能不完整或被篡改，已删除下载文件"
        )
    result["checksum_verified"] = True
    result["checksum_expected"] = expected
    return result


# -- 退出后替换 -------------------------------------------------------------


def install_target() -> Path | None:
    """返回要替换的 exe；源码运行且没有显式测试目标时返回 None。

    ``ADOFAI_UPDATE_INSTALL_TARGET`` 只在非冻结（源码）模式生效，
    打包版永远替换自己，避免环境变量被滥用。数据目录内的路径一律拒绝。
    """
    if getattr(sys, "frozen", False):
        target = Path(sys.executable)
    else:
        raw = os.environ.get(INSTALL_TARGET_ENV, "").strip()
        if not raw:
            return None
        target = Path(raw)
    try:
        target = target.resolve()
    except OSError:
        return None
    try:
        data_root = base_dir().resolve()
    except OSError:
        data_root = None
    if data_root is not None and (target == data_root or data_root in target.parents):
        logging.getLogger("adofai").warning(
            "拒绝把更新安装到数据目录：%s", target
        )
        return None
    return target


INSTALL_SCRIPT = r"""param(
    [Parameter(Mandatory=$true)][int]$ParentPid,
    [Parameter(Mandatory=$true)][string]$TargetPath,
    [Parameter(Mandatory=$true)][string]$NewPath,
    [string]$ExpectedSha256 = "",
    [Parameter(Mandatory=$true)][string]$LogPath,
    [int]$WaitSeconds = 600
)
$ErrorActionPreference = 'Stop'

function Write-UpdateLog([string]$message) {
    try {
        $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message
        Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    } catch { }
}

function Test-FileLocked([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return $false }
    try {
        $stream = [System.IO.File]::Open(
            $path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
        $stream.Close()
        return $false
    } catch {
        return $true
    }
}

Write-UpdateLog "helper started; waiting for pid $ParentPid to exit"
$deadline = (Get-Date).AddSeconds($WaitSeconds)
$exited = $false
while ((Get-Date) -lt $deadline) {
    $process = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
    if ($null -eq $process) { $exited = $true; break }
    Start-Sleep -Milliseconds 500
}
if (-not $exited) {
    Write-UpdateLog "parent process still running after $WaitSeconds seconds; abort"
    exit 2
}

# onefile 的引导进程可能比窗口进程晚退出一小会儿，等文件解锁再替换。
$unlockDeadline = (Get-Date).AddSeconds(60)
while ((Test-FileLocked $TargetPath) -and ((Get-Date) -lt $unlockDeadline)) {
    Start-Sleep -Milliseconds 500
}
if (Test-FileLocked $TargetPath) {
    Write-UpdateLog "target executable is still locked; abort"
    exit 3
}
if (-not (Test-Path -LiteralPath $NewPath)) {
    Write-UpdateLog "downloaded update file is missing; abort"
    exit 4
}

$backupPath = "$TargetPath.old"
$hadBackup = $false
try {
    if (Test-Path -LiteralPath $TargetPath) {
        Copy-Item -LiteralPath $TargetPath -Destination $backupPath -Force
        $hadBackup = $true
        Write-UpdateLog "backup written: $backupPath"
    }
    Copy-Item -LiteralPath $NewPath -Destination $TargetPath -Force
    if ($ExpectedSha256 -ne "") {
        $actual = (Get-FileHash -LiteralPath $TargetPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $ExpectedSha256.ToLowerInvariant()) {
            throw "sha256 mismatch after copy"
        }
    }
    Remove-Item -LiteralPath $NewPath -Force -ErrorAction SilentlyContinue
    Write-UpdateLog "update installed successfully"
    exit 0
} catch {
    Write-UpdateLog ("install failed: " + $_.Exception.GetType().FullName + ": " + $_.Exception.Message)
    Write-UpdateLog ("at: " + $_.ScriptStackTrace)
    if ($hadBackup) {
        try {
            Copy-Item -LiteralPath $backupPath -Destination $TargetPath -Force
            Write-UpdateLog "rollback completed"
        } catch {
            Write-UpdateLog ("rollback failed: " + $_.Exception.Message)
        }
    }
    exit 1
}
"""


def build_install_script() -> str:
    return INSTALL_SCRIPT


def _powershell_path() -> str:
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    candidate = (
        Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    return str(candidate) if candidate.is_file() else "powershell.exe"


def launch_install_helper(
    target: str | Path,
    new_file: str | Path,
    expected_sha256: str = "",
    *,
    parent_pid: int | None = None,
    wait_seconds: int = HELPER_WAIT_SECONDS,
    log_path: str | Path | None = None,
) -> Path:
    """启动独立 PowerShell helper，等当前进程退出后替换 exe。

    返回脚本路径，便于日志排查；helper 完全脱离本进程，窗口关闭不受影响。
    """
    if sys.platform != "win32":
        raise SelfUpdateError("自动替换仅支持 Windows")
    target = Path(target)
    new_file = Path(new_file)
    if not new_file.is_file():
        raise SelfUpdateError("更新包不存在，无法启动替换")
    folder = download_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    script = folder / f"install-update-{stamp}.ps1"
    script.write_text(build_install_script(), encoding="utf-8-sig", newline="\r\n")
    log = Path(log_path) if log_path is not None else folder / f"install-update-{stamp}.log"
    args = [
        _powershell_path(),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-ParentPid",
        str(int(parent_pid if parent_pid is not None else os.getpid())),
        "-TargetPath",
        str(target),
        "-NewPath",
        str(new_file),
        "-ExpectedSha256",
        str(expected_sha256 or ""),
        "-LogPath",
        str(log),
        "-WaitSeconds",
        str(int(wait_seconds)),
    ]
    creationflags = 0
    if os.name == "nt":
        # CREATE_NO_WINDOW：不弹窗。不要加 DETACHED_PROCESS——PowerShell 5.1
        # 在完全没有控制台时会直接退出（返回 0 但脚本不执行），helper 就失效了。
        # 子进程本身不随父进程退出，窗口关闭后仍会继续等待并替换。
        creationflags = 0x08000000
    subprocess.Popen(
        args,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    return script


def release_asset_local_path(base: str | Path, raw_url: str) -> Path | None:
    """把本地 Release 里的相对路径 / file:// URL 解析成绝对路径。"""
    text = (raw_url or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered.startswith("file://"):
        parsed = urlparse(text)
        path = Path(url2pathname(unquote(parsed.path)))
    elif lowered.startswith(("http://", "https://")):
        return None
    else:
        path = Path(text)
    if not path.is_absolute():
        path = Path(base) / path
    try:
        return path.resolve()
    except OSError:
        return path
