from __future__ import annotations

import logging
import os
import re
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from .batch_match import LocalNumbers, numeric_queries
from .browser import (
    BILI_HOME_URL,
    BILI_MANAGER_URL,
    BILI_UPLOAD_URL,
    LOGIN_FAILURE_CLOSED,
    LOGIN_FAILURE_LAUNCH,
    LOGIN_FAILURE_NOT_LOGGED_IN,
    LOGIN_FAILURE_TIMEOUT,
    TUF_HOME_URL,
    TUF_LOGIN_URL,
    click_first,
    context_cookies,
    describe_launch,
    extract_bvid,
    has_bili_login,
    is_context_closed,
    launch_context,
    login_failure_text,
    page_text,
    redact_secrets,
)
from .ard_parser import parse_ard
from .adofaitools import (
    ADOFAI_TOOLS_LOGIN_URL,
    ADOFAI_TOOLS_MODS_URL,
    save_token,
)
from .config import Settings
from .game_bridge import (
    GameBridgeError,
    install_bridge_mod,
    open_chart_in_game,
    repair_bridge_mod,
    uninstall_bridge_mod,
)
from .level_library import (
    LevelLibraryDB,
    local_fields_from_profile,
    parse_chart_source,
)
from .level_organizer import (
    apply_organize_plan,
    build_organize_plan,
    undo_last_organize,
    undo_manifest,
)
from .levellookup import (
    DEFAULT_SEARCH_LIMIT,
    DownloadCancelled,
    LevelLookup,
    search_candidates,
)
from .models import JUDGEMENT_KEYS, JUDGEMENT_LABELS, LevelInfo
from .tuf_browse import TufBrowser
from .tuf_review import match_submission, merge_review, normalize_review_status
from .tuf_submitter import TufClient
from .umm import (
    UmmCancelled,
    UmmError,
    fetch_umm_update,
    install_local_update,
    install_mod,
    install_umm,
    set_mod_enabled,
    uninstall_mod,
    uninstall_umm,
)
from .updates import UpdateError, check_for_update
from .self_update import (
    SelfUpdateCancelled,
    SelfUpdateError,
    download_release_update,
)
from .wizard_state import now_text


_logger = logging.getLogger("adofai")

# 需要用户到浏览器里手动完成时的等待上限（秒）
MANUAL_STEP_TIMEOUT = 300
MANUAL_FINISH_TIMEOUT = 600


def _log_failure(message: str) -> None:
    """失败类信息同时落 app.log，事后可以取证；写入前统一脱敏。"""

    _logger.warning("%s", redact_secrets(str(message)))


def _log_info(message: str) -> None:
    """诊断类信息落 app.log；同样统一脱敏，绝不写 Cookie。"""

    _logger.info("%s", redact_secrets(str(message)))


class UpdateCheckWorker(QObject):
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(
        self,
        repo: str,
        current_version: str,
        *,
        timeout: float = 10.0,
    ) -> None:
        super().__init__()
        self.repo = repo
        self.current_version = current_version
        self.timeout = timeout

    def run(self) -> None:
        try:
            self.finished.emit(
                check_for_update(
                    self.repo,
                    self.current_version,
                    timeout=self.timeout,
                )
            )
        except UpdateError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class UpdateDownloadWorker(QObject):
    """下载 Release 安装包并校验 SHA256；取消时清理未完成的临时文件。"""

    progress = Signal(int, int)
    stage = Signal(str)
    finished = Signal(dict)
    failed = Signal(str)
    cancelled = Signal(str)

    def __init__(
        self,
        exe_asset: dict[str, Any],
        checksum_asset: dict[str, Any] | None = None,
        dest_dir: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        super().__init__()
        self.exe_asset = dict(exe_asset or {})
        self.checksum_asset = dict(checksum_asset) if checksum_asset else None
        self.dest_dir = dest_dir
        self._cancel_event = cancel_event or threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            result = download_release_update(
                self.exe_asset,
                self.checksum_asset,
                dest_dir=self.dest_dir,
                progress=self._progress,
                cancel_event=self._cancel_event,
                on_stage=self.stage.emit,
            )
            self.finished.emit(result)
        except SelfUpdateCancelled as exc:
            self.cancelled.emit(str(exc) or "下载已取消")
        except SelfUpdateError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")

    def _progress(self, received: int, total: int) -> None:
        self.progress.emit(int(received), int(total))


class LevelLookupWorker(QObject):
    finished = Signal(list)
    failed = Signal(str)

    def __init__(
        self,
        query: str = "",
        level_id: int | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> None:
        super().__init__()
        self.query = query
        self.level_id = level_id
        self.limit = limit

    def run(self) -> None:
        try:
            lookup = LevelLookup()
            if self.level_id:
                results = [lookup.get(int(self.level_id))]
            else:
                results = lookup.search_with_fallback(self.query, limit=self.limit)
            self.finished.emit(results)
        except Exception as exc:
            self.failed.emit(str(exc))


class GameOpenWorker(QObject):
    """后台把谱面请求交给游戏内 Bridge Mod。"""

    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        settings: Settings,
        chart_path: str,
        *,
        auto_install: bool = True,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.chart_path = chart_path
        self.auto_install = auto_install

    def run(self) -> None:
        try:
            result = open_chart_in_game(
                self.settings,
                self.chart_path,
                auto_install=self.auto_install,
            )
            self.finished.emit(result)
        except GameBridgeError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class BatchLevelLookupWorker(QObject):
    """批量绑定：优先按数值召回 TUF 候选，名称只做兜底。

    ``targets`` 里每项是一个 dict（``key / name / bpm / tilecount /
    midspin_count / auto_tile_count / playable_hits / numeric_ready /
    numeric_missing``），其中 ``tilecount`` 已是 TUF 口径（总格 − 中旋）。
    """

    progress = Signal(str, list)
    finished = Signal(int)
    failed = Signal(str)

    NUMERIC_LIMIT = 100
    NAME_LIMIT = 5

    def __init__(self, targets: list[dict[str, Any]]) -> None:
        super().__init__()
        self.targets = [dict(target) for target in targets]
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @staticmethod
    def _local_from_target(target: dict[str, Any]) -> LocalNumbers | None:
        if not target.get("numeric_ready"):
            return None
        try:
            return LocalNumbers(
                bpm=float(target.get("bpm")),
                tilecount=int(target.get("tilecount")),
                midspin=int(target.get("midspin_count")),
                auto=int(target.get("auto_tile_count")),
            )
        except (TypeError, ValueError):
            return None

    def _search(self, lookup: Any, query: str, limit: int) -> list[Any]:
        try:
            return lookup.search(query, limit=limit) or []
        except Exception as exc:
            # 单条查询失败当空结果继续，不打断整批。
            _logger.debug(
                "批量绑定查询失败（%s，limit=%s）：%s", query, limit, exc
            )
            return []

    def run(self) -> None:
        count = 0
        try:
            lookup = LevelLookup()
            for target in self.targets:
                if self._cancelled.is_set():
                    break
                key = str(target.get("key") or "")
                name = str(target.get("name") or "")
                local = self._local_from_target(target)
                results: list[Any] = []
                strategy = "name"
                if local is not None:
                    for query in numeric_queries(local):
                        if self._cancelled.is_set():
                            break
                        results = self._search(lookup, query, self.NUMERIC_LIMIT)
                        _logger.debug(
                            "批量绑定 %s：数值查询 %r → %d 条",
                            key,
                            query,
                            len(results),
                        )
                        if results:
                            strategy = "numeric"
                            break
                if not results and not self._cancelled.is_set():
                    for candidate in _lookup_queries(name):
                        if self._cancelled.is_set():
                            break
                        results = self._search(lookup, candidate, self.NAME_LIMIT)
                        _logger.debug(
                            "批量绑定 %s：名称查询 %r → %d 条",
                            key,
                            candidate,
                            len(results),
                        )
                        if results:
                            break
                _logger.debug(
                    "批量绑定 %s：策略=%s，候选=%d 条", key, strategy, len(results)
                )
                self.progress.emit(key, [item.to_dict() for item in results])
                count += 1
            self.finished.emit(count)
        except Exception as exc:
            self.failed.emit(str(exc))


class UmmUpdateWorker(QObject):
    progress = Signal(str, int)
    finished = Signal(str, str)
    failed = Signal(str)
    cancelled = Signal(str)

    def __init__(self, mirror: str) -> None:
        super().__init__()
        self.mirror = mirror
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            version, target = fetch_umm_update(
                self.mirror,
                progress=self._progress,
                cancel=self._cancel_event.is_set,
            )
            self.finished.emit(version, str(target))
        except UmmCancelled as exc:
            self.cancelled.emit(str(exc) or "已取消")
        except UmmError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc))

    def _progress(self, message: str, percent: int) -> None:
        self.progress.emit(message, int(percent))


UMM_FILE_ACTIONS = (
    "install_umm",
    "uninstall_umm",
    "install_local_update",
    "install_mod",
    "uninstall_mod",
    "set_mod_enabled",
    "install_bridge",
    "repair_bridge",
    "uninstall_bridge",
)


class UmmActionWorker(QObject):
    """在后台线程执行 UMM / Bridge 的文件操作，避免模组页假死。

    完成后发出 ``finished(action, result)``；失败时统一转成
    ``failed(可读消息)``，不让异常冒到线程里。
    """

    finished = Signal(str, object)
    failed = Signal(str)

    def __init__(
        self,
        settings: Settings,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.action = action
        self.payload = dict(payload or {})

    def run(self) -> None:
        try:
            result = self._dispatch()
        except (UmmError, GameBridgeError) as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            # 线程里不能抛：未知异常也转成可读消息，避免界面收不到结果。
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.finished.emit(self.action, result)

    def _dispatch(self) -> Any:
        action = self.action
        payload = self.payload
        if action == "install_umm":
            return str(install_umm(self.settings))
        if action == "uninstall_umm":
            uninstall_umm(self.settings)
            return None
        if action == "install_local_update":
            return str(install_local_update())
        mod_id = str(payload.get("mod_id") or "")
        if action in ("install_mod", "uninstall_mod", "set_mod_enabled") and not mod_id:
            raise UmmError("缺少模组标识，请刷新模组列表后重试")
        if action == "install_mod":
            return install_mod(
                self.settings,
                mod_id,
                payload.get("archive_path"),
            )
        if action == "uninstall_mod":
            uninstall_mod(self.settings, mod_id)
            return None
        if action == "set_mod_enabled":
            set_mod_enabled(
                self.settings,
                mod_id,
                bool(payload.get("enabled")),
            )
            return None
        if action == "install_bridge":
            return str(install_bridge_mod(self.settings))
        if action == "repair_bridge":
            return str(repair_bridge_mod(self.settings))
        if action == "uninstall_bridge":
            uninstall_bridge_mod(self.settings)
            return None
        raise UmmError(f"未知的模组操作：{action}")


def _extract_access_token(payload: Any) -> str:
    """从登录/刷新响应里取出 accessToken，兼容 data 包裹与驼峰/下划线命名。"""
    candidates: list[Any] = []
    if isinstance(payload, dict):
        candidates.append(payload)
        data = payload.get("data")
        if isinstance(data, dict):
            candidates.append(data)
    for item in candidates:
        for key in ("accessToken", "access_token", "token"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


class AdofaiToolsLoginWorker(QObject):
    log = Signal(str)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self._cancel = threading.Event()
        self._token = ""

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        context = None
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                context = launch_context(pw, self.settings, headless=False)
                page = context.pages[0] if context.pages else context.new_page()

                def on_request(request: Any) -> None:
                    if "/api/" not in request.url:
                        return
                    auth = ""
                    try:
                        auth = request.headers.get("authorization") or ""
                    except Exception:
                        return
                    if auth.lower().startswith("bearer "):
                        self._token = auth[7:].strip()

                def on_response(response: Any) -> None:
                    url = getattr(response, "url", "")
                    if "/api/auth/" not in url:
                        return
                    try:
                        payload = response.json()
                    except Exception:
                        return
                    token = _extract_access_token(payload)
                    if token:
                        self._token = token

                context.on("request", on_request)
                context.on("response", on_response)
                page.on("request", on_request)
                page.on("response", on_response)
                self.log.emit("正在打开 ADOFAI Tools 检查登录态…")
                page.goto(ADOFAI_TOOLS_MODS_URL, wait_until="domcontentloaded", timeout=60000)
                self.log.emit("已登录会直接完成；未登录请在窗口里登录，最多等待 10 分钟。")
                deadline = time.time() + 600
                nudged = 0.0
                while time.time() < deadline:
                    if self._cancel.is_set():
                        self.failed.emit("登录已取消")
                        return
                    if is_context_closed(context):
                        self.failed.emit("登录窗口已关闭，已取消登录")
                        return
                    if self._token:
                        save_token(self._token)
                        self.settings.set("adofaitools_token", self._token)
                        self.settings.save()
                        self.log.emit("已获取 ADOFAI Tools 登录态")
                        self.finished.emit(self._token)
                        return
                    # 同步 Playwright 只有在其 API 调用期间才会派发事件，
                    # 因此用 wait_for_timeout 推进事件循环，而不是阻塞等待。
                    try:
                        page.wait_for_timeout(700)
                    except Exception:
                        self.failed.emit("登录窗口已关闭，已取消登录")
                        return
                    if time.time() - nudged > 6:
                        nudged = time.time()
                        try:
                            page.evaluate(
                                "() => fetch('/api/mods?page=1&pageSize=1', "
                                "{headers: {Accept: 'application/json'}})"
                                ".catch(() => null)"
                            )
                        except Exception:
                            pass
                self.failed.emit("等待登录超时，请重试")
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass


class AdofaiToolsListWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, token: str, resource_type: str = "MOD", page: int = 1) -> None:
        super().__init__()
        self.token = token
        self.resource_type = resource_type
        self.page = page

    def run(self) -> None:
        try:
            from .adofaitools import list_mods

            payload = list_mods(self.token, resource_type=self.resource_type, page=self.page)
            payload["page"] = self.page
            self.finished.emit(payload)
        except Exception as exc:
            self.failed.emit(str(exc))


class AdofaiToolsDownloadWorker(QObject):
    progress = Signal(str)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, token: str, item: dict[str, Any], settings: Settings) -> None:
        super().__init__()
        self.token = token
        self.item = item
        self.settings = settings

    def run(self) -> None:
        try:
            from .adofaitools import download_mod_file, mod_detail, normalize_mod
            from .config import base_dir
            from .umm import install_mod_archive

            resource_type = str(self.item.get("resource_type") or "MOD")
            slug = str(self.item.get("slug") or "")
            download_url = str(self.item.get("download_url") or "")
            file_name = str(self.item.get("file_name") or "")
            version = str(self.item.get("version") or "")
            if not download_url:
                detail = mod_detail(self.token, resource_type, slug)
                resolved = normalize_mod(detail)
                download_url = str(resolved.get("download_url") or "")
                file_name = file_name or str(resolved.get("file_name") or "")
                version = version or str(resolved.get("version") or "")
            mod_id = slug or str(self.item.get("display_name") or "adofai_mod")
            cache = base_dir() / "cache" / "adofaitools"
            archive = download_mod_file(
                self.token,
                resource_type,
                slug,
                download_url,
                cache,
                file_name=file_name,
                progress=self._progress,
            )
            result = install_mod_archive(
                self.settings,
                mod_id,
                archive,
                display_name=str(self.item.get("display_name") or ""),
                version=version,
            )
            try:
                archive.unlink(missing_ok=True)
            except OSError:
                pass
            self.finished.emit(
                f"{result.get('display_name') or mod_id} 已安装到 Mods\\{mod_id}"
            )
        except Exception as exc:
            self.failed.emit(str(exc))

    def _progress(self, received: int, total: int) -> None:
        if not total:
            return
        percent = int(received * 100 / total)
        self.progress.emit(
            f"正在下载 {received / 1024 / 1024:.1f} / {total / 1024 / 1024:.1f} MB（{percent}%）"
        )


