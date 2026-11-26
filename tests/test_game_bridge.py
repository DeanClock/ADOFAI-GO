from __future__ import annotations

import time
from pathlib import Path

import pytest

from adofai_publisher import game_bridge
from adofai_publisher.config import Settings


def _settings(tmp_path: Path, game: Path) -> Settings:
    settings = Settings(tmp_path / "settings.json")
    settings.set("umm_game_dir", str(game))
    return settings


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "bridge_source"
    source.mkdir()
    (source / "Info.json").write_text(
        f'{{"Id":"ADOFAIGOBridge","Version":"{game_bridge.BRIDGE_VERSION}"}}',
        encoding="utf-8",
    )
    (source / "ADOFAIGOBridge.dll").write_bytes(b"fake-dll")
    return source


def test_chart_file_from_entry_prefers_main_adofai(tmp_path):
    folder = tmp_path / "Song"
    folder.mkdir()
    (folder / "level.adofai").write_text("{}", encoding="utf-8")
    main = folder / "main.adofai"
    main.write_text("{}", encoding="utf-8")

    assert game_bridge.chart_file_from_entry({"folder": str(folder)}) == main


def test_chart_file_from_entry_accepts_path_and_falls_back(tmp_path):
    folder = tmp_path / "Song"
    folder.mkdir()
    chart = folder / "song.adofai"
    chart.write_text("{}", encoding="utf-8")

    assert (
        game_bridge.chart_file_from_entry({"path": str(chart), "folder": str(folder)})
        == chart
    )


def test_chart_file_from_entry_rejects_archive(tmp_path):
    archive = tmp_path / "Song.ard"
    archive.write_bytes(b"ard")

    with pytest.raises(game_bridge.GameBridgeError, match="压缩包"):
        game_bridge.chart_file_from_entry({"path": str(archive)})


