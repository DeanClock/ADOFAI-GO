"""审核状态纯函数：映射、容错解析、匹配与合并。"""

import pytest

from adofai_publisher.models import Draft
from adofai_publisher.tuf_review import (
    REVIEW_APPROVED,
    REVIEW_DECLINED,
    REVIEW_PENDING,
    REVIEW_UNKNOWN,
    _json_safe,
    extract_submission_item,
    match_submission,
    merge_review,
    normalize_review_status,
    review_text,
)


@pytest.mark.parametrize("size", [49, 50, 51])
@pytest.mark.parametrize("container", [dict, list, tuple, set])
def test_json_safe_limits_each_container_in_iteration_order(container, size):
    if container is dict:
        value = {str(index): index for index in range(size)}
        expected = {str(index): index for index in range(min(size, 50))}
    else:
        value = container(range(size))
        expected = list(value)[:50]
    assert _json_safe(value) == expected


@pytest.mark.parametrize("container", [dict, list, tuple, set])
def test_json_safe_reads_at_most_fifty_input_items(container):
    reads = []

    class Counted(container):
        def __iter__(self):
            for item in super().__iter__():
                reads.append(item)
                assert len(reads) <= 50
                yield item

        def items(self):
            for item in super().items():
                reads.append(item)
                assert len(reads) <= 50
                yield item

    value = Counted({str(i): i for i in range(1000)} if container is dict else range(1000))
    assert len(_json_safe(value)) == 50
    assert len(reads) == 50


@pytest.mark.parametrize("container", [dict, list])
def test_json_safe_snapshots_before_converting_values(container):
    class ClearsContainer:
        def __str__(self):
            value.clear()
            return "已转换"

    value = {"first": ClearsContainer(), "second": 2} if container is dict else [ClearsContainer(), 2]
    expected = {"first": "已转换", "second": 2} if container is dict else ["已转换", 2]
    assert _json_safe(value) == expected
    assert not value


def test_json_safe_preserves_depth_boundary_and_scalar_types():
    scalars = [None, True, False, 12, 1.25, "文本"]
    assert _json_safe(scalars, depth=3) == scalars
    assert _json_safe(scalars, depth=4) == [None] * len(scalars)
    for value in scalars + [{"key": 1}, [1]]:
        assert _json_safe(value, depth=5) is None
    assert _json_safe({"outer": {"inner": 1}}, depth=3) == {"outer": {"inner": None}}


@pytest.mark.parametrize("size", [299, 300, 301])
def test_json_safe_truncates_strings_and_other_objects(size):
    class TextValue:
        def __str__(self):
            return "字" * size

    expected = "字" * min(size, 300)
    assert _json_safe("字" * size) == expected
    assert _json_safe(TextValue()) == expected


def test_json_safe_input_limit_precedes_truncated_key_collisions():
    prefix = "键" * 60
    value = {prefix + str(index): index for index in range(51)}
    assert _json_safe(value) == {prefix: 49}


def test_large_flags_keep_format_through_extract_merge_and_draft():
    flags = [{"note": "字" * 301, "number": index} for index in range(51)]
    expected = [{"note": "字" * 300, "number": index} for index in range(50)]
    item = extract_submission_item({"id": 1, "levelId": 2, "status": "pending", "flags": flags})
    assert item["flags"] == expected
    review = merge_review(item, checked_at="2026-11-25 20:00")
    assert review["review_flags"] == expected
    assert "status" not in review
    restored = Draft.from_dict(Draft(**review).to_dict())
    assert restored.review_flags == expected


def test_normalize_approved_aliases():
    for value in ("approved", "Accepted", "passed", "VERIFIED", " approve "):
        assert normalize_review_status(value) == REVIEW_APPROVED, value


def test_normalize_declined_aliases():
    for value in ("declined", "Rejected", "denied", "failed"):
        assert normalize_review_status(value) == REVIEW_DECLINED, value


def test_normalize_pending_aliases():
    for value in ("pending", "reviewing", "in_review", "IN REVIEW", "waiting"):
        assert normalize_review_status(value) == REVIEW_PENDING, value


def test_unknown_status_is_never_declined():
    for value in ("", None, "archived", "banned", "???", 0, 3, [], {}, True):
        assert normalize_review_status(value) == REVIEW_UNKNOWN, value


def test_nested_status_objects_are_read():
    assert normalize_review_status({"name": "pending"}) == REVIEW_PENDING
    assert normalize_review_status({"value": "approved"}) == REVIEW_APPROVED
    assert normalize_review_status({"foo": "bar"}) == REVIEW_UNKNOWN


