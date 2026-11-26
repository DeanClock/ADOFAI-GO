from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import DEFAULT_SETTINGS, Settings
from ..adofaitools import RESOURCE_TYPES, load_token
from ..game_bridge import get_bridge_status
from ..hotkey import parse_hotkey, qt_key_name
from ..umm import DEFAULT_UMM_MIRROR, UMM_DOWNLOAD_ERROR_PREFIX, UMM_MIRRORS
from ..umm import (
    default_game_dir,
    get_umm_status,
    installer_dir,
    is_valid_game_dir,
    list_mods,
    umm_installer_needs_download,
    umm_installer_source,
)
from .theme import metrics
from .widgets import Card, ChoiceCombo, Disclosure


ACTION_LABELS = {
    "install_umm": "正在安装 UMM…",
    "uninstall_umm": "正在卸载 UMM…",
    "install_local_update": "正在解压本地更新包…",
    "install_mod": "正在安装模组…",
    "uninstall_mod": "正在卸载模组…",
    "set_mod_enabled": "正在切换模组状态…",
    "install_bridge": "正在安装 Bridge…",
    "repair_bridge": "正在修复 Bridge…",
    "uninstall_bridge": "正在卸载 Bridge…",
}

ACTION_ERROR_TITLES = {
    "install_umm": "安装失败",
    "uninstall_umm": "卸载失败",
    "install_local_update": "安装失败",
    "install_mod": "安装失败",
    "uninstall_mod": "卸载失败",
    "set_mod_enabled": "操作失败",
    "install_bridge": "安装失败",
    "repair_bridge": "修复失败",
    "uninstall_bridge": "卸载失败",
}


