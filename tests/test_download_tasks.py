import json
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from adofai_publisher.config import Settings
from adofai_publisher.download_tasks import (
    STATUS_CANCELLED,
    STATUS_CANCELLING,
    STATUS_COMPLETED,
    STATUS_DOWNLOADING,
    STATUS_EXTRACTING,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    DownloadQueue,
    DownloadTask,
    load_queue,
    save_queue,
)
from adofai_publisher.level_library import LevelLibraryDB
from adofai_publisher.models import LevelInfo
from adofai_publisher.ui.download_center import DownloadTasksDialog
from adofai_publisher.ui.main_window import MainWindow
from adofai_publisher.workers import ChartDownloadWorker


def _level(tuf_id: int, song: str = "") -> LevelInfo:
    return LevelInfo(
        tuf_id=tuf_id,
        song=song or f"Song {tuf_id}",
        charter="Charter",
        difficulty="P10",
    )


def test_download_queue_limits_concurrency_and_keeps_fifo():
    queue = DownloadQueue(max_active=2)
    first = queue.add(_level(1))
    second = queue.add(_level(2))
    third = queue.add(_level(3))

    assert queue.take_next() is first
    assert queue.take_next() is second
    assert queue.take_next() is None
    assert first.status == STATUS_RUNNING
    assert second.status == STATUS_RUNNING
    assert third.status == STATUS_PENDING

    queue.set_stage(first.task_id, "download")
    queue.update_progress(first.task_id, 50, 200)
    assert first.status == STATUS_DOWNLOADING
    assert first.progress_percent == 25

    queue.mark_completed(first.task_id, "/tmp/level.adofai")
    assert first.status == STATUS_COMPLETED
    assert queue.take_next() is third


def test_download_queue_cancel_and_retry_pending_and_active_tasks():
    queue = DownloadQueue(max_active=1)
    pending = queue.add(_level(11))
    assert queue.request_cancel(pending.task_id) == "pending"
    assert pending.status == STATUS_CANCELLED
    assert queue.retry(pending.task_id) is pending
    assert pending.status == STATUS_PENDING

    active = queue.take_next()
    assert active is pending
    assert queue.request_cancel(active.task_id) == "active"
    assert active.status == STATUS_CANCELLING
    queue.mark_cancelled(active.task_id, "用户取消")
    assert active.status == STATUS_CANCELLED
    assert active.error == "用户取消"


