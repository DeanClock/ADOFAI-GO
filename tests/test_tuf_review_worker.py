"""TufReviewSyncWorker：只产出 review 更新，不碰投稿 status。"""

import sys
import types

from adofai_publisher import workers as workers_module
from adofai_publisher.config import Settings
from adofai_publisher.tuf_submitter import ReviewItemResult, ReviewListResult


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
    items: dict = {}
    last_instance: "_FakeClient | None" = None

    def __init__(self, request_context=None, **_kwargs):
        self.request_context = request_context
        self.list_calls = []
        self.item_calls = []
        _FakeClient.last_instance = self

    def refresh_session(self):
        return dict(self.session)

    def list_submissions(self, **kwargs):
        self.list_calls.append(kwargs)
        key = (kwargs.get("status"), kwargs.get("page"))
        return self.pages.get(key, ReviewListResult(True, 200))

    def get_submission(self, submission_id="", kind="pass", *, pass_id=""):
        record_id = str(pass_id or submission_id)
        self.item_calls.append(record_id)
        return self.items.get(
            record_id, ReviewItemResult(False, 500, "读取失败")
        )


def _run(monkeypatch, tmp_path, records):
    _install_fake_playwright(monkeypatch)
    monkeypatch.setattr(workers_module, "TufClient", _FakeClient)
    worker = workers_module.TufReviewSyncWorker(
        Settings(tmp_path / "settings.json"), records
    )
    finished: dict = {}
    failed: list[str] = []
    worker.finished.connect(finished.update)
    worker.failed.connect(failed.append)
    worker.run()
    return finished, failed


def _record(record_id, tuf_id, tuf_record_id="", bvid=""):
    return {
        "id": record_id,
        "status": "submitted",
        "tuf_id": tuf_id,
        "tuf_record_id": tuf_record_id,
        "bvid": bvid,
    }


def test_worker_matches_list_and_merges_review_fields(monkeypatch, tmp_path):
    _FakeClient.session = {"user": {"username": "demo"}}
    _FakeClient.pages = {
        ("approved", 1): ReviewListResult(
            True,
            200,
            total=2,
            items=[
                {
                    "id": "sub-71967",
                    "submission_id": "sub-71967",
                    "pass_id": "71967",
                    "level_id": "7544",
                    "status": "approved",
                },
                {
                    "id": "sub-90001",
                    "submission_id": "sub-90001",
                    "pass_id": "90001",
                    "level_id": "8888",
                    "status": "pending",
                },
            ],
        )
    }
    _FakeClient.items = {}

    records = [
        _record("local-a", 7544, tuf_record_id="71967", bvid="BV1xx411c7mD"),
        _record("local-b", 8888),
    ]
    finished, failed = _run(monkeypatch, tmp_path, records)

    assert failed == []
    assert finished["checked"] == 2
    assert finished["failed"] == []
    assert finished["auth_error"] is False
    assert finished["retryable"] is False
    updates = {item["id"]: item for item in finished["updates"]}
    assert updates["local-a"]["review_status"] == "approved"
    assert updates["local-b"]["review_status"] == "pending"
    for update in finished["updates"]:
        assert "status" not in update
        assert update["review_checked_at"]
        assert set(update) == {
            "id",
            "review_status",
            "review_checked_at",
            "review_message",
            "review_assignee",
            "review_flags",
            "review_judgements",
        }


def test_worker_without_login_updates_nothing(monkeypatch, tmp_path):
    _FakeClient.session = {"user": None, "status": 401, "retryable": False}
    _FakeClient.pages = {}
    _FakeClient.items = {}

    finished, failed = _run(
        monkeypatch,
        tmp_path,
        [_record("local-a", 7544, tuf_record_id="71967")],
    )

    assert failed == []
    assert finished["updates"] == []
    assert finished["checked"] == 0
    assert finished["auth_error"] is True
    assert finished["retryable"] is False
    assert "登录" in finished["message"]


def test_worker_retryable_service_failure_updates_nothing(monkeypatch, tmp_path):
    _FakeClient.session = {"user": None, "status": 503, "retryable": True}
    _FakeClient.pages = {}
    _FakeClient.items = {}

    finished, _failed = _run(
        monkeypatch, tmp_path, [_record("local-a", 7544, tuf_record_id="71967")]
    )

    assert finished["updates"] == []
    assert finished["auth_error"] is False
    assert finished["retryable"] is True
    assert "服务暂不可用" in finished["message"]


def test_worker_single_item_failure_does_not_stop_others(monkeypatch, tmp_path):
    _FakeClient.session = {"user": {"username": "demo"}}
    _FakeClient.pages = {
        ("approved", 1): ReviewListResult(
            True,
            200,
            total=1,
            items=[
                {
                    "id": "sub-71967",
                    "submission_id": "sub-71967",
                    "pass_id": "71967",
                    "level_id": "7544",
                    "status": "approved",
                }
            ],
        )
    }
    _FakeClient.items = {
        "77777": ReviewItemResult(False, 500, "TUF 返回 500"),
    }

    records = [
        _record("local-a", 7544, tuf_record_id="71967"),
        _record("local-b", 9999, tuf_record_id="77777"),
    ]
    finished, failed = _run(monkeypatch, tmp_path, records)

    assert failed == []
    assert finished["checked"] == 1
    assert len(finished["failed"]) == 1
    assert finished["failed"][0]["id"] == "local-b"
    assert "500" in finished["failed"][0]["message"]
    assert finished["updates"][0]["id"] == "local-a"


def test_worker_reads_individual_submission_when_not_in_list(monkeypatch, tmp_path):
    _FakeClient.session = {"user": {"username": "demo"}}
    _FakeClient.pages = {}
    _FakeClient.items = {
        "71967": ReviewItemResult(
            True,
            200,
            item={
                "id": "71967",
                "level_id": "7544",
                "status": "declined",
                "message": "视频已失效",
                "judgements": {"perfect": 10},
            },
        )
    }

    finished, failed = _run(
        monkeypatch,
        tmp_path,
        [_record("local-a", 7544, tuf_record_id="71967")],
    )

    assert failed == []
    assert finished["checked"] == 1
    update = finished["updates"][0]
    assert update["review_status"] == "declined"
    assert update["review_message"] == "视频已失效"
    assert update["review_judgements"] == {"perfect": 10}


def test_worker_pages_until_total_is_reached(monkeypatch, tmp_path):
    _FakeClient.session = {"user": {"username": "demo"}}
    _FakeClient.pages = {
        ("approved", 1): ReviewListResult(
            True,
            200,
            total=3,
            items=[{"id": "sub-1", "pass_id": "1", "level_id": "7544", "status": "pending"}],
        ),
        ("approved", 2): ReviewListResult(
            True,
            200,
            total=3,
            items=[
                {"id": "sub-2", "pass_id": "2", "level_id": "8888", "status": "pending"},
                {"id": "sub-3", "pass_id": "3", "level_id": "9999", "status": "pending"},
            ],
        ),
    }
    _FakeClient.items = {}

    finished, _failed = _run(
        monkeypatch,
        tmp_path,
        [_record("local-a", 7544, tuf_record_id="1")],
    )

    assert finished["checked"] == 1
    assert _FakeClient.last_instance is not None
    approved_pages = [
        call["page"]
        for call in _FakeClient.last_instance.list_calls
        if call["status"] == "approved"
    ]
    assert approved_pages == [1, 2]
    # finished 结构完整，UI 可以直接消费。
    assert set(finished) == {
        "updates",
        "checked",
        "failed",
        "auth_error",
        "retryable",
        "message",
    }
