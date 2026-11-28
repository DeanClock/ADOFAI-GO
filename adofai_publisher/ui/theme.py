"""ADOFAI GO 的主题层。

职责分两层：

* :mod:`adofai_publisher.ui.design_tokens` 存放纯数据 token（颜色、尺寸、间距、动效）。
* 本模块把这些 token 渲染成 QSS / QPalette，并保留旧版 API 供现有页面继续调用。

旧 API（``DARK`` / ``LIGHT`` / ``palette`` / ``set_active_theme`` / ``active_theme`` /
``active_palette`` / ``build_qss``）签名与语义保持不变；新增 ``tokens``、``metrics``、
``qss_sections``、``build_qpalette``、``apply_theme``、``theme_bus`` 供后续 UI 会话使用。
"""

from __future__ import annotations

from typing import Any, Callable

from .design_tokens import (
    DARK_TOKENS,
    LIGHT_TOKENS,
    METRICS,
    MONO_FAMILY,
    SANS_FAMILY,
    TITLE_FAMILY,
    Metrics,
    ThemeTokens,
    tokens_for,
)

__all__ = [
    "DARK",
    "LIGHT",
    "palette",
    "set_active_theme",
    "active_theme",
    "active_palette",
    "build_qss",
    "qss_sections",
    "tokens",
    "metrics",
    "build_qpalette",
    "apply_theme",
    "theme_bus",
]


# ---------------------------------------------------------------------------
# 旧调色板：20 个键与改造前完全一致，只是换成新的暖色取值。
# ---------------------------------------------------------------------------


def _legacy_palette(t: ThemeTokens) -> dict[str, str]:
    """把 token 映射回旧调色板键集合（键名与数量必须保持不变）。"""
    return {
        "bg": t.paper_top,
        "sidebar": t.sidebar,
        "surface": t.surface,
        "card": t.surface,
        "card_hover": t.surface_hover,
        "border": t.line,
        "border_soft": t.line_soft,
        "text": t.ink,
        "muted": t.ink_muted,
        "faint": t.ink_faint,
        "accent": t.clay,
        "accent_strong": t.clay_hover,
        "accent_soft": t.accent_soft,
        "selection_text": t.on_accent,
        "success": t.success,
        "warning": t.warning,
        "danger": t.danger,
        "input": t.field,
        "shadow": t.shadow,
    }


DARK = _legacy_palette(DARK_TOKENS)
LIGHT = _legacy_palette(LIGHT_TOKENS)


def palette(theme: str) -> dict[str, str]:
    return LIGHT if theme == "light" else DARK


_ACTIVE_THEME = "dark"


def set_active_theme(theme: str) -> None:
    global _ACTIVE_THEME
    _ACTIVE_THEME = "light" if theme == "light" else "dark"


def active_theme() -> str:
    return _ACTIVE_THEME


def active_palette() -> dict[str, str]:
    return palette(_ACTIVE_THEME)


def tokens(theme: str | None = None) -> ThemeTokens:
    """取主题 token；``theme=None`` 时使用当前激活主题。"""
    return tokens_for(_ACTIVE_THEME if theme is None else theme)


def metrics() -> Metrics:
    """尺寸 / 间距 / 字号 / 动效 token。"""
    return METRICS


# ---------------------------------------------------------------------------
# QSS 渲染
# ---------------------------------------------------------------------------


def _rule(selector: str, *declarations: str) -> str:
    """渲染单个 QSS 规则块；declarations 每项形如 ``"color: #fff"``，不需要分号。"""
    body = "\n".join(f"    {item.rstrip(';')};" for item in declarations)
    return f"{selector} {{\n{body}\n}}"


def _foundation(t: ThemeTokens, m: Metrics) -> str:
    """全局基线：字体、窗口纸底、滚动容器、弹层与菜单。"""
    return "\n".join(
        [
            _rule(
                "*",
                f"font-family: {SANS_FAMILY}",
                f"font-size: {m.font_body}px",
                f"color: {t.ink}",
            ),
            _rule(
                "QWidget#AppRoot",
                "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                f"stop:0 {t.paper_top}, stop:1 {t.paper_bottom})",
            ),
            _rule(
                "QToolTip",
                f"background: {t.surface}",
                f"color: {t.ink}",
                f"border: 1px solid {t.line}",
                f"border-radius: {m.radius_control}px",
                f"padding: {m.space_4 + 2}px {m.space_8 + 1}px",
            ),
            _rule("QScrollArea", "background: transparent", "border: none"),
            _rule("QScrollArea > QWidget > QWidget", "background: transparent"),
            _rule("QDialog", f"background: {t.paper_top}"),
            _rule("QDialog#ChartDetailDialog QScrollArea", f"background: {t.paper_top}"),
            # 系统消息框的标题文字跟随标题字族 / 字重；正文保持默认。
            _rule(
                "QMessageBox QLabel#qt_msgbox_label",
                f"font-family: {TITLE_FAMILY}",
                f"font-weight: {m.title_weight}",
            ),
            _rule(
                "QMenu",
                f"background: {t.surface}",
                f"border: 1px solid {t.line}",
                f"border-radius: {m.radius_control}px",
                f"padding: {m.space_4 + 2}px",
            ),
            _rule(
                "QMenu::item",
                "padding: 6px 24px 6px 10px",
                f"border-radius: {m.radius_control}px",
            ),
            _rule(
                "QMenu::item:selected",
                f"background: {t.accent_soft}",
                f"color: {t.clay}",
            ),
            _rule(
                "QMenu::separator",
                "height: 1px",
                f"background: {t.line_soft}",
                f"margin: {m.space_4}px {m.space_8}px",
            ),
        ]
    )


