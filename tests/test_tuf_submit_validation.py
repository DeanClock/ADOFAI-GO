from adofai_publisher.tuf_submitter import TufClient


def test_submit_rejects_invalid_level_id():
    result = TufClient().submit_pass(
        0,
        "https://www.bilibili.com/video/BV1xx411c7mD",
        {},
    )
    assert not result.ok
    assert result.status == 0
    assert "编号" in result.message


def test_submit_rejects_non_bilibili_video_link():
    result = TufClient().submit_pass(
        1,
        "https://example.com/video/BV1xx411c7mD",
        {},
    )
    assert not result.ok
    assert result.status == 0
    assert "BV" in result.message


class FakeApiResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self.headers = {
            "content-type": "application/json",
            "x-csrf-token": "csrf-from-browser",
        }
        self._payload = payload

    def body(self):
        import json

        return json.dumps(self._payload).encode("utf-8")


class FakeRequestContext:
    def __init__(self):
        self.calls = []

    def get(self, url, **_kwargs):
        self.calls.append(("GET", url))
        return FakeApiResponse(
            {
                "user": {
                    "id": "1",
                    "username": "demo",
                    "nickname": "Demo",
                    "playerId": 10539,
                    "player": {"id": 10539, "name": "Demo"},
                },
                "csrfToken": "csrf",
            }
        )

    def post(self, url, data=None, headers=None, **_kwargs):
        self.calls.append(("POST", url, data, headers))
        return FakeApiResponse({"success": True, "pass": {"id": "record-1"}})


def test_tuf_client_uses_browser_request_context():
    request_context = FakeRequestContext()
    client = TufClient(request_context=request_context)

    session = client.refresh_session()
    assert session["user"]["username"] == "demo"
    assert client.csrf_token == "csrf"

    result = client.submit_pass(
        1,
        "https://www.bilibili.com/video/BV1xx411c7mD",
        {"perfect": 100},
        1.0,
        "G1",
    )

    assert result.ok is True
    assert result.data["record_id"] == "record-1"
    assert [call[0] for call in request_context.calls] == ["GET", "POST"]
    assert request_context.calls[1][2]
    assert request_context.calls[1][3]["Content-Type"] == "application/json"
    assert request_context.calls[1][3]["Origin"] == "https://tuforums.com"


def test_tuf_payload_matches_web_form_schema():
    import json

    request_context = FakeRequestContext()
    client = TufClient(request_context=request_context)
    client.refresh_session()

    client.submit_pass(
        12345,
        "https://www.bilibili.com/video/BV1xx411c7mD",
        {
            "ePerfect": 1,
            "perfectMinus": 2,
            "perfect": 3,
            "perfectPlus": 4,
            "lPerfect": 5,
            "earlySingle": 6,
            "lateSingle": 7,
            "earlyDouble": 8,
            "lateDouble": 9,
        },
        1.0,
        "G1",
        is_no_hold_tap=True,
        is_12k=True,
    )

    body = json.loads(request_context.calls[1][2])
    assert body["passer"] == "Demo"
    assert body["passerId"] == 10539
    assert body["passerRequest"] is False
    assert body["feelingDifficulty"] == "G1"
    assert body["perfect"] == 3
    # 字段名以 TUF 前端为准：Oe() 发出的内部键就是 earlyDouble/earlySingle/
    # lateSingle/lateDouble，服务端校验 os() 也只认这套。
    assert body["earlyDouble"] == 8
    assert body["earlySingle"] == 6
    assert body["lateSingle"] == 7
    assert body["lateDouble"] == 9
    # 旧别名一个都不发：服务端会静默丢弃，留着只会造成两套口径。
    for legacy in ("tooEarly", "early", "late", "tooLate"):
        assert legacy not in body, legacy
    assert body["isXPerfectMode"] is True
    assert body["isNoHoldTap"] is True
    assert body["is12K"] is True
    # 新接口是扁平字段，旧的嵌套 judgements 已经不被接受
    assert "judgements" not in body


