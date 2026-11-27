"""工作台「常用词条」卡片直接新增词条的测试。"""

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ADOFAI_NO_AUTOLOGIN", "1")

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from adofai_publisher.config import Settings
from adofai_publisher.ui import pages
from adofai_publisher.ui.pages import SettingsPage, WorkspacePage


class _StubSnippetDialog:
    """替身对话框：exec() 直接返回 Accepted，value() 弹出预置词条。"""

    values: list[tuple[str, str]] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def exec(self):
        return QDialog.DialogCode.Accepted

    def value(self) -> tuple[str, str]:
        return type(self).values.pop(0)


class _MessageBoxRecorder:
    """记录 QMessageBox.information 的调用，不弹真实窗口。"""

    def __init__(self) -> None:
        self.information_calls: list[tuple[str, str]] = []

    def information(self, _parent, title, text, *args, **kwargs):
        self.information_calls.append((title, text))
        return QMessageBox.StandardButton.Ok

    def question(self, *args, **kwargs):
        return QMessageBox.StandardButton.Yes


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def workspace(tmp_path, app):
    page = WorkspacePage(Settings(tmp_path / "settings.json"))
    page.show()
    page.windowHandle().requestActivate()
    page._show_step(3)
    app.processEvents()
    yield page
    page.close()
    page.deleteLater()
    app.processEvents()


def _chip_texts(page) -> list[str]:
    return [button.text() for button in page._snippet_buttons]


def _saved_snippets(tmp_path) -> list[dict[str, str]]:
    payload = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    return payload["copy_snippets"]


def test_add_snippet_button_click_saves_and_shows_chip(
    tmp_path, workspace, app, monkeypatch
):
    monkeypatch.setattr(pages, "CopySnippetDialog", _StubSnippetDialog)
    _StubSnippetDialog.values = [("初见即过", "成绩")]

    assert "初见即过" not in _chip_texts(workspace)

    workspace.snippet_add_button.click()
    app.processEvents()

    assert "初见即过" in _chip_texts(workspace)
    assert workspace.snippet_hint.text() == "已新增词条「初见即过」（成绩）"
    assert {"text": "初见即过", "group": "成绩"} in _saved_snippets(tmp_path)
    # 重启后仍在：新的 Settings 从同一份 settings.json 读回。
    assert {"text": "初见即过", "group": "成绩"} in Settings(
        tmp_path / "settings.json"
    ).get("copy_snippets")
    # 新按钮不是词条 chip，不参与插入/取消选中逻辑。
    assert workspace.snippet_add_button not in workspace._snippet_buttons
    assert not workspace.snippet_add_button.isCheckable()


def test_added_chip_still_inserts_into_title(tmp_path, workspace, app, monkeypatch):
    monkeypatch.setattr(pages, "CopySnippetDialog", _StubSnippetDialog)
    _StubSnippetDialog.values = [("初见即过", "成绩")]
    workspace.snippet_add_button.click()

    workspace.title_input.setText("曲名")
    workspace.title_input.setCursorPosition(len(workspace.title_input.text()))
    workspace.title_input.setFocus()
    app.processEvents()

    assert workspace._insert_copy_snippet("初见即过") is True
    assert workspace.title_input.text() == "曲名 初见即过"
    assert workspace.snippet_hint.text() == "已插入「初见即过」到标题"


def test_add_snippet_rejects_duplicate_and_empty(
    tmp_path, workspace, app, monkeypatch
):
    monkeypatch.setattr(pages, "CopySnippetDialog", _StubSnippetDialog)
    recorder = _MessageBoxRecorder()
    monkeypatch.setattr(pages, "QMessageBox", recorder)

    baseline = len(workspace._snippet_buttons)

    _StubSnippetDialog.values = [("初见即过", "成绩")]
    workspace.snippet_add_button.click()
    assert len(workspace._snippet_buttons) == baseline + 1

    # 同一 (text, group) 重复添加：提示、不写入、数量不变。
    _StubSnippetDialog.values = [("初见即过", "成绩")]
    workspace.snippet_add_button.click()
    assert len(workspace._snippet_buttons) == baseline + 1
    assert recorder.information_calls[-1][0] == "无法添加"

    # 空文本（对话框被绕过时）同样拒绝。
    _StubSnippetDialog.values = [("   ", "手感")]
    workspace.snippet_add_button.click()
    assert len(workspace._snippet_buttons) == baseline + 1
    assert len(recorder.information_calls) == 2

    matching = [
        item for item in _saved_snippets(tmp_path) if item["text"] == "初见即过"
    ]
    assert matching == [{"text": "初见即过", "group": "成绩"}]


def test_added_snippet_shows_up_in_settings_page(tmp_path, workspace, app, monkeypatch):
    monkeypatch.setattr(pages, "CopySnippetDialog", _StubSnippetDialog)
    _StubSnippetDialog.values = [("初见即过", "成绩")]
    workspace.snippet_add_button.click()

    settings_page = SettingsPage(Settings(tmp_path / "settings.json"))
    entries = [
        (
            settings_page.snippet_table.item(row, 0).text(),
            settings_page.snippet_table.item(row, 1).text(),
        )
        for row in range(settings_page.snippet_table.rowCount())
    ]
    assert ("成绩", "初见即过") in entries

    settings_page.close()
    settings_page.deleteLater()
    app.processEvents()
