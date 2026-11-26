from adofai_publisher.levellookup import LevelLookup
from adofai_publisher.models import LevelInfo


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.payload)


class _DownloadResponse:
    status_code = 200

    def iter_content(self, chunk_size=65536):
        yield b'{"chart": true}'


class _DownloadSession:
    def get(self, url, **kwargs):
        return _DownloadResponse()


class _ZipDownloadResponse:
    status_code = 200
    url = "https://cdn.tuforums.com/zips/demo/Song.zip"

    def iter_content(self, chunk_size=65536):
        yield b"PK\x03\x04demo"


class _ZipDownloadSession:
    def get(self, url, **kwargs):
        return _ZipDownloadResponse()


def test_search_uses_query_parameter():
    lookup = LevelLookup()
    session = _Session({"results": [{"id": 1, "song": "Knots"}]})
    lookup.session = session

    results = lookup.search("knots")

    assert results[0].song == "Knots"
    params = session.calls[0][1]["params"]
    assert params["query"] == "knots"
    assert "search" not in params


def test_empty_query_returns_empty_list():
    lookup = LevelLookup()
    session = _Session({"results": []})
    lookup.session = session
    assert lookup.search("   ") == []
    assert session.calls == []


def test_download_level_organizes_by_difficulty(tmp_path):
    lookup = LevelLookup()
    lookup.session = _DownloadSession()
    level = LevelInfo(
        tuf_id=17643,
        song="SUGARPOT",
        charter="3.14",
        difficulty="P10",
        dl_link="https://example.test/chart",
    )

    target = lookup.download_level(17643, tmp_path, level=level)

    assert target == tmp_path / "P" / "P10" / "SUGARPOT" / "SUGARPOT - 3.14 (17643).ard"
    assert target.is_file()


def test_download_level_can_disable_organization(tmp_path):
    lookup = LevelLookup()
    lookup.session = _DownloadSession()
    level = LevelInfo(
        tuf_id=17643,
        song="SUGARPOT",
        charter="3.14",
        difficulty="P10",
        dl_link="https://example.test/chart",
    )

    target = lookup.download_level(17643, tmp_path, level=level, organize=False)

    assert target.parent == tmp_path


def test_download_level_keeps_zip_suffix(tmp_path):
    lookup = LevelLookup()
    lookup.session = _ZipDownloadSession()
    level = LevelInfo(
        tuf_id=17643,
        song="SUGARPOT",
        charter="3.14",
        difficulty="P10",
        dl_link="https://example.test/chart",
    )

    target = lookup.download_level(17643, tmp_path, level=level)

    assert target == tmp_path / "P" / "P10" / "SUGARPOT" / "SUGARPOT - 3.14 (17643).zip"
    assert target.is_file()


class _QuerySession:
    """按 query 返回不同结果的替身，用来验证候选词回退顺序。"""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def get(self, url, **kwargs):
        query = kwargs["params"]["query"]
        self.calls.append((url, kwargs))
        return _Response({"results": self.mapping.get(query, [])})


def _hit(level_id: int, song: str) -> dict:
    return {"id": level_id, "song": song}


def test_search_uses_server_limit_of_100():
    lookup = LevelLookup()
    session = _Session({"results": []})
    lookup.session = session

    lookup.search("beneath")

    assert session.calls[0][1]["params"]["limit"] == 100


def test_search_candidates_order_is_text_then_no_paren_then_no_punct():
    from adofai_publisher.levellookup import search_candidates

    assert search_candidates("Song (feat. X)") == [
        "Song (feat. X)",
        "Song",
        "Song feat X",
    ]
    assert search_candidates("  ") == []
    assert search_candidates("Beneath") == ["Beneath"]


def test_search_with_fallback_retries_without_parentheses():
    lookup = LevelLookup()
    session = _QuerySession(
        {
            "Song (feat. X)": [],
            "Song": [_hit(1, "Song"), _hit(2, "Song 2"), _hit(3, "Song 3")],
        }
    )
    lookup.session = session

    results = lookup.search_with_fallback("Song (feat. X)")

    assert [item.tuf_id for item in results] == [1, 2, 3]
    assert [call[1]["params"]["query"] for call in session.calls] == [
        "Song (feat. X)",
        "Song",
    ]
    assert session.calls[0][1]["params"]["limit"] == 100


def test_search_with_fallback_keeps_single_unique_hit():
    lookup = LevelLookup()
    session = _QuerySession({"Only One": [_hit(9, "Only One")]})
    lookup.session = session

    results = lookup.search_with_fallback("Only One")

    assert [item.tuf_id for item in results] == [9]
    assert len(session.calls) == 1


