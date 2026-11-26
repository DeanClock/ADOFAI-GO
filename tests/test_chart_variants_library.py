import json
import zipfile

from adofai_publisher.ard_parser import (
    default_chart_member,
    parse_chart_member,
)
from adofai_publisher.game_bridge import chart_file_from_entry
from adofai_publisher.level_library import (
    LIBRARY_SCHEMA_VERSION,
    LevelLibraryDB,
    entry_variant,
    is_tuf_bound,
    variant_container,
)
from adofai_publisher.models import LevelInfo


def _payload(angles, actions=None, **settings):
    data = {"angleData": list(angles), "settings": dict(settings)}
    if actions is not None:
        data["actions"] = actions
    return json.dumps(data, ensure_ascii=False)


def _write_member(archive: zipfile.ZipFile, name, angles, actions=None, **settings):
    archive.writestr(name, _payload(angles, actions, **settings))


def _write_library(path, entries, schema_version):
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "schema_version": schema_version,
                "collections": [],
                "entries": entries,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_parse_chart_member_reads_only_selected_member(tmp_path):
    archive_path = tmp_path / "Song.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        _write_member(archive, "main.adofai", [0, 90, 180, 270], song="Full")
        _write_member(archive, "main nerf.adofai", [0, 90], song="Nerf")

    assert default_chart_member(archive_path) == "main.adofai"
    full = parse_chart_member(archive_path)
    nerf = parse_chart_member(archive_path, "main nerf.adofai")

    assert full.tile_count == 4
    assert full.song == "Full"
    assert nerf.tile_count == 2
    assert nerf.song == "Nerf"
    assert parse_chart_member(archive_path, "missing.adofai").error


def test_switch_variant_updates_entry_and_persists(tmp_path):
    folder = tmp_path / "Unranked" / "Song"
    folder.mkdir(parents=True)
    (folder / "level.adofai").write_text(
        _payload([0, 999, 90, 90, 90], song="Full Song", artist="Artist"),
        encoding="utf-8",
    )
    (folder / "level nerf.adofai").write_text(
        _payload([0, 90, 180], song="Nerf Song", artist="Artist"),
        encoding="utf-8",
    )
    library = tmp_path / "library.json"
    db = LevelLibraryDB(library)
    db.scan(tmp_path)

    entries = db.all()
    assert len(entries) == 1
    entry = entries[0]
    key = entry["key"]
    assert entry["variant"] == "level.adofai"
    assert entry["song"] == "Full Song"
    assert entry["tilecount"] == 5
    assert entry["midspin_count"] == 1
    assert entry["playable_hits"] == 4

    switched = db.set_variant(key, "level nerf.adofai")
    assert switched["variant"] == "level nerf.adofai"
    assert switched["song"] == "Nerf Song"
    assert switched["tilecount"] == 3
    assert switched["midspin_count"] == 0
    assert switched["playable_hits"] == 3
    assert switched["parse_error"] == ""
    db.save()

    reloaded = LevelLibraryDB(library).get(key)
    assert reloaded is not None
    assert reloaded["variant"] == "level nerf.adofai"
    assert reloaded["tilecount"] == 3
    assert reloaded["playable_hits"] == 3


def test_rescan_keeps_user_selected_variant(tmp_path):
    folder = tmp_path / "Unranked" / "Song"
    folder.mkdir(parents=True)
    (folder / "level.adofai").write_text(
        _payload([0, 90, 180, 270], song="Full Song"),
        encoding="utf-8",
    )
    (folder / "level nerf.adofai").write_text(
        _payload([0, 90], song="Nerf Song"),
        encoding="utf-8",
    )
    db = LevelLibraryDB(tmp_path / "library.json")
    db.scan(tmp_path)
    key = db.all()[0]["key"]
    db.set_variant(key, "level nerf.adofai")

    db.scan(tmp_path)

    entry = db.get(key)
    assert entry is not None
    assert entry["variant"] == "level nerf.adofai"
    assert entry["song"] == "Nerf Song"
    assert entry["tilecount"] == 2


def test_scan_detects_tuf_id_from_folder_name(tmp_path):
    folder = tmp_path / "version 10" / "tuf #1280"
    folder.mkdir(parents=True)
    archive_path = folder / "phonky town.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        _write_member(archive, "main.adofai", [0, 90, 180], song="Phonky Town")

    db = LevelLibraryDB(tmp_path / "library.json")
    db.scan(tmp_path)

    entry = db.all()[0]
    assert entry["tuf_id"] == 1280
    assert entry["key"] == "tuf:1280"


def test_scan_falls_back_meta_from_archive_name_and_records_missing(tmp_path):
    folder = tmp_path / "version 10" / "tuf #94"
    folder.mkdir(parents=True)
    archive_path = folder / "BIG SHOT (Camellia Remix) - Toby Fox & Camellia.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        _write_member(
            archive,
            "level.adofai",
            [0, 90],
            author="Charter",
        )

    db = LevelLibraryDB(tmp_path / "library.json")
    db.scan(tmp_path)

    entry = db.all()[0]
    assert entry["parse_error"] == ""
    assert entry["song"] == "BIG SHOT (Camellia Remix)"
    assert entry["artist"] == "Toby Fox & Camellia"
    assert entry["charter"] == "Charter"
    assert entry["meta_missing"] == ["song", "artist"]