def test_tuf_payload_respects_explicit_x_perfect_mode():
    import json

    request_context = FakeRequestContext()
    client = TufClient(request_context=request_context)
    client.refresh_session()

    client.submit_pass(
        12345,
        "https://www.bilibili.com/video/BV1xx411c7mD",
        {"perfect": 10, "ePerfect": 2, "lPerfect": 3},
        is_x_perfect=False,
    )
    body = json.loads(request_context.calls[1][2])
    assert body["isXPerfectMode"] is False
    assert body["perfectMinus"] == 0
    assert body["perfectPlus"] == 0

    other_context = FakeRequestContext()
    other_client = TufClient(request_context=other_context)
    other_client.refresh_session()
    other_client.submit_pass(
        12345,
        "https://www.bilibili.com/video/BV1xx411c7mD",
        {"perfect": 10, "perfectMinus": 2, "perfectPlus": 3},
        is_x_perfect=True,
    )
    body = json.loads(other_context.calls[1][2])
    assert body["isXPerfectMode"] is True
    assert body["perfectMinus"] == 2
    assert body["perfectPlus"] == 3


def test_tuf_payload_infers_x_perfect_when_unset():
    import json

    request_context = FakeRequestContext()
    client = TufClient(request_context=request_context)
    client.refresh_session()

    client.submit_pass(
        12345,
        "https://www.bilibili.com/video/BV1xx411c7mD",
        {"perfect": 10, "perfectPlus": 1},
    )
    body = json.loads(request_context.calls[1][2])
    assert body["isXPerfectMode"] is True


def test_tuf_payload_subtracts_midspins_for_pre_3_4_0():
    import json

    request_context = FakeRequestContext()
    client = TufClient(request_context=request_context)
    client.refresh_session()

    result = client.submit_pass(
        12345,
        "https://www.bilibili.com/video/BV1xx411c7mD",
        {"perfect": 100, "ePerfect": 2, "lateDouble": 1},
        midspin_count=7,
        adofai_version="3.3.1",
    )
    body = json.loads(request_context.calls[1][2])
    assert body["adofaiVersion"] == "3.3.1"
    assert body["perfect"] == 93
    assert body["ePerfect"] == 2
    assert result.data["midspin_note"] == "完美已按中旋数减去 7"
    assert "完美已按中旋数减去 7" in result.message


def test_tuf_payload_keeps_perfect_for_3_4_0_with_midspins():
    import json

    request_context = FakeRequestContext()
    client = TufClient(request_context=request_context)
    client.refresh_session()

    result = client.submit_pass(
        12345,
        "https://www.bilibili.com/video/BV1xx411c7mD",
        {"perfect": 100, "ePerfect": 2, "lateDouble": 1},
        midspin_count=7,
    )
    body = json.loads(request_context.calls[1][2])
    # 默认提交 3.4.0，TUF 前端 midspinPerfectDecrement 在 3.4.0+ 跳过。
    assert body["adofaiVersion"] == "3.4.0"
    assert body["perfect"] == 100
    assert "midspin_note" not in result.data


def test_tuf_payload_keeps_perfect_when_below_midspins():
    import json

    request_context = FakeRequestContext()
    client = TufClient(request_context=request_context)
    client.refresh_session()

    result = client.submit_pass(
        12345,
        "https://www.bilibili.com/video/BV1xx411c7mD",
        {"perfect": 3},
        midspin_count=10,
        adofai_version="3.3.0",
    )
    body = json.loads(request_context.calls[1][2])
    assert body["perfect"] == 3
    assert "midspin_note" not in result.data


def test_tuf_payload_unparsable_version_keeps_perfect():
    import json

    request_context = FakeRequestContext()
    client = TufClient(request_context=request_context)
    client.refresh_session()

    result = client.submit_pass(
        12345,
        "https://www.bilibili.com/video/BV1xx411c7mD",
        {"perfect": 100},
        midspin_count=7,
        adofai_version="latest",
    )
    body = json.loads(request_context.calls[1][2])
    assert body["perfect"] == 100
    assert "midspin_note" not in result.data


def test_tuf_payload_without_midspins_is_unchanged():
    import json

    request_context = FakeRequestContext()
    client = TufClient(request_context=request_context)
    client.refresh_session()

    result = client.submit_pass(
        12345,
        "https://www.bilibili.com/video/BV1xx411c7mD",
        {"perfect": 10},
        midspin_count=0,
        adofai_version="3.3.0",
    )
    body = json.loads(request_context.calls[1][2])
    assert body["perfect"] == 10
    assert "midspin_note" not in result.data