def test_download_tasks_dialog_shows_rows_and_actions(tmp_path):
    app = QApplication.instance() or QApplication([])
    queue = DownloadQueue(max_active=2)
    downloading = queue.add(_level(21, "Downloading"))
    queue.take_next()
    queue.set_stage(downloading.task_id, "download")
    queue.update_progress(downloading.task_id, 4 * 1024 * 1024, 8 * 1024 * 1024)

    failed = queue.add(_level(22, "Failed"))
    queue.take_next()
    queue.mark_failed(failed.task_id, "网络中断")

    completed = queue.add(_level(23, "Completed"))
    queue.mark_completed(completed.task_id, str(tmp_path / "chart.adofai"))

    dialog = DownloadTasksDialog()
    dialog.set_tasks(queue.tasks(), max_active=2)

    assert dialog.table.rowCount() == 3
    assert "1 个进行中" in dialog.summary.text()
    assert "1 个完成" in dialog.summary.text()
    assert "1 个失败" in dialog.summary.text()
    assert "50%" in dialog.table.cellWidget(0, 2).format()
    assert "8.0 MB" in dialog.table.item(0, 3).text()

    def action_text(row: int) -> str:
        widget = dialog.table.cellWidget(row, 4)
        buttons = widget.findChildren(QPushButton)
        return buttons[0].text() if buttons else ""

    assert action_text(0) == "取消"
    assert action_text(1) == "重试"
    assert action_text(2) == "打开目录"

    retried: list[str] = []
    cancelled: list[str] = []
    opened: list[str] = []
    dialog.cancelRequested.connect(cancelled.append)
    dialog.retryRequested.connect(retried.append)
    dialog.openFolderRequested.connect(opened.append)

    dialog.table.cellWidget(0, 4).findChildren(QPushButton)[0].click()
    dialog.table.cellWidget(1, 4).findChildren(QPushButton)[0].click()
    dialog.table.cellWidget(2, 4).findChildren(QPushButton)[0].click()
    app.processEvents()

    assert cancelled == [downloading.task_id]
    assert retried == [failed.task_id]
    assert opened == [completed.task_id]

    dialog.set_theme("light")
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_main_window_queue_reuses_library_entry_and_limits_started_tasks(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    monkeypatch.setenv("ADOFAI_NO_AUTOLOGIN", "1")
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    started: list[str] = []
    monkeypatch.setattr(
        window,
        "_start_download_task",
        lambda task: started.append(task.task_id),
    )

    window._download_library_card(_level(31, "First"))
    window._download_library_card(_level(32, "Second"))
    window._download_library_card(_level(33, "Third"))

    assert len(started) == 2
    assert window._download_queue.active_count == 2
    assert window._download_queue.pending_count == 1
    assert "下载任务 3" in window.library.download_tasks_button.text()

    pending = window._download_queue.pending_tasks()[0]
    window._cancel_download_task(pending.task_id)
    assert pending.status == STATUS_CANCELLED
    window._retry_download_task(pending.task_id)
    assert pending.status == STATUS_PENDING

    window.close()
    assert app is not None


def test_main_window_marks_completed_download_and_refreshes_library(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    monkeypatch.setenv("ADOFAI_NO_AUTOLOGIN", "1")
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    chart = tmp_path / "levels" / "chart.adofai"
    chart.parent.mkdir()
    chart.write_text("{}", encoding="utf-8")
    level = _level(41, "Completed")
    task = window._download_queue.add(level)
    assert window._download_queue.take_next() is task

    window._on_download_task_completed(
        task.task_id,
        {"path": str(chart), "level": level.to_dict()},
    )

    assert task.status == STATUS_COMPLETED
    assert Path(task.path) == chart
    entry = window.library_db.all()[0]
    assert entry["tuf_id"] == 41
    assert entry["path"] == str(chart)
    window.close()
    assert app is not None


def test_import_directory_can_become_default_level_root(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path / "appdata"))
    monkeypatch.setenv("ADOFAI_NO_AUTOLOGIN", "1")
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "appdata" / "settings.json"))
    imported = tmp_path / "DeskBox" / "自定义谱"
    imported.mkdir(parents=True)
    chart = imported / "song.adofai"
    chart.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    window._adopt_import_root(str(imported))

    assert Path(window.settings.get("level_dir")) == imported.resolve()
    assert chart.is_file()
    assert window._level_root() == imported.resolve()
    window.close()
    assert app is not None


def test_download_task_round_trip_keeps_progress_and_metadata_flag():
    queue = DownloadQueue(max_active=2)
    task = queue.add(_level(51, "Round Trip"), refresh_metadata=True)
    queue.take_next()
    queue.set_stage(task.task_id, "download")
    queue.update_progress(task.task_id, 321, 1024)
    task.path = "/tmp/partial.ard"
    task.attempts = 2

    data = task.to_dict()
    assert data["refresh_metadata"] is True
    restored = DownloadTask.from_dict(data)

    assert restored is not None
    assert restored.task_id == task.task_id
    assert restored.refresh_metadata is True
    assert restored.level.tuf_id == 51
    assert restored.received == 321
    assert restored.total == 1024
    assert restored.path == "/tmp/partial.ard"
    assert restored.attempts == 2
    # 中断状态落到磁盘后再读回，一律是等待中。
    assert restored.status == STATUS_PENDING

    terminal_data = dict(data, status=STATUS_FAILED, error="网络中断")
    terminal = DownloadTask.from_dict(terminal_data)
    assert terminal is not None
    assert terminal.status == STATUS_FAILED
    assert terminal.error == "网络中断"

    completed_data = dict(data, status=STATUS_COMPLETED, path="/tmp/final.ard")
    completed = DownloadTask.from_dict(completed_data)
    assert completed is not None and completed.status == STATUS_COMPLETED

    assert DownloadTask.from_dict({"task_id": "missing-level"}) is None
    assert DownloadTask.from_dict(None) is None


