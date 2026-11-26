from pathlib import Path

from adofai_publisher.level_paths import (
    category_for_path,
    chart_folder,
    classify_difficulty,
    entry_category,
)
from adofai_publisher.models import LevelInfo


def test_classify_standard_bands():
    assert classify_difficulty("P14") == ("P", "P14")
    assert classify_difficulty("g1") == ("G", "G1")
    assert classify_difficulty("U7") == ("U", "U7")


def test_classify_special_difficulties():
    assert classify_difficulty("Unranked") == ("Unranked", "")
    assert classify_difficulty("P0") == ("Unranked", "")
    assert classify_difficulty("Censored") == ("Censored", "")
    assert classify_difficulty("Impossible") == ("Impossible", "")
    assert classify_difficulty("") == ("Unranked", "")


def test_category_for_path():
    assert category_for_path(["P", "P14", "beyond"]) == ("P", "P14", "")
    assert category_for_path(["U", "U7", "Laur"]) == ("U", "U7", "")
    assert category_for_path(["-2and未评级", "Aoharu"]) == ("Unranked", "", "")
    assert category_for_path(["智柚下和", "未知的星球"]) == (
        "Collection",
        "",
        "智柚下和",
    )
    assert category_for_path([]) == ("Unranked", "", "")


def test_chart_folder_groups_by_band(tmp_path):
    level = LevelInfo(tuf_id=17643, song="SUGARPOT", charter="3.14", difficulty="P10")
    folder = chart_folder(tmp_path, level)
    assert folder == tmp_path / "P" / "P10" / "SUGARPOT"


def test_chart_folder_disambiguates_same_song(tmp_path):
    first = LevelInfo(tuf_id=1, song="Same", charter="A", difficulty="P10")
    second = LevelInfo(tuf_id=2, song="Same", charter="B", difficulty="P10")
    first_folder = chart_folder(tmp_path, first)
    first_folder.mkdir(parents=True)
    (first_folder / "Same - A (1).ard").write_bytes(b"x")

    second_folder = chart_folder(tmp_path, second)

    assert second_folder != first_folder
    assert second_folder.name == "Same - B (2)"


def test_chart_folder_reuses_existing_tuf_id(tmp_path):
    level = LevelInfo(tuf_id=7, song="Song", charter="C", difficulty="G5")
    folder = chart_folder(tmp_path, level)
    folder.mkdir(parents=True)
    (folder / "Song - C (7).ard").write_bytes(b"x")

    assert chart_folder(tmp_path, level) == folder


def test_chart_folder_reuses_existing_unranked_folder(tmp_path):
    (tmp_path / "-2and未评级").mkdir()
    level = LevelInfo(tuf_id=5, song="Aoharu", charter="X", difficulty="P0")

    assert chart_folder(tmp_path, level) == tmp_path / "-2and未评级" / "Aoharu"


def test_chart_folder_creates_unranked_folder_when_missing(tmp_path):
    level = LevelInfo(tuf_id=5, song="Aoharu", charter="X", difficulty="Unranked")

    assert chart_folder(tmp_path, level) == tmp_path / "Unranked" / "Aoharu"


def test_entry_category_falls_back_to_difficulty():
    assert entry_category({"category": "Collection"}) == "Collection"
    assert entry_category({"difficulty": "P17"}) == "P"
    assert entry_category({}) == "Unranked"
