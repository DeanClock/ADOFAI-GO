from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .disk_organizer import SelectionCheckDelegate
from .theme import active_theme, palette
from .widgets import ChoiceCombo

MANIFEST_STATUS_LABELS = {
    "started": "进行中",
    "completed": "已完成",
    "partial": "部分完成",
    "failed": "失败",
}

ITEM_STATUS_LABELS = {
    "pending": "未执行",
    "moved": "已移动",
    "failed": "失败",
    "undone": "已撤销",
}


def _short_path(value: str, limit: int = 78) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    head = max(14, (limit - 1) // 2)
    tail = max(14, limit - head - 1)
    return text[:head] + "…" + text[-tail:]


def _display_time(value: str) -> str:
    return str(value or "").replace("T", " ")[:19]


def _is_checked(value) -> bool:
    try:
        return int(value) == int(Qt.CheckState.Checked.value)
    except (TypeError, ValueError):
        return value == Qt.CheckState.Checked


class OrganizeHistoryDialog(QDialog):
    refreshRequested = Signal()
    undoRequested = Signal(str)
    undoItemsRequested = Signal(str, list)

    def __init__(
        self,
        records: list[dict],
        theme: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette(theme or active_theme())
        self._all_records = list(records)
        self._records: list[dict] = []
        self._busy = False
        self._detail_loading = False
        self.setWindowTitle("整理历史")
        self.resize(1240, 820)
        self._build_ui()
        self.set_records(records)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        title = QLabel("整理历史")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        hint = QLabel(
            "每次应用磁盘整理都会生成一条记录。可以按批次撤销，也可以在明细中"
            "搜索并只撤销某几张谱面；已撤销、失败或已失效的条目会安全跳过。"
        )
        hint.setObjectName("Faint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        filters = QHBoxLayout()
        self.record_search = QLineEdit()
        self.record_search.setPlaceholderText("搜索谱面、路径或错误原因")
        self.record_search.setClearButtonEnabled(True)
        self.record_search.textChanged.connect(
            lambda: self._apply_record_filter(self._current_path())
        )
        self.record_status = ChoiceCombo()
        self.record_status.addItem("全部状态", "all")
        self.record_status.addItem("仍可撤销", "undoable")
        self.record_status.addItem("有失败项", "failed")
        self.record_status.addItem("已全部撤销", "undone")
        self.record_status.addItem("已无移动项", "settled")
        self.record_status.currentIndexChanged.connect(
            lambda: self._apply_record_filter(self._current_path())
        )
        filters.addWidget(self.record_search, 1)
        filters.addWidget(self.record_status)
        layout.addLayout(filters)

        self.summary = QLabel("")
        self.summary.setObjectName("Muted")
        layout.addWidget(self.summary)

        splitter = QSplitter(Qt.Orientation.Vertical)

        self.records_table = QTableWidget(0, 6)
        self.records_table.setHorizontalHeaderLabels(
            ["时间", "状态", "已移动", "失败", "已撤销", "可撤销"]
        )
        self.records_table.verticalHeader().setVisible(False)
        self.records_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.records_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.records_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.records_table.setAlternatingRowColors(True)
        self.records_table.itemSelectionChanged.connect(self._show_details)
        header = self.records_table.horizontalHeader()
        for column in range(5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.records_table.setColumnWidth(0, 178)
        self.records_table.setColumnWidth(1, 100)
        self.records_table.setColumnWidth(2, 82)
        self.records_table.setColumnWidth(3, 72)
        self.records_table.setColumnWidth(4, 82)
        splitter.addWidget(self.records_table)

        details = QWidget()
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(0, 8, 0, 0)
        details_layout.setSpacing(6)
        detail_top = QHBoxLayout()
        self.detail_title = QLabel("选择一条整理记录查看明细")
        self.detail_title.setObjectName("Muted")
        self.detail_title.setWordWrap(True)
        self.detail_search = QLineEdit()
        self.detail_search.setPlaceholderText("在当前记录中搜索谱面或路径")
        self.detail_search.setClearButtonEnabled(True)
        self.detail_search.setMinimumWidth(280)
        self.detail_search.textChanged.connect(self._show_details)
        detail_top.addWidget(self.detail_title, 1)
        detail_top.addWidget(self.detail_search)
        details_layout.addLayout(detail_top)

        self.detail_table = QTableWidget(0, 6)
        self.detail_table.setHorizontalHeaderLabels(
            ["选择", "状态", "谱面", "原位置", "整理后位置", "说明"]
        )
        self.detail_table.verticalHeader().setVisible(False)
        self.detail_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.detail_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.detail_table.setAlternatingRowColors(True)
        self.detail_table.verticalHeader().setDefaultSectionSize(36)
        self.detail_table.setItemDelegateForColumn(
            0,
            SelectionCheckDelegate(self._palette, self.detail_table),
        )
        self.detail_table.itemChanged.connect(self._on_detail_item_changed)
        detail_header = self.detail_table.horizontalHeader()
        detail_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        detail_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        detail_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        detail_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        detail_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        detail_header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.detail_table.setColumnWidth(0, 72)
        self.detail_table.setColumnWidth(1, 78)
        self.detail_table.setColumnWidth(2, 170)
        details_layout.addWidget(self.detail_table, 1)
        splitter.addWidget(details)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)

        self.status = QLabel("")
        self.status.setObjectName("Faint")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        actions = QHBoxLayout()
        self.refresh_button = QPushButton("刷新历史")
        self.refresh_button.setObjectName("GhostButton")
        self.refresh_button.clicked.connect(self.refreshRequested.emit)
        self.select_all_button = QPushButton("全选可撤销")
        self.select_all_button.setObjectName("GhostButton")
        self.select_all_button.clicked.connect(self._select_all_undoable)
        self.clear_selection_button = QPushButton("清空选择")
        self.clear_selection_button.setObjectName("GhostButton")
        self.clear_selection_button.clicked.connect(self._clear_selection)
        self.undo_items_button = QPushButton("撤销选中项")
        self.undo_items_button.setObjectName("PrimaryButton")
        self.undo_items_button.clicked.connect(self._confirm_undo_items)
        self.undo_button = QPushButton("撤销整条记录")
        self.undo_button.setObjectName("GhostButton")
        self.undo_button.clicked.connect(self._confirm_undo_record)
        actions.addWidget(self.refresh_button)
        actions.addWidget(self.select_all_button)
        actions.addWidget(self.clear_selection_button)
        actions.addWidget(self.undo_items_button)
        actions.addWidget(self.undo_button)
        actions.addStretch(1)
        close = QPushButton("关闭")
        close.setObjectName("GhostButton")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        layout.addLayout(actions)

    def set_records(
        self,
        records: list[dict],
        select_path: str = "",
    ) -> None:
        self._all_records = list(records)
        self._apply_record_filter(select_path)

    def refresh_theme(self) -> None:
        """主题切换后更新缓存调色板并重绘记录/明细表。"""
        path = self._current_path()
        self._palette = palette(active_theme())
        delegate = self.detail_table.itemDelegateForColumn(0)
        if isinstance(delegate, SelectionCheckDelegate):
            delegate.set_palette(self._palette)
        self._apply_record_filter(path)

    def _current_path(self) -> str:
        record = self.selected_record()
        return str(record.get("path") or "") if record else ""

    def _record_matches(self, record: dict, query: str, mode: str) -> bool:
        if mode == "undoable" and not record.get("can_undo"):
            return False
        if mode == "failed" and int(record.get("failed") or 0) <= 0:
            return False
        if mode == "undone" and not (
            int(record.get("moved") or 0) == 0
            and int(record.get("undone") or 0) > 0
        ):
            return False
        if mode == "settled" and int(record.get("moved") or 0) > 0:
            return False
        if not query:
            return True
        fields = [
            str(record.get("root") or ""),
            str(record.get("created_at") or ""),
            str(record.get("status") or ""),
        ]
        for item in record.get("items") or []:
            fields.extend(
                [
                    str(item.get("song") or ""),
                    str(item.get("source_folder") or ""),
                    str(item.get("target_folder") or ""),
                    str(item.get("message") or ""),
                    str(item.get("error") or ""),
                    str(item.get("undo_error") or ""),
                ]
            )
        return query in "\n".join(fields).casefold()

    def _apply_record_filter(self, select_path: str = "") -> None:
        query = self.record_search.text().strip().casefold()
        mode = str(self.record_status.currentData() or "all")
        self._records = [
            record
            for record in self._all_records
            if self._record_matches(record, query, mode)
        ]
        self._populate_record_table(select_path)

    def _populate_record_table(self, select_path: str = "") -> None:
        self.records_table.blockSignals(True)
        self.records_table.setRowCount(len(self._records))
        for row, record in enumerate(self._records):
            time_item = QTableWidgetItem(_display_time(record.get("created_at", "")))
            time_item.setData(Qt.ItemDataRole.UserRole, str(record.get("path") or ""))
            time_item.setToolTip(str(record.get("path") or ""))
            self.records_table.setItem(row, 0, time_item)

            status = str(record.get("status") or "")
            status_item = QTableWidgetItem(
                MANIFEST_STATUS_LABELS.get(status, status or "未知")
            )
            status_color = {
                "completed": self._palette["success"],
                "partial": self._palette["warning"],
                "failed": self._palette["danger"],
                "started": self._palette["accent"],
            }.get(status, self._palette["muted"])
            status_item.setForeground(QColor(status_color))
            status_item.setToolTip(str(record.get("root") or ""))
            self.records_table.setItem(row, 1, status_item)

            for column, key in ((2, "moved"), (3, "failed"), (4, "undone")):
                value = QTableWidgetItem(str(int(record.get(key) or 0)))
                value.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.records_table.setItem(row, column, value)

            can_undo = bool(record.get("can_undo"))
            undo_item = QTableWidgetItem("可撤销" if can_undo else "不可撤销")
            undo_item.setForeground(
                QColor(
                    self._palette["accent"]
                    if can_undo
                    else self._palette["faint"]
                )
            )
            undo_item.setToolTip(str(record.get("root") or ""))
            self.records_table.setItem(row, 5, undo_item)
        self.records_table.blockSignals(False)

        undoable = sum(1 for record in self._records if record.get("can_undo"))
        self.summary.setText(
            f"显示 {len(self._records)} / {len(self._all_records)} 条整理记录 · "
            f"当前结果中 {undoable} 条仍可撤销"
        )
        if self._records:
            selected_row = 0
            if select_path:
                for row, record in enumerate(self._records):
                    if str(record.get("path") or "") == select_path:
                        selected_row = row
                        break
            self.records_table.selectRow(selected_row)
            self._show_details()
        else:
            self.records_table.clearSelection()
            self.detail_title.setText("没有匹配的整理记录")
            self.detail_table.setRowCount(0)
            self._update_action_state()

    def selected_record(self) -> dict | None:
        row = self.records_table.currentRow()
        if row < 0 or row >= len(self._records):
            return None
        return self._records[row]

    def _visible_items(self, record: dict, query: str) -> list[dict]:
        items = [
            item
            for item in (record.get("items") or [])
            if isinstance(item, dict)
        ]
        if not query:
            return items
        filtered: list[dict] = []
        for item in items:
            fields = [
                str(item.get("song") or ""),
                str(item.get("status") or ""),
                str(item.get("source_folder") or ""),
                str(item.get("target_folder") or ""),
                str(item.get("message") or ""),
                str(item.get("error") or ""),
                str(item.get("undo_error") or ""),
            ]
            if query in "\n".join(fields).casefold():
                filtered.append(item)
        return filtered

    def _show_details(self) -> None:
        record = self.selected_record()
        if record is None:
            self.detail_title.setText("选择一条整理记录查看明细")
            self.detail_table.setRowCount(0)
            self._update_action_state()
            return
        query = self.detail_search.text().strip().casefold()
        all_items = [
            item
            for item in (record.get("items") or [])
            if isinstance(item, dict)
        ]
        items = self._visible_items(record, query)
        created = _display_time(record.get("created_at", ""))
        root = str(record.get("root") or "")
        self.detail_title.setText(
            f"{created} · {_short_path(root, 90)} · "
            f"显示 {len(items)} / {len(all_items)} 项"
        )

        self._detail_loading = True
        try:
            self.detail_table.setRowCount(len(items))
            for row, item in enumerate(items):
                status = str(item.get("status") or "pending")
                selectable = status == "moved" and bool(item.get("key"))
                select_item = QTableWidgetItem("" if selectable else "—")
                select_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if selectable:
                    select_item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsSelectable
                        | Qt.ItemFlag.ItemIsUserCheckable
                    )
                    select_item.setData(
                        Qt.ItemDataRole.UserRole,
                        str(item.get("key") or ""),
                    )
                    select_item.setCheckState(Qt.CheckState.Unchecked)
                else:
                    select_item.setFlags(Qt.ItemFlag.NoItemFlags)
                    select_item.setForeground(QColor(self._palette["faint"]))
                self.detail_table.setItem(row, 0, select_item)

                status_item = QTableWidgetItem(
                    ITEM_STATUS_LABELS.get(status, status)
                )
                status_color = {
                    "moved": self._palette["accent"],
                    "failed": self._palette["danger"],
                    "undone": self._palette["success"],
                    "pending": self._palette["muted"],
                }.get(status, self._palette["muted"])
                status_item.setForeground(QColor(status_color))
                self.detail_table.setItem(row, 1, status_item)

                song = QTableWidgetItem(str(item.get("song") or "(未命名)"))
                song.setToolTip(str(item.get("message") or item.get("song") or ""))
                self.detail_table.setItem(row, 2, song)

                source = QTableWidgetItem(
                    _short_path(str(item.get("source_folder") or ""))
                )
                source.setToolTip(str(item.get("source_folder") or ""))
                self.detail_table.setItem(row, 3, source)

                target = QTableWidgetItem(
                    _short_path(str(item.get("target_folder") or ""))
                )
                target.setToolTip(str(item.get("target_folder") or ""))
                self.detail_table.setItem(row, 4, target)

                message = str(
                    item.get("undo_error")
                    or item.get("error")
                    or item.get("message")
                    or ""
                )
                note = QTableWidgetItem(message)
                note.setToolTip(message)
                self.detail_table.setItem(row, 5, note)
        finally:
            self._detail_loading = False
        self._update_action_state()

    def selected_item_keys(self) -> list[str]:
        keys: list[str] = []
        for row in range(self.detail_table.rowCount()):
            item = self.detail_table.item(row, 0)
            if item is None or not _is_checked(
                item.data(Qt.ItemDataRole.CheckStateRole)
            ):
                continue
            key = str(item.data(Qt.ItemDataRole.UserRole) or "")
            if key:
                keys.append(key)
        return keys

    def _select_all_undoable(self) -> None:
        self._set_all_checked(True)

    def _clear_selection(self) -> None:
        self._set_all_checked(False)

    def _set_all_checked(self, checked: bool) -> None:
        self._detail_loading = True
        try:
            for row in range(self.detail_table.rowCount()):
                item = self.detail_table.item(row, 0)
                if item is None or not item.data(Qt.ItemDataRole.UserRole):
                    continue
                item.setCheckState(
                    Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
                )
        finally:
            self._detail_loading = False
        self._update_action_state()

    def _on_detail_item_changed(self, item: QTableWidgetItem) -> None:
        if self._detail_loading or item.column() != 0:
            return
        self._update_action_state()

    def _update_action_state(self) -> None:
        record = self.selected_record()
        selected = len(self.selected_item_keys())
        movable_rows = 0
        for row in range(self.detail_table.rowCount()):
            item = self.detail_table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole):
                movable_rows += 1
        can_batch = bool(record and record.get("can_undo")) and not self._busy
        self.undo_button.setEnabled(can_batch)
        self.undo_button.setText("撤销整条记录")
        self.undo_items_button.setText(f"撤销选中项 ({selected})")
        self.undo_items_button.setEnabled(selected > 0 and not self._busy)
        self.select_all_button.setEnabled(movable_rows > 0 and not self._busy)
        self.clear_selection_button.setEnabled(selected > 0 and not self._busy)
        self.refresh_button.setEnabled(not self._busy)
        self.record_search.setEnabled(not self._busy)
        self.record_status.setEnabled(not self._busy)
        self.detail_search.setEnabled(not self._busy)
        self.records_table.setEnabled(not self._busy)
        self.detail_table.setEnabled(not self._busy)

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        self._update_action_state()
        if message:
            self.status.setText(message)

    def set_status(self, message: str) -> None:
        self.status.setText(message)

    def _confirm_undo_items(self) -> None:
        record = self.selected_record()
        keys = self.selected_item_keys()
        if record is None or not keys:
            return
        if QMessageBox.question(
            self,
            "撤销选中谱面",
            f"将按这条记录撤销选中的 {len(keys)} 张谱面，其他谱面保持不变。\n"
            "已经撤销、失败或已失效的条目会安全跳过。继续？",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.undoItemsRequested.emit(str(record.get("path") or ""), keys)

    def _confirm_undo_record(self) -> None:
        record = self.selected_record()
        if record is None or not record.get("can_undo"):
            return
        moved = int(record.get("moved") or 0)
        if QMessageBox.question(
            self,
            "撤销整条整理记录",
            f"将按这条记录把 {moved} 个仍处于已移动状态的谱面文件夹移回原位置。\n"
            "已经撤销、失败或已失效的条目会安全跳过。继续？",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.undoRequested.emit(str(record.get("path") or ""))
