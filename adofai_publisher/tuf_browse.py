from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

from .config import tuf_meta_cache_path
from .levellookup import BASE_URL, USER_AGENT
from .models import LevelInfo

SORT_OPTIONS: list[tuple[str, str]] = [
    ("最近收录", "RECENT"),
    ("难度", "DIFF"),
    ("通关数", "CLEARS"),
    ("总通关数", "TOTAL_CLEARS"),
    ("点赞", "LIKES"),
    ("Base Score", "BASESCORE"),
    ("BPM", "BPM"),
    ("物量", "TILES"),
    ("时长", "TIME"),
    ("随机", "RANDOM"),
]

SPECIAL_DIFFICULTIES = ["Unranked", "P0", "Censored", "Impossible"]

# TUF 网站第二个滑块 Qq→UQ4：不是独立参数，而是把 11 个 SPECIAL 难度名
# 并进 specialDifficulties（与 pguRange 是 OR 关系）。顺序来自
# /v2/database/difficulties 的 sortOrder 77–87；接口里的桶名带范围后缀，
# 少写后缀服务器会把该桶当成不存在的值忽略（只剩 Qq 生效）。
Q_SPECIAL_DIFFICULTIES: tuple[str, ...] = (
    "Qq",
    "GQ0 (G1~G4)",
    "GQ1 (G5~G8)",
    "GQ2 (G9~G12)",
    "GQ3 (G13~G16)",
    "GQ4 (G17~G20)",
    "UQ0 (U1~U4)",
    "UQ1 (U5~U8)",
    "UQ2 (U9~U12)",
    "UQ3 (U13~U16)",
    "UQ4 (U17~U20)",
)
# 滑块把手上的短标签；接口里的桶名带范围后缀（如 "GQ0 (G1~G4)"）。
Q_SPECIAL_SHORT_NAMES: tuple[str, ...] = (
    "Qq",
    "GQ0",
    "GQ1",
    "GQ2",
    "GQ3",
    "GQ4",
    "UQ0",
    "UQ1",
    "UQ2",
    "UQ3",
    "UQ4",
)
Q_SPECIAL_COUNT = len(Q_SPECIAL_DIFFICULTIES)

DIFFICULTY_BANDS = ["P", "G", "U"]

# TUF 的 /v2/database/levels 单页最多返回 100 条，传更大的值也只会回 100 条。
SERVER_PAGE_LIMIT = 100

# 难度 / 标签 / 策展类型属于低频变化的元数据：磁盘缓存默认一天，hash 一致就直接复用。
META_CACHE_VERSION = 1
META_CACHE_MAX_AGE = 86400.0
META_ENDPOINTS = (
    ("difficulties", "/v2/database/difficulties"),
    ("tags", "/v2/database/difficulties/tags"),
    ("curations", "/v2/admin/curations/types"),
)


class TufBrowseError(RuntimeError):
    pass


