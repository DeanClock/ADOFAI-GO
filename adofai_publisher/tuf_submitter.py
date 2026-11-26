from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

import requests

from .levellookup import BASE_URL, USER_AGENT
from .models import JUDGEMENT_KEYS, JUDGEMENT_LABELS, adjust_perfect_for_midspin
from .tuf_review import extract_submission_item

# 工具目前只提交 3.4.0；提交层、确认弹窗与日志都从这里取，保证同一口径。
DEFAULT_ADOFIA_VERSION = "3.4.0"

# TUF 文档：/v2/auth/session 的 503 表示会话查询失败，客户端应重试；
# 502/504 同属网关侧的临时故障，一并按可重试处理。
RETRYABLE_SESSION_STATUSES = (502, 503, 504)

_logger = logging.getLogger("adofai")


@dataclass
class SubmitResult:
    ok: bool
    status: int
    message: str
    data: dict[str, Any]


@dataclass
class ReviewListResult:
    """投稿列表 / 谱面通关列表的读取结果。"""

    ok: bool
    status: int
    message: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    raw: Any = None
    auth_error: bool = False
    retryable: bool = False


@dataclass
class ReviewItemResult:
    """单条投稿记录的读取结果。

    ``id_kind`` 记录这次用的是 submission id 还是 pass id 打开成功。
    """

    ok: bool
    status: int
    message: str = ""
    item: dict[str, Any] | None = None
    raw: Any = None
    auth_error: bool = False
    retryable: bool = False
    id_kind: str = ""


class _PlaywrightResponse:
    def __init__(self, response: Any) -> None:
        self.status_code = int(response.status)
        self.headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
        self._body = response.body()

    @property
    def content(self) -> bytes:
        return self._body

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


