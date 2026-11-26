import json
from pathlib import Path

from adofai_publisher.level_library import LevelLibraryDB
from adofai_publisher.level_organizer import (
    apply_organize_plan,
    build_organize_plan,
    infer_organize_root,
    list_organize_manifests,
    undo_manifest,
    undo_last_organize,
)
from adofai_publisher.models import LevelInfo


def _add_entry(
    db: LevelLibraryDB,
    root: Path,
    relative: str,
    *,
    song: str,
    difficulty: str,
    category: str,
    band: str,
    collection: str = "",
) -> dict:
    folder = root / relative
    folder.mkdir(parents=True)
    chart = folder / "level.adofai"
    chart.write_text("{}", encoding="utf-8")
    return db.upsert(
        LevelInfo(song=song, difficulty=difficulty, charter="Tester"),
        chart,
        {
            "source": "scan",
            "folder": str(folder),
            "category": category,
            "band": band,
            "collection": collection,
        },
    )


def test_organize_plan_apply_and_undo_moves_whole_folders(tmp_path):
    root = tmp_path / "levels"
    root.mkdir()
    db = LevelLibraryDB(tmp_path / "library.json")
    ranked = _add_entry(
        db,
        root,
        "Incoming/Ranked Song",
        song="Ranked Song",
        difficulty="P10",
        category="P",
        band="P10",
    )
    collected = _add_entry(
        db,
        root,
        "Incoming/Collection Song",
        song="Collection Song",
        difficulty="U7",
        category="Collection",
        band="U7",
        collection="智柚下和",
    )
    ranked_key = ranked["key"]
    collected_key = collected["key"]
    db.save()

    plan = build_organize_plan(db, root)
    assert plan.counts()["move"] == 2
    targets = {item.song: Path(item.target_folder) for item in plan.items}
    assert targets["Ranked Song"] == root / "P" / "P10" / "Ranked Song"
    assert targets["Collection Song"] == root / "智柚下和" / "Collection Song"

    result = apply_organize_plan(db, plan, tmp_path / "history")
    db.save()

    assert result.moved == 2
    assert result.failed == 0
    assert Path(result.backup_path).is_file()
    assert Path(result.manifest_path).is_file()
    assert (root / "P" / "P10" / "Ranked Song" / "level.adofai").is_file()
    assert (root / "智柚下和" / "Collection Song" / "level.adofai").is_file()
    assert not (root / "Incoming" / "Ranked Song").exists()

    ranked_after = db.find_by_folder(root / "P" / "P10" / "Ranked Song")
    assert ranked_after is not None
    assert ranked_after[1]["key"] == ranked_after[0]
    assert ranked_after[1]["path"].endswith(
        str(Path("P") / "P10" / "Ranked Song" / "level.adofai")
    )
    assert ranked_after[1]["key"] != ranked_key
    assert collected_key != ranked_key

    undo = undo_last_organize(db, tmp_path / "history")
    db.save()

    assert undo.moved == 2
    assert undo.failed == 0
    assert (root / "Incoming" / "Ranked Song" / "level.adofai").is_file()
    assert (root / "Incoming" / "Collection Song" / "level.adofai").is_file()
    assert not (root / "P" / "P10" / "Ranked Song").exists()
    assert db.find_by_folder(root / "Incoming" / "Ranked Song") is not None
    assert db.find_by_folder(root / "Incoming" / "Collection Song") is not None


def test_organize_plan_apply_only_selected_folders(tmp_path):
    root = tmp_path / "levels"
    root.mkdir()
    db = LevelLibraryDB(tmp_path / "library.json")
    first = _add_entry(
        db,
        root,
        "Incoming/First",
        song="First",
        difficulty="P10",
        category="P",
        band="P10",
    )
    second = _add_entry(
        db,
        root,
        "Incoming/Second",
        song="Second",
        difficulty="G2",
        category="G",
        band="G2",
    )
    db.save()
    first_key = first["key"]
    second_key = second["key"]
    plan = build_organize_plan(db, root)

    result = apply_organize_plan(
        db,
        plan,
        tmp_path / "history",
        selected_keys={first_key},
    )
    db.save()

    assert result.moved == 1
    assert result.failed == 0
    assert (root / "P" / "P10" / "First" / "level.adofai").is_file()
    assert not (root / "Incoming" / "First").exists()
    assert (root / "Incoming" / "Second" / "level.adofai").is_file()
    assert not (root / "G" / "G2" / "Second").exists()
    payload = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert [item["song"] for item in payload["items"]] == ["First"]
    assert payload["items"][0]["key"] == first_key
    assert second_key in {
        item.key for item in build_organize_plan(db, root).items
    }

    undo = undo_last_organize(db, tmp_path / "history")
    db.save()

    assert undo.moved == 1
    assert (root / "Incoming" / "First" / "level.adofai").is_file()
    assert not (root / "P" / "P10" / "First").exists()
    assert (root / "Incoming" / "Second" / "level.adofai").is_file()


