from __future__ import annotations

import logging
import platform
import re
from pathlib import Path
from typing import Any

from .config import Settings, browser_profile_dir
from .levellookup import cookies_to_dict

logger = logging.getLogger(__name__)

BILI_UPLOAD_URL = "https://member.bilibili.com/platform/upload/video/frame"
BILI_MANAGER_URL = "https://member.bilibili.com/platform/upload-manager/article"
BILI_HOME_URL = "https://www.bilibili.com/"
TUF_LOGIN_URL = "https://tuforums.com/login"
TUF_HOME_URL = "https://tuforums.com/"
TUF_API_URL = "https://api.tuforums.com/"

USER_DATA_DIR = "browser_profile_dir"

# Playwright 里空字符串 channel 表示用它自带的 Chromium。
BUNDLED_CHANNEL_LABEL = "bundled"
CHANNEL_DISPLAY_NAMES = {
    "chrome": "Chrome",
    "msedge": "Edge",
    BUNDLED_CHANNEL_LABEL: "内置 Chromium",
}

# 登录失败的四个类别：弹窗文案按类别各给一句可操作的话。
LOGIN_FAILURE_LAUNCH = "launch"
LOGIN_FAILURE_CLOSED = "closed"
LOGIN_FAILURE_TIMEOUT = "timeout"
LOGIN_FAILURE_NOT_LOGGED_IN = "not_logged_in"

_LOGIN_FAILURE_TEXTS = {
    LOGIN_FAILURE_LAUNCH: (
        "浏览器没能启动，登录窗口打不开。\n"
        "请确认电脑上装了 Chrome 或 Edge，或在设置页换一个浏览器再试；"
        "仍然失败就点设置页的「复制诊断信息」发给作者。"
    ),
    LOGIN_FAILURE_CLOSED: (
        "登录窗口被关掉了，这次登录已取消。\n"
        "需要重新点「打开浏览器登录双站」，登录完成之前别关窗口。"
    ),
    LOGIN_FAILURE_TIMEOUT: (
        "等待登录超时（10 分钟）。\n"
        "重新点「打开浏览器登录双站」，把 B 站和 TUF 都登录完，窗口放着别动，工具会自动继续。"
    ),
    LOGIN_FAILURE_NOT_LOGGED_IN: (
        "浏览器还开着，但 B 站和 TUF 都没有检测到登录。\n"
        "请在窗口里登录 B 站（右上角「登录」）和 TUF（Sign in），工具会自动继续；"
        "登录后仍不行，就在设置页点「复制诊断信息」发给作者。"
    ),
}


def login_failure_text(kind: str) -> str:
    """登录失败的八股文案按类别取；未知类别按「浏览器起不来」处理。"""

    return _LOGIN_FAILURE_TEXTS.get(kind) or _LOGIN_FAILURE_TEXTS[LOGIN_FAILURE_LAUNCH]


def candidate_labels(settings: Any) -> list[str]:
    """浏览器候选顺序：设置里的渠道 → chrome → msedge → 内置 Chromium，去重。"""

    channel = (settings.get("browser_channel") or "chrome").strip()
    labels: list[str] = []
    for candidate in (channel, "chrome", "msedge", ""):
        label = candidate or BUNDLED_CHANNEL_LABEL
        if label not in labels:
            labels.append(label)
    return labels


def channel_display(label: str) -> str:
    return CHANNEL_DISPLAY_NAMES.get(label, label or BUNDLED_CHANNEL_LABEL)


class BrowserLaunchError(RuntimeError):
    """所有候选浏览器都起不来；带上候选顺序与每个候选的失败原因。"""

    def __init__(
        self,
        message: str,
        candidates: list[str],
        errors: dict[str, str],
    ) -> None:
        super().__init__(message)
        self.candidates = list(candidates)
        self.errors = dict(errors)


# 最近一次启动的结果，登录诊断日志和「复制诊断信息」都读它。
_last_launch: dict[str, Any] = {"candidates": [], "used": "", "errors": {}}


def last_launch_info() -> dict[str, Any]:
    return {
        "candidates": list(_last_launch.get("candidates") or []),
        "used": str(_last_launch.get("used") or ""),
        "errors": dict(_last_launch.get("errors") or {}),
    }


