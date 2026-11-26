from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .text_utils import strip_rich_text

# 标签与顺序对齐 TUF /submission/pass 的真实表单（2026-11 版）：
# X 完美模式开启时是 -完美/完美/完美+ + 稍快/稍慢 + 提前/太快/太慢；
# 关闭时是 稍快/完美/稍慢 + 提前/太快/太慢。
# 「太慢！！」TUF 表单没有输入框（记录展示里才有），保留键与权重。
JUDGEMENT_FIELDS: list[tuple[str, str, float]] = [
    ("perfectMinus", "-完美", 1.0),
    ("perfect", "完美", 1.0),
    ("perfectPlus", "完美+", 1.0),
    ("ePerfect", "稍快", 0.75),
    ("lPerfect", "稍慢", 0.75),
    ("earlyDouble", "提前", 0.2),
    ("earlySingle", "太快", 0.4),
    ("lateSingle", "太慢", 0.4),
    ("lateDouble", "太慢！！", 0.2),
]

JUDGEMENT_KEYS = [item[0] for item in JUDGEMENT_FIELDS]
JUDGEMENT_LABELS = {key: label for key, label, _ in JUDGEMENT_FIELDS}
JUDGEMENT_WEIGHTS = {key: weight for key, _, weight in JUDGEMENT_FIELDS}


def empty_judgements() -> dict[str, int]:
    return {key: 0 for key in JUDGEMENT_KEYS}