def test_undo_specific_manifest_keeps_later_moves(tmp_path):
    root = tmp_path / "levels"
    root.mkdir()
    db = LevelLibraryDB(tmp_path / "library.json")
    first = _add_entry(
        db,
        root,
        "Incoming/First",
        song="First",
        difficulty="P10",
        category="P",
        band="P10",
    )
    second = _add_entry(
        db,
        root,
        "Incoming/Second",
        song="Second",
        difficulty="G2",
        category="G",
        band="G2",
    )
    db.save()
    history = tmp_path / "history"

    first_batch = apply_organize_plan(
        db,
        build_organize_plan(db, root),
        history,
        selected_keys={first["key"]},
    )
    db.save()
    second_batch = apply_organize_plan(
        db,
        build_organize_plan(db, root),
        history,
        selected_keys={second["key"]},
    )
    db.save()

    records = list_organize_manifests(history)
    assert len(records) == 2
    assert records[0]["path"] == second_batch.manifest_path
    assert records[1]["path"] == first_batch.manifest_path

    result = undo_manifest(db, first_batch.manifest_path, history)
    db.save()

    assert result.mode == "undo"
    assert result.moved == 1
    assert result.failed == 0
    assert (root / "Incoming" / "First" / "level.adofai").is_file()
    assert not (root / "P" / "P10" / "First").exists()
    assert (root / "G" / "G2" / "Second" / "level.adofai").is_file()

    records = list_organize_manifests(history)
    first_record = next(
        record for record in records if record["path"] == first_batch.manifest_path
    )
    second_record = next(
        record for record in records if record["path"] == second_batch.manifest_path
    )
    assert first_record["moved"] == 0
    assert first_record["undone"] == 1
    assert not first_record["can_undo"]
    assert second_record["moved"] == 1
    assert second_record["can_undo"]


def test_undo_only_selected_items_from_one_manifest(tmp_path):
    root = tmp_path / "levels"
    root.mkdir()
    db = LevelLibraryDB(tmp_path / "library.json")
    first = _add_entry(
        db,
        root,
        "Incoming/First",
        song="First",
        difficulty="P10",
        category="P",
        band="P10",
    )
    second = _add_entry(
        db,
        root,
        "Incoming/Second",
        song="Second",
        difficulty="G2",
        category="G",
        band="G2",
    )
    db.save()
    first_key = first["key"]
    second_key = second["key"]
    history = tmp_path / "history"
    batch = apply_organize_plan(
        db,
        build_organize_plan(db, root),
        history,
        selected_keys={first_key, second_key},
    )
    db.save()
    assert batch.moved == 2

    result = undo_manifest(
        db,
        batch.manifest_path,
        history,
        selected_keys={first_key},
    )
    db.save()

    assert result.moved == 1
    assert result.failed == 0
    assert (root / "Incoming" / "First" / "level.adofai").is_file()
    assert not (root / "P" / "P10" / "First").exists()
    assert (root / "G" / "G2" / "Second" / "level.adofai").is_file()
    record = list_organize_manifests(history)[0]
    assert record["moved"] == 1
    assert record["undone"] == 1
    assert record["can_undo"]


