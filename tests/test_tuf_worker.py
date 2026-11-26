"""TufSubmitWorker 的提交参数回归：中旋数必须传给 submit_pass。"""

import sys
import types

from adofai_publisher import workers as workers_module
from adofai_publisher.config import Settings
from adofai_publisher.models import LevelInfo
from adofai_publisher.tuf_submitter import SubmitResult


class _FakePlaywright:
    def __enter__(self):
        return object()

    def __exit__(self, *_exc):
        return False


class _FakeRequest:
    pass


class _FakeBrowserContext:
    def __init__(self):
        self.request = _FakeRequest()
        self.closed = False

    def close(self):
        self.closed = True


class _FakeTufClient:
    last_init: dict = {}
    last_submit: dict = {}

    def __init__(self, request_context=None):
        _FakeTufClient.last_init = {"request_context": request_context}

    def refresh_session(self):
        return {"user": {"username": "demo", "player": {"id": 1, "name": "Demo"}}}

    def submit_pass(self, **kwargs):
        _FakeTufClient.last_submit = kwargs
        return SubmitResult(
            True,
            200,
            "TUF 记录提交成功",
            {"record_id": "record-1", "submitted_judgements": {"perfect": 100}},
        )

    def fetch_level_passes(self, _level_id):
        return types.SimpleNamespace(ok=False, items=[])

    def get_submission(self, submission_id="", kind="pass", *, pass_id=""):
        return types.SimpleNamespace(ok=False, item=None)


def test_tuf_submit_worker_passes_midspin_count(monkeypatch, tmp_path):
    fake_sync = types.ModuleType("playwright.sync_api")
    fake_sync.sync_playwright = lambda: _FakePlaywright()
    fake_package = types.ModuleType("playwright")
    fake_package.sync_api = fake_sync
    monkeypatch.setitem(sys.modules, "playwright", fake_package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync)
    monkeypatch.setattr(workers_module, "launch_context", lambda *_a, **_k: _FakeBrowserContext())
    monkeypatch.setattr(workers_module, "TufClient", _FakeTufClient)

    worker = workers_module.TufSubmitWorker(
        Settings(tmp_path / "settings.json"),
        LevelInfo(tuf_id=13141, song="Fledgling (Super Easy)", midspin_count=7),
        "BV1xx411c7mD",
        {"perfect": 100},
        1.0,
        "G12",
    )
    finished: dict = {}
    failed: list[str] = []
    worker.finished.connect(finished.update)
    worker.failed.connect(failed.append)

    worker.run()

    assert failed == []
    assert finished["record_id"] == "record-1"
    assert _FakeTufClient.last_submit["midspin_count"] == 7
    assert _FakeTufClient.last_submit["level_id"] == 13141
    # 提交摘要走 worker 的日志通道。
    submit_log = _FakeTufClient.last_submit["log"]
    assert callable(submit_log)
    logs: list[str] = []
    worker.log.connect(logs.append)
    submit_log("TUF 判定：test")
    assert logs == ["TUF 判定：test"]


def test_tuf_submit_worker_defaults_midspin_to_zero(monkeypatch, tmp_path):
    fake_sync = types.ModuleType("playwright.sync_api")
    fake_sync.sync_playwright = lambda: _FakePlaywright()
    fake_package = types.ModuleType("playwright")
    fake_package.sync_api = fake_sync
    monkeypatch.setitem(sys.modules, "playwright", fake_package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync)
    monkeypatch.setattr(workers_module, "launch_context", lambda *_a, **_k: _FakeBrowserContext())
    monkeypatch.setattr(workers_module, "TufClient", _FakeTufClient)

    # 老记录里的 LevelInfo 没有中旋数，不能因此报错。
    worker = workers_module.TufSubmitWorker(
        Settings(tmp_path / "settings.json"),
        LevelInfo(tuf_id=13141, song="Fledgling (Super Easy)"),
        "BV1xx411c7mD",
        {"perfect": 100},
        1.0,
        "G12",
    )
    failed: list[str] = []
    worker.failed.connect(failed.append)

    worker.run()

    assert failed == []
    assert _FakeTufClient.last_submit["midspin_count"] == 0


class _RetryableTufClient:
    """会话检查遇到 503 时的替身。"""

    def __init__(self, request_context=None, **_kwargs):
        self.request_context = request_context
        self.submit_called = False

    def refresh_session(self):
        return {
            "user": None,
            "status": 503,
            "retryable": True,
            "error": "TUF 返回 503",
        }

    def submit_pass(self, **_kwargs):
        self.submit_called = True
        raise AssertionError("会话失败时不应提交")


def _install_fake_playwright(monkeypatch):
    fake_sync = types.ModuleType("playwright.sync_api")
    fake_sync.sync_playwright = lambda: _FakePlaywright()
    fake_package = types.ModuleType("playwright")
    fake_package.sync_api = fake_sync
    monkeypatch.setitem(sys.modules, "playwright", fake_package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync)
    monkeypatch.setattr(workers_module, "launch_context", lambda *_a, **_k: _FakeBrowserContext())


def test_session_check_worker_reports_retryable_error(monkeypatch, tmp_path):
    _install_fake_playwright(monkeypatch)
    monkeypatch.setattr(workers_module, "has_bili_login", lambda _context: True)
    monkeypatch.setattr(workers_module, "TufClient", _RetryableTufClient)

    worker = workers_module.SessionCheckWorker(Settings(tmp_path / "settings.json"))
    finished: dict = {}
    failed: list[str] = []
    worker.finished.connect(finished.update)
    worker.failed.connect(failed.append)

    worker.run()

    assert failed == []
    assert finished["bili"] is True
    assert finished["tuf"] is False
    assert "服务暂不可用" in finished["tuf_error"]


def test_tuf_submit_worker_reports_service_unavailable_not_logout(monkeypatch, tmp_path):
    _install_fake_playwright(monkeypatch)
    monkeypatch.setattr(workers_module, "TufClient", _RetryableTufClient)

    worker = workers_module.TufSubmitWorker(
        Settings(tmp_path / "settings.json"),
        LevelInfo(tuf_id=13141, song="Fledgling (Super Easy)"),
        "BV1xx411c7mD",
        {"perfect": 100},
        1.0,
        "G12",
    )
    failed: list[str] = []
    worker.failed.connect(failed.append)

    worker.run()

    assert len(failed) == 1
    assert "服务暂不可用" in failed[0]
    assert "重新登录" not in failed[0]
