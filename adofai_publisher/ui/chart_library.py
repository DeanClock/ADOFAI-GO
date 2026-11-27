from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from PySide6.QtCore import QAbstractListModel, QEvent, QModelIndex, QRect, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QListView,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionButton,
    QStyleOptionViewItem,
    QStylePainter,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ..download_tasks import (
    ACTIVE_STATUSES,
    PENDING_STATUSES,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_DOWNLOADING,
    STATUS_EXTRACTING,
    STATUS_FAILED,
    STATUS_RUNNING,
    UNFINISHED_STATUSES,
    DownloadTask,
)
from ..ard_parser import list_chart_variants
from ..models import LevelInfo
from ..difficulty_icons import difficulty_pixmap
from ..level_library import entry_variant, is_tuf_bound, variant_container
from ..level_paths import CATEGORY_LABELS, category_color, entry_band, entry_category
from ..submission_status import is_tuf_submitted
from ..thumbnails import ThumbnailLoader, shared_thumbnail_loader
from ..tuf_review import review_text
from ..tuf_browse import (
    SERVER_PAGE_LIMIT,
    SORT_OPTIONS,
    SPECIAL_DIFFICULTIES,
    Q_SPECIAL_DIFFICULTIES,
    pgu_name,
    q_bucket_label,
)
from .theme import active_palette, active_theme, metrics, palette


def _choice_combo() -> QComboBox:
    """延迟导入 ChoiceCombo：widgets 依赖本模块，顶层互导会成环。"""
    from .widgets import ChoiceCombo

    return ChoiceCombo()


