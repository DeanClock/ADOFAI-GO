import os
import sys
import time
import json
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ADOFAI_NO_AUTOLOGIN", "1")

from PySide6.QtCore import (
    QEvent,
    QItemSelectionModel,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QThread,
    Qt,
    Signal,
)
from PySide6.QtGui import QMouseEvent, QShortcut
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
)

from adofai_publisher.config import Settings
from adofai_publisher.chart_analyzer import ChartAnalysis, analyze_chart
from adofai_publisher.download_tasks import (
    STATUS_PENDING,
    DownloadQueue,
    save_queue,
)
from adofai_publisher.models import Draft, LevelInfo
from adofai_publisher.level_organizer import OrganizeItem, OrganizePlan
from adofai_publisher.tuf_browse import Q_SPECIAL_DIFFICULTIES
from adofai_publisher.ui.main_window import NAV_ITEMS, MainWindow, is_safe_profile_path
from adofai_publisher.ui.home_page import HomePage
from adofai_publisher.ui.pages import WorkspacePage
from adofai_publisher.ui.chart_library import (
    ChartDetailDialog,
    ChartItem,
    ChartLibraryPage,
    DifficultyRangeSlider,
    FilterMenuButton,
    PersistentMenu,
    SubmissionQueueDialog,
    _qss_color,
)
from adofai_publisher.ui.widgets import LevelPickerDialog
from adofai_publisher.ui.batch_bind import BatchBindDialog
from adofai_publisher.ui.collection_manager import CollectionManagerDialog
from adofai_publisher.ui.disk_organizer import DiskOrganizerDialog
from adofai_publisher.ui.organize_history import OrganizeHistoryDialog


def _wait_for_thread(thread, timeout: int = 5000) -> None:
    """等后台 QThread 收尾，不按固定时长 sleep。

    ``QSignalSpy.wait`` 在超时前跑真实事件循环，主线程仍会处理 ``quit`` 与
    ``singleShot(0)`` 回调；线程一结束就返回，不会因机器慢而误判。
    """
    spy = QSignalSpy(thread.finished)
    assert spy.wait(timeout), "后台线程没有在超时前结束"


def test_qss_color_uses_argb_order():
    assert _qss_color("#e1a8ff", 26) == "#1ae1a8ff"
    assert _qss_color("not-a-color", 96) == "#607dd3fc"


def test_page_argument_parsing(monkeypatch):
    from adofai_publisher.main import _page_argument, _tab_argument

    monkeypatch.setattr(sys, "argv", ["run.py", "--page", "library"])
    assert _page_argument() == "library"
    assert _tab_argument() == ""
    monkeypatch.setattr(sys, "argv", ["run.py", "--page=library", "--tab=local"])
    assert _page_argument() == "library"
    assert _tab_argument() == "local"
    monkeypatch.setattr(sys, "argv", ["run.py", "--page=settings"])
    assert _page_argument() == "settings"
    monkeypatch.setattr(sys, "argv", ["run.py"])
    assert _page_argument() == ""


def test_settings_page_collect_keys_all_have_defaults(tmp_path):
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.config import DEFAULT_SETTINGS
    from adofai_publisher.ui.pages import SettingsPage

    page = SettingsPage(Settings(tmp_path / "settings.json"))
    unknown = sorted(set(page.collect()) - set(DEFAULT_SETTINGS))
    assert unknown == []
    page.deleteLater()
    app.processEvents()


def test_settings_snippet_hint_points_to_workspace_step_four(tmp_path):
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.ui.pages import SettingsPage

    page = SettingsPage(Settings(tmp_path / "settings.json"))
    texts = [label.text() for label in page.findChildren(QLabel)]
    assert any("第 4 步 文案" in text for text in texts)
    assert not any("第 3 步 文案" in text for text in texts)
    page.deleteLater()
    app.processEvents()
    assert app is not None


def test_settings_page_obs_defaults_and_collect(tmp_path):
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.ui.pages import SettingsPage

    settings = Settings(tmp_path / "settings.json")
    page = SettingsPage(settings)

    assert page.obs_host_input.text() == "127.0.0.1"
    assert page.obs_port_input.value() == 4455
    assert page.obs_password_input.text() == ""
    assert page.obs_password_input.echoMode() == page.obs_password_input.EchoMode.Password

    page.obs_host_input.setText("192.168.1.10")
    page.obs_port_input.setValue(4466)
    page.obs_password_input.setText("local-secret")
    values = page.collect()
    assert values["obs_host"] == "192.168.1.10"
    assert values["obs_port"] == 4466
    assert values["obs_password"] == "local-secret"

    page.close()
    page.deleteLater()
    app.processEvents()


def test_settings_page_obs_recording_state_and_path(tmp_path):
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.ui.pages import SettingsPage

    settings = Settings(tmp_path / "settings.json")
    page = SettingsPage(settings)
    page.set_obs_recording(True)
    assert page.obs_stop_button.isEnabled()
    assert not page.obs_start_button.isEnabled()

    page.set_obs_busy(True, "正在停止…")
    assert not page.obs_stop_button.isEnabled()
    page.set_obs_busy(False)
    assert page.obs_stop_button.isEnabled()

    page.set_obs_recording(False)
    assert page.obs_start_button.isEnabled()
    assert not page.obs_stop_button.isEnabled()

    opened: list[str] = []
    page.obsOpenFolderRequested.connect(opened.append)
    page.set_obs_output_path(r"C:\Users\DeanClock\Videos\recording.mp4")
    assert "recording.mp4" in page.obs_path.text()
    assert page.obs_open_button.isEnabled()
    page.obs_open_button.click()
    assert opened == [r"C:\Users\DeanClock\Videos"]

    page.close()
    page.deleteLater()
    app.processEvents()


def test_settings_page_obs_advanced_defaults_and_expands(tmp_path):
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.ui.pages import SettingsPage

    settings = Settings(tmp_path / "settings.json")
    page = SettingsPage(settings)

    assert not page.obs_advanced_panel.isVisibleTo(page)
    assert not page.obs_advanced_button.isChecked()
    assert page.obs_exe_path_input.text() == ""

    page.obs_advanced_button.click()
    assert page.obs_advanced_panel.isVisibleTo(page)
    page.obs_advanced_button.click()
    assert not page.obs_advanced_panel.isVisibleTo(page)

    settings.set("obs_host", "192.168.1.10")
    page.load()
    assert page.obs_advanced_panel.isVisibleTo(page)
    assert page.obs_advanced_button.isChecked()

    page.obs_exe_path_input.setText(r"C:\Tools\obs64.exe")
    assert page.collect()["obs_exe_path"] == r"C:\Tools\obs64.exe"
    page.close()
    page.deleteLater()
    app.processEvents()


def test_settings_page_obs_launch_signal(tmp_path):
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.ui.pages import SettingsPage

    page = SettingsPage(Settings(tmp_path / "settings.json"))
    launched: list[bool] = []
    page.obsLaunchRequested.connect(lambda: launched.append(True))

    page.obs_launch_button.click()

    assert launched == [True]
    page.close()
    page.deleteLater()
    app.processEvents()


def test_settings_page_update_defaults_and_signals(tmp_path):
    app = QApplication.instance() or QApplication([])
    from adofai_publisher import __version__
    from adofai_publisher.ui.pages import SettingsPage

    settings = Settings(tmp_path / "settings.json")
    page = SettingsPage(settings)
    requested: list[str] = []
    page.updateCheckRequested.connect(requested.append)

    assert page.update_repo_input.text() == "DeanClock/ADOFAI-GO"
    assert page.update_repo_input.placeholderText() == "owner/repo"
    assert page.update_check_button.text() == "检查更新"
    assert "已配置更新仓库" in page.update_status.text()
    assert page.collect()["update_repo"] == "DeanClock/ADOFAI-GO"
    assert page.auto_check_updates_checkbox.isChecked() is True
    assert page.collect()["auto_check_updates"] is True

    page.update_check_button.click()
    assert requested == ["DeanClock/ADOFAI-GO"]
    assert page.update_check_button.isEnabled()

    page.update_repo_input.setText("DeanColck/ADOFAI-GO")
    assert page.collect()["update_repo"] == "DeanColck/ADOFAI-GO"
    page.set_update_busy(True)
    assert not page.update_check_button.isEnabled()
    assert page.update_check_button.text() == "检查中…"
    assert not page.update_repo_input.isEnabled()
    page.set_update_busy(False)
    assert page.update_check_button.isEnabled()

    page.show_update_result(
        {
            "latest": "v0.14.0",
            "current": __version__,
            "has_update": True,
            "body": "更新说明",
            "html_url": "https://github.com/DeanColck/ADOFAI-GO/releases",
        }
    )
    assert "发现新版本 v0.14.0" in page.update_status.text()
    assert page.update_notes.text() == "更新说明"
    assert not page.update_notes.isHidden()
    assert not page.update_download_button.isHidden()

    opened: list[str] = []
    page.updateOpenRequested.connect(opened.append)
    page.update_download_button.click()
    assert opened == ["https://github.com/DeanColck/ADOFAI-GO/releases"]

    page.show_update_result(
        {
            "latest": "v0.13.0",
            "current": __version__,
            "has_update": False,
        }
    )
    assert "未发现新版本" in page.update_status.text()
    assert page.update_notes.isHidden()
    assert page.update_download_button.isHidden()

    page.set_update_error("仓库不存在或没有可用的 Release（404）")
    assert "404" in page.update_status.text()

    settings.set("update_repo", "DeanColck/ADOFAI-GO")
    settings.set("auto_check_updates", False)
    page.load()
    assert page.update_repo_input.text() == "DeanColck/ADOFAI-GO"
    assert page.auto_check_updates_checkbox.isChecked() is False
    assert page.collect()["auto_check_updates"] is False
    assert "已配置更新仓库" in page.update_status.text()

    page.set_update_status("发现新版本 v0.14.0", "warn")
    page.refresh_theme()
    assert page.update_status.text() == "发现新版本 v0.14.0"
    page.close()
    page.deleteLater()
    app.processEvents()


def test_settings_page_manages_copy_snippets(tmp_path):
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.config import Settings
    from adofai_publisher.copy_snippets import DEFAULT_COPY_SNIPPETS
    from adofai_publisher.ui.pages import SettingsPage

    path = tmp_path / "settings.json"
    settings = Settings(path)
    page = SettingsPage(settings)

    assert page.snippet_table.rowCount() == len(DEFAULT_COPY_SNIPPETS)
    index = page.add_snippet("初见", "手感")
    assert index == len(DEFAULT_COPY_SNIPPETS)
    assert page.update_snippet(index, "初见即过", "成绩") is True
    assert page.collect()["copy_snippets"][-1] == {
        "text": "初见即过",
        "group": "成绩",
    }

    settings.update(page.collect())
    settings.save()
    reloaded = Settings(path)
    assert {"text": "初见即过", "group": "成绩"} in reloaded.get("copy_snippets")

    assert page.remove_snippet(index) is True
    assert page.snippet_table.rowCount() == len(DEFAULT_COPY_SNIPPETS)
    settings.update(page.collect())
    settings.save()
    assert {"text": "初见即过", "group": "成绩"} not in Settings(path).get(
        "copy_snippets"
    )
    page.close()
    page.deleteLater()
    app.processEvents()


def test_workspace_shows_snippet_cards(tmp_path):
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.config import Settings
    from adofai_publisher.ui.pages import WorkspacePage

    page = WorkspacePage(Settings(tmp_path / "settings.json"))
    texts = [button.text() for button in page._snippet_buttons]

    assert "严判击破" in texts
    assert "完美无瑕" in texts
    assert "手感极佳" in texts
    page.close()
    page.deleteLater()
    app.processEvents()


def test_workspace_snippet_inserts_at_cursor():
    app = QApplication.instance() or QApplication([])
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from adofai_publisher.config import Settings
    from adofai_publisher.ui.pages import WorkspacePage

    with TemporaryDirectory() as folder:
        page = WorkspacePage(Settings(Path(folder) / "settings.json"))
        page.show()
        page.windowHandle().requestActivate()
        app.processEvents()
        page.title_input.setText("【冰与火之舞】Song")
        page.title_input.setCursorPosition(len(page.title_input.text()))
        page.title_input.setFocus(Qt.FocusReason.MouseFocusReason)
        app.processEvents()

        page._insert_copy_snippet("严判击破")

        assert page.title_input.text() == "【冰与火之舞】Song 严判击破"
        assert "已插入「严判击破」到标题" == page.snippet_hint.text()
        page.close()
        page.deleteLater()
        app.processEvents()


def test_workspace_snippet_inserts_into_tags_when_tags_focused():
    app = QApplication.instance() or QApplication([])
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from adofai_publisher.config import Settings
    from adofai_publisher.ui.pages import WorkspacePage

    with TemporaryDirectory() as folder:
        page = WorkspacePage(Settings(Path(folder) / "settings.json"))
        page.show()
        page.windowHandle().requestActivate()
        app.processEvents()
        page.tags_input.setText("冰与火之舞, ADOFAI")
        page.tags_input.setCursorPosition(len(page.tags_input.text()))
        page.tags_input.setFocus(Qt.FocusReason.MouseFocusReason)
        app.processEvents()

        page._insert_copy_snippet("严判")

        assert page.tags_input.text() == "冰与火之舞, ADOFAI 严判"
        assert page.title_input.text() == ""
        assert page.desc_input.toPlainText() == ""
        assert page.snippet_hint.text() == "已插入「严判」到标签"
        page.close()
        page.deleteLater()
        app.processEvents()


def _open_workspace_page(app, tmp_path):
    page = WorkspacePage(Settings(tmp_path / "settings.json"))
    page.show()
    page.windowHandle().requestActivate()
    page._show_step(3)
    app.processEvents()
    return page


def _close_workspace_page(page, app):
    page.close()
    page.deleteLater()
    app.processEvents()


def _click_snippet(page, text):
    button = next(item for item in page._snippet_buttons if item.text() == text)
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    QApplication.instance().processEvents()
    return button


def test_workspace_snippet_real_click_inserts_into_title_at_cursor(tmp_path):
    app = QApplication.instance() or QApplication([])
    page = _open_workspace_page(app, tmp_path)
    page.title_input.setText("【冰与火之舞】Song")
    page.title_input.setCursorPosition(len(page.title_input.text()))
    page.title_input.setFocus(Qt.FocusReason.MouseFocusReason)
    app.processEvents()

    button = _click_snippet(page, "严判击破")

    assert page.title_input.text() == "【冰与火之舞】Song 严判击破"
    assert page.title_input.cursorPosition() == len(page.title_input.text())
    assert page.title_input.hasFocus()
    assert page.tags_input.text() == ""
    assert page.desc_input.toPlainText() == ""
    assert page.snippet_hint.text() == "已插入「严判击破」到标题"
    assert button.isChecked()
    _close_workspace_page(page, app)


def test_workspace_snippet_real_click_inserts_into_tags_at_cursor(tmp_path):
    app = QApplication.instance() or QApplication([])
    page = _open_workspace_page(app, tmp_path)
    page.tags_input.setText("冰与火之舞, ADOFAI")
    page.tags_input.setCursorPosition(5)
    page.tags_input.setFocus(Qt.FocusReason.MouseFocusReason)
    app.processEvents()

    _click_snippet(page, "严判")

    assert page.tags_input.text() == "冰与火之舞 严判, ADOFAI"
    assert page.tags_input.cursorPosition() == 8
    assert page.tags_input.hasFocus()
    assert page.title_input.text() == ""
    assert page.desc_input.toPlainText() == ""
    assert page.snippet_hint.text() == "已插入「严判」到标签"
    _close_workspace_page(page, app)


def test_workspace_snippet_real_click_inserts_into_desc_at_cursor(tmp_path):
    app = QApplication.instance() or QApplication([])
    page = _open_workspace_page(app, tmp_path)
    page.desc_input.setPlainText("第一行\n第二行\n第三行")
    cursor = page.desc_input.textCursor()
    cursor.setPosition(7)
    page.desc_input.setTextCursor(cursor)
    page.desc_input.setFocus(Qt.FocusReason.MouseFocusReason)
    app.processEvents()

    _click_snippet(page, "手感极佳")

    assert page.desc_input.toPlainText() == "第一行\n第二行 手感极佳\n第三行"
    assert page.desc_input.textCursor().position() == 12
    assert page.desc_input.hasFocus()
    assert page.title_input.text() == ""
    assert page.tags_input.text() == ""
    assert page.snippet_hint.text() == "已插入「手感极佳」到简介"
    _close_workspace_page(page, app)


def test_workspace_snippet_real_click_without_focus_shows_hint(tmp_path):
    app = QApplication.instance() or QApplication([])
    page = _open_workspace_page(app, tmp_path)
    page.title_input.setText("标题原文")
    page.title_input.setCursorPosition(len(page.title_input.text()))
    page.title_input.setFocus(Qt.FocusReason.MouseFocusReason)
    app.processEvents()
    first = _click_snippet(page, "严判")
    assert first.isChecked()

    page.desc_input.setPlainText("简介原文")
    page.tags_input.setText("标签原文")
    app.processEvents()
    page.next_button.setFocus(Qt.FocusReason.OtherFocusReason)
    app.processEvents()
    assert not page.title_input.hasFocus()
    assert not page.tags_input.hasFocus()
    assert not page.desc_input.hasFocus()

    _click_snippet(page, "完美无瑕")

    assert page.title_input.text() == "标题原文 严判"
    assert page.tags_input.text() == "标签原文"
    assert page.desc_input.toPlainText() == "简介原文"
    assert page.snippet_hint.text() == "先点一下标题、简介或标签，再点词条"
    assert not any(button.isChecked() for button in page._snippet_buttons)
    _close_workspace_page(page, app)


def test_workspace_snippet_consecutive_clicks_stay_in_title(tmp_path):
    app = QApplication.instance() or QApplication([])
    page = _open_workspace_page(app, tmp_path)
    page.title_input.setText("【冰与火之舞】Song")
    page.title_input.setCursorPosition(len(page.title_input.text()))
    page.title_input.setFocus(Qt.FocusReason.MouseFocusReason)
    app.processEvents()

    first = _click_snippet(page, "严判")
    second = _click_snippet(page, "标判")

    assert page.title_input.text() == "【冰与火之舞】Song 严判 标判"
    assert page.title_input.cursorPosition() == len(page.title_input.text())
    assert page.title_input.hasFocus()
    assert not first.isChecked()
    assert second.isChecked()
    assert page.desc_input.toPlainText() == ""
    assert page.tags_input.text() == ""
    _close_workspace_page(page, app)


def test_workspace_snippet_real_click_replaces_selection(tmp_path):
    app = QApplication.instance() or QApplication([])
    page = _open_workspace_page(app, tmp_path)
    page.title_input.setText("【冰与火之舞】Song")
    page.title_input.setSelection(7, 4)
    page.title_input.setFocus(Qt.FocusReason.MouseFocusReason)
    app.processEvents()

    _click_snippet(page, "完美无瑕")

    assert page.title_input.text() == "【冰与火之舞】完美无瑕"
    assert page.title_input.cursorPosition() == len(page.title_input.text())
    assert page.snippet_hint.text() == "已插入「完美无瑕」到标题"
    _close_workspace_page(page, app)


