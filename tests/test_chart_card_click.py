"""谱面卡片点击行为的测试：单击只选中，双击或「详情」按钮才打开详情。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, QRect, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QListView,
    QStyleOptionViewItem,
)

from adofai_publisher.models import LevelInfo
from adofai_publisher.ui.chart_library import (
    ChartCardDelegate,
    ChartItem,
    ChartListModel,
)

CARD_RECT = QRect(0, 0, 600, 116)


def _release_event(
    x: int,
    y: int,
    modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> QMouseEvent:
    point = QPointF(x, y)
    return QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        point,
        point,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        modifiers,
    )


def _setup(ids: list[int] | None = None):
    app = QApplication.instance() or QApplication([])
    model = ChartListModel()
    model.set_items(
        [
            ChartItem(
                level=LevelInfo(tuf_id=tuf_id, song=f"Song {tuf_id}", difficulty="P1"),
                source="tuf",
            )
            for tuf_id in (ids or [17700])
        ]
    )
    delegate = ChartCardDelegate()
    option = QStyleOptionViewItem()
    option.rect = QRect(CARD_RECT)
    return app, model, delegate, model.index(0, 0), option


def _spy(delegate: ChartCardDelegate) -> tuple[list, list]:
    detail: list = []
    download: list = []
    delegate.detailClicked.connect(detail.append)
    delegate.downloadClicked.connect(download.append)
    return detail, download


def test_card_body_click_only_selects():
    """单击卡片主体：不发详情、不吞事件，交给视图处理选中。"""
    app, model, delegate, index, option = _setup()
    detail, download = _spy(delegate)

    handled = delegate.editorEvent(_release_event(260, 50), model, option, index)

    assert handled is False
    assert detail == []
    assert download == []
    app.processEvents()


def test_detail_button_still_opens_detail():
    app, model, delegate, index, option = _setup()
    detail, download = _spy(delegate)
    item = model.item_at(0)
    _, detail_rect = delegate._action_rects(option.rect, item)
    assert detail_rect.isValid()

    handled = delegate.editorEvent(
        _release_event(detail_rect.center().x(), detail_rect.center().y()),
        model,
        option,
        index,
    )

    assert handled is True
    assert detail == [item]
    assert download == []
    app.processEvents()


def test_download_button_still_downloads():
    app, model, delegate, index, option = _setup()
    detail, download = _spy(delegate)
    item = model.item_at(0)
    download_rect, _ = delegate._action_rects(option.rect, item)

    handled = delegate.editorEvent(
        _release_event(download_rect.center().x(), download_rect.center().y()),
        model,
        option,
        index,
    )

    assert handled is True
    assert download == [item]
    assert detail == []
    app.processEvents()


def test_hidden_actions_click_does_not_open_detail():
    """选谱弹窗（set_actions_visible(False)）里点按钮原位置也只走选中。"""
    app, model, delegate, index, option = _setup()
    delegate.set_actions_visible(False)
    detail, download = _spy(delegate)
    item = model.item_at(0)
    download_rect, detail_rect = delegate._action_rects(option.rect, item)
    assert download_rect.isEmpty() and detail_rect.isEmpty()

    handled = delegate.editorEvent(_release_event(520, 90), model, option, index)

    assert handled is False
    assert detail == []
    assert download == []
    app.processEvents()


def test_ctrl_click_keeps_multi_selection_without_detail():
    """本地列表 Ctrl+单击两项后保持两条选中，且不弹详情。"""
    app, model, delegate, _, _ = _setup([17700, 17701])
    detail, _ = _spy(delegate)
    view = QListView()
    view.setModel(model)
    view.setItemDelegate(delegate)
    view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    view.resize(620, 260)
    view.show()
    app.processEvents()

    rect0 = view.visualRect(model.index(0, 0))
    rect1 = view.visualRect(model.index(1, 0))
    QTest.mouseClick(
        view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect0.center()
    )
    QTest.mouseClick(
        view.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ControlModifier,
        rect1.center(),
    )
    app.processEvents()

    rows = view.selectionModel().selectedRows()
    assert [index.row() for index in rows] == [0, 1]
    assert detail == []
    view.close()
    app.processEvents()
