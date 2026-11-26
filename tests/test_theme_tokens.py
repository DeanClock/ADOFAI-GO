"""主题地基（token 层 + QSS + 兼容层）的回归测试。"""

from __future__ import annotations

import os
import re
from dataclasses import FrozenInstanceError
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from adofai_publisher.ui import theme
from adofai_publisher.ui.design_tokens import (
    DARK_TOKENS,
    LIGHT_TOKENS,
    METRICS,
    Metrics,
    ThemeTokens,
)

# 改造前 theme.py 的键集合，逐字对照，不允许增删。
LEGACY_KEYS = {
    "bg",
    "sidebar",
    "surface",
    "card",
    "card_hover",
    "border",
    "border_soft",
    "text",
    "muted",
    "faint",
    "accent",
    "accent_strong",
    "accent_soft",
    "selection_text",
    "success",
    "warning",
    "danger",
    "input",
    "shadow",
}


def _selectors(qss: str) -> list[str]:
    return [match.group(1).strip() for match in re.finditer(r"([^{}]+)\{", qss)]


# ---------------------------------------------------------------------------
# 兼容层：旧调色板与旧 API
# ---------------------------------------------------------------------------


def test_legacy_palette_key_set_is_unchanged():
    assert set(theme.DARK) == LEGACY_KEYS
    assert set(theme.LIGHT) == LEGACY_KEYS
    for entry in (theme.DARK, theme.LIGHT):
        assert all(isinstance(value, str) and value for value in entry.values())


def test_legacy_palette_values_follow_tokens():
    assert theme.DARK["bg"] == DARK_TOKENS.paper_top
    assert theme.DARK["text"] == DARK_TOKENS.ink
    assert theme.DARK["accent"] == DARK_TOKENS.clay
    assert theme.DARK["selection_text"] == DARK_TOKENS.on_accent
    assert theme.LIGHT["surface"] == LIGHT_TOKENS.surface
    assert theme.LIGHT["input"] == LIGHT_TOKENS.field
    assert theme.LIGHT["accent_strong"] == LIGHT_TOKENS.clay_hover


def test_palette_falls_back_to_dark():
    assert theme.palette("light") is theme.LIGHT
    assert theme.palette("dark") is theme.DARK
    assert theme.palette("whatever") is theme.DARK


def test_active_theme_helpers_roundtrip():
    previous = theme.active_theme()
    try:
        theme.set_active_theme("light")
        assert theme.active_theme() == "light"
        assert theme.active_palette() is theme.LIGHT
        assert theme.tokens() is LIGHT_TOKENS
        theme.set_active_theme("nonsense")
        assert theme.active_theme() == "dark"
        assert theme.active_palette() is theme.DARK
    finally:
        theme.set_active_theme(previous)


# ---------------------------------------------------------------------------
# token 层
# ---------------------------------------------------------------------------


def test_tokens_and_metrics_are_frozen_data():
    assert isinstance(LIGHT_TOKENS, ThemeTokens)
    assert isinstance(DARK_TOKENS, ThemeTokens)
    assert isinstance(METRICS, Metrics)
    with pytest.raises(FrozenInstanceError):
        LIGHT_TOKENS.clay = "#000000"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        METRICS.sidebar_width = 1  # type: ignore[misc]


def test_tokens_follow_confirmed_palette():
    assert LIGHT_TOKENS.paper_top == "#FAF7F1"
    assert LIGHT_TOKENS.paper_bottom == "#F1ECE2"
    assert LIGHT_TOKENS.clay == "#B85735"
    assert LIGHT_TOKENS.success == "#3F7D58"
    assert LIGHT_TOKENS.warning == "#A66A1F"
    assert LIGHT_TOKENS.danger == "#B23A32"

    assert DARK_TOKENS.paper_top == "#262624"
    assert DARK_TOKENS.paper_bottom == "#1F1E1D"
    assert DARK_TOKENS.clay == "#D97757"
    assert DARK_TOKENS.success == "#7FBF9A"
    assert DARK_TOKENS.warning == "#E0B36A"
    assert DARK_TOKENS.danger == "#E08A82"


def test_metrics_scale_values():
    m = theme.metrics()
    assert m.sidebar_width == 232
    assert m.nav_item_height == 40
    assert m.radius_sheet == 14
    assert m.radius_card == 12
    assert m.radius_control == 8
    assert m.page_pad == (28, 20)
    assert m.page_pad_x == 28 and m.page_pad_y == 20
    assert m.spacing_scale == (4, 8, 12, 16, 24, 32)
    assert (m.motion_fast, m.motion_base, m.motion_slow) == (120, 160, 220)


def test_tokens_module_has_no_qt_dependency():
    module_path = (
        Path(__file__).resolve().parents[1]
        / "adofai_publisher"
        / "ui"
        / "design_tokens.py"
    )
    source = module_path.read_text(encoding="utf-8")
    assert "PySide6" not in source
    assert "QtCore" not in source and "QtWidgets" not in source


# ---------------------------------------------------------------------------
# QSS 结构
# ---------------------------------------------------------------------------


def test_build_qss_is_pure_and_idempotent():
    assert theme.build_qss("dark") == theme.build_qss("dark")
    assert theme.build_qss("light") == theme.build_qss("light")
    assert theme.build_qss("dark") != theme.build_qss("light")
    # 未知主题回退深色
    assert theme.build_qss("bogus") == theme.build_qss("dark")


