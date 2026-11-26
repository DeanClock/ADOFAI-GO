"""批量绑定：本地条目与 TUF 候选的数值匹配（纯逻辑，不依赖 Qt / 网络）。

口径（2026-11 实测，TUF #10808）：

- 本地条目 ``tilecount`` 是谱面总格数（含中旋、含自动砖）；
- TUF 列表接口的 ``tilecount`` = 本地 ``tilecount - midspin_count``
  （含自动砖、不含中旋格）；
- ``playable_hits = tilecount - midspin_count - auto_tile_count``。

因此匹配时先把本地物量换算成 TUF 口径，再和候选的四元组
（``bpm / tilecount / midspinCount / autoTileCount``）逐项比较。
字段是否存在只能查 ``LevelInfo.raw``：``from_tuf`` 缺字段时会静默补 0。
"""

from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass, field
from typing import Any

from .models import LevelInfo
from .text_utils import strip_rich_text

# TUF 列表接口 raw 里的原始字段名（顺序即界面上的四元组顺序）。
TUF_NUMERIC_FIELDS: tuple[tuple[str, str], ...] = (
    ("bpm", "BPM"),
    ("tilecount", "物量"),
    ("midspinCount", "中旋"),
    ("autoTileCount", "自动砖"),
)

# 名称相似度 ≥ 0.6 才预选候选；不再单独触发自动勾选。
PRESELECT_THRESHOLD = 0.6
# 名称兜底最多保留的候选数（与旧版一致）。
NAME_LIMIT = 5
# 候选菜单最多显示多少条（数值全等优先）。
MAX_MENU = 20


def _normalize(text: str) -> str:
    text = re.sub(r"[\s\-_()（）\[\]【】,，.。!！?？·]+", "", text or "")
    return text.casefold()


def _similarity(left: str, right: str) -> float:
    return _normalized_similarity(_normalize(left), _normalize(right))


def _normalized_similarity(a: str, b: str) -> float:
    """比较已规范化的名称，供一行内复用本地名称。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _number(value: Any) -> float | None:
    """把 JSON 里的数字转成 float；布尔、空值、非数字都返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None:
        return None
    return int(round(number))


def format_bpm(value: Any) -> str:
    """BPM 展示/查询串：整数不带 ``.0``，最多 3 位小数，不用科学计数法。"""
    number = _number(value)
    if number is None:
        return ""
    rounded = round(number, 3)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.3f}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class LocalNumbers:
    """本地条目换算后的数值；``tilecount`` 已是 TUF 口径（总格 − 中旋）。"""

    bpm: float
    tilecount: int
    midspin: int
    auto: int
    # 本地口径总格，只用于界面展示（状态列的「240 / 3355 / 12 / 0」）。
    total_tilecount: int = 0


def local_numbers(entry: dict[str, Any] | None) -> LocalNumbers | None:
    """从本地条目算出可参与匹配的数值；不满足条件时返回 ``None``。

    拒绝：解析失败、旧条目没有 ``playable_hits``、数值缺失或为负、
    ``playable_hits`` 与 ``tilecount - midspin - auto`` 不自洽。
    """
    local, _missing = _validate_local_numbers(entry)
    return local


def local_missing(entry: dict[str, Any] | None) -> list[str]:
    """本地条目为什么不能做数值匹配；能匹配时返回空列表。"""
    _local, missing = _validate_local_numbers(entry)
    return missing


