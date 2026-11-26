from __future__ import annotations

from typing import Iterable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..download_tasks import (
    ACTIVE_STATUSES,
    STATUS_CANCELLED,
    STATUS_CANCELLING,
    STATUS_COMPLETED,
    STATUS_DOWNLOADING,
    STATUS_EXTRACTING,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    DownloadTask,
    format_bytes,
)
from .theme import active_theme, palette


class DownloadTasksDialog(QDialog):
    cancelRequested = Signal(str)
    retryRequested = Signal(str)
    openFolderRequested = Signal(str)
    resumeRequested = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        theme: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = "light" if (theme or active_theme()) == "light" else "dark"
        self._tasks: list[DownloadTask] = []
        self._task_ids: list[str] = []
        self._rows: dict[str, dict[str, QWidget | str]] = {}
        self.setObjectName("DownloadTasksDialog")
        self.setWindowTitle("下载任务中心")
        self.resize(880, 560)
        self.setMinimumSize(720, 420)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("下载任务中心")
        title.setObjectName("PageTitle")
        self.summary = QLabel("暂无任务")
        self.summary.setObjectName("Muted")
        title_box.addWidget(title)
        title_box.addWidget(self.summary)
        header.addLayout(title_box, 1)
        self.limit_label = QLabel("最多同时下载 2 个")
        self.limit_label.setObjectName("Faint")
        header.addWidget(self.limit_label, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(header)

        hint = QLabel(
            "每个任务独立显示进度，可单独取消或重试。"
            "完成的谱面会自动加入本地谱面库，取消或失败会清理未完成文件。"
            "重启后未完成任务会保留，点「继续下载」才会开始。"
        )
        hint.setObjectName("Faint")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        self.table = QTableWidget(0, 5)
        self.table.setObjectName("DownloadTaskTable")
        self.table.setHorizontalHeaderLabels(["谱面", "状态", "进度", "大小", "操作"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(58)
        head = self.table.horizontalHeader()
        head.setHighlightSections(False)
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        outer.addWidget(self.table, 1)

        self.empty = QLabel("还没有下载任务。在谱面库点「下载」后，任务会出现在这里。")
        self.empty.setObjectName("ChartEmpty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.hide()
        outer.addWidget(self.empty, 1)

        footer = QHBoxLayout()
        self.resume_button = QPushButton("继续下载")
        self.resume_button.setObjectName("PrimaryButton")
        self.resume_button.setMinimumHeight(32)
        self.resume_button.setVisible(False)
        self.resume_button.clicked.connect(
            lambda _checked=False: self.resumeRequested.emit()
        )
        footer.addWidget(self.resume_button)
        footer.addStretch(1)
        close = QPushButton("关闭")
        close.setObjectName("GhostButton")
        close.clicked.connect(self.close)
        footer.addWidget(close)
        outer.addLayout(footer)

    def set_theme(self, theme: str) -> None:
        self._theme = "light" if theme == "light" else "dark"
        for row, task in enumerate(self._tasks):
            self._update_row(row, task)

    def refresh_theme(self) -> None:
        self.set_theme(active_theme())

    def set_tasks(
        self,
        tasks: Iterable[DownloadTask],
        max_active: int = 2,
    ) -> None:
        self._tasks = [task for task in tasks if isinstance(task, DownloadTask)]
        self.limit_label.setText(f"最多同时下载 {max(1, int(max_active))} 个")
        task_ids = [task.task_id for task in self._tasks]
        if task_ids != self._task_ids:
            self._rebuild()
        else:
            for row, task in enumerate(self._tasks):
                self._update_row(row, task)
        self._update_summary()

    def _rebuild(self) -> None:
        self.table.setRowCount(len(self._tasks))
        self._task_ids = [task.task_id for task in self._tasks]
        self._rows = {}
        for row, task in enumerate(self._tasks):
            name = QTableWidgetItem()
            status = QTableWidgetItem()
            size = QTableWidgetItem()
            size.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            progress = QProgressBar()
            progress.setObjectName("TaskProgress")
            progress.setTextVisible(True)
            progress.setAlignment(Qt.AlignmentFlag.AlignCenter)
            progress.setFixedHeight(22)
            actions = QWidget()
            action_layout = QHBoxLayout(actions)
            action_layout.setContentsMargins(4, 0, 4, 0)
            action_layout.setSpacing(6)
            action_layout.addStretch(1)
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, status)
            self.table.setCellWidget(row, 2, progress)
            self.table.setItem(row, 3, size)
            self.table.setCellWidget(row, 4, actions)
            self._rows[task.task_id] = {
                "name": name,
                "status": status,
                "progress": progress,
                "size": size,
                "actions": actions,
                "action_mode": "",
            }
            self._update_row(row, task)
        has_tasks = bool(self._tasks)
        self.table.setVisible(has_tasks)
        self.empty.setVisible(not has_tasks)

    def _update_row(self, row: int, task: DownloadTask) -> None:
        if row < 0 or row >= self.table.rowCount():
            return
        widgets = self._rows.get(task.task_id)
        if widgets is None:
            return
        name = widgets["name"]
        status = widgets["status"]
        progress = widgets["progress"]
        size = widgets["size"]
        actions = widgets["actions"]
        if not isinstance(name, QTableWidgetItem) or not isinstance(status, QTableWidgetItem):
            return
        if not isinstance(progress, QProgressBar) or not isinstance(size, QTableWidgetItem):
            return
        if not isinstance(actions, QWidget):
            return

        name.setText(task.title)
        details = []
        if task.level.tuf_id:
            details.append(f"TUF #{task.level.tuf_id}")
        if task.level.charter:
            details.append(f"谱师 {task.level.charter}")
        if task.level.difficulty:
            details.append(str(task.level.difficulty))
        name.setToolTip(" · ".join(details))

        status.setText(task.status_label)
        status.setForeground(QBrush(QColor(self._status_color(task.status))))
        tooltip = task.error or task.path
        status.setToolTip(tooltip if tooltip else task.status_label)

        self._set_progress(progress, task)
        size.setText(self._size_text(task))
        size.setToolTip(task.path or "")

        mode = self._action_mode(task.status)
        action_mode = str(widgets.get("action_mode") or "")
        if mode != action_mode:
            layout = actions.layout()
            while layout.count():
                item = layout.takeAt(0)
                child = item.widget()
                if child is not None:
                    child.deleteLater()
            button = self._action_button(task, mode)
            if button is not None:
                layout.addWidget(button)
            layout.addStretch(1)
            widgets["action_mode"] = mode

    def _set_progress(self, bar: QProgressBar, task: DownloadTask) -> None:
        if task.status == STATUS_PENDING:
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setFormat("等待")
        elif task.status in {STATUS_RUNNING, STATUS_CANCELLING}:
            bar.setRange(0, 0)
            bar.setFormat("连接中" if task.status == STATUS_RUNNING else "取消中")
        elif task.status == STATUS_DOWNLOADING:
            if task.total > 0:
                percent = task.progress_percent or 0
                bar.setRange(0, 100)
                bar.setValue(percent)
                bar.setFormat(f"{percent}%")
            else:
                bar.setRange(0, 0)
                bar.setFormat("下载中")
        elif task.status == STATUS_EXTRACTING:
            bar.setRange(0, 0)
            bar.setFormat("解压中")
        elif task.status == STATUS_COMPLETED:
            bar.setRange(0, 100)
            bar.setValue(100)
            bar.setFormat("完成")
        elif task.status == STATUS_FAILED:
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setFormat("失败")
        else:
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setFormat("已取消")

    @staticmethod
    def _size_text(task: DownloadTask) -> str:
        if task.total <= 0:
            return "-"
        if task.status == STATUS_COMPLETED:
            return format_bytes(task.total)
        if task.received > 0:
            return f"{format_bytes(task.received)} / {format_bytes(task.total)}"
        return format_bytes(task.total)

    @staticmethod
    def _action_mode(status: str) -> str:
        if status in {STATUS_PENDING, *ACTIVE_STATUSES}:
            return "cancel"
        if status in {STATUS_FAILED, STATUS_CANCELLED}:
            return "retry"
        if status == STATUS_COMPLETED:
            return "open"
        return ""

    def _action_button(self, task: DownloadTask, mode: str) -> QPushButton | None:
        if mode == "cancel":
            button = QPushButton("取消")
            button.setObjectName("TaskActionDanger")
            button.setEnabled(task.status != STATUS_CANCELLING)
            button.clicked.connect(
                lambda _checked=False, task_id=task.task_id: self._defer(
                    self.cancelRequested,
                    task_id,
                )
            )
        elif mode == "retry":
            button = QPushButton("重试")
            button.setObjectName("TaskAction")
            button.clicked.connect(
                lambda _checked=False, task_id=task.task_id: self._defer(
                    self.retryRequested,
                    task_id,
                )
            )
        elif mode == "open":
            button = QPushButton("打开目录")
            button.setObjectName("TaskAction")
            button.clicked.connect(
                lambda _checked=False, task_id=task.task_id: self._defer(
                    self.openFolderRequested,
                    task_id,
                )
            )
        else:
            return None
        button.setFixedHeight(28)
        button.setMinimumWidth(72)
        return button

    @staticmethod
    def _defer(signal: Signal, task_id: str) -> None:
        QTimer.singleShot(0, lambda: signal.emit(task_id))

    def _update_summary(self) -> None:
        counts = {
            "active": 0,
            "pending": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
        }
        for task in self._tasks:
            if task.status == STATUS_PENDING:
                counts["pending"] += 1
            elif task.status in ACTIVE_STATUSES:
                counts["active"] += 1
            elif task.status == STATUS_COMPLETED:
                counts["completed"] += 1
            elif task.status == STATUS_FAILED:
                counts["failed"] += 1
            elif task.status == STATUS_CANCELLED:
                counts["cancelled"] += 1
        parts: list[str] = []
        if counts["active"]:
            parts.append(f"{counts['active']} 个进行中")
        if counts["pending"]:
            parts.append(f"{counts['pending']} 个等待")
        if counts["completed"]:
            parts.append(f"{counts['completed']} 个完成")
        if counts["failed"]:
            parts.append(f"{counts['failed']} 个失败")
        if counts["cancelled"]:
            parts.append(f"{counts['cancelled']} 个已取消")
        self.summary.setText(" · ".join(parts) if parts else "暂无任务")
        pending = counts["pending"]
        self.resume_button.setText(f"继续下载（{pending}）")
        self.resume_button.setVisible(pending > 0)
        self.resume_button.setEnabled(pending > 0)

    def _status_color(self, status: str) -> str:
        colors = palette(self._theme)
        if status == STATUS_COMPLETED:
            return colors["success"]
        if status == STATUS_FAILED:
            return colors["danger"]
        if status == STATUS_CANCELLED:
            return colors["muted"]
        if status == STATUS_EXTRACTING:
            return colors["warning"]
        if status == STATUS_CANCELLING:
            return colors["warning"]
        if status in {STATUS_RUNNING, STATUS_DOWNLOADING}:
            return colors["accent"]
        return colors["muted"]