def test_qss_sections_order_and_content():
    sections = theme.qss_sections("light")
    assert [name for name, _body in sections] == [
        "foundation",
        "shell",
        "controls",
        "components",
        "states",
    ]
    assert all(body.strip() for _name, body in sections)
    assert "\n".join(body for _name, body in sections) == theme.build_qss("light")


def test_qss_has_no_duplicate_top_level_selector_blocks():
    for name in ("light", "dark"):
        selectors = _selectors(theme.build_qss(name))
        duplicates = {item for item in selectors if selectors.count(item) > 1}
        assert duplicates == set(), f"{name} 主题存在重复选择器：{duplicates}"


def test_qss_contains_required_markers():
    qss = theme.build_qss("light")
    for marker in (
        "QWidget#AppRoot",
        "QWidget#SideBar",
        "QPushButton#NavButton",
        'QLabel[tone="ok"]',
        ':pressed',
        ':focus',
        '[busy="true"]',
        'QLabel#Toast[level="ok"]',
    ):
        assert marker in qss, marker


def test_app_root_uses_warm_gradient():
    for name, top in (("light", LIGHT_TOKENS.paper_top), ("dark", DARK_TOKENS.paper_top)):
        qss = theme.build_qss(name)
        block = qss.split("QWidget#AppRoot {", 1)[1].split("}", 1)[0]
        assert "qlineargradient" in block
        assert top in block


def test_titles_use_bold_title_family():
    """DC 反馈标题字太弱：标题统一走微软雅黑 Bold，不再用衬线。"""
    from adofai_publisher.ui.design_tokens import TITLE_FAMILY

    assert "Microsoft YaHei" in TITLE_FAMILY
    for name in ("light", "dark"):
        qss = theme.build_qss(name)
        for selector in (
            "QLabel#PageTitle {",
            "QLabel#CardTitle {",
            "QLabel#HomeGreeting {",
        ):
            block = qss.split(selector, 1)[1].split("}", 1)[0]
            assert f"font-family: {TITLE_FAMILY}" in block
            assert f"font-weight: {METRICS.title_weight}" in block
            assert "Georgia" not in block


def test_qss_declarations_end_with_semicolon():
    """漏分号会让整张样式表被 Qt 丢弃，这里逐行兜底。"""
    for name in ("light", "dark"):
        for body in re.findall(r"\{([^{}]*)\}", theme.build_qss(name)):
            for line in body.splitlines():
                stripped = line.strip()
                if stripped:
                    assert stripped.endswith(";"), f"{name}: {stripped}"


def test_qss_has_no_bare_outline_none():
    for name in ("light", "dark"):
        assert "outline: none" not in theme.build_qss(name)


def test_light_qss_has_no_legacy_dark_hardcodes():
    qss = theme.build_qss("light")
    for legacy in (
        "#07101F",
        "#7DD3FC",
        "#38BDF8",
        "#0B1120",
        "#0A0F1C",
        "#34D399",
        "#FBBF24",
        "#F87171",
    ):
        assert legacy not in qss, legacy


def test_only_one_tooltip_definition():
    selectors = _selectors(theme.build_qss("dark"))
    assert selectors.count("QToolTip") == 1


# ---------------------------------------------------------------------------
# QPalette 与主题应用
# ---------------------------------------------------------------------------


def _offscreen_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_build_qpalette_follows_tokens():
    from PySide6.QtGui import QPalette

    palette = theme.build_qpalette("light")
    assert palette.color(QPalette.ColorRole.Window).name().lower() == LIGHT_TOKENS.paper_top.lower()
    assert palette.color(QPalette.ColorRole.Text).name().lower() == LIGHT_TOKENS.ink.lower()
    assert palette.color(QPalette.ColorRole.Highlight).name().lower() == LIGHT_TOKENS.clay.lower()

    dark = theme.build_qpalette("dark")
    assert dark.color(QPalette.ColorRole.Window).name().lower() == DARK_TOKENS.paper_top.lower()


def test_apply_theme_sets_stylesheet_and_emits_once():
    _offscreen_app()
    bus = theme.theme_bus()
    received: list[str] = []
    slot = received.append
    bus.changed.connect(slot)
    theme.set_active_theme("dark")
    try:
        theme.apply_theme("light")
        theme.apply_theme("light")
        assert received == ["light"]
        assert theme.active_theme() == "light"

        theme.apply_theme("dark")
        assert received == ["light", "dark"]

        theme.apply_theme("dark", force=True)
        assert received == ["light", "dark", "dark"]
    finally:
        bus.changed.disconnect(slot)
        theme.apply_theme("dark", force=True)


def test_apply_theme_writes_stylesheet_to_application():
    app = _offscreen_app()
    try:
        theme.apply_theme("light", app, force=True)
        assert app.styleSheet() == theme.build_qss("light")
    finally:
        theme.apply_theme("dark", app, force=True)


def test_theme_bus_is_singleton_and_carries_theme_name():
    bus = theme.theme_bus()
    assert bus is theme.theme_bus()
    received: list[str] = []
    slot = received.append
    bus.changed.connect(slot)
    try:
        bus.changed.emit("dark")
    finally:
        bus.changed.disconnect(slot)
    assert received == ["dark"]