def test_organize_plan_marks_existing_target_as_conflict(tmp_path):
    root = tmp_path / "levels"
    root.mkdir()
    db = LevelLibraryDB(tmp_path / "library.json")
    _add_entry(
        db,
        root,
        "Incoming/Song",
        song="Song",
        difficulty="P10",
        category="P",
        band="P10",
    )
    occupied = root / "P" / "P10" / "Song"
    occupied.mkdir(parents=True)
    (occupied / "other.adofai").write_text("{}", encoding="utf-8")

    plan = build_organize_plan(db, root)

    assert plan.counts()["conflict"] == 1
    result = apply_organize_plan(db, plan, tmp_path / "history")
    assert result.moved == 0
    assert (root / "Incoming" / "Song" / "level.adofai").is_file()
    assert (occupied / "other.adofai").is_file()


def test_organize_plan_moves_external_entry_into_root_and_undoes(tmp_path):
    root = tmp_path / "levels"
    root.mkdir()
    external = tmp_path / "external"
    db = LevelLibraryDB(tmp_path / "library.json")
    _add_entry(
        db,
        external,
        "Song",
        song="Song",
        difficulty="G2",
        category="G",
        band="G2",
    )
    db.save()

    plan = build_organize_plan(db, root)

    assert plan.counts()["move"] == 1
    assert Path(plan.items[0].target_folder) == root / "G" / "G2" / "Song"

    result = apply_organize_plan(db, plan, tmp_path / "history")
    db.save()

    assert result.moved == 1
    assert result.failed == 0
    assert (root / "G" / "G2" / "Song" / "level.adofai").is_file()
    assert not (external / "Song").exists()

    undo = undo_last_organize(db, tmp_path / "history")
    db.save()

    assert undo.moved == 1
    assert undo.failed == 0
    assert (external / "Song" / "level.adofai").is_file()
    assert not (root / "G" / "G2" / "Song").exists()


def test_organize_plan_rejects_source_that_contains_root(tmp_path):
    root = tmp_path / "levels"
    root.mkdir()
    db = LevelLibraryDB(tmp_path / "library.json")
    chart = tmp_path / "level.adofai"
    chart.write_text("{}", encoding="utf-8")
    db.upsert(
        LevelInfo(song="Outer", difficulty="P10", charter="Tester"),
        chart,
        {
            "source": "scan",
            "folder": str(tmp_path),
            "category": "P",
            "band": "P10",
        },
    )

    plan = build_organize_plan(db, root)

    assert plan.counts()["skip"] == 1
    assert "包含谱面根目录" in plan.items[0].message


def test_organize_manifest_contains_safety_backup(tmp_path):
    root = tmp_path / "levels"
    root.mkdir()
    db = LevelLibraryDB(tmp_path / "library.json")
    _add_entry(
        db,
        root,
        "Incoming/Song",
        song="Song",
        difficulty="Unranked",
        category="Unranked",
        band="",
    )
    db.save()
    plan = build_organize_plan(db, root)
    result = apply_organize_plan(db, plan, tmp_path / "history")

    payload = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert payload["mode"] == "organize"
    assert payload["items"][0]["status"] == "moved"
    assert payload["backup_path"] == result.backup_path
    assert json.loads(Path(result.backup_path).read_text(encoding="utf-8"))["entries"]


def test_infer_organize_root_from_multiple_library_entries(tmp_path):
    root = tmp_path / "自定义谱"
    first = root / "P" / "P10" / "One"
    second = root / "智柚下和" / "Two"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    entries = [
        {"folder": str(first)},
        {"folder": str(second)},
    ]

    assert infer_organize_root(entries) == root
    assert infer_organize_root([entries[0]]) is None


def test_infer_organize_root_uses_largest_imported_cluster(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    major = home / "DeskBox" / "自定义谱"
    minor = home / "AppData" / "levels"
    entries = []
    relative_folders = ("P/P10/Song 1", "P/P15/Song 2", "G/G2/Song 3", "U/U5/Song 4")
    for relative in relative_folders:
        folder = major / relative
        folder.mkdir(parents=True)
        entries.append({"folder": str(folder)})
    fallback = minor / "P" / "P10" / "Downloaded"
    fallback.mkdir(parents=True)
    entries.append({"folder": str(fallback)})

    assert infer_organize_root(entries) == major


def test_infer_organize_root_ignores_difficulty_folders(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    root = home / "自定义谱"
    entries = []
    for relative in ("P/P10/One", "P/P15/Two", "G/G2/Three"):
        folder = root / relative
        folder.mkdir(parents=True)
        entries.append({"folder": str(folder)})

    assert infer_organize_root(entries) == root
