"""判定两种口径（九格和 / 打击数）与中旋扣完美的单元测试。

口径照 TUF 官网 /submission/pass/calculator 前端源码：
``sumJudgements`` 九格全加，``tilecount`` 排除提前与太慢！！。
"""

from adofai_publisher.models import (
    JUDGEMENT_KEYS,
    Draft,
    adjust_perfect_for_midspin,
    empty_judgements,
    hit_count,
    judgement_hit_count,
    judgement_sum,
)


def _values(**kwargs: int) -> dict[str, int]:
    values = empty_judgements()
    values.update(kwargs)
    return values


def test_judgement_sum_adds_all_nine_cells():
    values = _values(
        perfectMinus=1,
        perfect=2,
        perfectPlus=3,
        ePerfect=4,
        lPerfect=5,
        earlyDouble=6,
        earlySingle=7,
        lateSingle=8,
        lateDouble=9,
    )
    assert judgement_sum(values) == 45


def test_hit_count_excludes_early_and_late_double():
    values = _values(
        perfect=100,
        ePerfect=10,
        lPerfect=5,
        earlySingle=3,
        lateSingle=2,
        earlyDouble=7,
        lateDouble=4,
    )
    assert judgement_sum(values) == 131
    assert judgement_hit_count(values) == 120
    assert hit_count(values) == judgement_hit_count(values)


def test_hit_count_matches_dc_screenshot_case():
    # 实测：完美 1195、稍快 25、稍慢 36、太快 9、太慢 8 → 1273
    values = _values(perfect=1195, ePerfect=25, lPerfect=36, earlySingle=9, lateSingle=8)
    assert judgement_sum(values) == 1273
    assert judgement_hit_count(values) == 1273

    # 「提前」只进九格和，不进打击数。
    values["earlyDouble"] = 1
    assert judgement_sum(values) == 1274
    assert judgement_hit_count(values) == 1273

    # 「太慢！！」同样只进九格和。
    values["lateDouble"] = 2
    assert judgement_sum(values) == 1276
    assert judgement_hit_count(values) == 1273


def test_bad_values_are_treated_as_zero():
    values = _values(perfect=10)
    values["ePerfect"] = "bad"  # type: ignore[assignment]
    values["lateDouble"] = None  # type: ignore[assignment]
    assert judgement_sum(values) == 10
    assert judgement_hit_count(values) == 10


def test_adjust_perfect_subtracts_midspins_below_3_4_0():
    values = _values(perfect=100, ePerfect=5, lateDouble=2)
    adjusted = adjust_perfect_for_midspin(values, 7, "3.3.1")
    assert adjusted["perfect"] == 93
    # 其它格原样保留，且不修改入参。
    assert adjusted["ePerfect"] == 5
    assert adjusted["lateDouble"] == 2
    assert values["perfect"] == 100
    assert set(adjusted) == set(JUDGEMENT_KEYS)


def test_adjust_perfect_keeps_perfect_for_3_4_0_and_newer():
    # TUF 前端 midspinPerfectDecrement 在 3.4.0+ 跳过（latest_era）。
    values = _values(perfect=100)
    assert adjust_perfect_for_midspin(values, 7)["perfect"] == 100
    assert adjust_perfect_for_midspin(values, 7, "3.4.0")["perfect"] == 100
    assert adjust_perfect_for_midspin(values, 7, "3.4")["perfect"] == 100
    assert adjust_perfect_for_midspin(values, 7, "3.4.0-beta")["perfect"] == 100
    assert adjust_perfect_for_midspin(values, 7, "4.0")["perfect"] == 100


def test_adjust_perfect_version_lower_than_3_4_0_decrements():
    for version in ("3.3.1", "3.3", "2.9", "v3.3.9", " 3.3.1 "):
        assert adjust_perfect_for_midspin(_values(perfect=10), 3, version)["perfect"] == 7, version


def test_adjust_perfect_unparsable_version_keeps_perfect():
    # 解析失败按 3.4.0+ 处理，不扣。
    for version in ("", "latest", None, "abc"):  # type: ignore[list-item]
        assert adjust_perfect_for_midspin(_values(perfect=10), 3, version)["perfect"] == 10, version


def test_adjust_perfect_keeps_when_perfect_below_midspins():
    values = _values(perfect=3)
    adjusted = adjust_perfect_for_midspin(values, 10, "3.3.0")
    assert adjusted["perfect"] == 3


def test_adjust_perfect_edge_and_noop_cases():
    # 正好相等时扣到 0，不会变负。
    assert adjust_perfect_for_midspin(_values(perfect=4), 4, "3.3.0")["perfect"] == 0
    # 中旋为 0 / 负数 / 非法值时不调整。
    assert adjust_perfect_for_midspin(_values(perfect=4), 0, "3.3.0")["perfect"] == 4
    assert adjust_perfect_for_midspin(_values(perfect=4), -2, "3.3.0")["perfect"] == 4
    assert adjust_perfect_for_midspin(_values(perfect=4), None, "3.3.0")["perfect"] == 4
    assert adjust_perfect_for_midspin(_values(perfect=4), "bad", "3.3.0")["perfect"] == 4  # type: ignore[arg-type]


def test_draft_round_trip_keeps_hidden_late_double():
    values = _values(perfect=1195, ePerfect=25, lateDouble=8)
    draft = Draft(judgements=values)
    restored = Draft.from_dict(draft.to_dict())
    assert restored.judgements["lateDouble"] == 8
    assert restored.judgements["perfect"] == 1195
    assert judgement_hit_count(restored.judgements) == judgement_hit_count(values)


def test_old_record_without_new_keys_loads_as_zero():
    # 老记录只有部分判定键，缺的按 0 补齐，不抛错。
    draft = Draft.from_dict({"judgements": {"perfect": 100, "unknown": 5}})
    assert set(draft.judgements) == set(JUDGEMENT_KEYS)
    assert draft.judgements["perfect"] == 100
    assert draft.judgements["lateDouble"] == 0
    assert judgement_sum(draft.judgements) == 100
    assert judgement_hit_count(draft.judgements) == 100
