from adofai_publisher.browser import clean_browser_error, friendly_browser_error


def test_browser_error_hides_launch_command():
    message = (
        "无法启动浏览器：msedge: BrowserType.launch_persistent_context: "
        "Target page, context or browser has been closed\n"
        "Browser logs:\n\n"
        "<launching> C:\\Program Files\\Microsoft\\Edge\\msedge.exe --headless "
        "--user-data-dir=C:\\Users\\demo\\AppData\\Roaming\\AdofaiTufBridge\\browser_profile\n"
        "<launched> pid=1234\n"
        "Call log:\n"
        "  - <launching> C:\\Program Files\\Microsoft\\Edge\\msedge.exe --headless\n"
    )
    friendly = friendly_browser_error(message)
    assert "工具浏览器窗口仍在运行" in friendly
    assert "<launching>" not in friendly
    assert "msedge.exe" not in friendly

    details = clean_browser_error(message)
    assert "<launching>" not in details
    assert "Target page" in details


def test_browser_error_reports_missing_browser():
    message = "Executable doesn't exist at C:\\Users\\demo\\ms-playwright\\chromium\\chrome.exe"
    friendly = friendly_browser_error(message)
    assert "没有找到可用的浏览器" in friendly


class FakeSettings:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key):
        return self.values.get(key)


def test_login_failure_texts_cover_four_categories():
    from adofai_publisher.browser import (
        LOGIN_FAILURE_CLOSED,
        LOGIN_FAILURE_LAUNCH,
        LOGIN_FAILURE_NOT_LOGGED_IN,
        LOGIN_FAILURE_TIMEOUT,
        login_failure_text,
    )

    texts = {
        kind: login_failure_text(kind)
        for kind in (
            LOGIN_FAILURE_LAUNCH,
            LOGIN_FAILURE_CLOSED,
            LOGIN_FAILURE_TIMEOUT,
            LOGIN_FAILURE_NOT_LOGGED_IN,
        )
    }
    assert all(text.strip() for text in texts.values())
    assert len(set(texts.values())) == 4, "四类失败必须各有各的说法"
    assert "浏览器没能启动" in texts[LOGIN_FAILURE_LAUNCH]
    assert "别关窗口" in texts[LOGIN_FAILURE_CLOSED]
    assert "超时" in texts[LOGIN_FAILURE_TIMEOUT]
    assert "没有检测到登录" in texts[LOGIN_FAILURE_NOT_LOGGED_IN]
    # 未知类别不能抛错，按「浏览器起不来」兜底。
    assert login_failure_text("unknown") == texts[LOGIN_FAILURE_LAUNCH]


def test_build_diagnostics_contains_environment_and_redacts(tmp_path):
    from adofai_publisher.browser import build_diagnostics

    settings = FakeSettings({"browser_channel": "msedge"})
    text = build_diagnostics(
        settings,
        app_version="0.16.0",
        platform_text="Windows-11-10.0.26200",
        profile_path=tmp_path,
        last_error="登录失败 SESSDATA=cookie-value-123 token=abc",
    )

    assert "v0.16.0" in text
    assert "Windows-11-10.0.26200" in text
    assert "Edge → Chrome → 内置 Chromium" in text
    assert "存在" in text
    assert str(tmp_path) in text
    assert "cookie-value-123" not in text
    assert "abc" not in text.split("token")[0][-10:]
    assert "<已脱敏>" in text
    assert "SESSDATA=<已脱敏>" in text


def test_build_diagnostics_reports_missing_profile_and_no_error(tmp_path):
    from adofai_publisher.browser import build_diagnostics

    missing = tmp_path / "nope" / "profile"
    text = build_diagnostics(
        FakeSettings(),
        app_version="0.16.0",
        platform_text="Windows",
        profile_path=missing,
    )
    assert "不存在" in text
    assert "最近一次登录错误：无" in text
