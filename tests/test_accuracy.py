from adofai_publisher.accuracy import calc_accuracy, calc_accuracy_ratio, format_accuracy
from adofai_publisher.models import empty_judgements


def test_all_perfect_is_100_percent():
    values = empty_judgements()
    values["perfect"] = 1000
    assert calc_accuracy(values) == 100.0


def test_eperfect_weight_is_075():
    values = empty_judgements()
    values["ePerfect"] = 200
    assert calc_accuracy(values) == 75.0


def test_single_weight_is_04():
    values = empty_judgements()
    values["earlySingle"] = 100
    values["lateSingle"] = 100
    assert calc_accuracy(values) == 40.0


def test_double_weight_is_02():
    values = empty_judgements()
    values["earlyDouble"] = 50
    values["lateDouble"] = 50
    assert calc_accuracy(values) == 20.0


def test_mixed_accuracy_matches_tuf_formula():
    values = empty_judgements()
    values["perfect"] = 50
    values["ePerfect"] = 50
    assert calc_accuracy_ratio(values) == 0.875
    assert calc_accuracy(values) == 87.5
    assert format_accuracy(values) == "87.50%"


def test_all_zero_returns_zero():
    assert calc_accuracy(empty_judgements()) == 0.0
    assert calc_accuracy(None) == 0.0


def test_negative_or_bad_values_are_ignored():
    values = empty_judgements()
    values["perfect"] = 10
    values["ePerfect"] = "bad"  # type: ignore[assignment]
    assert calc_accuracy(values) == 100.0
