import json
import os
import zipfile
from pathlib import Path

import pytest

from adofai_publisher.level_library import LIBRARY_SCHEMA_VERSION, LevelLibraryDB
from adofai_publisher.models import LevelInfo


def _level(tuf_id=17776, song="Beneath the Vortex", charter="BeholdenEagle"):
    return LevelInfo(
        tuf_id=tuf_id,
        song=song,
        artist="Kuro Games",
        charter=charter,
        difficulty="Unranked",
        bpm=180,
        tilecount=1242,
    )


def test_upsert_and_reload(tmp_path):
    library = tmp_path / "library.json"
    chart = tmp_path / "chart.ard"
    chart.write_bytes(b"data")
    db = LevelLibraryDB(library)
    entry = db.upsert(_level(), chart)
    db.save()

    reloaded = LevelLibraryDB(library)
    entries = reloaded.all()
    assert len(entries) == 1
    assert entries[0]["key"] == "tuf:17776"
    assert entries[0]["song"] == "Beneath the Vortex"
    assert entries[0]["size"] == 4
    assert entry["key"] == "tuf:17776"


def test_same_tuf_deduplicates(tmp_path):
    db = LevelLibraryDB(tmp_path / "library.json")
    first = tmp_path / "a.ard"
    second = tmp_path / "b.ard"
    first.write_bytes(b"a")
    second.write_bytes(b"bb")
    db.upsert(_level(), first)
    db.upsert(_level(), second)
    entries = db.all()
    assert len(entries) == 1
    assert entries[0]["filename"] == "b.ard"


def test_scan_imports_ard_files(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "one.ard").write_bytes(b"1")
    (tmp_path / "b" / "two.ARD").write_bytes(b"2")
    (tmp_path / "readme.txt").write_text("x", encoding="utf-8")
    db = LevelLibraryDB(tmp_path / "library.json")
    db.scan(tmp_path)
    assert len(db.all()) == 2


def test_scan_reads_tuf_id_from_filename(tmp_path):
    (tmp_path / "Song - Charter (17776).ard").write_bytes(b"x")
    db = LevelLibraryDB(tmp_path / "library.json")
    db.scan(tmp_path)
    entries = db.all()
    assert len(entries) == 1
    assert entries[0]["key"] == "tuf:17776"


def test_scan_reads_ard_metadata(tmp_path):
    chart = tmp_path / "G" / "G12" / "Local Chart.ard"
    chart.parent.mkdir(parents=True)
    payload = {
        "angleData": [0, 90, 180, 270],
        "settings": {
            "song": "Local Song",
            "artist": "Local Artist",
            "author": "Local Charter",
            "difficulty": "G12",
            "bpm": 165,
            "songDuration": 125000,
        },
    }
    with zipfile.ZipFile(chart, "w") as archive:
        archive.writestr("level.adofai", "\ufeff" + json.dumps(payload))

    db = LevelLibraryDB(tmp_path / "library.json")
    db.scan(tmp_path)
    entries = db.all()

    assert len(entries) == 1
    assert entries[0]["song"] == "Local Song"
    assert entries[0]["artist"] == "Local Artist"
    assert entries[0]["charter"] == "Local Charter"
    assert entries[0]["difficulty"] == "G12"
    assert entries[0]["category"] == "G"
    assert entries[0]["band"] == "G12"
    assert entries[0]["game_difficulty"] == "G12"
    assert entries[0]["bpm"] == 165
    assert entries[0]["tilecount"] == 4


def test_scan_records_parse_error_and_keeps_location(tmp_path):
    folder = tmp_path / "test"
    folder.mkdir()
    chart = folder / "level.adofai"
    chart.write_text("{not-json", encoding="utf-8")

    db = LevelLibraryDB(tmp_path / "library.json")
    result = db.scan(tmp_path)

    entry = db.all()[0]
    assert entry["song"] == "test"  # 回退文件夹名，但不伪装成正常条目
    assert entry["path"] == str(chart)
    assert entry["folder"] == str(folder)
    assert "JSON" in entry["parse_error"]
    assert entry["parse_error"].startswith("谱面 JSON 解析失败")
    assert result.failed_count == 1


def test_rescan_clears_parse_error_after_file_is_fixed(tmp_path):
    folder = tmp_path / "Turkish March"
    folder.mkdir()
    chart = folder / "level.adofai"
    chart.write_text("{broken", encoding="utf-8")

    db = LevelLibraryDB(tmp_path / "library.json")
    db.scan(tmp_path)
    key = db.all()[0]["key"]
    assert db.get(key)["parse_error"]

    chart.write_text(
        json.dumps(
            {
                "pathData": "RULJ",
                "settings": {
                    "song": "Turkish March",
                    "artist": "W. A. Mozart",
                    "author": "Misoguy",
                    "bpm": 160,
                },
            }
        ),
        encoding="utf-8",
    )
    db.scan(tmp_path)

    entry = db.get(key)
    assert entry is not None
    assert entry["parse_error"] == ""
    assert entry["song"] == "Turkish March"
    assert entry["artist"] == "W. A. Mozart"
    assert entry["charter"] == "Misoguy"
    assert entry["tilecount"] == 4
    assert entry["bpm"] == 160


def test_scan_reads_legacy_path_data_tile_count(tmp_path):
    folder = tmp_path / "Legacy"
    folder.mkdir()
    with zipfile.ZipFile(folder / "Legacy.zip", "w") as archive:
        archive.writestr(
            "level.adofai",
            json.dumps(
                {
                    "pathData": "RULJ" * 25,
                    "settings": {"song": "Legacy Song", "author": "Charter"},
                }
            ),
        )

    db = LevelLibraryDB(tmp_path / "library.json")
    result = db.scan(tmp_path)

    entry = db.all()[0]
    assert entry["parse_error"] == ""
    assert entry["song"] == "Legacy Song"
    assert entry["tilecount"] == 100
    assert entry["midspin_count"] == 0
    assert result.failed_count == 0