def test_review_text_labels():
    assert review_text("pending") == "待处理"
    assert review_text("approved") == "已通过"
    assert review_text("declined") == "未通过"
    assert review_text("something-new") == "未知"
    assert review_text("") == ""
    assert review_text(None) == ""


def test_extract_flat_item_keeps_only_known_judgements():
    payload = {
        "id": 71967,
        "level": {"id": 7544, "song": "Carnation"},
        "status": "Pending",
        "assignedPlayer": {"id": 10539, "name": "Someone"},
        "videoLink": "https://www.bilibili.com/video/BV1xx411c7mD",
        "flags": [{"type": "needs-info", "value": True}],
        "judgements": {"perfect": 10, "tooEarly": 5, "lateDouble": "3"},
        "message": "资料待补充",
    }

    item = extract_submission_item(payload)

    assert item is not None
    assert item["id"] == "71967"
    assert item["level_id"] == "7544"
    assert item["status"] == REVIEW_PENDING
    assert item["message"] == "资料待补充"
    assert item["assignee"] == "Someone"
    assert item["bvid"] == "BV1xx411c7mD"
    assert item["judgements"] == {"perfect": 10, "lateDouble": 3}
    assert item["flags"] == [{"type": "needs-info", "value": True}]


def test_extract_nested_wrappers_and_alternative_ids():
    payload = {"pass": {"passId": "123", "levelId": 99, "state": "approved"}}
    item = extract_submission_item(payload)
    assert item is not None
    # 顶层没有 id：内层 pass 的编号只能当 pass id，不能冒充 submission id。
    assert item["id"] == ""
    assert item["submission_id"] == ""
    assert item["pass_id"] == "123"
    assert item["level_id"] == "99"
    assert item["status"] == REVIEW_APPROVED

    listing = {"data": {"submissionId": 456, "level": 7, "reviewStatus": "declined"}}
    other = extract_submission_item(listing)
    assert other is not None
    assert other["id"] == "456"
    assert other["level_id"] == "7"
    assert other["status"] == REVIEW_DECLINED


def test_extract_unknown_payload_returns_none():
    assert extract_submission_item(None) is None
    assert extract_submission_item([]) is None
    assert extract_submission_item({"success": False}) is None
    assert extract_submission_item({"error": "not found"}) is None


def test_extract_player_id_from_public_pass_record():
    item = extract_submission_item(
        {"id": 5, "levelId": 7544, "playerId": 10539, "status": "approved"}
    )
    assert item is not None
    assert item["player_id"] == 10539


def _record(**extra):
    record = {
        "id": "local-1",
        "status": "submitted",
        "tuf_id": 7544,
        "tuf_record_id": "71967",
        "bvid": "BV1xx411c7mD",
    }
    record.update(extra)
    return record


def _item(**extra):
    raw_id = str(extra.pop("id", "71967"))
    item = {
        "id": f"sub-{raw_id}",
        "submission_id": f"sub-{raw_id}",
        "pass_id": raw_id,
        "level_id": "7544",
        "status": REVIEW_PENDING,
        "bvid": "BV1xx411c7mD",
        "video_link": "https://www.bilibili.com/video/BV1xx411c7mD",
    }
    item.update(extra)
    return item


def test_match_prefers_record_id():
    wanted = _item(status=REVIEW_APPROVED)
    other = _item(id="90001", status=REVIEW_DECLINED)
    assert match_submission(_record(), [other, wanted]) is wanted


def test_match_by_level_and_bvid():
    wanted = _item(id="90001")
    unrelated = _item(id="90002", bvid="BV1yy411c7mD", video_link="")
    matched = match_submission(
        _record(tuf_record_id=""), [wanted, unrelated]
    )
    assert matched is wanted


def test_match_ambiguous_level_and_bvid_returns_none():
    first = _item(id="90001")
    second = _item(id="90002")
    assert (
        match_submission(_record(tuf_record_id=""), [first, second]) is None
    )


def test_match_tuf_id_only_requires_unique_local_record():
    item = _item()
    record = _record(tuf_record_id="", bvid="")
    assert match_submission(record, [item], tuf_id_unique=True) is item
    assert match_submission(record, [item], tuf_id_unique=False) is None


def test_match_tuf_id_only_is_ambiguous_with_multiple_items():
    first = _item(id="90001")
    second = _item(id="90002")
    record = _record(tuf_record_id="", bvid="")
    assert match_submission(record, [first, second], tuf_id_unique=True) is None


