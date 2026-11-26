"""Thunderstore 备用源与两种更新包布局的归一化测试（全部离线：只替换 _http_get）。"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from adofai_publisher.umm import (
    THUNDERSTORE_UMM_PACKAGE_URL,
    UmmCancelled,
    UmmError,
    fetch_umm_thunderstore,
    install_local_update,
    installer_dir,
    umm_installer_ready,
)

DOWNLOAD_URL = "https://thunderstore.io/package/download/ADOFAI_Modding/UMM_ADOFAI/1.1.0/"

ADOFAI_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<Config Name="A Dance of Fire and Ice">
  <Folder>A Dance of Fire and Ice</Folder>
  <ModsDirectory>Mods</ModsDirectory>
  <EntryPoint>[UnityEngine.CoreModule.dll]UnityEngine.Application.cctor:Before</EntryPoint>
  <StartingPoint>[Assembly-CSharp.dll]ADOStartup.Startup:Before</StartingPoint>
  <UIStartingPoint>[Assembly-CSharp.dll]ADOStartup.Startup:After</UIStartingPoint>
</Config>
"""


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.headers = {"content-length": str(len(payload))}

    def raise_for_status(self) -> None:
        return None

    def json(self):
        import json as _json

        return _json.loads(self._payload.decode("utf-8"))

    def iter_content(self, chunk_size: int):
        for start in range(0, len(self._payload), chunk_size):
            yield self._payload[start : start + chunk_size]


def _metadata_bytes(version: str = "1.1.0", url: str = DOWNLOAD_URL) -> bytes:
    import json

    return json.dumps(
        {"latest": {"version_number": version, "download_url": url}}
    ).encode("utf-8")