def _shell(t: ThemeTokens, m: Metrics) -> str:
    """外壳：侧边栏、导航、顶栏、页面标题、状态栏。"""
    # 步骤条头部条与 ContentSheet 的 surface 拉开一档：浅色更深、深色更亮。
    # 深色若用 field，未激活步号圆圈（同样是 field）会糊在头部底色里。
    step_header_bg = t.surface_hover
    return "\n".join(
        [
            _rule(
                "QWidget#SideBar",
                f"background: {t.sidebar}",
                f"border-right: 1px solid {t.line_soft}",
            ),
            _rule(
                "QLabel#AppLogo",
                f"font-family: {TITLE_FAMILY}",
                "font-size: 20px",
                f"font-weight: {m.title_weight}",
                f"color: {t.clay}",
            ),
            _rule(
                "QLabel#LogoBadge",
                "background: transparent",
                "border: none",
                f"border-radius: {m.radius_control}px",
            ),
            _rule(
                "QLabel#AppName",
                f"font-family: {TITLE_FAMILY}",
                "font-size: 14px",
                f"font-weight: {m.title_weight}",
            ),
            _rule(
                "QLabel#AppVersion",
                f"color: {t.ink_faint}",
                f"font-size: {m.font_small}px",
            ),
            _rule(
                "QPushButton#NavButton",
                "text-align: left",
                f"padding: 0 {m.space_12 + 2}px",
                f"min-height: {m.nav_item_height}px",
                "border: 1px solid transparent",
                f"border-radius: {m.radius_control}px",
                f"color: {t.ink_muted}",
                "background: transparent",
                f"font-size: {m.font_body}px",
            ),
            _rule(
                "QPushButton#NavButton:hover",
                f"background: {t.surface_hover}",
                f"color: {t.ink}",
            ),
            _rule("QPushButton#NavButton:pressed", f"background: {t.accent_soft}"),
            _rule(
                "QPushButton#NavButton:checked",
                f"background: {t.accent_soft}",
                f"border: 1px solid {t.line}",
                f"border-left: 3px solid {t.clay}",
                f"color: {t.clay}",
                "font-weight: 600",
            ),
            _rule("QPushButton#NavButton:focus", f"border: 1px solid {t.clay}"),
            _rule(
                "QPushButton#NavButton:disabled",
                f"color: {t.ink_faint}",
                "background: transparent",
            ),
            _rule(
                "QFrame#TopBar",
                "background: transparent",
                f"border-bottom: 1px solid {t.line_soft}",
            ),
            _rule(
                "QLabel#PageTitle",
                f"font-family: {TITLE_FAMILY}",
                f"font-size: {m.font_page_title}px",
                f"font-weight: {m.title_weight}",
            ),
            _rule(
                "QLabel#PageSubtitle",
                f"color: {t.ink_muted}",
                f"font-size: {m.font_subtitle}px",
            ),
            _rule(
                "QFrame#Divider",
                f"background: {t.line_soft}",
                "max-height: 1px",
                "min-height: 1px",
                "border: none",
            ),
            _rule(
                "QStatusBar",
                f"background: {t.sidebar}",
                f"border-top: 1px solid {t.line_soft}",
                f"color: {t.ink_muted}",
            ),
            # 外壳（UI-4）：内容圆角板与底部状态行。
            _rule(
                "QFrame#ContentSheet",
                f"background: {t.surface}",
                f"border: 1px solid {t.line}",
                f"border-radius: {m.radius_sheet}px",
            ),
            # 工作台：步骤条眉页与滚动区分离，滚动区用纸色，卡片才能浮出来。
            _rule(
                "QFrame#StepHeader",
                f"background: {step_header_bg}",
                f"border-bottom: 1px solid {t.line}",
            ),
            _rule("QScrollArea#WorkspaceScroll", "background: transparent", "border: none"),
            _rule("QWidget#WorkspaceBody", f"background: {t.paper_top}"),
            _rule(
                "QFrame#StatusLine",
                "background: transparent",
                f"border-top: 1px solid {t.line_soft}",
            ),
            _rule("QLabel#StatusText", f"color: {t.ink_muted}"),
            _rule("QLabel#StatusMeta", f"color: {t.ink_faint}"),
            _rule(
                "QFrame#RunLogPanel",
                f"background: {t.surface}",
                f"border: 1px solid {t.line}",
                f"border-radius: {m.radius_card}px",
            ),
            _rule(
                "QPlainTextEdit#RunLogView",
                f"background: {t.field}",
                f"border: 1px solid {t.line_soft}",
                f"border-radius: {m.radius_control}px",
                f"color: {t.ink_muted}",
                f"font-family: {MONO_FAMILY}",
                f"font-size: {m.font_small}px",
                "padding: 6px",
            ),
        ]
    )


