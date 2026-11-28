"""侧边栏线性图标。

全部用 QPainterPath 画在 18×18 的逻辑画布里，按 ``(name, color, size, dpr)``
缓存成 QPixmap。不引入图片资源，换主题时颜色跟着 palette 走。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap

_S = 18.0  # 设计画布边长

_CACHE: dict[tuple[str, str, int, float], QPixmap] = {}


def _path(name: str) -> QPainterPath:
    path = QPainterPath()
    if name == "home":
        path.moveTo(3, 8)
        path.lineTo(9, 3)
        path.lineTo(15, 8)
        path.moveTo(4.6, 8)
        path.lineTo(4.6, 15)
        path.lineTo(13.4, 15)
        path.lineTo(13.4, 8)
        path.moveTo(7.4, 15)
        path.lineTo(7.4, 11)
        path.lineTo(10.6, 11)
        path.lineTo(10.6, 15)
    elif name == "workspace":
        for x, y in ((3.2, 3.2), (10.2, 3.2), (3.2, 10.2), (10.2, 10.2)):
            path.addRoundedRect(QRectF(x, y, 4.6, 4.6), 1.2, 1.2)
    elif name == "preview":
        path.moveTo(2.4, 9)
        path.cubicTo(5.2, 5.2, 7.0, 4.4, 9, 4.4)
        path.cubicTo(11.0, 4.4, 12.8, 5.2, 15.6, 9)
        path.cubicTo(12.8, 12.8, 11.0, 13.6, 9, 13.6)
        path.cubicTo(7.0, 13.6, 5.2, 12.8, 2.4, 9)
        path.addEllipse(QPointF(9, 9), 2.2, 2.2)
    elif name == "history":
        path.addEllipse(QPointF(9, 9), 6.2, 6.2)
        path.moveTo(9, 5.6)
        path.lineTo(9, 9)
        path.lineTo(11.8, 10.6)
    elif name == "library":
        for y in (5.0, 9.0, 13.0):
            path.moveTo(4.2, y)
            path.lineTo(13.8, y)
        path.moveTo(5.2, 3.2)
        path.lineTo(5.2, 15.0)
    elif name in ("modules", "umm"):
        # 侧边栏的导航键是 "umm"，「模组」用的是一枚拼图线稿：
        # 圆角方形本体，顶部一个凸起（knob），右侧一个凹槽（notch）。
        left, right = 3.0, 15.0
        top, bottom = 4.9, 15.2
        radius = 1.4
        kappa = radius * 0.5523  # 圆角用三次贝塞尔逼近四分之一圆
        knob_left, knob_right = 7.0, 11.0
        notch_top, notch_bottom = 9.0, 12.2
        notch_bulge = (notch_bottom - notch_top) * 2.0 / 3.0

        path.moveTo(left + radius, top)
        path.lineTo(knob_left, top)
        path.cubicTo(knob_left + 0.3, 2.1, knob_right - 0.3, 2.1, knob_right, top)
        path.lineTo(right - radius, top)
        path.cubicTo(
            right - radius + kappa, top,
            right, top + radius - kappa,
            right, top + radius,
        )
        path.lineTo(right, notch_top)
        path.cubicTo(
            right - notch_bulge, notch_top,
            right - notch_bulge, notch_bottom,
            right, notch_bottom,
        )
        path.lineTo(right, bottom - radius)
        path.cubicTo(
            right, bottom - radius + kappa,
            right - radius + kappa, bottom,
            right - radius, bottom,
        )
        path.lineTo(left + radius, bottom)
        path.cubicTo(
            left + radius - kappa, bottom,
            left, bottom - radius + kappa,
            left, bottom - radius,
        )
        path.lineTo(left, top + radius)
        path.cubicTo(
            left, top + radius - kappa,
            left + radius - kappa, top,
            left + radius, top,
        )
        path.closeSubpath()
    else:  # settings：齿轮（以前和「模组」共用滑杆造型，后来改掉）
        center = QPointF(9, 9)
        path.addEllipse(center, 4.4, 4.4)
        path.addEllipse(center, 1.8, 1.8)
        for index in range(8):
            angle = math.pi / 4 * index
            dx, dy = math.cos(angle), math.sin(angle)
            path.moveTo(9 + dx * 4.4, 9 + dy * 4.4)
            path.lineTo(9 + dx * 6.4, 9 + dy * 6.4)
    return path


def line_icon(name: str, color: str, size: int = 18, dpr: float = 1.0) -> QPixmap:
    """返回缓存好的线性图标；``dpr`` 用于高分屏。"""
    dpr = max(1.0, float(dpr))
    key = (name, str(color), int(size), round(dpr, 2))
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    pixmap = QPixmap(int(round(size * dpr)), int(round(size * dpr)))
    pixmap.fill(Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(dpr)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.scale(size / _S, size / _S)
        pen = QPen(QColor(color), 1.55)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(_path(name))
    finally:
        painter.end()
    _CACHE[key] = pixmap
    return pixmap


def clear_cache() -> None:
    _CACHE.clear()