class ElidedLabel(QLabel):
    """单行标签：宽度不够时显示省略号，完整文本放进 tooltip。

    在线列表的状态行会承载较长的错误信息（例如筛选数据加载失败的网络错误），
    单行 + 省略号可以避免它把结果列表往下顶，也不会撑宽挤走同一行的控件。
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setWordWrap(False)
        self.setText(text)

    def setText(self, text: str) -> None:  # type: ignore[override]
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self._update_elided()

    def fullText(self) -> str:
        return self._full_text

    def _update_elided(self) -> None:
        width = self.contentsRect().width()
        elided = self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, max(0, width)
        )
        super().setText(elided)

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._update_elided()

    def changeEvent(self, event: QEvent) -> None:  # type: ignore[override]
        super().changeEvent(event)
        # 换主题会改字号，省略位置要按新字体重算。
        if event.type() == QEvent.Type.FontChange:
            self._update_elided()


def _q_difficulty_title(count: int, total: int) -> str:
    """Q 难度下拉框的按钮文案：全选 / 部分 / 一个都不选。"""
    if count <= 0:
        return "Q 难度 · 不含"
    if count >= total:
        return "Q 难度"
    return f"Q 难度 · {count}/{total}"


_STATUS_LABELS = {
    "draft": "草稿",
    "bili_only": "仅 B 站",
    "bili_uploaded": "已上传 B 站",
    "submitted": "已投稿",
    "failed": "失败",
}


def _fmt_int(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "-"


def _fmt_duration(level: LevelInfo) -> str:
    return level.duration_text if level.duration_ms else "-"


def _format_bytes(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _tag_sort_key(tag: dict[str, Any]) -> tuple[int, float]:
    score = tag.get("score")
    try:
        numeric = float(score) if score is not None else 0.0
    except (TypeError, ValueError):
        numeric = 0.0
    return (0 if tag.get("pinned") else 1, -numeric)


def _color_icon(color: str) -> QIcon:
    pixmap = QPixmap(12, 12)
    pixmap.fill(QColor(color or active_palette()["accent"]))
    return QIcon(pixmap)


def _qss_color(color: str, alpha: int = 255, fallback: str = "#7DD3FC") -> str:
    value = QColor(color or fallback)
    if not value.isValid():
        value = QColor(fallback)
    value.setAlpha(max(0, min(255, int(alpha))))
    return value.name(QColor.NameFormat.HexArgb)


def _middle_elide(text: str, limit: int = 44) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    head = max(8, (limit - 1) // 2)
    tail = max(8, limit - head - 1)
    return value[:head] + "…" + value[-tail:]


_BAND_RE = re.compile(r"^([PGU])(\d{1,2})$", re.IGNORECASE)
_SERIES_OFFSET = {"P": 0, "G": 20, "U": 40}


def difficulty_value(entry: dict[str, Any]) -> int | None:
    """把难度映射到 1-60（P1-P20 / G1-G20 / U1-U20），无级别返回 None。"""
    match = _BAND_RE.match(entry_band(entry) or "")
    if not match:
        return None
    number = int(match.group(2))
    if not 1 <= number <= 20:
        return None
    return _SERIES_OFFSET[match.group(1).upper()] + number


@dataclass
class ChartItem:
    level: LevelInfo
    source: str = "tuf"
    entry: dict[str, Any] | None = None
    downloaded: bool = False
    submitted: int = 0
    matched: bool = True

    @property
    def tuf_id(self) -> int | None:
        return self.level.tuf_id

    @property
    def local_path(self) -> str:
        if self.entry:
            return str(self.entry.get("path") or "")
        return ""

    @property
    def parse_error(self) -> str:
        """本地谱面的解析失败原因；解析正常或 TUF 条目返回空字符串。"""
        if self.source == "tuf" or not self.entry:
            return ""
        return str(self.entry.get("parse_error") or "").strip()


class ChartListModel(QAbstractListModel):
    ItemRole = int(Qt.ItemDataRole.UserRole) + 1

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[ChartItem] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        if parent.isValid():
            return 0
        return len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None
        item = self._items[index.row()]
        if role == self.ItemRole:
            return item
        if role == Qt.ItemDataRole.DisplayRole:
            return item.level.song
        return None

    def items(self) -> list[ChartItem]:
        return list(self._items)

    def set_items(self, items: Iterable[ChartItem]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self.endResetModel()

    def append_items(self, items: Iterable[ChartItem]) -> None:
        rows = list(items)
        if not rows:
            return
        start = len(self._items)
        self.beginInsertRows(QModelIndex(), start, start + len(rows) - 1)
        self._items.extend(rows)
        self.endInsertRows()

    def clear(self) -> None:
        self.set_items([])

    def item_at(self, row: int) -> ChartItem | None:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    def mark_downloaded(self, tuf_id: int | None, path: str = "") -> None:
        if not tuf_id:
            return
        for row, item in enumerate(self._items):
            if item.level.tuf_id == tuf_id:
                item.downloaded = True
                if path and item.entry is not None:
                    item.entry["path"] = path
                index = self.index(row, 0)
                self.dataChanged.emit(index, index, [])


class ChartCardDelegate(QStyledItemDelegate):
    """谱面卡片绘制。

    在线卡片的曲绘缩略图在 0.17.15 撤下，恢复成难度图标；加载器、绘制方法与
    ``thumbnailReady`` 接线都保留，后续美化时把 ``show_thumbnails`` 打开即可，
    默认不请求也不绘制。
    """

    downloadClicked = Signal(object)
    detailClicked = Signal(object)

    show_thumbnails = False

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = active_theme()
        self._compact = False
        self.actions_visible = True
        self.downloading_ids: set[int] = set()
        self.thumbnail_loader: ThumbnailLoader | None = None
        self._scaled_thumbnails: dict[tuple[int, int], QPixmap] = {}

    def set_theme(self, theme: str) -> None:
        self._theme = theme

    def refresh_theme(self) -> None:
        self._theme = active_theme()

    def set_compact(self, compact: bool) -> None:
        self._compact = compact

    def set_actions_visible(self, visible: bool) -> None:
        """选谱等只读场景可隐藏「下载 / 详情」按钮，让文字占满整行。"""
        self.actions_visible = bool(visible)

    def set_thumbnail_loader(self, loader: ThumbnailLoader | None) -> None:
        """注入曲绘加载器；加载完成后的行重绘由页面 / 弹窗接线。"""
        self.thumbnail_loader = loader
        self._scaled_thumbnails.clear()

    def set_thumbnails_enabled(self, enabled: bool) -> None:
        """曲绘接口保留给后续美化用；默认关闭，卡片只画难度图标。"""
        self.show_thumbnails = bool(enabled)
        self._scaled_thumbnails.clear()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # type: ignore[override]
        return QSize(120, 92 if self._compact else 116)

    def _card_rect(self, rect: QRect) -> QRect:
        return rect.adjusted(6, 4, -6, -4)

    def _action_rects(self, rect: QRect, item: ChartItem) -> tuple[QRect, QRect]:
        if not self.actions_visible:
            return QRect(), QRect()
        card = self._card_rect(rect)
        btn_h = 26
        download = QRect(card.right() - 86, card.bottom() - btn_h - 9, 76, btn_h)
        detail = QRect(download.left() - 62, download.top(), 54, btn_h)
        return download, detail

    @staticmethod
    def badge_text(item: ChartItem) -> str:
        entry = item.entry or {}
        text = item.level.difficulty or entry_band(entry)
        if not text:
            if item.source != "local":
                text = "?"
            elif is_tuf_bound(entry):
                # 已绑定但 TUF 没给难度：不要误显示「未匹配」。
                text = "已匹配"
            else:
                text = "未匹配"
        return {
            "Unranked": "UR",
            "Impossible": "IM",
            "Censored": "CN",
        }.get(text, text)

    @staticmethod
    def meta_text(level: LevelInfo, local: bool = False) -> str:
        meta_parts = [
            level.duration_text if level.duration_ms else "",
            f"{_fmt_int(level.tilecount)} 物量" if level.tilecount else "",
            f"{_fmt_int(level.auto_tile_count)} 自动砖" if level.auto_tile_count else "",
            (
                f"可击 {_fmt_int(level.expected_hits)}"
                if local
                and level.expected_hits is not None
                and level.expected_hits != level.tilecount
                else ""
            ),
            f"{int(level.bpm)} BPM" if level.bpm else "",
            f"{level.midspin_count} midspin" if level.midspin_count else "",
        ]
        return " · ".join(part for part in meta_parts if part)

    @staticmethod
    def stats_text(item: ChartItem) -> str:
        level = item.level
        if item.source != "tuf":
            entry = item.entry or {}
            label = entry_band(entry) or str(entry.get("collection") or "")
            if not label:
                label = CATEGORY_LABELS.get(entry_category(entry), "本地")
            return f"{label} · 已投稿" if item.submitted else label
        raw = level.raw or {}
        if not (level.unique_clears or level.clears or level.likes):
            if not any(key in raw for key in ("uniqueClears", "clears", "likes")):
                return ""
        return f"{_fmt_int(level.unique_clears or level.clears)} 通关 · {_fmt_int(level.likes)} 赞"

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:  # type: ignore[override]
        item = index.data(ChartListModel.ItemRole)
        if not isinstance(item, ChartItem):
            return
        level = item.level
        c = palette(self._theme)
        card = self._card_rect(option.rect)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        bg = QColor(c["card_hover"] if selected or hover else c["card"])
        border = QColor(c["accent"] if selected else c["border"])
        painter.setBrush(bg)
        painter.setPen(QPen(border, 1))
        painter.drawRoundedRect(card, 10, 10)

        diff_color = QColor(level.difficulty_color or "")
        if not diff_color.isValid():
            fallback = category_color(item.entry or {}) if item.source == "local" else ""
            diff_color = QColor(fallback or c["accent"])

        bar = QRect(card.left() + 4, card.top() + 10, 3, card.height() - 20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(diff_color)
        painter.drawRoundedRect(bar, 1, 1)

        # 默认只画难度图标，在线卡片和本地卡片同一个尺寸（与加曲绘之前一致）。
        # 曲绘开关打开后，带 TUF 编号的卡片才改画缩略图。
        lead_size = 40 if self._compact else 46
        lead = QRect(
            card.left() + 14,
            card.top() + (card.height() - lead_size) // 2,
            lead_size,
            lead_size,
        )
        is_tuf_card = item.source == "tuf" and bool(level.tuf_id)
        thumbnail = self._thumbnail_for(item, lead_size) if is_tuf_card else None
        if thumbnail is not None:
            self._draw_thumbnail(painter, lead, thumbnail, c)
        else:
            self._draw_difficulty_badge(painter, lead, item, diff_color)

        download_rect, detail_rect = self._action_rects(option.rect, item)
        text_left = lead.right() + 12
        text_right = detail_rect.left() - 12 if self.actions_visible else card.right() - 14
        text_width = max(40, text_right - text_left)

        small_font = QFont("Microsoft YaHei UI", 8)
        small_font.setPixelSize(11)
        painter.setFont(small_font)
        painter.setPen(QColor(c["faint"]))
        if level.tuf_id:
            header_parts = [f"#{level.tuf_id}"]
            if level.artist:
                header_parts.append(level.artist)
            if level.base_score:
                header_parts.append(f"{level.base_score:g} PP")
            header = " · ".join(header_parts)
        else:
            header = "本地谱面" if item.source == "local" else (level.artist or "")
        stats = self.stats_text(item)
        stats_rect = QRect(card.right() - 190, card.top() + 8, 178, 16)
        if stats:
            painter.drawText(
                stats_rect,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                stats,
            )
        if header:
            painter.drawText(
                QRect(text_left, card.top() + 8, max(40, stats_rect.left() - text_left - 8), 16),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                header,
            )

        title_font = QFont("Microsoft YaHei UI", 12 if self._compact else 13)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(c["text"]))
        title = QFontMetrics(title_font).elidedText(
            level.song or "(未命名谱面)", Qt.TextElideMode.ElideRight, text_width
        )
        title_y = card.top() + (26 if self._compact else 28)
        painter.drawText(
            QRect(text_left, title_y, text_width, 20),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            title,
        )

        tag_y = card.top() + (46 if self._compact else 52)
        x = text_left
        tag_font = QFont("Microsoft YaHei UI", 8)
        tag_font.setPixelSize(10)
        pills = 0
        max_pills = 6 if self._compact else 8
        parse_error = item.parse_error
        if parse_error:
            # 用文字标签而不是只换颜色，保证一眼能看出这张谱面没解析成功。
            x = self._draw_pill(
                painter, x, tag_y, "⚠ 解析失败", c["warning"], tag_font, text_right
            )
            pills += 1
        if item.source == "tuf" and item.downloaded:
            x = self._draw_pill(painter, x, tag_y, "已下载", c["success"], tag_font, text_right)
            pills += 1
        if item.source == "tuf" and item.submitted:
            x = self._draw_pill(
                painter, x, tag_y, f"已投稿 {item.submitted}", c["accent"], tag_font, text_right
            )
            pills += 1
        if item.source == "local":
            status = "已下载" if item.matched else "未匹配"
            color = c["success"] if item.matched else c["warning"]
            x = self._draw_pill(painter, x, tag_y, status, color, tag_font, text_right)
            pills += 1
            collection = str((item.entry or {}).get("collection") or "")
            if collection:
                x = self._draw_pill(
                    painter,
                    x,
                    tag_y,
                    f"合集 · {collection}",
                    c["accent_strong"],
                    tag_font,
                    text_right,
                )
                pills += 1
            if item.submitted:
                x = self._draw_pill(
                    painter, x, tag_y, f"已投稿 {item.submitted}", c["accent"], tag_font, text_right
                )
                pills += 1
        for tag in sorted(level.tags, key=_tag_sort_key):
            if pills >= max_pills:
                break
            name = str(tag.get("name") or "")
            if not name:
                continue
            color = str(tag.get("color") or c["muted"])
            x = self._draw_pill(painter, x, tag_y, name, color, tag_font, text_right)
            pills += 1

        painter.setFont(small_font)
        meta = self.meta_text(level, local=item.source != "tuf")
        if parse_error:
            painter.setPen(QColor(c["warning"]))
            meta = QFontMetrics(small_font).elidedText(
                "解析失败：" + parse_error,
                Qt.TextElideMode.ElideRight,
                text_width,
            )
        else:
            painter.setPen(QColor(c["muted"]))
        meta_y = card.top() + (68 if self._compact else 76)
        painter.drawText(
            QRect(text_left, meta_y, text_width, 16),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            meta,
        )
        if not self._compact:
            creator_parts = [level.charter] if level.charter else []
            if level.vfxers:
                creator_parts.append("VFX: " + ", ".join(level.vfxers))
            if creator_parts:
                painter.setPen(QColor(c["faint"]))
                painter.drawText(
                    QRect(text_left, card.top() + 94, text_width, 14),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    QFontMetrics(small_font).elidedText(
                        "谱师：" + "  ·  ".join(creator_parts),
                        Qt.TextElideMode.ElideRight,
                        text_width,
                    ),
                )

        if self.actions_visible:
            can_download = bool(level.tuf_id)
            download_text = "下载中…" if level.tuf_id in self.downloading_ids else (
                "已下载" if item.downloaded else ("重新下载" if item.source == "local" else "下载")
            )
            if not can_download:
                download_text = "无编号"
            self._draw_button(
                painter,
                download_rect,
                download_text,
                c,
                primary=can_download and not item.downloaded,
                enabled=can_download,
            )
            self._draw_button(painter, detail_rect, "详情", c, primary=False, enabled=True)
        painter.restore()

    def _thumbnail_for(self, item: ChartItem, size: int) -> QPixmap | None:
        """只在该行真正绘制时请求曲绘；返回缩放后的缓存图，未就绪返回 None。"""
        if not self.show_thumbnails:
            return None
        loader = self.thumbnail_loader
        tuf_id = item.level.tuf_id
        if loader is None or not tuf_id:
            return None
        key = int(tuf_id)
        loader.request(key)
        pixmap = loader.pixmap(key)
        if pixmap is None or pixmap.isNull():
            return None
        cache_key = (key, size)
        cached = self._scaled_thumbnails.get(cache_key)
        if cached is not None:
            return cached
        scaled = pixmap.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        if len(self._scaled_thumbnails) > 256:
            self._scaled_thumbnails.clear()
        self._scaled_thumbnails[cache_key] = scaled
        return scaled

    def _draw_thumbnail(
        self,
        painter: QPainter,
        rect: QRect,
        pixmap: QPixmap,
        c: dict[str, str],
    ) -> None:
        path = QPainterPath()
        path.addRoundedRect(rect, 9, 9)
        painter.save()
        painter.setClipPath(path)
        source = QRect(
            max(0, (pixmap.width() - rect.width()) // 2),
            max(0, (pixmap.height() - rect.height()) // 2),
            rect.width(),
            rect.height(),
        )
        painter.drawPixmap(rect, pixmap, source)
        painter.restore()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(c["border"]), 1))
        painter.drawRoundedRect(rect, 9, 9)

    def _draw_difficulty_badge(
        self,
        painter: QPainter,
        badge: QRect,
        item: ChartItem,
        diff_color: QColor,
    ) -> None:
        """无曲绘 / 加载中时的回退：优先难度图标，其次文字徽章。"""
        icon = difficulty_pixmap(item.level.difficulty, 36 if self._compact else 40)
        if icon is not None:
            target = QRect(0, 0, icon.width(), icon.height())
            target.moveCenter(badge.center())
            painter.drawPixmap(target, icon)
            return
        fill = QColor(diff_color)
        fill.setAlpha(42)
        painter.setBrush(fill)
        painter.setPen(QPen(diff_color, 1))
        painter.drawRoundedRect(badge, 9, 9)
        badge_font = QFont("Microsoft YaHei UI", 11 if self._compact else 12)
        badge_font.setBold(True)
        painter.setFont(badge_font)
        painter.setPen(diff_color)
        badge_text = self.badge_text(item)
        metrics = QFontMetrics(badge_font)
        if metrics.horizontalAdvance(badge_text) > badge.width() - 6:
            badge_text = metrics.elidedText(
                badge_text, Qt.TextElideMode.ElideRight, badge.width() - 6
            )
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, badge_text)

    def _draw_pill(
        self,
        painter: QPainter,
        x: int,
        y: int,
        text: str,
        color: str,
        font: QFont,
        limit: int,
    ) -> int:
        fm = QFontMetrics(font)
        width = min(max(34, fm.horizontalAdvance(text) + 12), 96)
        if x + width > limit:
            return x
        rect = QRect(x, y, width, 18)
        base = QColor(color)
        if not base.isValid():
            base = QColor(palette(self._theme)["muted"])
        fill = QColor(base)
        fill.setAlpha(38)
        painter.setBrush(fill)
        border = QColor(base)
        border.setAlpha(150)
        painter.setPen(QPen(border, 1))
        painter.drawRoundedRect(rect, 9, 9)
        painter.setFont(font)
        painter.setPen(base)
        painter.drawText(
            rect.adjusted(6, 0, -6, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            fm.elidedText(text, Qt.TextElideMode.ElideRight, width - 12),
        )
        return x + width + 5

    def _draw_button(
        self,
        painter: QPainter,
        rect: QRect,
        text: str,
        c: dict[str, str],
        primary: bool,
        enabled: bool,
    ) -> None:
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        if primary and enabled:
            painter.setBrush(QColor(c["accent"]))
            painter.setPen(QPen(QColor(c["accent"]), 1))
            text_color = QColor(c["selection_text"])
        else:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(c["border"] if enabled else c["border_soft"]), 1))
            text_color = QColor(c["muted"] if enabled else c["faint"])
        painter.drawRoundedRect(rect, 7, 7)
        painter.setPen(text_color)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def editorEvent(  # type: ignore[override]
        self,
        event: QEvent,
        model: QAbstractListModel,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> bool:
        if event.type() != QEvent.Type.MouseButtonRelease or not self.actions_visible:
            return super().editorEvent(event, model, option, index)
        item = index.data(ChartListModel.ItemRole)
        if not isinstance(item, ChartItem):
            return False
        point = event.position().toPoint()  # type: ignore[attr-defined]
        download_rect, detail_rect = self._action_rects(option.rect, item)
        if download_rect.contains(point) and item.level.tuf_id:
            self.downloadClicked.emit(item)
            return True
        # 单击卡片主体交还给视图处理选中，Ctrl / Shift 多选才不会被吞掉；
        # 详情只走「详情」按钮和双击两个入口。
        if detail_rect.contains(point):
            self.detailClicked.emit(item)
            return True
        return super().editorEvent(event, model, option, index)

    def helpEvent(  # type: ignore[override]
        self,
        event: Any,
        view: Any,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> bool:
        item = index.data(ChartListModel.ItemRole)
        if isinstance(item, ChartItem) and item.parse_error:
            QToolTip.showText(event.globalPos(), "解析失败：" + item.parse_error, view)
            return True
        return super().helpEvent(event, view, option, index)


class DifficultyRangeSlider(QWidget):
    """双把手难度区间滑块。

    轨道上方是把手当前值，下方是整段区间两端的提示。两个把手重合时先不决定
    拖哪一个，等第一次移动的方向再定，避免区间被压成一个点后再也拉不开。
    """

    rangeChanged = Signal(int, int)

    HANDLE_RADIUS = 7
    HANDLE_RADIUS_HOVER = 9
    HIT_RADIUS = 12

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        labels: tuple[str, ...] | None = None,
        maximum: int = 60,
        low_hint: str = "P1",
        high_hint: str = "U20",
        high_hint_always: bool = False,
        height: int = 52,
    ) -> None:
        super().__init__(parent)
        self.labels = tuple(labels) if labels else None
        self.maximum = len(self.labels) if self.labels else max(1, int(maximum))
        self._low_hint = low_hint
        self._high_hint = high_hint
        self._high_hint_always = high_hint_always
        self.low = 1
        self.high = self.maximum
        self._drag = 0
        self._pending: int | None = None
        self._hover = 0
        self._theme = active_theme()
        self.setFixedHeight(max(36, int(height)))
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_theme(self, theme: str) -> None:
        self._theme = theme
        self.update()

    def refresh_theme(self) -> None:
        self.set_theme(active_theme())

    def _label(self, value: int) -> str:
        if self.labels:
            return self.labels[max(1, min(self.maximum, value)) - 1]
        return pgu_name(value)

    def set_range(self, low: int, high: int) -> None:
        low = max(1, min(self.maximum, int(low)))
        high = max(1, min(self.maximum, int(high)))
        if low > high:
            low, high = high, low
        if (low, high) != (self.low, self.high):
            self.low, self.high = low, high
            self.update()
            self.rangeChanged.emit(self.low, self.high)

    def _track(self) -> QRect:
        # 44 或 52 高都保持「上方值标签、下方整段提示」的排布。
        return QRect(16, self.height() // 2 - 2, max(20, self.width() - 32), 8)

    def _x_for(self, value: int) -> int:
        track = self._track()
        span = max(1, self.maximum - 1)
        return track.left() + round((value - 1) / span * track.width())

    def _value_for(self, x: int) -> int:
        track = self._track()
        ratio = (x - track.left()) / max(1, track.width())
        return max(1, min(self.maximum, round(ratio * (self.maximum - 1)) + 1))

    def paintEvent(self, _event: Any) -> None:
        c = palette(self._theme)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if not self.isEnabled():
            # 自定义绘制不会自动变灰，这里整体压暗表示「已关闭」。
            painter.setOpacity(0.38)
        track = self._track()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(c["border_soft"]))
        painter.drawRoundedRect(track, 4, 4)

        low_x = self._x_for(self.low)
        high_x = self._x_for(self.high)
        gradient = QLinearGradient(track.left(), 0, track.right(), 0)
        gradient.setColorAt(0.0, QColor(c["accent_strong"]))
        gradient.setColorAt(0.3, QColor(c["success"]))
        gradient.setColorAt(0.55, QColor(c["warning"]))
        gradient.setColorAt(0.78, QColor(c["danger"]))
        gradient.setColorAt(1.0, QColor(c["accent"]))
        painter.save()
        painter.setClipRect(QRect(low_x, track.top() - 2, max(1, high_x - low_x), track.height() + 4))
        painter.setBrush(gradient)
        painter.drawRoundedRect(track, 4, 4)
        painter.restore()

        active = self._drag or self._hover
        for handle, x in ((1, low_x), (2, high_x)):
            highlighted = self.isEnabled() and active == handle
            radius = self.HANDLE_RADIUS_HOVER if highlighted else self.HANDLE_RADIUS
            if highlighted:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(c["accent_soft"]))
                painter.drawEllipse(
                    x - radius - 3,
                    track.center().y() - radius - 3,
                    (radius + 3) * 2,
                    (radius + 3) * 2,
                )
            painter.setBrush(QColor(c["text"]))
            painter.setPen(QPen(QColor(c["surface"]), 3))
            painter.drawEllipse(
                x - radius, track.center().y() - radius, radius * 2, radius * 2
            )

        label_font = QFont("Microsoft YaHei UI", 8)
        label_font.setBold(True)
        painter.setFont(label_font)
        label_metrics = QFontMetrics(label_font)
        painter.setPen(QColor(c["text"]))
        # 两个把手重合时只画一次，否则粗体字会被叠加两次。
        labels = [(low_x, self.low)]
        if self.high != self.low:
            labels.append((high_x, self.high))
        for x, value in labels:
            text = self._label(value)
            width = label_metrics.horizontalAdvance(text) + 10
            left = max(0, min(self.width() - width, x - width // 2))
            painter.drawText(
                QRect(left, 2, width, 14),
                Qt.AlignmentFlag.AlignCenter,
                text,
            )

        small = QFont("Microsoft YaHei UI", 8)
        small.setPixelSize(10)
        painter.setFont(small)
        painter.setPen(QColor(c["faint"]))
        hint_y = track.bottom() + 4
        if self._low_hint and self.low > 1:
            painter.drawText(
                QRect(8, hint_y, 80, 14), Qt.AlignmentFlag.AlignLeft, self._low_hint
            )
        if self._high_hint and (
            self._high_hint_always or self.high < self.maximum
        ):
            painter.drawText(
                QRect(self.width() - 88, hint_y, 80, 14),
                Qt.AlignmentFlag.AlignRight,
                self._high_hint,
            )

    def _handle_near(self, x: int) -> int:
        """光标离哪个把手够近（1=低端，2=高端，0=都不近）。"""
        if self.low == self.high:
            return 2 if abs(x - self._x_for(self.low)) <= self.HIT_RADIUS else 0
        d_low = abs(x - self._x_for(self.low))
        d_high = abs(x - self._x_for(self.high))
        if min(d_low, d_high) > self.HIT_RADIUS:
            return 0
        return 1 if d_low <= d_high else 2

    def mousePressEvent(self, event: Any) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self.isEnabled():
            return
        x = event.position().toPoint().x()
        low_x = self._x_for(self.low)
        high_x = self._x_for(self.high)
        if self.low == self.high:
            self._drag = 0
            if abs(x - low_x) <= self.HIT_RADIUS:
                # 压成一个点时先不选把手，等第一次移动的方向决定拉开哪一边。
                self._pending = x
            else:
                self._pending = None
                self._drag = 2 if x > low_x else 1
                self._update_from_x(x)
        else:
            self._pending = None
            self._drag = 1 if abs(x - low_x) <= abs(x - high_x) else 2
            self._update_from_x(x)
        self._hover = self._drag
        self.update()
        event.accept()

    def mouseMoveEvent(self, event: Any) -> None:
        x = event.position().toPoint().x()
        if self._drag:
            self._update_from_x(x)
        elif self._pending is not None:
            delta = x - self._pending
            if abs(delta) >= 2:
                self._drag = 2 if delta > 0 else 1
                self._pending = None
                self._update_from_x(x)
                self.update()
        else:
            hover = self._handle_near(x)
            if hover != self._hover:
                self._hover = hover
                self.setCursor(
                    Qt.CursorShape.SizeHorCursor
                    if hover
                    else Qt.CursorShape.PointingHandCursor
                )
                self.update()

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._drag = 0
        self._pending = None
        self._hover = self._handle_near(event.position().toPoint().x())
        self.update()
        event.accept()

    def leaveEvent(self, event: Any) -> None:
        if self._hover:
            self._hover = 0
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.update()
        super().leaveEvent(event)

    def changeEvent(self, event: Any) -> None:
        if event.type() == QEvent.Type.EnabledChange:
            self._drag = 0
            self._pending = None
            self._hover = 0
            self.setCursor(
                Qt.CursorShape.PointingHandCursor
                if self.isEnabled()
                else Qt.CursorShape.ArrowCursor
            )
            self.update()
        super().changeEvent(event)

    def _update_from_x(self, x: int) -> None:
        value = self._value_for(x)
        if self._drag == 1:
            self.set_range(min(value, self.high), self.high)
        elif self._drag == 2:
            self.set_range(self.low, max(value, self.low))


class PersistentMenu(QMenu):
    """多选菜单：点勾选项时只切换勾选，菜单保持打开。

    点菜单外面、按 Esc、或键盘回车确认时仍按默认行为关闭。用
    ``setProperty("keepOpenAction", True)`` 也能让「全选 / 全不选」这类
    非勾选动作保持打开，方便一次看完勾选状态再关。
    """

    def mouseReleaseEvent(self, event: Any) -> None:
        action = self.actionAt(event.position().toPoint())
        if action is not None and not action.isSeparator() and action.isEnabled():
            if action.isCheckable() or action.property("keepOpenAction"):
                action.trigger()
                self.setActiveAction(None)
                self.update()
                event.accept()
                return
        super().mouseReleaseEvent(event)


class FilterMenuButton(QPushButton):
    """筛选下拉按钮：宽度不够时绘制省略号，完整文案放进 tooltip。

    1080×720 下筛选行只有约 750px，勾选「特殊难度 / Q 难度」后按钮文案会
    变长（例如「Q 难度 · 3/11」），QPushButton 默认把放不下的文字直接切掉。
    这里只在绘制时按当前宽度省略：``text()`` 与 sizeHint 仍按完整文案计算，
    不会被省略后的短文字反过来挤小，窗口变宽后自动恢复全文显示。
    """

    changed = Signal()

    def __init__(
        self,
        title: str,
        parent: QWidget | None = None,
        *,
        title_formatter: Callable[[int, int], str] | None = None,
    ) -> None:
        super().__init__(title, parent)
        self.setObjectName("GhostButton")
        self.setMinimumHeight(34)
        self._title = title
        self._title_formatter = title_formatter
        self._base_tooltip = ""
        self._menu = PersistentMenu(self)
        self.setMenu(self._menu)
        self._actions: list[QAction] = []
        self._group_menus: dict[str, QMenu] = {}
        self._updating = False

    def _label_width(self) -> int:
        """当前宽度下文字真正可用的宽度（扣掉内边距、边框与菜单箭头）。"""
        fm = self.fontMetrics()
        chrome = self.sizeHint().width() - fm.horizontalAdvance(self.text())
        return max(0, self.width() - chrome)

    def _elided_text(self) -> str:
        # 布局按 sizeHint 分配宽度；只有真的被挤窄时才省略。刚好拿到自然宽度
        # 时不能省：真实字体的 advance 是小数，horizontalAdvance 取整后按
        # 整数宽度去 elidedText 会误判「差一点」，把全文变成「Curati…」。
        if self.width() >= self.sizeHint().width():
            return self.text()
        return self.fontMetrics().elidedText(
            self.text(), Qt.TextElideMode.ElideRight, self._label_width()
        )

    def paintEvent(self, event: QPaintEvent) -> None:  # type: ignore[override]
        elided = self._elided_text()
        if elided == self.text():
            super().paintEvent(event)
            return
        # 与 QPushButton::paintEvent 相同，只是把交给样式绘制的文字换成省略版。
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = elided
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)
        painter.end()

    def setToolTip(self, text: str) -> None:  # type: ignore[override]
        self._base_tooltip = str(text)
        self._sync_tooltip()

    def _sync_tooltip(self) -> None:
        if self._elided_text() != self.text():
            tip = (
                self.text()
                if not self._base_tooltip
                else f"{self._base_tooltip}\n\n{self.text()}"
            )
        else:
            tip = self._base_tooltip
        if tip != QPushButton.toolTip(self):
            QPushButton.setToolTip(self, tip)

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._sync_tooltip()

    def changeEvent(self, event: QEvent) -> None:  # type: ignore[override]
        super().changeEvent(event)
        # 换主题会改字号，省略位置要按新字体重算。
        if event.type() == QEvent.Type.FontChange:
            self._sync_tooltip()

    def clear_menu(self) -> None:
        self._menu.clear()
        self._actions = []
        self._group_menus = {}
        self.setText(self._title)
        self._sync_tooltip()

    def add_item(
        self,
        name: str,
        data: Any,
        color: str = "",
        group: str | None = None,
    ) -> None:
        target = self._menu
        if group:
            target = self._group_menus.get(group)
            if target is None:
                # 分组子菜单同样保持打开，标签 / Curation 一次也能多选。
                submenu = PersistentMenu(group, self._menu)
                self._menu.addMenu(submenu)
                target = submenu
                self._group_menus[group] = target
        action = target.addAction(name)
        action.setCheckable(True)
        action.setData(data)
        if color:
            action.setIcon(_color_icon(color))
        action.toggled.connect(self._on_toggled)
        self._actions.append(action)

    def selected(self) -> list[Any]:
        return [action.data() for action in self._actions if action.isChecked()]

    def reset_selection(self) -> None:
        self.set_all(False)

    def set_all(self, checked: bool, *, notify: bool = True) -> None:
        """一键全选 / 全不选；逐个勾选期间不触发 changed，最后只发一次。"""
        self._updating = True
        for action in self._actions:
            action.setChecked(bool(checked))
        self._updating = False
        self._refresh_title()
        if notify:
            self.changed.emit()

    def _on_toggled(self, _checked: bool) -> None:
        if self._updating:
            return
        self._refresh_title()
        self.changed.emit()

    def _refresh_title(self) -> None:
        count = len(self.selected())
        if self._title_formatter is not None:
            self.setText(self._title_formatter(count, len(self._actions)))
        else:
            self.setText(f"{self._title} ({count})" if count else self._title)
        self._sync_tooltip()


DETAIL_COVER_SIZE = 112
DETAIL_COVER_RADIUS = 14

DETAIL_BADGE_SIZE = 66
# 徽章文字左右各留 5px，避免贴边；仍然超宽时走省略号兜底。
DETAIL_BADGE_TEXT_WIDTH = DETAIL_BADGE_SIZE - 10


def _short_difficulty_code(value: str) -> str:
    """难度徽章只放短码：取第一个空白或括号（半角/全角）之前的部分。

    TUF 的 difficulty 形如 ``GQ1 (G5~G8)``，整串塞进 66px 徽章会被裁切，
    这里只保留 ``GQ1``；完整原文由 tooltip 承载。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.match(r"[^\s(（]+", text)
    return match.group(0) if match else ""


