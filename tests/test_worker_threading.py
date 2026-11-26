"""下载线程信号必须回到主线程，否则直接碰 Qt 控件会原生崩溃（0xc0000005）。

PySide6 里把 QThread 内 QObject 的信号连到 lambda 时，lambda 在**发送方线程**
执行；只有连到接收方线程内的 QObject 绑定方法才会按 AutoConnection 排队。
下载过程中界面崩溃就是 ``worker.progress`` / ``worker.stage`` 的 lambda 在
下载线程里跑了 ``_sync_download_ui()``。这些用例走真实 QThread 边界做回归。
"""

import logging
import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ADOFAI_NO_AUTOLOGIN", "1")

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from adofai_publisher.config import Settings
from adofai_publisher.models import LevelInfo
from adofai_publisher.ui import main_window as main_window_module
from adofai_publisher.ui.main_window import MainWindow


class _FakeDownloadWorker(QObject):
    """模拟 ChartDownloadWorker 的信号面，完全不碰网络和磁盘。"""

    log = Signal(str)
    progress = Signal(object)
    stage = Signal(str)
    finished = Signal(dict)
    failed = Signal(str)
    cancelled = Signal(str)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__()
        self.emit_thread = None
        self.mode = str(kwargs.pop("mode", "ok"))
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        self.emit_thread = QThread.currentThread()
        for received in (10, 50, 100):
            self.progress.emit({"received": received, "total": 100})
            self.stage.emit("download")
        if self.mode == "cancel":
            self.cancelled.emit("下载已取消")
            return
        self.finished.emit({"path": "", "level": {}})


class _Marker(QObject):
    hit = Signal()


def _wait_for_thread(thread, timeout: int = 5000) -> None:
    """等后台 QThread 结束，跑真实事件循环，不按固定时长 sleep。"""
    spy = QSignalSpy(thread.finished)
    assert spy.wait(timeout), "后台线程没有在超时前结束"


def _marker_count(spy, timeout: int = 5000) -> int:
    """marker 可能在线程收尾的事件循环里已经发出，先看已有信号再等新的。"""
    if spy.count() == 0:
        spy.wait(timeout)
    return spy.count()


def _make_window(app, tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    window = MainWindow(Settings(tmp_path / "settings.json"))
    task = window._download_queue.add(
        LevelInfo(tuf_id=9001, song="ThreadBoundary", charter="C", difficulty="P10")
    )
    assert window._download_queue.take_next() is task
    return window, task


def _record(calls, name, marker=None):
    def handler(*args):
        calls.append((name, args, QThread.currentThread() is QApplication.instance().thread()))
        if marker is not None and name == "thread_finished":
            marker.hit.emit()

    return handler


def test_download_worker_signals_are_handled_on_ui_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(main_window_module, "ChartDownloadWorker", _FakeDownloadWorker)
    app = QApplication.instance() or QApplication([])
    window, task = _make_window(app, tmp_path, monkeypatch)

    calls: list[tuple[str, tuple, bool]] = []
    marker = _Marker()
    marker_spy = QSignalSpy(marker.hit)
    monkeypatch.setattr(window, "_on_download_task_progress", _record(calls, "progress"))
    monkeypatch.setattr(window, "_on_download_task_stage", _record(calls, "stage"))
    monkeypatch.setattr(window, "_on_download_task_completed", _record(calls, "completed"))
    monkeypatch.setattr(
        window,
        "_on_download_thread_finished",
        _record(calls, "thread_finished", marker),
    )

    window._start_download_task(task)
    thread = window._download_threads[task.task_id]
    worker = window._download_workers[task.task_id]
    _wait_for_thread(thread)
    # 线程结束后，singleShot 排回主线程的回调还需要事件循环再跑一轮。
    assert _marker_count(marker_spy) >= 1, "线程结束回调没有回到主线程"

    # 假 worker 确实在下载线程里发信号，测试覆盖的是真实线程边界。
    assert worker.emit_thread is not app.thread()

    names = [name for name, _args, _main in calls]
    assert names.count("progress") == 3
    assert names.count("stage") == 3
    assert "completed" in names
    assert "thread_finished" in names
    assert all(main for _name, _args, main in calls), [
        (name, main) for name, _args, main in calls if not main
    ]

    progress_received = [
        args[1]["received"] for name, args, _main in calls if name == "progress"
    ]
    assert progress_received == [10, 50, 100]
    stages = [args[1] for name, args, _main in calls if name == "stage"]
    assert stages == ["download", "download", "download"]

    window.close()
    app.processEvents()


def test_download_worker_cancelled_callback_runs_on_ui_thread(tmp_path, monkeypatch):
    class CancellingWorker(_FakeDownloadWorker):
        def __init__(self, *args, **kwargs) -> None:
            kwargs["mode"] = "cancel"
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(main_window_module, "ChartDownloadWorker", CancellingWorker)
    app = QApplication.instance() or QApplication([])
    window, task = _make_window(app, tmp_path, monkeypatch)

    calls: list[tuple[str, tuple, bool]] = []
    marker = _Marker()
    marker_spy = QSignalSpy(marker.hit)
    monkeypatch.setattr(window, "_on_download_task_progress", _record(calls, "progress"))
    monkeypatch.setattr(window, "_on_download_task_stage", _record(calls, "stage"))
    monkeypatch.setattr(window, "_on_download_task_cancelled", _record(calls, "cancelled"))
    monkeypatch.setattr(
        window,
        "_on_download_thread_finished",
        _record(calls, "thread_finished", marker),
    )

    window._start_download_task(task)
    thread = window._download_threads[task.task_id]
    _wait_for_thread(thread)
    assert _marker_count(marker_spy) >= 1, "取消回调没有回到主线程"

    names = [name for name, _args, _main in calls]
    assert "cancelled" in names
    assert "thread_finished" in names
    assert all(main for _name, _args, main in calls)

    window.close()
    app.processEvents()


def test_sync_download_ui_skips_and_warns_off_ui_thread(tmp_path, monkeypatch, caplog):
    app = QApplication.instance() or QApplication([])
    window, _task = _make_window(app, tmp_path, monkeypatch)

    touched: list[str] = []
    monkeypatch.setattr(
        window.library,
        "set_download_tasks",
        lambda *args: touched.append("ui_thread"),
    )
    window._sync_download_ui()  # 主线程照常刷新
    assert touched == ["ui_thread"]

    touched.clear()
    monkeypatch.setattr(
        window.library, "set_download_tasks", lambda *args: touched.append("worker")
    )
    with caplog.at_level(logging.WARNING, logger="adofai"):
        worker_thread = threading.Thread(target=window._sync_download_ui)
        worker_thread.start()
        worker_thread.join(5.0)
    assert not worker_thread.is_alive()

    # 非主线程不碰控件，只留一条 warning，下载逻辑不受影响。
    assert "worker" not in touched
    assert any("非主线程" in record.message for record in caplog.records)

    window.close()
    app.processEvents()