class UmmPage(QWidget):
    openFolderRequested = Signal(str)
    openLogRequested = Signal(str)
    hotkeyChanged = Signal()
    hotkeyRecordingChanged = Signal(bool)
    launchRequested = Signal()
    updateRequested = Signal(str)
    updateCancelRequested = Signal()
    toolsLoginRequested = Signal()
    toolsRefreshRequested = Signal(str)
    toolsDownloadRequested = Signal(object)
    actionRequested = Signal(str, dict)

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._rows: dict[str, dict[str, Any]] = {}
        self._action_busy = False
        # 在线更新 / 一键安装的自动下载是否正在占用下载通道。
        self._update_running = False
        # 本次下载完成后是否接着执行「一键安装 UMM」。
        self._install_after_download = False
        self._hotkey_recording = False
        self._hotkey_last_key = ""
        self._hotkey_last_time = 0.0
        self._hotkey_hint_timer = QTimer(self)
        self._hotkey_hint_timer.setSingleShot(True)
        self._hotkey_hint_timer.timeout.connect(self._hotkey_hint_timeout)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        tab_row = QHBoxLayout()
        tab_row.setContentsMargins(metrics().page_pad_x, 10, metrics().page_pad_x, 0)
        tab_row.setSpacing(8)
        self.umm_tab_button = QPushButton("UMM 与本地模组")
        self.umm_tab_button.setObjectName("SegmentButton")
        self.umm_tab_button.setCheckable(True)
        self.umm_tab_button.setChecked(True)
        self.umm_tab_button.clicked.connect(lambda: self.show_tab(0))
        self.tools_tab_button = QPushButton("ADOFAI Tools 在线资源")
        self.tools_tab_button.setObjectName("SegmentButton")
        self.tools_tab_button.setCheckable(True)
        self.tools_tab_button.clicked.connect(lambda: self.show_tab(1))
        tab_row.addWidget(self.umm_tab_button)
        tab_row.addWidget(self.tools_tab_button)
        tab_row.addStretch(1)
        root.addLayout(tab_row)

        self.action_status = QLabel("")
        self.action_status.setObjectName("Muted")
        self.action_status.setContentsMargins(
            metrics().page_pad_x, 0, metrics().page_pad_x, 0
        )
        self.action_status.hide()
        root.addWidget(self.action_status)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        local_content = QWidget()
        outer = QVBoxLayout(local_content)
        outer.setContentsMargins(
            metrics().page_pad_x, 8, metrics().page_pad_x, 18
        )
        outer.setSpacing(14)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(local_content)
        self.stack.addWidget(area)

        setup_card = Card(
            "游戏目录",
            "选择 ADOFAI 游戏安装目录（UMM 文件会安装到这里，mod 会放进这里的 Mods 文件夹）。",
        )
        dir_row = QHBoxLayout()
        self.dir_input = QLineEdit()
        self.dir_input.setPlaceholderText("留空则自动从 Steam 探测")
        browse = QPushButton("选择目录")
        browse.clicked.connect(self._browse_dir)
        dir_row.addWidget(self.dir_input, 1)
        dir_row.addWidget(browse)
        setup_card.body.addLayout(dir_row)
        detect_row = QHBoxLayout()
        detect = QPushButton("自动探测")
        detect.clicked.connect(self._auto_detect_dir)
        detect_row.addWidget(detect)
        self.launch_button = QPushButton("启动游戏")
        self.launch_button.setObjectName("PrimaryButton")
        self.launch_button.clicked.connect(self._launch_game)
        detect_row.addWidget(self.launch_button)
        detect_row.addStretch(1)
        setup_card.body.addLayout(detect_row)
        outer.addWidget(setup_card)

        manager_card = Card("Unity Mod Manager")
        status_row = QHBoxLayout()
        self.manager_status = QLabel("未检测")
        self.manager_status.setObjectName("Muted")
        status_row.addWidget(self.manager_status, 1)
        self.install_umm_button = QPushButton("一键安装 UMM")
        self.install_umm_button.setObjectName("PrimaryButton")
        self.install_umm_button.clicked.connect(self._do_install_umm)
        status_row.addWidget(self.install_umm_button)
        self.uninstall_umm_button = QPushButton("卸载 UMM")
        self.uninstall_umm_button.setObjectName("DangerButton")
        self.uninstall_umm_button.clicked.connect(self._do_uninstall_umm)
        status_row.addWidget(self.uninstall_umm_button)
        manager_card.body.addLayout(status_row)
        update_row = QHBoxLayout()
        # 没有更新动作时不占位；状态行只在更新开始后出现。
        self.update_status = QLabel("")
        self.update_status.setObjectName("Faint")
        self.update_status.hide()
        update_row.addWidget(self.update_status)
        update_row.addStretch(1)
        self.mirror_combo = ChoiceCombo()
        for label in UMM_MIRRORS:
            self.mirror_combo.addItem(label, label)
        index = self.mirror_combo.findData(self.settings.get("umm_mirror") or DEFAULT_UMM_MIRROR)
        self.mirror_combo.setCurrentIndex(max(0, index))
        update_row.addWidget(self.mirror_combo)
        self.update_button = QPushButton("在线更新 UMM")
        self.update_button.setObjectName("TaskAction")
        self.update_button.clicked.connect(self._start_update)
        update_row.addWidget(self.update_button)
        self.cancel_update_button = QPushButton("取消")
        self.cancel_update_button.setObjectName("TaskActionDanger")
        self.cancel_update_button.clicked.connect(self._cancel_update)
        self.cancel_update_button.hide()
        update_row.addWidget(self.cancel_update_button)
        manager_card.body.addLayout(update_row)
        local_row = QHBoxLayout()
        self.local_install_button = QPushButton("从本地更新包安装")
        self.local_install_button.setObjectName("TaskAction")
        self.local_install_button.clicked.connect(self._install_local_update)
        local_row.addWidget(self.local_install_button)
        local_row.addStretch(1)
        manager_card.body.addLayout(local_row)
        self.update_bar = QProgressBar()
        self.update_bar.setObjectName("DownloadBar")
        self.update_bar.setRange(0, 100)
        self.update_bar.setValue(0)
        self.update_bar.hide()
        manager_card.body.addWidget(self.update_bar)
        installer_row = QHBoxLayout()
        installer_label = QLabel("安装器目录")
        installer_row.addWidget(installer_label)
        self.installer_dir_input = QLineEdit()
        self.installer_dir_input.setPlaceholderText("留空则自动查找（数据目录）")
        self.installer_dir_input.setText(str(self.settings.get("umm_installer_dir") or ""))
        self.installer_dir_input.setToolTip(
            "UMM 安装器所在目录。留空时自动使用数据目录下的 UnityModManagerInstaller。"
        )
        self.installer_dir_input.editingFinished.connect(self._save_installer_dir)
        installer_row.addWidget(self.installer_dir_input, 1)
        browse_installer = QPushButton("选择安装器目录")
        browse_installer.clicked.connect(self._browse_installer_dir)
        installer_row.addWidget(browse_installer)
        clear_installer = QPushButton("恢复自动查找")
        clear_installer.clicked.connect(self._clear_installer_dir)
        installer_row.addWidget(clear_installer)
        manager_card.body.addLayout(installer_row)
        source_row = QHBoxLayout()
        source_hint = QLabel()
        source_hint.setObjectName("Faint")
        source_hint.setWordWrap(True)
        source_row.addWidget(source_hint, 1)
        open_installer = QPushButton("打开安装器目录")
        open_installer.clicked.connect(self._open_installer_dir)
        source_row.addWidget(open_installer)
        manager_card.body.addLayout(source_row)
        self.manager_source_label = source_hint
        # 安装原理收进「？」折叠：默认界面只留状态与按钮。
        manager_card.body.addWidget(
            Disclosure(
                "安装原理",
                "一键安装 / 卸载 UMM 本体。本地没有安装器时会自动下载安装包再安装，"
                "不需要先点「在线更新 UMM」。UMM 装进游戏目录，mod 装在游戏根目录的 "
                "Mods 文件夹里；运行时配置按安装包类型自动生成：官方 UMM 0.33 及以上"
                "写 ModsDirectory（相对游戏根目录），0.32.4 及更早的 fork 写 "
                "RelativeModsDirectory（相对 UMM 程序集目录），两种字段不会混用。"
                "卸载只删除 UMM 本体文件，不会删除 Mods 文件夹和游戏本体。\n"
                "自动下载先走 UMM 官方源；官方源（Dropbox）连不上时自动改用 "
                "Thunderstore 的 ADOFAI 专用 UMM 包，两个源都失败时才提示手动兜底："
                "把 umm_update.zip 放到数据目录，再点「从本地更新包安装」。",
            )
        )
        outer.addWidget(manager_card)

        bridge_card = Card(
            "ADOFAI GO Bridge",
            "桌面端专属桥接。",
        )
        bridge_row = QHBoxLayout()
        self.bridge_status = QLabel("未检测")
        self.bridge_status.setObjectName("Muted")
        bridge_row.addWidget(self.bridge_status)
        self.bridge_version = QLabel("")
        self.bridge_version.setObjectName("Muted")
        bridge_row.addWidget(self.bridge_version)
        bridge_row.addStretch(1)
        self.bridge_refresh_button = QPushButton("刷新状态")
        self.bridge_refresh_button.setObjectName("TaskAction")
        self.bridge_refresh_button.clicked.connect(self._refresh_bridge_status)
        bridge_row.addWidget(self.bridge_refresh_button)
        self.bridge_log_button = QPushButton("打开日志")
        self.bridge_log_button.setObjectName("TaskAction")
        self.bridge_log_button.clicked.connect(self._open_bridge_log)
        bridge_row.addWidget(self.bridge_log_button)
        bridge_card.body.addLayout(bridge_row)

        bridge_actions = QHBoxLayout()
        self.bridge_install_button = QPushButton("一键安装")
        self.bridge_install_button.setObjectName("PrimaryButton")
        self.bridge_install_button.clicked.connect(self._install_bridge)
        bridge_actions.addWidget(self.bridge_install_button)
        self.bridge_repair_button = QPushButton("修复安装")
        self.bridge_repair_button.setObjectName("TaskAction")
        self.bridge_repair_button.clicked.connect(self._repair_bridge)
        self.bridge_repair_button.setVisible(False)
        bridge_actions.addWidget(self.bridge_repair_button)
        self.bridge_uninstall_button = QPushButton("卸载 Bridge")
        self.bridge_uninstall_button.setObjectName("DangerButton")
        self.bridge_uninstall_button.clicked.connect(self._uninstall_bridge)
        self.bridge_uninstall_button.setVisible(False)
        bridge_actions.addWidget(self.bridge_uninstall_button)
        bridge_actions.addStretch(1)
        self.bridge_auto_install = QCheckBox("需要时自动安装 Bridge")
        self.bridge_auto_install.setToolTip(
            "关闭时会先询问；选择暂不安装不影响谱面库、下载、整理、投稿等桌面功能。"
        )
        self.bridge_auto_install.toggled.connect(self._on_bridge_auto_install)
        bridge_actions.addWidget(self.bridge_auto_install)
        bridge_card.body.addLayout(bridge_actions)

        # 长说明收进「？」折叠，默认只留状态与按钮。
        self.bridge_note = Disclosure(
            "Bridge 说明",
            "不安装不影响谱面库、下载、整理、投稿等桌面功能；"
            "只影响从软件直接让游戏编辑器打开谱面。安装只写入 Mods\\ADOFAIGOBridge，"
            "不改游戏本体、存档或其他模组，可随时卸载。",
        )
        bridge_card.body.addWidget(self.bridge_note)

        self.bridge_detail = QLabel("")
        self.bridge_detail.setObjectName("Faint")
        self.bridge_detail.setWordWrap(True)
        bridge_card.body.addWidget(self.bridge_detail)

        hotkey_row = QHBoxLayout()
        hotkey_title = QLabel("游戏内唤起热键")
        hotkey_title.setObjectName("Value")
        hotkey_row.addWidget(hotkey_title)
        self.hotkey_value = QLabel("")
        self.hotkey_value.setObjectName("ChipOk")
        hotkey_row.addWidget(self.hotkey_value)
        self.hotkey_record_button = QPushButton("录制")
        self.hotkey_record_button.setObjectName("TaskAction")
        self.hotkey_record_button.clicked.connect(self._toggle_hotkey_record)
        hotkey_row.addWidget(self.hotkey_record_button)
        self.hotkey_reset_button = QPushButton("恢复默认")
        self.hotkey_reset_button.setObjectName("TaskAction")
        self.hotkey_reset_button.clicked.connect(self._reset_hotkey)
        hotkey_row.addWidget(self.hotkey_reset_button)
        self.hotkey_enabled = QCheckBox("启用")
        self.hotkey_enabled.toggled.connect(self._on_hotkey_enabled)
        hotkey_row.addWidget(self.hotkey_enabled)
        hotkey_row.addStretch(1)
        self.hotkey_status = QLabel("等待启动")
        self.hotkey_status.setObjectName("Muted")
        hotkey_row.addWidget(self.hotkey_status)
        bridge_card.body.addLayout(hotkey_row)
        # 常驻说明收进「？」；录制过程中的临时提示才用下面这行小字。
        self.hotkey_hint = QLabel("")
        self.hotkey_hint.setObjectName("Faint")
        self.hotkey_hint.setWordWrap(True)
        self.hotkey_hint.hide()
        bridge_card.body.addWidget(self.hotkey_hint)
        self.hotkey_note = Disclosure(
            "热键说明",
            f"默认 {DEFAULT_SETTINGS['game_hotkey']}。软件保持运行即可，可最小化；"
            "热键由桌面端监听，不依赖 Bridge。",
        )
        bridge_card.body.addWidget(self.hotkey_note)
        outer.addWidget(bridge_card)

        mods_card = Card(
            "相关模组",
            "点击「安装」把压缩包解压到 Mods 目录；已安装的模组可以启用 / 禁用 / 卸载。"
            "启用状态写入 UMM 的 Params.xml，切换后需重启游戏才会生效。",
        )
        self.mods_grid = QGridLayout()
        self.mods_grid.setHorizontalSpacing(10)
        self.mods_grid.setVerticalSpacing(10)
        self.mods_grid.setColumnStretch(1, 1)
        mods_card.body.addLayout(self.mods_grid)
        outer.addWidget(mods_card)
        outer.addStretch(1)

        tools_content = QWidget()
        tools_outer = QVBoxLayout(tools_content)
        tools_outer.setContentsMargins(
            metrics().page_pad_x, 8, metrics().page_pad_x, 18
        )
        tools_outer.setSpacing(14)
        tools_area = QScrollArea()
        tools_area.setWidgetResizable(True)
        tools_area.setFrameShape(QFrame.Shape.NoFrame)
        tools_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        tools_area.setWidget(tools_content)
        self.stack.addWidget(tools_area)

        self.tools_card = Card(
            "ADOFAI Tools 资源",
            "登录 ADOFAI Tools 后可在这里浏览并安装在线模组。登录在独立浏览器窗口完成，"
            "工具只保存登录令牌，不接触密码。",
        )
        tools_head = QHBoxLayout()
        self.tools_status = QLabel("未登录")
        self.tools_status.setObjectName("Muted")
        tools_head.addWidget(self.tools_status, 1)
        self.tools_type_combo = ChoiceCombo()
        for value, label in RESOURCE_TYPES.items():
            self.tools_type_combo.addItem(label, value)
        self.tools_type_combo.currentIndexChanged.connect(self._on_tools_type_changed)
        tools_head.addWidget(self.tools_type_combo)
        self.tools_login_button = QPushButton("登录 ADOFAI Tools")
        self.tools_login_button.setObjectName("PrimaryButton")
        self.tools_login_button.clicked.connect(self._login_tools)
        tools_head.addWidget(self.tools_login_button)
        self.tools_refresh_button = QPushButton("刷新列表")
        self.tools_refresh_button.setObjectName("TaskAction")
        self.tools_refresh_button.clicked.connect(self._refresh_tools)
        tools_head.addWidget(self.tools_refresh_button)
        self.tools_card.body.addLayout(tools_head)
        self.tools_list = QVBoxLayout()
        self.tools_list.setSpacing(8)
        self.tools_card.body.addLayout(self.tools_list)
        self.tools_empty = QLabel("登录后点「刷新列表」加载在线资源。")
        self.tools_empty.setObjectName("ChartEmpty")
        self.tools_card.body.addWidget(self.tools_empty)
        self.tools_more_button = QPushButton("加载更多")
        self.tools_more_button.setObjectName("TaskAction")
        self.tools_more_button.clicked.connect(self._load_more_tools)
        self.tools_more_button.hide()
        self.tools_card.body.addWidget(self.tools_more_button)
        tools_outer.addWidget(self.tools_card)
        tools_outer.addStretch(1)
        self._tools_items: list[dict[str, Any]] = []
        self._tools_page = 0
        self._tools_total = 0
        self._refresh_tools_status()
        self._refresh_hotkey_controls()

    def _read_dir(self) -> str:
        return self.dir_input.text().strip()

    def show_tab(self, index: int) -> None:
        index = 1 if index == 1 else 0
        self.stack.setCurrentIndex(index)
        self.umm_tab_button.setChecked(index == 0)
        self.tools_tab_button.setChecked(index == 1)
        if index == 1:
            self._refresh_tools_status()

    def _save_dir(self) -> None:
        value = self._read_dir()
        if value:
            self.settings.set("umm_game_dir", value)
        else:
            self.settings.set("umm_game_dir", "")
        self.settings.save()

    def _browse_dir(self) -> None:
        start = self._read_dir() or str(default_game_dir() or "")
        path = QFileDialog.getExistingDirectory(self, "选择 ADOFAI 游戏目录", start)
        if path:
            self.dir_input.setText(path)
            self._save_dir()
            self.refresh()

    def _auto_detect_dir(self) -> None:
        detected = default_game_dir()
        if detected is None:
            self.dir_input.setText("")
            self._save_dir()
            self.refresh()
            self._show_message("未探测到游戏目录", "请手动选择游戏目录。", "warn")
            return
        self.dir_input.setText(str(detected))
        self._save_dir()
        self.refresh()
        self._show_message(
            "已自动探测",
            f"游戏目录：{detected}\n\n可执行文件存在：{is_valid_game_dir(detected)}",
            "ok",
        )

    def _launch_game(self) -> None:
        directory = self._read_dir() or str(default_game_dir() or "")
        if directory:
            self.dir_input.setText(directory)
            self._save_dir()
        self.launchRequested.emit()

    def _open_installer_dir(self) -> None:
        source = umm_installer_source(self.settings)
        if source is None:
            self._show_message(
                "找不到安装器目录",
                "直接点「一键安装 UMM」会自动下载安装包。\n\n"
                "也可以把 UMM 安装器解压到：\n"
                f"{installer_dir()}\n\n"
                "或在本卡片的「安装器目录」里指定一个目录。",
                "warn",
            )
            return
        self.openFolderRequested.emit(str(source))

    def _browse_installer_dir(self) -> None:
        current = self.installer_dir_input.text().strip()
        start = current or str(umm_installer_source(self.settings) or installer_dir())
        path = QFileDialog.getExistingDirectory(self, "选择 UMM 安装器目录", start)
        if not path:
            return
        if not any(
            (Path(path) / name).is_file()
            for name in ("UnityModManager.exe", "UnityModManager.dll")
        ):
            if QMessageBox.question(
                self,
                "目录里没有 UMM 文件",
                f"{path}\n\n没有找到 UnityModManager.exe / UnityModManager.dll，"
                "确定仍要使用这个目录吗？",
            ) != QMessageBox.StandardButton.Yes:
                return
        self.installer_dir_input.setText(path)
        self._save_installer_dir()

    def _save_installer_dir(self) -> None:
        value = self.installer_dir_input.text().strip()
        if value == str(self.settings.get("umm_installer_dir") or "").strip():
            return
        self.settings.set("umm_installer_dir", value)
        self.settings.save()
        self.refresh()

    def _clear_installer_dir(self) -> None:
        self.installer_dir_input.setText("")
        self._save_installer_dir()

    def _disclaimer_accepted(self) -> bool:
        result = QMessageBox.question(
            self,
            "在线更新 UMM · 免责声明",
            "在线更新将从第三方仓库下载 UMM 更新包。\n\n"
            "声明：\n"
            "1. UMM 由第三方开发者维护，本工具仅提供下载与安装辅助，"
            "不对其稳定性、安全性或与游戏版本的兼容性作任何保证。\n"
            "2. 更新包内容以官方发布为准，本工具不修改、不注入任何代码。\n"
            "3. 下载与安装风险由使用者自行承担；请确认更新源与游戏版本后使用。\n"
            "4. 国内网络下可能使用第三方镜像加速下载，镜像仅作网络转发；"
            "一键安装时若官方源不可用，会改用 Thunderstore 的第三方 UMM 包作为备用源。\n\n"
            "是否继续？",
        )
        return result == QMessageBox.StandardButton.Yes

    def _begin_update(self, mirror: str, status_text: str) -> None:
        """进入下载状态；在线更新与一键安装的自动下载共用同一套下载界面。"""
        self.settings.set("umm_mirror", mirror)
        self.settings.save()
        self._update_running = True
        self.update_button.setEnabled(False)
        # 下载期间不允许再点一键安装，避免两路同时下载安装包。
        self.install_umm_button.setEnabled(False)
        self.cancel_update_button.show()
        self.update_bar.setValue(0)
        self.update_bar.show()
        self.update_status.show()
        self.update_status.setText(status_text)
        self.updateRequested.emit(mirror)

    def _finish_update_ui(self) -> None:
        """结束下载状态，恢复更新与安装按钮（安装动作会自行再禁用）。"""
        self._update_running = False
        self.cancel_update_button.hide()
        self.update_bar.hide()
        self.update_button.setEnabled(True)
        self.install_umm_button.setEnabled(not self._action_busy)

    def _start_update(self) -> None:
        if not self._disclaimer_accepted():
            return
        self._install_after_download = False
        mirror = self.mirror_combo.currentData() or DEFAULT_UMM_MIRROR
        self._begin_update(mirror, "正在初始化…")

    def _start_install_download(self, mirror: str) -> None:
        """一键安装缺少安装器时：先下载安装包，完成后自动接着安装。"""
        self._install_after_download = True
        self._begin_update(mirror, "正在下载 UMM 安装包…")

    def _cancel_update(self) -> None:
        self.updateCancelRequested.emit()

    def set_update_progress(self, message: str, percent: int) -> None:
        self.update_status.show()
        if self._install_after_download:
            # 一键安装的自动下载：标明当前阶段，避免和手动「在线更新」混淆。
            message = f"下载 UMM 安装包：{message}"
        self.update_status.setText(message)
        self.update_bar.setValue(max(0, min(100, int(percent))))

    def set_update_done(self, version: str, target: str) -> None:
        self._finish_update_ui()
        if self._install_after_download:
            # 安装包已就位，接着执行安装；结果由安装动作自己提示。
            self._install_after_download = False
            self.update_status.hide()
            self._request_action("install_umm")
            return
        self.update_status.show()
        self.update_status.setText(f"UMM v{version} 已更新到本地安装器目录")
        self.update_status.setObjectName("ChipOk")
        self.refresh()
        self._show_message(
            "UMM 已更新",
            f"版本：{version}\n\n已解压到：{target}\n\n"
            "现在点「一键安装 UMM」即可把新版安装到游戏目录。",
            "ok",
        )

    def set_update_failed(self, message: str) -> None:
        self._finish_update_ui()
        if self._install_after_download:
            # 一键安装的预下载失败不在这里终止：官方源（Dropbox）在国内常常直连
            # 失败，交给安装动作继续走「官方源 → Thunderstore」两级回退下载。
            # 两级都失败时由安装动作统一报错（含两个源的原因与手动兜底）。
            self._install_after_download = False
            self.update_status.show()
            self.update_status.setText(
                "官方源下载失败，正在尝试备用源 Thunderstore…"
            )
            self.update_status.setObjectName("ChipWarn")
            self._request_action("install_umm")
            return
        self.update_status.show()
        self.update_status.setText("在线更新失败")
        self.update_status.setObjectName("ChipWarn")
        self._show_message("在线更新失败", message, "error")

    def set_update_cancelled(self) -> None:
        self._finish_update_ui()
        cancelled_install = self._install_after_download
        self._install_after_download = False
        self.update_status.show()
        self.update_status.setText(
            "已取消下载 UMM 安装包，未安装 UMM"
            if cancelled_install
            else "已取消在线更新"
        )
        self.update_status.setObjectName("ChipWarn")

    def _install_local_update(self) -> None:
        if QMessageBox.question(
            self,
            "从本地更新包安装",
            "将从数据目录读取 umm_update.zip 并解压到安装器目录。\n\n"
            "支持 UMM 官方更新包，也支持 Thunderstore 的 UMM_ADOFAI 包"
            "（会自动归一化成安装器目录结构）。\n\n"
            "请确保压缩包来自 UMM 官方或 Thunderstore 的发布页。继续吗？",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._request_action("install_local_update")

    def _refresh_tools_status(self) -> None:
        token = load_token(self.settings)
        if token:
            self.tools_status.setText("已登录 ADOFAI Tools")
            self.tools_status.setObjectName("ChipOk")
            self.tools_login_button.setText("刷新登录态")
        else:
            self.tools_status.setText("未登录 ADOFAI Tools")
            self.tools_status.setObjectName("Muted")
            self.tools_login_button.setText("登录 ADOFAI Tools")

    def _login_tools(self) -> None:
        has_token = bool(load_token(self.settings))
        if not has_token:
            if QMessageBox.question(
                self,
                "登录 ADOFAI Tools",
                "将打开浏览器窗口。如果这个窗口里已经登录过 ADOFAI Tools，"
                "工具会自动读取登录态；否则请在该窗口里登录。\n\n"
                "声明：ADOFAI Tools 为第三方站点，本工具仅代为保存登录令牌以读取其公开资源，"
                "不对该站点的内容、稳定性或安全性作任何保证，使用风险由使用者自行承担。\n\n"
                "继续吗？",
            ) != QMessageBox.StandardButton.Yes:
                return
        self.tools_login_button.setEnabled(False)
        self.tools_status.setText("正在检查登录态…")
        self.toolsLoginRequested.emit()

    def set_tools_login_done(self) -> None:
        self.tools_login_button.setEnabled(True)
        self._refresh_tools_status()
        self.refresh()

    def set_tools_login_failed(self, message: str) -> None:
        self.tools_login_button.setEnabled(True)
        self._refresh_tools_status()
        self._show_message("登录失败", message, "error")

    def _on_tools_type_changed(self) -> None:
        if load_token(self.settings):
            self._refresh_tools()

    def _refresh_tools(self) -> None:
        token = load_token(self.settings)
        if not token:
            self._show_message(
                "需要登录",
                "请先点「登录 ADOFAI Tools」，登录后再刷新列表。",
                "warn",
            )
            return
        resource_type = self.tools_type_combo.currentData() or "MOD"
        self.tools_status.setText("正在加载列表…")
        self._tools_page = 1
        self.toolsRefreshRequested.emit(f"{resource_type}|1")

    def _load_more_tools(self) -> None:
        token = load_token(self.settings)
        if not token:
            return
        resource_type = self.tools_type_combo.currentData() or "MOD"
        self._tools_page += 1
        self.tools_more_button.setEnabled(False)
        self.tools_more_button.setText("加载中…")
        self.toolsRefreshRequested.emit(f"{resource_type}|{self._tools_page}")

    def set_tools_items(self, payload: Any) -> None:
        items: list[dict[str, Any]] = []
        total = 0
        page = 1
        if isinstance(payload, dict):
            items = [item for item in (payload.get("items") or []) if isinstance(item, dict)]
            total = int(payload.get("total") or 0)
            page = int(payload.get("page") or 1)
        append = page > 1
        if append:
            self._tools_items.extend(items)
        else:
            self._tools_items = items
            self._clear_tools_list()
        self._tools_total = total or len(self._tools_items)
        self._refresh_tools_status()
        self.tools_more_button.setEnabled(True)
        self.tools_more_button.setText("加载更多")
        if not self._tools_items:
            self.tools_empty.setText("没有可用的在线资源。")
            self.tools_empty.show()
            self.tools_more_button.hide()
            return
        self.tools_empty.hide()
        for item in items:
            self.tools_list.addWidget(self._tools_row(item))
        remaining = self._tools_total - len(self._tools_items)
        if remaining > 0:
            self.tools_more_button.setText(f"加载更多（还有 {remaining} 个）")
            self.tools_more_button.show()
        else:
            self.tools_more_button.hide()

    def set_tools_error(self, message: str) -> None:
        self._refresh_tools_status()
        self.tools_more_button.setEnabled(True)
        self.tools_more_button.setText("加载更多")
        if self._tools_page > 1:
            self._tools_page -= 1
        self.tools_empty.setText("加载失败，请重试。")
        if not self._tools_items:
            self.tools_empty.show()
        self._show_message("加载失败", message, "error")

    def _clear_tools_list(self) -> None:
        while self.tools_list.count():
            entry = self.tools_list.takeAt(0)
            widget = entry.widget()
            if widget is not None:
                widget.deleteLater()

    def _tools_row(self, item: dict[str, Any]) -> QWidget:
        frame = QFrame()
        frame.setObjectName("UmmModRow")
        row = QHBoxLayout(frame)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(10)
        info_box = QVBoxLayout()
        info_box.setSpacing(2)
        name = QLabel(str(item.get("display_name") or item.get("slug") or ""))
        name.setObjectName("Value")
        summary = str(item.get("summary") or "")
        meta_parts = [part for part in (str(item.get("version") or ""), summary) if part]
        meta = QLabel(" · ".join(meta_parts) or "—")
        meta.setObjectName("Muted")
        meta.setWordWrap(True)
        info_box.addWidget(name)
        info_box.addWidget(meta)
        row.addLayout(info_box, 1)
        downloads = int(item.get("downloads") or 0)
        if downloads:
            stat = QLabel(f"{downloads} 次下载")
            stat.setObjectName("Faint")
            row.addWidget(stat, 0, Qt.AlignmentFlag.AlignVCenter)
        button = QPushButton("下载并安装")
        button.setObjectName("TaskAction")
        button.clicked.connect(lambda _=False, data=item: self._download_tool(data))
        row.addWidget(button)
        return frame

    def _download_tool(self, item: dict[str, Any]) -> None:
        if not str(item.get("slug") or ""):
            self._show_message("无法下载", "该资源缺少标识，无法解析下载地址。", "warn")
            return
        if QMessageBox.question(
            self,
            "下载并安装",
            f"将下载「{item.get('display_name') or item.get('slug')}」并解压到游戏 Mods 目录。\n\n"
            "第三方资源，安装风险由使用者自行承担。继续吗？",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.tools_status.setText(f"正在下载 {item.get('display_name') or item.get('slug')}…")
        self.toolsDownloadRequested.emit(item)

    def set_tools_download_done(self, message: str) -> None:
        self._refresh_tools_status()
        self.refresh()
        self._show_message("安装完成", message, "ok")

    def set_tools_download_progress(self, message: str) -> None:
        self.tools_status.setText(message)

    def set_tools_download_failed(self, message: str) -> None:
        self._refresh_tools_status()
        self._show_message("下载失败", message, "error")

    def _do_install_umm(self) -> None:
        directory = self._read_dir() or str(default_game_dir() or "")
        if not directory:
            self._show_message("请先选择游戏目录", "点击「自动探测」或「选择目录」。", "warn")
            return
        self._save_dir()
        needs_download = umm_installer_needs_download(self.settings)
        if needs_download:
            prompt = (
                f"把 UMM 本体安装到：\n\n{directory}\n\n"
                "本地还没有可用的 UMM 安装器，会先从 UMM 官方更新仓库（国内网络可能经由镜像）"
                "下载安装包；官方源（Dropbox）连不上时自动改用 Thunderstore 备用源。"
                "拿到安装包后再在游戏目录写入 UnityModManager 相关文件与 winhttp 补丁。\n\n"
                "声明：UMM 由第三方开发者维护，本工具仅提供下载与安装辅助，"
                "不修改、不注入任何代码，不对其稳定性、安全性或与游戏版本的兼容性作保证；"
                "下载与安装风险由使用者自行承担。\n\n继续吗？"
            )
        else:
            prompt = (
                f"把 UMM 本体安装到：\n\n{directory}\n\n"
                "会在游戏目录写入 UnityModManager 相关文件与 winhttp 补丁。继续吗？"
            )
        if QMessageBox.question(self, "安装 UMM", prompt) != QMessageBox.StandardButton.Yes:
            return
        if needs_download:
            mirror = self.mirror_combo.currentData() or DEFAULT_UMM_MIRROR
            self._start_install_download(str(mirror))
            return
        self._request_action("install_umm")

    def _do_uninstall_umm(self) -> None:
        directory = self._read_dir()
        if not directory:
            self._show_message("请先选择游戏目录", "未设置游戏目录。", "warn")
            return
        if QMessageBox.question(
            self,
            "卸载 UMM",
            f"将删除 {directory} 下的 UMM 文件：UnityModManager*.exe / .dll、"
            "winhttp.dll、doorstop_config.ini 等。\n\n"
            "如果 UMM 是用安装器的 Assembly 方式注入的，会先用 .original_ 备份"
            "还原入口程序集（UMM 0.33 是 UnityEngine.CoreModule.dll，旧版可能是 "
            "Assembly-CSharp.dll），再删除 UMM 文件；"
            "缺少备份时会停止卸载并提示，避免游戏启动报错。\n\n"
            "不会删除 Mods 文件夹和游戏本体。继续吗？",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._request_action("uninstall_umm")

    def refresh_theme(self) -> None:
        """主题切换后刷新页面（颜色全部走 QSS / tone，这里只需重建状态）。"""
        self.refresh()

    def refresh(self) -> None:
        if not self._read_dir():
            configured = str(self.settings.get("umm_game_dir") or "").strip()
            if configured:
                self.dir_input.setText(configured)
        self._save_dir()
        self._refresh_tools_status()
        status = get_umm_status(self.settings)
        game_dir = status.get("game_dir") or ""
        self.dir_input.setText(game_dir)
        # doorstop 与 Assembly 两种注入都算已安装，注入方式放在提示里。
        status_message = str(status.get("message") or "")
        self.manager_status.setToolTip(status_message)
        if game_dir:
            self.dir_input.setPlaceholderText("")
            self.dir_input.setToolTip(game_dir)
            if status.get("installed"):
                self.manager_status.setText(f"已安装 · {game_dir}")
                self.manager_status.setObjectName("ChipOk")
            else:
                self.manager_status.setText(f"未安装 · {game_dir}")
                self.manager_status.setObjectName("ChipWarn")
        else:
            self.manager_status.setText("未检测到游戏目录")
            self.manager_status.setObjectName("Muted")
        source = umm_installer_source(self.settings)
        configured = str(self.settings.get("umm_installer_dir") or "").strip()
        if source is None:
            self.manager_source_label.setText(
                "安装器来源：本地暂无 · 点「一键安装 UMM」会自动下载到 "
                f"{installer_dir()}"
            )
        elif configured and not Path(configured).is_dir():
            self.manager_source_label.setText(
                f"安装器来源：{source}（设置中的目录无效，已回退）"
            )
        elif umm_installer_needs_download(self.settings):
            self.manager_source_label.setText(
                f"安装器来源：{source}（文件不完整，安装时会自动重新下载）"
            )
        else:
            self.manager_source_label.setText(f"安装器来源：{source}")
        self.installer_dir_input.blockSignals(True)
        self.installer_dir_input.setText(configured)
        self.installer_dir_input.blockSignals(False)
        self._refresh_bridge_status()
        self._refresh_hotkey_controls()
        mods = list_mods(self.settings)
        self._rows.clear()
        self._clear_grid()
        if not mods:
            empty = QLabel("未找到可用模组，请确认安装器目录里有 ADOFAI 子目录。")
            empty.setObjectName("ChartEmpty")
            self.mods_grid.addWidget(empty, 0, 0)
            return
        for index, mod in enumerate(mods):
            self._add_mod_row(index, mod)

    def _refresh_bridge_status(self) -> None:
        try:
            status = get_bridge_status(self.settings)
        except Exception as exc:
            self.bridge_status.setText("状态读取失败")
            self.bridge_status.setObjectName("ChipWarn")
            self.bridge_version.setText("")
            self.bridge_detail.setText(str(exc))
            return
        self.bridge_status.setText(str(status.get("status_text") or "未检测"))
        level = str(status.get("status_level") or "muted")
        self.bridge_status.setObjectName(
            "ChipOk" if level == "ok" else "ChipWarn" if level == "warn" else "Muted"
        )
        self.bridge_status.style().unpolish(self.bridge_status)
        self.bridge_status.style().polish(self.bridge_status)
        installed = str(status.get("installed_version") or "")
        expected = str(status.get("expected_version") or "")
        if installed:
            self.bridge_version.setText(f"已装 v{installed} · 期望 v{expected}")
        else:
            self.bridge_version.setText(f"未安装 · 期望 v{expected}")
        mod_dir = str(status.get("mod_dir") or "")
        log_path = str(status.get("log_path") or "")
        installed = bool(status.get("installed"))
        self.bridge_install_button.setVisible(not installed)
        self.bridge_repair_button.setVisible(installed)
        self.bridge_uninstall_button.setVisible(installed)
        if mod_dir:
            self.bridge_detail.setText(f"安装目录：{mod_dir}\n日志：{log_path}")
        else:
            self.bridge_detail.setText("请先在「游戏目录」里选择 ADOFAI 安装位置。")

    def _open_bridge_log(self) -> None:
        status = get_bridge_status(self.settings)
        log_path = Path(str(status.get("log_path") or ""))
        if not log_path.is_file():
            self._show_message(
                "还没有 Bridge 日志",
                "先启动一次游戏并打开一张谱面，日志才会生成。",
                "warn",
            )
            return
        self.openLogRequested.emit(str(log_path))

    def _repair_bridge(self) -> None:
        if QMessageBox.question(
            self,
            "修复 ADOFAI GO Bridge",
            "将把最新的 Bridge 文件重新覆盖到 Mods\\ADOFAIGOBridge。\n\n"
            "如果游戏正在运行，覆盖可能失败；建议先关闭游戏再修复。继续吗？",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._request_action("repair_bridge")

    def _on_bridge_auto_install(self, checked: bool) -> None:
        self.settings.set("bridge_auto_install", bool(checked))
        self.settings.save()

    def _install_bridge(self) -> None:
        if QMessageBox.question(
            self,
            "一键安装 ADOFAI GO Bridge",
            "将把 Bridge 写入游戏目录的 Mods\\ADOFAIGOBridge。\n\n"
            "不会修改游戏本体、存档或其他模组，之后可以随时卸载。\n"
            "不安装也不影响谱面库、下载、整理、投稿等桌面功能，"
            "只影响从软件直接让游戏编辑器打开谱面。\n\n"
            "如果游戏正在运行，安装后需要完全重启一次游戏。继续吗？",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._request_action("install_bridge")

    def _uninstall_bridge(self) -> None:
        if QMessageBox.question(
            self,
            "卸载 ADOFAI GO Bridge",
            "将删除 Mods\\ADOFAIGOBridge。\n\n"
            "不会影响游戏本体、存档或其他模组；桌面端其他功能仍可正常使用，"
            "只是不能从软件直接让游戏编辑器打开谱面。继续吗？",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._request_action("uninstall_bridge")

    def _refresh_hotkey_controls(self) -> None:
        if self._hotkey_recording:
            return
        raw = str(self.settings.get("game_hotkey") or DEFAULT_SETTINGS["game_hotkey"])
        try:
            display = parse_hotkey(raw).display
        except ValueError:
            display = raw
        self.hotkey_value.setText(display)
        self.hotkey_enabled.blockSignals(True)
        self.hotkey_enabled.setChecked(bool(self.settings.get("game_hotkey_enabled", True)))
        self.hotkey_enabled.blockSignals(False)
        self.bridge_auto_install.blockSignals(True)
        self.bridge_auto_install.setChecked(bool(self.settings.get("bridge_auto_install", False)))
        self.bridge_auto_install.blockSignals(False)

    def _set_hotkey_hint(self, text: str) -> None:
        """录制 / 保存过程的临时提示：有内容才显示，空文本即收起。"""
        self.hotkey_hint.setText(text)
        self.hotkey_hint.setVisible(bool(text))

    def _on_hotkey_enabled(self, checked: bool) -> None:
        self.settings.set("game_hotkey_enabled", bool(checked))
        self.settings.save()
        self.hotkeyChanged.emit()

    def _reset_hotkey(self) -> None:
        self.settings.set("game_hotkey", DEFAULT_SETTINGS["game_hotkey"])
        self.settings.save()
        self._refresh_hotkey_controls()
        self._set_hotkey_hint(f"已恢复默认：{DEFAULT_SETTINGS['game_hotkey']}。")
        self.hotkeyChanged.emit()

    def _toggle_hotkey_record(self) -> None:
        if self._hotkey_recording:
            self._cancel_hotkey_record()
        else:
            self._start_hotkey_record()

    def _start_hotkey_record(self) -> None:
        self._hotkey_recording = True
        self._hotkey_last_key = ""
        self._hotkey_last_time = 0.0
        self.hotkey_record_button.setText("请按键… Esc 取消")
        self.hotkey_value.setText("等待输入")
        self._set_hotkey_hint(
            "按一次组合键，或双击同一个修饰键（如 Shift+Shift）。"
        )
        self.setFocus(Qt.FocusReason.StrongFocus)
        self.grabKeyboard()
        self.hotkeyRecordingChanged.emit(True)

    def _cancel_hotkey_record(self) -> None:
        if not self._hotkey_recording:
            return
        self._hotkey_recording = False
        self._hotkey_hint_timer.stop()
        self.releaseKeyboard()
        self.hotkey_record_button.setText("录制")
        self._refresh_hotkey_controls()
        self._set_hotkey_hint("")
        self.hotkeyRecordingChanged.emit(False)

    def _finish_hotkey_record(self, value: str) -> None:
        try:
            spec = parse_hotkey(value)
        except ValueError as exc:
            self._set_hotkey_hint(str(exc))
            self.hotkey_value.setText(value)
            return
        self.settings.set("game_hotkey", spec.display)
        self.settings.save()
        self._hotkey_recording = False
        self._hotkey_hint_timer.stop()
        self.releaseKeyboard()
        self.hotkey_record_button.setText("录制")
        self.hotkey_value.setText(spec.display)
        self._set_hotkey_hint(f"已保存：{spec.display}。软件保持运行即可在游戏内唤起。")
        self.hotkeyRecordingChanged.emit(False)
        self.hotkeyChanged.emit()

    def _hotkey_hint_timeout(self) -> None:
        if not self._hotkey_recording:
            return
        self._hotkey_last_key = ""
        self._set_hotkey_hint(
            "请再按一次同一个修饰键，或直接按组合键；Esc 取消。"
        )

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if not self._hotkey_recording:
            super().keyPressEvent(event)
            return
        if event.isAutoRepeat():
            event.accept()
            return
        if event.key() == int(Qt.Key.Key_Escape):
            self._cancel_hotkey_record()
            event.accept()
            return
        key = qt_key_name(event.key(), event.text())
        if not key:
            event.accept()
            return
        now = time.monotonic()
        if key in {"Shift", "Ctrl", "Alt", "Win"}:
            if (
                self._hotkey_last_key == key
                and now - self._hotkey_last_time <= 0.45
            ):
                self._finish_hotkey_record(f"{key}+{key}")
                event.accept()
                return
            self._hotkey_last_key = key
            self._hotkey_last_time = now
            self._hotkey_hint_timer.start(500)
            self._set_hotkey_hint(
                f"再按一次 {key} 可设为双击；或继续按其他键组成组合键。"
            )
            event.accept()
            return
        modifiers = event.modifiers()
        parts: list[str] = []
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            parts.append("Ctrl")
        if modifiers & Qt.KeyboardModifier.AltModifier:
            parts.append("Alt")
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            parts.append("Shift")
        if modifiers & Qt.KeyboardModifier.MetaModifier:
            parts.append("Win")
        parts.append(key)
        self._finish_hotkey_record("+".join(parts))
        event.accept()

    def set_hotkey_status(self, text: str, ok: bool) -> None:
        self.hotkey_status.setText(text)
        self.hotkey_status.setObjectName("ChipOk" if ok else "ChipWarn")
        self.hotkey_status.style().unpolish(self.hotkey_status)
        self.hotkey_status.style().polish(self.hotkey_status)

    def _clear_grid(self) -> None:
        while self.mods_grid.count():
            item = self.mods_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _add_mod_row(self, index: int, mod: dict[str, Any]) -> None:
        installed = bool(mod.get("installed"))
        enabled = bool(mod.get("enabled"))
        frame = QFrame()
        frame.setObjectName("UmmModRow")
        row = QHBoxLayout(frame)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(10)
        info_box = QVBoxLayout()
        info_box.setSpacing(2)
        name_label = QLabel(str(mod.get("display_name") or mod.get("id") or ""))
        name_label.setObjectName("Value")
        name_label.setToolTip(str(mod.get("description") or ""))
        meta = QLabel(self._mod_meta(mod))
        meta.setObjectName("Muted")
        info_box.addWidget(name_label)
        info_box.addWidget(meta)
        row.addLayout(info_box, 1)
        archive_label = QLabel(f"{len(mod.get('archives') or [])} 个版本")
        archive_label.setObjectName("Faint")
        row.addWidget(archive_label, 0, Qt.AlignmentFlag.AlignVCenter)
        buttons: list[QPushButton] = []
        if installed:
            status_label = QLabel("已启用" if enabled else "已禁用")
            status_label.setObjectName("ChipOk" if enabled else "ChipWarn")
            row.addWidget(status_label, 0, Qt.AlignmentFlag.AlignVCenter)
            toggle = QPushButton("禁用" if enabled else "启用")
            toggle.setObjectName("TaskAction")
            toggle.clicked.connect(
                lambda _=False, m=mod: self._toggle_mod(m)
            )
            row.addWidget(toggle)
            buttons.append(toggle)
            uninstall = QPushButton("卸载")
            uninstall.setObjectName("TaskActionDanger")
            uninstall.clicked.connect(lambda _=False, m=mod: self._uninstall_mod(m))
            row.addWidget(uninstall)
            buttons.append(uninstall)
        else:
            install = QPushButton("安装")
            install.setObjectName("TaskAction")
            install.clicked.connect(lambda _=False, m=mod: self._install_mod(m))
            row.addWidget(install)
            buttons.append(install)
        self.mods_grid.addWidget(frame, index, 0, 1, 4)
        self._rows[mod["id"]] = {"frame": frame, "buttons": buttons}

    @staticmethod
    def _mod_meta(mod: dict[str, Any]) -> str:
        parts: list[str] = []
        version = str(mod.get("version") or "")
        author = str(mod.get("author") or "")
        if version:
            parts.append(f"v{version}")
        if author:
            parts.append(author)
        description = str(mod.get("description") or "")
        if description:
            parts.append(description)
        return " · ".join(parts)

    def _install_mod(self, mod: dict[str, Any]) -> None:
        archives = mod.get("archives") or []
        archive_path: str | None = None
        if len(archives) == 1:
            archive_path = archives[0]
        elif len(archives) > 1:
            archive_path = self._choose_archive(mod, archives)
            if archive_path is None:
                return
        self._request_action(
            "install_mod",
            {
                "mod_id": mod["id"],
                "archive_path": archive_path,
                "display_name": str(mod.get("display_name") or mod["id"]),
            },
        )

    def _choose_archive(self, mod: dict[str, Any], archives: list[str]) -> str | None:
        import sys

        from PySide6.QtWidgets import QInputDialog

        items = [Path(path).name for path in archives]
        current = QInputDialog.getItem(
            self,
            "选择版本",
            f"{mod.get('display_name')} 有多个版本，选择要安装的版本：",
            items,
            0,
            False,
        )
        if not current[1]:
            return None
        index = items.index(current[0])
        return archives[index]

    def _uninstall_mod(self, mod: dict[str, Any]) -> None:
        if QMessageBox.question(
            self,
            "卸载模组",
            f"删除 Mods\\{mod['id']} 文件夹吗？\n\n不会影响安装器目录里的压缩包。",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._request_action(
            "uninstall_mod",
            {
                "mod_id": mod["id"],
                "display_name": str(mod.get("display_name") or mod["id"]),
            },
        )

    def _toggle_mod(self, mod: dict[str, Any]) -> None:
        enabled = bool(mod.get("enabled"))
        self._request_action(
            "set_mod_enabled",
            {
                "mod_id": mod["id"],
                "enabled": not enabled,
                "display_name": str(mod.get("display_name") or mod["id"]),
            },
        )

    def _request_action(
        self, action: str, payload: dict[str, Any] | None = None
    ) -> None:
        """把文件操作交给 MainWindow 的 worker，页面不再同步调用后端。"""
        if self._action_busy:
            return
        self.set_action_busy(True, self.action_label(action))
        if action == "install_umm":
            # 安装期间不允许再点「在线更新 UMM」，避免重复下载安装包。
            self.update_button.setEnabled(False)
        self.actionRequested.emit(action, dict(payload or {}))

    def _action_buttons(self) -> list[QPushButton]:
        buttons = [
            self.install_umm_button,
            self.uninstall_umm_button,
            self.local_install_button,
            self.bridge_install_button,
            self.bridge_repair_button,
            self.bridge_uninstall_button,
        ]
        for row in self._rows.values():
            buttons.extend(row.get("buttons") or [])
        return buttons

    def set_action_busy(self, busy: bool, label: str = "") -> None:
        """禁用文件操作按钮并显示当前操作；完成后恢复。"""
        self._action_busy = bool(busy)
        for button in self._action_buttons():
            button.setEnabled(not busy)
        if busy:
            self.action_status.setText(label or "正在处理…")
            self.action_status.show()
        else:
            self.action_status.setText("")
            self.action_status.hide()
            if not self._update_running:
                self.update_button.setEnabled(True)

    def action_label(self, action: str) -> str:
        return ACTION_LABELS.get(action, "正在处理…")

    def show_action_result(self, action: str, payload: dict[str, Any], result: Any) -> None:
        """在 UI 线程展示某个后台动作的结果。"""
        mod_id = str(payload.get("mod_id") or "")
        name = str(payload.get("display_name") or mod_id)
        if action == "install_umm":
            self._show_message(
                "UMM 已安装",
                f"已安装到：{result}\n\nmod 请从下方列表安装。",
                "ok",
            )
        elif action == "uninstall_umm":
            self._show_message("UMM 已卸载", "游戏目录里的 UMM 本体文件已删除。", "ok")
        elif action == "install_local_update":
            self._show_message(
                "本地更新包已安装",
                f"已解压到：{result}\n\n现在点「一键安装 UMM」即可安装到游戏目录。",
                "ok",
            )
        elif action == "install_mod":
            self._show_message(
                f"{name} 已安装",
                f"已解压到 Mods\\{mod_id}，游戏里打开 UMM 即可启用。",
                "ok",
            )
        elif action == "uninstall_mod":
            self._show_message("模组已卸载", f"已删除 Mods\\{mod_id}。", "ok")
        elif action == "set_mod_enabled":
            enabled = bool(payload.get("enabled"))
            self._show_message(
                f"{name} 已{'启用' if enabled else '禁用'}",
                "重启游戏后生效。",
                "ok",
            )
        elif action == "install_bridge":
            self._show_message(
                "Bridge 已安装",
                f"已安装到：\n{result}\n\n"
                "如果游戏刚才正在运行，请完全关闭游戏再重新启动，让 Bridge 生效。",
                "ok",
            )
        elif action == "repair_bridge":
            self._show_message(
                "Bridge 已修复",
                f"已重新安装到：\n{result}\n\n"
                "如果刚才游戏正在运行，请完全关闭游戏再重新启动，让新文件生效。",
                "ok",
            )
        elif action == "uninstall_bridge":
            self._show_message("Bridge 已卸载", "Mods\\ADOFAIGOBridge 已删除。", "ok")

    def show_action_error(self, action: str, message: str) -> None:
        title = ACTION_ERROR_TITLES.get(action, "操作失败")
        if action == "install_umm" and message.startswith(UMM_DOWNLOAD_ERROR_PREFIX):
            # 一键安装的自动下载失败：和真正的安装失败区分开。
            title = "下载失败"
        self._show_message(title, message, "error")

    def _show_message(self, title: str, text: str, level: str) -> None:
        if level == "ok":
            QMessageBox.information(self, title, text)
        elif level == "warn":
            QMessageBox.warning(self, title, text)
        else:
            QMessageBox.critical(self, title, text)

