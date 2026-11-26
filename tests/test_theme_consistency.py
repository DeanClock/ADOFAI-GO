"""页面主题一致性回归测试。

覆盖四件事：
1. 页面文件里不再有写死颜色的 ``setStyleSheet``；
2. ``set_tone`` 写入的语义属性能被 QSS 识别、也能清空；
3. 对话框 ``theme=None`` 时跟随当前激活主题；
4. 谱面卡 delegate 在深浅主题下确实画出不同颜色。
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QListView

from adofai_publisher.level_organizer import OrganizePlan
from adofai_publisher.models import LevelInfo
from adofai_publisher.ui.batch_bind import BatchBindDialog
from adofai_publisher.ui.chart_analyzer import ChartAnalyzerDialog
from adofai_publisher.ui.chart_library import (
    ChartCardDelegate,
    ChartItem,
    ChartListModel,
)
from adofai_publisher.ui.collection_manager import CollectionManagerDialog
from adofai_publisher.ui.disk_organizer import DiskOrganizerDialog
from adofai_publisher.ui.download_center import DownloadTasksDialog
from adofai_publisher.ui.interactions import set_tone
from adofai_publisher.ui.organize_history import OrganizeHistoryDialog
from adofai_publisher.ui.theme import palette, set_active_theme

UI_DIR = Path(__file__).resolve().parents[1] / "adofai_publisher" / "ui"

THEME_FILES = (
    "pages.py",
    "home_page.py",
    "chart_library.py",
    "umm_page.py",
    "download_center.py",
    "batch_bind.py",
    "disk_organizer.py",
    "organize_history.py",
    "collection_manager.py",
    "chart_analyzer.py",
)

# `color: #hex`、`background: rgba(...)` 这类写死颜色；纯字体设置不算。
INLINE_COLOR = re.compile(
    r"(?:color|background(?:-color)?)\s*:\s*(?:#[0-9a-fA-F]{3,8}\b|rgba?\()",
    re.IGNORECASE,
)


def _stylesheet_literals(path: Path):
    """产出 (行号, 字符串字面量)，覆盖普通字符串与 f-string 的静态部分。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setStyleSheet"
            and node.args
        ):
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            yield node.lineno, argument.value
        elif isinstance(argument, ast.JoinedStr):
            literal = "".join(
                value.value
                for value in argument.values
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            )
            yield node.lineno, literal


def test_no_inline_colors_in_page_stylesheets():
    offenders: list[str] = []
    scanned = 0
    for name in THEME_FILES:
        path = UI_DIR / name
        for line, text in _stylesheet_literals(path):
            scanned += 1
            if INLINE_COLOR.search(text):
                offenders.append(f"{name}:{line}: {text!r}")
    assert scanned > 0
    assert offenders == []


def test_set_tone_writes_property_and_clears(app):
    label = QLabel("状态")
    set_tone(label, "warn")
    assert label.property("tone") == "warn"
    set_tone(label, "danger")
    assert label.property("tone") == "bad"
    set_tone(label, None)
    assert label.property("tone") is None


@pytest.fixture()
def app():
    application = QApplication.instance() or QApplication([])
    yield application
    set_active_theme("dark")


def test_dialogs_follow_active_theme_when_theme_is_none(app):
    set_active_theme("light")
    expected = palette("light")

    batch = BatchBindDialog([])
    history = OrganizeHistoryDialog([])
    downloads = DownloadTasksDialog()
    try:
        assert batch._palette == expected
        assert history._palette == expected
        assert downloads._theme == "light"
    finally:
        for dialog in (batch, history, downloads):
            dialog.deleteLater()
        app.processEvents()


def test_dialog_titles_use_page_title_object_name(app):
    dialogs = [
        BatchBindDialog([]),
        DiskOrganizerDialog(OrganizePlan(root="C:/levels")),
        OrganizeHistoryDialog([]),
        CollectionManagerDialog([]),
        ChartAnalyzerDialog(),
        DownloadTasksDialog(),
    ]
    try:
        for dialog in dialogs:
            titles = [
                label
                for label in dialog.findChildren(QLabel)
                if label.objectName() == "PageTitle"
            ]
            assert titles, type(dialog).__name__
    finally:
        for dialog in dialogs:
            dialog.deleteLater()
        app.processEvents()


def _image_signature(image) -> tuple[int, int]:
    total = 0
    for y in range(0, image.height(), 7):
        for x in range(0, image.width(), 7):
            total += image.pixelColor(x, y).rgba() & 0xFFFFFF
    return image.width(), total


def test_chart_delegate_repaints_for_each_theme(app):
    item = ChartItem(
        level=LevelInfo(tuf_id=1, song="Theme Song", charter="C", difficulty="P10"),
        source="local",
        entry={"key": "tuf:1", "path": "C:/levels/1.ard"},
        downloaded=True,
        matched=True,
    )
    model = ChartListModel()
    model.set_items([item])
    delegate = ChartCardDelegate()
    view = QListView()
    view.setModel(model)
    view.setItemDelegate(delegate)
    view.resize(620, 160)
    view.show()
    app.processEvents()

    delegate.set_theme("dark")
    view.viewport().update()
    app.processEvents()
    dark = _image_signature(view.viewport().grab().toImage())

    delegate.set_theme("light")
    view.viewport().update()
    app.processEvents()
    light = _image_signature(view.viewport().grab().toImage())

    assert dark != light
    view.close()
    view.deleteLater()
    app.processEvents()
