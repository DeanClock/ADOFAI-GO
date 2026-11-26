from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..level_organizer import STATUS_LABELS, OrganizePlan
from .theme import active_theme, palette
from .widgets import ChoiceCombo


def _short_path(value: str, limit: int = 72) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    head = max(12, (limit - 1) // 2)
    tail = max(12, limit - head - 1)
    return text[:head] + "…" + text[-tail:]


def _is_checked(value) -> bool:
    try:
        return int(value) == int(Qt.CheckState.Checked.value)
    except (TypeError, ValueError):
        return value == Qt.CheckState.Checked


class SelectionCheckDelegate(QStyledItemDelegate):
    """在表格中绘制清晰、可点击的高对比选择框。"""

    BOX_SIZE = 24.0

    def __init__(self, colors: dict[str, str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._colors = colors

    def set_palette(self, colors: dict[str, str]) -> None:
        self._colors = colors

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index,
    ) -> None:
        style_option = QStyleOptionViewItem(option)
        self.initStyleOption(style_option, index)
        style_option.text = ""
        style_option.features &= ~QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        style = (
            option.widget.style()
            if option.widget is not None
            else QApplication.style()
        )
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem,
            style_option,
            painter,
            option.widget,
        )

        checkable = bool(index.flags() & Qt.ItemFlag.ItemIsUserCheckable)
        if not checkable:
            painter.save()
            painter.setPen(QColor(self._colors["faint"]))
            painter.drawText(
                option.rect,
                Qt.AlignmentFlag.AlignCenter,
                "—",
            )
            painter.restore()
            return

        checked = _is_checked(index.data(Qt.ItemDataRole.CheckStateRole))
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        box = QRectF(
            option.rect.center().x() - self.BOX_SIZE / 2,
            option.rect.center().y() - self.BOX_SIZE / 2,
            self.BOX_SIZE,
            self.BOX_SIZE,
        )

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        border = QColor(self._colors["accent_strong" if hovered else "accent"])
        if not checked and not hovered:
            border.setAlpha(190)
        if checked:
            painter.setBrush(QColor(self._colors["accent"]))
        else:
            painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(border, 2.0))
        painter.drawRoundedRect(box, 5.0, 5.0)
        if checked:
            check = QPolygonF(
                [
                    QPointF(box.left() + 5.5, box.center().y()),
                    QPointF(box.left() + 10.0, box.bottom() - 6.5),
                    QPointF(box.right() - 5.0, box.top() + 7.0),
                ]
            )
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(
                QPen(
                    QColor(self._colors["selection_text"]),
                    2.8,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
            painter.drawPolyline(check)
        painter.restore()

    def editorEvent(self, event, model, option, index) -> bool:
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
            and index.flags() & Qt.ItemFlag.ItemIsUserCheckable
        ):
            checked = _is_checked(index.data(Qt.ItemDataRole.CheckStateRole))
            model.setData(
                index,
                Qt.CheckState.Unchecked if checked else Qt.CheckState.Checked,
                Qt.ItemDataRole.CheckStateRole,
            )
            return True
        if (
            event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Select)
            and index.flags() & Qt.ItemFlag.ItemIsUserCheckable
        ):
            checked = _is_checked(index.data(Qt.ItemDataRole.CheckStateRole))
            model.setData(
                index,
                Qt.CheckState.Unchecked if checked else Qt.CheckState.Checked,
                Qt.ItemDataRole.CheckStateRole,
            )
            return True
        return super().editorEvent(event, model, option, index)


