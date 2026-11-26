import logging

from adofai_publisher.browser import (
    LOGIN_FAILURE_CLOSED,
    LOGIN_FAILURE_LAUNCH,
    LOGIN_FAILURE_NOT_LOGGED_IN,
    LOGIN_FAILURE_TIMEOUT,
    login_failure_text,
)
from adofai_publisher.workers import LoginWorker

SESSDATA_COOKIE = {"name": "SESSDATA", "value": "cookie-value-123", "domain": ".bilibili.com"}


class FakePage:
    def __init__(self, context):
        self.context = context

    def goto(self, *_args, **_kwargs):
        self.context.after_goto()
        return None


class FakeContext:
    def __init__(self, cookies=None):
        self.closed = False
        self.page = FakePage(self)
        self.cookie_items = list(cookies or [])

    @property
    def pages(self):
        return [] if self.closed else [self.page]

    def new_page(self):
        return self.page

    def cookies(self, _url):
        return list(self.cookie_items)

    def after_goto(self):
        pass

    def close(self):
        self.closed = True


class ClosingContext(FakeContext):
    def __init__(self):
        super().__init__()
        self.goto_count = 0

    def after_goto(self):
        self.goto_count += 1
        if self.goto_count >= 2:
            self.closed = True


class PollThenCloseContext(FakeContext):
    """第一轮轮询读完就关窗，让循环只落一轮诊断日志就结束。"""

    def cookies(self, url):
        items = super().cookies(url)
        self.closed = True
        return items


class FakePlaywright:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _worker(monkeypatch, tmp_path, context, **timeouts):
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: FakePlaywright(),
    )
    monkeypatch.setattr(
        "adofai_publisher.workers.launch_context",
        lambda *_args, **_kwargs: context,
    )

    from adofai_publisher.config import Settings

    worker = LoginWorker(Settings(path=tmp_path / "settings.json"))
    for name, value in timeouts.items():
        setattr(worker, name, value)
    failures: list[str] = []
    worker.failed.connect(failures.append)
    return worker, failures


def _messages(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records]


def test_login_worker_stops_when_browser_window_is_closed(monkeypatch, tmp_path):
    context = ClosingContext()
    worker, failures = _worker(monkeypatch, tmp_path, context)

    worker.run()

    assert failures == [login_failure_text(LOGIN_FAILURE_CLOSED)]
    assert context.closed is True


def test_login_worker_logs_each_round_without_cookie_values(
    monkeypatch, tmp_path, caplog
):
    context = PollThenCloseContext(cookies=[SESSDATA_COOKIE])
    worker, failures = _worker(
        monkeypatch, tmp_path, context, POLL_INTERVAL=0.001
    )
    caplog.set_level(logging.INFO, logger="adofai")

    worker.run()

    rounds = [text for text in _messages(caplog) if "登录轮询" in text]
    assert rounds, "每轮登录轮询都要写进 app.log"
    line = rounds[0]
    assert "B 站=已登录" in line
    assert "TUF=未登录" in line
    assert "浏览器候选" in line
    assert "cookie-value-123" not in "\n".join(_messages(caplog))
    assert failures == [login_failure_text(LOGIN_FAILURE_CLOSED)]


def test_login_worker_reports_launch_failure_and_redacts(monkeypatch, tmp_path, caplog):
    def boom(*_args, **_kwargs):
        raise RuntimeError("无法启动浏览器：SESSDATA=secret-value")

    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: FakePlaywright(),
    )
    monkeypatch.setattr("adofai_publisher.workers.launch_context", boom)

    from adofai_publisher.config import Settings

    worker = LoginWorker(Settings(path=tmp_path / "settings.json"))
    failures: list[str] = []
    worker.failed.connect(failures.append)
    caplog.set_level(logging.INFO, logger="adofai")

    worker.run()

    assert len(failures) == 1
    assert failures[0].startswith(login_failure_text(LOGIN_FAILURE_LAUNCH))
    assert "错误详情" in failures[0]
    assert "secret-value" not in failures[0]
    logged = "\n".join(_messages(caplog))
    assert "secret-value" not in logged
    assert "无法启动浏览器" in logged


def test_login_worker_timeout_while_one_site_logged_in(monkeypatch, tmp_path):
    context = FakeContext(cookies=[SESSDATA_COOKIE])
    worker, failures = _worker(
        monkeypatch,
        tmp_path,
        context,
        LOGIN_TIMEOUT=0.05,
        POLL_INTERVAL=0.001,
    )

    worker.run()

    assert failures == [login_failure_text(LOGIN_FAILURE_TIMEOUT)]


def test_login_worker_timeout_with_both_sites_logged_out(monkeypatch, tmp_path):
    context = FakeContext()
    worker, failures = _worker(
        monkeypatch,
        tmp_path,
        context,
        LOGIN_TIMEOUT=0.05,
        POLL_INTERVAL=0.001,
    )

    worker.run()

    assert failures == [login_failure_text(LOGIN_FAILURE_NOT_LOGGED_IN)]