def _controls(t: ThemeTokens, m: Metrics) -> str:
    """基础控件：输入、按钮、分段、复选框、滚动条。"""
    fields = (
        "QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox"
    )
    return "\n".join(
        [
            _rule(
                fields,
                f"background: {t.field}",
                f"border: 1px solid {t.line}",
                f"border-radius: {m.radius_control}px",
                f"padding: 7px {m.space_8 + 2}px",
                "min-height: 20px",
                f"selection-background-color: {t.clay}",
                f"selection-color: {t.on_accent}",
            ),
            _rule(
                "QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, "
                "QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus",
                f"border: 1px solid {t.clay}",
            ),
            _rule(
                "QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled, "
                "QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled",
                f"color: {t.ink_faint}",
                f"background: {t.surface}",
                f"border-color: {t.line_soft}",
            ),
            _rule("QComboBox", "min-height: 22px"),
            _rule("QComboBox::drop-down", "border: none", "width: 22px"),
            _rule(
                "QComboBox QAbstractItemView",
                f"background: {t.surface}",
                f"border: 1px solid {t.line}",
                f"selection-background-color: {t.clay}",
                f"selection-color: {t.on_accent}",
            ),
            _rule(
                "QSpinBox::up-button, QSpinBox::down-button, "
                "QDoubleSpinBox::up-button, QDoubleSpinBox::down-button",
                "width: 16px",
                "background: transparent",
                "border: none",
            ),
            _rule(
                "QPushButton",
                f"background: {t.surface}",
                f"border: 1px solid {t.line}",
                f"border-radius: {m.radius_control}px",
                "padding: 7px 14px",
                f"color: {t.ink}",
            ),
            _rule(
                "QPushButton:hover",
                f"background: {t.surface_hover}",
                f"border-color: {t.clay}",
            ),
            _rule(
                "QPushButton:pressed",
                f"background: {t.line_soft}",
                f"border-color: {t.clay_pressed}",
            ),
            _rule("QPushButton:focus", f"border: 1px solid {t.clay}"),
            _rule(
                "QPushButton:disabled",
                f"color: {t.ink_faint}",
                f"border-color: {t.line_soft}",
                f"background: {t.surface}",
            ),
            _rule(
                "QPushButton#PrimaryButton",
                f"background: {t.clay}",
                f"border: 1px solid {t.clay}",
                f"color: {t.on_accent}",
                "font-weight: 700",
                f"padding: 9px {m.space_16 + 2}px",
            ),
            _rule(
                "QPushButton#PrimaryButton:hover",
                f"background: {t.clay_hover}",
                f"border-color: {t.clay_hover}",
            ),
            _rule(
                "QPushButton#PrimaryButton:pressed",
                f"background: {t.clay_pressed}",
                f"border-color: {t.clay_pressed}",
            ),
            _rule(
                "QPushButton#PrimaryButton:focus",
                f"border: 1px solid {t.clay_pressed}",
            ),
            _rule(
                "QPushButton#PrimaryButton:disabled",
                f"background: {t.line}",
                f"border-color: {t.line}",
                f"color: {t.ink_faint}",
            ),
            _rule(
                "QPushButton#DangerButton",
                f"color: {t.danger}",
                f"border-color: {t.danger}",
                "background: transparent",
            ),
            _rule(
                "QPushButton#DangerButton:hover",
                f"background: {t.danger_soft}",
                f"border-color: {t.danger}",
            ),
            _rule(
                "QPushButton#DangerButton:pressed",
                f"background: {t.danger_soft}",
                f"border-color: {t.danger}",
                f"color: {t.ink_muted}",
            ),
            # 次操作按钮：常态也有 1px 描边和 surface 底，不能再是一段“裸文字”。
            _rule(
                "QPushButton#GhostButton",
                f"background: {t.surface}",
                f"border: 1px solid {t.line}",
                f"color: {t.ink_muted}",
            ),
            _rule(
                "QPushButton#GhostButton:hover",
                f"background: {t.surface_hover}",
                f"border-color: {t.clay}",
                f"color: {t.ink}",
            ),
            _rule(
                "QPushButton#GhostButton:pressed",
                f"background: {t.line_soft}",
                f"border-color: {t.clay_pressed}",
            ),
            # ID 选择器优先级高于通用 :focus，必须单独补焦点环。
            _rule("QPushButton#GhostButton:focus", f"border: 1px solid {t.clay}"),
            _rule(
                "QPushButton#GhostButton:disabled",
                f"background: {t.surface}",
                f"border-color: {t.line_soft}",
                f"color: {t.ink_faint}",
            ),
            _rule(
                "QPushButton#SegmentButton",
                "background: transparent",
                f"border: 1px solid {t.line}",
                f"border-radius: {m.radius_control}px",
                f"color: {t.ink_muted}",
                f"padding: 6px {m.space_16 + 2}px",
            ),
            _rule(
                "QPushButton#SegmentButton:hover",
                f"background: {t.surface_hover}",
                f"color: {t.ink}",
            ),
            _rule("QPushButton#SegmentButton:pressed", f"background: {t.line_soft}"),
            _rule(
                "QPushButton#SegmentButton:checked",
                f"background: {t.accent_soft}",
                f"border-color: {t.clay}",
                f"color: {t.clay}",
                "font-weight: 600",
            ),
            _rule("QPushButton#SegmentButton:focus", f"border: 1px solid {t.clay}"),
            _rule("QCheckBox", f"spacing: {m.space_8}px"),
            _rule(
                "QCheckBox::indicator",
                "width: 16px",
                "height: 16px",
                "border-radius: 4px",
                f"border: 1px solid {t.line}",
                f"background: {t.field}",
            ),
            _rule("QCheckBox::indicator:hover", f"border-color: {t.clay}"),
            _rule(
                "QCheckBox::indicator:checked",
                f"background: {t.clay}",
                f"border-color: {t.clay}",
            ),
            _rule("QCheckBox:disabled", f"color: {t.ink_faint}"),
            _rule(
                "QScrollBar:vertical",
                "background: transparent",
                "width: 10px",
                "margin: 2px",
            ),
            _rule(
                "QScrollBar::handle:vertical",
                f"background: {t.line}",
                "border-radius: 5px",
                "min-height: 28px",
            ),
            _rule("QScrollBar::handle:vertical:hover", f"background: {t.ink_faint}"),
            _rule(
                "QScrollBar::add-line, QScrollBar::sub-line",
                "height: 0",
                "width: 0",
                "background: transparent",
                "border: none",
            ),
            _rule(
                "QScrollBar::add-page, QScrollBar::sub-page",
                "background: transparent",
            ),
            _rule(
                "QScrollBar:horizontal",
                "background: transparent",
                "height: 10px",
                "margin: 2px",
            ),
            _rule(
                "QScrollBar::handle:horizontal",
                f"background: {t.line}",
                "border-radius: 5px",
                "min-width: 28px",
            ),
            _rule("QScrollBar::handle:horizontal:hover", f"background: {t.ink_faint}"),
        ]
    )


