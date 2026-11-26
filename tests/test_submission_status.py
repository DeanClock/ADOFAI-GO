from adofai_publisher.submission_status import (
    is_tuf_submitted,
    next_status_after_bvid,
)


def test_is_tuf_submitted_only_accepts_submitted():
    assert is_tuf_submitted({"status": "submitted"}) is True
    assert is_tuf_submitted({"status": "bili_uploaded"}) is False
    assert is_tuf_submitted({"status": "bili_only"}) is False
    assert is_tuf_submitted({"status": "draft"}) is False
    assert is_tuf_submitted({"status": "failed"}) is False


def test_is_tuf_submitted_requires_status_key():
    assert is_tuf_submitted({}) is False
    assert is_tuf_submitted({"tuf_id": 101, "title": "No Status"}) is False
    assert is_tuf_submitted(None) is False


def test_next_status_after_bvid_never_downgrades_submitted():
    assert next_status_after_bvid("submitted", "dual") == "submitted"
    assert next_status_after_bvid("submitted", "bili_only") == "submitted"


def test_next_status_after_bvid_keeps_bili_only():
    assert next_status_after_bvid("bili_only", "dual") == "bili_only"
    assert next_status_after_bvid("bili_only", "bili_only") == "bili_only"


def test_next_status_after_bvid_dual_mode_marks_bili_uploaded():
    assert next_status_after_bvid("draft", "dual") == "bili_uploaded"
    assert next_status_after_bvid("bili_uploaded", "dual") == "bili_uploaded"
    assert next_status_after_bvid("failed", "dual") == "bili_uploaded"
    assert next_status_after_bvid("", "dual") == "bili_uploaded"
    assert next_status_after_bvid(None, "dual") == "bili_uploaded"
    assert next_status_after_bvid("draft", None) == "bili_uploaded"


def test_next_status_after_bvid_bili_only_mode_marks_bili_only():
    assert next_status_after_bvid("draft", "bili_only") == "bili_only"
    assert next_status_after_bvid("bili_uploaded", "bili_only") == "bili_only"


def test_review_status_does_not_change_submission_semantics():
    # 审核未通过也表示记录已投给 TUF：status 语义保持不变。
    declined = {"status": "submitted", "review_status": "declined"}
    assert is_tuf_submitted(declined) is True
    assert next_status_after_bvid(declined["status"], "dual") == "submitted"

    pending = {"status": "submitted", "review_status": "pending"}
    assert is_tuf_submitted(pending) is True

    # 只有审核字段、没有 status 的记录仍然不算已投稿。
    assert is_tuf_submitted({"review_status": "approved"}) is False
