"""OBS 自动查找：注册表 / Steam 库 / 用户目录。

这些用例一律用临时目录和假来源，绝不读写本机真实注册表，也不启动 OBS。
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest

from adofai_publisher import obs_launcher
from adofai_publisher.obs_launcher import (
    OBS_INSTALL_RELATIVE,
    OBS_SEARCH_LOCATIONS,
    find_obs_executable,
)

# autouse 夹具会替换模块里的函数，这里留一份真实实现给「注册表出错」用例。
_REAL_REGISTRY_ENTRIES = obs_launcher._registry_obs_entries


@pytest.fixture(autouse=True)
def _isolate_host_lookups(monkeypatch, tmp_path):
    """清掉本机真实来源，避免用例依赖这台电脑装了什么。"""
    monkeypatch.setattr(obs_launcher, "_registry_obs_entries", lambda: [])
    monkeypatch.setattr(obs_launcher, "_steam_root", lambda: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    for name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(obs_launcher.shutil, "which", lambda _name: None)


def _make_exe(directory: Path) -> Path:
    exe = directory / "bin" / "64bit" / "obs64.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"exe")
    return exe


def _vdf_path(path: Path) -> str:
    """libraryfolders.vdf 里的路径是转义过的反斜杠。"""
    return str(path).replace("\\", "\\\\")


def _write_vdf(steam_root: Path, libraries: list[Path]) -> None:
    body = "\n".join(
        f'\t\t"{index}"\n\t\t{{\n\t\t\t"path"\t\t"{_vdf_path(lib)}"\n\t\t}}'
        for index, lib in enumerate(libraries)
    )
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    vdf.parent.mkdir(parents=True, exist_ok=True)
    vdf.write_text(
        f'"libraryfolders"\n{{\n{body}\n}}\n', encoding="utf-8"
    )


def test_registry_install_location_is_found(tmp_path, monkeypatch):
    install = tmp_path / "OBS Studio"
    exe = _make_exe(install)
    monkeypatch.setattr(
        obs_launcher,
        "_registry_obs_entries",
        lambda: [(str(install), "")],
    )

    assert find_obs_executable("") == exe


def test_registry_display_icon_index_is_stripped(tmp_path, monkeypatch):
    exe = _make_exe(tmp_path / "OBS Studio")
    monkeypatch.setattr(
        obs_launcher,
        "_registry_obs_entries",
        lambda: [("", f"{exe},0")],
    )

    assert find_obs_executable("") == exe


def test_registry_display_icon_quoted_with_index(tmp_path, monkeypatch):
    exe = _make_exe(tmp_path / "OBS Studio")
    monkeypatch.setattr(
        obs_launcher,
        "_registry_obs_entries",
        lambda: [("", f'"{exe}",0')],
    )

    assert find_obs_executable("") == exe


def test_display_icon_ignores_non_obs_icons():
    assert obs_launcher._display_icon_executable(r"C:\OBS\obs.ico") is None
    assert obs_launcher._display_icon_executable("") is None
    assert obs_launcher._display_icon_executable(
        r"C:\OBS\bin\64bit\obs64.exe,0"
    ) == Path(r"C:\OBS\bin\64bit\obs64.exe")


def test_registry_access_error_is_skipped(tmp_path, monkeypatch):
    """注册表权限不足时只跳过该来源，不抛异常。"""
    fake = types.ModuleType("winreg")
    fake.HKEY_LOCAL_MACHINE = object()
    fake.HKEY_CURRENT_USER = object()

    def deny(*_args, **_kwargs):
        raise OSError("access denied")

    fake.OpenKey = deny
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(obs_launcher.sys, "platform", "win32")
    monkeypatch.setattr(
        obs_launcher, "_registry_obs_entries", _REAL_REGISTRY_ENTRIES
    )

    assert obs_launcher._registry_obs_paths() == []

    # 整个查找也不受影响：Program Files 里的副本仍然能命中。
    root = tmp_path / "ProgramFiles"
    exe = _make_exe(root / "obs-studio")
    monkeypatch.setenv("ProgramFiles", str(root))
    assert find_obs_executable("") == exe


def test_steam_second_library_is_found(tmp_path, monkeypatch):
    steam_root = tmp_path / "Steam"
    second = tmp_path / "SteamLibrary"
    _write_vdf(steam_root, [steam_root, second])
    exe = second / "steamapps" / "common" / "OBS Studio" / "bin" / "64bit" / "obs64.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"exe")
    monkeypatch.setattr(obs_launcher, "_steam_root", lambda: steam_root)

    assert find_obs_executable("") == exe


def test_steam_root_is_checked_without_vdf(tmp_path, monkeypatch):
    steam_root = tmp_path / "Steam"
    exe = steam_root / "steamapps" / "common" / "OBS Studio" / "bin" / "64bit" / "obs64.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"exe")
    monkeypatch.setattr(obs_launcher, "_steam_root", lambda: steam_root)

    assert find_obs_executable("") == exe


def test_local_appdata_install_is_found(tmp_path):
    exe = _make_exe(tmp_path / "localappdata" / "Programs" / "obs-studio")

    assert find_obs_executable("") == exe


def test_configured_path_wins_over_registry(tmp_path, monkeypatch):
    configured = tmp_path / "Custom" / "obs64.exe"
    configured.parent.mkdir(parents=True)
    configured.write_bytes(b"exe")
    registry_install = tmp_path / "OBS Studio"
    _make_exe(registry_install)
    monkeypatch.setattr(
        obs_launcher,
        "_registry_obs_entries",
        lambda: [(str(registry_install), "")],
    )

    assert find_obs_executable(str(configured)) == configured


def test_candidate_order(tmp_path, monkeypatch):
    configured = tmp_path / "custom" / "obs64.exe"
    path_hit = tmp_path / "path" / "obs64.exe"
    registry_hit = tmp_path / "registry" / "obs64.exe"
    steam_hit = tmp_path / "steam" / "obs64.exe"
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "pf86"))
    monkeypatch.setenv("ProgramW6432", str(tmp_path / "pf64"))
    monkeypatch.setattr(
        obs_launcher.shutil, "which", lambda _name: str(path_hit)
    )
    monkeypatch.setattr(
        obs_launcher, "_registry_obs_paths", lambda: [registry_hit]
    )
    monkeypatch.setattr(
        obs_launcher, "_steam_obs_paths", lambda: [steam_hit]
    )

    assert obs_launcher._candidate_paths(str(configured)) == [
        configured,
        path_hit,
        tmp_path / "pf" / OBS_INSTALL_RELATIVE,
        tmp_path / "pf86" / OBS_INSTALL_RELATIVE,
        tmp_path / "pf64" / OBS_INSTALL_RELATIVE,
        registry_hit,
        steam_hit,
        tmp_path
        / "localappdata"
        / "Programs"
        / OBS_INSTALL_RELATIVE,
    ]


def test_candidates_are_deduplicated(tmp_path, monkeypatch):
    install = tmp_path / "OBS Studio"
    exe = _make_exe(install)
    # 注册表和 Steam 库指向同一份安装时只出现一次。
    monkeypatch.setattr(
        obs_launcher, "_registry_obs_paths", lambda: [exe, exe]
    )

    assert obs_launcher._candidate_paths("").count(exe) == 1


def test_logs_found_path_and_searched_locations(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="adofai_publisher.obs_launcher")
    exe = _make_exe(tmp_path / "localappdata" / "Programs" / "obs-studio")

    assert find_obs_executable("") == exe
    assert f"找到 OBS：{exe}" in caplog.text

    caplog.clear()
    exe.unlink()
    assert find_obs_executable("") is None
    assert OBS_SEARCH_LOCATIONS in caplog.text