def _components(t: ThemeTokens, m: Metrics) -> str:
    """业务组件：卡片、磁贴、列表、表格、下载条、Toast、Chip。"""
    return "\n".join(
        [
            _rule(
                "QFrame#Card",
                f"background: {t.surface}",
                f"border: 1px solid {t.line}",
                f"border-radius: {m.radius_card}px",
            ),
            _rule(
                "QLabel#CardTitle",
                f"font-family: {TITLE_FAMILY}",
                f"font-size: {m.font_card_title}px",
                f"font-weight: {m.title_weight}",
            ),
            _rule(
                "QLabel#CardHint",
                f"color: {t.ink_muted}",
                f"font-size: {m.font_subtitle}px",
            ),
            _rule(
                "QFrame#StatCard",
                f"background: {t.surface}",
                f"border: 1px solid {t.line_soft}",
                f"border-radius: {m.radius_card}px",
            ),
            _rule(
                "QLabel#StatValue",
                f"font-family: {TITLE_FAMILY}",
                f"font-size: {m.font_display}px",
                f"font-weight: {m.title_weight}",
            ),
            _rule(
                "QLabel#StatLabel",
                f"font-size: {m.font_subtitle}px",
                f"color: {t.ink_muted}",
            ),
            _rule(
                "QLabel#StatHint",
                f"font-size: {m.font_caption}px",
                f"color: {t.ink_faint}",
            ),
            _rule(
                "QLabel#HomeGreeting",
                f"font-family: {TITLE_FAMILY}",
                f"font-size: {m.font_heading}px",
                f"font-weight: {m.title_weight}",
            ),
            _rule(
                "QLabel#HomeSubtitle",
                f"font-size: {m.font_subtitle}px",
                f"color: {t.ink_muted}",
            ),
            _rule(
                "QFrame#GuideStep",
                f"background: {t.surface}",
                f"border: 1px solid {t.line_soft}",
                f"border-radius: {m.radius_card}px",
            ),
            _rule(
                "QLabel#GuideStepNumber",
                f"color: {t.clay}",
                f"font-size: {m.font_small}px",
                "font-weight: 700",
            ),
            _rule(
                "QLabel#GuideStepTitle",
                f"font-family: {TITLE_FAMILY}",
                f"font-size: {m.font_body}px",
                f"font-weight: {m.title_weight}",
            ),
            _rule(
                "QLabel#GuideStepText",
                f"color: {t.ink_muted}",
                f"font-size: {m.font_small}px",
            ),
            # 「高级 / ？说明」折叠按钮：给一条更弱的描边，和正文区分但不像主按钮。
            _rule(
                "QToolButton#ObsAdvancedButton",
                "background: transparent",
                f"border: 1px solid {t.line}",
                f"border-radius: {m.radius_control}px",
                f"color: {t.ink_muted}",
                f"padding: {m.space_4}px {m.space_8}px",
                "text-align: left",
            ),
            _rule(
                "QToolButton#ObsAdvancedButton:hover",
                f"background: {t.surface_hover}",
                f"border-color: {t.line}",
                f"color: {t.clay}",
            ),
            _rule("QToolButton#ObsAdvancedButton:focus", f"border-color: {t.clay}"),
            _rule(
                "QPushButton#ToolTile",
                "text-align: left",
                f"background: {t.surface}",
                f"border: 1px solid {t.line_soft}",
                f"border-radius: {m.radius_card}px",
                f"padding: {m.space_8 + 2}px {m.space_12 + 2}px",
                f"color: {t.ink}",
            ),
            _rule(
                "QPushButton#ToolTile:hover",
                f"background: {t.accent_soft}",
                f"border-color: {t.clay}",
                f"color: {t.clay}",
            ),
            _rule(
                "QPushButton#SnippetCard",
                f"background: {t.field}",
                f"border: 1px solid {t.line}",
                f"border-radius: {m.radius_control}px",
                f"padding: 6px {m.space_12}px",
                f"color: {t.ink}",
            ),
            _rule(
                "QPushButton#SnippetCard:hover",
                f"background: {t.accent_soft}",
                f"border-color: {t.clay}",
                f"color: {t.clay}",
            ),
            _rule(
                "QPushButton#SnippetCard:checked",
                f"background: {t.clay}",
                f"border-color: {t.clay}",
                f"color: {t.on_accent}",
                "font-weight: 700",
            ),
            _rule(
                "QLabel#SnippetGroup",
                f"color: {t.ink_muted}",
                f"font-size: {m.font_small}px",
                "font-weight: 600",
                "padding-top: 7px",
            ),
            _rule(
                "QFrame#DropArea",
                f"background: {t.surface}",
                f"border: 2px dashed {t.line}",
                f"border-radius: {m.radius_card}px",
            ),
            _rule(
                "QFrame#DropZone, QFrame#CoverPicker",
                f"background: {t.field}",
                f"border: 1px dashed {t.line}",
                f"border-radius: {m.radius_card}px",
            ),
            _rule(
                "QLabel#DropTitle",
                f"font-family: {TITLE_FAMILY}",
                "font-size: 15px",
                f"font-weight: {m.title_weight}",
            ),
            _rule(
                "QLabel#DropHint",
                f"color: {t.ink_muted}",
                f"font-size: {m.font_subtitle}px",
            ),
            _rule(
                "QLabel#CoverPreview",
                f"color: {t.ink_muted}",
                f"font-size: {m.font_body}px",
            ),
            _rule(
                "QFrame#DownloadPanel",
                f"background: {t.surface}",
                f"border: 1px solid {t.line}",
                f"border-radius: {m.radius_card}px",
            ),
            _rule(
                "QPushButton#DownloadQueueButton",
                f"background: {t.surface}",
                f"border: 1px solid {t.line}",
                f"color: {t.ink_muted}",
                f"padding: 7px {m.space_12 + 1}px",
                f"border-radius: {m.radius_control}px",
            ),
            _rule(
                'QPushButton#DownloadQueueButton[active="true"]',
                f"background: {t.accent_soft}",
                f"border: 1px solid {t.clay}",
                f"color: {t.clay}",
                "font-weight: 700",
            ),
            _rule(
                "QPushButton#DownloadQueueButton:hover",
                f"border-color: {t.clay}",
                f"color: {t.clay}",
            ),
            _rule(
                "QProgressBar#DownloadBar",
                f"background: {t.field}",
                f"border: 1px solid {t.line}",
                "border-radius: 4px",
                "height: 8px",
            ),
            _rule("QProgressBar#DownloadBar::chunk", f"background: {t.clay}", "border-radius: 3px"),
            _rule(
                "QProgressBar#TaskProgress",
                f"background: {t.field}",
                f"border: 1px solid {t.line}",
                "border-radius: 6px",
                f"color: {t.ink}",
                f"font-size: {m.font_caption}px",
                "text-align: center",
            ),
            _rule("QProgressBar#TaskProgress::chunk", f"background: {t.clay}", "border-radius: 5px"),
            _rule(
                "QPushButton#TaskAction, QPushButton#TaskActionDanger",
                "padding: 5px 10px",
                f"font-size: {m.font_subtitle}px",
                f"border-radius: {m.radius_control}px",
                f"background: {t.surface}",
            ),
            _rule(
                "QPushButton#TaskAction",
                f"color: {t.clay}",
                f"border: 1px solid {t.line}",
            ),
            _rule(
                "QPushButton#TaskActionDanger",
                f"color: {t.danger}",
                f"border: 1px solid {t.danger}",
                "background: transparent",
            ),
            _rule(
                "QPushButton#TaskAction:hover",
                f"background: {t.accent_soft}",
                f"border-color: {t.clay}",
            ),
            _rule(
                "QPushButton#TaskActionDanger:hover",
                f"background: {t.danger_soft}",
                f"border-color: {t.danger}",
            ),
            _rule(
                "QFrame#UmmModRow",
                f"background: {t.surface}",
                f"border: 1px solid {t.line}",
                f"border-radius: {m.radius_card}px",
            ),
            _rule("QFrame#UmmModRow:hover", f"background: {t.surface_hover}"),
            _rule(
                "QFrame#FlowTile",
                f"background: {t.surface}",
                f"border: 1px solid {t.line_soft}",
                f"border-radius: {m.radius_card}px",
            ),
            _rule(
                "QLabel#FlowNumber",
                f"color: {t.clay}",
                f"font-family: {MONO_FAMILY}",
                f"font-size: {m.font_small}px",
                "font-weight: 700",
            ),
            _rule(
                "QPushButton#RecentRow",
                "text-align: left",
                "background: transparent",
                "border: none",
                f"border-bottom: 1px solid {t.line_soft}",
                "border-radius: 0",
                f"padding: 9px {m.space_4}px",
                f"color: {t.ink}",
            ),
            _rule(
                "QPushButton#RecentRow:hover",
                f"background: {t.accent_soft}",
                f"color: {t.clay}",
            ),
            _rule("QLabel#Muted", f"color: {t.ink_muted}"),
            _rule("QLabel#Faint", f"color: {t.ink_faint}", f"font-size: {m.font_small}px"),
            _rule("QLabel#Value", "font-weight: 600"),
            _rule("QLabel#Mono", f"font-family: {MONO_FAMILY}"),
            _rule(
                "QLabel#AccuracyBig",
                f"font-family: {MONO_FAMILY}",
                "font-size: 32px",
                "font-weight: 700",
                f"color: {t.clay}",
            ),
            _rule(
                "QTextEdit#Log",
                f"font-family: {MONO_FAMILY}",
                f"font-size: {m.font_small}px",
                f"background: {t.field}",
                f"color: {t.ink_muted}",
                f"border: 1px solid {t.line}",
                f"border-radius: {m.radius_control}px",
                f"padding: {m.space_8}px",
            ),
            _rule(
                "QListWidget, QListView, QTreeWidget, QTreeView, QTableView",
                f"background: {t.field}",
                f"border: 1px solid {t.line}",
                f"border-radius: {m.radius_control}px",
                f"alternate-background-color: {t.surface}",
                f"color: {t.ink}",
                f"selection-background-color: {t.clay}",
                f"selection-color: {t.on_accent}",
            ),
            _rule(
                "QListWidget::item, QListView::item, QTreeWidget::item, QTreeView::item",
                "padding: 5px 8px",
                "border-radius: 6px",
                f"color: {t.ink}",
            ),
            _rule(
                "QListWidget::item:hover, QListView::item:hover, "
                "QTreeWidget::item:hover, QTreeView::item:hover",
                f"background: {t.surface_hover}",
            ),
            _rule(
                "QListWidget::item:selected, QListView::item:selected, "
                "QTreeWidget::item:selected, QTreeView::item:selected",
                f"background: {t.clay}",
                f"color: {t.on_accent}",
            ),
            _rule(
                "QTableWidget",
                f"background: {t.surface}",
                f"border: 1px solid {t.line}",
                f"border-radius: {m.radius_card}px",
                f"gridline-color: {t.line_soft}",
                f"alternate-background-color: {t.surface}",
                f"color: {t.ink}",
                f"selection-background-color: {t.clay}",
                f"selection-color: {t.on_accent}",
            ),
            _rule("QTableWidget::item", "padding: 6px"),
            _rule(
                "QTableWidget::item:selected",
                f"background: {t.clay}",
                f"color: {t.on_accent}",
            ),
            _rule(
                "QTableWidget#DownloadTaskTable",
                f"background: {t.field}",
                f"alternate-background-color: {t.surface}",
                f"border: 1px solid {t.line}",
                f"border-radius: {m.radius_card}px",
                f"color: {t.ink}",
                "selection-background-color: transparent",
            ),
            _rule(
                "QTableWidget#DownloadTaskTable::item",
                f"border-bottom: 1px solid {t.line_soft}",
                "padding: 7px 9px",
            ),
            _rule("QHeaderView", f"background: {t.surface}"),
            _rule(
                "QHeaderView::section",
                f"background: {t.surface}",
                f"color: {t.ink_muted}",
                "border: none",
                f"border-bottom: 1px solid {t.line}",
                "padding: 8px 9px",
                f"font-size: {m.font_small}px",
                "font-weight: 600",
            ),
            _rule("QTableCornerButton::section", f"background: {t.surface}", "border: none"),
            _rule(
                "QFrame#PreviewShell",
                f"background: {t.surface}",
                f"border: 1px solid {t.line}",
                f"border-radius: {m.radius_card}px",
            ),
            _rule(
                "QFrame#CoverPlaceholder",
                f"border-radius: {m.radius_card}px",
                "background: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
                f"stop:0 {t.cover_top}, stop:0.55 {t.cover_mid}, stop:1 {t.cover_bottom})",
            ),
            _rule(
                "QFrame#FilterPanel",
                f"background: {t.surface}",
                f"border: 1px solid {t.line_soft}",
                f"border-radius: {m.radius_card}px",
            ),
            _rule(
                "QListView#ChartList",
                "background: transparent",
                "border: none",
                "padding: 0",
            ),
            _rule("QListView#ChartList::item", "border: none", "padding: 0"),
            _rule(
                "QLabel#ChartEmpty",
                f"color: {t.ink_faint}",
                f"font-size: {m.font_body}px",
                "padding: 40px",
            ),
            _rule(
                "QLabel#Toast",
                f"background: {t.surface}",
                f"border: 1px solid {t.clay}",
                f"border-radius: {m.radius_card}px",
                f"padding: {m.space_8 + 2}px {m.space_16}px",
                "font-weight: 600",
            ),
            _rule(
                "QPushButton#ToastAction",
                f"background: {t.accent_soft}",
                f"border: 1px solid {t.clay}",
                f"border-radius: {m.radius_control}px",
                f"color: {t.clay}",
                f"padding: 5px {m.space_12}px",
                "font-weight: 600",
            ),
            _rule(
                "QPushButton#ToastAction:hover",
                f"background: {t.clay}",
                f"color: {t.on_accent}",
            ),
            _rule(
                "QLabel#Chip",
                f"background: {t.accent_soft}",
                f"border: 1px solid {t.line}",
                f"border-radius: {m.radius_control}px",
                "padding: 2px 8px",
                f"color: {t.clay}",
                f"font-size: {m.font_small}px",
            ),
            _rule(
                "QLabel#ChipOk",
                f"background: {t.success_soft}",
                f"border: 1px solid {t.success}",
                f"border-radius: {m.radius_control}px",
                "padding: 2px 8px",
                f"color: {t.success}",
                f"font-size: {m.font_small}px",
            ),
            _rule(
                "QLabel#ChipWarn",
                f"background: {t.warning_soft}",
                f"border: 1px solid {t.warning}",
                f"border-radius: {m.radius_control}px",
                "padding: 2px 8px",
                f"color: {t.warning}",
                f"font-size: {m.font_small}px",
            ),
            _rule(
                "QLabel#ChipBad",
                f"background: {t.danger_soft}",
                f"border: 1px solid {t.danger}",
                f"border-radius: {m.radius_control}px",
                "padding: 2px 8px",
                f"color: {t.danger}",
                f"font-size: {m.font_small}px",
            ),
            _rule(
                "QProgressBar",
                f"background: {t.field}",
                f"border: 1px solid {t.line}",
                "border-radius: 7px",
                "height: 14px",
                "text-align: center",
                f"color: {t.ink}",
                f"font-size: {m.font_small}px",
            ),
            _rule("QProgressBar::chunk", f"background: {t.clay}", "border-radius: 6px"),
            _rule("QDialog#DownloadTasksDialog", f"background: {t.paper_top}"),
        ]
    )