def test_download_queue_round_trip_keeps_order_and_skips_bad_entries():
    queue = DownloadQueue(max_active=2)
    first = queue.add(_level(61, "First"))
    second = queue.add(_level(62, "Second"), refresh_metadata=True)
    third = queue.add(_level(63, "Third"))
    queue.take_next()
    queue.take_next()
    queue.mark_completed(first.task_id, "/tmp/first.ard")
    queue.update_progress(second.task_id, 7, 70)

    data = queue.to_dict()
    assert data["version"] == 1
    assert data["order"] == [first.task_id, second.task_id, third.task_id]

    data["tasks"].append({"task_id": "broken"})
    data["tasks"].append(dict(data["tasks"][0]))
    restored = DownloadQueue.from_dict(data)

    assert [task.task_id for task in restored.tasks()] == [
        first.task_id,
        second.task_id,
        third.task_id,
    ]
    assert restored.tasks()[0].status == STATUS_COMPLETED
    assert restored.tasks()[1].status == STATUS_PENDING
    assert restored.tasks()[1].refresh_metadata is True
    assert restored.tasks()[1].received == 7
    assert restored.tasks()[2].status == STATUS_PENDING


def test_interrupted_statuses_load_as_pending_but_keep_progress():
    base = {
        "tuf_id": 71,
        "song": "Song 71",
        "charter": "Charter",
        "difficulty": "P10",
        "path": "/tmp/partial.ard",
        "error": "上一次中断",
        "attempts": 3,
        "received": 5,
        "total": 9,
    }
    for status in (
        STATUS_RUNNING,
        STATUS_DOWNLOADING,
        STATUS_EXTRACTING,
        STATUS_CANCELLING,
    ):
        task = DownloadTask.from_dict(dict(base, status=status))
        assert task is not None
        assert task.status == STATUS_PENDING
        assert task.attempts == 3
        assert (task.received, task.total) == (5, 9)
        assert task.path == "/tmp/partial.ard"
        assert task.error == "上一次中断"

    cancelled = DownloadTask.from_dict(dict(base, status=STATUS_CANCELLED))
    assert cancelled is not None and cancelled.status == STATUS_CANCELLED
    unknown = DownloadTask.from_dict(dict(base, status="something-else"))
    assert unknown is not None and unknown.status == STATUS_PENDING


def test_terminal_tasks_are_capped_at_100_newest_and_keep_pending():
    queue = DownloadQueue(max_active=1)
    for index in range(105):
        task = queue.add(_level(index + 1, f"Song {index + 1}"))
        task.status = STATUS_COMPLETED
        task.finished_at = f"2026-01-01T{index // 60:02d}:{index % 60:02d}:00"
    pending = queue.add(_level(999, "Pending"))

    data = queue.to_dict()
    assert len(data["tasks"]) == 101
    restored = DownloadQueue.from_dict(data)
    tasks = restored.tasks()
    terminal = [task for task in tasks if task.status == STATUS_COMPLETED]
    assert len(terminal) == 100
    # 最旧的 5 条（TUF #1..#5）被裁掉。
    assert terminal[0].level.tuf_id == 6
    assert terminal[-1].level.tuf_id == 105
    assert tasks[-1].task_id == pending.task_id
    assert tasks[-1].status == STATUS_PENDING


def test_save_and_load_queue_round_trip(tmp_path):
    path = tmp_path / "download_queue.json"
    queue = DownloadQueue(max_active=2)
    task = queue.add(_level(81, "Saved"), refresh_metadata=True)
    queue.take_next()
    queue.set_stage(task.task_id, "download")
    queue.update_progress(task.task_id, 10, 40)

    assert save_queue(queue, path) is True
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["order"] == [task.task_id]
    assert raw["tasks"][0]["refresh_metadata"] is True

    restored = load_queue(path)
    assert [item.task_id for item in restored.tasks()] == [task.task_id]
    assert restored.tasks()[0].status == STATUS_PENDING
    assert restored.tasks()[0].received == 10
    assert restored.tasks()[0].total == 40

    assert load_queue(tmp_path / "missing.json").tasks() == []


