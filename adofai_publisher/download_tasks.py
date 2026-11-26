from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .config import base_dir
from .models import LevelInfo

logger = logging.getLogger(__name__)

QUEUE_VERSION = 1
QUEUE_FILE_NAME = "download_queue.json"
TERMINAL_RETENTION_LIMIT = 100

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DOWNLOADING = "downloading"
STATUS_EXTRACTING = "extracting"
STATUS_CANCELLING = "cancelling"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

ACTIVE_STATUSES = frozenset(
    {
        STATUS_RUNNING,
        STATUS_DOWNLOADING,
        STATUS_EXTRACTING,
        STATUS_CANCELLING,
    }
)
PENDING_STATUSES = frozenset({STATUS_PENDING})
UNFINISHED_STATUSES = ACTIVE_STATUSES | PENDING_STATUSES
TERMINAL_STATUSES = frozenset(
    {
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_CANCELLED,
    }
)

STATUS_LABELS = {
    STATUS_PENDING: "等待中",
    STATUS_RUNNING: "准备中",
    STATUS_DOWNLOADING: "下载中",
    STATUS_EXTRACTING: "解压中",
    STATUS_CANCELLING: "正在取消",
    STATUS_COMPLETED: "已完成",
    STATUS_FAILED: "失败",
    STATUS_CANCELLED: "已取消",
}
ALL_STATUSES = frozenset(STATUS_LABELS)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def format_bytes(size: int) -> str:
    value = float(max(0, int(size or 0)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


@dataclass
class DownloadTask:
    level: LevelInfo
    refresh_metadata: bool = False
    task_id: str = ""
    status: str = STATUS_PENDING
    received: int = 0
    total: int = 0
    path: str = ""
    error: str = ""
    attempts: int = 0
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""

    def __post_init__(self) -> None:
        if not self.task_id:
            self.task_id = uuid.uuid4().hex
        if not self.created_at:
            self.created_at = _now()

    @property
    def tuf_id(self) -> int | None:
        return self.level.tuf_id

    @property
    def title(self) -> str:
        return self.level.song or f"TUF #{self.level.tuf_id or '?'}"

    @property
    def progress_percent(self) -> int | None:
        if self.total <= 0:
            return None
        return max(0, min(100, round(self.received * 100 / self.total)))

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def is_unfinished(self) -> bool:
        return self.status in UNFINISHED_STATUSES

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    def to_dict(self) -> dict[str, Any]:
        data = self.level.to_dict()
        data.update(
            {
                "task_id": self.task_id,
                "refresh_metadata": self.refresh_metadata,
                "status": self.status,
                "received": self.received,
                "total": self.total,
                "path": self.path,
                "error": self.error,
                "attempts": self.attempts,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }
        )
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "DownloadTask | None":
        """还原持久化的任务；缺谱面（tuf_id）的坏数据返回 None，缺 task_id 时重新生成。"""
        if not isinstance(data, dict):
            return None
        nested = data.get("level")
        level_data = nested if isinstance(nested, dict) else data
        try:
            level = LevelInfo.from_dict(level_data)
        except (TypeError, ValueError):
            return None
        if level is None or not level.tuf_id:
            return None
        status = str(data.get("status") or STATUS_PENDING)
        if status not in ALL_STATUSES:
            status = STATUS_PENDING
        # 工具退出时没跑完的任务，重开后一律回到等待中，绝不自动续传。
        if status in ACTIVE_STATUSES:
            status = STATUS_PENDING
        return cls(
            level=level,
            refresh_metadata=bool(data.get("refresh_metadata")),
            task_id=str(data.get("task_id") or ""),
            status=status,
            received=max(0, _as_int(data.get("received"))),
            total=max(0, _as_int(data.get("total"))),
            path=str(data.get("path") or ""),
            error=str(data.get("error") or ""),
            attempts=max(0, _as_int(data.get("attempts"))),
            created_at=str(data.get("created_at") or ""),
            started_at=str(data.get("started_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
        )


class DownloadQueue:
    """按创建顺序调度下载任务，限制同时运行的任务数量。"""

    def __init__(self, max_active: int = 2) -> None:
        self.max_active = max(1, int(max_active))
        self._tasks: dict[str, DownloadTask] = {}
        self._order: list[str] = []

    def add(
        self,
        level: LevelInfo,
        refresh_metadata: bool = False,
        task_id: str = "",
    ) -> DownloadTask:
        task = DownloadTask(
            level=level,
            refresh_metadata=refresh_metadata,
            task_id=task_id,
        )
        self._tasks[task.task_id] = task
        self._order.append(task.task_id)
        return task

    def get(self, task_id: str) -> DownloadTask | None:
        return self._tasks.get(task_id)

    def tasks(self) -> list[DownloadTask]:
        return [
            self._tasks[task_id]
            for task_id in self._order
            if task_id in self._tasks
        ]

    def active_tasks(self) -> list[DownloadTask]:
        return [task for task in self.tasks() if task.status in ACTIVE_STATUSES]

    def pending_tasks(self) -> list[DownloadTask]:
        return [task for task in self.tasks() if task.status == STATUS_PENDING]

    def unfinished_tasks(self) -> list[DownloadTask]:
        return [task for task in self.tasks() if task.status in UNFINISHED_STATUSES]

    def find_unfinished(self, tuf_id: int | None) -> DownloadTask | None:
        if not tuf_id:
            return None
        for task in self.tasks():
            if task.tuf_id == int(tuf_id) and task.status in UNFINISHED_STATUSES:
                return task
        return None

    def counts(self) -> dict[str, int]:
        result = {
            "pending": 0,
            "active": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
        }
        for task in self.tasks():
            if task.status == STATUS_PENDING:
                result["pending"] += 1
            elif task.status in ACTIVE_STATUSES:
                result["active"] += 1
            elif task.status == STATUS_COMPLETED:
                result["completed"] += 1
            elif task.status == STATUS_FAILED:
                result["failed"] += 1
            elif task.status == STATUS_CANCELLED:
                result["cancelled"] += 1
        return result

    @property
    def active_count(self) -> int:
        return len(self.active_tasks())

    @property
    def pending_count(self) -> int:
        return len(self.pending_tasks())

    def take_next(self) -> DownloadTask | None:
        if self.active_count >= self.max_active:
            return None
        for task in self.tasks():
            if task.status != STATUS_PENDING:
                continue
            task.status = STATUS_RUNNING
            task.attempts += 1
            task.started_at = _now()
            task.finished_at = ""
            task.error = ""
            return task
        return None

    def update_progress(self, task_id: str, received: int, total: int) -> None:
        task = self._tasks.get(task_id)
        if task is None or task.status not in {
            STATUS_RUNNING,
            STATUS_DOWNLOADING,
            STATUS_EXTRACTING,
        }:
            return
        task.received = max(0, int(received or 0))
        task.total = max(0, int(total or 0))

    def set_stage(self, task_id: str, stage: str) -> None:
        task = self._tasks.get(task_id)
        if task is None or task.status == STATUS_CANCELLING:
            return
        if stage == "download":
            task.status = STATUS_DOWNLOADING
        elif stage == "extract":
            task.status = STATUS_EXTRACTING

    def mark_completed(self, task_id: str, path: str = "") -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.status = STATUS_COMPLETED
        task.path = str(path or "")
        task.error = ""
        task.finished_at = _now()
        if task.total <= 0 and task.path:
            try:
                task.total = Path(task.path).stat().st_size
                task.received = task.total
            except OSError:
                pass

    def mark_failed(self, task_id: str, message: str) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.status = STATUS_FAILED
        task.error = str(message or "下载失败")
        task.finished_at = _now()

    def request_cancel(self, task_id: str) -> str:
        """返回 pending（已取消）、active（已请求取消）或空字符串。"""
        task = self._tasks.get(task_id)
        if task is None:
            return ""
        if task.status == STATUS_PENDING:
            task.status = STATUS_CANCELLED
            task.error = ""
            task.finished_at = _now()
            return "pending"
        if task.status in ACTIVE_STATUSES and task.status != STATUS_CANCELLING:
            task.status = STATUS_CANCELLING
            return "active"
        return ""

    def mark_cancelled(self, task_id: str, message: str = "已取消") -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.status = STATUS_CANCELLED
        task.error = str(message or "已取消")
        task.finished_at = _now()

    def retry(self, task_id: str) -> DownloadTask | None:
        task = self._tasks.get(task_id)
        if task is None or task.status not in {STATUS_FAILED, STATUS_CANCELLED}:
            return None
        task.status = STATUS_PENDING
        task.received = 0
        task.total = 0
        task.path = ""
        task.error = ""
        task.started_at = ""
        task.finished_at = ""
        if task_id in self._order:
            self._order.remove(task_id)
            self._order.append(task_id)
        return task

    def to_dict(self) -> dict[str, Any]:
        tasks = _retained_tasks(self.tasks())
        return {
            "version": QUEUE_VERSION,
            "order": [task.task_id for task in tasks],
            "tasks": [task.to_dict() for task in tasks],
        }

    @classmethod
    def from_dict(cls, data: Any, max_active: int = 2) -> "DownloadQueue":
        queue = cls(max_active=max_active)
        if not isinstance(data, dict):
            return queue
        raw_tasks = data.get("tasks")
        if not isinstance(raw_tasks, list):
            return queue

        restored: list[DownloadTask] = []
        seen: set[str] = set()
        for raw in raw_tasks:
            task = DownloadTask.from_dict(raw)
            if task is None or task.task_id in seen:
                continue
            seen.add(task.task_id)
            restored.append(task)
        if not restored:
            return queue

        by_id = {task.task_id: task for task in restored}
        ordered: list[DownloadTask] = []
        used: set[str] = set()
        raw_order = data.get("order")
        if isinstance(raw_order, list):
            for raw_id in raw_order:
                task_id = str(raw_id)
                task = by_id.get(task_id)
                if task is None or task_id in used:
                    continue
                used.add(task_id)
                ordered.append(task)
        ordered.extend(task for task in restored if task.task_id not in used)

        for task in _retained_tasks(ordered):
            queue._tasks[task.task_id] = task
            queue._order.append(task.task_id)
        return queue


def _retained_tasks(
    tasks: Iterable[DownloadTask],
    limit: int = TERMINAL_RETENTION_LIMIT,
) -> list[DownloadTask]:
    """终态任务只留最新 limit 条；未完成任务原样保留并保持原顺序。"""
    tasks = list(tasks)
    terminal = [task for task in tasks if task.status in TERMINAL_STATUSES]
    if limit < 0 or len(terminal) <= limit:
        return tasks
    newest = sorted(
        terminal,
        key=lambda task: (
            task.finished_at or task.created_at or "",
            task.created_at or "",
            task.task_id,
        ),
        reverse=True,
    )
    keep = {task.task_id for task in newest[:limit]}
    return [
        task
        for task in tasks
        if task.status not in TERMINAL_STATUSES or task.task_id in keep
    ]


def queue_path() -> Path:
    """下载队列文件放在工具数据目录，兼容 portable 与 ADOFAI_TUF_HOME。"""
    return base_dir() / QUEUE_FILE_NAME


def load_queue(
    path: str | Path | None = None,
    max_active: int = 2,
) -> DownloadQueue:
    target = Path(path) if path is not None else queue_path()
    if not target.is_file():
        return DownloadQueue(max_active=max_active)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("下载队列读取失败，按空队列处理：%s", exc)
        _quarantine_corrupt_file(target)
        return DownloadQueue(max_active=max_active)
    return DownloadQueue.from_dict(data, max_active=max_active)


def _quarantine_corrupt_file(target: Path) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = target.with_name(f"{target.stem}.corrupt-{stamp}{target.suffix}")
    try:
        os.replace(target, backup)
    except OSError as exc:
        logger.warning("无法备份损坏的下载队列文件：%s", exc)


def save_queue(
    queue: DownloadQueue,
    path: str | Path | None = None,
) -> bool:
    """原子写入下载队列；失败只记日志并返回 False，不向上抛异常。"""
    target = Path(path) if path is not None else queue_path()
    temp_name = ""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(queue.to_dict(), ensure_ascii=False, indent=2)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(target.parent),
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        temp_name = ""
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("下载队列保存失败：%s", exc)
        return False
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