def _states(t: ThemeTokens, m: Metrics) -> str:
    """状态：Toast 级别、语义文字 tone、忙碌态、拖拽态。"""
    return "\n".join(
        [
            _rule(
                'QLabel#Toast[level="error"]',
                f"border-color: {t.danger}",
                f"color: {t.danger}",
            ),
            _rule(
                'QLabel#Toast[level="warn"]',
                f"border-color: {t.warning}",
                f"color: {t.warning}",
            ),
            _rule(
                'QLabel#Toast[level="ok"]',
                f"border-color: {t.success}",
                f"color: {t.success}",
            ),
            _rule('QLabel[tone="ok"]', f"color: {t.success}"),
            _rule('QLabel[tone="warn"]', f"color: {t.warning}"),
            _rule('QLabel[tone="bad"]', f"color: {t.danger}"),
            _rule('QLabel[tone="muted"]', f"color: {t.ink_muted}"),
            _rule('QLabel[tone="faint"]', f"color: {t.ink_faint}"),
            _rule('QPushButton[busy="true"]', f"color: {t.ink_muted}"),
            _rule(
                'QPushButton[busy="true"]:disabled',
                f"color: {t.ink_muted}",
                f"background: {t.surface_hover}",
                f"border-color: {t.line}",
            ),
            _rule('QLabel[busy="true"]', f"color: {t.ink_muted}"),
            _rule(
                'QFrame#DropZone[dragging="true"], QFrame#CoverPicker[dragging="true"]',
                f"border: 1px dashed {t.clay}",
                f"background: {t.accent_soft}",
            ),
        ]
    )