def test_load_queue_recovers_from_corrupt_file(tmp_path):
    path = tmp_path / "download_queue.json"
    path.write_text("{not valid json", encoding="utf-8")

    queue = load_queue(path)

    assert queue.tasks() == []
    assert not path.exists()
    assert list(tmp_path.glob("download_queue.corrupt-*.json"))


def test_download_tasks_dialog_resume_button_follows_pending_tasks(tmp_path):
    app = QApplication.instance() or QApplication([])
    queue = DownloadQueue(max_active=2)
    dialog = DownloadTasksDialog()

    dialog.set_tasks([], max_active=2)
    assert dialog.resume_button.isHidden()

    completed = queue.add(_level(91, "Completed"))
    completed.status = STATUS_COMPLETED
    dialog.set_tasks(queue.tasks())
    assert dialog.resume_button.isHidden()

    queue.add(_level(92, "Pending"))
    dialog.set_tasks(queue.tasks())

    assert not dialog.resume_button.isHidden()
    assert dialog.resume_button.text() == "继续下载（1）"

    requested: list[bool] = []
    dialog.resumeRequested.connect(lambda: requested.append(True))
    dialog.resume_button.click()
    app.processEvents()
    assert requested == [True]

    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_main_window_persists_inferred_level_root(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path / "appdata"))
    monkeypatch.setenv("ADOFAI_NO_AUTOLOGIN", "1")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    root = tmp_path / "DeskBox" / "自定义谱"
    db = LevelLibraryDB()
    for index, relative in enumerate(
        ("P/P10/One", "P/P15/Two", "G/G2/Three", "U/U5/Four")
    ):
        folder = root / relative
        folder.mkdir(parents=True)
        chart = folder / "level.adofai"
        chart.write_text("{}", encoding="utf-8")
        db.upsert(
            LevelInfo(
                song=f"Song {index}",
                difficulty=relative.split("/")[1],
            ),
            chart,
            {
                "source": "scan",
                "folder": str(folder),
                "category": relative[0],
                "band": relative[:3],
            },
        )
    db.save()
    app = QApplication.instance() or QApplication([])

    window = MainWindow(Settings(tmp_path / "appdata" / "settings.json"))

    assert Path(window.settings.get("level_dir")) == root.resolve()
    assert window._level_root() == root.resolve()
    window.close()
    assert app is not None


def _saved_task_status(tmp_path: Path, task_id: str) -> str:
    data = json.loads((tmp_path / "download_queue.json").read_text(encoding="utf-8"))
    for raw in data["tasks"]:
        if raw.get("task_id") == task_id:
            return str(raw.get("status"))
    raise AssertionError(f"队列文件里没有任务 {task_id}")