def test_is_advanced_query_recognizes_field_syntax():
    from adofai_publisher.levellookup import is_advanced_query

    for query in (
        "song:carnation",
        "artist = camellia",
        "bpm:>150, tilecount:>=1000",
        "song=goTe | song:xnor",
        "song:hello \\!artist:camellia",
        "#1234",
        "id=1234",
        "time:2m",
        "dllink:https://example.com/a.ard",
    ):
        assert is_advanced_query(query), query


def test_is_advanced_query_ignores_plain_titles():
    from adofai_publisher.levellookup import is_advanced_query

    for query in ("Re:End", "Song, Part 2", "Beneath the Vortex", "MySong:Home", "17776"):
        assert not is_advanced_query(query), query


def test_advanced_query_is_sent_once_without_fallback():
    lookup = LevelLookup()
    session = _QuerySession({"song:carnation": [_hit(1, "Carnation")]})
    lookup.session = session

    results = lookup.search_with_fallback("song:carnation")

    assert [item.tuf_id for item in results] == [1]
    assert [call[1]["params"]["query"] for call in session.calls] == ["song:carnation"]


def test_advanced_query_with_no_results_does_not_retry():
    lookup = LevelLookup()
    session = _QuerySession({})
    lookup.session = session

    assert lookup.search_with_fallback("bpm:>150, tilecount:>=1000") == []
    assert len(session.calls) == 1


def test_hash_and_id_queries_use_list_endpoint_not_get():
    for query in ("#1234", "id=1234"):
        lookup = LevelLookup()
        session = _QuerySession({query: [_hit(1234, "Carnation")]})
        lookup.session = session

        results = lookup.search_with_fallback(query)

        assert [item.tuf_id for item in results] == [1234]
        assert session.calls[0][0].endswith("/v2/database/levels")
        assert session.calls[0][1]["params"]["query"] == query


def test_level_lookup_worker_requests_fallback_with_limit_100(monkeypatch):
    from adofai_publisher import workers

    calls = []

    class FakeLookup:
        def search_with_fallback(self, query, limit=8):
            calls.append((query, limit))
            return ["level"]

    monkeypatch.setattr(workers, "LevelLookup", FakeLookup)
    worker = workers.LevelLookupWorker("beneath")
    results: list = []
    worker.finished.connect(results.append)

    worker.run()

    assert results == [["level"]]
    assert calls == [("beneath", 100)]


class _ChartResponse:
    def __init__(self, content=b"", status_code=200, headers=None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}


class _ChartSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _chart_bytes():
    import json

    return json.dumps(
        {
            "settings": {"bpm": 180, "song": "Online"},
            "angleData": [0, 90, 180],
            "actions": [{"floor": 1, "eventType": "SetSpeed", "speed": 2}],
        }
    ).encode("utf-8")


def test_fetch_chart_json_reads_bare_and_wrapped_payloads():
    import json

    chart = json.loads(_chart_bytes())
    for payload in (chart, {"level": chart}, {"data": chart}):
        lookup = LevelLookup()
        lookup.session = _ChartSession(
            _ChartResponse(json.dumps(payload).encode("utf-8"))
        )

        data, error = lookup.fetch_chart_json(17776)

        assert error == ""
        assert data is not None
        assert data["settings"]["song"] == "Online"
        assert data["angleData"] == [0, 90, 180]
        assert lookup.session.calls[0][0].endswith(
            "/v2/database/levels/17776/level.adofai"
        )


def test_fetch_chart_json_reports_404():
    lookup = LevelLookup()
    lookup.session = _ChartSession(_ChartResponse(b"", status_code=404))

    data, error = lookup.fetch_chart_json(424242)

    assert data is None
    assert "没有找到" in error and "424242" in error


def test_fetch_chart_json_rejects_html():
    lookup = LevelLookup()
    lookup.session = _ChartSession(
        _ChartResponse(b"<!DOCTYPE html><html><body>login</body></html>")
    )

    data, error = lookup.fetch_chart_json(17776)

    assert data is None
    assert "HTML" in error


def test_fetch_chart_json_rejects_oversized_payload():
    from adofai_publisher.levellookup import MAX_CHART_JSON_BYTES

    lookup = LevelLookup()
    lookup.session = _ChartSession(
        _ChartResponse(
            b"{}",
            headers={"Content-Length": str(MAX_CHART_JSON_BYTES + 1)},
        )
    )

    data, error = lookup.fetch_chart_json(17776)

    assert data is None
    assert "过大" in error

    lookup.session = _ChartSession(
        _ChartResponse(b"{" + b" " * (MAX_CHART_JSON_BYTES + 1) + b"}")
    )
    data, error = lookup.fetch_chart_json(17776)

    assert data is None
    assert "过大" in error