_SECTION_BUILDERS: tuple[tuple[str, Callable[[ThemeTokens, Metrics], str]], ...] = (
    ("foundation", _foundation),
    ("shell", _shell),
    ("controls", _controls),
    ("components", _components),
    ("states", _states),
)


def qss_sections(theme: str) -> list[tuple[str, str]]:
    """返回 ``[(段名, QSS 片段)]``，顺序固定，便于测试与调试。"""
    t = tokens_for(theme)
    return [(name, builder(t, METRICS)) for name, builder in _SECTION_BUILDERS]


def build_qss(theme: str) -> str:
    """把分段 QSS 拼成完整样式表。纯函数：同输入必得同输出。"""
    return "\n".join(body for _name, body in qss_sections(theme))


# ---------------------------------------------------------------------------
# 主题应用
# ---------------------------------------------------------------------------


def build_qpalette(theme: str) -> Any:
    """构造与 QSS 一致的 QPalette，供原生控件与系统对话框使用。"""
    from PySide6.QtGui import QColor, QPalette

    t = tokens_for(theme)
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, QColor(t.paper_top))
    p.setColor(QPalette.ColorRole.WindowText, QColor(t.ink))
    p.setColor(QPalette.ColorRole.Base, QColor(t.field))
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(t.surface))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(t.surface))
    p.setColor(QPalette.ColorRole.ToolTipText, QColor(t.ink))
    p.setColor(QPalette.ColorRole.Text, QColor(t.ink))
    p.setColor(QPalette.ColorRole.Button, QColor(t.surface))
    p.setColor(QPalette.ColorRole.ButtonText, QColor(t.ink))
    p.setColor(QPalette.ColorRole.BrightText, QColor(t.on_accent))
    p.setColor(QPalette.ColorRole.Highlight, QColor(t.clay))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(t.on_accent))
    p.setColor(QPalette.ColorRole.Link, QColor(t.clay))
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(t.ink_faint))
    disabled = QPalette.ColorGroup.Disabled
    p.setColor(disabled, QPalette.ColorRole.Text, QColor(t.ink_faint))
    p.setColor(disabled, QPalette.ColorRole.WindowText, QColor(t.ink_faint))
    p.setColor(disabled, QPalette.ColorRole.ButtonText, QColor(t.ink_faint))
    return p