def _validate_local_numbers(
    entry: dict[str, Any] | None,
) -> tuple[LocalNumbers | None, list[str]]:
    """只转换一次本地数值，同时返回匹配数值或按原顺序排列的缺失原因。"""
    if not isinstance(entry, dict):
        return None, ["本地条目"]
    if str(entry.get("parse_error") or "").strip():
        return None, ["解析失败"]
    missing: list[str] = []
    bpm = _number(entry.get("bpm"))
    tilecount = _integer(entry.get("tilecount"))
    midspin = _integer(entry.get("midspin_count"))
    auto = _integer(entry.get("auto_tile_count"))
    playable = _integer(entry.get("playable_hits"))
    if bpm is None or bpm <= 0:
        missing.append("BPM")
    if tilecount is None or tilecount <= 0:
        missing.append("物量")
    if midspin is None or midspin < 0:
        missing.append("中旋")
    if auto is None or auto < 0:
        missing.append("自动砖")
    if playable is None:
        missing.append("可击物量")
    if missing:
        return None, missing
    if (
        bpm is None
        or tilecount is None
        or midspin is None
        or auto is None
        or playable is None
    ):
        return None, ["数值不完整"]
    if playable != tilecount - midspin - auto:
        return None, ["数值不自洽"]
    tuf_tilecount = tilecount - midspin
    if tuf_tilecount <= 0:
        return None, ["物量"]
    return LocalNumbers(
        bpm=bpm,
        tilecount=tuf_tilecount,
        midspin=midspin,
        auto=auto,
        total_tilecount=tilecount,
    ), []


def format_local_quad(local: LocalNumbers | None) -> str:
    """状态列/提示里的本地四元组（本地口径总格）。"""
    if local is None:
        return ""
    return (
        f"{format_bpm(local.bpm)} / {local.total_tilecount} / "
        f"{local.midspin} / {local.auto}"
    )


@dataclass(frozen=True)
class NumericCheck:
    """一个 TUF 候选与本地数值的比较结果。"""

    complete: bool
    equal: bool
    missing: tuple[str, ...] = ()
    mismatch: tuple[str, ...] = ()


def check_numeric(level: LevelInfo | None, local: LocalNumbers | None) -> NumericCheck:
    """比较候选与本地数值；字段存在性只看 ``level.raw``。"""
    if level is None or local is None:
        return NumericCheck(complete=False, equal=False)
    raw = level.raw if isinstance(level.raw, dict) else {}
    values: dict[str, float] = {}
    missing: list[str] = []
    for key, label in TUF_NUMERIC_FIELDS:
        number = _number(raw.get(key))
        if number is None:
            missing.append(label)
        else:
            values[key] = number
    if missing:
        return NumericCheck(
            complete=False,
            equal=False,
            missing=tuple(missing),
        )
    mismatch: list[str] = []
    candidate_bpm = int(round(values["bpm"] * 1000))
    local_bpm = int(round(local.bpm * 1000))
    if candidate_bpm != local_bpm:
        mismatch.append(f"BPM {format_bpm(values['bpm'])}≠{format_bpm(local.bpm)}")
    for key, label, expected in (
        ("tilecount", "物量", local.tilecount),
        ("midspinCount", "中旋", local.midspin),
        ("autoTileCount", "自动砖", local.auto),
    ):
        candidate_value = int(round(values[key]))
        if candidate_value != expected:
            mismatch.append(f"{label} {candidate_value}≠{expected}")
    return NumericCheck(
        complete=True,
        equal=not mismatch,
        mismatch=tuple(mismatch),
    )


def describe_check(check: NumericCheck) -> str:
    """tooltip 用的单行说明：✓ / 数值不同（…） / 数值不完整（缺 …）。"""
    if check.complete and check.equal:
        return "✓ 数值全等"
    if not check.complete:
        if check.missing:
            return "数值不完整（缺 " + "、".join(check.missing) + "）"
        return "数值不完整"
    return "数值不同（" + "；".join(check.mismatch) + "）"


def format_level_quad(level: LevelInfo | None) -> str:
    """候选的 TUF 四元组；raw 缺字段时用 ``?`` 占位。"""
    if level is None:
        return ""
    raw = level.raw if isinstance(level.raw, dict) else {}
    parts: list[str] = []
    for key, _label in TUF_NUMERIC_FIELDS:
        value = raw.get(key)
        number = _number(value)
        if number is None:
            parts.append("?")
        elif key == "bpm":
            parts.append(format_bpm(number))
        else:
            parts.append(str(int(round(number))))
    return " / ".join(parts)