def _record_launch(
    candidates: list[str], used: str, errors: dict[str, str]
) -> None:
    _last_launch["candidates"] = list(candidates)
    _last_launch["used"] = used
    _last_launch["errors"] = dict(errors)


def describe_launch(info: dict[str, Any] | None = None) -> str:
    """一行说明候选顺序、实际使用与各候选失败原因，供 app.log 用。"""

    data = info or last_launch_info()
    candidates = data.get("candidates") or []
    order = " → ".join(channel_display(item) for item in candidates) or "未知"
    used = channel_display(str(data.get("used") or "")) if data.get("used") else "尚未成功启动"
    errors = data.get("errors") or {}
    reasons = "；".join(
        f"{channel_display(label)}：{redact_secrets(str(reason))}"
        for label, reason in errors.items()
    )
    return f"浏览器候选 {order}；实际使用 {used}；各候选失败原因 {reasons or '无'}"


def build_diagnostics(
    settings: Any,
    *,
    app_version: str = "",
    platform_text: str = "",
    profile_path: str | Path | None = None,
    last_error: str = "",
) -> str:
    """生成给测试者回传的诊断文本；整段都会过 :func:`redact_secrets`。

    只写版本、系统、浏览器候选、profile 目录是否存在和错误文本，
    不读 Cookie，也不会把登录态写进这段文本。
    """

    raw_profile = profile_path or settings.get("browser_profile_dir") or browser_profile_dir()
    profile = Path(str(raw_profile))
    try:
        profile_exists = profile.exists()
    except OSError:
        profile_exists = False
    labels = candidate_labels(settings)
    lines = [
        "ADOFAI GO 登录诊断",
        f"版本：v{app_version}" if app_version else "版本：未知",
        f"系统：{platform_text or platform.platform()}",
        "浏览器候选顺序：" + " → ".join(channel_display(item) for item in labels),
        f"浏览器 profile：{profile}（{'存在' if profile_exists else '不存在'}）",
        f"最近一次浏览器启动：{describe_launch()}",
    ]
    error_text = _strip_launch_noise(last_error) if last_error else ""
    if not error_text.strip():
        lines.append("最近一次登录错误：无")
    else:
        lines.append("最近一次登录错误：")
        lines.extend("  " + line for line in error_text.splitlines()[:20])
    return redact_secrets("\n".join(lines))


FALLBACK_VIEWPORT = {"width": 1440, "height": 900}
VISIBLE_ARGS = ["--start-maximized", "--window-position=0,0"]
HEADLESS_ARGS = ["--start-maximized"]


def _launch_kwargs(profile: str, channel: str, headless: bool, no_viewport: bool) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "user_data_dir": profile,
        "headless": headless,
        "args": list(HEADLESS_ARGS if headless else VISIBLE_ARGS),
    }
    if headless or not no_viewport:
        # 无头模式保持固定视口，自检/登录检查的渲染行为不变；
        # 可见模式下只有在当前 Playwright 不支持 no_viewport 时才回退到这里。
        kwargs["viewport"] = dict(FALLBACK_VIEWPORT)
    else:
        # 视口跟随真实窗口尺寸：窗口最大化后页面铺满，手动最大化/还原都不会留白。
        kwargs["no_viewport"] = True
    if channel:
        kwargs["channel"] = channel
    return kwargs


def launch_context(playwright: Any, settings: Settings, headless: bool = False) -> Any:
    profile = settings.get("browser_profile_dir") or str(browser_profile_dir())
    Path(profile).mkdir(parents=True, exist_ok=True)
    use_no_viewport = not headless
    labels = candidate_labels(settings)
    errors: dict[str, str] = {}
    tried: set[str] = set()
    for candidate in (labels[0], "chrome", "msedge", ""):
        label = candidate or BUNDLED_CHANNEL_LABEL
        if label in tried:
            continue
        tried.add(label)
        while True:
            kwargs = _launch_kwargs(profile, candidate, headless, use_no_viewport)
            try:
                context = playwright.chromium.launch_persistent_context(**kwargs)
            except TypeError as exc:
                if use_no_viewport and "no_viewport" in str(exc):
                    logger.warning(
                        "当前 Playwright 不支持 no_viewport，回退到固定视口 %sx%s：%s",
                        FALLBACK_VIEWPORT["width"],
                        FALLBACK_VIEWPORT["height"],
                        exc,
                    )
                    use_no_viewport = False
                    continue
                errors[label] = str(exc)
                break
            except Exception as exc:
                errors[label] = str(exc)
                break
            _record_launch(labels, label, errors)
            return context
    _record_launch(labels, "", errors)
    message = "无法启动浏览器：" + " | ".join(
        f"{label}: {reason}" for label, reason in errors.items()
    )
    raise BrowserLaunchError(message, labels, errors)