def _thunderstore_zip_bytes(
    *,
    prefix: str = "",
    include_doorstop: bool = True,
    include_manifest: bool = True,
) -> bytes:
    """构造 Thunderstore ADOFAI UMM 包（UMM/Core/* + 根目录 winhttp.dll）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(f"{prefix}.doorstop_version", "4.4.1")
        zf.writestr(f"{prefix}doorstop_config.ini", "[General]\nenabled = true\n")
        if include_doorstop:
            zf.writestr(f"{prefix}winhttp.dll", b"winhttp-x64")
        zf.writestr(f"{prefix}UMM/Core/0Harmony.dll", b"harmony")
        zf.writestr(f"{prefix}UMM/Core/dnlib.dll", b"dnlib")
        zf.writestr(f"{prefix}UMM/Core/UnityModManager.dll", b"umm-dll")
        zf.writestr(f"{prefix}UMM/Core/UnityModManager.xml", b"umm-xml")
        zf.writestr(f"{prefix}UMM/Core/Config.xml", ADOFAI_CONFIG)
        if include_manifest:
            zf.writestr(f"{prefix}manifest.json", "{}")
            zf.writestr(f"{prefix}README.md", "readme")
            zf.writestr(f"{prefix}run_umm.sh", "#!/bin/sh\n")
            zf.writestr(f"{prefix}doorstop_libs/libdoorstop_x64.so", b"so")
        # 目录穿越条目：归一化时必须被丢弃，不能写到安装器目录之外。
        zf.writestr("../evil.txt", b"evil")
    return buffer.getvalue()


def _fake_get(archive: bytes, *, metadata: bytes | None = None):
    def fake_get(url, timeout=20.0, stream=False):
        if url == THUNDERSTORE_UMM_PACKAGE_URL:
            return _FakeResponse(metadata if metadata is not None else _metadata_bytes())
        if url == DOWNLOAD_URL:
            return _FakeResponse(archive)
        raise AssertionError(f"意外的下载地址：{url}")

    return fake_get


def _assert_normalized_installer(target: Path, tmp_path: Path) -> None:
    # 安装器根目录结构（与 umm_installer_source() 认的目录一致）。
    assert (target / "UnityModManager.dll").read_bytes() == b"umm-dll"
    assert (target / "UnityModManager.xml").read_bytes() == b"umm-xml"
    assert (target / "0Harmony.dll").read_bytes() == b"harmony"
    assert (target / "dnlib.dll").read_bytes() == b"dnlib"
    assert ADOFAI_CONFIG in (target / "Config.xml").read_text(encoding="utf-8")
    # winhttp.dll 改名成 doorstop 代理要找的 winhttp_x64.dll。
    assert (target / "winhttp_x64.dll").read_bytes() == b"winhttp-x64"
    assert not (target / "winhttp.dll").exists()
    # UMM/Core 前缀拍平，Linux 用的文件和包元数据全部丢弃。
    assert not (target / "UMM").exists()
    assert not (target / "manifest.json").exists()
    assert not (target / "run_umm.sh").exists()
    assert not (target / "doorstop_libs").exists()
    assert not (tmp_path / "evil.txt").exists()
    assert umm_installer_ready(target) is True


def test_fetch_umm_thunderstore_normalizes_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    monkeypatch.setattr(
        "adofai_publisher.umm._http_get",
        _fake_get(_thunderstore_zip_bytes()),
    )

    progress: list[tuple[str, int]] = []
    version, target = fetch_umm_thunderstore(
        progress=lambda message, percent: progress.append((message, percent))
    )

    assert version == "1.1.0 (Thunderstore)"
    assert target == installer_dir()
    _assert_normalized_installer(target, tmp_path)
    messages = [message for message, _percent in progress]
    assert any("从 Thunderstore 下载" in message for message in messages)
    # 版本号来自接口，不能写死。
    assert any("1.1.0" in message for message in messages)


def test_fetch_umm_thunderstore_version_comes_from_api(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    monkeypatch.setattr(
        "adofai_publisher.umm._http_get",
        _fake_get(
            _thunderstore_zip_bytes(),
            metadata=_metadata_bytes(version="9.9.9"),
        ),
    )

    version, _target = fetch_umm_thunderstore()

    assert version == "9.9.9 (Thunderstore)"


def test_fetch_umm_thunderstore_handles_nested_package_root(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    monkeypatch.setattr(
        "adofai_publisher.umm._http_get",
        _fake_get(_thunderstore_zip_bytes(prefix="UMM_ADOFAI/")),
    )

    _version, target = fetch_umm_thunderstore()

    _assert_normalized_installer(target, tmp_path)


def test_fetch_umm_thunderstore_rejects_package_without_doorstop(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    monkeypatch.setattr(
        "adofai_publisher.umm._http_get",
        _fake_get(_thunderstore_zip_bytes(include_doorstop=False)),
    )

    with pytest.raises(UmmError, match="winhttp.dll"):
        fetch_umm_thunderstore()


def test_fetch_umm_thunderstore_metadata_failure_is_readable(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))

    def failing_get(url, timeout=20.0, stream=False):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("adofai_publisher.umm._http_get", failing_get)

    with pytest.raises(UmmError, match="Thunderstore"):
        fetch_umm_thunderstore()


def test_fetch_umm_thunderstore_without_download_url_is_readable(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    monkeypatch.setattr(
        "adofai_publisher.umm._http_get",
        _fake_get(b"", metadata=b'{"latest": {"version_number": "1.1.0"}}'),
    )

    with pytest.raises(UmmError, match="没有 UMM 下载地址"):
        fetch_umm_thunderstore()


def test_fetch_umm_thunderstore_cancel_stops_download(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    monkeypatch.setattr(
        "adofai_publisher.umm._http_get",
        _fake_get(_thunderstore_zip_bytes()),
    )
    calls = {"count": 0}

    def cancel() -> bool:
        calls["count"] += 1
        # 前两次（进入函数、读完版本信息）放行，下载分块时取消。
        return calls["count"] > 2

    with pytest.raises(UmmCancelled):
        fetch_umm_thunderstore(cancel=cancel)

    # 取消后不留下半个安装器目录。
    assert not umm_installer_ready(installer_dir())


def test_install_local_update_accepts_thunderstore_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    target = installer_dir()
    target.mkdir(parents=True)
    (target / "umm_update.zip").write_bytes(_thunderstore_zip_bytes())

    assert install_local_update() == target
    _assert_normalized_installer(target, tmp_path)
    # 本地包本身保留，方便用户重试。
    assert (target / "umm_update.zip").is_file()


def test_install_local_update_accepts_official_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    target = installer_dir()
    target.mkdir(parents=True)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("UnityModManager.exe", b"exe")
        zf.writestr("UnityModManager.dll", b"dll")
        zf.writestr("winhttp_x64.dll", b"proxy")
    (target / "umm_update.zip").write_bytes(buffer.getvalue())

    assert install_local_update() == target
    assert (target / "UnityModManager.exe").read_bytes() == b"exe"
    assert (target / "winhttp_x64.dll").read_bytes() == b"proxy"
    assert umm_installer_ready(target) is True


def test_install_local_update_missing_archive_is_readable(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))

    with pytest.raises(UmmError, match="umm_update.zip"):
        install_local_update()
