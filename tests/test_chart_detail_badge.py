"""谱面详情弹窗难度徽章的测试（全部离线，曲绘加载器均注入假实现）。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QFont, QFontMetrics, QPixmap
from PySide6.QtWidgets import QApplication

from adofai_publisher.models import LevelInfo
from adofai_publisher.ui.chart_library import (
    DETAIL_BADGE_SIZE,
    DETAIL_BADGE_TEXT_WIDTH,
    ChartDetailDialog,
    ChartItem,
)


class _FakeThumbnailLoader(QObject):
    """只记录请求的假曲绘加载器；徽章测试不关心图片本身。"""

    thumbnailReady = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.requested: list[int] = []

    def request(self, tuf_id: int) -> None:
        self.requested.append(int(tuf_id))

    def pixmap(self, tuf_id: int) -> QPixmap | None:
        return None

    def is_loaded(self, tuf_id: int) -> bool:
        return False


def _tuf_item(difficulty: str, tuf_id: int | None = 17700) -> ChartItem:
    return ChartItem(
        level=LevelInfo(tuf_id=tuf_id, song="Sinkhole", difficulty=difficulty),
        source="tuf",
    )


def _local_item(**entry_overrides) -> ChartItem:
    entry = LevelInfo(song="Local", difficulty="").to_dict()
    entry.update(
        {
            "key": "folder:c:/levels/local",
            "path": "C:/levels/local/chart.adofai",
            "folder": "C:/levels/local",
        }
    )
    entry.update(entry_overrides)
    return ChartItem(level=LevelInfo.from_dict(entry), source="local", entry=entry)


def _dialog(
    item: ChartItem, loader: _FakeThumbnailLoader | None = None
) -> ChartDetailDialog:
    return ChartDetailDialog(
        item, {}, [], None, thumbnail_loader=loader or _FakeThumbnailLoader()
    )


def _close(app: QApplication, dialog: ChartDetailDialog) -> None:
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def _small_badge_font() -> QFont:
    """与实现一致的 12px 度量字体，用来验证兜底文本真的装得下。"""
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(12)
    font.setBold(True)
    return font


def test_badge_shows_short_code_and_keeps_full_text_in_tooltip():
    app = QApplication.instance() or QApplication([])
    for difficulty, expected in [
        ("GQ1 (G5~G8)", "GQ1"),
        ("G1 (G5~G8)", "G1"),
        ("GQ1（G5~G8）", "GQ1"),
    ]:
        dialog = _dialog(_tuf_item(difficulty))
        try:
            assert dialog._badge_label.text() == expected
            # 完整难度原文不丢，只是挪进 tooltip。
            assert dialog._badge_label.toolTip() == difficulty
            assert "font-size: 17px" in dialog._badge_label.styleSheet()
            assert dialog._badge_label.size().width() == DETAIL_BADGE_SIZE
        finally:
            _close(app, dialog)


def test_badge_keeps_plain_code():
    app = QApplication.instance() or QApplication([])
    dialog = _dialog(_tuf_item("P10"))
    try:
        assert dialog._badge_label.text() == "P10"
        assert dialog._badge_label.toolTip() == "P10"
        assert "font-size: 17px" in dialog._badge_label.styleSheet()
    finally:
        _close(app, dialog)


def test_badge_shrinks_and_elides_long_text():
    app = QApplication.instance() or QApplication([])
    dialog = _dialog(_tuf_item("Unranked"))
    try:
        label = dialog._badge_label
        assert "font-size: 12px" in label.styleSheet()
        # 66px 放不下整串：12px 下仍超宽，走 ElideRight 兜底。
        assert label.text() != "Unranked"
        assert label.text().endswith("…")
        metrics = QFontMetrics(_small_badge_font())
        assert metrics.horizontalAdvance(label.text()) <= DETAIL_BADGE_TEXT_WIDTH
        assert label.size().width() == DETAIL_BADGE_SIZE
        assert label.size().height() == DETAIL_BADGE_SIZE
        assert label.toolTip() == "Unranked"
    finally:
        _close(app, dialog)


def test_badge_local_unbound_still_shows_fallback():
    app = QApplication.instance() or QApplication([])
    dialog = _dialog(_local_item())
    try:
        assert dialog._badge_label.text() == "未匹配"
        assert "font-size: 17px" in dialog._badge_label.styleSheet()
    finally:
        _close(app, dialog)


def test_badge_local_uses_band_without_breaking_fallback():
    app = QApplication.instance() or QApplication([])
    dialog = _dialog(_local_item(band="P10", tuf_id=15269, tuf_bound=True))
    try:
        assert dialog._badge_label.text() == "P10"
        assert dialog._badge_label.toolTip() == "P10"
    finally:
        _close(app, dialog)


def test_badge_short_code_does_not_disturb_cover_request():
    app = QApplication.instance() or QApplication([])
    loader = _FakeThumbnailLoader()
    dialog = _dialog(_tuf_item("GQ1 (G5~G8)", tuf_id=17700), loader)
    try:
        # 有 tuf_id 时曲绘请求照发，占位框照旧。
        assert loader.requested == [17700]
        assert dialog._cover_view.pixmap() is None
        assert dialog._cover_view.placeholder_text() == "加载中…"
        assert dialog._badge_label.text() == "GQ1"
    finally:
        _close(app, dialog)
