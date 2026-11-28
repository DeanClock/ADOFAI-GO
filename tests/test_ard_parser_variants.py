import json
import zipfile

import pytest

from adofai_publisher.ard_parser import (
    ARDProfile,
    ChartVariant,
    _count_auto_tiles,
    _escape_control_chars_in_strings,
    find_chart_member,
    list_chart_variants,
    load_adofai_data,
    parse_ard,
)


def _write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_raw(path, text):
    """写入原文，保留真正的控制字符（json.dumps 会把它们转义掉）。"""
    path.write_bytes(text.encode("utf-8"))
    return path


# --- pathData 旧格式的中旋统计 -------------------------------------------------


def test_legacy_path_data_counts_bang_as_midspin(tmp_path):
    path_data = "RUL!JN" * 4  # 24 字符，其中 4 个 `!`
    path = _write(tmp_path / "legacy.adofai", {"pathData": path_data})
    profile = parse_ard(str(path))
    assert profile.error == ""
    assert profile.tile_count == 24
    assert profile.midspin_count == 4
    assert profile.zero_angle_count == 0
    assert profile.auto_tile_count == 0
    assert profile.playable_hits == 20


def test_legacy_path_data_note_mentions_midspin(tmp_path):
    path_data = "RU!L" * 5
    path = _write(tmp_path / "legacy_note.adofai", {"pathData": path_data})
    profile = parse_ard(str(path))
    note = " ".join(profile.notes)
    assert "pathData" in note
    assert "中旋" in note
    assert "没有中旋" not in note


def test_legacy_path_data_without_bang_keeps_full_tile_count(tmp_path):
    path_data = "RULJ" * 10
    path = _write(tmp_path / "legacy_plain.adofai", {"pathData": path_data})
    profile = parse_ard(str(path))
    assert profile.tile_count == 40
    assert profile.midspin_count == 0
    assert profile.playable_hits == 40


# --- AutoPlayTiles（自动砖） ---------------------------------------------------


@pytest.mark.parametrize(
    ("tile_count", "events", "expected"),
    [
        (10, [], 0),
        (20, [(12, False), (3, True), (10, True), (5, False)], 4),
        (10, [(3, True), (3, False)], 0),
        (10, [(3, False), (3, True)], 8),
        (10, [(2, True), (3, True), (5, False)], 3),
        (10, [(2, False), (5, False)], 0),
        (10, [(-3, True), (2, False)], 5),
        (10, [(12, True), (20, False)], 8),
        (10, [(-5, True), (100, False)], 10),
        (10, [(12, True)], 0),
        (10, [(10, True)], 1),
        (0, [(3, True)], 1),
        (0, [(3, True), (8, True)], 6),
        (0, [(3, True), (8, False)], 5),
        (-1, [(3, True), (8, True)], 6),
        (10, [(3, True), (5, False), (5, True), (7, False)], 4),
        (10, [(3, True), (5, True), (5, False)], 2),
        (10, [(3, True), (5, False), (5, False)], 2),
    ],
)
def test_autoplay_interval_accumulation_preserves_order_and_final_bounds(tile_count, events, expected):
    actions = [{"floor": floor, "eventType": "AutoPlayTiles", "enabled": enabled}
               for floor, enabled in events]
    assert _count_auto_tiles(actions, tile_count) == expected


def test_many_autoplay_intervals_preserve_profile_hit_counts(tmp_path):
    actions = []
    for index in range(1000):
        actions.extend([
            {"floor": 4 * index + 1, "eventType": "AutoPlayTiles", "enabled": True},
            {"floor": 4 * index + 3, "eventType": "AutoPlayTiles", "enabled": False},
        ])
    path = _write(tmp_path / "many-intervals.adofai", {"angleData": [0] * 4000, "actions": actions})
    profile = parse_ard(str(path))
    assert profile.tile_count == 4000
    assert profile.auto_tile_count == 2000
    assert profile.playable_hits == 2000
    assert profile.event_summary == {"AutoPlayTiles": 2000}


def test_autoplay_tiles_ranges_exclude_disable_floor(tmp_path):
    path = _write(
        tmp_path / "autoplay.adofai",
        {
            "angleData": [0] * 20,
            "actions": [
                {"floor": 3, "eventType": "AutoPlayTiles", "enabled": True},
                {"floor": 5, "eventType": "AutoPlayTiles", "enabled": False},
                {"floor": 10, "eventType": "AutoPlayTiles", "enabled": True},
                {"floor": 12, "eventType": "AutoPlayTiles", "enabled": False},
            ],
        },
    )
    profile = parse_ard(str(path))
    # [3,5) = 2 格，[10,12) = 2 格；关闭事件所在的 5、12 不算自动砖。
    assert profile.auto_tile_count == 4
    assert profile.tile_count == 20
    assert profile.playable_hits == 16