def test_main_window_update_check_unconfigured_does_not_start(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    started: list[bool] = []
    monkeypatch.setattr(
        window,
        "_start_worker",
        lambda *args, **kwargs: started.append(True),
    )

    window._check_update("   ")

    assert started == []
    assert "未配置更新仓库" in window.settings_page.update_status.text()
    assert window._update_check_worker is None
    window.close()
    app.processEvents()


def test_main_window_update_check_starts_worker_and_saves_repo(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    captured = {}

    def fake_start(worker, **kwargs):
        captured["worker"] = worker
        captured["kwargs"] = kwargs

    monkeypatch.setattr(window, "_start_worker", fake_start)

    window._check_update("https://github.com/DeanColck/ADOFAI-GO/")

    worker = captured["worker"]
    assert worker.repo == "DeanColck/ADOFAI-GO"
    assert window.settings.get("update_repo") == "DeanColck/ADOFAI-GO"
    assert window.settings_page.update_check_button.text() == "检查中…"
    assert "callbacks" not in captured["kwargs"]
    assert captured["kwargs"]["on_finished"] == window._on_update_checked
    assert captured["kwargs"]["on_failed"] == window._on_update_check_failed

    window._on_update_checked(
        {
            "latest": "v0.14.0",
            "current": "0.13.0",
            "has_update": True,
            "body": "更新说明",
            "html_url": "https://github.com/DeanColck/ADOFAI-GO/releases",
        }
    )
    assert "发现新版本 v0.14.0" in window.settings_page.update_status.text()
    assert window.settings_page.update_check_button.isEnabled()
    window._on_update_check_finished()
    assert window._update_check_worker is None
    window.close()
    app.processEvents()


def test_main_window_update_check_failure_is_readable(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))

    window._on_update_check_failed("GitHub 限流（403），请稍后再试")

    assert "403" in window.settings_page.update_status.text()
    assert window.settings_page.update_check_button.isEnabled()
    assert window.settings_page.update_download_button.isHidden()
    window.close()
    app.processEvents()


def test_home_page_obs_busy_toggles_button(tmp_path):
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.ui.home_page import HomePage

    page = HomePage()
    page.set_obs_busy(True)
    assert not page.guide_obs_launch_button.isEnabled()
    page.set_obs_busy(False)
    assert page.guide_obs_launch_button.isEnabled()
    page.close()
    page.deleteLater()
    app.processEvents()


def test_home_page_guide_is_persistent(tmp_path):
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.ui.home_page import HomePage

    page = HomePage()
    assert not page.guide_card.isHidden()
    assert not hasattr(page, "guide_dismiss_button")
    page.close()
    page.deleteLater()
    app.processEvents()


def test_home_greeting_does_not_hardcode_author_name():
    from adofai_publisher.ui.home_page import _greeting

    greeting = _greeting()
    assert greeting
    assert "DC" not in greeting


def test_home_page_guide_launch_button_order_and_signal():
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.ui.home_page import HomePage

    page = HomePage()
    actions = next(
        layout
        for layout in (
            page.guide_card.body.itemAt(index).layout()
            for index in range(page.guide_card.body.count())
        )
        if layout is not None and layout.indexOf(page.guide_workspace_button) >= 0
    )
    ordered = [
        actions.itemAt(index).widget()
        for index in range(actions.count())
        if actions.itemAt(index).widget() is not None
    ]
    assert ordered == [
        page.guide_workspace_button,
        page.guide_launch_button,
        page.guide_obs_launch_button,
        page.guide_obs_settings_button,
    ]
    assert page.guide_launch_button.text() == "打开 ADOFAI"

    launched: list[bool] = []
    page.launchGameRequested.connect(lambda: launched.append(True))
    page.guide_launch_button.click()
    assert launched == [True]
    page.close()
    page.deleteLater()
    app.processEvents()


def test_main_window_home_guide_actions_navigate(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))

    window.home.guide_workspace_button.click()
    assert window.stack.currentWidget() is window.workspace

    window._navigate("home")
    focused: list[bool] = []
    monkeypatch.setattr(
        window.settings_page,
        "focus_obs_section",
        lambda: focused.append(True),
    )
    window.home.guide_obs_settings_button.click()
    app.processEvents()

    assert window.stack.currentWidget() is window.settings_page
    assert focused == [True]
    window.close()
    app.processEvents()


