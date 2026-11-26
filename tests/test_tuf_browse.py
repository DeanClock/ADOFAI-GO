import json

from adofai_publisher.tuf_browse import (
    Q_SPECIAL_DIFFICULTIES,
    Q_SPECIAL_SHORT_NAMES,
    TufBrowser,
    pgu_name,
    pgu_range_text,
    q_bucket_label,
    q_difficulty_names,
    q_range_names,
)
from adofai_publisher.tuf_browse import TufPage


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Session:
    def __init__(self, payload=None, fail_url=None):
        self.payload = payload or {"results": [], "total": 0}
        self.fail_url = fail_url
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.fail_url and self.fail_url in url:
            return _Response({}, status_code=500)
        return _Response(self.payload)


def test_pgu_name_boundaries():
    assert pgu_name(1) == "P1"
    assert pgu_name(20) == "P20"
    assert pgu_name(21) == "G1"
    assert pgu_name(40) == "G20"
    assert pgu_name(60) == "U20"
    assert pgu_range_text(1, 60) == "P1,U20"


def test_fetch_page_builds_filters_and_facets():
    session = _Session(
        {
            "results": [{"id": 7, "song": "Song", "difficulty": {"name": "G12"}}],
            "total": 80,
            "offset": 50,
            "limit": 50,
            "hasMore": True,
        }
    )
    browser = TufBrowser()
    browser.session = session

    page = browser.fetch_page(
        query="  song  ",
        offset=50,
        limit=999,
        sort="DIFF",
        descending=False,
        pgu_range=(21, 40),
        specials=["Unranked", "Bogus"],
        tag_ids=[3, 11],
        curation_ids=[19],
    )

    params = session.calls[0][1]["params"]
    assert params["query"] == "song"
    assert params["limit"] == 100
    assert params["offset"] == 50
    assert params["sort"] == "DIFF_ASC"
    assert params["pguRange"] == "G1,G20"
    assert params["specialDifficulties"] == ",".join(
        ["Unranked", *Q_SPECIAL_DIFFICULTIES]
    )
    facet = json.loads(params["facetQuery"])
    assert facet["tags"]["ids"] == [3, 11]
    assert facet["curationTypes"]["ids"] == [19]
    assert page.total == 80
    assert page.has_more is True
    assert page.items[0].song == "Song"


