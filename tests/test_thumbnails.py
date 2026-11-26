"""曲绘缩略图加载器与卡片绘制的测试（全部离线，fetch 均注入）。"""

from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice, QRect, QThreadPool
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem

from adofai_publisher.models import LevelInfo
from adofai_publisher.thumbnails import ThumbnailLoader
from adofai_publisher.ui.chart_library import (
    ChartCardDelegate,
    ChartItem,
    ChartLibraryPage,
    ChartListModel,
)
from adofai_publisher.ui.widgets import LevelPickerDialog


def _png_bytes(width: int = 16, height: int = 16) -> bytes:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor("#e02020"))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def _make_loader(tmp_path, fetch):
    pool = QThreadPool()
    pool.setMaxThreadCount(2)
    loader = ThumbnailLoader(fetch=fetch, cache_dir=tmp_path / "thumbs", pool=pool)
    return loader, pool


def _wait_loaded(loader: ThumbnailLoader, tuf_id: int, timeout: float = 5.0) -> None:
    """QSignalSpy.wait 收不到跨线程排队信号，这里手动跑主线程事件循环。"""
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app is not None:
            app.processEvents()
        if loader.is_loaded(tuf_id):
            return
        time.sleep(0.01)
    raise AssertionError(f"缩略图 {tuf_id} 没有在 {timeout}s 内加载完成")


def test_loader_builds_pixmap_and_writes_disk_cache(tmp_path):
    app = QApplication.instance() or QApplication([])
    calls: list[int] = []

    def fetch(tuf_id: int) -> bytes:
        calls.append(tuf_id)
        return _png_bytes()

    loader, pool = _make_loader(tmp_path, fetch)
    loader.request(17700)
    _wait_loaded(loader, 17700)
    pool.waitForDone(5000)

    assert calls == [17700]
    pixmap = loader.pixmap(17700)
    assert pixmap is not None and not pixmap.isNull()
    assert loader.is_loaded(17700)
    assert (tmp_path / "thumbs" / "17700.img").is_file()


def test_loader_negative_caches_204_and_skips_repeat_requests(tmp_path):
    app = QApplication.instance() or QApplication([])
    calls: list[int] = []

    def fetch(tuf_id: int) -> None:
        calls.append(tuf_id)
        return None

    loader, pool = _make_loader(tmp_path, fetch)
    loader.request(17776)
    _wait_loaded(loader, 17776)

    loader.request(17776)
    loader.request(17776)
    app.processEvents()
    pool.waitForDone(5000)

    assert calls == [17776]
    assert loader.is_loaded(17776)
    assert loader.pixmap(17776) is None
    assert not (tmp_path / "thumbs" / "17776.img").exists()


def test_loader_negative_caches_fetch_errors(tmp_path):
    app = QApplication.instance() or QApplication([])
    calls: list[int] = []

    def fetch(tuf_id: int) -> bytes:
        calls.append(tuf_id)
        raise RuntimeError("network down")

    loader, pool = _make_loader(tmp_path, fetch)
    loader.request(9)
    _wait_loaded(loader, 9)
    pool.waitForDone(5000)

    assert calls == [9]
    assert loader.is_loaded(9)
    assert loader.pixmap(9) is None


def test_loader_dedupes_same_id_in_flight(tmp_path):
    app = QApplication.instance() or QApplication([])
    calls: list[int] = []
    started = threading.Event()
    gate = threading.Event()

    def fetch(tuf_id: int) -> bytes:
        calls.append(tuf_id)
        started.set()
        gate.wait(5)
        return _png_bytes()

    loader, pool = _make_loader(tmp_path, fetch)
    for _ in range(6):
        loader.request(17700)
    assert started.wait(5)
    for _ in range(6):
        loader.request(17700)
    assert calls == [17700]
    gate.set()
    _wait_loaded(loader, 17700)
    pool.waitForDone(5000)
    assert calls == [17700]


