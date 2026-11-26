from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..accuracy import format_accuracy
from ..adofaitools import clear_token, load_token
from ..ard_parser import ARDProfile
from ..browser import (
    build_diagnostics,
    clean_browser_error,
    friendly_browser_error,
    redact_secrets,
)
from ..compliance import check_draft
from ..config import (
    DEFAULT_SETTINGS,
    Settings,
    base_dir,
    browser_profile_dir,
    level_dir,
    organize_history_dir,
    preview_dir,
)
from ..download_tasks import (
    ACTIVE_STATUSES,
    STATUS_CANCELLING,
    STATUS_PENDING,
    DownloadQueue,
    DownloadTask,
    load_queue,
    save_queue,
)
from ..game_bridge import (
    GameBridgeError,
    chart_file_from_entry,
    get_bridge_status,
    install_bridge_mod,
)
from ..hotkey import GlobalHotkey, bring_window_to_front
from ..histdb import HistoryDB
from ..level_paths import category_for_path
from ..level_library import (
    LevelLibraryDB,
    local_fields_from_profile,
    parse_chart_source,
)
from ..level_organizer import (
    build_organize_plan,
    infer_organize_root,
    latest_manifest,
    list_organize_manifests,
)
from ..models import Draft, LevelInfo
from ..obs_launcher import (
    OBS_SEARCH_LOCATIONS,
    find_obs_executable,
    is_obs_running,
    launch_obs,
)
from ..obs_ws import ObsClient
from ..resources import icon_path
from ..sav import check_strict
from ..submission_status import is_tuf_submitted, next_status_after_bvid
from ..tuf_submitter import (
    DEFAULT_ADOFIA_VERSION,
    format_judgement_summary,
    prepare_submission_judgements,
)
from ..umm import UmmError, launch_game
from ..updates import UpdateError, normalize_repo
from ..wizard_state import (
    clear_draft,
    draft_to_record,
    load_draft,
    save_draft,
)
from ..workers import (
    ARDProfileWorker,
    BatchLevelLookupWorker,
    BiliUploadWorker,
    ChartDownloadWorker,
    GameOpenWorker,
    LevelLookupWorker,
    LevelOrganizeWorker,
    LevelScanWorker,
    LoginWorker,
    SessionCheckWorker,
    TufBrowseWorker,
    TufChartAnalyzeWorker,
    TufMetaWorker,
    TufReviewSyncWorker,
    TufSubmissionListWorker,
    TufSubmitWorker,
    AdofaiToolsLoginWorker,
    AdofaiToolsDownloadWorker,
    AdofaiToolsListWorker,
    UmmActionWorker,
    UmmUpdateWorker,
    UpdateCheckWorker,
)
from .batch_bind import BatchBindDialog
from .chart_library import (
    ChartDetailDialog,
    ChartItem,
    ChartLibraryPage,
    SubmissionQueueDialog,
)
from .chart_analyzer import ChartAnalyzerDialog
from .collection_manager import CollectionManagerDialog
from .disk_organizer import DiskOrganizerDialog
from .download_center import DownloadTasksDialog
from .home_page import HomePage
from .organize_history import OrganizeHistoryDialog
from .pages import (
    HistoryPage,
    LevelProfileDialog,
    PreviewPage,
    SettingsPage,
    TufSubmissionListDialog,
    WorkspacePage,
)
from .shell import AnimatedStack, SideBar, StatusLine
from .theme import apply_theme, theme_bus
from .umm_page import UmmPage
from .widgets import Toast, chip
from .window_placement import (
    DEFAULT_WINDOW_SIZE,
    MIN_VISIBLE_TITLE,
    MIN_WINDOW_SIZE,
    Placement,
    Rect,
    rect_from_value,
    resolve_window_rect,
    title_bar_visible,
)

NAV_ITEMS = [
    ("home", "⌂", "首页", "工具箱首页", "总览、待投稿与常用工具。"),
    ("workspace", "◈", "工作台", "投稿工作台", "发布方式 → 选片 → 谱面 → 文案，一条龙。"),
    ("preview", "✦", "文案预览", "文案预览", "B 站与 TUF 的最终字段，一眼核对。"),
    ("history", "☰", "历史", "投稿历史", "随时载入旧记录，复用文案与判定。"),
    ("library", "▤", "谱面库", "在线 TUF / 本地谱面", "浏览、筛选、下载与管理 TUF 谱面。"),
    ("umm", "⬡", "模组", "内置 UMM 模组加载器", "一键安装 UMM 与 ADOFAI 相关 mod。"),
    ("settings", "⚙", "设置", "设置", "浏览器、模板、账号、OBS 与软件更新。"),
]


def is_safe_profile_path(profile: str | Path, root: Path | None = None) -> bool:
    root_path = (root or base_dir()).resolve()
    target = Path(profile).resolve()
    return root_path in target.parents


def _post_to_ui_thread(owner: QWidget, callback: Any, *args: Any) -> None:
    """Schedule a worker-originated callback on the owner's event loop."""
    QTimer.singleShot(0, owner, lambda: callback(*args))


