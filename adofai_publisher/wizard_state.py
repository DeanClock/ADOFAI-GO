from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .config import Settings, draft_path
from .models import Draft


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def load_draft() -> Draft | None:
    path = draft_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return Draft.from_dict(data)


def save_draft(draft: Draft) -> None:
    draft.updated_at = datetime.now().isoformat(timespec="seconds")
    path = draft_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(draft.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def clear_draft() -> None:
    path = draft_path()
    if path.is_file():
        try:
            path.unlink()
        except OSError:
            pass


def template_values(draft: Draft, accuracy_text: str) -> dict[str, str]:
    level = draft.level
    values: dict[str, str] = {
        "song": level.song if level else "",
        "artist": level.artist if level else "",
        "charter": level.charter if level else "",
        "difficulty": level.difficulty if level else "",
        "bpm": str(int(level.bpm)) if level and level.bpm else "",
        "duration": level.duration_text if level else "",
        "tuf_id": str(level.tuf_id) if level and level.tuf_id else "",
        "tuf_url": level.url if level else "",
        "accuracy": accuracy_text,
        "bvid": draft.bvid,
        "partition": draft.partition,
        "speed": f"{draft.speed:g}",
    }
    for key, value in draft.judgements.items():
        values[key] = str(value)
    return values


def render_text(template: str, values: dict[str, str]) -> str:
    class _Safe(dict):
        def __missing__(self, key: str) -> str:
            return ""

    try:
        return template.format_map(_Safe(values))
    except (ValueError, IndexError):
        return template


def make_tags(draft: Draft, settings: Settings, limit: int = 10) -> list[str]:
    tags: list[str] = []
    candidates = list(settings.get("default_tags") or [])
    if draft.level:
        candidates.extend([draft.level.song, draft.level.artist, draft.level.difficulty])
    for tag in candidates:
        tag = (tag or "").strip()
        if tag and tag not in tags and len(tag) <= 20:
            tags.append(tag)
        if len(tags) >= limit:
            break
    return tags


def generate_copy(draft: Draft, settings: Settings, accuracy_text: str) -> tuple[str, str, list[str]]:
    values = template_values(draft, accuracy_text)
    title = render_text(settings.get("title_template") or "", values).strip()
    description = render_text(settings.get("desc_template") or "", values).strip()
    return title, description, make_tags(draft, settings)


def draft_to_record(draft: Draft, accuracy_text: str, **extra: Any) -> dict[str, Any]:
    level = draft.level
    record = {
        "video_path": draft.video_path,
        "cover_path": draft.cover_path,
        "cover_path_43": draft.cover_path_43,
        "song": level.song if level else "",
        "artist": level.artist if level else "",
        "charter": level.charter if level else "",
        "difficulty": level.difficulty if level else "",
        "bpm": level.bpm if level else None,
        "duration_ms": level.duration_ms if level else None,
        "tilecount": level.tilecount if level else None,
        "auto_tile_count": level.auto_tile_count if level else 0,
        "base_score": level.base_score if level else None,
        "level_video_link": level.video_link if level else "",
        "tuf_id": level.tuf_id if level else None,
        "tuf_url": level.url if level else "",
        "judgements": dict(draft.judgements),
        "accuracy": accuracy_text,
        "title": draft.title,
        "description": draft.description,
        "tags": list(draft.tags),
        "partition": draft.partition,
        "creative_declaration": draft.creative_declaration,
        "bvid": draft.bvid,
        "bili_url": f"https://www.bilibili.com/video/{draft.bvid}" if draft.bvid else "",
        "tuf_record_id": draft.tuf_record_id,
        "review_status": draft.review_status,
        "review_checked_at": draft.review_checked_at,
        "review_message": draft.review_message,
        "review_assignee": draft.review_assignee,
        "review_flags": draft.review_flags,
        "review_judgements": dict(draft.review_judgements),
        "status": draft.status,
        "is_no_hold_tap": draft.is_no_hold_tap,
        "is_12k": draft.is_12k,
        "is_16k": draft.is_16k,
        "x_perfect": draft.x_perfect,
    }
    record.update(extra)
    return record