def _application() -> Any:
    try:
        from PySide6.QtWidgets import QApplication
    except Exception:  # pragma: no cover - 无 Qt 环境下静默降级
        return None
    return QApplication.instance()


def _apply_color_scheme(app: Any, theme: str) -> None:
    """尽量让系统原生控件（标题栏、对话框）跟随主题；旧 Qt 上安静跳过。"""
    try:
        from PySide6.QtCore import Qt
    except Exception:  # pragma: no cover
        return
    hints = app.styleHints() if hasattr(app, "styleHints") else None
    scheme = getattr(Qt, "ColorScheme", None)
    if hints is None or scheme is None or not hasattr(hints, "setColorScheme"):
        return
    try:
        hints.setColorScheme(
            scheme.Light if theme == "light" else scheme.Dark
        )
    except (AttributeError, TypeError, RuntimeError):  # pragma: no cover
        return


def apply_theme(theme: str, app: Any = None, force: bool = False) -> str:
    """应用主题：写入 QPalette、QSS 与系统配色，并在主题变化时广播一次。

    同一主题重复调用默认不再触发 ``theme_bus().changed``；``force=True`` 可用于强制刷新。
    """
    normalized = "light" if theme == "light" else "dark"
    if app is None:
        app = _application()
    previous = active_theme()
    set_active_theme(normalized)
    if app is not None:
        # 顺序：系统配色 → 调色板 → 样式表，避免后两者被平台主题覆盖。
        #
        # 同一份样式表重复安装会触发 QApplication 对**所有现存控件**重新
        # polish，代价随控件总数线性增长。窗口构造时都会调一次 apply_theme，
        # 多开窗口（例如 UI 冒烟测试里连续创建 MainWindow）就变成
        # O(窗口数²)，整套测试会越跑越慢直到看起来挂死；正式程序启动时也会
        # 白做一次全量 polish。内容一致时直接跳过即可，视觉结果不变。
        qss = build_qss(normalized)
        if force or app.styleSheet() != qss:
            _apply_color_scheme(app, normalized)
            app.setPalette(build_qpalette(normalized))
            app.setStyleSheet(qss)
    if force or normalized != previous:
        theme_bus().changed.emit(normalized)
    return normalized


_THEME_BUS: Any = None
_THEME_BUS_CLASS: Any = None


def _theme_bus_class() -> Any:
    global _THEME_BUS_CLASS
    if _THEME_BUS_CLASS is None:
        from PySide6.QtCore import QObject, Signal

        class ThemeBus(QObject):
            changed = Signal(str)

        _THEME_BUS_CLASS = ThemeBus
    return _THEME_BUS_CLASS


def theme_bus() -> Any:
    """全进程唯一的主题广播对象（惰性创建，避免模块导入即依赖 Qt）。"""
    global _THEME_BUS
    if _THEME_BUS is None:
        _THEME_BUS = _theme_bus_class()()
    return _THEME_BUS