def test_closing_window_ignores_cancelled_and_failed_callbacks(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    monkeypatch.setenv("ADOFAI_NO_AUTOLOGIN", "1")
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    task = window._download_queue.add(_level(801, "Closing"))
    assert window._download_queue.take_next() is task
    window._save_download_queue()

    # 关闭流程中 worker.cancel() 产生的回声：不能改状态、不能写 cancelled。
    window._closing = True
    window._on_download_task_cancelled(task.task_id, "下载已取消")
    window._on_download_task_failed(task.task_id, "线程已停止")

    assert task.status == STATUS_RUNNING
    assert _saved_task_status(tmp_path, task.task_id) == STATUS_RUNNING

    window._closing = False
    window.close()
    assert app is not None


def test_user_cancel_callback_still_marks_cancelled_when_not_closing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    monkeypatch.setenv("ADOFAI_NO_AUTOLOGIN", "1")
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    task = window._download_queue.add(_level(802, "User Cancel"))
    assert window._download_queue.take_next() is task
    pumped: list[bool] = []
    monkeypatch.setattr(window, "_pump_download_queue", lambda: pumped.append(True))

    assert window._closing is False
    window._on_download_task_cancelled(task.task_id, "用户取消")

    assert task.status == STATUS_CANCELLED
    assert _saved_task_status(tmp_path, task.task_id) == STATUS_CANCELLED
    assert pumped == [True]
    window.close()
    assert app is not None


def test_close_event_keeps_interrupted_task_pending_after_late_cancel(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    monkeypatch.setenv("ADOFAI_NO_AUTOLOGIN", "1")
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    task = window._download_queue.add(_level(803, "Interrupted"))
    assert window._download_queue.take_next() is task
    window._download_queue.update_progress(task.task_id, 40, 100)
    # 没有真实 worker/线程，closeEvent 不会弹「任务进行中」。
    assert not window._jobs

    window.close()
    app.processEvents()

    assert window._closing is True
    assert task.status == STATUS_PENDING
    assert _saved_task_status(tmp_path, task.task_id) == STATUS_PENDING

    # closeEvent 收尾后才轮到的取消回调，不能把 pending 覆盖成 cancelled。
    window._on_download_task_cancelled(task.task_id, "下载已取消")
    assert task.status == STATUS_PENDING
    assert _saved_task_status(tmp_path, task.task_id) == STATUS_PENDING
    assert app is not None


def test_declining_close_restores_cancelled_callbacks(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    monkeypatch.setenv("ADOFAI_NO_AUTOLOGIN", "1")
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    task = window._download_queue.add(_level(806, "Stay Open"))
    assert window._download_queue.take_next() is task
    asked: list[bool] = []

    def decline(*args, **kwargs):
        asked.append(True)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", decline)
    window._busy = True  # 触发「任务进行中」确认框

    window.close()
    app.processEvents()

    # 用户选择不退出：关闭标志必须复位，否则之后的取消回调会被永久吞掉。
    assert asked == [True]
    assert window._closing is False
    window._busy = False
    monkeypatch.setattr(window, "_pump_download_queue", lambda: None)
    window._on_download_task_cancelled(task.task_id, "用户取消")
    assert task.status == STATUS_CANCELLED

    window.deleteLater()
    app.processEvents()
    assert app is not None


def test_completed_during_close_is_kept_without_starting_next_task(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    monkeypatch.setenv("ADOFAI_NO_AUTOLOGIN", "1")
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    level = _level(804, "Finished Just In Time")
    task = window._download_queue.add(level)
    assert window._download_queue.take_next() is task
    queued = window._download_queue.add(_level(805, "Still Waiting"))
    chart = tmp_path / "levels" / "chart.adofai"
    chart.parent.mkdir()
    chart.write_text("{}", encoding="utf-8")
    pumped: list[bool] = []
    monkeypatch.setattr(window, "_pump_download_queue", lambda: pumped.append(True))

    window._closing = True
    window._on_download_task_completed(
        task.task_id,
        {"path": str(chart), "level": level.to_dict()},
    )

    assert task.status == STATUS_COMPLETED
    assert queued.status == STATUS_PENDING
    assert _saved_task_status(tmp_path, task.task_id) == STATUS_COMPLETED
    assert pumped == []
    window.close()
    assert app is not None

def _write_chart(path: Path, angles=(0, 90, 999, 0), bpm=180, duration=90000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "angleData": list(angles),
                "settings": {
                    "song": "Local Song",
                    "artist": "Local Artist",
                    "author": "Local Charter",
                    "bpm": bpm,
                    "songDuration": duration,
                },
            }
        ),
        encoding="utf-8",
    )


def test_chart_download_worker_reports_local_numbers(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    chart = tmp_path / "P" / "P10" / "Song" / "level.adofai"
    _write_chart(chart)

    class FakeLookup:
        def __init__(self, timeout: float = 0) -> None:
            pass

        def download_level(self, *args, **kwargs):
            return chart

    monkeypatch.setattr("adofai_publisher.workers.LevelLookup", FakeLookup)
    level = LevelInfo(
        tuf_id=9001,
        song="TUF Song",
        artist="TUF Artist",
        charter="TUF Charter",
        difficulty="P10",
        tilecount=2,
        midspin_count=1,
        auto_tile_count=0,
    )
    worker = ChartDownloadWorker(level, str(tmp_path), "", refresh_metadata=False)
    payloads: list[dict] = []
    worker.finished.connect(payloads.append)
    worker.run()

    assert len(payloads) == 1
    data = payloads[0]["level"]
    assert payloads[0]["path"] == str(chart)
    assert payloads[0]["parse_error"] == ""
    assert (data["tilecount"], data["midspin_count"], data["auto_tile_count"]) == (4, 1, 0)
    assert data["playable_hits"] == 3
    assert data["bpm"] == 180
    assert data["duration_ms"] == 90000
    # TUF 元数据原样保留。
    assert data["song"] == "TUF Song"
    assert data["artist"] == "TUF Artist"
    assert data["charter"] == "TUF Charter"
    assert data["difficulty"] == "P10"
    assert app is not None


def test_chart_download_worker_keeps_tuf_numbers_when_parse_fails(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    chart = tmp_path / "P" / "P10" / "Broken" / "level.adofai"
    chart.parent.mkdir(parents=True)
    chart.write_text("{not-json", encoding="utf-8")

    class FakeLookup:
        def __init__(self, timeout: float = 0) -> None:
            pass

        def download_level(self, *args, **kwargs):
            return chart

    monkeypatch.setattr("adofai_publisher.workers.LevelLookup", FakeLookup)
    level = LevelInfo(
        tuf_id=9002,
        song="TUF Song",
        difficulty="P10",
        tilecount=100,
        midspin_count=5,
        auto_tile_count=2,
    )
    worker = ChartDownloadWorker(level, str(tmp_path), "", refresh_metadata=False)
    payloads: list[dict] = []
    worker.finished.connect(payloads.append)
    worker.run()

    data = payloads[0]["level"]
    assert (data["tilecount"], data["midspin_count"], data["auto_tile_count"]) == (100, 5, 2)
    assert data["playable_hits"] is None
    assert payloads[0]["parse_error"]
    assert app is not None


def test_on_chart_downloaded_merges_local_numbers_with_tuf_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    monkeypatch.setenv("ADOFAI_NO_AUTOLOGIN", "1")
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    chart = tmp_path / "levels" / "P" / "P15" / "Song" / "level.adofai"
    _write_chart(chart, angles=(0, 90, 180, 999, 0))
    level = LevelInfo(
        tuf_id=9003,
        song="TUF Song",
        artist="TUF Artist",
        charter="TUF Charter",
        vfxers=["TUF VFX"],
        difficulty="P15",
        tilecount=4,
        midspin_count=1,
        auto_tile_count=0,
        base_score=12.5,
        tags=[{"id": 1, "name": "tech"}],
        clears=12,
        raw={"id": 9003},
    )
    data = level.to_dict()
    data.update(
        {
            "tilecount": 5,
            "midspin_count": 1,
            "auto_tile_count": 0,
            "playable_hits": 4,
            "bpm": 180,
            "duration_ms": 90000,
        }
    )

    window._on_chart_downloaded({"path": str(chart), "level": data})

    entry = window.library_db.all()[0]
    assert entry["tuf_id"] == 9003
    assert entry["song"] == "TUF Song"
    assert entry["artist"] == "TUF Artist"
    assert entry["charter"] == "TUF Charter"
    assert entry["vfxers"] == ["TUF VFX"]
    assert entry["difficulty"] == "P15"
    assert entry["base_score"] == 12.5
    assert entry["tags"] == [{"id": 1, "name": "tech"}]
    assert entry["raw"] == {"id": 9003}
    assert (entry["tilecount"], entry["midspin_count"], entry["playable_hits"]) == (5, 1, 4)
    assert entry["bpm"] == 180
    window.close()
    app.processEvents()