def test_prune_missing_removes_gone_files(tmp_path):
    db = LevelLibraryDB(tmp_path / "library.json")
    chart = tmp_path / "gone.ard"
    chart.write_bytes(b"x")
    db.upsert(_level(1, "Keep"), chart)
    db.upsert(_level(2, "Drop"), tmp_path / "missing.ard")
    removed = db.prune_missing()
    assert removed == ["tuf:2"]
    assert [entry["tuf_id"] for entry in db.all()] == [1]


def test_delete_entry(tmp_path):
    db = LevelLibraryDB(tmp_path / "library.json")
    chart = tmp_path / "chart.ard"
    chart.write_bytes(b"x")
    entry = db.upsert(_level(), chart)
    assert db.delete(entry["key"], delete_file=True) is True
    assert db.all() == []
    assert not chart.exists()


def test_upsert_persists_tuf_metadata(tmp_path):
    chart = tmp_path / "chart.ard"
    chart.write_bytes(b"x")
    level = _level()
    level.vfxers = ["VFXer"]
    level.difficulty_id = 12
    level.difficulty_type = "G"
    level.difficulty_color = "#34D399"
    level.base_score = 12.5
    level.duration_ms = 180000
    level.midspin_count = 9
    level.dl_link = "https://example.com/chart"
    level.workshop_link = "https://steam.example/item"
    level.file_id = "abc"
    level.tags = [{"id": 3, "name": "Full VFX", "color": "#E1A8FF"}]
    level.curation_type_ids = [19]
    level.clears = 100
    level.unique_clears = 80
    level.likes = 25
    level.download_count = 300

    db = LevelLibraryDB(tmp_path / "library.json")
    db.upsert(level, chart)
    db.save()

    entry = LevelLibraryDB(tmp_path / "library.json").all()[0]
    assert entry["vfxers"] == ["VFXer"]
    assert entry["difficulty_id"] == 12
    assert entry["difficulty_color"] == "#34D399"
    assert entry["base_score"] == 12.5
    assert entry["midspin_count"] == 9
    assert entry["workshop_link"] == "https://steam.example/item"
    assert entry["tags"][0]["name"] == "Full VFX"
    assert entry["curation_type_ids"] == [19]
    assert entry["unique_clears"] == 80
    assert entry["download_count"] == 300

    restored = LevelInfo.from_dict(entry)
    assert restored is not None
    assert restored.vfxers == ["VFXer"]
    assert restored.base_score == 12.5
    assert restored.tags[0]["id"] == 3


def test_scan_uses_folder_as_unit_and_skips_backup(tmp_path):
    folder = tmp_path / "P" / "P10" / "Song"
    folder.mkdir(parents=True)
    (folder / "backup.ard").write_bytes(b"b" * 500)
    (folder / "Song - Charter (12345).ard").write_bytes(b"m" * 100)

    db = LevelLibraryDB(tmp_path / "library.json")
    db.scan(tmp_path)
    entries = db.all()

    assert len(entries) == 1
    entry = entries[0]
    assert entry["tuf_id"] == 12345
    assert entry["category"] == "P"
    assert entry["band"] == "P10"
    assert entry["difficulty"] == "P10"
    assert entry["folder"] == str(folder)
    assert entry["path"].endswith("Song - Charter (12345).ard")
    assert entry["key"] == "tuf:12345"


def test_scan_indexes_zip_package_as_one_chart(tmp_path):
    folder = tmp_path / "P" / "P10" / "Zipped Song"
    folder.mkdir(parents=True)
    archive_path = folder / "Zipped Song - Charter (22222).zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "level.adofai",
            json.dumps(
                {
                    "angleData": [0, 90, 180],
                    "settings": {"song": "Zipped Song", "author": "Charter"},
                }
            ),
        )
        archive.writestr("song.ogg", b"audio")

    db = LevelLibraryDB(tmp_path / "library.json")
    db.scan(tmp_path)
    entry = db.all()[0]

    assert entry["path"] == str(archive_path)
    assert entry["tuf_id"] == 22222
    assert entry["tilecount"] == 3
    assert entry["category"] == "P"
    assert entry["band"] == "P10"


def test_scan_skips_zip_when_extracted_copy_exists(tmp_path):
    folder = tmp_path / "Unranked" / "Album"
    folder.mkdir(parents=True)
    with zipfile.ZipFile(folder / "Leaf - Song.zip", "w") as archive:
        archive.writestr("level.adofai", json.dumps({"angleData": [0, 90]}))
    extracted = folder / "Leaf - Song"
    extracted.mkdir()
    (extracted / "level.adofai").write_text(
        json.dumps({"angleData": [0, 90, 180]}),
        encoding="utf-8",
    )

    db = LevelLibraryDB(tmp_path / "library.json")
    db.scan(tmp_path)
    entries = db.all()

    assert len(entries) == 1
    assert entries[0]["path"] == str(extracted / "level.adofai")