class DiskOrganizerDialog(QDialog):
    refreshRequested = Signal()
    applyRequested = Signal(list)
    undoRequested = Signal()
    historyRequested = Signal()

    def __init__(
        self,
        plan: OrganizePlan,
        theme: str | None = None,
        undo_available: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette(theme or active_theme())
        self._plan = plan
        self._busy = False
        self._loading = False
        self.setWindowTitle("磁盘整理预览")
        self.resize(1120, 720)
        self._build_ui()
        self.set_plan(plan)
        self.set_undo_available(undo_available)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        title = QLabel("磁盘整理预览")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        hint = QLabel(
            "这里只展示计划，不会立即移动文件。勾选要整理的谱面后再应用；"
            "默认不选择任何项，确认后按整张谱面文件夹移动，并保存数据库备份和可撤销清单。"
            "来自其他目录的谱面也可以移入当前根目录。"
        )
        hint.setObjectName("Faint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        root_row = QHBoxLayout()
        root_label = QLabel("谱面根目录")
        root_label.setObjectName("Faint")
        self.root_value = QLabel(str(self._plan.root))
        self.root_value.setObjectName("Mono")
        self.root_value.setWordWrap(True)
        self.root_value.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        root_row.addWidget(root_label)
        root_row.addWidget(self.root_value, 1)
        layout.addLayout(root_row)

        self.summary = QLabel("")
        self.summary.setObjectName("Muted")
        layout.addWidget(self.summary)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索谱面、当前路径或目标路径")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        self.status_filter = ChoiceCombo()
        self.status_filter.addItem("全部状态", "all")
        self.status_filter.addItem("仅可移动", "move")
        self.status_filter.addItem("已就位", "same")
        self.status_filter.addItem("仅冲突", "conflict")
        self.status_filter.addItem("仅跳过", "skip")
        self.status_filter.currentIndexChanged.connect(self._apply_filter)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.status_filter)
        layout.addLayout(filters)

        self.selection = QLabel("")
        self.selection.setObjectName("Muted")
        layout.addWidget(self.selection)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["选择", "状态", "谱面", "当前路径", "目标路径"]
        )
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.setItemDelegateForColumn(
            0,
            SelectionCheckDelegate(self._palette, self.table),
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 72)
        self.table.setColumnWidth(1, 88)
        self.table.setColumnWidth(2, 180)
        self.table.horizontalHeaderItem(0).setTextAlignment(
            Qt.AlignmentFlag.AlignCenter
        )
        layout.addWidget(self.table, 1)

        self.status = QLabel("")
        self.status.setObjectName("Faint")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        actions = QHBoxLayout()
        self.refresh_button = QPushButton("刷新预览")
        self.refresh_button.setObjectName("GhostButton")
        self.refresh_button.clicked.connect(self.refreshRequested.emit)
        self.select_all_button = QPushButton("全选可移动")
        self.select_all_button.setObjectName("GhostButton")
        self.select_all_button.clicked.connect(self._select_all)
        self.clear_selection_button = QPushButton("清空选择")
        self.clear_selection_button.setObjectName("GhostButton")
        self.clear_selection_button.clicked.connect(self._clear_selection)
        self.apply_button = QPushButton("应用选中项")
        self.apply_button.setObjectName("PrimaryButton")
        self.apply_button.clicked.connect(self._confirm_apply)
        self.undo_button = QPushButton("撤销上次整理")
        self.undo_button.setObjectName("GhostButton")
        self.undo_button.clicked.connect(self._confirm_undo)
        self.history_button = QPushButton("整理历史")
        self.history_button.setObjectName("GhostButton")
        self.history_button.clicked.connect(self.historyRequested.emit)
        actions.addWidget(self.refresh_button)
        actions.addWidget(self.select_all_button)
        actions.addWidget(self.clear_selection_button)
        actions.addWidget(self.apply_button)
        actions.addWidget(self.undo_button)
        actions.addWidget(self.history_button)
        actions.addStretch(1)
        close = QPushButton("关闭")
        close.setObjectName("GhostButton")
        close.clicked.connect(self.reject)
        actions.addWidget(close)
        layout.addLayout(actions)

    def set_plan(self, plan: OrganizePlan) -> None:
        self._plan = plan
        self.root_value.setText(str(plan.root))
        counts = plan.counts()
        self.summary.setText(
            f"可移动 {counts.get('move', 0)} · "
            f"已就位 {counts.get('same', 0)} · "
            f"冲突 {counts.get('conflict', 0)} · "
            f"跳过 {counts.get('skip', 0)}"
        )
        self._loading = True
        try:
            self.table.setRowCount(len(plan.items))
            for row, item in enumerate(plan.items):
                selectable = item.status == "move"
                select_item = QTableWidgetItem("" if selectable else "—")
                select_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                select_item.setToolTip(
                    "勾选后应用整理" if selectable else item.message or "不可移动"
                )
                if selectable:
                    select_item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsSelectable
                        | Qt.ItemFlag.ItemIsUserCheckable
                    )
                    select_item.setData(Qt.ItemDataRole.UserRole, item.key)
                    select_item.setCheckState(Qt.CheckState.Unchecked)
                else:
                    select_item.setFlags(Qt.ItemFlag.NoItemFlags)
                    select_item.setForeground(QColor(self._palette["muted"]))
                self.table.setItem(row, 0, select_item)

                status = QTableWidgetItem(STATUS_LABELS.get(item.status, item.status))
                color = {
                    "move": self._palette["accent"],
                    "same": self._palette["success"],
                    "conflict": self._palette["danger"],
                    "skip": self._palette["warning"],
                }.get(item.status, self._palette["muted"])
                status.setForeground(QColor(color))
                status.setToolTip(item.message)
                self.table.setItem(row, 1, status)

                song = QTableWidgetItem(item.song or "(未命名)")
                song.setToolTip(item.message or item.song)
                self.table.setItem(row, 2, song)

                source = QTableWidgetItem(_short_path(item.source_folder))
                source.setToolTip(item.source_folder)
                self.table.setItem(row, 3, source)

                target = QTableWidgetItem(_short_path(item.target_folder))
                target.setToolTip(item.target_folder)
                self.table.setItem(row, 4, target)
        finally:
            self._loading = False
        self._apply_filter()

    def refresh_theme(self) -> None:
        """主题切换后更新缓存调色板并重绘表格，保留已勾选的行。"""
        checked: set[str] = set()
        for row in range(self.table.rowCount()):
            cell = self.table.item(row, 0)
            if cell is not None and cell.checkState() == Qt.CheckState.Checked:
                key = str(cell.data(Qt.ItemDataRole.UserRole) or "")
                if key:
                    checked.add(key)
        self._palette = palette(active_theme())
        delegate = self.table.itemDelegateForColumn(0)
        if isinstance(delegate, SelectionCheckDelegate):
            delegate.set_palette(self._palette)
        self.set_plan(self._plan)
        if checked:
            for row in range(self.table.rowCount()):
                cell = self.table.item(row, 0)
                if cell is None:
                    continue
                key = str(cell.data(Qt.ItemDataRole.UserRole) or "")
                if key in checked:
                    cell.setCheckState(Qt.CheckState.Checked)

    def _row_matches(self, item, query: str, mode: str) -> bool:
        if mode != "all" and item.status != mode:
            return False
        if not query:
            return True
        fields = [
            item.song,
            item.source_folder,
            item.target_folder,
            item.source_path,
            item.target_path,
            STATUS_LABELS.get(item.status, item.status),
            item.message,
        ]
        return query in "\n".join(str(value or "") for value in fields).casefold()

    def _apply_filter(self) -> None:
        query = self.search.text().strip().casefold()
        mode = str(self.status_filter.currentData() or "all")
        self._loading = True
        try:
            for row, item in enumerate(self._plan.items):
                visible = self._row_matches(item, query, mode)
                self.table.setRowHidden(row, not visible)
                if not visible and item.status == "move":
                    select_item = self.table.item(row, 0)
                    if select_item is not None:
                        select_item.setCheckState(Qt.CheckState.Unchecked)
        finally:
            self._loading = False
        self._update_selection_state()
        self._update_selection_state()

    def selected_keys(self) -> list[str]:
        keys: list[str] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None or item.checkState() != Qt.CheckState.Checked:
                continue
            key = str(item.data(Qt.ItemDataRole.UserRole) or "")
            if key:
                keys.append(key)
        return keys

    def _select_all(self) -> None:
        self._set_all_checked(True)

    def _clear_selection(self) -> None:
        self._set_all_checked(False)

    def _set_all_checked(self, checked: bool) -> None:
        self._loading = True
        try:
            for row in range(self.table.rowCount()):
                if self.table.isRowHidden(row):
                    continue
                item = self.table.item(row, 0)
                if item is None or not item.data(Qt.ItemDataRole.UserRole):
                    continue
                item.setCheckState(
                    Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
                )
        finally:
            self._loading = False
        self._update_selection_state()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() != 0:
            return
        self._update_selection_state()

    def _update_selection_state(self) -> None:
        selected = len(self.selected_keys())
        moves = len(self._plan.move_items)
        visible_rows = sum(
            1
            for row in range(self.table.rowCount())
            if not self.table.isRowHidden(row)
        )
        self.selection.setText(
            f"已选 {selected} / {moves} 个可移动项 · 当前显示 {visible_rows} 项"
        )
        self.apply_button.setText(f"应用选中项 ({selected})")
        self.apply_button.setEnabled(selected > 0 and not self._busy)
        self.select_all_button.setEnabled(moves > 0 and not self._busy)
        self.clear_selection_button.setEnabled(selected > 0 and not self._busy)

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        self.refresh_button.setEnabled(not busy)
        self.undo_button.setEnabled(not busy and self.undo_button.property("available"))
        self.history_button.setEnabled(not busy)
        self._update_selection_state()
        self.table.setEnabled(not busy)
        if message:
            self.status.setText(message)

    def set_undo_available(self, available: bool) -> None:
        self.undo_button.setProperty("available", available)
        self.undo_button.setEnabled(available and not self._busy)

    def set_status(self, message: str) -> None:
        self.status.setText(message)

    def _confirm_apply(self) -> None:
        selected = self.selected_keys()
        if not selected:
            return
        if QMessageBox.question(
            self,
            "确认整理磁盘",
            f"将移动选中的 {len(selected)} 个谱面文件夹到目标目录。\n"
            "冲突、跳过和已就位的条目不会移动。\n"
            "整理前会备份谱面库，并生成可撤销清单。继续？",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.applyRequested.emit(selected)

    def _confirm_undo(self) -> None:
        if QMessageBox.question(
            self,
            "撤销上次整理",
            "将按最近一份整理清单把已移动的谱面文件夹移回原位置。继续？",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.undoRequested.emit()
