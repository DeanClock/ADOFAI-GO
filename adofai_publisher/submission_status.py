"""投稿状态判定与状态迁移。

投稿记录共有四种状态：

- ``draft``：草稿，还没上传 B 站。
- ``bili_uploaded``：视频已上传 B 站，但 TUF 还没投稿（仍应出现在待投稿队列）。
- ``bili_only``：只投 B 站，不会提交 TUF。
- ``submitted``：TUF 已投稿，这是唯一代表“已投稿”的状态。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

TUF_SUBMITTED_STATUS = "submitted"
KEEP_STATUSES = frozenset({"submitted", "bili_only"})


def is_tuf_submitted(record: Mapping[str, Any] | None) -> bool:
    """记录是否代表 TUF 已投稿。

    只有 ``status == "submitted"`` 才为真；``bili_uploaded``、``bili_only``、
    缺失 status 或其他状态都不算已投稿。
    """

    if not isinstance(record, Mapping):
        return False
    return record.get("status") == TUF_SUBMITTED_STATUS


def next_status_after_bvid(current_status: Any, publish_mode: Any) -> str:
    """拿到 BV 号并写回记录后应处于的状态。

    ``submitted`` 与 ``bili_only`` 保持不变（不得把已投稿降级）；
    双投模式下其余状态（草稿、已上传 B 站等）变为 ``bili_uploaded``，
    仅 B 站模式下变为 ``bili_only``。
    """

    status = str(current_status or "")
    if status in KEEP_STATUSES:
        return status
    if str(publish_mode or "dual") == "bili_only":
        return "bili_only"
    return "bili_uploaded"
