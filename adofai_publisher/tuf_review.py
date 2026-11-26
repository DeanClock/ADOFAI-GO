"""TUF 投稿审核状态的容错解析与匹配。

本模块只做纯函数，不发网络请求：

- ``normalize_review_status``：把 TUF 各种审核状态写法归一到四种口径；
  未知写法一律 ``unknown``，绝不猜成 ``declined``。
- ``extract_submission_item``：TUF 没有公开响应 schema，从任意形状的
  JSON 里尽量取出审核需要的字段；取不到的部分留空。
- ``match_submission``：把本地投稿记录对到 TUF 列表里的一条记录，
  依据只有 TUF 记录编号或「谱面编号 + BV 号」，歧义时返回 ``None``。
- ``merge_review``：只产出 ``review_*`` 字段，绝不包含 ``status``——
  本地投稿状态语义由 ``submission_status`` 独占。

审核状态与投稿状态是两件事：``status == "submitted"`` 只代表记录已投给
TUF，审核是否通过要看 ``review_status``。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from itertools import islice
from typing import Any

from .models import JUDGEMENT_KEYS

REVIEW_PENDING = "pending"
REVIEW_APPROVED = "approved"
REVIEW_DECLINED = "declined"
REVIEW_UNKNOWN = "unknown"

REVIEW_LABELS: dict[str, str] = {
    REVIEW_PENDING: "待处理",
    REVIEW_APPROVED: "已通过",
    REVIEW_DECLINED: "未通过",
    REVIEW_UNKNOWN: "未知",
}

# UI 共用：审核状态 → 现有 chip 样式对象名。
REVIEW_CHIP_OBJECTS: dict[str, str] = {
    REVIEW_PENDING: "ChipWarn",
    REVIEW_APPROVED: "ChipOk",
    REVIEW_DECLINED: "ChipBad",
    REVIEW_UNKNOWN: "Chip",
}

_APPROVED_ALIASES = frozenset(
    {
        "approved",
        "approve",
        "accepted",
        "accept",
        "passed",
        "pass",
        "verified",
        "verify",
    }
)
_DECLINED_ALIASES = frozenset(
    {
        "declined",
        "decline",
        "rejected",
        "reject",
        "denied",
        "deny",
        "failed",
        "fail",
    }
)
_PENDING_ALIASES = frozenset(
    {
        "pending",
        "wait",
        "waiting",
        "reviewing",
        "in_review",
        "under_review",
        "queued",
        "queue",
    }
)

_ID_KEYS = ("id", "passId", "submissionId", "recordId", "_id")
_LEVEL_ID_KEYS = ("levelId", "level_id")
_STATUS_KEYS = ("status", "reviewStatus", "state")
_ASSIGNEE_KEYS = ("assignedPlayer", "assignee", "reviewer", "player")
_PLAYER_ID_KEYS = ("playerId", "player_id", "userId", "user_id")
_MESSAGE_KEYS = ("message", "reason", "note")
_VIDEO_KEYS = ("videoLink", "video_link", "videoUrl", "video_url", "url")
_BVID_KEYS = ("bvid", "BVID")
_NESTED_ITEM_KEYS = ("pass", "submission", "item", "data")
_BVID_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}")
# 真实列表项没有 pass/level 子对象，编号只在 href / passHref 里。
_LEVEL_ID_PATTERN = re.compile(r"/levels?/(\d+)", re.IGNORECASE)
_PASS_ID_PATTERN = re.compile(r"/pass(?:es)?/(\d+)", re.IGNORECASE)
_LEVEL_LINK_KEYS = ("href", "levelHref", "level_href")
_LEVEL_FALLBACK_LINK_KEYS = ("downloadLink", "download_link", "workshopLink", "workshop_link")
_EXTRA_DIFFICULTY_KEYS = ("difficulty", "diff", "difficultyName", "name", "label")


def _text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (Mapping, list, tuple, set)):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _first_text(source: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        text = _text(source.get(key))
        if text:
            return text
    return ""


def normalize_review_status(value: Any) -> str:
    """审核状态归一：approved / declined / pending / unknown。

    认不出的写法（包括空值、数字、未知英文）一律 ``unknown``；
    绝不能把未知状态当成 ``declined``，否则会把待审记录误报成未通过。
    """

    if isinstance(value, Mapping):
        # 有些接口把状态写成 ``{"name": "pending"}`` 之类的对象。
        for key in ("value", "status", "state", "name", "label"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return normalize_review_status(nested)
        return REVIEW_UNKNOWN
    text = _text(value).lower()
    if not text:
        return REVIEW_UNKNOWN
    # 兼容 "IN_REVIEW"、"in review"、"in-review" 等写法。
    text = re.sub(r"[\s\-]+", "_", text)
    if text in _APPROVED_ALIASES:
        return REVIEW_APPROVED
    if text in _DECLINED_ALIASES:
        return REVIEW_DECLINED
    if text in _PENDING_ALIASES:
        return REVIEW_PENDING
    return REVIEW_UNKNOWN


def review_text(value: Any) -> str:
    """审核状态的中文文案；空值返回空串，便于 UI 显示「未同步」。"""

    if value is None:
        return ""
    if isinstance(value, str) and not value.strip():
        return ""
    return REVIEW_LABELS.get(normalize_review_status(value), REVIEW_LABELS[REVIEW_UNKNOWN])


def _display_text(value: Any, depth: int = 0) -> str:
    """展示用文本：字符串直接用，对象取 name/label/title，取不到留空。"""

    if isinstance(value, Mapping):
        if depth > 2:
            return ""
        for key in ("name", "label", "title", "value", "difficultyName"):
            text = _text(value.get(key))
            if text:
                return text[:200]
        for key in ("difficulty", "level"):
            nested = value.get(key)
            if isinstance(nested, Mapping):
                text = _display_text(nested, depth + 1)
                if text:
                    return text
        return ""
    return _text(value)[:200]


def _person_text(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("name", "nickname", "username", "displayName", "id"):
            text = _text(value.get(key))
            if text:
                return text[:200]
        return ""
    return _text(value)[:200]


def _player_id(value: Any) -> int | None:
    if isinstance(value, Mapping):
        for key in ("id", "playerId", "player_id"):
            found = _player_id(value.get(key))
            if found is not None:
                return found
        return None
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _json_safe(value: Any, depth: int = 0) -> Any:
    """把任意 JSON 值压成可安全写进 records.json 的紧凑结构。"""

    if depth > 4:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:300]
    if isinstance(value, Mapping):
        return {
            str(key)[:60]: _json_safe(item, depth + 1)
            for key, item in tuple(islice(value.items(), 50))
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item, depth + 1) for item in tuple(islice(value, 50))]
    return str(value)[:300]


def _judgements(source: Mapping[str, Any]) -> dict[str, int]:
    """只保留 TUF 判定九格，值统一成整数；其他字段一概不写。"""

    raw = source.get("judgements")
    if isinstance(raw, Mapping):
        pairs: list[tuple[Any, Any]] = list(raw.items())
    else:
        pairs = [(key, source.get(key)) for key in JUDGEMENT_KEYS if key in source]
    result: dict[str, int] = {}
    for key, value in pairs:
        if key not in JUDGEMENT_KEYS:
            continue
        try:
            result[str(key)] = int(value or 0)
        except (TypeError, ValueError):
            continue
    return result


def _review_block(source: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = source.get("review")
    return nested if isinstance(nested, Mapping) else {}


def _looks_like_item(source: Mapping[str, Any]) -> bool:
    if not source:
        return False
    if any(_text(source.get(key)) for key in _ID_KEYS):
        return True
    if any(key in source for key in ("levelId", "level", *_STATUS_KEYS)):
        return True
    if isinstance(source.get("judgements"), Mapping):
        return True
    return bool(_review_block(source))


def _first_value(sources: list[Mapping[str, Any]], keys: tuple[str, ...]) -> Any:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return value
    return None


def _first_in_sources(
    sources: list[Mapping[str, Any]], keys: tuple[str, ...]
) -> str:
    for source in sources:
        text = _first_text(source, keys)
        if text:
            return text
    return ""


def _match_id(text: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(text or "")
    return match.group(1) if match else ""


def _field_names(value: Any, limit: int = 40) -> list[str]:
    """只返回字段名（不返回值），用于诊断真实响应结构。"""

    if not isinstance(value, Mapping):
        return []
    names: list[str] = []
    for key in list(value.keys())[:limit]:
        text = str(key).strip()[:40]
        if text:
            names.append(text)
    return names


def extract_submission_item(payload: Any) -> dict[str, Any] | None:
    """从任意形状的 TUF 响应里取出审核字段。

    接受扁平对象、``{"pass": {...}}``、``{"data": {...}}`` 等一层嵌套；
    顶层即使已经「像一条记录」，也继续到 ``pass`` / ``level`` 子对象里补
    展示字段（真实列表把谱面信息放在嵌套对象里）。

    编号分两类：

    - ``submission_id``：投稿系统的编号，取自顶层 ``id`` /
      ``submissionId``；
    - ``pass_id``：游戏内通关记录编号，取自 ``passId`` / ``pass.id`` /
      ``recordId``。本地 ``tuf_record_id`` 存的是这一类。

    完全不像投稿记录（例如 ``{"success": false}``）时返回 ``None``。
    """

    if not isinstance(payload, Mapping):
        return None

    top_is_item = _looks_like_item(payload)
    source: Mapping[str, Any] = payload
    primary_key = ""
    if not top_is_item:
        for key in _NESTED_ITEM_KEYS:
            nested = payload.get(key)
            if isinstance(nested, Mapping) and _looks_like_item(nested):
                source = nested
                primary_key = key
                break
        else:
            return None

    # 顶层看起来像 item 时也不能停止：pass/level 里可能还有展示字段。
    sources: list[Mapping[str, Any]] = [payload]
    for key in _NESTED_ITEM_KEYS:
        nested = payload.get(key)
        if isinstance(nested, Mapping) and all(
            nested is not existing for existing in sources
        ):
            sources.append(nested)
    if all(source is not existing for existing in sources):
        sources.append(source)

    review: Mapping[str, Any] = {}
    for candidate in sources:
        block = _review_block(candidate)
        if block:
            review = block
            break

    status_source = _first_value(sources, _STATUS_KEYS)
    if status_source is None:
        status_source = _first_value([review], _STATUS_KEYS)
    status = normalize_review_status(status_source)

    level_block: Mapping[str, Any] = {}
    for candidate in sources:
        level_value = candidate.get("level")
        if isinstance(level_value, Mapping) and level_value:
            level_block = level_value
            break
    level_id = _first_text(level_block, ("id", "levelId"))
    if not level_id:
        level_id = _first_in_sources(sources, _LEVEL_ID_KEYS)
    if not level_id:
        for candidate in sources:
            text = _text(candidate.get("level"))
            if text:
                level_id = text
                break
    if not level_id:
        # 真实列表项只在 href / 下载链接里带谱面编号。
        level_id = _match_id(
            _first_in_sources(sources, _LEVEL_LINK_KEYS), _LEVEL_ID_PATTERN
        )
    if not level_id:
        for key in _LEVEL_FALLBACK_LINK_KEYS:
            level_id = _match_id(
                _first_in_sources(sources, (key,)), _LEVEL_ID_PATTERN
            )
            if level_id:
                break

    assignee_value = _first_value(sources, _ASSIGNEE_KEYS)
    if assignee_value is None:
        assignee_value = _first_value([review], _ASSIGNEE_KEYS)

    message = _first_in_sources(sources, _MESSAGE_KEYS) or _first_text(
        review, _MESSAGE_KEYS
    )
    video_link = _first_in_sources(sources, _VIDEO_KEYS)
    bvid = _first_in_sources(sources, _BVID_KEYS)
    if not bvid:
        match = _BVID_PATTERN.search(video_link)
        bvid = match.group(0) if match else ""

    raw_flags: Any = None
    for candidate in sources:
        if candidate.get("flags") is not None:
            raw_flags = candidate.get("flags")
            break
    if raw_flags is None:
        raw_flags = review.get("flags")
    flags = _json_safe(raw_flags) if raw_flags is not None else []

    # 两类编号分开取：顶层 id 只当 submission id，绝不再冒充 pass id。
    submission_id = _first_in_sources(sources, ("submissionId", "submission_id"))
    if not submission_id and top_is_item:
        submission_id = _text(payload.get("id"))
    if not submission_id and primary_key not in ("", "pass"):
        submission_id = _text(source.get("id"))

    pass_id = _first_in_sources(
        sources, ("passId", "pass_id", "recordId", "record_id", "passRecordId")
    )
    if not pass_id:
        pass_block = payload.get("pass")
        if isinstance(pass_block, Mapping):
            pass_id = _text(pass_block.get("id"))
    if not pass_id:
        pass_id = _match_id(
            _first_in_sources(
                sources,
                ("passHref", "pass_href", "passUrl", "pass_url"),
            ),
            _PASS_ID_PATTERN,
        )
    if not pass_id and primary_key == "pass":
        pass_id = _text(source.get("id"))

    # 列表展示字段：有就显示，没有留空，不拿别的字段猜。
    # 真实列表项用 title/artist；嵌套 level 里则可能是 song/artist。
    song = (
        _first_in_sources(sources, ("song", "songName"))
        or _first_text(level_block, ("song", "songName"))
        or _first_in_sources(sources, ("title",))
    )
    artist = _first_in_sources(sources, ("artist", "artistName")) or _first_text(
        level_block, ("artist", "artistName")
    )

    extra_value = _first_value(sources, ("extra",))
    extra_block: Mapping[str, Any] = {}
    if isinstance(extra_value, Mapping):
        extra_block = extra_value
    elif isinstance(extra_value, str) and extra_value.strip().startswith("{"):
        # extra 有时是 JSON 字符串；解析失败就当没有，不猜。
        try:
            parsed_extra = json.loads(extra_value)
        except ValueError:
            parsed_extra = None
        if isinstance(parsed_extra, Mapping):
            extra_block = parsed_extra

    difficulty_value = _first_value(sources, ("difficulty", "difficultyName"))
    if difficulty_value in (None, ""):
        difficulty_value = level_block.get("difficulty")
    if difficulty_value in (None, ""):
        for key in _EXTRA_DIFFICULTY_KEYS:
            value = extra_block.get(key)
            if value not in (None, ""):
                difficulty_value = value
                break
    difficulty = _display_text(difficulty_value)
    created_at = _first_in_sources(
        sources,
        (
            "createdAt",
            "created_at",
            "submittedAt",
            "submitted_at",
            "uploadTime",
            "uploadedAt",
        ),
    )
    updated_at = _first_in_sources(sources, ("updatedAt", "updated_at"))

    missing: list[str] = []
    if not pass_id:
        missing.append("pass_id")
    if not level_id:
        missing.append("level_id")
    if not difficulty and extra_block:
        # 编号都拿到了但难度取不到：把 extra 的字段名也带上，方便继续定位。
        missing.append("difficulty")
    diagnostics = None
    if missing:
        # 只记字段名，不记字段值：方便下一轮确认真实结构，又不会把数据写进日志。
        diagnostics = {
            "missing": missing,
            "top": _field_names(payload),
            "pass": _field_names(payload.get("pass")),
            "level": _field_names(level_block),
            "extra": _field_names(extra_block),
        }

    item: dict[str, Any] = {
        "id": submission_id,
        "submission_id": submission_id,
        "pass_id": pass_id,
        "level_id": level_id,
        "status": status,
        "message": message[:500],
        "assignee": _person_text(assignee_value),
        "flags": flags,
        "judgements": _judgements(source),
        "video_link": video_link,
        "bvid": bvid,
        "player_id": _player_id(
            _first_in_sources(sources, _PLAYER_ID_KEYS)
            or _first_value(sources, ("player",))
        ),
        "song": song[:200],
        "artist": artist[:200],
        "difficulty": difficulty,
        "created_at": created_at[:60],
        "updated_at": updated_at[:60],
        "diagnostics": diagnostics,
    }
    return item


def match_submission(
    record: Mapping[str, Any] | None,
    items: list[dict[str, Any]] | None,
    *,
    tuf_id_unique: bool = True,
) -> dict[str, Any] | None:
    """把一条本地记录对到 TUF 列表里的一条。

    匹配顺序：

    1. ``tuf_record_id`` 与 TUF 的 pass id（其次 submission id）完全相等；
    2. 谱面编号相同且 BV 号/视频链接相同；
    3. 仅谱面编号相同——只有当该谱面在本地只有这一条记录
       （``tuf_id_unique=True``）时才使用。

    任何一步出现多条候选都返回 ``None``，不猜。
    """

    if not isinstance(record, Mapping):
        return None
    candidates = [item for item in (items or []) if isinstance(item, Mapping)]

    record_id = _text(record.get("tuf_record_id"))
    if record_id:
        # 本地存的是提交响应里的 pass.id；列表顶层的 id 是 submission id，
        # 两类都接受，但不再把顶层 id 当成 pass id。
        by_id = [
            item
            for item in candidates
            if record_id
            in (_text(item.get("pass_id")), _text(item.get("submission_id")))
        ]
        if len(by_id) == 1:
            return by_id[0]
        if by_id:
            return None

    tuf_id = _text(record.get("tuf_id"))
    if not tuf_id:
        return None
    same_level = [
        item for item in candidates if _text(item.get("level_id")) == tuf_id
    ]
    if not same_level:
        return None

    bvid = _record_bvid(record)
    if bvid:
        by_video = [item for item in same_level if _item_has_bvid(item, bvid)]
        if len(by_video) == 1:
            return by_video[0]
        if by_video:
            return None

    if not tuf_id_unique:
        return None
    if len(same_level) == 1:
        return same_level[0]
    return None


def _record_bvid(record: Mapping[str, Any]) -> str:
    bvid = _text(record.get("bvid"))
    if bvid:
        return bvid
    for key in ("bili_url", "video_link", "level_video_link"):
        match = _BVID_PATTERN.search(_text(record.get(key)))
        if match:
            return match.group(0)
    return ""


def _item_has_bvid(item: Mapping[str, Any], bvid: str) -> bool:
    if _text(item.get("bvid")) == bvid:
        return True
    return bvid in _text(item.get("video_link"))


def merge_review(item: Mapping[str, Any] | None, checked_at: str = "") -> dict[str, Any]:
    """从提取好的 item 生成要写回本地记录的 ``review_*`` 字段。

    返回字典里永远没有 ``status``：投稿状态由 ``submission_status`` 管，
    审核状态只能进 ``review_status``。
    """

    if not isinstance(item, Mapping):
        return {}
    review = {
        "review_status": normalize_review_status(item.get("status")),
        "review_checked_at": _text(checked_at),
        "review_message": _text(item.get("message"))[:500],
        "review_assignee": _person_text(item.get("assignee")),
        "review_flags": _json_safe(item.get("flags")) if item.get("flags") is not None else [],
        "review_judgements": dict(item.get("judgements") or {}),
    }
    return review
