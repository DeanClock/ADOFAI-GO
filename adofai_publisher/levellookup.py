from __future__ import annotations

import re
import shutil
import stat
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import requests

from .ard_parser import _decode_chart_payload, find_chart_member
from .level_paths import chart_folder
from .models import LevelInfo

BASE_URL = "https://api.tuforums.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

FALLBACK_FILENAME = "{song} - {charter} ({tuf_id}).ard"
CHART_SUFFIXES = (".ard", ".adofai", ".zip", ".adozip")
ARCHIVE_SUFFIXES = (".zip", ".adozip")

# 在线谱面 JSON 正常只有几十 KB；超过 20 MB 直接放弃，避免把异常响应读进内存。
MAX_CHART_JSON_BYTES = 20 * 1024 * 1024

# 服务端单页上限是 100 条；工作台默认就按上限取，避免只看到前 8 条。
DEFAULT_SEARCH_LIMIT = 100
MAX_SEARCH_LIMIT = 100

# TUF /v2/database/levels 支持的字段语法（来自 tuforums.com/levels 的「搜索帮助」）。
ADVANCED_TEXT_FIELDS = ("song", "artist", "creator", "charter", "vfxer", "team", "comment")
ADVANCED_NUMERIC_FIELDS = ("bpm", "tilecount", "time", "likes", "clears")
ADVANCED_ID_FIELDS = ("id", "dlLink", "workshopLink", "legacyDllink", "videolink")
ADVANCED_FIELDS = ADVANCED_TEXT_FIELDS + ADVANCED_NUMERIC_FIELDS + ADVANCED_ID_FIELDS

# 字段名必须落在 token 边界上，否则曲名里的「Re:End」之类会被误判成语法。
_ADVANCED_FIELD_RE = re.compile(
    r"(?<![\w])(?:" + "|".join(re.escape(field) for field in ADVANCED_FIELDS) + r")\s*[:=]",
    re.IGNORECASE,
)
_ADVANCED_ID_RE = re.compile(r"(?<![\w])#\d+(?![\w])")
_ADVANCED_NOT = "\\!"


class LookupError(RuntimeError):
    pass


def is_advanced_query(query: str) -> bool:
    """判断输入是否用了 TUF 的字段语法；用了就不再本地拆词回退重搜。

    只认 token 边界上的 ``字段:`` / ``字段=``、``#编号``、``\\!`` 与 ``|``；
    普通曲名里的冒号、逗号（如 ``Re:End``、``Song, Part 2``）不算。
    纯数字仍由 :meth:`LevelLookup.search` 当编号处理，``#123`` / ``id=123``
    走正常的列表查询。
    """
    text = (query or "").strip()
    if not text:
        return False
    if _ADVANCED_NOT in text or "|" in text:
        return True
    if _ADVANCED_ID_RE.search(text):
        return True
    return _ADVANCED_FIELD_RE.search(text) is not None


def search_candidates(query: str) -> list[str]:
    """把用户输入拆成按召回优先排序的候选词：原文 → 去括号内容 → 去标点。

    批量绑定（``ui/batch_bind.py``）与工作台共用这一套顺序，避免各写一份。
    """
    text = (query or "").strip()
    if not text:
        return []
    candidates = [text]
    without_paren = re.sub(r"\([^)]*\)|（[^）]*）", " ", text)
    without_paren = re.sub(r"\s+", " ", without_paren).strip(" -_·")
    if without_paren and without_paren not in candidates:
        candidates.append(without_paren)
    cleaned = re.sub(r"[^\w一-鿿]+", " ", text).strip()
    if cleaned and cleaned not in candidates:
        candidates.append(cleaned)
    return candidates


class DownloadCancelled(LookupError):
    pass