def test_merge_review_never_touches_status():
    review = merge_review(_item(status=REVIEW_APPROVED), checked_at="2026-11-23 10:00")

    assert "status" not in review
    assert set(review) == {
        "review_status",
        "review_checked_at",
        "review_message",
        "review_assignee",
        "review_flags",
        "review_judgements",
    }
    assert review["review_status"] == REVIEW_APPROVED
    assert review["review_checked_at"] == "2026-11-23 10:00"


def test_merge_review_handles_missing_item():
    assert merge_review(None) == {}


def test_old_draft_without_review_fields_loads_empty():
    draft = Draft.from_dict({"song": "旧草稿", "status": "submitted"})
    assert draft.review_status == ""
    assert draft.review_checked_at == ""
    assert draft.review_message == ""
    assert draft.review_assignee == ""
    assert draft.review_flags is None
    assert draft.review_judgements == {}


def test_draft_roundtrip_keeps_review_fields():
    draft = Draft(
        review_status=REVIEW_APPROVED,
        review_checked_at="2026-11-23 10:00",
        review_message="已通过",
        review_assignee="mod",
        review_flags=[{"flag": "ok"}],
        review_judgements={"perfect": 12, "lateDouble": "bad"},
    )
    restored = Draft.from_dict(draft.to_dict())
    assert restored.review_status == REVIEW_APPROVED
    assert restored.review_checked_at == "2026-11-23 10:00"
    assert restored.review_message == "已通过"
    assert restored.review_assignee == "mod"
    assert restored.review_flags == [{"flag": "ok"}]
    # 非数字判定值被忽略，不影响载入。
    assert restored.review_judgements == {"perfect": 12}


def test_extract_display_fields_from_flat_item():
    item = extract_submission_item(
        {
            "id": "71967",
            "levelId": 7544,
            "status": "approved",
            "song": "Carnation",
            "difficulty": "P15",
            "createdAt": "2026-11-21T08:30:00.000Z",
            "updatedAt": "2026-11-22T09:00:00Z",
        }
    )

    assert item is not None
    assert item["song"] == "Carnation"
    assert item["difficulty"] == "P15"
    assert item["created_at"].startswith("2026-11-21")
    assert item["updated_at"].startswith("2026-11-22")


def test_extract_display_fields_from_nested_level():
    item = extract_submission_item(
        {
            "id": 1,
            "level": {
                "id": 7544,
                "song": "Carnation",
                "difficulty": {"name": "P15"},
            },
            "status": "pending",
            "submittedAt": "2026-11-21T08:30:00Z",
        }
    )

    assert item is not None
    assert item["song"] == "Carnation"
    assert item["difficulty"] == "P15"
    assert item["created_at"].startswith("2026-11-21")


def test_extract_display_fields_stay_empty_when_absent():
    item = extract_submission_item({"id": 1, "levelId": 2, "status": "pending"})

    assert item is not None
    assert item["song"] == ""
    assert item["difficulty"] == ""
    assert item["created_at"] == ""
    assert item["updated_at"] == ""


def test_extract_keeps_submission_and_pass_ids_separate():
    payload = {
        "id": 57530,
        "passId": 71967,
        "status": "approved",
        "levelId": 7544,
    }
    item = extract_submission_item(payload)

    assert item is not None
    assert item["id"] == "57530"
    assert item["submission_id"] == "57530"
    assert item["pass_id"] == "71967"


def test_extract_reads_pass_id_from_nested_pass_object():
    item = extract_submission_item(
        {"id": 57530, "status": "pending", "pass": {"id": 71967, "levelId": 7544}}
    )

    assert item is not None
    assert item["submission_id"] == "57530"
    assert item["pass_id"] == "71967"
    assert item["level_id"] == "7544"


def test_extract_reads_pass_id_from_record_id_alias():
    item = extract_submission_item({"id": 57530, "recordId": 71967, "status": "pending"})

    assert item is not None
    assert item["submission_id"] == "57530"
    assert item["pass_id"] == "71967"


def test_extract_merges_display_fields_from_nested_pass_even_for_top_level_item():
    payload = {
        "id": 57530,
        "status": "approved",
        "createdAt": "2026-11-21T08:30:00Z",
        "pass": {
            "id": 71967,
            "level": {
                "id": 7544,
                "song": "Carnation",
                "difficulty": {"name": "P15"},
            },
        },
    }

    item = extract_submission_item(payload)

    assert item is not None
    assert item["submission_id"] == "57530"
    assert item["pass_id"] == "71967"
    assert item["level_id"] == "7544"
    assert item["song"] == "Carnation"
    assert item["difficulty"] == "P15"
    assert item["created_at"].startswith("2026-11-21")


