from __future__ import annotations

from typing import Any

from .models import JUDGEMENT_KEYS, JUDGEMENT_WEIGHTS, judgement_sum


def calc_accuracy_ratio(judgements: dict[str, Any] | None) -> float:
    if not judgements:
        return 0.0
    total = judgement_sum(judgements)
    if total <= 0:
        return 0.0
    weighted = 0.0
    for key in JUDGEMENT_KEYS:
        try:
            count = int(judgements.get(key, 0) or 0)
        except (TypeError, ValueError):
            count = 0
        weighted += count * JUDGEMENT_WEIGHTS[key]
    return weighted / total


def calc_accuracy(judgements: dict[str, Any] | None) -> float:
    return calc_accuracy_ratio(judgements) * 100.0


def format_accuracy(judgements: dict[str, Any] | None, digits: int = 2) -> str:
    return f"{calc_accuracy(judgements):.{digits}f}%"


def format_accuracy_value(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}%"