def _warn_if_not_ui_thread(what: str) -> bool:
    """后台线程直接碰 Qt 控件会触发原生崩溃（0xc0000005），这里只记日志。

    返回 True 表示当前不在主线程。调用方应跳过界面更新，但继续跑下载逻辑，
    绝不抛异常打断后台任务。
    """
    app = QApplication.instance()
    if app is None or QThread.currentThread() is app.thread():
        return False
    logging.getLogger("adofai").warning(
        "%s 在非主线程执行，已跳过这次界面刷新", what
    )
    return True


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.db = HistoryDB()
        self.draft = load_draft() if settings.get("restore_last_draft") else None
        self.draft = self.draft or Draft(partition=settings.get("default_partition") or "单机游戏")
        self._jobs: dict[QThread, Any] = {}
        self._download_queue = DownloadQueue(max_active=2)
        self._download_workers: dict[str, ChartDownloadWorker] = {}
        self._download_threads: dict[str, QThread] = {}
        self._download_dialog: DownloadTasksDialog | None = None
        # 关闭流程中为 True：worker.cancel() 触发的 cancelled/failed 回调
        # 不能覆盖 closeEvent 刚给未完成任务设好的「等待中」。
        self._closing = False
        self._current_history_id = ""
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(700)
        self._save_timer.timeout.connect(self._persist_draft)
        self._busy = False
        self._browser_job = ""
        self._active_browser_worker: Any = None
        self._tuf_list_dialog: Any = None
        # 最近一次浏览器/登录失败原文，设置页「复制诊断信息」会带上（脱敏后）。
        self._last_login_error = ""
        self._meta_loading = False
        self._meta_failed = False
        self._umm_update_worker: UmmUpdateWorker | None = None
        self._umm_action_worker: UmmActionWorker | None = None
        self._umm_action = ""
        self._update_check_worker: UpdateCheckWorker | None = None
        self._tools_login_worker: AdofaiToolsLoginWorker | None = None
        self._obs = ObsClient(self)
        self._obs_launch_timer = QTimer(self)
        self._obs_launch_timer.setSingleShot(True)
        self._obs_launch_timer.setInterval(5000)
        self._obs_launch_timer.timeout.connect(self._obs_test_connection)
        self._startup_update_timer = QTimer(self)
        self._startup_update_timer.setSingleShot(True)
        self._startup_update_timer.setInterval(4000)
        self._startup_update_timer.timeout.connect(self._check_updates_on_startup)
        self._hotkey = GlobalHotkey(self)
        self._hotkey.activated.connect(self._on_hotkey_activated)
        self._hotkey_started = False
        self._active_workers = 0
        self._styleguide_page = None

        self.setWindowTitle("ADOFAI GO")
        self.setWindowIcon(QIcon(str(icon_path())))
        self._window_state_applied = False
        self._restore_maximized = bool(self.settings.get("window_maximized"))
        self._restore_window_geometry()
        self._build_ui()
        self._download_save_error_shown = False
        self._download_save_timer = QTimer(self)
        self._download_save_timer.setSingleShot(True)
        self._download_save_timer.setInterval(1000)
        self._download_save_timer.timeout.connect(self._save_download_queue)
        self._restore_download_queue()
        theme_bus().changed.connect(self._on_theme_changed)
        self._apply_theme()
        self._persist_inferred_level_root()
        self._bind_shortcuts()
        self._load_initial_draft()
        self._refresh_history()
        self._refresh_library()
        shutil.rmtree(preview_dir(), ignore_errors=True)
        if os.environ.get("ADOFAI_NO_AUTOLOGIN") != "1":
            QTimer.singleShot(400, self._check_login)
        if bool(self.settings.get("auto_check_updates", True)):
            self._startup_update_timer.start()

    # ---- 窗口位置 -------------------------------------------------------
    # 位置由我们自己保存和恢复，不再交给 Windows / Qt 猜。保存的矩形是窗口
    # 客户区的屏幕坐标；恢复前用纯函数 window_placement 校验，屏幕断开或
    # 坐标跑到屏幕外时回落到主屏居中。

    def _screen_rects(self) -> tuple[list[Rect], int]:
        screens = QGuiApplication.screens()
        rects = []
        for screen in screens:
            geometry = screen.availableGeometry()
            rects.append(Rect(geometry.x(), geometry.y(), geometry.width(), geometry.height()))
        primary = QGuiApplication.primaryScreen()
        primary_index = 0
        for index, screen in enumerate(screens):
            if screen is primary:
                primary_index = index
                break
        return rects, primary_index

    def _resolve_placement(
        self,
        saved: Rect | None,
        screens: tuple[list[Rect], int] | None = None,
    ) -> Placement:
        rects, primary_index = screens or self._screen_rects()
        return resolve_window_rect(
            saved,
            rects,
            primary_index=primary_index,
            default_size=DEFAULT_WINDOW_SIZE,
            min_size=MIN_WINDOW_SIZE,
        )

    def _apply_placement(self, placement: Placement) -> None:
        rect = placement.rect
        self.setMinimumSize(*placement.minimum_size)
        self.setGeometry(rect.x, rect.y, rect.width, rect.height)

    def _restore_window_geometry(self) -> None:
        saved = rect_from_value(self.settings.get("window_rect"))
        placement = self._resolve_placement(saved)
        if placement.fell_back and saved is not None:
            logging.getLogger("adofai").info(
                "上次的窗口位置 %s 已不在任何屏幕内，改在主屏居中显示", saved.as_list()
            )
        self._apply_placement(placement)

    def _window_rect_for_settings(self) -> Rect:
        # 最大化/最小化时 geometry() 不是用户想要的普通矩形，用 normalGeometry()。
        if self.isMaximized() or self.isMinimized() or self.isFullScreen():
            geometry = self.normalGeometry()
        else:
            geometry = self.geometry()
        return Rect(geometry.x(), geometry.y(), geometry.width(), geometry.height())

    def _save_window_state(self) -> None:
        rect = self._window_rect_for_settings()
        if rect.is_empty:
            return
        self.settings.set("window_rect", rect.as_list())
        self.settings.set("window_maximized", self.isMaximized())
        self.settings.save()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if self._window_state_applied:
            return
        self._window_state_applied = True
        if self._restore_maximized:
            self.showMaximized()
        # 窗口管理器可能在 show 时改写位置，下一轮事件循环按真实边框再校准一次。
        QTimer.singleShot(0, self, self._enforce_window_visible)

    def _enforce_window_visible(self) -> None:
        if self.isMaximized() or self.isMinimized() or self.isFullScreen():
            return
        frame = self.frameGeometry()
        current = Rect(frame.x(), frame.y(), frame.width(), frame.height())
        screens = self._screen_rects()
        # 启动时位置已经算好；这里只兜底「标题栏跑到屏幕外」这一种情况，
        # 不因为窗口比屏幕大就替用户改尺寸或搬位置。
        if title_bar_visible(current, screens[0], MIN_VISIBLE_TITLE):
            return
        placement = self._resolve_placement(current, screens)
        target = placement.rect
        if target.as_list() == current.as_list():
            return
        self.setMinimumSize(*placement.minimum_size)
        # move() 移动的是窗口边框；尺寸用差值调客户区，保证和边框保持一致。
        if (target.x, target.y) != (current.x, current.y):
            self.move(target.x, target.y)
        extra_width = current.width - self.width()
        extra_height = current.height - self.height()
        width = max(1, target.width - extra_width)
        height = max(1, target.height - extra_height)
        if (width, height) != (self.width(), self.height()):
            self.resize(width, height)

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("AppRoot")
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.sidebar = SideBar(
            [(key, label) for key, _icon, label, _title, _subtitle in NAV_ITEMS]
        )
        self.sidebar.navigate.connect(self._navigate)
        self.nav_buttons = self.sidebar.buttons
        self.nav_group = self.sidebar.group

        content_wrap = QWidget()
        content_wrap.setObjectName("ContentWrap")
        wrap_layout = QVBoxLayout(content_wrap)
        wrap_layout.setContentsMargins(10, 10, 10, 10)
        wrap_layout.setSpacing(0)

        sheet = QFrame()
        sheet.setObjectName("ContentSheet")
        main_layout = QVBoxLayout(sheet)
        main_layout.setContentsMargins(1, 1, 1, 1)
        main_layout.setSpacing(0)
        wrap_layout.addWidget(sheet, 1)

        top_bar = QFrame()
        top_bar.setObjectName("TopBar")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(28, 14, 24, 14)
        top_layout.setSpacing(12)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self.page_title = QLabel("投稿工作台")
        self.page_title.setObjectName("PageTitle")
        self.page_subtitle = QLabel("发布方式 → 选片 → 谱面 → 文案，一条龙。")
        self.page_subtitle.setObjectName("PageSubtitle")
        title_box.addWidget(self.page_title)
        title_box.addWidget(self.page_subtitle)
        top_layout.addLayout(title_box, 1)
        self.bili_chip = chip("B 站：未检测")
        self.tuf_chip = chip("TUF：未检测")
        top_layout.addWidget(self.bili_chip)
        top_layout.addWidget(self.tuf_chip)
        self.check_button = QPushButton("检查登录")
        self.check_button.setObjectName("GhostButton")
        self.check_button.clicked.connect(self._on_check_button)
        top_layout.addWidget(self.check_button)
        main_layout.addWidget(top_bar)
        # 谱面库页把页眉收紧（只影响这一页），其它页面保持原样。
        self._top_bar = top_bar
        self._top_bar_layout = top_layout
        self._top_bar_title_box = title_box
        self._top_bar_compact = False

        self.stack = AnimatedStack()
        self.home = HomePage()
        self.workspace = WorkspacePage(self.settings)
        self.publish_panel = self.workspace.publish_panel
        self.preview = PreviewPage(self.settings)
        self.history = HistoryPage()
        self.library_db = LevelLibraryDB()
        self.library = ChartLibraryPage()
        self.umm = UmmPage(self.settings)
        self.settings_page = SettingsPage(self.settings)
        self.pages = {
            "home": self.home,
            "workspace": self.workspace,
            "preview": self.preview,
            "history": self.history,
            "library": self.library,
            "umm": self.umm,
            "settings": self.settings_page,
        }
        for key, _icon, _label, _title, _subtitle in NAV_ITEMS:
            self.stack.addWidget(self.pages[key])
        main_layout.addWidget(self.stack, 1)

        self.status_line = StatusLine()
        self.status_line.set_meta(f"ADOFAI GO v{__version__} · 本地运行")
        main_layout.addWidget(self.status_line)

        root_layout.addWidget(self.sidebar)
        root_layout.addWidget(content_wrap, 1)

        self.status_line.show_message("就绪")
        self.toast = Toast(self)

        self.workspace.changed.connect(self._on_draft_changed)
        self.workspace.lookupRequested.connect(self._lookup_level)
        self.workspace.previewRequested.connect(self._open_preview)
        self.workspace.recentRequested.connect(self._load_record)
        self.workspace.uploadRequested.connect(self._start_upload)
        self.workspace.tufRequested.connect(self._start_tuf)
        self.workspace.bvidCommitted.connect(self._on_bvid_committed)
        self.publish_panel.manualDoneRequested.connect(self._on_manual_done)
        self.publish_panel.manualCancelRequested.connect(self._on_manual_cancel)
        self.workspace.historyRequested.connect(lambda: self._navigate("history"))
        self.workspace.draftRestoreRequested.connect(self._resume_saved_draft)
        self.workspace.openBiliRequested.connect(
            lambda: QDesktopServices.openUrl(
                QUrl("https://member.bilibili.com/platform/upload/video/frame")
            )
        )
        self.home.queueRequested.connect(self._open_submission_queue)
        self.home.analyzeRequested.connect(self._open_chart_analyzer)
        self.home.browseTufRequested.connect(
            lambda: self.open_page("library", "online")
        )
        self.home.importRequested.connect(self._import_level_dir)
        self.home.batchBindRequested.connect(self._open_batch_bind)
        self.home.organizeRequested.connect(self._open_disk_organizer)
        self.home.openDirRequested.connect(self._open_level_dir)
        self.home.ummRequested.connect(lambda: self._navigate("umm"))
        self.home.launchGameRequested.connect(self._launch_game)
        self.home.chartRequested.connect(self._show_chart_detail)
        self.home.recordRequested.connect(self._load_record)
        self.home.guideWorkspaceRequested.connect(lambda: self._navigate("workspace"))
        self.home.obsLaunchRequested.connect(self._obs_launch)
        self.home.obsSettingsRequested.connect(self._open_obs_settings)
        self.preview.editRequested.connect(self._edit_draft)
        self.preview.publishRequested.connect(self._open_publish)
        self.publish_panel.reviewSyncRequested.connect(self._sync_review)
        self.history.loadRequested.connect(self._load_record)
        self.history.deleteRequested.connect(self._delete_record)
        self.history.refreshRequested.connect(self._refresh_history)
        self.history.clearRequested.connect(self._clear_history)
        self.history.reviewSyncRequested.connect(self._sync_review)
        self.history.tufListRequested.connect(self._open_tuf_submission_list)
        self.library.refreshRequested.connect(self._refresh_library)
        self.library.searchRequested.connect(self._browse_tuf)
        self.library.loadMoreRequested.connect(self._browse_tuf_more)
        self.library.downloadRequested.connect(self._download_library_card)
        self.library.downloadTasksRequested.connect(self._show_download_tasks)
        self.library.detailRequested.connect(self._show_chart_detail)
        self.library.queueRequested.connect(self._open_submission_queue)
        self.library.profileRequested.connect(self._show_level_profile)
        self.library.importRequested.connect(self._import_level_dir)
        self.library.pruneRequested.connect(self._prune_library)
        self.library.openDirRequested.connect(self._open_level_dir)
        self.library.redownloadRequested.connect(self._redownload_library_entry)
        self.library.deleteRequested.connect(self._delete_library_entry)
        self.library.deleteManyRequested.connect(self._delete_library_entries)
        self.library.batchBindRequested.connect(self._open_batch_bind)
        self.library.manageCollectionsRequested.connect(self._manage_collections)
        self.library.diskOrganizeRequested.connect(self._open_disk_organizer)
        self.library.organizeHistoryRequested.connect(
            self._open_organize_history_from_library
        )
        self.umm.openFolderRequested.connect(
            lambda path: QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        )
        self.umm.openLogRequested.connect(
            lambda path: QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        )
        self.umm.launchRequested.connect(self._launch_game)
        self.umm.updateRequested.connect(self._start_umm_update)
        self.umm.updateCancelRequested.connect(self._cancel_umm_update)
        self.umm.actionRequested.connect(self._start_umm_action)
        self.umm.toolsLoginRequested.connect(self._login_adofaitools)
        self.umm.toolsRefreshRequested.connect(self._load_adofaitools_mods)
        self.umm.toolsDownloadRequested.connect(self._download_adofaitools_mod)
        self.umm.hotkeyChanged.connect(self._reload_hotkey)
        self.umm.hotkeyRecordingChanged.connect(self._hotkey.set_paused)
        self.settings_page.saved.connect(self._save_settings)
        self.settings_page.loginRequested.connect(self._login)
        self.settings_page.clearLoginRequested.connect(self._clear_login_data)
        self.settings_page.diagnosticsRequested.connect(self._copy_diagnostics)
        self.settings_page.savRequested.connect(self._check_sav)
        self.settings_page.obsLaunchRequested.connect(self._obs_launch)
        self.settings_page.obsTestRequested.connect(self._obs_test_connection)
        self.settings_page.obsStartRequested.connect(self._obs_start_recording)
        self.settings_page.obsStopRequested.connect(self._obs_stop_recording)
        self.settings_page.obsOpenFolderRequested.connect(self._open_folder)
        self.settings_page.updateCheckRequested.connect(self._check_update)
        self.settings_page.updateOpenRequested.connect(self._open_update_page)

        self._obs.test_succeeded.connect(self._on_obs_test_ok)
        self._obs.record_started.connect(self._on_obs_record_started)
        self._obs.record_stopped.connect(self._on_obs_record_stopped)
        self._obs.failed.connect(self._on_obs_failed)

    def _bind_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self._new_draft)
        QShortcut(QKeySequence("Ctrl+J"), self, activated=self._shortcut_publish)
        for index, (key, *_rest) in enumerate(NAV_ITEMS):
            number = 0 if key == "home" else index
            QShortcut(
                QKeySequence(f"Ctrl+{number}"),
                self,
                activated=lambda _=False, k=key: self._navigate(k),
            )

    def start_hotkey_listener(self) -> None:
        self._hotkey_started = True
        self._reload_hotkey()

    def _reload_hotkey(self) -> None:
        if not self._hotkey_started:
            return
        if not bool(self.settings.get("game_hotkey_enabled", True)):
            self._hotkey.stop()
            self.umm.set_hotkey_status("已关闭", False)
            return
        hotkey = str(self.settings.get("game_hotkey") or DEFAULT_SETTINGS["game_hotkey"])
        ok, message = self._hotkey.start(hotkey)
        self.umm.set_hotkey_status(message, ok)

    def _on_hotkey_activated(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        QTimer.singleShot(
            0,
            lambda: bring_window_to_front(int(self.winId())),
        )

    def _load_initial_draft(self) -> None:
        self.workspace.load_draft(self.draft)
        self._navigate("home")
        if self.draft.video_path or self.draft.level or self.draft.title:
            self.toast.show_message("已恢复上次未完成的投稿草稿", "ok")

    def open_page(self, key: str, tab: str = "") -> None:
        if key == "styleguide":
            self._open_styleguide()
            return
        if key in {"analyzer", "analyze"}:
            self._navigate("home")
            QTimer.singleShot(0, self._open_chart_analyzer)
            return
        if key in self.pages:
            self._navigate(key)
            if key == "library" and tab:
                self.library.show_tab(tab)
            elif key == "umm" and tab:
                self.umm.show_tab(1 if tab in {"tools", "adofaitools", "online"} else 0)
            elif key == "settings" and tab in {"obs", "record", "recording"}:
                QTimer.singleShot(0, self.settings_page.focus_obs_section)
            elif key == "workspace" and tab:
                self.workspace.open_step_argument(tab)
            return
        if key:
            available = "、".join([*self.pages, "analyzer"])
            logging.getLogger(__name__).warning(
                "未知页面 %r，已回退首页；可用页面：%s", key, available
            )
            print(
                f"[ADOFAI GO] 未知页面「{key}」，已回退首页。可用页面：{available}",
                file=sys.stderr,
            )
            self._navigate("home")

    def _open_styleguide(self) -> None:
        """懒创建隐藏样张页；不占 7 个导航页的位置。"""
        if self._styleguide_page is None:
            from .styleguide_page import StyleGuidePage

            self._styleguide_page = StyleGuidePage()
            self.stack.addWidget(self._styleguide_page)
        self.stack.setCurrentIndex(self.stack.indexOf(self._styleguide_page))
        self._set_top_bar_compact(False)
        self.page_title.setText("控件样张")
        self.page_subtitle.setText(
            "按钮四态、表单、卡片、Chip、Issue、步骤条、Toast 与状态行。"
        )

    def _navigate(self, key: str) -> None:
        if not self._may_leave_settings(key):
            return
        if key != "workspace":
            self._sync_workspace()
        index = list(self.pages).index(key)
        self.stack.setCurrentIndex(index)
        self.sidebar.set_current(key)
        self._set_top_bar_compact(key == "library")
        for nav_key, _icon, _label, title, subtitle in NAV_ITEMS:
            if nav_key == key:
                self.page_title.setText(title)
                self.page_subtitle.setText(subtitle)
                break
        if key == "home":
            self._refresh_home()
        elif key == "workspace":
            self.workspace.set_publish_mode(self._publish_mode())
        elif key == "preview":
            self.preview.refresh(self.draft)
        elif key == "library":
            self._refresh_library()
            self._ensure_online_browse()
        elif key == "history":
            self._refresh_history()
        elif key == "settings":
            self.settings_page.load()
        elif key == "umm":
            self.umm.refresh()

    def _set_top_bar_compact(self, compact: bool) -> None:
        """页眉紧凑模式：谱面库页顶部留白少一些，其它页恢复默认间距。"""
        compact = bool(compact)
        if compact == self._top_bar_compact:
            return
        self._top_bar_compact = compact
        self._top_bar_layout.setContentsMargins(
            28, 4 if compact else 14, 24, 4 if compact else 14
        )
        self._top_bar_title_box.setSpacing(0 if compact else 2)
        # 立即重排，切页时不出现一帧旧高度。
        self._top_bar_layout.activate()
        self._top_bar.updateGeometry()

    def _may_leave_settings(self, key: str) -> bool:
        """设置页有未保存修改时先确认；返回 False 表示留在设置页。"""
        if key == "settings" or key not in self.pages:
            return True
        if self.stack.currentWidget() is not self.settings_page:
            return True
        if not self.settings_page.is_dirty():
            return True
        choice = self._ask_save_settings()
        if choice == "save":
            self.settings_page.save()
            return True
        if choice == "discard":
            self.settings_page.discard_changes()
            return True
        self.sidebar.set_current("settings")
        return False

    def _ask_save_settings(self) -> str:
        """问用户怎么处理未保存的设置，返回 save / discard / cancel。"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("设置尚未保存")
        box.setText("设置页还有未保存的修改，直接离开会丢失。")
        box.setInformativeText("要保存后再离开吗？")
        save_button = box.addButton("保存并离开", QMessageBox.ButtonRole.AcceptRole)
        discard_button = box.addButton(
            "不保存离开", QMessageBox.ButtonRole.DestructiveRole
        )
        cancel_button = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_button)
        box.setEscapeButton(cancel_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_button:
            return "save"
        if clicked is discard_button:
            return "discard"
        return "cancel"

    def _sync_workspace(self) -> None:
        if not hasattr(self, "workspace"):
            return
        draft = self.workspace.collect()
        self.draft = draft
        if self.settings.get("restore_last_draft"):
            self._save_timer.start()

    def _on_draft_changed(self) -> None:
        self.draft = self.workspace.collect()
        if self.settings.get("restore_last_draft"):
            self._save_timer.start()

    def _persist_draft(self) -> None:
        try:
            save_draft(self.draft)
        except OSError:
            pass

    def _open_preview(self) -> None:
        self._on_draft_changed()
        self._navigate("preview")

    def _open_publish(self) -> None:
        self._on_draft_changed()
        self._navigate("workspace")
        self.workspace.open_publish_step()

    def _edit_draft(self) -> None:
        self._navigate("workspace")
        self.workspace._show_step(3)

    def _resume_saved_draft(self) -> None:
        draft = load_draft()
        if draft is None:
            QMessageBox.information(
                self, "没有草稿", "还没有保存过的草稿，从下一步开始新投稿吧。"
            )
            return
        self.draft = draft
        self.workspace.load_draft(draft)
        self._navigate("workspace")
        self.toast.show_message("已恢复上次草稿", "ok")

    def _shortcut_publish(self) -> None:
        if (
            self.stack.currentWidget() is self.workspace
            and self.workspace.current_step == 4
            and self.draft.bvid
            and self._publish_mode() == "dual"
        ):
            self._start_tuf()
        else:
            self._open_publish()

    def _new_draft(self) -> None:
        if QMessageBox.question(
            self, "新建投稿", "新建会清空当前草稿，继续吗？"
        ) != QMessageBox.StandardButton.Yes:
            return
        clear_draft()
        self.draft = Draft(partition=self.settings.get("default_partition") or "单机游戏")
        self.workspace.load_draft(self.draft)
        self._current_history_id = ""
        self._navigate("workspace")
        self.toast.show_message("已新建投稿草稿", "ok")

    def _lookup_level(self, query: str) -> None:
        worker = LevelLookupWorker(query)
        self._start_worker(
            worker,
            on_finished=self.workspace.show_lookup_results,
            on_failed=self.workspace.set_lookup_error,
        )

    def _load_tuf_meta(self) -> None:
        if self._meta_loading:
            return
        self._meta_loading = True
        self._meta_failed = False
        self.library.set_meta_loading()
        worker = TufMetaWorker()
        self._start_worker(
            worker,
            on_finished=self._on_tuf_meta,
            on_failed=self._on_tuf_meta_failed,
        )

    def _on_tuf_meta(self, meta: dict[str, Any]) -> None:
        self._meta_loading = False
        self.library.set_meta(meta)
        if self.stack.currentWidget() is self.library:
            self._ensure_online_browse()

    def _on_tuf_meta_failed(self, message: str) -> None:
        self._meta_loading = False
        self._meta_failed = True
        self.library.set_meta_error(message)
        if self.stack.currentWidget() is self.library:
            self.library.start_search()

    def _ensure_online_browse(self) -> None:
        if self.library.online_model.items():
            return
        if not self.library.meta():
            if self._meta_failed:
                self.library.start_search()
                return
            self._load_tuf_meta()
            return
        self.library.start_search()

    def _browse_tuf(self, state: dict[str, Any]) -> None:
        worker = TufBrowseWorker(state)
        self._start_worker(
            worker,
            on_finished=lambda payload: self.library.set_page(payload, append=False),
            on_failed=self.library.set_error,
        )

    def _browse_tuf_more(self, state: dict[str, Any]) -> None:
        worker = TufBrowseWorker(state)
        self._start_worker(
            worker,
            on_finished=lambda payload: self.library.set_page(payload, append=True),
            on_failed=self.library.set_error,
        )

    def _level_root(self) -> Path:
        configured = str(self.settings.get("level_dir") or "").strip()
        if configured:
            return level_dir(configured)
        inferred = infer_organize_root(self.library_db.all())
        return inferred or level_dir("")

    def _persist_inferred_level_root(self) -> None:
        if str(self.settings.get("level_dir") or "").strip():
            return
        entries = self.library_db.all()
        if len(entries) < 3:
            return
        inferred = infer_organize_root(entries)
        if inferred is None:
            return
        try:
            if inferred.resolve() == level_dir("").resolve():
                return
        except OSError:
            pass
        self.settings.set("level_dir", str(inferred))
        self.settings.save()
        self.settings_page.load()

    def _download_library_card(self, level: LevelInfo, refresh_metadata: bool = False) -> None:
        if not isinstance(level, LevelInfo) or not level.tuf_id:
            self.toast.show_message("该谱面没有 TUF 编号，无法下载", "warn")
            return
        existing = self._download_queue.find_unfinished(level.tuf_id)
        if existing is not None:
            self.toast.show_message(f"「{existing.title}」已经在下载队列中", "warn")
            self._show_download_tasks()
            return
        task = self._download_queue.add(
            level,
            refresh_metadata=refresh_metadata,
        )
        self.status_line.show_message(f"已加入下载队列：{task.title}")
        self._save_download_queue()
        self._sync_download_ui()
        self._pump_download_queue()

    def _restore_download_queue(self) -> None:
        """重开工具时只恢复任务列表，绝不自动开始下载。"""
        self._download_queue = load_queue(max_active=2)
        self._sync_download_ui()
        unfinished = self._download_queue.unfinished_tasks()
        if unfinished:
            self.status_line.show_message(
                f"有 {len(unfinished)} 个未完成下载任务，可在下载任务中心继续"
            )

    def _save_download_queue(self) -> None:
        timer = getattr(self, "_download_save_timer", None)
        if timer is not None and timer.isActive():
            timer.stop()
        if save_queue(self._download_queue):
            self._download_save_error_shown = False
            return
        if not getattr(self, "_download_save_error_shown", False):
            self._download_save_error_shown = True
            toast = getattr(self, "toast", None)
            if toast is not None:
                toast.show_message(
                    "下载队列保存失败，重启后可能丢失任务",
                    "warn",
                    6000,
                )

    def _resume_download_queue(self) -> None:
        pending = self._download_queue.pending_tasks()
        if not pending:
            return
        self.status_line.show_message(f"继续下载 {len(pending)} 个任务")
        self._pump_download_queue()
        self._save_download_queue()

    def _download_settings(self) -> tuple[str, str, bool, bool]:
        dest = str(self._level_root())
        template = str(self.settings.get("level_filename_template") or "")
        organize = bool(self.settings.get("organize_levels", True))
        extract = bool(self.settings.get("auto_extract_levels", True))
        return dest, template, organize, extract

    def _pump_download_queue(self) -> None:
        while True:
            task = self._download_queue.take_next()
            if task is None:
                break
            try:
                self._start_download_task(task)
            except Exception as exc:
                self._download_queue.mark_failed(task.task_id, str(exc))
                self.toast.show_message(f"下载任务启动失败：{exc}", "bad", 6000)
        self._sync_download_ui()
        self._save_download_queue()

    def _start_download_task(self, task: DownloadTask) -> None:
        dest, template, organize, extract = self._download_settings()
        worker = ChartDownloadWorker(
            task.level,
            dest,
            template,
            refresh_metadata=task.refresh_metadata,
            organize=organize,
            extract=extract,
            cancel_event=threading.Event(),
        )
        task_id = task.task_id
        worker.log.connect(self._on_worker_log)
        # worker 的信号在下载线程发出；连到 lambda 时 lambda 会直接在下载线程执行，
        # 里面再碰 QTableWidget / QProgressBar 就是原生访问违例。统一转发回主线程。
        worker.progress.connect(
            lambda payload, tid=task_id: _post_to_ui_thread(
                self, self._on_download_task_progress, tid, payload
            )
        )
        worker.stage.connect(
            lambda stage, tid=task_id: _post_to_ui_thread(
                self, self._on_download_task_stage, tid, stage
            )
        )
        thread = self._start_worker(
            worker,
            on_finished=lambda payload, tid=task_id: self._on_download_task_completed(
                tid,
                payload,
            ),
            on_failed=lambda message, tid=task_id: self._on_download_task_failed(
                tid,
                message,
            ),
            on_cancelled=lambda message, tid=task_id: self._on_download_task_cancelled(
                tid,
                message,
            ),
            on_thread_finished=lambda tid=task_id: self._on_download_thread_finished(tid),
        )
        self._download_workers[task_id] = worker
        self._download_threads[task_id] = thread

    def _on_download_task_progress(self, task_id: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        try:
            received = int(payload.get("received") or 0)
            total = int(payload.get("total") or 0)
        except (TypeError, ValueError):
            return
        self._download_queue.update_progress(task_id, received, total)
        self._sync_download_ui()
        # 进度信号很密，最多每 1 秒落盘一次。只有定时器空闲时才启动，
        # 否则 start() 会不断重新计时，持续下载时可能一直不落盘。
        if not self._download_save_timer.isActive():
            self._download_save_timer.start()

    def _on_download_task_stage(self, task_id: str, stage: str) -> None:
        # 关闭期间排队的阶段信号会把刚设回 pending 的任务改回 downloading，
        # 直接忽略，保证落盘状态是「等待中」。
        if getattr(self, "_closing", False):
            return
        self._download_queue.set_stage(task_id, stage)
        self._sync_download_ui()
        self._save_download_queue()

    def _on_download_task_completed(self, task_id: str, payload: dict[str, Any]) -> None:
        path = str(payload.get("path") or "")
        self._download_queue.mark_completed(task_id, path)
        if getattr(self, "_closing", False):
            # 关闭前真正跑完的任务保留「已完成」并照常登记谱面库，
            # 但不要再启动队列里剩下的任务（那会让关闭流程重新拉线程）。
            try:
                self._on_chart_downloaded(payload)
            except Exception:  # pragma: no cover - 关闭路径兜底，不能把异常抛回事件循环
                logging.getLogger("adofai").exception("关闭期间登记下载结果失败")
            finally:
                self._save_download_queue()
            return
        try:
            self._on_chart_downloaded(payload)
        finally:
            self._sync_download_ui()
            self._save_download_queue()
            self._pump_download_queue()

    def _on_download_task_failed(self, task_id: str, message: str) -> None:
        if getattr(self, "_closing", False):
            # 线程被强制 quit 可能报错；失败不能盖掉 closeEvent 设好的 pending。
            return
        self._download_queue.mark_failed(task_id, message)
        self.library.online_status.setText(f"下载失败：{message}")
        self.toast.show_message(message, "bad", 6000)
        self._sync_download_ui()
        self._save_download_queue()
        self._pump_download_queue()

    def _on_download_task_cancelled(self, task_id: str, message: str) -> None:
        if getattr(self, "_closing", False):
            # 这是 closeEvent 主动 worker.cancel() 的回声，不是用户取消：
            # 状态由 closeEvent 的 interrupted→pending 逻辑决定。
            return
        self._download_queue.mark_cancelled(task_id, message)
        self.library.online_status.setText("下载已取消，未完成文件已清理")
        self.status_line.show_message(message or "下载已取消")
        self._sync_download_ui()
        self._save_download_queue()
        self._pump_download_queue()

    def _on_download_thread_finished(self, task_id: str) -> None:
        self._download_workers.pop(task_id, None)
        self._download_threads.pop(task_id, None)
        self._sync_download_ui()

    def _cancel_download_task(self, task_id: str) -> None:
        action = self._download_queue.request_cancel(task_id)
        if action == "active":
            worker = self._download_workers.get(task_id)
            if worker is not None:
                worker.cancel()
                self.status_line.show_message("正在取消下载…")
            else:
                self._download_queue.mark_cancelled(task_id, "任务已停止")
        elif action == "pending":
            self.status_line.show_message("已从队列取消下载任务")
        self._sync_download_ui()
        self._save_download_queue()
        if action == "pending":
            self._pump_download_queue()

    def _retry_download_task(self, task_id: str) -> None:
        task = self._download_queue.retry(task_id)
        if task is None:
            return
        self.status_line.show_message(f"已重新加入下载队列：{task.title}")
        self._sync_download_ui()
        self._save_download_queue()
        self._pump_download_queue()

    def _open_download_folder(self, task_id: str) -> None:
        task = self._download_queue.get(task_id)
        if task is None or not task.path:
            self.toast.show_message("这个任务还没有可打开的目录", "warn")
            return
        folder = Path(task.path)
        if folder.is_file():
            folder = folder.parent
        if not folder.is_dir():
            self.toast.show_message("下载目录不存在，可能已被移动或删除", "warn")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _show_download_tasks(self) -> None:
        if self._download_dialog is None:
            dialog = DownloadTasksDialog(self)
            dialog.cancelRequested.connect(self._cancel_download_task)
            dialog.retryRequested.connect(self._retry_download_task)
            dialog.openFolderRequested.connect(self._open_download_folder)
            dialog.resumeRequested.connect(self._resume_download_queue)
            self._download_dialog = dialog
        self._sync_download_ui()
        self._download_dialog.show()
        self._download_dialog.raise_()
        self._download_dialog.activateWindow()

    def _sync_download_ui(self) -> None:
        if _warn_if_not_ui_thread("_sync_download_ui"):
            return
        tasks = self._download_queue.tasks()
        self.library.set_download_tasks(tasks)
        if self._download_dialog is not None:
            self._download_dialog.set_tasks(
                tasks,
                max_active=self._download_queue.max_active,
            )

    def _on_chart_downloaded(self, payload: dict[str, Any]) -> None:
        path = str(payload.get("path") or "")
        level = LevelInfo.from_dict(payload.get("level") or {})
        if level is None:
            return
        folder = Path(path).parent if path else None
        category = ""
        band = ""
        collection = ""
        if folder is not None:
            try:
                relative = folder.relative_to(self._level_root())
                category, band, collection = category_for_path(list(relative.parts))
            except ValueError:
                pass
        entry = self.library_db.upsert(
            level,
            path,
            {
                "source": "download",
                "folder": str(folder) if folder is not None else "",
                "category": category,
                "band": band,
                "collection": collection,
                # 下载 worker 解析失败时保留 TUF 数值，同时把文件问题标出来。
                "parse_error": payload.get("parse_error"),
            },
        )
        self.library_db.save()
        size = int(entry.get("size") or 0)
        if not size and path:
            try:
                size = Path(path).stat().st_size
            except OSError:
                size = 0
        if self.workspace._level is not None and self.workspace._level.tuf_id == level.tuf_id:
            self.workspace.draft.chart_path = path
            self._on_draft_changed()
        self.library.mark_downloaded(level.tuf_id, path, entry)
        self._refresh_library()
        self.toast.show_message("谱面已保存到谱面库", "ok")

    def _on_chart_download_failed(self, message: str) -> None:
        self.library.set_download_failed(message)
        self.toast.show_message(message, "bad", 6000)

    def _show_chart_detail(self, item: ChartItem) -> None:
        if not isinstance(item, ChartItem):
            return
        dialog = ChartDetailDialog(
            item,
            self.library.meta(),
            self.library.records_for(item.level.tuf_id),
            self,
            collections=[name for name, _count in self.library_db.collections()],
        )
        dialog.downloadRequested.connect(
            lambda level: self._download_library_card(level, refresh_metadata=item.source == "local")
        )
        dialog.publishRequested.connect(
            lambda level: self._detail_publish(dialog, level)
        )
        dialog.recordLoadRequested.connect(
            lambda record: self._detail_load_record(dialog, record)
        )
        dialog.profileRequested.connect(self._show_level_profile)
        dialog.bindSearchRequested.connect(
            lambda query: self._search_bind_candidates(dialog, query)
        )
        dialog.bindRequested.connect(
            lambda level: self._bind_library_entry(dialog, item, level)
        )
        dialog.collectionAssignRequested.connect(
            lambda key, name: self._assign_library_collection(dialog, key, name)
        )
        dialog.unbindRequested.connect(
            lambda key: self._unbind_library_entry(dialog, key)
        )
        dialog.variantRequested.connect(
            lambda member: self._apply_library_variant(dialog, item, member)
        )
        dialog.tufFillRequested.connect(
            lambda key: self._fill_tuf_metadata(dialog, item, key)
        )
        dialog.gameOpenRequested.connect(self._open_chart_in_game)
        dialog.analyzeOnlineRequested.connect(
            lambda level_id: self._open_chart_analyzer_from_detail(dialog, level_id)
        )

        def remove_entry(key: str) -> None:
            dialog.accept()
            self._delete_library_entry(key)

        dialog.removeRequested.connect(remove_entry)
        dialog.exec()

    def _open_chart_analyzer_from_detail(
        self, dialog: ChartDetailDialog, level_id: int
    ) -> None:
        dialog.accept()
        QTimer.singleShot(0, lambda: self._open_chart_analyzer(int(level_id)))

    def _ensure_bridge_ready_for_open(self) -> bool:
        status = get_bridge_status(self.settings)
        if not status.get("game_dir"):
            QMessageBox.warning(
                self,
                "未找到游戏目录",
                "请先在「05 模组」页选择或自动探测 ADOFAI 游戏目录。",
            )
            return False
        if status.get("installed") and status.get("version_ok"):
            return True
        if bool(self.settings.get("bridge_auto_install", False)):
            return True
        return self._prompt_bridge_install(bool(status.get("installed")))

    def _prompt_bridge_install(self, updating: bool) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("需要 ADOFAI GO Bridge")
        box.setText(
            "要更新 ADOFAI GO Bridge 吗？"
            if updating
            else "要从软件直接让游戏编辑器打开谱面，需要安装 ADOFAI GO Bridge。"
        )
        box.setInformativeText(
            "安装范围：只写入 Mods\\ADOFAIGOBridge，不会修改游戏本体、存档或其他模组，"
            "之后可以随时卸载。\n\n"
            "不安装也能正常使用谱面库、下载、整理、投稿等桌面功能，"
            "仅无法从软件直接让游戏编辑器打开谱面。\n\n"
            "安装或更新后，如果游戏正在运行，需要完全重启一次游戏。"
        )
        install_button = box.addButton(
            "一键安装并打开" if not updating else "一键更新并打开",
            QMessageBox.ButtonRole.AcceptRole,
        )
        box.addButton("暂不安装", QMessageBox.ButtonRole.RejectRole)
        auto = QCheckBox("以后自动安装，不再询问")
        auto.setChecked(bool(self.settings.get("bridge_auto_install", False)))
        box.setCheckBox(auto)
        box.exec()
        if box.clickedButton() is not install_button:
            self.toast.show_message(
                "已跳过 Bridge 安装；其他桌面功能不受影响",
                "info",
                5000,
            )
            return False
        self.settings.set("bridge_auto_install", bool(auto.isChecked()))
        self.settings.save()
        try:
            install_bridge_mod(self.settings)
        except GameBridgeError as exc:
            QMessageBox.critical(self, "Bridge 安装失败", str(exc))
            return False
        self.umm.refresh()
        return True

    def _open_chart_in_game(self, item: ChartItem) -> None:
        if not isinstance(item, ChartItem) or not item.entry:
            return
        try:
            chart = chart_file_from_entry(item.entry)
        except GameBridgeError as exc:
            QMessageBox.critical(self, "无法在游戏中打开", str(exc))
            return

        if not self._ensure_bridge_ready_for_open():
            return
        self.toast.show_message("正在通知游戏载入谱面…", "info", 3000)
        worker = GameOpenWorker(self.settings, str(chart), auto_install=True)
        self._start_worker(
            worker,
            on_finished=self._on_game_open_finished,
            on_failed=self._on_game_open_failed,
        )

    def _on_game_open_finished(self, result: Any) -> None:
        suffix = "（已启动游戏）" if getattr(result, "launched", False) else ""
        self.toast.show_message(
            f"已载入：{result.chart_path.name}{suffix}；"
            "若被游戏内全屏界面遮挡，按 Esc",
            "ok",
            8000,
        )

    def _on_game_open_failed(self, message: str) -> None:
        QMessageBox.warning(self, "无法在游戏中打开", str(message))

    def _detail_publish(self, dialog: ChartDetailDialog, level: LevelInfo) -> None:
        dialog.accept()
        QTimer.singleShot(0, lambda: self._start_submission_from_level(level))

    def _detail_load_record(
        self, dialog: ChartDetailDialog, record: dict[str, Any]
    ) -> None:
        dialog.accept()
        QTimer.singleShot(0, lambda: self._load_record(record))

    def _open_submission_queue(self) -> None:
        dialog = SubmissionQueueDialog(self.library.pending_items(), self)
        dialog.publishRequested.connect(
            lambda item: self._queue_publish(dialog, item)
        )
        dialog.detailRequested.connect(
            lambda item: self._queue_detail(dialog, item)
        )
        dialog.refreshRequested.connect(
            lambda: dialog.set_items(self.library.pending_items())
        )
        dialog.exec()

    def _queue_publish(
        self, dialog: SubmissionQueueDialog, item: ChartItem
    ) -> None:
        dialog.accept()
        QTimer.singleShot(0, lambda: self._start_submission_from_level(item.level))

    def _queue_detail(
        self, dialog: SubmissionQueueDialog, item: ChartItem
    ) -> None:
        dialog.accept()
        QTimer.singleShot(0, lambda: self._show_chart_detail(item))

    def _open_chart_analyzer(self, level_id: int | None = None) -> None:
        dialog = ChartAnalyzerDialog(self)
        dialog.addRequested.connect(
            lambda level: self._add_analyzed_level(dialog, level)
        )
        dialog.publishRequested.connect(
            lambda level: self._publish_analyzed_level(dialog, level)
        )
        dialog.onlineAnalyzeRequested.connect(
            lambda number: self._analyze_tuf_chart(dialog, number)
        )
        if level_id:
            dialog.request_online(int(level_id))
        dialog.exec()

    def _analyze_tuf_chart(
        self, dialog: ChartAnalyzerDialog, level_id: int
    ) -> None:
        worker = TufChartAnalyzeWorker(int(level_id))
        self._start_worker(
            worker,
            on_finished=lambda payload: self._on_tuf_chart_analyzed(dialog, payload),
            on_failed=lambda message: dialog.set_status(f"在线谱面分析失败：{message}"),
        )

    def _on_tuf_chart_analyzed(
        self, dialog: ChartAnalyzerDialog, payload: dict[str, Any]
    ) -> None:
        from ..chart_analyzer import ChartAnalysis

        raw = payload.get("analysis") if isinstance(payload, dict) else None
        if not isinstance(raw, dict):
            dialog.set_status("在线谱面分析失败：没有返回分析结果")
            return
        try:
            analysis = ChartAnalysis(**raw)
        except TypeError:
            dialog.set_status("在线谱面分析失败：结果格式不正确")
            return
        level = LevelInfo.from_dict(payload.get("level"))
        dialog.present_online(analysis, level)

    def _add_analyzed_level(
        self, dialog: ChartAnalyzerDialog, level: LevelInfo
    ) -> None:
        analysis = dialog.analysis
        if analysis is None or not analysis.ok:
            return
        path = Path(analysis.path)
        category = ""
        band = ""
        collection = ""
        try:
            relative = path.parent.relative_to(self._level_root())
            category, band, collection = category_for_path(list(relative.parts))
        except ValueError:
            pass
        # 分析器只给出物量/中旋；再解析一次拿到自动砖与可击物量，
        # 保证 analyze 来源的条目也是完整的本地数值。
        local: dict[str, Any] = {}
        if analysis.source != "tuf":
            try:
                local = local_fields_from_profile(parse_chart_source(path))
            except Exception:
                local = {}
        for field, value in local.items():
            setattr(level, field, value)
        entry = self.library_db.upsert(
            level,
            path,
            {
                "source": "analyze",
                "category": category,
                "band": band,
                "collection": collection,
                "folder": str(path.parent),
                "game_difficulty": analysis.game_difficulty,
                # 能走到这里说明 analysis.ok，之前的解析失败状态要清掉。
                "parse_error": "",
                **local,
            },
        )
        self.library_db.save()
        self._refresh_library()
        dialog.set_status(
            f"已加入本地谱面库：{entry.get('song') or path.name}"
        )
        self.toast.show_message("已加入本地谱面库", "ok")

    def _publish_analyzed_level(
        self, dialog: ChartAnalyzerDialog, level: LevelInfo
    ) -> None:
        dialog.accept()
        QTimer.singleShot(0, lambda: self._start_submission_from_level(level))

    def _draft_has_content(self) -> bool:
        draft = self.draft
        return bool(
            draft.level
            or draft.video_path
            or draft.cover_path
            or draft.cover_path_43
            or draft.title.strip()
            or draft.bvid
        )

    def _start_submission_from_level(self, level: LevelInfo) -> None:
        if not isinstance(level, LevelInfo):
            return
        if self._draft_has_content():
            answer = QMessageBox.question(
                self,
                "用这张谱面投稿",
                f"当前草稿已有内容，用《{level.song or '未命名谱面'}》新建投稿会清空它，继续吗？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        clear_draft()
        draft = Draft(
            partition=self.settings.get("default_partition") or "单机游戏"
        )
        draft.level = level
        self.draft = draft
        self._current_history_id = ""
        self.workspace.load_draft(draft)
        self.workspace.bind_level(level)
        self.draft = self.workspace.collect()
        self._persist_draft()
        self._navigate("workspace")
        # 从谱面库/待投稿队列/分析器进来的投稿，先回到第 1 步「发布方式」，
        # 让用户自己确认发布方式并选择视频；谱面保持预绑定，第 3 步会直接带上。
        self.workspace.go_step(0)
        self.toast.show_message(
            f"已载入《{level.song or '未命名谱面'}》，先选择发布方式并选择视频；"
            "第 3 步会自动带上这张谱面",
            "ok",
        )

    def _assign_library_collection(
        self,
        dialog: ChartDetailDialog,
        key: str,
        name: str,
    ) -> None:
        try:
            entry = self.library_db.assign_collection(key, name)
        except (KeyError, ValueError) as exc:
            current = self.library_db.get(key) or {}
            dialog.set_collection_result(
                str(current.get("collection") or ""),
                f"合集更新失败：{exc}",
            )
            return
        self.library_db.save()
        self._refresh_library()
        collection = str(entry.get("collection") or "")
        message = (
            f"已归入合集「{collection}」。本地文件未移动。"
            if collection
            else "已移出合集。本地文件未移动。"
        )
        dialog.set_collection_result(collection, message)
        self.toast.show_message(message, "ok", 5000)

    def _manage_collections(self) -> None:
        dialog = CollectionManagerDialog(self.library_db.collections(), self)

        def refresh(message: str) -> None:
            self._refresh_library()
            dialog.set_collections(self.library_db.collections())
            dialog.set_status(message)

        def create(name: str) -> None:
            try:
                created = self.library_db.create_collection(name)
            except ValueError as exc:
                dialog.set_status(str(exc))
                return
            self.library_db.save()
            refresh(f"已新建合集「{created}」。")

        def rename(old: str, new: str) -> None:
            try:
                changed = self.library_db.rename_collection(old, new)
            except ValueError as exc:
                dialog.set_status(str(exc))
                return
            self.library_db.save()
            refresh(f"已改名「{old}」→「{new}」，更新 {changed} 条谱面。")

        def dissolve(name: str) -> None:
            try:
                changed = self.library_db.dissolve_collection(name)
            except ValueError as exc:
                dialog.set_status(str(exc))
                return
            self.library_db.save()
            refresh(f"已解散「{name}」，{changed} 条谱面回到原分类。")

        dialog.createRequested.connect(create)
        dialog.renameRequested.connect(rename)
        dialog.dissolveRequested.connect(dissolve)
        dialog.exec()

    def _open_disk_organizer(self) -> None:
        root = self._organize_root()
        plan = build_organize_plan(self.library_db, root)
        if not plan.items:
            self.toast.show_message("谱面库没有可整理的条目", "warn")
            return
        dialog = DiskOrganizerDialog(
            plan,
            str(self.settings.get("theme") or "dark"),
            undo_available=latest_manifest(organize_history_dir()) is not None,
            parent=self,
        )
        dialog.refreshRequested.connect(
            lambda: self._refresh_disk_organizer(dialog)
        )
        dialog.applyRequested.connect(
            lambda keys: self._start_organize_worker(
                dialog,
                "apply",
                selected_keys=keys,
            )
        )
        dialog.undoRequested.connect(
            lambda: self._start_organize_worker(dialog, "undo")
        )
        dialog.historyRequested.connect(
            lambda: self._open_organize_history(dialog)
        )
        dialog.exec()

    def _organize_root(self) -> Path:
        return self._level_root()

    def _refresh_disk_organizer(self, dialog: DiskOrganizerDialog) -> None:
        if not self._dialog_alive(dialog):
            return
        self.library_db.load()
        plan = build_organize_plan(
            self.library_db,
            self._organize_root(),
        )
        dialog.set_plan(plan)
        dialog.set_undo_available(latest_manifest(organize_history_dir()) is not None)
        dialog.set_status("预览已刷新，尚未移动任何文件。")

    def _start_organize_worker(
        self,
        dialog: DiskOrganizerDialog,
        action: str,
        selected_keys: list[str] | None = None,
    ) -> None:
        if not self._dialog_alive(dialog):
            return
        dialog.set_busy(
            True,
            "正在撤销上次整理…" if action == "undo" else "正在整理磁盘…",
        )
        worker = LevelOrganizeWorker(
            str(self.library_db.path),
            str(self._organize_root()),
            str(organize_history_dir()),
            action=action,
            selected_keys=selected_keys,
        )
        self._start_worker(
            worker,
            on_finished=lambda payload: self._finish_organize_worker(dialog, payload),
            on_failed=lambda message: self._fail_organize_worker(dialog, message),
        )

    def _finish_organize_worker(
        self,
        dialog: DiskOrganizerDialog,
        payload: dict[str, Any],
    ) -> None:
        self.library_db.load()
        self._refresh_library()
        if not self._dialog_alive(dialog):
            return
        plan = build_organize_plan(
            self.library_db,
            self._organize_root(),
        )
        dialog.set_plan(plan)
        dialog.set_busy(False)
        dialog.set_undo_available(latest_manifest(organize_history_dir()) is not None)
        mode = str(payload.get("mode") or "organize")
        moved = int(payload.get("moved") or 0)
        failed = int(payload.get("failed") or 0)
        verb = "撤销" if mode == "undo" else "整理"
        message = f"{verb}完成：移动 {moved} 个文件夹，失败 {failed} 个。"
        manifest = str(payload.get("manifest_path") or "")
        if manifest:
            message += f"\n清单：{manifest}"
        errors = payload.get("errors") or []
        if errors:
            message += "\n首个错误：" + str(errors[0])
        dialog.set_status(message)
        self.toast.show_message(
            f"磁盘{verb}完成：{moved} 个文件夹"
            + (f"，失败 {failed}" if failed else ""),
            "ok" if not failed else "warn",
            6000,
        )

    def _fail_organize_worker(
        self,
        dialog: DiskOrganizerDialog,
        message: str,
    ) -> None:
        self.library_db.load()
        self._refresh_library()
        if self._dialog_alive(dialog):
            dialog.set_busy(False)
            dialog.set_status(f"操作失败：{message}")
        self.toast.show_message(f"磁盘整理失败：{message}", "bad", 7000)

    def _open_organize_history(self, owner: DiskOrganizerDialog) -> None:
        self._show_organize_history(owner)

    def _open_organize_history_from_library(self) -> None:
        self._show_organize_history(None)

    def _show_organize_history(
        self,
        owner: DiskOrganizerDialog | None,
    ) -> None:
        records = list_organize_manifests(organize_history_dir())
        if not records:
            self.toast.show_message("还没有磁盘整理记录", "warn")
            return
        dialog = OrganizeHistoryDialog(
            records,
            str(self.settings.get("theme") or "dark"),
            parent=owner or self,
        )
        dialog.refreshRequested.connect(
            lambda: self._refresh_organize_history(dialog)
        )
        dialog.undoRequested.connect(
            lambda path: self._start_organize_history_undo(
                dialog,
                owner,
                path,
            )
        )
        dialog.undoItemsRequested.connect(
            lambda path, keys: self._start_organize_history_undo(
                dialog,
                owner,
                path,
                selected_keys=keys,
            )
        )
        dialog.exec()
        if owner is not None and self._dialog_alive(owner):
            self._refresh_disk_organizer(owner)

    def _refresh_organize_history(
        self,
        dialog: OrganizeHistoryDialog,
    ) -> None:
        if not self._dialog_alive(dialog):
            return
        current = dialog.selected_record() or {}
        dialog.set_records(
            list_organize_manifests(organize_history_dir()),
            select_path=str(current.get("path") or ""),
        )
        dialog.set_status("整理历史已刷新。")

    def _start_organize_history_undo(
        self,
        dialog: OrganizeHistoryDialog,
        owner: DiskOrganizerDialog | None,
        manifest_path: str,
        selected_keys: list[str] | None = None,
    ) -> None:
        if not self._dialog_alive(dialog):
            return
        dialog.set_busy(
            True,
            "正在撤销选中谱面…"
            if selected_keys is not None
            else "正在撤销所选整理记录…",
        )
        worker = LevelOrganizeWorker(
            str(self.library_db.path),
            str(self._organize_root()),
            str(organize_history_dir()),
            action="undo_manifest",
            manifest_path=manifest_path,
            selected_keys=selected_keys,
        )
        self._start_worker(
            worker,
            on_finished=lambda payload: self._finish_organize_history_undo(
                dialog,
                owner,
                payload,
            ),
            on_failed=lambda message: self._fail_organize_history_undo(
                dialog,
                owner,
                message,
            ),
        )

    def _finish_organize_history_undo(
        self,
        dialog: OrganizeHistoryDialog,
        owner: DiskOrganizerDialog | None,
        payload: dict[str, Any],
    ) -> None:
        self.library_db.load()
        self._refresh_library()
        manifest_path = str(payload.get("manifest_path") or "")
        if self._dialog_alive(dialog):
            dialog.set_records(
                list_organize_manifests(organize_history_dir()),
                select_path=manifest_path,
            )
            dialog.set_busy(False)
            moved = int(payload.get("moved") or 0)
            failed = int(payload.get("failed") or 0)
            message = f"撤销完成：移回 {moved} 个文件夹，失败 {failed} 个。"
            errors = payload.get("errors") or []
            if errors:
                message += "\n首个错误：" + str(errors[0])
            dialog.set_status(message)
            self.toast.show_message(
                f"撤销完成：{moved} 个文件夹"
                + (f"，失败 {failed}" if failed else ""),
                "ok" if not failed else "warn",
                6000,
            )
        if owner is not None and self._dialog_alive(owner):
            self._refresh_disk_organizer(owner)

    def _fail_organize_history_undo(
        self,
        dialog: OrganizeHistoryDialog,
        owner: DiskOrganizerDialog | None,
        message: str,
    ) -> None:
        self.library_db.load()
        self._refresh_library()
        if self._dialog_alive(dialog):
            dialog.set_busy(False)
            dialog.set_status(f"撤销失败：{message}")
        if owner is not None and self._dialog_alive(owner):
            self._refresh_disk_organizer(owner)
        self.toast.show_message(f"撤销失败：{message}", "bad", 7000)

    def _search_bind_candidates(self, dialog: ChartDetailDialog, query: str) -> None:
        raw = query.strip().lstrip("#")
        level_id = int(raw) if raw.isdigit() else None
        worker = LevelLookupWorker("" if level_id else raw, level_id=level_id)
        self._start_worker(
            worker,
            on_finished=lambda results: self._deliver_bind_results(dialog, results),
            on_failed=lambda message: self._deliver_bind_error(dialog, message),
        )

    @staticmethod
    def _dialog_alive(dialog: QWidget) -> bool:
        try:
            dialog.isVisible()
            return True
        except RuntimeError:
            return False

    def _deliver_bind_results(
        self,
        dialog: ChartDetailDialog,
        results: list[LevelInfo],
    ) -> None:
        if self._dialog_alive(dialog):
            dialog.set_bind_results(results)

    def _deliver_bind_error(self, dialog: ChartDetailDialog, message: str) -> None:
        if self._dialog_alive(dialog):
            dialog.set_bind_error(message)

    def _bind_library_entry(
        self,
        dialog: ChartDetailDialog,
        item: ChartItem,
        level: LevelInfo,
    ) -> None:
        if not level or not level.tuf_id:
            dialog.reset_bind_action("绑定失败：没有 TUF 编号")
            return
        if level.dl_link:
            self._apply_bind(dialog, item, level)
            return
        dialog.bind_status.setText("正在获取 TUF 详情…")
        worker = LevelLookupWorker(level_id=int(level.tuf_id))
        self._start_worker(
            worker,
            on_finished=lambda results: self._apply_bind(
                dialog,
                item,
                results[0] if results else level,
            ),
            on_failed=lambda message: self._deliver_bind_error(
                dialog,
                f"获取详情失败：{message}",
            ),
        )

    def _apply_bind(
        self,
        dialog: ChartDetailDialog,
        item: ChartItem,
        level: LevelInfo,
    ) -> None:
        if not self._dialog_alive(dialog):
            return
        key = str((item.entry or {}).get("key") or "")
        if not key or not level.tuf_id:
            dialog.reset_bind_action("绑定失败：条目缺少 key 或 TUF 编号")
            return
        ok, merged, message = self._bind_entry_to_level(key, level, dialog)
        if not ok:
            dialog.reset_bind_action(message)
            return
        self.library_db.save()
        self._refresh_library()
        note = f"，已合并 {merged} 条重复记录" if merged else ""
        self.toast.show_message(f"已绑定 TUF #{level.tuf_id}{note}", "ok")
        dialog.accept()

    def _bind_entry_to_level(
        self,
        key: str,
        level: LevelInfo,
        parent: Any,
        duplicate_mode: str = "ask",
    ) -> tuple[bool, int, str]:
        duplicates = self.library_db.find_by_tuf(int(level.tuf_id), exclude_key=key)
        merge = False
        if duplicates:
            if duplicate_mode == "merge":
                merge = True
            elif duplicate_mode == "keep":
                merge = False
            else:
                choice = self._ask_duplicate(parent, level, duplicates)
                if choice == "cancel":
                    return False, 0, "已取消绑定"
                merge = choice == "merge"
        try:
            self.library_db.bind_tuf(key, level)
            if merge:
                for dup_key, _entry in duplicates:
                    self.library_db.merge_entries(key, dup_key)
        except (KeyError, OSError) as exc:
            return False, 0, f"绑定失败：{exc}"
        return True, len(duplicates) if merge else 0, ""

    def _ask_duplicate(
        self,
        parent: Any,
        level: LevelInfo,
        duplicates: list[tuple[str, dict[str, Any]]],
    ) -> str:
        lines = "\n".join(
            f"· {entry.get('song') or '未命名'}（{entry.get('path') or entry.get('folder') or '无路径'}）"
            for _dup_key, entry in duplicates[:4]
        )
        more = f"\n…另有 {len(duplicates) - 4} 条" if len(duplicates) > 4 else ""
        box = QMessageBox(parent)
        box.setWindowTitle("该 TUF 编号已在谱面库中")
        box.setText(
            f"要绑定的 TUF #{level.tuf_id} 已存在于谱面库：\n{lines}{more}\n\n"
            "合并后只保留当前条目，另一条的索引会移除，磁盘文件不会被删除。"
        )
        merge_button = box.addButton("合并为一条", QMessageBox.ButtonRole.AcceptRole)
        keep_button = box.addButton("保留双份", QMessageBox.ButtonRole.ActionRole)
        cancel_button = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel_button:
            return "cancel"
        return "keep" if clicked is keep_button else "merge"

    def _unbind_library_entry(self, dialog: ChartDetailDialog, key: str) -> None:
        entry = self.library_db.get(key)
        if entry is None:
            return
        name = entry.get("song") or entry.get("filename") or key
        if QMessageBox.question(
            dialog,
            "解除 TUF 绑定",
            f"确定解除「{name}」的 TUF 绑定？\n本地文件不会被修改或删除。",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.library_db.unbind_tuf(key)
        self.library_db.save()
        self._refresh_library()
        self.toast.show_message("已解除 TUF 绑定", "ok")
        dialog.accept()

    def _apply_library_variant(
        self,
        dialog: ChartDetailDialog,
        item: ChartItem,
        member: str,
    ) -> None:
        """切换到谱面包里的某个玩法变体，并原地刷新详情。"""
        if not self._dialog_alive(dialog):
            return
        key = str((item.entry or {}).get("key") or "")
        if not key:
            dialog.set_variant_error("切换失败：条目缺少 key")
            return
        try:
            entry = self.library_db.set_variant(key, member)
        except (KeyError, OSError, ValueError) as exc:
            dialog.set_variant_error(f"切换失败：{exc}")
            return
        self.library_db.save()
        self._refresh_library()
        item.entry = entry
        level = LevelInfo.from_dict(entry)
        if level is not None:
            item.level = level
        dialog.refresh_entry(entry, note=f"已切换到变体：{member}")
        self.toast.show_message(f"已切换谱面变体：{member}", "ok")

    def _fill_tuf_metadata(
        self,
        dialog: ChartDetailDialog,
        item: ChartItem,
        key: str,
    ) -> None:
        """从 TUF 拉取已识别编号的详情并绑定。"""
        if not self._dialog_alive(dialog):
            return
        entry = self.library_db.get(key) or item.entry or {}
        try:
            tuf_id = int(entry.get("tuf_id") or 0)
        except (TypeError, ValueError):
            tuf_id = 0
        if tuf_id <= 0:
            dialog.set_tuf_fill_status("补齐失败：条目没有可用的 TUF 编号", error=True)
            return
        dialog.set_tuf_fill_status(f"正在从 TUF 获取 #{tuf_id}…")
        worker = LevelLookupWorker(level_id=tuf_id)
        self._start_worker(
            worker,
            on_finished=lambda results: self._finish_tuf_fill(
                dialog, item, key, results[0] if results else None
            ),
            on_failed=lambda message: self._deliver_tuf_fill_error(dialog, message),
        )

    def _deliver_tuf_fill_error(
        self, dialog: ChartDetailDialog, message: str
    ) -> None:
        if self._dialog_alive(dialog):
            dialog.set_tuf_fill_status(f"补齐失败：{message}", error=True)

    def _finish_tuf_fill(
        self,
        dialog: ChartDetailDialog,
        item: ChartItem,
        key: str,
        level: LevelInfo | None,
    ) -> None:
        if not self._dialog_alive(dialog):
            return
        if level is None:
            dialog.set_tuf_fill_status("补齐失败：TUF 没有返回这张谱面", error=True)
            return
        entry = self.library_db.get(key) or item.entry or {}
        mismatched: list[str] = []
        for field, label in (("song", "曲名"), ("artist", "曲师"), ("charter", "谱师")):
            local_value = str(entry.get(field) or "").strip()
            remote_value = str(getattr(level, field, "") or "").strip()
            if local_value and remote_value and local_value != remote_value:
                mismatched.append(label)
        try:
            self.library_db.bind_tuf(key, level)
        except (KeyError, OSError) as exc:
            dialog.set_tuf_fill_status(f"补齐失败：{exc}", error=True)
            return
        self.library_db.save()
        self._refresh_library()
        updated = self.library_db.get(key) or {}
        item.entry = updated
        new_level = LevelInfo.from_dict(updated)
        if new_level is not None:
            item.level = new_level
        note = ""
        if mismatched:
            note = (
                "本地谱面信息与 TUF 不一致（"
                + "、".join(mismatched)
                + "），已按 TUF 显示。"
            )
        dialog.refresh_entry(updated, note=note)
        self.toast.show_message(f"已从 TUF 补齐 #{level.tuf_id}", "ok")

    def _open_batch_bind(self) -> None:
        entries = [entry for entry in self.library_db.all() if not entry.get("tuf_id")]
        if not entries:
            self.toast.show_message("没有未匹配的谱面", "ok")
            return
        dialog = BatchBindDialog(
            entries,
            str(self.settings.get("theme") or "dark"),
            self,
        )
        self._batch_worker = None
        self._batch_dialog: BatchBindDialog | None = None
        dialog.searchRequested.connect(
            lambda targets: self._start_batch_search(dialog, targets)
        )
        dialog.bindRequested.connect(
            lambda rows: self._bind_batch_entries(dialog, rows)
        )
        dialog.cancelSearchRequested.connect(self._stop_batch_search)
        self._batch_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._batch_dialog = None
            self._batch_worker = None

    def _start_batch_search(self, dialog: BatchBindDialog, targets: list[dict[str, Any]]) -> None:
        if not self._dialog_alive(dialog) or not targets:
            return
        self._stop_batch_search()
        worker = BatchLevelLookupWorker(targets)
        self._batch_worker = worker
        self._batch_dialog = dialog
        worker.progress.connect(
            self._deliver_batch_results,
            Qt.ConnectionType.QueuedConnection,
        )
        worker.finished.connect(
            self._deliver_batch_finished,
            Qt.ConnectionType.QueuedConnection,
        )
        worker.failed.connect(
            self._deliver_batch_error,
            Qt.ConnectionType.QueuedConnection,
        )
        self._start_worker(worker)

    def _stop_batch_search(self) -> None:
        worker = getattr(self, "_batch_worker", None)
        if worker is not None:
            worker.cancel()

    def _deliver_batch_results(self, key: str, results: list[dict[str, Any]]) -> None:
        dialog = self._batch_dialog
        if dialog is not None and self._dialog_alive(dialog):
            dialog.set_row_results(key, results)

    def _deliver_batch_finished(self, count: int) -> None:
        dialog = self._batch_dialog
        if dialog is not None and self._dialog_alive(dialog):
            dialog.set_search_finished(count)

    def _deliver_batch_error(self, message: str) -> None:
        dialog = self._batch_dialog
        if dialog is not None and self._dialog_alive(dialog):
            dialog.set_error(message)

    def _bind_batch_entries(
        self,
        dialog: BatchBindDialog,
        rows: list[tuple[str, LevelInfo]],
    ) -> None:
        if not self._dialog_alive(dialog) or not rows:
            return
        conflicts = 0
        for key, level in rows:
            if not level.tuf_id:
                continue
            if self.library_db.find_by_tuf(int(level.tuf_id), exclude_key=key):
                conflicts += 1
        mode = "ask"
        if conflicts:
            box = QMessageBox(dialog)
            box.setWindowTitle("批量绑定：发现重复编号")
            box.setText(
                f"有 {conflicts} 条要绑定的编号已在谱面库中。\n"
                "要如何处理这些重复记录？"
            )
            merge_all = box.addButton("全部合并", QMessageBox.ButtonRole.AcceptRole)
            keep_all = box.addButton("全部保留双份", QMessageBox.ButtonRole.ActionRole)
            box.addButton("逐条询问", QMessageBox.ButtonRole.ActionRole)
            cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is cancel:
                dialog.refresh_bind_button()
                return
            if clicked is merge_all:
                mode = "merge"
            elif clicked is keep_all:
                mode = "keep"
        bound_keys: set[str] = set()
        skipped = 0
        for key, level in rows:
            if not level.tuf_id:
                skipped += 1
                continue
            ok, _merged, _message = self._bind_entry_to_level(key, level, dialog, mode)
            if ok:
                bound_keys.add(key)
            else:
                skipped += 1
        if bound_keys:
            self.library_db.save()
            self._refresh_library()
        self.toast.show_message(
            f"批量绑定完成：成功 {len(bound_keys)} 条，未绑定 {skipped} 条",
            "ok" if not skipped else "warn",
            6000,
        )
        dialog.mark_results(bound_keys, skipped)

    def _show_level_profile(self, path: str) -> None:
        if not path or not Path(path).is_file():
            self.toast.show_message("谱面文件不存在，请重新下载", "warn")
            return
        worker = ARDProfileWorker(path)
        self._start_worker(
            worker,
            on_finished=self._on_profile_ready,
            on_failed=lambda text: self.toast.show_message(text, "bad", 6000),
        )

    def _on_profile_ready(self, payload: dict[str, Any]) -> None:
        dialog = LevelProfileDialog(ARDProfile(**payload), self)
        dialog.exec()

    def _open_level_dir(self) -> None:
        target = self._level_root()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.toast.show_message(f"无法创建谱面目录：{exc}", "bad")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _open_folder(self, path: str) -> None:
        folder = Path(path)
        if folder.is_file():
            folder = folder.parent
        if not folder.is_dir():
            self.toast.show_message("录制保存目录不存在，可能已被移动或删除", "warn")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _launch_game(self) -> None:
        try:
            exe = launch_game(self.settings)
        except UmmError as exc:
            self.toast.show_message(str(exc), "bad", 6000)
            return
        self.toast.show_message(f"已启动 ADOFAI：{exe.name}", "ok")

    def _refresh_library(self) -> None:
        self.library.set_collection_names(
            [name for name, _count in self.library_db.collections()]
        )
        self.library.set_local_entries(self.library_db.all())
        self._refresh_home()

    def _refresh_home(self) -> None:
        if not hasattr(self, "home"):
            return
        entries = [
            entry for entry in self.library_db.all() if isinstance(entry, dict)
        ]
        records = self.db.all()

        matched_ids: set[int] = set()
        for entry in entries:
            try:
                matched_ids.add(int(entry.get("tuf_id")))
            except (TypeError, ValueError):
                continue
        submitted_ids: set[int] = set()
        for record in records:
            if not is_tuf_submitted(record):
                continue
            try:
                submitted_ids.add(int(record.get("tuf_id")))
            except (TypeError, ValueError):
                continue

        recent_entries = sorted(
            entries,
            key=lambda entry: str(entry.get("updated_at") or ""),
            reverse=True,
        )[:5]
        recent_charts: list[ChartItem] = []
        for entry in recent_entries:
            level = LevelInfo.from_dict(entry)
            if level is None:
                continue
            recent_charts.append(
                ChartItem(
                    level=level,
                    source="local",
                    entry=dict(entry),
                    downloaded=True,
                    matched=bool(level.tuf_id),
                    submitted=1 if level.tuf_id and int(level.tuf_id) in submitted_ids else 0,
                )
            )

        self.home.set_data(
            local_count=len(entries),
            matched_count=len(matched_ids),
            pending_count=len(matched_ids - submitted_ids),
            submitted_count=len(matched_ids & submitted_ids),
            collection_count=len(self.library_db.collections()),
            recent_records=records,
            recent_charts=recent_charts,
        )

    def _adopt_import_root(self, directory: str) -> None:
        selected = Path(directory).resolve()
        configured = str(self.settings.get("level_dir") or "").strip()
        try:
            effective = self._level_root().resolve()
        except OSError:
            effective = selected
        if configured:
            try:
                configured_path = level_dir(configured).resolve()
            except OSError:
                configured_path = Path(configured)
            if selected == configured_path or configured_path in selected.parents:
                return
        elif selected == effective or effective in selected.parents:
            if selected == effective:
                self.settings.set("level_dir", str(selected))
                self.settings.save()
                self.settings_page.load()
            return
        if QMessageBox.question(
            self,
            "设为默认谱面目录",
            f"是否把下面的目录设为默认谱面目录？\n\n{selected}\n\n"
            "导入本身只登记，不复制、不移动文件。设为默认后，"
            "TUF 新下载和磁盘整理都会以这里为根目录。",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.settings.set("level_dir", str(selected))
        self.settings.save()
        self.settings_page.load()
        self.toast.show_message("已将导入目录设为默认谱面目录", "ok", 5000)

    def _import_level_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            "选择包含谱面文件或压缩包的目录",
            str(self._level_root()),
        )
        if not directory:
            return
        self._adopt_import_root(directory)
        self.status_line.show_message("正在扫描谱面目录…")
        self.toast.show_message("正在扫描谱面目录，谱面多时需要一会儿…", "ok", 4000)
        worker = LevelScanWorker(str(self.library_db.path), directory)
        self._start_worker(
            worker,
            on_finished=self._on_scan_finished,
            on_failed=lambda message: self.toast.show_message(message, "bad", 6000),
        )

    def _on_scan_finished(self, payload: dict[str, Any]) -> None:
        self.library_db.load()
        self._refresh_library()
        count = int(payload.get("count") or 0)
        created = int(payload.get("created", count) or 0)
        updated = int(payload.get("updated") or 0)
        failed = int(payload.get("failed") or 0)
        skipped = int(payload.get("skipped") or 0)
        detail = f"新增 {created}，更新 {updated}" if updated else f"新增 {created}"
        if failed:
            detail += f"，{failed} 个解析失败"
        if skipped:
            detail += f"，跳过 {skipped} 个无谱面包"
        self.status_line.show_message(f"扫描完成：{count} 个谱面（{detail}）")
        if skipped:
            self.toast.show_message(
                f"扫描完成，跳过 {skipped} 个无谱面包（安装包/发布包等不含 .adofai 的文件）",
                "warn",
                7000,
            )
        elif failed:
            self.toast.show_message(
                f"已导入 {created} 个谱面，更新 {updated} 个；"
                f"{failed} 个谱面解析失败，卡片上有警告提示",
                "warn",
                7000,
            )
        else:
            self.toast.show_message(f"已导入 {created} 个谱面，更新 {updated} 个", "ok")

    def _prune_library(self) -> None:
        removed = self.library_db.prune_missing()
        if not removed:
            self.toast.show_message("没有失效条目", "ok")
            return
        self.library_db.save()
        self._refresh_library()
        self.toast.show_message(f"已清理 {len(removed)} 个失效条目", "ok")

    def _redownload_library_entry(self, entry: dict[str, Any]) -> None:
        level = LevelInfo.from_dict(entry)
        if level is None or not level.tuf_id:
            self.toast.show_message("该条目没有 TUF 编号，无法重新下载", "warn")
            return
        self._download_library_card(level, refresh_metadata=True)

    def _delete_library_entry(self, key: str) -> None:
        entry = self.library_db.get(key)
        if entry is None:
            return
        name = entry.get("song") or entry.get("filename") or key
        if QMessageBox.question(
            self,
            "从谱面库移除",
            f"确定从谱面库移除「{name}」？\n本地文件不会被删除。",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.library_db.delete(key, delete_file=False)
        self.library_db.save()
        self._refresh_library()

    def _delete_library_entries(self, keys: list[str]) -> None:
        """批量从谱面库索引移除；只删索引，不动磁盘文件。"""
        wanted = [str(key) for key in keys if str(key)]
        if not wanted:
            return
        if len(wanted) == 1:
            self._delete_library_entry(wanted[0])
            return
        if QMessageBox.question(
            self,
            "从谱面库移除",
            f"确定从谱面库移除选中的 {len(wanted)} 个谱面？\n本地文件不会被删除。",
        ) != QMessageBox.StandardButton.Yes:
            return
        removed = sum(
            1
            for key in wanted
            if self.library_db.delete(key, delete_file=False)
        )
        if not removed:
            return
        self.library_db.save()
        self._refresh_library()
        self.toast.show_message(
            f"已从谱面库移除 {removed} 个谱面，本地文件未删除",
            "ok",
        )

    def _start_worker(
        self,
        worker: Any,
        on_finished: Any = None,
        on_failed: Any = None,
        on_cancelled: Any = None,
        on_thread_finished: Any = None,
    ) -> QThread:
        self._prune_jobs()
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        if on_finished is not None:
            worker.finished.connect(
                lambda *args: _post_to_ui_thread(self, on_finished, *args)
            )
        if on_failed is not None:
            worker.failed.connect(
                lambda *args: _post_to_ui_thread(self, on_failed, *args)
            )
        if on_cancelled is not None and hasattr(worker, "cancelled"):
            worker.cancelled.connect(
                lambda *args: _post_to_ui_thread(self, on_cancelled, *args)
            )
        worker.finished.connect(thread.quit)
        if hasattr(worker, "failed"):
            worker.failed.connect(thread.quit)
        if hasattr(worker, "cancelled"):
            worker.cancelled.connect(thread.quit)
        if on_thread_finished is not None:
            # QThread.finished 不收参数；调用方传进来的多是 lambda，统一回主线程再跑。
            thread.finished.connect(
                lambda: _post_to_ui_thread(self, on_thread_finished)
            )
        thread.finished.connect(self._on_worker_thread_finished)
        self._jobs[thread] = worker
        self._active_workers += 1
        self.status_line.set_busy(True)
        thread.start()
        return thread

    def _on_worker_thread_finished(self) -> None:
        self._active_workers = max(0, self._active_workers - 1)
        if hasattr(self, "status_line"):
            self.status_line.set_busy(self._active_workers > 0)

    def _prune_jobs(self) -> None:
        for thread in list(self._jobs):
            if not thread.isRunning():
                self._jobs.pop(thread, None)

    def _on_browser_job_finished(self) -> None:
        self._busy = False
        self._browser_job = ""
        self._active_browser_worker = None
        self.check_button.setEnabled(True)
        self.check_button.setText("检查登录")
        self.check_button.setToolTip("")
        if hasattr(self, "history"):
            self.history.review_sync_button.setEnabled(True)
            self.history.tuf_list_button.setEnabled(True)
        # 登录检查会刷掉状态栏，结束后把未完成下载的提示补回来。
        unfinished = self._download_queue.unfinished_tasks()
        if unfinished:
            self.status_line.show_message(
                f"有 {len(unfinished)} 个未完成下载任务，可在下载任务中心继续"
            )

    def _on_browser_log(self, text: str) -> None:
        self.status_line.show_message(text)

    def _on_worker_log(self, text: str) -> None:
        self.status_line.show_message(text)

    def _on_upload_needs_login(self, text: str) -> None:
        self.toast.show_message(text, "warn", 6000)

    def _on_check_button(self) -> None:
        if self._busy:
            if self._browser_job == "login":
                self._cancel_login()
            return
        self._check_login()

    def _check_login(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._browser_job = "check"
        self.check_button.setEnabled(False)
        self.check_button.setText("检查中…")
        self.bili_chip.setText("B 站：检查中")
        self.tuf_chip.setText("TUF：检查中")
        worker = SessionCheckWorker(self.settings)
        self._active_browser_worker = worker
        worker.log.connect(self._on_browser_log)
        self._start_worker(
            worker,
            on_finished=self._on_session_checked,
            on_failed=self._on_session_failed,
            on_thread_finished=self._on_browser_job_finished,
        )

    def _on_session_checked(self, data: dict[str, Any]) -> None:
        bili = bool(data.get("bili"))
        tuf = bool(data.get("tuf"))
        tuf_error = str(data.get("tuf_error") or "")
        self._set_account_chips(bili, tuf, data.get("tuf_name", ""), tuf_error)
        self.publish_panel.set_accounts(bili, tuf, tuf_error)
        self.settings_page.set_login_status(bili, tuf)
        if bili and tuf:
            self.toast.show_message("双站登录态正常", "ok")
        elif tuf_error:
            self.toast.show_message(
                "TUF 服务暂不可用，稍后再点“检查登录”重试", "warn", 6000
            )
        else:
            self.toast.show_message("有站点未登录，点“检查登录”旁的浏览器登录入口", "warn")

    def _on_session_failed(self, message: str) -> None:
        self.bili_chip.setText("B 站：未检测")
        self.tuf_chip.setText("TUF：未检测")
        self._last_login_error = redact_secrets(str(message))
        logging.getLogger("adofai").error(
            "登录态检查失败：%s", self._last_login_error
        )
        self.toast.show_message(
            f"登录态检查失败：{friendly_browser_error(message)}",
            "error",
            8000,
        )

    def _set_account_chips(
        self, bili: bool, tuf: bool, tuf_name: str = "", tuf_error: str = ""
    ) -> None:
        self.bili_chip.setText("B 站：已登录" if bili else "B 站：未登录")
        self.bili_chip.setObjectName("ChipOk" if bili else "ChipBad")
        if tuf:
            self.tuf_chip.setText(f"TUF：{tuf_name}" if tuf_name else "TUF：已登录")
            self.tuf_chip.setObjectName("ChipOk")
        elif tuf_error:
            # 服务端临时故障不能显示成「未登录」，否则会误导去重新登录。
            self.tuf_chip.setText("TUF：服务暂不可用")
            self.tuf_chip.setObjectName("ChipWarn")
        else:
            self.tuf_chip.setText("TUF：未登录")
            self.tuf_chip.setObjectName("ChipBad")
        for widget in (self.bili_chip, self.tuf_chip):
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def _login(self) -> None:
        if self._busy:
            self.toast.show_message("当前有浏览器任务在执行，请先等它结束或关闭登录窗口", "warn", 6000)
            return
        self._busy = True
        self._browser_job = "login"
        self.check_button.setEnabled(True)
        self.check_button.setText("取消登录")
        self.check_button.setToolTip("取消本次登录并释放工具浏览器")
        worker = LoginWorker(self.settings)
        self._active_browser_worker = worker
        worker.log.connect(self._on_browser_log)
        self._start_worker(
            worker,
            on_finished=self._on_login_finished,
            on_failed=self._on_login_failed,
            on_thread_finished=self._on_browser_job_finished,
        )
        self.toast.show_message(
            "请在打开的浏览器里登录 B 站与 TUF；关闭浏览器窗口可取消登录",
            "info",
            7000,
        )

    def _cancel_login(self) -> None:
        worker = self._active_browser_worker
        if worker is None or not hasattr(worker, "cancel"):
            return
        worker.cancel()
        self.check_button.setEnabled(False)
        self.check_button.setText("正在取消…")
        self.status_line.show_message("正在取消登录…")
        self.toast.show_message("正在取消登录，请稍候…", "info", 3000)

    def _on_login_finished(self, data: dict[str, Any]) -> None:
        bili = bool(data.get("bili"))
        tuf = bool(data.get("tuf"))
        self._set_account_chips(bili, tuf)
        self.publish_panel.set_accounts(bili, tuf)
        self.settings_page.set_login_status(bili, tuf)
        self.toast.show_message("双站登录完成", "ok")

    def _on_login_failed(self, message: str) -> None:
        self._last_login_error = redact_secrets(str(message))
        logging.getLogger("adofai").error("登录失败：%s", self._last_login_error)
        if message == "登录已取消":
            self.status_line.show_message("登录已取消")
            self.toast.show_message("登录已取消", "info", 5000)
            return
        # 其余四类（浏览器起不来 / 窗口被关 / 等待超时 / 窗口开着没登录）
        # 都由 LoginWorker 给出可操作文案，这里统一弹窗展示。
        self.status_line.show_message("登录未完成")
        self._show_error_dialog("登录失败", message, clean_browser_error(message))

    def _copy_diagnostics(self) -> None:
        """设置页「复制诊断信息」：版本 / 系统 / 浏览器候选 / profile / 最近错误。"""

        text = build_diagnostics(
            self.settings,
            app_version=__version__,
            last_error=self._last_login_error,
        )
        QApplication.clipboard().setText(text)
        self.settings_page.set_diagnostics_status(
            "已复制到剪贴板，可直接粘贴给作者（不含 Cookie）。"
        )
        self.toast.show_message("诊断信息已复制到剪贴板", "ok")

    def _show_error_dialog(self, title: str, summary: str, details: str = "") -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle(title)
        box.setText(summary)
        if details and details != summary:
            box.setDetailedText(details)
        box.exec()

    def _validate(self) -> list[str]:
        issues = check_draft(
            self.draft,
            banned_words=self.settings.get("banned_words"),
            check_video_file=True,
            publish_mode=self._publish_mode(),
        )
        return [issue.message for issue in issues if issue.is_error]

    def _publish_mode(self) -> str:
        mode = str(self.settings.get("publish_mode") or "dual")
        return "bili_only" if mode == "bili_only" else "dual"

    def _start_upload(self) -> None:
        if self._busy:
            return
        self._on_draft_changed()
        errors = self._validate()
        if errors:
            QMessageBox.warning(self, "还不能上传", "\n".join(errors))
            return
        if not self.draft.title.strip():
            QMessageBox.warning(self, "还不能上传", "请先填写标题。")
            return
        if QMessageBox.question(
            self,
            "仅上传到 B 站" if self._publish_mode() == "bili_only" else "上传到 B 站",
            (
                "当前为仅 B 站模式：只上传视频，不会提交 TUF。\n"
                if self._publish_mode() == "bili_only"
                else ""
            )
            + "将打开浏览器并自动填写投稿页、上传视频。\n上传期间请不要关闭浏览器，继续吗？",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.publish_panel.clear_log()
        self.publish_panel.set_busy(True, "上传 B 站")
        self.publish_panel.set_progress(0)
        self._busy = True
        self.status_line.show_message("正在上传 B 站…")
        self._navigate("workspace")
        self.workspace.open_publish_step()
        worker = BiliUploadWorker(
            self.settings,
            self.draft.video_path,
            self.draft.cover_path,
            self.draft.title,
            self.draft.description,
            self.draft.tags,
            self.draft.partition,
            self.draft.creative_declaration,
            cover_path_43=self.draft.cover_path_43,
        )
        worker.log.connect(self.publish_panel.append_log)
        worker.progress.connect(self.publish_panel.set_progress)
        worker.stage.connect(self.publish_panel.set_stage)
        worker.needs_login.connect(self._on_upload_needs_login)
        worker.needs_manual.connect(self._on_upload_needs_manual)
        worker.manual_resolved.connect(self.publish_panel.clear_manual_pending)
        self._active_browser_worker = worker
        self._start_worker(
            worker,
            on_finished=self._on_upload_finished,
            on_failed=self._on_upload_failed,
            on_thread_finished=self._on_browser_job_finished,
        )

    def _on_upload_needs_manual(self, message: str) -> None:
        self.publish_panel.set_manual_pending(message)
        self.status_line.show_message("等待你在浏览器里完成投稿")
        self.toast.show_message(
            "请在浏览器里完成界面提示的操作，完成后点「我已完成」；"
            "也可以点「取消并关闭浏览器」",
            "warn",
            9000,
        )

    def _on_manual_done(self) -> None:
        worker = self._active_browser_worker
        if worker is None or not hasattr(worker, "confirm_manual"):
            return
        worker.confirm_manual()
        self.publish_panel.set_manual_accepted()

    def _on_manual_cancel(self) -> None:
        worker = self._active_browser_worker
        if worker is None or not hasattr(worker, "cancel"):
            return
        worker.cancel()
        self.publish_panel.set_manual_cancelling()
        self.status_line.show_message("正在取消上传…")

    def _on_upload_finished(self, data: dict[str, Any]) -> None:
        self.publish_panel.set_busy(False)
        bvid = str(data.get("bvid") or "")
        self.publish_panel.set_progress(100)
        bili_only = self._publish_mode() == "bili_only"
        status = next_status_after_bvid("draft", self._publish_mode())
        if bvid:
            self.draft.bvid = bvid
            self.workspace.update_bvid(bvid)
            self.publish_panel.set_bvid(bvid)
            self.publish_panel.append_log(
                f"√ B 站投稿完成：https://www.bilibili.com/video/{bvid}"
            )
            self.toast.show_message(f"B 站上传完成：{bvid}", "ok")
            self.status_line.show_message(
                "仅 B 站投稿完成，本次不提交 TUF"
                if bili_only
                else "B 站投稿完成，下一步提交 TUF"
            )
        else:
            self.publish_panel.append_log(
                "× 页面已确认投稿成功，但没有抓到 BV 号：记录已保存为待补 BV，"
                "请到 B 站稿件管理页复制 BV 后点“保存 BV”。"
            )
            self.toast.show_message(
                "投稿成功但没抓到 BV 号，记录已保存为待补 BV", "warn", 8000
            )
            self.status_line.show_message("B 站投稿完成，等待补填 BV 号")
        self.draft.status = status
        self._persist_draft()
        self._save_upload_record(status)

    def _on_upload_failed(self, message: str) -> None:
        self.publish_panel.set_busy(False)
        detail = clean_browser_error(message)
        self.publish_panel.append_log("× " + detail)
        logging.getLogger("adofai").error(
            "B 站上传失败：%s", redact_secrets(message)
        )
        if self.draft.video_path or self.draft.title:
            self._save_upload_record("draft", last_error=detail[:500])
        self._show_error_dialog(
            "B 站上传失败",
            friendly_browser_error(message),
            detail,
        )
        self.status_line.show_message("B 站上传失败")

    def _save_upload_record(self, status: str, **extra: Any) -> dict[str, Any]:
        """把本次上传结果落库：已有草稿记录就更新，否则新建一条。"""

        current = (
            self.db.get(self._current_history_id) if self._current_history_id else None
        )
        record = draft_to_record(
            self.draft,
            format_accuracy(self.draft.judgements),
            status=status,
            **extra,
        )
        if current is not None and current.get("status") == "draft":
            saved = self.db.update(self._current_history_id, record) or record
        else:
            saved = self.db.add(record)
        self._current_history_id = saved.get("id", "")
        self._refresh_history()
        return saved

    def _start_tuf(self) -> None:
        if self._busy:
            return
        self._on_draft_changed()
        if self._publish_mode() == "bili_only":
            self.toast.show_message("当前是仅 B 站模式，本次不会提交 TUF", "warn")
            return
        if not self.draft.bvid:
            self.toast.show_message("先拿到 BV 号，才能提交 TUF", "warn")
            return
        if not self.draft.level or not self.draft.level.tuf_id:
            self.toast.show_message("缺少 TUF 谱面信息", "error")
            return
        if not self.draft.feeling_rating.strip():
            self.toast.show_message("TUF 投稿需要体感难度，例如 G12", "warn")
            return
        # 弹窗里的判定摘要与实际提交同源：同一组判定值、默认版本与中旋规则。
        preview_judgements, midspin_reduced = prepare_submission_judgements(
            self.draft.judgements,
            int(getattr(self.draft.level, "midspin_count", 0) or 0),
            DEFAULT_ADOFIA_VERSION,
        )
        summary = format_judgement_summary(
            preview_judgements,
            midspin_reduced,
            DEFAULT_ADOFIA_VERSION,
            self.draft.x_perfect,
        )
        if QMessageBox.question(
            self,
            "提交 TUF",
            f"将向 TUF 提交：{self.draft.level.song}（ID {self.draft.level.tuf_id}）\n"
            f"准确率 {format_accuracy(self.draft.judgements)}\n"
            f"{summary}\n继续吗？",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.publish_panel.set_busy(True, "提交 TUF")
        self._busy = True
        self.publish_panel.append_log("正在提交 TUF 记录…")
        self.status_line.show_message("正在提交 TUF…")
        worker = TufSubmitWorker(
            self.settings,
            self.draft.level,
            self.draft.bvid,
            self.draft.judgements,
            self.draft.speed,
            self.draft.feeling_rating,
            self.draft.is_no_hold_tap,
            self.draft.is_12k,
            self.draft.is_16k,
            self.draft.x_perfect,
        )
        worker.log.connect(self.publish_panel.append_log)
        self._start_worker(
            worker,
            on_finished=self._on_tuf_finished,
            on_failed=self._on_tuf_failed,
            on_thread_finished=self._on_browser_job_finished,
        )

    def _on_tuf_finished(self, data: dict[str, Any]) -> None:
        self.publish_panel.set_busy(False)
        record_id = data.get("record_id", "")
        self.draft.tuf_record_id = record_id
        self.draft.status = "submitted"
        # 提交后回读到的审核字段（可能为空）：只写 review_*，不碰 status。
        review = data.get("review")
        if isinstance(review, dict):
            for key, value in review.items():
                if hasattr(self.draft, key):
                    setattr(self.draft, key, value)
            self.publish_panel.set_review(
                self.draft.review_status,
                self.draft.review_checked_at,
                self.draft.review_message,
            )
        self.publish_panel.set_tuf_ok(record_id)
        self.publish_panel.append_log(f"√ TUF 提交成功 {record_id}".strip())
        self._persist_draft()
        updates = {
            "status": "submitted",
            "tuf_record_id": record_id,
            "accuracy": format_accuracy(self.draft.judgements),
            "bvid": self.draft.bvid,
        }
        if isinstance(review, dict) and review:
            updates.update(review)
        if data.get("review_mismatch"):
            updates["review_mismatch"] = True
        if self._current_history_id and self.db.get(self._current_history_id):
            self.db.update(self._current_history_id, updates)
        else:
            existing = next(
                (item for item in self.db.all() if item.get("bvid") == self.draft.bvid),
                None,
            )
            if existing:
                self.db.update(existing["id"], updates)
                self._current_history_id = existing["id"]
            else:
                saved = self.db.add(
                    draft_to_record(self.draft, format_accuracy(self.draft.judgements), **updates)
                )
                self._current_history_id = saved["id"]
        self._refresh_history()
        if data.get("review_mismatch"):
            # 提交已经成功；这里只是回读自检发现判定对不上，提示去 TUF 核对。
            self.publish_panel.append_log(
                "! 回读判定与提交值不一致，请到 TUF 记录页核对"
            )
            self.toast.show_message(
                "TUF 已提交，但回读判定与提交值不一致，请到 TUF 核对",
                "warn",
                9000,
            )
        else:
            self.toast.show_message("双投完成！TUF 记录已提交", "ok", 5000)
        self.status_line.show_message("双投完成")

    def _on_tuf_failed(self, message: str) -> None:
        self.publish_panel.set_busy(False)
        self.publish_panel.append_log("× " + message)
        logging.getLogger("adofai").error("TUF 提交失败：%s", message)
        first_line = (message or "TUF 提交失败").splitlines()[0][:180]
        self.toast.show_message(f"TUF 提交失败：{first_line}", "error", 8000)
        self.status_line.show_message("TUF 提交失败")

    def _sync_review(self) -> None:
        """读回 TUF 审核状态；未登录只提示，绝不修改本地记录。"""

        if self._busy:
            self.toast.show_message(
                "当前有浏览器任务在执行，请稍后再同步审核状态", "warn", 6000
            )
            return
        records = [
            dict(item)
            for item in self.db.all()
            if is_tuf_submitted(item)
            and (item.get("tuf_record_id") or item.get("bvid"))
        ]
        if not records:
            self.toast.show_message("没有可同步审核状态的已投稿记录", "warn")
            return
        self._busy = True
        self._browser_job = "review"
        self.history.review_sync_button.setEnabled(False)
        self.status_line.show_message("正在同步 TUF 审核状态…")
        worker = TufReviewSyncWorker(self.settings, records)
        worker.log.connect(self._on_browser_log)
        self._start_worker(
            worker,
            on_finished=self._on_review_synced,
            on_failed=self._on_review_failed,
            on_thread_finished=self._on_browser_job_finished,
        )

    def _on_review_synced(self, data: dict[str, Any]) -> None:
        self.history.review_sync_button.setEnabled(True)
        updates = data.get("updates") or []
        current_review: dict[str, Any] | None = None
        applied = 0
        for update in updates:
            if not isinstance(update, dict):
                continue
            record_id = str(update.get("id") or "")
            values = {key: value for key, value in update.items() if key != "id"}
            if not record_id or not values:
                continue
            if self.db.update(record_id, values) is None:
                continue
            applied += 1
            if record_id == self._current_history_id:
                current_review = values
                if self.draft is not None:
                    for key, value in values.items():
                        if hasattr(self.draft, key):
                            setattr(self.draft, key, value)
        if current_review is not None and self.draft is not None:
            self.publish_panel.set_review(
                self.draft.review_status,
                self.draft.review_checked_at,
                self.draft.review_message,
            )
            self._persist_draft()
        if applied:
            self._refresh_history()

        message = str(data.get("message") or "")
        if data.get("auth_error"):
            self.toast.show_message(
                message or "TUF 未登录，审核状态未更新", "warn", 7000
            )
            self.status_line.show_message("审核状态未同步：TUF 未登录")
            return
        if data.get("retryable"):
            self.toast.show_message(
                message or "TUF 服务暂不可用，请稍后重试", "warn", 7000
            )
            self.status_line.show_message("审核状态未同步：服务暂不可用")
            return
        failed = data.get("failed") or []
        if applied:
            self.toast.show_message(f"审核状态已同步：更新 {applied} 条", "ok", 5000)
        else:
            self.toast.show_message("没有匹配到可更新的审核记录", "warn", 6000)
        self.status_line.show_message(
            f"审核同步完成，{len(failed)} 条读取失败" if failed else "审核状态同步完成"
        )

    def _on_review_failed(self, message: str) -> None:
        self.history.review_sync_button.setEnabled(True)
        first_line = (message or "同步审核状态失败").splitlines()[0][:180]
        logging.getLogger("adofai").error("同步审核状态失败：%s", first_line)
        self.toast.show_message(f"同步审核状态失败：{first_line}", "error", 8000)
        self.status_line.show_message("同步审核状态失败")

    def _open_tuf_submission_list(self) -> None:
        """打开 TUF 只读投稿列表；不读也不写本地记录。"""

        if self._busy:
            self.toast.show_message(
                "当前有浏览器任务正在执行，请稍后再查看 TUF 投稿记录", "warn", 6000
            )
            return
        if self._tuf_list_dialog is None:
            dialog = TufSubmissionListDialog(self)
            dialog.refreshRequested.connect(self._reload_tuf_submission_list)
            self._tuf_list_dialog = dialog
        self._tuf_list_dialog.set_loading()
        self._tuf_list_dialog.show()
        self._tuf_list_dialog.raise_()
        self._tuf_list_dialog.activateWindow()
        self._start_tuf_submission_list()

    def _reload_tuf_submission_list(self) -> None:
        if self._busy:
            if self._tuf_list_dialog is not None:
                self._tuf_list_dialog.set_failed(
                    "当前有浏览器任务正在执行，请稍后再点「重新读取」"
                )
            return
        if self._tuf_list_dialog is not None:
            self._tuf_list_dialog.set_loading()
        self._start_tuf_submission_list()

    def _start_tuf_submission_list(self) -> None:
        self._busy = True
        self._browser_job = "tuf-list"
        self.history.tuf_list_button.setEnabled(False)
        self.status_line.show_message("正在读取 TUF 投稿记录…")
        worker = TufSubmissionListWorker(self.settings)
        worker.log.connect(self._on_browser_log)
        self._start_worker(
            worker,
            on_finished=self._on_tuf_submission_list,
            on_failed=self._on_tuf_submission_list_failed,
            on_thread_finished=self._on_browser_job_finished,
        )

    def _on_tuf_submission_list(self, data: dict[str, Any]) -> None:
        local_ids = {
            str(item.get("tuf_record_id"))
            for item in self.db.all()
            if item.get("tuf_record_id")
        }
        if self._tuf_list_dialog is not None:
            self._tuf_list_dialog.set_result(data, local_ids)
        items = data.get("items") or []
        if data.get("auth_error"):
            self.status_line.show_message("TUF 投稿记录：未登录")
        elif data.get("retryable"):
            self.status_line.show_message("TUF 投稿记录：服务暂不可用")
        elif data.get("message") and data.get("failed"):
            self.status_line.show_message("TUF 投稿记录：部分页读取失败")
        else:
            self.status_line.show_message(f"TUF 投稿记录：{len(items)} 条")

    def _on_tuf_submission_list_failed(self, message: str) -> None:
        first_line = (message or "读取 TUF 投稿记录失败").splitlines()[0][:180]
        if self._tuf_list_dialog is not None:
            self._tuf_list_dialog.set_failed(first_line)
        logging.getLogger("adofai").error("读取 TUF 投稿记录失败：%s", first_line)
        self.toast.show_message(f"读取 TUF 投稿记录失败：{first_line}", "error", 8000)
        self.status_line.show_message("读取 TUF 投稿记录失败")

    def _refresh_history(self) -> None:
        records = self.db.all()
        self.history.set_records(records)
        self.history.remember(records)
        self.workspace.set_recent(records)
        self.library.set_submitted_records(records)
        self._refresh_home()

    def _load_record(self, record: dict[str, Any]) -> None:
        draft = self._draft_from_record(record)
        self.draft = draft
        self._current_history_id = record.get("id", "")
        self.workspace.load_draft(draft)
        self._navigate("workspace")
        self.toast.show_message("已载入历史记录，可修改后重新发布", "ok")

    def _delete_record(self, record_id: str) -> None:
        self.db.delete(record_id)
        if self._current_history_id == record_id:
            self._current_history_id = ""
        self._refresh_history()

    def _clear_history(self) -> None:
        self.db.clear()
        self._current_history_id = ""
        self._refresh_history()
        self.toast.show_message("历史记录已清空", "ok")

    def _draft_from_record(self, record: dict[str, Any]) -> Draft:
        level = LevelInfo(
            tuf_id=record.get("tuf_id"),
            song=record.get("song", ""),
            artist=record.get("artist", ""),
            charter=record.get("charter", ""),
            bpm=record.get("bpm"),
            duration_ms=record.get("duration_ms"),
            difficulty=record.get("difficulty", ""),
            base_score=record.get("base_score"),
            video_link=record.get("level_video_link", ""),
            tilecount=record.get("tilecount"),
            auto_tile_count=int(record.get("auto_tile_count") or 0),
        )
        return Draft(
            video_path=record.get("video_path", ""),
            cover_path=record.get("cover_path", ""),
            # 旧记录没有这个键，按空串处理，副槽留空即可。
            cover_path_43=record.get("cover_path_43", ""),
            level=level if level.song or level.tuf_id else None,
            judgements=dict(record.get("judgements") or {}),
            speed=float(record.get("speed") or 1.0),
            feeling_rating=record.get("feeling_rating", ""),
            title=record.get("title", ""),
            description=record.get("description", ""),
            tags=list(record.get("tags") or []),
            partition=record.get("partition") or "单机游戏",
            creative_declaration=record.get("creative_declaration") or "内容无需标注",
            bvid=record.get("bvid", ""),
            tuf_record_id=record.get("tuf_record_id", ""),
            review_status=record.get("review_status") or "",
            review_checked_at=record.get("review_checked_at") or "",
            review_message=record.get("review_message") or "",
            review_assignee=record.get("review_assignee") or "",
            review_flags=record.get("review_flags")
            if isinstance(record.get("review_flags"), (list, dict))
            else None,
            review_judgements=dict(record.get("review_judgements") or {}),
            status=record.get("status") or "draft",
            is_no_hold_tap=bool(record.get("is_no_hold_tap", False)),
            is_12k=bool(record.get("is_12k", False)),
            is_16k=bool(record.get("is_16k", False)),
            x_perfect=bool(record.get("x_perfect", False)),
        )

    def _on_bvid_committed(self, bvid: str) -> None:
        self.draft.bvid = bvid
        self.workspace.update_bvid(bvid)
        self._persist_draft()
        if self._current_history_id and self.db.get(self._current_history_id):
            current = self.db.get(self._current_history_id) or {}
            status = next_status_after_bvid(
                current.get("status"), self._publish_mode()
            )
            self.db.update(
                self._current_history_id,
                {
                    "bvid": bvid,
                    "bili_url": f"https://www.bilibili.com/video/{bvid}",
                    "status": status,
                },
            )
            self._refresh_history()

    def _save_settings(self, values: dict[str, Any]) -> None:
        self.settings.update(values)
        self.settings.save()
        self._apply_theme()
        self.workspace.settings = self.settings
        self.preview.settings = self.settings
        self.workspace.refresh_snippets()
        self.workspace.set_publish_mode(self._publish_mode())
        self.toast.show_message("设置已保存", "ok")

    def _clear_login_data(self) -> None:
        if self._busy:
            self.toast.show_message("当前有任务在执行，请稍后再清除", "warn")
            return
        root = base_dir().resolve()
        profile = Path(self.settings.get("browser_profile_dir") or str(browser_profile_dir())).resolve()
        if not is_safe_profile_path(profile, root):
            QMessageBox.warning(
                self,
                "已拒绝自动删除",
                "当前浏览器配置目录不在工具数据目录内。\n"
                "为避免误删你日常使用的 Edge/Chrome 配置，工具不会自动删除，请手动处理。",
            )
            return
        if QMessageBox.question(
            self,
            "清除登录数据",
            "将删除工具专用浏览器配置里的登录 Cookie。\n"
            "不会影响你日常使用的 Edge/Chrome。\n\n继续吗？",
        ) != QMessageBox.StandardButton.Yes:
            return
        import shutil

        try:
            if profile.exists():
                shutil.rmtree(profile)
        except OSError as exc:
            self.toast.show_message(f"清除失败：{exc}", "error", 6000)
            return
        self._set_account_chips(False, False)
        self.publish_panel.set_accounts(False, False)
        self.settings_page.set_login_status(False, False)
        self.toast.show_message("登录数据已清除，下次使用需要重新登录", "ok")

    def _start_umm_update(self, mirror: str) -> None:
        if self._umm_update_worker is not None:
            return
        worker = UmmUpdateWorker(mirror)
        self._umm_update_worker = worker
        worker.progress.connect(
            lambda message, percent: _post_to_ui_thread(
                self, self.umm.set_update_progress, message, percent
            )
        )
        worker.finished.connect(
            lambda version, target: _post_to_ui_thread(
                self, self.umm.set_update_done, version, target
            )
        )
        worker.failed.connect(
            lambda message: _post_to_ui_thread(self, self.umm.set_update_failed, message)
        )
        worker.cancelled.connect(
            lambda _message: _post_to_ui_thread(self, self.umm.set_update_cancelled)
        )
        thread = self._start_worker(
            worker,
            on_thread_finished=self._on_umm_update_finished,
        )
        worker._thread = thread  # type: ignore[attr-defined]

    def _cancel_umm_update(self) -> None:
        if self._umm_update_worker is not None:
            self._umm_update_worker.cancel()

    def _on_umm_update_finished(self) -> None:
        self._umm_update_worker = None

    def _start_umm_action(self, action: str, payload: dict[str, Any]) -> None:
        """把模组页的文件操作放进后台线程，避免界面假死。"""
        if self._umm_action:
            self.toast.show_message("已有模组操作正在进行，请稍候", "warn")
            return
        payload = dict(payload or {})
        self._umm_action = action
        self.umm.set_action_busy(True, self.umm.action_label(action))
        worker = UmmActionWorker(self.settings, action, payload)
        self._umm_action_worker = worker
        self._start_worker(
            worker,
            on_finished=lambda finished_action, result: self._on_umm_action_finished(
                finished_action, payload, result
            ),
            on_failed=lambda message: self._on_umm_action_failed(
                action, payload, message
            ),
        )

    def _on_umm_action_finished(
        self, action: str, payload: dict[str, Any], result: Any
    ) -> None:
        self._umm_action = ""
        self._umm_action_worker = None
        if action == "uninstall_bridge":
            self.settings.set("bridge_auto_install", False)
            self.settings.save()
        self.umm.set_action_busy(False)
        self.umm.refresh()
        self.umm.show_action_result(action, payload, result)
        self.toast.show_message(self._umm_action_toast(action, payload), "ok", 4000)

    def _on_umm_action_failed(
        self, action: str, payload: dict[str, Any], message: str
    ) -> None:
        self._umm_action = ""
        self._umm_action_worker = None
        self.umm.set_action_busy(False)
        self.umm.refresh()
        self.umm.show_action_error(action, str(message))

    @staticmethod
    def _umm_action_toast(action: str, payload: dict[str, Any]) -> str:
        name = str(payload.get("display_name") or payload.get("mod_id") or "模组")
        if action == "install_umm":
            return "UMM 已安装"
        if action == "uninstall_umm":
            return "UMM 已卸载"
        if action == "install_local_update":
            return "本地更新包已安装"
        if action == "install_mod":
            return f"{name} 已安装"
        if action == "uninstall_mod":
            return "模组已卸载"
        if action == "set_mod_enabled":
            enabled = bool(payload.get("enabled"))
            return f"{name} 已{'启用' if enabled else '禁用'}"
        if action == "install_bridge":
            return "Bridge 已安装"
        if action == "repair_bridge":
            return "Bridge 已修复"
        if action == "uninstall_bridge":
            return "Bridge 已卸载"
        return "操作完成"

    def _check_updates_on_startup(self) -> None:
        """启动后静默检查一次；不打断已开始的手动检查。"""
        if not bool(self.settings.get("auto_check_updates", True)):
            return
        repo = str(self.settings.get("update_repo") or "").strip()
        if not repo:
            repo = str(DEFAULT_SETTINGS["update_repo"] or "").strip()
        if not repo or self._update_check_worker is not None:
            return
        self._check_update(repo, silent=True)

    def _check_update(self, repo: str, *, silent: bool = False) -> None:
        if self._update_check_worker is not None:
            return
        try:
            repo = normalize_repo(repo)
        except UpdateError as exc:
            if silent:
                logging.getLogger("adofai").info(
                    "启动时检查更新失败：%s", exc
                )
            else:
                self.settings_page.set_update_error(str(exc))
                if not (repo or "").strip():
                    self.toast.show_message("未配置更新仓库", "warn")
                else:
                    self.toast.show_message(str(exc), "error", 6000)
            return
        self.settings.set("update_repo", repo)
        self.settings.save()
        worker = UpdateCheckWorker(repo, __version__)
        self._update_check_worker = worker
        if not silent:
            self.settings_page.set_update_busy(True)
        self._start_worker(
            worker,
            on_finished=(
                (lambda result: self._handle_update_checked(result, silent=True))
                if silent
                else self._on_update_checked
            ),
            on_failed=(
                (lambda message: self._handle_update_check_failed(message, silent=True))
                if silent
                else self._on_update_check_failed
            ),
            on_thread_finished=self._on_update_check_finished,
        )

    def _on_update_checked(self, result: dict[str, Any]) -> None:
        self._handle_update_checked(result, silent=False)

    def _handle_update_checked(
        self, result: dict[str, Any], *, silent: bool
    ) -> None:
        self.settings_page.set_update_busy(False)
        self.settings_page.show_update_result(result)
        if result.get("has_update"):
            self.toast.show_message(
                f"发现新版本 {result.get('latest')}，可打开下载页",
                "warn",
                5000,
            )
        elif not silent:
            self.toast.show_message("当前已是最新版本", "ok")

    def _on_update_check_failed(self, message: str) -> None:
        self._handle_update_check_failed(message, silent=False)

    def _handle_update_check_failed(self, message: str, *, silent: bool) -> None:
        if silent:
            logging.getLogger("adofai").info("启动时检查更新失败：%s", message)
            return
        self.settings_page.set_update_busy(False)
        self.settings_page.set_update_error(message)
        self.toast.show_message(f"检查更新失败：{message}", "error", 6000)

    def _on_update_check_finished(self) -> None:
        self._update_check_worker = None

    def _open_update_page(self, url: str) -> None:
        if not url.startswith(("https://", "http://")):
            self.toast.show_message("下载页地址无效", "error")
            return
        QDesktopServices.openUrl(QUrl(url))

    def _login_adofaitools(self) -> None:
        if self._tools_login_worker is not None:
            return
        worker = AdofaiToolsLoginWorker(self.settings)
        self._tools_login_worker = worker
        worker.finished.connect(
            lambda _token: _post_to_ui_thread(self, self.umm.set_tools_login_done)
        )
        worker.failed.connect(
            lambda message: _post_to_ui_thread(self, self.umm.set_tools_login_failed, message)
        )
        self._start_worker(
            worker,
            on_thread_finished=self._on_tools_login_finished,
        )

    def _on_tools_login_finished(self) -> None:
        self._tools_login_worker = None

    def _load_adofaitools_mods(self, request: str) -> None:
        token = load_token(self.settings)
        if not token:
            self.umm.set_tools_error("请先登录 ADOFAI Tools")
            return
        resource_type, _, page_text = str(request or "").partition("|")
        try:
            page = max(1, int(page_text or "1"))
        except ValueError:
            page = 1
        worker = AdofaiToolsListWorker(token, resource_type or "MOD", page)
        self._start_worker(
            worker,
            on_finished=self.umm.set_tools_items,
            on_failed=self._on_adofaitools_list_failed,
        )

    def _on_adofaitools_list_failed(self, message: str) -> None:
        if "登录已失效" in str(message):
            clear_token()
            self.settings.set("adofaitools_token", "")
            self.settings.save()
        self.umm.set_tools_error(message)

    def _download_adofaitools_mod(self, item: Any) -> None:
        token = load_token(self.settings)
        if not token:
            self.umm.set_tools_download_failed("请先登录 ADOFAI Tools")
            return
        worker = AdofaiToolsDownloadWorker(token, dict(item), self.settings)
        worker.progress.connect(self.umm.set_tools_download_progress)
        worker.finished.connect(self.umm.set_tools_download_done)
        worker.failed.connect(self.umm.set_tools_download_failed)
        self._start_worker(worker)

    def _apply_theme(self) -> None:
        theme = self.settings.get("theme") or "dark"
        apply_theme(theme, QApplication.instance())
        self._on_theme_changed(theme)

    def _on_theme_changed(self, theme: str) -> None:
        """主题变化的统一分发点（UI-4 起由 theme_bus 触发）。"""
        if hasattr(self, "sidebar"):
            self.sidebar.refresh_theme()
        if hasattr(self, "status_line"):
            self.status_line.refresh_theme()
        if hasattr(self, "toast"):
            self.toast.update()
        if hasattr(self, "workspace"):
            self.workspace.step_bar.set_theme(theme)
        if hasattr(self, "library"):
            self.library.set_theme(theme)
        for page in getattr(self, "pages", {}).values():
            refresh = getattr(page, "refresh_theme", None)
            if callable(refresh):
                refresh()
        styleguide = getattr(self, "_styleguide_page", None)
        if styleguide is not None:
            refresh = getattr(styleguide, "refresh_theme", None)
            if callable(refresh):
                refresh()
        dialog = getattr(self, "_download_dialog", None)
        if dialog is not None:
            refresh = getattr(dialog, "refresh_theme", None)
            if callable(refresh):
                refresh()
            else:
                dialog.set_theme(theme)

    def _open_obs_settings(self) -> None:
        self._navigate("settings")
        QTimer.singleShot(0, self.settings_page.focus_obs_section)

    def _obs_settings(self) -> tuple[str, int, str]:
        values = self.settings_page.collect()
        self.settings.update(
            {
                "obs_host": values.get("obs_host") or "127.0.0.1",
                "obs_port": values.get("obs_port") or 4455,
                "obs_password": values.get("obs_password") or "",
                "obs_exe_path": values.get("obs_exe_path") or "",
            }
        )
        self.settings.save()
        host = str(self.settings.get("obs_host") or "127.0.0.1").strip() or "127.0.0.1"
        try:
            port = int(self.settings.get("obs_port") or 4455)
        except (TypeError, ValueError):
            port = 4455
        password = str(self.settings.get("obs_password") or "")
        return host, port, password

    def _obs_set_busy(self, busy: bool, text: str = "") -> None:
        self.settings_page.set_obs_busy(busy, text)
        self.home.set_obs_busy(busy)

    def _obs_test_connection(self) -> None:
        self._obs_launch_timer.stop()
        host, port, password = self._obs_settings()
        self._obs_set_busy(True, "正在连接 OBS…")
        self._obs.test_connection(host, port, password)

    def _obs_launch(self) -> None:
        if self._obs_launch_timer.isActive():
            self.toast.show_message("正在启动 OBS，请稍候…", "warn", 3000)
            return

        self._obs_settings()
        self._obs_set_busy(True, "正在检查 OBS…")
        if is_obs_running():
            self._obs_set_busy(True, "OBS 已在运行，正在测试连接…")
            self.toast.show_message(
                "OBS 已在运行，正在测试连接…", "info", 4000
            )
            self._obs_launch_timer.start(200)
            return

        executable = find_obs_executable(
            str(self.settings.get("obs_exe_path") or "")
        )
        if executable is None:
            self._obs_set_busy(False, "未找到 OBS，请选择 obs64.exe。")
            executable = self._choose_obs_executable()
            if executable is None:
                message = (
                    f"未找到 OBS。已自动查找：{OBS_SEARCH_LOCATIONS}。"
                    "如果确实装了 OBS，可以在「设置 → OBS → 高级设置」里点"
                    "「OBS 程序」旁的「选择」，手动指定 obs64.exe。"
                )
                self.settings_page.set_obs_status(message, "bad")
                self.toast.show_message(
                    "未找到 OBS，可以在「设置 → OBS」里手动选择 obs64.exe。",
                    "bad",
                    8000,
                )
                return
            self.settings.set("obs_exe_path", str(executable))
            self.settings.save()
            self.settings_page.obs_exe_path_input.setText(str(executable))
            self.settings_page.set_obs_advanced_visible(True)

        try:
            launch_obs(executable)
        except (OSError, ValueError) as exc:
            message = f"启动 OBS 失败：{exc}"
            self._obs_set_busy(False, message)
            self.settings_page.set_obs_status(message, "bad")
            self.toast.show_message(message, "bad", 8000)
            return

        self._obs_set_busy(
            True, f"已找到 OBS：{executable}，正在启动，约 5 秒后自动测试连接…"
        )
        self.toast.show_message(
            "正在启动 OBS，稍后自动测试连接…", "info", 5000
        )
        self._obs_launch_timer.start()

    def _choose_obs_executable(self) -> Path | None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 OBS 程序",
            "",
            "OBS Studio (obs64.exe);;可执行文件 (*.exe);;所有文件 (*.*)",
        )
        return Path(path) if path else None

    def _on_obs_test_ok(self) -> None:
        self._obs_set_busy(False)
        self.settings_page.set_obs_status(
            "已连接 OBS，可以开始录制。", "ok"
        )
        self.toast.show_message("OBS 连接成功", "ok")

    def _obs_start_recording(self) -> None:
        host, port, password = self._obs_settings()
        self._obs_set_busy(True, "正在请求 OBS 开始录制…")
        self._obs.start_recording(host, port, password)

    def _on_obs_record_started(self) -> None:
        self._obs_set_busy(False)
        self.settings_page.set_obs_recording(True)
        self.settings_page.set_obs_status("OBS 正在录制。", "ok")
        self.toast.show_message("OBS 已开始录制", "ok")

    def _obs_stop_recording(self) -> None:
        host, port, password = self._obs_settings()
        self._obs_set_busy(True, "正在请求 OBS 停止录制…")
        self._obs.stop_recording(host, port, password)

    def _on_obs_record_stopped(self, path: str) -> None:
        self._obs_set_busy(False)
        self.settings_page.set_obs_recording(False)
        path = str(path or "").strip()
        if not path:
            self.settings_page.set_obs_status(
                "录制已停止，但 OBS 没有返回保存路径。请检查 OBS 的录制输出设置。",
                "warn",
            )
            self.toast.show_message(
                "录制已停止，但 OBS 没有返回保存路径", "warn", 6000
            )
            return
        self.settings_page.set_obs_output_path(path)
        self.settings_page.set_obs_status(f"录制已保存：{path}", "ok")
        self.toast.show_message(
            f"录制已保存：\n{path}",
            "ok",
            12000,
            action_text="打开文件夹",
            action=lambda: self._open_folder(path),
        )

    def _on_obs_failed(self, message: str) -> None:
        self._obs_set_busy(False)
        self.settings_page.set_obs_recording(False)
        self.settings_page.set_obs_status(message, "bad")
        self.toast.show_message(message, "bad", 8000)

    def _check_sav(self, path: str) -> None:
        result = check_strict(path or None)
        self.settings_page.set_sav_result(result.message, result.strict)
        if result.strict:
            self.toast.show_message("判定模式为严格 ✓", "ok")
        elif result.strict is False:
            self.toast.show_message(result.message, "warn", 5000)
        else:
            self.toast.show_message(result.message, "warn", 5000)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if hasattr(self, "toast") and self.toast.isVisible():
            self.toast.hide()
        if hasattr(self, "status_line"):
            self.status_line.position_panel()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        # 从这里开始，worker.cancel() 派生的 cancelled/failed 回调都视为关闭噪声，
        # 不再改任务状态；任务是完成还是回到等待中由本方法决定。
        self._closing = True
        try:
            theme_bus().changed.disconnect(self._on_theme_changed)
        except (RuntimeError, TypeError):
            pass
        self._hotkey.stop()
        self._obs_launch_timer.stop()
        self._obs.close()
        self._prune_jobs()
        running = [thread for thread in self._jobs if thread.isRunning()]
        interrupted = [
            task.task_id
            for task in self._download_queue.active_tasks()
            if task.status != STATUS_CANCELLING
        ]
        if running or self._busy:
            answer = QMessageBox.question(
                self,
                "任务进行中",
                "仍有任务在执行，关闭可能导致失败。确定退出吗？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                # 用户决定不退出：恢复正常回调处理。
                self._closing = False
                event.ignore()
                return
            for thread in running:
                worker = self._jobs.get(thread)
                if worker is not None and hasattr(worker, "cancel"):
                    worker.cancel()
            for thread in running:
                thread.quit()
                thread.wait(6000)
            self._jobs.clear()
        # 退出时被中断的任务回到等待中：重开工具后由用户点「继续下载」再开始。
        for task_id in interrupted:
            task = self._download_queue.get(task_id)
            if task is not None and task.status in ACTIVE_STATUSES:
                task.status = STATUS_PENDING
        save_timer = getattr(self, "_download_save_timer", None)
        if save_timer is not None:
            save_timer.stop()
        self._save_download_queue()
        self._sync_workspace()
        if self.settings.get("restore_last_draft"):
            self._persist_draft()
        self._startup_update_timer.stop()
        self._save_window_state()
        super().closeEvent(event)