def test_main_window_obs_launch_when_running(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    launched: list[Path] = []
    monkeypatch.setattr(
        "adofai_publisher.ui.main_window.is_obs_running", lambda: True
    )
    monkeypatch.setattr(
        "adofai_publisher.ui.main_window.find_obs_executable",
        lambda _path: None,
    )
    monkeypatch.setattr(
        "adofai_publisher.ui.main_window.launch_obs",
        lambda path: launched.append(path),
    )

    window._obs_launch()

    assert "已在运行" in window.settings_page.obs_status.text()
    assert window._obs_launch_timer.isActive()
    assert window._obs_launch_timer.remainingTime() <= 200
    assert launched == []
    window.close()
    app.processEvents()


def test_main_window_obs_launch_not_running_schedules_test(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    exe = tmp_path / "obs64.exe"
    exe.write_bytes(b"exe")
    launched: list[Path] = []
    tested: list[tuple[str, int, str]] = []
    monkeypatch.setattr(
        "adofai_publisher.ui.main_window.is_obs_running", lambda: False
    )
    monkeypatch.setattr(
        "adofai_publisher.ui.main_window.find_obs_executable",
        lambda _path: exe,
    )
    monkeypatch.setattr(
        "adofai_publisher.ui.main_window.launch_obs",
        lambda path: launched.append(path),
    )
    monkeypatch.setattr(
        window._obs,
        "test_connection",
        lambda host, port, password: tested.append((host, port, password)),
    )
    window.settings_page.obs_password_input.setText("secret")

    window._obs_launch()

    assert launched == [exe]
    assert "5 秒后自动测试" in window.settings_page.obs_status.text()
    assert window._obs_launch_timer.isActive()
    window._obs_launch_timer.timeout.emit()
    assert tested == [("127.0.0.1", 4455, "secret")]
    assert not window._obs_launch_timer.isActive()
    window.close()
    app.processEvents()


def test_main_window_obs_launch_missing_cancel(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    monkeypatch.setattr(
        "adofai_publisher.ui.main_window.is_obs_running", lambda: False
    )
    monkeypatch.setattr(
        "adofai_publisher.ui.main_window.find_obs_executable",
        lambda _path: None,
    )
    monkeypatch.setattr(
        "adofai_publisher.ui.main_window.QFileDialog.getOpenFileName",
        lambda *args, **kwargs: ("", ""),
    )

    window._obs_launch()

    assert "未找到 OBS" in window.settings_page.obs_status.text()
    assert not window._obs_launch_timer.isActive()
    assert window.settings_page.obs_launch_button.isEnabled()
    window.close()
    app.processEvents()


def test_main_window_obs_launch_missing_selects_and_saves_path(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    exe = tmp_path / "obs64.exe"
    exe.write_bytes(b"exe")
    launched: list[Path] = []
    monkeypatch.setattr(
        "adofai_publisher.ui.main_window.is_obs_running", lambda: False
    )
    monkeypatch.setattr(
        "adofai_publisher.ui.main_window.find_obs_executable",
        lambda _path: None,
    )
    monkeypatch.setattr(
        "adofai_publisher.ui.main_window.QFileDialog.getOpenFileName",
        lambda *args, **kwargs: (str(exe), ""),
    )
    monkeypatch.setattr(
        "adofai_publisher.ui.main_window.launch_obs",
        lambda path: launched.append(path),
    )

    window._obs_launch()

    assert window.settings.get("obs_exe_path") == str(exe)
    assert launched == [exe]
    assert window.settings_page.obs_advanced_panel.isVisibleTo(
        window.settings_page
    )
    assert window._obs_launch_timer.isActive()
    window.close()
    app.processEvents()


def test_main_window_obs_launch_failure_resets_state(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    exe = tmp_path / "obs64.exe"
    exe.write_bytes(b"exe")
    monkeypatch.setattr(
        "adofai_publisher.ui.main_window.is_obs_running", lambda: False
    )
    monkeypatch.setattr(
        "adofai_publisher.ui.main_window.find_obs_executable",
        lambda _path: exe,
    )

    def fail(_path):
        raise OSError("cannot start")

    monkeypatch.setattr(
        "adofai_publisher.ui.main_window.launch_obs", fail
    )

    window._obs_launch()

    assert "启动 OBS 失败" in window.settings_page.obs_status.text()
    assert "cannot start" in window.settings_page.obs_status.text()
    assert not window._obs_launch_timer.isActive()
    assert window.settings_page.obs_launch_button.isEnabled()
    window.close()
    app.processEvents()


def test_main_window_obs_stop_shows_path_and_opens_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    folder = tmp_path / "Videos"
    folder.mkdir()
    path = folder / "recording.mp4"
    path.write_bytes(b"video")

    opened: list[str] = []
    monkeypatch.setattr(
        "adofai_publisher.ui.main_window.QDesktopServices.openUrl",
        lambda url: opened.append(url.toLocalFile()),
    )

    window._on_obs_record_stopped(str(path))

    assert window.settings_page.obs_start_button.isEnabled()
    assert "recording.mp4" in window.settings_page.obs_path.text()
    assert not window.toast.action_button.isHidden()
    assert "recording.mp4" in window.toast.text()
    window.toast.action_button.click()
    assert opened == [folder.as_posix()]
    window.close()
    app.processEvents()


def test_main_window_obs_failure_resets_buttons(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    window.settings_page.set_obs_recording(True)

    window._on_obs_failed("OBS 密码错误")

    assert window.settings_page.obs_start_button.isEnabled()
    assert not window.settings_page.obs_stop_button.isEnabled()
    assert "密码错误" in window.settings_page.obs_status.text()
    window.close()
    app.processEvents()


def test_main_window_builds(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    settings = Settings(tmp_path / "settings.json")
    window = MainWindow(settings)
    assert window.stack.count() == len(NAV_ITEMS) == 7
    assert window.stack.currentWidget() is window.home
    assert window.home is not None
    assert [
        window.nav_buttons[key].text().split()[0] for key, *_rest in NAV_ITEMS
    ] == ["00", "01", "02", "03", "04", "05", "06"]
    assert "publish" not in window.nav_buttons
    assert "publish" not in window.pages
    assert window.library is not None
    assert window.library.online_list is not None
    assert window.library.local_list is not None
    assert window.workspace.step_bar.steps == [
        "发布方式",
        "选片",
        "谱面 & 数据",
        "文案",
        "检查与发布",
    ]
    assert window.workspace.mode_dual_button.isChecked()
    assert window.workspace.publish_panel.upload_button.text() == "上传到 B 站"
    window.close()
    assert app is not None


def test_navigation_shortcuts_match_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))

    sequences = {
        shortcut.key().toString()
        for shortcut in window.findChildren(QShortcut)
        if shortcut.key().toString().startswith("Ctrl+")
        and shortcut.key().toString()[5:].isdigit()
    }

    assert sequences == {f"Ctrl+{index}" for index in range(len(NAV_ITEMS))}
    window.close()
    assert app is not None


def test_open_page_switches_to_library(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    window._load_tuf_meta = lambda: None

    window.open_page("library", "local")

    assert window.stack.currentWidget() is window.library
    assert window.page_title.text() == "在线 TUF / 本地谱面"
    assert window.library.stack.currentIndex() == 1
    window.close()
    assert app is not None


def test_open_page_unknown_key_falls_back_to_home(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    window._load_tuf_meta = lambda: None
    window.open_page("library", "local")

    window.open_page("no_such_page")

    assert window.stack.currentWidget() is window.home
    message = capsys.readouterr().err
    assert "no_such_page" in message
    assert "workspace" in message and "settings" in message
    window.close()
    assert app is not None


def test_open_page_without_argument_keeps_current_page(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    window._load_tuf_meta = lambda: None
    window.open_page("settings")

    window.open_page("")

    assert window.stack.currentWidget() is window.settings_page
    window.close()
    assert app is not None


def test_open_page_unknown_key_writes_log_warning(tmp_path, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    window._load_tuf_meta = lambda: None

    with caplog.at_level(logging.WARNING, logger="adofai_publisher.ui.main_window"):
        window.open_page("bogus")

    assert "未知页面" in caplog.text
    assert "bogus" in caplog.text
    window.close()
    assert app is not None


def test_open_page_library_without_tab_keeps_default_tab(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    window._load_tuf_meta = lambda: None

    window.open_page("library")

    assert window.stack.currentWidget() is window.library
    assert window.library.stack.currentIndex() == 0
    window.close()
    assert app is not None


def test_umm_page_has_two_tabs(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.ui.umm_page import UmmPage

    page = UmmPage(Settings(tmp_path / "settings.json"))
    assert page.stack.count() == 2
    assert page.stack.currentIndex() == 0
    page.show_tab(1)
    assert page.stack.currentIndex() == 1
    assert page.tools_tab_button.isChecked()
    assert not page.umm_tab_button.isChecked()
    page.show_tab(0)
    assert page.stack.currentIndex() == 0
    page.close()
    page.deleteLater()
    app.processEvents()
    assert app is not None


def test_umm_page_launch_game_button_emits(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.ui.umm_page import UmmPage

    page = UmmPage(Settings(tmp_path / "settings.json"))
    captured: list[bool] = []
    page.launchRequested.connect(lambda: captured.append(True))
    assert page.launch_button.text() == "启动游戏"
    page.launch_button.click()
    assert captured == [True]
    page.close()
    page.deleteLater()
    app.processEvents()


def test_umm_page_shows_bridge_status(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = tmp_path / "game"
    game.mkdir()
    mod_dir = game / "Mods" / "ADOFAIGOBridge"
    mod_dir.mkdir(parents=True)
    from adofai_publisher import game_bridge

    (mod_dir / "Info.json").write_text(
        '{"Id":"ADOFAIGOBridge","Version":"%s"}' % game_bridge.BRIDGE_VERSION,
        encoding="utf-8",
    )
    (mod_dir / "ADOFAIGOBridge.dll").write_bytes(b"dll")
    (mod_dir / "heartbeat.txt").write_text(
        f"version={game_bridge.BRIDGE_VERSION}\nstate=idle\ntime={int(time.time())}\n",
        encoding="utf-8",
    )
    (mod_dir / "bridge.log").write_text("loaded\n", encoding="utf-8")
    monkeypatch.setattr(
        "adofai_publisher.game_bridge.is_game_running",
        lambda: True,
    )
    app = QApplication.instance() or QApplication([])
    settings = Settings(tmp_path / "settings.json")
    settings.set("umm_game_dir", str(game))
    from adofai_publisher.ui.umm_page import UmmPage

    page = UmmPage(settings)
    page.refresh()

    assert "在线" in page.bridge_status.text()
    assert game_bridge.BRIDGE_VERSION in page.bridge_version.text()
    assert page.bridge_log_button.isEnabled()
    assert page.bridge_install_button.isHidden()
    assert not page.bridge_repair_button.isHidden()
    assert page.hotkey_value.text() == "Alt+X"
    page.close()
    page.deleteLater()
    app.processEvents()


def test_umm_page_saves_hotkey_and_install_preference(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    settings = Settings(tmp_path / "settings.json")
    from adofai_publisher.ui.umm_page import UmmPage

    page = UmmPage(settings)
    page._finish_hotkey_record("Ctrl+Shift+G")
    page.hotkey_enabled.setChecked(False)
    page.bridge_auto_install.setChecked(True)

    assert settings.get("game_hotkey") == "Ctrl+Shift+G"
    assert settings.get("game_hotkey_enabled") is False
    assert settings.get("bridge_auto_install") is True
    assert page.hotkey_value.text() == "Ctrl+Shift+G"
    page.close()
    page.deleteLater()
    app.processEvents()


def test_umm_page_install_requests_worker_without_calling_backend(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    settings = Settings(tmp_path / "settings.json")
    (tmp_path / "game").mkdir()
    settings.set("umm_game_dir", str(tmp_path / "game"))
    # 已有安装器：一键安装直接派发安装动作，不进入下载流程。
    monkeypatch.setattr(
        "adofai_publisher.ui.umm_page.umm_installer_source",
        lambda *_args, **_kwargs: tmp_path / "installer",
    )
    monkeypatch.setattr(
        "adofai_publisher.ui.umm_page.umm_installer_needs_download",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes),
    )
    backend_calls: list[str] = []
    for name in ("install_umm", "uninstall_umm"):
        monkeypatch.setattr(
            f"adofai_publisher.umm.{name}",
            lambda *a, _name=name, **k: backend_calls.append(_name),
        )
        monkeypatch.setattr(
            f"adofai_publisher.ui.umm_page.{name}",
            lambda *a, _name=name, **k: backend_calls.append(_name),
            raising=False,
        )
    from adofai_publisher.ui.umm_page import UmmPage

    page = UmmPage(settings)
    requested: list[tuple[str, dict]] = []
    page.actionRequested.connect(lambda action, payload: requested.append((action, payload)))

    page.install_umm_button.click()

    assert backend_calls == []
    assert requested == [("install_umm", {})]
    assert page._action_busy is True
    assert not page.install_umm_button.isEnabled()
    # 安装期间「在线更新 UMM」一并禁用，避免重复下载。
    assert not page.update_button.isEnabled()
    assert not page.uninstall_umm_button.isEnabled()
    assert not page.local_install_button.isEnabled()
    assert not page.action_status.isHidden()
    assert "正在安装 UMM" in page.action_status.text()

    page.set_action_busy(False)
    assert page.install_umm_button.isEnabled()
    assert page.update_button.isEnabled()
    assert page.uninstall_umm_button.isEnabled()
    assert page.local_install_button.isEnabled()
    assert page.action_status.isHidden()

    page.close()
    page.deleteLater()
    app.processEvents()


def test_umm_page_install_without_installer_downloads_then_installs(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    game = tmp_path / "game"
    game.mkdir()
    settings = Settings(tmp_path / "settings.json")
    settings.set("umm_game_dir", str(game))
    monkeypatch.setattr(
        "adofai_publisher.ui.umm_page.umm_installer_source",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "adofai_publisher.ui.umm_page.umm_installer_needs_download",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes),
    )
    from adofai_publisher.ui.umm_page import UmmPage

    page = UmmPage(settings)
    updates: list[str] = []
    actions: list[tuple[str, dict]] = []
    page.updateRequested.connect(updates.append)
    page.actionRequested.connect(
        lambda action, payload: actions.append((action, payload))
    )

    page.install_umm_button.click()

    # 没有安装器：先下载，不直接派发安装。
    assert actions == []
    assert updates == [str(page.mirror_combo.currentData())]
    assert page._install_after_download is True
    assert not page.install_umm_button.isEnabled()
    assert not page.update_button.isEnabled()
    assert not page.cancel_update_button.isHidden()
    assert "正在下载 UMM 安装包" in page.update_status.text()

    page.set_update_progress("正在下载 UMM v1.0.0…", 40)
    assert page.update_status.text().startswith("下载 UMM 安装包：")

    # 下载完成后自动接着安装，且安装期间仍互斥禁用更新按钮。
    page.set_update_done("1.0.0", str(tmp_path / "installer"))
    assert actions == [("install_umm", {})]
    assert page._install_after_download is False
    assert not page.update_button.isEnabled()
    assert page._action_busy is True

    page.set_action_busy(False)
    assert page.update_button.isEnabled()
    assert page.install_umm_button.isEnabled()

    page.close()
    page.deleteLater()
    app.processEvents()


def test_umm_page_install_download_failure_falls_back_to_thunderstore(
    tmp_path, monkeypatch
):
    """官方源预下载失败时不终止，改用安装动作自动回退 Thunderstore。"""
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    game = tmp_path / "game"
    game.mkdir()
    settings = Settings(tmp_path / "settings.json")
    settings.set("umm_game_dir", str(game))
    monkeypatch.setattr(
        "adofai_publisher.ui.umm_page.umm_installer_source",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "adofai_publisher.ui.umm_page.umm_installer_needs_download",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes),
    )
    from adofai_publisher.ui.umm_page import UmmPage

    page = UmmPage(settings)
    messages: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        page, "_show_message", lambda title, text, level: messages.append((title, text, level))
    )
    actions: list[tuple[str, dict]] = []
    page.actionRequested.connect(
        lambda action, payload: actions.append((action, payload))
    )

    page.install_umm_button.click()
    assert actions == []
    page.set_update_failed("无法读取官方版本信息：网络不可达")

    # 不弹「下载失败」死路，而是接着执行安装（安装里会走两级回退下载）。
    assert messages == []
    assert actions == [("install_umm", {})]
    assert page._install_after_download is False
    assert "Thunderstore" in page.update_status.text()
    assert not page.action_status.isHidden()
    assert "正在安装 UMM" in page.action_status.text()
    assert not page.install_umm_button.isEnabled()
    assert not page.update_button.isEnabled()
    assert page.cancel_update_button.isHidden()

    page.set_action_busy(False)
    assert page.install_umm_button.isEnabled()
    assert page.update_button.isEnabled()

    page.close()
    page.deleteLater()
    app.processEvents()


def test_umm_page_shows_assembly_injection_as_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    game = tmp_path / "game"
    game.mkdir()
    settings = Settings(tmp_path / "settings.json")
    settings.set("umm_game_dir", str(game))
    entry = str(
        game / "A Dance of Fire and Ice_Data" / "Managed" / "Assembly-CSharp.dll"
    )
    monkeypatch.setattr(
        "adofai_publisher.ui.umm_page.get_umm_status",
        lambda *_args, **_kwargs: {
            "game_dir": str(game),
            "installed": True,
            "injected": True,
            "doorstop_injected": False,
            "assembly_injected": True,
            "manager_installed": True,
            "mode": "assembly",
            "entry_assembly": entry,
            "state": "installed",
            "message": "UMM 已安装，Assembly 注入完成",
        },
    )
    from adofai_publisher.ui.umm_page import UmmPage

    page = UmmPage(settings)
    page.refresh()

    # Assembly 注入在状态 chip 上也显示为「已安装」，注入方式放在提示里。
    assert page.manager_status.text().startswith("已安装")
    assert "Assembly 注入完成" in page.manager_status.toolTip()
    page.close()
    page.deleteLater()
    app.processEvents()


def test_umm_page_uninstall_confirm_covers_assembly_restore(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    game = tmp_path / "game"
    game.mkdir()
    settings = Settings(tmp_path / "settings.json")
    settings.set("umm_game_dir", str(game))
    prompts: list[str] = []

    def fake_question(_parent, _title, text, *args, **kwargs):
        prompts.append(str(text))
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))
    monkeypatch.setattr(
        "adofai_publisher.ui.umm_page.get_umm_status",
        lambda *_args, **_kwargs: {
            "game_dir": str(game),
            "installed": False,
            "injected": False,
            "doorstop_injected": False,
            "assembly_injected": False,
            "manager_installed": False,
            "mode": None,
            "entry_assembly": "",
            "state": "none",
            "message": "未安装 UMM",
        },
    )
    from adofai_publisher.ui.umm_page import UmmPage

    page = UmmPage(settings)
    page.refresh()
    page.uninstall_umm_button.click()

    assert prompts
    assert ".original_" in prompts[0]
    assert "Assembly" in prompts[0]
    page.close()
    page.deleteLater()
    app.processEvents()


def test_umm_page_mod_toggle_emits_payload_and_disables_row(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    settings = Settings(tmp_path / "settings.json")
    from adofai_publisher.ui.umm_page import UmmPage

    page = UmmPage(settings)
    page._clear_grid()
    page._add_mod_row(
        0,
        {
            "id": "AdofaiTweaks",
            "display_name": "AdofaiTweaks",
            "installed": True,
            "enabled": True,
            "archives": [],
        },
    )
    requested: list[tuple[str, dict]] = []
    page.actionRequested.connect(lambda action, payload: requested.append((action, payload)))

    toggle = page._rows["AdofaiTweaks"]["buttons"][0]
    toggle.click()

    assert requested == [
        (
            "set_mod_enabled",
            {
                "mod_id": "AdofaiTweaks",
                "enabled": False,
                "display_name": "AdofaiTweaks",
            },
        )
    ]
    assert page._action_busy is True
    assert not toggle.isEnabled()

    page.set_action_busy(False)
    assert toggle.isEnabled()
    page.close()
    page.deleteLater()
    app.processEvents()


def test_umm_page_mods_card_mentions_params_and_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    settings = Settings(tmp_path / "settings.json")
    from adofai_publisher.ui.umm_page import UmmPage

    page = UmmPage(settings)
    hints = [
        label.text()
        for label in page.findChildren(QLabel)
        if label.objectName() == "CardHint"
    ]

    assert any("Params.xml" in text and "重启游戏" in text for text in hints)
    page.close()
    page.deleteLater()
    app.processEvents()


def test_main_window_runs_umm_action_in_worker_and_restores_buttons(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    captured = {}

    def fake_start(worker, **kwargs):
        captured["worker"] = worker
        captured["kwargs"] = kwargs

    monkeypatch.setattr(window, "_start_worker", fake_start)

    def fake_refresh() -> None:
        captured["refresh"] = captured.get("refresh", 0) + 1

    monkeypatch.setattr(window.umm, "refresh", fake_refresh)
    monkeypatch.setattr(
        window.umm,
        "show_action_result",
        lambda action, payload, result: captured.setdefault(
            "result", (action, payload, result)
        ),
    )

    window.umm.actionRequested.emit(
        "install_mod", {"mod_id": "AdofaiTweaks", "archive_path": None}
    )

    worker = captured["worker"]
    assert worker.action == "install_mod"
    assert worker.payload == {"mod_id": "AdofaiTweaks", "archive_path": None}
    assert window._umm_action == "install_mod"
    assert window.umm._action_busy is True
    assert not window.umm.install_umm_button.isEnabled()

    # 同一时间只允许一个模组操作
    window.umm.actionRequested.emit("uninstall_mod", {"mod_id": "AdofaiTweaks"})
    assert captured["worker"] is worker
    assert window._umm_action == "install_mod"

    captured["kwargs"]["on_finished"]("install_mod", {"display_name": "AdofaiTweaks"})

    assert window._umm_action == ""
    assert window._umm_action_worker is None
    assert window.umm._action_busy is False
    assert window.umm.install_umm_button.isEnabled()
    assert captured["refresh"] == 1
    assert captured["result"] == (
        "install_mod",
        {"mod_id": "AdofaiTweaks", "archive_path": None},
        {"display_name": "AdofaiTweaks"},
    )
    window.close()
    app.processEvents()


def test_main_window_umm_action_failure_restores_buttons(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    captured = {}

    monkeypatch.setattr(window, "_start_worker", lambda worker, **kwargs: captured.update(kwargs))
    monkeypatch.setattr(window.umm, "refresh", lambda: None)
    monkeypatch.setattr(
        window.umm,
        "show_action_error",
        lambda action, message: captured.setdefault("error", (action, message)),
    )

    window.umm.actionRequested.emit("uninstall_umm", {})
    captured["on_failed"]("删除 UnityModManager.exe 失败：权限不足")

    assert window._umm_action == ""
    assert window.umm._action_busy is False
    assert window.umm.install_umm_button.isEnabled()
    assert captured["error"] == ("uninstall_umm", "删除 UnityModManager.exe 失败：权限不足")
    window.close()
    app.processEvents()


def test_main_window_umm_action_runs_off_ui_thread_and_recovers(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    main_thread = QThread.currentThread()
    seen: list[bool] = []

    def fake_install(_settings, mod_id: str, archive=None):
        seen.append(QThread.currentThread() is main_thread)
        return {"display_name": mod_id}

    monkeypatch.setattr("adofai_publisher.workers.install_mod", fake_install)
    monkeypatch.setattr(window.umm, "refresh", lambda: None)
    results: list[tuple[str, object]] = []
    monkeypatch.setattr(
        window.umm,
        "show_action_result",
        lambda action, payload, result: results.append((action, result)),
    )

    window.umm.actionRequested.emit("install_mod", {"mod_id": "AdofaiTweaks"})
    assert window.umm._action_busy is True
    assert not window.umm.install_umm_button.isEnabled()

    worker = window._umm_action_worker
    thread = next(t for t, w in window._jobs.items() if w is worker)
    _wait_for_thread(thread)
    app.processEvents()

    assert seen == [False]
    assert window.umm._action_busy is False
    assert window.umm.install_umm_button.isEnabled()
    assert results == [("install_mod", {"display_name": "AdofaiTweaks"})]
    window.close()
    app.processEvents()


def test_auto_copy_refreshes_when_judgements_change(tmp_path):
    app = QApplication.instance() or QApplication([])
    page = WorkspacePage(Settings(tmp_path / "settings.json"))
    page.bind_level(
        LevelInfo(tuf_id=7, song="Song", artist="Artist", charter="Charter", difficulty="G12")
    )
    assert "0.00%" in page.title_input.text()

    page.judgement_grid.set_values({"perfect": 100})
    page._on_judgements_changed()
    assert "100.00%" in page.title_input.text()
    assert "100.00%" in page.desc_input.toPlainText()

    page.title_input.setText("我自己的标题")
    page.judgement_grid.set_values({"perfect": 50, "ePerfect": 50})
    page._on_judgements_changed()
    assert page.title_input.text() == "我自己的标题"
    page.close()
    page.deleteLater()
    app.processEvents()
    assert app is not None


def test_workspace_publish_mode_switches_and_persists(tmp_path):
    app = QApplication.instance() or QApplication([])

    settings = Settings(tmp_path / "settings.json")
    page = WorkspacePage(settings)
    assert page._publish_mode() == "dual"
    assert page.mode_dual_button.isChecked()
    assert page.publish_panel.upload_button.text() == "上传到 B 站"
    assert not page.publish_panel.tuf_button.isHidden()

    page.mode_bili_button.click()
    assert page._publish_mode() == "bili_only"
    assert settings.get("publish_mode") == "bili_only"
    assert Settings(tmp_path / "settings.json").get("publish_mode") == "bili_only"
    assert page.mode_bili_button.isChecked()
    assert page.publish_panel.upload_button.text() == "仅上传到 B 站"
    assert page.publish_panel.tuf_button.isHidden()
    assert page.publish_panel.tuf_status.text() == "TUF：本次跳过"

    page.mode_dual_button.click()
    assert page._publish_mode() == "dual"
    assert settings.get("publish_mode") == "dual"
    assert Settings(tmp_path / "settings.json").get("publish_mode") == "dual"
    assert page.publish_panel.upload_button.text() == "上传到 B 站"
    assert not page.publish_panel.tuf_button.isHidden()
    page.close()
    page.deleteLater()
    app.processEvents()


def test_workspace_restores_saved_publish_mode(tmp_path):
    app = QApplication.instance() or QApplication([])

    path = tmp_path / "settings.json"
    settings = Settings(path)
    settings.set("publish_mode", "bili_only")
    settings.save()

    page = WorkspacePage(Settings(path))
    assert page._publish_mode() == "bili_only"
    assert page.mode_bili_button.isChecked()
    assert page.publish_panel.upload_button.text() == "仅上传到 B 站"
    assert page.publish_panel.tuf_button.isHidden()
    page.close()
    page.deleteLater()
    app.processEvents()


def test_workspace_step_guard_respects_publish_mode(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    page = WorkspacePage(Settings(tmp_path / "settings.json"))
    messages: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda *args, **kwargs: messages.append(str(args[2])),
    )

    page.go_step(3)
    assert page.current_step == 0

    page.go_step(2)
    page.next_step()
    assert page.current_step == 2
    assert any("TUF" in message for message in messages)

    page.go_step(0)
    page.mode_bili_button.click()
    page.go_step(2)
    page.next_step()
    assert page.current_step == 3
    assert not page.step_four.isHidden()
    assert not page.copy_preview.isHidden()
    assert page.step_five.isHidden()

    page.next_step()
    assert page.current_step == 4
    assert not page.step_five.isHidden()
    assert page.step_four.isHidden()
    assert not page.publish_panel.upload_button.isHidden()
    assert not page.publish_panel.bv_input.isHidden()
    assert not page.publish_panel.log_view.isHidden()
    page.close()
    page.deleteLater()
    app.processEvents()


def test_workspace_open_step_argument_accepts_number_and_alias(tmp_path):
    app = QApplication.instance() or QApplication([])
    page = WorkspacePage(Settings(tmp_path / "settings.json"))
    page.show()
    app.processEvents()

    assert page.open_step_argument("4") is True
    assert page.current_step == 3
    assert page.step_four.isVisible()
    assert not page.step_one.isVisible()

    assert page.open_step_argument("1") is True
    assert page.current_step == 0
    assert page.step_one.isVisible()
    assert not page.step_four.isVisible()

    assert page.open_step_argument("文案") is True
    assert page.current_step == 3
    assert page.open_step_argument("copywriting") is True
    assert page.current_step == 3

    assert page.open_step_argument("abc") is False
    assert page.current_step == 3
    assert page.step_four.isVisible()
    page.close()
    page.deleteLater()
    app.processEvents()


def test_main_window_open_page_workspace_step_argument(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))

    assert window.workspace.current_step == 0
    window.open_page("workspace")
    assert window.stack.currentWidget() is window.workspace
    assert window.workspace.current_step == 0

    window.open_page("workspace", "4")
    assert window.stack.currentWidget() is window.workspace
    assert window.workspace.current_step == 3
    assert not window.workspace.step_four.isHidden()
    assert window.workspace.step_one.isHidden()
    window.close()
    app.processEvents()


def test_workspace_step_bar_has_five_steps_and_keyboard_navigates(tmp_path):
    app = QApplication.instance() or QApplication([])
    page = WorkspacePage(Settings(tmp_path / "settings.json"))
    page.mode_bili_button.click()

    assert len(page.step_bar.steps) == 5
    for _ in range(6):
        QTest.keyClick(page.step_bar, Qt.Key.Key_Right)
    assert page.current_step == 4
    for _ in range(6):
        QTest.keyClick(page.step_bar, Qt.Key.Key_Left)
    assert page.current_step == 0
    page.close()
    page.deleteLater()
    app.processEvents()


def test_workspace_step_five_holds_check_card_and_publish_panel(tmp_path):
    app = QApplication.instance() or QApplication([])
    page = WorkspacePage(Settings(tmp_path / "settings.json"))

    assert page.issue_list.parent() is not None
    assert page.step_five.isAncestorOf(page.issue_list)
    assert page.step_five.isAncestorOf(page.publish_panel)
    assert not page.step_four.isAncestorOf(page.publish_panel)
    page.close()
    page.deleteLater()
    app.processEvents()


def test_workspace_step_one_has_side_entries(tmp_path):
    app = QApplication.instance() or QApplication([])
    page = WorkspacePage(Settings(tmp_path / "settings.json"))

    assert page.resume_draft_button.text() == "继续上次草稿"
    assert page.recent_entries_button.text() == "最近投稿"
    page.close()
    page.deleteLater()
    app.processEvents()


def test_workspace_step_two_dropped_the_flow_card(tmp_path):
    app = QApplication.instance() or QApplication([])
    page = WorkspacePage(Settings(tmp_path / "settings.json"))
    labels = page.step_two.findChildren(QLabel)

    assert not any("一条龙流程" in label.text() for label in labels)
    assert not any(label.objectName() == "FlowNumber" for label in labels)
    page.close()
    page.deleteLater()
    app.processEvents()


def test_workspace_step_four_embeds_copy_preview(tmp_path):
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.ui.pages import CopyPreview

    page = WorkspacePage(Settings(tmp_path / "settings.json"))
    assert isinstance(page.copy_preview, CopyPreview)
    assert page.step_four.isAncestorOf(page.copy_preview)

    page._show_step(3)
    page.title_input.setText("【冰与火之舞】Beneath the Vortex")
    page.desc_input.setPlainText("谱面信息与成绩")
    page.tags_input.setText("冰与火之舞, ADOFAI")

    assert page.copy_preview.preview_title.text() == "【冰与火之舞】Beneath the Vortex"
    assert page.copy_preview.preview_desc.text() == "谱面信息与成绩"
    page.close()
    page.deleteLater()
    app.processEvents()


def test_preview_page_and_workspace_share_copy_preview(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.ui.pages import CopyPreview

    window = MainWindow(Settings(tmp_path / "settings.json"))
    assert isinstance(window.preview.copy_preview, CopyPreview)
    assert isinstance(window.workspace.copy_preview, CopyPreview)

    window.draft.title = "两处共用的标题"
    window.preview.refresh(window.draft)
    assert window.preview.copy_preview.preview_title.text() == "两处共用的标题"
    window.close()
    app.processEvents()


def _visible_issue_texts(page) -> list[str]:
    """只取检查列表当前布局里的文字，避开 deleteLater 尚未销毁的旧标签。"""

    texts: list[str] = []
    box = page.issue_list.layout_box
    for index in range(box.count()):
        item = box.itemAt(index)
        row = item.layout()
        labels = (
            [row.itemAt(i).widget() for i in range(row.count())]
            if row is not None
            else [item.widget()]
        )
        texts.extend(label.text() for label in labels if isinstance(label, QLabel))
    return texts


def test_workspace_flags_missing_feeling_rating_only_in_dual_mode(tmp_path):
    app = QApplication.instance() or QApplication([])
    page = WorkspacePage(Settings(tmp_path / "settings.json"))

    assert any("体感难度" in text for text in _visible_issue_texts(page))

    page.mode_bili_button.click()
    assert page._publish_mode() == "bili_only"
    assert not any("体感难度" in text for text in _visible_issue_texts(page))
    page.close()
    page.deleteLater()
    app.processEvents()


def test_ctrl_j_from_other_page_opens_publish_step(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    window._navigate("home")

    window._shortcut_publish()

    assert window.stack.currentWidget() is window.workspace
    assert window.workspace.current_step == 4
    window.close()
    app.processEvents()


def test_workspace_resume_button_restores_saved_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.models import Draft

    window = MainWindow(Settings(tmp_path / "settings.json"))
    window._navigate("home")
    monkeypatch.setattr(
        "adofai_publisher.ui.main_window.load_draft",
        lambda: Draft(title="上次没写完的标题"),
    )

    window.workspace.resume_draft_button.click()

    assert window.workspace.title_input.text() == "上次没写完的标题"
    assert window.stack.currentWidget() is window.workspace
    window.close()
    app.processEvents()


def test_bili_only_upload_validation_skips_tuf(tmp_path):
    app = QApplication.instance() or QApplication([])
    video = tmp_path / "clear.mp4"
    video.write_bytes(b"x")
    settings = Settings(tmp_path / "settings.json")
    settings.set("publish_mode", "bili_only")
    settings.save()
    window = MainWindow(settings)
    window.draft.video_path = str(video)
    window.draft.level = None
    window.draft.title = "仅 B 站投稿测试"
    window.draft.tags = ["冰与火之舞"]
    window.draft.description = "测试简介"

    assert window._validate() == []
    window.close()
    app.processEvents()


def test_dual_upload_validation_still_requires_tuf(tmp_path):
    app = QApplication.instance() or QApplication([])
    video = tmp_path / "clear.mp4"
    video.write_bytes(b"x")
    settings = Settings(tmp_path / "settings.json")
    window = MainWindow(settings)
    window.draft.video_path = str(video)
    window.draft.level = None
    window.draft.title = "双投投稿测试"
    window.draft.tags = ["冰与火之舞"]
    window.draft.description = "测试简介"

    errors = window._validate()
    assert any("TUF 谱面" in error for error in errors)
    assert any("体感难度" in error for error in errors)
    window.close()
    app.processEvents()


def test_preview_publish_jumps_to_workspace_last_step(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))

    window.preview.publish_button.click()
    app.processEvents()

    assert window.stack.currentWidget() is window.workspace
    assert window.workspace.current_step == 4
    window.close()
    app.processEvents()


def test_ctrl_j_submits_tuf_from_workspace_publish_step(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    window._navigate("workspace")
    window.workspace._show_step(4)
    window.draft.bvid = "BV1xx411c7mD"
    submitted: list[bool] = []
    monkeypatch.setattr(window, "_start_tuf", lambda: submitted.append(True))

    window._shortcut_publish()

    assert submitted == [True]
    window.close()
    app.processEvents()


def _set_tuf_draft(window) -> None:
    """TUF 提交用例共用的草稿（pass 71967 那组数据）。

    ``_start_tuf`` 会先从工作区界面 collect 草稿，所以数据要经 ``load_draft``
    灌进界面，而不是直接改 ``window.draft``。
    """
    window.workspace.load_draft(
        Draft(
            level=LevelInfo(
                tuf_id=5756, song="Vanilla Flavored Soda", midspin_count=48
            ),
            bvid="BV1xx411c7mD",
            feeling_rating="G12",
            x_perfect=True,
            judgements={
                "perfectMinus": 345,
                "perfect": 1230,
                "perfectPlus": 91,
                "ePerfect": 24,
                "lPerfect": 5,
                "earlyDouble": 2,
                "earlySingle": 8,
                "lateSingle": 1,
                "lateDouble": 0,
            },
        )
    )


def test_start_tuf_confirmation_shows_judgement_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    _set_tuf_draft(window)
    texts: list[str] = []

    def decline(parent, title, text, *args, **kwargs):
        texts.append(text)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", staticmethod(decline))
    submitted: list[object] = []
    monkeypatch.setattr(
        window, "_start_worker", lambda worker, **kwargs: submitted.append(worker)
    )

    window._start_tuf()

    assert len(texts) == 1
    assert "将向 TUF 提交：Vanilla Flavored Soda（ID 5756）" in texts[0]
    assert "准确率" in texts[0]
    # 摘要与提交日志同一份格式：3.4.0 默认不扣中旋（midspin 48 也不扣）。
    assert (
        "TUF 判定：-完美 345 / 完美 1230 / 完美+ 91 / 稍快 24 / 稍慢 5 / "
        "提前 2 / 太快 8 / 太慢 1；游戏版本 3.4.0，不扣中旋；X 完美模式开"
    ) in texts[0]
    # 点「否」不派发 worker，也就不会发出任何网络请求。
    assert submitted == []
    assert "正在提交 TUF 记录" not in window.publish_panel.log_view.toPlainText()
    window.close()
    app.processEvents()


def test_start_tuf_confirmation_dispatches_worker_when_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    _set_tuf_draft(window)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes),
    )
    captured: list[object] = []
    monkeypatch.setattr(
        window, "_start_worker", lambda worker, **kwargs: captured.append(worker)
    )

    window._start_tuf()

    assert len(captured) == 1
    worker = captured[0]
    assert worker.level.tuf_id == 5756
    # 中旋扣减仍只发生在 submit_pass 里，worker 拿到的是原始判定。
    assert worker.judgements["perfect"] == 1230
    assert "正在提交 TUF 记录" in window.publish_panel.log_view.toPlainText()
    window.close()
    app.processEvents()


def test_chart_library_local_sort_slider_and_random():
    app = QApplication.instance() or QApplication([])
    page = ChartLibraryPage()
    rows = []
    for tuf_id, song, difficulty in (
        (1, "Beta", "P10"),
        (2, "Alpha", "G1"),
        (3, "Gamma", "U7"),
        (4, "Delta", "Unranked"),
    ):
        row = LevelInfo(
            tuf_id=tuf_id,
            song=song,
            charter="Charter",
            difficulty=difficulty,
        ).to_dict()
        row.update({"key": f"tuf:{tuf_id}", "path": f"C:/levels/{tuf_id}.ard"})
        rows.append(row)
    page.set_local_entries(rows)
    page.show_tab("local")

    page.local_sort_combo.setCurrentIndex(1)
    page.local_order_button.setText("↑ 升序")
    page._apply_local_filter()
    assert [item.level.song for item in page.local_model.items()] == [
        "Alpha",
        "Beta",
        "Delta",
        "Gamma",
    ]

    page.local_sort_combo.setCurrentIndex(2)
    page.local_order_button.setText("↑ 升序")
    page._apply_local_filter()
    assert [item.level.song for item in page.local_model.items()] == [
        "Beta",
        "Alpha",
        "Gamma",
        "Delta",
    ]

    page.local_order_button.setText("↓ 降序")
    page._apply_local_filter()
    assert [item.level.song for item in page.local_model.items()] == [
        "Gamma",
        "Alpha",
        "Beta",
        "Delta",
    ]

    page.local_difficulty_slider.set_range(1, 20)
    assert [item.level.song for item in page.local_model.items()] == ["Beta"]
    page.local_difficulty_slider.set_range(41, 60)
    assert [item.level.song for item in page.local_model.items()] == ["Gamma"]

    page._pick_random()
    assert page.local_model.items()[0].level.song == "Gamma"
    assert "随机抽到：Gamma" in page.local_status.text()

    page._reset_local_filters()
    assert len(page.local_model.items()) == 4
    assert page.local_difficulty_slider.low == 1
    assert page.local_difficulty_slider.high == 60


def test_chart_detail_shows_parse_error_banner():
    app = QApplication.instance() or QApplication([])
    entry = LevelInfo(song="test").to_dict()
    entry.update(
        {
            "key": "folder:c:/levels/test",
            "path": "C:/levels/test/level.adofai",
            "folder": "C:/levels/test",
            "source": "scan",
            "parse_error": "谱面 JSON 解析失败（第 3 行第 1 列：Expecting ',' delimiter）",
        }
    )
    item = ChartItem(
        level=LevelInfo.from_dict(entry),
        source="local",
        entry=entry,
        downloaded=True,
        matched=False,
    )
    dialog = ChartDetailDialog(item, {}, [], None)

    banners = [
        label
        for label in dialog.findChildren(QLabel)
        if label.objectName() == "ParseErrorBanner"
    ]
    assert banners
    assert "解析失败" in banners[0].text()
    assert "Expecting ',' delimiter" in banners[0].text()
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_chart_item_parse_error_is_empty_for_healthy_and_tuf_items():
    healthy = ChartItem(level=LevelInfo(song="Song"), source="local", entry={"key": "a"})
    broken = ChartItem(
        level=LevelInfo(song="Song"),
        source="local",
        entry={"key": "b", "parse_error": "坏文件"},
    )
    online = ChartItem(level=LevelInfo(tuf_id=1, song="Song"), source="tuf")

    assert healthy.parse_error == ""
    assert broken.parse_error == "坏文件"
    assert online.parse_error == ""


def test_chart_detail_bind_section_emits_selected_level():
    app = QApplication.instance() or QApplication([])
    entry = LevelInfo(song="Local Song", difficulty="P10").to_dict()
    entry.update(
        {
            "key": "folder:c:/levels/local",
            "path": "C:/levels/local/chart.adofai",
            "source": "scan",
        }
    )
    item = ChartItem(
        level=LevelInfo.from_dict(entry),
        source="local",
        entry=entry,
        downloaded=True,
        matched=False,
    )
    dialog = ChartDetailDialog(item, {}, [], None)
    captured: list[LevelInfo] = []
    dialog.bindRequested.connect(captured.append)

    dialog.set_bind_results(
        [
            LevelInfo(tuf_id=1, song="First", difficulty="P10"),
            LevelInfo(tuf_id=2, song="Second", difficulty="P11"),
        ]
    )
    dialog.bind_results.setCurrentRow(1)
    dialog._emit_bind()

    assert len(captured) == 1
    assert captured[0].tuf_id == 2
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_chart_detail_game_open_button_emits_local_item(tmp_path):
    app = QApplication.instance() or QApplication([])
    folder = tmp_path / "levels" / "Song"
    folder.mkdir(parents=True)
    chart = folder / "level.adofai"
    chart.write_text("{}", encoding="utf-8")
    entry = LevelInfo(tuf_id=88, song="Worlds Song", difficulty="P10").to_dict()
    entry.update(
        {
            "key": f"folder:{folder}".lower(),
            "path": str(chart),
            "folder": str(folder),
        }
    )
    item = ChartItem(
        level=LevelInfo.from_dict(entry),
        source="local",
        entry=entry,
        downloaded=True,
        matched=True,
    )
    dialog = ChartDetailDialog(item, {}, [], None)
    captured: list[ChartItem] = []
    dialog.gameOpenRequested.connect(captured.append)

    assert dialog.game_open_button.text() == "在游戏中打开"
    assert dialog.game_open_button.objectName() == "PrimaryButton"
    dialog.game_open_button.click()

    assert captured == [item]
    hints = [
        label
        for label in dialog.findChildren(QLabel)
        if "ADOFAI GO Bridge" in label.text()
    ]
    assert hints and hints[0].objectName() == "Faint"
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_main_window_open_chart_in_game_uses_bridge_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    # Bridge 状态来自本机游戏目录的探测结果：没装 Bridge 时会弹模态框询问
    # 是否安装，offscreen 下 exec() 会一直挡住测试。这里固定成「已安装且版本
    # 可用」，让用例只关心「是否把 GameOpenWorker 交给了工作线程」。
    monkeypatch.setattr(
        "adofai_publisher.ui.main_window.get_bridge_status",
        lambda _settings: {
            "game_dir": str(tmp_path / "game"),
            "installed": True,
            "version_ok": True,
        },
    )
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    folder = tmp_path / "levels" / "P" / "P10" / "Song"
    folder.mkdir(parents=True)
    chart = folder / "level.adofai"
    chart.write_text("{}", encoding="utf-8")
    entry = window.library_db.upsert(
        LevelInfo(tuf_id=501, song="Song", charter="C", difficulty="P10"),
        chart,
        {"folder": str(folder), "category": "P", "band": "P10"},
    )
    item = ChartItem(
        level=LevelInfo.from_dict(entry),
        source="local",
        entry=entry,
        downloaded=True,
        matched=True,
    )
    captured: list[object] = []
    monkeypatch.setattr(
        window,
        "_start_worker",
        lambda worker, **kwargs: captured.append(worker),
    )

    window._open_chart_in_game(item)

    assert len(captured) == 1
    assert Path(captured[0].chart_path) == chart
    window.close()
    app.processEvents()


def test_main_window_open_chart_in_game_rejects_missing_chart(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    folder = tmp_path / "levels" / "Missing"
    entry = {
        "key": f"folder:{folder}".lower(),
        "path": str(folder / "level.adofai"),
        "folder": str(folder),
        "song": "Missing",
        "difficulty": "P10",
    }
    item = ChartItem(
        level=LevelInfo.from_dict(entry),
        source="local",
        entry=entry,
        downloaded=True,
        matched=True,
    )
    errors: list[str] = []
    monkeypatch.setattr(
        "adofai_publisher.ui.main_window.QMessageBox.critical",
        staticmethod(lambda parent, title, text: errors.append(text)),
    )

    window._open_chart_in_game(item)

    assert errors and "没有找到可打开的 .adofai 文件" in errors[0]
    window.close()
    app.processEvents()


def test_main_window_open_chart_in_game_rejects_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    archive = tmp_path / "levels" / "Song.ard"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"ard")
    entry = {
        "key": f"file:{archive}".lower(),
        "path": str(archive),
        "folder": str(archive.parent),
        "song": "Song",
        "difficulty": "P10",
    }
    item = ChartItem(
        level=LevelInfo.from_dict(entry),
        source="local",
        entry=entry,
        downloaded=True,
        matched=True,
    )
    errors: list[str] = []
    monkeypatch.setattr(
        "adofai_publisher.ui.main_window.QMessageBox.critical",
        staticmethod(lambda parent, title, text: errors.append(text)),
    )

    window._open_chart_in_game(item)

    assert errors and "压缩包" in errors[0]
    window.close()
    app.processEvents()


def test_organize_history_filters_records_and_detail_items():
    app = QApplication.instance() or QApplication([])

    def record(path: str, song: str, status: str, moved: int, undone: int) -> dict:
        return {
            "path": path,
            "created_at": "2026-01-01T12:00:00",
            "finished_at": "",
            "undo_finished_at": "",
            "status": status,
            "root": "C:/levels",
            "backup_path": "",
            "total": 1,
            "moved": moved,
            "failed": 0,
            "undone": undone,
            "pending": 0,
            "counts": {},
            "can_undo": moved > 0,
            "items": [
                {
                    "key": f"folder:{song.casefold()}",
                    "song": song,
                    "status": "moved" if moved else "undone",
                    "source_folder": f"C:/levels/Incoming/{song}",
                    "target_folder": f"C:/levels/P/P10/{song}",
                    "source_path": f"C:/levels/Incoming/{song}/level.adofai",
                    "target_path": f"C:/levels/P/P10/{song}/level.adofai",
                    "message": "",
                    "error": "",
                    "undo_error": "",
                }
            ],
        }

    records = [
        record("C:/history/organize-1.json", "First Song", "completed", 1, 0),
        record("C:/history/organize-2.json", "Second Song", "completed", 0, 1),
    ]
    dialog = OrganizeHistoryDialog(records, "dark")
    assert dialog.records_table.rowCount() == 2

    dialog.record_search.setText("Second")
    app.processEvents()
    assert dialog.records_table.rowCount() == 1
    assert dialog.detail_table.item(0, 2).text() == "Second Song"

    dialog.record_search.clear()
    dialog.record_status.setCurrentIndex(3)
    app.processEvents()
    assert dialog.records_table.rowCount() == 1
    assert dialog.detail_table.item(0, 2).text() == "Second Song"

    dialog.record_status.setCurrentIndex(0)
    dialog.detail_search.setText("missing")
    app.processEvents()
    assert dialog.detail_table.rowCount() == 0
    assert "显示 0 / 1 项" in dialog.detail_title.text()
    dialog.detail_search.clear()
    app.processEvents()
    assert dialog.detail_table.rowCount() == 1
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_batch_bind_dialog_selects_best_candidate():
    app = QApplication.instance() or QApplication([])
    entries = [
        {
            "key": "folder:a",
            "song": "Beneath the Vortex",
            "difficulty": "P1",
            "path": "C:/levels/a.adofai",
            "bpm": 240.0,
            "tilecount": 1000,
            "midspin_count": 10,
            "auto_tile_count": 5,
            "playable_hits": 985,
            "parse_error": "",
        },
        {
            "key": "folder:b",
            "song": "Unknown Song",
            "difficulty": "G2",
            "path": "C:/levels/b.adofai",
            "bpm": 180.0,
            "tilecount": 800,
            "midspin_count": 0,
            "auto_tile_count": 20,
            "playable_hits": 780,
            "parse_error": "",
        },
    ]
    dialog = BatchBindDialog(entries, "light")
    assert [target["key"] for target in dialog.targets()] == ["folder:a", "folder:b"]

    dialog.set_row_results(
        "folder:a",
        [
            {
                "tuf_id": 1,
                "song": "Beneath the Vortex",
                "difficulty": "P14",
                "bpm": 240,
                "tilecount": 990,
                "midspin_count": 10,
                "auto_tile_count": 5,
                "raw": {
                    "id": 1,
                    "song": "Beneath the Vortex",
                    "bpm": 240,
                    "tilecount": 990,
                    "midspinCount": 10,
                    "autoTileCount": 5,
                },
            },
            {
                "tuf_id": 2,
                "song": "Other",
                "difficulty": "P2",
                "bpm": 200,
                "tilecount": 500,
                "midspin_count": 0,
                "auto_tile_count": 0,
                "raw": {
                    "id": 2,
                    "song": "Other",
                    "bpm": 200,
                    "tilecount": 500,
                    "midspinCount": 0,
                    "autoTileCount": 0,
                },
            },
        ],
    )
    assert dialog.table.cellWidget(0, 3) is None
    assert "#1" in dialog.table.item(0, 3).text()
    assert "数值唯一" in dialog.table.item(0, 4).text()
    bindings = dialog.selected_bindings()
    assert len(bindings) == 1
    assert bindings[0][0] == "folder:a"
    assert bindings[0][1].tuf_id == 1

    dialog.set_row_results(
        "folder:b",
        [
            {
                "tuf_id": 3,
                "song": "Totally Different",
                "difficulty": "G1",
                "bpm": 150,
                "tilecount": 300,
                "midspin_count": 0,
                "auto_tile_count": 0,
                "raw": {
                    "id": 3,
                    "song": "Totally Different",
                    "bpm": 150,
                    "tilecount": 300,
                    "midspinCount": 0,
                    "autoTileCount": 0,
                },
            }
        ],
    )
    assert "仅名称匹配" in dialog.table.item(1, 4).text()
    assert len(dialog.selected_bindings()) == 1

    dialog.mark_results({"folder:a"}, 0)
    assert dialog.table.rowCount() == 1
    assert dialog._entries[0]["key"] == "folder:b"
    assert "已绑定 1 条" in dialog.summary.text()

    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_batch_bind_many_rows_avoids_embedded_widgets():
    app = QApplication.instance() or QApplication([])
    entries = [
        {
            "key": f"folder:{index}",
            "song": f"Song {index}",
            "difficulty": "P10",
            "path": f"C:/levels/{index}.adofai",
            "bpm": 200.0,
            "tilecount": 1000 + index,
            "midspin_count": 0,
            "auto_tile_count": 0,
            "playable_hits": 1000 + index,
            "parse_error": "",
        }
        for index in range(120)
    ]
    dialog = BatchBindDialog(entries, "dark")
    assert all(
        dialog.table.cellWidget(row, 3) is None
        for row in range(dialog.table.rowCount())
    )
    dialog._start_search()
    for index in range(120):
        dialog.set_row_results(
            f"folder:{index}",
            [
                {
                    "tuf_id": index + 1,
                    "song": f"Song {index}",
                    "difficulty": "P10",
                    "bpm": 200,
                    "tilecount": 1000 + index,
                    "midspin_count": 0,
                    "auto_tile_count": 0,
                    "raw": {
                        "id": index + 1,
                        "song": f"Song {index}",
                        "bpm": 200,
                        "tilecount": 1000 + index,
                        "midspinCount": 0,
                        "autoTileCount": 0,
                    },
                }
            ],
        )
    assert dialog.progress.value() == 120
    assert len(dialog.selected_bindings()) == 120
    assert all(
        dialog.table.cellWidget(row, 3) is None
        for row in range(dialog.table.rowCount())
    )
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_batch_bind_removes_non_contiguous_rows_correctly():
    app = QApplication.instance() or QApplication([])
    entries = [
        {
            "key": f"folder:{index}",
            "song": f"Song {index}",
            "difficulty": "P10",
            "path": f"C:/levels/{index}.adofai",
            "bpm": 200.0,
            "tilecount": 1000 + index,
            "midspin_count": 0,
            "auto_tile_count": 0,
            "playable_hits": 1000 + index,
            "parse_error": "",
        }
        for index in range(4)
    ]
    dialog = BatchBindDialog(entries, "light")
    dialog.mark_results({"folder:0", "folder:2"}, 0)
    assert [entry["key"] for entry in dialog._entries] == ["folder:1", "folder:3"]
    assert dialog.table.rowCount() == 2
    assert dialog.table.item(0, 1).text() == "Song 1"
    assert dialog.table.item(1, 1).text() == "Song 3"
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_start_worker_callbacks_run_on_ui_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    main_thread = QThread.currentThread()
    seen = []

    class Worker(QObject):
        finished = Signal(dict)

        def run(self) -> None:
            self.finished.emit({"ok": True})

    thread = window._start_worker(
        Worker(),
        on_finished=lambda payload: seen.append(
            (QThread.currentThread() is main_thread, payload)
        ),
    )
    _wait_for_thread(thread)
    app.processEvents()
    assert seen == [(True, {"ok": True})]
    window.close()
    app.processEvents()


def test_clear_login_only_allows_tool_subdirectory(tmp_path):
    root = tmp_path / "appdata"
    assert is_safe_profile_path(root / "browser_profile", root)
    assert not is_safe_profile_path(root, root)
    assert not is_safe_profile_path(tmp_path / "Edge" / "User Data", root)


def test_downloaded_chart_enters_library(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    chart = tmp_path / "chart.ard"
    chart.write_bytes(b"PK\x03\x04demo")
    level = LevelInfo(tuf_id=4242, song="Demo Song", charter="Tester", difficulty="G12")
    window.workspace.bind_level(level)
    window._on_chart_downloaded({"path": str(chart), "level": level.to_dict()})
    entries = window.library_db.all()
    assert len(entries) == 1
    assert entries[0]["tuf_id"] == 4242
    assert window.workspace.draft.chart_path == str(chart)
    assert len(window.library.local_model.items()) == 1
    window.close()
    assert app is not None


def test_downloaded_extracted_chart_records_folder_and_category(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    chart = tmp_path / "levels" / "P" / "P15" / "Icedust" / "level.adofai"
    chart.parent.mkdir(parents=True)
    chart.write_text("{}", encoding="utf-8")
    level = LevelInfo(tuf_id=17198, song="Icedust", charter="mik", difficulty="P15")

    window._on_chart_downloaded({"path": str(chart), "level": level.to_dict()})

    entry = window.library_db.all()[0]
    assert entry["folder"] == str(chart.parent)
    assert entry["category"] == "P"
    assert entry["band"] == "P15"
    window.close()
    assert app is not None


def test_chart_library_links_local_and_submitted_status():
    app = QApplication.instance() or QApplication([])
    page = ChartLibraryPage()
    downloaded = LevelInfo(
        tuf_id=17713,
        song="Downloaded",
        charter="Charter",
        difficulty="P17",
    ).to_dict()
    downloaded.update({"key": "tuf:17713", "path": "C:/levels/17713.ard"})
    unmatched = LevelInfo(song="Local Only", difficulty="G8").to_dict()
    unmatched.update({"key": "file:local", "path": "C:/levels/local.ard"})

    page.set_local_entries([downloaded, unmatched])
    items = page.local_model.items()
    assert len(items) == 2
    assert items[0].matched is True
    assert items[1].matched is False

    page.set_submitted_records(
        [{"tuf_id": 17713, "bvid": "BV1xx", "accuracy": "99.00%", "status": "submitted"}]
    )
    items = page.local_model.items()
    assert items[0].submitted == 1
    assert page.records_for(17713)[0]["bvid"] == "BV1xx"

    page.local_search.setText("Local")
    assert len(page.local_model.items()) == 1
    assert page.local_model.items()[0].level.song == "Local Only"
    page.local_search.clear()

    page.local_filter_combo.setCurrentIndex(2)
    assert len(page.local_model.items()) == 1
    assert page.local_model.items()[0].matched is False
    page.local_filter_combo.setCurrentIndex(3)
    assert len(page.local_model.items()) == 1
    assert page.local_model.items()[0].submitted == 1
    page.local_filter_combo.setCurrentIndex(0)
    assert len(page.local_model.items()) == 2

    collected = LevelInfo(song="Collection Song", difficulty="P10").to_dict()
    collected.update(
        {
            "key": "folder:collection",
            "path": "C:/levels/collection/song.adofai",
            "category": "Collection",
            "collection": "智柚下和",
        }
    )
    page.set_local_entries([downloaded, unmatched, collected])
    page.local_category_combo.setCurrentIndex(7)
    items = page.local_model.items()
    assert len(items) == 1
    assert items[0].entry["collection"] == "智柚下和"
    page.local_category_combo.setCurrentIndex(1)
    assert [item.level.song for item in page.local_model.items()] == ["Downloaded"]
    page.local_category_combo.setCurrentIndex(0)

    page.set_page(
        {
            "request_id": 0,
            "items": [downloaded],
            "total": 1,
            "offset": 0,
            "limit": 50,
            "has_more": False,
        }
    )
    online = page.online_model.items()[0]
    assert online.downloaded is True
    assert online.submitted == 1
    page.close()
    page.deleteLater()
    app.processEvents()
    assert app is not None


def test_chart_library_bili_only_record_is_not_tuf_submitted():
    app = QApplication.instance() or QApplication([])
    page = ChartLibraryPage()
    downloaded = LevelInfo(
        tuf_id=17714,
        song="Bili Only Song",
        charter="Charter",
        difficulty="P10",
    ).to_dict()
    downloaded.update({"key": "tuf:17714", "path": "C:/levels/17714.ard"})
    page.set_local_entries([downloaded])
    page.set_submitted_records(
        [
            {
                "tuf_id": 17714,
                "bvid": "BV1yy",
                "accuracy": "98.00%",
                "status": "bili_only",
            }
        ]
    )

    assert page.local_model.items()[0].submitted == 0
    assert [item.level.tuf_id for item in page.pending_items()] == [17714]
    assert page.records_for(17714)[0]["bvid"] == "BV1yy"
    page.close()
    page.deleteLater()
    app.processEvents()


def test_chart_library_bili_uploaded_record_is_not_tuf_submitted():
    app = QApplication.instance() or QApplication([])
    page = ChartLibraryPage()
    uploaded = LevelInfo(
        tuf_id=17715,
        song="Bili Uploaded Song",
        charter="Charter",
        difficulty="P11",
    ).to_dict()
    uploaded.update({"key": "tuf:17715", "path": "C:/levels/17715.ard"})
    submitted = LevelInfo(
        tuf_id=17716,
        song="Submitted Song",
        charter="Charter",
        difficulty="P12",
    ).to_dict()
    submitted.update({"key": "tuf:17716", "path": "C:/levels/17716.ard"})
    page.set_local_entries([uploaded, submitted])
    page.set_submitted_records(
        [
            {
                "tuf_id": 17715,
                "bvid": "BV1zz",
                "accuracy": "97.00%",
                "status": "bili_uploaded",
            },
            {
                "tuf_id": 17716,
                "bvid": "BV1ww",
                "accuracy": "99.50%",
                "status": "submitted",
            },
        ]
    )

    items = {item.level.tuf_id: item for item in page.local_model.items()}
    assert items[17715].submitted == 0
    assert items[17716].submitted == 1
    assert [item.level.tuf_id for item in page.pending_items()] == [17715]
    assert page.records_for(17715)[0]["bvid"] == "BV1zz"

    page.local_filter_combo.setCurrentIndex(3)
    assert [item.level.tuf_id for item in page.local_model.items()] == [17716]
    page.local_filter_combo.setCurrentIndex(0)

    page.close()
    page.deleteLater()
    app.processEvents()


def test_chart_library_download_progress_panel():
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.download_tasks import (
        STATUS_DOWNLOADING,
        STATUS_EXTRACTING,
        STATUS_PENDING,
        DownloadTask,
    )

    page = ChartLibraryPage()
    assert page.download_panel.isHidden()
    level = LevelInfo(tuf_id=7544, song="carnation")

    page.set_download_tasks([DownloadTask(level=level, status=STATUS_PENDING)])
    assert not page.download_panel.isHidden()
    assert "等待" in page.download_title.text()
    assert "carnation" in page.download_detail.text()

    task = DownloadTask(
        level=level,
        status=STATUS_DOWNLOADING,
        received=2 * 1024 * 1024,
        total=8 * 1024 * 1024,
    )
    page.set_download_tasks([task])
    assert page.download_bar.value() == 25
    assert "25%" in page.download_detail.text()

    task.status = STATUS_EXTRACTING
    page.set_download_tasks([task])
    assert page.download_bar.value() == 100
    assert "解压" in page.download_detail.text()

    page.mark_downloaded(7544, "C:/levels/P10/carnation/main.adofai")
    assert page.download_panel.isHidden()
    page.close()
    page.deleteLater()
    app.processEvents()
    assert app is not None


def test_chart_library_hides_panel_after_tasks_finish():
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.download_tasks import STATUS_COMPLETED, DownloadTask

    page = ChartLibraryPage()
    page.set_download_tasks(
        [DownloadTask(level=LevelInfo(tuf_id=7544, song="carnation"), status=STATUS_COMPLETED)]
    )

    assert page.download_panel.isHidden()
    assert not page.download_tasks_button.isHidden()
    assert "记录" in page.download_tasks_button.text()
    page.close()
    page.deleteLater()
    app.processEvents()
    assert app is not None


def test_chart_library_download_tasks_button_counts():
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.download_tasks import STATUS_COMPLETED, DownloadTask

    page = ChartLibraryPage()
    page.set_download_tasks(
        [
            DownloadTask(level=LevelInfo(tuf_id=1, song="Waiting")),
            DownloadTask(
                level=LevelInfo(tuf_id=2, song="Done"), status=STATUS_COMPLETED
            ),
        ]
    )

    assert page.download_tasks_button.text() == "下载任务 1"
    tooltip = page.download_tasks_button.toolTip()
    assert "等待 1" in tooltip and "完成 1" in tooltip
    assert "1 个等待" in page.download_title.text()
    assert page.download_panel.isHidden() is False
    page.close()
    page.deleteLater()
    app.processEvents()
    assert app is not None


def test_chart_library_collection_filter_and_empty_collection_names():
    app = QApplication.instance() or QApplication([])
    page = ChartLibraryPage()
    collected = LevelInfo(song="Collection Song", difficulty="P10").to_dict()
    collected.update(
        {
            "key": "folder:collection",
            "path": "C:/levels/collection/song.adofai",
            "category": "Collection",
            "band": "P10",
            "collection": "作者合集",
        }
    )
    regular = LevelInfo(song="Regular Song", difficulty="P10").to_dict()
    regular.update(
        {
            "key": "folder:regular",
            "path": "C:/levels/regular/song.adofai",
            "category": "P",
            "band": "P10",
        }
    )
    page.set_collection_names(["作者合集", "空合集"])
    page.set_local_entries([collected, regular])

    assert page.local_collection_combo.findData("作者合集") >= 0
    assert page.local_collection_combo.findData("空合集") >= 0
    page.local_collection_combo.setCurrentIndex(
        page.local_collection_combo.findData("作者合集")
    )
    assert page.local_category_combo.currentData() == "Collection"
    assert [item.level.song for item in page.local_model.items()] == [
        "Collection Song"
    ]

    page._reset_local_filters()
    assert len(page.local_model.items()) == 2
    page.close()
    page.deleteLater()
    app.processEvents()


def test_chart_detail_emits_collection_assignment():
    app = QApplication.instance() or QApplication([])
    entry = LevelInfo(song="Local Song", difficulty="P10").to_dict()
    entry.update(
        {
            "key": "folder:c:/levels/local",
            "path": "C:/levels/local/chart.adofai",
            "source": "scan",
        }
    )
    item = ChartItem(
        level=LevelInfo.from_dict(entry),
        source="local",
        entry=entry,
        downloaded=True,
        matched=False,
    )
    dialog = ChartDetailDialog(
        item,
        {},
        [],
        None,
        collections=["已有合集"],
    )
    captured: list[tuple[str, str]] = []
    dialog.collectionAssignRequested.connect(
        lambda key, name: captured.append((key, name))
    )

    dialog.collection_combo.setCurrentText("新合集")
    dialog.collection_apply_button.click()
    assert captured == [("folder:c:/levels/local", "新合集")]

    dialog.set_collection_result("新合集", "已归入合集")
    assert item.entry["collection"] == "新合集"
    assert item.entry["category"] == "Collection"
    assert dialog.collection_value.text() == "新合集"
    assert "已归入合集" in dialog.collection_status.text()
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_chart_library_pending_queue_excludes_submitted_and_unmatched():
    app = QApplication.instance() or QApplication([])
    page = ChartLibraryPage()
    pending = LevelInfo(tuf_id=101, song="Pending", difficulty="P10").to_dict()
    pending.update({"key": "tuf:101", "path": "C:/levels/101.ard"})
    submitted = LevelInfo(tuf_id=102, song="Submitted", difficulty="G5").to_dict()
    submitted.update({"key": "tuf:102", "path": "C:/levels/102.ard"})
    unmatched = LevelInfo(song="Local Only", difficulty="U3").to_dict()
    unmatched.update({"key": "folder:local", "path": "C:/levels/local/adofai.ard"})

    page.set_local_entries([pending, submitted, unmatched])
    page.set_submitted_records(
        [{"tuf_id": 102, "bvid": "BV1xx", "status": "submitted"}]
    )

    queue = page.pending_items()
    assert [item.level.tuf_id for item in queue] == [101]
    emitted: list[bool] = []
    page.queueRequested.connect(lambda: emitted.append(True))
    page.queue_button.click()
    assert emitted == [True]
    page.close()
    page.deleteLater()
    app.processEvents()


def test_chart_detail_emits_publish_and_loads_recent_record():
    app = QApplication.instance() or QApplication([])
    entry = LevelInfo(tuf_id=17713, song="Detail Song", difficulty="P17").to_dict()
    entry.update({"key": "tuf:17713", "path": "C:/levels/17713.ard"})
    item = ChartItem(
        level=LevelInfo.from_dict(entry),
        source="local",
        entry=entry,
        downloaded=True,
        matched=True,
    )
    records = [
        {
            "created_at": "2026-01-01T12:00:00",
            "title": "【冰与火之舞】Detail Song",
            "bvid": "BV1xx",
            "accuracy": "99.00%",
            "status": "submitted",
            "bili_url": "https://www.bilibili.com/video/BV1xx",
        }
    ]
    dialog = ChartDetailDialog(item, {}, records, None)
    published: list[LevelInfo] = []
    loaded: list[dict] = []
    dialog.publishRequested.connect(published.append)
    dialog.recordLoadRequested.connect(loaded.append)

    assert dialog.history_table.rowCount() == 1
    assert dialog.history_table.item(0, 1).text().startswith("【冰与火之舞】")
    dialog.publish_button.click()
    assert published[0].tuf_id == 17713
    dialog.history_load_button.click()
    assert loaded[0]["bvid"] == "BV1xx"
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_submission_queue_dialog_emits_current_item():
    app = QApplication.instance() or QApplication([])
    items = [
        ChartItem(
            level=LevelInfo(tuf_id=201, song="First", difficulty="P1"),
            source="local",
            entry={"key": "tuf:201", "path": "C:/levels/201.ard"},
            downloaded=True,
            matched=True,
        ),
        ChartItem(
            level=LevelInfo(tuf_id=202, song="Second", difficulty="G2"),
            source="local",
            entry={"key": "tuf:202", "path": "C:/levels/202.ard"},
            downloaded=True,
            matched=True,
        ),
    ]
    dialog = SubmissionQueueDialog(items)
    captured: list[ChartItem] = []
    dialog.publishRequested.connect(captured.append)
    assert dialog.table.rowCount() == 2
    dialog.table.setCurrentCell(1, 0)
    dialog.publish_button.click()
    assert captured[0].level.tuf_id == 202

    dialog.set_items([])
    assert dialog.count_label.text() == "0 个待投稿谱面"
    assert not dialog.publish_button.isEnabled()
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_main_window_starts_submission_from_level(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    level = LevelInfo(
        tuf_id=17713,
        song="Queue Song",
        artist="Artist",
        charter="Charter",
        difficulty="P10",
        bpm=120,
        tilecount=100,
    )

    window._start_submission_from_level(level)

    assert window.draft.level is not None
    assert window.draft.level.tuf_id == 17713
    assert window.stack.currentWidget() is window.workspace
    assert window.workspace.current_step == 0
    assert not window.workspace.step_one.isHidden()
    assert window.workspace.step_two.isHidden()
    assert window.workspace.step_three.isHidden()
    assert window.workspace.search_input.text() == "17713"
    window.close()
    app.processEvents()


def test_start_submission_from_level_walks_publish_mode_and_video(
    tmp_path, monkeypatch
):
    """从谱面库投稿：第 1 步选发布方式 → 第 2 步选视频 → 第 3 步仍带原谱面。"""
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    level = LevelInfo(
        tuf_id=20201,
        song="Step One Song",
        artist="Artist",
        charter="Charter",
        difficulty="P11",
        bpm=150,
        tilecount=200,
    )

    window._start_submission_from_level(level)
    workspace = window.workspace
    assert workspace.current_step == 0

    # 第 1 步：发布方式由用户确认后，下一步进入选片。
    workspace.mode_dual_button.click()
    assert workspace.mode_dual_button.isChecked()
    workspace.next_button.click()
    assert workspace.current_step == 1
    assert not workspace.step_two.isHidden()
    assert workspace.step_one.isHidden()

    # 第 2 步：没有视频时被门禁拦下，不会跳到谱面步骤。
    messages: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda *args, **kwargs: messages.append(str(args[2])),
    )
    workspace.next_button.click()
    assert workspace.current_step == 1
    assert any("视频" in message for message in messages)

    video = tmp_path / "run.mp4"
    video.write_bytes(b"fake video")
    workspace.draft.video_path = str(video)
    workspace.next_button.click()
    assert workspace.current_step == 2
    assert not workspace.step_three.isHidden()
    assert workspace.step_two.isHidden()
    assert workspace._level is not None
    assert workspace._level.tuf_id == 20201
    assert workspace.search_input.text() == "20201"
    window.close()
    app.processEvents()


def test_home_page_shows_stats_and_emits_actions():
    app = QApplication.instance() or QApplication([])
    page = HomePage()
    chart = ChartItem(
        level=LevelInfo(tuf_id=301, song="Home Song", difficulty="P10"),
        source="local",
        entry={"key": "tuf:301", "path": "C:/levels/301.ard"},
        downloaded=True,
        matched=True,
    )
    page.set_data(
        local_count=12,
        matched_count=9,
        pending_count=3,
        submitted_count=6,
        collection_count=2,
        recent_records=[
            {
                "created_at": "2026-01-01T12:00:00",
                "title": "Home Record",
                "bvid": "BV1xx",
            }
        ],
        recent_charts=[chart],
    )
    assert page.stat_values["local"].text() == "12"
    assert page.stat_values["pending"].text() == "3"
    assert page.stat_hints["local"].text() == "已匹配 9"
    assert "待投稿 3" in page.pending_chip.text()
    assert page.queue_tool.text().endswith("3")
    assert page.recent_records.count() == 1
    assert page.recent_charts.count() == 1

    records: list[dict] = []
    charts: list[ChartItem] = []
    analyzed: list[bool] = []
    launched: list[bool] = []
    page.recordRequested.connect(records.append)
    page.chartRequested.connect(charts.append)
    page.analyzeRequested.connect(lambda: analyzed.append(True))
    page.launchGameRequested.connect(lambda: launched.append(True))
    page._open_record(page.recent_records.item(0))
    page._open_chart(page.recent_charts.item(0))
    page.analyze_tool.click()
    page.launch_tool.click()
    assert records[0]["bvid"] == "BV1xx"
    assert charts[0].level.tuf_id == 301
    assert analyzed == [True]
    assert launched == [True]
    page.close()
    page.deleteLater()
    app.processEvents()


def test_main_window_home_summary_counts_pending_charts(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    window.library_db.all = lambda: [
        {
            "key": "tuf:401",
            "path": "C:/levels/401.ard",
            "tuf_id": 401,
            "song": "Pending",
            "difficulty": "P1",
            "updated_at": "2026-01-02T00:00:00",
        },
        {
            "key": "tuf:402",
            "path": "C:/levels/402.ard",
            "tuf_id": 402,
            "song": "Done",
            "difficulty": "G2",
            "updated_at": "2026-01-01T00:00:00",
        },
        {
            "key": "folder:403",
            "path": "C:/levels/403.ard",
            "song": "Unmatched",
            "difficulty": "U3",
        },
    ]
    window.db.all = lambda: [
        {"id": "r1", "tuf_id": 402, "title": "Done", "status": "submitted"}
    ]
    window._refresh_home()
    assert window.home.stat_values["local"].text() == "3"
    assert window.home.stat_hints["local"].text() == "已匹配 2"
    assert window.home.stat_values["pending"].text() == "1"
    assert window.home.stat_values["submitted"].text() == "1"
    assert window.home.recent_charts.count() == 3
    window.close()
    app.processEvents()


def test_main_window_home_only_counts_submitted_as_submitted(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    window.library_db.all = lambda: [
        {
            "key": "tuf:501",
            "path": "C:/levels/501.ard",
            "tuf_id": 501,
            "song": "Submitted",
            "difficulty": "P1",
            "updated_at": "2026-01-03T00:00:00",
        },
        {
            "key": "tuf:502",
            "path": "C:/levels/502.ard",
            "tuf_id": 502,
            "song": "Uploaded",
            "difficulty": "P2",
            "updated_at": "2026-01-02T00:00:00",
        },
        {
            "key": "tuf:503",
            "path": "C:/levels/503.ard",
            "tuf_id": 503,
            "song": "Bili Only",
            "difficulty": "P3",
            "updated_at": "2026-01-01T00:00:00",
        },
    ]
    window.db.all = lambda: [
        {"id": "r1", "tuf_id": 501, "title": "Submitted", "status": "submitted"},
        {"id": "r2", "tuf_id": 502, "title": "Uploaded", "status": "bili_uploaded"},
        {"id": "r3", "tuf_id": 503, "title": "Bili Only", "status": "bili_only"},
    ]
    window._refresh_home()
    assert window.home.stat_hints["local"].text() == "已匹配 3"
    assert window.home.stat_values["submitted"].text() == "1"
    assert window.home.stat_values["pending"].text() == "2"
    assert "待投稿 2" in window.home.pending_chip.text()
    window.close()
    app.processEvents()


def test_main_window_bvid_commit_does_not_downgrade_submitted(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    submitted = window.db.add(
        {"tuf_id": 601, "title": "Submitted", "status": "submitted"}
    )
    uploaded = window.db.add(
        {"tuf_id": 602, "title": "Uploaded", "status": "bili_uploaded"}
    )
    draft = window.db.add({"tuf_id": 603, "title": "Draft", "status": "draft"})

    window._current_history_id = submitted["id"]
    window._on_bvid_committed("BV1aa")
    assert window.db.get(submitted["id"])["status"] == "submitted"
    assert window.db.get(submitted["id"])["bvid"] == "BV1aa"

    window._current_history_id = uploaded["id"]
    window._on_bvid_committed("BV1bb")
    assert window.db.get(uploaded["id"])["status"] == "bili_uploaded"

    window._current_history_id = draft["id"]
    window._on_bvid_committed("BV1cc")
    record = window.db.get(draft["id"])
    assert record["status"] == "bili_uploaded"
    assert record["bvid"] == "BV1cc"
    assert record["bili_url"] == "https://www.bilibili.com/video/BV1cc"
    window.close()
    app.processEvents()


def test_main_window_adds_analyzed_chart_to_library(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    folder = tmp_path / "levels" / "song"
    folder.mkdir(parents=True)
    chart = folder / "level.adofai"
    chart.write_text(
        json.dumps(
            {
                "settings": {
                    "bpm": 120,
                    "song": "Analyzed Song",
                    "artist": "Artist",
                    "author": "Charter",
                },
                "angleData": [0, 90, 180, 270],
                "actions": [],
            }
        ),
        encoding="utf-8",
    )
    analysis = analyze_chart(chart)
    level = LevelInfo(
        song=analysis.song,
        artist=analysis.artist,
        charter=analysis.author,
        bpm=analysis.bpm,
        tilecount=analysis.tile_count,
        midspin_count=analysis.midspin_count,
    )

    class _Dialog:
        def __init__(self):
            self.analysis = analysis
            self.message = ""

        def set_status(self, message: str) -> None:
            self.message = message

    dialog = _Dialog()
    window._add_analyzed_level(dialog, level)

    entries = window.library_db.all()
    assert len(entries) == 1
    assert entries[0]["song"] == "Analyzed Song"
    assert entries[0]["path"] == str(chart)
    assert entries[0]["tilecount"] == 4
    assert entries[0]["midspin_count"] == 0
    assert entries[0]["auto_tile_count"] == 0
    assert entries[0]["playable_hits"] == 4
    assert LevelInfo.from_dict(entries[0]).expected_hits == 4
    assert "已加入本地谱面库" in dialog.message
    assert window.home.stat_values["local"].text() == "1"
    window.close()
    app.processEvents()


def test_main_window_assigns_collection_without_touching_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    folder = tmp_path / "levels" / "song"
    folder.mkdir(parents=True)
    chart = folder / "level.adofai"
    chart.write_text("{}", encoding="utf-8")
    entry = window.library_db.upsert(
        LevelInfo(song="Song", difficulty="P10"),
        chart,
        {
            "folder": str(folder),
            "category": "P",
            "band": "P10",
            "source": "scan",
        },
    )
    window.library_db.save()
    window._refresh_library()
    item = ChartItem(
        level=LevelInfo.from_dict(entry),
        source="local",
        entry=entry,
        downloaded=True,
        matched=False,
    )
    dialog = ChartDetailDialog(item, {}, [], window, collections=[])

    window._assign_library_collection(dialog, entry["key"], "测试合集")

    saved = window.library_db.get(entry["key"])
    assert saved is not None
    assert saved["collection"] == "测试合集"
    assert saved["category"] == "Collection"
    assert saved["path"] == str(chart)
    assert chart.is_file()
    assert window.library.local_collection_combo.findData("测试合集") >= 0
    dialog.close()
    window.close()
    app.processEvents()


def test_collection_manager_emits_create_rename_dissolve(monkeypatch):
    app = QApplication.instance() or QApplication([])
    dialog = CollectionManagerDialog([("旧合集", 2)])
    created: list[str] = []
    renamed: list[tuple[str, str]] = []
    dissolved: list[str] = []
    dialog.createRequested.connect(created.append)
    dialog.renameRequested.connect(lambda old, new: renamed.append((old, new)))
    dialog.dissolveRequested.connect(dissolved.append)

    monkeypatch.setattr(
        QInputDialog,
        "getText",
        staticmethod(lambda *args, **kwargs: ("新合集", True)),
    )
    dialog._create()
    assert created == ["新合集"]

    dialog.list.setCurrentRow(0)
    monkeypatch.setattr(
        QInputDialog,
        "getText",
        staticmethod(lambda *args, **kwargs: ("改名合集", True)),
    )
    dialog._rename()
    assert renamed == [("旧合集", "改名合集")]

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes),
    )
    dialog._dissolve()
    assert dissolved == ["旧合集"]
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_disk_organizer_dialog_previews_only_and_confirms(monkeypatch):
    app = QApplication.instance() or QApplication([])
    plan = OrganizePlan(
        root="C:/levels",
        items=[
            OrganizeItem(
                key="folder:a",
                song="Move Song",
                source_folder="C:/levels/Incoming/Move Song",
                target_folder="C:/levels/P/P10/Move Song",
                source_path="C:/levels/Incoming/Move Song/level.adofai",
                target_path="C:/levels/P/P10/Move Song/level.adofai",
                old_category="P",
                old_band="P10",
                old_collection="",
                status="move",
            ),
            OrganizeItem(
                key="folder:b",
                song="Conflict Song",
                source_folder="C:/levels/Incoming/Conflict Song",
                target_folder="C:/levels/P/P10/Conflict Song",
                source_path="C:/levels/Incoming/Conflict Song/level.adofai",
                target_path="C:/levels/P/P10/Conflict Song/level.adofai",
                old_category="P",
                old_band="P10",
                old_collection="",
                status="conflict",
                message="目标目录已存在",
            ),
        ],
    )
    dialog = DiskOrganizerDialog(plan, "light", undo_available=False)
    assert dialog.table.rowCount() == 2
    assert "可移动 1" in dialog.summary.text()
    assert "已选 0 / 1 个可移动项" in dialog.selection.text()
    assert dialog.apply_button.text() == "应用选中项 (0)"
    assert not dialog.apply_button.isEnabled()
    assert not dialog.undo_button.isEnabled()
    assert not (
        dialog.table.item(1, 0).flags()
        & Qt.ItemFlag.ItemIsUserCheckable
    )

    dialog.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert "已选 1 / 1 个可移动项" in dialog.selection.text()
    assert dialog.apply_button.text() == "应用选中项 (1)"
    assert dialog.apply_button.isEnabled()

    captured: list[list[str]] = []
    dialog.applyRequested.connect(lambda keys: captured.append(keys))
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes),
    )
    dialog._confirm_apply()
    assert captured == [["folder:a"]]
    dialog._clear_selection()
    assert dialog.selected_keys() == []
    assert not dialog.apply_button.isEnabled()
    dialog._select_all()
    assert dialog.selected_keys() == ["folder:a"]
    dialog._clear_selection()
    dialog.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    dialog.search.setText("Conflict")
    app.processEvents()
    assert dialog.table.isRowHidden(0)
    assert not dialog.table.isRowHidden(1)
    assert dialog.selected_keys() == []
    assert not dialog.apply_button.isEnabled()
    dialog.search.clear()
    app.processEvents()
    assert not dialog.table.isRowHidden(0)
    dialog.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    dialog.set_busy(True, "正在整理磁盘…")
    assert not dialog.apply_button.isEnabled()
    dialog.set_busy(False)
    assert dialog.apply_button.isEnabled()
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_disk_organizer_checkbox_click_is_clear_and_toggles():
    app = QApplication.instance() or QApplication([])
    plan = OrganizePlan(
        root="C:/levels",
        items=[
            OrganizeItem(
                key="folder:a",
                song="Move Song",
                source_folder="C:/levels/Incoming/Move Song",
                target_folder="C:/levels/P/P10/Move Song",
                source_path="C:/levels/Incoming/Move Song/level.adofai",
                target_path="C:/levels/P/P10/Move Song/level.adofai",
                old_category="P",
                old_band="P10",
                old_collection="",
                status="move",
            )
        ],
    )
    dialog = DiskOrganizerDialog(plan, "dark")
    dialog.show()
    app.processEvents()

    rect = dialog.table.visualRect(dialog.table.model().index(0, 0))
    QTest.mouseClick(
        dialog.table.viewport(),
        Qt.MouseButton.LeftButton,
        pos=rect.center(),
    )
    app.processEvents()

    assert dialog.selected_keys() == ["folder:a"]
    assert "已选 1 / 1 个可移动项" in dialog.selection.text()
    assert dialog.apply_button.isEnabled()
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_organize_history_dialog_shows_items_and_requests_undo(monkeypatch):
    app = QApplication.instance() or QApplication([])
    records = [
        {
            "path": "C:/history/organize-20260101-120000.json",
            "created_at": "2026-01-01T12:00:00",
            "finished_at": "2026-01-01T12:00:01",
            "undo_finished_at": "",
            "status": "completed",
            "root": "C:/levels",
            "backup_path": "C:/history/level_library.json",
            "total": 1,
            "moved": 1,
            "failed": 0,
            "undone": 0,
            "pending": 0,
            "counts": {"moved": 1},
            "can_undo": True,
            "items": [
                {
                    "key": "folder:a",
                    "song": "Move Song",
                    "status": "moved",
                    "source_folder": "C:/levels/Incoming/Move Song",
                    "target_folder": "C:/levels/P/P10/Move Song",
                    "source_path": "C:/levels/Incoming/Move Song/level.adofai",
                    "target_path": "C:/levels/P/P10/Move Song/level.adofai",
                    "message": "",
                    "error": "",
                    "undo_error": "",
                }
            ],
        }
    ]
    dialog = OrganizeHistoryDialog(records, "light")
    assert dialog.records_table.rowCount() == 1
    assert dialog.detail_table.rowCount() == 1
    assert "显示 1 / 1 条整理记录" in dialog.summary.text()
    assert "1 条仍可撤销" in dialog.summary.text()
    assert dialog.detail_table.item(0, 2).text() == "Move Song"
    assert dialog.undo_button.isEnabled()

    captured: list[str] = []
    dialog.undoRequested.connect(captured.append)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes),
    )
    dialog._confirm_undo_record()
    assert captured == ["C:/history/organize-20260101-120000.json"]

    captured_items: list[tuple[str, list[str]]] = []
    dialog.undoItemsRequested.connect(
        lambda path, keys: captured_items.append((path, keys))
    )
    dialog.detail_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert dialog.selected_item_keys() == ["folder:a"]
    assert dialog.undo_items_button.isEnabled()
    dialog._confirm_undo_items()
    assert captured_items == [
        ("C:/history/organize-20260101-120000.json", ["folder:a"])
    ]

    dialog.set_busy(True, "正在撤销所选整理记录…")
    assert not dialog.undo_items_button.isEnabled()
    assert not dialog.undo_button.isEnabled()
    dialog.set_busy(False)
    assert dialog.undo_button.isEnabled()
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def _picker_levels() -> list[LevelInfo]:
    return [
        LevelInfo(
            tuf_id=17700,
            song="carnation [Featured ver.]",
            artist="rKizCora",
            charter="rKizCora",
            bpm=180,
            duration_ms=118000,
            difficulty="P15",
            difficulty_color="#e1a8ff",
            tilecount=411,
            midspin_count=17,
            tags=[{"name": "Featured ver.", "color": "#F472B6"}],
            unique_clears=3,
            likes=9,
            base_score=0.5,
        ),
        LevelInfo(
            tuf_id=17687,
            song="carnation [Long ver.] [Featured ver.]",
            artist="ゲサ",
            charter="ゲサ",
            difficulty="P15",
        ),
    ]


def test_level_picker_renders_chart_cards():
    app = QApplication.instance() or QApplication([])
    levels = _picker_levels()
    dialog = LevelPickerDialog(levels)
    assert dialog.model.rowCount() == 2
    first = dialog.model.item_at(0)
    assert first is not None and first.level.tuf_id == 17700
    assert dialog.delegate.actions_visible is False
    assert dialog.delegate.badge_text(first) == "P15"
    assert (
        dialog.delegate.meta_text(first.level)
        == "1:58 · 411 物量 · 180 BPM · 17 midspin"
    )
    assert dialog.delegate.stats_text(first) == "3 通关 · 9 赞"
    assert dialog.model.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == levels[0].song

    dialog.show()
    app.processEvents()
    image = dialog.list.viewport().grab().toImage()
    assert not image.isNull()
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_level_picker_double_click_and_enter_select_level():
    app = QApplication.instance() or QApplication([])
    levels = _picker_levels()

    double_clicked = LevelPickerDialog(levels)
    index = double_clicked.model.index(1, 0)
    double_clicked.list.setCurrentIndex(index)
    double_clicked.list.doubleClicked.emit(index)
    assert double_clicked.result() == int(QDialog.DialogCode.Accepted)
    assert double_clicked.selected is levels[1]
    double_clicked.deleteLater()

    activated = LevelPickerDialog(levels)
    first = activated.model.index(0, 0)
    activated.list.setCurrentIndex(first)
    activated.list.activated.emit(first)
    assert activated.selected is levels[0]
    activated.deleteLater()

    button = LevelPickerDialog(levels)
    button.list.setCurrentIndex(button.model.index(1, 0))
    button.ok_button.click()
    assert button.selected is levels[1]
    button.deleteLater()
    app.processEvents()


def test_level_picker_empty_state():
    app = QApplication.instance() or QApplication([])
    dialog = LevelPickerDialog([])
    assert dialog.model.rowCount() == 0
    assert dialog.empty.isVisibleTo(dialog)
    assert not dialog.list.isVisibleTo(dialog)
    assert not dialog.ok_button.isEnabled()
    dialog.deleteLater()
    app.processEvents()


def test_level_picker_degrades_without_metadata():
    app = QApplication.instance() or QApplication([])
    level = LevelInfo(song="只有曲名")
    dialog = LevelPickerDialog([level])
    item = dialog.model.item_at(0)
    assert item is not None
    assert dialog.delegate.badge_text(item) == "?"
    assert dialog.delegate.meta_text(level) == ""
    assert dialog.delegate.stats_text(item) == ""
    dialog.show()
    app.processEvents()
    assert not dialog.list.viewport().grab().isNull()
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_difficulty_icons_mapping_and_assets():
    from adofai_publisher import difficulty_icons as icons

    assert icons.icon_filename("P15") == "P15.png"
    assert icons.icon_filename("P0") == "P0.png"
    assert icons.icon_filename("G20") == "G20.png"
    assert icons.icon_filename("Unranked") == "Unranked.png"
    assert icons.icon_filename("Qq") == "Qq.png"
    assert icons.icon_filename("UQ0 (U1~U4)") == "Q0.png"
    assert icons.icon_filename("UQ3 (U13~U16)") == "Q3.png"
    assert icons.icon_filename("UQ4 (U17~U20)") == "Q4.png"
    assert icons.icon_filename("GQ0") == "GQ0.png"
    assert icons.icon_filename("GQ4") == "GQ4.png"
    assert icons.icon_filename("GQ0 (G1~G4)") == "GQ0.png"
    assert icons.icon_filename("GQ1 (G5~G8)") == "GQ1.png"
    assert icons.icon_filename("GQ2 (G9~G12)") == "GQ2.png"
    assert icons.icon_filename("GQ3 (G13~G16)") == "GQ3.png"
    assert icons.icon_filename("GQ4 (G17~G20)") == "GQ4.png"
    assert icons.icon_filename("Marathon") == "MA.png"
    assert icons.icon_filename("119HT") is None
    assert icons.icon_filename("") is None
    missing = [
        filename
        for filename in icons.DIFFICULTY_ICONS.values()
        if not (icons.DIFFICULTY_ICON_DIR / filename).is_file()
    ]
    assert missing == []


def test_special_difficulty_icons_load():
    """P0 与 GQ0~GQ4 的素材能被 QPixmap 加载（offscreen）。"""
    app = QApplication.instance() or QApplication([])
    from adofai_publisher import difficulty_icons as icons

    icons.clear_icon_cache()
    difficulties = (
        "P0",
        "GQ0 (G1~G4)",
        "GQ1 (G5~G8)",
        "GQ2 (G9~G12)",
        "GQ3 (G13~G16)",
        "GQ4 (G17~G20)",
    )
    for difficulty in difficulties:
        pixmap = icons.difficulty_pixmap(difficulty, 40)
        assert pixmap is not None, difficulty
        assert not pixmap.isNull(), difficulty
    # 短名与全名指向同一张素材。
    assert icons.difficulty_pixmap("GQ3", 40) is icons.difficulty_pixmap(
        "GQ3 (G13~G16)", 40
    )
    # 未收录的难度仍然回退文字徽章。
    assert icons.difficulty_pixmap("U16", 40) is None
    icons.clear_icon_cache()
    assert app is not None


def test_difficulty_pixmap_is_cached_and_missing_is_safe(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    from adofai_publisher import difficulty_icons as icons

    icons.clear_icon_cache()
    first = icons.difficulty_pixmap("P15", 40)
    second = icons.difficulty_pixmap("P15", 40)
    assert first is not None and not first.isNull()
    assert first is second
    assert icons.difficulty_pixmap("119HT", 40) is None

    monkeypatch.setattr(icons, "DIFFICULTY_ICON_DIR", tmp_path)
    icons.clear_icon_cache()
    assert icons.difficulty_pixmap("P15", 40) is None
    icons.clear_icon_cache()
    assert app is not None


def test_chart_card_uses_difficulty_icon_and_text_fallback(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    from adofai_publisher import difficulty_icons as icons

    known = LevelInfo(tuf_id=17701, song="P15 Song", difficulty="P15")
    unknown = LevelInfo(tuf_id=17702, song="119HT Song", difficulty="119HT")
    dialog = LevelPickerDialog([known, unknown])
    dialog.show()
    app.processEvents()
    assert not dialog.list.viewport().grab().isNull()
    assert icons.difficulty_pixmap(known.difficulty, 40) is not None
    unknown_item = dialog.model.item_at(1)
    assert unknown_item is not None
    assert dialog.delegate.badge_text(unknown_item) == "119HT"

    monkeypatch.setattr(icons, "DIFFICULTY_ICON_DIR", tmp_path)
    icons.clear_icon_cache()
    assert icons.difficulty_pixmap("P15", 40) is None
    dialog.list.viewport().update()
    app.processEvents()
    assert not dialog.list.viewport().grab().isNull()

    icons.clear_icon_cache()
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_level_picker_shows_candidate_total_and_id_hint():
    app = QApplication.instance() or QApplication([])
    levels = [LevelInfo(tuf_id=index, song=f"song {index}") for index in range(25)]
    dialog = LevelPickerDialog(levels)
    text = dialog.hint_label.text()
    assert "25 个候选" in text
    assert "输入编号" in text
    dialog.deleteLater()

    empty = LevelPickerDialog([])
    assert "输入编号" in empty.hint_label.text()
    empty.deleteLater()
    app.processEvents()


def test_settings_page_diagnostics_button_emits(tmp_path):
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.ui.pages import SettingsPage

    page = SettingsPage(Settings(tmp_path / "settings.json"))
    fired: list[bool] = []
    page.diagnosticsRequested.connect(lambda: fired.append(True))

    page.diagnostics_button.click()

    assert fired == [True]
    page.set_diagnostics_status("已复制到剪贴板")
    assert "已复制到剪贴板" in page.diagnostics_status.text()
    page.deleteLater()
    app.processEvents()


def test_workspace_cover_slots_sync_clear_and_legacy_draft(tmp_path, monkeypatch):
    from PySide6.QtGui import QImage

    from adofai_publisher.ui.widgets import CoverPicker

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "adofai_publisher.ui.widgets.cover_sync_dir",
        lambda: tmp_path / "cover_sync",
    )
    wide = tmp_path / "wide.png"
    image = QImage(400, 200, QImage.Format.Format_RGB32)
    image.fill(0)
    assert image.save(str(wide))

    page = WorkspacePage(Settings(tmp_path / "settings.json"))
    try:
        page.cover_picker.set_file(str(wide))
        assert page.draft.cover_path == str(wide)

        page._sync_cover_ratio()
        assert page.draft.cover_path_43
        saved = QImage(page.draft.cover_path_43)
        assert not saved.isNull()
        assert abs(saved.width() / saved.height() - 4 / 3) < 0.02
        assert page.cover_clear.isEnabled()

        page.cover_clear.click()
        assert page.draft.cover_path_43 == ""
        assert page.draft.cover_path == str(wide)

        # 旧草稿没有 cover_path_43：载入不报错，两个槽各自回填。
        page.load_draft(Draft(cover_path=str(wide), title="旧草稿"))
        assert page.draft.cover_path == str(wide)
        assert page.draft.cover_path_43 == ""
        assert page.cover_picker.active_slot == CoverPicker.SLOT_PRIMARY
    finally:
        page.deleteLater()
        app.processEvents()


def test_history_record_restores_both_cover_slots(tmp_path):
    from PySide6.QtGui import QImage

    from adofai_publisher.histdb import HistoryDB
    from adofai_publisher.ui.widgets import CoverPicker
    from adofai_publisher.wizard_state import draft_to_record

    app = QApplication.instance() or QApplication([])
    wide = tmp_path / "wide.png"
    square = tmp_path / "square.png"
    for path, (width, height) in ((wide, (400, 200)), (square, (300, 225))):
        image = QImage(width, height, QImage.Format.Format_RGB32)
        image.fill(0)
        assert image.save(str(path))

    draft = Draft(cover_path=str(wide), cover_path_43=str(square))
    database = HistoryDB(tmp_path / "records.json")
    stored = database.add(draft_to_record(draft, "100.00%"))
    record = HistoryDB(tmp_path / "records.json").get(stored["id"])
    assert record is not None

    restored = MainWindow._draft_from_record(None, record)
    page = WorkspacePage(Settings(tmp_path / "settings.json"))
    try:
        page.load_draft(restored)
        assert page.cover_picker.path_for(CoverPicker.SLOT_PRIMARY) == str(wide)
        assert page.cover_picker.path_for(CoverPicker.SLOT_ALTERNATE) == str(square)

        # 旧记录没有 cover_path_43：载入不报错，副槽为空、主图仍在。
        legacy = MainWindow._draft_from_record(
            None, {"cover_path": str(wide), "title": "旧投稿"}
        )
        assert legacy.cover_path_43 == ""
        page.load_draft(legacy)
        assert page.draft.cover_path == str(wide)
        assert page.draft.cover_path_43 == ""
        assert page.cover_picker.path_for(CoverPicker.SLOT_ALTERNATE) == ""
    finally:
        page.deleteLater()
        app.processEvents()


def _filter_levels():
    return [
        LevelInfo(
            tuf_id=101,
            song="Alpha Song",
            charter="Zeta",
            difficulty="P15",
            clears=10,
            likes=1,
            bpm=120,
            duration_ms=60000,
        ),
        LevelInfo(
            tuf_id=102,
            song="Beta Tune",
            charter="Amy",
            difficulty="G3",
            clears=99,
            likes=50,
            bpm=180,
            duration_ms=90000,
        ),
        LevelInfo(tuf_id=103, song="Gamma", charter="Bob", difficulty="Unranked"),
        LevelInfo(
            tuf_id=104,
            song="Delta",
            charter="Cid",
            difficulty="Censored",
            clears=0,
            likes=2,
            bpm=90,
            duration_ms=30000,
        ),
        LevelInfo(
            tuf_id=105,
            song="Epsilon",
            charter="Dan",
            difficulty="U7",
            clears=5,
            likes=9,
            bpm=150,
            duration_ms=120000,
        ),
    ]


def _picker_songs(dialog):
    return [dialog.model.item_at(row).level.song for row in range(dialog.model.rowCount())]


def test_level_picker_filters_by_keyword_case_insensitively():
    app = QApplication.instance() or QApplication([])
    dialog = LevelPickerDialog(_filter_levels())
    try:
        assert dialog.count_label.text() == "显示 5 / 共 5 条"

        dialog.filter_input.setText("ALPHA")
        app.processEvents()
        assert _picker_songs(dialog) == ["Alpha Song"]
        assert dialog.count_label.text() == "显示 1 / 共 5 条"

        dialog.filter_input.setText("amy")  # 谱师
        app.processEvents()
        assert _picker_songs(dialog) == ["Beta Tune"]

        dialog.filter_input.setText("103")  # TUF 编号
        app.processEvents()
        assert _picker_songs(dialog) == ["Gamma"]

        dialog.filter_input.setText("g3")  # 难度名
        app.processEvents()
        assert _picker_songs(dialog) == ["Beta Tune"]
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_level_picker_filters_by_difficulty_group():
    app = QApplication.instance() or QApplication([])
    dialog = LevelPickerDialog(_filter_levels())
    try:
        for band, expected in (
            ("P", ["Alpha Song"]),
            ("G", ["Beta Tune"]),
            ("U", ["Epsilon"]),
            ("其他", ["Gamma", "Delta"]),
            ("全部", ["Alpha Song", "Beta Tune", "Gamma", "Delta", "Epsilon"]),
        ):
            dialog.difficulty_combo.setCurrentText(band)
            app.processEvents()
            assert _picker_songs(dialog) == expected, band
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_level_picker_sort_orders_and_missing_values_last():
    app = QApplication.instance() or QApplication([])
    dialog = LevelPickerDialog(_filter_levels())
    try:
        expected = {
            "默认顺序": ["Alpha Song", "Beta Tune", "Gamma", "Delta", "Epsilon"],
            # U7 > G3 > P15；无难度的 Gamma / Delta 始终最后且保持原顺序。
            "难度 高→低": ["Epsilon", "Beta Tune", "Alpha Song", "Gamma", "Delta"],
            "难度 低→高": ["Alpha Song", "Beta Tune", "Epsilon", "Gamma", "Delta"],
            "通关数": ["Beta Tune", "Alpha Song", "Epsilon", "Gamma", "Delta"],
            "点赞数": ["Beta Tune", "Epsilon", "Delta", "Alpha Song", "Gamma"],
            # 无 BPM / 时长的 Gamma 排在最后。
            "BPM": ["Beta Tune", "Epsilon", "Alpha Song", "Delta", "Gamma"],
            "时长": ["Epsilon", "Beta Tune", "Alpha Song", "Delta", "Gamma"],
            "编号": ["Alpha Song", "Beta Tune", "Gamma", "Delta", "Epsilon"],
        }
        for mode, songs in expected.items():
            dialog.sort_combo.setCurrentText(mode)
            app.processEvents()
            assert _picker_songs(dialog) == songs, mode
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_level_picker_empty_filter_result_shows_hint():
    app = QApplication.instance() or QApplication([])
    dialog = LevelPickerDialog(_filter_levels())
    dialog.show()
    app.processEvents()
    try:
        dialog.filter_input.setText("不存在的谱面")
        app.processEvents()

        assert dialog.model.rowCount() == 0
        assert not dialog.list.isVisibleTo(dialog)
        assert dialog.empty.isVisibleTo(dialog)
        assert "没有匹配的候选" in dialog.empty.text()
        assert "输入编号" in dialog.empty.text()
        assert not dialog.ok_button.isEnabled()
        assert dialog.count_label.text() == "显示 0 / 共 5 条"

        dialog.filter_input.clear()
        app.processEvents()
        assert dialog.model.rowCount() == 5
        assert dialog.ok_button.isEnabled()
        assert not dialog.empty.isVisibleTo(dialog)
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_level_picker_enter_uses_highlighted_filtered_item():
    app = QApplication.instance() or QApplication([])
    dialog = LevelPickerDialog(_filter_levels())
    try:
        dialog.filter_input.setText("am")  # 命中 Beta Tune 与 Gamma
        app.processEvents()
        assert _picker_songs(dialog) == ["Beta Tune", "Gamma"]

        dialog.list.setCurrentIndex(dialog.model.index(1, 0))
        dialog.accept()

        assert dialog.result() == int(QDialog.DialogCode.Accepted)
        assert dialog.selected is not None
        # 选的是过滤后高亮的 Gamma，而不是原始列表第 2 项 Beta。
        assert dialog.selected.song == "Gamma"
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_main_window_restores_saved_download_queue_without_starting(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    queue = DownloadQueue(max_active=2)
    task = queue.add(
        LevelInfo(tuf_id=601, song="Restored", charter="C", difficulty="P10")
    )
    queue.take_next()
    queue.set_stage(task.task_id, "download")
    queue.update_progress(task.task_id, 50, 100)
    assert save_queue(queue) is True

    started: list[str] = []
    monkeypatch.setattr(
        MainWindow,
        "_start_download_task",
        lambda self, item: started.append(item.task_id),
    )
    monkeypatch.setattr(
        MainWindow,
        "_pump_download_queue",
        lambda self: started.append("pump"),
    )
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))

    # 重开工具只恢复任务，不自动开始下载。
    assert started == []
    tasks = window._download_queue.tasks()
    assert [item.task_id for item in tasks] == [task.task_id]
    assert tasks[0].status == STATUS_PENDING
    assert tasks[0].received == 50
    assert "下载任务 1" in window.library.download_tasks_button.text()

    window._show_download_tasks()
    dialog = window._download_dialog
    assert dialog is not None
    assert not dialog.resume_button.isHidden()
    assert dialog.resume_button.text() == "继续下载（1）"

    dialog.resume_button.click()
    app.processEvents()
    assert started == ["pump"]

    window.close()
    app.processEvents()


def test_download_progress_persist_timer_is_throttled_not_restarted(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    task = window._download_queue.add(LevelInfo(tuf_id=701, song="Throttle"))
    assert window._download_queue.take_next() is task

    timer = window._download_save_timer
    starts: list[bool] = []
    original_start = timer.start

    def counted_start(*args, **kwargs):
        starts.append(True)
        original_start(*args, **kwargs)

    monkeypatch.setattr(timer, "start", counted_start)
    saves: list[bool] = []
    monkeypatch.setattr(window, "_save_download_queue", lambda: saves.append(True))

    for received in range(20):
        window._on_download_task_progress(
            task.task_id,
            {"received": received, "total": 1000},
        )

    # 连续的进度信号不会反复重置定时器，也不会每次立刻落盘。
    assert starts == [True]
    assert timer.isActive()
    assert saves == []

    window.close()
    app.processEvents()


def test_settings_about_author_shows_author(tmp_path):
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.ui.pages import SettingsPage

    page = SettingsPage(Settings(tmp_path / "settings.json"))
    texts = [label.text() for label in page.findChildren(QLabel)]
    assert "DeanClock & " in texts
    page.close()
    page.deleteLater()
    app.processEvents()


def _variant_chart(angles, **settings):
    return json.dumps({"angleData": angles, "settings": settings}, ensure_ascii=False)


def test_chart_detail_lists_variants_and_emits_switch(tmp_path):
    app = QApplication.instance() or QApplication([])
    folder = tmp_path / "Song"
    folder.mkdir()
    full = folder / "level.adofai"
    full.write_text(_variant_chart([0, 90, 180, 270], song="Full Song"), encoding="utf-8")
    (folder / "level nerf.adofai").write_text(
        _variant_chart([0, 90], song="Nerf Song"), encoding="utf-8"
    )
    entry = LevelInfo(song="Full Song", tilecount=4).to_dict()
    entry.update(
        {
            "key": "folder:x",
            "path": str(full),
            "folder": str(folder),
            "source": "scan",
            "variant": "level.adofai",
            "playable_hits": 4,
        }
    )
    item = ChartItem(
        level=LevelInfo.from_dict(entry),
        source="local",
        entry=entry,
        downloaded=True,
        matched=False,
    )
    dialog = ChartDetailDialog(item, {}, [], None)

    texts = [label.text() for label in dialog.findChildren(QLabel)]
    assert "谱面变体" in texts
    assert "level nerf.adofai" in texts
    assert dialog._variant_marks["level.adofai"].isHidden() is False
    assert dialog._variant_marks["level nerf.adofai"].isHidden() is True

    captured: list[str] = []
    dialog.variantRequested.connect(captured.append)
    dialog._choose_variant("level nerf.adofai")
    assert captured == ["level nerf.adofai"]

    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_chart_detail_detected_tuf_offers_fill_button(tmp_path):
    app = QApplication.instance() or QApplication([])
    folder = tmp_path / "Song"
    folder.mkdir()
    chart = folder / "level.adofai"
    chart.write_text(_variant_chart([0, 90], song="Song"), encoding="utf-8")
    entry = LevelInfo(tuf_id=94, song="Song", tilecount=2).to_dict()
    entry.update(
        {
            "key": "tuf:94",
            "path": str(chart),
            "folder": str(folder),
            "source": "scan",
            "tuf_bound": False,
            "meta_missing": ["artist"],
        }
    )
    item = ChartItem(
        level=LevelInfo.from_dict(entry),
        source="local",
        entry=entry,
        downloaded=True,
        matched=True,
    )
    dialog = ChartDetailDialog(item, {}, [], None)

    texts = [label.text() for label in dialog.findChildren(QLabel)]
    assert "已识别 TUF #94（未绑定）" in texts
    assert any("谱面里没写曲师" in text for text in texts)

    buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}
    assert "从 TUF 补齐信息" in buttons
    captured: list[str] = []
    dialog.tufFillRequested.connect(captured.append)
    buttons["从 TUF 补齐信息"].click()
    assert captured == ["tuf:94"]

    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_chart_library_q_menu_defaults_state_and_reset():
    app = QApplication.instance() or QApplication([])
    page = ChartLibraryPage()

    assert page.q_button.text() == "Q 难度"
    assert len(page.q_button._actions) == 11
    assert page.q_button.selected() == list(Q_SPECIAL_DIFFICULTIES)
    labels = [action.text() for action in page.q_button._actions]
    assert labels[0] == "Qq"
    assert labels[1] == "GQ0（G1~G4）"
    assert labels[5] == "GQ4（G17~G20）"
    assert labels[6] == "UQ0（U1~U4）"
    assert labels[-1] == "UQ4（U17~U20）"
    assert "每 4 级" in page.q_button.toolTip()

    assert page.current_state(0)["q_difficulties"] == list(Q_SPECIAL_DIFFICULTIES)
    # Q 难度已经从滑块 + 复选框改成下拉框。
    assert not hasattr(page, "q_slider")
    assert not hasattr(page, "q_toggle")

    page.difficulty_slider.set_range(21, 40)
    page.special_button._actions[0].setChecked(True)
    page._reset_filters()
    assert (page.difficulty_slider.low, page.difficulty_slider.high) == (1, 60)
    assert page.special_button.selected() == []
    assert page.q_button.text() == "Q 难度"
    assert page.current_state(0)["q_difficulties"] == list(Q_SPECIAL_DIFFICULTIES)

    # 本地谱面页没有 Q 控件，仍用 P1~U20。
    assert not hasattr(page, "local_q_button")
    assert page.local_difficulty_slider._label(1) == "P1"
    assert page.local_difficulty_slider._label(60) == "U20"


def test_chart_library_q_menu_partial_and_none_selection():
    app = QApplication.instance() or QApplication([])
    page = ChartLibraryPage()
    actions = page.q_button._actions
    by_name = {action.data(): action for action in actions}

    page._search_timer.stop()
    page.q_button.set_all(False, notify=False)
    for name in ("Qq", "GQ0 (G1~G4)", "UQ0 (U1~U4)"):
        by_name[name].setChecked(True)
    assert page.q_button.text() == "Q 难度 · 3/11"
    assert page.current_state(0)["q_difficulties"] == [
        "Qq",
        "GQ0 (G1~G4)",
        "UQ0 (U1~U4)",
    ]
    # 勾选本身就要触发一次查询，否则结果不会更新。
    assert page._search_timer.isActive()

    page.q_button.set_all(False)
    assert page.q_button.text() == "Q 难度 · 不含"
    assert page.current_state(0)["q_difficulties"] == []

    spy = QSignalSpy(page.searchRequested)
    page._emit_search()
    assert spy.count() == 1
    assert spy.at(0)[0]["q_difficulties"] == []

    # 菜单里的快捷项：全选 / 全不选。
    shortcuts = {
        action.text(): action
        for action in page.q_button.menu().actions()
        if action.text() in {"全选", "全不选"}
    }
    assert set(shortcuts) == {"全选", "全不选"}
    shortcuts["全选"].trigger()
    assert page.q_button.text() == "Q 难度"
    assert page.current_state(0)["q_difficulties"] == list(Q_SPECIAL_DIFFICULTIES)
    shortcuts["全不选"].trigger()
    assert page.q_button.text() == "Q 难度 · 不含"

    page._reset_filters()
    assert page.q_button.text() == "Q 难度"
    assert page.current_state(0)["q_difficulties"] == list(Q_SPECIAL_DIFFICULTIES)

    page.close()
    page.deleteLater()
    app.processEvents()


def test_filter_menu_stays_open_while_toggling_items():
    app = QApplication.instance() or QApplication([])
    page = ChartLibraryPage()
    page.show()
    app.processEvents()
    button = page.q_button
    menu = button.menu()
    assert isinstance(menu, PersistentMenu)

    def click(action) -> None:
        QTest.mouseClick(
            menu,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            menu.actionGeometry(action).center(),
        )
        app.processEvents()

    button.showMenu()
    app.processEvents()
    assert menu.isVisible()

    # 连续勾掉两个桶，菜单都应保持打开。
    click(menu.actions()[0])
    assert menu.isVisible()
    assert menu.actions()[0].isChecked() is False
    assert len(button.selected()) == 10
    click(menu.actions()[1])
    assert menu.isVisible()
    assert len(button.selected()) == 9
    assert button.text() == "Q 难度 · 9/11"

    # 「全不选」也不关菜单，方便看完勾选状态。
    all_none = next(a for a in menu.actions() if a.text() == "全不选")
    click(all_none)
    assert menu.isVisible()
    assert button.text() == "Q 难度 · 不含"
    assert button.selected() == []

    # Esc 正常关闭。
    QTest.keyClick(menu, Qt.Key.Key_Escape)
    app.processEvents()
    assert not menu.isVisible()

    # 点菜单外面同样关闭。
    button.showMenu()
    app.processEvents()
    assert menu.isVisible()
    menu.mousePressEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(-40, -40),
            QPointF(-40, -40),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )
    app.processEvents()
    assert not menu.isVisible()

    page.close()
    page.deleteLater()
    app.processEvents()


def test_filter_menu_closes_on_plain_action():
    app = QApplication.instance() or QApplication([])
    button = FilterMenuButton("测试")
    button.add_item("A", "a")
    menu = button.menu()
    fired: list[bool] = []
    plain = menu.addAction("普通项")
    plain.triggered.connect(lambda: fired.append(True))
    button.show()
    app.processEvents()
    button.showMenu()
    app.processEvents()
    assert menu.isVisible()

    QTest.mouseClick(
        menu,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        menu.actionGeometry(plain).center(),
    )
    app.processEvents()

    assert fired == [True]
    assert not menu.isVisible()

    button.close()
    button.deleteLater()
    app.processEvents()


def test_main_window_top_bar_compacts_only_on_library(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    # 打开谱面库页不联网：只关心页眉高度。
    monkeypatch.setattr(window, "_ensure_online_browse", lambda: None)
    monkeypatch.setattr(window, "_refresh_library", lambda: None)
    window.resize(1200, 800)
    window.show()
    app.processEvents()

    window.open_page("home")
    app.processEvents()
    normal = window._top_bar.height()
    assert window._top_bar_compact is False

    window.open_page("library")
    app.processEvents()
    compact = window._top_bar.height()
    assert window._top_bar_compact is True
    # 谱面库页页眉收紧约 20px，其它页面不受影响。
    assert normal - compact >= 18

    window.open_page("history")
    app.processEvents()
    assert window._top_bar_compact is False
    assert window._top_bar.height() == normal

    window.close()
    app.processEvents()


def test_chart_library_filter_rows_do_not_overlap():
    app = QApplication.instance() or QApplication([])
    page = ChartLibraryPage()
    page.resize(1100, 760)
    page.show()
    app.processEvents()

    def rect(widget) -> QRect:
        top = widget.mapTo(page, QPoint(0, 0))
        return QRect(top, widget.size())

    main = rect(page.difficulty_slider)
    assert main.height() == 44
    assert not rect(page.difficulty_title).intersects(main)
    for widget in (
        page.special_button,
        page.q_button,
        page.curation_button,
        page.tags_button,
        page.reset_button,
    ):
        button = rect(widget)
        assert not button.intersects(main), widget
        assert button.top() > main.bottom(), widget
    # 按钮右端与滑块右端对齐。
    assert abs(rect(page.reset_button).right() - main.right()) <= 1
    # 压缩之后筛选面板不应该再占两百多像素。
    panel = page.difficulty_slider.parentWidget()
    assert panel.height() <= 110

    page.close()
    page.deleteLater()
    app.processEvents()


def test_chart_library_main_slider_drag_refreshes_results():
    app = QApplication.instance() or QApplication([])
    page = ChartLibraryPage()
    page.resize(1100, 760)
    page.show()
    app.processEvents()

    def drag(slider, from_x: int, to_x: int) -> None:
        y = slider._track().center().y()
        QTest.mousePress(
            slider,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPoint(from_x, y),
        )
        QTest.mouseMove(slider, QPoint(to_x, y))
        QTest.mouseRelease(
            slider,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPoint(to_x, y),
        )
        app.processEvents()

    page._search_timer.stop()
    main = page.difficulty_slider
    drag(main, main._x_for(60), main._x_for(60) - 100)
    assert main.high < 60
    assert page.current_state(0)["pgu_range"] == (main.low, main.high)
    # 松开后要自动刷新结果，而不是等用户再点「查询」。
    assert page._search_timer.isActive()

    page.close()
    page.deleteLater()
    app.processEvents()


def test_difficulty_range_slider_handles_drag_and_reopen_when_collapsed():
    app = QApplication.instance() or QApplication([])
    slider = DifficultyRangeSlider()
    slider.resize(400, 52)
    slider.show()
    app.processEvents()
    y = slider._track().center().y()

    def drag(from_x: int, to_x: int) -> None:
        QTest.mousePress(
            slider,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPoint(from_x, y),
        )
        QTest.mouseMove(slider, QPoint(to_x, y))
        QTest.mouseRelease(
            slider,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPoint(to_x, y),
        )
        app.processEvents()

    spy = QSignalSpy(slider.rangeChanged)
    drag(slider._x_for(60), slider._x_for(60) - 120)
    assert slider.low == 1
    assert slider.high < 60
    assert spy.count() >= 1
    emitted_high = slider.high

    drag(slider._x_for(1), slider._x_for(1) + 60)
    assert slider.low > 1
    assert slider.high == emitted_high

    # 两端重合后按拖动方向决定拉开哪一边（0.17.14 里会彻底卡死）。
    slider.set_range(30, 30)
    x = slider._x_for(30)
    drag(x, x + 80)
    assert slider.low == 30
    assert slider.high > 30

    slider.set_range(30, 30)
    drag(x, x - 80)
    assert slider.high == 30
    assert slider.low < 30

    # 悬停把手时放大并换成左右箭头光标。直接投递事件，避免 offscreen 下
    # QTest.mouseMove 依赖窗口激活状态而不稳定。
    def move_to(x: int) -> None:
        slider.mouseMoveEvent(
            QMouseEvent(
                QEvent.Type.MouseMove,
                QPointF(x, y),
                QPointF(slider.mapToGlobal(QPoint(x, y))),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )

    slider.set_range(1, 60)
    move_to(slider._x_for(60))
    assert slider._hover == 2
    assert slider.cursor().shape() == Qt.CursorShape.SizeHorCursor
    move_to(200)
    assert slider._hover == 0
    assert slider.cursor().shape() == Qt.CursorShape.PointingHandCursor

    # 点轨道空白处仍然就近移动把手。
    drag(slider._x_for(50), slider._x_for(50))
    assert slider.high == 50

    slider.close()
    slider.deleteLater()
    app.processEvents()


def test_chart_library_online_paging_uses_server_limit_100():
    app = QApplication.instance() or QApplication([])
    page = ChartLibraryPage()
    assert page.current_state(0)["limit"] == 100

    def _level(tuf_id: int, song: str) -> dict:
        return LevelInfo(tuf_id=tuf_id, song=song).to_dict()

    page.set_page(
        {
            "request_id": 0,
            "items": [_level(1, "One"), _level(2, "Two")],
            "total": 300,
            "offset": 0,
            "limit": 100,
            "has_more": True,
        }
    )
    assert [item.level.tuf_id for item in page.online_model.items()] == [1, 2]
    assert page._next_offset == 2

    page.set_page(
        {
            "request_id": 0,
            "items": [
                _level(2, "Two duplicate"),
                _level(2, "Two duplicate again"),
                _level(3, "Three"),
            ],
            "total": 300,
            "offset": 100,
            "limit": 100,
            "has_more": True,
        },
        append=True,
    )
    # 重复 tuf_id 只保留第一次出现的；偏移按服务端回显 + 原始条数推进。
    assert [item.level.tuf_id for item in page.online_model.items()] == [1, 2, 3]
    assert page.online_model.items()[1].level.song == "Two"
    assert page._next_offset == 103

    page.set_page(
        {
            "request_id": 0,
            "items": [_level(4, "Four")],
            "total": 300,
            "offset": 103,
            "limit": 100,
            "has_more": True,
        },
        append=True,
    )
    assert page._next_offset == 104

    # 空页即使服务端还说有更多，也必须停，避免滚动死循环。
    page.set_page(
        {
            "request_id": 0,
            "items": [],
            "total": 300,
            "offset": 104,
            "limit": 100,
            "has_more": True,
        },
        append=True,
    )
    assert page._has_more is False
    assert page._next_offset == 104

    # 服务端回显 offset 没前进（重复页）：同样停，且不把偏移退回去。
    page.set_page(
        {
            "request_id": 0,
            "items": [_level(1, "One")],
            "total": 300,
            "offset": 0,
            "limit": 100,
            "has_more": True,
        },
        append=True,
    )
    assert page._has_more is False
    assert page._next_offset == 104

    page.close()
    page.deleteLater()
    app.processEvents()


def test_history_page_has_review_column(tmp_path):
    from adofai_publisher.ui.pages import HistoryPage

    app = QApplication.instance() or QApplication([])
    page = HistoryPage()
    try:
        page.set_records(
            [
                {
                    "id": "1",
                    "song": "Carnation",
                    "status": "submitted",
                    "tuf_record_id": "71967",
                    "review_status": "approved",
                },
                {"id": "2", "song": "Plain", "status": "submitted"},
            ]
        )
        assert page.table.columnCount() == 8
        assert page.table.horizontalHeaderItem(7).text() == "审核"
        assert page.table.item(0, 7).text() == "已通过"
        # 没同步过审核的记录显示「-」，不是「未通过」。
        assert page.table.item(1, 7).text() == "-"
        assert hasattr(page, "review_sync_button")
        assert page.review_sync_button.text() == "同步审核状态"
    finally:
        page.deleteLater()
        app.processEvents()


def test_publish_panel_review_chip_tones(tmp_path):
    from adofai_publisher.ui.pages import PublishPanel

    app = QApplication.instance() or QApplication([])
    panel = PublishPanel(Settings(tmp_path / "settings.json"))
    try:
        panel.set_draft(
            Draft(
                tuf_record_id="71967",
                review_status="pending",
                review_checked_at="2026-11-23 10:00",
                review_message="等待版主处理",
            )
        )
        assert panel.review_chip.objectName() == "ChipWarn"
        assert "待处理" in panel.review_chip.text()
        assert "2026-11-23 10:00" in panel.review_chip.toolTip()
        assert "等待版主处理" in panel.review_chip.toolTip()
        assert not panel.review_refresh_button.isHidden()

        panel.set_review("approved")
        assert panel.review_chip.objectName() == "ChipOk"
        assert "已通过" in panel.review_chip.text()

        panel.set_review("declined")
        assert panel.review_chip.objectName() == "ChipBad"
        assert "未通过" in panel.review_chip.text()

        panel.set_review("something-new")
        assert panel.review_chip.objectName() == "Chip"
        assert "未知" in panel.review_chip.text()
    finally:
        panel.deleteLater()
        app.processEvents()

    # 没有 TUF 记录编号时，「刷新审核状态」按钮隐藏。
    plain = PublishPanel(Settings(tmp_path / "settings.json"))
    try:
        plain.set_draft(Draft())
        assert plain.review_refresh_button.isHidden()
        assert "未同步" in plain.review_chip.text()
    finally:
        plain.deleteLater()
        app.processEvents()


def test_draft_from_record_keeps_review_fields():
    draft = MainWindow._draft_from_record(
        None,
        {
            "song": "Carnation",
            "tuf_id": 7544,
            "tuf_record_id": "71967",
            "status": "submitted",
            "review_status": "approved",
            "review_checked_at": "2026-11-23 10:00",
            "review_message": "ok",
            "review_assignee": "mod",
            "review_flags": [{"flag": "ok"}],
            "review_judgements": {"perfect": 10},
        },
    )

    assert draft.review_status == "approved"
    assert draft.review_checked_at == "2026-11-23 10:00"
    assert draft.review_message == "ok"
    assert draft.review_assignee == "mod"
    assert draft.review_flags == [{"flag": "ok"}]
    assert draft.review_judgements == {"perfect": 10}
    # 审核状态不影响投稿状态语义。
    assert draft.status == "submitted"


def test_history_page_has_tuf_submission_list_button():
    from adofai_publisher.ui.pages import HistoryPage

    app = QApplication.instance() or QApplication([])
    page = HistoryPage()
    try:
        assert page.tuf_list_button.text() == "查看 TUF 投稿记录"
        fired: list[bool] = []
        page.tufListRequested.connect(lambda: fired.append(True))
        page.tuf_list_button.click()
        assert fired == [True]
    finally:
        page.deleteLater()
        app.processEvents()


def test_tuf_submission_list_dialog_columns_and_states():
    from adofai_publisher.ui.pages import TufSubmissionListDialog

    app = QApplication.instance() or QApplication([])
    dialog = TufSubmissionListDialog()
    try:
        dialog.set_result(
            {
                "items": [
                    {
                        "id": "sub-57530",
                        "submission_id": "sub-57530",
                        "pass_id": "71967",
                        "level_id": "7544",
                        "song": "Carnation",
                        "artist": "Some Artist",
                        "difficulty": "P15",
                        "status": "approved",
                        "created_at": "2026-11-21T08:30:00.000Z",
                        "video_link": "https://www.bilibili.com/video/BV1xx411c7mD",
                    }
                ],
                "total": 1,
                "failed": 0,
                "auth_error": False,
                "retryable": False,
                "message": "",
                "counts": {"pending": 0, "approved": 1, "declined": 0},
            },
            {"71967"},
        )

        assert dialog.table.columnCount() == 6
        headers = [
            dialog.table.horizontalHeaderItem(index).text() for index in range(6)
        ]
        assert headers == [
            "记录编号",
            "谱面编号",
            "曲名 · 艺术家 · 难度",
            "审核状态",
            "提交时间",
            "视频链接",
        ]
        assert dialog.table.item(0, 0).text() == "71967（本地已有）"
        assert dialog.table.item(0, 1).text() == "7544"
        assert dialog.table.item(0, 2).text() == "Carnation · Some Artist · P15"
        assert dialog.table.item(0, 3).text() == "已通过"
        assert dialog.table.item(0, 4).text() == "2026-11-21 08:30"
        assert dialog.table.item(0, 5).text().endswith("BV1xx411c7mD")
        # 编号列显示 pass id（本地 tuf_record_id 用的是这一类），
        # 悬停能看到 submission id。
        assert "sub-57530" in dialog.table.item(0, 0).toolTip()
        assert dialog.footer.text() == (
            "共 1 条（待处理 0 / 已通过 1 / 未通过 0），读取失败 0 条"
        )
        assert not dialog.table.isHidden()

        # 未登录：明确文案，不出空表。
        dialog.set_result(
            {
                "items": [],
                "total": 0,
                "failed": 0,
                "auth_error": True,
                "retryable": False,
                "message": "TUF 未登录，请先登录后再查看投稿记录",
            },
            set(),
        )
        assert dialog.table.isHidden()
        assert "登录" in dialog.status_label.text()

        # 登录了但确实没有投稿：空表隐藏，页脚显示 0 条。
        dialog.set_result(
            {"items": [], "total": 0, "failed": 0, "auth_error": False, "retryable": False},
            set(),
        )
        assert dialog.table.isHidden()
        assert "还没有" in dialog.status_label.text()
        assert dialog.footer.text() == (
            "共 0 条（待处理 0 / 已通过 0 / 未通过 0），读取失败 0 条"
        )

        # 字段诊断只在页脚显示字段名，不显示字段值。
        dialog.set_result(
            {
                "items": [
                    {
                        "id": "sub-1",
                        "status": "pending",
                        "diagnostics": {
                            "missing": ["pass_id", "level_id"],
                            "top": ["id", "status", "mysteryTop"],
                            "pass": ["mysteryPass"],
                            "level": ["mysteryLevel"],
                        },
                    }
                ],
                "total": 1,
                "failed": 0,
                "auth_error": False,
                "retryable": False,
                "diagnostics": [
                    {
                        "missing": ["pass_id", "level_id"],
                        "top": ["id", "status", "mysteryTop"],
                        "pass": ["mysteryPass"],
                        "level": ["mysteryLevel"],
                        "extra": ["mysteryExtra"],
                    }
                ],
            },
            set(),
        )
        footer = dialog.footer.text()
        assert "字段诊断：缺少 pass_id、level_id" in footer
        assert "mysteryTop" in footer and "mysteryPass" in footer
        assert "mysteryLevel" in footer and "mysteryExtra" in footer

        # 未知状态显示「未知」而不是「未通过」。
        dialog.set_result(
            {
                "items": [
                    {
                        "id": "90001",
                        "level_id": "8888",
                        "status": "unknown",
                        "created_at": "",
                    }
                ],
                "total": 1,
                "failed": 0,
                "auth_error": False,
                "retryable": False,
            },
            set(),
        )
        assert dialog.table.item(0, 0).text() == "90001"
        assert dialog.table.item(0, 2).text() == "-"
        assert dialog.table.item(0, 3).text() == "未知"
        assert dialog.table.item(0, 4).text() == "-"
        assert dialog.table.item(0, 5).text() == "-"
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_workspace_search_syntax_button_opens_dialog(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.ui import widgets

    opened = []
    monkeypatch.setattr(
        widgets.SearchSyntaxDialog, "exec", lambda self: opened.append(self) or 0
    )
    page = WorkspacePage(Settings(tmp_path / "settings.json"))
    page.search_syntax_button.click()
    app.processEvents()

    assert len(opened) == 1
    assert opened[0].windowTitle() == "TUF 搜索语法"
    assert "song" in page.search_input.toolTip()
    page.deleteLater()
    app.processEvents()


def test_library_search_syntax_button_opens_dialog(monkeypatch):
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.ui import widgets

    opened = []
    monkeypatch.setattr(
        widgets.SearchSyntaxDialog, "exec", lambda self: opened.append(self) or 0
    )
    page = ChartLibraryPage()
    page.search_syntax_button.click()
    app.processEvents()

    assert len(opened) == 1
    assert opened[0].windowTitle() == "TUF 搜索语法"
    page.deleteLater()
    app.processEvents()


def test_search_syntax_dialog_lists_fields_and_examples():
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.ui.widgets import SearchSyntaxDialog

    dialog = SearchSyntaxDialog()
    texts = "\n".join(label.text() for label in dialog.findChildren(QLabel))
    for token in ("song", "artist", "bpm", "tilecount", "id", "#1234", "camellia"):
        assert token in texts
    dialog.deleteLater()
    app.processEvents()


def test_chart_analyzer_online_input_emits_tuf_id():
    from adofai_publisher.ui.chart_analyzer import ChartAnalyzerDialog

    app = QApplication.instance() or QApplication([])
    dialog = ChartAnalyzerDialog()
    captured: list[int] = []
    dialog.onlineAnalyzeRequested.connect(captured.append)

    dialog.online_input.setText("#17776")
    dialog.online_button.click()

    assert captured == [17776]
    assert "17776" in dialog.status.text()
    assert not dialog.add_button.isEnabled()
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_chart_analyzer_online_result_disables_add_but_allows_publish():
    from adofai_publisher.ui.chart_analyzer import ChartAnalyzerDialog

    app = QApplication.instance() or QApplication([])
    dialog = ChartAnalyzerDialog()
    analysis = ChartAnalysis(
        tuf_id=17776,
        source="tuf",
        song="Online Song",
        bpm=180,
        tile_count=1000,
        midspin_count=12,
        song_duration_ms=100000,
        chart_duration_ms=100000,
        speed_min=1,
        speed_max=2,
    )
    level = LevelInfo(tuf_id=17776, song="Online Song", difficulty="P10")

    dialog.present_online(analysis, level)

    assert dialog.values["song"].text() == "Online Song"
    assert dialog.values["bpm"].text() == "180"
    assert dialog.values["tiles"].text() == "1,000"
    assert dialog.values["midspin"].text() == "12"
    assert not dialog.add_button.isEnabled()
    assert dialog.publish_button.isEnabled()
    assert dialog.level() is level

    captured: list[LevelInfo] = []
    dialog.publishRequested.connect(captured.append)
    dialog.publish_button.click()
    assert captured[0].tuf_id == 17776
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_chart_analyzer_online_error_disables_actions():
    from adofai_publisher.ui.chart_analyzer import ChartAnalyzerDialog

    app = QApplication.instance() or QApplication([])
    dialog = ChartAnalyzerDialog()

    dialog.present_online(
        ChartAnalysis(tuf_id=404, source="tuf", error="没有找到 TUF 关卡 404 的在线谱面")
    )

    assert "解析失败" in dialog.status.text()
    assert not dialog.add_button.isEnabled()
    assert not dialog.publish_button.isEnabled()
    assert dialog.level() is None
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_chart_detail_dialog_offers_online_analysis_for_tuf_charts():
    app = QApplication.instance() or QApplication([])
    item = ChartItem(
        level=LevelInfo(tuf_id=17776, song="Online Song", difficulty="P10"),
        source="tuf",
    )
    dialog = ChartDetailDialog(item, {}, [], None)
    buttons = [
        button
        for button in dialog.findChildren(QPushButton)
        if button.text() == "分析在线谱面"
    ]

    assert len(buttons) == 1
    captured: list[int] = []
    dialog.analyzeOnlineRequested.connect(captured.append)
    buttons[0].click()
    assert captured == [17776]

    no_tuf = ChartItem(level=LevelInfo(song="No Id"), source="tuf")
    plain = ChartDetailDialog(no_tuf, {}, [], None)
    assert not [
        button
        for button in plain.findChildren(QPushButton)
        if button.text() == "分析在线谱面"
    ]
    dialog.close()
    plain.close()
    dialog.deleteLater()
    plain.deleteLater()
    app.processEvents()

# --- 本地谱面库：已绑定徽章 / 批量删除 ---------------------------------------

def _local_entry(key: str, path: Path, **overrides) -> dict:
    data = LevelInfo(song=key, difficulty="").to_dict()
    data.update({"key": key, "path": str(path), "folder": str(path.parent)})
    data.update(overrides)
    return data


def test_local_badge_uses_band_or_bound_fallback():
    app = QApplication.instance() or QApplication([])
    page = ChartLibraryPage()
    page.set_local_entries(
        [
            _local_entry(
                "bound-band",
                Path("C:/levels/bound-band.ard"),
                tuf_id=15269,
                tuf_bound=True,
                band="P10",
            ),
            _local_entry(
                "bound-plain",
                Path("C:/levels/bound-plain.ard"),
                tuf_id=15270,
                tuf_bound=True,
            ),
            _local_entry("unbound", Path("C:/levels/unbound.ard")),
        ]
    )
    items = {item.entry["key"]: item for item in page.local_model.items()}
    delegate = page.local_delegate

    assert delegate.badge_text(items["bound-band"]) == "P10"
    assert delegate.badge_text(items["bound-plain"]) == "已匹配"
    assert delegate.badge_text(items["unbound"]) == "未匹配"

    dialog = ChartDetailDialog(items["bound-plain"], {}, [], None)
    assert dialog._badge_label.text() == "已匹配"
    assert "物量按本地文件" in dialog._tuf_hint.text()
    dialog.close()
    dialog.deleteLater()
    page.deleteLater()
    app.processEvents()


def test_local_list_multi_select_emits_batch_delete_keys():
    app = QApplication.instance() or QApplication([])
    page = ChartLibraryPage()
    page.set_local_entries(
        [
            _local_entry("first", Path("C:/levels/first.ard")),
            _local_entry("second", Path("C:/levels/second.ard")),
            _local_entry("third", Path("C:/levels/third.ard")),
        ]
    )
    selection = page.local_list.selectionModel()
    flag = QItemSelectionModel.SelectionFlag.Select
    selection.select(page.local_model.index(0, 0), flag)
    selection.select(page.local_model.index(2, 0), flag)
    app.processEvents()

    assert page.local_delete_button.text() == "删除选中 (2)"
    assert page.local_delete_button.isEnabled()

    captured: list[list[str]] = []
    page.deleteManyRequested.connect(captured.append)
    page.local_delete_button.click()

    assert captured == [["first", "third"]]
    page.deleteLater()
    app.processEvents()


def test_batch_delete_removes_index_only_and_keeps_files(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    monkeypatch.setenv("ADOFAI_NO_AUTOLOGIN", "1")
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    charts = []
    keys = []
    for name in ("keep", "drop-a", "drop-b"):
        folder = tmp_path / "levels" / name
        folder.mkdir(parents=True)
        chart = folder / "level.adofai"
        chart.write_text("{}", encoding="utf-8")
        entry = window.library_db.upsert(
            LevelInfo(song=name, difficulty="P10"),
            chart,
            {"source": "scan", "folder": str(folder)},
        )
        charts.append(chart)
        keys.append(entry["key"])
    window.library_db.save()
    window._refresh_library()

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes),
    )
    window._delete_library_entries(keys[1:])

    remaining = {entry["song"] for entry in window.library_db.all()}
    assert remaining == {"keep"}
    assert all(chart.is_file() for chart in charts)
    window.close()
    app.processEvents()
