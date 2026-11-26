from __future__ import annotations

import os
import subprocess
import sys

import pytest

from adofai_publisher import child_process as child_process_module
from adofai_publisher.child_process import (
    clean_child_env,
    clean_dll_search_path,
    spawn_detached,
    vc_runtime_guard_dir,
)


def _set_meipass(monkeypatch, path) -> None:
    monkeypatch.setattr(sys, "_MEIPASS", str(path), raising=False)


def _drop_meipass(monkeypatch) -> None:
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)


def test_clean_child_env_strips_meipass_entries(tmp_path, monkeypatch):
    mei = tmp_path / "_MEI00004c842"
    _set_meipass(monkeypatch, mei)
    windows = r"C:\Windows"
    tools = str(tmp_path / "tools")
    # 覆盖三种写法：原样、结尾反斜杠、大小写不同。
    path = os.pathsep.join(
        [windows, str(mei), str(mei) + "\\", str(mei).upper(), tools]
    )
    monkeypatch.setenv("PATH", path)

    env = clean_child_env()

    assert env["PATH"].split(os.pathsep) == [windows, tools]


def test_clean_child_env_strips_subdirectory_inside_meipass(tmp_path, monkeypatch):
    mei = tmp_path / "_MEI00004c842"
    _set_meipass(monkeypatch, mei)
    monkeypatch.setenv(
        "PATH", os.pathsep.join([r"C:\Windows", str(mei / "PySide6")])
    )

    assert clean_child_env()["PATH"] == r"C:\Windows"


def test_clean_child_env_keeps_path_without_meipass(monkeypatch):
    _drop_meipass(monkeypatch)
    path = os.pathsep.join([r"C:\Windows", r"C:\Temp\_MEI00004c842"])
    monkeypatch.setenv("PATH", path)

    assert clean_child_env()["PATH"] == path


def test_clean_child_env_matches_path_key_case_insensitively(tmp_path, monkeypatch):
    mei = tmp_path / "_MEI00004c842"
    _set_meipass(monkeypatch, mei)

    env = clean_child_env({"Path": os.pathsep.join([r"C:\Windows", str(mei)])})

    assert env["Path"] == r"C:\Windows"


def test_clean_child_env_drops_pyinstaller_variables(monkeypatch):
    _drop_meipass(monkeypatch)
    monkeypatch.setenv("PYTHONHOME", r"C:\Python")
    monkeypatch.setenv("PYTHONPATH", r"C:\Python\Lib")
    monkeypatch.setenv("_MEIPASS2", r"C:\Temp\_MEI00000001")
    before = dict(os.environ)

    env = clean_child_env()

    for name in ("PYTHONHOME", "PYTHONPATH", "_MEIPASS2"):
        assert name not in env
    assert dict(os.environ) == before


def test_spawn_detached_uses_clean_env_and_detaches(tmp_path, monkeypatch):
    exe = tmp_path / "obs64.exe"
    exe.write_bytes(b"exe")
    mei = tmp_path / "_MEI00004c842"
    _set_meipass(monkeypatch, mei)
    monkeypatch.setenv("PATH", os.pathsep.join([r"C:\Windows", str(mei)]))
    captured: dict[str, object] = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    spawn_detached(exe, cwd=tmp_path, args=["--port", "4455"])

    assert captured["command"] == [str(exe), "--port", "4455"]
    kwargs = captured["kwargs"]
    assert kwargs["env"]["PATH"] == r"C:\Windows"
    assert kwargs["close_fds"] is True
    assert kwargs["cwd"] == str(tmp_path)
    if sys.platform == "win32":
        flags = kwargs["creationflags"]
        assert flags & subprocess.DETACHED_PROCESS
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP


def test_spawn_detached_without_creationflags_off_windows(tmp_path, monkeypatch):
    exe = tmp_path / "obs64.exe"
    exe.write_bytes(b"exe")
    _drop_meipass(monkeypatch)
    captured: dict[str, object] = {}

    def fake_popen(command, **kwargs):
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr("adofai_publisher.child_process.sys.platform", "linux")

    spawn_detached(exe)

    assert "creationflags" not in captured["kwargs"]
    assert "cwd" not in captured["kwargs"]


def test_spawn_detached_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="executable not found"):
        spawn_detached(tmp_path / "missing.exe")


def test_clean_dll_search_path_noop_off_windows(monkeypatch):
    monkeypatch.setattr(child_process_module.sys, "platform", "linux")
    calls: list[object] = []
    monkeypatch.setattr(
        child_process_module, "set_dll_directory", lambda path: calls.append(path) or True
    )

    with clean_dll_search_path():
        pass

    assert calls == []


