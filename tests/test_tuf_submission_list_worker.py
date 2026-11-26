"""TufSubmissionListWorker：分状态读取、登录态、分页与字段诊断。"""

import sys
import types

from adofai_publisher import workers as workers_module
from adofai_publisher.config import Settings
from adofai_publisher.tuf_submitter import ReviewListResult


class _FakePlaywright:
    def __enter__(self):
        return object()

    def __exit__(self, *_exc):
        return False


class _FakeBrowserContext:
    def __init__(self):
        self.request = object()
        self.closed = False

    def close(self):
        self.closed = True


def _install_fake_playwright(monkeypatch):
    fake_sync = types.ModuleType("playwright.sync_api")
    fake_sync.sync_playwright = lambda: _FakePlaywright()
    fake_package = types.ModuleType("playwright")
    fake_package.sync_api = fake_sync
    monkeypatch.setitem(sys.modules, "playwright", fake_package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync)
    monkeypatch.setattr(
        workers_module, "launch_context", lambda *_a, **_k: _FakeBrowserContext()
    )


class _FakeClient:
    session: dict = {}
    pages: dict = {}
    last_instance: "_FakeClient | None" = None

    def __init__(self, request_context=None, **_kwargs):
        self.request_context = request_context
        self.list_calls = []
        _FakeClient.last_instance = self

    def refresh_session(self):
        return dict(self.session)

    def list_submissions(self, **kwargs):
        self.list_calls.append(kwargs)
        key = (kwargs.get("status"), kwargs.get("page"))
        return self.pages.get(key, ReviewListResult(True, 200))


def _run(monkeypatch, tmp_path):
    _install_fake_playwright(monkeypatch)
    monkeypatch.setattr(workers_module, "TufClient", _FakeClient)
    worker = workers_module.TufSubmissionListWorker(
        Settings(tmp_path / "settings.json")
    )
    finished: dict = {}
    failed: list[str] = []
    logs: list[str] = []
    worker.finished.connect(finished.update)
    worker.failed.connect(failed.append)
    worker.log.connect(logs.append)
    worker.run()
    return finished, failed, logs


def _item(pass_id, status, level_id="7544", **extra):
    item = {
        "id": f"sub-{pass_id}",
        "submission_id": f"sub-{pass_id}",
        "pass_id": pass_id,
        "level_id": level_id,
        "status": status,
    }
    item.update(extra)
    return item


def test_worker_fetches_each_status_separately(monkeypatch, tmp_path):
    _FakeClient.session = {"user": {"username": "demo"}}
    _FakeClient.pages = {
        ("pending", 1): ReviewListResult(
            True, 200, total=1, items=[_item("71967", "pending")]
        ),
        ("approved", 1): ReviewListResult(
            True,
            200,
            total=3,
            items=[_item("57530", "approved"), _item("57531", "approved", "7545")],
        ),
        ("approved", 2): ReviewListResult(
            True, 200, total=3, items=[_item("57532", "approved", "7546")]
        ),
    }

    finished, failed, _logs = _run(monkeypatch, tmp_path)

    assert failed == []
    assert [item["pass_id"] for item in finished["items"]] == [
        "71967",
        "57530",
        "57531",
        "57532",
    ]
    assert finished["counts"] == {"pending": 1, "approved": 3, "declined": 0}
    assert finished["total"] == 4
    assert finished["failed"] == 0
    assert finished["auth_error"] is False
    assert finished["retryable"] is False
    calls = _FakeClient.last_instance.list_calls
    assert [(call["status"], call["page"]) for call in calls] == [
        ("pending", 1),
        ("approved", 1),
        ("approved", 2),
        ("declined", 1),
    ]
    first_call = calls[0]
    assert first_call["kind"] == "pass"
    assert first_call["limit"] == 100


def test_worker_without_login_returns_message_and_no_list_call(monkeypatch, tmp_path):
    _FakeClient.session = {"user": None, "status": 401, "retryable": False}
    _FakeClient.pages = {}

    finished, failed, _logs = _run(monkeypatch, tmp_path)

    assert failed == []
    assert finished["items"] == []
    assert finished["auth_error"] is True
    assert finished["retryable"] is False
    assert "登录" in finished["message"]
    assert _FakeClient.last_instance.list_calls == []