def test_scan_keeps_multiple_archives_in_same_folder(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    for name, tuf_id in (("First (10001).zip", 10001), ("Second (10002).adozip", 10002)):
        with zipfile.ZipFile(folder / name, "w") as archive:
            archive.writestr(
                "level.adofai",
                json.dumps({"angleData": [0, 90], "settings": {"song": name}}),
            )

    db = LevelLibraryDB(tmp_path / "library.json")
    db.scan(tmp_path)

    assert {entry["tuf_id"] for entry in db.all()} == {10001, 10002}


def test_scan_marks_unranked_and_collection(tmp_path):
    unranked = tmp_path / "-2and未评级" / "Aoharu" / "Aoharu"
    unranked.mkdir(parents=True)
    (unranked / "Aoharu.ard").write_bytes(b"x")
    collection = tmp_path / "智柚下和" / "未知的星球"
    collection.mkdir(parents=True)
    (collection / "shikisai.ard").write_bytes(b"y")

    db = LevelLibraryDB(tmp_path / "library.json")
    db.scan(tmp_path)
    entries = {entry["song"]: entry for entry in db.all()}

    assert len(entries) == 2
    unranked_entry = next(e for e in entries.values() if e["category"] == "Unranked")
    collection_entry = next(e for e in entries.values() if e["category"] == "Collection")
    assert unranked_entry["difficulty"] == "Unranked"
    assert collection_entry["collection"] == "智柚下和"
    assert collection_entry["difficulty"] == ""


def test_prune_missing_removes_missing_folder(tmp_path):
    folder = tmp_path / "P" / "P1" / "Song"
    folder.mkdir(parents=True)
    chart = folder / "song.ard"
    chart.write_bytes(b"x")
    db = LevelLibraryDB(tmp_path / "library.json")
    db.upsert(
        _level(9, "Folder Song"),
        chart,
        {"folder": str(folder), "category": "P", "band": "P1"},
    )

    chart.unlink()
    folder.rmdir()

    assert db.prune_missing() == ["tuf:9"]


def test_scan_prefers_level_over_backup_and_aux(tmp_path):
    folder = tmp_path / "U" / "U7" / "Tricky"
    folder.mkdir(parents=True)
    (folder / "backup2.ard").write_bytes(b"b" * 900)
    (folder / "credit.ard").write_bytes(b"c" * 800)
    (folder / "effect.ard").write_bytes(b"e" * 700)
    (folder / "level.ard").write_bytes(b"l" * 100)

    db = LevelLibraryDB(tmp_path / "library.json")
    db.scan(tmp_path)

    entry = db.all()[0]
    assert entry["path"].endswith("level.ard")


def test_bind_tuf_keeps_local_location(tmp_path):
    folder = tmp_path / "Local Song"
    folder.mkdir()
    chart = folder / "chart.adofai"
    chart.write_text("{}", encoding="utf-8")
    db = LevelLibraryDB(tmp_path / "library.json")
    entry = db.upsert(
        LevelInfo(song="Local Song", difficulty="P10", charter="Local"),
        chart,
        {"folder": str(folder), "source": "scan"},
    )
    key = entry["key"]

    bound = db.bind_tuf(
        key,
        LevelInfo(
            tuf_id=17776,
            song="TUF Song",
            artist="Artist",
            charter="Charter",
            difficulty="P10",
            base_score=12.5,
            dl_link="https://example.test/chart.ard",
            tags=[{"id": 1, "name": "tech"}],
        ),
    )

    assert bound["key"] == key
    assert bound["tuf_id"] == 17776
    assert bound["path"] == str(chart)
    assert bound["folder"] == str(folder)
    assert bound["song"] == "TUF Song"
    assert bound["dl_link"] == "https://example.test/chart.ard"
    assert bound["tags"] == [{"id": 1, "name": "tech"}]


def test_merge_entries_keeps_local_path_and_unions_tags(tmp_path):
    local_folder = tmp_path / "Local Song"
    local_folder.mkdir()
    local_chart = local_folder / "chart.adofai"
    local_chart.write_text("{}", encoding="utf-8")
    downloaded = tmp_path / "TUF Song (17776).ard"
    downloaded.write_bytes(b"data")
    db = LevelLibraryDB(tmp_path / "library.json")
    local_entry = db.upsert(
        LevelInfo(song="Local Song", difficulty="P10", charter="Local"),
        local_chart,
        {"folder": str(local_folder), "source": "scan"},
    )
    db.upsert(
        LevelInfo(
            tuf_id=17776,
            song="TUF Song",
            difficulty="P10",
            tags=[{"id": 1, "name": "tech"}, {"id": 2, "name": "vfx"}],
        ),
        downloaded,
    )

    db.bind_tuf(
        local_entry["key"],
        LevelInfo(
            tuf_id=17776,
            song="TUF Song",
            difficulty="P10",
            tags=[{"id": 1, "name": "tech"}],
        ),
    )
    merged = db.merge_entries(local_entry["key"], "tuf:17776")

    entries = db.all()
    assert len(entries) == 1
    assert merged["key"] == local_entry["key"]
    assert merged["path"] == str(local_chart)
    assert merged["folder"] == str(local_folder)
    assert [tag["name"] for tag in merged["tags"]] == ["tech", "vfx"]


def test_unbind_tuf_clears_metadata_but_keeps_file(tmp_path):
    chart = tmp_path / "chart.adofai"
    chart.write_text("{}", encoding="utf-8")
    db = LevelLibraryDB(tmp_path / "library.json")
    entry = db.upsert(
        LevelInfo(song="Local", difficulty="P10"),
        chart,
        {"source": "scan"},
    )
    db.bind_tuf(
        entry["key"],
        LevelInfo(tuf_id=17776, song="Bound", dl_link="https://example.test/x.ard"),
    )

    db.unbind_tuf(entry["key"])

    assert entry["tuf_id"] is None
    assert entry["dl_link"] == ""
    assert entry["tags"] == []
    assert entry["path"] == str(chart)
    assert chart.is_file()


def test_rescan_keeps_bound_tuf_metadata(tmp_path):
    folder = tmp_path / "P" / "P10" / "Song"
    folder.mkdir(parents=True)
    chart = folder / "chart.adofai"
    chart.write_text("{}", encoding="utf-8")
    db = LevelLibraryDB(tmp_path / "library.json")
    entry = db.upsert(LevelInfo(song="Song", difficulty="P10"), chart, {"folder": str(folder)})
    key = entry["key"]
    db.bind_tuf(
        key,
        LevelInfo(
            tuf_id=17776,
            song="Song",
            difficulty="P10",
            base_score=12.5,
            likes=99,
            dl_link="https://example.test/chart.ard",
            tags=[{"id": 1, "name": "tech"}],
        ),
    )

    db.scan(tmp_path)

    rescan = db.get(key)
    assert rescan is not None
    assert rescan["tuf_id"] == 17776
    assert rescan["base_score"] == 12.5
    assert rescan["likes"] == 99
    assert rescan["dl_link"] == "https://example.test/chart.ard"
    assert rescan["tags"] == [{"id": 1, "name": "tech"}]


def test_collection_assignment_only_changes_library_index(tmp_path):
    folder = tmp_path / "P" / "P10" / "Song"
    folder.mkdir(parents=True)
    chart = folder / "chart.adofai"
    chart.write_text("{}", encoding="utf-8")
    db = LevelLibraryDB(tmp_path / "library.json")
    entry = db.upsert(
        LevelInfo(song="Song", difficulty="P10"),
        chart,
        {"folder": str(folder), "category": "P", "band": "P10"},
    )
    key = entry["key"]

    assigned = db.assign_collection(key, "作者合集")

    assert assigned["collection"] == "作者合集"
    assert assigned["category"] == "Collection"
    assert assigned["band"] == "P10"
    assert assigned["path"] == str(chart)
    assert assigned["folder"] == str(folder)
    assert chart.is_file()

    removed = db.assign_collection(key, "")

    assert removed["collection"] == ""
    assert removed["category"] == "P"
    assert removed["band"] == "P10"
    assert removed["path"] == str(chart)
    assert chart.is_file()


def test_collection_names_counts_rename_and_persistence(tmp_path):
    db = LevelLibraryDB(tmp_path / "library.json")
    first = tmp_path / "one.adofai"
    second = tmp_path / "two.adofai"
    first.write_text("{}", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")
    first_entry = db.upsert(LevelInfo(song="One", difficulty="G4"), first)
    second_entry = db.upsert(LevelInfo(song="Two", difficulty="U2"), second)
    db.create_collection("空合集")
    db.assign_collection(first_entry["key"], "作者 A")
    db.assign_collection(second_entry["key"], "作者 A")
    db.save()

    reloaded = LevelLibraryDB(tmp_path / "library.json")
    assert reloaded.collections() == [("作者 A", 2), ("空合集", 0)]

    changed = reloaded.rename_collection("作者 A", "作者 B")
    reloaded.save()

    assert changed == 2
    assert reloaded.collections() == [("作者 B", 2), ("空合集", 0)]
    assert {
        entry["collection"] for entry in reloaded.all()
    } == {"作者 B"}


def test_dissolve_collection_restores_categories(tmp_path):
    db = LevelLibraryDB(tmp_path / "library.json")
    chart = tmp_path / "song.adofai"
    chart.write_text("{}", encoding="utf-8")
    entry = db.upsert(LevelInfo(song="Song", difficulty="U7"), chart)
    db.assign_collection(entry["key"], "待解散")

    changed = db.dissolve_collection("待解散")

    assert changed == 1
    assert entry["collection"] == ""
    assert entry["category"] == "U"
    assert entry["band"] == "U7"
    assert db.collections() == []


def test_collection_name_validation(tmp_path):
    db = LevelLibraryDB(tmp_path / "library.json")
    for name in ("", "   ", "a/b", "CON"):
        try:
            db.create_collection(name)
        except ValueError:
            pass
        else:
            raise AssertionError(f"应拒绝合集名称：{name!r}")


def test_rescan_keeps_manual_collection_category(tmp_path):
    folder = tmp_path / "P" / "P10" / "Song"
    folder.mkdir(parents=True)
    chart = folder / "level.adofai"
    chart.write_text("{}", encoding="utf-8")
    db = LevelLibraryDB(tmp_path / "library.json")
    entry = db.upsert(
        LevelInfo(song="Song", difficulty="P10"),
        chart,
        {"folder": str(folder), "category": "P", "band": "P10"},
    )
    db.assign_collection(entry["key"], "手动合集")

    db.scan(tmp_path)

    saved = db.get(entry["key"])
    assert saved is not None
    assert saved["collection"] == "手动合集"
    assert saved["category"] == "Collection"
    assert saved["band"] == "P10"


def test_scan_parent_dir_updates_tuf_entry_in_place(tmp_path):
    root = tmp_path / "levels"
    folder = root / "P" / "P1" / "Song"
    folder.mkdir(parents=True)
    chart = folder / "Song - Charter.ard"
    chart.write_bytes(b"x")

    db = LevelLibraryDB(tmp_path / "library.json")
    db.upsert(_level(17776, "Song"), chart, {"folder": str(folder), "category": "P", "band": "P1"})
    stored = db.get("tuf:17776")
    stored["tags"] = [{"id": 1, "name": "tech"}]
    stored["base_score"] = 12.5
    stored["curation_type_ids"] = [19]

    result = db.scan(tmp_path)

    entries = db.all()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["key"] == "tuf:17776"
    assert entry["tuf_id"] == 17776
    assert entry["tags"] == [{"id": 1, "name": "tech"}]
    assert entry["base_score"] == 12.5
    assert entry["curation_type_ids"] == [19]
    assert entry["path"] == str(chart)
    assert entry["filename"] == chart.name
    assert len(result) == 1
    assert result.created_count == 0
    assert result.updated_count == 1


def test_scan_parent_after_child_scan_does_not_add_entries(tmp_path):
    root = tmp_path / "levels"
    for name in ("One", "Two"):
        folder = root / "Unranked" / name
        folder.mkdir(parents=True)
        (folder / "level.adofai").write_text("{}", encoding="utf-8")

    db = LevelLibraryDB(tmp_path / "library.json")
    first = db.scan(root)
    assert len(db.all()) == 2
    assert first.created_count == 2
    assert first.updated_count == 0

    second = db.scan(tmp_path)
    assert len(db.all()) == 2
    assert second.created_count == 0
    assert second.updated_count == 2


def test_upsert_matches_existing_entry_by_normalized_path(tmp_path):
    folder = tmp_path / "Song"
    folder.mkdir()
    chart = folder / "chart.ard"
    chart.write_bytes(b"x")

    db = LevelLibraryDB(tmp_path / "library.json")
    db.upsert(_level(17776, "Song"), chart, {"folder": str(folder)})

    variant = str(chart)
    if os.name == "nt":
        variant = variant.upper().replace(os.sep, "/")
    entry = db.upsert(LevelInfo(song="Song"), Path(variant), {"folder": str(folder)})

    assert entry["key"] == "tuf:17776"
    assert entry["tuf_id"] == 17776
    assert len(db.all()) == 1


def test_same_folder_different_tuf_ids_are_not_merged(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    first = folder / "First (10001).ard"
    second = folder / "Second (10002).ard"
    first.write_bytes(b"1")
    second.write_bytes(b"2")

    db = LevelLibraryDB(tmp_path / "library.json")
    db.upsert(_level(10001, "First"), first, {"folder": str(folder)})
    db.upsert(_level(10002, "Second"), second, {"folder": str(folder)})

    assert {entry["key"] for entry in db.all()} == {"tuf:10001", "tuf:10002"}


def test_scan_keeps_nontuf_archives_in_same_folder_apart(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    for name in ("First.zip", "Second.zip"):
        with zipfile.ZipFile(folder / name, "w") as archive:
            archive.writestr("level.adofai", json.dumps({"angleData": [0, 90]}))

    db = LevelLibraryDB(tmp_path / "library.json")
    result = db.scan(tmp_path)

    assert len(db.all()) == 2
    assert result.created_count == 2


def _write_library(path, entries, schema_version=None):
    payload = {"version": 1, "collections": [], "entries": entries}
    if schema_version is not None:
        payload["schema_version"] = schema_version
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_scanned_chart(path, angles, **settings):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"angleData": angles, "settings": settings}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("level.adofai", json.dumps(payload))
    return path


def test_save_writes_schema_version(tmp_path):
    library = tmp_path / "library.json"
    LevelLibraryDB(library).save()

    raw = json.loads(library.read_text(encoding="utf-8"))
    assert raw["schema_version"] == LIBRARY_SCHEMA_VERSION


def test_load_migrates_legacy_midspin_for_scanned_entries(tmp_path):
    chart = _write_scanned_chart(
        tmp_path / "G" / "G12" / "Song.ard",
        [0, 999, 90, 999],
        song="Fixed Song",
        artist="Artist",
        author="Charter",
        bpm=150,
        difficulty="G12",
    )
    library = tmp_path / "library.json"
    _write_library(
        library,
        {
            "file:legacy": {
                "key": "file:legacy",
                "source": "scan",
                "path": str(chart),
                "folder": str(chart.parent),
                "song": "Song",
                "midspin_count": 3,  # 旧算法：非 0 角度数量
                "tilecount": 4,
                "bpm": 120,
                "added_at": "2026-11-19T10:00:00",
                "updated_at": "2026-11-19T10:00:00",
            }
        },
    )

    db = LevelLibraryDB(library)

    entry = db.get("file:legacy")
    assert entry is not None
    assert entry["midspin_count"] == 2
    assert entry["tilecount"] == 4
    assert entry["song"] == "Fixed Song"
    assert entry["artist"] == "Artist"
    assert entry["charter"] == "Charter"
    assert entry["bpm"] == 150
    assert entry["game_difficulty"] == "G12"
    assert entry["updated_at"] == "2026-11-19T10:00:00"

    raw = json.loads(library.read_text(encoding="utf-8"))
    assert raw["schema_version"] == LIBRARY_SCHEMA_VERSION
    assert raw["entries"]["file:legacy"]["midspin_count"] == 2
    assert raw["entries"]["file:legacy"]["parse_error"] == ""


def test_load_migration_keeps_download_midspin(tmp_path):
    chart = _write_scanned_chart(tmp_path / "Server Song.ard", [0, 999, 999])
    library = tmp_path / "library.json"
    _write_library(
        library,
        {
            "tuf:10808": {
                "key": "tuf:10808",
                "tuf_id": 10808,
                "source": "download",
                "path": str(chart),
                "song": "Time Leaper",
                "midspin_count": 25,
                "tilecount": 3,
            }
        },
    )

    db = LevelLibraryDB(library)

    entry = db.get("tuf:10808")
    assert entry is not None
    assert entry["midspin_count"] == 25
    assert entry["tilecount"] == 3


def test_load_migration_keeps_value_when_file_missing(tmp_path):
    library = tmp_path / "library.json"
    _write_library(
        library,
        {
            "file:gone": {
                "key": "file:gone",
                "source": "scan",
                "path": str(tmp_path / "gone.ard"),
                "song": "Gone",
                "midspin_count": 5,
            }
        },
    )

    db = LevelLibraryDB(library)

    entry = db.get("file:gone")
    assert entry is not None
    assert entry["midspin_count"] == 5
    raw = json.loads(library.read_text(encoding="utf-8"))
    assert raw["schema_version"] == LIBRARY_SCHEMA_VERSION


def test_load_migration_keeps_value_when_parse_fails(tmp_path):
    chart = tmp_path / "broken.ard"
    chart.write_text("not a chart", encoding="utf-8")
    library = tmp_path / "library.json"
    _write_library(
        library,
        {
            "file:broken": {
                "key": "file:broken",
                "source": "scan",
                "path": str(chart),
                "song": "Broken",
                "midspin_count": 7,
            }
        },
    )

    db = LevelLibraryDB(library)

    entry = db.get("file:broken")
    assert entry is not None
    assert entry["midspin_count"] == 7
    assert entry["parse_error"]


def test_schema_version_two_keeps_healthy_entries(tmp_path):
    chart = _write_scanned_chart(tmp_path / "Song.ard", [0, 999, 90])
    library = tmp_path / "library.json"
    _write_library(
        library,
        {
            "file:current": {
                "key": "file:current",
                "source": "scan",
                "path": str(chart),
                "song": "Healthy Song",
                "tilecount": 3,
                "midspin_count": 9,
            }
        },
        schema_version=2,
    )

    db = LevelLibraryDB(library)

    entry = db.get("file:current")
    assert entry is not None
    assert entry["midspin_count"] == 9
    assert entry["parse_error"] == ""


def test_schema_version_two_reparses_entry_with_empty_song(tmp_path):
    chart = _write_scanned_chart(
        tmp_path / "Song.ard",
        [0, 999, 90],
        song="Recovered Song",
        author="Charter",
    )
    library = tmp_path / "library.json"
    _write_library(
        library,
        {
            "file:broken": {
                "key": "file:broken",
                "source": "scan",
                "path": str(chart),
                "song": "",
                "midspin_count": 9,
            }
        },
        schema_version=2,
    )

    db = LevelLibraryDB(library)

    entry = db.get("file:broken")
    assert entry is not None
    assert entry["song"] == "Recovered Song"
    assert entry["midspin_count"] == 1
    assert entry["parse_error"] == ""


@pytest.mark.parametrize("tilecount", [None, 0])
def test_schema_version_two_reparses_entry_with_missing_tilecount(tmp_path, tilecount):
    """静默失败过的旧条目：曲名是文件夹名、物量为空，v3 迁移要重解析。"""
    chart = _write_scanned_chart(
        tmp_path / "Turkish March" / "level.adofai",
        [0, 90, 180],
        song="Turkish March",
        artist="W. A. Mozart",
        author="Misoguy",
    )
    server_chart = _write_scanned_chart(
        tmp_path / "Server Song.ard",
        [0, 90, 180, 270, 0],
        song="Server Song",
    )
    library = tmp_path / "library.json"
    _write_library(
        library,
        {
            "folder:test": {
                "key": "folder:test",
                "source": "scan",
                "path": str(chart),
                "folder": str(chart.parent),
                "song": "test",  # 旧版本解析失败时回退成文件夹名
                "tilecount": tilecount,
                "midspin_count": 0,
            },
            "tuf:99": {
                "key": "tuf:99",
                "tuf_id": 99,
                "source": "download",
                "path": str(server_chart),
                "song": "Server Song",
                "tilecount": 0,
            },
        },
        schema_version=2,
    )

    db = LevelLibraryDB(library)

    entry = db.get("folder:test")
    assert entry is not None
    assert entry["song"] == "Turkish March"
    assert entry["artist"] == "W. A. Mozart"
    assert entry["charter"] == "Misoguy"
    assert entry["tilecount"] == 3
    assert entry["parse_error"] == ""

    download = db.get("tuf:99")
    assert download is not None
    assert download["song"] == "Server Song"
    assert download["tilecount"] == 0  # download 条目保持服务端数据，不重解析
    assert download["parse_error"] == ""


def test_schema_version_two_reparses_entry_with_parse_error(tmp_path):
    chart = _write_scanned_chart(
        tmp_path / "Song.ard",
        [0, 90],
        song="Fixed Song",
    )
    library = tmp_path / "library.json"
    _write_library(
        library,
        {
            "file:failed": {
                "key": "file:failed",
                "source": "scan",
                "path": str(chart),
                "song": "Fixed Song",
                "parse_error": "谱面 JSON 解析失败",
            }
        },
        schema_version=2,
    )

    db = LevelLibraryDB(library)

    entry = db.get("file:failed")
    assert entry is not None
    assert entry["parse_error"] == ""
    assert entry["tilecount"] == 2


def test_load_merges_duplicate_entries_for_same_path(tmp_path):
    folder = tmp_path / "Song"
    folder.mkdir()
    chart = folder / "chart.ard"
    chart.write_bytes(b"x")
    location = str(chart)
    duplicate_location = location.upper().replace(os.sep, "/") if os.name == "nt" else location

    library = tmp_path / "library.json"
    _write_library(
        library,
        {
            "tuf:17776": {
                "key": "tuf:17776",
                "tuf_id": 17776,
                "song": "TUF Song",
                "artist": "Artist",
                "path": location,
                "folder": str(folder),
                "filename": chart.name,
                "tags": [{"id": 1, "name": "tech"}],
                "base_score": 12.5,
                "added_at": "2026-11-19T10:00:00",
                "updated_at": "2026-11-19T10:00:00",
            },
            "folder:legacy": {
                "key": "folder:legacy",
                "tuf_id": None,
                "song": "",
                "charter": "Local Charter",
                "path": duplicate_location,
                "folder": str(folder),
                "filename": chart.name,
                "tags": [{"id": 2, "name": "vfx"}],
                "added_at": "2026-11-20T09:00:00",
                "updated_at": "2026-11-20T10:00:00",
            },
        },
    )

    db = LevelLibraryDB(library)
    entries = db.all()

    assert len(entries) == 1
    entry = entries[0]
    assert entry["key"] == "tuf:17776"
    assert entry["tuf_id"] == 17776
    assert entry["base_score"] == 12.5
    assert entry["charter"] == "Local Charter"
    assert [tag["name"] for tag in entry["tags"]] == ["tech", "vfx"]
    assert entry["added_at"] == "2026-11-19T10:00:00"
    assert entry["updated_at"] == "2026-11-20T10:00:00"

    db.save()
    raw = json.loads(library.read_text(encoding="utf-8"))
    assert list(raw["entries"]) == ["tuf:17776"]


def test_load_does_not_merge_entries_with_different_tuf_ids(tmp_path):
    folder = tmp_path / "Song"
    folder.mkdir()
    chart = folder / "chart.ard"
    chart.write_bytes(b"x")
    location = str(chart)

    library = tmp_path / "library.json"
    _write_library(
        library,
        {
            "tuf:1": {
                "key": "tuf:1",
                "tuf_id": 1,
                "song": "First",
                "path": location,
                "folder": str(folder),
            },
            "tuf:2": {
                "key": "tuf:2",
                "tuf_id": 2,
                "song": "Second",
                "path": location,
                "folder": str(folder),
            },
        },
    )

    db = LevelLibraryDB(library)
    assert {entry["key"] for entry in db.all()} == {"tuf:1", "tuf:2"}

# --- 本地优先口径 / 无谱面包跳过 ------------------------------------------------

def _write_local_chart(folder, angles=(0, 90, 999, 0), bpm=180, duration=90000):
    folder.mkdir(parents=True)
    chart = folder / "level.adofai"
    chart.write_text(
        json.dumps(
            {
                "angleData": list(angles),
                "settings": {
                    "song": "Local Song",
                    "artist": "Local Artist",
                    "author": "Local Charter",
                    "difficulty": "Hard",
                    "bpm": bpm,
                    "songDuration": duration,
                },
            }
        ),
        encoding="utf-8",
    )
    return chart


def test_bind_tuf_keeps_local_numbers_and_adds_tuf_metadata(tmp_path):
    folder = tmp_path / "P" / "P10" / "Song"
    chart = _write_local_chart(folder)
    db = LevelLibraryDB(tmp_path / "library.json")
    db.scan(tmp_path)
    entry = db.all()[0]
    assert (entry["tilecount"], entry["midspin_count"], entry["playable_hits"]) == (4, 1, 3)

    bound = db.bind_tuf(
        entry["key"],
        LevelInfo(
            tuf_id=15269,
            song="TUF Song",
            artist="TUF Artist",
            charter="TUF Charter",
            vfxers=["TUF VFX"],
            difficulty="P10",
            bpm=200,
            duration_ms=100000,
            tilecount=2,
            midspin_count=1,
            auto_tile_count=0,
            base_score=12.5,
            dl_link="https://example.test/chart.ard",
            tags=[{"id": 1, "name": "tech"}],
        ),
    )

    # 物量/可击物量/BPM/时长按本地文件，绑定前后不变。
    assert bound["tilecount"] == 4
    assert bound["midspin_count"] == 1
    assert bound["auto_tile_count"] == 0
    assert bound["playable_hits"] == 3
    assert bound["bpm"] == 180
    assert bound["duration_ms"] == 90000
    # TUF 只补元数据。
    assert bound["song"] == "TUF Song"
    assert bound["artist"] == "TUF Artist"
    assert bound["charter"] == "TUF Charter"
    assert bound["vfxers"] == ["TUF VFX"]
    assert bound["difficulty"] == "P10"
    assert bound["base_score"] == 12.5
    assert bound["dl_link"] == "https://example.test/chart.ard"
    assert bound["tags"] == [{"id": 1, "name": "tech"}]
    assert bound["tuf_bound"] is True
    assert bound["meta_missing"] == []
    assert bound["path"] == str(chart)
    assert bound["folder"] == str(folder)
    assert LevelInfo.from_dict(bound).expected_hits == 3


def test_bind_tuf_falls_back_to_tuf_numbers_when_local_parse_failed(tmp_path):
    folder = tmp_path / "Broken Song"
    folder.mkdir()
    chart = folder / "level.adofai"
    chart.write_text("{not-json", encoding="utf-8")
    db = LevelLibraryDB(tmp_path / "library.json")
    db.scan(tmp_path)
    entry = db.all()[0]
    assert entry["parse_error"]

    bound = db.bind_tuf(
        entry["key"],
        LevelInfo(
            tuf_id=15271,
            song="TUF Song",
            difficulty="P10",
            tilecount=100,
            midspin_count=5,
            auto_tile_count=2,
        ),
    )

    assert bound["tilecount"] == 100
    assert bound["midspin_count"] == 5
    assert bound["auto_tile_count"] == 2
    assert bound["playable_hits"] is None
    assert bound["parse_error"] == entry["parse_error"]
    assert LevelInfo.from_dict(bound).expected_hits == 98


def test_rescan_bound_entry_keeps_tuf_metadata_and_refreshes_local_numbers(tmp_path):
    folder = tmp_path / "P" / "P10" / "Song"
    chart = _write_local_chart(folder)
    db = LevelLibraryDB(tmp_path / "library.json")
    db.scan(tmp_path)
    key = db.all()[0]["key"]
    db.bind_tuf(
        key,
        LevelInfo(
            tuf_id=15269,
            song="TUF Song",
            artist="TUF Artist",
            charter="TUF Charter",
            vfxers=["TUF VFX"],
            difficulty="P10",
            duration_ms=100000,
            base_score=12.5,
            video_link="https://example.test/video",
            dl_link="https://example.test/chart.ard",
            workshop_link="https://example.test/workshop",
            tags=[{"id": 1, "name": "tech"}],
            curation_type_ids=[19],
            clears=30,
            unique_clears=25,
            likes=99,
            download_count=300,
            raw={"id": 15269},
        ),
    )
    db.assign_collection(key, "手动合集")
    db.get(key)["review_status"] = "pending"

    chart.write_text(
        json.dumps(
            {
                "angleData": [0, 90, 999, 0, 90, 180],
                "settings": {
                    "song": "Changed Song",
                    "artist": "Changed Artist",
                    "author": "Changed Charter",
                    "difficulty": "Easy",
                    "bpm": 222,
                    "songDuration": 120000,
                },
            }
        ),
        encoding="utf-8",
    )
    db.scan(tmp_path)

    rescanned = db.get(key)
    assert rescanned["tuf_bound"] is True
    assert rescanned["song"] == "TUF Song"
    assert rescanned["artist"] == "TUF Artist"
    assert rescanned["charter"] == "TUF Charter"
    assert rescanned["vfxers"] == ["TUF VFX"]
    assert rescanned["difficulty"] == "P10"
    assert rescanned["duration_ms"] == 120000  # 本地能解析出的时长优先
    assert rescanned["base_score"] == 12.5
    assert rescanned["video_link"] == "https://example.test/video"
    assert rescanned["dl_link"] == "https://example.test/chart.ard"
    assert rescanned["workshop_link"] == "https://example.test/workshop"
    assert rescanned["tags"] == [{"id": 1, "name": "tech"}]
    assert rescanned["curation_type_ids"] == [19]
    assert (rescanned["clears"], rescanned["unique_clears"]) == (30, 25)
    assert (rescanned["likes"], rescanned["download_count"]) == (99, 300)
    assert rescanned["raw"] == {"id": 15269}
    assert rescanned["collection"] == "手动合集"
    assert rescanned["review_status"] == "pending"
    # 文件相关与本地数值刷新。
    assert rescanned["tilecount"] == 6
    assert rescanned["midspin_count"] == 1
    assert rescanned["auto_tile_count"] == 0
    assert rescanned["playable_hits"] == 5
    assert rescanned["bpm"] == 222
    assert rescanned["game_difficulty"] == "Easy"


def test_rescan_bound_entry_with_broken_file_keeps_numbers_and_metadata(tmp_path):
    folder = tmp_path / "P" / "P10" / "Song"
    chart = _write_local_chart(folder)
    db = LevelLibraryDB(tmp_path / "library.json")
    db.scan(tmp_path)
    key = db.all()[0]["key"]
    db.bind_tuf(key, LevelInfo(tuf_id=15269, song="TUF Song", difficulty="P10"))

    chart.write_text("{broken", encoding="utf-8")
    db.scan(tmp_path)

    rescanned = db.get(key)
    assert rescanned["tuf_bound"] is True
    assert rescanned["song"] == "TUF Song"
    assert rescanned["difficulty"] == "P10"
    assert rescanned["tilecount"] == 4
    assert rescanned["playable_hits"] == 3
    assert rescanned["parse_error"]


def test_scan_skips_and_cleans_archives_without_chart_content(tmp_path):
    setup = tmp_path / "ADOFAI GO Setup.zip"
    with zipfile.ZipFile(setup, "w") as archive:
        archive.writestr("app/readme.txt", "no charts here")
    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as archive:
        archive.writestr(
            "level.adofai",
            json.dumps({"angleData": [0, 90, 180], "settings": {"song": "Good"}}),
        )

    db = LevelLibraryDB(tmp_path / "library.json")
    result = db.scan(tmp_path)

    assert result.skipped_count == 1
    assert result.failed_count == 0
    assert [entry["song"] for entry in db.all()] == ["Good"]

    # 旧版本留下的垃圾条目，重扫时按同一路径清掉。
    db.upsert(
        LevelInfo(song="Junk", tilecount=7),
        setup,
        {
            "source": "scan",
            "folder": str(tmp_path),
            "parse_error": "谱面包里没有找到 .adofai 内容",
        },
    )
    assert len(db.all()) == 2

    again = db.scan(tmp_path)
    assert again.skipped_count == 1
    assert [entry["song"] for entry in db.all()] == ["Good"]


def test_scan_keeps_non_archive_parse_errors(tmp_path):
    folder = tmp_path / "Broken"
    folder.mkdir()
    (folder / "level.adofai").write_text("{broken", encoding="utf-8")

    db = LevelLibraryDB(tmp_path / "library.json")
    result = db.scan(tmp_path)

    assert result.skipped_count == 0
    assert result.failed_count == 1
    assert len(db.all()) == 1
    assert db.all()[0]["parse_error"]


def test_rescan_bound_entry_updates_category_band_and_collection(tmp_path):
    p_folder = tmp_path / "P" / "P10" / "Song A (15269)"
    a_chart = _write_local_chart(p_folder)
    b_folder = tmp_path / "P" / "P10" / "Song B (15270)"
    b_chart = _write_local_chart(b_folder)
    db = LevelLibraryDB(tmp_path / "library.json")
    db.scan(tmp_path)
    entries = {entry["tuf_id"]: entry for entry in db.all()}
    key_a = entries[15269]["key"]
    key_b = entries[15270]["key"]
    for tuf_id, key in ((15269, key_a), (15270, key_b)):
        db.bind_tuf(
            key,
            LevelInfo(
                tuf_id=tuf_id,
                song=f"TUF Song {tuf_id}",
                artist="TUF Artist",
                charter="TUF Charter",
                difficulty="P10",
                base_score=12.5,
                tags=[{"id": 1, "name": "tech"}],
            ),
        )

    # 已绑定条目换文件夹：A 从 P/P10 移到 G/G12，B 移进合集目录。
    g_folder = tmp_path / "G" / "G12" / "Song A (15269)"
    g_folder.mkdir(parents=True)
    g_chart = g_folder / a_chart.name
    a_chart.replace(g_chart)
    collection_folder = tmp_path / "作者合集" / "Song B (15270)"
    collection_folder.mkdir(parents=True)
    b_new = collection_folder / b_chart.name
    b_chart.replace(b_new)

    db.scan(tmp_path)

    moved = db.get(key_a)
    assert moved["path"] == str(g_chart)
    assert moved["folder"] == str(g_folder)
    assert (moved["category"], moved["band"]) == ("G", "G12")
    # TUF 元数据与本地数值仍保留。
    assert moved["song"] == "TUF Song 15269"
    assert moved["artist"] == "TUF Artist"
    assert moved["difficulty"] == "P10"
    assert moved["base_score"] == 12.5
    assert moved["tags"] == [{"id": 1, "name": "tech"}]
    assert (moved["tilecount"], moved["midspin_count"], moved["playable_hits"]) == (
        4,
        1,
        3,
    )

    collected = db.get(key_b)
    assert collected["path"] == str(b_new)
    assert collected["collection"] == "作者合集"
    assert collected["category"] == "Collection"
    assert collected["song"] == "TUF Song 15270"
    assert (collected["tilecount"], collected["playable_hits"]) == (4, 3)
    assert ("作者合集", 1) in db.collections()
