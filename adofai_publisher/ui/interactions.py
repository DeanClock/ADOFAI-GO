"""全局交互层。

这一层只做“输入与反馈”，不改变任何页面结构：

* :class:`PointerCursorFilter`：能点的控件自动显示手型光标，禁用时恢复箭头；
  同时给各页面现有的 ``QCheckBox`` 补上可见的勾选勾；并拦截会吃滚轮改值的
  控件（下拉框 / 数值框 / 滑条 / 旋钮 / 标签栏）的滚轮，把滚动交给页面或列表，
  任何情况下都不让滚轮改变控件值。
* :func:`set_tone` / :func:`repolish`：写入语义属性并即时重绘。
* :func:`animations_enabled`：统一的动效开关（离屏测试与减弱动效时关闭）。

设计约束：不修改 ``theme.py`` 的 QSS、不替换应用样式、不碰业务逻辑。
"""

from __future__ import annotations

import atexit
import hashlib
import os
import tempfile
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QStandardPaths, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDial,
    QRadioButton,
    QSlider,
    QStyle,
    QStyleOptionButton,
    QTabBar,
    QWidget,
)

from .theme import active_palette, theme_bus

try:  # PySide 自带，用于判断包装对象是否已被删除
    from shiboken6 import isValid as _is_valid
except Exception:  # pragma: no cover - 极端环境下的降级

    def _is_valid(obj: object) -> bool:
        return obj is not None

# 需要手型光标的控件类型。输入类控件（QLineEdit / QTextEdit / QSpinBox）刻意不在此列。
POINTER_WIDGET_TYPES = (QAbstractButton, QComboBox, QCheckBox, QRadioButton)

# 吃滚轮会改值的控件：滚轮一律交给页面/列表滚动，绝不修改控件自身。
# 不含 QScrollBar（滚轮要滚滚动条），也不含 QAbstractItemView / QTextEdit
# 等本来就靠滚轮滚动的控件。
WHEEL_GUARD_TYPES = (QComboBox, QAbstractSpinBox, QSlider, QDial, QTabBar)


SCROLL_AREA_TYPES = (QAbstractScrollArea,)


def _scrollable_child(parent: QWidget) -> QAbstractScrollArea | None:
    """在祖先的子树里找第一个能滚的视图（页面级滚动区不存在时的兜底）。

    谱面库这类页面没有外层滚动区，下拉框和 ``QListView`` 是兄弟：
    优先拿纵向有范围的视图，都没有范围时退回第一个可见的滚动视图。
    """

    best: QAbstractScrollArea | None = None
    fallback: QAbstractScrollArea | None = None
    for child in parent.findChildren(QAbstractScrollArea):
        if child is None or not _is_valid(child) or child.isHidden():
            continue
        try:
            scrollable = child.verticalScrollBar().maximum() > 0
        except (RuntimeError, AttributeError):
            continue
        if scrollable and child.isVisibleTo(parent):
            return child
        if scrollable and best is None:
            best = child
        if fallback is None and child.isVisibleTo(parent):
            fallback = child
    return best or fallback


def nearest_scroll_area(widget: QWidget) -> QAbstractScrollArea | None:
    """沿父链找最近的滚动视图（``QScrollArea`` / ``QListView`` / ``QTableView`` 都算）。

    先把 ``QAbstractScrollArea`` 当滚动区用，覆盖页面级 ``QScrollArea`` 和列表视图；
    父链上没有时，再逐级看祖先子树里的滚动视图，让「下拉框 + 列表」这种兄弟布局
    也能把滚轮转给列表，而不是被拦下来吞掉。
    """

    chain: list[QWidget] = []
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, SCROLL_AREA_TYPES):
            return parent
        chain.append(parent)
        parent = parent.parentWidget()
    for ancestor in chain:
        candidate = _scrollable_child(ancestor)
        if candidate is not None:
            return candidate
    return None


def forward_wheel_to_scroll_area(
    widget: QWidget, area: QAbstractScrollArea, event: QWheelEvent
) -> bool:
    """把滚轮事件按其窗口坐标转交给滚动区视口，让页面照常滚动。"""
    viewport = area.viewport()
    if viewport is None or not _is_valid(viewport):
        return False
    local = event.position()
    target = viewport.mapFrom(widget, local.toPoint())
    cloned = QWheelEvent(
        QPointF(target),
        event.globalPosition(),
        event.pixelDelta(),
        event.angleDelta(),
        event.buttons(),
        event.modifiers(),
        event.phase(),
        event.inverted(),
        event.source(),
    )
    QApplication.sendEvent(viewport, cloned)
    return True