def test_diagnostics_only_contain_field_names():
    import json

    item = extract_submission_item(
        {
            "id": 57530,
            "status": "approved",
            "secretValue": "绝不能出现在诊断里",
            "pass": {"mysteryPassField": "也绝不能出现"},
            "level": {"mysteryLevelField": "更不行"},
        }
    )

    assert item is not None
    diag = item["diagnostics"]
    assert set(diag["missing"]) == {"pass_id", "level_id"}
    assert "secretValue" in diag["top"]
    assert "mysteryPassField" in diag["pass"]
    assert "mysteryLevelField" in diag["level"]
    dumped = json.dumps(diag, ensure_ascii=False)
    assert "绝不能出现在诊断里" not in dumped
    assert "也绝不能出现" not in dumped
    assert "更不行" not in dumped


def test_match_accepts_submission_id_when_record_stores_it():
    item = _item()
    record = _record(tuf_record_id="sub-71967")
    assert match_submission(record, [item]) is item


def _real_list_item(**overrides):
    """DC 实测的扁平列表结构：没有 pass/level 子对象。"""

    payload = {
        "kind": "pass",
        "id": 57530,
        "status": "approved",
        "createdAt": "2026-11-13T10:20:30.000Z",
        "videoLink": "https://www.bilibili.com/video/BV1xx411c7mD",
        "title": "Vanilla Flavored Soda",
        "artist": "Some Artist",
        "href": "https://tuforums.com/levels/5756",
        "passHref": "https://tuforums.com/passes/71967",
        "workshopLink": "https://steamcommunity.com/sharedfiles/filedetails/?id=123",
        "downloadLink": "https://example.com/download/5756.zip",
        "extra": {"difficulty": "P13", "bpm": 120},
    }
    payload.update(overrides)
    return payload


def test_extract_real_flat_list_item():
    item = extract_submission_item(_real_list_item())

    assert item is not None
    assert item["submission_id"] == "57530"
    assert item["id"] == "57530"
    assert item["pass_id"] == "71967"
    assert item["level_id"] == "5756"
    assert item["song"] == "Vanilla Flavored Soda"
    assert item["artist"] == "Some Artist"
    assert item["difficulty"] == "P13"
    assert item["created_at"].startswith("2026-11-13")
    # 编号与难度都取到了，不再产生诊断。
    assert item["diagnostics"] is None


def test_extract_href_and_pass_href_url_variants():
    cases = [
        ("/level/42", "/pass/7"),
        ("/levels/42?foo=1", "/passes/7/"),
        (
            "https://api.tuforums.com/v2/database/levels/42",
            "https://tuforums.com/passes/7",
        ),
    ]
    for href, pass_href in cases:
        item = extract_submission_item(
            {"id": 1, "status": "approved", "href": href, "passHref": pass_href}
        )
        assert item is not None
        assert item["level_id"] == "42", href
        assert item["pass_id"] == "7", pass_href


def test_extract_level_id_falls_back_to_download_and_workshop_links():
    via_download = extract_submission_item(
        {
            "id": 1,
            "status": "approved",
            "downloadLink": "https://example.com/levels/99/files.zip",
        }
    )
    assert via_download is not None
    assert via_download["level_id"] == "99"

    via_workshop = extract_submission_item(
        {
            "id": 2,
            "status": "approved",
            "workshopLink": "https://example.com/levels/88",
        }
    )
    assert via_workshop is not None
    assert via_workshop["level_id"] == "88"


def test_extract_difficulty_from_json_string_extra():
    payload = _real_list_item(extra='{"difficulty": "U5"}')
    item = extract_submission_item(payload)
    assert item is not None
    assert item["difficulty"] == "U5"


def test_extract_missing_or_empty_extra_leaves_difficulty_empty():
    for extra in ({}, None, "not json"):
        payload = _real_list_item()
        payload["extra"] = extra
        item = extract_submission_item(payload)
        assert item is not None
        assert item["difficulty"] == ""
        # 编号都在，只是难度缺失；空 extra 不产生诊断。
        assert item["diagnostics"] is None


def test_diagnostics_include_extra_field_names_only():
    import json

    item = extract_submission_item(
        {
            "id": 1,
            "status": "approved",
            "href": "https://tuforums.com/levels/5",
            "passHref": "https://tuforums.com/passes/6",
            "extra": {"mysteryKey": "绝不能出现的值"},
        }
    )

    assert item is not None
    diag = item["diagnostics"]
    assert diag["missing"] == ["difficulty"]
    assert "mysteryKey" in diag["extra"]
    assert "绝不能出现的值" not in json.dumps(diag, ensure_ascii=False)
