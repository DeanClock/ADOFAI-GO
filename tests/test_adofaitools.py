from __future__ import annotations

import json

import pytest

from adofai_publisher import adofaitools
from adofai_publisher.adofaitools import (
    AdofaiToolsError,
    clear_token,
    load_token,
    normalize_list,
    normalize_mod,
    save_token,
)


def test_token_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    assert load_token() == ""
    save_token("abc123")
    assert load_token() == "abc123"
    clear_token()
    assert load_token() == ""


def test_settings_token_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    save_token("file-token")

    class _Settings:
        def get(self, key, default=None):
            return "settings-token" if key == "adofaitools_token" else default

    assert load_token(_Settings()) == "settings-token"


def test_expired_token_is_discarded(tmp_path, monkeypatch):
    import base64
    import time as _time

    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))

    def _jwt(exp):
        header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
        body = base64.urlsafe_b64encode(
            json.dumps({"exp": exp}).encode()
        ).rstrip(b"=").decode()
        return f"{header}.{body}.sig"

    expired = _jwt(int(_time.time()) - 3600)
    valid = _jwt(int(_time.time()) + 3600)
    assert adofaitools.token_expired(expired) is True
    assert adofaitools.token_expired(valid) is False
    assert adofaitools.token_expired("not-a-jwt") is False

    save_token(expired)
    assert load_token() == ""
    assert not adofaitools.token_path().exists()


def test_normalize_mod_extracts_latest_version():
    raw = {
        "id": "1",
        "slug": "mod-levellibrary",
        "displayName": "LevelLibrary",
        "resourceType": "MOD",
        "downloadCount": 42,
        "latestVersion": {"id": "pk-1", "versionId": "3.4-130"},
        "versions": [
            {
                "id": "pk-1",
                "versionId": "3.4-130",
                "files": [
                    {
                        "id": "f1",
                        "name": "LevelLibrary_v3.4-130.zip",
                        "status": "READY",
                        "downloadUrl": "/api/mod-files/f1/download",
                    }
                ],
            }
        ],
    }
    result = normalize_mod(raw)
    assert result["slug"] == "mod-levellibrary"
    assert result["display_name"] == "LevelLibrary"
    assert result["version"] == "3.4-130"
    assert result["download_url"] == "/api/mod-files/f1/download"
    assert result["file_name"] == "LevelLibrary_v3.4-130.zip"
    assert result["downloads"] == 42


def test_normalize_list_supports_common_shapes():
    assert normalize_list([{"slug": "a"}])["total"] == 1
    assert normalize_list({"data": {"items": [{"slug": "b"}], "total": 7}})["total"] == 7
    assert normalize_list({"items": [{"slug": "c"}]})["items"][0]["slug"] == "c"
    assert normalize_list({"data": [{"slug": "d"}]})["total"] == 1


def test_list_mods_sends_bearer_token(monkeypatch):
    seen = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"items": [{"slug": "mod-x", "displayName": "X"}], "total": 1}

    def fake_get(url, headers=None, params=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers or {}
        seen["params"] = params or {}
        return _Resp()

    monkeypatch.setattr("requests.get", fake_get)
    payload = adofaitools.list_mods("tok", page=2, page_size=5, keyword="lib")
    assert seen["url"].endswith("/api/mods")
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert seen["params"]["page"] == 2
    assert seen["params"]["pageSize"] == 5
    assert seen["params"]["keyword"] == "lib"
    assert payload["items"][0]["slug"] == "mod-x"


def test_list_mods_raises_on_401(monkeypatch):
    class _Resp:
        status_code = 401

        @staticmethod
        def json():
            return {}

    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp())
    with pytest.raises(AdofaiToolsError) as excinfo:
        adofaitools.list_mods("bad")
    assert "登录已失效" in str(excinfo.value)