def test_autoplay_tiles_open_without_close_runs_to_last_tile(tmp_path):
    path = _write(
        tmp_path / "autoplay_open.adofai",
        {
            "angleData": [0] * 10,
            "actions": [
                {"floor": 8, "eventType": "AutoPlayTiles", "enabled": True},
            ],
        },
    )
    profile = parse_ard(str(path))
    # 只开不关：楼层 8、9、10 共 3 格。
    assert profile.auto_tile_count == 3
    assert profile.playable_hits == 7


def test_autoplay_tiles_ignores_stray_disable_and_bad_floors(tmp_path):
    path = _write(
        tmp_path / "autoplay_stray.adofai",
        {
            "angleData": [0] * 10,
            "actions": [
                {"floor": 4, "eventType": "AutoPlayTiles", "enabled": False},
                {"floor": "bad", "eventType": "AutoPlayTiles", "enabled": True},
                {"floor": 6, "eventType": "Twirl"},
            ],
        },
    )
    profile = parse_ard(str(path))
    assert profile.auto_tile_count == 0
    assert profile.playable_hits == 10


def test_autoplay_tiles_support_legacy_path_data(tmp_path):
    path_data = "RULJ" * 5  # 20 格
    path = _write(
        tmp_path / "autoplay_legacy.adofai",
        {
            "pathData": path_data,
            "actions": [
                {"floor": 15, "eventType": "AutoPlayTiles", "enabled": True},
            ],
        },
    )
    profile = parse_ard(str(path))
    assert profile.tile_count == 20
    assert profile.auto_tile_count == 6
    assert profile.playable_hits == 14


def test_autoplay_tiles_missing_enabled_flag_counts_as_open(tmp_path):
    path = _write(
        tmp_path / "autoplay_default.adofai",
        {
            "angleData": [0] * 5,
            "actions": [
                {"floor": 1, "eventType": "AutoPlayTiles"},
                {"floor": 3, "eventType": "AutoPlayTiles", "enabled": False},
            ],
        },
    )
    profile = parse_ard(str(path))
    assert profile.auto_tile_count == 2


def test_autoplay_tiles_inactive_open_event_is_ignored(tmp_path):
    path = _write(
        tmp_path / "autoplay_inactive.adofai",
        {
            "angleData": [0] * 40,
            "actions": [
                {"floor": 1, "eventType": "AutoPlayTiles", "active": False, "enabled": True},
                {"floor": 27, "eventType": "AutoPlayTiles", "enabled": False},
            ],
        },
    )
    profile = parse_ard(str(path))
    # floor 1 未生效，不开；floor 27 是孤立的关闭事件，不关。
    assert profile.auto_tile_count == 0
    assert profile.playable_hits == 40
    # event_summary 只统计「写了什么」，不受 active 影响。
    assert profile.event_summary == {"AutoPlayTiles": 2}


def test_autoplay_tiles_active_true_counts_normally(tmp_path):
    path = _write(
        tmp_path / "autoplay_active.adofai",
        {
            "angleData": [0] * 10,
            "actions": [
                {"floor": 2, "eventType": "AutoPlayTiles", "active": True, "enabled": True},
                {"floor": 5, "eventType": "AutoPlayTiles", "active": True, "enabled": False},
            ],
        },
    )
    profile = parse_ard(str(path))
    assert profile.auto_tile_count == 3


def test_autoplay_tiles_missing_active_counts_as_open(tmp_path):
    path = _write(
        tmp_path / "autoplay_no_active.adofai",
        {
            "angleData": [0] * 10,
            "actions": [
                {"floor": 4, "eventType": "AutoPlayTiles", "enabled": True},
                {"floor": 6, "eventType": "AutoPlayTiles", "enabled": False},
            ],
        },
    )
    profile = parse_ard(str(path))
    assert profile.auto_tile_count == 2


def test_autoplay_tiles_string_false_active_is_ignored(tmp_path):
    path = _write(
        tmp_path / "autoplay_string_false.adofai",
        {
            "angleData": [0] * 10,
            "actions": [
                {"floor": 1, "eventType": "AutoPlayTiles", "active": " FALSE ", "enabled": True},
                {"floor": 3, "eventType": "AutoPlayTiles", "active": 0, "enabled": True},
                {"floor": 5, "eventType": "AutoPlayTiles", "active": "true", "enabled": True},
            ],
        },
    )
    profile = parse_ard(str(path))
    # 只有 floor 5（active "true"）真正开启，覆盖到最后一格：10-5+1 = 6。
    assert profile.auto_tile_count == 6