def _badge_font(pixel_size: int) -> QFont:
    """与徽章 QSS 一致的度量字体（标题字族 + 700 字重），用于按宽度省略。"""
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(pixel_size)
    font.setBold(True)
    return font


class _CoverView(QWidget):
    """详情弹窗 header 左侧的 TUF 曲绘框。

    尺寸固定，图片按 cover 语义（等比铺满 + 居中裁剪）绘制，占位与贴图之间
    只换内容不换尺寸，不会把标题挤动。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._placeholder = "加载中…"
        self.setFixedSize(DETAIL_COVER_SIZE, DETAIL_COVER_SIZE)
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(12)
        self.setFont(font)
        self.setToolTip("TUF 曲绘加载中…")

    def pixmap(self) -> QPixmap | None:
        return self._pixmap

    def placeholder_text(self) -> str:
        return self._placeholder if self._pixmap is None else ""

    def set_placeholder(self, text: str) -> None:
        self._pixmap = None
        self._placeholder = text
        self.update()

    def set_cover(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        self.update()

    def _cover_source(self, pixmap: QPixmap) -> QRect:
        """取居中裁剪区域：宽图裁两边，高图裁上下。"""
        source_w, source_h = pixmap.width(), pixmap.height()
        target_w, target_h = self.width(), self.height()
        if source_w * target_h > source_h * target_w:
            crop_w = max(1, round(source_h * target_w / target_h))
            return QRect((source_w - crop_w) // 2, 0, crop_w, source_h)
        crop_h = max(1, round(source_w * target_h / target_w))
        return QRect(0, (source_h - crop_h) // 2, source_w, crop_h)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt 事件
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        palette_colors = active_palette()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, DETAIL_COVER_RADIUS, DETAIL_COVER_RADIUS)
        if self._pixmap is None or self._pixmap.isNull():
            painter.fillPath(path, QColor(palette_colors["surface"]))
            painter.setPen(QPen(QColor(palette_colors["border"]), 1))
            painter.drawPath(path)
            painter.setPen(QColor(palette_colors["muted"]))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, self._placeholder
            )
            return
        painter.setClipPath(path)
        painter.drawPixmap(self.rect(), self._pixmap, self._cover_source(self._pixmap))
        painter.setClipping(False)
        painter.setPen(QPen(QColor(palette_colors["border"]), 1))
        painter.drawPath(path)


class ChartDetailDialog(QDialog):
    downloadRequested = Signal(object)
    publishRequested = Signal(object)
    profileRequested = Signal(str)
    removeRequested = Signal(str)
    gameOpenRequested = Signal(object)
    bindSearchRequested = Signal(str)
    bindRequested = Signal(object)
    unbindRequested = Signal(str)
    collectionAssignRequested = Signal(str, str)
    recordLoadRequested = Signal(object)
    variantRequested = Signal(str)
    tufFillRequested = Signal(str)
    # 用公开接口分析在线谱面（不需要下载整包）。
    analyzeOnlineRequested = Signal(int)

    def __init__(
        self,
        item: ChartItem,
        meta: dict[str, Any] | None = None,
        records: list[dict[str, Any]] | None = None,
        parent: QWidget | None = None,
        collections: list[str] | None = None,
        thumbnail_loader: ThumbnailLoader | None = None,
    ) -> None:
        super().__init__(parent)
        self.item = item
        self.level = item.level
        self._meta = meta or {}
        self._records = records or []
        self._collections = list(collections or [])
        self._bind_results: list[LevelInfo] = []
        # 曲绘与列表卡片共用进程内加载器；测试可注入假加载器。
        self.thumbnail_loader = (
            thumbnail_loader
            if thumbnail_loader is not None
            else shared_thumbnail_loader()
        )
        self.thumbnail_loader.thumbnailReady.connect(self._on_thumbnail_ready)
        self.setObjectName("ChartDetailDialog")
        self.setWindowTitle("谱面详情")
        self.resize(760, 660)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 14)
        outer.setSpacing(12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        scroll.setWidget(inner)
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(4, 4, 12, 8)
        layout.setSpacing(14)

        self._variants: list[Any] = []
        self._variant_buttons: dict[str, QPushButton] = {}
        self._variant_marks: dict[str, QLabel] = {}
        self._variant_status: QLabel | None = None
        self._tuf_hint: QLabel | None = None
        self._row_labels: dict[str, QLabel] = {}

        header = QHBoxLayout()
        self._cover_view = _CoverView()
        header.addWidget(self._cover_view)
        self._badge_label = QLabel()
        self._badge_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._badge_label.setFixedSize(DETAIL_BADGE_SIZE, DETAIL_BADGE_SIZE)
        header.addWidget(self._badge_label)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self._title_label = QLabel()
        self._title_label.setObjectName("PageTitle")
        self._subtitle_label = QLabel()
        self._subtitle_label.setObjectName("Muted")
        title_box.addWidget(self._title_label)
        title_box.addWidget(self._subtitle_label)
        header.addLayout(title_box, 1)
        layout.addLayout(header)
        self._apply_header()
        self._apply_cover()

        self._note_label = QLabel("")
        self._note_label.setObjectName("Faint")
        self._note_label.setWordWrap(True)
        self._note_label.hide()
        layout.addWidget(self._note_label)

        self._parse_banner = QLabel()
        self._parse_banner.setObjectName("ParseErrorBanner")
        self._parse_banner.setWordWrap(True)
        warn = active_palette()["warning"]
        self._parse_banner.setStyleSheet(
            f"background: {_qss_color(warn, 30)};"
            f"color: {_qss_color(warn)};"
            f"border: 1px solid {_qss_color(warn, 140)};"
            "border-radius: 10px; padding: 10px 12px;"
        )
        self._parse_banner.hide()
        layout.addWidget(self._parse_banner)
        self._apply_parse_banner()

        self._meta_hint = QLabel("")
        self._meta_hint.setObjectName("Faint")
        self._meta_hint.setWordWrap(True)
        self._meta_hint.hide()
        layout.addWidget(self._meta_hint)
        self._apply_meta_hint()

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)
        for index, (label_text, _value_text) in enumerate(self._row_values()):
            row, col = divmod(index, 2)
            label = QLabel(label_text)
            label.setObjectName("Faint")
            value = QLabel("-")
            value.setWordWrap(True)
            value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            if label_text == "合集":
                self.collection_value = value
            self._row_labels[label_text] = value
            grid.addWidget(label, row, col * 2)
            grid.addWidget(value, row, col * 2 + 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        layout.addLayout(grid)
        self._apply_row_values()

        self._build_variant_section(layout)

        self._build_bind_section(layout)
        self._build_collection_section(layout)

        tags = self.level.tags
        if tags:
            layout.addWidget(QLabel("标签"))
            tag_grid = QGridLayout()
            tag_grid.setHorizontalSpacing(6)
            tag_grid.setVerticalSpacing(6)
            for index, tag in enumerate(sorted(tags, key=_tag_sort_key)):
                name = str(tag.get("name") or "")
                if not name:
                    continue
                color = str(tag.get("color") or active_palette()["accent"])
                score = tag.get("score")
                text = name + (f"  {round(float(score) * 100)}%" if score is not None else "")
                chip = QLabel(text)
                chip.setFixedHeight(26)
                chip.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
                chip.setStyleSheet(
                    f"background: {_qss_color(color, 26)};"
                    f"border: 1px solid {_qss_color(color, 96)};"
                    "border-radius: 9px; padding: 4px 8px; font-size: 11px;"
                )
                tag_grid.addWidget(
                    chip, index // 4, index % 4, Qt.AlignmentFlag.AlignLeft
                )
            tag_grid.setColumnStretch(4, 1)
            layout.addLayout(tag_grid)

        links = QHBoxLayout()
        if self.level.tuf_id:
            self._link_button(links, "打开 TUF 页面", f"https://tuforums.com/levels/{self.level.tuf_id}")
            analyze_button = QPushButton("分析在线谱面")
            analyze_button.setObjectName("GhostButton")
            analyze_button.setToolTip(
                "直接读取 TUF 公开的谱面 JSON，分析 BPM、物量、中旋与变速（不下载整包）"
            )
            analyze_button.clicked.connect(
                lambda: self.analyzeOnlineRequested.emit(int(self.level.tuf_id or 0))
            )
            links.addWidget(analyze_button)
        if self.level.video_link:
            self._link_button(links, "打开视频", self.level.video_link)
        if self.level.workshop_link:
            self._link_button(links, "打开工坊", self.level.workshop_link)
        if item.local_path:
            self._link_button(links, "打开文件位置", str(Path(item.local_path).parent))
            profile_button = QPushButton("解析 ARD 详档")
            profile_button.setObjectName("GhostButton")
            profile_button.clicked.connect(lambda: self.profileRequested.emit(item.local_path))
            links.addWidget(profile_button)
        links.addStretch(1)
        layout.addLayout(links)

        history_header = QHBoxLayout()
        history_header.addWidget(QLabel("投稿历史"))
        history_header.addStretch(1)
        if self._records:
            self.history_load_button = QPushButton("载入最近投稿")
            self.history_load_button.setObjectName("GhostButton")
            self.history_load_button.setToolTip("把最近一次投稿的判定、文案和 BV 号载入工作台")
            self.history_load_button.clicked.connect(
                lambda: self.recordLoadRequested.emit(dict(self._records[0]))
            )
            history_header.addWidget(self.history_load_button)
        layout.addLayout(history_header)

        if self._records:
            table = QTableWidget(0, 6)
            table.setHorizontalHeaderLabels(
                ["时间", "标题", "BV 号", "准度", "状态", "审核"]
            )
            table.verticalHeader().setVisible(False)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            table.setToolTip("双击记录可打开对应的 B 站稿件")
            for record in self._records[:20]:
                row = table.rowCount()
                table.insertRow(row)
                bvid = str(record.get("bvid") or "")
                url = str(
                    record.get("bili_url")
                    or (f"https://www.bilibili.com/video/{bvid}" if bvid else "")
                    or record.get("tuf_url")
                    or ""
                )
                values = [
                    str(record.get("created_at") or "")[:16].replace("T", " "),
                    record.get("title") or record.get("song") or "-",
                    bvid or "-",
                    record.get("accuracy") or "-",
                    _STATUS_LABELS.get(
                        str(record.get("status") or ""), record.get("status") or "-"
                    ),
                    review_text(record.get("review_status")) or "-",
                ]
                for col, text in enumerate(values):
                    cell = QTableWidgetItem(str(text))
                    if col == 0 and url:
                        cell.setData(Qt.ItemDataRole.UserRole, url)
                    table.setItem(row, col, cell)
            table.cellDoubleClicked.connect(self._open_history_row)
            table.resizeColumnsToContents()
            table.setMinimumHeight(150)
            self.history_table = table
            layout.addWidget(table)
        else:
            empty = QLabel("还没有投稿记录。完成 B 站上传并提交 TUF 后，这里会出现记录。")
            empty.setObjectName("Faint")
            empty.setWordWrap(True)
            layout.addWidget(empty)

        actions = QHBoxLayout()
        if item.source == "local" and item.entry and item.entry.get("key"):
            remove = QPushButton("从谱面库移除")
            remove.setObjectName("DangerButton")
            remove.clicked.connect(lambda: self.removeRequested.emit(str(item.entry.get("key"))))
            actions.addWidget(remove)
        if item.source == "local" and item.entry and (
            item.entry.get("folder") or item.entry.get("path")
        ):
            self.game_open_button = QPushButton("在游戏中打开")
            self.game_open_button.setObjectName("PrimaryButton")
            self.game_open_button.setToolTip(
                "通过 ADOFAI GO Bridge 在 ADOFAI 编辑器中载入这张谱面"
            )
            self.game_open_button.clicked.connect(
                lambda: self.gameOpenRequested.emit(self.item)
            )
            actions.addWidget(self.game_open_button)
            game_hint = QLabel(
                "需要 ADOFAI GO Bridge；可在「05 模组」页一键安装，首次安装后重启一次游戏"
            )
            game_hint.setObjectName("Faint")
            actions.addWidget(game_hint)
        actions.addStretch(1)
        if self.level.song or self.level.tuf_id:
            self.publish_button = QPushButton("用这张谱面去投稿")
            self.publish_button.setObjectName("PrimaryButton")
            self.publish_button.clicked.connect(
                lambda: self.publishRequested.emit(self.level)
            )
            actions.addWidget(self.publish_button)
        if self.level.tuf_id:
            download = QPushButton("重新下载" if item.source == "local" else "下载到本地库")
            download.setObjectName("PrimaryButton")
            download.clicked.connect(lambda: self.downloadRequested.emit(self.level))
            actions.addWidget(download)
        close = QPushButton("关闭")
        close.setObjectName("GhostButton")
        close.clicked.connect(self.reject)
        actions.addWidget(close)
        outer.addWidget(scroll, 1)
        outer.addLayout(actions)

    # --- 条目信息展示（支持不关窗刷新） -------------------------------------

    def _apply_header(self) -> None:
        level = self.level
        entry = self.item.entry or {}
        color = level.difficulty_color or active_palette()["accent"]
        # 徽章只显示短码；完整难度原文放到 tooltip，避免 66px 内被裁切。
        full_badge = str(level.difficulty or entry_band(entry) or "").strip()
        if full_badge:
            badge = _short_difficulty_code(full_badge) or full_badge
        else:
            if self.item.source == "local":
                badge = "已匹配" if is_tuf_bound(entry) else "未匹配"
            else:
                badge = "?"
            full_badge = badge
        font_size = 17 if len(badge) <= 3 else 12
        text = badge
        if font_size != 17:
            text = QFontMetrics(_badge_font(font_size)).elidedText(
                badge, Qt.TextElideMode.ElideRight, DETAIL_BADGE_TEXT_WIDTH
            )
        self._badge_label.setText(text)
        self._badge_label.setToolTip(full_badge)
        self._badge_label.setStyleSheet(
            f"background: {_qss_color(color, 30)}; color: {_qss_color(color)};"
            f"border: 1px solid {_qss_color(color, 150)};"
            f"border-radius: 14px; font-size: {font_size}px; font-weight: 700;"
        )
        self._title_label.setText(level.song or "(未命名谱面)")
        if self.item.source == "tuf":
            subtitle = f"#{level.tuf_id}" if level.tuf_id else "TUF 谱面"
        elif level.tuf_id and is_tuf_bound(entry):
            subtitle = f"本地谱面 · 已绑定 TUF #{level.tuf_id}"
        elif level.tuf_id:
            subtitle = f"本地谱面 · 已识别 TUF #{level.tuf_id}（未绑定）"
        else:
            subtitle = "本地谱面 · 未匹配 TUF"
        self._subtitle_label.setText(subtitle)

    # --- TUF 曲绘 -----------------------------------------------------------

    def _cover_tuf_id(self) -> int | None:
        """当前条目可用于取曲绘的 TUF 编号；没有合法编号时返回 None。"""
        tuf_id = self.level.tuf_id
        if not tuf_id:
            return None
        try:
            key = int(tuf_id)
        except (TypeError, ValueError):
            return None
        return key if key > 0 else None

    def _apply_cover(self) -> None:
        """按当前条目请求并贴曲绘；无编号、加载中或无图都保持固定占位框。"""
        tuf_id = self._cover_tuf_id()
        if tuf_id is None:
            self._cover_view.set_placeholder("无曲绘")
            self._cover_view.setToolTip("本地谱面未绑定 TUF，暂无曲绘")
            return
        loader = self.thumbnail_loader
        # 已缓存或在途时加载器内部直接返回，这里只负责保证请求已经发出。
        loader.request(tuf_id)
        pixmap = loader.pixmap(tuf_id)
        if pixmap is not None and not pixmap.isNull():
            self._cover_view.set_cover(pixmap)
            self._cover_view.setToolTip(f"TUF #{tuf_id} 曲绘")
            return
        if loader.is_loaded(tuf_id):
            self._cover_view.set_placeholder("无曲绘")
            self._cover_view.setToolTip("TUF 没有为这张谱面提供曲绘")
            return
        self._cover_view.set_placeholder("加载中…")
        self._cover_view.setToolTip("TUF 曲绘加载中…")

    def _on_thumbnail_ready(self, tuf_id: int) -> None:
        """曲绘到货：只贴弹窗当前条目自己的图，避免切条目后贴错。"""
        current = self._cover_tuf_id()
        if current is None or int(tuf_id) != current:
            return
        self._apply_cover()

    def _apply_parse_banner(self) -> None:
        error = self.item.parse_error
        if not error:
            self._parse_banner.hide()
            return
        self._parse_banner.setText(
            f"⚠ 解析失败：{error}\n"
            "曲名等字段已回退为文件 / 文件夹名，物量、时长等信息可能缺失。"
            "请检查谱面文件，或在谱面库重新导入。"
        )
        self._parse_banner.show()

    def _apply_meta_hint(self) -> None:
        entry = self.item.entry or {}
        missing = [
            str(field)
            for field in (entry.get("meta_missing") or [])
            if str(field).strip()
        ]
        if not missing or self.item.parse_error:
            self._meta_hint.hide()
            return
        labels = {"song": "曲名", "artist": "曲师", "charter": "谱师"}
        values = {
            "song": self.level.song,
            "artist": self.level.artist,
            "charter": self.level.charter,
        }
        parts: list[str] = []
        for field in missing:
            label = labels.get(field, field)
            if str(values.get(field) or "").strip():
                parts.append(f"谱面里没写{label}，当前显示来自文件/文件夹名")
            else:
                parts.append(f"谱面里没写{label}")
        text = "提示：" + "；".join(parts) + "。不影响解析。"
        if self.level.tuf_id and not is_tuf_bound(entry):
            text += "可点下方「从 TUF 补齐信息」。"
        self._meta_hint.setText(text)
        self._meta_hint.show()

    def _row_values(self) -> list[tuple[str, str]]:
        level = self.level
        item = self.item
        curation_lookup = {
            int(entry["id"]): entry
            for entry in self._meta.get("curations") or []
            if isinstance(entry, dict) and str(entry.get("id", "")).isdigit()
        }
        curation_names = [
            str(curation_lookup[type_id].get("name") or type_id)
            for type_id in level.curation_type_ids
            if type_id in curation_lookup
        ]
        local_file = Path(item.local_path).name if item.local_path else "-"
        local_folder = _middle_elide(
            str((item.entry or {}).get("folder") or "-"),
            40,
        )
        return [
            ("曲师", level.artist or "-"),
            ("谱师", level.charter or "-"),
            ("VFX", ", ".join(level.vfxers) or "-"),
            ("难度", level.difficulty or "-"),
            ("Base Score", f"{level.base_score:g} PP" if level.base_score else "-"),
            ("Curation", ", ".join(curation_names) or "-"),
            ("BPM", str(int(level.bpm)) if level.bpm else "-"),
            ("时长", _fmt_duration(level)),
            ("物量", _fmt_int(level.tilecount) if level.tilecount else "-"),
            ("自动砖", _fmt_int(level.auto_tile_count)),
            ("可击物量", _fmt_int(level.expected_hits) if level.expected_hits else "-"),
            ("midspin", _fmt_int(level.midspin_count)),
            ("通关 / 点赞", f"{_fmt_int(level.unique_clears)} / {_fmt_int(level.likes)}"),
            ("下载次数", _fmt_int(level.download_count)),
            ("本地文件", local_file),
            ("分类", CATEGORY_LABELS.get(entry_category(item.entry or {}), "-")),
            ("合集", str((item.entry or {}).get("collection") or "-")),
            ("目录", local_folder),
            ("游戏难度", str((item.entry or {}).get("game_difficulty") or "-")),
        ]

    def _apply_row_values(self) -> None:
        for label_text, value_text in self._row_values():
            value = self._row_labels.get(label_text)
            if value is None:
                continue
            value.setText(value_text)
            if label_text == "本地文件" and self.item.local_path:
                value.setToolTip(self.item.local_path)
            elif label_text == "目录" and self.item.entry:
                value.setToolTip(str(self.item.entry.get("folder") or ""))

    def _show_note(self, message: str) -> None:
        if message:
            self._note_label.setText(message)
            self._note_label.show()
        else:
            self._note_label.hide()

    def refresh_entry(self, entry: dict[str, Any], note: str = "") -> None:
        """用谱面库里的最新条目刷新弹窗，不关闭窗口。"""
        self.item.entry = entry
        level = LevelInfo.from_dict(entry)
        if level is not None:
            self.item.level = level
            self.level = level
        self._apply_header()
        self._apply_cover()
        self._apply_parse_banner()
        self._apply_meta_hint()
        self._apply_row_values()
        self._apply_tuf_section()
        self._apply_variant_marks()
        if self._variant_status is not None:
            self._variant_status.setText("")
            self._variant_status.hide()
        self._show_note(note)

    def set_variant_error(self, message: str) -> None:
        if self._variant_status is not None:
            self._variant_status.setText(message)
            self._variant_status.show()
        self._apply_variant_marks()

    def set_tuf_fill_status(self, message: str, error: bool = False) -> None:
        if self._tuf_hint is None:
            return
        self._tuf_hint.setText(message)
        color = active_palette()["warning" if error else "muted"]
        self._tuf_hint.setStyleSheet(f"color: {_qss_color(color)};")
        self._tuf_hint.show()

    # --- 谱面变体 -----------------------------------------------------------

    def _build_variant_section(self, layout: QVBoxLayout) -> None:
        key = str((self.item.entry or {}).get("key") or "")
        if self.item.source != "local" or not key:
            return
        container = variant_container(self.item.entry)
        if container is None:
            return
        try:
            if not container.exists():
                return
            variants = list_chart_variants(container)
        except OSError:
            return
        current = entry_variant(self.item.entry)
        if container.is_dir():
            # 文件夹条目只列同一层的谱面；否则扫描根目录会变成「整个库的变体」。
            variants = [
                variant
                for variant in variants
                if "/" not in variant.member or variant.member == current
            ]
        if len(variants) <= 1:
            return
        self._variants = variants

        frame = QFrame()
        frame.setObjectName("FilterPanel")
        box = QVBoxLayout(frame)
        box.setContentsMargins(12, 10, 12, 10)
        box.setSpacing(6)
        title = QLabel("谱面变体")
        title.setStyleSheet("font-weight: 600;")
        box.addWidget(title)
        hint = QLabel(
            f"这个谱面包里有 {len(variants)} 份 .adofai，不同变体的物量可能不同。"
            "标「使用中」的是当前解析的玩法；切换只更新谱面库索引，不修改文件。"
        )
        hint.setObjectName("Faint")
        hint.setWordWrap(True)
        box.addWidget(hint)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        text_font = QFont("Microsoft YaHei UI", 8)
        text_font.setPixelSize(11)
        for row, variant in enumerate(variants):
            name = QLabel(variant.member)
            name.setWordWrap(True)
            name.setFont(text_font)
            name.setToolTip(str(container))
            grid.addWidget(name, row, 0)
            if variant.error:
                stats_text = "解析失败：" + variant.error
            else:
                stats_text = (
                    f"{variant.tile_count:,} 格 · 中旋 {variant.midspin_count} · "
                    f"自动 {variant.auto_tile_count} · 可击 {variant.playable_hits:,}"
                )
            stats = QLabel(stats_text)
            stats.setObjectName("Faint")
            stats.setFont(text_font)
            stats.setWordWrap(True)
            grid.addWidget(stats, row, 1)
            mark = QLabel("使用中")
            mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
            mark.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            color = active_palette()["accent"]
            mark.setStyleSheet(
                f"background: {_qss_color(color, 30)}; color: {_qss_color(color)};"
                f"border: 1px solid {_qss_color(color, 130)};"
                "border-radius: 8px; padding: 2px 8px; font-size: 11px;"
            )
            self._variant_marks[variant.member] = mark
            grid.addWidget(mark, row, 2)
            button = QPushButton("使用此变体")
            button.setObjectName("GhostButton")
            button.clicked.connect(
                lambda _checked=False, member=variant.member: self._choose_variant(member)
            )
            self._variant_buttons[variant.member] = button
            grid.addWidget(button, row, 3)
        grid.setColumnStretch(1, 1)
        box.addLayout(grid)

        self._variant_status = QLabel("")
        self._variant_status.setObjectName("Faint")
        self._variant_status.setWordWrap(True)
        self._variant_status.hide()
        box.addWidget(self._variant_status)
        self._apply_variant_marks()
        layout.addWidget(frame)

    def _choose_variant(self, member: str) -> None:
        for button in self._variant_buttons.values():
            button.setEnabled(False)
        if self._variant_status is not None:
            self._variant_status.setText(f"正在切换变体：{member}…")
            self._variant_status.show()
        self.variantRequested.emit(member)

    def _apply_variant_marks(self) -> None:
        if not self._variants:
            return
        current = entry_variant(self.item.entry)
        for member, mark in self._variant_marks.items():
            is_current = member == current
            mark.setVisible(is_current)
            button = self._variant_buttons.get(member)
            if button is not None:
                button.setVisible(True)
                button.setEnabled(not is_current)

    def _open_history_row(self, item: QTableWidgetItem) -> None:
        first = self.history_table.item(item.row(), 0)
        url = str(first.data(Qt.ItemDataRole.UserRole) or "") if first else ""
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _build_bind_section(self, layout: QVBoxLayout) -> None:
        key = str((self.item.entry or {}).get("key") or "")
        if self.item.source != "local" or not key:
            return
        source = str((self.item.entry or {}).get("source") or "")
        if self.level.tuf_id:
            if source == "download":
                return
            frame = QFrame()
            frame.setObjectName("FilterPanel")
            box = QVBoxLayout(frame)
            box.setContentsMargins(12, 10, 12, 10)
            box.setSpacing(6)
            self._tuf_frame = frame
            self._tuf_bind_label = QLabel("")
            self._tuf_bind_label.setObjectName("Muted")
            self._tuf_bind_label.setWordWrap(True)
            self._tuf_fill_button = QPushButton("从 TUF 补齐信息")
            self._tuf_fill_button.setObjectName("PrimaryButton")
            self._tuf_fill_button.clicked.connect(
                lambda: self.tufFillRequested.emit(key)
            )
            self._tuf_unbind_button = QPushButton("解除绑定")
            self._tuf_unbind_button.setObjectName("GhostButton")
            self._tuf_unbind_button.clicked.connect(
                lambda: self.unbindRequested.emit(key)
            )
            row = QHBoxLayout()
            row.addWidget(self._tuf_bind_label, 1)
            row.addWidget(self._tuf_fill_button)
            row.addWidget(self._tuf_unbind_button)
            box.addLayout(row)
            self._tuf_hint = QLabel("")
            self._tuf_hint.setObjectName("Faint")
            self._tuf_hint.setWordWrap(True)
            box.addWidget(self._tuf_hint)
            self._apply_tuf_section()
            layout.addWidget(frame)
            return

        frame = QFrame()
        frame.setObjectName("FilterPanel")
        box = QVBoxLayout(frame)
        box.setContentsMargins(12, 10, 12, 10)
        box.setSpacing(8)
        title = QLabel("绑定 TUF 编号")
        title.setStyleSheet("font-weight: 600;")
        box.addWidget(title)
        hint = QLabel("输入编号直接查询，或输入曲名 / 谱师关键词搜索。绑定不会移动或重命名本地文件。")
        hint.setObjectName("Faint")
        hint.setWordWrap(True)
        box.addWidget(hint)

        search_row = QHBoxLayout()
        self.bind_input = QLineEdit()
        self.bind_input.setPlaceholderText("TUF 编号，或曲名 / 谱师关键词")
        self.bind_input.returnPressed.connect(self._request_bind_search)
        self.bind_search_button = QPushButton("搜索")
        self.bind_search_button.setObjectName("GhostButton")
        self.bind_search_button.clicked.connect(self._request_bind_search)
        search_row.addWidget(self.bind_input, 1)
        search_row.addWidget(self.bind_search_button)
        box.addLayout(search_row)

        self.bind_status = QLabel("")
        self.bind_status.setObjectName("Faint")
        self.bind_status.setWordWrap(True)
        box.addWidget(self.bind_status)

        self.bind_results = QListWidget()
        self.bind_results.setFixedHeight(104)
        self.bind_results.hide()
        self.bind_results.itemDoubleClicked.connect(lambda *_: self._emit_bind())
        box.addWidget(self.bind_results)

        self.bind_button = QPushButton("绑定选中谱面")
        self.bind_button.setObjectName("PrimaryButton")
        self.bind_button.setEnabled(False)
        self.bind_button.clicked.connect(self._emit_bind)
        box.addWidget(self.bind_button)
        layout.addWidget(frame)

    def _apply_tuf_section(self) -> None:
        if not hasattr(self, "_tuf_bind_label") or not hasattr(self, "_tuf_hint"):
            return
        tuf_id = self.level.tuf_id
        if not tuf_id:
            self._tuf_frame.hide()
            return
        self._tuf_frame.show()
        if is_tuf_bound(self.item.entry):
            self._tuf_bind_label.setText(f"已绑定 TUF #{tuf_id}")
            self._tuf_fill_button.hide()
            self._tuf_unbind_button.show()
            self._tuf_hint.setText(
                "曲名、曲师、谱师、难度等按 TUF 数据显示；物量按本地文件。"
            )
        else:
            self._tuf_bind_label.setText(f"已识别 TUF #{tuf_id}（未绑定）")
            self._tuf_fill_button.show()
            self._tuf_fill_button.setEnabled(True)
            self._tuf_unbind_button.hide()
            self._tuf_hint.setText(
                "编号来自文件名/文件夹名，还没有联网获取 TUF 数据。"
                "点「从 TUF 补齐信息」可校正曲名/曲师/谱师等 TUF 数据，物量仍按本地文件。"
            )
        self._tuf_hint.setStyleSheet("")
        self._tuf_hint.show()

    def _build_collection_section(self, layout: QVBoxLayout) -> None:
        key = str((self.item.entry or {}).get("key") or "")
        if self.item.source != "local" or not key:
            return
        frame = QFrame()
        frame.setObjectName("FilterPanel")
        box = QVBoxLayout(frame)
        box.setContentsMargins(12, 10, 12, 10)
        box.setSpacing(8)
        title = QLabel("合集归类")
        title.setStyleSheet("font-weight: 600;")
        box.addWidget(title)
        hint = QLabel(
            "选择已有合集，或直接输入新名称。只更新谱面库索引，不移动或重命名本地文件。"
        )
        hint.setObjectName("Faint")
        hint.setWordWrap(True)
        box.addWidget(hint)

        row = QHBoxLayout()
        self.collection_combo = _choice_combo()
        self.collection_combo.setEditable(True)
        self.collection_combo.addItem("不归入合集", "")
        for name in self._collections:
            self.collection_combo.addItem(name, name)
        current = str((self.item.entry or {}).get("collection") or "")
        index = self.collection_combo.findData(current) if current else 0
        if current and index < 0:
            self.collection_combo.addItem(current, current)
            index = self.collection_combo.count() - 1
        self.collection_combo.setCurrentIndex(max(0, index))
        if self.collection_combo.lineEdit() is not None:
            self.collection_combo.lineEdit().setPlaceholderText("选择或输入合集名称")
        self.collection_apply_button = QPushButton("应用归类")
        self.collection_apply_button.setObjectName("PrimaryButton")
        self.collection_apply_button.clicked.connect(self._emit_collection)
        row.addWidget(self.collection_combo, 1)
        row.addWidget(self.collection_apply_button)
        box.addLayout(row)

        self.collection_status = QLabel("")
        self.collection_status.setObjectName("Faint")
        self.collection_status.setWordWrap(True)
        box.addWidget(self.collection_status)
        layout.addWidget(frame)

    def _emit_collection(self) -> None:
        key = str((self.item.entry or {}).get("key") or "")
        if not key:
            return
        name = self.collection_combo.currentText().strip()
        if name == "不归入合集":
            name = ""
        self.collection_apply_button.setEnabled(False)
        self.collection_status.setText("正在更新谱面库索引…")
        self.collectionAssignRequested.emit(key, name)

    def set_collection_result(self, collection: str, message: str) -> None:
        if self.item.entry is not None:
            self.item.entry["collection"] = collection
            if collection:
                self.item.entry["category"] = "Collection"
        self.collection_value.setText(collection or "-")
        self.collection_apply_button.setEnabled(True)
        self.collection_status.setText(message)
        index = self.collection_combo.findData(collection) if collection else 0
        if collection and index < 0:
            self.collection_combo.addItem(collection, collection)
            index = self.collection_combo.count() - 1
        self.collection_combo.setCurrentIndex(max(0, index))

    def _request_bind_search(self) -> None:
        query = self.bind_input.text().strip()
        if not query:
            self.bind_status.setText("请输入 TUF 编号或关键词")
            return
        self.bind_search_button.setEnabled(False)
        self.bind_status.setText("正在查询 TUF…")
        self.bindSearchRequested.emit(query)

    def set_bind_results(self, results: list[Any]) -> None:
        self.bind_search_button.setEnabled(True)
        self._bind_results = [item for item in results if isinstance(item, LevelInfo)]
        self.bind_results.clear()
        for level in self._bind_results:
            text = (
                f"{level.song or '(未命名)'} · {level.difficulty or '未评级'} · "
                f"{level.charter or '未知谱师'} · #{level.tuf_id}"
            )
            row = QListWidgetItem(text)
            row.setData(Qt.ItemDataRole.UserRole, level)
            self.bind_results.addItem(row)
        self.bind_results.setVisible(bool(self._bind_results))
        self.bind_button.setEnabled(bool(self._bind_results))
        if self._bind_results:
            self.bind_results.setCurrentRow(0)
            self.bind_status.setText(
                f"找到 {len(self._bind_results)} 个结果，确认后点「绑定选中谱面」。"
            )
        else:
            self.bind_status.setText("没有找到匹配的谱面，换个编号或关键词试试。")

    def set_bind_error(self, message: str) -> None:
        self.bind_search_button.setEnabled(True)
        self.bind_button.setEnabled(bool(self._bind_results))
        self.bind_status.setText(message or "查询失败")

    def reset_bind_action(self, message: str = "") -> None:
        self.bind_button.setEnabled(bool(self._bind_results))
        self.bind_search_button.setEnabled(True)
        if message:
            self.bind_status.setText(message)

    def _selected_bind_level(self) -> LevelInfo | None:
        row = self.bind_results.currentItem()
        if row is None:
            return None
        level = row.data(Qt.ItemDataRole.UserRole)
        return level if isinstance(level, LevelInfo) else None

    def _emit_bind(self) -> None:
        level = self._selected_bind_level()
        if level is None:
            self.bind_status.setText("请先选择一个搜索结果")
            return
        self.bind_button.setEnabled(False)
        self.bind_status.setText("正在绑定…")
        self.bindRequested.emit(level)

    def _link_button(self, layout: QHBoxLayout, text: str, url: str) -> None:
        button = QPushButton(text)
        button.setObjectName("GhostButton")
        button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
        layout.addWidget(button)


class SubmissionQueueDialog(QDialog):
    publishRequested = Signal(object)
    detailRequested = Signal(object)
    refreshRequested = Signal()

    def __init__(self, items: list[ChartItem], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[ChartItem] = []
        self.setObjectName("SubmissionQueueDialog")
        self.setWindowTitle("待投稿队列")
        self.resize(780, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)

        title = QLabel("待投稿队列")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        self.count_label = QLabel("")
        self.count_label.setObjectName("Muted")
        layout.addWidget(self.count_label)
        hint = QLabel(
            "只显示已下载、已匹配 TUF、且还没有投稿记录的谱面。"
            "投稿完成后回来点「刷新队列」，已投稿的谱面会自动移出。"
        )
        hint.setObjectName("Faint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["曲名", "难度", "TUF", "合集"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.itemDoubleClicked.connect(lambda *_: self._emit_publish())
        layout.addWidget(self.table, 1)

        self.empty = QLabel(
            "队列是空的：本地谱面都已投稿，或还没有匹配 TUF 编号的谱面。"
        )
        self.empty.setObjectName("ChartEmpty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.hide()
        layout.addWidget(self.empty, 1)

        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("刷新队列")
        self.refresh_button.setObjectName("GhostButton")
        self.refresh_button.clicked.connect(lambda: self.refreshRequested.emit())
        self.detail_button = QPushButton("查看详情")
        self.detail_button.setObjectName("GhostButton")
        self.detail_button.clicked.connect(self._emit_detail)
        self.publish_button = QPushButton("去投稿")
        self.publish_button.setObjectName("PrimaryButton")
        self.publish_button.clicked.connect(self._emit_publish)
        close = QPushButton("关闭")
        close.setObjectName("GhostButton")
        close.clicked.connect(self.reject)
        buttons.addWidget(self.refresh_button)
        buttons.addStretch(1)
        buttons.addWidget(self.detail_button)
        buttons.addWidget(self.publish_button)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self.set_items(items)

    def set_items(self, items: list[ChartItem]) -> None:
        self._items = list(items)
        self.table.setRowCount(0)
        for item in self._items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            level = item.level
            values = [
                level.song or "(未命名谱面)",
                level.difficulty or entry_band(item.entry or {}) or "-",
                f"#{level.tuf_id}" if level.tuf_id else "-",
                str((item.entry or {}).get("collection") or "-"),
            ]
            for col, text in enumerate(values):
                cell = QTableWidgetItem(str(text))
                if col == 0 and item.local_path:
                    cell.setToolTip(item.local_path)
                self.table.setItem(row, col, cell)
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(0, max(240, self.table.columnWidth(0)))
        has_items = bool(self._items)
        if has_items:
            self.table.setCurrentCell(0, 0)
        self.table.setVisible(has_items)
        self.empty.setVisible(not has_items)
        self.count_label.setText(
            f"{len(self._items)} 个待投稿谱面 · 双击可直接开始"
            if has_items
            else "0 个待投稿谱面"
        )
        self.detail_button.setEnabled(has_items)
        self.publish_button.setEnabled(has_items)

    def _current_item(self) -> ChartItem | None:
        row = self.table.currentRow()
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    def _emit_publish(self) -> None:
        item = self._current_item()
        if item is not None:
            self.publishRequested.emit(item)

    def _emit_detail(self) -> None:
        item = self._current_item()
        if item is not None:
            self.detailRequested.emit(item)


class ChartLibraryPage(QWidget):
    searchRequested = Signal(dict)
    loadMoreRequested = Signal(dict)
    downloadRequested = Signal(object)
    downloadTasksRequested = Signal()
    detailRequested = Signal(object)
    queueRequested = Signal()
    profileRequested = Signal(str)
    refreshRequested = Signal()
    importRequested = Signal()
    pruneRequested = Signal()
    openDirRequested = Signal()
    redownloadRequested = Signal(dict)
    deleteRequested = Signal(str)
    deleteManyRequested = Signal(list)
    batchBindRequested = Signal()
    manageCollectionsRequested = Signal()
    diskOrganizeRequested = Signal()
    organizeHistoryRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = active_theme()
        self._meta: dict[str, Any] = {}
        self._request_id = 0
        self._loading = False
        self._has_more = False
        self._next_offset = 0
        self._total = 0
        self._submitted_records: dict[int, list[dict[str, Any]]] = {}
        self._tuf_submitted_ids: set[int] = set()
        self._tuf_submitted_counts: dict[int, int] = {}
        self._local_entries: list[dict[str, Any]] = []
        self._collection_names: list[str] = []
        self._last_random_key = ""
        self._download_song = ""
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(450)
        self._search_timer.timeout.connect(self._emit_search)
        self._build_ui()

    def set_theme(self, theme: str) -> None:
        self._theme = theme
        self.online_delegate.set_theme(theme)
        self.local_delegate.set_theme(theme)
        self.difficulty_slider.set_theme(theme)
        self.local_difficulty_slider.set_theme(theme)
        self.online_list.viewport().update()
        self.local_list.viewport().update()

    def refresh_theme(self) -> None:
        self.set_theme(active_theme())

    def _build_ui(self) -> None:
        m = metrics()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(m.page_pad_x, 8, m.page_pad_x, 18)
        outer.setSpacing(m.space_12)

        switch_row = QHBoxLayout()
        switch_row.setSpacing(m.space_8)
        self.online_button = QPushButton("在线 TUF")
        self.local_button = QPushButton("本地谱面")
        for button in (self.online_button, self.local_button):
            button.setCheckable(True)
            button.setObjectName("SegmentButton")
            button.setMinimumHeight(34)
        self.online_button.setChecked(True)
        self.online_button.clicked.connect(lambda: self._switch_view(0))
        self.local_button.clicked.connect(lambda: self._switch_view(1))
        switch_row.addWidget(self.online_button)
        switch_row.addWidget(self.local_button)
        switch_row.addStretch(1)
        self.download_tasks_button = QPushButton("下载任务中心")
        self.download_tasks_button.setObjectName("DownloadQueueButton")
        self.download_tasks_button.clicked.connect(
            lambda: self.downloadTasksRequested.emit()
        )
        self.download_tasks_button.hide()
        switch_row.addWidget(self.download_tasks_button)
        outer.addLayout(switch_row)

        self.download_panel = QFrame()
        self.download_panel.setObjectName("DownloadPanel")
        download_layout = QVBoxLayout(self.download_panel)
        download_layout.setContentsMargins(14, 9, 14, 10)
        download_layout.setSpacing(6)
        download_row = QHBoxLayout()
        self.download_title = QLabel("正在下载")
        self.download_title.setObjectName("Value")
        self.download_detail = QLabel("准备中…")
        self.download_detail.setObjectName("Muted")
        download_row.addWidget(self.download_title)
        download_row.addStretch(1)
        download_row.addWidget(self.download_detail)
        download_layout.addLayout(download_row)
        self.download_bar = QProgressBar()
        self.download_bar.setObjectName("DownloadBar")
        self.download_bar.setTextVisible(False)
        self.download_bar.setRange(0, 0)
        self.download_bar.setFixedHeight(8)
        download_layout.addWidget(self.download_bar)
        self.download_panel.hide()
        outer.addWidget(self.download_panel)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_online_view())
        self.stack.addWidget(self._build_local_view())
        outer.addWidget(self.stack, 1)

        self.online_delegate = ChartCardDelegate(self)
        self.local_delegate = ChartCardDelegate(self)
        self.online_delegate.downloadClicked.connect(self._on_download)
        self.local_delegate.downloadClicked.connect(self._on_download)
        self.online_delegate.detailClicked.connect(self.detailRequested.emit)
        self.local_delegate.detailClicked.connect(self.detailRequested.emit)
        self.online_list.setItemDelegate(self.online_delegate)
        self.local_list.setItemDelegate(self.local_delegate)
        # 曲绘接口保留给后续美化：加载器与「加载完成只重绘对应行」的接线都还在，
        # 但 delegate 默认 show_thumbnails=False，不请求、不绘制，卡片显示难度图标。
        self.thumbnail_loader = shared_thumbnail_loader()
        self.thumbnail_loader.thumbnailReady.connect(self._on_thumbnail_ready)
        self.online_delegate.set_thumbnail_loader(self.thumbnail_loader)

    def _build_online_view(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        search_row = QHBoxLayout()
        search_row.setSpacing(metrics().space_8)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            "搜索曲名、曲师、谱师、TUF 编号，或 song:carnation 语法"
        )
        self.search_input.setToolTip(
            "支持 TUF 搜索语法：song:carnation、bpm:>150、id=1234，点「搜索语法」查看全部字段"
        )
        self.search_input.setMinimumHeight(38)
        self.search_input.setClearButtonEnabled(True)
        self.search_input.returnPressed.connect(self._emit_search)
        self.search_input.textChanged.connect(self._schedule_search)
        self.search_syntax_button = QPushButton("搜索语法")
        self.search_syntax_button.setObjectName("GhostButton")
        self.search_syntax_button.setToolTip("查看 TUF 支持的字段、运算符与示例")
        self.search_syntax_button.clicked.connect(self._show_search_syntax)
        self.search_button = QPushButton("查询")
        self.search_button.setObjectName("PrimaryButton")
        self.search_button.clicked.connect(self._emit_search)
        search_row.addWidget(self.search_input, 1)
        search_row.addWidget(self.search_syntax_button)
        search_row.addWidget(self.search_button)
        layout.addLayout(search_row)

        filters = QFrame()
        filters.setObjectName("FilterPanel")
        # 这一行在 1080×720（库页内容宽 824）下要放排序、四个筛选下拉、重置和
        # 视图切换，按主题默认的 14px 横向内边距会把「Curation」挤出可视区。
        # 只收紧筛选面板里按钮的横向内边距（纵向仍是 7px，高度不变）。
        filters.setStyleSheet("QPushButton#GhostButton { padding: 7px 8px; }")
        filter_layout = QVBoxLayout(filters)
        filter_layout.setContentsMargins(8, 8, 8, 8)
        filter_layout.setSpacing(0)

        # 第一行：标题 + 主难度滑块（整行，避免被按钮挤窄）。
        slider_row = QHBoxLayout()
        slider_row.setSpacing(10)
        self.difficulty_title = QLabel("P/G/U 难度")
        self.difficulty_title.setObjectName("Muted")
        slider_row.addWidget(self.difficulty_title)
        self.difficulty_slider = DifficultyRangeSlider(height=44)
        self.difficulty_slider.setMinimumWidth(280)
        self.difficulty_slider.setToolTip("主难度区间 P1~U20：拖动两端的把手收窄范围")
        self.difficulty_slider.rangeChanged.connect(lambda *_: self._schedule_search())
        slider_row.addWidget(self.difficulty_slider, 1)
        filter_layout.addLayout(slider_row)
        filter_layout.addSpacing(8)

        # 第二行：左端排序控件（原来单独占一行，移到这里用掉空白），
        # 右端筛选下拉框，右端与滑块右端对齐。
        filter_row = QHBoxLayout()
        # 1080 宽度下这一行只有约 742px，压到 6px 才能让每个控件的文字都完整。
        filter_row.setSpacing(6)
        sort_label = QLabel("排序")
        sort_label.setObjectName("Faint")
        self.sort_combo = _choice_combo()
        # 排序项里最长的是「总通关数」，按 3 个字的度量宽度定自然宽度（实际
        # 内容区 61px，足够放下 4 个汉字）；窗口窄时不会把同一行的筛选按钮挤到重叠。
        self.sort_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.sort_combo.setMinimumContentsLength(3)
        for label, _key in SORT_OPTIONS:
            self.sort_combo.addItem(label)
        self.sort_combo.currentIndexChanged.connect(lambda *_: self._emit_search())
        self.order_button = QPushButton("↓ 降序")
        self.order_button.setObjectName("GhostButton")
        self.order_button.setFixedWidth(76)
        self.order_button.clicked.connect(self._toggle_order)
        filter_row.addWidget(sort_label)
        filter_row.addWidget(self.sort_combo)
        filter_row.addWidget(self.order_button)
        filter_row.addStretch(1)
        self.special_button = FilterMenuButton("特殊难度")
        for name in SPECIAL_DIFFICULTIES:
            self.special_button.add_item(name, name)
        self.q_button = FilterMenuButton(
            "Q 难度", title_formatter=_q_difficulty_title
        )
        self.q_button.setToolTip(
            "TUF 的 Q 难度按 G/U 每 4 级分桶，共 11 个桶（Qq、GQ0~GQ4、UQ0~UQ4），"
            "每桶覆盖 4 个小级（如 GQ0 = G1~G4）。默认全选；全部取消即不带任何 Q 桶。"
        )
        for name in Q_SPECIAL_DIFFICULTIES:
            self.q_button.add_item(q_bucket_label(name), name)
        self.q_button.set_all(True, notify=False)
        q_menu = self.q_button.menu()
        q_menu.addSeparator()
        # 「全选 / 全不选」也保持菜单打开，方便一次看完勾选状态再关。
        select_all = q_menu.addAction("全选")
        select_all.setProperty("keepOpenAction", True)
        select_all.triggered.connect(lambda: self.q_button.set_all(True))
        select_none = q_menu.addAction("全不选")
        select_none.setProperty("keepOpenAction", True)
        select_none.triggered.connect(lambda: self.q_button.set_all(False))
        self.curation_button = FilterMenuButton("Curation")
        self.curation_button.setEnabled(False)
        self.tags_button = FilterMenuButton("标签")
        self.tags_button.setEnabled(False)
        for button in (
            self.special_button,
            self.q_button,
            self.curation_button,
            self.tags_button,
        ):
            button.changed.connect(self._schedule_search)
            filter_row.addWidget(button)
        self.reset_button = QPushButton("重置")
        self.reset_button.setObjectName("GhostButton")
        self.reset_button.clicked.connect(self._reset_filters)
        filter_row.addWidget(self.reset_button)
        # 视图切换也并入筛选行右端：下拉框在真实主题下高 38px，
        # 留在 meta 行会把结果列表往下顶，移到这里 meta 行只剩单行文字。
        self.view_combo = _choice_combo()
        self.view_combo.addItems(["标准视图", "紧凑视图"])
        self.view_combo.currentIndexChanged.connect(self._set_compact)
        filter_row.addWidget(self.view_combo)
        filter_layout.addLayout(filter_row)
        layout.addWidget(filters)

        # 紧凑 meta 行：状态与结果总数共用一行，把列表往上提。
        meta_row = QHBoxLayout()
        meta_row.setSpacing(8)
        self.online_status = ElidedLabel("输入关键词，或直接浏览 TUF 谱面库")
        self.online_status.setObjectName("Faint")
        self.online_status.setWordWrap(False)
        self.online_status.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        meta_row.addWidget(self.online_status, 1)
        self.total_label = QLabel("0 条结果")
        self.total_label.setObjectName("Muted")
        meta_row.addWidget(self.total_label)
        layout.addLayout(meta_row)

        self.online_model = ChartListModel(self)
        self.online_list = self._make_list(self.online_model)
        self.online_empty = QLabel("没有匹配的谱面")
        self.online_empty.setObjectName("ChartEmpty")
        self.online_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.online_empty.hide()
        layout.addWidget(self.online_list, 1)
        layout.addWidget(self.online_empty, 1)
        self.online_list.verticalScrollBar().valueChanged.connect(self._on_scroll)

        more_row = QHBoxLayout()
        more_row.addStretch(1)
        self.more_button = QPushButton("加载更多")
        self.more_button.setObjectName("GhostButton")
        self.more_button.setMinimumWidth(110)
        self.more_button.setToolTip("继续滚动到底部，或点「加载更多」继续加载")
        self.more_button.clicked.connect(self._load_more_clicked)
        self.more_button.hide()
        more_row.addWidget(self.more_button)
        layout.addLayout(more_row)
        return page

    def _build_local_view(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(metrics().space_8)
        self.local_count = QLabel("0 个谱面")
        self.local_count.setObjectName("Muted")
        toolbar.addWidget(self.local_count)
        toolbar.addStretch(1)
        for text, signal in (
            ("刷新", self.refreshRequested),
            ("导入本地目录", self.importRequested),
            ("批量绑定", self.batchBindRequested),
            ("合集管理", self.manageCollectionsRequested),
            ("磁盘整理", self.diskOrganizeRequested),
            ("整理历史", self.organizeHistoryRequested),
            ("清理失效", self.pruneRequested),
            ("打开目录", self.openDirRequested),
        ):
            button = QPushButton(text)
            button.setObjectName("GhostButton")
            button.clicked.connect(signal.emit)
            toolbar.addWidget(button)
        self.local_delete_button = QPushButton("删除选中")
        self.local_delete_button.setObjectName("DangerButton")
        self.local_delete_button.setEnabled(False)
        self.local_delete_button.setToolTip(
            "从谱面库索引移除选中的谱面（Ctrl / Shift 多选），不会删除磁盘文件"
        )
        self.local_delete_button.clicked.connect(self._request_delete_selected)
        toolbar.addWidget(self.local_delete_button)
        layout.addLayout(toolbar)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(metrics().space_8)
        self.local_search = QLineEdit()
        self.local_search.setPlaceholderText("搜索曲名、曲师、谱师、难度或 TUF 编号")
        self.local_search.setClearButtonEnabled(True)
        self.local_search.textChanged.connect(self._apply_local_filter)
        self.local_filter_combo = _choice_combo()
        self.local_filter_combo.addItems(["全部状态", "已下载", "未匹配", "已投稿"])
        self.local_filter_combo.currentIndexChanged.connect(self._apply_local_filter)
        self.local_category_combo = _choice_combo()
        self.local_category_combo.addItem("全部分类", "")
        for key in ("P", "G", "U", "Unranked", "Censored", "Impossible", "Collection"):
            self.local_category_combo.addItem(CATEGORY_LABELS[key], key)
        self.local_category_combo.currentIndexChanged.connect(
            self._on_category_filter_changed
        )
        self.local_collection_combo = _choice_combo()
        self.local_collection_combo.setMinimumWidth(150)
        self.local_collection_combo.currentIndexChanged.connect(
            self._on_collection_filter_changed
        )
        filter_row.addWidget(self.local_search, 1)
        filter_row.addWidget(self.local_filter_combo)
        filter_row.addWidget(self.local_category_combo)
        filter_row.addWidget(self.local_collection_combo)
        layout.addLayout(filter_row)

        slider_row = QFrame()
        slider_row.setObjectName("FilterPanel")
        slider_layout = QHBoxLayout(slider_row)
        slider_layout.setContentsMargins(14, 10, 14, 10)
        slider_layout.setSpacing(10)
        self.local_difficulty_slider = DifficultyRangeSlider()
        self.local_difficulty_slider.setMinimumWidth(300)
        self.local_difficulty_slider.rangeChanged.connect(lambda *_: self._apply_local_filter())
        slider_layout.addWidget(self.local_difficulty_slider, 1)

        self.local_sort_combo = _choice_combo()
        self.local_sort_combo.addItem("最近更新", "recent")
        self.local_sort_combo.addItem("名称排序", "name")
        self.local_sort_combo.addItem("难度排序", "difficulty")
        self.local_sort_combo.currentIndexChanged.connect(self._apply_local_filter)
        slider_layout.addWidget(self.local_sort_combo)

        self.local_order_button = QPushButton("↓ 降序")
        self.local_order_button.setObjectName("GhostButton")
        self.local_order_button.setFixedWidth(88)
        self.local_order_button.clicked.connect(self._toggle_local_order)
        slider_layout.addWidget(self.local_order_button)

        self.local_random_button = QPushButton("随机抽谱")
        self.local_random_button.setObjectName("GhostButton")
        self.local_random_button.clicked.connect(self._pick_random)
        slider_layout.addWidget(self.local_random_button)

        self.queue_button = QPushButton("待投稿队列")
        self.queue_button.setObjectName("GhostButton")
        self.queue_button.clicked.connect(lambda: self.queueRequested.emit())
        slider_layout.addWidget(self.queue_button)

        reset_local = QPushButton("重置筛选")
        reset_local.setObjectName("GhostButton")
        reset_local.clicked.connect(self._reset_local_filters)
        slider_layout.addWidget(reset_local)
        layout.addWidget(slider_row)

        self.local_status = QLabel("下载或导入的谱面会出现在这里，双击卡片查看详情")
        self.local_status.setObjectName("Faint")
        layout.addWidget(self.local_status)

        self.local_model = ChartListModel(self)
        self.local_list = self._make_list(self.local_model)
        # 本地谱面支持 Ctrl / Shift 多选，用于批量从索引移除。
        self.local_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.local_list.selectionModel().selectionChanged.connect(
            lambda *_: self._update_delete_button()
        )
        self.local_empty = QLabel("谱面库还是空的")
        self.local_empty.setObjectName("ChartEmpty")
        self.local_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.local_empty.show()
        layout.addWidget(self.local_list, 1)
        layout.addWidget(self.local_empty, 1)
        return page

    def _make_list(self, model: ChartListModel) -> QListView:
        view = QListView()
        view.setObjectName("ChartList")
        view.setModel(model)
        view.setUniformItemSizes(True)
        view.setMouseTracking(True)
        view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setSpacing(0)
        view.doubleClicked.connect(
            lambda index: self._emit_detail(index.data(ChartListModel.ItemRole))
        )
        return view

    def _on_thumbnail_ready(self, tuf_id: int) -> None:
        """缩略图到货后只刷新对应编号的在线行，不整表重绘。"""
        model = self.online_model
        for row in range(model.rowCount()):
            item = model.item_at(row)
            if item is not None and item.level.tuf_id == tuf_id:
                self.online_list.update(model.index(row, 0))

    def _switch_view(self, index: int) -> None:
        self.online_button.setChecked(index == 0)
        self.local_button.setChecked(index == 1)
        self.stack.setCurrentIndex(index)

    def show_tab(self, name: str) -> None:
        self._switch_view(1 if name == "local" else 0)

    def _emit_detail(self, item: Any) -> None:
        if isinstance(item, ChartItem):
            self.detailRequested.emit(item)

    def _on_download(self, item: Any) -> None:
        if not isinstance(item, ChartItem) or not item.level.tuf_id:
            if isinstance(item, ChartItem):
                self.detailRequested.emit(item)
            return
        delegate = self.local_delegate if item.source == "local" else self.online_delegate
        if int(item.level.tuf_id) in delegate.downloading_ids:
            return
        if item.source == "local" and item.entry:
            self.redownloadRequested.emit(item.entry)
        else:
            self.downloadRequested.emit(item.level)

    def _toggle_order(self) -> None:
        if self.order_button.text().startswith("↓"):
            self.order_button.setText("↑ 升序")
        else:
            self.order_button.setText("↓ 降序")
        self._emit_search()

    def _toggle_local_order(self) -> None:
        if self.local_order_button.text().startswith("↓"):
            self.local_order_button.setText("↑ 升序")
        else:
            self.local_order_button.setText("↓ 降序")
        self._apply_local_filter()

    def selected_local_keys(self) -> list[str]:
        """当前多选的本地条目 key；只读取选中索引，不遍历整张列表。"""
        selection = self.local_list.selectionModel()
        if selection is None:
            return []
        keys: list[str] = []
        seen: set[str] = set()
        for index in selection.selectedIndexes():
            item = self.local_model.item_at(index.row())
            if item is None or item.source != "local" or not item.entry:
                continue
            key = str(item.entry.get("key") or "")
            if key and key not in seen:
                seen.add(key)
                keys.append(key)
        return keys

    def _update_delete_button(self) -> None:
        button = getattr(self, "local_delete_button", None)
        if button is None:
            return
        count = len(self.selected_local_keys())
        button.setEnabled(bool(count))
        button.setText(f"删除选中 ({count})" if count else "删除选中")

    def _request_delete_selected(self) -> None:
        keys = self.selected_local_keys()
        if keys:
            self.deleteManyRequested.emit(keys)

    def _reset_local_filters(self) -> None:
        self.local_search.clear()
        self.local_filter_combo.setCurrentIndex(0)
        self.local_category_combo.setCurrentIndex(0)
        self.local_collection_combo.setCurrentIndex(0)
        self.local_sort_combo.setCurrentIndex(0)
        self.local_order_button.setText("↓ 降序")
        self.local_difficulty_slider.set_range(1, 60)
        self.local_status.setText("下载或导入的谱面会出现在这里，双击卡片查看详情")
        self._apply_local_filter()

    def _pick_random(self) -> None:
        items = self.local_model.items()
        if not items:
            self.local_status.setText("当前筛选下没有可抽取的谱面")
            return
        pool = items
        if len(items) > 1 and self._last_random_key:
            fresh = [item for item in items if self._item_key(item) != self._last_random_key]
            pool = fresh or items
        choice = random.choice(pool)
        self._last_random_key = self._item_key(choice)
        row = next(pos for pos, item in enumerate(items) if item is choice)
        index = self.local_model.index(row, 0)
        self.local_list.setCurrentIndex(index)
        self.local_list.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
        difficulty = choice.level.difficulty or entry_band(choice.entry or {}) or "未评级"
        self.local_status.setText(f"随机抽到：{choice.level.song}（{difficulty}）")

    def _on_category_filter_changed(self) -> None:
        if (
            self.local_category_combo.currentData() != "Collection"
            and self.local_collection_combo.currentData()
        ):
            self.local_collection_combo.blockSignals(True)
            self.local_collection_combo.setCurrentIndex(0)
            self.local_collection_combo.blockSignals(False)
        self._apply_local_filter()

    def _on_collection_filter_changed(self) -> None:
        if self.local_collection_combo.currentData():
            index = self.local_category_combo.findData("Collection")
            if index >= 0 and self.local_category_combo.currentIndex() != index:
                self.local_category_combo.blockSignals(True)
                self.local_category_combo.setCurrentIndex(index)
                self.local_category_combo.blockSignals(False)
        self._apply_local_filter()

    def _refresh_collection_filter(self) -> None:
        current = str(self.local_collection_combo.currentData() or "")
        counts: dict[str, int] = {}
        for entry in self._local_entries:
            name = str(entry.get("collection") or "").strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
        names = set(self._collection_names) | set(counts)
        self.local_collection_combo.blockSignals(True)
        self.local_collection_combo.clear()
        self.local_collection_combo.addItem("全部合集", "")
        for name in sorted(names, key=str.casefold):
            self.local_collection_combo.addItem(f"{name} ({counts.get(name, 0)})", name)
        index = self.local_collection_combo.findData(current)
        self.local_collection_combo.setCurrentIndex(max(0, index))
        self.local_collection_combo.blockSignals(False)

    @staticmethod
    def _item_key(item: ChartItem) -> str:
        entry = item.entry or {}
        return str(
            entry.get("key")
            or entry.get("path")
            or item.level.tuf_id
            or item.level.song
        )

    def _set_compact(self, index: int) -> None:
        compact = index == 1
        self.online_delegate.set_compact(compact)
        self.local_delegate.set_compact(compact)
        self.online_list.scheduleDelayedItemsLayout()
        self.local_list.scheduleDelayedItemsLayout()

    def start_search(self) -> None:
        self._emit_search()

    def _schedule_search(self) -> None:
        self._search_timer.start()

    def _reset_filters(self) -> None:
        self._search_timer.stop()
        self.difficulty_slider.set_range(1, 60)
        self.q_button.set_all(True)
        self.special_button.reset_selection()
        self.curation_button.reset_selection()
        self.tags_button.reset_selection()
        self._emit_search()

    def current_state(self, offset: int = 0) -> dict[str, Any]:
        sort_key = SORT_OPTIONS[self.sort_combo.currentIndex()][1]
        descending = self.order_button.text().startswith("↓")
        return {
            "request_id": self._request_id,
            "query": self.search_input.text().strip(),
            "offset": max(0, int(offset)),
            "limit": SERVER_PAGE_LIMIT,
            "sort": sort_key,
            "descending": descending,
            "pgu_range": (self.difficulty_slider.low, self.difficulty_slider.high),
            "q_difficulties": list(self.q_button.selected()),
            "specials": self.special_button.selected(),
            "tag_ids": self.tags_button.selected(),
            "curation_ids": self.curation_button.selected(),
        }

    def _show_search_syntax(self) -> None:
        from .widgets import SearchSyntaxDialog

        SearchSyntaxDialog(self).exec()

    def _emit_search(self) -> None:
        self._search_timer.stop()
        self._request_id += 1
        self._loading = True
        self._has_more = False
        self._next_offset = 0
        self._total = 0
        self.online_model.clear()
        self.online_list.show()
        self.online_empty.hide()
        self.more_button.hide()
        self.search_button.setEnabled(False)
        self.online_status.setText("正在查询 TUF…")
        self.total_label.setText("加载中…")
        self.searchRequested.emit(self.current_state(0))

    def _load_more_clicked(self) -> None:
        if self._loading or not self._has_more:
            return
        self._loading = True
        self.more_button.hide()
        self.online_status.setText("正在加载更多…")
        self.loadMoreRequested.emit(self.current_state(self._next_offset))

    def _on_scroll(self, value: int) -> None:
        if not self._has_more or self._loading:
            return
        bar = self.online_list.verticalScrollBar()
        if value >= bar.maximum() - 160:
            self._loading = True
            self.more_button.hide()
            self.online_status.setText("正在加载更多…")
            self.loadMoreRequested.emit(self.current_state(self._next_offset))

    def set_meta(self, meta: dict[str, Any]) -> None:
        self._meta = meta or {}
        tags = [item for item in self._meta.get("tags") or [] if isinstance(item, dict)]
        tag_groups: dict[str, list[dict[str, Any]]] = {}
        for tag in tags:
            group = str(tag.get("group") or "其他")
            tag_groups.setdefault(group, []).append(tag)
        self.tags_button.clear_menu()
        for group, items in tag_groups.items():
            for tag in sorted(items, key=lambda x: (x.get("sortOrder") or 0, str(x.get("name")))):
                try:
                    tag_id = int(tag.get("id"))
                except (TypeError, ValueError):
                    continue
                self.tags_button.add_item(
                    str(tag.get("name") or tag_id),
                    tag_id,
                    str(tag.get("color") or ""),
                    group,
                )
        self.tags_button.setEnabled(bool(tags))

        curations = [
            item for item in self._meta.get("curations") or [] if isinstance(item, dict)
        ]
        self.curation_button.clear_menu()
        groups: dict[str, list[dict[str, Any]]] = {}
        for curation in curations:
            group = str(curation.get("group") or "其他")
            groups.setdefault(group, []).append(curation)
        for group, items in groups.items():
            for curation in sorted(
                items, key=lambda x: (x.get("groupSortOrder") or 0, x.get("sortOrder") or 0)
            ):
                try:
                    curation_id = int(curation.get("id"))
                except (TypeError, ValueError):
                    continue
                self.curation_button.add_item(
                    str(curation.get("name") or curation_id),
                    curation_id,
                    str(curation.get("color") or ""),
                    group,
                )
        self.curation_button.setEnabled(bool(curations))
        if self._meta.get("errors"):
            self.online_status.setText("部分筛选数据加载失败：" + "；".join(self._meta["errors"]))

    def set_meta_loading(self) -> None:
        self.online_status.setText("正在加载筛选数据…")

    def set_meta_error(self, message: str) -> None:
        self._meta = {}
        self.online_status.setText("筛选数据加载失败：" + message)

    def set_page(self, payload: dict[str, Any], append: bool = False) -> None:
        request_id = payload.get("request_id")
        if request_id not in (None, 0, self._request_id):
            return
        self._loading = False
        self.search_button.setEnabled(True)
        self._total = int(payload.get("total") or 0)
        downloaded_ids = self._downloaded_ids()
        items: list[ChartItem] = []
        for data in payload.get("items") or []:
            if not isinstance(data, dict):
                continue
            level = LevelInfo.from_dict(data)
            if level is None:
                continue
            items.append(
                ChartItem(
                    level=level,
                    source="tuf",
                    downloaded=bool(level.tuf_id and int(level.tuf_id) in downloaded_ids),
                    submitted=self._tuf_submitted_counts.get(
                        int(level.tuf_id or 0), 0
                    ),
                )
            )
        # 偏移以服务端回显为准：TUF 会把 page/offset 统一成实际 offset，
        # 用「回显 offset + 原始条数」推进，去重后的条数不能拿来算下一页。
        server_offset = max(0, int(payload.get("offset") or 0))
        next_offset = server_offset + len(items)
        previous_next = self._next_offset
        next_has_more = bool(payload.get("has_more"))
        if not items:
            next_has_more = False
        if append and next_offset <= previous_next:
            # 服务端没有前进（或返回了重复页）：停止加载更多，避免滚动死循环。
            next_has_more = False
            next_offset = previous_next
        self._next_offset = next_offset
        self._has_more = next_has_more
        if append:
            seen_ids = {
                int(item.level.tuf_id)
                for item in self.online_model.items()
                if item.level.tuf_id
            }
            fresh: list[ChartItem] = []
            for item in items:
                tuf_id = item.level.tuf_id
                if tuf_id:
                    try:
                        key = int(tuf_id)
                    except (TypeError, ValueError):
                        key = None
                    if key is not None:
                        if key in seen_ids:
                            continue
                        seen_ids.add(key)
                fresh.append(item)
            self.online_model.append_items(fresh)
        else:
            self.online_model.set_items(items)
        count = len(self.online_model.items())
        self.total_label.setText(f"{self._total:,} 条结果" if self._total else f"{count} 条")
        self.online_status.setText(f"已显示 {count} 条")
        self.more_button.setVisible(self._has_more)
        self._show_empty(self.online_list, self.online_empty, count == 0)

    def _downloaded_ids(self) -> set[int]:
        ids: set[int] = set()
        for item in self.local_model.items():
            if item.level.tuf_id:
                ids.add(int(item.level.tuf_id))
        return ids

    def set_error(self, message: str) -> None:
        self._loading = False
        self.search_button.setEnabled(True)
        self.more_button.setVisible(self._has_more and bool(self.online_model.items()))
        self.online_status.setText(message)
        self.total_label.setText("加载失败")

    def set_download_tasks(self, tasks: list[DownloadTask]) -> None:
        items = [task for task in tasks if isinstance(task, DownloadTask)]
        unfinished = [task for task in items if task.status in UNFINISHED_STATUSES]
        active = [task for task in items if task.status in ACTIVE_STATUSES]
        pending = [task for task in items if task.status in PENDING_STATUSES]
        task_ids = {
            int(task.tuf_id)
            for task in unfinished
            if task.tuf_id
        }
        for delegate in (self.online_delegate, self.local_delegate):
            delegate.downloading_ids = set(task_ids)
        self.online_list.viewport().update()
        self.local_list.viewport().update()

        self.download_tasks_button.setVisible(bool(items))
        if items:
            self.download_tasks_button.setText(
                f"下载任务 {len(unfinished)}" if unfinished else "下载任务 · 记录"
            )
            if bool(self.download_tasks_button.property("active")) != bool(unfinished):
                self.download_tasks_button.setProperty("active", bool(unfinished))
                self.download_tasks_button.style().unpolish(self.download_tasks_button)
                self.download_tasks_button.style().polish(self.download_tasks_button)
            completed = sum(1 for task in items if task.status == STATUS_COMPLETED)
            failed = sum(1 for task in items if task.status == STATUS_FAILED)
            cancelled = sum(1 for task in items if task.status == STATUS_CANCELLED)
            self.download_tasks_button.setToolTip(
                f"进行中 {len(active)} · 等待 {len(pending)} · "
                f"完成 {completed} · 失败 {failed} · 取消 {cancelled}"
            )

        if not unfinished:
            self.download_panel.hide()
            self._download_song = ""
            return

        self.download_panel.show()
        if active:
            current = active[-1]
            self._download_song = current.title
            extra = f" · 共 {len(active)} 个同时进行" if len(active) > 1 else ""
            self.download_title.setText(f"下载任务中心 · {len(active)} 个进行中")
            if current.status == STATUS_EXTRACTING:
                self.download_detail.setText(f"{current.title} · 正在解压并归类{extra}")
                self.download_bar.setRange(0, 100)
                self.download_bar.setValue(100)
            elif current.status == STATUS_DOWNLOADING:
                if current.total > 0:
                    percent = current.progress_percent or 0
                    self.download_bar.setRange(0, 100)
                    self.download_bar.setValue(percent)
                    self.download_detail.setText(
                        f"{current.title} · {percent}% · "
                        f"{_format_bytes(current.received)} / {_format_bytes(current.total)}"
                        f"{extra}"
                    )
                else:
                    self.download_bar.setRange(0, 0)
                    self.download_detail.setText(
                        f"{current.title} · 已下载 {_format_bytes(current.received)}{extra}"
                    )
            elif current.status == STATUS_RUNNING:
                self.download_bar.setRange(0, 0)
                self.download_detail.setText(f"{current.title} · 正在连接 TUF…{extra}")
            else:
                self.download_bar.setRange(0, 0)
                self.download_detail.setText(f"{current.title} · 正在取消…")
            return

        first = pending[0] if pending else unfinished[0]
        self._download_song = first.title
        self.download_title.setText(f"下载任务中心 · {len(pending)} 个等待")
        self.download_detail.setText(f"队列下一位：{first.title}")
        self.download_bar.setRange(0, 100)
        self.download_bar.setValue(0)

    def set_download_failed(self, message: str) -> None:
        self.online_delegate.downloading_ids.clear()
        self.local_delegate.downloading_ids.clear()
        self.online_status.setText(message)
        self.download_panel.hide()
        self._download_song = ""
        self.online_list.viewport().update()
        self.local_list.viewport().update()

    def mark_downloaded(self, tuf_id: int | None, path: str, entry: dict[str, Any] | None = None) -> None:
        if tuf_id:
            self.online_delegate.downloading_ids.discard(int(tuf_id))
            self.local_delegate.downloading_ids.discard(int(tuf_id))
        self.online_model.mark_downloaded(tuf_id, path)
        self.online_list.viewport().update()
        self.download_panel.hide()
        self._download_song = ""
        if entry:
            self._refresh_local_item(entry)

    def set_local_entries(self, entries: list[dict[str, Any]]) -> None:
        self._local_entries = [dict(entry) for entry in entries if isinstance(entry, dict)]
        self._refresh_collection_filter()
        self._apply_local_filter()

    def set_collection_names(self, names: list[str]) -> None:
        self._collection_names = [
            str(name).strip() for name in names if str(name).strip()
        ]
        self._refresh_collection_filter()
        self._apply_local_filter()

    def _sort_local_items(self, items: list[ChartItem]) -> list[ChartItem]:
        mode = str(self.local_sort_combo.currentData() or "recent")
        descending = self.local_order_button.text().startswith("↓")
        if mode == "name":
            return sorted(
                items,
                key=lambda item: (
                    (item.level.song or "").casefold(),
                    (item.level.charter or "").casefold(),
                    int(item.level.tuf_id or 0),
                ),
                reverse=descending,
            )
        if mode == "difficulty":
            ranked: list[tuple[int, ChartItem]] = []
            rest: list[ChartItem] = []
            for item in items:
                value = difficulty_value(item.entry or {})
                if value is None:
                    rest.append(item)
                else:
                    ranked.append((value, item))
            ranked.sort(
                key=lambda pair: (pair[0], (pair[1].level.song or "").casefold()),
                reverse=descending,
            )
            rest.sort(
                key=lambda item: (
                    entry_category(item.entry or {}),
                    (item.level.song or "").casefold(),
                )
            )
            return [item for _value, item in ranked] + rest
        return sorted(
            items,
            key=lambda item: str((item.entry or {}).get("updated_at") or ""),
            reverse=descending,
        )

    def _apply_local_filter(self) -> None:
        query = self.local_search.text().strip().lower()
        mode = self.local_filter_combo.currentIndex()
        wanted_category = self.local_category_combo.currentData()
        wanted_collection = str(self.local_collection_combo.currentData() or "")
        low = self.local_difficulty_slider.low
        high = self.local_difficulty_slider.high
        items: list[ChartItem] = []
        pending_count = 0
        for entry in self._local_entries:
            level = LevelInfo.from_dict(entry)
            if level is None:
                continue
            tuf_id = int(level.tuf_id) if level.tuf_id else None
            if level.tuf_id and tuf_id not in self._tuf_submitted_ids:
                pending_count += 1
            submitted = self._tuf_submitted_counts.get(tuf_id or 0, 0)
            matched = bool(tuf_id)
            category = entry_category(entry)
            if mode == 1 and not matched:
                continue
            if mode == 2 and matched:
                continue
            if mode == 3 and not submitted:
                continue
            if wanted_category and category != wanted_category:
                continue
            if wanted_collection and str(entry.get("collection") or "") != wanted_collection:
                continue
            if (low, high) != (1, 60):
                value = difficulty_value(entry)
                if value is None or not (low <= value <= high):
                    continue
            if query:
                haystack = " ".join(
                    [
                        level.song,
                        level.artist,
                        level.charter,
                        level.difficulty,
                        entry_band(entry),
                        str(entry.get("collection") or ""),
                        str(level.tuf_id or ""),
                        " ".join(level.vfxers),
                        " ".join(str(tag.get("name") or "") for tag in level.tags),
                    ]
                ).lower()
                if query not in haystack:
                    continue
            items.append(
                ChartItem(
                    level=level,
                    source="local",
                    entry=dict(entry),
                    downloaded=True,
                    submitted=submitted,
                    matched=matched,
                )
            )
        items = self._sort_local_items(items)
        self.local_model.set_items(items)
        downloaded_ids = {int(item.level.tuf_id) for item in items if item.level.tuf_id}
        online_items = self.online_model.items()
        for item in online_items:
            item.downloaded = bool(item.level.tuf_id and int(item.level.tuf_id) in downloaded_ids)
        if online_items:
            self.online_model.set_items(online_items)
        self.online_list.viewport().update()
        total = len(self._local_entries)
        self.local_count.setText(
            f"显示 {len(items)} / {total} 个谱面" if len(items) != total else f"{total} 个谱面"
        )
        self.local_empty.setText("谱面库还是空的" if not total else "没有匹配的本地谱面")
        self._show_empty(self.local_list, self.local_empty, not items)
        self._update_delete_button()
        self._update_queue_button(pending_count)

    def _update_queue_button(self, count: int) -> None:
        self.queue_button.setText(
            f"待投稿队列 · {count}" if count else "待投稿队列"
        )

    def set_submitted_records(self, records: list[dict[str, Any]]) -> None:
        mapping: dict[int, list[dict[str, Any]]] = {}
        tuf_submitted: set[int] = set()
        tuf_counts: dict[int, int] = {}
        for record in records:
            tuf_id = record.get("tuf_id")
            if not tuf_id:
                level = record.get("level")
                if isinstance(level, dict):
                    tuf_id = level.get("tuf_id")
            try:
                key = int(tuf_id)
            except (TypeError, ValueError):
                continue
            mapping.setdefault(key, []).append(record)
            if is_tuf_submitted(record):
                tuf_submitted.add(key)
                tuf_counts[key] = tuf_counts.get(key, 0) + 1
        self._submitted_records = mapping
        self._tuf_submitted_ids = tuf_submitted
        self._tuf_submitted_counts = tuf_counts
        self._apply_local_filter()
        online_items = self.online_model.items()
        for item in online_items:
            item.submitted = tuf_counts.get(int(item.level.tuf_id or 0), 0)
        if online_items:
            self.online_model.set_items(online_items)

    def records_for(self, tuf_id: int | None) -> list[dict[str, Any]]:
        if not tuf_id:
            return []
        return list(self._submitted_records.get(int(tuf_id), []))

    def pending_items(self) -> list[ChartItem]:
        items: list[ChartItem] = []
        for entry in self._local_entries:
            level = LevelInfo.from_dict(entry)
            if level is None or not level.tuf_id:
                continue
            if int(level.tuf_id) in self._tuf_submitted_ids:
                continue
            items.append(
                ChartItem(
                    level=level,
                    source="local",
                    entry=dict(entry),
                    downloaded=True,
                    submitted=0,
                    matched=True,
                )
            )
        return self._sort_local_items(items)

    def meta(self) -> dict[str, Any]:
        return self._meta

    def _refresh_local_item(self, entry: dict[str, Any]) -> None:
        level = LevelInfo.from_dict(entry)
        if level is None:
            return
        key = str(entry.get("key") or "")
        for index, current in enumerate(self._local_entries):
            same_key = key and str(current.get("key") or "") == key
            same_tuf = level.tuf_id and current.get("tuf_id") == level.tuf_id
            if same_key or same_tuf:
                self._local_entries[index] = dict(entry)
                break
        else:
            self._local_entries.insert(0, dict(entry))
        self._apply_local_filter()

    @staticmethod
    def _show_empty(view: QListView, label: QLabel, empty: bool) -> None:
        view.setVisible(not empty)
        label.setVisible(empty)
