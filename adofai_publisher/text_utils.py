from __future__ import annotations

import re
from typing import Any

_RICH_TEXT_RE = re.compile(
    r"</?(?:color|size|b|i|u|s|sup|sub|br|align|indent|line-height|sprite|link|mark"
    r"|gradient|font|material|noparse|lowercase|uppercase|smallcaps|voffset|cspace"
    r"|mspace|width|alpha|rotate|scale)(?:=[^<>]*)?\s*/?>",
    re.IGNORECASE,
)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def strip_rich_text(value: Any) -> str:
    """去掉 Unity / TextMeshPro 富文本标签，返回适合展示的纯文本。"""
    if value is None:
        return ""
    text = str(value)
    text = _BR_RE.sub(" ", text)
    text = _RICH_TEXT_RE.sub("", text)
    text = text.replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s{2,}", " ", text).strip()