class RecordingContext(FakeRequestContext):
    """把 POST 记进共享事件表，用来确认摘要日志发生在真正提交之前。"""

    def __init__(self, events):
        super().__init__()
        self.events = events

    def post(self, url, data=None, headers=None, **_kwargs):
        self.events.append("POST")
        return super().post(url, data=data, headers=headers, **_kwargs)


def test_tuf_submit_logs_judgement_summary_once_before_post():
    events: list[str] = []
    logs: list[str] = []
    request_context = RecordingContext(events)
    client = TufClient(request_context=request_context)
    client.refresh_session()

    client.submit_pass(
        5756,
        "https://www.bilibili.com/video/BV1xx411c7mD",
        {
            "perfect": 1230,
            "ePerfect": 24,
            "perfectMinus": 345,
            "perfectPlus": 91,
            "lPerfect": 5,
            "earlyDouble": 2,
            "earlySingle": 8,
            "lateSingle": 1,
        },
        midspin_count=48,
        log=lambda line: (events.append("LOG"), logs.append(line)),
    )

    # 只在真正 POST 之前打一次，且不泄露玩家信息。
    assert events == ["LOG", "POST"]
    assert len(logs) == 1
    assert logs[0] == (
        "TUF 判定：-完美 345 / 完美 1230 / 完美+ 91 / 稍快 24 / 稍慢 5 / "
        "提前 2 / 太快 8 / 太慢 1；游戏版本 3.4.0，不扣中旋；X 完美模式开"
    )
    assert "Demo" not in logs[0]
    assert "csrf" not in logs[0]
    assert "{" not in logs[0]


def test_tuf_submit_log_shows_midspin_deduction_for_old_version():
    logs: list[str] = []
    request_context = FakeRequestContext()
    client = TufClient(request_context=request_context)
    client.refresh_session()

    client.submit_pass(
        12345,
        "https://www.bilibili.com/video/BV1xx411c7mD",
        {"perfect": 100, "ePerfect": 2, "lateDouble": 1},
        midspin_count=7,
        adofai_version="3.3.0",
        log=logs.append,
    )

    assert len(logs) == 1
    assert "完美 100 → 93（扣中旋 7）" in logs[0]
    assert "游戏版本 3.3.0，扣中旋 7" in logs[0]
    assert "太慢！！ 1" in logs[0]
    assert "X 完美模式关" in logs[0]


def test_submit_log_uses_the_same_summary_as_the_confirm_dialog():
    """弹窗与提交日志必须同源：同一组判定、同一版本默认值、同一格式函数。"""
    from adofai_publisher.tuf_submitter import (
        DEFAULT_ADOFIA_VERSION,
        format_judgement_summary,
        prepare_submission_judgements,
    )

    values = {
        "perfectMinus": 345,
        "perfect": 1230,
        "perfectPlus": 91,
        "ePerfect": 24,
        "lPerfect": 5,
        "earlyDouble": 2,
        "earlySingle": 8,
        "lateSingle": 1,
        "lateDouble": 0,
    }
    request_context = FakeRequestContext()
    client = TufClient(request_context=request_context)
    client.refresh_session()
    logs: list[str] = []

    client.submit_pass(
        5756,
        "https://www.bilibili.com/video/BV1xx411c7mD",
        values,
        midspin_count=48,
        is_x_perfect=True,
        log=logs.append,
    )

    # 确认弹窗在没有 payload 时的算法：draft 判定 + 默认版本 + 谱面中旋。
    adjusted, reduced = prepare_submission_judgements(
        values, 48, DEFAULT_ADOFIA_VERSION
    )
    expected = format_judgement_summary(
        adjusted, reduced, DEFAULT_ADOFIA_VERSION, True
    )
    assert logs == [expected]


def test_tuf_submit_without_log_callback_still_submits():
    request_context = FakeRequestContext()
    client = TufClient(request_context=request_context)
    client.refresh_session()

    result = client.submit_pass(
        12345,
        "https://www.bilibili.com/video/BV1xx411c7mD",
        {"perfect": 10},
    )
    assert result.ok is True