def test_q_special_names_match_site_order():
    assert len(Q_SPECIAL_DIFFICULTIES) == 11
    assert len(Q_SPECIAL_SHORT_NAMES) == 11
    # 接口桶名带范围后缀，短标签只给 UI 用。
    assert Q_SPECIAL_DIFFICULTIES[0] == "Qq"
    assert Q_SPECIAL_SHORT_NAMES == (
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
    assert Q_SPECIAL_DIFFICULTIES[1:6] == (
        "GQ0 (G1~G4)",
        "GQ1 (G5~G8)",
        "GQ2 (G9~G12)",
        "GQ3 (G13~G16)",
        "GQ4 (G17~G20)",
    )
    assert Q_SPECIAL_DIFFICULTIES[6:] == (
        "UQ0 (U1~U4)",
        "UQ1 (U5~U8)",
        "UQ2 (U9~U12)",
        "UQ3 (U13~U16)",
        "UQ4 (U17~U20)",
    )


def test_q_range_names_boundaries():
    assert q_range_names(None) == list(Q_SPECIAL_DIFFICULTIES)
    assert q_range_names((1, 1)) == ["Qq"]
    assert q_range_names((2, 5)) == [
        "GQ0 (G1~G4)",
        "GQ1 (G5~G8)",
        "GQ2 (G9~G12)",
        "GQ3 (G13~G16)",
    ]
    assert q_range_names((2, 4)) == [
        "GQ0 (G1~G4)",
        "GQ1 (G5~G8)",
        "GQ2 (G9~G12)",
    ]
    assert q_range_names((11, 11)) == ["UQ4 (U17~U20)"]
    assert q_range_names((0, 99)) == list(Q_SPECIAL_DIFFICULTIES)
    # 高值小于低值时按区间处理。
    assert q_range_names((5, 2)) == [
        "GQ0 (G1~G4)",
        "GQ1 (G5~G8)",
        "GQ2 (G9~G12)",
        "GQ3 (G13~G16)",
    ]
    # 显式空元组表示关闭 Q 桶。
    assert q_range_names(()) == []


def _browse_params(**kwargs):
    session = _Session()
    browser = TufBrowser()
    browser.session = session
    browser.fetch_page(**kwargs)
    return session.calls[0][1]["params"]


def test_fetch_page_sends_all_q_buckets_by_default():
    params = _browse_params()
    assert params["specialDifficulties"] == ",".join(Q_SPECIAL_DIFFICULTIES)


def test_fetch_page_narrow_q_range_sends_only_selected_buckets():
    params = _browse_params(q_range=(1, 1))
    assert params["specialDifficulties"] == "Qq"

    params = _browse_params(q_range=(2, 5))
    assert params["specialDifficulties"] == ",".join(
        [
            "GQ0 (G1~G4)",
            "GQ1 (G5~G8)",
            "GQ2 (G9~G12)",
            "GQ3 (G13~G16)",
        ]
    )


def test_fetch_page_merges_specials_and_q_buckets_without_duplicates():
    params = _browse_params(
        specials=["Unranked", "UQ1 (U5~U8)", "Bogus"],
        q_range=(7, 11),
    )
    assert params["specialDifficulties"] == ",".join(
        [
            "Unranked",
            "UQ1 (U5~U8)",
            "UQ0 (U1~U4)",
            "UQ2 (U9~U12)",
            "UQ3 (U13~U16)",
            "UQ4 (U17~U20)",
        ]
    )
    assert params["pguRange"] == "P1,U20"


def test_fetch_page_empty_q_range_sends_no_q_buckets():
    params = _browse_params(q_range=())
    assert "specialDifficulties" not in params


def test_q_bucket_label_uses_full_width_parentheses():
    assert q_bucket_label("Qq") == "Qq"
    assert q_bucket_label("GQ0 (G1~G4)") == "GQ0（G1~G4）"
    assert q_bucket_label("UQ4 (U17~U20)") == "UQ4（U17~U20）"


def test_q_difficulty_names_keeps_order_and_drops_unknown():
    assert q_difficulty_names(None) is None
    assert q_difficulty_names([]) == []
    assert q_difficulty_names(["UQ4 (U17~U20)", "Qq", "Qq"]) == [
        "UQ4 (U17~U20)",
        "Qq",
    ]
    # 未知桶名与空串被丢掉，避免把服务器忽略的值发出去。
    assert q_difficulty_names(["Bogus", " ", "GQ0 (G1~G4)"]) == ["GQ0 (G1~G4)"]
    assert q_difficulty_names(["Bogus"]) == []


def test_fetch_page_q_difficulties_subset_sends_exact_buckets():
    params = _browse_params(
        q_difficulties=["Qq", "UQ4 (U17~U20)", "Bogus"]
    )
    assert params["specialDifficulties"] == "Qq,UQ4 (U17~U20)"


def test_fetch_page_q_difficulties_empty_sends_no_q_buckets():
    params = _browse_params(q_difficulties=[])
    assert "specialDifficulties" not in params


def test_fetch_page_q_difficulties_merges_with_specials():
    params = _browse_params(
        specials=["Unranked", "Qq"],
        q_difficulties=["Qq", "UQ0 (U1~U4)"],
    )
    assert params["specialDifficulties"] == "Unranked,Qq,UQ0 (U1~U4)"


def test_fetch_page_q_difficulties_wins_over_legacy_q_range():
    params = _browse_params(q_range=(1, 1), q_difficulties=["UQ2 (U9~U12)"])
    assert params["specialDifficulties"] == "UQ2 (U9~U12)"


def test_random_sort_adds_seed():
    session = _Session()
    browser = TufBrowser()
    browser.session = session

    browser.fetch_page(sort="RANDOM", limit=1)

    params = session.calls[0][1]["params"]
    assert params["sort"] == "RANDOM_DESC"
    assert isinstance(params["seed"], int)
    assert "facetQuery" not in params


def test_fetch_page_derives_has_more_without_server_flag():
    session = _Session({"results": [{"id": 1}, {"id": 2}], "total": 5, "offset": 0})
    browser = TufBrowser()
    browser.session = session

    page = browser.fetch_page(limit=2)

    assert page.has_more is True


def test_fetch_meta_keeps_partial_results_on_failure(tmp_path):
    class _MetaSession:
        def get(self, url, **kwargs):
            if url.endswith("/v2/database/difficulties/hash"):
                return _Response({}, status_code=500)
            if url.endswith("/v2/database/difficulties"):
                return _Response([{"id": 1, "name": "P1"}])
            if url.endswith("/tags"):
                return _Response([{"id": 3, "name": "Full VFX"}])
            return _Response({}, status_code=500)

    browser = TufBrowser()
    browser.session = _MetaSession()
    meta = browser.fetch_meta(cache_path=tmp_path / "cache" / "tuf_meta.json")

    assert len(meta["difficulties"]) == 1
    assert len(meta["tags"]) == 1
    assert meta["curations"] == []
    assert meta["errors"]


def test_browse_worker_does_not_forward_request_id(monkeypatch):
    from adofai_publisher import workers

    captured = {}

    class _Browser:
        def fetch_page(self, **kwargs):
            captured.update(kwargs)
            return TufPage(
                items=[],
                total=0,
                offset=kwargs.get("offset", 0),
                limit=kwargs.get("limit", 50),
                has_more=False,
            )

    monkeypatch.setattr(workers, "TufBrowser", _Browser)
    worker = workers.TufBrowseWorker({"request_id": 9, "query": "x", "offset": 0})
    results = []
    worker.finished.connect(results.append)

    worker.run()

    assert "request_id" not in captured
    assert results[0]["request_id"] == 9


META_LISTS = {
    "difficulties": [{"id": 1, "name": "P1"}],
    "tags": [{"id": 2, "name": "Full VFX"}],
    "curations": [{"id": 3, "name": "Collab"}],
}


def _write_meta_cache(path, *, hash_value="h1", version=1, fetched_at=None, meta=None):
    from datetime import datetime, timezone

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": version,
        "fetched_at": fetched_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hash": hash_value,
        "meta": meta if meta is not None else META_LISTS,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


class _MetaSession:
    def __init__(self, hash_value="h1", hash_status=200, lists=None, fail_paths=()):
        self.hash_value = hash_value
        self.hash_status = hash_status
        self.lists = lists if lists is not None else META_LISTS
        self.fail_paths = set(fail_paths)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url.endswith("/v2/database/difficulties/hash"):
            if self.hash_status != 200:
                return _Response({}, status_code=self.hash_status)
            return _Response({"hash": self.hash_value})
        for key, path in (
            ("difficulties", "/v2/database/difficulties"),
            ("tags", "/v2/database/difficulties/tags"),
            ("curations", "/v2/admin/curations/types"),
        ):
            if url.endswith(path):
                if path in self.fail_paths:
                    return _Response({}, status_code=500)
                return _Response(self.lists[key])
        return _Response({}, status_code=500)

    @property
    def list_calls(self):
        return [url for url in self.calls if not url.endswith("/hash")]


def test_fetch_meta_uses_cache_when_hash_matches(tmp_path):
    cache = tmp_path / "cache" / "tuf_meta.json"
    _write_meta_cache(cache, hash_value="h1")
    session = _MetaSession(hash_value="h1")
    browser = TufBrowser()
    browser.session = session

    meta = browser.fetch_meta(cache_path=cache)

    assert meta["cached"] is True
    assert meta["stale"] is False
    assert meta["difficulties"][0]["name"] == "P1"
    assert meta["fetched_at"]
    assert session.list_calls == []
    assert len(session.calls) == 1


def test_fetch_meta_refetches_and_rewrites_when_hash_changes(tmp_path):
    cache = tmp_path / "cache" / "tuf_meta.json"
    _write_meta_cache(cache, hash_value="old")
    session = _MetaSession(
        hash_value="new",
        lists={
            "difficulties": [{"id": 9, "name": "U1"}],
            "tags": [],
            "curations": [],
        },
    )
    browser = TufBrowser()
    browser.session = session

    meta = browser.fetch_meta(cache_path=cache)

    assert meta["cached"] is False
    assert meta["stale"] is False
    assert meta["difficulties"] == [{"id": 9, "name": "U1"}]
    assert len(session.list_calls) == 3
    stored = json.loads(cache.read_text(encoding="utf-8"))
    assert stored["hash"] == "new"
    assert stored["meta"]["difficulties"] == [{"id": 9, "name": "U1"}]


def test_fetch_meta_refetches_when_cache_corrupt_or_wrong_version(tmp_path):
    for name, writer in (
        ("broken.json", lambda path: path.write_text("{not json", encoding="utf-8")),
        ("version.json", lambda path: _write_meta_cache(path, version=2)),
    ):
        cache = tmp_path / "cache" / name
        cache.parent.mkdir(parents=True, exist_ok=True)
        writer(cache)
        session = _MetaSession(hash_value="h1")
        browser = TufBrowser()
        browser.session = session

        meta = browser.fetch_meta(cache_path=cache)

        assert meta["cached"] is False
        assert meta["difficulties"] == META_LISTS["difficulties"]
        assert len(session.list_calls) == 3
        stored = json.loads(cache.read_text(encoding="utf-8"))
        assert stored["version"] == 1


def test_fetch_meta_falls_back_to_fresh_cache_when_hash_fails(tmp_path):
    cache = tmp_path / "cache" / "tuf_meta.json"
    _write_meta_cache(cache, hash_value="h1")
    session = _MetaSession(hash_status=500)
    browser = TufBrowser()
    browser.session = session

    meta = browser.fetch_meta(cache_path=cache)

    assert meta["cached"] is True
    assert meta["stale"] is True
    assert meta["tags"] == META_LISTS["tags"]
    assert session.list_calls == []


def test_fetch_meta_fills_partial_failures_from_cache(tmp_path):
    cache = tmp_path / "cache" / "tuf_meta.json"
    old_meta = {
        "difficulties": [{"id": 1, "name": "OldP"}],
        "tags": [{"id": 2, "name": "Old Tag"}],
        "curations": [{"id": 3, "name": "Old Collab"}],
    }
    _write_meta_cache(cache, hash_value="old", meta=old_meta)
    session = _MetaSession(
        hash_value="new",
        lists={
            "difficulties": [{"id": 1, "name": "NewP"}],
            "tags": [{"id": 2, "name": "New Tag"}],
            "curations": [{"id": 3, "name": "New Collab"}],
        },
        fail_paths=("/v2/database/difficulties/tags",),
    )
    browser = TufBrowser()
    browser.session = session

    meta = browser.fetch_meta(cache_path=cache)

    assert meta["cached"] is False
    assert meta["stale"] is True
    assert meta["tags"] == old_meta["tags"]
    assert meta["difficulties"] == [{"id": 1, "name": "NewP"}]
    assert any("补齐" in item for item in meta["errors"])
    # 三个端点没有全部成功：不覆盖旧缓存。
    stored = json.loads(cache.read_text(encoding="utf-8"))
    assert stored["hash"] == "old"
    assert stored["meta"]["difficulties"] == old_meta["difficulties"]


def test_fetch_meta_ignores_cache_write_failures(tmp_path):
    cache = tmp_path / "cache" / "tuf_meta.json"
    cache.mkdir(parents=True)
    session = _MetaSession(hash_value="h1")
    browser = TufBrowser()
    browser.session = session

    meta = browser.fetch_meta(cache_path=cache)

    assert meta["cached"] is False
    assert meta["stale"] is False
    assert meta["difficulties"] == META_LISTS["difficulties"]
    assert cache.is_dir()


def test_fetch_meta_without_hash_uses_fresh_cache(tmp_path):
    cache = tmp_path / "cache" / "tuf_meta.json"
    _write_meta_cache(cache)
    session = _MetaSession()
    browser = TufBrowser()
    browser.session = session

    meta = browser.fetch_meta(cache_path=cache, use_hash=False)

    assert meta["cached"] is True
    assert session.calls == []
