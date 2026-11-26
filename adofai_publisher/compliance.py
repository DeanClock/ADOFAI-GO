from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

from .accuracy import calc_accuracy
from .models import Draft, judgement_hit_count, judgement_sum

TITLE_HARD_LIMIT = 80
TITLE_WARN_LIMIT = 50
DESC_HARD_LIMIT = 2000
MAX_TAGS = 10
TAG_MAX_LEN = 20
VIDEO_EXTENSIONS = {
    ".mp4",
    ".flv",
    ".mov",
    ".mkv",
    ".avi",
    ".wmv",
    ".webm",
    ".m4v",
}
COVER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
DEFAULT_BANNED_WORDS = ("外挂", "作弊器", "代打", "刷分服务")


@dataclass(frozen=True)
class Issue:
    level: str
    field: str
    message: str

    @property
    def is_error(self) -> bool:
        return self.level == "error"

    @property
    def is_warning(self) -> bool:
        return self.level == "warn"


def check_draft(
    draft: Draft,
    banned_words: Iterable[str] | None = None,
    check_video_file: bool = True,
    publish_mode: str = "dual",
) -> list[Issue]:
    issues: list[Issue] = []
    banned = tuple(banned_words or DEFAULT_BANNED_WORDS)
    tuf_mode = publish_mode != "bili_only"

    if check_video_file:
        if not draft.video_path:
            issues.append(Issue("error", "video", "还没有选择视频文件"))
        elif not os.path.isfile(draft.video_path):
            issues.append(Issue("error", "video", "视频文件不存在或已被移动"))
        else:
            ext = os.path.splitext(draft.video_path)[1].lower()
            if ext not in VIDEO_EXTENSIONS:
                issues.append(Issue("warn", "video", f"视频格式 {ext or '未知'} 可能不被 B 站支持"))
            if os.path.getsize(draft.video_path) > 8 * 1024**3:
                issues.append(Issue("warn", "video", "视频超过 8GB，网页投稿可能失败"))

    # 封面分两张：16:9 主图进「个人空间封面」，4:3 副图进「首页推荐封面」，
    # 各自校验存在性 / 格式 / 大小。
    for field, label, path, missing_text in (
        (
            "cover",
            "16:9 主图",
            draft.cover_path,
            "16:9 主图不存在，将使用 B 站自动封面",
        ),
        (
            "cover_43",
            "4:3 副图",
            draft.cover_path_43,
            "4:3 副图不存在，投稿时 4:3 框会复用 16:9 主图",
        ),
    ):
        if not path:
            continue
        if not os.path.isfile(path):
            issues.append(Issue("warn", field, missing_text))
            continue
        ext = os.path.splitext(path)[1].lower()
        if ext not in COVER_EXTENSIONS:
            issues.append(
                Issue("warn", field, f"{label}格式 {ext or '未知'} 可能不被 B 站支持")
            )
        if os.path.getsize(path) > 5 * 1024**2:
            issues.append(Issue("warn", field, f"{label}超过 5MB，建议压缩后再上传"))

    if tuf_mode and not draft.level:
        issues.append(Issue("error", "level", "还没有绑定 TUF 谱面"))
    if tuf_mode and not draft.feeling_rating.strip():
        issues.append(Issue("error", "feeling", "TUF 投稿需要体感难度，例如 G12"))

    title = draft.title.strip()
    if not title:
        issues.append(Issue("error", "title", "标题不能为空"))
    else:
        if len(title) > TITLE_HARD_LIMIT:
            issues.append(Issue("error", "title", f"标题超过 B 站上限 {TITLE_HARD_LIMIT} 字"))
        elif len(title) > TITLE_WARN_LIMIT:
            issues.append(Issue("warn", "title", f"标题超过 {TITLE_WARN_LIMIT} 字，建议精简"))
        if "\n" in title or "\r" in title:
            issues.append(Issue("error", "title", "标题不能包含换行"))
        for word in banned:
            if word and word in title:
                issues.append(Issue("warn", "title", f"标题包含敏感词：{word}"))

    if not draft.description.strip():
        issues.append(Issue("warn", "description", "简介为空，建议附上谱面与成绩信息"))
    elif len(draft.description) > DESC_HARD_LIMIT:
        issues.append(Issue("error", "description", f"简介超过 B 站上限 {DESC_HARD_LIMIT} 字"))

    if not draft.tags:
        issues.append(Issue("warn", "tags", "还没有添加标签"))
    else:
        if len(draft.tags) > MAX_TAGS:
            issues.append(Issue("error", "tags", f"标签最多 {MAX_TAGS} 个"))
        for tag in draft.tags:
            if len(tag) > TAG_MAX_LEN:
                issues.append(Issue("error", "tags", f"标签「{tag}」超过 {TAG_MAX_LEN} 字"))
            if not tag.strip():
                issues.append(Issue("error", "tags", "存在空标签"))

    if tuf_mode:
        total = judgement_sum(draft.judgements)
        # 物量校验用 TUF 的 tilecount 口径（排除提前 / 太慢！！），不能用九格和。
        hits = judgement_hit_count(draft.judgements)
        if total <= 0:
            issues.append(Issue("error", "judgements", "判定次数全为 0"))
        else:
            acc = calc_accuracy(draft.judgements)
            if acc < 0 or acc > 100.0001:
                issues.append(Issue("error", "judgements", "准确率超出合理范围"))
            expected = draft.level.expected_hits if draft.level else None
            if expected and hits != expected:
                level = draft.level
                tiles = int(level.tilecount or 0)
                autos = int(level.auto_tile_count or 0)
                issues.append(
                    Issue(
                        "error",
                        "judgements",
                        f"打击数 {hits} 与可击中物量 {expected} 不一致"
                        f"（判定总数 {total}，共 {tiles} 个砖块，自动砖块 {autos} 个）"
                        "。提前 / 太慢！！不计入打击数，TUF 也是这样",
                    )
                )
            elif expected:
                level = draft.level
                tiles = int(level.tilecount or 0)
                autos = int(level.auto_tile_count or 0)
                issues.append(
                    Issue(
                        "ok",
                        "judgements",
                        f"打击数与物量一致（打击数 {hits}，判定总数 {total}，"
                        f"共 {tiles} 个砖块，自动砖块 {autos} 个）",
                    )
                )

    return issues


def summarize(issues: Iterable[Issue]) -> tuple[int, int]:
    errors = sum(1 for issue in issues if issue.is_error)
    warnings = sum(1 for issue in issues if issue.is_warning)
    return errors, warnings
