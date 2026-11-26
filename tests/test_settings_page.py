import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ADOFAI_NO_AUTOLOGIN", "1")

from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from adofai_publisher.config import Settings
from adofai_publisher.ui.main_window import MainWindow
from adofai_publisher.ui.pages import SettingsPage


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _page(tmp_path) -> SettingsPage:
    return SettingsPage(Settings(tmp_path / "settings.json"))


def _choose_dialog_button(monkeypatch, label: str) -> None:
    """把 QMessageBox.exec 换成「点某个按钮」，模拟用户选择。"""

    def fake_exec(box: QMessageBox) -> int:
        for button in box.buttons():
            if button.text() == label:
                button.click()
                return 0
        raise AssertionError(f"弹窗里找不到「{label}」按钮")

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)


def _window(tmp_path, monkeypatch) -> MainWindow:
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    _app()
    return MainWindow(Settings(tmp_path / "settings.json"))


def _open_settings_dirty(window: MainWindow) -> None:
    window._navigate("settings")
    assert window.stack.currentWidget() is window.settings_page
    assert not window.settings_page.is_dirty()
    # 「浅色」= 索引 1，默认是深色，所以这一定是一处真实修改。
    window.settings_page.theme_combo.setCurrentIndex(1)
    assert window.settings_page.is_dirty()


def test_settings_page_starts_clean(tmp_path):
    app = _app()
    page = _page(tmp_path)
    assert page.is_dirty() is False
    page.deleteLater()
    app.processEvents()


def test_settings_page_dirty_after_theme_change(tmp_path):
    page = _page(tmp_path)
    page.theme_combo.setCurrentIndex(1)
    assert page.is_dirty() is True
    page.load()
    assert page.is_dirty() is False


def test_settings_page_dirty_after_partition_change(tmp_path):
    page = _page(tmp_path)
    page.partition_combo.setCurrentIndex(2)
    assert page.is_dirty() is True


def test_settings_page_dirty_after_checkbox_change(tmp_path):
    page = _page(tmp_path)
    page.restore_draft.setChecked(not page.restore_draft.isChecked())
    assert page.is_dirty() is True


def test_auto_check_updates_setting_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(path)
    page = SettingsPage(settings)

    assert page.auto_check_updates_checkbox.isChecked() is True
    page.auto_check_updates_checkbox.setChecked(False)
    values = page.collect()
    assert values["auto_check_updates"] is False

    settings.update(values)
    settings.save()
    loaded = Settings(path)
    assert loaded.get("auto_check_updates") is False
    assert SettingsPage(loaded).auto_check_updates_checkbox.isChecked() is False


def test_settings_page_dirty_after_snippet_added(tmp_path):
    page = _page(tmp_path)
    assert page.add_snippet("测试词条", "自定义") is not None
    assert page.is_dirty() is True


def test_settings_page_save_emits_values_and_clears_dirty(tmp_path):
    page = _page(tmp_path)
    page.theme_combo.setCurrentIndex(1)
    spy = QSignalSpy(page.saved)
    page.save()
    assert spy.count() == 1
    assert spy.at(0)[0]["theme"] == "light"
    assert page.is_dirty() is False


def test_settings_page_discard_changes_restores_saved_values(tmp_path):
    page = _page(tmp_path)
    page.theme_combo.setCurrentIndex(1)
    page.partition_combo.setCurrentIndex(3)
    page.discard_changes()
    assert page.theme_combo.currentIndex() == 0
    assert page.partition_combo.currentText() == "单机游戏"
    assert page.is_dirty() is False


def test_leaving_clean_settings_page_does_not_prompt(tmp_path, monkeypatch):
    window = _window(tmp_path, monkeypatch)

    def explode() -> str:
        raise AssertionError("设置没有改动时不该弹窗")

    window._ask_save_settings = explode
    window._navigate("settings")
    window._navigate("home")
    assert window.stack.currentWidget() is window.home

    # 在设置页内部再进设置页也不该弹窗。
    window._navigate("settings")
    window._navigate("settings")
    assert window.stack.currentWidget() is window.settings_page
    window.close()
    _app().processEvents()


def test_leave_settings_cancel_stays_and_restores_sidebar(tmp_path, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    _open_settings_dirty(window)
    _choose_dialog_button(monkeypatch, "取消")

    window.nav_buttons["home"].click()

    assert window.stack.currentWidget() is window.settings_page
    assert window.nav_buttons["settings"].isChecked()
    assert not window.nav_buttons["home"].isChecked()
    assert window.settings_page.is_dirty() is True
    assert window.settings.get("theme") == "dark"
    window.close()
    _app().processEvents()


def test_leave_settings_save_persists_and_navigates(tmp_path, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    _open_settings_dirty(window)
    _choose_dialog_button(monkeypatch, "保存并离开")

    window._navigate("home")

    assert window.stack.currentWidget() is window.home
    assert window.settings.get("theme") == "light"
    assert not window.settings_page.is_dirty()
    on_disk = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert on_disk["theme"] == "light"
    window.close()
    _app().processEvents()


def test_leave_settings_discard_navigates_and_form_resets(tmp_path, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    _open_settings_dirty(window)
    _choose_dialog_button(monkeypatch, "不保存离开")

    window._navigate("home")

    assert window.stack.currentWidget() is window.home
    assert window.settings.get("theme") == "dark"
    assert window.settings_page.is_dirty() is False

    window._navigate("settings")
    assert window.settings_page.theme_combo.currentIndex() == 0
    assert window.settings_page.is_dirty() is False
    window.close()
    _app().processEvents()


def test_save_button_still_saves_and_clears_dirty(tmp_path, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    _open_settings_dirty(window)
    save_button = next(
        button
        for button in window.settings_page.findChildren(QPushButton)
        if button.text() == "保存设置"
    )

    save_button.click()

    assert window.settings.get("theme") == "light"
    assert window.settings_page.is_dirty() is False
    window.close()
    _app().processEvents()


def test_leave_settings_dialog_offers_three_choices(tmp_path, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    _open_settings_dirty(window)
    seen: list[list[str]] = []

    def fake_exec(box: QMessageBox) -> int:
        seen.append([button.text() for button in box.buttons()])
        for button in box.buttons():
            if button.text() == "取消":
                button.click()
                return 0
        raise AssertionError("弹窗里找不到「取消」按钮")

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    window._navigate("home")

    assert len(seen) == 1
    assert set(seen[0]) == {"保存并离开", "不保存离开", "取消"}
    assert window.stack.currentWidget() is window.settings_page
    window.close()
    _app().processEvents()
