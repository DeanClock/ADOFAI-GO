from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ard_parser import (
    ARDProfile,
    load_adofai_data,
    profile_from_chart_data,
)
from .models import LevelInfo


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


@dataclass
class ChartAnalysis:
    path: str = ""
    song: str = ""
    artist: str = ""
    author: str = ""
    game_difficulty: str = ""
    bpm: float | None = None
    tile_count: int = 0
    midspin_count: int = 0
    song_duration_ms: float | None = None
    chart_duration_ms: float | None = None
    hits_per_second: float | None = None
    speed_min: float = 1.0
    speed_max: float = 1.0
    event_summary: dict[str, int] = field(default_factory=dict)
    tuf_id: int | None = None
    error: str = ""
    # "file" = 本地文件分析；"tuf" = 直接分析 TUF 在线谱面 JSON。
    source: str = "file"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "song": self.song,
            "artist": self.artist,
            "author": self.author,
            "game_difficulty": self.game_difficulty,
            "bpm": self.bpm,
            "tile_count": self.tile_count,
            "midspin_count": self.midspin_count,
            "song_duration_ms": self.song_duration_ms,
            "chart_duration_ms": self.chart_duration_ms,
            "hits_per_second": self.hits_per_second,
            "speed_min": self.speed_min,
            "speed_max": self.speed_max,
            "event_summary": dict(self.event_summary),
            "tuf_id": self.tuf_id,
            "error": self.error,
            "source": self.source,
        }

    @property
    def duration_text(self) -> str:
        value = self.song_duration_ms or self.chart_duration_ms
        if not value:
            return "-"
        seconds = int(round(value / 1000))
        return f"{seconds // 60}:{seconds % 60:02d}"

    @property
    def chart_duration_text(self) -> str:
        if not self.chart_duration_ms:
            return "-"
        seconds = int(round(self.chart_duration_ms / 1000))
        return f"{seconds // 60}:{seconds % 60:02d}"

    @property
    def density_text(self) -> str:
        if not self.hits_per_second:
            return "-"
        return f"{self.hits_per_second:.2f} tiles/s"

    @property
    def speed_text(self) -> str:
        if abs(self.speed_max - self.speed_min) < 1e-6:
            return f"{self.speed_min:g}x"
        return f"{self.speed_min:g}x – {self.speed_max:g}x"

    @property
    def event_text(self) -> str:
        if not self.event_summary:
            return "-"
        return " / ".join(
            f"{name} ×{count}"
            for name, count in sorted(
                self.event_summary.items(), key=lambda item: (-item[1], item[0])
            )[:6]
        )

    @property
    def ok(self) -> bool:
        return not self.error


def _tuf_id_from_path(target: Path) -> int | None:
    match = re.search(r"\((\d{4,})\)", target.stem) or re.search(
        r"\((\d{4,})\)", target.parent.name
    )
    return int(match.group(1)) if match else None