TONE_VALUES = ("ok", "warn", "bad", "muted", "faint")

_TONE_ALIASES = {
    "ok": "ok",
    "success": "ok",
    "warn": "warn",
    "warning": "warn",
    "bad": "bad",
    "danger": "bad",
    "error": "bad",
    "muted": "muted",
    "faint": "faint",
}

_FILTER_ATTR = "_adofai_interactions_filter"


def _is_pointer_widget(widget: QWidget) -> bool:
    return isinstance(widget, POINTER_WIDGET_TYPES)


def animations_enabled() -> bool:
    """是否允许播放动效。

    离屏平台（pytest / 截图）与显式 ``ADOFAI_REDUCE_MOTION=1`` 时一律关闭。
    """
    if os.environ.get("ADOFAI_REDUCE_MOTION") == "1":
        return False
    platform = os.environ.get("QT_QPA_PLATFORM", "").strip().lower()
    platform = platform.split(":", 1)[0]
    if platform == "offscreen":
        return False
    return True


def repolish(widget: QWidget) -> None:
    """属性变化后让 QSS 立即生效。"""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def set_tone(widget: QWidget, tone: str | None) -> None:
    """写入语义色调属性（ok / warn / bad / muted / faint / None）。"""
    value = _TONE_ALIASES.get((tone or "").strip().lower())
    widget.setProperty("tone", value)
    repolish(widget)


