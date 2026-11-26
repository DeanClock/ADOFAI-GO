"""TUF 在线谱面曲绘缩略图。

接口 ``GET /v2/media/thumbnail/level/{id}`` 是公开的：有图返回 200 + 图片，
无图返回 204。缩略图只用于在线列表与选谱弹窗展示，不写入本地谱面库。

加载全部在后台线程完成，同一个编号在内存里只请求一次；失败与 204 都做负缓存，
避免滚动时反复打接口。QPixmap 只能在主线程创建，所以工作线程只回传图片字节，
由主线程解码并发出 :attr:`ThumbnailLoader.thumbnailReady` 让视图重绘对应行。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import requests
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtGui import QGuiApplication, QPixmap

THUMBNAIL_URL = "https://api.tuforums.com/v2/media/thumbnail/level/{tuf_id}"
THUMBNAIL_TIMEOUT = (4.0, 6.0)
MAX_THUMBNAIL_BYTES = 8 * 1024 * 1024
# 接口返回 800×420 的整张卡片；内存里留 160px 足够 60px 缩略图与 2x 屏使用。
MAX_CACHED_SIDE = 160

FetchFunc = Callable[[int], "bytes | None"]

_DEFAULT_HEADERS = {
    "User-Agent": "ADOFAI-GO thumbnail loader",
    "Accept": "image/*",
}


def _remote_requests_allowed() -> bool:
    """离屏（测试 / 无头）环境不真的联网；真正需要时由调用方注入 fetch。"""
    if os.environ.get("ADOFAI_THUMBNAILS") == "0":
        return False
    app = QGuiApplication.instance()
    return app is not None and app.platformName() != "offscreen"


def fetch_thumbnail_bytes(tuf_id: int) -> bytes | None:
    """请求一张曲绘；204、非 200、超大响应或异常都返回 None。"""
    if not _remote_requests_allowed():
        return None
    try:
        response = requests.get(
            THUMBNAIL_URL.format(tuf_id=int(tuf_id)),
            headers=_DEFAULT_HEADERS,
            timeout=THUMBNAIL_TIMEOUT,
        )
        try:
            if response.status_code != 200:
                return None
            data = response.content
        finally:
            response.close()
    except (requests.RequestException, OSError, ValueError):
        return None
    if not data or len(data) > MAX_THUMBNAIL_BYTES:
        return None
    return bytes(data)


class _ThumbnailSignals(QObject):
    finished = Signal(int, object)


class _ThumbnailTask(QRunnable):
    """后台取图：先读磁盘缓存，未命中再请求；写盘失败忽略。"""

    def __init__(
        self,
        tuf_id: int,
        fetch: FetchFunc,
        signals: _ThumbnailSignals,
        cache_path: Path | None,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._tuf_id = int(tuf_id)
        self._fetch = fetch
        self._signals = signals
        self._cache_path = cache_path

    def run(self) -> None:  # noqa: D102 - QRunnable
        data: bytes | None = None
        if self._cache_path is not None:
            try:
                if self._cache_path.is_file():
                    data = self._cache_path.read_bytes() or None
            except OSError:
                data = None
        if data is None:
            try:
                data = self._fetch(self._tuf_id)
            except Exception:  # 网络、解码等任何失败都按“无图”处理
                data = None
        if not isinstance(data, (bytes, bytearray)) or not data:
            data = None
        else:
            data = bytes(data)
            if self._cache_path is not None:
                try:
                    self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                    self._cache_path.write_bytes(data)
                except OSError:
                    pass
        try:
            self._signals.finished.emit(self._tuf_id, data)
        except RuntimeError:
            # loader 已经在任务结束前被销毁，丢弃结果即可。
            pass


class ThumbnailLoader(QObject):
    """按 TUF 编号异步加载曲绘，主线程内缓存 QPixmap。

    ``pixmap()`` 返回 ``None`` 有两种含义：还没加载完，或确认无图；用
    ``is_loaded()`` 区分。负缓存后不会再次请求。
    """

    thumbnailReady = Signal(int)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        fetch: FetchFunc | None = None,
        cache_dir: Path | str | None = None,
        pool: QThreadPool | None = None,
    ) -> None:
        super().__init__(parent)
        self._fetch: FetchFunc = fetch or fetch_thumbnail_bytes
        self._cache: dict[int, QPixmap | None] = {}
        self._pending: set[int] = set()
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._pool = pool or QThreadPool.globalInstance()
        self._signals = _ThumbnailSignals(self)
        self._signals.finished.connect(self._on_finished)

    # -- 查询 ---------------------------------------------------------------

    def is_loaded(self, tuf_id: int) -> bool:
        return int(tuf_id) in self._cache

    def pixmap(self, tuf_id: int) -> QPixmap | None:
        return self._cache.get(int(tuf_id))

    # -- 请求 ---------------------------------------------------------------

    def request(self, tuf_id: int) -> None:
        """请求缩略图；已缓存或已在途时立即返回，不阻塞 UI 线程。"""
        key = int(tuf_id)
        if key <= 0 or key in self._cache or key in self._pending:
            return
        self._pending.add(key)
        cache_path = self._cache_dir / f"{key}.img" if self._cache_dir else None
        self._pool.start(_ThumbnailTask(key, self._fetch, self._signals, cache_path))

    def _on_finished(self, tuf_id: int, data: Any) -> None:
        key = int(tuf_id)
        self._pending.discard(key)
        pixmap: QPixmap | None = None
        if isinstance(data, (bytes, bytearray)) and data:
            loaded = QPixmap()
            if loaded.loadFromData(bytes(data)):
                if loaded.width() > MAX_CACHED_SIDE or loaded.height() > MAX_CACHED_SIDE:
                    loaded = loaded.scaled(
                        MAX_CACHED_SIDE,
                        MAX_CACHED_SIDE,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                pixmap = loaded
        if pixmap is None and self._cache_dir is not None:
            # 磁盘缓存损坏时删掉，避免下次启动继续读到同一份坏图。
            try:
                (self._cache_dir / f"{key}.img").unlink(missing_ok=True)
            except OSError:
                pass
        self._cache[key] = pixmap
        self.thumbnailReady.emit(key)


_SHARED_LOADER: ThumbnailLoader | None = None


def shared_thumbnail_loader() -> ThumbnailLoader:
    """进程内共享的加载器：谱面库与选谱弹窗共用缓存，避免重复请求。"""
    global _SHARED_LOADER
    if _SHARED_LOADER is None:
        from .config import thumbnail_cache_dir

        _SHARED_LOADER = ThumbnailLoader(cache_dir=thumbnail_cache_dir())
    return _SHARED_LOADER


def clear_shared_thumbnail_loader() -> None:
    """测试用：丢弃共享实例，避免缓存跨用例串味。"""
    global _SHARED_LOADER
    _SHARED_LOADER = None