class TufClient:
    def __init__(
        self,
        cookies: Mapping[str, str] | None = None,
        timeout: float = 25.0,
        request_context: Any = None,
    ) -> None:
        self.timeout = timeout
        self.request_context = request_context
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Referer": "https://tuforums.com/",
                "Origin": "https://tuforums.com",
            }
        )
        if cookies:
            self.session.cookies.update(dict(cookies))
        self.csrf_token = ""
        # 新投稿接口要求带提交者（passer / passerId），从 /v2/auth/session 的用户资料里取
        self.player_name = ""
        self.player_id: int | None = None

    def refresh_session(
        self,
        retries: int = 2,
        retry_delay: float = 0.6,
        sleep: Callable[[float], None] | None = None,
    ) -> dict[str, Any]:
        """读取 TUF 会话；502/503/504 视为服务端临时故障，自动重试。

        失败返回 ``{"user": None, "status": code, "retryable": bool,
        "error": text}``。失败时不清空已有的 csrf_token / player_name /
        player_id——这些值可能来自上一次成功的会话，只是这次检查没成功。
        """
        wait = sleep or time.sleep
        attempts = max(0, int(retries)) + 1
        last_status = 0
        last_error = ""
        for attempt in range(attempts):
            try:
                response = self._get(f"{BASE_URL}/v2/auth/session")
            except requests.RequestException as exc:
                last_status = 0
                last_error = str(exc)
                retryable = True
            else:
                last_status = int(response.status_code)
                if last_status == 200:
                    try:
                        data = response.json() if response.content else {}
                    except ValueError as exc:
                        return {
                            "user": None,
                            "status": last_status,
                            "retryable": False,
                            "error": f"TUF 会话数据无法解析：{exc}",
                        }
                    if not isinstance(data, dict):
                        return {
                            "user": None,
                            "status": last_status,
                            "retryable": False,
                            "error": "TUF 会话数据格式异常",
                        }
                    token = (
                        data.get("csrfToken")
                        or response.headers.get("x-csrf-token")
                        or ""
                    )
                    if token:
                        self.csrf_token = token
                    user = data.get("user") or {}
                    player = user.get("player") or {}
                    self.player_name = str(
                        player.get("name")
                        or user.get("nickname")
                        or user.get("username")
                        or ""
                    ).strip()
                    player_id = player.get("id") or user.get("playerId")
                    try:
                        self.player_id = (
                            int(player_id) if player_id not in (None, "") else None
                        )
                    except (TypeError, ValueError):
                        self.player_id = None
                    return data
                retryable = last_status in RETRYABLE_SESSION_STATUSES
                last_error = f"TUF 返回 {last_status}"
            if retryable and attempt + 1 < attempts:
                wait(retry_delay)
                continue
            return {
                "user": None,
                "status": last_status,
                "retryable": retryable,
                "error": last_error,
            }
        # 理论上不会走到这里；保留兜底，避免返回 None。
        return {
            "user": None,
            "status": last_status,
            "retryable": False,
            "error": last_error or "TUF 会话检查失败",
        }

    def ensure_session(self) -> bool:
        """没有 CSRF token 时先读一次会话；已有 token 视为已登录。"""

        if self.csrf_token:
            return True
        session = self.refresh_session()
        return bool(session.get("user") or {})

    def list_submissions(
        self,
        kind: str = "pass",
        status: str = "all",
        query: str = "",
        page: int = 1,
        limit: int = 100,
        sort: str = "",
    ) -> ReviewListResult:
        """读取当前登录用户的投稿列表（``/v2/form/submissions``）。

        需要登录；TUF 没有公开响应 schema，这里只做容错解析。
        """

        kind = kind if kind in ("all", "pass", "level") else "pass"
        status = status if status in ("all", "pending", "approved", "declined") else "all"
        try:
            page = max(1, int(page))
        except (TypeError, ValueError):
            page = 1
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            limit = 100
        params: list[tuple[str, str]] = [
            ("type", kind),
            ("status", status),
            ("page", str(page)),
            ("limit", str(limit)),
        ]
        query = (query or "").strip()
        if query:
            params.append(("query", query))
        if sort:
            params.append(("sort", sort))
        url = f"{BASE_URL}/v2/form/submissions?{urlencode(params)}"
        try:
            response = self._get(url)
        except requests.RequestException as exc:
            return ReviewListResult(
                False, 0, f"网络请求失败：{exc}", retryable=True
            )
        return self._parse_list_response(response)

    def get_submission(
        self,
        submission_id: Any = "",
        kind: str = "pass",
        *,
        pass_id: Any = "",
    ) -> ReviewItemResult:
        """读取自己的一条投稿记录（``/v2/form/pass/{id}``）。

        优先按 submission id 读；404 且同时给了 ``pass_id`` 时，再用 pass id
        兜底重试一次。成功时 ``id_kind`` 说明用的是哪一类编号。
        """

        path = "level" if kind == "level" else "pass"
        submission_text = str(submission_id or "").strip()
        pass_text = str(pass_id or "").strip()
        attempts: list[tuple[str, str]] = []
        if re.fullmatch(r"[0-9]{1,20}", submission_text):
            attempts.append((submission_text, "submission"))
        if (
            re.fullmatch(r"[0-9]{1,20}", pass_text)
            and pass_text != submission_text
        ):
            attempts.append((pass_text, "pass"))
        if not attempts:
            return ReviewItemResult(False, 0, "TUF 记录编号无效")

        last: ReviewItemResult | None = None
        for candidate, id_kind in attempts:
            result = self._get_submission_once(candidate, path, id_kind)
            if result.ok:
                _logger.info(
                    "TUF 投稿记录读取成功：使用 %s id",
                    "submission" if id_kind == "submission" else "pass",
                )
                return result
            last = result
            if result.status != 404:
                return result
        return last or ReviewItemResult(False, 0, "TUF 记录编号无效")

    def _get_submission_once(
        self, record_id: str, path: str, id_kind: str
    ) -> ReviewItemResult:
        try:
            response = self._get(f"{BASE_URL}/v2/form/{path}/{record_id}")
        except requests.RequestException as exc:
            return ReviewItemResult(
                False, 0, f"网络请求失败：{exc}", retryable=True
            )
        status = int(getattr(response, "status_code", 0) or 0)
        text = response.text if response.content else ""
        if status in (401, 403):
            return ReviewItemResult(
                False, status, "TUF 登录态失效，请重新登录", auth_error=True
            )
        if status in RETRYABLE_SESSION_STATUSES:
            return ReviewItemResult(
                False, status, "TUF 服务暂不可用，请稍后重试", retryable=True
            )
        if status == 404:
            return ReviewItemResult(False, status, "TUF 上找不到这条投稿记录")
        if status != 200:
            return ReviewItemResult(
                False, status, self._error_message(text) or f"TUF 返回 {status}"
            )
        payload = self._json_or_none(response, text)
        if payload is None:
            if _looks_like_html(text):
                return ReviewItemResult(
                    False, status, "TUF 返回了登录页，可能需要重新登录"
                )
            return ReviewItemResult(False, status, "TUF 返回了无法解析的数据")
        item = extract_submission_item(payload)
        if item is None:
            return ReviewItemResult(
                False, status, "TUF 返回的审核记录格式无法识别", raw=payload
            )
        return ReviewItemResult(
            True, status, "", item=item, raw=payload, id_kind=id_kind
        )

    def fetch_level_passes(self, level_id: Any) -> ReviewListResult:
        """读取某个谱面的通关记录（公开接口，无需登录）。"""

        try:
            level = int(level_id)
        except (TypeError, ValueError):
            return ReviewListResult(False, 0, "TUF 谱面编号无效")
        if level <= 0:
            return ReviewListResult(False, 0, "TUF 谱面编号无效")
        try:
            response = self._get(f"{BASE_URL}/v2/database/passes/level/{level}")
        except requests.RequestException as exc:
            return ReviewListResult(
                False, 0, f"网络请求失败：{exc}", retryable=True
            )
        return self._parse_list_response(response)

    def _parse_list_response(self, response: Any) -> ReviewListResult:
        status = int(getattr(response, "status_code", 0) or 0)
        text = response.text if response.content else ""
        if status in (401, 403):
            return ReviewListResult(
                False, status, "TUF 登录态失效，请重新登录", auth_error=True
            )
        if status in RETRYABLE_SESSION_STATUSES:
            return ReviewListResult(
                False, status, "TUF 服务暂不可用，请稍后重试", retryable=True
            )
        if status != 200:
            return ReviewListResult(
                False, status, self._error_message(text) or f"TUF 返回 {status}"
            )
        payload = self._json_or_none(response, text)
        if payload is None:
            if _looks_like_html(text):
                return ReviewListResult(
                    False, status, "TUF 返回了登录页，可能需要重新登录"
                )
            return ReviewListResult(False, status, "TUF 返回了无法解析的数据")
        items, total = _extract_list_items(payload)
        return ReviewListResult(
            True, status, "", items=items, total=total, raw=payload
        )

    @staticmethod
    def _json_or_none(response: Any, text: str) -> Any:
        if not text:
            return {}
        try:
            return response.json()
        except ValueError:
            return None

    @staticmethod
    def _error_message(text: str) -> str:
        if not text:
            return ""
        try:
            payload = json.loads(text)
        except ValueError:
            return ""
        if isinstance(payload, dict):
            for key in ("error", "message", "detail"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:300]
        return ""

    def submit_pass(
        self,
        level_id: int,
        video_link: str,
        judgements: dict[str, int],
        speed: float = 1.0,
        feeling_rating: str = "",
        is_no_hold_tap: bool = False,
        key_count: int | None = None,
        is_12k: bool = False,
        is_16k: bool = False,
        adofai_version: str = DEFAULT_ADOFIA_VERSION,
        details: str = "",
        expected_rating: str = "",
        video_from_own_channel: bool = True,
        extra: dict[str, Any] | None = None,
        is_x_perfect: bool | None = None,
        midspin_count: int = 0,
        log: Callable[[str], None] | None = None,
    ) -> SubmitResult:
        if int(level_id) <= 0:
            return SubmitResult(False, 0, "TUF 谱面编号无效", {})
        if not re.fullmatch(
            r"https://www\.bilibili\.com/video/BV[0-9A-Za-z]{10}/?",
            video_link.strip(),
        ):
            return SubmitResult(False, 0, "视频链接不是有效的 B 站 BV 链接", {})
        if not self.csrf_token or not self.player_name:
            session = self.refresh_session()
            if not (session.get("user") or {}):
                status = int(session.get("status") or 0)
                if session.get("retryable"):
                    return SubmitResult(
                        False, status, "TUF 服务暂不可用，请稍后重试", {}
                    )
                return SubmitResult(
                    False, status, "TUF 登录态失效，请重新登录", {}
                )
        # TUF 口径（前端 ``usePassCoreForm`` 的 ``midspinPerfectDecrement``）：
        # 只有 adofaiVersion 低于 3.4.0 时才把中旋数从完美里扣掉，3.4.0+ 跳过。
        # 本工具默认提交 3.4.0，所以默认路径不扣；perfect < midspin 时 TUF
        # 不调整，这里同样不动。
        judgements, midspin_reduced = prepare_submission_judgements(
            judgements, midspin_count, adofai_version
        )
        midspin_note = f"完美已按中旋数减去 {midspin_reduced}" if midspin_reduced > 0 else ""
        if is_x_perfect is None:
            # 兼容旧调用：没有显式传模式时按「有 -完美/完美+」推断。
            x_perfect = bool(
                judgements.get("perfectMinus") or judgements.get("perfectPlus")
            )
        else:
            x_perfect = bool(is_x_perfect)
        expected = expected_rating.strip()
        # 与 TUF 网页 /submission/pass 当前提交的字段保持一致（2026-11 接口改版）
        payload: dict[str, Any] = {
            "levelId": int(level_id),
            "videoLink": video_link.strip(),
            "title": "",
            "passer": self.player_name,
            "passerId": self.player_id,
            "passerRequest": False,
            "speed": float(speed or 1.0),
            "feelingDifficulty": feeling_rating.strip(),
            "expectedDifficulty": expected or None,
            "feelingExplanation": None,
            "details": details.strip(),
            "keyCount": int(key_count) if key_count else None,
            # 字段名以 TUF 前端为准：``usePassCoreForm`` 的 ``Oe()`` 会把界面上的
            # tooEarly/early/late 表单键转成内部键 earlyDouble/earlySingle/
            # lateSingle 再发出去，服务端表单校验 ``os()`` 也只认内部键。之前
            # 直接发 tooEarly/early/late，服务端静默丢弃，TUF 记录里这些格全存 0
            #（2026-11-21 pass 71967 的教训）。这里只发内部键，不再同时发别名：
            # 服务端会忽略未知字段，别名没有作用，只会让字段来源出现两套口径。
            "earlyDouble": int(judgements.get("earlyDouble") or 0),
            "earlySingle": int(judgements.get("earlySingle") or 0),
            "lateSingle": int(judgements.get("lateSingle") or 0),
            "lateDouble": int(judgements.get("lateDouble") or 0),
            "ePerfect": int(judgements.get("ePerfect") or 0),
            "lPerfect": int(judgements.get("lPerfect") or 0),
            "perfectMinus": int(judgements.get("perfectMinus") or 0),
            "perfect": int(judgements.get("perfect") or 0),
            "perfectPlus": int(judgements.get("perfectPlus") or 0),
            "isNoHoldTap": bool(is_no_hold_tap),
            "isAdofaiV2": False,
            "adofaiVersion": adofai_version,
            "isXPerfectMode": x_perfect,
        }
        if is_12k:
            payload["is12K"] = True
        if is_16k:
            payload["is16K"] = True
        if extra:
            payload.update(extra)

        if log is not None:
            # 提交前给 DC 一行摘要，方便投完直接对照 TUF 记录。
            # 只含判定数字与版本，不含 Cookie、玩家名或完整 JSON。
            try:
                log(
                    format_judgement_summary(
                        payload,
                        midspin_reduced,
                        str(payload.get("adofaiVersion") or DEFAULT_ADOFIA_VERSION),
                        payload.get("isXPerfectMode"),
                    )
                )
            except Exception:
                pass

        try:
            response = self._post_payload(payload)
        except requests.RequestException as exc:
            return SubmitResult(False, 0, f"网络请求失败：{exc}", {})

        data: dict[str, Any] = {}
        if response is None:
            return SubmitResult(False, 0, "没有可用的提交请求", {})
        if response.content:
            try:
                parsed = response.json()
                if isinstance(parsed, dict):
                    data = parsed
            except ValueError:
                data = {"text": response.text[:500]}

        if response.status_code in (200, 201) and data.get("success") is not False:
            message = data.get("message") or "TUF 记录提交成功"
            record = data.get("pass") or data.get("record") or data.get("submission") or {}
            record_id = ""
            if isinstance(record, dict):
                record_id = str(record.get("id") or "")
            result_data = {**data, "record_id": record_id}
            # 提交响应里若同时给了 submission id，回读时优先用它。
            submission_id = str(
                data.get("submissionId") or data.get("submission_id") or ""
            ).strip()
            if not submission_id:
                top_id = data.get("id")
                if top_id not in (None, ""):
                    submission_id = str(top_id).strip()
            result_data["submission_id"] = submission_id
            # 实际提交的判定值（已含中旋调整），供提交后回读比对。
            result_data["submitted_judgements"] = dict(judgements)
            if midspin_note:
                result_data["midspin_note"] = midspin_note
                message = f"{message}（{midspin_note}）"
            return SubmitResult(True, response.status_code, message, result_data)
        if response.status_code in (401, 403):
            return SubmitResult(False, response.status_code, "TUF 登录态失效，请重新登录", data)
        if response.status_code == 422:
            return SubmitResult(False, response.status_code, _validation_message(data), data)
        if response.status_code in RETRYABLE_SESSION_STATUSES:
            # 投稿 POST 不自动重试：网络层重发可能造成重复投稿，这里只提示稍后再试。
            return SubmitResult(
                False, response.status_code, "TUF 服务暂不可用，请稍后重试", data
            )
        reason = data.get("error") or data.get("message") or data.get("text")
        if reason:
            return SubmitResult(
                False,
                response.status_code,
                f"TUF 提交失败：{str(reason)[:300]}",
                data,
            )
        return SubmitResult(
            False,
            response.status_code,
            f"TUF 提交失败（HTTP {response.status_code}）",
            data,
        )

    def _post_payload(self, payload: dict[str, Any]) -> requests.Response:
        headers = dict(self.session.headers)
        headers.update({"Content-Type": "application/json", "X-Form-Type": "pass"})
        if self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        url = f"{BASE_URL}/v2/form/pass/submit"
        if self.request_context is not None:
            try:
                response = self.request_context.post(
                    url,
                    data=json.dumps(payload),
                    headers=headers,
                    timeout=int(self.timeout * 1000),
                )
            except Exception as exc:
                raise requests.RequestException(str(exc)) from exc
            return _PlaywrightResponse(response)  # type: ignore[return-value]
        return self.session.post(url, json=payload, headers=headers, timeout=self.timeout)

    def _get(self, url: str) -> Any:
        if self.request_context is not None:
            try:
                response = self.request_context.get(
                    url,
                    headers=dict(self.session.headers),
                    timeout=int(self.timeout * 1000),
                )
            except Exception as exc:
                raise requests.RequestException(str(exc)) from exc
            return _PlaywrightResponse(response)
        return self.session.get(url, timeout=self.timeout)


