"""谱面详情弹窗 TUF 曲绘的测试（全部离线，加载器均注入假实现）。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication

from adofai_publisher.models import LevelInfo
from adofai_publisher.ui.chart_library import (
    DETAIL_COVER_SIZE,
    ChartDetailDialog,
    ChartItem,
)


class FakeThumbnailLoader(QObject):
    """记录请求、由测试手动控制到货时机的假加载器。"""

    thumbnailReady = Signal(int)

    def __init__(self, images: dict[int, QPixmap] | None = None) -> None:
        super().__init__()
        self.images = dict(images or {})
        self.requested: list[int] = []
        self.loaded: set[int] = set(self.images)

    def request(self, tuf_id: int) -> None:
        self.requested.append(int(tuf_id))

    def pixmap(self, tuf_id: int) -> QPixmap | None:
        return self.images.get(int(tuf_id))

    def is_loaded(self, tuf_id: int) -> bool:
        return int(tuf_id) in self.loaded

    def deliver(self, tuf_id: int, pixmap: QPixmap | None) -> None:
        """模拟后台加载完成：先写缓存，再发 thumbnailReady。"""
        key = int(tuf_id)
        self.loaded.add(key)
        if pixmap is None:
            self.images.pop(key, None)
        else:
            self.images[key] = pixmap
        self.thumbnailReady.emit(key)


def _pixmap(color: str) -> QPixmap:
    pixmap = QPixmap(80, 44)
    pixmap.fill(QColor(color))
    return pixmap


def _item(tuf_id: int | None, source: str = "tuf") -> ChartItem:
    return ChartItem(
        level=LevelInfo(
            tuf_id=tuf_id, song=f"Song {tuf_id or 'local'}", difficulty="P10"
        ),
        source=source,
    )


def _dialog(item: ChartItem, loader: FakeThumbnailLoader) -> ChartDetailDialog:
    return ChartDetailDialog(
        item, {}, [], None, thumbnail_loader=loader  # type: ignore[arg-type]
    )


def _close(app: QApplication, dialog: ChartDetailDialog) -> None:
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def _rendered_center(dialog: ChartDetailDialog) -> QColor:
    rendered = dialog._cover_view.grab().toImage()
    return rendered.pixelColor(rendered.width() // 2, rendered.height() // 2)


def test_detail_requests_cover_for_tuf_chart():
    app = QApplication.instance() or QApplication([])
    loader = FakeThumbnailLoader()
    dialog = _dialog(_item(17700), loader)
    try:
        assert loader.requested == [17700]
        # 还没到货：固定尺寸占位，难度徽章照旧可见。
        assert dialog._cover_view.pixmap() is None
        assert dialog._cover_view.placeholder_text() == "加载中…"
        assert dialog._cover_view.size().width() == DETAIL_COVER_SIZE
        assert dialog._badge_label.text() == "P10"
    finally:
        _close(app, dialog)


def test_detail_shows_cover_when_loader_delivers():
    app = QApplication.instance() or QApplication([])
    loader = FakeThumbnailLoader()
    dialog = _dialog(_item(17700), loader)
    try:
        loader.deliver(17700, _pixmap("#e02020"))
        app.processEvents()

        cover = dialog._cover_view.pixmap()
        assert cover is not None and not cover.isNull()
        assert dialog._cover_view.placeholder_text() == ""
        # 贴图不改变占位框尺寸，难度徽章仍在 header 里。
        assert dialog._cover_view.size().width() == DETAIL_COVER_SIZE
        assert dialog._badge_label.text() == "P10"
        # 真的画上去了：封面中心应是曲绘的红色。
        center = _rendered_center(dialog)
        assert center.red() > 150 and center.green() < 100
    finally:
        _close(app, dialog)


def test_detail_shows_cached_cover_immediately():
    app = QApplication.instance() or QApplication([])
    cached = _pixmap("#20a020")
    loader = FakeThumbnailLoader({17700: cached})
    dialog = _dialog(_item(17700), loader)
    try:
        # 请求照发（加载器内部命中缓存直接返回），图片同步贴上。
        assert loader.requested == [17700]
        assert dialog._cover_view.pixmap() is cached
        assert dialog._cover_view.placeholder_text() == ""
    finally:
        _close(app, dialog)


def test_detail_without_tuf_id_keeps_placeholder():
    app = QApplication.instance() or QApplication([])
    loader = FakeThumbnailLoader()
    dialog = _dialog(_item(None, source="local"), loader)
    try:
        assert loader.requested == []
        assert dialog._cover_view.pixmap() is None
        assert dialog._cover_view.placeholder_text() == "无曲绘"
    finally:
        _close(app, dialog)


def test_detail_shows_placeholder_when_tuf_has_no_cover():
    app = QApplication.instance() or QApplication([])
    loader = FakeThumbnailLoader()
    dialog = _dialog(_item(17700), loader)
    try:
        loader.deliver(17700, None)
        app.processEvents()

        assert dialog._cover_view.pixmap() is None
        assert dialog._cover_view.placeholder_text() == "无曲绘"
    finally:
        _close(app, dialog)


def test_detail_ignores_ready_signal_for_another_tuf_id():
    app = QApplication.instance() or QApplication([])
    loader = FakeThumbnailLoader()
    dialog = _dialog(_item(17700), loader)
    try:
        # 别的条目的曲绘晚到，不能贴错。
        loader.deliver(17776, _pixmap("#e02020"))
        app.processEvents()
        assert dialog._cover_view.pixmap() is None

        loader.deliver(17700, _pixmap("#2040e0"))
        app.processEvents()
        assert dialog._cover_view.pixmap() is not None
        center = _rendered_center(dialog)
        assert center.blue() > 150 and center.red() < 100
    finally:
        _close(app, dialog)


def test_detail_refresh_entry_loads_new_cover_and_drops_stale():
    """本地谱面绑定 TUF 后不关窗刷新：换请求、旧图不贴、新图正常贴。"""
    app = QApplication.instance() or QApplication([])
    entry = LevelInfo(song="Local", difficulty="P10").to_dict()
    entry.update(
        {
            "key": "folder:c:/levels/local",
            "path": "C:/levels/local/chart.adofai",
            "folder": "C:/levels/local",
        }
    )
    item = ChartItem(level=LevelInfo.from_dict(entry), source="local", entry=entry)
    loader = FakeThumbnailLoader()
    dialog = _dialog(item, loader)
    try:
        assert loader.requested == []

        bound = dict(entry)
        bound.update({"tuf_id": 22222, "tuf_bound": True})
        dialog.refresh_entry(bound)
        assert loader.requested == [22222]

        # 旧编号的迟到结果不贴。
        loader.deliver(11111, _pixmap("#e02020"))
        app.processEvents()
        assert dialog._cover_view.pixmap() is None

        loader.deliver(22222, _pixmap("#2040e0"))
        app.processEvents()
        cover = dialog._cover_view.pixmap()
        assert cover is not None and not cover.isNull()
    finally:
        _close(app, dialog)