def _judgement_count(values: dict[str, Any], key: str) -> int:
    try:
        return int(values.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def judgement_sum(values: dict[str, Any]) -> int:
    """TUF 前端 ``sumJudgements``：九格全加。

    准度分母和界面上的「判定总数（九格）」都走这个口径。
    """
    return sum(_judgement_count(values, key) for key in JUDGEMENT_KEYS)


# TUF 前端 ``tilecount(j)`` 排除这两格：「提前」(earlyDouble) 与「太慢！！」(lateDouble)。
HIT_COUNT_EXCLUDED_KEYS = ("earlyDouble", "lateDouble")


def judgement_hit_count(values: dict[str, Any]) -> int:
    """TUF 前端 ``tilecount(j)``：打击数，排除「提前」与「太慢！！」。

    物量校验必须用这个口径，不能用九格和。
    """
    return sum(
        _judgement_count(values, key)
        for key in JUDGEMENT_KEYS
        if key not in HIT_COUNT_EXCLUDED_KEYS
    )


# 两个名字都对外提供：hit_count 是简写，行为与 judgement_hit_count 完全一致。
hit_count = judgement_hit_count


# TUF 前端 ``midspinPerfectDecrement``：只有 ``adofaiVersion`` 低于 3.4.0
# 时才把中旋数从完美里扣掉；3.4.0 起结算已经把中旋算进判定（"latest_era"），
# 再扣就会少算。
MIDSPIN_DECREMENT_MAX_VERSION = (3, 4, 0)


def _version_tuple(version: Any) -> tuple[int, int, int] | None:
    """把 "3.4.0" / "3.3.1" / "v2.9" 之类解析成可比较的数字元组。

    取第一段版本号，补足 / 截断到三段；解析不出数字时返回 ``None``。
    """
    match = re.search(r"\d+(?:\.\d+)*", str(version or ""))
    if not match:
        return None
    parts = [int(item) for item in match.group(0).split(".")]
    parts = (parts + [0, 0, 0])[:3]
    return (parts[0], parts[1], parts[2])


def adjust_perfect_for_midspin(
    values: dict[str, Any],
    midspin_count: int | None,
    adofai_version: str = "3.4.0",
) -> dict[str, int]:
    """TUF 提交口径：只有 3.4.0 之前的版本才用中旋数扣完美。

    TUF 原文：「This chart has {{count}} midspin tiles. They will be
    subtracted from Perfect when you submit.」但前端 ``midspinPerfectDecrement``
    只在 ``adofaiVersion`` 低于 3.4.0 时执行；3.4.0+ 跳过。版本解析失败按
    3.4.0+ 处理（不扣）；``perfect < midspin`` 时不调整（TUF 同样不做调整），
    结果不会出现负数。
    """
    adjusted = {key: _judgement_count(values, key) for key in JUDGEMENT_KEYS}
    try:
        midspins = int(midspin_count or 0)
    except (TypeError, ValueError):
        midspins = 0
    parsed = _version_tuple(adofai_version)
    should_decrement = parsed is not None and parsed < MIDSPIN_DECREMENT_MAX_VERSION
    if should_decrement and midspins > 0 and adjusted["perfect"] >= midspins:
        adjusted["perfect"] -= midspins
    return adjusted


@dataclass
class LevelInfo:
    tuf_id: int | None = None
    song: str = ""
    artist: str = ""
    charter: str = ""
    vfxers: list[str] = field(default_factory=list)
    bpm: float | None = None
    duration_ms: float | None = None
    difficulty: str = ""
    difficulty_id: int | None = None
    difficulty_type: str = ""
    difficulty_color: str = ""
    difficulty_icon: str = ""
    difficulty_sort_order: int | None = None
    base_score: float | None = None
    video_link: str = ""
    tilecount: int | None = None
    auto_tile_count: int = 0
    midspin_count: int = 0
    # 本机解析出的可击物量（总格 - 中旋 - 自动砖）。TUF 服务端条目没有这个值，
    # 继续按 tilecount - auto_tile_count 计算。
    playable_hits: int | None = None
    dl_link: str = ""
    workshop_link: str = ""
    file_id: str = ""
    tags: list[dict[str, Any]] = field(default_factory=list)
    curation_type_ids: list[int] = field(default_factory=list)
    clears: int = 0
    unique_clears: int = 0
    likes: int = 0
    download_count: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.song = strip_rich_text(self.song)
        self.artist = strip_rich_text(self.artist)
        self.charter = strip_rich_text(self.charter)
        self.difficulty = strip_rich_text(self.difficulty)
        self.vfxers = [strip_rich_text(name) for name in (self.vfxers or [])]

    @property
    def duration_text(self) -> str:
        if not self.duration_ms:
            return "-"
        seconds = int(round(self.duration_ms / 1000))
        return f"{seconds // 60}:{seconds % 60:02d}"

    @property
    def expected_hits(self) -> int | None:
        # 本机条目已有精确值（扣除了中旋与自动砖）时优先用它；
        # 服务端 TUF 条目没有 playable_hits，退回 tilecount - autoTileCount。
        if self.playable_hits is not None:
            return max(int(self.playable_hits), 0)
        if self.tilecount is None:
            return None
        return max(int(self.tilecount) - int(self.auto_tile_count or 0), 0)

    @property
    def url(self) -> str:
        return f"https://tuforums.com/levels/{self.tuf_id}" if self.tuf_id else ""

    @property
    def creator_display(self) -> str:
        return self.charter or "-"

    def to_dict(self) -> dict[str, Any]:
        return {
            "tuf_id": self.tuf_id,
            "song": self.song,
            "artist": self.artist,
            "charter": self.charter,
            "vfxers": list(self.vfxers),
            "bpm": self.bpm,
            "duration_ms": self.duration_ms,
            "difficulty": self.difficulty,
            "difficulty_id": self.difficulty_id,
            "difficulty_type": self.difficulty_type,
            "difficulty_color": self.difficulty_color,
            "difficulty_icon": self.difficulty_icon,
            "difficulty_sort_order": self.difficulty_sort_order,
            "base_score": self.base_score,
            "video_link": self.video_link,
            "tilecount": self.tilecount,
            "auto_tile_count": self.auto_tile_count,
            "midspin_count": self.midspin_count,
            "playable_hits": self.playable_hits,
            "dl_link": self.dl_link,
            "workshop_link": self.workshop_link,
            "file_id": self.file_id,
            "tags": [dict(tag) for tag in self.tags],
            "curation_type_ids": list(self.curation_type_ids),
            "clears": self.clears,
            "unique_clears": self.unique_clears,
            "likes": self.likes,
            "download_count": self.download_count,
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "LevelInfo | None":
        if not data:
            return None
        return cls(
            tuf_id=data.get("tuf_id"),
            song=data.get("song", ""),
            artist=data.get("artist", ""),
            charter=data.get("charter", ""),
            vfxers=list(data.get("vfxers") or []),
            bpm=data.get("bpm"),
            duration_ms=data.get("duration_ms"),
            difficulty=data.get("difficulty", ""),
            difficulty_id=data.get("difficulty_id"),
            difficulty_type=data.get("difficulty_type", ""),
            difficulty_color=data.get("difficulty_color", ""),
            difficulty_icon=data.get("difficulty_icon", ""),
            difficulty_sort_order=data.get("difficulty_sort_order"),
            base_score=data.get("base_score"),
            video_link=data.get("video_link", ""),
            tilecount=data.get("tilecount"),
            auto_tile_count=int(data.get("auto_tile_count") or 0),
            midspin_count=int(data.get("midspin_count") or 0),
            playable_hits=(
                int(data["playable_hits"])
                if data.get("playable_hits") is not None
                else None
            ),
            dl_link=data.get("dl_link", ""),
            workshop_link=data.get("workshop_link", ""),
            file_id=str(data.get("file_id") or ""),
            tags=[dict(tag) for tag in (data.get("tags") or []) if isinstance(tag, dict)],
            curation_type_ids=[
                int(item) for item in (data.get("curation_type_ids") or []) if str(item).isdigit()
            ],
            clears=int(data.get("clears") or 0),
            unique_clears=int(data.get("unique_clears") or 0),
            likes=int(data.get("likes") or 0),
            download_count=int(data.get("download_count") or 0),
            raw=data.get("raw") or {},
        )

    @classmethod
    def from_tuf(cls, data: dict[str, Any]) -> "LevelInfo":
        payload = data.get("level") if isinstance(data.get("level"), dict) else data
        payload = payload or {}

        artists = [item.get("name", "") for item in payload.get("artists") or []]
        artist = ", ".join(name for name in artists if name) or payload.get("artist") or ""

        charters: list[str] = []
        vfxers: list[str] = []
        for credit in payload.get("levelCredits") or []:
            name = (credit.get("creator") or {}).get("name", "")
            if not name:
                continue
            if credit.get("role") == "charter":
                charters.append(name)
            elif credit.get("role") == "vfxer":
                vfxers.append(name)
        charter = ", ".join(dict.fromkeys(charters)) or payload.get("creator") or ""

        difficulty = payload.get("difficulty")
        if isinstance(difficulty, str):
            diff_name = difficulty
            difficulty = {}
        elif isinstance(difficulty, dict):
            diff_name = difficulty.get("name") or "Unranked"
        else:
            difficulty = {}
            diff_name = "Unranked"
        base_score = payload.get("baseScore")
        if not base_score:
            base_score = difficulty.get("baseScore")

        curations = payload.get("curations")
        if not isinstance(curations, list):
            curations = [payload.get("curation")]
        type_ids: list[int] = []
        for curation in curations:
            if not isinstance(curation, dict):
                continue
            for item in curation.get("typeIds") or []:
                try:
                    type_id = int(item)
                except (TypeError, ValueError):
                    continue
                if type_id not in type_ids:
                    type_ids.append(type_id)

        tags = [dict(tag) for tag in (payload.get("tags") or []) if isinstance(tag, dict)]

        raw_file_id = payload.get("fileId")
        return cls(
            tuf_id=payload.get("id"),
            song=payload.get("song") or "",
            artist=artist,
            charter=charter,
            vfxers=list(dict.fromkeys(vfxers)),
            bpm=payload.get("bpm"),
            duration_ms=payload.get("levelLengthInMs"),
            difficulty=diff_name,
            difficulty_id=difficulty.get("id"),
            difficulty_type=difficulty.get("type") or "",
            difficulty_color=difficulty.get("color") or "",
            difficulty_icon=difficulty.get("icon") or "",
            difficulty_sort_order=difficulty.get("sortOrder"),
            base_score=base_score,
            video_link=payload.get("videoLink") or "",
            tilecount=payload.get("tilecount"),
            auto_tile_count=int(payload.get("autoTileCount") or 0),
            midspin_count=int(payload.get("midspinCount") or 0),
            dl_link=payload.get("dlLink") or "",
            workshop_link=payload.get("workshopLink") or "",
            file_id=str(raw_file_id) if raw_file_id is not None else "",
            tags=tags,
            curation_type_ids=type_ids,
            clears=int(payload.get("clears") or 0),
            unique_clears=int(payload.get("uniqueClears") or 0),
            likes=int(payload.get("likes") or 0),
            download_count=int(payload.get("downloadCount") or 0),
            raw=payload,
        )


@dataclass
class Draft:
    video_path: str = ""
    # 16:9 主图 → B 站「个人空间封面（16:9）」；cover_path_43 是 4:3 副图 →
    # 「首页推荐封面（4:3）」，旧草稿没有这个键，按空串处理（两个框都用主图）。
    cover_path: str = ""
    cover_path_43: str = ""
    level: LevelInfo | None = None
    judgements: dict[str, int] = field(default_factory=empty_judgements)
    speed: float = 1.0
    feeling_rating: str = ""
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    partition: str = "单机游戏"
    creative_declaration: str = "内容无需标注"
    bvid: str = ""
    tuf_record_id: str = ""
    # 审核状态与投稿状态是两件事：status 只表示「已投稿」，
    # review_status 才表示 TUF 那边待处理/已通过/未通过。
    review_status: str = ""
    review_checked_at: str = ""
    review_message: str = ""
    review_assignee: str = ""
    review_flags: Any = None
    review_judgements: dict[str, int] = field(default_factory=dict)
    chart_path: str = ""
    status: str = "draft"
    is_no_hold_tap: bool = False
    is_12k: bool = False
    is_16k: bool = False
    x_perfect: bool = False
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_path": self.video_path,
            "cover_path": self.cover_path,
            "cover_path_43": self.cover_path_43,
            "level": self.level.to_dict() if self.level else None,
            "judgements": dict(self.judgements),
            "speed": self.speed,
            "feeling_rating": self.feeling_rating,
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
            "partition": self.partition,
            "creative_declaration": self.creative_declaration,
            "bvid": self.bvid,
            "tuf_record_id": self.tuf_record_id,
            "review_status": self.review_status,
            "review_checked_at": self.review_checked_at,
            "review_message": self.review_message,
            "review_assignee": self.review_assignee,
            "review_flags": self.review_flags,
            "review_judgements": dict(self.review_judgements),
            "chart_path": self.chart_path,
            "status": self.status,
            "is_no_hold_tap": self.is_no_hold_tap,
            "is_12k": self.is_12k,
            "is_16k": self.is_16k,
            "x_perfect": self.x_perfect,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Draft":
        data = data or {}
        judgements = empty_judgements()
        for key, value in (data.get("judgements") or {}).items():
            if key in judgements:
                try:
                    judgements[key] = int(value or 0)
                except (TypeError, ValueError):
                    judgements[key] = 0
        # 旧草稿没有 review_* 字段：一律按空值处理，审核状态保持「未同步」。
        review_flags = data.get("review_flags")
        if not isinstance(review_flags, (list, dict)):
            review_flags = None
        review_judgements: dict[str, int] = {}
        for key, value in (data.get("review_judgements") or {}).items():
            try:
                review_judgements[str(key)] = int(value or 0)
            except (TypeError, ValueError):
                continue
        return cls(
            video_path=data.get("video_path", ""),
            cover_path=data.get("cover_path", ""),
            cover_path_43=data.get("cover_path_43", ""),
            level=LevelInfo.from_dict(data.get("level")),
            judgements=judgements,
            speed=float(data.get("speed") or 1.0),
            feeling_rating=data.get("feeling_rating", ""),
            title=data.get("title", ""),
            description=data.get("description", ""),
            tags=list(data.get("tags") or []),
            partition=data.get("partition") or "单机游戏",
            creative_declaration=data.get("creative_declaration") or "内容无需标注",
            bvid=data.get("bvid", ""),
            tuf_record_id=data.get("tuf_record_id", ""),
            review_status=data.get("review_status") or "",
            review_checked_at=data.get("review_checked_at") or "",
            review_message=data.get("review_message") or "",
            review_assignee=data.get("review_assignee") or "",
            review_flags=review_flags,
            review_judgements=review_judgements,
            chart_path=data.get("chart_path", ""),
            status=data.get("status") or "draft",
            is_no_hold_tap=bool(data.get("is_no_hold_tap", False)),
            is_12k=bool(data.get("is_12k", False)),
            is_16k=bool(data.get("is_16k", False)),
            x_perfect=bool(data.get("x_perfect", False)),
            updated_at=data.get("updated_at", ""),
        )