def test_autoplay_tiles_inactive_disable_does_not_close_open_range(tmp_path):
    path = _write(
        tmp_path / "autoplay_inactive_disable.adofai",
        {
            "angleData": [0] * 10,
            "actions": [
                {"floor": 2, "eventType": "AutoPlayTiles", "active": True, "enabled": True},
                {"floor": 4, "eventType": "AutoPlayTiles", "active": False, "enabled": False},
                {"floor": 9, "eventType": "AutoPlayTiles", "active": True, "enabled": False},
            ],
        },
    )
    profile = parse_ard(str(path))
    # floor 4 未生效，不关闭；区间 [2, 9) = 7 格。
    assert profile.auto_tile_count == 7


def test_autoplay_tiles_inactive_event_matches_reported_level(tmp_path):
    # 对应实报的 Bismuth (729).zip：3,355 格、12 中旋、自动砖 0、可击 3,343。
    path = _write(
        tmp_path / "bismuth_like.adofai",
        {
            "angleData": [999] * 12 + [0] * (3355 - 12),
            "actions": [
                {"floor": 1, "eventType": "AutoPlayTiles", "active": False, "enabled": True},
                {"floor": 27, "eventType": "AutoPlayTiles", "enabled": False},
            ],
        },
    )
    profile = parse_ard(str(path))
    assert profile.tile_count == 3355
    assert profile.midspin_count == 12
    assert profile.auto_tile_count == 0
    assert profile.playable_hits == 3343


# --- playable_hits -------------------------------------------------------------


def test_playable_hits_subtracts_midspins_and_autos(tmp_path):
    path = _write(
        tmp_path / "playable.adofai",
        {
            "angleData": [999, 0, 0, 999, 0, 0, 0, 0],
            "actions": [
                {"floor": 2, "eventType": "AutoPlayTiles", "enabled": True},
                {"floor": 3, "eventType": "AutoPlayTiles", "enabled": False},
            ],
        },
    )
    profile = parse_ard(str(path))
    assert profile.tile_count == 8
    assert profile.midspin_count == 2
    assert profile.auto_tile_count == 1
    assert profile.playable_hits == 5


def test_playable_hits_never_negative():
    profile = ARDProfile(tile_count=3, midspin_count=2, auto_tile_count=5)
    assert profile.playable_hits == 0
    assert ARDProfile().playable_hits == 0


def test_profile_to_dict_includes_auto_tile_count():
    profile = ARDProfile(tile_count=10, midspin_count=2, auto_tile_count=3)
    data = profile.to_dict()
    assert data["auto_tile_count"] == 3
    assert "playable_hits" not in data
    assert ARDProfile(**data).auto_tile_count == 3


# --- 重复逗号容错 --------------------------------------------------------------


def test_duplicate_commas_are_folded(tmp_path):
    path = tmp_path / "double_comma.adofai"
    path.write_text(
        '{ "angleData": [0, 90, 180],'
        ' "settings": { "song": "Song", "difficulty": 1,, "author": "A" } }',
        encoding="utf-8",
    )
    profile = parse_ard(str(path))
    assert profile.error == ""
    assert profile.tile_count == 3
    assert profile.difficulty == 1
    assert profile.song == "Song"


def test_duplicate_comma_before_closing_brace_and_trailing_comma(tmp_path):
    path = tmp_path / "double_comma_close.adofai"
    path.write_text(
        '{ "angleData": [0, 90],'
        ' "settings": { "song": "S", "difficulty": 2,, }, }',
        encoding="utf-8",
    )
    profile = parse_ard(str(path))
    assert profile.error == ""
    assert profile.difficulty == 2
    assert profile.tile_count == 2


def test_duplicate_commas_inside_strings_are_kept(tmp_path):
    path = tmp_path / "comma_in_string.adofai"
    path.write_text(
        '{ "angleData": [0,, 90],'
        ' "settings": { "song": "a,,b", "author": "x,,y" } }',
        encoding="utf-8",
    )
    profile = parse_ard(str(path))
    assert profile.error == ""
    assert profile.tile_count == 2
    assert profile.song == "a,,b"
    assert profile.author == "x,,y"


# --- 字符串内裸控制字符容错（TUF #10551） ---------------------------------------


