from collections import Counter

import pytest

import adofai_publisher.batch_match as batch_match
from adofai_publisher.batch_match import (
    LocalNumbers,
    check_numeric,
    classify_candidates,
    describe_check,
    format_bpm,
    format_level_quad,
    format_local_quad,
    local_missing,
    local_numbers,
    numeric_queries,
)
from adofai_publisher.models import LevelInfo


def _entry(**overrides):
    entry = {
        "key": "folder:x",
        "song": "Time Leaper",
        "path": "C:/levels/x.adofai",
        "bpm": 350.0,
        "tilecount": 2629,
        "midspin_count": 25,
        "auto_tile_count": 0,
        "playable_hits": 2604,
        "parse_error": "",
    }
    entry.update(overrides)
    return entry


def _tuf(
    level_id=10808,
    song="Time Leaper",
    bpm=350,
    tilecount=2604,
    midspin=25,
    auto=0,
    drop=(),
):
    raw = {
        "id": level_id,
        "song": song,
        "bpm": bpm,
        "tilecount": tilecount,
        "midspinCount": midspin,
        "autoTileCount": auto,
    }
    for key in drop:
        raw.pop(key, None)
    return LevelInfo.from_tuf(raw)


def test_local_numbers_converts_tilecount_to_tuf_units():
    local = local_numbers(_entry())

    assert local is not None
    assert local.bpm == 350.0
    assert local.tilecount == 2604  # 2629 总格 − 25 中旋
    assert local.midspin == 25
    assert local.auto == 0
    assert local.total_tilecount == 2629


def test_local_numbers_rejects_parse_error_and_old_entries():
    assert local_numbers(_entry(parse_error="谱面文件为空")) is None
    assert local_numbers(_entry(playable_hits=None)) is None
    assert local_numbers(None) is None


def test_local_numbers_rejects_inconsistent_numbers():
    assert local_numbers(_entry(playable_hits=2600)) is None
    assert local_numbers(_entry(bpm=0)) is None
    assert local_numbers(_entry(tilecount=0)) is None
    assert local_numbers(_entry(midspin_count=-1)) is None
    assert local_numbers(_entry(tilecount=25, playable_hits=0)) is None


def test_local_missing_names_the_rejected_fields():
    assert local_missing(_entry()) == []
    assert local_missing(_entry(parse_error="坏了")) == ["解析失败"]
    assert local_missing(_entry(playable_hits=None)) == ["可击物量"]
    assert local_missing(_entry(playable_hits=2600)) == ["数值不自洽"]
    entry = _entry()
    entry.pop("auto_tile_count")
    assert "自动砖" in local_missing(entry)


@pytest.mark.parametrize(
    ("entry", "reasons"),
    [
        (None, ["本地条目"]),
        ([], ["本地条目"]),
        (_entry(parse_error=" 坏了 ", bpm=None), ["解析失败"]),
        (
            _entry(
                bpm=0,
                tilecount=None,
                midspin_count=-1,
                auto_tile_count="bad",
                playable_hits=None,
            ),
            ["BPM", "物量", "中旋", "自动砖", "可击物量"],
        ),
        # 字段无效优先于自洽检查；自洽检查优先于 TUF 物量检查。
        (_entry(bpm=0, playable_hits=1), ["BPM"]),
        (_entry(tilecount=25, playable_hits=1), ["数值不自洽"]),
        (_entry(tilecount=25, playable_hits=0), ["物量"]),
        (_entry(tilecount=24, playable_hits=-1), ["物量"]),
    ],
)
def test_local_validation_preserves_reason_order_and_priority(entry, reasons):
    assert local_numbers(entry) is None
    missing = local_missing(entry)
    assert isinstance(missing, list)
    assert missing == reasons

    match = classify_candidates(entry, [_tuf()])

    assert match.local is None
    assert isinstance(match.local_missing, tuple)
    assert match.local_missing == tuple(reasons)
    assert match.kind == "name_only"
    assert match.auto_check is False
    assert match.equal_count == 0


