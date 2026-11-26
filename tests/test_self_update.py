"""应用内更新的离线单测：假网络、假文件，绝不替换本机 exe。"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import requests

from adofai_publisher import self_update
from adofai_publisher.self_update import (
    SelfUpdateCancelled,
    SelfUpdateError,
    build_install_script,
    download_file,
    download_release_update,
    fetch_checksum,
    find_checksum_asset,
    install_target,
    launch_install_helper,
    parse_checksum,
    pick_exe_asset,
    safe_download_name,
    sha256_file,
)
from adofai_publisher.workers import UpdateDownloadWorker


def _asset(path: Path, name: str | None = None) -> dict:
    data = path.read_bytes()
    return {
        "name": name or path.name,
        "size": len(data),
        "url": str(path),
        "local": True,
    }


class _FakeResponse:
    def __init__(self, chunks=None, *, status=200, headers=None, text=""):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = list(chunks or [])
        self.text = text
        self.closed = False

    def iter_content(self, chunk_size=1024):
        yield from self._chunks

    def close(self):
        self.closed = True


# -- 资产与摘要 -------------------------------------------------------------


def test_pick_exe_asset_ignores_checksum_and_prefers_adofai():
    assets = [
        {"name": "README.txt", "url": "https://example.com/readme"},
        {"name": "other.exe", "url": "https://example.com/other.exe"},
        {"name": "ADOFAI_GO_v1.exe", "url": "https://example.com/app.exe"},
        {"name": "ADOFAI_GO_v1.exe.sha256", "url": "https://example.com/app.exe.sha256"},
    ]
    assert pick_exe_asset(assets)["name"] == "ADOFAI_GO_v1.exe"
    assert pick_exe_asset(assets[2:])["name"] == "ADOFAI_GO_v1.exe"
    assert pick_exe_asset([]) is None
    assert pick_exe_asset(None) is None


def test_find_checksum_asset_matches_common_suffixes():
    exe = {"name": "ADOFAI_GO_v1.exe", "url": "https://example.com/app.exe"}
    for name in (
        "ADOFAI_GO_v1.exe.sha256",
        "ADOFAI_GO_V1.EXE.SHA256",
        "ADOFAI_GO_v1.exe.sha256.txt",
        "ADOFAI_GO_v1.exe.sha256sum",
    ):
        assets = [{"name": name, "url": "https://example.com/sum"}]
        assert find_checksum_asset(assets, exe) is not None
    assert find_checksum_asset([], exe) is None
    assert find_checksum_asset(
        [{"name": "other.exe.sha256", "url": "x"}], exe
    ) is None


def test_parse_checksum_accepts_common_formats():
    digest = "a" * 64
    assert parse_checksum(digest) == digest
    assert parse_checksum(f"{digest}  ADOFAI_GO.exe") == digest
    assert parse_checksum(digest.upper()) == digest
    assert parse_checksum("SHA256 (file) = " + digest) == digest
    assert parse_checksum("not a checksum") is None
    assert parse_checksum("") is None


def test_sha256_file_matches_hashlib(tmp_path):
    path = tmp_path / "payload.bin"
    data = b"adofai-go" * 1000
    path.write_bytes(data)
    assert sha256_file(path) == hashlib.sha256(data).hexdigest()


def test_safe_download_name_strips_directories():
    assert safe_download_name({"name": "../../evil.exe"}) == "evil.exe"
    assert safe_download_name({"name": "setup.txt"}) == "ADOFAI_GO_update.exe"
    assert safe_download_name({}) == "ADOFAI_GO_update.exe"


# -- 下载 -------------------------------------------------------------------


def test_download_file_local_reports_progress_and_replaces_target(tmp_path):
    source = tmp_path / "source.exe"
    data = b"x" * (300 * 1024)
    source.write_bytes(data)
    dest = tmp_path / "downloads" / "new.exe"
    seen: list[tuple[int, int]] = []

    received = download_file(
        _asset(source),
        dest,
        progress=lambda done, total: seen.append((done, total)),
    )

    assert received == len(data)
    assert dest.read_bytes() == data
    assert seen and seen[-1] == (len(data), len(data))
    assert not (tmp_path / "downloads" / "new.exe.part").exists()


def test_download_file_local_missing_source(tmp_path):
    with pytest.raises(SelfUpdateError, match="不存在"):
        download_file(
            {"name": "new.exe", "url": str(tmp_path / "missing.exe"), "local": True},
            tmp_path / "new.exe",
        )


def test_download_file_local_cancel_cleans_partial_file(tmp_path):
    source = tmp_path / "source.exe"
    source.write_bytes(b"x" * 1024)
    dest = tmp_path / "new.exe"
    event = __import__("threading").Event()
    event.set()

    with pytest.raises(SelfUpdateCancelled):
        download_file(
            _asset(source),
            dest,
            cancel_event=event,
        )

    assert not dest.exists()
    assert not Path(str(dest) + ".part").exists()


def test_download_file_http_success(monkeypatch, tmp_path):
    data = b"http-payload" * 1000

    def fake_get(url, **kwargs):
        assert kwargs["stream"] is True
        return _FakeResponse(
            [data[:5000], data[5000:]],
            headers={"Content-Length": str(len(data))},
        )

    monkeypatch.setattr("adofai_publisher.self_update.requests.get", fake_get)
    dest = tmp_path / "http.exe"
    received = download_file(
        {"name": "http.exe", "url": "https://example.com/a.exe"},
        dest,
    )

    assert received == len(data)
    assert dest.read_bytes() == data


def test_download_file_http_403_is_readable(monkeypatch, tmp_path):
    def fake_get(url, **kwargs):
        return _FakeResponse(status=403)

    monkeypatch.setattr("adofai_publisher.self_update.requests.get", fake_get)

    with pytest.raises(SelfUpdateError, match="403"):
        download_file(
            {"name": "a.exe", "url": "https://example.com/a.exe"},
            tmp_path / "a.exe",
        )


def test_download_file_http_network_error_is_readable(monkeypatch, tmp_path):
    def fake_get(url, **kwargs):
        raise requests.ConnectionError("connection reset")

    monkeypatch.setattr("adofai_publisher.self_update.requests.get", fake_get)

    with pytest.raises(SelfUpdateError, match="网络不通"):
        download_file(
            {"name": "a.exe", "url": "https://example.com/a.exe"},
            tmp_path / "a.exe",
        )


def test_download_file_rejects_non_http_remote_url(tmp_path):
    with pytest.raises(SelfUpdateError, match="地址无效"):
        download_file(
            {"name": "a.exe", "url": "ftp://example.com/a.exe"},
            tmp_path / "a.exe",
        )


def test_download_file_detects_incomplete_download(monkeypatch, tmp_path):
    def fake_get(url, **kwargs):
        return _FakeResponse(
            [b"short"],
            headers={"Content-Length": "100"},
        )

    monkeypatch.setattr("adofai_publisher.self_update.requests.get", fake_get)

    with pytest.raises(SelfUpdateError, match="不完整"):
        download_file(
            {"name": "a.exe", "url": "https://example.com/a.exe"},
            tmp_path / "a.exe",
        )
    assert not (tmp_path / "a.exe").exists()


# -- 校验 -------------------------------------------------------------------


def test_fetch_checksum_local_and_bad_format(tmp_path):
    good = tmp_path / "a.exe.sha256"
    digest = "b" * 64
    good.write_text(f"{digest}  a.exe\n", encoding="utf-8")
    assert fetch_checksum(_asset(good)) == digest

    bad = tmp_path / "bad.sha256"
    bad.write_text("what is this", encoding="utf-8")
    with pytest.raises(SelfUpdateError, match="格式"):
        fetch_checksum(_asset(bad))


def test_download_release_update_verifies_checksum(tmp_path):
    exe = tmp_path / "ADOFAI_GO_v9.exe"
    exe.write_bytes(b"new build" * 500)
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    checksum = tmp_path / "ADOFAI_GO_v9.exe.sha256"
    checksum.write_text(f"{digest}  {exe.name}\n", encoding="utf-8")
    dest_dir = tmp_path / "downloads"
    stages: list[str] = []

    result = download_release_update(
        _asset(exe), _asset(checksum), dest_dir=dest_dir, on_stage=stages.append
    )

    assert result["checksum_verified"] is True
    assert result["sha256"] == digest
    assert stages == ["download", "checksum"]
    assert Path(result["path"]).read_bytes() == exe.read_bytes()


def test_download_release_update_checksum_mismatch_removes_file(tmp_path):
    exe = tmp_path / "ADOFAI_GO_v9.exe"
    exe.write_bytes(b"new build")
    checksum = tmp_path / "ADOFAI_GO_v9.exe.sha256"
    checksum.write_text(f"{'c' * 64}  {exe.name}\n", encoding="utf-8")
    dest_dir = tmp_path / "downloads"

    with pytest.raises(SelfUpdateError, match="校验失败"):
        download_release_update(_asset(exe), _asset(checksum), dest_dir=dest_dir)

    assert list(dest_dir.glob("*")) == []


def test_download_release_update_without_checksum_warns(tmp_path):
    exe = tmp_path / "ADOFAI_GO_v9.exe"
    exe.write_bytes(b"new build")

    result = download_release_update(_asset(exe), None, dest_dir=tmp_path / "d")

    assert result["checksum_verified"] is False
    assert Path(result["path"]).is_file()


def test_download_release_update_removes_file_when_checksum_unreadable(tmp_path):
    exe = tmp_path / "ADOFAI_GO_v9.exe"
    exe.write_bytes(b"new build")
    checksum = tmp_path / "ADOFAI_GO_v9.exe.sha256"
    checksum.write_text("not a sha256 file", encoding="utf-8")
    dest_dir = tmp_path / "downloads"

    with pytest.raises(SelfUpdateError, match="格式"):
        download_release_update(_asset(exe), _asset(checksum), dest_dir=dest_dir)

    assert list(dest_dir.glob("*")) == []


# -- 替换目标与 helper ------------------------------------------------------


def test_install_target_uses_env_only_in_source_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path / "data"))
    target = tmp_path / "target.exe"
    monkeypatch.setenv("ADOFAI_UPDATE_INSTALL_TARGET", str(target))
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert install_target() == target.resolve()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(target))
    assert install_target() == target.resolve()


def test_install_target_rejects_paths_inside_data_dir(tmp_path, monkeypatch):
    home = tmp_path / "data"
    home.mkdir()
    target = home / "target.exe"
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(home))
    monkeypatch.setenv("ADOFAI_UPDATE_INSTALL_TARGET", str(target))
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert install_target() is None


def test_install_target_missing_env_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    monkeypatch.delenv("ADOFAI_UPDATE_INSTALL_TARGET", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert install_target() is None


def test_build_install_script_has_backup_rollback_and_hash():
    script = build_install_script()
    assert "param(" in script
    assert "$TargetPath.old" in script
    assert "Copy-Item" in script
    assert "Get-FileHash" in script
    assert "rollback completed" in script
    assert "Get-Process -Id $ParentPid" in script


def test_launch_install_helper_passes_paths_and_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(self_update, "download_dir", lambda: tmp_path / "helper")
    target = tmp_path / "target.exe"
    target.write_bytes(b"old")
    new_file = tmp_path / "new.exe"
    new_file.write_bytes(b"new")
    captured: dict = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(
        "adofai_publisher.self_update.subprocess.Popen", fake_popen
    )

    script = launch_install_helper(
        target, new_file, "d" * 64, parent_pid=4321, wait_seconds=12
    )

    assert script.is_file()
    assert script.read_text(encoding="utf-8-sig").startswith("param(")
    args = captured["args"]
    assert "-ParentPid" in args and args[args.index("-ParentPid") + 1] == "4321"
    assert args[args.index("-TargetPath") + 1] == str(target)
    assert args[args.index("-NewPath") + 1] == str(new_file)
    assert args[args.index("-ExpectedSha256") + 1] == "d" * 64
    assert args[args.index("-WaitSeconds") + 1] == "12"
    assert captured["kwargs"]["creationflags"] == 0x08000000


def test_launch_install_helper_requires_existing_download(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(SelfUpdateError, match="不存在"):
        launch_install_helper(
            tmp_path / "target.exe", tmp_path / "missing.exe"
        )


def test_launch_install_helper_non_windows_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(SelfUpdateError, match="Windows"):
        launch_install_helper(tmp_path / "target.exe", tmp_path / "new.exe")


def _run_helper_sync(script_args, timeout=120):
    return subprocess.run(
        script_args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="helper 只在 Windows 运行")
def test_install_helper_replaces_dummy_exe_and_keeps_backup(tmp_path, monkeypatch):
    real_popen = subprocess.Popen
    monkeypatch.setattr(self_update, "download_dir", lambda: tmp_path / "helper")
    target = tmp_path / "target.exe"
    target.write_bytes(b"OLD BUILD")
    new_file = tmp_path / "new.exe"
    new_data = b"NEW BUILD" * 100
    new_file.write_bytes(new_data)
    digest = hashlib.sha256(new_data).hexdigest()
    captured: dict = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        return object()

    monkeypatch.setattr(
        "adofai_publisher.self_update.subprocess.Popen", fake_popen
    )
    launch_install_helper(
        target, new_file, digest, parent_pid=2147483647, wait_seconds=1
    )
    args = captured["args"]
    monkeypatch.setattr(
        "adofai_publisher.self_update.subprocess.Popen", real_popen
    )
    completed = _run_helper_sync(args)

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert target.read_bytes() == new_data
    assert (tmp_path / "target.exe.old").read_bytes() == b"OLD BUILD"
    assert not new_file.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="helper 只在 Windows 运行")
def test_install_helper_rolls_back_on_hash_mismatch(tmp_path, monkeypatch):
    real_popen = subprocess.Popen
    monkeypatch.setattr(self_update, "download_dir", lambda: tmp_path / "helper")
    target = tmp_path / "target.exe"
    target.write_bytes(b"OLD BUILD")
    new_file = tmp_path / "new.exe"
    new_file.write_bytes(b"NEW BUILD")
    captured: dict = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        return object()

    monkeypatch.setattr(
        "adofai_publisher.self_update.subprocess.Popen", fake_popen
    )
    launch_install_helper(
        target,
        new_file,
        "0" * 64,
        parent_pid=2147483647,
        wait_seconds=1,
    )
    args = captured["args"]
    monkeypatch.setattr(
        "adofai_publisher.self_update.subprocess.Popen", real_popen
    )
    completed = _run_helper_sync(args)

    assert completed.returncode == 1
    assert target.read_bytes() == b"OLD BUILD"
    assert (tmp_path / "target.exe.old").read_bytes() == b"OLD BUILD"


# -- Worker -----------------------------------------------------------------


def test_update_download_worker_emits_finished(tmp_path, monkeypatch):
    payload = {
        "path": str(tmp_path / "new.exe"),
        "sha256": "e" * 64,
        "checksum_verified": True,
    }
    monkeypatch.setattr(
        "adofai_publisher.workers.download_release_update",
        lambda *args, **kwargs: payload,
    )
    worker = UpdateDownloadWorker(
        {"name": "a.exe", "url": "https://example.com/a.exe"}, None
    )
    finished: list[dict] = []
    failed: list[str] = []
    worker.finished.connect(finished.append)
    worker.failed.connect(failed.append)

    worker.run()

    assert finished == [payload]
    assert failed == []


def test_update_download_worker_emits_progress_via_callback(tmp_path, monkeypatch):
    def fake_download(_exe, _checksum, *, progress=None, **kwargs):
        if progress is not None:
            progress(10, 20)
        return {"path": "x", "sha256": "e" * 64, "checksum_verified": True}

    monkeypatch.setattr(
        "adofai_publisher.workers.download_release_update", fake_download
    )
    worker = UpdateDownloadWorker({"name": "a.exe", "url": "u"}, None)
    seen: list[tuple[int, int]] = []
    worker.progress.connect(lambda done, total: seen.append((done, total)))

    worker.run()

    assert seen == [(10, 20)]


def test_update_download_worker_cancelled(monkeypatch):
    def fake_download(_exe, _checksum, *, cancel_event=None, **kwargs):
        raise SelfUpdateCancelled("下载已取消")

    monkeypatch.setattr(
        "adofai_publisher.workers.download_release_update", fake_download
    )
    worker = UpdateDownloadWorker({"name": "a.exe", "url": "u"}, None)
    messages: list[str] = []
    worker.cancelled.connect(messages.append)
    worker.cancel()

    worker.run()

    assert messages == ["下载已取消"]
    assert worker._cancel_event.is_set()


def test_update_download_worker_failed(monkeypatch):
    def fake_download(*args, **kwargs):
        raise SelfUpdateError("SHA256 校验失败")

    monkeypatch.setattr(
        "adofai_publisher.workers.download_release_update", fake_download
    )
    worker = UpdateDownloadWorker({"name": "a.exe", "url": "u"}, None)
    messages: list[str] = []
    worker.failed.connect(messages.append)

    worker.run()

    assert messages == ["SHA256 校验失败"]
