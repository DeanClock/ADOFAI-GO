from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .text_utils import strip_rich_text

BACKUP_RE = re.compile(r"^backup\d*$", re.IGNORECASE)

# ADOFAI 在 angleData 里用 999 表示中旋格（不是「角度 999 度」）。
MIDSPIN_ANGLE = 999.0

# 旧版 pathData 里 `!` 表示中旋格（不是路径字符）。
LEGACY_MIDSPIN_CHAR = "!"

AUTOPLAY_TILES_EVENT = "AutoPlayTiles"

# 压缩包/谱面文件夹里没有任何 .adofai 时统一用这个错误文本。
# 谱面库扫描据此跳过（并清理）误导入的发布包、安装包等无谱面内容。
NO_CHART_CONTENT_ERROR = "谱面包里没有找到 .adofai 内容"


def _angle_value(item: Any) -> float | None:
    """把 angleData 的一项转成角度；无法识别的项返回 None（按非中旋跳过）。"""
    if isinstance(item, bool):
        return None
    try:
        return float(item)
    except (TypeError, ValueError):
        return None


@dataclass
class ARDProfile:
    path: str = ""
    tile_count: int = 0
    zero_angle_count: int = 0
    midspin_count: int = 0
    auto_tile_count: int = 0
    angle_len: int = 0
    event_summary: dict[str, int] = field(default_factory=dict)
    version: Any = None
    bpm: Any = None
    song: str = ""
    artist: str = ""
    author: str = ""
    difficulty: Any = None
    hitsound: str = ""
    song_filename: str = ""
    song_duration_ms: Any = None
    notes: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def playable_hits(self) -> int:
        """可击物量：总格数扣除中旋格与自动砖，口径同 TUF 的 tilecount - autoTileCount。"""
        return max(self.tile_count - self.midspin_count - self.auto_tile_count, 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "tile_count": self.tile_count,
            "zero_angle_count": self.zero_angle_count,
            "midspin_count": self.midspin_count,
            "auto_tile_count": self.auto_tile_count,
            "angle_len": self.angle_len,
            "event_summary": dict(self.event_summary),
            "version": self.version,
            "bpm": self.bpm,
            "song": self.song,
            "artist": self.artist,
            "author": self.author,
            "difficulty": self.difficulty,
            "hitsound": self.hitsound,
            "song_filename": self.song_filename,
            "song_duration_ms": self.song_duration_ms,
            "notes": list(self.notes),
            "error": self.error,
        }