def test_worker_service_unavailable_is_retryable(monkeypatch, tmp_path):
    _FakeClient.session = {"user": None, "status": 503, "retryable": True}
    _FakeClient.pages = {}

    finished, _failed, _logs = _run(monkeypatch, tmp_path)

    assert finished["items"] == []
    assert finished["auth_error"] is False
    assert finished["retryable"] is True
    assert "服务暂不可用" in finished["message"]


def test_worker_one_status_failure_does_not_stop_others(monkeypatch, tmp_path):
    _FakeClient.session = {"user": {"username": "demo"}}
    _FakeClient.pages = {
        ("pending", 1): ReviewListResult(False, 500, "TUF 返回 500"),
        ("approved", 1): ReviewListResult(
            True, 200, total=1, items=[_item("57530", "approved")]
        ),
    }

    finished, failed, _logs = _run(monkeypatch, tmp_path)

    assert failed == []
    assert [item["pass_id"] for item in finished["items"]] == ["57530"]
    assert finished["failed"] == 1
    assert "pending" in finished["message"]
    assert "500" in finished["message"]
    assert [call["status"] for call in _FakeClient.last_instance.list_calls] == [
        "pending",
        "approved",
        "declined",
    ]


def test_worker_later_page_failure_keeps_existing_items(monkeypatch, tmp_path):
    _FakeClient.session = {"user": {"username": "demo"}}
    _FakeClient.pages = {
        ("approved", 1): ReviewListResult(
            True, 200, total=3, items=[_item("57530", "approved")]
        ),
        ("approved", 2): ReviewListResult(False, 500, "TUF 返回 500"),
    }

    finished, failed, _logs = _run(monkeypatch, tmp_path)

    assert failed == []
    assert [item["pass_id"] for item in finished["items"]] == ["57530"]
    assert finished["failed"] == 1
    assert "approved 第 2 页" in finished["message"]
    assert [call["status"] for call in _FakeClient.last_instance.list_calls] == [
        "pending",
        "approved",
        "approved",
        "declined",
    ]


def test_worker_deduplicates_across_statuses(monkeypatch, tmp_path):
    duplicate = _item("71967", "pending")
    _FakeClient.session = {"user": {"username": "demo"}}
    _FakeClient.pages = {
        ("pending", 1): ReviewListResult(True, 200, total=1, items=[duplicate]),
        ("approved", 1): ReviewListResult(
            True, 200, total=1, items=[dict(duplicate, status="approved")]
        ),
    }

    finished, _failed, _logs = _run(monkeypatch, tmp_path)

    assert len(finished["items"]) == 1
    # 先取到的那条（pending）保留，不会因后出现 approved 而改变。
    assert finished["items"][0]["status"] == "pending"
    assert finished["counts"] == {"pending": 1, "approved": 0, "declined": 0}
    assert set(finished) == {
        "items",
        "total",
        "pages",
        "failed",
        "auth_error",
        "retryable",
        "message",
        "counts",
        "diagnostics",
    }


def test_worker_logs_only_field_names_for_missing_ids(
    monkeypatch, tmp_path, caplog
):
    import logging

    diagnostic_item = {
        "id": "sub-1",
        "status": "approved",
        "diagnostics": {
            "missing": ["pass_id", "level_id"],
            "top": ["id", "status", "mysteryTopField"],
            "pass": ["mysteryPassField"],
            "level": ["mysteryLevelField"],
        },
    }
    _FakeClient.session = {"user": {"username": "demo"}}
    _FakeClient.pages = {
        ("approved", 1): ReviewListResult(
            True, 200, total=1, items=[diagnostic_item]
        )
    }
    caplog.set_level(logging.INFO, logger="adofai")

    finished, _failed, logs = _run(monkeypatch, tmp_path)

    assert finished["diagnostics"][0]["missing"] == ["pass_id", "level_id"]
    messages = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "adofai"
    )
    # app.log 里只有字段名，没有任何字段值。
    assert "mysteryTopField" in messages
    assert "mysteryPassField" in messages
    assert "mysteryLevelField" in messages
    assert "缺少 pass_id、level_id" in messages
    # worker 的界面日志只有进度行，不夹带原始响应。
    assert logs == ["正在读取 TUF 投稿记录…"]
