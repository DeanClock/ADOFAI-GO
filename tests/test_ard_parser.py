import io
import json
import zipfile

from adofai_publisher.ard_parser import (
    parse_ard,
    profile_from_chart_bytes,
    profile_from_chart_data,
)
from adofai_publisher.models import LevelInfo


def _write_ard(path, payload):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("level.adofai", "\ufeff" + json.dumps(payload))
    return path


def test_parse_zip_level(tmp_path):
    path = _write_ard(
        tmp_path / "chart.ard",
        {
            "angleData": [0, 0, 180, 90],
            "settings": {
                "version": 19,
                "bpm": 180,
                "song": "Demo",
                "artist": "Artist",
                "author": "Charter",
                "hitsound": "Kick",
                "songFilename": "audio.ogg",
            },
            "actions": [
                {"eventType": "Twirl"},
                {"eventType": "Twirl"},
                {"eventType": "SetSpeed"},
            ],
        },
    )
    profile = parse_ard(str(path))
    assert profile.error == ""
    assert profile.tile_count == 4
    assert profile.zero_angle_count == 2
    # 90 / 180 是普通角度，不是中旋。
    assert profile.midspin_count == 0
    assert profile.bpm == 180
    assert profile.version == 19
    assert profile.song == "Demo"
    assert profile.artist == "Artist"
    assert profile.author == "Charter"
    assert profile.event_summary == {"Twirl": 2, "SetSpeed": 1}


def test_midspin_counts_only_999_entries(tmp_path):
    path = _write_ard(
        tmp_path / "midspin.ard",
        {"angleData": [0, 999, 90, 999.0, "999", 180, "bad", None]},
    )
    profile = parse_ard(str(path))
    assert profile.error == ""
    assert profile.tile_count == 8
    assert profile.zero_angle_count == 1
    assert profile.midspin_count == 3


def test_midspin_is_zero_for_plain_angles(tmp_path):
    path = tmp_path / "plain_angles.adofai"
    path.write_text(json.dumps({"angleData": [0, 90, 180, 270]}), encoding="utf-8")
    profile = parse_ard(str(path))
    assert profile.tile_count == 4
    assert profile.zero_angle_count == 1
    assert profile.midspin_count == 0


def test_parse_zip_prefers_level_over_backup(tmp_path):
    path = tmp_path / "chart.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("backup.adofai", json.dumps({"angleData": [0, 90]}))
        archive.writestr(
            "Pack/level.adofai",
            json.dumps(
                {
                    "angleData": [0, 90, 180],
                    "settings": {"song": "Pack Song", "author": "Charter"},
                }
            ),
        )
    profile = parse_ard(str(path))
    assert profile.error == ""
    assert profile.song == "Pack Song"
    assert profile.tile_count == 3


def test_parse_plain_json_level(tmp_path):
    path = tmp_path / "plain.adofai"
    path.write_text(json.dumps({"angleData": [0, 45, 90]}), encoding="utf-8")
    profile = parse_ard(str(path))
    assert profile.error == ""
    assert profile.tile_count == 3


def test_missing_file_reports_error(tmp_path):
    profile = parse_ard(str(tmp_path / "missing.ard"))
    assert "无法读取" in profile.error


def test_zip_without_level_reports_error(tmp_path):
    path = tmp_path / "empty.ard"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("audio.mp3", b"nothing")
    profile = parse_ard(str(path))
    assert "adofai" in profile.error


def test_invalid_json_reports_error(tmp_path):
    path = tmp_path / "bad.ard"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("level.adofai", b"{not-json")
    profile = parse_ard(str(path))
    assert "JSON" in profile.error


def test_parse_json_with_trailing_commas(tmp_path):
    path = tmp_path / "ado_write.adofai"
    text = (
        '{ "angleData": [0, 90, 180,],'
        ' "settings": { "song": "Song, With Comma", "author": "A", },'
        ' "actions": [ { "eventType": "Twirl", }, ], }'
    )
    path.write_text(text, encoding="utf-8")
    profile = parse_ard(str(path))
    assert profile.error == ""
    assert profile.song == "Song, With Comma"
    assert profile.author == "A"
    assert profile.tile_count == 3
    assert profile.event_summary == {"Twirl": 1}


def test_parse_plain_json_keeps_comma_in_string(tmp_path):
    path = tmp_path / "comma.adofai"
    path.write_text(
        '{ "settings": { "song": "a, }b", "author": "x", } }',
        encoding="utf-8",
    )
    profile = parse_ard(str(path))
    assert profile.error == ""
    assert profile.song == "a, }b"


def test_parse_fixes_missing_comma_between_action_objects(tmp_path):
    """真实样本 Turkish March.zip 的缺陷：两个 actions 对象之间漏了逗号。"""
    path = tmp_path / "missing_comma.adofai"
    text = (
        '{ "angleData": [0, 90],'
        ' "settings": { "song": "Comma Song", "author": "A" },'
        ' "actions": ['
        ' { "floor": 1, "eventType": "Twirl" }'
        ' { "floor": 2, "eventType": "SetSpeed" },'
        ' ] }'
    )
    path.write_text(text, encoding="utf-8")
    profile = parse_ard(str(path))
    assert profile.error == ""
    assert profile.song == "Comma Song"
    assert profile.tile_count == 2
    assert profile.event_summary == {"Twirl": 1, "SetSpeed": 1}


