from __future__ import annotations

import json
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from adofai_publisher.config import Settings
from adofai_publisher.umm import (
    MODS_DIR_NAME,
    STEAM_APP_ID,
    STEAM_RUN_GAME_URL,
    UMM_SOURCE_FILES,
    UmmCancelled,
    UmmError,
    _version_sort_key,
    fetch_umm_update,
    install_local_update,
    install_mod,
    install_mod_archive,
    install_umm,
    is_valid_game_dir,
    launch_game,
    list_mods,
    resolve_mod_dir,
    set_mod_enabled,
    umm_installer_source,
    uninstall_mod,
    uninstall_umm,
)


def _write_mod_zip(path: Path, mod_id: str, version: str = "1.0.0") -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            f"{mod_id}/Info.json",
            json.dumps(
                {
                    "Id": mod_id,
                    "DisplayName": mod_id,
                    "Author": "Test",
                    "Version": version,
                }
            ),
        )
        zf.writestr(f"{mod_id}/mod.dll", b"binary")


def _make_versioned_environment(
    tmp_path: Path, archives: dict[str, list[tuple[str, str]]]
) -> tuple[Path, Path, Settings]:
    """构造只含指定安装器压缩包的临时环境（archives: mod -> [(文件名, 版本)])。"""
    game = tmp_path / "game"
    game.mkdir()
    (game / "A Dance of Fire and Ice.exe").write_bytes(b"")
    (game / MODS_DIR_NAME).mkdir()
    installer = tmp_path / "installer"
    installer.mkdir()
    for mod_id, entries in archives.items():
        mod_root = installer / "ADOFAI" / mod_id
        mod_root.mkdir(parents=True)
        for filename, version in entries:
            _write_mod_zip(mod_root / filename, mod_id, version)
    settings = Settings(tmp_path / "home" / "settings.json")
    settings.set("umm_game_dir", str(game))
    settings.save()
    return game, installer, settings


def _install_mod_folder(
    game: Path, mod_id: str, version: str, *, display_name: str | None = None, author: str = "Installed"
) -> Path:
    installed = game / MODS_DIR_NAME / mod_id
    installed.mkdir(parents=True)
    (installed / "Info.json").write_text(
        json.dumps(
            {
                "Id": mod_id,
                "DisplayName": display_name or mod_id,
                "Author": author,
                "Version": version,
            }
        ),
        encoding="utf-8",
    )
    return installed


PARAMS_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<Param>
  <Language>zh</Language>
  <ModParams>
    <Mod Id="OtherMod" Enabled="false">
      <Hotkey>Ctrl+F1</Hotkey>
    </Mod>
    <Mod Id="AdofaiTweaks" Enabled="true">
      <Hotkey>Ctrl+F2</Hotkey>
    </Mod>
  </ModParams>
