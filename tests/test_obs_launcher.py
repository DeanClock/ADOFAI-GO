from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from adofai_publisher import obs_launcher
from adofai_publisher.obs_launcher import (
    find_obs_executable,
    is_obs_running,
    launch_obs,
    parse_tasklist_output,
)


@pytest.fixture(autouse=True)
def _isolate_host_lookups(monkeypatch):
    """这些用例只验证基础顺序，不让它们读本机注册表 / Steam / 用户目录。"""
    monkeypatch.setattr(obs_launcher, "_registry_obs_entries", lambda: [])
    monkeypatch.setattr(obs_launcher, "_steam_root", lambda: None)
    monkeypatch.setattr(obs_launcher, "_local_appdata_obs_path", lambda: None)
    for name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        monkeypatch.delenv(name, raising=False)


def test_find_obs_prefers_configured_path(tmp_path, monkeypatch):
    exe = tmp_path / "obs64.exe"
    exe.write_bytes(b"exe")
    monkeypatch.setattr(
        "adofai_publisher.obs_launcher.shutil.which", lambda _name: None
    )

    assert find_obs_executable(str(exe)) == exe


def test_find_obs_falls_back_to_program_files(tmp_path, monkeypatch):
    root = tmp_path / "ProgramFiles"
    exe = root / "obs-studio" / "bin" / "64bit" / "obs64.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"exe")
    monkeypatch.setattr(
        "adofai_publisher.obs_launcher.shutil.which", lambda _name: None
    )
    monkeypatch.setenv("ProgramFiles", str(root))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)

    assert find_obs_executable("") == exe


def test_find_obs_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "adofai_publisher.obs_launcher.shutil.which", lambda _name: None
    )
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "empty"))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)

    assert find_obs_executable("") is None


def test_parse_tasklist_output():
    assert parse_tasklist_output(
        "obs64.exe                    1234 Console                    1     45,000 K"
    )
    assert not parse_tasklist_output("INFO: No tasks are running")


def test_is_obs_running_uses_tasklist(monkeypatch):
    monkeypatch.setattr("adofai_publisher.obs_launcher.sys.platform", "win32")
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="obs64.exe 1234 Console 1 45,000 K",
            stderr="",
        )

    monkeypatch.setattr(
        "adofai_publisher.obs_launcher.subprocess.run", fake_run
    )

    assert is_obs_running() is True
    assert calls and calls[0][0] == "tasklist"
    assert "obs64.exe" in calls[0][-2]


def test_is_obs_running_returns_false_on_tasklist_error(monkeypatch):
    monkeypatch.setattr("adofai_publisher.obs_launcher.sys.platform", "win32")

    def fail(*args, **kwargs):
        raise OSError("tasklist unavailable")

    monkeypatch.setattr(
        "adofai_publisher.obs_launcher.subprocess.run", fail
    )

    assert is_obs_running() is False


def test_launch_obs_uses_executable_directory_as_cwd(tmp_path, monkeypatch):
    exe = tmp_path / "obs64.exe"
    exe.write_bytes(b"exe")
    captured: dict[str, object] = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["cwd"] = kwargs.get("cwd")
        return object()

    monkeypatch.setattr(
        "adofai_publisher.obs_launcher.subprocess.Popen", fake_popen
    )

    launch_obs(exe)

    assert captured == {"args": [str(exe)], "cwd": str(exe.parent)}


def test_launch_obs_uses_spawn_detached(tmp_path, monkeypatch):
    exe = tmp_path / "obs64.exe"
    exe.write_bytes(b"exe")
    calls: list[tuple[Path, Path | None]] = []

    def fake_spawn(target, cwd=None, args=None):
        calls.append((Path(target), Path(cwd) if cwd else None))
        return object()

    monkeypatch.setattr(
        "adofai_publisher.obs_launcher.spawn_detached", fake_spawn
    )

    launch_obs(exe)

    assert calls == [(exe, exe.parent)]


def test_launch_obs_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        launch_obs(tmp_path / "missing" / "obs64.exe")
