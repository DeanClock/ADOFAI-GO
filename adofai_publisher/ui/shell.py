"""应用外壳：侧边栏、切页动画、底部状态行。

这些控件只负责观感与交互，不承载业务逻辑；主题色统一从
``theme.palette(active_theme())`` 读取，换肤时由 MainWindow 统一分发刷新。
"""

from __future__ import annotations

import re
from collections import deque
from typing import Iterable

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..config import log_dir
from ..resources import icon_path
from .icons import line_icon
from .interactions import animations_enabled
from .theme import active_theme, metrics, palette

def _qcolor(value: str) -> QColor:
    """palette 里有 QSS 的 ``rgba(...)`` 写法，QColor 不认，需要手动解析。"""
    color = QColor(value)
    if color.isValid():
        return color
    match = re.match(r"rgba?\(([^)]+)\)", str(value))
    if not match:
        return color
    parts = [part.strip() for part in match.group(1).split(",")]
    if len(parts) < 3:
        return color
    try:
        red, green, blue = (int(float(part)) for part in parts[:3])
        alpha = float(parts[3]) if len(parts) > 3 else 1.0
    except ValueError:
        return color
    return QColor(red, green, blue, max(0, min(255, int(round(alpha * 255)))))


_NAV_TOOLTIPS = {
    "home": "Ctrl+0",
    "workspace": "Ctrl+1",
    "preview": "Ctrl+2",
    "history": "Ctrl+3",
    "library": "Ctrl+4",
    "umm": "Ctrl+5",
    "settings": "Ctrl+6",
}


class NavItem(QAbstractButton):
    """自绘导航项：线性图标 + 名称 + 弱化行号。"""

    ICON_SIZE = 18

    def __init__(
        self,
        key: str,
        number: str,
        label: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.key = key
        self.number = number
        self.label = label
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(metrics().nav_item_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip(_NAV_TOOLTIPS.get(key, ""))
        self.setAccessibleName(f"{number} {label}")

    def text(self) -> str:  # type: ignore[override]
        """保留 ``"00   首页"`` 形式（既有测试与快捷键提示依赖）。"""
        return f"{self.number}   {self.label}"

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(200, metrics().nav_item_height)

    def enterEvent(self, event) -> None:  # type: ignore[override]
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        super().leaveEvent(event)
        self.update()

    def _siblings(self) -> list["NavItem"]:
        parent = self.parentWidget()
        if parent is None:
            return [self]
        items = [child for child in parent.children() if isinstance(child, NavItem)]
        return items or [self]

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            self.click()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            siblings = self._siblings()
            if self in siblings:
                step = -1 if event.key() == Qt.Key.Key_Up else 1
                index = (siblings.index(self) + step) % len(siblings)
                siblings[index].setFocus(Qt.FocusReason.TabFocusReason)
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        colors = palette(active_theme())
        rect = self.rect()
        checked = self.isChecked()
        hovered = self.underMouse()
        pressed = self.isDown()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)

        if checked:
            painter.setBrush(_qcolor(colors["accent_soft"]))
        elif pressed:
            painter.setBrush(_qcolor(colors["accent_soft"]))
        elif hovered:
            painter.setBrush(QColor(colors["card_hover"]))
        else:
            painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)

        if checked:
            bar = QRectF(1.5, 8.0, 3.0, max(0.0, rect.height() - 16.0))
            painter.setBrush(QColor(colors["accent"]))
            painter.drawRoundedRect(bar, 1.5, 1.5)

        icon_color = (
            colors["accent"]
            if checked
            else (colors["text"] if hovered else colors["muted"])
        )
        icon = line_icon(
            self.key,
            icon_color,
            size=self.ICON_SIZE,
            dpr=self.devicePixelRatioF(),
        )
        icon_y = (rect.height() - self.ICON_SIZE) // 2
        painter.drawPixmap(14, icon_y, icon)

        text_color = (
            colors["accent"]
            if checked
            else (colors["text"] if hovered else colors["muted"])
        )
        font = QFont(self.font())
        font.setPointSizeF(10.0)
        font.setBold(checked)
        painter.setFont(font)
        painter.setPen(QColor(text_color))
        text_rect = rect.adjusted(40, 0, -38, 0)
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self.label,
        )

        number_font = QFont(self.font())
        number_font.setPointSizeF(8.5)
        painter.setFont(number_font)
        painter.setPen(
            QColor(colors["accent"] if checked else colors["faint"])
        )
        painter.drawText(
            rect.adjusted(0, 0, -12, 0),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            self.number,
        )

        if self.hasFocus():
            pen = QPen(QColor(colors["accent"]), 1.0)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(pen)
            painter.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        painter.end()