class _BatchLookup:
    """批量绑定 worker 的替身：按 query 返回候选，并记下查询顺序与 limit。"""

    def __init__(self, mapping=None, error_queries=()):
        self.mapping = dict(mapping or {})
        self.error_queries = set(error_queries)
        self.calls = []

    def search(self, query, limit=100):
        self.calls.append((query, limit))
        if query in self.error_queries:
            raise RuntimeError("网络请求失败")
        return list(self.mapping.get(query, []))


def _batch_target(**overrides):
    target = {
        "key": "folder:a",
        "name": "Time Leaper",
        "bpm": 350.0,
        "tilecount": 2604,
        "midspin_count": 25,
        "auto_tile_count": 0,
        "playable_hits": 2604,
        "numeric_ready": True,
        "numeric_missing": [],
    }
    target.update(overrides)
    return target


def _tuf_level(level_id=10808, song="Time Leaper", tilecount=2604):
    return LevelInfo.from_tuf(
        {
            "id": level_id,
            "song": song,
            "bpm": 350,
            "tilecount": tilecount,
            "midspinCount": 25,
            "autoTileCount": 0,
        }
    )


def _run_batch_worker(monkeypatch, lookup, targets):
    from adofai_publisher import workers

    monkeypatch.setattr(workers, "LevelLookup", lambda: lookup)
    worker = workers.BatchLevelLookupWorker(targets)
    payloads = []
    finished = []
    worker.progress.connect(lambda key, payload: payloads.append((key, payload)))
    worker.finished.connect(finished.append)
    worker.run()
    return payloads, finished


def test_batch_worker_queries_numbers_first_with_limit_100(monkeypatch):
    lookup = _BatchLookup(
        {
            "bpm=350, tilecount=2604": [_tuf_level()],
            "bpm=350": [_tuf_level(level_id=2, tilecount=2000)],
        }
    )

    payloads, finished = _run_batch_worker(
        monkeypatch, lookup, [_batch_target()]
    )

    assert lookup.calls == [("bpm=350, tilecount=2604", 100)]
    assert finished == [1]
    assert len(payloads) == 1
    key, payload = payloads[0]
    assert key == "folder:a"
    assert payload[0]["tuf_id"] == 10808
    # payload 要带四元组和 raw，对话框靠 raw 判断字段是否存在。
    assert payload[0]["tilecount"] == 2604
    assert payload[0]["midspin_count"] == 25
    assert payload[0]["auto_tile_count"] == 0
    assert payload[0]["raw"]["midspinCount"] == 25
    assert payload[0]["raw"]["autoTileCount"] == 0


def test_batch_worker_falls_back_to_bpm_then_name(monkeypatch):
    lookup = _BatchLookup({"Time Leaper": [_tuf_level()]})

    payloads, finished = _run_batch_worker(
        monkeypatch, lookup, [_batch_target()]
    )

    assert lookup.calls == [
        ("bpm=350, tilecount=2604", 100),
        ("bpm=350", 100),
        ("Time Leaper", 5),
    ]
    assert finished == [1]
    assert payloads[0][1][0]["tuf_id"] == 10808


def test_batch_worker_skips_numeric_queries_when_not_ready(monkeypatch):
    lookup = _BatchLookup({"Time Leaper": [_tuf_level()]})

    payloads, _finished = _run_batch_worker(
        monkeypatch,
        lookup,
        [_batch_target(numeric_ready=False, numeric_missing=["可击物量"])],
    )

    assert lookup.calls == [("Time Leaper", 5)]


def test_batch_worker_treats_query_errors_as_empty(monkeypatch):
    lookup = _BatchLookup(
        {"bpm=350": [_tuf_level()]},
        error_queries=("bpm=350, tilecount=2604",),
    )

    payloads, finished = _run_batch_worker(
        monkeypatch, lookup, [_batch_target()]
    )

    assert lookup.calls == [
        ("bpm=350, tilecount=2604", 100),
        ("bpm=350", 100),
    ]
    assert finished == [1]
    assert payloads[0][1][0]["tuf_id"] == 10808


def test_batch_worker_cancel_before_run_skips_all_queries(monkeypatch):
    from adofai_publisher import workers

    lookup = _BatchLookup({"bpm=350": [_tuf_level()]})
    monkeypatch.setattr(workers, "LevelLookup", lambda: lookup)
    worker = workers.BatchLevelLookupWorker([_batch_target()])
    payloads = []
    finished = []
    worker.progress.connect(lambda key, payload: payloads.append(key))
    worker.finished.connect(finished.append)

    worker.cancel()
    worker.run()

    assert lookup.calls == []
    assert payloads == []
    assert finished == [0]