class ErrorResponseContext(FakeRequestContext):
    def post(self, url, data=None, headers=None, **_kwargs):
        self.calls.append(("POST", url, data, headers))
        return FakeApiResponse(
            {"error": "Missing or invalid required field: passer", "field": "passer"},
            status=400,
        )


def test_tuf_submit_reports_api_error_field():
    client = TufClient(request_context=ErrorResponseContext())
    client.refresh_session()

    result = client.submit_pass(
        1,
        "https://www.bilibili.com/video/BV1xx411c7mD",
        {"perfect": 100},
    )

    assert result.ok is False
    assert result.status == 400
    assert "passer" in result.message


class SequencedSessionContext(FakeRequestContext):
    """按顺序返回 /v2/auth/session 的状态码，并统计 GET / POST 次数。"""

    def __init__(self, session_statuses, post_status=200):
        super().__init__()
        self.session_statuses = list(session_statuses)
        self.post_status = post_status
        self.get_count = 0
        self.post_count = 0

    def get(self, url, **_kwargs):
        self.calls.append(("GET", url))
        index = min(self.get_count, len(self.session_statuses) - 1)
        status = self.session_statuses[index]
        self.get_count += 1
        if status == 200:
            return FakeApiResponse(
                {
                    "user": {
                        "id": "1",
                        "username": "demo",
                        "nickname": "Demo",
                        "playerId": 10539,
                        "player": {"id": 10539, "name": "Demo"},
                    },
                    "csrfToken": "csrf",
                },
                status=200,
            )
        return FakeApiResponse({"error": "service unavailable"}, status=status)

    def post(self, url, data=None, headers=None, **_kwargs):
        self.calls.append(("POST", url, data, headers))
        self.post_count += 1
        if self.post_status == 200:
            return FakeApiResponse({"success": True, "pass": {"id": "record-1"}})
        return FakeApiResponse({"error": "service unavailable"}, status=self.post_status)


def test_refresh_session_retries_503_then_succeeds():
    context = SequencedSessionContext([503, 200])
    client = TufClient(request_context=context)
    sleeps: list[float] = []

    session = client.refresh_session(
        retries=2, retry_delay=0.4, sleep=sleeps.append
    )

    assert session["user"]["username"] == "demo"
    assert client.csrf_token == "csrf"
    assert context.get_count == 2
    assert sleeps == [0.4]


def test_refresh_session_reports_retryable_after_consecutive_503():
    context = SequencedSessionContext([503, 503, 503])
    client = TufClient(request_context=context)

    session = client.refresh_session(
        retries=2, retry_delay=0.1, sleep=lambda _delay: None
    )

    assert session["user"] is None
    assert session["status"] == 503
    assert session["retryable"] is True
    assert "503" in session["error"]
    assert context.get_count == 3


def test_refresh_session_does_not_retry_auth_failure():
    for status in (401, 403):
        context = SequencedSessionContext([status, 200])
        client = TufClient(request_context=context)
        sleeps: list[float] = []

        session = client.refresh_session(
            retries=2, retry_delay=0.1, sleep=sleeps.append
        )

        assert session["user"] is None
        assert session["status"] == status
        assert session["retryable"] is False
        assert context.get_count == 1
        assert sleeps == []


def test_refresh_session_failure_keeps_existing_identity():
    context = SequencedSessionContext([503, 503, 503])
    client = TufClient(request_context=context)
    client.csrf_token = "keep-csrf"
    client.player_name = "KeepPlayer"
    client.player_id = 42

    client.refresh_session(retries=1, retry_delay=0, sleep=lambda _delay: None)

    assert client.csrf_token == "keep-csrf"
    assert client.player_name == "KeepPlayer"
    assert client.player_id == 42


def test_submit_pass_reports_service_unavailable_without_posting(monkeypatch):
    monkeypatch.setattr("adofai_publisher.tuf_submitter.time.sleep", lambda _s: None)
    context = SequencedSessionContext([503, 503, 503])
    client = TufClient(request_context=context)

    result = client.submit_pass(
        1,
        "https://www.bilibili.com/video/BV1xx411c7mD",
        {"perfect": 100},
    )

    assert result.ok is False
    assert result.status == 503
    assert "服务暂不可用" in result.message
    assert "登录" not in result.message
    assert context.post_count == 0


