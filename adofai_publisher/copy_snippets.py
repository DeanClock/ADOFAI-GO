from __future__ import annotations

from typing import Any

SNIPPET_GROUPS = ("判定", "成绩", "手感", "自定义")

DEFAULT_COPY_SNIPPETS: list[dict[str, str]] = [
    {"text": "严判", "group": "判定"},
    {"text": "标判", "group": "判定"},
    {"text": "严判击破", "group": "成绩"},
    {"text": "完美无瑕", "group": "成绩"},
    {"text": "全连", "group": "成绩"},
    {"text": "AP", "group": "成绩"},
    {"text": "FC", "group": "成绩"},
    {"text": "零失误", "group": "成绩"},
    {"text": "手感极佳", "group": "手感"},
]

_NO_SPACE_BEFORE = "（([【{“‘'\"，。！？、；：,.!?;:…）)]】}”’"
_NO_SPACE_AFTER = "）)]】}”’'\"，。！？、；：,.!?;:…"


def default_copy_snippets() -> list[dict[str, str]]:
    return [dict(item) for item in DEFAULT_COPY_SNIPPETS]


def normalize_copy_snippets(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return default_copy_snippets()
    normalized: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            group = "自定义"
        elif isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            group = str(item.get("group") or "").strip()
        else:
            continue
        if not text:
            continue
        if group not in SNIPPET_GROUPS:
            group = "自定义"
        normalized.append({"text": text, "group": group})
    return normalized


def insert_snippet(
    text: str,
    cursor: int,
    snippet: str,
    selection_end: int | None = None,
) -> tuple[str, int]:
    snippet = (snippet or "").strip()
    if not snippet:
        return text, max(0, min(cursor, len(text)))

    if selection_end is not None:
        start = max(0, min(cursor, selection_end, len(text)))
        end = max(0, min(max(cursor, selection_end), len(text)))
        text = text[:start] + text[end:]
        cursor = start
    else:
        cursor = max(0, min(cursor, len(text)))

    prefix = ""
    if cursor > 0 and not text[cursor - 1].isspace() and text[cursor - 1] not in _NO_SPACE_BEFORE:
        prefix = " "

    suffix = ""
    if cursor < len(text) and not text[cursor].isspace() and text[cursor] not in _NO_SPACE_AFTER:
        suffix = " "

    inserted = prefix + snippet + suffix
    updated = text[:cursor] + inserted + text[cursor:]
    return updated, cursor + len(inserted)