def test_download_mod_file_writes_and_reports_progress(tmp_path, monkeypatch):
    class _Resp:
        status_code = 200
        headers = {"content-length": "8", "content-disposition": 'attachment; filename="LevelLibrary.zip"'}
        url = "https://www.adofaitools.top/api/mods/MOD/x/versions/1/download"

        @staticmethod
        def iter_content(chunk_size):
            yield b"1234"
            yield b"5678"

    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp())
    progress = []
    target = adofaitools.download_mod_file(
        "tok", "MOD", "mod-x", "/api/mod-files/f1/download", tmp_path,
        progress=lambda r, t: progress.append((r, t)),
    )
    assert target.name == "mod-x.zip"
    assert target.read_bytes() == b"12345678"
    assert progress[-1] == (8, 8)


def test_download_mod_file_builds_absolute_url(tmp_path, monkeypatch):
    seen = {}

    class _Resp:
        status_code = 200
        headers = {"content-length": "2"}
        url = "https://www.adofaitools.top/api/mod-files/f1/download"

        @staticmethod
        def iter_content(chunk_size):
            yield b"ok"

    def fake_get(url, **kwargs):
        seen["url"] = url
        return _Resp()

    monkeypatch.setattr("requests.get", fake_get)
    target = adofaitools.download_mod_file(
        "tok", "MOD", "mod-x", "/api/mod-files/f1/download", tmp_path,
        file_name="Thing.zip",
    )
    assert seen["url"] == "https://www.adofaitools.top/api/mod-files/f1/download"
    assert target.name == "Thing.zip"


def test_extract_access_token_shapes():
    from adofai_publisher.workers import _extract_access_token

    assert _extract_access_token({"accessToken": "a1"}) == "a1"
    assert _extract_access_token({"data": {"accessToken": "a2"}}) == "a2"
    assert _extract_access_token({"data": {"access_token": "a3"}}) == "a3"
    assert _extract_access_token({"token": "a4"}) == "a4"
    assert _extract_access_token({"data": {}}) == ""
    assert _extract_access_token(None) == ""


def test_list_items_have_no_download_url_but_detail_does():
    listing = normalize_mod(
        {"slug": "mod-x", "displayName": "X", "resourceType": "MOD", "latestVersion": {"versionId": "1.0"}}
    )
    assert listing["download_url"] == ""
    detail = normalize_mod(
        {
            "slug": "mod-x",
            "displayName": "X",
            "latestVersion": {"id": "p1", "versionId": "1.0"},
            "versions": [
                {
                    "id": "p1",
                    "versionId": "1.0",
                    "files": [
                        {"status": "READY", "name": "X.zip", "downloadUrl": "/api/mod-files/f/download"}
                    ],
                }
            ],
        }
    )
    assert detail["download_url"] == "/api/mod-files/f/download"
    assert detail["file_name"] == "X.zip"


def test_total_from_content_range():
    from adofai_publisher.adofaitools import _total_from_content_range

    assert _total_from_content_range("bytes 0-1023/620392") == 620392
    assert _total_from_content_range("bytes 100-/47605586") == 47605586
    assert _total_from_content_range("garbage") == 0
    assert _total_from_content_range("") == 0


def test_download_resumes_after_interruption(tmp_path, monkeypatch):
    payload = b"0123456789" * 10
    calls = {"n": 0}

    class _Resp:
        def __init__(self, status, body, headers):
            self.status_code = status
            self._body = body
            self.headers = headers
            self.url = "https://www.adofaitools.top/api/mod-files/f/download"

        def iter_content(self, chunk_size):
            for start in range(0, len(self._body), chunk_size):
                yield self._body[start:start + chunk_size]

        def close(self):
            pass

    def fake_get(url, headers=None, **kwargs):
        calls["n"] += 1
        start = 0
        rng = (headers or {}).get("Range", "")
        if rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0])
        if calls["n"] == 1:
            # 第一次只给一半就断流
            return _Resp(200, payload[:50], {"content-length": str(len(payload))})
        return _Resp(
            206,
            payload[start:],
            {
                "content-range": f"bytes {start}-{len(payload)-1}/{len(payload)}",
                "content-length": str(len(payload) - start),
            },
        )

    monkeypatch.setattr("requests.get", fake_get)
    target = adofaitools.download_mod_file(
        "tok", "MOD", "mod-x", "/api/mod-files/f/download", tmp_path,
        file_name="Big.zip",
    )
    assert target.read_bytes() == payload
    assert calls["n"] >= 2