def test_submit_pass_reports_login_invalid_when_session_401(monkeypatch):
    monkeypatch.setattr("adofai_publisher.tuf_submitter.time.sleep", lambda _s: None)
    context = SequencedSessionContext([401])
    client = TufClient(request_context=context)

    result = client.submit_pass(
        1,
        "https://www.bilibili.com/video/BV1xx411c7mD",
        {"perfect": 100},
    )

    assert result.ok is False
    assert result.status == 401
    assert "登录态失效" in result.message
    assert context.post_count == 0


class FakeTextResponse:
    """返回 HTML 的 200 响应，模拟登录页/网关页。"""

    def __init__(self, text: str, status: int = 200) -> None:
        self.status = status
        self.headers = {"content-type": "text/html"}
        self._text = text

    def body(self) -> bytes:
        return self._text.encode("utf-8")


class RoutedContext(FakeRequestContext):
    """按 URL 片段返回固定响应，并记录每次 GET 的完整地址。"""

    def __init__(self, routes=None) -> None:
        super().__init__()
        self.routes = routes or {}
        self.get_urls = []

    def get(self, url, **_kwargs):
        self.calls.append(("GET", url))
        self.get_urls.append(url)
        for marker, (payload, status) in self.routes.items():
            if marker in url:
                if isinstance(payload, str):
                    return FakeTextResponse(payload, status)
                return FakeApiResponse(payload, status=status)
        return FakeApiResponse({"error": "not found"}, status=404)


def test_list_submissions_sends_documented_params():
    context = RoutedContext(
        {
            "/v2/form/submissions": (
                {
                    "results": [
                        {"id": 71967, "levelId": 7544, "status": "pending"}
                    ],
                    "total": 1,
                    "page": 2,
                    "limit": 50,
                },
                200,
            )
        }
    )
    client = TufClient(request_context=context)

    result = client.list_submissions(
        kind="pass", status="all", query=" carnation ", page=2, limit=50, sort="DATE_DESC"
    )

    assert result.ok is True
    assert result.total == 1
    assert result.items[0]["id"] == "71967"
    url = context.get_urls[0]
    assert "/v2/form/submissions?" in url
    for part in ("type=pass", "status=all", "page=2", "limit=50", "query=carnation", "sort=DATE_DESC"):
        assert part in url, part


def test_list_submissions_parses_top_level_and_nested_shapes():
    top = TufClient(
        request_context=RoutedContext(
            {"/v2/form/submissions": ([{"id": "1", "levelId": 5, "status": "approved"}], 200)}
        )
    )
    result = top.list_submissions()
    assert result.ok is True
    assert result.items[0]["id"] == "1"

    nested = TufClient(
        request_context=RoutedContext(
            {
                "/v2/form/submissions": (
                    {
                        "data": {
                            "results": [{"recordId": "2", "level": {"id": 6}, "state": "declined"}],
                            "total": 9,
                        }
                    },
                    200,
                )
            }
        )
    )
    nested_result = nested.list_submissions()
    assert nested_result.ok is True
    assert nested_result.total == 9
    assert nested_result.items[0]["pass_id"] == "2"
    assert nested_result.items[0]["level_id"] == "6"
    assert nested_result.items[0]["status"] == "declined"


def test_list_submissions_reports_auth_error_on_401():
    client = TufClient(
        request_context=RoutedContext(
            {"/v2/form/submissions": ({"error": "unauthorized"}, 401)}
        )
    )

    result = client.list_submissions()

    assert result.ok is False
    assert result.auth_error is True
    assert result.retryable is False
    assert "登录" in result.message
    assert result.items == []


def test_list_submissions_marks_503_retryable():
    client = TufClient(
        request_context=RoutedContext(
            {"/v2/form/submissions": ({"error": "unavailable"}, 503)}
        )
    )

    result = client.list_submissions()

    assert result.ok is False
    assert result.retryable is True
    assert result.auth_error is False
    assert "服务暂不可用" in result.message