def _is_macosx_member(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return bool(parts) and "__macosx" in parts[0].lower()


def _member_sort_key(name: str) -> tuple[int, int, int, str]:
    """谱面成员排序键：level/main 优先，备份与附属谱面靠后。"""
    stem = PurePosixPath(name).stem.lower()
    if BACKUP_RE.match(stem):
        rank = 5
    elif stem.startswith(("level", "main")):
        rank = 0 if stem == "level" else 1
    elif stem.startswith(("credit", "effect", "camera", "sub")):
        rank = 4
    else:
        rank = 2
    depth = 0 if len(PurePosixPath(name).parts) == 1 else 1
    return (rank, depth, len(name), name)


def find_chart_member(names: list[str]) -> str:
    """从压缩包里选出最像主谱面的 .adofai 条目。"""
    candidates = [
        (_member_sort_key(name), name)
        for name in names
        if name.lower().endswith(".adofai") and not _is_macosx_member(name)
    ]
    if not candidates:
        return ""
    return min(candidates)[1]


@dataclass
class ChartVariant:
    """谱面包（压缩包/文件夹）里的一份 .adofai 及其解析结果。"""

    member: str
    profile: ARDProfile

    @property
    def tile_count(self) -> int:
        return self.profile.tile_count

    @property
    def midspin_count(self) -> int:
        return self.profile.midspin_count

    @property
    def auto_tile_count(self) -> int:
        return self.profile.auto_tile_count

    @property
    def playable_hits(self) -> int:
        return self.profile.playable_hits

    @property
    def song(self) -> str:
        return self.profile.song

    @property
    def artist(self) -> str:
        return self.profile.artist

    @property
    def author(self) -> str:
        return self.profile.author

    @property
    def version(self) -> Any:
        return self.profile.version

    @property
    def error(self) -> str:
        return self.profile.error

    def to_dict(self) -> dict[str, Any]:
        data = self.profile.to_dict()
        data["member"] = self.member
        data["playable_hits"] = self.playable_hits
        return data


def list_chart_variants(path: str | Path) -> list[ChartVariant]:
    """列出一个谱面包里所有 .adofai 变体（压缩包内成员，或文件夹下的文件）。

    返回项按 :func:`find_chart_member` 同样的偏好稳定排序：level/main 在前，
    嵌在子目录里的次之，backup/credit/effect 等附属谱面最后。单个 .adofai
    文件也可以传进来，返回只含它自己的一项。读不了的包返回空列表。
    """
    target = Path(path)
    variants: list[ChartVariant] = []
    if not target.exists():
        return []

    if target.is_dir():
        files: list[tuple[str, Path]] = []
        try:
            children = list(target.rglob("*.adofai"))
        except OSError:
            return []
        for file in children:
            try:
                relative = file.relative_to(target).as_posix()
            except ValueError:
                relative = file.name
            if _is_macosx_member(relative):
                continue
            files.append((relative, file))
        files.sort(key=lambda item: _member_sort_key(item[0]))
        for relative, file in files:
            profile = ARDProfile(path=str(file))
            try:
                payload = file.read_bytes()
            except OSError:
                profile.error = "无法读取谱面文件"
            else:
                _fill_profile_from_payload(profile, payload)
            variants.append(ChartVariant(member=relative, profile=profile))
        return variants

    try:
        is_archive = zipfile.is_zipfile(target)
    except OSError:
        return []

    if not is_archive:
        profile = parse_ard(str(target))
        return [ChartVariant(member=target.name, profile=profile)]

    try:
        with zipfile.ZipFile(target) as archive:
            names = sorted(
                (
                    name
                    for name in archive.namelist()
                    if name.lower().endswith(".adofai")
                    and not _is_macosx_member(name)
                ),
                key=_member_sort_key,
            )
            for name in names:
                profile = ARDProfile(path=name)
                try:
                    payload = archive.read(name)
                except (KeyError, OSError, RuntimeError):
                    profile.error = "谱面文件读取失败"
                else:
                    _fill_profile_from_payload(profile, payload)
                variants.append(ChartVariant(member=name, profile=profile))
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return []
    return variants


def default_chart_member(container: str | Path) -> str:
    """返回容器（压缩包 / 单个谱面文件）默认选中的成员名。

    文件夹容器没有唯一的「默认成员」，由调用方（谱面库扫描）自行决定，
    这里返回空字符串。读不了的包返回空字符串。
    """
    target = Path(container)
    if target.is_dir():
        return ""
    try:
        if zipfile.is_zipfile(target):
            with zipfile.ZipFile(target) as archive:
                return find_chart_member(archive.namelist())
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return ""
    return target.name


def parse_chart_member(container: str | Path, member: str = "") -> ARDProfile:
    """只解析容器里指定的那一份谱面，不枚举其它变体。

    ``container`` 可以是压缩包、文件夹或单个 .adofai 文件；``member`` 是压缩包
    成员名，或相对文件夹的路径。``member`` 为空时用 :func:`default_chart_member`
    的默认选择。扫描/切换变体时用这个入口，避免为几十 MB 的包每次枚举全部成员。
    """
    target = Path(container)
    profile = ARDProfile(path=member or str(target))
    if not target.exists():
        profile.error = "谱面文件不存在"
        return profile

    if target.is_dir():
        if not member:
            profile.error = "没有指定谱面变体"
            return profile
        file = target / PurePosixPath(str(member).replace("\\", "/"))
        try:
            file.resolve().relative_to(target.resolve())
        except (OSError, ValueError):
            profile.error = "谱面变体路径无效"
            return profile
        if not file.is_file():
            profile.error = f"谱面变体不存在：{member}"
            return profile
        profile.path = str(file)
        try:
            payload = file.read_bytes()
        except OSError:
            profile.error = "无法读取谱面文件"
            return profile
        _fill_profile_from_payload(profile, payload)
        return profile

    try:
        is_archive = zipfile.is_zipfile(target)
    except OSError:
        profile.error = "无法读取谱面文件"
        return profile

    if not is_archive:
        try:
            payload = target.read_bytes()
        except OSError:
            profile.error = "无法读取谱面文件"
            return profile
        profile.path = str(target)
        _fill_profile_from_payload(profile, payload)
        return profile

    try:
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
            name = member or find_chart_member(names)
            if not name:
                profile.error = NO_CHART_CONTENT_ERROR
                return profile
            if name not in names:
                profile.error = f"谱面变体不存在：{name}"
                return profile
            try:
                payload = archive.read(name)
            except (KeyError, OSError, RuntimeError):
                profile.error = "谱面文件读取失败"
                return profile
    except (zipfile.BadZipFile, OSError, RuntimeError):
        profile.error = "谱面包解析失败"
        return profile

    profile.path = name
    _fill_profile_from_payload(profile, payload)
    return profile


def _decode_chart_payload(payload: bytes) -> tuple[dict[str, Any] | None, str]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, "谱面文件编码无法识别"

    # 老编辑器留下的缺陷：尾随逗号、重复逗号、相邻数组元素之间少了逗号，
    # 以及字符串里的裸控制字符（TUF #10551 的 main.adofai 有裸 CR）。
    # 候选按「能修好的最小改动优先」排序，这些缺陷常常同时出现，所以组合后的
    # 形态也要试；控制字符排在逗号修复之后，正常谱面和只有逗号问题的谱面都不会
    # 走到它。全部失败才报告错误。
    def candidates():
        yield text
        stripped = _strip_trailing_commas(text)
        yield stripped
        folded = _fold_duplicate_commas(text)
        yield folded
        folded_stripped = _strip_trailing_commas(folded)
        yield folded_stripped
        yield _insert_missing_commas(stripped)
        yield _insert_missing_commas(folded_stripped)

        sanitized = _escape_control_chars_in_strings(text)
        if sanitized == text:
            return
        yield sanitized
        sanitized_stripped = _strip_trailing_commas(sanitized)
        yield sanitized_stripped
        sanitized_folded = _fold_duplicate_commas(sanitized)
        yield sanitized_folded
        sanitized_folded_stripped = _strip_trailing_commas(sanitized_folded)
        yield sanitized_folded_stripped
        yield _insert_missing_commas(sanitized_stripped)
        yield _insert_missing_commas(sanitized_folded_stripped)

    last_error: ValueError | None = None
    seen: set[str] = set()
    for candidate in candidates():
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            data = json.loads(candidate)
        except ValueError as exc:
            last_error = exc
            continue
        if not isinstance(data, dict):
            return None, "谱面 JSON 顶层不是对象"
        return data, ""

    return None, _json_error_text(last_error)


def _json_error_text(error: ValueError | None) -> str:
    if isinstance(error, json.JSONDecodeError):
        message = error.msg or "格式错误"
        return (
            f"谱面 JSON 解析失败（第 {error.lineno} 行第 {error.colno} 列：{message}，"
            "已尝试修复字符串内控制字符与缺失/多余的逗号）"
        )
    return "谱面 JSON 解析失败（已尝试修复字符串内控制字符与缺失/多余的逗号）"


def load_adofai_data(path: str) -> tuple[dict[str, Any] | None, str]:
    """读取 .adofai、.ard、.zip、.adozip 谱面包，返回 (JSON 对象, 错误信息)。"""
    try:
        is_archive = zipfile.is_zipfile(path)
    except OSError:
        return None, "无法读取谱面文件"
    if is_archive:
        try:
            with zipfile.ZipFile(path) as archive:
                target_name = find_chart_member(archive.namelist())
                if not target_name:
                    return None, NO_CHART_CONTENT_ERROR
                payload = archive.read(target_name)
        except (zipfile.BadZipFile, KeyError, OSError, RuntimeError):
            return None, "谱面包解析失败"
    else:
        try:
            payload = Path(path).read_bytes()
        except OSError:
            return None, "无法读取谱面文件"

    if not payload:
        return None, "谱面文件为空"
    return _decode_chart_payload(payload)


def profile_from_chart_data(data: dict[str, Any], path: str = "") -> ARDProfile:
    """直接解析已经读进内存的谱面 JSON（例如 TUF 在线谱面接口返回的对象）。"""
    profile = ARDProfile(path=path)
    if not isinstance(data, dict):
        profile.error = "谱面 JSON 顶层不是对象"
        return profile
    _fill_profile(profile, data)
    return profile


def profile_from_chart_bytes(payload: bytes, path: str = "") -> ARDProfile:
    """解析内存里的 .adofai 字节内容，容错规则与读文件时一致。"""
    profile = ARDProfile(path=path)
    _fill_profile_from_payload(profile, payload)
    return profile


def parse_ard(path: str) -> ARDProfile:
    data, error = load_adofai_data(path)
    if data is None:
        profile = ARDProfile(path=path)
        profile.error = error
        return profile
    return profile_from_chart_data(data, path)


def _fill_profile_from_payload(profile: ARDProfile, payload: bytes) -> None:
    if not payload:
        profile.error = "谱面文件为空"
        return
    data, error = _decode_chart_payload(payload)
    if data is None:
        profile.error = error
        return
    _fill_profile(profile, data)


def _fill_profile(profile: ARDProfile, data: dict[str, Any]) -> None:
    settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    profile.version = settings.get("version")
    profile.bpm = settings.get("bpm")
    profile.song = strip_rich_text(settings.get("song") or "")
    profile.artist = strip_rich_text(settings.get("artist") or "")
    profile.author = strip_rich_text(settings.get("author") or "")
    profile.difficulty = settings.get("difficulty")
    profile.hitsound = settings.get("hitsound") or ""
    profile.song_filename = settings.get("songFilename") or ""
    profile.song_duration_ms = settings.get("songDuration") or settings.get("duration")

    angles = data.get("angleData")
    if isinstance(angles, list):
        profile.angle_len = len(angles)
        profile.tile_count = len(angles)
        zero_count = 0
        midspin_count = 0
        for item in angles:
            value = _angle_value(item)
            if value is None:
                continue
            if value == 0:
                zero_count += 1
            elif value == MIDSPIN_ANGLE:
                midspin_count += 1
        profile.zero_angle_count = zero_count
        profile.midspin_count = midspin_count
    if not profile.tile_count:
        _apply_legacy_path_data(profile, data.get("pathData"))

    actions = data.get("actions")
    if isinstance(actions, list):
        summary: dict[str, int] = {}
        for action in actions:
            if not isinstance(action, dict):
                continue
            kind = str(action.get("eventType") or action.get("type") or "Unspecified")
            summary[kind] = summary.get(kind, 0) + 1
        profile.event_summary = summary
        profile.auto_tile_count = _count_auto_tiles(actions, profile.tile_count)

    if not profile.tile_count:
        profile.notes.append("没有读到 angleData / pathData，无法判断物量")
    if settings.get("songFilename"):
        profile.notes.append("谱面自带音频文件：" + str(settings["songFilename"]))


def _event_floor(action: dict[str, Any]) -> int | None:
    floor = action.get("floor")
    if isinstance(floor, bool):
        return None
    if isinstance(floor, (int, float)):
        return int(floor)
    if isinstance(floor, str):
        try:
            return int(float(floor))
        except ValueError:
            return None
    return None


def _event_enabled(action: dict[str, Any]) -> bool:
    enabled = action.get("enabled")
    if enabled is None:
        # 事件本身叫 AutoPlayTiles，缺省按「打开」理解。
        return True
    if isinstance(enabled, str):
        return enabled.strip().lower() not in ("false", "0", "")
    return bool(enabled)


def _event_active(action: dict[str, Any]) -> bool:
    """``active`` 关卡是否真正生效；``false`` / ``0`` / ``"false"`` 表示没生效。

    旧谱面没有这个字段，按 ``True`` 处理。字符串比较容错大小写与首尾空白。
    """
    active = action.get("active")
    if active is None:
        return True
    if isinstance(active, str):
        return active.strip().lower() not in ("false", "0", "")
    return bool(active)


def _count_auto_tiles(actions: list[Any], tile_count: int) -> int:
    """统计自动砖（AutoPlayTiles）覆盖的格数。

    AutoPlayTiles 用 ``{"floor": S, "eventType": "AutoPlayTiles", "enabled": …}``
    成对开关，覆盖区间是 ``[S, E)``——关闭事件所在的楼层 E 本身不算自动砖
    （实测 TUF #15785：349→350、484→488、673→674、930→939 共 15 格，
    与 TUF 的 autoTileCount 一致）。只开不关则一直算到最后一格。

    被禁用的关卡不参与状态机：``active`` 为 ``false`` / ``0`` / ``"false"``
    时整条事件跳过——既不开启也不关闭自动砖。旧谱面没有 ``active``，按
    ``True`` 处理；``enabled`` 仍是开关的取值。

    已知边界：``eventTag`` / ``SetConditionalEvents`` 在运行时改变 ``active``
    的情况不建模，只看谱面里写死的 ``active``。
    """
    events: list[tuple[int, bool]] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        kind = action.get("eventType") or action.get("type")
        if str(kind or "") != AUTOPLAY_TILES_EVENT:
            continue
        if not _event_active(action):
            continue
        floor = _event_floor(action)
        if floor is None:
            continue
        events.append((floor, _event_enabled(action)))

    if not events:
        return 0
    events.sort(key=lambda item: item[0])

    total = 0
    open_at: int | None = None
    for floor, enabled in events:
        if enabled:
            if open_at is None:
                open_at = floor
        elif open_at is not None:
            if floor > open_at:
                total += floor - open_at
            open_at = None
    if open_at is not None:
        # 只开不关：覆盖到最后一格（含），楼层从 1 数到 tile_count。
        if tile_count > 0:
            end = tile_count + 1
        else:
            end = max(floor for floor, _ in events) + 1
        if end > open_at:
            total += end - open_at

    if tile_count > 0:
        total = min(total, tile_count)
    return max(total, 0)


def _insert_missing_commas(text: str) -> str:
    """补齐数组/对象元素之间漏掉的逗号（例如 ``}{`` 相邻）。

    逐字符扫描，字符串内部（含转义）原样跳过，只在结构层面判断：如果上一个
    完整值后面直接出现下一个值的开头，就补一个逗号。不处理缺冒号等其它损坏。
    """
    out: list[str] = []
    index = 0
    length = len(text)
    prev = ""  # "" / "value" / "open" / "comma" / "colon"
    in_number = False
    while index < length:
        char = text[index]
        if char in " \t\r\n":
            in_number = False
            out.append(char)
            index += 1
            continue
        if in_number and (char.isdigit() or char in "+-.eE"):
            out.append(char)
            index += 1
            continue
        in_number = False
        if char == '"':
            if prev == "value":
                out.append(",")
            end = index + 1
            escaped = False
            while end < length:
                current = text[end]
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    break
                end += 1
            out.append(text[index : end + 1])
            index = end + 1
            prev = "value"
            continue
        if char in "{[":
            if prev == "value":
                out.append(",")
            out.append(char)
            index += 1
            prev = "open"
            continue
        if char in "}]":
            out.append(char)
            index += 1
            prev = "value"
            continue
        if char == ",":
            out.append(char)
            index += 1
            prev = "comma"
            continue
        if char == ":":
            out.append(char)
            index += 1
            prev = "colon"
            continue
        if char == "-" or char.isdigit():
            if prev == "value":
                out.append(",")
            out.append(char)
            index += 1
            in_number = True
            prev = "value"
            continue
        if char.isalpha():
            if prev == "value":
                out.append(",")
            out.append(char)
            index += 1
            while index < length and (text[index].isalnum() or text[index] in "+-.eE"):
                out.append(text[index])
                index += 1
            prev = "value"
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _apply_legacy_path_data(profile: ARDProfile, path_data: Any) -> None:
    """兼容旧版（settings.version 很低）只有 pathData 的谱面。

    旧格式里每个字符代表一格，物量 = 字符数，**不是**字符数 + 1：
    pathData 的第一个字符就是第一格的走向，起点格不单独占字符。
    实测依据：样本 ``Turkish March.zip``（作者 Misoguy，settings.version=2）
    的 pathData 长 1283，TUF #3048（同一张谱面，物量 1283）与之完全一致；
    压缩包里 actions 的 floor 最大也是 1283（新版谱面 floor 可等于物量）。

    中旋在旧格式里用 ``!`` 编码，而不是 angleData 的 999；TUF 的 ``tilecount``
    只数可击格，等于「字符数 - ``!`` 数」，所以 ``tile_count`` 保持字符数，
    ``midspin_count`` 记 ``!`` 的个数。实测对照（TUF 数值）：
    #1 5012/182、#2 1730/107、#54 2022/52、#3118 1920/45。旧格式没有可靠的
    0 度编码，``zero_angle_count`` 保持 0。
    """
    if not isinstance(path_data, str):
        return
    tiles = path_data.strip()
    if not tiles:
        return
    profile.tile_count = len(tiles)
    profile.angle_len = len(tiles)
    profile.zero_angle_count = 0
    profile.midspin_count = tiles.count(LEGACY_MIDSPIN_CHAR)
    profile.notes.append(
        "旧版 pathData 格式：物量按 pathData 字符数统计，其中 `!` 是中旋格"
        "（已计入中旋数）"
    )


def _fold_duplicate_commas(text: str) -> str:
    """把字符串外连续出现的多个逗号折叠成一个（例如 ``"difficulty": 1,,``）。

    字符串内部（含转义）原样保留，只处理结构层。
    """
    out: list[str] = []
    in_string = False
    escaped = False
    last_significant = ""
    for char in text:
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            out.append(char)
            last_significant = char
            continue
        if char in " \t\r\n":
            out.append(char)
            continue
        if char == ",":
            if last_significant == ",":
                continue
            out.append(char)
            last_significant = char
            continue
        out.append(char)
        last_significant = char
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    """ADOFAI 编辑器会写出 `, }` 这类尾随逗号，标准 JSON 不接受。"""
    out: list[str] = []
    in_string = False
    escaped = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == ",":
            look = index + 1
            while look < length and text[look] in " \t\r\n":
                look += 1
            if look < length and text[look] in "}]":
                index += 1
                continue
        out.append(char)
        index += 1
    return "".join(out)


_CONTROL_ESCAPES = {"\r": "\\r", "\n": "\\n", "\t": "\\t"}


def _escape_control_chars_in_strings(text: str) -> str:
    """把 JSON 字符串内部的裸控制字符转成合法转义。

    TUF #10551 的 ``main.adofai`` 由编辑器写坏了：``"levelDesc"`` 里是真实的
    0x0D（后面跟着转义序列 ``\\n``），而标准 JSON 不允许字符串内出现裸控制字符。
    这里只改**字符串内部**：CR/LF/TAB 转成 ``\\r``/``\\n``/``\\t``，其余
    ``< 0x20`` 的字符转成 ``\\u00XX``，解析出来的字符串值仍然带换行。字符串外
    的 CR/LF/TAB 是合法 JSON 空白，原样保留；反斜杠转义状态按 JSON 规则跟踪，
    ``\\"`` 不会提前结束字符串，``\\\\`` 里的引号也不会被当成转义。

    没有需要处理的地方时返回原对象，方便调用方判断是否值得多试一个候选。
    """
    out: list[str] = []
    in_string = False
    escaped = False
    changed = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
                out.append(char)
                continue
            if char == "\\":
                escaped = True
                out.append(char)
                continue
            if char == '"':
                in_string = False
                out.append(char)
                continue
            if char < " ":
                out.append(_CONTROL_ESCAPES.get(char, f"\\u{ord(char):04x}"))
                changed = True
                continue
            out.append(char)
            continue
        if char == '"':
            in_string = True
        out.append(char)
    if not changed:
        return text
    return "".join(out)
