"""主窗口位置保存 / 恢复的测试。

重点是测试者那台「显示器 1 在上、显示器 2 在下」的纵向布局：窗口贴着下面
那块屏的底边关闭后，重启必须仍然出现在下面那块屏里；保存的位置彻底跑到屏
幕外时，要安全地回落到主屏居中。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ADOFAI_NO_AUTOLOGIN", "1")

import pytest

from adofai_publisher.config import Settings
from adofai_publisher.ui.window_placement import (
    DEFAULT_WINDOW_SIZE,
    MIN_WINDOW_SIZE,
    Rect,
    rect_from_value,
    resolve_window_rect,
    title_bar_visible,
)

# 测试者的真实布局：显示器 1 在上（y 为负数），显示器 2 在下，主屏是上面的 1。
TOP_SCREEN = Rect(0, -1080, 1920, 1080)
BOTTOM_SCREEN = Rect(0, 0, 1920, 1080)


def _assert_inside(rect: Rect, screen: Rect) -> None:
    assert screen.contains(rect), f"{rect} 没有完全落在 {screen} 里"


def test_bottom_edge_of_lower_screen_restores_on_same_screen():
    # 贴着下面那块屏的底边关闭：窗口下半截已经出屏，标题栏还留在屏 2 底部。
    saved = Rect(200, 1020, 1280, 860)

    placement = resolve_window_rect(saved, [TOP_SCREEN, BOTTOM_SCREEN], primary_index=0)

    assert placement.fell_back is False
    _assert_inside(placement.rect, BOTTOM_SCREEN)
    assert placement.rect.x == 200
    assert placement.rect.y == 1080 - 860
    assert (placement.rect.width, placement.rect.height) == (1280, 860)


def test_saved_rect_above_upper_screen_falls_back_to_primary_center():
    # 完全落在屏 1 上方（也就是屏幕外），不能恢复。
    saved = Rect(300, -4000, 1280, 860)

    placement = resolve_window_rect(saved, [TOP_SCREEN, BOTTOM_SCREEN], primary_index=0)

    assert placement.fell_back is True
    _assert_inside(placement.rect, TOP_SCREEN)
    assert placement.rect.width == 1280
    assert placement.rect.height == 860
    assert placement.rect.x == (1920 - 1280) // 2
    assert placement.rect.y == -1080 + (1080 - 860) // 2


def test_only_a_thin_sliver_of_title_bar_visible_falls_back():
    # 只在屏幕左边缘露出 80 像素宽的标题栏，抓不住，按屏幕外处理。
    saved = Rect(-1200, 300, 1280, 860)

    placement = resolve_window_rect(saved, [TOP_SCREEN, BOTTOM_SCREEN], primary_index=0)

    assert placement.fell_back is True
    _assert_inside(placement.rect, TOP_SCREEN)


def test_disconnected_screen_falls_back_to_primary():
    # 保存时在第二块屏上，重启时只剩一块屏。
    only_screen = Rect(0, 0, 1920, 1080)
    saved = Rect(2200, 200, 1280, 860)

    placement = resolve_window_rect(saved, [only_screen], primary_index=0)

    assert placement.fell_back is True
    _assert_inside(placement.rect, only_screen)
    assert (placement.rect.width, placement.rect.height) == (1280, 860)


def test_saved_rect_larger_than_screen_is_shrunk():
    saved = Rect(-100, 100, 3000, 2000)

    placement = resolve_window_rect(saved, [BOTTOM_SCREEN], primary_index=0)

    assert placement.fell_back is False
    assert (placement.rect.width, placement.rect.height) == (1920, 1080)
    assert (placement.rect.x, placement.rect.y) == (0, 0)
    _assert_inside(placement.rect, BOTTOM_SCREEN)
    assert placement.minimum_size == MIN_WINDOW_SIZE


def test_minimum_size_is_relaxed_when_screen_is_smaller():
    tiny_screen = Rect(0, 0, 800, 600)
    saved = Rect(0, 0, 1280, 860)

    placement = resolve_window_rect(
        saved, [tiny_screen], primary_index=0, min_size=MIN_WINDOW_SIZE
    )

    assert (placement.rect.width, placement.rect.height) == (800, 600)
    assert placement.minimum_size == (800, 600)
    _assert_inside(placement.rect, tiny_screen)


def test_fallback_default_size_is_also_clamped_to_tiny_screen():
    tiny_screen = Rect(0, 0, 800, 600)

    placement = resolve_window_rect(
        None, [tiny_screen], primary_index=0, min_size=MIN_WINDOW_SIZE
    )

    assert placement.fell_back is True
    assert (placement.rect.width, placement.rect.height) == (800, 600)
    assert placement.minimum_size == (800, 600)
    _assert_inside(placement.rect, tiny_screen)


def test_title_bar_visible_needs_enough_of_the_top_strip():
    screens = [TOP_SCREEN, BOTTOM_SCREEN]

    # 标题栏整个落在屏 2 底部 60 像素内。
    assert title_bar_visible(Rect(200, 1020, 1280, 860), screens)
    # 只剩 40 像素可见，不够用户抓标题栏。
    assert not title_bar_visible(Rect(200, 1040, 1280, 860), screens)
    # 只在左边缘露出 80 像素而已。
    assert not title_bar_visible(Rect(-1200, 300, 1280, 860), screens)
    # 屏幕外。
    assert not title_bar_visible(Rect(300, -4000, 1280, 860), screens)


def test_first_run_centers_default_size_on_primary():
    placement = resolve_window_rect(None, [BOTTOM_SCREEN, TOP_SCREEN], primary_index=1)

    assert placement.fell_back is True
    assert (placement.rect.width, placement.rect.height) == DEFAULT_WINDOW_SIZE
    _assert_inside(placement.rect, TOP_SCREEN)
    assert placement.rect.x == (1920 - DEFAULT_WINDOW_SIZE[0]) // 2


@pytest.mark.parametrize(
    "value",
    [None, [], [1, 2, 3], [1, 2, 3, 4, 5], "0,0,1280,860", {"x": 1, "y": 2}, [0, 0, 0, 0]],
)
def test_invalid_saved_rect_is_ignored(value):
    assert rect_from_value(value) is None


def test_saved_rect_accepts_list_and_dict():
    assert rect_from_value([10, 20, 1280, 860]) == Rect(10, 20, 1280, 860)
    assert rect_from_value([10.7, 20.2, 1280.9, 860.1]) == Rect(10, 20, 1280, 860)
    assert rect_from_value(
        {"x": 10, "y": 20, "width": 1280, "height": 860}
    ) == Rect(10, 20, 1280, 860)


def test_window_state_round_trips_through_settings(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(path)
    assert settings.get("window_rect") == []
    assert settings.get("window_maximized") is False

    settings.set("window_rect", [200, 220, 1280, 860])
    settings.set("window_maximized", True)
    settings.save()

    loaded = Settings(path)
    assert loaded.get("window_rect") == [200, 220, 1280, 860]
    assert loaded.get("window_maximized") is True
    assert rect_from_value(loaded.get("window_rect")) == Rect(200, 220, 1280, 860)


def test_legacy_settings_without_window_keys_keep_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"theme": "light"}', encoding="utf-8")

    settings = Settings(path)

    assert settings.get("theme") == "light"
    assert settings.get("window_rect") == []
    assert settings.get("window_maximized") is False


def test_main_window_restores_saved_rect_and_saves_state_on_close(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from adofai_publisher.ui.main_window import MainWindow

    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    settings = Settings(tmp_path / "settings.json")
    settings.set("window_rect", [200, 220, 1280, 860])
    settings.save()

    window = MainWindow(settings)
    try:
        # 真实机器只有一块屏，这里换成测试者的纵向布局。
        monkeypatch.setattr(window, "_screen_rects", lambda: ([TOP_SCREEN, BOTTOM_SCREEN], 0))
        window._restore_window_geometry()
        geometry = window.geometry()
        assert (geometry.x(), geometry.y()) == (200, 220)
        assert (geometry.width(), geometry.height()) == (1280, 860)

        window._save_window_state()
        saved = Settings(tmp_path / "settings.json")
        assert saved.get("window_rect") == [200, 220, 1280, 860]
        assert saved.get("window_maximized") is False
    finally:
        window.close()
        app.processEvents()