def test_list_submissions_reports_html_log_in_page():
    client = TufClient(
        request_context=RoutedContext(
            {"/v2/form/submissions": ("<!DOCTYPE html><html>login</html>", 200)}
        )
    )

    result = client.list_submissions()

    assert result.ok is False
    assert result.auth_error is False
    assert "登录页" in result.message


def test_get_submission_reads_own_record():
    context = RoutedContext(
        {
            "/v2/form/pass/71967": (
                {
                    "id": 71967,
                    "level": {"id": 7544},
                    "status": "approved",
                    "judgements": {"perfect": 10},
                },
                200,
            )
        }
    )
    client = TufClient(request_context=context)

    result = client.get_submission("71967")

    assert result.ok is True
    assert result.item is not None
    assert result.item["status"] == "approved"
    assert result.item["judgements"] == {"perfect": 10}
    assert context.get_urls[0].endswith("/v2/form/pass/71967")


def test_get_submission_falls_back_to_pass_id_after_404():
    context = RoutedContext(
        {
            "/v2/form/pass/57530": ({"error": "not found"}, 404),
            "/v2/form/pass/71967": (
                {"id": 71967, "levelId": 5756, "status": "approved"},
                200,
            ),
        }
    )
    client = TufClient(request_context=context)

    result = client.get_submission("57530", pass_id="71967")

    assert result.ok is True
    assert result.id_kind == "pass"
    assert result.item is not None
    assert result.item["status"] == "approved"
    assert context.get_urls == [
        "https://api.tuforums.com/v2/form/pass/57530",
        "https://api.tuforums.com/v2/form/pass/71967",
    ]


def test_get_submission_reports_submission_id_on_first_success():
    context = RoutedContext(
        {
            "/v2/form/pass/57530": (
                {"id": 57530, "passId": 71967, "levelId": 5756, "status": "pending"},
                200,
            )
        }
    )
    client = TufClient(request_context=context)

    result = client.get_submission("57530", pass_id="71967")

    assert result.ok is True
    assert result.id_kind == "submission"
    assert result.item is not None
    assert result.item["pass_id"] == "71967"
    assert context.get_urls == ["https://api.tuforums.com/v2/form/pass/57530"]


def test_get_submission_reports_auth_and_invalid_id():
    auth = TufClient(
        request_context=RoutedContext(
            {"/v2/form/pass/1": ({"error": "unauthorized"}, 401)}
        )
    )
    result = auth.get_submission(1)
    assert result.ok is False
    assert result.auth_error is True

    invalid = TufClient(request_context=RoutedContext())
    bad = invalid.get_submission("not-a-number")
    assert bad.ok is False
    assert bad.status == 0
    assert "编号" in bad.message


def test_fetch_level_passes_is_public_and_parses_list():
    context = RoutedContext(
        {
            "/v2/database/passes/level/7544": (
                {
                    "results": [
                        {"id": 71967, "levelId": 7544, "playerId": 10539, "status": "approved"}
                    ],
                    "total": 1,
                },
                200,
            )
        }
    )
    client = TufClient(request_context=context)

    result = client.fetch_level_passes(7544)

    assert result.ok is True
    assert result.items[0]["player_id"] == 10539
    assert context.get_urls[0].endswith("/v2/database/passes/level/7544")


def test_ensure_session_skips_session_check_when_token_present():
    context = RoutedContext({"/v2/auth/session": ({"user": {"username": "demo"}}, 200)})
    client = TufClient(request_context=context)
    client.csrf_token = "token"

    assert client.ensure_session() is True
    assert context.get_urls == []

    fresh = TufClient(request_context=context)
    assert fresh.ensure_session() is True
    assert fresh.csrf_token == "csrf-from-browser"
    assert context.get_urls == ["https://api.tuforums.com/v2/auth/session"]


def test_submit_post_is_not_retried_on_503():
    context = SequencedSessionContext([200], post_status=503)
    client = TufClient(request_context=context)
    client.refresh_session()

    result = client.submit_pass(
        1,
        "https://www.bilibili.com/video/BV1xx411c7mD",
        {"perfect": 100},
    )

    assert result.ok is False
    assert result.status == 503
    assert "服务暂不可用" in result.message
    assert context.post_count == 1