@pytest.mark.parametrize(
    ("field", "label"),
    [
        ("bpm", "BPM"),
        ("tilecount", "物量"),
        ("midspin_count", "中旋"),
        ("auto_tile_count", "自动砖"),
        ("playable_hits", "可击物量"),
    ],
)
@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"), float("-inf")])
def test_local_validation_rejects_boolean_and_nonfinite_fields(field, label, value):
    entry = _entry(**{field: value})

    assert local_numbers(entry) is None
    assert local_missing(entry) == [label]

    match = classify_candidates(entry, [_tuf()])
    assert match.local is None
    assert match.local_missing == (label,)
    assert not match.auto_check


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        # 各字段保留 float 转换和 round 的半偶数舍入。
        (
            dict(
                bpm="240.12345",
                tilecount="10.5",
                midspin_count="2.5",
                auto_tile_count="1.5",
                playable_hits="6.5",
            ),
            LocalNumbers(240.12345, 8, 2, 2, 10),
        ),
        (
            dict(tilecount=11.5, midspin_count=1.5, auto_tile_count=0.5, playable_hits=10.5),
            LocalNumbers(350.0, 10, 2, 0, 12),
        ),
        # 可击物量为零或负值时，旧规则仍只检查等式和正的 TUF 物量。
        (
            dict(tilecount=3, midspin_count=2, auto_tile_count=1, playable_hits=0),
            LocalNumbers(350.0, 1, 2, 1, 3),
        ),
        (
            dict(tilecount=3, midspin_count=2, auto_tile_count=2, playable_hits=-1),
            LocalNumbers(350.0, 1, 2, 2, 3),
        ),
    ],
)
def test_local_validation_preserves_rounding_and_tilecount_units(overrides, expected):
    entry = _entry(parse_error=" \t ", **overrides)

    assert local_numbers(entry) == expected
    assert local_missing(entry) == []

    candidate = _tuf(
        bpm=expected.bpm,
        tilecount=expected.tilecount,
        midspin=expected.midspin,
        auto=expected.auto,
    )
    match = classify_candidates(entry, [candidate])

    assert match.local == expected
    assert match.local_missing == ()
    assert match.kind == "numeric_unique"
    assert match.selected is candidate
    assert match.auto_check is True


@pytest.mark.parametrize("playable", [2604, None, 2600])
def test_classify_candidates_reads_local_numeric_fields_once(playable):
    class CountingEntry(dict):
        def get(self, key, default=None):
            reads[key] += 1
            return super().get(key, default)

    reads = Counter()
    entry = CountingEntry(_entry(playable_hits=playable))

    match = classify_candidates(entry, [_tuf()])

    for field in ("bpm", "tilecount", "midspin_count", "auto_tile_count", "playable_hits"):
        assert reads[field] == 1
    assert match.local_missing == {
        2604: (),
        None: ("可击物量",),
        2600: ("数值不自洽",),
    }[playable]
    assert match.auto_check is (playable == 2604)


def test_format_bpm_avoids_float_noise_and_scientific_notation():
    assert format_bpm(240.0) == "240"
    assert format_bpm(240.5) == "240.5"
    assert format_bpm(240.1234) == "240.123"
    assert format_bpm(240.0004) == "240"
    assert format_bpm(0.00001) == "0"
    assert format_bpm("abc") == ""
    assert format_bpm(None) == ""


def test_numeric_queries_put_tilecount_before_bpm_only():
    local = local_numbers(_entry())

    assert numeric_queries(local) == [
        "bpm=350, tilecount=2604",
        "bpm=350",
    ]
    assert numeric_queries(None) == []