def test_raw_crlf_inside_string_is_escaped_and_kept(tmp_path):
    # levelDesc 里是真实的 CR + 转义序列 `\n`，与 TUF #10551 的 main.adofai 同形。
    text = (
        "{\n"
        '  "settings": {\n'
        '    "song": "M.I.L.F",\n'
        '    "version": 15,\n'
        '    "levelDesc": "――PUT YOUR HANDS IN THE AIR RIGHT NOW!\r\\n'
        'The assets are borrowed\r\\n\r\\n[Duration] 2:05"\n'
        "  },\n"
        '  "angleData": [0, 0, 0, 45, 90]\n'
        "}\n"
    )
    path = _write_raw(tmp_path / "bare_cr.adofai", text)
    data, error = load_adofai_data(str(path))
    assert error == ""
    assert data["settings"]["song"] == "M.I.L.F"
    assert data["settings"]["version"] == 15
    assert data["settings"]["levelDesc"].startswith("――PUT YOUR HANDS")
    # 裸 CR 和转义 `\n` 都还原成换行，不会被吃掉。
    assert data["settings"]["levelDesc"].count("\r\n") == 3
    profile = parse_ard(str(path))
    assert profile.error == ""
    assert profile.tile_count == 5


def test_raw_lf_and_tab_inside_string_are_escaped(tmp_path):
    text = '{"settings": {"song": "a\tb\nc"}}'
    path = _write_raw(tmp_path / "bare_lf_tab.adofai", text)
    data, error = load_adofai_data(str(path))
    assert error == ""
    assert data["settings"]["song"] == "a\tb\nc"


def test_other_control_chars_become_unicode_escapes(tmp_path):
    text = '{"settings": {"song": "a\x01b\x1fc"}}'
    path = _write_raw(tmp_path / "bare_ctrl.adofai", text)
    data, error = load_adofai_data(str(path))
    assert error == ""
    assert data["settings"]["song"] == "a\x01b\x1fc"


def test_control_char_repair_keeps_whitespace_outside_strings():
    text = '{\r\n\t"a": "x\r\ny"\r\n}\r\n'
    fixed = _escape_control_chars_in_strings(text)
    # 字符串外的 CR/LF/TAB 是合法空白，原样保留；字符串内的才转义。
    assert fixed == '{\r\n\t"a": "x\\r\\ny"\r\n}\r\n'


def test_raw_cr_combines_with_comma_repairs(tmp_path):
    text = (
        '{ "angleData": [0, 90,, 180],\n'
        '  "settings": { "song": "line1\rline2", "difficulty": 3, } }'
    )
    path = _write_raw(tmp_path / "cr_and_commas.adofai", text)
    data, error = load_adofai_data(str(path))
    assert error == ""
    # 换行保留在 JSON 值里（profile.song 会被 strip_rich_text 折成空格，属正常展示行为）。
    assert data["settings"]["song"] == "line1\rline2"
    profile = parse_ard(str(path))
    assert profile.error == ""
    assert profile.difficulty == 3
    assert profile.tile_count == 3


def test_raw_cr_near_escaped_quote_keeps_string_boundary(tmp_path):
    # 值里先有一个转义引号，再是裸 CR/LF：不能把 `\"` 当成字符串结束。
    text = '{"settings": {"song": "a\\"b\r\nc"}}'
    path = _write_raw(tmp_path / "cr_escaped_quote.adofai", text)
    data, error = load_adofai_data(str(path))
    assert error == ""
    assert data["settings"]["song"] == 'a"b\r\nc'


def test_raw_cr_after_escaped_backslash_keeps_string_boundary(tmp_path):
    # `\\` 是两个字符的转义反斜杠，后面的引号才是字符串结束。
    text = '{"settings": {"song": "a\\\\\r\nb"}}'
    path = _write_raw(tmp_path / "cr_escaped_backslash.adofai", text)
    data, error = load_adofai_data(str(path))
    assert error == ""
    assert data["settings"]["song"] == "a\\\r\nb"


def test_normal_json_is_parsed_without_sanitizing(tmp_path):
    path = _write(tmp_path / "normal.adofai", {"angleData": [0, 90], "settings": {"song": "S"}})
    data, error = load_adofai_data(str(path))
    assert error == ""
    assert data["settings"]["song"] == "S"
    # 正常谱面不会被重写：helper 原样返回同一个对象。
    text = path.read_text(encoding="utf-8")
    assert _escape_control_chars_in_strings(text) is text


