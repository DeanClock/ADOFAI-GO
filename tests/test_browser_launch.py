import logging

import pytest

from adofai_publisher import browser


class FakeSettings:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key):
        return self.values.get(key)


class FakeChromium:
    def __init__(self, reject_no_viewport: bool = False, fail_channels=()):
        self.calls = []
        self.reject_no_viewport = reject_no_viewport
        self.fail_channels = set(fail_channels)

    def launch_persistent_context(self, **kwargs):
        self.calls.append(kwargs)
        if self.reject_no_viewport and "no_viewport" in kwargs:
            raise TypeError(
                "BrowserType.launch_persistent_context() got an unexpected "
                "keyword argument 'no_viewport'"
            )
        if kwargs.get("channel") in self.fail_channels:
            raise RuntimeError(f"channel {kwargs.get('channel')} 启动失败")
        return {"context": True}


class FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium


def _settings(tmp_path):
    return FakeSettings({"browser_profile_dir": str(tmp_path / "profile")})


def test_visible_mode_uses_window_viewport(tmp_path):
    chromium = FakeChromium()

    browser.launch_context(FakePlaywright(chromium), _settings(tmp_path), headless=False)

    assert len(chromium.calls) == 1
    kwargs = chromium.calls[0]
    assert kwargs["no_viewport"] is True
    assert "viewport" not in kwargs
    assert kwargs["headless"] is False
    assert kwargs["channel"] == "chrome"
    assert "--start-maximized" in kwargs["args"]
    assert "--window-position=0,0" in kwargs["args"]


def test_headless_mode_keeps_fixed_viewport(tmp_path):
    chromium = FakeChromium()

    browser.launch_context(FakePlaywright(chromium), _settings(tmp_path), headless=True)

    assert len(chromium.calls) == 1
    kwargs = chromium.calls[0]
    assert "no_viewport" not in kwargs
    assert kwargs["viewport"] == {"width": 1440, "height": 900}
    assert kwargs["headless"] is True
    assert "--window-position=0,0" not in kwargs["args"]


def test_visible_mode_falls_back_when_no_viewport_unsupported(tmp_path, caplog):
    chromium = FakeChromium(reject_no_viewport=True)

    with caplog.at_level(logging.WARNING, logger=browser.__name__):
        result = browser.launch_context(
            FakePlaywright(chromium), _settings(tmp_path), headless=False
        )

    assert result == {"context": True}
    assert len(chromium.calls) == 2
    assert chromium.calls[0]["no_viewport"] is True
    fallback = chromium.calls[1]
    assert "no_viewport" not in fallback
    assert fallback["viewport"] == {"width": 1440, "height": 900}
    assert fallback["headless"] is False
    assert "--start-maximized" in fallback["args"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "no_viewport" in warnings[0].getMessage()


def test_fallback_keeps_channel_retry_behaviour(tmp_path):
    chromium = FakeChromium(reject_no_viewport=True, fail_channels={"chrome", "msedge", None})

    with pytest.raises(RuntimeError) as excinfo:
        browser.launch_context(FakePlaywright(chromium), _settings(tmp_path), headless=False)

    # chrome 两种写法都失败后继续尝试下一个渠道，回退只发生一次
    assert [call.get("channel") for call in chromium.calls] == ["chrome", "chrome", "msedge", None]
    assert "msedge" in str(excinfo.value)