def test_check_numeric_requires_fields_to_exist_in_raw():
    local = local_numbers(_entry())

    equal = check_numeric(_tuf(), local)
    assert equal.complete and equal.equal
    assert describe_check(equal) == "✓ 数值全等"

    missing_midspin = check_numeric(_tuf(drop=("midspinCount",)), local)
    assert not missing_midspin.complete
    assert missing_midspin.missing == ("中旋",)
    assert "缺 中旋" in describe_check(missing_midspin)

    missing_auto = check_numeric(_tuf(drop=("autoTileCount",)), local)
    assert not missing_auto.complete
    assert missing_auto.missing == ("自动砖",)


def test_check_numeric_bpm_uses_three_decimal_tolerance():
    local = local_numbers(_entry(bpm=240.0))

    near = _tuf(bpm=240.0004, tilecount=2604, midspin=25, auto=0)
    assert check_numeric(near, local).equal

    off = _tuf(bpm=240.001, tilecount=2604, midspin=25, auto=0)
    result = check_numeric(off, local)
    assert result.complete and not result.equal
    assert result.mismatch == ("BPM 240.001≠240",)


def test_check_numeric_reports_each_mismatched_field():
    local = local_numbers(_entry())
    result = check_numeric(
        _tuf(tilecount=2605, midspin=24, auto=1), local
    )

    assert result.complete and not result.equal
    assert "物量 2605≠2604" in result.mismatch
    assert "中旋 24≠25" in result.mismatch
    assert "自动砖 1≠0" in result.mismatch


def test_unique_numeric_match_autochecks_even_with_zero_name_similarity():
    match = classify_candidates(_entry(song="AAAA"), [_tuf(song="ZZZZ")])

    assert match.kind == "numeric_unique"
    assert match.auto_check is True
    assert match.selected is not None
    assert match.selected.tuf_id == 10808
    assert match.equal_count == 1


def test_multiple_numeric_matches_preselect_best_name_but_do_not_autocheck():
    match = classify_candidates(
        _entry(song="Time Leaper"),
        [
            _tuf(level_id=2, song="Completely Different"),
            _tuf(level_id=1, song="Time Leaper"),
        ],
    )

    assert match.kind == "numeric_multi"
    assert match.auto_check is False
    assert match.equal_count == 2
    assert match.selected is not None
    assert match.selected.tuf_id == 1
    assert [item.level.tuf_id for item in match.candidates[:2]] == [1, 2]


def test_name_only_match_never_autochecks():
    match = classify_candidates(
        _entry(song="Time Leaper"),
        [_tuf(song="Time Leaper", bpm=300, tilecount=2000, midspin=0, auto=0)],
    )

    assert match.kind == "name_only"
    assert match.auto_check is False
    assert match.selected is not None
    assert match.selected.tuf_id == 10808
    assert match.best_score == 1.0


def test_name_only_with_low_similarity_does_not_preselect():
    match = classify_candidates(
        _entry(song="Time Leaper"),
        [_tuf(song="Zzzz", bpm=300, tilecount=2000, midspin=0, auto=0)],
    )

    assert match.kind == "name_only"
    assert match.selected is None
    assert match.auto_check is False


def test_candidates_with_incomplete_numbers_are_not_autochecked():
    match = classify_candidates(
        _entry(),
        [_tuf(drop=("midspinCount", "autoTileCount"))],
    )

    assert match.kind == "name_only"
    assert match.incomplete_only is True
    assert match.auto_check is False
    assert match.candidates[0].check.missing == ("中旋", "自动砖")


def test_candidates_sorted_equal_first_and_menu_is_capped():
    levels = [
        _tuf(level_id=index + 1, song=f"Song {index}")
        for index in range(25)
    ]
    levels.append(
        _tuf(level_id=999, song="Time Leaper", bpm=1, tilecount=1, midspin=0, auto=0)
    )

    match = classify_candidates(_entry(), levels, max_menu=20)

    assert match.kind == "numeric_multi"
    assert match.equal_count == 25
    assert len(match.candidates) == 20
    assert all(item.equal for item in match.candidates)


