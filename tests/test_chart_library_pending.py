"""待投稿数量始终统计全库，筛选时复用已有的谱面解析。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ADOFAI_NO_AUTOLOGIN", "1")

import pytest
from PySide6.QtWidgets import QApplication

from adofai_publisher.models import LevelInfo
from adofai_publisher.ui.chart_library import ChartLibraryPage


@pytest.fixture
def page(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    widget = ChartLibraryPage()
    entries = []
    for index, (number, song, difficulty, collection) in enumerate(
        [(101, "Zulu", "P10", "合集"), (101, "Alpha", "P10", ""),
         (102, "Submitted", "G5", ""), (None, "Unmatched", "U3", ""),
         (103, "Bili only", "P12", ""), (104, "Bili uploaded", "P13", "")]
    ):
        entry = LevelInfo(tuf_id=number, song=song, difficulty=difficulty).to_dict()
        entry.update(key=f"sample:{index}", path=str(tmp_path / f"{index}.adofai"),
                     collection=collection, updated_at=f"2026-11-25 20:0{index}")
        if collection:
            entry["category"] = "Collection"
        entries.append(entry)
    widget.set_local_entries(entries)
    widget.set_submitted_records(
        [{"tuf_id": 102, "status": "submitted"},
         {"tuf_id": 103, "status": "bili_only"},
         {"tuf_id": 104, "status": "bili_uploaded"}]
    )
    yield widget
    widget.close()
    widget.deleteLater()
    app.processEvents()


@pytest.mark.parametrize("filter_name", ["search", "status", "category", "collection", "difficulty"])
def test_pending_count_is_independent_of_visible_filter(page, filter_name):
    expected_keys = [item.entry["key"] for item in page.pending_items()]
    assert len(expected_keys) == 4
    if filter_name == "search":
        page.local_search.setText("不存在的曲名")
        assert page.local_model.items() == []
    elif filter_name == "status":
        page.local_filter_combo.setCurrentIndex(2)
        assert len(page.local_model.items()) == 1
    elif filter_name == "category":
        page.local_category_combo.setCurrentIndex(page.local_category_combo.findData("G"))
        assert len(page.local_model.items()) == 1
    elif filter_name == "collection":
        page.local_collection_combo.setCurrentIndex(page.local_collection_combo.findData("合集"))
        assert len(page.local_model.items()) == 1
    else:
        page.local_difficulty_slider.set_range(60, 60)
        assert page.local_model.items() == []
    assert page.queue_button.text() == "待投稿队列 · 4"
    assert [item.entry["key"] for item in page.pending_items()] == expected_keys


@pytest.mark.parametrize("mode", ["recent", "name", "difficulty"])
@pytest.mark.parametrize("descending", [False, True])
def test_pending_count_and_queue_order_follow_existing_sort(page, mode, descending):
    page.local_sort_combo.setCurrentIndex(page.local_sort_combo.findData(mode))
    page.local_order_button.setText("↓ 降序" if descending else "↑ 升序")
    page._apply_local_filter()
    visible_pending = [item.entry["key"] for item in page.local_model.items()
                       if item.level.tuf_id and not item.submitted]
    assert [item.entry["key"] for item in page.pending_items()] == visible_pending
    assert page.queue_button.text() == "待投稿队列 · 4"


def test_pending_count_updates_records_entries_and_duplicate_ids(page):
    page.local_search.setText("不存在的曲名")
    page.set_submitted_records([{"tuf_id": 101, "status": "submitted"}])
    assert page.queue_button.text() == "待投稿队列 · 3"
    assert {item.level.tuf_id for item in page.pending_items()} == {102, 103, 104}
    page.set_submitted_records([])
    assert page.queue_button.text() == "待投稿队列 · 5"
    assert len(page.pending_items()) == 5
    page.set_local_entries([])
    assert page.queue_button.text() == "待投稿队列"
    assert page.pending_items() == []


def test_pending_count_preserves_raw_id_truthiness_and_integer_conversion(page):
    entries = [LevelInfo(tuf_id=number, song=str(index)).to_dict()
               for index, number in enumerate([None, 0, "0", "101", -1, False])]
    page.set_submitted_records([{"tuf_id": 101, "status": "submitted"}])
    page.set_local_entries([{}, *entries])
    assert [item.level.tuf_id for item in page.pending_items()] == ["0", -1]
    assert page.queue_button.text() == "待投稿队列 · 2"


def test_filter_parses_each_entry_once_without_materializing_queue(page, monkeypatch):
    parsed = []
    sorted_sizes = []
    original_parse = LevelInfo.from_dict
    original_sort = page._sort_local_items

    def parse(cls, entry):
        parsed.append(entry["key"])
        return original_parse(entry)

    def sort(items):
        sorted_sizes.append(len(items))
        return original_sort(items)

    def unexpected_queue():
        pytest.fail("筛选计数不应构造完整待投稿队列")

    monkeypatch.setattr(LevelInfo, "from_dict", classmethod(parse))
    monkeypatch.setattr(page, "_sort_local_items", sort)
    monkeypatch.setattr(page, "pending_items", unexpected_queue)
    page._apply_local_filter()
    assert parsed == [entry["key"] for entry in page._local_entries]
    assert sorted_sizes == [6]
    assert page.queue_button.text() == "待投稿队列 · 4"
