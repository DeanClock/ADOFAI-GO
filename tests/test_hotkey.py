from __future__ import annotations

import pytest

from adofai_publisher.hotkey import (
    HotkeyMatcher,
    display_key_name,
    normalize_key_name,
    parse_hotkey,
    qt_key_name,
    vk_key_name,
)


def test_normalize_and_display_common_keys():
    assert normalize_key_name("shift") == "Shift"
    assert normalize_key_name("control") == "Ctrl"
    assert normalize_key_name("escape") == "Esc"
    assert normalize_key_name("comma") == "Comma"
    assert display_key_name("Comma") == ","
    assert vk_key_name(0xA0) == "Shift"
    assert vk_key_name(0x47) == "G"
    assert qt_key_name(int(0x41), "a") == "A"


def test_parse_double_tap_and_combo():
    double = parse_hotkey("shift+shift")
    assert double.display == "Shift+Shift"
    assert double.double_key == "Shift"
    assert double.keys == frozenset()

    combo = parse_hotkey("shift+ctrl+g")
    assert combo.display == "Ctrl+Shift+G"
    assert combo.keys == frozenset({"Ctrl", "Shift", "G"})
    assert combo.double_key is None


def test_parse_rejects_single_modifier_and_unknown_key():
    with pytest.raises(ValueError, match="单个修饰键"):
        parse_hotkey("Shift")
    with pytest.raises(ValueError, match="无法识别"):
        parse_hotkey("Ctrl+Nope")


def test_double_tap_matcher():
    matcher = HotkeyMatcher(parse_hotkey("Shift+Shift"))
    assert matcher.key_down("Shift", 1.0) is False
    matcher.key_up("Shift")
    assert matcher.key_down("Shift", 1.2) is True
    matcher.key_up("Shift")
    assert matcher.key_down("Shift", 1.8) is False
    assert matcher.key_down("Shift", 3.0) is False


def test_combo_matcher_fires_once_until_release():
    matcher = HotkeyMatcher(parse_hotkey("Ctrl+Shift+G"))
    assert matcher.key_down("Ctrl") is False
    assert matcher.key_down("Shift") is False
    assert matcher.key_down("G") is True
    assert matcher.key_down("G") is False
    matcher.key_up("G")
    assert matcher.key_down("G") is True


def test_single_key_matcher():
    matcher = HotkeyMatcher(parse_hotkey("F8"))
    assert matcher.key_down("F8") is True
    assert matcher.key_down("F8") is False
    matcher.key_up("F8")
    assert matcher.key_down("F8") is True


def test_double_tap_resets_when_other_key_pressed():
    matcher = HotkeyMatcher(parse_hotkey("Shift+Shift"))
    assert matcher.key_down("Shift", 1.0) is False
    matcher.key_up("Shift")
    assert matcher.key_down("A", 1.1) is False
    matcher.key_up("A")
    assert matcher.key_down("Shift", 1.2) is False