def test_loader_reads_disk_cache_before_fetching(tmp_path):
    app = QApplication.instance() or QApplication([])
    cache_dir = tmp_path / "thumbs"
    cache_dir.mkdir()
    (cache_dir / "17700.img").write_bytes(_png_bytes())
    calls: list[int] = []

    def fetch(tuf_id: int) -> bytes:
        calls.append(tuf_id)
        raise AssertionError("不应联网")

    loader, pool = _make_loader(tmp_path, fetch)
    loader.request(17700)
    _wait_loaded(loader, 17700)
    pool.waitForDone(5000)

    assert calls == []
    assert loader.pixmap(17700) is not None


def test_loader_downscales_large_images_before_caching(tmp_path):
    app = QApplication.instance() or QApplication([])
    loader, pool = _make_loader(tmp_path, lambda _tid: _png_bytes(800, 420))
    loader.request(17700)
    _wait_loaded(loader, 17700)
    pool.waitForDone(5000)

    pixmap = loader.pixmap(17700)
    assert pixmap is not None
    assert max(pixmap.width(), pixmap.height()) <= 160
    app.processEvents()


def test_loader_removes_corrupt_disk_cache(tmp_path):
    app = QApplication.instance() or QApplication([])
    cache_dir = tmp_path / "thumbs"
    cache_dir.mkdir()
    corrupt = cache_dir / "17700.img"
    corrupt.write_bytes(b"not an image")

    def fetch(_tid: int) -> bytes:
        raise AssertionError("不应联网")

    loader, pool = _make_loader(tmp_path, fetch)
    loader.request(17700)
    _wait_loaded(loader, 17700)
    pool.waitForDone(5000)

    assert loader.pixmap(17700) is None
    assert not corrupt.exists()
    app.processEvents()


class FakeLoader:
    """记录 request 调用，返回固定图片；用于验证 delegate 的绘制分支。"""

    def __init__(self, image: QPixmap | None = None) -> None:
        self.image = image
        self.requested: list[int] = []

    def request(self, tuf_id: int) -> None:
        self.requested.append(int(tuf_id))

    def pixmap(self, tuf_id: int) -> QPixmap | None:
        return self.image


def _model_for(ids: list[int], source: str = "tuf") -> ChartListModel:
    model = ChartListModel()
    model.set_items(
        [
            ChartItem(
                level=LevelInfo(tuf_id=tuf_id, song=f"Song {tuf_id}", difficulty="P1"),
                source=source,
            )
            for tuf_id in ids
        ]
    )
    return model


def _paint(delegate: ChartCardDelegate, model: ChartListModel, row: int) -> QImage:
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 600, 116)
    canvas = QPixmap(600, 116)
    canvas.fill(QColor("#101010"))
    painter = QPainter(canvas)
    delegate.paint(painter, option, model.index(row, 0))
    painter.end()
    return canvas.toImage()


def test_delegate_does_not_request_thumbnails_by_default():
    """曲绘已从卡片撤下：默认不请求加载器，也不画缩略图。"""
    app = QApplication.instance() or QApplication([])
    image = QPixmap(80, 80)
    image.fill(QColor("#e02020"))
    loader = FakeLoader(image)
    delegate = ChartCardDelegate()
    delegate.set_thumbnail_loader(loader)  # type: ignore[arg-type]
    model = _model_for([17700])

    rendered = _paint(delegate, model, 0)

    assert ChartCardDelegate.show_thumbnails is False
    assert delegate.show_thumbnails is False
    assert loader.requested == []
    # 卡片左侧照旧是难度图标，不是红色曲绘。
    center = rendered.pixelColor(44, 58)
    assert not (center.red() > 150 and center.green() < 100)
    app.processEvents()


def test_delegate_requests_only_the_painted_row():
    app = QApplication.instance() or QApplication([])
    loader = FakeLoader()
    delegate = ChartCardDelegate()
    delegate.set_thumbnail_loader(loader)  # type: ignore[arg-type]
    delegate.set_thumbnails_enabled(True)
    model = _model_for([11, 12, 13])

    _paint(delegate, model, 1)

    assert loader.requested == [12]
    app.processEvents()


