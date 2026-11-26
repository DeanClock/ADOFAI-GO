from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .theme import active_theme


class CollectionManagerDialog(QDialog):
    createRequested = Signal(str)
    renameRequested = Signal(str, str)
    dissolveRequested = Signal(str)

    def __init__(
        self,
        collections: list[tuple[str, int]],
        parent: QWidget | None = None,
        theme: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme or active_theme()
        self.setWindowTitle("合集管理")
        self.resize(560, 480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        title = QLabel("合集管理")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        hint = QLabel(
            "新建、改名或解散合集只会更新谱面库索引，不会移动、复制或删除本地文件。"
        )
        hint.setObjectName("Faint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.list = QListWidget()
        self.list.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.list, 1)

        self.status = QLabel("")
        self.status.setObjectName("Faint")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        actions = QHBoxLayout()
        self.create_button = QPushButton("新建合集")
        self.create_button.setObjectName("PrimaryButton")
        self.create_button.clicked.connect(self._create)
        self.rename_button = QPushButton("重命名")
        self.rename_button.setObjectName("GhostButton")
        self.rename_button.clicked.connect(self._rename)
        self.dissolve_button = QPushButton("解散合集")
        self.dissolve_button.setObjectName("DangerButton")
        self.dissolve_button.clicked.connect(self._dissolve)
        actions.addWidget(self.create_button)
        actions.addWidget(self.rename_button)
        actions.addWidget(self.dissolve_button)
        actions.addStretch(1)
        close = QPushButton("关闭")
        close.setObjectName("GhostButton")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        layout.addLayout(actions)

        self.set_collections(collections)

    def set_collections(self, collections: list[tuple[str, int]]) -> None:
        selected = self._selected_name()
        self.list.clear()
        for name, count in collections:
            item = QListWidgetItem(f"{name}    {count} 张")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.list.addItem(item)
            if selected and selected == name:
                self.list.setCurrentItem(item)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self._update_buttons()

    def set_status(self, message: str) -> None:
        self.status.setText(message)

    def _selected_name(self) -> str:
        item = self.list.currentItem()
        if item is None:
            return ""
        return str(item.data(Qt.ItemDataRole.UserRole) or "")

    def _update_buttons(self) -> None:
        selected = bool(self._selected_name())
        self.rename_button.setEnabled(selected)
        self.dissolve_button.setEnabled(selected)

    def _create(self) -> None:
        name, ok = QInputDialog.getText(self, "新建合集", "合集名称：")
        if ok and name.strip():
            self.createRequested.emit(name)

    def _rename(self) -> None:
        current = self._selected_name()
        if not current:
            return
        name, ok = QInputDialog.getText(
            self,
            "重命名合集",
            "新名称：",
            text=current,
        )
        if ok and name.strip() and name.strip() != current:
            self.renameRequested.emit(current, name)

    def _dissolve(self) -> None:
        current = self._selected_name()
        if not current:
            return
        if QMessageBox.question(
            self,
            "解散合集",
            f"确定解散合集「{current}」？\n"
            "其中的谱面会回到原难度分类，本地文件不会被移动或删除。",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.dissolveRequested.emit(current)
