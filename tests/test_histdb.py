import json

from adofai_publisher.histdb import HistoryDB


def test_roundtrip(tmp_path):
    path = tmp_path / "records.json"
    db = HistoryDB(path)
    record = db.add({"song": "Test Song", "status": "draft"})
    assert record["id"]
    assert record["created_at"]

    reloaded = HistoryDB(path)
    assert len(reloaded.all()) == 1
    assert reloaded.all()[0]["song"] == "Test Song"

    updated = reloaded.update(record["id"], {"status": "submitted"})
    assert updated is not None
    assert updated["status"] == "submitted"

    assert reloaded.delete(record["id"]) is True
    assert reloaded.all() == []


def test_legacy_list_format(tmp_path):
    path = tmp_path / "records.json"
    path.write_text(
        json.dumps([{"song": "Legacy A"}, {"song": "Legacy B"}]),
        encoding="utf-8",
    )
    db = HistoryDB(path)
    assert {item["song"] for item in db.all()} == {"Legacy A", "Legacy B"}
    assert all(item["id"] for item in db.all())


def test_legacy_records_dict_format(tmp_path):
    path = tmp_path / "records.json"
    path.write_text(
        json.dumps({"records": [{"song": "Wrapped"}]}),
        encoding="utf-8",
    )
    db = HistoryDB(path)
    assert db.all()[0]["song"] == "Wrapped"


def test_corrupt_file_is_ignored(tmp_path):
    path = tmp_path / "records.json"
    path.write_text("{not json", encoding="utf-8")
    db = HistoryDB(path)
    assert db.all() == []


def test_review_fields_survive_reload(tmp_path):
    path = tmp_path / "records.json"
    db = HistoryDB(path)
    record = db.add(
        {
            "song": "Carnation",
            "status": "submitted",
            "tuf_record_id": "71967",
            "review_status": "pending",
            "review_checked_at": "2026-11-23 10:00",
            "review_message": "等待版主处理",
            "review_assignee": "mod",
            "review_flags": [{"flag": "ok"}],
            "review_judgements": {"perfect": 10},
        }
    )

    reloaded = HistoryDB(path).get(record["id"])
    assert reloaded is not None
    assert reloaded["status"] == "submitted"
    assert reloaded["review_status"] == "pending"
    assert reloaded["review_checked_at"] == "2026-11-23 10:00"
    assert reloaded["review_message"] == "等待版主处理"
    assert reloaded["review_assignee"] == "mod"
    assert reloaded["review_flags"] == [{"flag": "ok"}]
    assert reloaded["review_judgements"] == {"perfect": 10}