@pytest.mark.skipif(sys.platform != "win32", reason="DLL 搜索目录只在 Windows 上存在")
def test_spawn_detached_clears_and_restores_dll_directory(tmp_path, monkeypatch):
    exe = tmp_path / "obs64.exe"
    exe.write_bytes(b"exe")
    events: list[tuple[str, object]] = []
    previous = r"C:\Users\User\AppData\Local\Temp\_MEI00002a82"
    monkeypatch.setattr(
        child_process_module, "current_dll_directory", lambda: previous
    )
    monkeypatch.setattr(
        child_process_module,
        "set_dll_directory",
        lambda path: events.append(("set", path)) or True,
    )
    monkeypatch.setattr(
        child_process_module, "vc_runtime_guard_dir", lambda *a, **k: None
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda command, **kwargs: events.append(("popen", command)) or object(),
    )

    spawn_detached(exe)

    # 创建子进程前清空，创建后恢复，否则 _MEI 会被 obs64.exe 继承并锁住。
    assert events[0] == ("set", None)
    assert events[1][0] == "popen"
    assert events[-1] == ("set", previous)


@pytest.mark.skipif(sys.platform != "win32", reason="DLL 搜索目录只在 Windows 上存在")
def test_spawn_detached_uses_runtime_guard_directory(tmp_path, monkeypatch):
    exe = tmp_path / "obs64.exe"
    exe.write_bytes(b"exe")
    guard = tmp_path / "runtime" / "x64"
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(child_process_module, "current_dll_directory", lambda: None)
    monkeypatch.setattr(
        child_process_module,
        "set_dll_directory",
        lambda path: events.append(("set", path)) or True,
    )
    monkeypatch.setattr(
        child_process_module, "vc_runtime_guard_dir", lambda *a, **k: guard
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda command, **kwargs: events.append(("popen", command)) or object(),
    )

    spawn_detached(exe)

    assert events[0] == ("set", None)
    assert events[1] == ("set", str(guard))
    assert events[2][0] == "popen"
    assert events[-1] == ("set", None)


@pytest.mark.skipif(sys.platform != "win32", reason="DLL 搜索目录只在 Windows 上存在")
def test_spawn_detached_restores_dll_directory_when_popen_fails(tmp_path, monkeypatch):
    exe = tmp_path / "obs64.exe"
    exe.write_bytes(b"exe")
    events: list[object] = []
    previous = r"C:\Temp\_MEI00004c842"
    monkeypatch.setattr(
        child_process_module, "current_dll_directory", lambda: previous
    )
    monkeypatch.setattr(
        child_process_module,
        "set_dll_directory",
        lambda path: events.append(path) or True,
    )
    monkeypatch.setattr(
        child_process_module, "vc_runtime_guard_dir", lambda *a, **k: None
    )

    def failing_popen(command, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(subprocess, "Popen", failing_popen)

    with pytest.raises(OSError):
        spawn_detached(exe)

    assert events == [None, previous]


def test_vc_runtime_guard_dir_copies_bundled_runtime(tmp_path, monkeypatch):
    mei = tmp_path / "_MEI00004c842"
    mei.mkdir()
    (mei / "vcruntime140.dll").write_bytes(b"vcruntime")
    (mei / "vcruntime140_1.dll").write_bytes(b"vcruntime1")
    monkeypatch.setattr(child_process_module, "system_has_vc_runtime", lambda: False)

    guard = vc_runtime_guard_dir(meipass=mei, target_root=tmp_path / "data")

    assert guard == tmp_path / "data" / "runtime" / "x64"
    assert (guard / "vcruntime140.dll").read_bytes() == b"vcruntime"
    assert (guard / "vcruntime140_1.dll").read_bytes() == b"vcruntime1"
    assert not (guard / "msvcp140.dll").exists()


def test_vc_runtime_guard_dir_skips_when_system_has_runtime(tmp_path, monkeypatch):
    mei = tmp_path / "_MEI00004c842"
    mei.mkdir()
    (mei / "vcruntime140.dll").write_bytes(b"vcruntime")
    monkeypatch.setattr(child_process_module, "system_has_vc_runtime", lambda: True)

    assert vc_runtime_guard_dir(meipass=mei, target_root=tmp_path / "data") is None
    assert not (tmp_path / "data").exists()


def test_vc_runtime_guard_dir_returns_none_without_bundled_dlls(tmp_path, monkeypatch):
    mei = tmp_path / "_MEI00004c842"
    mei.mkdir()
    monkeypatch.setattr(child_process_module, "system_has_vc_runtime", lambda: False)

    assert vc_runtime_guard_dir(meipass=mei, target_root=tmp_path / "data") is None


def test_vc_runtime_guard_dir_returns_none_outside_pyinstaller(tmp_path, monkeypatch):
    _drop_meipass(monkeypatch)
    monkeypatch.setattr(child_process_module, "system_has_vc_runtime", lambda: False)

    assert vc_runtime_guard_dir(target_root=tmp_path / "data") is None


def test_vc_runtime_guard_dir_reads_meipass(tmp_path, monkeypatch):
    mei = tmp_path / "_MEI00004c842"
    mei.mkdir()
    (mei / "vcruntime140.dll").write_bytes(b"vcruntime")
    _set_meipass(monkeypatch, mei)
    monkeypatch.setattr(child_process_module, "system_has_vc_runtime", lambda: False)

    guard = vc_runtime_guard_dir(target_root=tmp_path / "data")

    assert guard is not None
    assert (guard / "vcruntime140.dll").read_bytes() == b"vcruntime"