def numeric_queries(local: LocalNumbers | None) -> list[str]:
    """数值召回查询串：先 BPM + 物量，再只查 BPM。"""
    if local is None:
        return []
    bpm = format_bpm(local.bpm)
    if not bpm or local.tilecount <= 0:
        return []
    return [
        f"bpm={bpm}, tilecount={local.tilecount}",
        f"bpm={bpm}",
    ]


@dataclass
class Candidate:
    """一个去重后的 TUF 候选，附带名称相似度与数值判定。"""

    level: LevelInfo
    score: float
    check: NumericCheck

    @property
    def equal(self) -> bool:
        return self.check.complete and self.check.equal


@dataclass
class RowMatch:
    """一行的匹配判定结果，界面只消费这个对象。"""

    kind: str  # numeric_unique / numeric_multi / name_only / none
    candidates: list[Candidate] = field(default_factory=list)
    selected: LevelInfo | None = None
    auto_check: bool = False
    local: LocalNumbers | None = None
    local_missing: tuple[str, ...] = ()
    best_score: float = 0.0
    equal_count: int = 0
    incomplete_count: int = 0

    @property
    def incomplete_only(self) -> bool:
        """候选全都缺数值字段（本地数值本身可用时才算）。"""
        return (
            self.local is not None
            and bool(self.candidates)
            and self.incomplete_count == len(self.candidates)
        )


def _local_name(entry: dict[str, Any] | None) -> str:
    if not isinstance(entry, dict):
        return ""
    song = strip_rich_text(str(entry.get("song") or "")).strip()
    if song:
        return song
    path = str(entry.get("path") or "")
    return re.split(r"[\\/]", path)[-1].rsplit(".", 1)[0] if path else ""


def _id_key(level: LevelInfo) -> int:
    try:
        return int(level.tuf_id or 0)
    except (TypeError, ValueError):
        return 0


def classify_candidates(
    entry: dict[str, Any] | None,
    levels: list[LevelInfo] | None,
    name_limit: int = NAME_LIMIT,
    max_menu: int = MAX_MENU,
) -> RowMatch:
    """去重、打分、按四元组筛选并给出预选与自动勾选结论。

    - 数值全等且仅 1 张 → ``numeric_unique``，自动勾选；
    - 数值全等多于 1 张 → ``numeric_multi``，按名称相似度预选、不勾选；
    - 没有全等但有候选 → ``name_only``，最佳候选 ≥0.6 才预选、不勾选；
    - 没有候选 → ``none``。
    """
    local, missing = _validate_local_numbers(entry)
    name = _normalize(_local_name(entry))
    seen: set[int] = set()
    scored: list[Candidate] = []
    for level in levels or []:
        if level is None or not level.tuf_id:
            continue
        number = _id_key(level)
        if number in seen:
            continue
        seen.add(number)
        scored.append(
            Candidate(
                level=level,
                score=_normalized_similarity(name, _normalize(level.song)),
                check=check_numeric(level, local),
            )
        )
    equal = sorted(
        (item for item in scored if item.equal),
        key=lambda item: (-item.score, _id_key(item.level)),
    )
    others = sorted(
        (item for item in scored if not item.equal),
        key=lambda item: (-item.score, _id_key(item.level)),
    )

    if equal:
        ordered = equal + others[: max(0, name_limit)]
        selected = equal[0].level
        kind = "numeric_unique" if len(equal) == 1 else "numeric_multi"
        auto_check = len(equal) == 1
    elif others:
        ordered = others[: max(0, name_limit)]
        best = ordered[0]
        selected = best.level if best.score >= PRESELECT_THRESHOLD else None
        kind = "name_only"
        auto_check = False
    else:
        ordered = []
        selected = None
        kind = "none"
        auto_check = False

    ordered = ordered[: max(0, max_menu)]
    return RowMatch(
        kind=kind,
        candidates=ordered,
        selected=selected,
        auto_check=auto_check,
        local=local,
        local_missing=tuple(missing),
        best_score=ordered[0].score if ordered else 0.0,
        equal_count=len(equal),
        incomplete_count=sum(1 for item in ordered if not item.check.complete),
    )