def test_duplicate_tuf_ids_are_removed():
    level = _tuf()

    match = classify_candidates(_entry(), [level, level])

    assert match.kind == "numeric_unique"
    assert len(match.candidates) == 1


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_tuf_id_keeps_first_objects_name_and_numbers(reverse):
    equal = _tuf(song="Time Leaper")
    different = _tuf(song="ZZZZ", bpm=300, tilecount=2000, midspin=0, auto=1)
    levels = [equal, different]
    if reverse:
        levels.reverse()

    match = classify_candidates(_entry(), levels)

    assert len(match.candidates) == 1
    assert match.candidates[0].level is levels[0]
    assert match.candidates[0].score == (0.0 if reverse else 1.0)
    assert match.candidates[0].check.complete
    assert match.candidates[0].equal is (not reverse)
    assert match.equal_count == (0 if reverse else 1)
    assert match.kind == ("name_only" if reverse else "numeric_unique")
    assert match.selected is (None if reverse else equal)
    assert match.auto_check is (not reverse)


@pytest.mark.parametrize("numeric_equal", [False, True])
def test_same_score_candidates_sort_by_id_in_each_numeric_group(numeric_equal):
    levels = [
        _tuf(level_id=number, bpm=350 if numeric_equal else 300)
        for number in (9, 3, 1)
    ]

    match = classify_candidates(_entry(), levels)

    assert [item.level.tuf_id for item in match.candidates] == [1, 3, 9]
    assert [item.score for item in match.candidates] == [1.0, 1.0, 1.0]
    assert match.selected is levels[-1]
    assert match.kind == ("numeric_multi" if numeric_equal else "name_only")
    assert match.equal_count == (3 if numeric_equal else 0)
    assert match.auto_check is False


@pytest.mark.parametrize(
    ("song", "score", "preselected"),
    [("abcdefwxyz", 0.6, True), ("abcdefwxyzz", 12 / 21, False)],
)
def test_name_preselection_at_real_similarity_threshold(song, score, preselected):
    candidate = _tuf(song=song, bpm=300)

    match = classify_candidates(_entry(song="abcdefghij"), [candidate])

    assert match.candidates[0].score == score
    assert match.best_score == score
    assert match.kind == "name_only"
    assert match.selected is (candidate if preselected else None)
    assert match.equal_count == 0
    assert match.auto_check is False


@pytest.mark.parametrize("equal_count", [0, 1, 3])
@pytest.mark.parametrize("max_menu", [1, 2])
def test_small_menu_preserves_full_decision_and_counts_visible_incomplete(
    equal_count, max_menu
):
    equal = [
        _tuf(level_id=number, song="ZZZZ")
        for number in range(1, equal_count + 1)
    ]
    others = [
        _tuf(level_id=100, drop=("midspinCount",)),
        _tuf(level_id=101, bpm=300),
        _tuf(level_id=102, song="ZZZZ", drop=("autoTileCount",)),
    ]

    match = classify_candidates(_entry(), list(reversed(equal)) + others, max_menu=max_menu)

    visible_ids = ([level.tuf_id for level in equal] + [100, 101, 102])[:max_menu]
    assert [item.level.tuf_id for item in match.candidates] == visible_ids
    assert match.equal_count == equal_count
    assert match.kind == {0: "name_only", 1: "numeric_unique", 3: "numeric_multi"}[equal_count]
    assert match.selected is (equal[0] if equal else others[0])
    assert match.auto_check is (equal_count == 1)
    # 数值全等组优先，因此菜单首项不一定是名称分数最高的候选。
    assert match.best_score == (0.0 if equal else 1.0)
    assert match.incomplete_count == sum(number in (100, 102) for number in visible_ids)


def test_no_candidates_returns_none_kind():
    match = classify_candidates(_entry(), [])

    assert match.kind == "none"
    assert match.candidates == []
    assert match.selected is None
    assert match.auto_check is False