def context_cookies(context: Any, url: str) -> list[dict[str, Any]]:
    try:
        return context.cookies(url)
    except Exception:
        return []


def has_bili_login(context: Any) -> bool:
    names = {item.get("name") for item in context_cookies(context, BILI_HOME_URL)}
    return "SESSDATA" in names


def tuf_cookies(context: Any) -> dict[str, str]:
    cookies: dict[str, str] = {}
    cookies.update(cookies_to_dict(context_cookies(context, TUF_HOME_URL)))
    cookies.update(cookies_to_dict(context_cookies(context, TUF_API_URL)))
    return cookies


def is_context_closed(context: Any) -> bool:
    try:
        return not context.pages
    except Exception:
        return True


def extract_bvid(text: str) -> str:
    match = re.search(r"BV[0-9A-Za-z]{10}", text or "")
    return match.group(0) if match else ""


def fill_first(page: Any, selectors: list[str], value: str, timeout: int = 4000) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout)
            locator.fill(value)
            return True
        except Exception:
            continue
    return False


def click_first(page: Any, selectors: list[str], timeout: int = 3000) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout)
            locator.click()
            return True
        except Exception:
            continue
    return False


def page_text(page: Any) -> str:
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


_SECRET_PATTERNS = (
    (re.compile(r"(?i)(\b(?:cookie|set-cookie)\s*:\s*)[^\r\n]+"), r"\1<已脱敏>"),
    (re.compile(r"(?i)(\bauthorization\s*:\s*)(?:bearer\s+)?[^\r\n]+"), r"\1<已脱敏>"),
    (
        re.compile(
            r"(?i)\b((?:access|refresh|csrf|session|device|trusted)[_-]?token"
            r"|SESSDATA|bili_jct|DedeUserID|sid)\b\s*[=:]\s*[^\s;,})'\"\]]+"
        ),
        r"\1=<已脱敏>",
    ),
    (
        re.compile(
            r"(?i)\b(token|password|passwd|secret)\b\s*[=:]\s*[^\s;,})'\"\]]+"
        ),
        r"\1=<已脱敏>",
    ),
)


def redact_secrets(text: str) -> str:
    """把日志文本里的 cookie / token 打码，避免登录态被写进 app.log。"""

    result = text or ""
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def _strip_launch_noise(message: str) -> str:
    lines: list[str] = []
    for line in (message or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("<launching>", "- <launching>", "<launched>", "- <launched>")):
            continue
        if stripped.startswith("Call log:"):
            continue
        if stripped.startswith("- [pid="):
            continue
        if stripped.startswith("- starting temporary") or stripped.startswith("- finished temporary"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def clean_browser_error(message: str) -> str:
    text = _strip_launch_noise(message)
    if len(text) > 4000:
        text = text[:4000].rstrip() + "\n…（完整内容见日志）"
    return text or "浏览器启动失败，请查看日志。"


def friendly_browser_error(message: str) -> str:
    text = _strip_launch_noise(message)
    lowered = text.lower()
    if (
        "target page, context or browser has been closed" in lowered
        or "processsingleton" in lowered
        or "singletonlock" in lowered
        or "already in use" in lowered
    ):
        return (
            "工具浏览器窗口仍在运行，或上次浏览器没有正常关闭。\n"
            "请先关闭工具打开的 Edge/Chrome 窗口，再点“检查登录”。"
        )
    if "executable doesn't exist" in lowered:
        return (
            "没有找到可用的浏览器。请在设置里选择已安装的 Chrome/Edge，"
            "或安装 Playwright 内置 Chromium。"
        )
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith(("Browser logs:", "无法启动浏览器："))
    ]
    return (lines[0] if lines else "浏览器启动失败，请查看日志。")[:300]
