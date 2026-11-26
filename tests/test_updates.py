from __future__ import annotations

import json
import sys

import pytest
import requests

from adofai_publisher.updates import (
    UpdateError,
    UpdateNotConfigured,
    check_for_update,
    compare_versions,
    normalize_repo,
)


@pytest.mark.parametrize(
    ("latest", "current", "expected"),
    [
        ("v0.14.0", "0.13.0", 1),
        ("0.13.0", "v0.13.0", 0),
        ("0.9.0", "0.13.0", -1),
        ("v0.13.10", "0.13.9", 1),
        ("0.14.0-beta", "0.14.0", -1),
        ("0.14.0-beta.2", "0.14.0-beta.1", 1),
        ("0.14.0", "0.14.0-beta", 1),
    ],
)
def test_compare_versions(latest, current, expected):
    assert compare_versions(latest, current) == expected


def test_compare_versions_rejects_invalid_input():
    with pytest.raises(ValueError):
        compare_versions("not-a-version", "0.13.0")


def test_normalize_repo_accepts_github_url():
    assert normalize_repo("https://github.com/DeanColck/ADOFAI-GO/") == (
        "DeanColck/ADOFAI-GO"
    )
    assert normalize_repo("DeanColck/ADOFAI-GO.git") == "DeanColck/ADOFAI-GO"


def test_unconfigured_repo_does_not_request(monkeypatch):
    called = []
    monkeypatch.setattr("requests.get", lambda *a, **k: called.append(True))

    with pytest.raises(UpdateNotConfigured):
        check_for_update("", "0.13.0")
    with pytest.raises(UpdateNotConfigured):
        check_for_update("   ", "0.13.0")
    assert called == []


class _Response:
    def __init__(self, status_code=200, payload=None, invalid_json=False):
        self.status_code = status_code
        self._payload = payload
        self._invalid_json = invalid_json

    def json(self):
        if self._invalid_json:
            raise ValueError("bad json")
        return self._payload


def _release_payload(**overrides):
    payload = {
        "tag_name": "v0.14.0",
        "body": "## 更新\n- 软件更新检查",
        "html_url": "https://github.com/DeanColck/ADOFAI-GO/releases/tag/v0.14.0",
        "assets": [
            {
                "name": "ADOFAI-GO-v0.14.0.exe",
                "size": 123,
                "browser_download_url": "https://example.com/app.exe",
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_check_for_update_has_new_version(monkeypatch):
    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers or {}
        seen["timeout"] = timeout
        return _Response(payload=_release_payload())

    monkeypatch.setattr("requests.get", fake_get)

    result = check_for_update("DeanColck/ADOFAI-GO", "0.13.0", timeout=5)

    assert seen["url"] == (
        "https://api.github.com/repos/DeanColck/ADOFAI-GO/releases/latest"
    )
    assert seen["headers"]["Accept"] == "application/vnd.github+json"
    assert seen["timeout"] == 5
    assert result["has_update"] is True
    assert result["latest"] == "v0.14.0"
    assert result["html_url"].endswith("/releases/tag/v0.14.0")
    assert result["assets"][0]["name"] == "ADOFAI-GO-v0.14.0.exe"


def test_check_for_update_no_new_version(monkeypatch):
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: _Response(payload=_release_payload(tag_name="v0.13.0")),
    )

    result = check_for_update("DeanColck/ADOFAI-GO", "0.13.0")

    assert result["has_update"] is False


def test_check_for_update_rate_limited(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: _Response(status_code=403))

    with pytest.raises(UpdateError, match="403"):
        check_for_update("DeanColck/ADOFAI-GO", "0.13.0")


def test_check_for_update_repo_missing(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: _Response(status_code=404))

    with pytest.raises(UpdateError, match="404"):
        check_for_update("DeanColck/missing", "0.13.0")


def test_check_for_update_network_error(monkeypatch):
    def fail(*args, **kwargs):
        raise requests.ConnectionError("connection reset")

    monkeypatch.setattr("requests.get", fail)

    with pytest.raises(UpdateError, match="网络不通"):
        check_for_update("DeanColck/ADOFAI-GO", "0.13.0")


def test_check_for_update_timeout(monkeypatch):
    def fail(*args, **kwargs):
        raise requests.Timeout("timed out")

    monkeypatch.setattr("requests.get", fail)

    with pytest.raises(UpdateError, match="超时"):
        check_for_update("DeanColck/ADOFAI-GO", "0.13.0")


def test_check_for_update_invalid_json(monkeypatch):
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: _Response(invalid_json=True),
    )

    with pytest.raises(UpdateError, match="解析失败"):
        check_for_update("DeanColck/ADOFAI-GO", "0.13.0")


def test_check_for_update_missing_tag(monkeypatch):
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: _Response(payload={"body": "no tag"}),
    )

    with pytest.raises(UpdateError, match="tag_name"):
        check_for_update("DeanColck/ADOFAI-GO", "0.13.0")


def test_local_release_file_bypasses_network_and_marks_local_assets(
    tmp_path, monkeypatch
):
    exe = tmp_path / "ADOFAI_GO_v9.9.9.exe"
    exe.write_bytes(b"payload")
    checksum = tmp_path / "ADOFAI_GO_v9.9.9.exe.sha256"
    checksum.write_text("a" * 64, encoding="utf-8")
    release = tmp_path / "release.json"
    release.write_text(
        json.dumps(
            {
                "tag_name": "v9.9.9",
                "body": "离线验收",
                "html_url": "https://github.com/DeanClock/ADOFAI-GO/releases",
                "assets": [
                    {
                        "name": exe.name,
                        "size": exe.stat().st_size,
                        "browser_download_url": exe.name,
                    },
                    {
                        "name": checksum.name,
                        "size": checksum.stat().st_size,
                        "browser_download_url": checksum.name,
                    },
                    {
                        "name": "remote.exe",
                        "size": 1,
                        "browser_download_url": "https://example.com/remote.exe",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ADOFAI_UPDATE_RELEASE_FILE", str(release))
    called: list[bool] = []
    monkeypatch.setattr("requests.get", lambda *a, **k: called.append(True))

    result = check_for_update("DeanClock/ADOFAI-GO", "0.17.20")

    assert called == []
    assert result["has_update"] is True
    assets = {asset["name"]: asset for asset in result["assets"]}
    assert assets[exe.name]["local"] is True
    assert assets[exe.name]["url"] == str(exe.resolve())
    assert assets[checksum.name]["local"] is True
    assert assets["remote.exe"]["local"] is False
    assert assets["remote.exe"]["url"] == "https://example.com/remote.exe"


def test_local_release_file_missing_reports_error(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "ADOFAI_UPDATE_RELEASE_FILE", str(tmp_path / "missing.json")
    )

    with pytest.raises(UpdateError, match="读取失败"):
        check_for_update("DeanClock/ADOFAI-GO", "0.17.20")


def test_local_release_file_invalid_json_reports_error(tmp_path, monkeypatch):
    release = tmp_path / "release.json"
    release.write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv("ADOFAI_UPDATE_RELEASE_FILE", str(release))

    with pytest.raises(UpdateError, match="JSON"):
        check_for_update("DeanClock/ADOFAI-GO", "0.17.20")


def test_local_release_file_ignored_in_frozen_build(tmp_path, monkeypatch):
    from adofai_publisher.updates import _load_release_override

    release = tmp_path / "release.json"
    release.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("ADOFAI_UPDATE_RELEASE_FILE", str(release))
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    assert _load_release_override() is None