def test_scan_keeps_bound_tuf_when_folder_name_has_another_id(tmp_path):
    folder = tmp_path / "tuf #1280" / "Song"
    folder.mkdir(parents=True)
    chart = folder / "level.adofai"
    chart.write_text(_payload([0, 90], song="Song"), encoding="utf-8")
    library = tmp_path / "library.json"
    db = LevelLibraryDB(library)
    db.upsert(
        LevelInfo(song="Song"),
        chart,
        {"folder": str(folder), "source": "scan"},
    )
    key = db.all()[0]["key"]
    db.bind_tuf(key, LevelInfo(tuf_id=999, song="Bound", dl_link="https://x"))

    db.scan(tmp_path)

    entry = db.get(key)
    assert entry is not None
    assert entry["tuf_id"] == 999
    assert is_tuf_bound(entry) is True


def test_schema_v3_migration_backfills_auto_and_playable(tmp_path):
    archive_path = tmp_path / "Song.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        _write_member(
            archive,
            "level.adofai",
            [0] * 10,
            actions=[
                {"floor": 3, "eventType": "AutoPlayTiles", "enabled": True},
                {"floor": 5, "eventType": "AutoPlayTiles", "enabled": False},
            ],
            song="Song",
        )
    server_chart = tmp_path / "Server Song.ard"
    with zipfile.ZipFile(server_chart, "w") as archive:
        _write_member(archive, "level.adofai", [0] * 5, song="Server Song")
    library = tmp_path / "library.json"
    _write_library(
        library,
        {
            "file:old": {
                "key": "file:old",
                "source": "scan",
                "path": str(archive_path),
                "folder": str(archive_path.parent),
                "song": "Song",
                "tilecount": 10,
                "midspin_count": 0,
            },
            "tuf:77": {
                "key": "tuf:77",
                "tuf_id": 77,
                "source": "download",
                "path": str(server_chart),
                "song": "Server Song",
                "tilecount": 5,
            },
        },
        LIBRARY_SCHEMA_VERSION - 1,
    )

    db = LevelLibraryDB(library)

    entry = db.get("file:old")
    assert entry is not None
    assert entry["auto_tile_count"] == 2
    assert entry["playable_hits"] == 8
    assert entry["variant"] == "level.adofai"
    assert entry["meta_missing"] == ["artist", "charter"]

    download = db.get("tuf:77")
    assert download is not None
    assert download["tilecount"] == 5
    assert download.get("playable_hits") is None

    raw = json.loads(library.read_text(encoding="utf-8"))
    assert raw["schema_version"] == LIBRARY_SCHEMA_VERSION


def test_schema_v3_migration_reparses_saved_variant(tmp_path):
    folder = tmp_path / "Unranked" / "Song"
    folder.mkdir(parents=True)
    full = folder / "level.adofai"
    full.write_text(_payload([0] * 6, song="Full Song"), encoding="utf-8")
    (folder / "level nerf.adofai").write_text(
        _payload([0] * 3, song="Nerf Song"), encoding="utf-8"
    )
    library = tmp_path / "library.json"
    _write_library(
        library,
        {
            "folder:x": {
                "key": "folder:x",
                "source": "scan",
                "path": str(full),
                "folder": str(folder),
                "song": "Full Song",
                "tilecount": 6,
                "variant": "level nerf.adofai",
            }
        },
        LIBRARY_SCHEMA_VERSION - 1,
    )

    entry = LevelLibraryDB(library).get("folder:x")

    assert entry is not None
    assert entry["variant"] == "level nerf.adofai"
    assert entry["song"] == "Nerf Song"
    assert entry["tilecount"] == 3
    assert entry["playable_hits"] == 3


def test_expected_hits_prefers_local_playable_hits():
    level = LevelInfo(tilecount=100, auto_tile_count=10, midspin_count=20, playable_hits=70)
    assert level.expected_hits == 70

    server_level = LevelInfo(tilecount=100, auto_tile_count=10)
    assert server_level.expected_hits == 90


def test_chart_file_from_entry_honors_selected_variant(tmp_path):
    folder = tmp_path / "Song"
    folder.mkdir()
    (folder / "level.adofai").write_text(_payload([0, 90]), encoding="utf-8")
    nerf = folder / "level nerf.adofai"
    nerf.write_text(_payload([0]), encoding="utf-8")

    entry = {
        "path": str(folder / "level.adofai"),
        "folder": str(folder),
        "variant": "level nerf.adofai",
    }
    assert chart_file_from_entry(entry) == nerf


def test_variant_container_and_entry_variant_for_folder(tmp_path):
    folder = tmp_path / "Song"
    folder.mkdir()
    chart = folder / "level.adofai"
    chart.write_text(_payload([0, 90]), encoding="utf-8")
    entry = {"key": "folder:x", "path": str(chart), "folder": str(folder)}

    assert variant_container(entry) == folder
    assert entry_variant(entry) == "level.adofai"