class LevelLookup:
    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Referer": "https://tuforums.com/",
                "Origin": "https://tuforums.com",
            }
        )

    def search(self, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> list[LevelInfo]:
        query = (query or "").strip()
        if not query:
            return []
        if query.isdigit():
            try:
                return [self.get(int(query))]
            except LookupError:
                pass
        try:
            response = self.session.get(
                f"{BASE_URL}/v2/database/levels",
                params={"query": query, "limit": max(1, min(limit, MAX_SEARCH_LIMIT))},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise LookupError(f"网络请求失败：{exc}") from exc
        if response.status_code != 200:
            raise LookupError(f"TUF 返回 {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise LookupError("TUF 返回了无法解析的数据") from exc
        results = payload.get("results") or []
        return [LevelInfo.from_tuf(item) for item in results if isinstance(item, dict)]

    def search_with_fallback(
        self, query: str, limit: int = DEFAULT_SEARCH_LIMIT
    ) -> list[LevelInfo]:
        """按候选词依次检索，命中为空或只有 1 条时换下一个候选词。

        谱面名带括号、标点时 TUF 的关键词匹配会漏；原文拿到 0 或 1 条就退一步再查，
        只要某个候选词拿到多条结果就立即返回。所有候选词都只有 ≤1 条时，
        返回第一次拿到的非空结果，保证「1 条唯一命中」不会被回退弄丢。
        用了字段语法时由服务器解析，拆括号/去标点会破坏运算符，只发原查询一次。
        """
        if is_advanced_query(query):
            return self.search(query, limit=limit)
        best: list[LevelInfo] = []
        for candidate in search_candidates(query):
            results = self.search(candidate, limit=limit)
            if len(results) > 1:
                return results
            if results and not best:
                best = results
        return best

    def get(self, level_id: int) -> LevelInfo:
        try:
            response = self.session.get(
                f"{BASE_URL}/v2/database/levels/{int(level_id)}",
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise LookupError(f"网络请求失败：{exc}") from exc
        if response.status_code == 404:
            raise LookupError(f"没有找到 TUF 关卡 {level_id}")
        if response.status_code != 200:
            raise LookupError(f"TUF 返回 {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise LookupError("TUF 返回了无法解析的数据") from exc
        level = payload.get("level") if isinstance(payload, dict) else None
        if not isinstance(level, dict):
            raise LookupError("TUF 没有返回关卡数据")
        return LevelInfo.from_tuf(level)

    def fetch_chart_json(self, level_id: int) -> tuple[dict[str, Any] | None, str]:
        """读取公开的在线谱面 JSON：``/v2/database/levels/{id}/level.adofai``。

        不需要登录，也不用下载整个谱面包。返回 ``(谱面对象, 错误信息)``：
        404、HTML 页面、超大响应、JSON 解析失败都只返回错误字符串，不抛异常。
        """
        number = int(level_id)
        url = f"{BASE_URL}/v2/database/levels/{number}/level.adofai"
        try:
            response = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            return None, f"网络请求失败：{exc}"

        status = int(getattr(response, "status_code", 0) or 0)
        if status == 404:
            return None, f"没有找到 TUF 关卡 {number} 的在线谱面"
        if status != 200:
            return None, f"TUF 返回 {status}"

        headers = getattr(response, "headers", None) or {}
        try:
            declared = int(headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared > MAX_CHART_JSON_BYTES:
            return None, "在线谱面文件过大（超过 20 MB），已取消分析"

        raw = getattr(response, "content", None)
        if not isinstance(raw, (bytes, bytearray)):
            raw = str(getattr(response, "text", "") or "").encode("utf-8", "ignore")
        raw = bytes(raw)
        if len(raw) > MAX_CHART_JSON_BYTES:
            return None, "在线谱面文件过大（超过 20 MB），已取消分析"
        if not raw:
            return None, "在线谱面文件为空"
        head = raw.decode("utf-8-sig", "replace").lstrip()
        if head.startswith("<"):
            return None, "TUF 返回的不是谱面 JSON（可能是 HTML 页面）"

        data, error = _decode_chart_payload(raw)
        if data is None:
            return None, error
        return _unwrap_chart_payload(data), ""

    def download_level(
        self,
        level_id: int,
        dest_dir,
        filename_template: str = "",
        level: LevelInfo | None = None,
        organize: bool = True,
        extract: bool = False,
        progress=None,
        stage=None,
        cancel_event=None,
    ) -> Path:
        dest = Path(dest_dir)
        _raise_if_cancelled(cancel_event)
        if level is None:
            level = self.get(level_id)
        _raise_if_cancelled(cancel_event)
        link = level.dl_link
        if not link:
            raise LookupError("该谱面没有可下载文件")
        target: Path | None = None
        completed = False
        try:
            response = self.session.get(
                link,
                timeout=self.timeout,
                allow_redirects=True,
                stream=True,
            )
            if response.status_code in (401, 403):
                raise LookupError("下载被拒绝：该谱面可能需要登录")
            if response.status_code != 200:
                raise LookupError(f"TUF 下载返回 {response.status_code}")
            total_bytes = 0
            try:
                total_bytes = int(getattr(response, "headers", {}).get("Content-Length") or 0)
            except (TypeError, ValueError):
                total_bytes = 0
            if callable(stage):
                stage("download")
            _raise_if_cancelled(cancel_event)
            chunks = response.iter_content(chunk_size=65536)
            first = b""
            while True:
                _raise_if_cancelled(cancel_event)
                piece = next(chunks, None)
                if piece is None:
                    break
                if piece:
                    first = piece
                    break
            suffix = _download_suffix(response, first)
            target = build_level_path(
                dest,
                level,
                filename_template,
                organize=organize,
                package_suffix=suffix,
            )
            total = 0
            last_report = 0.0
            with open(target, "wb") as handle:
                if first:
                    total += len(first)
                    handle.write(first)
                for chunk in chunks:
                    _raise_if_cancelled(cancel_event)
                    if chunk:
                        total += len(chunk)
                        handle.write(chunk)
                    now = time.monotonic()
                    if callable(progress) and (
                        now - last_report >= 0.08
                        or (total_bytes > 0 and total >= total_bytes)
                    ):
                        progress(total, total_bytes)
                        last_report = now
            _raise_if_cancelled(cancel_event)
            if callable(progress):
                progress(total, total)
            _raise_if_cancelled(cancel_event)
            if total == 0:
                raise LookupError("下载成功但文件为空")
            with open(target, "rb") as handle:
                head = handle.read(8)
            if head[:3] == b"\xef\xbb\xbf":
                head = head[3:]
            if not (head.startswith(b"PK") or head[:1] in (b"{", b"[")):
                raise LookupError("下载内容不是有效的谱面文件")
            if extract and zipfile.is_zipfile(target):
                _raise_if_cancelled(cancel_event)
                if callable(stage):
                    stage("extract")
                extracted = extract_level_to_library(
                    target,
                    dest,
                    level,
                    organize=organize,
                    cancel_event=cancel_event,
                )
                try:
                    target.unlink()
                except OSError:
                    pass
                completed = True
                if callable(stage):
                    stage("done")
                return extracted
            completed = True
            if callable(stage):
                stage("done")
            return target
        except LookupError:
            try:
                if not completed and target is not None and target.exists():
                    target.unlink()
            except OSError:
                pass
            raise
        except requests.RequestException as exc:
            raise LookupError(f"下载失败：{exc}") from exc
        except OSError as exc:
            raise LookupError(f"无法写入谱面文件：{exc}") from exc


def _looks_like_chart(value: Any) -> bool:
    return isinstance(value, dict) and any(
        key in value for key in ("angleData", "pathData", "settings", "actions")
    )


def _unwrap_chart_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """兼容 ``{"level": {...}}`` / ``{"data": {...}}`` 包裹，裸谱面对象原样返回。"""
    if _looks_like_chart(payload):
        return payload
    for key in ("level", "data"):
        inner = payload.get(key)
        if _looks_like_chart(inner):
            return inner
    return payload


def _download_suffix(response: Any, first: bytes) -> str:
    url_suffix = Path(urlparse(str(getattr(response, "url", ""))).path).suffix.lower()
    if first.startswith(b"PK"):
        if url_suffix in (".ard", ".zip", ".adozip"):
            return url_suffix
        return ".zip"
    if first[:3] == b"\xef\xbb\xbf":
        first = first[3:]
    if first.startswith((b"{", b"[")):
        if url_suffix in (".ard", ".adofai"):
            return url_suffix
    return ""


def _raise_if_cancelled(cancel_event: Any) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("下载已取消")


def _safe_filename(name: str, level: LevelInfo, suffix: str = ".ard") -> str:
    keep = "-_.()[]（） "
    cleaned = "".join(ch if ch.isalnum() or ch in keep else "_" for ch in (name or "").strip())
    cleaned = cleaned.strip("._")[:80]
    current_suffix = next(
        (item for item in CHART_SUFFIXES if cleaned.lower().endswith(item)),
        "",
    )
    if not cleaned:
        cleaned = f"level_{level.tuf_id or 'unknown'}{suffix or '.ard'}"
    elif suffix:
        if current_suffix:
            cleaned = cleaned[: -len(current_suffix)] + suffix
        else:
            cleaned = f"{cleaned}{suffix}"
    elif not current_suffix:
        cleaned = f"{cleaned}.ard"
    return cleaned


def _render_filename(template: str, level: LevelInfo) -> str:
    values = {
        "song": (level.song or "").strip(),
        "artist": (level.artist or "").strip(),
        "charter": (level.charter or "").strip(),
        "difficulty": (level.difficulty or "").strip(),
        "tuf_id": str(level.tuf_id or "").strip(),
    }
    rendered = template or "level_{tuf_id}.ard"
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 1
    while True:
        candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def build_level_path(
    dest_dir,
    level: LevelInfo,
    filename_template: str = "",
    organize: bool = True,
    package_suffix: str = "",
) -> Path:
    dest = Path(dest_dir)
    if organize:
        dest = chart_folder(dest, level)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LookupError("无法写入目标目录") from exc
    name = _safe_filename(
        _render_filename(filename_template or FALLBACK_FILENAME, level),
        level,
        suffix=package_suffix or ".ard",
    )
    return _unique_path(dest / name)


def save_level_file(
    source,
    dest_dir,
    level: LevelInfo,
    filename_template: str = "",
    organize: bool = True,
) -> Path:
    source_path = Path(source)
    suffix = ""
    if source_path.suffix.lower() in ARCHIVE_SUFFIXES and zipfile.is_zipfile(source_path):
        suffix = source_path.suffix.lower()
    target = build_level_path(
        dest_dir,
        level,
        filename_template,
        organize=organize,
        package_suffix=suffix,
    )
    try:
        shutil.copyfile(source, target)
    except OSError as exc:
        raise LookupError("无法保存谱面文件") from exc
    return target


def install_level_file(
    source,
    dest_dir,
    level: LevelInfo,
    filename_template: str = "",
    organize: bool = True,
    extract: bool = False,
) -> Path:
    """把预览缓存中的谱面安装到本地库，压缩包可按设置解压。"""
    source_path = Path(source)
    if extract and zipfile.is_zipfile(source_path):
        return extract_level_to_library(
            source_path,
            dest_dir,
            level,
            organize=organize,
        )
    return save_level_file(
        source_path,
        dest_dir,
        level,
        filename_template,
        organize=organize,
    )


def extract_level_to_library(
    source,
    dest_dir,
    level: LevelInfo,
    organize: bool = True,
    cancel_event=None,
) -> Path:
    """把压缩包解压到归类后的谱面文件夹，返回主 .adofai 路径。"""
    _raise_if_cancelled(cancel_event)
    source_path = Path(source)
    if not zipfile.is_zipfile(source_path):
        return source_path
    dest_root = Path(dest_dir)
    if organize:
        base = chart_folder(dest_root, level)
    else:
        base = dest_root / _safe_folder_name(level.song or f"level_{level.tuf_id or 'unknown'}")
    folder = _prepare_extract_folder(base, source_path)
    try:
        empty_before = not folder.exists() or not any(folder.iterdir())
    except OSError:
        empty_before = not folder.exists()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        return extract_level_archive(
            source_path,
            folder,
            cancel_event=cancel_event,
        )
    except Exception:
        if empty_before:
            shutil.rmtree(folder, ignore_errors=True)
        raise


def extract_level_archive(source, destination, cancel_event=None) -> Path:
    """安全解压谱面包；若压缩包只有一个顶层目录则自动拍平。"""
    _raise_if_cancelled(cancel_event)
    source_path = Path(source)
    folder = Path(destination)
    try:
        archive = zipfile.ZipFile(source_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise LookupError(f"压缩包无法读取：{exc}") from exc
    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        names = [info.filename.replace("\\", "/") for info in infos]
        main_name = find_chart_member(names)
        if not main_name:
            raise LookupError("压缩包里没有找到 .adofai 主谱面")
        wrapper = _archive_wrapper(names)
        root = folder.resolve()
        main_path: Path | None = None
        for info, name in zip(infos, names):
            _raise_if_cancelled(cancel_event)
            relative = _archive_relative(name, wrapper)
            if relative is None:
                continue
            if _is_symlink(info):
                continue
            target = folder / relative
            try:
                resolved = target.resolve()
            except OSError as exc:
                raise LookupError(f"压缩包路径无法解析：{exc}") from exc
            if not resolved.is_relative_to(root):
                raise LookupError("压缩包包含不安全的路径")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as reader, open(target, "wb") as writer:
                    while True:
                        _raise_if_cancelled(cancel_event)
                        chunk = reader.read(1024 * 1024)
                        if not chunk:
                            break
                        writer.write(chunk)
            except OSError as exc:
                raise LookupError(f"解压失败：{exc}") from exc
            if name == main_name:
                main_path = target
        if main_path is None or not main_path.is_file():
            raise LookupError("压缩包解压后没有找到主谱面")
        return main_path


def _safe_folder_name(value: str) -> str:
    keep = "-_.()[]（） "
    cleaned = "".join(ch if ch.isalnum() or ch in keep else "_" for ch in value.strip())
    return cleaned.strip(" .")[:80] or "未命名谱面"


def _prepare_extract_folder(base: Path, source: Path) -> Path:
    if not base.exists():
        return base
    if not base.is_dir():
        return _unique_directory(base)
    try:
        entries = [item for item in base.iterdir() if item.resolve() != source.resolve()]
    except OSError:
        entries = []
    if not entries:
        return base
    return _unique_directory(base)


def _unique_directory(path: Path) -> Path:
    counter = 1
    while True:
        candidate = path.with_name(f"{path.name}-{counter}")
        if not candidate.exists():
            return candidate
        counter += 1


def _archive_wrapper(names: list[str]) -> str:
    parts = [_archive_parts(name) for name in names]
    parts = [item for item in parts if item]
    if not parts:
        return ""
    first = parts[0][0]
    if all(len(item) > 1 and item[0] == first for item in parts):
        return first
    return ""


def _archive_relative(name: str, wrapper: str) -> Path | None:
    parts = _archive_parts(name)
    if not parts:
        return None
    if wrapper:
        if parts[0] != wrapper or len(parts) < 2:
            return None
        parts = parts[1:]
    return Path(*parts)


def _archive_parts(name: str) -> tuple[str, ...]:
    raw = name.replace("\\", "/")
    path = PurePosixPath(raw)
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if not parts:
        return ()
    if parts[0].lower() in ("__macosx", ".ds_store", "thumbs.db"):
        return ()
    if any(part == ".." or ":" in part for part in parts):
        raise LookupError("压缩包包含不安全的路径")
    return parts


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return stat.S_ISLNK(mode)


def cookies_to_dict(raw: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "")
        if "tuforums.com" not in domain:
            continue
        name = item.get("name")
        value = item.get("value")
        if name and value is not None:
            result[str(name)] = str(value)
    return result