@pytest.mark.parametrize(
    ("left", "right", "score"),
    [
        (" TiME - Leaper!", "time_leaper", 1.0),
        ("Straße", "STRASSE", 1.0),
        (" \t-_", "", 0.0),
        ("", "song", 0.0),
        ("song", "", 0.0),
        ("tide", "diet", 0.25),
        ("diet", "tide", 0.5),
        # 保留 SequenceMatcher 默认的 autojunk 行为。
        ("a" * 200 + "b", "b" + "a" * 200, 1 / 201),
    ],
)
def test_similarity_preserves_normalization_direction_and_defaults(left, right, score):
    assert batch_match._similarity(left, right) == score


@pytest.mark.parametrize(
    ("entry", "song", "score", "preselected"),
    [
        (_entry(song=" TiME - Leaper!"), "time_leaper", 1.0, True),
        (_entry(song="Straße"), "STRASSE", 1.0, True),
        (_entry(song="<b>Time Leaper</b>"), "time_leaper", 1.0, True),
        (
            _entry(song="", path="C:/levels/Time-Leaper.adofai"),
            "time_leaper",
            1.0,
            True,
        ),
        (_entry(song=" \t", path=""), "song", 0.0, False),
        (_entry(song="song"), "", 0.0, False),
        (_entry(song="", path=""), "", 0.0, False),
        (_entry(song="tide"), "diet", 0.25, False),
        (_entry(song="diet"), "tide", 0.5, False),
    ],
)
def test_name_scoring_preserves_complete_row_decision(entry, song, score, preselected):
    candidate = _tuf(song=song, bpm=300)

    match = classify_candidates(entry, [candidate])

    assert match == batch_match.RowMatch(
        kind="name_only",
        candidates=[
            batch_match.Candidate(
                level=candidate,
                score=score,
                check=batch_match.NumericCheck(
                    complete=True, equal=False, mismatch=("BPM 300≠350",)
                ),
            )
        ],
        selected=candidate if preselected else None,
        auto_check=False,
        local=LocalNumbers(350.0, 2604, 25, 0, 2629),
        local_missing=(),
        best_score=score,
        equal_count=0,
        incomplete_count=0,
    )


def test_each_row_normalizes_local_name_once_and_only_scores_unique_candidates(monkeypatch):
    local_name = "LOCAL Song!"
    names = ["local_song", "Local-Song", "local song"]
    levels = [_tuf(level_id=number, song=name) for number, name in enumerate(names, 1)]
    duplicate = _tuf(level_id=1, song="Duplicate must not be scored")
    calls = []
    normalize = batch_match._normalize

    def record_normalize(text):
        calls.append(text)
        return normalize(text)

    monkeypatch.setattr(batch_match, "_normalize", record_normalize)

    for _ in range(2):
        match = classify_candidates(
            _entry(song=local_name),
            [None, _tuf(level_id=0), levels[0], duplicate, *levels[1:]],
        )
        assert [item.level for item in match.candidates] == levels
        assert [item.score for item in match.candidates] == [1.0, 1.0, 1.0]
        assert match.kind == "numeric_multi"
        assert match.equal_count == 3
        assert match.selected is levels[0]
        assert match.auto_check is False

    # 每行独立规范化一次本地名；无效项和重复 ID 不触发候选名规范化。
    assert calls == [local_name, *names] * 2


def test_local_missing_blocks_numeric_matching():
    match = classify_candidates(
        _entry(parse_error="坏了"),
        [_tuf()],
    )

    assert match.local is None
    assert match.kind == "name_only"
    assert match.auto_check is False
    assert match.local_missing == ("解析失败",)


def test_tooltip_formatting_helpers():
    local = local_numbers(_entry())

    assert format_local_quad(local) == "350 / 2629 / 25 / 0"
    assert format_level_quad(_tuf()) == "350 / 2604 / 25 / 0"
    assert format_level_quad(_tuf(drop=("midspinCount",))) == "350 / 2604 / ? / 0"
    assert isinstance(local, LocalNumbers)