def test_ensure_bridge_mod_copies_source(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    source = _source(tmp_path)
    monkeypatch.setattr(game_bridge, "bridge_source_dir", lambda: source)
    settings = _settings(tmp_path, game)

    mod_dir = game_bridge.ensure_bridge_mod(settings)

    assert mod_dir == game / "Mods" / "ADOFAIGOBridge"
    assert (mod_dir / "Info.json").is_file()
    assert (mod_dir / "ADOFAIGOBridge.dll").read_bytes() == b"fake-dll"


def test_ensure_bridge_mod_reports_missing_build(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    source = tmp_path / "empty"
    source.mkdir()
    monkeypatch.setattr(game_bridge, "bridge_source_dir", lambda: source)

    with pytest.raises(game_bridge.GameBridgeError, match="build.ps1"):
        game_bridge.ensure_bridge_mod(_settings(tmp_path, game))


def test_ensure_bridge_mod_replaces_stale_dll(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    source = _source(tmp_path)
    monkeypatch.setattr(game_bridge, "bridge_source_dir", lambda: source)
    settings = _settings(tmp_path, game)
    mod_dir = game / "Mods" / "ADOFAIGOBridge"
    mod_dir.mkdir(parents=True)
    (mod_dir / "Info.json").write_text(
        f'{{"Id":"ADOFAIGOBridge","Version":"{game_bridge.BRIDGE_VERSION}"}}',
        encoding="utf-8",
    )
    (mod_dir / "ADOFAIGOBridge.dll").write_bytes(b"stale-dll")

    game_bridge.ensure_bridge_mod(settings)

    assert (mod_dir / "ADOFAIGOBridge.dll").read_bytes() == b"fake-dll"


def test_repair_bridge_mod_reinstalls_missing_files(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    source = _source(tmp_path)
    monkeypatch.setattr(game_bridge, "bridge_source_dir", lambda: source)
    settings = _settings(tmp_path, game)
    mod_dir = game / "Mods" / "ADOFAIGOBridge"
    mod_dir.mkdir(parents=True)

    result = game_bridge.repair_bridge_mod(settings)

    assert result == mod_dir
    assert (mod_dir / "Info.json").is_file()
    assert (mod_dir / "ADOFAIGOBridge.dll").read_bytes() == b"fake-dll"


def test_uninstall_bridge_mod_removes_only_bridge_directory(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    source = _source(tmp_path)
    monkeypatch.setattr(game_bridge, "bridge_source_dir", lambda: source)
    settings = _settings(tmp_path, game)
    mod_dir = game / "Mods" / "ADOFAIGOBridge"
    other = game / "Mods" / "OtherMod"
    mod_dir.mkdir(parents=True)
    other.mkdir(parents=True)
    (other / "Info.json").write_text("{}", encoding="utf-8")

    game_bridge.uninstall_bridge_mod(settings)

    assert not mod_dir.exists()
    assert other.is_dir()


def _legacy_bridge_dir(game: Path) -> Path:
    mod_dir = game / "Mods" / "ADOFAIGOBridge.disabled"
    mod_dir.mkdir(parents=True)
    (mod_dir / "Info.json").write_text(
        f'{{"Id":"ADOFAIGOBridge","Version":"{game_bridge.BRIDGE_VERSION}"}}',
        encoding="utf-8",
    )
    (mod_dir / "ADOFAIGOBridge.dll").write_bytes(b"fake-dll")
    return mod_dir


def test_bridge_status_detects_legacy_disabled_folder(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    mod_dir = _legacy_bridge_dir(game)
    monkeypatch.setattr(game_bridge, "is_game_running", lambda: False)

    status = game_bridge.get_bridge_status(_settings(tmp_path, game))

    assert status["installed"] is True
    assert status["installed_version"] == game_bridge.BRIDGE_VERSION
    assert status["version_ok"] is True
    assert status["mod_dir"] == str(mod_dir)
    assert status["status_text"] == "已安装 · 游戏未启动"


def test_install_bridge_mod_updates_legacy_disabled_folder_in_place(
    tmp_path, monkeypatch
):
    game = tmp_path / "game"
    game.mkdir()
    source = _source(tmp_path)
    monkeypatch.setattr(game_bridge, "bridge_source_dir", lambda: source)
    settings = _settings(tmp_path, game)
    legacy = game / "Mods" / "ADOFAIGOBridge.disabled"
    legacy.mkdir(parents=True)
    (legacy / "Info.json").write_text(
        '{"Id":"ADOFAIGOBridge","Version":"0.0.1"}', encoding="utf-8"
    )
    (legacy / "ADOFAIGOBridge.dll").write_bytes(b"stale-dll")

    mod_dir = game_bridge.ensure_bridge_mod(settings)

    assert mod_dir == legacy
    assert (legacy / "ADOFAIGOBridge.dll").read_bytes() == b"fake-dll"
    # 不能再出现第二个同 Id 的目录。
    assert not (game / "Mods" / "ADOFAIGOBridge").exists()


def test_uninstall_bridge_mod_removes_legacy_disabled_folder(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    legacy = _legacy_bridge_dir(game)

    game_bridge.uninstall_bridge_mod(_settings(tmp_path, game))

    assert not legacy.exists()


def test_open_chart_without_bridge_requires_explicit_install(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    source = _source(tmp_path)
    monkeypatch.setattr(game_bridge, "bridge_source_dir", lambda: source)
    settings = _settings(tmp_path, game)
    chart = tmp_path / "Song" / "main.adofai"
    chart.parent.mkdir()
    chart.write_text("{}", encoding="utf-8")

    with pytest.raises(game_bridge.GameBridgeError, match="尚未安装"):
        game_bridge.open_chart_in_game(settings, chart, auto_install=False)

    assert not (game / "Mods" / "ADOFAIGOBridge").exists()


def test_bridge_status_reports_online_heartbeat(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    settings = _settings(tmp_path, game)
    mod_dir = game / "Mods" / "ADOFAIGOBridge"
    mod_dir.mkdir(parents=True)
    (mod_dir / "Info.json").write_text(
        f'{{"Id":"ADOFAIGOBridge","Version":"{game_bridge.BRIDGE_VERSION}"}}',
        encoding="utf-8",
    )
    (mod_dir / "ADOFAIGOBridge.dll").write_bytes(b"fake-dll")
    (mod_dir / "heartbeat.txt").write_text(
        f"version={game_bridge.BRIDGE_VERSION}\nstate=idle\ntime={int(time.time())}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(game_bridge, "is_game_running", lambda: True)

    status = game_bridge.get_bridge_status(settings)

    assert status["installed"] is True
    assert status["installed_version"] == game_bridge.BRIDGE_VERSION
    assert status["online"] is True
    assert "在线" in status["status_text"]
    assert status["log_path"].endswith("bridge.log")


def test_bridge_status_handles_missing_bridge(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    monkeypatch.setattr(game_bridge, "is_game_running", lambda: False)

    status = game_bridge.get_bridge_status(_settings(tmp_path, game))

    assert status["installed"] is False
    assert status["online"] is False
    assert status["status_text"] == "未安装"


def test_open_chart_waits_for_matching_status(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    source = _source(tmp_path)
    monkeypatch.setattr(game_bridge, "bridge_source_dir", lambda: source)
    settings = _settings(tmp_path, game)
    chart = tmp_path / "Song" / "main.adofai"
    chart.parent.mkdir()
    chart.write_text("{}", encoding="utf-8")
    mod_dir = game_bridge.ensure_bridge_mod(settings)
    (mod_dir / "heartbeat.txt").write_text(
        f"version=0.1.0\ntime={int(time.time())}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(game_bridge, "is_game_running", lambda: True)

    def fake_status(path: Path):
        request = (path / "request.txt").read_text(encoding="utf-8")
        values = dict(
            line.split("=", 1) for line in request.splitlines() if "=" in line
        )
        return {
            "id": values["id"],
            "ok": "1",
            "message": "已在编辑器中打开",
        }

    monkeypatch.setattr(game_bridge, "read_status", fake_status)

    result = game_bridge.open_chart_in_game(settings, chart)

    assert result.chart_path == chart
    assert result.launched is False
    assert result.message == "已在编辑器中打开"


def test_open_chart_requires_loaded_mod_when_running(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    source = _source(tmp_path)
    monkeypatch.setattr(game_bridge, "bridge_source_dir", lambda: source)
    settings = _settings(tmp_path, game)
    chart = tmp_path / "Song" / "main.adofai"
    chart.parent.mkdir()
    chart.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(game_bridge, "is_game_running", lambda: True)
    monkeypatch.setattr(game_bridge, "wait_for_heartbeat", lambda *args: None)

    with pytest.raises(game_bridge.GameBridgeError, match="没有响应"):
        game_bridge.open_chart_in_game(settings, chart)


def test_open_chart_launches_game_when_not_running(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    source = _source(tmp_path)
    monkeypatch.setattr(game_bridge, "bridge_source_dir", lambda: source)
    settings = _settings(tmp_path, game)
    chart = tmp_path / "Song" / "main.adofai"
    chart.parent.mkdir()
    chart.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(game_bridge, "is_game_running", lambda: False)

    def fake_launch(settings_arg):
        mod_dir = game / "Mods" / "ADOFAIGOBridge"
        (mod_dir / "heartbeat.txt").write_text(
            f"version=0.1.0\ntime={int(time.time())}\n",
            encoding="utf-8",
        )
        return game / "A Dance of Fire and Ice.exe"

    monkeypatch.setattr(game_bridge, "launch_game", fake_launch)
    monkeypatch.setattr(
        game_bridge,
        "read_status",
        lambda path: {
            "id": dict(
                line.split("=", 1)
                for line in (path / "request.txt").read_text(encoding="utf-8").splitlines()
                if "=" in line
            )["id"],
            "ok": "1",
            "message": "已请求编辑器载入",
        },
    )

    result = game_bridge.open_chart_in_game(settings, chart, timeout=1)

    assert result.launched is True


def test_open_chart_timeout_keeps_actionable_error(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    source = _source(tmp_path)
    monkeypatch.setattr(game_bridge, "bridge_source_dir", lambda: source)
    settings = _settings(tmp_path, game)
    chart = tmp_path / "Song" / "main.adofai"
    chart.parent.mkdir()
    chart.write_text("{}", encoding="utf-8")
    mod_dir = game_bridge.ensure_bridge_mod(settings)
    (mod_dir / "heartbeat.txt").write_text(
        f"version=0.1.0\ntime={int(time.time())}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(game_bridge, "is_game_running", lambda: True)
    monkeypatch.setattr(game_bridge, "read_status", lambda path: None)

    with pytest.raises(game_bridge.GameBridgeError, match="没有收到确认"):
        game_bridge.open_chart_in_game(
            settings,
            chart,
            timeout=0.05,
            poll_interval=0.01,
        )


def test_default_wait_limits_leave_room_for_bridge_fallback():
    # 模组内部最多等 20 秒，工具端必须留出余量，否则会先弹超时。
    assert game_bridge.GAME_RUNNING_WAIT_SECONDS > 20.0
    assert game_bridge.GAME_LAUNCH_WAIT_SECONDS > game_bridge.GAME_RUNNING_WAIT_SECONDS


def _ready_running_game(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    source = _source(tmp_path)
    monkeypatch.setattr(game_bridge, "bridge_source_dir", lambda: source)
    settings = _settings(tmp_path, game)
    chart = tmp_path / "Song" / "main.adofai"
    chart.parent.mkdir()
    chart.write_text("{}", encoding="utf-8")
    mod_dir = game_bridge.ensure_bridge_mod(settings)
    (mod_dir / "heartbeat.txt").write_text(
        f"version={game_bridge.BRIDGE_VERSION}\ntime={int(time.time())}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(game_bridge, "is_game_running", lambda: True)
    monkeypatch.setattr(game_bridge, "read_status", lambda path: None)
    return settings, chart


def test_open_chart_uses_running_wait_limit_by_default(tmp_path, monkeypatch):
    settings, chart = _ready_running_game(tmp_path, monkeypatch)
    monkeypatch.setattr(game_bridge, "GAME_RUNNING_WAIT_SECONDS", 1.0)

    with pytest.raises(game_bridge.GameBridgeError, match="1 秒内没有收到确认"):
        game_bridge.open_chart_in_game(settings, chart, poll_interval=0.05)


def test_open_chart_uses_launch_wait_limit_by_default(tmp_path, monkeypatch):
    settings, chart = _ready_running_game(tmp_path, monkeypatch)
    monkeypatch.setattr(game_bridge, "GAME_RUNNING_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(game_bridge, "GAME_LAUNCH_WAIT_SECONDS", 2.0)
    monkeypatch.setattr(game_bridge, "is_game_running", lambda: False)
    monkeypatch.setattr(game_bridge, "wait_for_heartbeat", lambda *args: {})
    monkeypatch.setattr(game_bridge, "launch_game", lambda settings_arg: None)

    with pytest.raises(game_bridge.GameBridgeError, match="2 秒内没有收到确认"):
        game_bridge.open_chart_in_game(settings, chart, poll_interval=0.05)