def test_cr_chart_with_broken_json_still_reports_error(tmp_path):
    text = '{"settings": {"song": "a\rb"'
    path = _write_raw(tmp_path / "cr_broken.adofai", text)
    data, error = load_adofai_data(str(path))
    assert data is None
    assert "谱面 JSON 解析失败" in error


# --- list_chart_variants -------------------------------------------------------


def _make_variant_zip(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "Pack/backup.adofai",
            json.dumps({"angleData": [0, 90], "settings": {"song": "Backup"}}),
        )
        archive.writestr(
            "Pack/main nerf.adofai",
            json.dumps(
                {
                    "angleData": [0, 999, 90, 90],
                    "settings": {"song": "Main Nerf", "author": "A", "version": 19},
                }
            ),
        )
        archive.writestr(
            "Pack/level.adofai",
            json.dumps(
                {
                    "angleData": [0, 0, 90],
                    "settings": {
                        "song": "Level",
                        "artist": "Artist",
                        "author": "Charter",
                        "version": 19,
                    },
                    "actions": [
                        {"floor": 2, "eventType": "AutoPlayTiles", "enabled": True},
                        {"floor": 3, "eventType": "AutoPlayTiles", "enabled": False},
                    ],
                }
            ),
        )
        archive.writestr("Pack/broken.adofai", b"{not-json")
        archive.writestr("__MACOSX/Pack/._level.adofai", b"\x00\x05\x16\x07")
    return path


def test_list_chart_variants_enumerates_and_sorts(tmp_path):
    path = _make_variant_zip(tmp_path / "variants.zip")
    variants = list_chart_variants(path)
    assert [v.member for v in variants] == [
        "Pack/level.adofai",
        "Pack/main nerf.adofai",
        "Pack/broken.adofai",
        "Pack/backup.adofai",
    ]
    assert all(isinstance(v, ChartVariant) for v in variants)

    level, nerf, broken, backup = variants
    assert level.error == ""
    assert (level.tile_count, level.midspin_count, level.auto_tile_count) == (3, 0, 1)
    assert level.playable_hits == 2
    assert level.song == "Level"
    assert level.artist == "Artist"
    assert level.author == "Charter"
    assert level.version == 19

    assert (nerf.tile_count, nerf.midspin_count, nerf.playable_hits) == (4, 1, 3)
    assert nerf.song == "Main Nerf"

    assert broken.error != ""
    assert broken.tile_count == 0

    assert backup.song == "Backup"


def test_list_chart_variants_prefers_level_and_puts_backup_last(tmp_path):
    path = _make_variant_zip(tmp_path / "variants_sort.zip")
    names = [v.member for v in list_chart_variants(path)]
    assert names[0].endswith("level.adofai")
    assert names[-1].endswith("backup.adofai")
    assert find_chart_member(names) == "Pack/level.adofai"


def test_list_chart_variants_to_dict(tmp_path):
    path = _make_variant_zip(tmp_path / "variants_dict.zip")
    data = list_chart_variants(path)[0].to_dict()
    assert data["member"] == "Pack/level.adofai"
    assert data["tile_count"] == 3
    assert data["auto_tile_count"] == 1
    assert data["playable_hits"] == 2


def test_list_chart_variants_reads_folder(tmp_path):
    folder = tmp_path / "charts"
    (folder / "sub").mkdir(parents=True)
    _write(folder / "main.adofai", {"angleData": [0, 90], "settings": {"song": "Main"}})
    _write(
        folder / "sub" / "level.adofai",
        {"angleData": [0, 999, 90], "settings": {"song": "Level"}},
    )
    _write(folder / "backup.adofai", {"angleData": [0], "settings": {"song": "Backup"}})
    variants = list_chart_variants(folder)
    assert [v.member for v in variants] == [
        "sub/level.adofai",
        "main.adofai",
        "backup.adofai",
    ]
    assert variants[0].song == "Level"
    assert variants[0].midspin_count == 1


def test_list_chart_variants_accepts_single_file(tmp_path):
    path = _write(tmp_path / "single.adofai", {"angleData": [0, 90, 180]})
    variants = list_chart_variants(path)
    assert len(variants) == 1
    assert variants[0].member == "single.adofai"
    assert variants[0].tile_count == 3


def test_list_chart_variants_missing_path_returns_empty(tmp_path):
    assert list_chart_variants(tmp_path / "nope.zip") == []
    assert list_chart_variants(tmp_path / "nope_dir") == []


def test_list_chart_variants_zip_without_charts_is_empty(tmp_path):
    path = tmp_path / "audio_only.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("audio.ogg", b"nothing")
    assert list_chart_variants(path) == []