def _lookup_queries(query: str) -> list[str]:
    """批量绑定用的候选词顺序，与工作台搜索共用同一套实现。"""
    return search_candidates(query)


class ChartDownloadWorker(QObject):
    log = Signal(str)
    progress = Signal(object)
    stage = Signal(str)
    finished = Signal(dict)
    failed = Signal(str)
    cancelled = Signal(str)

    def __init__(
        self,
        level: LevelInfo,
        dest_dir: str,
        filename_template: str,
        refresh_metadata: bool = False,
        organize: bool = True,
        extract: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> None:
        super().__init__()
        self.level = level
        self.dest_dir = dest_dir
        self.filename_template = filename_template
        self.refresh_metadata = refresh_metadata
        self.organize = organize
        self.extract = extract
        self._cancel_event = cancel_event or threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            if self._cancel_event.is_set():
                raise DownloadCancelled("下载已取消")
            self.log.emit("正在下载谱面文件…")
            lookup = LevelLookup(timeout=120.0)
            level = self.level
            if self.refresh_metadata or not level.dl_link:
                try:
                    level = lookup.get(int(level.tuf_id or 0))
                except Exception:
                    level = self.level
            if self._cancel_event.is_set():
                raise DownloadCancelled("下载已取消")
            target = lookup.download_level(
                int(self.level.tuf_id or 0),
                self.dest_dir,
                self.filename_template,
                level=level,
                organize=self.organize,
                extract=self.extract,
                progress=self._progress,
                stage=self.stage.emit,
                cancel_event=self._cancel_event,
            )
            self.log.emit("下载完成：" + target.name)
            level_data = level.to_dict()
            # 下载下来的文件能解析时，把本地数值带进 payload；解析失败则保留
            # TUF 下发的 tilecount / auto / midspin。
            try:
                profile = parse_chart_source(target)
                local = local_fields_from_profile(profile)
                parse_error = profile.error or ""
            except Exception:
                local = {}
                parse_error = "下载后解析谱面失败"
            if local:
                level_data.update(local)
            self.finished.emit(
                {
                    "path": str(target),
                    "level": level_data,
                    "parse_error": parse_error,
                }
            )
        except DownloadCancelled as exc:
            self.cancelled.emit(str(exc) or "下载已取消")
        except Exception as exc:
            self.failed.emit(str(exc))

    def _progress(self, received: int, total: int) -> None:
        self.progress.emit({"received": int(received), "total": int(total)})


class TufBrowseWorker(QObject):
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, state: dict[str, Any]) -> None:
        super().__init__()
        self.state = dict(state)

    def run(self) -> None:
        try:
            options = dict(self.state)
            options.pop("request_id", None)
            page = TufBrowser().fetch_page(**options)
            self.finished.emit(
                {
                    "request_id": self.state.get("request_id", 0),
                    "items": [item.to_dict() for item in page.items],
                    "total": page.total,
                    "offset": page.offset,
                    "limit": page.limit,
                    "has_more": page.has_more,
                }
            )
        except Exception as exc:
            self.failed.emit(str(exc))


class TufMetaWorker(QObject):
    finished = Signal(dict)
    failed = Signal(str)

    def run(self) -> None:
        try:
            self.finished.emit(TufBrowser().fetch_meta())
        except Exception as exc:
            self.failed.emit(str(exc))


class TufChartAnalyzeWorker(QObject):
    """后台读取 TUF 在线谱面 JSON 并分析，不需要 Playwright。"""

    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, level_id: int) -> None:
        super().__init__()
        self.level_id = int(level_id)

    def run(self) -> None:
        try:
            from .chart_analyzer import analyze_tuf_level

            analysis, level = analyze_tuf_level(self.level_id)
            self.finished.emit(
                {
                    "analysis": analysis.to_dict(),
                    "level": level.to_dict() if level is not None else None,
                }
            )
        except Exception as exc:
            self.failed.emit(str(exc))


class LevelScanWorker(QObject):
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, library_path: str, directory: str) -> None:
        super().__init__()
        self.library_path = library_path
        self.directory = directory

    def run(self) -> None:
        try:
            db = LevelLibraryDB(self.library_path)
            result = db.scan(self.directory)
            db.save()
            self.finished.emit(
                {
                    "count": len(result),
                    "created": getattr(result, "created_count", len(result)),
                    "updated": getattr(result, "updated_count", 0),
                    "failed": getattr(result, "failed_count", 0),
                    "skipped": getattr(result, "skipped_count", 0),
                    "directory": self.directory,
                }
            )
        except Exception as exc:
            self.failed.emit(str(exc))


class LevelOrganizeWorker(QObject):
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(
        self,
        library_path: str,
        root: str,
        history_dir: str,
        action: str = "apply",
        selected_keys: list[str] | None = None,
        manifest_path: str = "",
    ) -> None:
        super().__init__()
        self.library_path = library_path
        self.root = root
        self.history_dir = history_dir
        self.action = action
        self.selected_keys = selected_keys
        self.manifest_path = manifest_path

    def run(self) -> None:
        try:
            db = LevelLibraryDB(self.library_path)
            if self.action == "undo_manifest":
                result = undo_manifest(
                    db,
                    self.manifest_path,
                    self.history_dir,
                    selected_keys=(
                        set(self.selected_keys)
                        if self.selected_keys is not None
                        else None
                    ),
                )
            elif self.action == "undo":
                result = undo_last_organize(db, self.history_dir)
            else:
                plan = build_organize_plan(db, self.root)
                result = apply_organize_plan(
                    db,
                    plan,
                    self.history_dir,
                    selected_keys=(
                        set(self.selected_keys)
                        if self.selected_keys is not None
                        else None
                    ),
                )
            db.save()
            self.finished.emit(result.to_dict())
        except Exception as exc:
            self.failed.emit(str(exc))


class ARDProfileWorker(QObject):
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path

    def run(self) -> None:
        try:
            profile = parse_ard(self.path)
            self.finished.emit(profile.to_dict())
        except Exception as exc:
            self.failed.emit(str(exc))


class SessionCheckWorker(QObject):
    finished = Signal(dict)
    failed = Signal(str)
    log = Signal(str)

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright

            self.log.emit("正在启动浏览器读取登录态…")
            with sync_playwright() as pw:
                context = launch_context(pw, self.settings, headless=True)
                try:
                    bili = has_bili_login(context)
                    self.log.emit("正在检查 TUF 会话…")
                    client = TufClient(timeout=12.0, request_context=context.request)
                    session = client.refresh_session()
                    user = session.get("user") or {}
                    self.log.emit("登录态检查完成")
                    tuf_error = ""
                    if not user and session.get("retryable"):
                        # 503/502/504 是服务端临时故障，不能当成登录态失效。
                        tuf_error = "TUF 服务暂不可用，请稍后重试"
                    self.finished.emit(
                        {
                            "bili": bili,
                            "tuf": bool(user),
                            "tuf_name": user.get("username") or user.get("name") or "",
                            "tuf_error": tuf_error,
                        }
                    )
                finally:
                    try:
                        context.close()
                    except Exception:
                        pass
        except Exception as exc:
            self.failed.emit(str(exc))