def prepare_submission_judgements(
    judgements: Mapping[str, Any],
    midspin_count: int | None = 0,
    adofai_version: str = DEFAULT_ADOFIA_VERSION,
) -> tuple[dict[str, int], int]:
    """按 TUF 规则算出最终判定值，以及完美被中旋扣掉的数量。

    提交、确认弹窗与日志都走这里，保证「弹窗里写的就是真正提交的值」。
    返回（内部键判定值, 扣掉的中旋数）。
    """
    try:
        raw_perfect = int(judgements.get("perfect") or 0)
    except (TypeError, ValueError):
        raw_perfect = 0
    adjusted = adjust_perfect_for_midspin(judgements, midspin_count, adofai_version)
    midspin_reduced = max(raw_perfect - int(adjusted["perfect"]), 0)
    return adjusted, midspin_reduced


def format_judgement_summary(
    values: Mapping[str, Any],
    midspin_reduced: int = 0,
    adofai_version: str = DEFAULT_ADOFIA_VERSION,
    is_x_perfect: Any = None,
) -> str:
    """给 DC 核对的判定摘要：提交日志与提交确认弹窗共用这一份格式。

    数值取自最终判定值（已含中旋调整）；只含判定数字与游戏版本，不含
    Cookie、玩家信息或完整 JSON。
    """

    def _count(key: str) -> int:
        try:
            return int(values.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    parts: list[str] = []
    for key in JUDGEMENT_KEYS:
        value = _count(key)
        # 「太慢！！」网页表单没有输入框，通常为 0；为 0 时不占日志长度。
        if key == "lateDouble" and value == 0:
            continue
        if key == "perfect" and midspin_reduced > 0:
            parts.append(
                f"完美 {value + midspin_reduced} → {value}（扣中旋 {midspin_reduced}）"
            )
        else:
            parts.append(f"{JUDGEMENT_LABELS[key]} {value}")
    if midspin_reduced > 0:
        midspin_text = f"游戏版本 {adofai_version}，扣中旋 {midspin_reduced}"
    else:
        midspin_text = f"游戏版本 {adofai_version}，不扣中旋"
    mode = "开" if is_x_perfect else "关"
    return f"TUF 判定：{' / '.join(parts)}；{midspin_text}；X 完美模式{mode}"


def _looks_like_html(text: str) -> bool:
    head = (text or "").lstrip()[:200].lower()
    return head.startswith("<!doctype") or head.startswith("<html") or head.startswith("<")


def _extract_list_items(payload: Any) -> tuple[list[dict[str, Any]], int]:
    """从列表响应里取出条目；顶层 list 或 results/submissions/items/data 均可。

    TUF 文档只承诺 ``results`` + ``total``，但实际响应没有 schema 约束，
    这里允许一层嵌套，其他形状一律按空列表处理。
    """

    raw_items: list[Any] | None = None
    total_value: Any = None
    if isinstance(payload, list):
        raw_items = payload
        total_value = len(payload)
    elif isinstance(payload, Mapping):
        for key in ("results", "submissions", "items", "records", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                raw_items = value
                break
            if isinstance(value, Mapping):
                for inner in ("results", "submissions", "items", "records"):
                    if isinstance(value.get(inner), list):
                        raw_items = value[inner]
                        total_value = value.get("total")
                        break
                if raw_items is not None:
                    break
        if raw_items is None:
            raw_items = []
        if total_value is None:
            total_value = payload.get("total")
    else:
        return [], 0

    items: list[dict[str, Any]] = []
    for entry in raw_items:
        if not isinstance(entry, Mapping):
            continue
        item = extract_submission_item(entry)
        if item is not None:
            items.append(item)
    try:
        total = int(total_value or 0)
    except (TypeError, ValueError):
        total = 0
    return items, total


def _validation_message(data: dict[str, Any]) -> str:
    errors = data.get("errors") or data.get("error") or data.get("details")
    if isinstance(errors, list):
        parts = [str(item.get("message") if isinstance(item, dict) else item) for item in errors]
        return "提交校验失败：" + "；".join(part for part in parts if part)
    if isinstance(errors, dict):
        parts = [f"{key}: {value}" for key, value in errors.items()]
        return "提交校验失败：" + "；".join(parts)
    if errors:
        return f"提交校验失败：{errors}"
    return "TUF 拒绝了这条记录，请核对判定与视频链接"