class PointerCursorFilter(QObject):
    """全局交互过滤器：手型光标 + 复选框勾选纹理 + 滚轮防误操作。

    三件事合在同一个过滤器里，是为了让每个 Qt 事件只走一次 Python 回调；
    分开装多个过滤器会让整个 UI 测试的耗时明显上升。
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if QApplication.closingDown():
            return False
        etype = event.type()
        if etype == QEvent.Type.Wheel and isinstance(obj, WHEEL_GUARD_TYPES):
            return self.guard_wheel(obj, event)  # type: ignore[arg-type]
        if etype in (QEvent.Type.Show, QEvent.Type.Polish, QEvent.Type.EnabledChange):
            # Polish 是控件构造完成后第一个能拿到正确类型的信号（ChildAdded
            # 里的子对象还是半构造状态，不能判别类型也不能延迟捕获）。
            if isinstance(obj, QWidget):
                self.apply(obj)
                if etype != QEvent.Type.EnabledChange and isinstance(obj, QCheckBox):
                    apply_check_mark_style(obj)
        elif etype in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange):
            # 主题切换时 Qt 会给所有控件发 StyleChange，用来刷新勾选纹理。
            if isinstance(obj, QCheckBox):
                apply_check_mark_style(obj)
        return super().eventFilter(obj, event)

    def guard_wheel(self, widget: QWidget, event: QWheelEvent) -> bool:
        """滚轮永远不改变控件值；返回是否已拦下事件。

        无论控件是否获得焦点（用户点过与否），都把滚动转交给最近的滚动视图
        （页面滚动区 / 列表视图都算）；找不到滚动区就吞掉，避免在弹窗里
        误改选项。滚轮只用于页面和列表滚动。
        """
        if not _is_valid(widget):
            return False
        area = nearest_scroll_area(widget)
        if area is None:
            return True
        forward_wheel_to_scroll_area(widget, area, event)
        return True

    def apply(self, widget: QWidget) -> bool:
        """给单个控件应用光标规则；返回是否改动了光标。"""
        if not _is_valid(widget) or not _is_pointer_widget(widget):
            return False
        if widget.property("noPointer") or widget.property("pointerExplicit"):
            return False
        shape = (
            Qt.CursorShape.PointingHandCursor
            if widget.isEnabled()
            else Qt.CursorShape.ArrowCursor
        )
        if widget.cursor().shape() == shape:
            return False
        widget.setCursor(shape)
        return True

    def apply_all(self, app: QApplication) -> None:
        if QApplication.closingDown():
            return
        for widget in app.allWidgets():
            self.apply(widget)
            apply_check_mark_style(widget)


def _check_asset_path(color: str) -> str:
    """为指定颜色准备一张勾选纹理，返回 QSS 可用的路径。"""
    digest = hashlib.sha1(color.encode("utf-8")).hexdigest()[:10]
    cache_root = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.CacheLocation
    )
    folder = Path(cache_root) / "adofai-go" if cache_root else Path(
        tempfile.gettempdir()
    ) / "adofai-go"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"check-{digest}.png"
    if path.is_file():
        return path.as_posix()

    size, ratio = 16, 2
    pixmap = QPixmap(size * ratio, size * ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(color), 2.0 * ratio)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        side = size * ratio
        painter.drawPolyline(
            QPolygonF(
                [
                    QPointF(side * 0.22, side * 0.53),
                    QPointF(side * 0.42, side * 0.74),
                    QPointF(side * 0.80, side * 0.28),
                ]
            )
        )
    finally:
        painter.end()
    pixmap.save(str(path))
    return path.as_posix()


def apply_check_mark_style(widget: QWidget) -> bool:
    """给普通 ``QCheckBox`` 注入勾选纹理（不覆盖原有的控件样式表）。"""
    if not _is_valid(widget) or not isinstance(widget, QCheckBox):
        return False
    if getattr(widget, "_adofai_custom_paint", False):
        return False
    color = active_palette()["selection_text"]
    if widget.property("_adofaiCheckColor") == color:
        return False
    try:
        asset = _check_asset_path(color)
    except OSError:
        return False
    base = widget.property("_adofaiBaseStyleSheet")
    if base is None:
        base = widget.styleSheet()
        widget.setProperty("_adofaiBaseStyleSheet", base)
    widget.setProperty("_adofaiCheckColor", color)
    rule = f'QCheckBox::indicator:checked {{ image: url("{asset}"); }}'
    widget.setStyleSheet(f"{base}\n{rule}" if base else rule)
    return True


def check_indicator_rect(widget: QCheckBox) -> QRect:
    """取复选框指示器的真实矩形（受 QSS 宽高影响）。"""
    option = QStyleOptionButton()
    option.initFrom(widget)
    option.rect = widget.rect()
    if widget.isChecked():
        option.state |= QStyle.StateFlag.State_On
    return widget.style().subElementRect(
        QStyle.SubElement.SE_CheckBoxIndicator, option, widget
    )


def paint_check_mark(widget: QCheckBox, color: str | None = None) -> None:
    """在复选框指示器中央画一个圆头勾。颜色默认取 ``on_accent``。"""
    if not widget.isChecked():
        return
    rect = check_indicator_rect(widget)
    if rect.width() < 8 or rect.height() < 8:
        return
    pen = QPen(QColor(color or active_palette()["selection_text"]), 2.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter = QPainter(widget)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolyline(
            QPolygonF(
                [
                    QPointF(
                        rect.x() + rect.width() * 0.22,
                        rect.y() + rect.height() * 0.53,
                    ),
                    QPointF(
                        rect.x() + rect.width() * 0.42,
                        rect.y() + rect.height() * 0.74,
                    ),
                    QPointF(
                        rect.x() + rect.width() * 0.80,
                        rect.y() + rect.height() * 0.28,
                    ),
                ]
            )
        )
    finally:
        painter.end()


def uninstall_interactions(app: QApplication | None = None) -> None:
    """移除事件过滤器与主题订阅（退出前调用，避免 Qt 销毁阶段回调 Python）。"""
    if app is None:
        app = QApplication.instance()
    if app is None or not _is_valid(app):
        return
    pointer = getattr(app, _FILTER_ATTR, None)
    try:
        if pointer is not None:
            app.removeEventFilter(pointer)
    except RuntimeError:  # pragma: no cover - 解释器退出阶段的兜底
        pass
    slot = getattr(app, "_adofai_check_theme_slot", None)
    if slot is not None:
        try:
            theme_bus().changed.disconnect(slot)
        except (RuntimeError, TypeError):  # pragma: no cover
            pass
    for name in (
        _FILTER_ATTR,
        "_adofai_check_theme_slot",
        "_adofai_check_detach",
    ):
        try:
            delattr(app, name)
        except AttributeError:
            pass


def install_interactions(app: QApplication | None = None) -> PointerCursorFilter | None:
    """把交互层安装到 QApplication 上；重复调用幂等。"""
    if app is None:
        app = QApplication.instance()
    if app is None:
        return None
    existing = getattr(app, _FILTER_ATTR, None)
    if existing is not None:
        return existing

    pointer = PointerCursorFilter(app)
    app.installEventFilter(pointer)
    setattr(app, _FILTER_ATTR, pointer)

    # 主题切换后重新生成勾选纹理，保持与 on_accent 一致。
    def _refresh_check_marks(_theme: str) -> None:
        if QApplication.closingDown() or not _is_valid(app):
            return
        for widget in app.allWidgets():
            apply_check_mark_style(widget)

    theme_bus().changed.connect(_refresh_check_marks)
    setattr(app, "_adofai_check_theme_slot", _refresh_check_marks)

    def _detach() -> None:
        uninstall_interactions(app)

    app.aboutToQuit.connect(_detach)
    setattr(app, "_adofai_check_detach", _detach)
    atexit.register(_detach)

    pointer.apply_all(app)
    return pointer
