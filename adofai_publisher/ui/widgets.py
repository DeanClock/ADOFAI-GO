from __future__ import annotations

import hashlib
import os
import re
import weakref
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QModelIndex,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QIntValidator,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QListView,
    QPushButton,
    QSizePolicy,
    QLineEdit,
    QStatusBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..models import (
    JUDGEMENT_FIELDS,
    JUDGEMENT_KEYS,
    LevelInfo,
    judgement_hit_count,
    judgement_sum,
)
from ..compliance import Issue
from ..config import cover_sync_dir
from ..level_paths import classify_difficulty
from ..levellookup import (
    ADVANCED_ID_FIELDS,
    ADVANCED_NUMERIC_FIELDS,
    ADVANCED_TEXT_FIELDS,
    DEFAULT_SEARCH_LIMIT,
)
from .chart_library import (
    ChartCardDelegate,
    ChartItem,
    ChartListModel,
    difficulty_value,
)
from ..thumbnails import ThumbnailLoader, shared_thumbnail_loader
from .interactions import animations_enabled, paint_check_mark, repolish, set_tone
from .theme import active_palette, active_theme, palette


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


class FlowLayout(QLayout):
    """按可用宽度自动换行的横向布局。

    提示 chip 行用它：窄窗口下换行而不是把整张卡片的最小宽度顶宽，
    否则工作台第二步在 1280 宽下右侧卡片会被横向裁掉。
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        h_spacing: int = 10,
        v_spacing: int = 8,
    ) -> None:
        super().__init__(parent)
        self._items: list = []
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self.setContentsMargins(0, 0, 0, 0)

    # -- QLayout 接口 ---------------------------------------------------
    def addItem(self, item) -> None:  # type: ignore[override]
        self._items.append(item)

    def count(self) -> int:  # type: ignore[override]
        return len(self._items)

    def itemAt(self, index: int):  # type: ignore[override]
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # type: ignore[override]
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # type: ignore[override]
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # type: ignore[override]
        return True

    def heightForWidth(self, width: int) -> int:  # type: ignore[override]
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # type: ignore[override]
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # type: ignore[override]
        size = QSize(0, 0)
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(
            margins.left() + margins.right(), margins.top() + margins.bottom()
        )

    # -- 内部 -----------------------------------------------------------
    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(
            margins.left(), margins.top(), -margins.right(), -margins.bottom()
        )
        x, y, line_height = area.x(), area.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._h_spacing
            if next_x - self._h_spacing > area.right() + 1 and line_height > 0:
                x = area.x()
                y = y + line_height + self._v_spacing
                next_x = x + hint.width() + self._h_spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


class Card(QFrame):
    def __init__(
        self,
        title: str = "",
        subtitle: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(10)
        if title:
            title_label = QLabel(title)
            title_label.setObjectName("CardTitle")
            outer.addWidget(title_label)
        if subtitle:
            subtitle_label = QLabel(subtitle)
            subtitle_label.setObjectName("CardHint")
            subtitle_label.setWordWrap(True)
            outer.addWidget(subtitle_label)
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        outer.addLayout(self.body)


class Divider(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Divider")
        self.setFixedHeight(1)


class Disclosure(QWidget):
    """「？」折叠说明：默认只留一个问号按钮，点开后显示长说明。

    长解释不该常驻界面。用法与设置页的 ``ObsAdvancedButton`` 一致：
    可勾选按钮 + 箭头，展开时把说明文字显示出来。
    """

    def __init__(
        self,
        title: str,
        text: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)

        self.button = QToolButton(self)
        self.button.setObjectName("ObsAdvancedButton")
        self.button.setText(f"？ {title}")
        self.button.setCheckable(True)
        self.button.setChecked(False)
        self.button.setArrowType(Qt.ArrowType.RightArrow)
        self.button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.button.clicked.connect(self._on_toggled)

        self.text_label = QLabel(text, self)
        self.text_label.setObjectName("CardHint")
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.text_label.hide()

        box.addWidget(self.button)
        box.addWidget(self.text_label)

    def _on_toggled(self, checked: bool) -> None:
        self.set_expanded(checked)

    def is_expanded(self) -> bool:
        return self.button.isChecked()

    def set_expanded(self, expanded: bool) -> None:
        self.button.setChecked(expanded)
        self.button.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self.text_label.setVisible(expanded)

    def set_text(self, text: str) -> None:
        self.text_label.setText(text)


class StepBar(QWidget):
    stepClicked = Signal(int)

    def __init__(self, steps: Iterable[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.steps = list(steps)
        self.current = 0
        self._theme = active_theme()
        self._hover = -1
        self.setMinimumHeight(66)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("投稿步骤")

    def set_theme(self, theme: str) -> None:
        self._theme = theme
        self.update()

    def refresh_theme(self) -> None:
        """跟随当前激活主题重绘（页面刷新主题时调用）。"""
        self._theme = active_theme()
        self.update()

    def set_current(self, index: int) -> None:
        if not self.steps:
            self.current = 0
            self.update()
            return
        self.current = max(0, min(index, len(self.steps) - 1))
        self.update()

    def _index_at(self, x: float) -> int:
        count = len(self.steps)
        if count <= 0 or self.width() <= 0:
            return -1
        slot = self.width() / count
        return max(0, min(int(x // slot), count - 1))

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        index = self._index_at(event.position().x())
        if index != self._hover:
            self._hover = index
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        if self._hover != -1:
            self._hover = -1
            self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:  # type: ignore[override]
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:  # type: ignore[override]
        super().focusOutEvent(event)
        self.update()

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if self.steps and event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            step = 1 if event.key() == Qt.Key.Key_Right else -1
            target = max(0, min(self.current + step, len(self.steps) - 1))
            if target != self.current:
                # 仍走 stepClicked，让页面的门禁逻辑（未选发布方式等）照常生效。
                self.stepClicked.emit(target)
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        index = self._index_at(event.position().x())
        if 0 <= index < len(self.steps):
            self.stepClicked.emit(index)
        event.accept()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        if not self.steps:
            return
        colors = palette(self._theme)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        count = len(self.steps)
        slot = self.width() / count
        y = 22

        for index in range(count - 1):
            x1 = slot * (index + 0.5) + 16
            x2 = slot * (index + 1.5) - 16
            done = index < self.current
            pen = QPen(QColor(colors["accent"] if done else colors["border"]))
            pen.setWidth(2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(int(x1), y, int(x2), y)

        for index, label in enumerate(self.steps):
            cx = slot * (index + 0.5)
            active = index == self.current
            done = index < self.current
            hovered = index == self._hover and not active
            if active:
                fill = QColor(colors["accent"])
                text_color = QColor(colors["selection_text"])
                border = QColor(colors["accent"])
                label_color = QColor(colors["text"])
            elif done:
                fill = _qcolor(colors["accent_soft"])
                text_color = QColor(colors["accent"])
                border = QColor(
                    colors["accent_strong"] if hovered else colors["accent"]
                )
                label_color = QColor(colors["text"] if hovered else colors["muted"])
            elif hovered:
                fill = _qcolor(colors["accent_soft"])
                text_color = QColor(colors["text"])
                border = QColor(colors["accent"])
                label_color = QColor(colors["text"])
            else:
                fill = QColor(colors["input"])
                text_color = QColor(colors["faint"])
                border = QColor(colors["border"])
                label_color = QColor(colors["muted"])
            painter.setBrush(fill)
            painter.setPen(QPen(border, 1.5))
            radius = 15 if active else 13
            painter.drawEllipse(int(cx - radius), int(y - radius), radius * 2, radius * 2)

            font = QFont(self.font())
            font.setBold(active)
            font.setPointSizeF(9.5)
            painter.setFont(font)
            painter.setPen(text_color)
            painter.drawText(
                int(cx - radius),
                int(y - radius),
                radius * 2,
                radius * 2,
                Qt.AlignmentFlag.AlignCenter,
                str(index + 1),
            )

            label_font = QFont(self.font())
            label_font.setPointSizeF(9)
            label_font.setBold(active)
            painter.setFont(label_font)
            painter.setPen(label_color)
            painter.drawText(
                int(cx - slot // 2),
                y + 22,
                int(slot),
                20,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                label,
            )

        if self.hasFocus() and 0 <= self.current < count:
            # 键盘焦点只给当前步号圆圈加一圈细环，不画覆盖整步的大虚线框
            # （那个框会压在下方内容卡上，像多余的选框）。
            cx = slot * (self.current + 0.5)
            ring_radius = 18.5
            ring = QPen(QColor(colors["accent"]), 1.5)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(ring)
            painter.drawEllipse(
                QRectF(
                    cx - ring_radius,
                    y - ring_radius,
                    ring_radius * 2,
                    ring_radius * 2,
                )
            )
        painter.end()


class DropZone(QFrame):
    fileSelected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(150)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon = DropIcon()
        self.title = QLabel("把录好的视频拖到这里")
        self.title.setObjectName("DropTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint = QLabel("或点击选择文件 · 支持 MP4 / FLV / MOV / MKV")
        self.hint.setObjectName("DropHint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.file_label = QLabel("")
        self.file_label.setObjectName("Chip")
        self.file_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.file_label.hide()
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.title)
        layout.addWidget(self.hint)
        layout.addWidget(self.file_label)

    def set_file(self, path: str) -> None:
        if not path:
            self.file_label.hide()
            self.file_label.setToolTip("")
            self.title.setText("把录好的视频拖到这里")
            return
        name = Path(path).name
        size = os.path.getsize(path) / 1024**2 if os.path.isfile(path) else 0
        full = f"{name} · {size:.1f} MB"
        # 长文件名不能把 DropZone（进而整张卡片）的最小宽度顶宽，
        # 否则窄窗口下第二步的右侧卡片会被裁掉；中间省略并给完整路径做 tooltip。
        elided = self.file_label.fontMetrics().elidedText(
            full, Qt.TextElideMode.ElideMiddle, 360
        )
        self.file_label.setText(elided)
        self.file_label.setToolTip(full)
        self.file_label.show()
        self.title.setText("视频已就绪")
        self.fileSelected.emit(path)

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setProperty("dragging", True)
            self.style().unpolish(self)
            self.style().polish(self)

    def dragLeaveEvent(self, event) -> None:  # type: ignore[override]
        self.setProperty("dragging", False)
        self.style().unpolish(self)
        self.style().polish(self)

    def dropEvent(self, event) -> None:  # type: ignore[override]
        self.setProperty("dragging", False)
        self.style().unpolish(self)
        self.style().polish(self)
        urls = event.mimeData().urls()
        if urls:
            self.set_file(urls[0].toLocalFile())

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择通关视频",
            "",
            "视频文件 (*.mp4 *.flv *.mov *.mkv *.avi *.wmv *.webm *.m4v);;所有文件 (*.*)",
        )
        if path:
            self.set_file(path)


class DropIcon(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(54, 54)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        accent = QColor(active_palette()["accent"])
        fill = QColor(accent)
        fill.setAlpha(28)
        painter.setPen(QPen(accent, 2))
        painter.setBrush(fill)
        painter.drawRoundedRect(2, 2, 50, 50, 13, 13)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent)
        painter.drawPolygon(
            QPolygonF([QPointF(21, 16), QPointF(39, 27), QPointF(21, 38)])
        )
        painter.end()


class CoverPicker(QFrame):
    """封面选择器：主封面 + 备用比例两个槽。

    每个槽各存一份源图路径；``active_slot`` 决定当前显示与拖放目标，
    默认是主封面，因此现有页面只调 ``set_file / clear`` 时行为不变。

    主槽放 16:9 主图、副槽放 4:3 副图。主图变化后，用 ``QImage`` 居中裁剪出
    4:3 的图，通过 ``sync_callback`` / ``syncImageChanged`` 交给页面；
    ``save_sync`` 负责把它落到缓存目录。组件本身不读写草稿字段，接线由页面负责。
    副槽也可以单独指定图片。

    预览「点一个是一个」：每个槽只显示自己的图片，副槽为空时不再拿主图的裁剪版
    顶替——那只是投稿时的兜底——这样一眼能看出副槽到底传没传。预览框按较高的
    4:3 撑开，16:9 与 4:3 的图都完整显示，不做居中裁剪。
    """

    fileSelected = Signal(str)
    slotChanged = Signal(str)
    syncImageChanged = Signal(str, QImage)

    SLOT_PRIMARY = "primary"
    SLOT_ALTERNATE = "alternate"
    SLOT_LABELS = {SLOT_PRIMARY: "16:9 主图", SLOT_ALTERNATE: "4:3 副图"}
    # 主图按 16:9 提交（B 站会按 16:10 裁切），副图按 4:3 裁剪生成。
    SLOT_RATIOS = {SLOT_PRIMARY: (16, 9), SLOT_ALTERNATE: (4, 3)}
    SLOT_HINTS = {
        SLOT_PRIMARY: "JPG / PNG / WEBP · 16:9 主图，B 站会按 16:10 裁切",
        SLOT_ALTERNATE: "JPG / PNG / WEBP · 4:3 副图",
    }
    # 有自己文件的槽在按钮文字后打勾，切换时一眼分清谁传了、谁没传。
    SLOT_DONE_MARK = "  ✓"
    # 预览框按较高的 4:3 撑开，两种比例都能完整放下：16:9 上下留白，
    # 切换槽位时高度不跳，卡片也不会被挤到重叠。
    PREVIEW_RATIO = (4, 3)
    PREVIEW_MIN_HEIGHT = 140
    PREVIEW_MAX_HEIGHT = 420

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CoverPicker")
        self.setAcceptDrops(True)
        self.setMinimumHeight(240)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._slot = self.SLOT_PRIMARY
        self._paths = {self.SLOT_PRIMARY: "", self.SLOT_ALTERNATE: ""}
        # 解码结果按槽缓存：resizeEvent 会频繁触发 _render，不能每次都读盘解码。
        self._images = {self.SLOT_PRIMARY: QImage(), self.SLOT_ALTERNATE: QImage()}
        self._sync_image = QImage()
        self._sync_callback: Callable[[str, QImage], None] | None = None
        # resizeEvent → setPixmap 会再次触发布局；重入直接返回，避免无限递归。
        self._rendering = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        # 槽切换条默认隐藏：骨架先就位，页面接线时调 set_slot_bar_visible(True)。
        self.slot_bar = QWidget(self)
        # 槽切换条用 FlowLayout：两个按钮在窄卡片里换行，不把卡片最小宽度顶宽。
        slot_row = FlowLayout(self.slot_bar, h_spacing=6, v_spacing=4)
        self.slot_buttons: dict[str, QPushButton] = {}
        for key in (self.SLOT_PRIMARY, self.SLOT_ALTERNATE):
            button = QPushButton(self.SLOT_LABELS[key])
            button.setObjectName("SegmentButton")
            button.setCheckable(True)
            button.setChecked(key == self._slot)
            button.clicked.connect(lambda _=False, k=key: self.set_active_slot(k))
            self.slot_buttons[key] = button
            slot_row.addWidget(button)
        self._refresh_slot_buttons()
        self.slot_bar.hide()
        layout.addWidget(self.slot_bar)

        self.preview = QLabel("拖入封面图片\n或点击选择")
        self.preview.setObjectName("CoverPreview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(self.PREVIEW_MIN_HEIGHT)
        self.preview.setWordWrap(True)
        # 关键：忽略 pixmap 的 sizeHint。否则大图会把 QLabel 撑大，布局再次
        # resize，_render 又按更大的尺寸缩放……正反馈一直到爆栈。
        self.preview.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored
        )
        self.hint = QLabel(self.SLOT_HINTS[self._slot])
        self.hint.setObjectName("DropHint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setWordWrap(True)
        layout.addWidget(self.preview, 1)
        layout.addWidget(self.hint)

    # -- 槽 -----------------------------------------------------------------

    @property
    def active_slot(self) -> str:
        return self._slot

    def set_active_slot(self, key: str) -> None:
        if key not in self._paths:
            return
        changed = key != self._slot
        self._slot = key
        for slot, button in self.slot_buttons.items():
            button.setChecked(slot == key)
        if not changed:
            return
        self._render()
        self.slotChanged.emit(key)

    def set_slot_bar_visible(self, visible: bool) -> None:
        """显示 / 隐藏槽切换条；骨架默认隐藏，避免未接线时多出一行。"""
        self.slot_bar.setVisible(visible)

    def _refresh_slot_buttons(self) -> None:
        """槽按钮的文字后缀 / 提示随「这个槽有没有自己的图」变化。"""
        for slot, button in self.slot_buttons.items():
            path = self._paths[slot]
            button.setText(
                self.SLOT_LABELS[slot] + (self.SLOT_DONE_MARK if path else "")
            )
            if path:
                button.setToolTip(f"已选：{Path(path).name}")
            elif slot == self.SLOT_ALTERNATE and self._paths[self.SLOT_PRIMARY]:
                button.setToolTip("未单独上传；投稿时会用主图裁剪版补 4:3")
            else:
                button.setToolTip("尚未上传")

    def path_for(self, key: str | None = None) -> str:
        return self._paths.get(key or self._slot, "")

    def sync_image(self) -> QImage:
        """主封面裁出的备用比例图；没有主封面时返回空图。"""
        return QImage(self._sync_image)

    def set_sync_callback(
        self, callback: Callable[[str, QImage], None] | None
    ) -> None:
        """注册同步回调：主封面变化时收到 ``("alternate", QImage)``。"""
        self._sync_callback = callback

    @classmethod
    def center_crop(
        cls, image: QImage, ratio: tuple[int, int] = (16, 9)
    ) -> QImage:
        """按目标比例居中裁剪 ``QImage``，不缩放、不引入 Pillow。"""
        if image.isNull() or ratio[0] <= 0 or ratio[1] <= 0:
            return QImage()
        target = ratio[0] / ratio[1]
        width, height = image.width(), image.height()
        if width <= 0 or height <= 0:
            return QImage()
        current = width / height
        if current > target:
            new_width = max(1, round(height * target))
            x = (width - new_width) // 2
            return image.copy(x, 0, new_width, height)
        new_height = max(1, round(width / target))
        y = (height - new_height) // 2
        return image.copy(0, y, width, new_height)

    # -- 选图 ---------------------------------------------------------------

    def set_file(self, path: str) -> None:
        if not path:
            self.clear()
            return
        image = QImage(path)
        if image.isNull():
            self._paths[self._slot] = ""
            self._images[self._slot] = QImage()
            if self._slot == self.SLOT_PRIMARY:
                self._update_sync()
            self._refresh_slot_buttons()
            self.preview.setPixmap(QPixmap())
            self.preview.setText("无法读取这张图片")
            self.hint.setText("请选择 JPG / PNG / WEBP 图片")
            return
        self._paths[self._slot] = path
        self._images[self._slot] = image
        if self._slot == self.SLOT_PRIMARY:
            self._update_sync()
        self._refresh_slot_buttons()
        self._render()
        self.fileSelected.emit(path)

    def clear(self) -> None:
        self._paths[self._slot] = ""
        self._images[self._slot] = QImage()
        if self._slot == self.SLOT_PRIMARY:
            self._update_sync()
        self._refresh_slot_buttons()
        self._render()
        self.fileSelected.emit("")

    def _update_sync(self) -> None:
        image = self._images[self.SLOT_PRIMARY]
        self._sync_image = (
            self.center_crop(image, self.SLOT_RATIOS[self.SLOT_ALTERNATE])
            if not image.isNull()
            else QImage()
        )
        self.syncImageChanged.emit(self.SLOT_ALTERNATE, self._sync_image)
        if self._sync_callback is not None:
            self._sync_callback(self.SLOT_ALTERNATE, QImage(self._sync_image))

    def save_sync(self, folder: str | Path | None = None) -> str:
        """把主图裁出的 4:3 图写成 PNG 并返回路径；没有主图时返回空串。

        只依赖 Qt 的图片读写，不引入 Pillow；默认落在 ``cache/cover_sync``。
        """
        if self._sync_image.isNull():
            return ""
        source = self._paths[self.SLOT_PRIMARY]
        target_dir = Path(folder) if folder else cover_sync_dir()
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return ""
        stem = Path(source).stem[:60] or "cover"
        digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:8]
        path = target_dir / f"{stem}-4x3-{digest}.png"
        if not self._sync_image.save(str(path), "PNG"):
            return ""
        return str(path)

    # -- 绘制 ---------------------------------------------------------------

    def _slot_image(self) -> QImage:
        """当前槽自己的图片；没有就是空图，不拿另一槽的图顶替预览。"""
        image = self._images.get(self._slot)
        return QImage() if image is None else image

    def _placeholder_text(self) -> str:
        if self._slot == self.SLOT_ALTERNATE:
            if self._paths[self.SLOT_PRIMARY]:
                return "尚未单独上传 4:3 副图\n拖入图片，或点下方「用这张同步 4:3」"
            return "先选 16:9 主图再同步\n也可直接拖入 4:3 图片"
        return "拖入封面图片\n或点击选择"

    def _hint_text(self) -> str:
        path = self._paths[self._slot]
        if path:
            return Path(path).name
        if self._slot == self.SLOT_ALTERNATE and self._paths[self.SLOT_PRIMARY]:
            return "投稿时会用主图裁剪版补 4:3"
        return self.SLOT_HINTS[self._slot]

    def _preview_target_size(self) -> QSize:
        """按卡片实际宽度算预览框大小：高度按 4:3 撑开，夹在上下限之间。"""
        width = self.preview.width()
        if width <= 0:
            return QSize()
        ratio_w, ratio_h = self.PREVIEW_RATIO
        height = round(width * ratio_h / ratio_w)
        height = max(self.PREVIEW_MIN_HEIGHT, min(height, self.PREVIEW_MAX_HEIGHT))
        return QSize(width, height)

    def _apply_preview_height(self) -> None:
        target = self._preview_target_size()
        if target.isValid() and self.preview.minimumHeight() != target.height():
            self.preview.setMinimumHeight(target.height())

    def _render(self) -> None:
        # setPixmap / setMinimumHeight 会同步触发布局与新的 resizeEvent；
        # 重入时直接返回，避免 resizeEvent → _render → 布局 → resizeEvent 递归。
        if self._rendering:
            return
        self._rendering = True
        try:
            self._apply_preview_height()
            image = self._slot_image()
            if image.isNull():
                self.preview.setPixmap(QPixmap())
                self.preview.setText(self._placeholder_text())
                self.hint.setText(self._hint_text())
                return
            size = self._preview_target_size()
            if size.isEmpty():
                return
            # 完整显示整张图，不居中裁剪：预览框按 4:3 撑开，图片等比缩放进去。
            scaled = image.scaled(
                size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.preview.setPixmap(QPixmap.fromImage(scaled))
            self.preview.setText("")
            self.hint.setText(self._hint_text())
        finally:
            self._rendering = False

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._render()

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setProperty("dragging", True)
            self.style().unpolish(self)
            self.style().polish(self)

    def dragLeaveEvent(self, event) -> None:  # type: ignore[override]
        self.setProperty("dragging", False)
        self.style().unpolish(self)
        self.style().polish(self)

    def dropEvent(self, event) -> None:  # type: ignore[override]
        self.setProperty("dragging", False)
        self.style().unpolish(self)
        self.style().polish(self)
        urls = event.mimeData().urls()
        if urls:
            self.set_file(urls[0].toLocalFile())

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择投稿封面",
            "",
            "图片文件 (*.jpg *.jpeg *.png *.webp);;所有文件 (*.*)",
        )
        if path:
            self.set_file(path)


class JudgementGrid(QWidget):
    """TUF 判定输入网格。

    对齐 TUF ``/submission/pass`` 的真实表单：X 完美关闭时 6 格
    （稍快 / 完美 / 稍慢 + 提前 / 太快 / 太慢），开启时 8 格
    （-完美 / 完美 / 完美+ + 稍快 / 稍慢 + 提前 / 太快 / 太慢）。

    TUF 表单没有「太慢！！」输入框（只在记录展示里出现），这里同样不显示，
    但键与权重保留：``set_values`` 能载入旧值，``values()`` 始终返回全部 9 键。

    网格下方常驻两行小字，按 TUF 官网分数计算器的两种口径各算一遍：
    「判定总数（九格）」是 sumJudgements，「打击数」是 tilecount（排除
    提前 / 太慢！！），物量校验只看后者。
    """

    changed = Signal()

    _GREEN_KEYS = ("perfectMinus", "perfect", "perfectPlus", "ePerfect", "lPerfect")
    _X_ON_ROWS = (
        ("perfectMinus", "perfect", "perfectPlus"),
        ("ePerfect", "lPerfect"),
        ("earlyDouble", "earlySingle", "lateSingle"),
    )
    _X_OFF_ROWS = (
        ("ePerfect", "perfect", "lPerfect"),
        ("earlyDouble", "earlySingle", "lateSingle"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._x_perfect = False
        self.spins: dict[str, IntField] = {}
        self._cells: dict[str, tuple[QVBoxLayout, QLabel, IntField]] = {}
        # lateDouble 没有输入框；X 关闭时 perfectMinus/perfectPlus 也隐藏。
        self._hidden: dict[str, int] = {key: 0 for key in ("lateDouble",)}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        self._grid = QGridLayout()
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(14)
        self._grid.setVerticalSpacing(10)
        self._grid.setColumnStretch(3, 1)
        outer.addLayout(self._grid)
        self.counts_label = QLabel()
        self.counts_label.setObjectName("Faint")
        self.counts_label.setWordWrap(True)
        outer.addWidget(self.counts_label)
        self._build_cells()
        self._arrange()
        self._refresh_counts()

    # -- 对外 -----------------------------------------------------------
    def is_x_perfect(self) -> bool:
        return self._x_perfect

    def set_x_perfect(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._x_perfect:
            return
        self._x_perfect = enabled
        if not enabled:
            # 隐藏的 -完美/完美+ 必须清零，否则会静默带进提交。
            for key in ("perfectMinus", "perfectPlus"):
                spin = self.spins.get(key)
                if spin is not None:
                    spin.blockSignals(True)
                    spin.setValue(0)
                    spin.blockSignals(False)
            self._hidden["perfectMinus"] = 0
            self._hidden["perfectPlus"] = 0
        self._arrange()
        self._refresh_counts()

    def values(self) -> dict[str, int]:
        data = {key: 0 for key in JUDGEMENT_KEYS}
        data.update(self._hidden)
        for key, spin in self.spins.items():
            data[key] = spin.value()
        return data

    def set_values(self, values: dict[str, int]) -> None:
        values = values or {}
        self._hidden["lateDouble"] = _as_int(values.get("lateDouble"))
        for key, spin in self.spins.items():
            value = _as_int(values.get(key))
            if key in ("perfectMinus", "perfectPlus") and not self._x_perfect:
                value = 0
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self._refresh_counts()

    def counts_text(self) -> str:
        """两行小字：TUF 的九格和与打击数两种口径。"""
        values = self.values()
        return (
            f"判定总数（九格）：{judgement_sum(values)}\n"
            f"打击数（排除提前 / 太慢！！）：{judgement_hit_count(values)}"
        )

    def _refresh_counts(self) -> None:
        self.counts_label.setText(self.counts_text())

    # -- 内部 -----------------------------------------------------------
    def _build_cells(self) -> None:
        for key, label, weight in JUDGEMENT_FIELDS:
            if key == "lateDouble":
                continue
            cell = QVBoxLayout()
            cell.setSpacing(3)
            name = QLabel(label)
            # 不要挂 #Muted：ID 选择器会压过 QSS 的 [tone] 规则，绿/红就没了。
            set_tone(name, "ok" if key in self._GREEN_KEYS else "bad")
            spin = IntField()
            spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
            spin.setMinimumHeight(32)
            spin.setMinimumWidth(92)
            # TUF 不显示权重，只留在 tooltip 里（标签和输入框都能看到）。
            name.setToolTip(f"{label} · 权重 ×{weight:g}")
            spin.setToolTip(f"{label} · 权重 ×{weight:g}")
            spin.textChanged.connect(self._refresh_counts)
            spin.textChanged.connect(self.changed)
            cell.addWidget(name)
            cell.addWidget(spin)
            self._cells[key] = (cell, name, spin)
            self.spins[key] = spin

    def _arrange(self) -> None:
        for cell, name, spin in self._cells.values():
            self._grid.removeItem(cell)
            name.hide()
            spin.hide()
        rows = self._X_ON_ROWS if self._x_perfect else self._X_OFF_ROWS
        for row, keys in enumerate(rows):
            for column, key in enumerate(keys):
                cell, name, spin = self._cells[key]
                self._grid.addLayout(cell, row, column)
                name.show()
                spin.show()
        self._grid.invalidate()


def _as_int(value: object) -> int:
    try:
        return int(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


class IntField(QLineEdit):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setValidator(QIntValidator(0, 999999, self))
        self.setText("0")

    def value(self) -> int:
        try:
            return int(self.text() or 0)
        except ValueError:
            return 0

    def setValue(self, value: int) -> None:
        self.setText(str(int(value)))


class ThemedCheckBox(QCheckBox):
    """带可见勾的复选框。

    主题 QSS 只画了选中态的陶土色底，这里在标准绘制之后用真实指示器
    矩形补一个 ``on_accent`` 色的勾，随主题切换取色。
    """

    _adofai_custom_paint = True

    def __init__(
        self, text: str = "", parent: QWidget | None = None
    ) -> None:
        super().__init__(text, parent)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        paint_check_mark(self)


class ChoiceCombo(QComboBox):
    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(active_palette()["muted"]), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        x = self.width() - 14
        y = self.height() // 2
        painter.drawLine(x - 4, y - 2, x, y + 2)
        painter.drawLine(x, y + 2, x + 4, y - 2)
        painter.end()


class AccuracyBadge(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        caption = QLabel("X-精准度")
        caption.setObjectName("Faint")
        self.value = QLabel("0.00%")
        self.value.setObjectName("AccuracyBig")
        self.detail = QLabel("判定总数为 0")
        self.detail.setObjectName("Muted")
        layout.addWidget(caption)
        layout.addWidget(self.value)
        layout.addWidget(self.detail)

    def set_accuracy(self, accuracy: float, total: int) -> None:
        self.value.setText(f"{accuracy:.2f}%")
        self.detail.setText(f"判定总数 {total}")


class IssueList(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.layout_box = QVBoxLayout(self)
        self.layout_box.setContentsMargins(0, 0, 0, 0)
        self.layout_box.setSpacing(5)

    def set_issues(self, issues: list[Issue]) -> None:
        while self.layout_box.count():
            item = self.layout_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if not issues:
            label = QLabel("没有发现问题")
            label.setObjectName("Faint")
            self.layout_box.addWidget(label)
            return
        colors = active_palette()
        level_colors = {
            "error": colors["danger"],
            "warn": colors["warning"],
            "ok": colors["success"],
        }
        for issue in issues:
            color = level_colors.get(issue.level, colors["muted"])
            icon = {"error": "×", "warn": "!", "ok": "√"}.get(issue.level, "·")
            row = QHBoxLayout()
            dot = QLabel(icon)
            dot.setFixedWidth(16)
            dot.setStyleSheet(f"color: {color}; font-weight: 700;")
            text = QLabel(issue.message)
            text.setWordWrap(True)
            text.setStyleSheet(f"color: {color};")
            row.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(text, 1)
            self.layout_box.addLayout(row)


def normalize_toast_level(level: str | None) -> str:
    """把历史上的各种级别写法收敛成 ok / warn / error / info。"""
    value = (level or "").strip().lower()
    if value in ("bad", "danger", "error"):
        return "error"
    if value in ("ok", "success"):
        return "ok"
    if value in ("warn", "warning"):
        return "warn"
    return "info"


def _prepare_toast_text(text: str) -> str:
    text = (text or "").strip()
    lines = text.splitlines()
    if len(lines) > 4 or len(text) > 260:
        text = "\n".join(lines[:4])
        if len(text) > 260:
            text = text[:260].rstrip()
        text = text.rstrip() + "…"
    return text


def _new_ui_timer(parent: QWidget | None = None) -> QTimer:
    """UI 倒计时用的定时器工厂；生产环境就是真实 ``QTimer``。"""
    return QTimer(parent)


# Toast 只通过这个工厂拿倒计时定时器。测试把它换成可手动推进的假时钟，
# 断言「推进 N 毫秒后收起」，不必再用 qWait 等真实时间。
_timer_factory: Callable[[QWidget | None], Any] = _new_ui_timer


def set_timer_factory_for_tests(
    factory: Callable[[QWidget | None], Any] | None,
) -> None:
    """替换 Toast 倒计时定时器；传 ``None`` 恢复真实 ``QTimer``。仅供测试。"""
    global _timer_factory
    _timer_factory = factory or _new_ui_timer


class Toast(QLabel):
    """右下角轻提示：级别归一化、排队、淡入淡出、跟随窗口尺寸。"""

    MAX_QUEUE = 4
    FADE_IN_MS = 120
    FADE_OUT_MS = 140
    MARGIN_RIGHT = 28
    MARGIN_BOTTOM = 24

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setMaximumWidth(560)
        self.setMaximumHeight(180)
        self.action_button = QPushButton("打开文件夹", self)
        self.action_button.setObjectName("ToastAction")
        self.action_button.hide()
        self.action_button.clicked.connect(self._run_action)
        self._action: Callable[[], None] | None = None
        self._card_action: Callable[[], None] | None = None
        self._queue: deque[dict] = deque(maxlen=self.MAX_QUEUE)
        self._current: dict | None = None
        self._effect: QGraphicsOpacityEffect | None = None
        self.animation: QPropertyAnimation | None = None
        self._parent_resizing = False
        self._self_hiding = False
        self.hide()
        self.timer = _timer_factory(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._fade_out)
        if parent is not None:
            parent.installEventFilter(self)

    # -- 公共接口 ---------------------------------------------------------

    def show_message(
        self,
        text: str,
        level: str = "info",
        duration: int = 3200,
        action_text: str = "",
        action: Callable[[], None] | None = None,
        *,
        card_action: Callable[[], None] | None = None,
    ) -> None:
        entry = {
            "text": _prepare_toast_text(text),
            "level": normalize_toast_level(level),
            "duration": max(400, int(duration or 3200)),
            "action_text": action_text or "",
            "action": action,
            # 整卡点击（例如「发现新版本」→ 打开发布页）；不影响内嵌按钮。
            "card_action": card_action,
        }
        if self._current is not None and not self.isVisible():
            # 窗口被外部关闭时清掉残留状态，避免新消息卡在队列里。
            self._current = None
            self._queue.clear()
            self.timer.stop()
            self._stop_animation()
        if self._current is not None:
            if entry["text"] == self._current["text"]:
                # 同一条消息重复出现时只重置倒计时，不重复排队。
                if card_action is not None:
                    self._current["card_action"] = card_action
                    self._card_action = card_action
                    self._update_cursor()
                self.timer.start(entry["duration"])
                return
            if entry["level"] == "error":
                # 错误提示优先展示，直接抢占当前消息。
                self._show_entry(entry)
                return
            self._queue.append(entry)
            return
        self._show_entry(entry)

    def dismiss(self) -> None:
        """立即收起当前提示并展示下一条。"""
        self._stop_animation()
        self.timer.stop()
        self._hide_and_advance()

    # -- 内部实现 ---------------------------------------------------------

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            # 父窗口改尺寸时实时贴右下角，而不是只在 show 时定位。
            self._parent_resizing = True
            self._reposition()
            QTimer.singleShot(0, self._clear_resize_flag)
        return super().eventFilter(obj, event)

    def _clear_resize_flag(self) -> None:
        self._parent_resizing = False

    def hideEvent(self, event) -> None:  # type: ignore[override]
        # MainWindow.resizeEvent 会顺手 hide 提示；按交互规范 resize 不应隐藏，
        # 这里在 hide 发生的那一刻恢复（不改 main_window.py）。
        if (
            self._parent_resizing
            and not self._self_hiding
            and self._current is not None
        ):
            self.show()
            self.raise_()
            self._reposition()
        super().hideEvent(event)

    def _show_entry(self, entry: dict) -> None:
        self._stop_animation()
        self._current = entry
        self.setProperty("level", entry["level"])
        repolish(self)
        self.setText(entry["text"])
        self._action = entry["action"]
        self._card_action = entry.get("card_action")
        self._update_cursor()
        self.action_button.setText(entry["action_text"] or "打开文件夹")
        self.action_button.setVisible(entry["action"] is not None)
        self.setContentsMargins(
            12, 10, 12, 40 if entry["action"] is not None else 10
        )
        self.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
            if entry["action"] is not None
            else Qt.AlignmentFlag.AlignCenter
        )
        self.adjustSize()
        self._reposition()
        animated = animations_enabled()
        if animated:
            self._ensure_effect().setOpacity(0.0)
        self.show()
        self.raise_()
        if entry["action"] is not None:
            self._place_action_button()
        if animated:
            self._animate(0.0, 1.0, self.FADE_IN_MS)
        self.timer.start(entry["duration"])

    def _update_cursor(self) -> None:
        if self._card_action is not None:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.unsetCursor()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        # 内嵌按钮会先拿到自己的点击事件，这里只处理落在卡片空白处的左键。
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._card_action is not None
        ):
            action = self._card_action
            self.dismiss()
            action()
            event.accept()
            return
        super().mousePressEvent(event)

    def _place_action_button(self) -> None:
        self.action_button.show()
        self.action_button.raise_()
        self.action_button.adjustSize()
        self.action_button.move(
            max(8, (self.width() - self.action_button.width()) // 2),
            max(8, self.height() - self.action_button.height() - 8),
        )

    def _reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        bottom = self.MARGIN_BOTTOM
        # 只查找、不创建：QMainWindow.statusBar() 会在无状态栏时懒创建一个，
        # UI-4 起主窗口改用自定义 StatusLine，这里绝不能把默认 QStatusBar 招回来。
        bar = parent.findChild(QStatusBar)
        if bar is not None and bar.isVisible():
            bottom += bar.height()
        x = parent.width() - self.width() - self.MARGIN_RIGHT
        y = parent.height() - self.height() - bottom
        self.move(max(20, x), max(20, y))

    def _ensure_effect(self) -> QGraphicsOpacityEffect:
        if self._effect is None:
            self._effect = QGraphicsOpacityEffect(self)
            self._effect.setOpacity(1.0)
            self.setGraphicsEffect(self._effect)
        return self._effect

    def _animate(
        self,
        start: float,
        end: float,
        duration: int,
        on_finished: Callable[[], None] | None = None,
    ) -> None:
        effect = self._ensure_effect()
        effect.setOpacity(start)
        animation = QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(duration)
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.animation = animation

        def _done() -> None:
            if self.animation is animation:
                self.animation = None
            animation.deleteLater()
            if on_finished is not None:
                on_finished()

        animation.finished.connect(_done)
        animation.start()

    def _stop_animation(self) -> None:
        animation = self.animation
        self.animation = None
        if animation is not None:
            animation.stop()
            animation.deleteLater()

    def _fade_out(self) -> None:
        if self._current is None:
            return
        self.timer.stop()
        if animations_enabled():
            self._animate(1.0, 0.0, self.FADE_OUT_MS, self._hide_and_advance)
        else:
            self._hide_and_advance()

    def _hide_and_advance(self) -> None:
        self._current = None
        self.action_button.hide()
        self._action = None
        self._card_action = None
        self.unsetCursor()
        self._self_hiding = True
        try:
            self.hide()
        finally:
            self._self_hiding = False
        if self._queue:
            self._show_entry(self._queue.popleft())

    def _run_action(self) -> None:
        action = self._action
        self.dismiss()
        if action is not None:
            action()


class ToastManager:
    """按窗口缓存 Toast，供对话框等没有 ``MainWindow.toast`` 的地方使用。"""

    _instance: "ToastManager | None" = None

    def __init__(self) -> None:
        self._toasts: "weakref.WeakKeyDictionary[QWidget, Toast]" = (
            weakref.WeakKeyDictionary()
        )

    @classmethod
    def instance(cls) -> "ToastManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def toast_for(self, anchor_widget: QWidget | None) -> Toast | None:
        if anchor_widget is None:
            return None
        window = anchor_widget.window()
        if window is None:
            return None
        existing = getattr(window, "toast", None)
        if isinstance(existing, Toast):
            return existing
        try:
            toast = self._toasts.get(window)
        except RuntimeError:  # pragma: no cover - 窗口已销毁
            toast = None
        if toast is None:
            toast = Toast(window)
            self._toasts[window] = toast
        return toast

    @classmethod
    def show(
        cls,
        anchor_widget: QWidget,
        text: str,
        level: str = "info",
        duration: int = 3200,
        action_text: str = "",
        action: Callable[[], None] | None = None,
        *,
        card_action: Callable[[], None] | None = None,
    ) -> Toast | None:
        toast = cls.instance().toast_for(anchor_widget)
        if toast is not None:
            toast.show_message(
                text,
                level,
                duration,
                action_text,
                action,
                card_action=card_action,
            )
        return toast


class LevelPickerDialog(QDialog):
    """TUF 多结果选谱弹窗，卡片样式与 04 谱面库的在线列表一致。

    候选最多 25 条，弹窗里直接就地筛选 / 排序，不会重新请求接口；
    ``accept`` 读的是当前列表（过滤后）的高亮项。
    """

    DIFFICULTY_FILTERS = ("全部", "P", "G", "U", "其他")
    SORT_OPTIONS = (
        "默认顺序",
        "难度 高→低",
        "难度 低→高",
        "通关数",
        "点赞数",
        "BPM",
        "时长",
        "编号",
    )

    def __init__(
        self,
        levels: list[LevelInfo],
        parent: QWidget | None = None,
        *,
        thumbnail_loader: ThumbnailLoader | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择一个 TUF 谱面")
        self.resize(760, max(360, min(560, 170 + 116 * len(levels))))
        self.selected: LevelInfo | None = None
        self._closing = False
        self._levels = list(levels)
        layout = QVBoxLayout(self)
        self.hint_label = QLabel(self._hint_text(levels))
        self.hint_label.setObjectName("Muted")
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

        # 筛选栏：关键词 + 难度段 + 排序 + 条数，全部作用在已拿到的候选上。
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("筛选：曲名 / 谱师 / 难度 / 编号")
        self.filter_input.setClearButtonEnabled(True)
        filter_row.addWidget(self.filter_input, 1)
        self.difficulty_combo = ChoiceCombo()
        self.difficulty_combo.addItems(self.DIFFICULTY_FILTERS)
        filter_row.addWidget(self.difficulty_combo)
        self.sort_combo = ChoiceCombo()
        self.sort_combo.addItems(self.SORT_OPTIONS)
        filter_row.addWidget(self.sort_combo)
        self.count_label = QLabel("")
        self.count_label.setObjectName("Faint")
        filter_row.addWidget(self.count_label)
        layout.addLayout(filter_row)
        # addItems 会触发一次 currentIndexChanged，等列表建好再接线。
        self.filter_input.textChanged.connect(self._apply_filters)
        self.difficulty_combo.currentIndexChanged.connect(self._apply_filters)
        self.sort_combo.currentIndexChanged.connect(self._apply_filters)

        self.model = ChartListModel(self)

        self.delegate = ChartCardDelegate(self)
        self.delegate.set_theme(active_theme())
        self.delegate.set_actions_visible(False)
        self.thumbnail_loader = (
            thumbnail_loader
            if thumbnail_loader is not None
            else shared_thumbnail_loader()
        )
        self.thumbnail_loader.thumbnailReady.connect(self._on_thumbnail_ready)
        self.delegate.set_thumbnail_loader(self.thumbnail_loader)

        self.list = QListView()
        self.list.setObjectName("ChartList")
        self.list.setModel(self.model)
        self.list.setItemDelegate(self.delegate)
        self.list.setUniformItemSizes(True)
        self.list.setMouseTracking(True)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.activated.connect(lambda _index: self.accept())
        self.list.doubleClicked.connect(lambda _index: self.accept())
        layout.addWidget(self.list, 1)

        self.empty = QLabel(self._empty_text())
        self.empty.setObjectName("ChartEmpty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText("使用这个谱面")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._apply_filters()

    # -- 筛选 / 排序 --------------------------------------------------------

    def _on_thumbnail_ready(self, tuf_id: int) -> None:
        """曲绘到货后只刷新对应候选行。"""
        model = self.model
        for row in range(model.rowCount()):
            item = model.item_at(row)
            if item is not None and item.level.tuf_id == tuf_id:
                self.list.update(model.index(row, 0))

    def _apply_filters(self, *_args) -> None:
        """按当前关键词与难度段过滤、排序，并同步列表、空状态与按钮。"""
        previous = self._current_level()
        shown = self._sorted_levels(self._filtered_levels())
        self.model.set_items([ChartItem(level=level, source="tuf") for level in shown])
        self.count_label.setText(f"显示 {len(shown)} / 共 {len(self._levels)} 条")
        if shown:
            self.list.setVisible(True)
            self.empty.setVisible(False)
            row = 0
            if previous is not None:
                for index, level in enumerate(shown):
                    if level is previous or (
                        level.tuf_id is not None and level.tuf_id == previous.tuf_id
                    ):
                        row = index
                        break
            self.list.setCurrentIndex(self.model.index(row, 0))
            self.ok_button.setEnabled(True)
        else:
            self.list.setVisible(False)
            self.empty.setText(self._empty_text())
            self.empty.setVisible(True)
            self.list.setCurrentIndex(QModelIndex())
            self.ok_button.setEnabled(False)

    def _current_level(self) -> LevelInfo | None:
        index = self.list.currentIndex()
        if not index.isValid():
            return None
        item = self.model.item_at(index.row())
        return item.level if item is not None else None

    def _empty_text(self) -> str:
        if not self._levels:
            return "没有匹配的谱面"
        return "没有匹配的候选，清空筛选或直接输入编号精确查询"

    def _filtered_levels(self) -> list[LevelInfo]:
        keyword = self.filter_input.text().strip().casefold()
        band = self.difficulty_combo.currentText()
        result: list[LevelInfo] = []
        for level in self._levels:
            if band != "全部":
                category = self._difficulty_group(level)
                if band == "其他":
                    if category in ("P", "G", "U"):
                        continue
                elif category != band:
                    continue
            if keyword and not self._matches_keyword(level, keyword):
                continue
            result.append(level)
        return result

    @staticmethod
    def _matches_keyword(level: LevelInfo, keyword: str) -> bool:
        fields = (
            level.song,
            level.charter,
            level.difficulty,
            "" if level.tuf_id is None else str(level.tuf_id),
        )
        return keyword in " ".join(fields).casefold()

    @staticmethod
    def _difficulty_group(level: LevelInfo) -> str:
        """复用 level_paths 的难度分类：P/G/U，其余归「其他」。"""
        category, _band = classify_difficulty(level.difficulty or "")
        return category if category in ("P", "G", "U") else "其他"

    def _sorted_levels(self, levels: list[LevelInfo]) -> list[LevelInfo]:
        mode = self.sort_combo.currentText()
        if mode == "默认顺序" or len(levels) < 2:
            return list(levels)
        if mode == "编号":
            return self._order_by(levels, lambda level: level.tuf_id, descending=False)
        if mode in ("难度 高→低", "难度 低→高"):
            return self._order_by(
                levels,
                self._difficulty_rank,
                descending=mode == "难度 高→低",
            )
        getters = {
            "通关数": lambda level: level.clears,
            "点赞数": lambda level: level.likes,
            "BPM": lambda level: level.bpm,
            "时长": lambda level: level.duration_ms,
        }
        getter = getters.get(mode)
        if getter is None:
            return list(levels)
        return self._order_by(levels, getter, descending=True)

    @staticmethod
    def _difficulty_rank(level: LevelInfo) -> int | None:
        """P1-P20 / G1-G20 / U1-U20 → 1-60；无级别或特殊难度返回 None。"""
        return difficulty_value({"difficulty": level.difficulty or ""})

    @staticmethod
    def _order_by(
        levels: list[LevelInfo],
        getter: Callable[[LevelInfo], Any],
        descending: bool,
    ) -> list[LevelInfo]:
        """数值排序；缺数据的项无论升降序都排在最后，同值保持原有顺序。"""
        paired = list(enumerate(levels))

        def key(pair):
            index, level = pair
            value = getter(level)
            if value is None:
                return (1, 0, index)
            return (0, -value if descending else value, index)

        paired.sort(key=key)
        return [level for _, level in paired]

    @staticmethod
    def _hint_text(levels: list[LevelInfo]) -> str:
        """候选总数 + 「已知编号可直接输入编号精确查询」的引导。"""
        if not levels:
            return (
                "没有匹配的谱面，换个关键词再试；"
                "已知编号可直接输入编号精确查询（例如 17776）。"
            )
        return (
            f"找到 {len(levels)} 个候选（TUF 单次最多返回 {DEFAULT_SEARCH_LIMIT} 条）。"
            "服务器已按搜索语法过滤（如 song:… 、bpm:>150），本地筛选只作用于返回结果。\n"
            "单击选中，双击或按回车直接使用；已知编号可直接输入编号精确查询。"
        )

    def accept(self) -> None:
        if self._closing:
            return
        index = self.list.currentIndex()
        # 只看当前（过滤后）列表的高亮项；空结果时按回车不当作已选择。
        item = self.model.item_at(index.row()) if index.isValid() else None
        if item is None:
            return
        self._closing = True
        self.selected = item.level
        super().accept()


class SearchSyntaxDialog(QDialog):
    """TUF 搜索语法速查：字段分组、运算符与三个示例，只读不写。"""

    OPERATORS = (
        ("字段:值", "包含该值（部分匹配），例如 song:carnation"),
        ("字段=值", "完全匹配，例如 id=1234"),
        (",", "同时满足（AND），例如 song:hello, artist:camellia"),
        ("|", "任一满足（OR），例如 song=goTe | song:xnor"),
        ("\\!", "排除包含该词（NOT）"),
        ("> < >= <=", "数值比较，例如 bpm:>150"),
    )
    EXAMPLES = (
        ("song:hello, artist:camellia", "曲名含 hello，且曲师是 camellia"),
        ("bpm:>100, bpm:<200", "BPM 在 100 到 200 之间"),
        ("id=1234 / #1234", "按 TUF 编号精确查"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("TUF 搜索语法")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        title = QLabel("TUF 搜索语法")
        title.setObjectName("CardTitle")
        layout.addWidget(title)
        intro = QLabel(
            "搜索框内容会原样交给 TUF 服务器，支持字段过滤与组合条件，不区分大小写。"
        )
        intro.setObjectName("CardHint")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        groups = (
            ("文本字段", "、".join(ADVANCED_TEXT_FIELDS)),
            ("数值字段", "、".join(ADVANCED_NUMERIC_FIELDS)),
            ("编号 / 链接", "、".join(ADVANCED_ID_FIELDS) + "，另有 #编号 简写"),
        )
        for row, (heading, body) in enumerate(groups):
            name = QLabel(heading)
            name.setObjectName("Muted")
            name.setWordWrap(True)
            value = QLabel(body)
            value.setObjectName("Mono")
            value.setWordWrap(True)
            grid.addWidget(name, row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(value, row, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        layout.addWidget(hline())

        ops_title = QLabel("运算符")
        ops_title.setObjectName("Muted")
        layout.addWidget(ops_title)
        ops_grid = QGridLayout()
        ops_grid.setHorizontalSpacing(16)
        ops_grid.setVerticalSpacing(4)
        for row, (symbol, meaning) in enumerate(self.OPERATORS):
            code = QLabel(symbol)
            code.setObjectName("Mono")
            text = QLabel(meaning)
            text.setObjectName("Muted")
            text.setWordWrap(True)
            ops_grid.addWidget(code, row, 0, Qt.AlignmentFlag.AlignTop)
            ops_grid.addWidget(text, row, 1)
        ops_grid.setColumnStretch(1, 1)
        layout.addLayout(ops_grid)

        layout.addWidget(hline())

        examples_title = QLabel("示例")
        examples_title.setObjectName("Muted")
        layout.addWidget(examples_title)
        for code_text, meaning in self.EXAMPLES:
            row = QHBoxLayout()
            row.setSpacing(12)
            code = QLabel(code_text)
            code.setObjectName("Mono")
            code.setWordWrap(True)
            code.setMinimumWidth(230)
            text = QLabel(meaning)
            text.setObjectName("Muted")
            text.setWordWrap(True)
            row.addWidget(code, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(text, 1)
            layout.addLayout(row)

        note = QLabel(
            "用了字段语法后只查一次，不会再去掉括号或标点重搜；值里可以带空格，"
            "A,B|C,D 表示 (A 且 B) 或 (C 且 D)。"
        )
        note.setObjectName("Faint")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("知道了")
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class ClickableLabel(QLabel):
    """可点击的 QLabel（TopBar 登录芯片用）：手型光标 + ``clicked`` 信号。"""

    clicked = Signal()

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


def chip(text: str, level: str = "info", *, clickable: bool = False) -> QLabel:
    label = ClickableLabel(text) if clickable else QLabel(text)
    label.setObjectName({"ok": "ChipOk", "warn": "ChipWarn", "bad": "ChipBad"}.get(level, "Chip"))
    return label


def hline() -> Divider:
    return Divider()