</Param>
"""


def _params_path(game: Path) -> Path:
    return (
        game
        / "A Dance of Fire and Ice_Data"
        / "Managed"
        / "UnityModManager"
        / "Params.xml"
    )


def _write_params(game: Path, text: str) -> Path:
    path = _params_path(game)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _params_states(game: Path) -> dict[str, bool]:
    root = ET.fromstring(_params_path(game).read_text(encoding="utf-8-sig"))
    container = root.find("ModParams")
    assert container is not None
    states: dict[str, bool] = {}
    for node in container.findall("Mod"):
        raw = (node.get("Enabled") or "true").strip().lower()
        states[node.get("Id") or ""] = raw != "false"
    return states


def _make_environment(tmp_path: Path) -> tuple[Path, Path, Settings]:
    game = tmp_path / "game"
    game.mkdir()
    (game / "A Dance of Fire and Ice.exe").write_bytes(b"")
    # 真实游戏目录一定有 *_Data\Managed；UMM 运行库要装到这里。
    managed = game / "A Dance of Fire and Ice_Data" / "Managed"
    managed.mkdir(parents=True)
    (managed / "Assembly-CSharp.dll").write_bytes(b"")
    (managed / "System.Xml.dll").write_bytes(b"")
    installer = tmp_path / "installer"
    installer.mkdir()
    for name in UMM_SOURCE_FILES:
        content = (
            b'<Config><GameInfo Name="A Dance of Fire and Ice" /></Config>'
            if name == "UnityModManagerConfig.xml"
            else b""
        )
        (installer / name).write_bytes(content)
    harmony = installer / "Harmony" / "2.2"
    harmony.mkdir(parents=True)
    (harmony / "0Harmony.dll").write_bytes(b"")
    mod_root = installer / "ADOFAI" / "AdofaiTweaks"
    mod_root.mkdir(parents=True)
    _write_mod_zip(mod_root / "AdofaiTweaks-2-9-2.zip", "AdofaiTweaks")
    settings_path = tmp_path / "home" / "settings.json"
    settings = Settings(settings_path)
    settings.set("umm_game_dir", str(game))
    settings.save()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    return game, installer, settings


def test_detect_and_valid_game_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game, _installer, _settings = _make_environment(tmp_path)
    assert is_valid_game_dir(game)
    assert not is_valid_game_dir(tmp_path / "missing")


def test_list_mods_from_installer(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    _game, installer, settings = _make_environment(tmp_path)
    monkeypatch.setattr(
        "adofai_publisher.umm.umm_installer_source",
        lambda *_args, **_kwargs: installer,
    )
    mods = list_mods(settings)
    assert len(mods) == 1
    mod = mods[0]
    assert mod["id"] == "AdofaiTweaks"
    assert mod["display_name"] == "AdofaiTweaks"
    assert mod["version"] == "1.0.0"
    assert mod["installed"] is False
    assert len(mod["archives"]) == 1


def test_install_umm_copies_files_and_writes_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game, installer, settings = _make_environment(tmp_path)
    monkeypatch.setattr(
        "adofai_publisher.umm.umm_installer_source",
        lambda *_args, **_kwargs: installer,
    )
    result = install_umm(settings)
    assert result == game
    # 官方布局：本体在 Managed\UnityModManager，游戏根目录只留 doorstop。
    manager = (
        game
        / "A Dance of Fire and Ice_Data"
        / "Managed"
        / "UnityModManager"
    )
    assert (manager / "UnityModManager.dll").is_file()
    assert (manager / "UnityModManager.xml").is_file()
    assert (manager / "dnlib.dll").is_file()
    assert (manager / "0Harmony.dll").is_file()
    for name in UMM_SOURCE_FILES:
        assert not (game / name).exists(), name
    assert (game / MODS_DIR_NAME).is_dir()
    assert (game / "winhttp.dll").is_file()
    assert (game / "doorstop_config.ini").is_file()
    config = (manager / "Config.xml").read_text(encoding="utf-8")
    # 安装器 GameInfo 没有启动挂点时退回官方内置模板。
    assert "ADOStartup.Startup" in config
    assert "<ModsDirectory>Mods</ModsDirectory>" in config
    assert "<RelativeModsDirectory>" not in config


def test_install_and_toggle_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game, installer, settings = _make_environment(tmp_path)
    monkeypatch.setattr(
        "adofai_publisher.umm.umm_installer_source",
        lambda *_args, **_kwargs: installer,
    )
    install_umm(settings)
    result = install_mod(settings, "AdofaiTweaks")
    assert result["installed"] is True
    target = game / MODS_DIR_NAME / "AdofaiTweaks"
    assert (target / "Info.json").is_file()
    assert (target / "mod.dll").is_file()
    set_mod_enabled(settings, "AdofaiTweaks", False)
    # UMM 不看目录名后缀，禁用只写 Params.xml，目录保持不变。
    assert target.is_dir()
    assert not (game / MODS_DIR_NAME / "AdofaiTweaks.disabled").exists()
    assert _params_states(game)["AdofaiTweaks"] is False
    set_mod_enabled(settings, "AdofaiTweaks", True)
    assert target.is_dir()
    assert _params_states(game)["AdofaiTweaks"] is True
    uninstall_mod(settings, "AdofaiTweaks")
    assert not target.exists()
    assert "AdofaiTweaks" not in _params_states(game)


def test_uninstall_umm_removes_only_manager_files(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game, installer, settings = _make_environment(tmp_path)
    monkeypatch.setattr(
        "adofai_publisher.umm.umm_installer_source",
        lambda *_args, **_kwargs: installer,
    )
    install_umm(settings)
    (game / "A Dance of Fire and Ice.exe").write_bytes(b"exe")
    uninstall_umm(settings)
    assert not (game / "UnityModManager.exe").exists()
    assert not (game / "winhttp.dll").exists()
    assert not (game / "doorstop_config.ini").exists()
    assert (game / "A Dance of Fire and Ice.exe").is_file()
    assert (game / MODS_DIR_NAME).is_dir()
    managed = game / "A Dance of Fire and Ice_Data" / "Managed"
    assert not (managed / "UnityModManager").exists()
    assert (managed / "Assembly-CSharp.dll").is_file()


def test_launch_game_starts_exe_in_game_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game, _installer, settings = _make_environment(tmp_path)
    calls: list[tuple[str, str]] = []

    def fake_spawn(exe, cwd=None, args=None):
        calls.append((str(exe), str(cwd)))
        return object()

    monkeypatch.setattr("adofai_publisher.umm.spawn_detached", fake_spawn)
    exe = launch_game(settings)
    assert exe == game / "A Dance of Fire and Ice.exe"
    assert calls == [(str(game / "A Dance of Fire and Ice.exe"), str(game))]


def test_launch_game_rejects_missing_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    settings = Settings(tmp_path / "settings.json")
    settings.set("umm_game_dir", str(tmp_path / "missing"))
    monkeypatch.setattr("adofai_publisher.umm.default_game_dir", lambda: None)
    try:
        launch_game(settings)
    except UmmError as exc:
        assert "未找到 ADOFAI 游戏目录" in str(exc)
    else:
        raise AssertionError("expected UmmError")


def test_launch_game_rejects_directory_without_exe(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = tmp_path / "game"
    game.mkdir()
    settings = Settings(tmp_path / "settings.json")
    settings.set("umm_game_dir", str(game))
    try:
        launch_game(settings)
    except UmmError as exc:
        assert "缺少 A Dance of Fire and Ice.exe" in str(exc)
    else:
        raise AssertionError("expected UmmError")


def _make_steam_library(root: Path) -> Path:
    game = root / "steamapps" / "common" / "A Dance of Fire and Ice"
    game.mkdir(parents=True)
    (game / "A Dance of Fire and Ice.exe").write_bytes(b"")
    return game


def _steam_settings(tmp_path: Path, game: Path) -> Settings:
    settings = Settings(tmp_path / "steam-home" / "settings.json")
    settings.set("umm_game_dir", str(game))
    return settings


def _record_launch(monkeypatch):
    opened: list[str] = []
    spawned: list[tuple[str, str]] = []
    monkeypatch.setattr("adofai_publisher.umm._open_steam_url", opened.append)
    monkeypatch.setattr(
        "adofai_publisher.umm.spawn_detached",
        lambda exe, cwd=None, args=None: spawned.append((str(exe), str(cwd))),
    )
    return opened, spawned


def test_launch_game_uses_steam_url_for_steam_install(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    root = tmp_path / "Steam"
    game = _make_steam_library(root)
    settings = _steam_settings(tmp_path, game)
    monkeypatch.setattr("adofai_publisher.umm.steam_path", lambda: root)
    opened, spawned = _record_launch(monkeypatch)

    exe = launch_game(settings)

    assert exe == game / "A Dance of Fire and Ice.exe"
    assert STEAM_APP_ID == "977950"
    assert opened == [STEAM_RUN_GAME_URL]
    assert spawned == []


def test_launch_game_detects_steam_install_in_second_library(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    root = tmp_path / "Steam"
    second = tmp_path / "SteamLibrary"
    game = _make_steam_library(second)
    steamapps = root / "steamapps"
    steamapps.mkdir(parents=True)
    (steamapps / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n'
        '    "0"\n    {\n        "path"    "%s"\n    }\n'
        '    "1"\n    {\n        "path"    "%s"\n    }\n'
        "}\n" % (root, second),
        encoding="utf-8",
    )
    settings = _steam_settings(tmp_path, game)
    monkeypatch.setattr("adofai_publisher.umm.steam_path", lambda: root)
    opened, spawned = _record_launch(monkeypatch)

    launch_game(settings)

    assert opened == [STEAM_RUN_GAME_URL]
    assert spawned == []


def test_launch_game_falls_back_when_steam_url_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    root = tmp_path / "Steam"
    game = _make_steam_library(root)
    settings = _steam_settings(tmp_path, game)
    monkeypatch.setattr("adofai_publisher.umm.steam_path", lambda: root)
    opened, spawned = _record_launch(monkeypatch)

    def failing_open(url: str) -> None:
        opened.append(url)
        raise OSError("没有 steam:// 处理程序")

    monkeypatch.setattr("adofai_publisher.umm._open_steam_url", failing_open)

    launch_game(settings)

    exe = game / "A Dance of Fire and Ice.exe"
    assert opened == [STEAM_RUN_GAME_URL]
    assert spawned == [(str(exe), str(game))]


def test_launch_game_spawns_directly_without_steam(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    root = tmp_path / "Steam"
    game = _make_steam_library(root)
    settings = _steam_settings(tmp_path, game)
    monkeypatch.setattr("adofai_publisher.umm.steam_path", lambda: None)
    opened, spawned = _record_launch(monkeypatch)

    launch_game(settings)

    exe = game / "A Dance of Fire and Ice.exe"
    assert opened == []
    assert spawned == [(str(exe), str(game))]


def test_installer_source_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    fallback = tmp_path / "UnityModManagerInstaller"
    fallback.mkdir(parents=True)
    (fallback / "UnityModManager.exe").write_bytes(b"")
    assert umm_installer_source() == fallback


def test_installer_source_prefers_configured_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    configured = tmp_path / "custom-installer"
    configured.mkdir()
    fallback = tmp_path / "UnityModManagerInstaller"
    fallback.mkdir(parents=True)
    settings = Settings(tmp_path / "settings.json")
    settings.set("umm_installer_dir", str(configured))

    assert umm_installer_source(settings) == configured


def test_installer_source_skips_missing_configured_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    fallback = tmp_path / "UnityModManagerInstaller"
    fallback.mkdir(parents=True)
    settings = Settings(tmp_path / "settings.json")
    settings.set("umm_installer_dir", str(tmp_path / "missing"))

    assert umm_installer_source(settings) == fallback


def test_installer_source_ignores_configured_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    configured = tmp_path / "installer.txt"
    configured.write_bytes(b"")
    fallback = tmp_path / "UnityModManagerInstaller"
    fallback.mkdir(parents=True)
    settings = Settings(tmp_path / "settings.json")
    settings.set("umm_installer_dir", str(configured))

    assert umm_installer_source(settings) == fallback


def test_installer_source_returns_none_without_installer(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    settings = Settings(tmp_path / "settings.json")

    assert umm_installer_source() is None
    assert umm_installer_source(settings) is None


def test_install_umm_and_list_mods_use_configured_installer_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game, installer, settings = _make_environment(tmp_path)
    settings.set("umm_installer_dir", str(installer))
    settings.save()

    assert umm_installer_source(settings) == installer
    install_umm(settings)
    manager = (
        game
        / "A Dance of Fire and Ice_Data"
        / "Managed"
        / "UnityModManager"
        / "UnityModManager.dll"
    )
    assert manager.is_file()
    assert [mod["id"] for mod in list_mods(settings)] == ["AdofaiTweaks"]


def test_uninstall_uses_info_json_id_when_folder_differs(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game, _installer, settings = _make_environment(tmp_path)
    mod_dir = game / MODS_DIR_NAME / "cherytools-fable"
    mod_dir.mkdir(parents=True)
    (mod_dir / "Info.json").write_text(
        json.dumps({"Id": "CheryTools", "DisplayName": "CheryTools"}), encoding="utf-8"
    )
    (mod_dir / "a.dll").write_bytes(b"x")

    assert resolve_mod_dir(settings, "CheryTools") == mod_dir
    uninstall_mod(settings, "CheryTools")
    assert not mod_dir.exists()
    assert resolve_mod_dir(settings, "CheryTools") is None


def test_uninstall_reports_error_when_not_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    _game, _installer, settings = _make_environment(tmp_path)
    from adofai_publisher.umm import UmmError

    try:
        uninstall_mod(settings, "Nothing")
    except UmmError as exc:
        assert "没有找到" in str(exc)
    else:
        raise AssertionError("expected UmmError")


def test_install_renames_folder_to_real_id(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game, _installer, settings = _make_environment(tmp_path)
    archive = tmp_path / "cherytools-fable.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("cherytools-fable/Info.json", json.dumps({"Id": "CheryTools", "DisplayName": "CheryTools"}))
        zf.writestr("cherytools-fable/a.dll", b"x")
    result = install_mod_archive(settings, "cherytools-fable", archive)
    assert result["id"] == "CheryTools"
    assert (game / MODS_DIR_NAME / "CheryTools" / "Info.json").is_file()
    assert not (game / MODS_DIR_NAME / "cherytools-fable").exists()


def test_disable_and_enable_with_mismatched_folder_name(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game, _installer, settings = _make_environment(tmp_path)
    mod_dir = game / MODS_DIR_NAME / "cherytools-fable"
    mod_dir.mkdir(parents=True)
    (mod_dir / "Info.json").write_text(json.dumps({"Id": "CheryTools"}), encoding="utf-8")

    set_mod_enabled(settings, "CheryTools", False)
    assert mod_dir.is_dir()
    assert not (game / MODS_DIR_NAME / "cherytools-fable.disabled").exists()
    assert _params_states(game)["CheryTools"] is False
    set_mod_enabled(settings, "CheryTools", True)
    assert mod_dir.is_dir()
    assert _params_states(game)["CheryTools"] is True


def test_set_mod_enabled_preserves_other_entries_and_hotkeys(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game, _installer, settings = _make_environment(tmp_path)
    _install_mod_folder(game, "AdofaiTweaks", "1.0.0")
    _write_params(game, PARAMS_SAMPLE)

    set_mod_enabled(settings, "AdofaiTweaks", False)

    root = ET.fromstring(_params_path(game).read_text(encoding="utf-8-sig"))
    assert root.findtext("Language") == "zh"
    container = root.find("ModParams")
    assert container is not None
    mods = {node.get("Id"): node for node in container.findall("Mod")}
    assert mods["AdofaiTweaks"].get("Enabled") == "false"
    assert mods["AdofaiTweaks"].findtext("Hotkey") == "Ctrl+F2"
    assert mods["OtherMod"].get("Enabled") == "false"
    assert mods["OtherMod"].findtext("Hotkey") == "Ctrl+F1"


def test_set_mod_enabled_creates_params_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game, _installer, settings = _make_environment(tmp_path)
    _install_mod_folder(game, "AdofaiTweaks", "1.0.0")
    assert not _params_path(game).exists()

    set_mod_enabled(settings, "AdofaiTweaks", False)

    assert _params_path(game).is_file()
    assert _params_states(game) == {"AdofaiTweaks": False}
    root = ET.fromstring(_params_path(game).read_text(encoding="utf-8-sig"))
    assert root.tag == "Param"
    assert root.find("ModParams") is not None


def test_set_mod_enabled_matches_id_case_insensitively(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game, _installer, settings = _make_environment(tmp_path)
    _install_mod_folder(game, "AdofaiTweaks", "1.0.0")
    _write_params(
        game,
        '<?xml version="1.0" encoding="utf-8"?>'
        '<Param><ModParams><Mod Id="adofaitweaks" Enabled="true" />'
        "</ModParams></Param>",
    )

    set_mod_enabled(settings, "AdofaiTweaks", False)

    root = ET.fromstring(_params_path(game).read_text(encoding="utf-8-sig"))
    nodes = root.find("ModParams").findall("Mod")
    assert len(nodes) == 1
    assert nodes[0].get("Id") == "adofaitweaks"
    assert nodes[0].get("Enabled") == "false"


def test_set_mod_enabled_refuses_corrupt_params(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game, _installer, settings = _make_environment(tmp_path)
    _install_mod_folder(game, "AdofaiTweaks", "1.0.0")
    path = _write_params(game, "<Param><ModParams>")
    original = path.read_bytes()

    with pytest.raises(UmmError, match="参数文件无法解析"):
        set_mod_enabled(settings, "AdofaiTweaks", False)

    assert path.read_bytes() == original


def test_list_mods_reads_enabled_state_from_params(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game, _installer, settings = _make_environment(tmp_path)
    _install_mod_folder(game, "AdofaiTweaks", "1.0.0")
    _write_params(
        game,
        '<?xml version="1.0" encoding="utf-8"?>'
        '<Param><ModParams><Mod Id="AdofaiTweaks" Enabled="false" />'
        "</ModParams></Param>",
    )

    mods = list_mods(settings)

    assert [mod["id"] for mod in mods] == ["AdofaiTweaks"]
    assert mods[0]["enabled"] is False


def test_legacy_disabled_folder_uses_params_and_renames_back(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game, _installer, settings = _make_environment(tmp_path)
    legacy = game / MODS_DIR_NAME / "AdofaiTweaks.disabled"
    legacy.mkdir(parents=True)
    (legacy / "Info.json").write_text(json.dumps({"Id": "AdofaiTweaks"}), encoding="utf-8")

    # 没有 Params.xml 条目 = UMM 默认启用；旧目录后缀不应影响状态。
    mods = list_mods(settings)
    assert mods[0]["id"] == "AdofaiTweaks"
    assert mods[0]["enabled"] is True

    set_mod_enabled(settings, "AdofaiTweaks", False)

    assert (game / MODS_DIR_NAME / "AdofaiTweaks").is_dir()
    assert not legacy.exists()
    assert _params_states(game)["AdofaiTweaks"] is False


def test_legacy_disabled_folder_reads_params_state(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game, _installer, settings = _make_environment(tmp_path)
    legacy = game / MODS_DIR_NAME / "AdofaiTweaks.disabled"
    legacy.mkdir(parents=True)
    (legacy / "Info.json").write_text(json.dumps({"Id": "AdofaiTweaks"}), encoding="utf-8")
    _write_params(
        game,
        '<?xml version="1.0" encoding="utf-8"?>'
        '<Param><ModParams><Mod Id="AdofaiTweaks" Enabled="false" />'
        "</ModParams></Param>",
    )

    mods = list_mods(settings)
    assert mods[0]["enabled"] is False

    set_mod_enabled(settings, "AdofaiTweaks", True)

    assert (game / MODS_DIR_NAME / "AdofaiTweaks").is_dir()
    assert _params_states(game)["AdofaiTweaks"] is True


def test_install_mod_archive_enables_existing_params_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game, _installer, settings = _make_environment(tmp_path)
    _write_params(
        game,
        '<?xml version="1.0" encoding="utf-8"?>'
        '<Param><ModParams><Mod Id="CheryTools" Enabled="false" />'
        "</ModParams></Param>",
    )
    archive = tmp_path / "CheryTools.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("CheryTools/Info.json", json.dumps({"Id": "CheryTools"}))
        zf.writestr("CheryTools/a.dll", b"x")

    install_mod_archive(settings, "CheryTools", archive)

    assert _params_states(game)["CheryTools"] is True


def test_uninstall_mod_removes_params_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game, _installer, settings = _make_environment(tmp_path)
    _install_mod_folder(game, "AdofaiTweaks", "1.0.0")
    _write_params(game, PARAMS_SAMPLE)

    uninstall_mod(settings, "AdofaiTweaks")

    states = _params_states(game)
    assert "AdofaiTweaks" not in states
    # 其它模组条目不受影响。
    assert states["OtherMod"] is False


def test_list_mods_merges_installed_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game, installer, settings = _make_environment(tmp_path)
    monkeypatch.setattr(
        "adofai_publisher.umm.umm_installer_source",
        lambda *_args, **_kwargs: installer,
    )
    installed = game / MODS_DIR_NAME / "AdofaiTweaks"
    installed.mkdir(parents=True)
    (installed / "Info.json").write_text(
        json.dumps({"Id": "AdofaiTweaks", "DisplayName": "AdofaiTweaks", "Version": "2.0.0"}),
        encoding="utf-8",
    )
    (installed / "mod.dll").write_bytes(b"")
    skipped = game / MODS_DIR_NAME / "__MACOSX"
    skipped.mkdir()
    mods = list_mods(settings)
    assert len(mods) == 1
    assert mods[0]["installed"] is True
    assert mods[0]["enabled"] is True
    # 已安装目录的 Info.json 优先于安装器压缩包里的旧版本。
    assert mods[0]["version"] == "2.0.0"


def test_version_sort_key_orders_numerically():
    assert _version_sort_key("2.10") > _version_sort_key("2.9")
    assert _version_sort_key("2.17") > _version_sort_key("2.14")
    assert _version_sort_key("2.1.1") > _version_sort_key("2.0.0-alpha-137")
    # 空版本排在有版本号之后。
    assert _version_sort_key("") > _version_sort_key("0.0.1")
    assert max(["2.14", "2.17", "2.9"], key=_version_sort_key) == "2.17"


def test_list_mods_picks_newest_installer_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    _game, installer, settings = _make_versioned_environment(
        tmp_path,
        {
            "Creplay": [
                ("Creplay-2-14.zip", "2.14"),
                ("Creplay-2-9.zip", "2.9"),
                ("Creplay-2-17.zip", "2.17"),
            ]
        },
    )
    monkeypatch.setattr(
        "adofai_publisher.umm.umm_installer_source",
        lambda *_args, **_kwargs: installer,
    )
    mods = list_mods(settings)
    assert len(mods) == 1
    assert mods[0]["id"] == "Creplay"
    assert mods[0]["version"] == "2.17"
    assert mods[0]["installed"] is False
    # 「x 个版本」仍统计压缩包数量，含义不变。
    assert len(mods[0]["archives"]) == 3


def test_list_mods_prefers_installed_version_over_installer(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game, installer, settings = _make_versioned_environment(
        tmp_path, {"Creplay": [("Creplay-2-14.zip", "2.14")]}
    )
    monkeypatch.setattr(
        "adofai_publisher.umm.umm_installer_source",
        lambda *_args, **_kwargs: installer,
    )
    _install_mod_folder(
        game, "Creplay", "2.17", display_name="Creplay Installed", author="Real Author"
    )
    mods = list_mods(settings)
    assert len(mods) == 1
    mod = mods[0]
    assert mod["installed"] is True
    assert mod["enabled"] is True
    assert mod["version"] == "2.17"
    assert mod["display_name"] == "Creplay Installed"
    assert mod["author"] == "Real Author"
    # 安装器条目的压缩包信息要保留，卸载/重装仍可用。
    assert mod["has_archive"] is True
    assert len(mod["archives"]) == 1


def test_list_mods_quartz_uses_installed_version(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game, installer, settings = _make_versioned_environment(
        tmp_path, {"Quartz": [("Quartz-2.0.0-alpha-137.zip", "2.0.0-alpha-137")]}
    )
    monkeypatch.setattr(
        "adofai_publisher.umm.umm_installer_source",
        lambda *_args, **_kwargs: installer,
    )
    _install_mod_folder(game, "Quartz", "2.1.1")
    mods = list_mods(settings)
    assert len(mods) == 1
    assert mods[0]["version"] == "2.1.1"
    assert mods[0]["installed"] is True


class _FakeResponse:
    def __init__(self, payload: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self._payload = payload
        self.headers = {"content-length": str(len(payload))}
        if headers:
            self.headers.update(headers)

    def raise_for_status(self) -> None:
        if not self._ok:
            raise RuntimeError("http error")

    def json(self):
        import json as _json

        return _json.loads(self._payload.decode("utf-8"))

    def iter_content(self, chunk_size: int):
        for start in range(0, len(self._payload), chunk_size):
            yield self._payload[start : start + chunk_size]

    @property
    def _ok(self) -> bool:
        return True


def test_fetch_umm_update_downloads_and_extracts(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    installer = tmp_path / "UnityModManagerInstaller"
    archive_bytes = _make_update_zip_bytes()

    def fake_get(url, timeout=20.0, stream=False):
        if "Repository_beta_13" in url:
            return _FakeResponse(
                b'{"Releases":[{"Id":"UnityModManager","Version":"0.33.3",'
                b'"DownloadUrl":"https://example.com/umm_update.zip"}]}'
            )
        return _FakeResponse(archive_bytes)

    monkeypatch.setattr("adofai_publisher.umm._http_get", fake_get)
    version, target = fetch_umm_update("ghfast.top")
    assert version == "0.33.3"
    assert target == installer
    assert (installer / "UnityModManager.exe").is_file()


def _make_update_zip_bytes() -> bytes:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("UnityModManager.exe", b"exe")
        zf.writestr("UnityModManager.dll", b"dll")
        zf.writestr("0Harmony.dll", b"harmony")
    return buffer.getvalue()


def test_fetch_umm_update_fails_when_repository_unreachable(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))

    def fake_get(url, timeout=20.0, stream=False):
        raise RuntimeError("network down")

    monkeypatch.setattr("adofai_publisher.umm._http_get", fake_get)
    try:
        fetch_umm_update("ghfast.top")
    except UmmError as exc:
        assert "无法读取官方版本信息" in str(exc)
    else:
        raise AssertionError("expected UmmError")


def test_fetch_umm_update_cancel(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))

    def fake_get(url, timeout=20.0, stream=False):
        if "Repository_beta_13" in url:
            return _FakeResponse(
                b'{"Releases":[{"Id":"UnityModManager","Version":"0.33.3",'
                b'"DownloadUrl":"https://example.com/umm_update.zip"}]}'
            )
        return _FakeResponse(b"x" * 1024)

    monkeypatch.setattr("adofai_publisher.umm._http_get", fake_get)
    cancelled = False

    def cancel():
        nonlocal cancelled
        cancelled = True
        return True

    try:
        fetch_umm_update("ghfast.top", cancel=cancel)
    except UmmCancelled:
        pass
    else:
        raise AssertionError("expected UmmCancelled")


def test_install_local_update_extracts_to_installer_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    installer = tmp_path / "UnityModManagerInstaller"
    installer.mkdir(parents=True)
    (installer / "umm_update.zip").write_bytes(_make_update_zip_bytes())
    target = install_local_update()
    assert target == installer
    assert (installer / "UnityModManager.exe").is_file()


def test_fetch_umm_update_error_suggests_local_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))

    def fake_get(url, timeout=20.0, stream=False):
        if "Repository_beta_13" in url:
            return _FakeResponse(
                b'{"Releases":[{"Id":"UnityModManager","Version":"0.33.3",'
                b'"DownloadUrl":"https://www.dropbox.com/s/x/umm_update.zip?dl=1"}]}'
            )
        raise RuntimeError("dropbox unreachable")

    monkeypatch.setattr("adofai_publisher.umm._http_get", fake_get)
    try:
        fetch_umm_update("ghfast.top")
    except UmmError as exc:
        message = str(exc)
        assert "umm_update.zip" in message
        assert "本地更新包" in message
    else:
        raise AssertionError("expected UmmError")