def _analyze_loaded(
    data: dict[str, Any],
    profile: ARDProfile,
    *,
    path: str = "",
    tuf_id: int | None = None,
    source: str = "file",
) -> ChartAnalysis:
    """把已解析的谱面 JSON 与 ARDProfile 汇总成界面用的分析结果。"""
    target = Path(path) if path else None
    analysis = ChartAnalysis(path=path, source=source)
    if profile.error:
        analysis.error = profile.error
        return analysis

    analysis.song = profile.song or ""
    if not analysis.song and target is not None:
        analysis.song = (
            target.parent.name
            if target.stem.lower() in {"level", "main"}
            else target.stem
        )
    analysis.artist = profile.artist or ""
    analysis.author = profile.author or ""
    analysis.game_difficulty = str(profile.difficulty or "")
    analysis.bpm = _as_float(profile.bpm)
    analysis.tile_count = int(profile.tile_count or 0)
    analysis.midspin_count = int(profile.midspin_count or 0)
    analysis.song_duration_ms = _as_float(profile.song_duration_ms)
    analysis.event_summary = dict(profile.event_summary)
    analysis.tuf_id = tuf_id if tuf_id is not None else (
        _tuf_id_from_path(target) if target is not None else None
    )

    actions = data.get("actions") if isinstance(data, dict) else None
    events_by_floor: dict[int, list[dict[str, Any]]] = {}
    if isinstance(actions, list):
        for action in actions:
            if not isinstance(action, dict):
                continue
            try:
                floor = int(action.get("floor") or 0)
            except (TypeError, ValueError):
                continue
            kind = str(action.get("eventType") or action.get("type") or "")
            if kind == "SetSpeed":
                events_by_floor.setdefault(floor, []).append(action)

    bpm = analysis.bpm
    if bpm and analysis.tile_count:
        speed = 1.0
        total_seconds = 0.0
        speed_min = 1.0
        speed_max = 1.0
        for floor in range(analysis.tile_count):
            for action in events_by_floor.get(floor, ()):
                value = _as_float(action.get("speed"))
                if value:
                    speed = value
                    speed_min = min(speed_min, speed)
                    speed_max = max(speed_max, speed)
            total_seconds += 60.0 / (bpm * max(speed, 0.01))
        analysis.chart_duration_ms = total_seconds * 1000 if total_seconds else None
        analysis.speed_min = speed_min
        analysis.speed_max = speed_max
        if analysis.chart_duration_ms:
            analysis.hits_per_second = analysis.tile_count / (total_seconds or 1)
    return analysis


def analyze_chart(path: Path | str) -> ChartAnalysis:
    target = Path(path)
    data, error = load_adofai_data(str(target))
    if data is None:
        return ChartAnalysis(path=str(target), error=error, source="file")
    profile = profile_from_chart_data(data, str(target))
    return _analyze_loaded(data, profile, path=str(target), source="file")


def analyze_chart_data(
    data: dict[str, Any],
    *,
    path: str = "",
    tuf_id: int | None = None,
) -> ChartAnalysis:
    """直接分析谱面 JSON（TUF 在线接口返回的对象），不需要本地文件。"""
    source = "tuf" if tuf_id is not None else "file"
    if not isinstance(data, dict):
        return ChartAnalysis(
            path=path,
            tuf_id=tuf_id,
            source=source,
            error="谱面数据不是 JSON 对象",
        )
    profile = profile_from_chart_data(data, path)
    return _analyze_loaded(
        data,
        profile,
        path=path,
        tuf_id=tuf_id,
        source=source,
    )


def analyze_tuf_level(
    level_id: int,
    *,
    lookup: Any = None,
) -> tuple[ChartAnalysis, LevelInfo | None]:
    """用公开接口读取 TUF 谱面 JSON 并分析，返回 (分析结果, TUF 元数据)。

    ``lookup`` 可注入替身（需要 ``fetch_chart_json``，可选 ``get``）；
    读取谱面失败时不抛异常，错误写在 ``analysis.error`` 里。
    """
    from .levellookup import LevelLookup

    number = int(level_id)
    lookup = lookup or LevelLookup()
    data, error = lookup.fetch_chart_json(number)

    level: LevelInfo | None = None
    try:
        level = lookup.get(number)
    except Exception:
        level = None

    if data is None:
        return (
            ChartAnalysis(
                tuf_id=number,
                source="tuf",
                error=error or "无法读取 TUF 在线谱面",
            ),
            level,
        )

    analysis = analyze_chart_data(data, tuf_id=number)
    if analysis.ok and level is not None:
        # 在线谱面 JSON 常常没有填 settings.song / artist / author，
        # 用 TUF 元数据补齐展示；不覆盖谱面自己写明的值。
        analysis.song = analysis.song or level.song
        analysis.artist = analysis.artist or level.artist
        analysis.author = analysis.author or level.charter
        if not analysis.song_duration_ms:
            analysis.song_duration_ms = _as_float(level.duration_ms)
    if level is None and analysis.ok:
        level = LevelInfo(
            tuf_id=number,
            song=analysis.song,
            artist=analysis.artist,
            charter=analysis.author,
            bpm=analysis.bpm,
            duration_ms=analysis.song_duration_ms or analysis.chart_duration_ms,
            tilecount=analysis.tile_count or None,
            midspin_count=analysis.midspin_count,
        )
    return analysis, level
