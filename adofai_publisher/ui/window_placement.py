"""主窗口位置恢复的纯逻辑。

这个模块刻意不导入 Qt：调用方把 ``QScreen.availableGeometry()`` 换算成
:class:`Rect` 传进来，测试就能直接构造纵向 / 横向多屏布局，不需要真实显示器。

背景：以前窗口位置完全交给 Windows / Qt 自己决定。测试者把窗口拖到
「上下排列」布局里下面那块屏的底边，重启后窗口跑到了上面那块屏的外侧。
现在由我们自己保存窗口矩形，启动时先校验再恢复，跑出屏幕就回落到主屏。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

DEFAULT_WINDOW_SIZE = (1280, 860)
MIN_WINDOW_SIZE = (1080, 720)

# 恢复位置前，窗口顶部（标题栏）至少要有这么多像素落在某块屏幕的可用区域里，
# 否则视为「跑到屏幕外」，不恢复，改用主屏居中。宽度也够用户抓得住标题栏。
MIN_VISIBLE_TITLE = (160, 60)
# 参与可见性判断的顶部条带高度；超过这段的区域不算「看得到标题栏」。
TITLE_STRIP_HEIGHT = 60


@dataclass(frozen=True)
class Rect:
    """屏幕或窗口的矩形，单位都是逻辑像素，原点在虚拟桌面左上角。"""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    def intersection(self, other: "Rect") -> tuple[int, int]:
        """两个矩形重叠部分的宽高；不相交时返回 (0, 0)。"""
        width = min(self.right, other.right) - max(self.x, other.x)
        height = min(self.bottom, other.bottom) - max(self.y, other.y)
        return (max(0, width), max(0, height))

    def intersection_area(self, other: "Rect") -> int:
        width, height = self.intersection(other)
        return width * height

    def as_list(self) -> list[int]:
        return [self.x, self.y, self.width, self.height]

    def with_size(self, width: int, height: int) -> "Rect":
        return Rect(self.x, self.y, width, height)

    def centered_in(self, area: "Rect") -> "Rect":
        return Rect(
            area.x + (area.width - self.width) // 2,
            area.y + (area.height - self.height) // 2,
            self.width,
            self.height,
        )

    def contains(self, other: "Rect") -> bool:
        return (
            other.x >= self.x
            and other.y >= self.y
            and other.right <= self.right
            and other.bottom <= self.bottom
        )


@dataclass(frozen=True)
class Placement:
    """一次恢复计算的结果。

    ``minimum_size`` 是在这块屏幕上可以安全使用的最小尺寸：屏幕比 1080×720
    还小时会临时放宽，避免标题栏被最小尺寸顶出屏幕。
    """

    rect: Rect
    minimum_size: tuple[int, int]
    fell_back: bool = False


def rect_from_value(value: Any) -> Rect | None:
    """把 settings.json 里的值解析成 :class:`Rect`；非法或空值返回 None。"""
    if isinstance(value, dict):
        try:
            values = [value["x"], value["y"], value["width"], value["height"]]
        except KeyError:
            return None
    elif isinstance(value, (list, tuple)) and len(value) == 4:
        values = list(value)
    else:
        return None
    try:
        numbers = [int(item) for item in values]
    except (TypeError, ValueError):
        return None
    rect = Rect(*numbers)
    return None if rect.is_empty else rect


def clamp_int(value: int, low: int, high: int) -> int:
    if high < low:
        return low
    return max(low, min(high, value))


def _sanitize_size(size: Sequence[int]) -> tuple[int, int]:
    width = max(1, int(size[0]))
    height = max(1, int(size[1]))
    return width, height


def _fitted_size(
    desired: Sequence[int],
    screen: Rect,
    min_size: Sequence[int],
) -> tuple[int, int]:
    """把期望尺寸裁进屏幕：不小于最小尺寸（屏幕放得下时），也不超过屏幕。"""
    width, height = _sanitize_size(desired)
    min_width, min_height = _sanitize_size(min_size)
    width = min(max(width, min_width), max(1, screen.width))
    height = min(max(height, min_height), max(1, screen.height))
    return width, height


def _relaxed_minimum(rect: Rect, min_size: Sequence[int]) -> tuple[int, int]:
    min_width, min_height = _sanitize_size(min_size)
    return (min(min_width, rect.width), min(min_height, rect.height))


def _visible_title_screen(
    saved: Rect,
    screens: Sequence[Rect],
    title_visible: Sequence[int],
) -> Rect | None:
    """找出标题栏区域足够可见的那块屏幕；没有就返回 None。"""
    min_width = max(1, int(title_visible[0]))
    min_height = max(1, int(title_visible[1]))
    strip = Rect(
        saved.x,
        saved.y,
        saved.width,
        min(saved.height, TITLE_STRIP_HEIGHT),
    )
    best: Rect | None = None
    best_key: tuple[int, int] | None = None
    for screen in screens:
        visible_width, visible_height = strip.intersection(screen)
        if visible_width < min_width or visible_height < min_height:
            continue
        # 先比窗口与屏幕的整体重叠面积（少裁一点），再比标题栏可见面积。
        key = (saved.intersection_area(screen), visible_width * visible_height)
        if best_key is None or key > best_key:
            best = screen
            best_key = key
    return best


def title_bar_visible(
    rect: Rect,
    screens: Iterable[Rect],
    title_visible: Sequence[int] = MIN_VISIBLE_TITLE,
) -> bool:
    """窗口顶部（标题栏）在任意一块屏幕上是否留有足够的可见区域。"""
    available = [screen for screen in screens if not screen.is_empty]
    return _visible_title_screen(rect, available, title_visible) is not None


def _fit_inside(
    saved: Rect,
    screen: Rect,
    min_size: Sequence[int],
) -> Rect:
    width, height = _fitted_size((saved.width, saved.height), screen, min_size)
    x = clamp_int(saved.x, screen.x, screen.right - width)
    y = clamp_int(saved.y, screen.y, screen.bottom - height)
    return Rect(x, y, width, height)


def resolve_window_rect(
    saved: Rect | None,
    screens: Iterable[Rect],
    *,
    primary_index: int = 0,
    default_size: Sequence[int] = DEFAULT_WINDOW_SIZE,
    min_size: Sequence[int] = MIN_WINDOW_SIZE,
    title_visible: Sequence[int] = MIN_VISIBLE_TITLE,
) -> Placement:
    """根据保存的窗口矩形和当前屏幕布局，算出最终应该使用的窗口矩形。

    - 保存的矩形在某块屏幕上标题栏足够可见 → 留在那块屏，并整体钳进屏幕内；
    - 否则（屏幕断开、坐标在屏幕外、布局上下翻转等）→ 主屏可用区域居中，
      使用默认尺寸。
    """
    available = [screen for screen in screens if not screen.is_empty]
    if not available:
        width, height = _sanitize_size(default_size)
        return Placement(
            Rect(0, 0, width, height),
            _relaxed_minimum(Rect(0, 0, width, height), min_size),
            True,
        )

    index = clamp_int(int(primary_index), 0, len(available) - 1)
    primary = available[index]

    if saved is not None and not saved.is_empty:
        target = _visible_title_screen(saved, available, title_visible)
        if target is not None:
            rect = _fit_inside(saved, target, min_size)
            return Placement(rect, _relaxed_minimum(rect, min_size), False)

    width, height = _fitted_size(default_size, primary, min_size)
    centered = Rect(0, 0, width, height).centered_in(primary)
    return Placement(centered, _relaxed_minimum(centered, min_size), True)