def test_missing_comma_repair_ignores_string_contents(tmp_path):
    """补逗号只能动结构层，字符串里的 `}{`、`"` 不能被改坏。"""
    path = tmp_path / "string_safe.adofai"
    text = (
        '{ "settings": { "song": "a}{b\\"c", "author": "x", },'
        ' "actions": [ { "eventType": "Twirl" } { "eventType": "Flash" } ] }'
    )
    path.write_text(text, encoding="utf-8")
    profile = parse_ard(str(path))
    assert profile.error == ""
    assert profile.song == 'a}{b"c'
    assert profile.event_summary == {"Twirl": 1, "Flash": 1}


def test_parse_fixes_missing_comma_between_array_values(tmp_path):
    path = tmp_path / "array_comma.adofai"
    path.write_text(
        '{ "angleData": [0 90 180], "settings": { "song": "Array" } }',
        encoding="utf-8",
    )
    profile = parse_ard(str(path))
    assert profile.error == ""
    assert profile.tile_count == 3
    assert profile.song == "Array"


def test_parse_legacy_path_data_counts_characters(tmp_path):
    """旧版只有 pathData：物量 = 字符数（不是 +1），旧格式没有中旋。"""
    path_data = "RULJ" * 20 + "N"
    path = tmp_path / "legacy.adofai"
    path.write_text(
        json.dumps(
            {
                "pathData": path_data,
                "settings": {
                    "version": 2,
                    "song": "Legacy Song",
                    "artist": "Artist",
                    "author": "Charter",
                    "bpm": 160,
                    "difficulty": 9,
                },
            }
        ),
        encoding="utf-8",
    )
    profile = parse_ard(str(path))
    assert profile.error == ""
    assert profile.tile_count == len(path_data) == 81
    assert profile.angle_len == 81
    assert profile.midspin_count == 0
    assert profile.zero_angle_count == 0
    assert profile.song == "Legacy Song"
    assert profile.author == "Charter"
    assert any("pathData" in note for note in profile.notes)


def test_parse_prefers_angle_data_over_path_data(tmp_path):
    path = tmp_path / "both.adofai"
    path.write_text(
        json.dumps({"angleData": [0, 999, 90], "pathData": "RRRRRR"}),
        encoding="utf-8",
    )
    profile = parse_ard(str(path))
    assert profile.tile_count == 3
    assert profile.midspin_count == 1


def test_parse_strips_unity_rich_text(tmp_path):
    path = tmp_path / "rich.adofai"
    path.write_text(
        json.dumps(
            {
                "settings": {
                    "song": "<color=#ffa500ff>ク</color> <size=40>Song</size>",
                    "artist": "<b>Artist</b>",
                    "author": "A<br>B",
                }
            }
        ),
        encoding="utf-8",
    )
    profile = parse_ard(str(path))
    assert profile.song == "ク Song"
    assert profile.artist == "Artist"
    assert profile.author == "A B"


def test_level_info_strips_rich_text_for_existing_entries():
    level = LevelInfo(
        song="<color=#f1f743>Ko</color><size=30>re</size>",
        artist="<i>Artist</i>",
        charter="<b>A</b> & <b>B</b>",
        difficulty="P4",
        vfxers=["<color=#fff>VFX</color>"],
    )
    assert level.song == "Kore"
    assert level.artist == "Artist"
    assert level.charter == "A & B"
    assert level.difficulty == "P4"
    assert level.vfxers == ["VFX"]


def test_profile_from_chart_data_matches_parse_ard(tmp_path):
    payload = {
        "angleData": [0, 999, 90, 180, "999"],
        "settings": {
            "version": 19,
            "bpm": 150,
            "song": "Online Song",
            "artist": "Artist",
            "author": "Charter",
            "difficulty": "Hard",
            "songFilename": "audio.ogg",
        },
        "actions": [
            {"eventType": "SetSpeed", "floor": 1, "speed": 2},
            {"eventType": "AutoPlayTiles", "floor": 2, "enabled": True},
        ],
    }
    path = tmp_path / "level.adofai"
    path.write_text(json.dumps(payload), encoding="utf-8")

    parsed = parse_ard(str(path))
    direct = profile_from_chart_data(payload, path=str(path))

    assert direct.to_dict() == parsed.to_dict()
    assert direct.tile_count == 5
    assert direct.midspin_count == 2
    assert direct.auto_tile_count == 4
    assert direct.playable_hits == parsed.playable_hits == 0


def test_profile_from_chart_bytes_matches_file_parsing(tmp_path):
    payload = {"angleData": [0, 90, 180], "settings": {"bpm": 120, "song": "Bytes"}}
    path = tmp_path / "level.adofai"
    path.write_text(json.dumps(payload), encoding="utf-8")

    parsed = parse_ard(str(path))
    direct = profile_from_chart_bytes(path.read_bytes(), path=str(path))

    assert direct.to_dict() == parsed.to_dict()
    assert direct.song == "Bytes"


def test_profile_from_chart_data_rejects_non_object():
    profile = profile_from_chart_data([1, 2, 3], path="online")

    assert not profile.tile_count
    assert "顶层不是对象" in profile.error