def test_delegate_draws_thumbnail_when_cached():
    app = QApplication.instance() or QApplication([])
    image = QPixmap(80, 80)
    image.fill(QColor("#e02020"))
    loader = FakeLoader(image)
    delegate = ChartCardDelegate()
    delegate.set_thumbnail_loader(loader)  # type: ignore[arg-type]
    delegate.set_thumbnails_enabled(True)
    model = _model_for([17700])

    rendered = _paint(delegate, model, 0)

    assert loader.requested == [17700]
    # 缩略图槽中心（卡片左侧 14px 起、宽 46px）应是图片的红色。
    center = rendered.pixelColor(44, 58)
    assert center.red() > 150 and center.green() < 100
    app.processEvents()


def test_delegate_falls_back_to_placeholder_without_cache():
    app = QApplication.instance() or QApplication([])
    loader = FakeLoader(None)
    delegate = ChartCardDelegate()
    delegate.set_thumbnail_loader(loader)  # type: ignore[arg-type]
    delegate.set_thumbnails_enabled(True)
    model = _model_for([17776])

    rendered = _paint(delegate, model, 0)

    assert loader.requested == [17776]
    assert not rendered.isNull()
    center = rendered.pixelColor(44, 58)
    assert not (center.red() > 150 and center.green() < 100)


def test_delegate_ignores_local_cards_for_thumbnails():
    app = QApplication.instance() or QApplication([])
    loader = FakeLoader(QPixmap(80, 80))
    delegate = ChartCardDelegate()
    delegate.set_thumbnail_loader(loader)  # type: ignore[arg-type]
    delegate.set_thumbnails_enabled(True)
    model = _model_for([5], source="local")

    _paint(delegate, model, 0)

    assert loader.requested == []
    app.processEvents()


def test_chart_library_wires_shared_loader_to_online_cards():
    app = QApplication.instance() or QApplication([])
    page = ChartLibraryPage()
    try:
        # 接口仍然接着，只是默认不启用绘制。
        assert page.online_delegate.thumbnail_loader is page.thumbnail_loader
        assert page.local_delegate.thumbnail_loader is None
        assert page.online_delegate.show_thumbnails is False
    finally:
        page.deleteLater()
        app.processEvents()


def test_level_picker_shows_badges_without_fetching_thumbnails(tmp_path):
    app = QApplication.instance() or QApplication([])
    calls: list[int] = []

    def fetch(tuf_id: int) -> bytes:
        calls.append(tuf_id)
        return _png_bytes()

    loader, pool = _make_loader(tmp_path, fetch)
    dialog = LevelPickerDialog(
        [LevelInfo(tuf_id=17700, song="Alpha", difficulty="P15")],
        thumbnail_loader=loader,
    )
    try:
        dialog.show()
        app.processEvents()
        time.sleep(0.2)
        app.processEvents()
        pool.waitForDone(5000)

        assert calls == []
        assert dialog.delegate.show_thumbnails is False
        assert dialog.delegate.thumbnail_loader is loader
        assert not dialog.list.viewport().grab().isNull()
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_level_picker_can_still_load_thumbnails_when_enabled(tmp_path):
    app = QApplication.instance() or QApplication([])
    calls: list[int] = []

    def fetch(tuf_id: int) -> bytes:
        calls.append(tuf_id)
        return _png_bytes()

    loader, pool = _make_loader(tmp_path, fetch)
    dialog = LevelPickerDialog(
        [LevelInfo(tuf_id=17700, song="Alpha", difficulty="P15")],
        thumbnail_loader=loader,
    )
    try:
        dialog.delegate.set_thumbnails_enabled(True)
        dialog.show()
        app.processEvents()
        _wait_loaded(loader, 17700)
        app.processEvents()
        pool.waitForDone(5000)

        assert calls == [17700]
        assert loader.pixmap(17700) is not None
        assert dialog.delegate.thumbnail_loader is loader
        assert not dialog.list.viewport().grab().isNull()
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()