def pgu_name(index: int) -> str:
    index = max(1, min(int(index), 60))
    band = DIFFICULTY_BANDS[(index - 1) // 20]
    number = (index - 1) % 20 + 1
    return f"{band}{number}"


def pgu_range_text(low: int, high: int) -> str:
    return f"{pgu_name(low)},{pgu_name(high)}"


def q_range_names(q_range: tuple[int, int] | None) -> list[str]:
    """把 Q 滑块的 1..11 区间换成 specialDifficulties 里的桶名。

    ``None`` 表示全选（默认）；空元组表示不发送任何 Q 桶，方便调用方显式关闭。
    旧版 UI 的兼容入口，下拉框改用 :func:`q_difficulty_names`。
    """
    if q_range is None:
        return list(Q_SPECIAL_DIFFICULTIES)
    values = [int(value) for value in q_range if value is not None]
    if not values:
        return []
    low = max(1, min(Q_SPECIAL_COUNT, min(values)))
    high = max(1, min(Q_SPECIAL_COUNT, max(values)))
    return list(Q_SPECIAL_DIFFICULTIES[low - 1 : high])


def q_difficulty_names(q_difficulties: Iterable[str] | None) -> list[str] | None:
    """按接口完整桶名校验 Q 难度子集。

    ``None`` 留给调用方表示「默认全选」；``[]`` 表示完全不带 Q 桶；
    非空列表是精确子集，未知桶名丢弃、重复去重、顺序保持调用方给的顺序。
    """
    if q_difficulties is None:
        return None
    allowed = set(Q_SPECIAL_DIFFICULTIES)
    picked: list[str] = []
    for raw in q_difficulties:
        name = str(raw).strip()
        if name in allowed and name not in picked:
            picked.append(name)
    return picked


def q_bucket_label(name: str) -> str:
    """菜单里显示的桶名：``GQ0 (G1~G4)`` → ``GQ0（G1~G4）``。"""
    return name.replace(" (", "（").replace(")", "）")


@dataclass
class TufPage:
    items: list[LevelInfo]
    total: int
    offset: int
    limit: int
    has_more: bool


class TufBrowser:
    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Referer": "https://tuforums.com/",
                "Origin": "https://tuforums.com",
            }
        )

    def fetch_page(
        self,
        query: str = "",
        offset: int = 0,
        limit: int = SERVER_PAGE_LIMIT,
        sort: str = "RECENT",
        descending: bool = True,
        pgu_range: tuple[int, int] = (1, 60),
        specials: list[str] | None = None,
        q_range: tuple[int, int] | None = None,
        q_difficulties: Iterable[str] | None = None,
        tag_ids: list[int] | None = None,
        curation_ids: list[int] | None = None,
    ) -> TufPage:
        limit = max(1, min(int(limit), SERVER_PAGE_LIMIT))
        offset = max(0, int(offset))
        low, high = sorted((max(1, int(pgu_range[0])), min(60, int(pgu_range[1]))))
        sort_key = sort if sort in {key for _label, key in SORT_OPTIONS} else "RECENT"
        sort_value = f"{sort_key}_{'DESC' if descending else 'ASC'}"

        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "query": (query or "").strip(),
            "sort": sort_value,
            "deletedFilter": "hide",
            "clearedFilter": "show",
            "pguRange": pgu_range_text(low, high),
            "availableDlFilter": "show",
        }
        allowed_specials = set(SPECIAL_DIFFICULTIES) | set(Q_SPECIAL_DIFFICULTIES)
        selected = [
            item for item in (specials or []) if item in allowed_specials
        ]
        # 下拉框传完整桶名子集；没传时退回旧的 1..11 区间参数（默认全选）。
        if q_difficulties is not None:
            q_names = q_difficulty_names(q_difficulties) or []
        else:
            q_names = q_range_names(q_range)
        for name in q_names:
            if name not in selected:
                selected.append(name)
        if selected:
            params["specialDifficulties"] = ",".join(selected)

        facet: dict[str, Any] = {"v": 1, "combine": "and"}
        if tag_ids:
            facet["tags"] = {"mode": "simple", "op": "or", "ids": [int(item) for item in tag_ids]}
        if curation_ids:
            facet["curationTypes"] = {
                "mode": "simple",
                "op": "or",
                "ids": [int(item) for item in curation_ids],
            }
        if len(facet) > 2:
            params["facetQuery"] = json.dumps(facet, ensure_ascii=False, separators=(",", ":"))
        if sort_key == "RANDOM":
            params["seed"] = random.randint(1, 2**31 - 1)

        try:
            response = self.session.get(
                f"{BASE_URL}/v2/database/levels",
                params=params,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise TufBrowseError(f"TUF 请求失败：{exc}") from exc
        if response.status_code != 200:
            raise TufBrowseError(f"TUF 返回 {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TufBrowseError("TUF 返回了无法解析的数据") from exc

        results = payload.get("results") if isinstance(payload, dict) else None
        items = [LevelInfo.from_tuf(item) for item in results or [] if isinstance(item, dict)]
        total = int(payload.get("total") or 0)
        has_more = bool(payload.get("hasMore"))
        if not has_more and results:
            has_more = offset + len(results) < total
        return TufPage(
            items=items,
            total=total,
            offset=int(payload.get("offset") or offset),
            limit=int(payload.get("limit") or limit),
            has_more=has_more,
        )

    def fetch_meta(
        self,
        *,
        cache_path: str | Path | None = None,
        max_age: float = META_CACHE_MAX_AGE,
        force: bool = False,
        use_hash: bool = True,
    ) -> dict[str, Any]:
        """读取难度 / 标签 / 策展类型，并落磁盘缓存。

        流程：先读缓存；再用 ``/difficulties/hash`` 判断是否需要刷新。
        hash 一致时只用缓存、不发三个列表请求；hash 请求失败但缓存未超龄时
        用缓存并标 ``stale``。缓存不可用或已超龄才真正拉三个端点；部分失败时
        用缓存补齐对应 key，只有三个端点全部成功才写盘（原子替换，写失败忽略）。
        """
        path = Path(cache_path) if cache_path is not None else tuf_meta_cache_path()
        cached = _read_meta_cache(path)

        remote_hash = ""
        hash_error = ""
        if use_hash:
            remote_hash, hash_error = self._fetch_meta_hash()

        if not force and cached is not None:
            if use_hash and remote_hash:
                if cached.get("hash") == remote_hash:
                    return _cached_meta_result(cached, stale=False)
            elif _cache_age_seconds(cached) <= max_age:
                # 没开 hash 校验、或 hash 请求失败：缓存没过期就直接用。
                return _cached_meta_result(
                    cached,
                    stale=bool(hash_error) or not use_hash,
                )

        meta: dict[str, Any] = {
            "difficulties": [],
            "tags": [],
            "curations": [],
            "errors": [],
        }
        cached_meta = (cached or {}).get("meta") or {}
        succeeded = 0
        for key, endpoint in META_ENDPOINTS:
            ok = False
            try:
                response = self.session.get(
                    f"{BASE_URL}{endpoint}", timeout=self.timeout
                )
                if response.status_code != 200:
                    meta["errors"].append(f"{endpoint} HTTP {response.status_code}")
                else:
                    data = response.json()
                    if isinstance(data, list):
                        meta[key] = data
                        ok = True
                        succeeded += 1
                    else:
                        meta["errors"].append(f"{endpoint} 返回格式异常")
            except (requests.RequestException, ValueError) as exc:
                meta["errors"].append(f"{endpoint}: {exc}")
            if not ok:
                fallback = cached_meta.get(key)
                if isinstance(fallback, list) and fallback:
                    meta[key] = fallback
                    meta["errors"].append(f"{endpoint} 使用本地缓存补齐")

        fetched_at = ""
        if succeeded == len(META_ENDPOINTS):
            fetched_at = _utc_now_iso()
            _write_meta_cache(
                path,
                {
                    "version": META_CACHE_VERSION,
                    "fetched_at": fetched_at,
                    "hash": remote_hash,
                    "meta": {
                        key: meta[key] for key, _endpoint in META_ENDPOINTS
                    },
                },
            )
        elif cached is not None:
            fetched_at = str(cached.get("fetched_at") or "")

        return {
            "difficulties": meta["difficulties"],
            "tags": meta["tags"],
            "curations": meta["curations"],
            "errors": meta["errors"],
            "cached": False,
            "stale": succeeded != len(META_ENDPOINTS),
            "fetched_at": fetched_at,
        }

    def _fetch_meta_hash(self) -> tuple[str, str]:
        """读取元数据版本 hash；失败返回 (空串, 错误信息)，不抛异常。"""
        try:
            response = self.session.get(
                f"{BASE_URL}/v2/database/difficulties/hash",
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            return "", f"网络请求失败：{exc}"
        if response.status_code != 200:
            return "", f"TUF 返回 {response.status_code}"
        try:
            payload = response.json()
        except ValueError:
            return "", "TUF 返回了无法解析的数据"
        value = payload.get("hash") if isinstance(payload, dict) else None
        if value is None or value == "":
            return "", "返回里没有 hash"
        return str(value).strip(), ""


def _read_meta_cache(path: Path) -> dict[str, Any] | None:
    """读缓存；版本不符、结构损坏或读不了都当没有缓存。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != META_CACHE_VERSION:
        return None
    meta = raw.get("meta")
    if not isinstance(meta, dict):
        return None
    cleaned: dict[str, list[Any]] = {}
    for key, _endpoint in META_ENDPOINTS:
        value = meta.get(key)
        if not isinstance(value, list):
            return None
        cleaned[key] = value
    return {
        "fetched_at": str(raw.get("fetched_at") or ""),
        "hash": str(raw.get("hash") or ""),
        "meta": cleaned,
    }


def _cache_age_seconds(cached: dict[str, Any]) -> float:
    stamp = str(cached.get("fetched_at") or "")
    if not stamp:
        return float("inf")
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - parsed).total_seconds(), 0.0)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cached_meta_result(
    cached: dict[str, Any],
    *,
    stale: bool = False,
) -> dict[str, Any]:
    meta = cached.get("meta") or {}
    return {
        "difficulties": list(meta.get("difficulties") or []),
        "tags": list(meta.get("tags") or []),
        "curations": list(meta.get("curations") or []),
        "errors": [],
        "cached": True,
        "stale": bool(stale),
        "fetched_at": str(cached.get("fetched_at") or ""),
    }


def _write_meta_cache(path: Path, payload: dict[str, Any]) -> None:
    """tmp + replace 原子写入；任何写盘失败都静默忽略。"""
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