class LoginWorker(QObject):
    log = Signal(str)
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self._cancelled = threading.Event()
        # 会话检查临时失败时沿用上一次确定的 TUF 登录态，不据此判定「未登录」。
        self._tuf_known_ok = False

    def cancel(self) -> None:
        self._cancelled.set()

    LOGIN_TIMEOUT = 600
    POLL_INTERVAL = 4

    def run(self) -> None:
        context = None
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                try:
                    context = launch_context(pw, self.settings, headless=False)
                except Exception as exc:
                    # 候选顺序和每个候选的失败原因一起落日志，事后能定位是哪一层没起来。
                    _log_failure(f"登录窗口启动失败：{exc}；{describe_launch()}")
                    detail = redact_secrets(str(exc))[:600]
                    self.failed.emit(
                        f"{login_failure_text(LOGIN_FAILURE_LAUNCH)}\n\n（错误详情：{detail}）"
                    )
                    return
                _log_info(f"登录窗口已启动：{describe_launch()}")
                page = context.pages[0] if context.pages else context.new_page()
                self.log.emit("已打开登录窗口，请在浏览器中登录 B 站和 TUF。")
                page.goto(BILI_HOME_URL, wait_until="domcontentloaded", timeout=60000)
                login_page = context.new_page()
                login_page.goto(TUF_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                self.log.emit("登录完成后工具会自动继续，最多等待 10 分钟。")

                bili = False
                tuf_ok = False
                deadline = time.time() + self.LOGIN_TIMEOUT
                while time.time() < deadline:
                    if self._cancelled.is_set():
                        _log_info("登录轮询：用户取消")
                        self.failed.emit("登录已取消")
                        return
                    if is_context_closed(context):
                        _log_failure(f"登录失败（窗口被关闭）：{self._snapshot_text(bili, tuf_ok)}")
                        self.failed.emit(login_failure_text(LOGIN_FAILURE_CLOSED))
                        return
                    bili, tuf_ok = self._poll(context)
                    # 每轮都落 app.log：两个站点的登录态 + 浏览器候选与失败原因。
                    _log_info(self._snapshot_text(bili, tuf_ok))
                    self.log.emit(
                        f"检测中：B 站{'已登录' if bili else '未登录'} / "
                        f"TUF{'已登录' if tuf_ok else '未登录'}"
                    )
                    if bili and tuf_ok:
                        self.finished.emit({"bili": True, "tuf": True})
                        return
                    if self._cancelled.wait(self.POLL_INTERVAL):
                        self.failed.emit("登录已取消")
                        return
                # 超时后再分两类：至少一个站点登录过 → 等超时；两个都没登录 → 窗口开着没操作。
                kind = (
                    LOGIN_FAILURE_TIMEOUT
                    if (bili or tuf_ok)
                    else LOGIN_FAILURE_NOT_LOGGED_IN
                )
                _log_failure(f"登录失败（{kind}）：{self._snapshot_text(bili, tuf_ok)}")
                self.failed.emit(login_failure_text(kind))
        except Exception as exc:
            _log_failure(f"登录流程异常：{type(exc).__name__}: {exc}")
            self.failed.emit(
                login_failure_text(LOGIN_FAILURE_LAUNCH)
                + f"\n\n（错误详情：{redact_secrets(str(exc))[:300]}）"
            )
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass

    def _poll(self, context: Any) -> tuple[bool, bool]:
        bili = has_bili_login(context)
        try:
            session = TufClient(
                timeout=12.0,
                request_context=context.request,
            ).refresh_session()
        except Exception as exc:
            # 检查本身失败不是「未登录」的证据：写日志后继续轮询。
            _log_info(f"TUF 会话检查失败：{type(exc).__name__}: {exc}")
            return bili, self._tuf_known_ok
        if session.get("user"):
            self._tuf_known_ok = True
            return bili, True
        if session.get("retryable"):
            # 503/502/504：服务暂时不可用，只记日志继续等，不翻成未登录。
            _log_info(
                "TUF 会话检查暂时失败"
                f"（HTTP {session.get('status')}），继续轮询"
            )
            return bili, self._tuf_known_ok
        self._tuf_known_ok = False
        return bili, False

    @staticmethod
    def _snapshot_text(bili: bool, tuf_ok: bool) -> str:
        return (
            f"登录轮询：B 站={'已登录' if bili else '未登录'}；"
            f"TUF={'已登录' if tuf_ok else '未登录'}；{describe_launch()}"
        )


class BiliUploadWorker(QObject):
    log = Signal(str)
    progress = Signal(int)
    stage = Signal(str)
    finished = Signal(dict)
    failed = Signal(str)
    needs_login = Signal(str)
    # 需要用户到浏览器里手动完成：界面据此显示「我已完成 / 取消」入口
    needs_manual = Signal(str)
    manual_resolved = Signal()

    def __init__(
        self,
        settings: Settings,
        video_path: str,
        cover_path: str,
        title: str,
        description: str,
        tags: list[str],
        partition: str,
        creative_declaration: str,
        cover_path_43: str = "",
    ) -> None:
        super().__init__()
        self.settings = settings
        self.video_path = video_path
        self.cover_path = cover_path
        # 4:3 副图进「首页推荐封面」，16:9 主图进「个人空间封面」
        self.cover_path_43 = cover_path_43
        self.title = title
        self.description = description
        self.tags = tags
        self.partition = partition
        self.creative_declaration = creative_declaration
        self._cancelled = threading.Event()
        self._manual_done = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def confirm_manual(self) -> None:
        """界面上的「我已完成」：让后台线程继续检查/结束等待。"""

        self._manual_done.set()

    def _warn(self, message: str) -> None:
        """失败类日志：界面日志面板 + app.log（脱敏后）。"""

        self.log.emit(message)
        _log_failure(message)

    def _manual_step(
        self,
        page: Any,
        prompt: str,
        check: Any = None,
        timeout: float = MANUAL_STEP_TIMEOUT,
    ) -> str:
        """把某个中间步骤交给用户在浏览器里做，做完后自动继续。"""

        self._manual_done.clear()
        self.stage.emit("等待手动操作")
        self._warn(prompt)
        self.needs_manual.emit(prompt)
        try:
            return wait_for_user_step(
                page,
                check=check,
                cancelled=self._cancelled,
                done=self._manual_done,
                timeout=timeout,
            )
        finally:
            self.manual_resolved.emit()

    def _manual_finish(self, page: Any, prompt: str) -> tuple[str, str]:
        """投稿这一步交给用户手动点，后台继续等 BV / 成功提示。"""

        self._manual_done.clear()
        self.stage.emit("等待手动完成投稿")
        self._warn(prompt)
        self.needs_manual.emit(prompt)
        try:
            return wait_for_manual_finish(
                page,
                cancelled=self._cancelled,
                done=self._manual_done,
                timeout=MANUAL_FINISH_TIMEOUT,
                log=self.log.emit,
            )
        finally:
            self.manual_resolved.emit()

    def _lookup_manager_bvid(self, page: Any) -> str:
        """到稿件管理页按标题找本次投稿的 BV。"""

        try:
            page.goto(BILI_MANAGER_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            return ""
        for _ in range(5):
            try:
                page.wait_for_timeout(3000)
                bvid = find_manager_bvid(page, self.title)
            except Exception:
                return ""
            if bvid:
                return bvid
        return ""

    def _fail_cancelled(self) -> None:
        self.failed.emit("上传已取消")

    def _fail_closed(self) -> None:
        self.failed.emit("浏览器窗口已被关闭，上传已取消")

    def run(self) -> None:
        context = None
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                if self._cancelled.is_set():
                    self.failed.emit("上传已取消")
                    return
                self.stage.emit("启动浏览器")
                self.log.emit("正在启动已登录的浏览器…")
                context = launch_context(pw, self.settings, headless=False)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(BILI_UPLOAD_URL, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(3000)

                if "passport" in page.url or not has_bili_login(context):
                    self.needs_login.emit("B 站未登录，请在打开的浏览器中完成登录")
                    self.log.emit("等待 B 站登录…")
                    deadline = time.time() + 600
                    while time.time() < deadline:
                        if self._cancelled.is_set():
                            self.failed.emit("上传已取消")
                            return
                        if has_bili_login(context):
                            break
                        if self._cancelled.wait(3):
                            self.failed.emit("上传已取消")
                            return
                    if not has_bili_login(context):
                        self.failed.emit("B 站登录超时")
                        return
                    page.goto(BILI_UPLOAD_URL, wait_until="domcontentloaded", timeout=90000)
                    page.wait_for_timeout(3000)

                self.stage.emit("选择视频")
                self.log.emit("正在准备投稿页…")
                file_input = None
                for selector in (
                    'input[type="file"][accept*="video"]',
                    'input[type="file"][accept*="mp4"]',
                    'input[name="buploader"]',
                    'input[type="file"]',
                ):
                    try:
                        candidate = page.locator(selector).first
                        candidate.wait_for(state="attached", timeout=3000)
                        file_input = candidate
                        break
                    except Exception:
                        continue
                if file_input is None:
                    self._warn(
                        "没有找到视频上传输入框（已尝试："
                        "input[type=file][accept*=video]、input[type=file][accept*=mp4]、"
                        "input[name=buploader]、input[type=file]）"
                    )
                    state = self._manual_step(
                        page,
                        "没找到视频上传入口：请在浏览器里手动选择视频，"
                        "等投稿编辑器出来后点「我已完成」，工具会接着自动填写（最多等 5 分钟）",
                        check=editor_ready,
                    )
                    if state == "cancelled":
                        self._fail_cancelled()
                        return
                    if state == "closed":
                        self._fail_closed()
                        return
                    if state == "timeout":
                        self.failed.emit(
                            "等了 5 分钟，视频还没有在浏览器里选上，也没有收到「我已完成」。\n"
                            "请重新点「上传到 B 站」，或直接在浏览器里手动投稿后把 BV 号填到「保存 BV」。"
                        )
                        return
                else:
                    file_input.set_input_files(self.video_path)
                    self.stage.emit("上传视频")
                    self.log.emit("已选择视频，正在上传…")

                    deadline = time.time() + 60 * 60
                    last_percent = -1
                    while time.time() < deadline:
                        if self._cancelled.is_set():
                            self._fail_cancelled()
                            return
                        text = page_text(page)
                        percent = _parse_percent(text)
                        if percent is not None and percent != last_percent:
                            last_percent = percent
                            self.progress.emit(percent)
                            self.log.emit(f"上传进度 {percent}%")
                        if percent is not None and percent >= 100:
                            page.wait_for_timeout(2500)
                            self.progress.emit(100)
                            break
                        if "上传完成" in text or "秒传成功" in text or "投稿成功" in text:
                            self.progress.emit(100)
                            break
                        if "重新上传" in text or "上传失败" in text:
                            self.failed.emit("B 站上传失败，请查看浏览器提示")
                            return
                        if not _sleep(page, 2000):
                            self._fail_closed()
                            return
                    else:
                        self.failed.emit("上传超时（1 小时）")
                        return

                if not wait_for_editor(page, self._cancelled, timeout=90):
                    if self._cancelled.is_set():
                        self._fail_cancelled()
                        return
                    if page_closed(page):
                        self._fail_closed()
                        return
                    self._warn("视频上传后投稿编辑器没有加载出来")
                    state = self._manual_step(
                        page,
                        "投稿编辑器没有加载出来：请在浏览器里看看是否有弹窗需要处理，"
                        "处理完点「我已完成」，工具会接着自动填写（最多等 5 分钟）",
                        check=editor_ready,
                    )
                    if state == "cancelled":
                        self._fail_cancelled()
                        return
                    if state == "closed":
                        self._fail_closed()
                        return
                    if state == "timeout":
                        self.failed.emit(
                            "等了 5 分钟，投稿编辑器仍没有加载出来，已停止自动投稿。\n"
                            "请在浏览器里手动完成投稿后，把 BV 号填到发布页的「保存 BV」。"
                        )
                        return

                self.stage.emit("填写标题与简介")
                self.log.emit("正在填写投稿信息…")
                fill_title(page, self.title, self.log.emit, self._warn)

                if self.cover_path or self.cover_path_43:
                    self.stage.emit("上传封面")
                    self.log.emit("正在设置自定义封面…")
                    cover_ok = upload_cover(
                        page,
                        self.cover_path,
                        self.log.emit,
                        self._warn,
                        cover_path_43=self.cover_path_43,
                    )
                    if cover_ok:
                        self.stage.emit("确认封面")
                        self.log.emit(
                            "封面已上传：请在浏览器里检查 4:3 与 16:9 两个框，"
                            "调整好后点「完成」（或“确认”/“确定”），"
                            "确认好后工具会自动继续（最多等 5 分钟）"
                        )
                        if not wait_for_cover_confirmed(
                            page, self._cancelled, log=self.log.emit
                        ):
                            if self._cancelled.is_set():
                                self._fail_cancelled()
                                return
                            state = self._manual_step(
                                page,
                                "封面还没有确认：请在浏览器里点「完成」"
                                "（或「确认」/「确定」），"
                                "然后点「我已完成」，工具会接着填写剩下的信息（最多等 5 分钟）",
                                check=lambda: not cover_confirm_pending(page),
                            )
                            if state == "cancelled":
                                self._fail_cancelled()
                                return
                            if state == "closed":
                                self._fail_closed()
                                return
                            if state == "timeout":
                                self.failed.emit(
                                    "封面一直没有确认（等了 5 分钟）。\n"
                                    "请重新点「上传到 B 站」，或在浏览器里确认封面并手动投稿后，"
                                    "把 BV 号填到发布页的「保存 BV」。"
                                )
                                return
                        self.log.emit("封面已确认，继续填写投稿信息")
                else:
                    self.log.emit("未选择自定义封面，B 站会从视频帧自动生成")

                self.stage.emit("填写创作声明")
                self.log.emit("正在选择创作声明…")
                declaration = (self.creative_declaration or "内容无需标注").strip()
                if not select_creative_declaration(
                    page, declaration, self.log.emit, self._warn
                ):
                    state = self._manual_step(
                        page,
                        f"创作声明没有自动选中：请在浏览器里手动选择「{declaration}」，"
                        "然后点「我已完成」，工具会接着自动投稿（最多等 5 分钟）",
                        check=lambda: _declaration_selected(page, declaration),
                    )
                    if state == "cancelled":
                        self._fail_cancelled()
                        return
                    if state == "closed":
                        self._fail_closed()
                        return
                    if state == "timeout":
                        self.failed.emit(
                            f"创作声明仍未选择（目标：{declaration}）。\n"
                            "B 站要求先选创作声明才能投稿，已停止自动投稿。\n"
                            "请在浏览器里手动选好创作声明并投稿后，"
                            "把 BV 号填到发布页的「保存 BV」。"
                        )
                        return
                    self.log.emit(f"创作声明已选择：{declaration}（人工补选）")

                fill_title(page, self.title, self.log.emit, self._warn)

                if not description_ok(page, self.description):
                    self._warn(
                        "没找到简介输入框（已尝试：.ql-editor、div[contenteditable=true]、"
                        "textarea[placeholder*=简介]、textarea），请在浏览器中手动粘贴简介"
                    )
                else:
                    self.log.emit("简介已填写")

                tag_count = fill_tags(page, self.tags, self.log.emit, self._warn)
                if tag_count:
                    self.log.emit(f"已填写 {tag_count} 个标签")

                self.stage.emit("选择分区")
                target_partition = partition_target(self.partition)
                if not select_partition(
                    page, self.partition, self.log.emit, self._warn
                ):
                    state = self._manual_step(
                        page,
                        f"分区没有自动选中（目标：{target_partition}）："
                        "请在浏览器里手动选择分区，然后点「我已完成」（最多等 5 分钟）",
                        check=lambda: partition_selected(page, target_partition),
                    )
                    if state == "cancelled":
                        self._fail_cancelled()
                        return
                    if state == "closed":
                        self._fail_closed()
                        return
                    if state == "timeout":
                        self.failed.emit(
                            f"分区仍未选择（目标：{target_partition}）。\n"
                            "B 站要求选好分区才能投稿，已停止自动投稿。\n"
                            "请在浏览器里手动选好分区并投稿后，"
                            "把 BV 号填到发布页的「保存 BV」。"
                        )
                        return
                    self.log.emit(f"分区已选择：{target_partition}（人工补选）")

                self.stage.emit("提交投稿")
                bvid = ""
                if click_submit(page, self._warn):
                    if not _sleep(page, 5000):
                        self._fail_closed()
                        return
                    click_first(
                        page,
                        [
                            'button:has-text("确认投稿")',
                            'button:has-text("确认")',
                            'button:has-text("确定")',
                        ],
                        timeout=1500,
                    )
                    self.stage.emit("等待投稿结果")
                    state, detail = wait_submit_result(page, self._cancelled)
                else:
                    state, detail = "manual", ""

                if state == "submitted":
                    bvid = detail
                    if not bvid:
                        # 页面只说成功、没露 BV：去稿件管理页按标题补抓一次
                        self.log.emit("页面已确认投稿成功，正在稿件管理页补抓 BV 号…")
                        bvid = self._lookup_manager_bvid(page)
                elif state == "cancelled":
                    self._fail_cancelled()
                    return
                elif state == "closed":
                    self._fail_closed()
                    return
                else:
                    if state == "blocked":
                        self._warn(f"B 站拦住了投稿：{detail}")
                        prompt = (
                            f"B 站拦住了投稿：{detail}\n"
                            "请在浏览器里按提示补齐后手动点「立即投稿」，"
                            "完成后点「我已完成」，工具会继续等 BV（最多 10 分钟）"
                        )
                    elif state == "manual":
                        prompt = (
                            "没找到「立即投稿」按钮。\n"
                            "请在浏览器里手动点「立即投稿」，完成后点「我已完成」，"
                            "工具会继续等 BV（最多 10 分钟）"
                        )
                    else:
                        prompt = (
                            "180 秒内没等到投稿结果。\n"
                            "如果还停在投稿页，请手动点「立即投稿」；"
                            "如果已经投出去了，请点「我已完成」，工具会去稿件管理页找 BV（最多 10 分钟）"
                        )
                    state, detail = self._manual_finish(page, prompt)
                    if state == "cancelled":
                        self._fail_cancelled()
                        return
                    if state == "closed":
                        self._fail_closed()
                        return
                    if state == "submitted":
                        bvid = detail
                    else:
                        bvid = self._lookup_manager_bvid(page)
                        if not bvid:
                            if state == "done":
                                self.failed.emit(
                                    "你点了「我已完成」，但稿件管理页里没有找到这次投稿的 BV 号。\n"
                                    "请到 B 站稿件管理页确认投稿状态，拿到 BV 后"
                                    "填到发布页的「保存 BV」。"
                                )
                            else:
                                self.failed.emit(
                                    "等了 10 分钟，浏览器里没有检测到投稿成功"
                                    "（没有 BV 号或成功提示），也没有收到「我已完成」。\n"
                                    "如果在 B 站页面里已经投出去了，请到稿件管理页复制 BV，"
                                    "填到发布页的「保存 BV」。"
                                )
                            return
                        self.log.emit(f"在稿件管理页找到 BV 号：{bvid}")

                if bvid:
                    self.log.emit(f"已获取 BV 号：{bvid}")
                    self.finished.emit(
                        {"bvid": bvid, "url": f"https://www.bilibili.com/video/{bvid}", "submitted": True}
                    )
                else:
                    self.log.emit("页面已确认投稿成功，但没有抓到 BV 号，记录会保存为待补 BV")
                    self.finished.emit({"bvid": "", "submitted": True})
        except Exception:
            self.failed.emit("B 站上传异常：\n" + traceback.format_exc(limit=3))
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass


class TufSubmitWorker(QObject):
    log = Signal(str)
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(
        self,
        settings: Settings,
        level: LevelInfo,
        bvid: str,
        judgements: dict[str, int],
        speed: float,
        feeling_rating: str,
        is_no_hold_tap: bool = False,
        is_12k: bool = False,
        is_16k: bool = False,
        is_x_perfect: bool = False,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.level = level
        self.bvid = bvid
        self.judgements = judgements
        self.speed = speed
        self.feeling_rating = feeling_rating
        self.is_no_hold_tap = is_no_hold_tap
        self.is_12k = is_12k
        self.is_16k = is_16k
        self.is_x_perfect = is_x_perfect

    def run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright

            if not re.fullmatch(r"BV[0-9A-Za-z]{10}", self.bvid or ""):
                self.failed.emit("BV 号格式不正确，请回到发布页重新填写")
                return
            if not self.level or not self.level.tuf_id or int(self.level.tuf_id) <= 0:
                self.failed.emit("TUF 谱面编号无效，请重新绑定谱面")
                return

            self.log.emit("正在读取 TUF 登录态…")
            readback: dict[str, Any] = {}
            with sync_playwright() as pw:
                context = launch_context(pw, self.settings, headless=True)
                try:
                    client = TufClient(request_context=context.request)
                    session = client.refresh_session()
                    user = session.get("user") or {}
                    if not user:
                        if session.get("retryable"):
                            self.failed.emit("TUF 服务暂不可用，请稍后重试")
                        else:
                            self.failed.emit(
                                "TUF 登录态失效，请先在“检查登录”里重新登录"
                            )
                        return
                    self.log.emit(f"已登录 TUF：{user.get('username') or user.get('name') or '当前账号'}")
                    # 只提示、不阻断：TUF 允许同一玩家对同一谱面重新投稿。
                    self._warn_existing_pass(client)
                    video_link = f"https://www.bilibili.com/video/{self.bvid}"
                    result = client.submit_pass(
                        level_id=int(self.level.tuf_id or 0),
                        video_link=video_link,
                        judgements=self.judgements,
                        speed=self.speed,
                        feeling_rating=self.feeling_rating,
                        is_no_hold_tap=self.is_no_hold_tap,
                        is_12k=self.is_12k,
                        is_16k=self.is_16k,
                        is_x_perfect=self.is_x_perfect,
                        # 中旋数只在 adofaiVersion 低于 3.4.0 时才会被 TUF
                        # 从完美里扣掉；默认提交 3.4.0，所以只是带上备用。
                        midspin_count=int(getattr(self.level, "midspin_count", 0) or 0),
                        # 提交前在日志里留一行判定摘要，方便对照 TUF 记录。
                        log=self.log.emit,
                    )
                    if result.ok:
                        # best-effort 自检：失败只记日志，不影响「提交成功」的结论。
                        readback = self._readback(client, result)
                finally:
                    try:
                        context.close()
                    except Exception:
                        pass
            if result.ok:
                self.finished.emit(
                    {
                        "record_id": result.data.get("record_id", ""),
                        "message": result.message,
                        "video_link": video_link,
                        **readback,
                    }
                )
            else:
                self.failed.emit(result.message)
        except Exception:
            self.failed.emit("TUF 提交异常：\n" + traceback.format_exc(limit=3))

    def _warn_existing_pass(self, client: Any) -> None:
        """提交前查公开通关记录；已有自己的记录只提示，不阻断。"""

        try:
            passes = client.fetch_level_passes(int(self.level.tuf_id or 0))
        except Exception:
            return
        if not getattr(passes, "ok", False):
            return
        player_id = getattr(client, "player_id", None)
        if player_id is None:
            return
        for item in getattr(passes, "items", []) or []:
            found = item.get("player_id")
            if found is None:
                continue
            try:
                same = int(found) == int(player_id)
            except (TypeError, ValueError):
                continue
            if same:
                self.log.emit(
                    f"提示：该谱面已有你的通关记录 #{item.get('id') or '未知'}；"
                    "TUF 允许重新投稿，本次仍会提交。"
                )
                return

    def _readback(self, client: Any, result: Any) -> dict[str, Any]:
        """提交后回读自己的记录，取审核状态并比对判定。

        只记判定格名称，不打印任何 Cookie、玩家名或原始响应。
        """

        record_id = str(result.data.get("record_id") or "").strip()
        if not record_id:
            return {}
        submission_id = str(result.data.get("submission_id") or "").strip()
        try:
            # 提交响应给的两类编号都带上：优先 submission id，404 再用 pass id。
            read = client.get_submission(submission_id, pass_id=record_id)
        except Exception as exc:
            self.log.emit(f"提交后回读审核记录失败：{type(exc).__name__}")
            return {}
        if not getattr(read, "ok", False) or not isinstance(read.item, dict):
            return {}
        extra: dict[str, Any] = {
            "review": merge_review(read.item, checked_at=now_text())
        }
        submitted = result.data.get("submitted_judgements") or {}
        stored = read.item.get("judgements") or {}
        if isinstance(submitted, dict) and isinstance(stored, dict) and stored:
            mismatch: list[str] = []
            for key in JUDGEMENT_KEYS:
                if key not in stored:
                    continue
                try:
                    if int(stored.get(key) or 0) != int(submitted.get(key) or 0):
                        mismatch.append(JUDGEMENT_LABELS.get(key, key))
                except (TypeError, ValueError):
                    continue
            if mismatch:
                extra["review_mismatch"] = True
                self.log.emit(
                    "TUF 回读判定与提交值不一致："
                    + "、".join(mismatch)
                    + "。请在 TUF 记录页核对，本次提交仍按成功处理。"
                )
        return extra


class TufReviewSyncWorker(QObject):
    """把本地「已投稿」记录的 TUF 审核状态同步回来。

    只读不写：``finished`` 里给出需要更新的字段，由主窗口落库。
    审核状态独立于 ``status``，未知状态一律 ``unknown``。
    """

    finished = Signal(dict)
    failed = Signal(str)
    log = Signal(str)

    MAX_PAGES = 5
    PAGE_LIMIT = 100
    MAX_ITEM_LOOKUPS = 5
    # status=all 实测会漏掉待处理，审核同步也按状态分开读。
    STATUSES = ("pending", "approved", "declined")

    def __init__(self, settings: Settings, records: list[dict[str, Any]]) -> None:
        super().__init__()
        self.settings = settings
        self.records = [
            dict(record) for record in (records or []) if isinstance(record, dict)
        ]

    def run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright

            self.log.emit("正在启动浏览器读取 TUF 登录态…")
            with sync_playwright() as pw:
                context = launch_context(pw, self.settings, headless=True)
                try:
                    client = TufClient(timeout=20.0, request_context=context.request)
                    session = client.refresh_session()
                    if not (session.get("user") or {}):
                        retryable = bool(session.get("retryable"))
                        self.finished.emit(
                            {
                                "updates": [],
                                "checked": 0,
                                "failed": [],
                                "auth_error": not retryable,
                                "retryable": retryable,
                                "message": (
                                    "TUF 服务暂不可用，请稍后重试"
                                    if retryable
                                    else "TUF 未登录，请先登录后再同步审核状态"
                                ),
                            }
                        )
                        return
                    items, list_error = self._collect(client)
                    if list_error is not None:
                        self.finished.emit(
                            {
                                "updates": [],
                                "checked": 0,
                                "failed": [],
                                "auth_error": list_error.auth_error,
                                "retryable": list_error.retryable,
                                "message": list_error.message,
                            }
                        )
                        return
                    self.log.emit(
                        f"TUF 投稿列表 {len(items)} 条，本地待同步 {len(self.records)} 条"
                    )
                    updates, checked, failed = self._match_all(client, items)
                    self.log.emit(
                        f"审核状态同步完成：检查 {checked} 条，失败 {len(failed)} 条"
                    )
                    self.finished.emit(
                        {
                            "updates": updates,
                            "checked": checked,
                            "failed": failed,
                            "auth_error": False,
                            "retryable": False,
                            "message": "",
                        }
                    )
                finally:
                    try:
                        context.close()
                    except Exception:
                        pass
        except Exception:
            self.failed.emit("同步审核状态异常：\n" + traceback.format_exc(limit=3))

    def _collect(self, client: Any) -> tuple[list[dict[str, Any]], Any]:
        """按 pending / approved / declined 分页读投稿列表并合并去重。

        登录失效或服务不可用时直接返回错误；某个状态读取失败时保留已取到的
        条目继续匹配，避免一条坏响应让整次同步失败。
        """

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        failures: list[Any] = []
        messages: list[str] = []
        for status in self.STATUSES:
            status_fetched = 0
            for page in range(1, self.MAX_PAGES + 1):
                result = client.list_submissions(
                    kind="pass",
                    status=status,
                    page=page,
                    limit=self.PAGE_LIMIT,
                )
                if not getattr(result, "ok", False):
                    if getattr(result, "auth_error", False) or getattr(
                        result, "retryable", False
                    ):
                        return [], result
                    failures.append(result)
                    messages.append(
                        f"{status} 第 {page} 页："
                        + str(getattr(result, "message", "") or "读取失败")
                    )
                    break
                page_items = list(getattr(result, "items", []) or [])
                status_fetched += len(page_items)
                for item in page_items:
                    if not isinstance(item, dict):
                        continue
                    key = str(
                        item.get("pass_id")
                        or item.get("submission_id")
                        or item.get("id")
                        or ""
                    )
                    if key and key in seen:
                        continue
                    if key:
                        seen.add(key)
                    items.append(item)
                total = int(getattr(result, "total", 0) or 0)
                if not page_items or (total and status_fetched >= total):
                    break
        if messages:
            self.log.emit(
                "部分投稿列表读取失败，按已取到的 "
                f"{len(items)} 条继续：" + "；".join(messages)[:200]
            )
        if not items and failures:
            return [], failures[0]
        return items, None

    def _match_all(
        self, client: Any, items: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
        # 「仅按谱面编号匹配」只在该谱面本地只有一条记录时允许。
        local_counts: dict[str, int] = {}
        for record in self.records:
            key = str(record.get("tuf_id") or "").strip()
            if key:
                local_counts[key] = local_counts.get(key, 0) + 1

        checked_at = now_text()
        updates: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        checked = 0
        unmatched: list[dict[str, Any]] = []
        for record in self.records:
            key = str(record.get("tuf_id") or "").strip()
            matched = match_submission(
                record,
                items,
                tuf_id_unique=local_counts.get(key, 0) <= 1,
            )
            if matched is None:
                unmatched.append(record)
                continue
            updates.append(
                {"id": record.get("id"), **merge_review(matched, checked_at=checked_at)}
            )
            checked += 1

        lookups = 0
        for record in unmatched:
            record_id = str(record.get("tuf_record_id") or "").strip()
            if not record_id or lookups >= self.MAX_ITEM_LOOKUPS:
                continue
            lookups += 1
            # 本地只存了 pass id：作为兜底编号交给客户端尝试。
            result = client.get_submission(pass_id=record_id)
            if getattr(result, "ok", False) and isinstance(result.item, dict):
                updates.append(
                    {
                        "id": record.get("id"),
                        **merge_review(result.item, checked_at=checked_at),
                    }
                )
                checked += 1
            else:
                failed.append(
                    {
                        "id": record.get("id"),
                        "message": str(getattr(result, "message", "") or "读取失败")[:200],
                    }
                )
        return updates, checked, failed


class TufSubmissionListWorker(QObject):
    """只读读取 TUF 服务器上当前账号的 pass 投稿列表。

    不依赖本地 records.json，也不写任何记录；未登录/服务故障时只返回提示。
    """

    finished = Signal(dict)
    failed = Signal(str)
    log = Signal(str)

    MAX_PAGES = 5
    PAGE_LIMIT = 100
    # status=all 实测只回「已通过」，待处理要单独查。
    STATUSES = ("pending", "approved", "declined")

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings

    def run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright

            self.log.emit("正在读取 TUF 投稿记录…")
            with sync_playwright() as pw:
                context = launch_context(pw, self.settings, headless=True)
                try:
                    client = TufClient(timeout=20.0, request_context=context.request)
                    session = client.refresh_session()
                    if not (session.get("user") or {}):
                        retryable = bool(session.get("retryable"))
                        self.finished.emit(
                            self._result(
                                auth_error=not retryable,
                                retryable=retryable,
                                message=(
                                    "TUF 服务暂不可用，请稍后重试"
                                    if retryable
                                    else "TUF 未登录，请先登录后再查看投稿记录"
                                ),
                            )
                        )
                        return
                    self.finished.emit(self._collect(client))
                finally:
                    try:
                        context.close()
                    except Exception:
                        pass
        except Exception:
            self.failed.emit("读取 TUF 投稿记录异常：\n" + traceback.format_exc(limit=3))

    @staticmethod
    def _result(
        *,
        items: list[dict[str, Any]] | None = None,
        total: int = 0,
        pages: int = 0,
        failed: int = 0,
        auth_error: bool = False,
        retryable: bool = False,
        message: str = "",
        counts: dict[str, int] | None = None,
        diagnostics: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "items": list(items or []),
            "total": int(total or 0),
            "pages": int(pages or 0),
            "failed": int(failed or 0),
            "auth_error": bool(auth_error),
            "retryable": bool(retryable),
            "message": message,
            "counts": dict(counts or {}),
            "diagnostics": list(diagnostics or []),
        }

    def _collect(self, client: Any) -> dict[str, Any]:
        """pending / approved / declined 分开读，合并去重。

        每类状态最多 5 页；某一页失败时保留已取到的条目，并把失败页写进
        ``failed`` / ``message``。不依赖 ``status=all``（实测会漏掉待处理）。
        """

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        pages = 0
        failed = 0
        messages: list[str] = []
        for status in self.STATUSES:
            status_fetched = 0
            for page in range(1, self.MAX_PAGES + 1):
                result = client.list_submissions(
                    kind="pass",
                    status=status,
                    page=page,
                    limit=self.PAGE_LIMIT,
                )
                if not getattr(result, "ok", False):
                    failed += 1
                    page_message = str(
                        getattr(result, "message", "") or "读取失败"
                    )
                    if getattr(result, "auth_error", False) or getattr(
                        result, "retryable", False
                    ):
                        return self._result(
                            items=items,
                            total=len(items),
                            pages=pages,
                            failed=failed,
                            auth_error=bool(getattr(result, "auth_error", False)),
                            retryable=bool(getattr(result, "retryable", False)),
                            message=page_message,
                        )
                    messages.append(f"{status} 第 {page} 页：{page_message}")
                    break
                pages += 1
                reported_total = int(getattr(result, "total", 0) or 0)
                page_items = list(getattr(result, "items", []) or [])
                status_fetched += len(page_items)
                for item in page_items:
                    if not isinstance(item, dict):
                        continue
                    key = str(
                        item.get("pass_id")
                        or item.get("submission_id")
                        or item.get("id")
                        or ""
                    )
                    if key and key in seen:
                        continue
                    if key:
                        seen.add(key)
                    items.append(item)
                if not page_items or (
                    reported_total and status_fetched >= reported_total
                ):
                    break

        counts = {"pending": 0, "approved": 0, "declined": 0}
        for item in items:
            item_status = normalize_review_status(item.get("status"))
            if item_status in counts:
                counts[item_status] += 1

        diagnostics: list[dict[str, Any]] = []
        seen_signatures: set[tuple[Any, ...]] = set()
        for item in items:
            diag = item.get("diagnostics")
            if not isinstance(diag, dict):
                continue
            signature = (
                tuple(diag.get("missing") or []),
                tuple(diag.get("top") or []),
                tuple(diag.get("pass") or []),
                tuple(diag.get("level") or []),
            )
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            diagnostics.append(diag)
            # 只写字段名，不写任何字段值。
            _logger.info(
                "TUF 投稿记录字段诊断：缺少 %s；顶层字段=%s；pass 字段=%s；level 字段=%s",
                "、".join(diag.get("missing") or []) or "-",
                diag.get("top") or [],
                diag.get("pass") or [],
                diag.get("level") or [],
            )
            if len(diagnostics) >= 5:
                break

        return self._result(
            items=items,
            total=len(items),
            pages=pages,
            failed=failed,
            message="；".join(messages)[:300],
            counts=counts,
            diagnostics=diagnostics,
        )


IMAGE_ACCEPT_HINTS = ("image", ".jpg", ".jpeg", ".png", ".webp")


def find_image_input(page: Any) -> Any:
    try:
        inputs = page.locator('input[type="file"]')
        count = inputs.count()
    except Exception:
        return None
    fallback = None
    for index in range(count):
        try:
            candidate = inputs.nth(index)
            accept = (candidate.get_attribute("accept") or "").lower()
        except Exception:
            continue
        if any(hint in accept for hint in IMAGE_ACCEPT_HINTS):
            if fallback is None:
                fallback = candidate
            context = _input_context(candidate)
            if "封面" in context or "cover" in context.lower():
                return candidate
    return fallback


def _input_context(candidate: Any) -> str:
    try:
        return str(
            candidate.evaluate(
                """el => {
                    let node = el;
                    for (let i = 0; i < 6 && node; i++, node = node.parentElement) {
                        const text = (node.innerText || '').trim();
                        if (text) return text.slice(0, 500);
                    }
                    return '';
                }"""
            )
        )
    except Exception:
        return ""


# ------------------------------------------------------------------ 封面弹窗
#
# 2026-11 从 B 站投稿编辑器的前端 bundle（creator-monorepo/videoup 的
# index.*.js，cover-editor 组件）核对出来的「封面制作」弹窗结构：
#
#   .cover-editor                                  bcc-dialog 根
#   .cover-editor-panel-canvas-title .text         「首页推荐封面（4:3）」「个人空间封面（16:9）」，
#                                                  点标题切换 activeEditor（两个比例面板上下排列）
#   .cover-editor-panel-canvas-empty.ratio_4_3     空面板：里面的 bcc-upload 有 display:none 的
#                                                  input[type=file]（16:9 对应 .ratio_16_9）
#   #editor_4_3 / #editor_16_9                     已经有封面图的面板（这时面板里没有 input）
#   .cover-editor-panel-select .cover-upload       底部「上传封面 / 拖拽图片或点击上传」，
#                                                  上传目标是当前 activeEditor
#   .sync-checkbox（bcc-checkbox）                 「双比例同步改动」，4:3 和 16:9 各一个，
#                                                  共用同一个 isSyncEnabled；勾选状态写在 label 的
#                                                  bcc-checkbox-checked class 和内部 input.checked 上
#
# 对应源码行为：
#   * 勾了同步 → 传一张图会同时写进 4:3 和 16:9；没勾 → 只写进收到文件的那个面板，
#     底部上传区没有面板信息，按 activeEditor 决定；
#   * 同步勾选状态存在 localStorage（bili_videoup_cover_sync_checkbox_state），
#     下一次打开弹窗会沿用，所以每轮都必须显式设置，不能假设默认未勾选。
#
# 2026-11-21 在真实投稿页（登录后传了个 3 秒测试视频）核对的实测结论：
#   * 根节点是 ``div.cover-editor.bcc-dialog__wrap.bcc-dialog__wrap-mask``；
#   * 两个面板标题就是「首页推荐封面（4:3）」「个人空间封面（16:9）」，
#     选择器 ``.cover-editor-panel-canvas-title .text`` 命中；
#   * 视频传完后 B 站会自动填好两个框（``#editor_4_3`` / ``#editor_16_9`` 都在），
#     所以实际最常走的是「切 activeEditor + 底部上传」这条路，面板里的 input 只是空面板时才有；
#   * 弹窗里唯一的 ``input[type=file]`` 在
#     ``.cover-editor-panel-select .cover-upload`` 里（accept 只有 png/jpeg）；
#   * 同步勾选框是 ``label.sync-checkbox.bcc-checkbox``，未勾选时 label 里没有
#     ``bcc-checkbox-checked``，内部 ``input[type=checkbox].checked`` 是准的；
#     label 自己的文字是 "false"，「双比例同步改动」几个字在外层 wrapper 的 span 里；
#   * 底部「完成」是 ``div.button.submit``（文案「完成」），不是 <button>。
COVER_EDITOR_SELECTOR = ".cover-editor"
COVER_OPEN_TEXTS = ("添加封面", "上传封面", "更换封面", "选择封面", "上传封面图")
COVER_IMAGE_ACCEPT_HINTS = ("image", ".jpg", ".jpeg", ".png")

COVER_RATIO_43 = "4:3"
COVER_RATIO_169 = "16:9"
# 面板标题：按文案定位，括号兼容全角 / 半角
COVER_RATIO_TEXTS = {
    COVER_RATIO_43: ("首页推荐封面（4:3）", "首页推荐封面(4:3)"),
    COVER_RATIO_169: ("个人空间封面（16:9）", "个人空间封面(16:9)"),
}
COVER_RATIO_TITLE_SELECTOR = ".cover-editor-panel-canvas-title .text"
# 空面板自带的上传 input（面板有图后这些 input 就不存在了）
COVER_RATIO_INPUT_SELECTORS = {
    COVER_RATIO_43: (
        '.cover-editor-panel-canvas-empty.ratio_4_3 input[type="file"]',
        '.cover-editor-panel-canvas-loading.ratio_4_3 input[type="file"]',
        '#editor_4_3 input[type="file"]',
    ),
    COVER_RATIO_169: (
        '.cover-editor-panel-canvas-empty.ratio_16_9 input[type="file"]',
        '.cover-editor-panel-canvas-loading.ratio_16_9 input[type="file"]',
        '#editor_16_9 input[type="file"]',
    ),
}
# 「这个面板已经有封面图了」的判据
COVER_RATIO_PANEL_SELECTORS = {
    COVER_RATIO_43: (
        "#editor_4_3",
        ".cover-editor-panel-canvas-image.editor_4_3",
        ".cover-editor .editor_4_3",
    ),
    COVER_RATIO_169: (
        "#editor_16_9",
        ".cover-editor-panel-canvas-image.editor_16_9",
        ".cover-editor .editor_16_9",
    ),
}
# 面板有图时只能走底部「上传封面」，目标由 activeEditor 决定
COVER_SHARED_INPUT_SELECTORS = (
    '.cover-editor-panel-select .cover-upload input[type="file"]',
    '.cover-editor .cover-upload input[type="file"]',
    '.cover-editor .upload-area input[type="file"]',
)
COVER_SYNC_CHECKBOX_SELECTORS = (
    ".cover-editor .sync-checkbox",
    ".cover-editor .sync-checkbox-wrapper .bcc-checkbox",
    ".cover-editor .sync-checkbox-wrapper",
)
COVER_SYNC_INPUT_SELECTORS = (
    '.cover-editor .sync-checkbox input[type="checkbox"]',
    '.cover-editor .sync-checkbox-wrapper input[type="checkbox"]',
)
COVER_SYNC_TEXT = "双比例同步改动"
COVER_SYNC_LEGACY_SELECTOR = ".cover-editor label"
# 弹窗底部「完成」不是 <button>，是 div.cover-editor-button .button.submit
COVER_EDITOR_DONE_SELECTOR = ".cover-editor .cover-editor-button .button.submit"


def _find_cover_image_input(page: Any) -> Any:
    """在已打开的封面制作弹窗里找图片 input，优先「上传封面」那个。"""

    try:
        inputs = page.locator('input[type="file"]')
        total = min(int(inputs.count()), 12)
    except Exception:
        return None
    fallback = None
    for index in range(total):
        try:
            node = inputs.nth(index)
            accept = (node.get_attribute("accept") or "").lower()
        except Exception:
            continue
        if not any(hint in accept for hint in COVER_IMAGE_ACCEPT_HINTS):
            continue
        context_text = _input_context(node)
        if "上传封面" in context_text:
            return node
        if fallback is None:
            fallback = node
    return fallback


def _first_line(exc: Any) -> str:
    return str(exc).splitlines()[0][:120]


def _attached(owner: Any, selectors: tuple[str, ...] | list[str]) -> Any:
    """按候选顺序找第一个已挂载的节点；隐藏的 input（display:none）也算。"""

    for selector in selectors:
        try:
            locator = owner.locator(selector)
            total = int(locator.count())
        except Exception:
            continue
        if total <= 0:
            continue
        for index in range(min(total, 3)):
            try:
                node = locator.nth(index) if total > 1 else locator.first
                node.wait_for(state="attached", timeout=800)
            except Exception:
                continue
            return node
    return None


def cover_editor_open(page: Any) -> bool:
    """「封面制作」弹窗是否已经打开。"""

    try:
        editors = page.locator(COVER_EDITOR_SELECTOR)
        total = min(int(editors.count()), 3)
    except Exception:
        return False
    for index in range(total):
        try:
            if editors.nth(index).is_visible():
                return True
        except Exception:
            continue
    return False


def _cover_ratio_label(node: Any) -> str:
    try:
        return "".join(str(node.inner_text(timeout=600) or "").split())
    except Exception:
        return ""


def _cover_ratio_active(node: Any) -> bool | None:
    """点完标题后读面板外层的 active / inactive class；读不到返回 None。"""

    try:
        classes = str(
            node.evaluate(
                "el => { const p = el.closest('.active, .inactive'); "
                "return p ? (p.className || '') : ''; }"
            )
            or ""
        )
    except Exception:
        return None
    tokens = classes.split()
    if "active" in tokens:
        return True
    if "inactive" in tokens:
        return False
    return None


def activate_cover_ratio(
    page: Any, ratio: str, log: Any = None, warn: Any = None
) -> bool:
    """把某个比例设成当前编辑中的面板（底部「上传封面」会传给 activeEditor）。"""

    texts = tuple(text.replace(" ", "") for text in COVER_RATIO_TEXTS[ratio])
    try:
        titles = page.locator(COVER_RATIO_TITLE_SELECTOR)
        total = min(int(titles.count()), 6)
    except Exception:
        total = 0
    for index in range(total):
        try:
            node = titles.nth(index)
            label = _cover_ratio_label(node)
            if not any(text in label for text in texts):
                continue
            node.click(timeout=2500)
            page.wait_for_timeout(500)
            if _cover_ratio_active(node) is False and warn:
                warn(
                    f"点了「{ratio}」面板标题，但面板没有变成选中状态，"
                    "这张图请在上传后到浏览器里确认落在哪个框"
                )
            return True
        except Exception:
            continue
    # 标题找不到时退一步：有图的面板本身点一下也会切过去
    for selector in COVER_RATIO_PANEL_SELECTORS[ratio]:
        try:
            node = page.locator(selector).first
            node.wait_for(state="visible", timeout=800)
            node.click(timeout=2500)
            page.wait_for_timeout(400)
            return True
        except Exception:
            continue
    if warn:
        warn(f"没有找到「{ratio}」封面面板的标题，无法切换到这个比例")
    return False


def _cover_sync_label(page: Any) -> Any:
    node = _attached(page, COVER_SYNC_CHECKBOX_SELECTORS)
    if node is not None:
        return node
    # class 改名时退回按文案找：弹窗里含「双比例同步改动」的 label
    try:
        nodes = page.locator(COVER_SYNC_LEGACY_SELECTOR)
        total = min(int(nodes.count()), 20)
    except Exception:
        return None
    for index in range(total):
        try:
            node = nodes.nth(index)
            if COVER_SYNC_TEXT in _cover_ratio_label(node):
                return node
        except Exception:
            continue
    return None


def read_cover_sync(page: Any) -> bool | None:
    """读「双比例同步改动」的勾选状态；找不到勾选框时返回 None。"""

    for selector in COVER_SYNC_INPUT_SELECTORS:
        try:
            inputs = page.locator(selector)
            if int(inputs.count()) <= 0:
                continue
            return bool(inputs.first.is_checked())
        except Exception:
            continue
    node = _cover_sync_label(page)
    if node is None:
        return None
    try:
        classes = str(node.get_attribute("class") or "")
    except Exception:
        classes = ""
    if "bcc-checkbox-checked" in classes:
        return True
    # 只信 bcc-checkbox 自己的 label；外层 wrapper 没有勾选状态，别误判成未勾选
    if "bcc-checkbox" in classes:
        return False
    return None


def _click_cover_sync(page: Any, desired: bool | None = None) -> bool:
    """点「双比例同步改动」；给了 ``desired`` 时要点到状态真的变过去才算成功。"""

    def _label_clicked():
        node = _cover_sync_label(page)
        if node is None:
            raise RuntimeError("no sync label")
        node.click(timeout=2500)

    def _label_force():
        node = _cover_sync_label(page)
        if node is None:
            raise RuntimeError("no sync label")
        node.click(timeout=2500, force=True)

    def _wrapper_clicked():
        # 提示气泡盖住 label 时，点外层 div 也会翻转同一个状态
        page.locator(".cover-editor .sync-checkbox-wrapper").first.click(timeout=2500)

    def _input_js():
        inputs = page.locator('.cover-editor .sync-checkbox input[type="checkbox"]')
        if int(inputs.count()) <= 0:
            raise RuntimeError("no sync input")
        inputs.first.evaluate("el => el.click()")

    for action in (_label_clicked, _label_force, _wrapper_clicked, _input_js):
        try:
            action()
        except Exception:
            continue
        page.wait_for_timeout(400)
        after = read_cover_sync(page)
        # 读不回来时按「点到了」算，避免再点一次把它翻回去
        if desired is None or after is None or after == desired:
            return True
    return False


def set_cover_sync(
    page: Any, desired: bool, log: Any = None, warn: Any = None
) -> str:
    """把「双比例同步改动」调成 ``desired``，返回「已勾选」/「未勾选」/「未找到」。"""

    warn = warn or log
    current = read_cover_sync(page)
    if current is None:
        if warn:
            warn(
                "没有找到「双比例同步改动」勾选框，已跳过同步设置，"
                "两张封面会分别上传，不影响继续"
            )
        return "未找到"
    if current == desired:
        return "已勾选" if current else "未勾选"
    if not _click_cover_sync(page, desired):
        if warn:
            warn(
                "「双比例同步改动」点不动，请在上传后到浏览器里手动确认"
                + ("勾选" if desired else "不勾选")
            )
        return "已勾选" if current else "未勾选"
    after = read_cover_sync(page)
    if after is None:
        return "已勾选" if desired else "未勾选"
    if after != desired:
        if warn:
            warn(
                "「双比例同步改动」没有切换成功（现在"
                + ("已勾选" if after else "未勾选")
                + "），请在上传后到浏览器里手动确认"
            )
    return "已勾选" if after else "未勾选"


def cover_ratio_ready(page: Any, ratio: str) -> bool:
    """这个比例的面板里是否已经有封面图（有图就渲染 #editor_4_3 / #editor_16_9）。"""

    node, _attempts = first_visible(page, COVER_RATIO_PANEL_SELECTORS[ratio], timeout=400)
    return node is not None


def _wait_cover_ratio_ready(
    page: Any, ratio: str, timeout: float = 15.0, poll: float = 400
) -> bool:
    now = time.time
    deadline = now() + timeout
    while now() < deadline:
        if cover_ratio_ready(page, ratio):
            return True
        if not _sleep(page, poll):
            return False
    return False


def _upload_cover_to_ratio(
    page: Any, ratio: str, path: str, log: Any = None, warn: Any = None
) -> bool:
    """把一张图送进指定比例的面板，并等预览出现。"""

    warn = warn or log
    node = _attached(page, COVER_RATIO_INPUT_SELECTORS[ratio])
    if node is None:
        # 面板里已经有图时没有自己的 input，只能切到该比例再用底部「上传封面」
        if not activate_cover_ratio(page, ratio, log, warn):
            if warn:
                warn(f"没有找到「{ratio}」封面框，这张图没有上传")
            return False
        node = _attached(page, COVER_SHARED_INPUT_SELECTORS)
        if node is None:
            if warn:
                warn(f"没有找到「{ratio}」封面的上传入口，这张图没有上传")
            return False
    try:
        node.set_input_files(path)
    except Exception as exc:
        if warn:
            warn(f"「{ratio}」封面上传失败，请在浏览器中手动上传：{_first_line(exc)}")
        return False
    if not _wait_cover_ratio_ready(page, ratio):
        if warn:
            warn(f"「{ratio}」封面预览没有出现，请在上传后到浏览器里确认这张图")
    return True


def _upload_ratio_covers(
    page: Any, main: str, alt: str, log: Any, warn: Any
) -> bool:
    """两张图分别进 4:3 / 16:9 两个框；返回是否至少送进去一张。"""

    two = bool(main and alt)
    # 勾选框要先有一个 activeEditor 才会生效，先点一下 4:3 的标题
    activate_cover_ratio(page, COVER_RATIO_43, log, warn)
    state = set_cover_sync(page, desired=not two, log=log, warn=warn)
    log(
        f"同步勾选状态：{state}"
        + ("（两张封面各自独立）" if two else "（两个比例用同一张图）")
    )

    uploaded = False
    if alt:
        if _upload_cover_to_ratio(page, COVER_RATIO_43, alt, log, warn):
            uploaded = True
            log(f"已上传 4:3 副图（首页推荐封面）：{Path(alt).name}")
    if main:
        activate_cover_ratio(page, COVER_RATIO_169, log, warn)
        if _upload_cover_to_ratio(page, COVER_RATIO_169, main, log, warn):
            uploaded = True
            log(f"已上传 16:9 主图（个人空间封面）：{Path(main).name}")
        if not alt:
            # 只有主图：同步确实勾上时一张就会铺满两个框（B 站会自动填好两个框，
            # 所以只有「已勾选」时才能拿 4:3 面板有图当作同步生效的证据）
            if state == "已勾选" and cover_ratio_ready(page, COVER_RATIO_43):
                log("4:3 副图未单独提供：已用同一张主图填充「首页推荐封面」")
            else:
                activate_cover_ratio(page, COVER_RATIO_43, log, warn)
                if _upload_cover_to_ratio(page, COVER_RATIO_43, main, log, warn):
                    log("4:3 副图未单独提供：16:9 主图也放进了「首页推荐封面」")
                elif warn:
                    warn("「首页推荐封面（4:3）」没有同步到主图，请在浏览器里手动确认")
    return uploaded


def _cover_ratio_structure(page: Any) -> bool:
    """弹窗里能不能认出 4:3 / 16:9 两个比例的封面框。"""

    for ratio in (COVER_RATIO_43, COVER_RATIO_169):
        if _attached(page, COVER_RATIO_INPUT_SELECTORS[ratio]) is not None:
            return True
        if cover_ratio_ready(page, ratio):
            return True
    return _attached(page, COVER_SHARED_INPUT_SELECTORS) is not None


def _open_cover_dialog(page: Any, path: str, log: Any) -> str:
    """点「添加封面」之类的入口。返回 ``"dialog"`` / ``"uploaded"`` / ``"none"``。"""

    for text in COVER_OPEN_TEXTS:
        try:
            with page.expect_file_chooser(timeout=1800) as chooser_info:
                page.get_by_text(text, exact=False).first.click(timeout=2000)
        except Exception:
            # 没有弹文件选择框：可能是新编辑器的「封面制作」弹窗
            if cover_editor_open(page):
                page.wait_for_timeout(800)
                return "dialog"
            continue
        try:
            chooser_info.value.set_files(path)
            page.wait_for_timeout(2000)
        except Exception:
            pass
        if cover_editor_open(page):
            return "dialog"
        log("自定义封面已上传")
        return "uploaded"
    if cover_editor_open(page):
        return "dialog"
    return "none"


def upload_cover(
    page: Any,
    cover_path: str,
    log: Any,
    warn: Any = None,
    cover_path_43: str = "",
) -> bool:
    """上传自定义封面：4:3 副图进「首页推荐封面」，16:9 主图进「个人空间封面」。

    只给一张图时，两个比例都用它，并把「双比例同步改动」勾上。
    """

    warn = warn or log
    main = cover_path if cover_path and os.path.isfile(cover_path) else ""
    alt = cover_path_43 if cover_path_43 and os.path.isfile(cover_path_43) else ""
    if cover_path and not main:
        warn("16:9 主图文件不存在，已跳过")
    if cover_path_43 and not alt:
        warn("4:3 副图文件不存在，已跳过")
    # B 站封面弹窗的 bcc-upload 只收 png/jpeg，webp 会被前端直接拒掉
    for path, label in ((main, "16:9 主图"), (alt, "4:3 副图")):
        suffix = Path(path).suffix.lower() if path else ""
        if path and suffix not in (".png", ".jpg", ".jpeg"):
            warn(f"{label}是 {suffix or '未知格式'}，B 站封面只接受 PNG/JPEG，建议换成 PNG")
    if not main and not alt:
        warn("封面文件不存在，已跳过；B 站会使用自动封面")
        return False
    primary = main or alt

    # 1) 先把「封面制作」弹窗打开；旧编辑器的页面上本来就有图片 input
    dialog_open = cover_editor_open(page)
    candidate = None
    if not dialog_open:
        candidate = find_image_input(page)
        if candidate is None:
            state = _open_cover_dialog(page, primary, log)
            if state == "uploaded":
                return True
            dialog_open = state == "dialog"
        if not dialog_open and candidate is None:
            warn("没有找到封面上传入口，请在浏览器中手动上传封面")
            return False

    # 2) 弹窗里按比例分别上传
    if dialog_open:
        if _cover_ratio_structure(page) and _upload_ratio_covers(
            page, main, alt, log, warn
        ):
            return True
        # 3) 认不出两个比例框时回退：弹窗里随便找一个图片 input
        candidate = _find_cover_image_input(page)
        if candidate is None:
            warn("没有找到封面上传入口，请在浏览器中手动上传封面")
            return False
        try:
            candidate.set_input_files(primary)
            page.wait_for_timeout(2500)
            log(
                "自定义封面已上传到封面制作弹窗："
                "请在浏览器里调整后点「完成」或「确定」"
            )
            return True
        except Exception as exc:
            warn(f"封面上传失败，请在浏览器中手动上传：{_first_line(exc)}")
            return False

    # 4) 旧编辑器：直接把文件塞进页面上那个 input
    try:
        candidate.set_input_files(primary)
        page.wait_for_timeout(2000)
        log("自定义封面已上传")
        return True
    except Exception as exc:
        warn(f"封面上传失败，请在浏览器中手动上传：{_first_line(exc)}")
        return False


COVER_DIALOG_SELECTORS = (
    '[role="dialog"]',
    ".bili-dialog",
    ".bcc-dialog",
    ".vui_dialog",
    ".modal",
    '[class*="dialog"]',
    '[class*="modal"]',
    '[class*="crop"]',
)
COVER_CONFIRM_BUTTONS = (
    'button:has-text("确认封面")',
    'div[role="button"]:has-text("确认封面")',
    'button:has-text("使用此封面")',
    'button:has-text("确定使用")',
    # 新版「封面制作」弹窗底部的「完成」是 div，不是 <button>
    COVER_EDITOR_DONE_SELECTOR,
)
COVER_PROMPT_WORDS = ("封面", "裁剪")
COVER_CONFIRM_WORDS = ("确定", "确认", "完成", "保存", "确认封面", "使用此封面")


def _dialog_has_confirm_button(dialog: Any) -> bool:
    try:
        buttons = dialog.locator("button, [role=\"button\"]")
        total = min(int(buttons.count()), 10)
    except Exception:
        return False
    for index in range(total):
        try:
            text = str(buttons.nth(index).inner_text(timeout=300) or "").strip()
        except Exception:
            continue
        if text in COVER_CONFIRM_WORDS:
            return True
    return False


def cover_confirm_pending(page: Any) -> bool:
    """页面上是否还挂着需要人工确认的封面弹窗。"""

    # 新编辑器的「封面制作」弹窗内容很长，不能套用下面的 600 字长度门槛
    try:
        editors = page.locator(COVER_EDITOR_SELECTOR)
        for index in range(min(int(editors.count()), 3)):
            dialog = editors.nth(index)
            if dialog.is_visible() and _dialog_has_confirm_button(dialog):
                return True
    except Exception:
        pass

    for selector in COVER_CONFIRM_BUTTONS:
        try:
            nodes = page.locator(selector)
            total = min(int(nodes.count()), 3)
        except Exception:
            continue
        for index in range(total):
            try:
                if nodes.nth(index).is_visible():
                    return True
            except Exception:
                continue
    for selector in COVER_DIALOG_SELECTORS:
        try:
            nodes = page.locator(selector)
            total = min(int(nodes.count()), 3)
        except Exception:
            continue
        for index in range(total):
            try:
                dialog = nodes.nth(index)
                if not dialog.is_visible():
                    continue
                text = str(dialog.inner_text(timeout=600) or "")
            except Exception:
                continue
            # 整页容器也会命中 [class*="dialog"]，用长度和确认按钮两道门槛挡掉
            if len(text) > 600:
                continue
            if not any(word in text for word in COVER_PROMPT_WORDS):
                continue
            # 裁剪界面本身就是要人点一下才算数，按钮文案认不全也算待确认
            if _dialog_has_confirm_button(dialog) or "裁剪" in text:
                return True
    return False


def wait_for_cover_confirmed(
    page: Any,
    cancelled: Any = None,
    timeout: float = 300,
    grace: float = 10,
    poll: float = 1500,
    clock: Any = None,
    log: Any = None,
) -> bool:
    """等用户在浏览器里确认封面。

    - 出现过封面确认弹窗、随后消失 → True
    - 一开始就没有弹窗（B 站已自动应用封面）→ 宽限 ``grace`` 秒后 True
    - 弹窗一直不消失 → 超时返回 False
    """

    now = clock or time.time
    started = now()
    deadline = started + timeout
    seen = False
    clear_polls = 0
    announced = False
    while now() < deadline:
        if cancelled is not None and cancelled.is_set():
            return False
        if cover_confirm_pending(page):
            seen = True
            clear_polls = 0
            if not announced:
                announced = True
                if log:
                    log("检测到封面确认框：请在浏览器里调整并点“确认”")
        else:
            clear_polls += 1
            if seen and clear_polls >= 2:
                return True
            if not seen and now() - started >= grace:
                return True
        if not _sleep(page, poll):
            return False
    return False


DECLARATION_SELECTORS = (
    'input[placeholder*="创作声明"]',
    'input[placeholder*="符合您视频内容"]',
    '[class*="declaration"] input',
)
DECLARATION_OPTION_SELECTORS = (
    '[role="option"]',
    ".bili-dropdown li",
    ".vui_dropdown li",
    '[class*="dropdown"] li',
    '[class*="dropdown-panel"] li',
    '[class*="option"]',
)


def _field_text(field: Any) -> str:
    """读取字段当前显示的内容：input 取 value，其他取可见文本。"""

    try:
        value = field.input_value(timeout=800)
        if isinstance(value, str):
            return " ".join(value.split())
    except Exception:
        pass
    try:
        return " ".join(str(field.inner_text(timeout=800) or "").split())
    except Exception:
        return ""


def _declaration_selected(page: Any, value: str) -> bool:
    field = _find_declaration_field(page)
    return field is not None and _field_text(field) == (value or "").strip()


def _find_declaration_field(page: Any) -> Any:
    for selector in DECLARATION_SELECTORS:
        try:
            candidate = page.locator(selector).first
            candidate.wait_for(state="visible", timeout=2500)
            return candidate
        except Exception:
            continue
    return None


def _click_declaration_option(page: Any, value: str) -> bool:
    """在已展开的下拉面板里点目标项，避免命中页面上的不可见同名节点。"""

    for selector in DECLARATION_OPTION_SELECTORS:
        try:
            options = page.locator(selector)
            total = min(int(options.count()), 20)
        except Exception:
            continue
        for index in range(total):
            try:
                option = options.nth(index)
                if not option.is_visible():
                    continue
                text = " ".join(str(option.inner_text(timeout=600) or "").split())
            except Exception:
                continue
            if text and text == value:
                try:
                    option.click(timeout=1500)
                    return True
                except Exception:
                    continue
    try:
        page.get_by_text(value, exact=True).last.click(timeout=2500)
        return True
    except Exception:
        return False


def select_creative_declaration(
    page: Any, value: str, log: Any, warn: Any = None
) -> bool:
    """选择创作声明；点完必须回读字段确认，选不上就返回 False。"""

    warn = warn or log
    value = (value or "内容无需标注").strip()
    field = _find_declaration_field(page)
    if field is None:
        warn("没有找到创作声明字段，请在浏览器中手动选择")
        return False
    if _field_text(field) == value:
        log(f"创作声明已选择：{value}")
        return True
    try:
        field.click(timeout=2500)
    except Exception:
        warn("创作声明字段点不开，请在浏览器中手动选择")
        return False
    page.wait_for_timeout(600)
    _click_declaration_option(page, value)
    page.wait_for_timeout(400)
    actual = _field_text(field)
    if actual == value:
        log(f"创作声明已选择：{value}")
        return True
    warn(f"创作声明没有选中：目标「{value}」，字段当前显示「{actual or '空'}」")
    return False


# 2026-11 实测的新投稿编辑器（creator-monorepo/videoup）结构：
#   标题：.video-title input.input-val（placeholder「请输入稿件标题」）
#   标签：#tag-container .tag-input-wrp input.input-val（placeholder「按回车键Enter创建标签」）
#   标签块：.tag-pre-wrp .label-item-v2-container > p.label-item-v2-content
#   投稿：span.submit-add（文案「立即投稿」，不是 <button>）
# 旧版结构的选择器保留在后面兜底。
TITLE_SELECTORS = (
    ".video-title input",
    'input[placeholder*="稿件标题"]',
    'input[placeholder*="标题"]',
    ".video-title-content input",
)
TAG_INPUT_SELECTORS = (
    "#tag-container input",
    ".tag-input-wrp input",
    'input[placeholder*="回车"]',
    'input[placeholder*="标签"]',
    ".tag-input input",
)
TAG_CHIP_SELECTORS = (
    ".tag-pre-wrp .label-item-v2-content",
    ".label-item-v2-content",
    ".tag-pre-wrp .label-item-v2-container",
    ".tag-pre",
    ".tag-item",
    ".bili-tag",
    ".vui_tag",
    ".tag-input .tag",
)
# 标签容器在、但一个标签块都没有时，要返回 []（空）而不是 None（识别不了）
TAG_CONTAINER_SELECTORS = ("#tag-container", ".tag-input-wrp", ".tag-input")
SUBMIT_BUTTON_SELECTORS = (
    ".submit-button-anchor .submit-add",
    ".submit-add",
    'span:has-text("立即投稿")',
    'text=立即投稿',
    '[class*="submit"]:has-text("立即投稿")',
    'button:has-text("立即投稿")',
    'div[role="button"]:has-text("立即投稿")',
    'button:has-text("确认投稿")',
    'button:has-text("发布")',
)
TAG_CLEAR_LIMIT = 20

# 新编辑器只有一级分区；旧草稿里存的是「单机游戏」这类二级分区，映射到主分区
PARTITION_MAIN_ALIASES = {
    "单机游戏": "游戏",
    "网络游戏": "游戏",
    "电子竞技": "游戏",
    "手机游戏": "游戏",
    "桌游棋牌": "游戏",
    "游戏攻略": "游戏",
    "游戏解说": "游戏",
    "游戏赛事": "游戏",
    "音游": "游戏",
}
PARTITION_AREA_SELECTORS = (".video-human-type", ".selector-container")
PARTITION_READ_SELECTORS = (
    ".video-human-type .select-item-cont",
    ".video-human-type .select-controller",
)
PARTITION_OPEN_SELECTORS = (
    ".video-human-type .select-controller",
    ".video-human-type .select-container",
)
PARTITION_OPTION_SELECTORS = (
    ".video-human-type .drop-list-v2-item",
    '.video-human-type [class*="drop-list"] [class*="item"]',
)


def partition_target(partition: str) -> str:
    value = (partition or "").strip()
    return PARTITION_MAIN_ALIASES.get(value, value or "游戏")


def read_partition(page: Any) -> str:
    node, _ = first_visible(page, PARTITION_READ_SELECTORS, timeout=500)
    if node is None:
        return ""
    try:
        return " ".join(str(node.inner_text(timeout=800) or "").split())
    except Exception:
        return ""


def partition_selected(page: Any, target: str) -> bool:
    return read_partition(page) == (target or "").strip()


def select_partition(
    page: Any, partition: str, log: Any = None, warn: Any = None
) -> bool:
    """把 B 站分区选成草稿对应的主分区（单机游戏 → 游戏）。"""

    warn = warn or log
    target = partition_target(partition)
    current = read_partition(page)
    if current == target:
        if log:
            log(f"分区已是：{target}")
        return True
    opener, attempts = first_visible(page, PARTITION_OPEN_SELECTORS)
    if opener is None:
        if warn:
            warn(
                "没找到分区控件（已尝试："
                + _attempts_text(attempts)
                + "），请手动选择分区"
            )
        return False
    try:
        opener.click(timeout=3000)
        page.wait_for_timeout(800)
    except Exception as exc:
        first_line = str(exc).splitlines()[0][:120]
        if warn:
            warn(f"分区下拉打不开：{first_line}；请手动选择分区")
        return False
    for selector in PARTITION_OPTION_SELECTORS:
        try:
            options = page.locator(selector)
            total = min(int(options.count()), 80)
        except Exception:
            continue
        for index in range(total):
            try:
                option = options.nth(index)
                title = str(option.get_attribute("title") or "").strip()
                text = " ".join(str(option.inner_text(timeout=600) or "").split())
            except Exception:
                continue
            if title == target or text == target:
                try:
                    option.click(timeout=2500)
                    page.wait_for_timeout(600)
                except Exception:
                    continue
                if partition_selected(page, target):
                    if log:
                        log(f"分区已选择：{target}")
                    return True
                break
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    if warn:
        actual = read_partition(page)
        warn(
            f"分区没有自动选中（目标：{target}，页面当前：{actual or '空'}），"
            "请手动选择分区"
        )
    return False


def _attempts_text(attempts: list[str]) -> str:
    return "、".join(attempts) if attempts else "没有可用的候选"


def first_visible(
    page: Any, selectors: tuple[str, ...] | list[str], timeout: int = 1200
) -> tuple[Any, list[str]]:
    """按候选顺序找第一个可见节点，返回 (节点, 尝试记录)。

    ``.first`` 可能命中隐藏的旧节点，所以要把每个候选的前几个匹配都看一眼，
    并把尝试过的选择器记下来，失败时写进日志方便下次定位。
    """

    attempts: list[str] = []
    for selector in selectors:
        try:
            locator = page.locator(selector)
            total = int(locator.count())
        except Exception:
            attempts.append(f"{selector}（定位失败）")
            continue
        if total <= 0:
            attempts.append(f"{selector}（0 个）")
            continue
        for index in range(min(total, 5)):
            try:
                node = locator.nth(index) if total > 1 else locator.first
            except Exception:
                attempts.append(f"{selector}（取不到第 {index + 1} 个）")
                continue
            try:
                node.wait_for(state="visible", timeout=timeout)
            except Exception:
                attempts.append(f"{selector}（第 {index + 1} 个不可见）")
                continue
            return node, attempts
    return None, attempts


def _input_text(node: Any) -> str | None:
    try:
        value = node.input_value(timeout=800)
    except Exception:
        return None
    return value if isinstance(value, str) else None


def fill_title(page: Any, title: str, log: Any = None, warn: Any = None) -> bool:
    """填写标题并回读确认；失败时把尝试过的选择器写进提示。"""

    warn = warn or log
    value = (title or "")[:80]
    node, attempts = first_visible(page, TITLE_SELECTORS)
    if node is None:
        if warn:
            warn(
                "没找到标题输入框（已尝试："
                + _attempts_text(attempts)
                + "），请在浏览器中手动填写"
            )
        return False
    if _input_text(node) == value:
        return True
    try:
        node.fill(value)
    except Exception as exc:
        first_line = str(exc).splitlines()[0][:120]
        if warn:
            warn(f"标题输入框填写失败：{first_line}；请在浏览器中手动填写")
        return False
    actual = _input_text(node)
    if actual is not None and actual != value:
        if warn:
            warn(
                f"标题写入后回读不一致（页面显示「{actual[:24] or '空'}」），请手动确认标题"
            )
        return False
    if log:
        log("标题已填写")
    return True


def find_tag_input(page: Any) -> tuple[Any, list[str]]:
    return first_visible(page, TAG_INPUT_SELECTORS)


def click_submit(page: Any, warn: Any = None) -> bool:
    """点「立即投稿」；找不到时把尝试过的选择器报出来。"""

    node, attempts = first_visible(page, SUBMIT_BUTTON_SELECTORS)
    if node is None:
        if warn:
            warn(
                "没找到「立即投稿」按钮（已尝试："
                + _attempts_text(attempts)
                + "），没有点出投稿"
            )
        return False
    try:
        node.click(timeout=3000)
        return True
    except Exception as exc:
        first_line = str(exc).splitlines()[0][:120]
        if warn:
            warn(f"「立即投稿」按钮点不动：{first_line}")
        return False


def editor_ready(page: Any) -> bool:
    """新投稿编辑器是否已经渲染出标题/标签输入框或投稿按钮。"""

    node, _ = first_visible(page, TITLE_SELECTORS, timeout=400)
    if node is not None:
        return True
    node, _ = first_visible(page, TAG_INPUT_SELECTORS, timeout=400)
    if node is not None:
        return True
    node, _ = first_visible(page, SUBMIT_BUTTON_SELECTORS, timeout=400)
    return node is not None


def page_closed(page: Any) -> bool:
    try:
        if hasattr(page, "is_closed") and page.is_closed():
            return True
        page.url  # 页面已关闭时 Playwright 会抛异常
        return False
    except Exception:
        return True


def _sleep(page: Any, milliseconds: float) -> bool:
    """等待一小段；浏览器被用户关掉时返回 False 而不是抛异常。"""

    try:
        page.wait_for_timeout(milliseconds)
        return True
    except Exception:
        return False


def wait_for_editor(
    page: Any,
    cancelled: Any = None,
    timeout: float = 90,
    poll: float = 1500,
    clock: Any = None,
) -> bool:
    """等投稿编辑器渲染出来；取消或超时返回 False。"""

    now = clock or time.time
    deadline = now() + timeout
    while now() < deadline:
        if cancelled is not None and cancelled.is_set():
            return False
        if page_closed(page):
            return False
        try:
            if editor_ready(page):
                return True
        except Exception:
            pass
        if not _sleep(page, poll):
            return False
    return False


def wait_for_user_step(
    page: Any,
    check: Any = None,
    cancelled: Any = None,
    done: Any = None,
    timeout: float = MANUAL_STEP_TIMEOUT,
    poll: float = 1500,
    clock: Any = None,
) -> str:
    """等用户在浏览器里完成一个中间步骤。

    返回 ``"ok"``（条件达成或用户点了「我已完成」）、``"cancelled"``、
    ``"closed"``（浏览器被关）、``"timeout"``。
    """

    now = clock or time.time
    deadline = now() + timeout
    while now() < deadline:
        if cancelled is not None and cancelled.is_set():
            return "cancelled"
        if page_closed(page):
            return "closed"
        if check is not None:
            try:
                if check():
                    return "ok"
            except Exception:
                pass
        if done is not None and done.is_set():
            return "ok"
        if not _sleep(page, poll):
            return "closed"
    return "timeout"


def wait_for_manual_finish(
    page: Any,
    cancelled: Any = None,
    done: Any = None,
    timeout: float = MANUAL_FINISH_TIMEOUT,
    poll: float = 2000,
    clock: Any = None,
    log: Any = None,
) -> tuple[str, str]:
    """等用户在浏览器里把投稿点出去，返回 (状态, 详情)。

    - ``("submitted", bvid)``：出现 BV 号
    - ``("submitted", "")``：页面确认投稿成功但没抓到 BV
    - ``("done", "")``：用户点了「我已完成」
    - ``("cancelled", "")`` / ``("closed", "")`` / ``("timeout", "")``
    """

    now = clock or time.time
    deadline = now() + timeout
    announced = False
    while now() < deadline:
        if cancelled is not None and cancelled.is_set():
            return "cancelled", ""
        if page_closed(page):
            return "closed", ""
        body = page_text(page)
        try:
            url = str(page.url or "")
        except Exception:
            return "closed", ""
        bvid = extract_bvid(url) or extract_bvid(body)
        if bvid:
            return "submitted", bvid
        toasts = _toast_texts(page)
        if _looks_submitted(page, body, toasts):
            return "submitted", ""
        if done is not None and done.is_set():
            return "done", ""
        if not announced and log:
            announced = True
            log("等待你在浏览器里完成投稿：看到 BV 号或成功提示会自动继续")
        if not _sleep(page, poll):
            return "closed", ""
    return "timeout", ""


def tag_chip_texts(page: Any) -> list[str] | None:
    """页面上已有的标签文本；识别不了页面结构时返回 None，不臆测。"""

    for selector in TAG_CHIP_SELECTORS:
        try:
            chips = page.locator(selector)
            total = min(int(chips.count()), 30)
        except Exception:
            continue
        if total <= 0:
            continue
        texts: list[str] = []
        for index in range(total):
            try:
                raw = str(chips.nth(index).inner_text(timeout=600) or "")
            except Exception:
                continue
            text = raw.replace("×", "").replace("✕", "").strip()
            if text:
                texts.append(text)
        return texts
    for selector in TAG_CONTAINER_SELECTORS:
        try:
            if int(page.locator(selector).count()) > 0:
                return []
        except Exception:
            continue
    return None


def clear_tags(page: Any, locator: Any) -> int:
    """用退格清空标签输入框里已有的标签，返回按了几次。"""

    try:
        locator.click(timeout=1500)
        locator.fill("")
    except Exception:
        pass
    removed = 0
    for _ in range(TAG_CLEAR_LIMIT):
        if tag_chip_texts(page) in (None, []):
            break
        try:
            locator.press("Backspace")
        except Exception:
            break
        removed += 1
        page.wait_for_timeout(150)
    return removed


def verify_tags(page: Any, expected: list[str], log: Any = None) -> bool:
    """核对页面标签与草稿是否一致；页面结构识别不了时返回 True（不误报）。"""

    actual = tag_chip_texts(page)
    if actual is None:
        return True
    missing = [tag for tag in expected if tag not in actual]
    extra = [tag for tag in actual if tag not in expected]
    if not missing and not extra:
        if log:
            log(f"标签核对通过：{len(actual)} 个")
        return True
    if log:
        parts = [f"页面 {len(actual)} 个 / 草稿 {len(expected)} 个"]
        if extra:
            parts.append("页面多出：" + "、".join(extra))
        if missing:
            parts.append("没有填上：" + "、".join(missing))
        log("标签与草稿不一致（" + "；".join(parts) + "）")
    return False


def _add_tag(page: Any, locator: Any, tag: str) -> bool:
    """输入一个标签并回车，等页面上真的出现这个标签块。

    新编辑器是异步校验标签的；``fill`` + ``Enter`` 偶尔不触发组件建块，
    所以失败后用逐字符输入再试一次。
    """

    for attempt in range(2):
        try:
            locator.click(timeout=1000)
            locator.fill("")
            if attempt == 0:
                locator.fill(tag)
            else:
                keyboard = getattr(page, "keyboard", None)
                if keyboard is not None:
                    keyboard.type(tag, delay=20)
                else:
                    locator.fill(tag)
            locator.press("Enter")
        except Exception:
            continue
        for _ in range(5):
            if not _sleep(page, 500):
                return False
            texts = tag_chip_texts(page)
            if texts is None or tag in texts:
                return True
    return False


def fill_tags(
    page: Any, tags: list[str], log: Any = None, warn: Any = None
) -> int:
    if not tags:
        return 0
    warn = warn or log
    locator, attempts = find_tag_input(page)
    if locator is None:
        if warn:
            warn(
                "没找到标签输入框（已尝试："
                + _attempts_text(attempts)
                + "），请手动添加标签"
            )
        return 0
    removed = clear_tags(page, locator)
    if removed and log:
        log(f"已清空投稿页原有的 {removed} 个标签")
    added: list[str] = []
    for tag in tags:
        if _add_tag(page, locator, tag):
            added.append(tag)
            continue
        if warn:
            warn(
                f"标签「{tag}」没有出现在页面上，已停止自动填标签；"
                "请手动检查/添加标签"
            )
        break
    verify_tags(page, tags, warn)
    actual = tag_chip_texts(page)
    if actual is None:
        return len(added)
    return sum(1 for tag in tags if tag in actual)


SUBMIT_BLOCK_SELECTORS = (
    ".bcc-message",
    ".bili-toast",
    ".bili-message",
    ".vui_message",
    ".vui-toast",
    '[role="alert"]',
    '[class*="error-tip"]',
    '[class*="errorTip"]',
)
SUBMIT_BLOCK_WORDS = (
    "请先",
    "请选择",
    "请上传",
    "请确认",
    "请填写",
    "请添加",
    "请设置",
    "不能为空",
    "未选择",
    "未确认",
    "提交失败",
    "投稿失败",
    "不符合",
    "必填",
    "必选",
)
# 只放「一定是报错」的整句；下拉框占位文字（如“请选择分区”）不能放进来，
# 否则投稿页一打开就会被误判成被拦截。
SUBMIT_BLOCK_PHRASES = (
    "请先确认封面",
    "请确认封面",
    "请先上传封面",
    "请先设置封面",
    "封面不能为空",
    "标题不能为空",
    "简介不能为空",
    "请先上传视频",
)
# 投稿前的二次确认弹窗也是“请确认…”字样，不能当成拦截
SUBMIT_PROMPT_HINTS = ("确认发布", "确认投稿", "确认提交", "确认稿件", "稿件信息")
SUBMIT_SUCCESS_PHRASES = (
    "投稿成功",
    "稿件投递成功",
    "提交成功",
    "稿件已提交",
    "投递成功",
)
# 正文里只认明确的整句；「投稿成功后可在…」这类帮助文案不能算成功
SUBMIT_SUCCESS_BODY_PHRASES = (
    "稿件投递成功",
    "稿件已提交",
    "提交成功",
)
SUBMIT_SUCCESS_URL_HINTS = ("upload-manager", "upload/success", "video/success")


def _toast_texts(page: Any) -> list[str]:
    """读取页面上可见的 toast / 提示容器文本。"""

    texts: list[str] = []
    for selector in SUBMIT_BLOCK_SELECTORS:
        try:
            nodes = page.locator(selector)
            total = min(int(nodes.count()), 5)
        except Exception:
            continue
        for index in range(total):
            try:
                node = nodes.nth(index)
                if not node.is_visible():
                    continue
                text = " ".join(str(node.inner_text(timeout=800) or "").split())
            except Exception:
                continue
            if text:
                texts.append(text)
    return texts


def _is_block_message(text: str) -> bool:
    text = (text or "").strip()
    if not text or len(text) > 200:
        return False
    if any(hint in text for hint in SUBMIT_PROMPT_HINTS) and not any(
        word in text for word in ("请先", "不能为空", "失败", "未选择", "未确认")
    ):
        return False
    return any(word in text for word in SUBMIT_BLOCK_WORDS)


def find_submit_block(
    page: Any, body: str | None = None, toasts: list[str] | None = None
) -> str:
    """检测 B 站拦住投稿的校验/toast 提示，命中时返回提示原文。"""

    if toasts is None:
        toasts = _toast_texts(page)
    for text in toasts:
        if _is_block_message(text):
            return text[:200]
    if body is None:
        body = page_text(page)
    for phrase in SUBMIT_BLOCK_PHRASES:
        if phrase in (body or ""):
            return phrase
    return ""


def _title_hint(title: str) -> str:
    return " ".join((title or "").split())[:10]


def find_manager_bvid(page: Any, title: str) -> str:
    """在稿件管理页找本次视频的 BV：只认和本次标题相邻的那个，避免抓到旧视频。"""

    body = page_text(page)
    hint = _title_hint(title)
    if not hint:
        return ""
    index = body.find(hint)
    if index < 0:
        return ""
    return extract_bvid(body[index : index + 2000])


def _looks_submitted(page: Any, body: str, toasts: list[str]) -> bool:
    try:
        url = str(page.url or "").lower()
    except Exception:
        url = ""
    if any(hint in url for hint in SUBMIT_SUCCESS_URL_HINTS):
        return True
    if any(
        phrase in text for text in toasts for phrase in SUBMIT_SUCCESS_PHRASES
    ):
        return True
    return any(phrase in (body or "") for phrase in SUBMIT_SUCCESS_BODY_PHRASES)


def wait_submit_result(
    page: Any,
    cancelled: Any = None,
    timeout: float = 180,
    block_window: float = 12,
    clock: Any = None,
) -> tuple[str, str]:
    """等投稿结果，返回 (状态, 详情)。

    - ``("submitted", bvid)``：出现 BV 号
    - ``("submitted", "")``：页面确认投稿成功，但没抓到 BV 号
    - ``("blocked", 提示原文)``：B 站拦住了投稿
    - ``("timeout", "")``：超时仍没有任何结果
    - ``("cancelled", "")``：用户取消
    - ``("closed", "")``：浏览器被用户关掉了
    """

    now = clock or time.time
    started = now()
    deadline = started + timeout
    while now() < deadline:
        if cancelled is not None and cancelled.is_set():
            return "cancelled", ""
        body = page_text(page)
        try:
            url = str(page.url or "")
        except Exception:
            url = ""
        bvid = extract_bvid(url) or extract_bvid(body)
        if bvid:
            return "submitted", bvid
        toasts = _toast_texts(page)
        reason = find_submit_block(page, body, toasts)
        if reason:
            return "blocked", reason
        if _looks_submitted(page, body, toasts):
            return "submitted", ""
        if not _sleep(page, 600 if now() - started < block_window else 3000):
            return "closed", ""
    return "timeout", ""


def _parse_percent(text: str) -> int | None:
    import re

    matches = re.findall(r"(\d{1,3})\s*%", text or "")
    if not matches:
        return None
    value = max(int(item) for item in matches)
    return max(0, min(100, value))


DESCRIPTION_SELECTORS = (
    ".ql-editor[contenteditable=\"true\"]",
    'div[contenteditable="true"]',
    'textarea[placeholder*="简介"]',
    "textarea",
)


def description_ok(page: Any, description: str) -> bool:
    node, _attempts = first_visible(page, DESCRIPTION_SELECTORS)
    if node is None:
        return False
    try:
        node.fill(description[:2000])
        return True
    except Exception:
        return False