class SideBar(QWidget):
    """232px 图标侧边栏：品牌区 + 导航 + 本地提示。"""

    navigate = Signal(str)

    def __init__(
        self,
        items: Iterable[tuple[str, str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SideBar")
        self.setFixedWidth(metrics().sidebar_width)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 18, 14, 16)
        layout.setSpacing(6)

        logo_row = QHBoxLayout()
        logo_row.setSpacing(10)
        logo = QLabel()
        logo.setObjectName("LogoBadge")
        logo.setFixedSize(32, 32)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setPixmap(QPixmap(str(icon_path("app-logo.png"))))
        if not logo.pixmap().isNull():
            logo.setPixmap(
                logo.pixmap().scaled(
                    30,
                    30,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        name_box = QVBoxLayout()
        name_box.setSpacing(0)
        name = QLabel("ADOFAI GO")
        name.setObjectName("AppName")
        version = QLabel(f"v{__version__}")
        version.setObjectName("AppVersion")
        name_box.addWidget(name)
        name_box.addWidget(version)
        logo_row.addWidget(logo)
        logo_row.addLayout(name_box, 1)
        layout.addLayout(logo_row)
        layout.addSpacing(18)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, NavItem] = {}
        for index, (key, label) in enumerate(items):
            number = "00" if key == "home" else f"{index:02d}"
            item = NavItem(key, number, label, self)
            item.clicked.connect(lambda _=False, k=key: self.navigate.emit(k))
            self.group.addButton(item)
            self.buttons[key] = item
            layout.addWidget(item)
        layout.addStretch(1)

        hint = QLabel("本地运行 · 无遥测")
        hint.setObjectName("Faint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

    def set_current(self, key: str) -> None:
        button = self.buttons.get(key)
        if button is not None and not button.isChecked():
            button.setChecked(True)

    def refresh_theme(self) -> None:
        for button in self.buttons.values():
            button.update()
        self.update()


class AnimatedStack(QStackedWidget):
    """切页交叉淡出：先同步切页，再用旧页快照覆盖淡出。"""

    DURATION_MS = 160

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._overlay: QLabel | None = None
        self._animation: QPropertyAnimation | None = None

    def setCurrentIndex(self, index: int) -> None:  # type: ignore[override]
        if (
            index == self.currentIndex()
            or not animations_enabled()
            or not self.isVisible()
        ):
            self._finish_overlay()
            super().setCurrentIndex(index)
            return

        previous = self.currentWidget()
        snapshot: QPixmap | None = None
        if previous is not None and previous.isVisible() and previous.width() > 0:
            snapshot = previous.grab()

        self._finish_overlay()
        super().setCurrentIndex(index)
        if snapshot is None or snapshot.isNull():
            return

        overlay = QLabel(self)
        overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        overlay.setPixmap(snapshot)
        overlay.setGeometry(self.rect())
        effective = QGraphicsOpacityEffect(overlay)
        overlay.setGraphicsEffect(effective)
        effective.setOpacity(1.0)
        overlay.show()
        overlay.raise_()

        animation = QPropertyAnimation(effective, b"opacity", overlay)
        animation.setDuration(self.DURATION_MS)
        animation.setStartValue(1.0)
        animation.setEndValue(0.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.finished.connect(lambda: self._end_overlay(overlay))
        self._overlay = overlay
        self._animation = animation
        animation.start()

    def _end_overlay(self, overlay: QLabel) -> None:
        if self._overlay is overlay:
            self._overlay = None
            self._animation = None
        overlay.deleteLater()

    def _finish_overlay(self) -> None:
        animation = self._animation
        self._animation = None
        if animation is not None:
            animation.stop()
            animation.deleteLater()
        overlay = self._overlay
        self._overlay = None
        if overlay is not None:
            overlay.deleteLater()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._overlay is not None:
            self._overlay.setGeometry(self.rect())


class StatusSpinner(QWidget):
    """16px 小转盘；只有 busy 时 QTimer 才运行。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self.hide()

    def is_spinning(self) -> bool:
        return self._timer.isActive()

    def set_busy(self, busy: bool) -> None:
        if busy:
            self.show()
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
            self.hide()

    def _tick(self) -> None:
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        colors = palette(active_theme())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(colors["accent"]), 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        rect = QRectF(2.0, 2.0, 12.0, 12.0)
        for offset in (0, 120, 240):
            painter.drawArc(rect, int((self._angle + offset) * 16), 70 * 16)
        painter.end()


class StatusLine(QFrame):
    """底部状态行 + 运行日志面板（替代默认 QStatusBar）。"""

    MAX_LOG_LINES = 200

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StatusLine")
        self.setFixedHeight(34)
        self._recent: deque[str] = deque(maxlen=self.MAX_LOG_LINES)
        self._panel: QFrame | None = None
        self._view: QPlainTextEdit | None = None
        self._base_text = "就绪"

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(8)
        self.spinner = StatusSpinner(self)
        self.text_label = QLabel("就绪")
        self.text_label.setObjectName("StatusText")
        self.meta_label = QLabel(f"ADOFAI GO v{__version__}")
        self.meta_label.setObjectName("StatusMeta")
        self.log_button = QPushButton("运行日志▸")
        self.log_button.setObjectName("GhostButton")
        self.log_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.log_button.clicked.connect(self.toggle_log)
        layout.addWidget(self.spinner)
        layout.addWidget(self.text_label, 1)
        layout.addWidget(self.meta_label)
        layout.addWidget(self.log_button)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._clear_message)

    # -- 状态文本 ---------------------------------------------------------

    def show_message(self, text: str, timeout: int = 0) -> None:
        value = str(text or "")
        self.text_label.setText(value)
        self.append_log(value)
        if timeout and timeout > 0:
            self._timer.start(int(timeout))
        else:
            self._timer.stop()

    def current_message(self) -> str:
        return self.text_label.text()

    def _clear_message(self) -> None:
        self.text_label.setText(self._base_text)

    def set_busy(self, busy: bool) -> None:
        self.spinner.set_busy(bool(busy))

    def set_meta(self, text: str) -> None:
        self.meta_label.setText(text)

    def refresh_theme(self) -> None:
        self.spinner.update()
        self.update()
        if self._panel is not None:
            self._panel.update()

    # -- 运行日志 ---------------------------------------------------------

    def append_log(self, text: str) -> None:
        value = str(text or "").strip()
        if not value:
            return
        self._recent.append(value)
        if self._view is not None and self._panel is not None and self._panel.isVisible():
            self._view.appendPlainText(value)

    def toggle_log(self) -> None:
        if self._panel is not None and self._panel.isVisible():
            self._panel.hide()
            self.log_button.setText("运行日志▸")
            return
        panel = self._ensure_panel()
        self._fill_log()
        panel.show()
        panel.raise_()
        self.position_panel()
        self.log_button.setText("运行日志▾")

    def position_panel(self) -> None:
        if self._panel is None or not self._panel.isVisible():
            return
        window = self.window()
        if window is None:
            return
        point = self.mapTo(window, QPoint(0, 0))
        x = max(12, window.width() - self._panel.width() - 24)
        y = max(12, point.y() - self._panel.height() - 8)
        self._panel.move(x, y)

    def _ensure_panel(self) -> QFrame:
        if self._panel is not None:
            return self._panel
        panel = QFrame(self.window())
        panel.setObjectName("RunLogPanel")
        panel.resize(560, 280)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        header = QHBoxLayout()
        title = QLabel("运行日志")
        title.setObjectName("CardTitle")
        close = QPushButton("关闭")
        close.setObjectName("GhostButton")
        close.clicked.connect(self.toggle_log)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(close)
        layout.addLayout(header)
        view = QPlainTextEdit()
        view.setObjectName("RunLogView")
        view.setReadOnly(True)
        view.setMaximumBlockCount(self.MAX_LOG_LINES)
        view.setPlaceholderText("还没有日志。任务运行时这里会持续输出。")
        layout.addWidget(view, 1)
        self._panel = panel
        self._view = view
        return panel

    def _fill_log(self) -> None:
        if self._view is None:
            return
        lines: list[str] = []
        try:
            path = log_dir() / "app.log"
            if path.is_file():
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    lines = handle.readlines()[-self.MAX_LOG_LINES :]
        except OSError:
            lines = []
        combined = [line.rstrip("\n") for line in lines] + list(self._recent)
        self._view.setPlainText("\n".join(combined[-self.MAX_LOG_LINES :]))
        self._view.verticalScrollBar().setValue(
            self._view.verticalScrollBar().maximum()
        )


