from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from adofai_publisher.config import Settings
from adofai_publisher.umm import (
    UMM_SOURCE_FILES,
    UmmError,
    get_umm_status,
    install_umm,
    installer_dir,
    uninstall_umm,
)

# 真实官方 UMM 0.33.3 安装包的路径由环境变量 UMM_0333_ARCHIVE 提供；
# 未设置或文件缺失时对应测试跳过，套件本身不依赖任何本机路径。
_UMM_0333_ARCHIVE_ENV = os.environ.get("UMM_0333_ARCHIVE", "")
REAL_UMM_0333_ARCHIVE = Path(_UMM_0333_ARCHIVE_ENV) if _UMM_0333_ARCHIVE_ENV else Path()


def _game_dir(tmp_path: Path) -> Path:
    game = tmp_path / "game"
    game.mkdir()
    (game / "A Dance of Fire and Ice.exe").write_bytes(b"MZ")
    managed = game / "A Dance of Fire and Ice_Data" / "Managed"
    managed.mkdir(parents=True)
    (managed / "Assembly-CSharp.dll").write_bytes(b"asm")
    (managed / "System.Xml.dll").write_bytes(b"system-xml")
    (managed / "Other.dll").write_bytes(b"keep-me")
    return game


# 官方 UMM 0.33.3 安装包 UnityModManagerConfig.xml 的 ADOFAI GameInfo（摘录，
# 字段顺序与真实文件一致）。官方运行库只认 <ModsDirectory>。
INSTALLER_CATALOG = """<?xml version="1.0" encoding="utf-8"?>
<Config xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <GameInfo Name="A Dance of Fire and Ice">
    <Folder>ADOFAI</Folder>
    <ModsDirectory>Mods</ModsDirectory>
    <ModInfo>Info.json</ModInfo>
    <GameExe>A Dance of Fire and Ice.exe</GameExe>
    <EntryPoint>[UnityEngine.CoreModule.dll]UnityEngine.MonoBehaviour.cctor:Before</EntryPoint>
    <StartingPoint>[Assembly-CSharp.dll]ADOStartup.Startup:Before</StartingPoint>
    <UIStartingPoint>[Assembly-CSharp.dll]ADOStartup.Startup:After</UIStartingPoint>
    <MinimalManagerVersion>0.22.14</MinimalManagerVersion>
    <Comment>Required minimum game version 2.7.0</Comment>
  </GameInfo>
  <GameInfo Name="Another Game">
    <EntryPoint>[Assembly-CSharp.dll]Other.Game:Before</EntryPoint>
  </GameInfo>
</Config>
"""

# Thunderstore UMM_ADOFAI（0.32.4 fork）归一化后的运行时 Config.xml：用
# RelativeModsDirectory，默认 ../Mods 会落到 Managed\Mods，安装时必须改写。
FORK_RUNTIME_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<Config xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" Name="A Dance of Fire and Ice">
  <Folder>A Dance of Fire and Ice</Folder>
  <RelativeModsDirectory>../Mods</RelativeModsDirectory>
  <ModInfo>Info.json</ModInfo>
  <GameExe>A Dance of Fire and Ice.exe</GameExe>
  <EntryPoint>[UnityEngine.CoreModule.dll]UnityEngine.Application.cctor:Before</EntryPoint>
  <StartingPoint>[Assembly-CSharp.dll]ADOStartup.Startup:Before</StartingPoint>
  <UIStartingPoint>[Assembly-CSharp.dll]ADOStartup.Startup:After</UIStartingPoint>
</Config>
"""


def _populate_installer(installer: Path) -> Path:
    """在指定目录放一套官方 0.33.x 风格的假 UMM 安装器文件。

    根目录带 UnityModManager.exe 与 UnityModManagerConfig.xml，因此按官方
    布局安装（根目录不复制本体）；0Harmony 根目录与 Harmony\\2.2 内容不同，
    用来验证取的是根目录那份（与 UMM 自带安装器一致）。
    """
    installer.mkdir(parents=True, exist_ok=True)
    for name in UMM_SOURCE_FILES:
        if name == "UnityModManagerConfig.xml":
            (installer / name).write_text(INSTALLER_CATALOG, encoding="utf-8")
        elif name == "0Harmony.dll":
            (installer / name).write_bytes(b"harmony-root")
        else:
            (installer / name).write_bytes(name.encode("utf-8"))
    harmony = installer / "Harmony" / "2.2"
    harmony.mkdir(parents=True, exist_ok=True)
    (harmony / "0Harmony.dll").write_bytes(b"harmony-2.2")
    return installer


def _populate_fork_installer(installer: Path) -> Path:
    """Thunderstore UMM_ADOFAI（0.32.4 fork）归一化后的安装器目录。

    没有 UnityModManager.exe / UnityModManagerConfig.xml，运行时配置用 Rel；
    运行库内容里含 RelativeModsDirectory 字符串（真实 fork DLL 也含）。
    """
    installer.mkdir(parents=True, exist_ok=True)
    (installer / "UnityModManager.dll").write_bytes(
        b"fork-manager RelativeModsDirectory"
    )
    (installer / "UnityModManager.xml").write_bytes(b"fork-xml")
    (installer / "0Harmony.dll").write_bytes(b"harmony-fork")
    (installer / "dnlib.dll").write_bytes(b"dnlib-fork")
    (installer / "Ionic.Zip.dll").write_bytes(b"ionic-fork")
    (installer / "Newtonsoft.Json.dll").write_bytes(b"newtonsoft-fork")
    (installer / "System.Xml.dll").write_bytes(b"system-xml-fork")
    (installer / "Config.xml").write_text(FORK_RUNTIME_CONFIG, encoding="utf-8")
    (installer / "winhttp_x64.dll").write_bytes(b"winhttp-fork-x64")
    (installer / "winhttp_x86.dll").write_bytes(b"winhttp-fork-x86")
    return installer


def _installer_dir(tmp_path: Path) -> Path:
    return _populate_installer(tmp_path / "installer")


def _settings(tmp_path: Path, game: Path) -> Settings:
    settings = Settings(tmp_path / "settings.json")
    settings.set("umm_game_dir", str(game))
    settings.save()
    return settings


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    installer = _installer_dir(tmp_path)
    monkeypatch.setattr(
        "adofai_publisher.umm.umm_installer_source",
        lambda *_args, **_kwargs: installer,
    )
    return game, installer, _settings(tmp_path, game)


@pytest.fixture
def fork_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    installer = _populate_fork_installer(tmp_path / "fork-installer")
    monkeypatch.setattr(
        "adofai_publisher.umm.umm_installer_source",
        lambda *_args, **_kwargs: installer,
    )
    return game, installer, _settings(tmp_path, game)


def _managed(game: Path) -> Path:
    return game / "A Dance of Fire and Ice_Data" / "Managed"


def _assert_official_mods_directory(config_text: str) -> None:
    """官方 0.33.x 风格：只写 ModsDirectory（相对游戏根目录），没有 Rel。"""
    root = ET.fromstring(config_text)
    assert root.find("RelativeModsDirectory") is None
    assert root.findtext("ModsDirectory") == "Mods"


def _assert_fork_mods_directory_points_to_game_root(
    config_text: str, game: Path
) -> None:
    """fork 风格：RelativeModsDirectory 从 UMM 程序集目录出发指到游戏根目录 Mods。"""
    root = ET.fromstring(config_text)
    assert root.find("ModsDirectory") is None
    relative = root.findtext("RelativeModsDirectory")
    assert relative
    manager = _managed(game) / "UnityModManager"
    assert Path(manager, relative).resolve() == (game / "Mods").resolve()


def test_install_umm_completes_doorstop_injection(environment):
    game, _installer, settings = environment
    assert install_umm(settings) == game

    # 1) doorstop 代理 DLL 必须落在游戏根目录，且按 64 位游戏取 x64 版本。
    assert (game / "winhttp.dll").read_bytes() == b"winhttp_x64.dll"

    # 2) doorstop_config.ini 指向绝对路径，enabled=true。
    ini = (game / "doorstop_config.ini").read_text(encoding="utf-8")
    target = (_managed(game) / "UnityModManager" / "UnityModManager.dll").resolve()
    lines = [line.strip() for line in ini.splitlines()]
    assert "[General]" in lines
    assert "enabled = true" in lines
    assert f"target_assembly = {target}" in lines
    assert "[UnityMono]" in lines
    assert target.is_file()

    # 3) 运行库与运行时配置进入 Managed\UnityModManager。
    manager = _managed(game) / "UnityModManager"
    assert (manager / "UnityModManager.dll").read_bytes() == b"UnityModManager.dll"
    assert (manager / "dnlib.dll").is_file()
    assert (manager / "UnityModManager.xml").is_file()
    # 官方安装器在游戏自带 System.Xml.dll 时跳过，避免版本冲突。
    assert not (manager / "System.Xml.dll").exists()
    # 官方 0.33.x 布局不带 Ionic.Zip / Newtonsoft.Json。
    assert not (manager / "Ionic.Zip.dll").exists()
    assert not (manager / "Newtonsoft.Json.dll").exists()
    # Harmony 优先使用安装器根目录那份（与 UMM 自带安装器一致）。
    assert (manager / "0Harmony.dll").read_bytes() == b"harmony-root"
    config = (manager / "Config.xml").read_text(encoding="utf-8")
    assert "A Dance of Fire and Ice" in config
    _assert_official_mods_directory(config)
    assert "<Folder>ADOFAI</Folder>" in config
    assert "<ModInfo>Info.json</ModInfo>" in config
    assert "<MinimalManagerVersion>0.22.14</MinimalManagerVersion>" in config
    assert "<Comment>Required minimum game version 2.7.0</Comment>" in config
    assert "[UnityEngine.CoreModule.dll]UnityEngine.MonoBehaviour.cctor:Before" in config
    assert "[Assembly-CSharp.dll]ADOStartup.Startup:Before" in config
    assert "[Assembly-CSharp.dll]ADOStartup.Startup:After" in config

    # 4) 官方布局：根目录不放 UMM 本体（exe / dll / 配置目录副本）。
    for name in (
        "UnityModManager.exe",
        "UnityModManager.dll",
        "UnityModManager.xml",
        "UnityModManagerConfig.xml",
        "0Harmony.dll",
        "dnlib.dll",
        "winhttp_x64.dll",
        "winhttp_x86.dll",
    ):
        assert not (game / name).exists(), name
    assert (game / "Mods").is_dir()


def test_install_uses_installer_game_info(environment):
    _game, installer, settings = environment
    (installer / "UnityModManagerConfig.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<Config>
  <GameInfo Name="A Dance of Fire and Ice">
    <EntryPoint>[Assembly-CSharp.dll]Custom.Hook:Before</EntryPoint>
    <StartingPoint>[Assembly-CSharp.dll]Custom.Start:After</StartingPoint>
    <UIStartingPoint>[Assembly-CSharp.dll]Custom.UI:After</UIStartingPoint>
    <SessionStartPoint>[Assembly-CSharp.dll]Custom.Session:Before</SessionStartPoint>
  </GameInfo>
</Config>
""",
        encoding="utf-8",
    )

    install_umm(settings)

    config = (
        _managed(_game) / "UnityModManager" / "Config.xml"
    ).read_text(encoding="utf-8")
    assert "Custom.Hook:Before" in config
    assert "Custom.Start:After" in config
    assert "Custom.UI:After" in config
    assert "Custom.Session:Before" in config
    assert "ADOStartup" not in config
    # 安装器 GameInfo 里没写目录字段时按运行库风格补 ModsDirectory。
    _assert_official_mods_directory(config)
    assert "<ModInfo>Info.json</ModInfo>" in config


def test_install_prefers_installer_runtime_config(environment):
    game, installer, settings = environment
    direct = (
        '<Config Name="A Dance of Fire and Ice">'
        "<EntryPoint>[Assembly-CSharp.dll]Direct.Hook:Before</EntryPoint>"
        "<StartingPoint>[Assembly-CSharp.dll]Direct.Start:Before</StartingPoint>"
        "<UIStartingPoint>[Assembly-CSharp.dll]Direct.UI:After</UIStartingPoint>"
        "</Config>"
    )
    (installer / "Config.xml").write_text(direct, encoding="utf-8")

    install_umm(settings)

    manager_config = _managed(game) / "UnityModManager" / "Config.xml"
    config = manager_config.read_text(encoding="utf-8")
    # 安装器配置里的挂点原样保留，mod 目录按官方风格补 ModsDirectory。
    assert "Direct.Hook:Before" in config
    assert "Direct.Start:Before" in config
    assert "Direct.UI:After" in config
    _assert_official_mods_directory(config)
    assert "<ModInfo>Info.json</ModInfo>" in config


def test_install_falls_back_to_builtin_config_with_warning(environment, caplog):
    game, installer, settings = environment
    (installer / "UnityModManagerConfig.xml").unlink()

    with caplog.at_level(logging.WARNING, logger="adofai_publisher.umm"):
        install_umm(settings)

    config = (
        _managed(game) / "UnityModManager" / "Config.xml"
    ).read_text(encoding="utf-8")
    # 官方兜底模板：只认 ModsDirectory 的 0.33.x 运行库，不能写 Rel。
    assert "[UnityEngine.CoreModule.dll]UnityEngine.MonoBehaviour.cctor:Before" in config
    assert "ADOStartup.Startup" in config
    _assert_official_mods_directory(config)
    assert "<ModInfo>Info.json</ModInfo>" in config
    assert "<MinimalManagerVersion>0.22.14</MinimalManagerVersion>" in config
    assert "<Comment>Required minimum game version 2.7.0</Comment>" in config
    assert "内置合成配置" in caplog.text
    assert "需真机确认" in caplog.text


def test_install_uses_fork_builtin_config_when_package_has_no_adofai_config(
    tmp_path, monkeypatch, caplog
):
    """没有任何 ADOFAI 配置时，DLL 里的 Rel 字段决定用 fork 兜底模板。"""
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    installer = tmp_path / "bare-fork-installer"
    installer.mkdir()
    (installer / "UnityModManager.dll").write_bytes(
        b"fork-manager RelativeModsDirectory"
    )
    (installer / "winhttp_x64.dll").write_bytes(b"proxy")
    monkeypatch.setattr(
        "adofai_publisher.umm.umm_installer_source",
        lambda *_args, **_kwargs: installer,
    )
    settings = _settings(tmp_path, game)

    with caplog.at_level(logging.WARNING, logger="adofai_publisher.umm"):
        install_umm(settings)

    config = (
        _managed(game) / "UnityModManager" / "Config.xml"
    ).read_text(encoding="utf-8")
    _assert_fork_mods_directory_points_to_game_root(config, game)
    assert "[UnityEngine.CoreModule.dll]UnityEngine.Application.cctor:Before" in config
    # 内置模板里的占位符必须被替换掉，不能原样落盘。
    assert "{relative_mods_directory}" not in config


@pytest.mark.parametrize(
    ("version", "official"),
    [
        ((0, 32, 4), False),
        ((0, 33, 3, 0), True),
    ],
)
def test_builtin_config_style_falls_back_to_dll_version(
    tmp_path, monkeypatch, version, official
):
    """配置与 DLL 内容都判断不出风格时，按 DLL FileVersion 回退。"""
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    installer = tmp_path / "versioned-installer"
    installer.mkdir()
    (installer / "UnityModManager.dll").write_bytes(b"manager-without-marker")
    (installer / "winhttp_x64.dll").write_bytes(b"proxy")
    monkeypatch.setattr(
        "adofai_publisher.umm.umm_installer_source",
        lambda *_args, **_kwargs: installer,
    )
    monkeypatch.setattr(
        "adofai_publisher.umm._manager_dll_version",
        lambda _path: version,
    )
    settings = _settings(tmp_path, game)

    install_umm(settings)

    config = (
        _managed(game) / "UnityModManager" / "Config.xml"
    ).read_text(encoding="utf-8")
    if official:
        _assert_official_mods_directory(config)
    else:
        _assert_fork_mods_directory_points_to_game_root(config, game)


def test_install_umm_updates_existing_config_and_keeps_fields(environment):
    game, _installer, settings = environment
    manager = _managed(game) / "UnityModManager"
    manager.mkdir(parents=True)
    existing = (
        '<Config Name="A Dance of Fire and Ice">'
        "<EntryPoint>[UnityEngine.CoreModule.dll]UnityEngine.Application.cctor:Before</EntryPoint>"
        "<StartingPoint>[Assembly-CSharp.dll]ADOStartup.Startup:Before</StartingPoint>"
        "<UIStartingPoint>[Assembly-CSharp.dll]ADOStartup.Startup:After</UIStartingPoint>"
        "<TextureReplacingPoint>[Assembly-CSharp.dll]ADOBase.SomePoint</TextureReplacingPoint>"
        "<ModsDirectory>Mods</ModsDirectory>"
        "</Config>"
    )
    (manager / "Config.xml").write_text(existing, encoding="utf-8")

    install_umm(settings)

    config = (manager / "Config.xml").read_text(encoding="utf-8")
    # 已有官方配置里的挂点与额外字段不能被覆盖掉。
    assert "[UnityEngine.CoreModule.dll]UnityEngine.Application.cctor:Before" in config
    assert "[Assembly-CSharp.dll]ADOStartup.Startup:Before" in config
    assert "[Assembly-CSharp.dll]ADOStartup.Startup:After" in config
    assert "<TextureReplacingPoint>[Assembly-CSharp.dll]ADOBase.SomePoint" in config
    # 官方风格：保留 ModsDirectory，不强行加 Rel；缺失的 ModInfo 补上。
    assert "<ModsDirectory>Mods</ModsDirectory>" in config
    assert "<RelativeModsDirectory>" not in config
    assert "<ModInfo>Info.json</ModInfo>" in config


def test_install_umm_keeps_existing_mod_info_value(environment):
    """已有配置里的自定义 ModInfo 不是缺失字段，保留原值。"""
    game, _installer, settings = environment
    manager = _managed(game) / "UnityModManager"
    manager.mkdir(parents=True)
    (manager / "Config.xml").write_text(
        '<Config Name="A Dance of Fire and Ice">'
        "<ModInfo>Custom.json</ModInfo>"
        "</Config>",
        encoding="utf-8",
    )

    install_umm(settings)

    config = (manager / "Config.xml").read_text(encoding="utf-8")
    assert "<ModInfo>Custom.json</ModInfo>" in config
    _assert_official_mods_directory(config)


def test_fork_install_rewrites_default_relative_mods_directory(fork_environment):
    """fork 默认的 ../Mods 会落到 Managed\\Mods，必须改写成回游戏根目录的路径。"""
    game, _installer, settings = fork_environment
    manager = _managed(game) / "UnityModManager"
    manager.mkdir(parents=True)
    (manager / "Config.xml").write_text(FORK_RUNTIME_CONFIG, encoding="utf-8")

    install_umm(settings)

    config = (manager / "Config.xml").read_text(encoding="utf-8")
    assert "<RelativeModsDirectory>../Mods</RelativeModsDirectory>" not in config
    _assert_fork_mods_directory_points_to_game_root(config, game)
    # 启动挂点不受影响。
    assert "[Assembly-CSharp.dll]ADOStartup.Startup:Before" in config


def test_fork_install_keeps_legacy_root_layout(fork_environment):
    """fork 包安装保持旧布局：根目录复制本体，运行库带 Ionic.Zip / Newtonsoft.Json。"""
    game, installer, settings = fork_environment

    install_umm(settings)

    # 根目录仍是旧布局：安装器文件被复制过去（历史行为，不影响运行）。
    assert (game / "UnityModManager.dll").read_bytes() == (
        installer / "UnityModManager.dll"
    ).read_bytes()
    assert (game / "0Harmony.dll").read_bytes() == b"harmony-fork"
    assert (game / "Ionic.Zip.dll").read_bytes() == b"ionic-fork"
    assert (game / "winhttp.dll").read_bytes() == b"winhttp-fork-x64"

    manager = _managed(game) / "UnityModManager"
    assert (manager / "Ionic.Zip.dll").read_bytes() == b"ionic-fork"
    assert (manager / "Newtonsoft.Json.dll").read_bytes() == b"newtonsoft-fork"
    assert (manager / "0Harmony.dll").read_bytes() == b"harmony-fork"
    # 游戏 Managed 自带 System.Xml.dll 时仍跳过。
    assert not (manager / "System.Xml.dll").exists()
    config = (manager / "Config.xml").read_text(encoding="utf-8")
    _assert_fork_mods_directory_points_to_game_root(config, game)


def test_official_install_converts_existing_fork_config(environment):
    """旧安装留下的 fork 配置（只有 Rel）装官方 0.33.x 时必须转成 ModsDirectory。

    这是曾遇到的崩溃：0.33.3 读不到 ModsDirectory 时
    Path.Combine(cwd, null) 抛 ArgumentNullException，游戏启动即崩。
    """
    game, _installer, settings = environment
    manager = _managed(game) / "UnityModManager"
    manager.mkdir(parents=True)
    (manager / "Config.xml").write_text(
        '<Config Name="A Dance of Fire and Ice">'
        "<EntryPoint>[UnityEngine.CoreModule.dll]UnityEngine.MonoBehaviour.cctor:Before</EntryPoint>"
        "<StartingPoint>[Assembly-CSharp.dll]ADOStartup.Startup:Before</StartingPoint>"
        "<UIStartingPoint>[Assembly-CSharp.dll]ADOStartup.Startup:After</UIStartingPoint>"
        "<RelativeModsDirectory>..\\..\\..\\Mods</RelativeModsDirectory>"
        "<TextureReplacingPoint>[Assembly-CSharp.dll]ADOBase.SomePoint</TextureReplacingPoint>"
        "</Config>",
        encoding="utf-8",
    )

    install_umm(settings)

    config = (manager / "Config.xml").read_text(encoding="utf-8")
    _assert_official_mods_directory(config)
    # 挂点与额外字段保留下来。
    assert "[UnityEngine.CoreModule.dll]UnityEngine.MonoBehaviour.cctor:Before" in config
    assert "<TextureReplacingPoint>[Assembly-CSharp.dll]ADOBase.SomePoint" in config
    assert "<ModInfo>Info.json</ModInfo>" in config


def test_official_install_keeps_existing_mods_directory_value(environment):
    """已有官方风格配置（含旧版写的绝对 ModsDirectory）原样保留，不改写成 Rel。"""
    game, _installer, settings = environment
    manager = _managed(game) / "UnityModManager"
    manager.mkdir(parents=True)
    (manager / "Config.xml").write_text(
        '<Config Name="A Dance of Fire and Ice">'
        "<EntryPoint>[UnityEngine.CoreModule.dll]UnityEngine.Application.cctor:Before</EntryPoint>"
        "<StartingPoint>[Assembly-CSharp.dll]ADOStartup.Startup:Before</StartingPoint>"
        "<UIStartingPoint>[Assembly-CSharp.dll]ADOStartup.Startup:After</UIStartingPoint>"
        f"<ModsDirectory>{game / 'Mods'}</ModsDirectory>"
        "</Config>",
        encoding="utf-8",
    )

    install_umm(settings)

    config = (manager / "Config.xml").read_text(encoding="utf-8")
    # 官方运行库能解析绝对路径（Path.Combine 遇到绝对 path2 直接返回它），保留原值。
    assert f"<ModsDirectory>{game / 'Mods'}</ModsDirectory>" in config
    assert "<RelativeModsDirectory>" not in config
    assert "<ModInfo>Info.json</ModInfo>" in config


def test_install_umm_cleans_empty_stray_managed_mods(environment):
    game, _installer, settings = environment
    stray = _managed(game) / "Mods"
    stray.mkdir(parents=True)

    install_umm(settings)

    assert not stray.exists()
    # 游戏根目录的 Mods 才是 mod 的家，不能误删。
    assert (game / "Mods").is_dir()


def test_install_umm_keeps_non_empty_stray_managed_mods(environment):
    game, _installer, settings = environment
    stray = _managed(game) / "Mods"
    stray.mkdir(parents=True)
    (stray / "keep.txt").write_text("user data", encoding="utf-8")

    install_umm(settings)

    assert (stray / "keep.txt").is_file()


def test_uninstall_umm_cleans_empty_stray_managed_mods(environment):
    game, _installer, settings = environment
    install_umm(settings)
    stray = _managed(game) / "Mods"
    stray.mkdir()

    uninstall_umm(settings)

    assert not stray.exists()


def test_install_umm_picks_x86_proxy_for_32bit_game(environment):
    game, _installer, settings = environment
    pe = bytearray(0x100)
    pe[0:2] = b"MZ"
    pe[0x3C:0x40] = (0x80).to_bytes(4, "little")
    pe[0x80:0x84] = b"PE\0\0"
    pe[0x84:0x86] = (0x014C).to_bytes(2, "little")
    (game / "A Dance of Fire and Ice.exe").write_bytes(bytes(pe))

    install_umm(settings)

    assert (game / "winhttp.dll").read_bytes() == b"winhttp_x86.dll"


def test_install_umm_falls_back_to_available_proxy(environment):
    game, installer, settings = environment
    (installer / "winhttp_x64.dll").unlink()

    install_umm(settings)

    assert (game / "winhttp.dll").read_bytes() == b"winhttp_x86.dll"


def test_install_umm_harmony_falls_back_to_2_2_folder(environment):
    """根目录没有 0Harmony.dll 时才退到 Harmony\\2.2 子目录。"""
    game, installer, settings = environment
    (installer / "0Harmony.dll").unlink()

    install_umm(settings)

    manager = _managed(game) / "UnityModManager"
    assert (manager / "0Harmony.dll").read_bytes() == b"harmony-2.2"


def test_install_umm_cleans_legacy_root_copies(environment):
    """旧版复制到游戏根目录的本体文件被清掉，用户自己的文件不动。"""
    game, _installer, settings = environment
    legacy = (
        "UnityModManager.exe",
        "UnityModManager.dll",
        "UnityModManager.xml",
        "UnityModManagerConfig.xml",
        "0Harmony.dll",
        "dnlib.dll",
        "Ionic.Zip.dll",
        "Newtonsoft.Json.dll",
        "System.Xml.dll",
        "winhttp_x64.dll",
        "winhttp_x86.dll",
    )
    for name in legacy:
        (game / name).write_bytes(b"legacy")
    (game / "user-notes.txt").write_text("keep me", encoding="utf-8")

    install_umm(settings)

    for name in legacy:
        assert not (game / name).exists(), name
    assert (game / "user-notes.txt").read_text(encoding="utf-8") == "keep me"
    assert (game / "winhttp.dll").is_file()
    assert (game / "doorstop_config.ini").is_file()


def test_install_umm_requires_manager_dll(environment):
    game, installer, settings = environment
    (installer / "UnityModManager.dll").unlink()

    with pytest.raises(UmmError, match="UnityModManager.dll"):
        install_umm(settings)

    # 缺必需文件时不应留下半套根目录文件。
    assert not (game / "UnityModManager.exe").exists()


def test_install_umm_requires_managed_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = tmp_path / "game"
    game.mkdir()
    (game / "A Dance of Fire and Ice.exe").write_bytes(b"MZ")
    installer = _installer_dir(tmp_path)
    monkeypatch.setattr(
        "adofai_publisher.umm.umm_installer_source",
        lambda *_args, **_kwargs: installer,
    )

    with pytest.raises(UmmError, match="Managed"):
        install_umm(_settings(tmp_path, game))


def test_status_distinguishes_copied_files_from_injection(environment):
    game, _installer, settings = environment

    assert get_umm_status(settings)["state"] == "none"
    assert get_umm_status(settings)["installed"] is False

    # 旧版安装的结果：只复制了 UMM 文件，没有 doorstop 注入。
    (game / "UnityModManager.exe").write_bytes(b"")
    partial = get_umm_status(settings)
    assert partial["installed"] is False
    assert partial["injected"] is False
    assert partial["manager_installed"] is False
    assert partial["state"] == "partial"
    assert "注入未完成" in partial["message"]

    install_umm(settings)
    installed = get_umm_status(settings)
    assert installed["installed"] is True
    assert installed["injected"] is True
    assert installed["doorstop_injected"] is True
    assert installed["assembly_injected"] is False
    assert installed["manager_installed"] is True
    assert installed["mode"] == "doorstop"
    assert "doorstop" in installed["message"]
    assert installed["state"] == "installed"

    # 注入文件缺失时回到中间状态，而不是仍显示已安装。
    (game / "doorstop_config.ini").unlink()
    broken = get_umm_status(settings)
    assert broken["installed"] is False
    assert broken["state"] == "partial"
    assert broken["manager_installed"] is True
    assert broken["injected"] is False
    assert broken["mode"] is None


def test_uninstall_umm_removes_injection_and_keeps_managed_files(environment):
    game, _installer, settings = environment

    install_umm(settings)
    uninstall_umm(settings)

    assert not (game / "winhttp.dll").exists()
    assert not (game / "doorstop_config.ini").exists()
    assert not (game / "UnityModManager.dll").exists()
    assert not (_managed(game) / "UnityModManager").exists()
    # Managed 下其它文件、Mods 与游戏本体都不动。
    assert (_managed(game) / "Assembly-CSharp.dll").is_file()
    assert (_managed(game) / "Other.dll").is_file()
    assert (game / "Mods").is_dir()
    assert (game / "A Dance of Fire and Ice.exe").is_file()
    assert get_umm_status(settings)["state"] == "none"


def test_install_umm_auto_downloads_missing_installer(tmp_path, monkeypatch):
    """新机器没有安装器时，一键安装应先自动下载再继续安装。"""
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    settings = _settings(tmp_path, game)
    settings.set("umm_mirror", "gh-proxy.com")
    settings.save()

    calls: list[str | None] = []
    progress_events: list[tuple[str, int]] = []
    marker_cancel = lambda: False  # noqa: E731

    def fake_fetch(mirror=None, *, progress=None, cancel=None):
        calls.append(mirror)
        assert callable(progress)
        assert cancel is marker_cancel
        progress("正在读取官方版本信息…", 0)
        target = _populate_installer(installer_dir())
        progress("正在下载 UMM v1.0.0…", 40)
        return "1.0.0", target

    monkeypatch.setattr("adofai_publisher.umm.fetch_umm_update", fake_fetch)

    result = install_umm(
        settings,
        progress=lambda message, percent: progress_events.append((message, percent)),
        cancel=marker_cancel,
    )

    assert result == game
    assert calls == ["gh-proxy.com"]
    messages = [message for message, _percent in progress_events]
    assert messages[0] == "正在下载 UMM 安装包…"
    assert messages[-1] == "正在安装 UMM…"
    # 下载后确实继续完成了安装。
    assert (game / "winhttp.dll").is_file()
    assert (game / "doorstop_config.ini").is_file()
    assert (_managed(game) / "UnityModManager" / "UnityModManager.dll").is_file()


def test_install_umm_falls_back_to_thunderstore(tmp_path, monkeypatch):
    """官方源（Dropbox）失败后自动改用 Thunderstore，并继续完成安装。"""
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    settings = _settings(tmp_path, game)

    def failing_fetch(mirror=None, *, progress=None, cancel=None):
        progress("正在读取官方版本信息…", 0)
        raise UmmError("Dropbox 拒绝连接（WinError 10061）")

    thunderstore_calls: list[str] = []

    def fake_thunderstore(*, progress=None, cancel=None):
        thunderstore_calls.append("called")
        progress("正在从 Thunderstore 读取 UMM 版本信息…", 0)
        progress("正在从 Thunderstore 下载 UMM v1.1.0…", 20)
        return "1.1.0 (Thunderstore)", _populate_installer(installer_dir())

    monkeypatch.setattr("adofai_publisher.umm.fetch_umm_update", failing_fetch)
    monkeypatch.setattr(
        "adofai_publisher.umm.fetch_umm_thunderstore", fake_thunderstore
    )

    progress_events: list[tuple[str, int]] = []
    assert install_umm(
        settings,
        progress=lambda message, percent: progress_events.append((message, percent)),
    ) == game

    assert thunderstore_calls == ["called"]
    messages = [message for message, _percent in progress_events]
    assert messages[0] == "正在下载 UMM 安装包…"
    assert any("官方源下载失败" in message and "Thunderstore" in message for message in messages)
    assert any("从 Thunderstore 下载" in message for message in messages)
    assert messages[-1] == "正在安装 UMM…"
    # 回退下载的安装器确实被安装进了游戏目录。
    assert (game / "winhttp.dll").is_file()
    assert (game / "doorstop_config.ini").is_file()
    assert (_managed(game) / "UnityModManager" / "UnityModManager.dll").is_file()


def test_install_umm_thunderstore_cancel_does_not_install(tmp_path, monkeypatch):
    from adofai_publisher.umm import UmmCancelled

    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    settings = _settings(tmp_path, game)

    def failing_fetch(mirror=None, *, progress=None, cancel=None):
        raise UmmError("Dropbox 拒绝连接")

    def cancelling_thunderstore(*, progress=None, cancel=None):
        raise UmmCancelled("已取消在线更新")

    monkeypatch.setattr("adofai_publisher.umm.fetch_umm_update", failing_fetch)
    monkeypatch.setattr(
        "adofai_publisher.umm.fetch_umm_thunderstore", cancelling_thunderstore
    )

    with pytest.raises(UmmError, match="已取消下载 UMM 安装包"):
        install_umm(settings)

    assert not (game / "winhttp.dll").exists()
    assert not (game / "doorstop_config.ini").exists()


def test_install_umm_auto_download_failure_is_readable(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    settings = _settings(tmp_path, game)

    def failing_fetch(mirror=None, *, progress=None, cancel=None):
        raise UmmError("无法读取官方版本信息：网络不可达")

    def failing_thunderstore(*, progress=None, cancel=None):
        raise UmmError("Thunderstore 不可达")

    monkeypatch.setattr("adofai_publisher.umm.fetch_umm_update", failing_fetch)
    monkeypatch.setattr(
        "adofai_publisher.umm.fetch_umm_thunderstore", failing_thunderstore
    )

    with pytest.raises(UmmError) as info:
        install_umm(settings)

    message = str(info.value)
    # 不能静默失败：要说清是下载失败、两个源各自的原因和手动兜底路径。
    assert message.startswith("下载 UMM 安装包失败")
    assert "官方源失败原因" in message
    assert "网络不可达" in message
    assert "Thunderstore 失败原因" in message
    assert "Thunderstore 不可达" in message
    assert "umm_update.zip" in message
    assert not (game / "winhttp.dll").exists()
    assert not (game / "doorstop_config.ini").exists()


def test_install_umm_skips_download_when_installer_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    _populate_installer(installer_dir())
    settings = _settings(tmp_path, game)

    def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("已有安装器时不应触发下载")

    monkeypatch.setattr("adofai_publisher.umm.fetch_umm_update", unexpected_fetch)

    progress_events: list[str] = []
    assert install_umm(
        settings,
        progress=lambda message, _percent: progress_events.append(message),
    ) == game
    assert progress_events == ["正在安装 UMM…"]
    assert (game / "winhttp.dll").is_file()


def test_install_umm_auto_download_cancel_does_not_install(tmp_path, monkeypatch):
    from adofai_publisher.umm import UmmCancelled

    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    settings = _settings(tmp_path, game)

    def cancelling_fetch(mirror=None, *, progress=None, cancel=None):
        raise UmmCancelled("已取消在线更新")

    monkeypatch.setattr("adofai_publisher.umm.fetch_umm_update", cancelling_fetch)

    with pytest.raises(UmmError, match="已取消下载 UMM 安装包"):
        install_umm(settings)

    assert not (game / "winhttp.dll").exists()
    assert not (game / "doorstop_config.ini").exists()


def test_install_umm_redownloads_incomplete_managed_installer(tmp_path, monkeypatch):
    """数据目录里的安装器被清空（只剩目录）时，也应按缺失处理并重新下载。"""
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    installer_dir().mkdir(parents=True)
    settings = _settings(tmp_path, game)

    calls: list[str | None] = []

    def fake_fetch(mirror=None, *, progress=None, cancel=None):
        calls.append(mirror)
        return "1.0.0", _populate_installer(installer_dir())

    monkeypatch.setattr("adofai_publisher.umm.fetch_umm_update", fake_fetch)

    assert install_umm(settings) == game
    assert len(calls) == 1
    assert (game / "winhttp.dll").is_file()


def test_umm_installer_needs_download_semantics(tmp_path, monkeypatch):
    from adofai_publisher.umm import (
        umm_installer_needs_download,
        umm_installer_ready,
    )

    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    settings = _settings(tmp_path, _game_dir(tmp_path))

    # 没有安装器：需要下载。
    assert umm_installer_ready(None) is False
    assert umm_installer_needs_download(settings) is True

    # 数据目录里的安装器完整：不需要下载。
    _populate_installer(installer_dir())
    assert umm_installer_ready(installer_dir()) is True
    assert umm_installer_needs_download(settings) is False

    # 用户自己指定但缺文件的目录不自动替换，交给安装阶段报错。
    configured = tmp_path / "configured"
    configured.mkdir()
    (configured / "UnityModManager.exe").write_bytes(b"")
    settings.set("umm_installer_dir", str(configured))
    assert umm_installer_ready(configured) is False
    assert umm_installer_needs_download(settings) is False


def test_install_umm_reports_when_download_creates_no_installer(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    settings = _settings(tmp_path, game)

    def empty_fetch(mirror=None, *, progress=None, cancel=None):
        return "1.0.0", tmp_path / "nowhere"

    monkeypatch.setattr("adofai_publisher.umm.fetch_umm_update", empty_fetch)

    with pytest.raises(UmmError, match="仍未找到安装器目录"):
        install_umm(settings)

    assert not (game / "winhttp.dll").exists()


# ---------------------------------------------------------------------------
# 真实官方 UMM 0.33.3 安装包端到端（不碰真实 Steam 目录；包不在本机时跳过）。
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not REAL_UMM_0333_ARCHIVE.is_file(),
    reason="本机没有真实 UMM 0.33.3 安装包：请设置 UMM_0333_ARCHIVE 后运行",
)
def test_install_real_umm_0333_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    package = tmp_path / "package"
    with zipfile.ZipFile(REAL_UMM_0333_ARCHIVE) as zf:
        zf.extractall(package)
    installer = package / "UnityModManagerInstaller"
    assert installer.is_dir()
    assert (installer / "UnityModManager.dll").is_file()
    monkeypatch.setattr(
        "adofai_publisher.umm.umm_installer_source",
        lambda *_args, **_kwargs: installer,
    )
    game = _game_dir(tmp_path)
    settings = _settings(tmp_path, game)

    assert install_umm(settings) == game

    # 运行时配置跟随 0.33.3 的 ADOFAI GameInfo：ModsDirectory=Mods，绝不写 Rel。
    config = (
        _managed(game) / "UnityModManager" / "Config.xml"
    ).read_text(encoding="utf-8")
    _assert_official_mods_directory(config)
    assert "<Folder>ADOFAI</Folder>" in config
    assert "<ModInfo>Info.json</ModInfo>" in config
    assert "<MinimalManagerVersion>0.22.14</MinimalManagerVersion>" in config
    assert "<Comment>Required minimum game version 2.7.0</Comment>" in config
    assert "[UnityEngine.CoreModule.dll]UnityEngine.MonoBehaviour.cctor:Before" in config
    assert "[Assembly-CSharp.dll]ADOStartup.Startup:Before" in config
    assert "[Assembly-CSharp.dll]ADOStartup.Startup:After" in config

    # 文件布局与 UMM 自带安装器一致：根目录只有 doorstop，本体在 Managed 下。
    manager = _managed(game) / "UnityModManager"
    assert (game / "winhttp.dll").is_file()
    assert (game / "doorstop_config.ini").is_file()
    for name in UMM_SOURCE_FILES:
        assert not (game / name).exists(), name
    for name in (
        "UnityModManager.dll",
        "UnityModManager.xml",
        "dnlib.dll",
        "0Harmony.dll",
        "Config.xml",
    ):
        assert (manager / name).is_file(), name
    # 0Harmony 取安装包根目录那份（2.2.x 完整版，不是 Harmony\2.2 子目录）。
    assert (manager / "0Harmony.dll").read_bytes() == (
        installer / "0Harmony.dll"
    ).read_bytes()
    # 官方 0.33.3 不放 Ionic.Zip / Newtonsoft.Json / System.Xml。
    for name in ("Ionic.Zip.dll", "Newtonsoft.Json.dll", "System.Xml.dll"):
        assert not (manager / name).exists(), name


# ---------------------------------------------------------------------------
# Assembly 注入模式（UMM 安装器不写 winhttp.dll / doorstop_config.ini，
# 直接给入口程序集打补丁）：
#   tests/test_umm_install.py 的其余用例都走 doorstop。
# ---------------------------------------------------------------------------

ENTRY_MARKER = b"UnityModManagerNet.Injection.UnityModManagerStarter"
ORIGINAL_ASSEMBLY = b"MZ-original-assembly-bytes"
PATCHED_ASSEMBLY = ORIGINAL_ASSEMBLY + b"\0" + ENTRY_MARKER + b"\0"


def _write_assembly_manager(game: Path, starting_point: str) -> Path:
    """构造 UMM 运行库目录与运行时 Config.xml（Assembly 模式只留这些）。"""
    manager = _managed(game) / "UnityModManager"
    manager.mkdir(parents=True, exist_ok=True)
    (manager / "UnityModManager.dll").write_bytes(b"manager")
    (manager / "Config.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<Config Name="A Dance of Fire and Ice">'
        f"<StartingPoint>{starting_point}</StartingPoint>"
        "<EntryPoint>[UnityEngine.CoreModule.dll]UnityEngine.Application.cctor:Before</EntryPoint>"
        "</Config>",
        encoding="utf-8",
    )
    return manager


def _install_assembly_mode(
    game: Path,
    *,
    backup: bool = True,
    starting_point: str = "[Assembly-CSharp.dll]ADOStartup.Startup:Before",
) -> Path:
    """把游戏目录造成「UMM 官方安装器 Assembly 注入完成」的样子。"""
    _write_assembly_manager(game, starting_point)
    entry = _managed(game) / "Assembly-CSharp.dll"
    entry.write_bytes(PATCHED_ASSEMBLY)
    if backup:
        entry.with_name(entry.name + ".original_").write_bytes(ORIGINAL_ASSEMBLY)
    return entry


@pytest.fixture
def assembly_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    installer = _installer_dir(tmp_path)
    monkeypatch.setattr(
        "adofai_publisher.umm.umm_installer_source",
        lambda *_args, **_kwargs: installer,
    )
    _install_assembly_mode(game)
    (game / "Mods").mkdir(exist_ok=True)
    return game, installer, _settings(tmp_path, game)


def test_status_recognizes_assembly_injection(assembly_environment):
    game, _installer, settings = assembly_environment

    status = get_umm_status(settings)

    assert status["installed"] is True
    assert status["injected"] is True
    assert status["assembly_injected"] is True
    assert status["doorstop_injected"] is False
    assert status["manager_installed"] is True
    assert status["mode"] == "assembly"
    assert status["state"] == "installed"
    assert status["message"] == "UMM 已安装，Assembly 注入完成"
    assert status["entry_assembly"] == str(_managed(game) / "Assembly-CSharp.dll")


def test_status_prefers_doorstop_when_both_injections_present(assembly_environment):
    game, _installer, settings = assembly_environment
    (game / "winhttp.dll").write_bytes(b"winhttp_x64.dll")
    (game / "doorstop_config.ini").write_text("[General]\nenabled = true\n", encoding="utf-8")

    status = get_umm_status(settings)

    assert status["installed"] is True
    assert status["doorstop_injected"] is True
    assert status["assembly_injected"] is True
    assert status["mode"] == "doorstop"
    assert status["message"] == "UMM 已安装，doorstop 注入完成"


def test_status_reads_entry_assembly_from_starting_point(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    _write_assembly_manager(
        game, "[CustomEntry.dll]Custom.Startup:Before"
    )
    (game / "A Dance of Fire and Ice_Data" / "Managed" / "CustomEntry.dll").write_bytes(
        PATCHED_ASSEMBLY
    )
    # 默认的 Assembly-CSharp.dll 保持原版，仍应识别出注入。
    status = get_umm_status(_settings(tmp_path, game))

    assert status["assembly_injected"] is True
    assert status["mode"] == "assembly"
    assert status["entry_assembly"].endswith("CustomEntry.dll")


def test_status_parses_config_with_bom(tmp_path, monkeypatch):
    """官方安装器写出的 Config.xml 可能带 UTF-8 BOM。"""
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    manager = _write_assembly_manager(game, "[CustomEntry.dll]Custom.Startup:Before")
    config = (manager / "Config.xml").read_text(encoding="utf-8")
    (manager / "Config.xml").write_bytes(b"\xef\xbb\xbf" + config.encode("utf-8"))
    (_managed(game) / "CustomEntry.dll").write_bytes(PATCHED_ASSEMBLY)

    status = get_umm_status(_settings(tmp_path, game))

    assert status["assembly_injected"] is True
    assert status["entry_assembly"].endswith("CustomEntry.dll")


def test_status_does_not_trust_original_backup_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    _write_assembly_manager(game, "[Assembly-CSharp.dll]ADOStartup.Startup:Before")
    entry = _managed(game) / "Assembly-CSharp.dll"
    entry.write_bytes(ORIGINAL_ASSEMBLY)
    # 只有备份、入口程序集没有补丁标记：不能算注入。
    entry.with_name(entry.name + ".original_").write_bytes(ORIGINAL_ASSEMBLY)
    status = get_umm_status(_settings(tmp_path, game))

    assert status["assembly_injected"] is False
    assert status["installed"] is False
    assert status["manager_installed"] is True
    assert status["state"] == "partial"
    assert "注入未完成" in status["message"]


def test_status_treats_unreadable_entry_as_not_injected(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    _write_assembly_manager(game, "[Assembly-CSharp.dll]ADOStartup.Startup:Before")
    entry = _managed(game) / "Assembly-CSharp.dll"
    entry.unlink()
    entry.mkdir()  # 读文件会抛 IsADirectoryError

    settings = _settings(tmp_path, game)
    with caplog.at_level(logging.WARNING, logger="adofai_publisher.umm"):
        status = get_umm_status(settings)

    assert status["assembly_injected"] is False
    assert status["installed"] is False
    assert "读取入口程序集失败" in caplog.text


def test_uninstall_restores_custom_entry_assembly(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    _write_assembly_manager(game, "[CustomEntry.dll]Custom.Startup:Before")
    custom = _managed(game) / "CustomEntry.dll"
    custom.write_bytes(PATCHED_ASSEMBLY)
    custom.with_name(custom.name + ".original_").write_bytes(ORIGINAL_ASSEMBLY)

    uninstall_umm(_settings(tmp_path, game))

    assert custom.read_bytes() == ORIGINAL_ASSEMBLY
    assert not custom.with_name(custom.name + ".original_").exists()
    # 没被注入的默认入口程序集不动。
    assert (_managed(game) / "Assembly-CSharp.dll").read_bytes() == b"asm"


def test_uninstall_restores_assembly_injection(assembly_environment):
    game, _installer, settings = assembly_environment
    entry = _managed(game) / "Assembly-CSharp.dll"
    backup = entry.with_name(entry.name + ".original_")

    uninstall_umm(settings)

    assert entry.read_bytes() == ORIGINAL_ASSEMBLY
    assert not backup.exists()
    assert not (_managed(game) / "UnityModManager").exists()
    assert not (game / "winhttp.dll").exists()
    assert not (game / "UnityModManager.exe").exists()
    # 其它游戏文件与 Mods 不受影响。
    assert (_managed(game) / "Other.dll").is_file()
    assert (game / "Mods").is_dir()
    assert get_umm_status(settings)["state"] == "none"


def test_uninstall_refuses_assembly_injection_without_backup(assembly_environment):
    game, _installer, settings = assembly_environment
    entry = _managed(game) / "Assembly-CSharp.dll"
    entry.with_name(entry.name + ".original_").unlink()

    with pytest.raises(UmmError, match="找不到原文件备份"):
        uninstall_umm(settings)

    # 什么都没删：入口程序集仍是打过补丁的，运行库也还在。
    assert entry.read_bytes() == PATCHED_ASSEMBLY
    assert (_managed(game) / "UnityModManager" / "UnityModManager.dll").is_file()
    assert get_umm_status(settings)["installed"] is True


def test_uninstall_removes_leftover_backup_when_entry_is_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    _write_assembly_manager(game, "[Assembly-CSharp.dll]ADOStartup.Startup:Before")
    entry = _managed(game) / "Assembly-CSharp.dll"
    backup = entry.with_name(entry.name + ".original_")
    backup.write_bytes(ORIGINAL_ASSEMBLY)

    uninstall_umm(_settings(tmp_path, game))

    assert entry.read_bytes() == b"asm"
    assert not backup.exists()
    assert not (_managed(game) / "UnityModManager").exists()


def test_install_restores_assembly_before_doorstop(assembly_environment):
    game, _installer, settings = assembly_environment
    entry = _managed(game) / "Assembly-CSharp.dll"

    install_umm(settings)

    # 先还原入口程序集，再写 doorstop，避免 UMM 双重加载。
    assert entry.read_bytes() == ORIGINAL_ASSEMBLY
    assert ENTRY_MARKER not in entry.read_bytes()
    assert not entry.with_name(entry.name + ".original_").exists()
    assert (game / "winhttp.dll").read_bytes() == b"winhttp_x64.dll"
    assert (game / "doorstop_config.ini").is_file()
    status = get_umm_status(settings)
    assert status["mode"] == "doorstop"
    assert status["assembly_injected"] is False


def test_install_refuses_assembly_injection_without_backup(assembly_environment):
    game, _installer, settings = assembly_environment
    entry = _managed(game) / "Assembly-CSharp.dll"
    entry.with_name(entry.name + ".original_").unlink()

    with pytest.raises(UmmError, match="找不到原文件备份"):
        install_umm(settings)

    # 拒绝时不能写出半套 doorstop 文件，入口程序集也不能被覆盖。
    assert not (game / "winhttp.dll").exists()
    assert not (game / "doorstop_config.ini").exists()
    assert not (game / "UnityModManager.exe").exists()
    assert entry.read_bytes() == PATCHED_ASSEMBLY
    assert get_umm_status(settings)["mode"] == "assembly"


# ---------------------------------------------------------------------------
# UMM 0.33.x 的真实 Assembly 注入：Config.xml 里 EntryPoint 指向
# UnityEngine.CoreModule.dll，安装器 patch 的就是它；StartingPoint 的
# Assembly-CSharp.dll 保持原版。检测与还原必须按 EntryPoint 找到 CoreModule。
# ---------------------------------------------------------------------------

COREMODULE_NAME = "UnityEngine.CoreModule.dll"
ENTRY_POINT_LINE = "[UnityEngine.CoreModule.dll]UnityEngine.Application.cctor:Before"
STARTING_POINT_LINE = "[Assembly-CSharp.dll]ADOStartup.Startup:Before"


def _coremodule(game: Path) -> Path:
    return _managed(game) / COREMODULE_NAME


def _install_entry_point_mode(
    game: Path,
    *,
    backup: bool = True,
    patch_starting_point: bool = False,
) -> Path:
    """造出 UMM 0.33.x Assembly 注入后的游戏目录，返回被 patch 的 CoreModule。

    Config.xml 同时带 EntryPoint(CoreModule) 与 StartingPoint(Assembly-CSharp)，
    默认只有 EntryPoint 指向的 CoreModule 含补丁标记——与真实 UMM 0.33.3 一致。
    """
    manager = _managed(game) / "UnityModManager"
    manager.mkdir(parents=True, exist_ok=True)
    (manager / "UnityModManager.dll").write_bytes(b"manager")
    (manager / "Config.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<Config Name="A Dance of Fire and Ice">'
        f"<EntryPoint>{ENTRY_POINT_LINE}</EntryPoint>"
        f"<StartingPoint>{STARTING_POINT_LINE}</StartingPoint>"
        "<UIStartingPoint>[Assembly-CSharp.dll]ADOStartup.Startup:After</UIStartingPoint>"
        "</Config>",
        encoding="utf-8",
    )
    core = _coremodule(game)
    core.write_bytes(PATCHED_ASSEMBLY)
    if backup:
        core.with_name(core.name + ".original_").write_bytes(ORIGINAL_ASSEMBLY)
    if patch_starting_point:
        starting = _managed(game) / "Assembly-CSharp.dll"
        starting.write_bytes(PATCHED_ASSEMBLY)
        if backup:
            starting.with_name(starting.name + ".original_").write_bytes(ORIGINAL_ASSEMBLY)
    return core


@pytest.fixture
def entry_point_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    installer = _installer_dir(tmp_path)
    monkeypatch.setattr(
        "adofai_publisher.umm.umm_installer_source",
        lambda *_args, **_kwargs: installer,
    )
    _install_entry_point_mode(game)
    (game / "Mods").mkdir(exist_ok=True)
    return game, installer, _settings(tmp_path, game)


def test_status_recognizes_entry_point_coremodule_injection(entry_point_environment):
    game, _installer, settings = entry_point_environment

    status = get_umm_status(settings)

    assert status["installed"] is True
    assert status["injected"] is True
    assert status["assembly_injected"] is True
    assert status["doorstop_injected"] is False
    assert status["manager_installed"] is True
    assert status["mode"] == "assembly"
    assert status["state"] == "installed"
    assert status["message"] == "UMM 已安装，Assembly 注入完成"
    assert status["entry_assembly"] == str(_coremodule(game))


def test_uninstall_restores_coremodule_entry_assembly(entry_point_environment):
    game, _installer, settings = entry_point_environment
    core = _coremodule(game)
    backup = core.with_name(core.name + ".original_")

    uninstall_umm(settings)

    assert core.read_bytes() == ORIGINAL_ASSEMBLY
    assert ENTRY_MARKER not in core.read_bytes()
    assert not backup.exists()
    assert not (_managed(game) / "UnityModManager").exists()
    assert not (game / "winhttp.dll").exists()
    # 只 patch 了 EntryPoint 指向的程序集，StartingPoint 的程序集不能被误动。
    assert (_managed(game) / "Assembly-CSharp.dll").read_bytes() == b"asm"
    assert not (_managed(game) / "Assembly-CSharp.dll.original_").exists()
    assert (_managed(game) / "Other.dll").is_file()
    assert (game / "Mods").is_dir()
    assert get_umm_status(settings)["state"] == "none"


def test_install_restores_coremodule_before_doorstop(entry_point_environment):
    game, _installer, settings = entry_point_environment
    core = _coremodule(game)

    install_umm(settings)

    # 先还原 CoreModule，再写 doorstop，避免 UMM 双重加载。
    assert core.read_bytes() == ORIGINAL_ASSEMBLY
    assert ENTRY_MARKER not in core.read_bytes()
    assert not core.with_name(core.name + ".original_").exists()
    assert (game / "winhttp.dll").read_bytes() == b"winhttp_x64.dll"
    assert (game / "doorstop_config.ini").is_file()
    status = get_umm_status(settings)
    assert status["mode"] == "doorstop"
    assert status["doorstop_injected"] is True
    assert status["assembly_injected"] is False


def test_uninstall_restores_all_patched_entry_candidates(tmp_path, monkeypatch):
    """两个候选都被 patch 时（旧版叠加 0.33 安装），卸载要把两份都还原。"""
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    core = _install_entry_point_mode(game, patch_starting_point=True)

    uninstall_umm(_settings(tmp_path, game))

    starting = _managed(game) / "Assembly-CSharp.dll"
    assert core.read_bytes() == ORIGINAL_ASSEMBLY
    assert starting.read_bytes() == ORIGINAL_ASSEMBLY
    assert not core.with_name(core.name + ".original_").exists()
    assert not starting.with_name(starting.name + ".original_").exists()
    assert not (_managed(game) / "UnityModManager").exists()


def test_uninstall_refuses_coremodule_injection_without_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    core = _install_entry_point_mode(game, backup=False)

    with pytest.raises(UmmError, match="找不到原文件备份"):
        uninstall_umm(_settings(tmp_path, game))

    # 什么都没删：CoreModule 仍是打过补丁的，运行库也还在。
    assert core.read_bytes() == PATCHED_ASSEMBLY
    assert (_managed(game) / "UnityModManager" / "UnityModManager.dll").is_file()
    assert (_managed(game) / "Assembly-CSharp.dll").read_bytes() == b"asm"
    assert get_umm_status(_settings(tmp_path, game))["installed"] is True


def test_install_refuses_coremodule_injection_without_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game_dir(tmp_path)
    installer = _installer_dir(tmp_path)
    monkeypatch.setattr(
        "adofai_publisher.umm.umm_installer_source",
        lambda *_args, **_kwargs: installer,
    )
    core = _install_entry_point_mode(game, backup=False)

    with pytest.raises(UmmError, match="找不到原文件备份"):
        install_umm(_settings(tmp_path, game))

    assert not (game / "winhttp.dll").exists()
    assert not (game / "doorstop_config.ini").exists()
    assert core.read_bytes() == PATCHED_ASSEMBLY


def test_status_marker_cache_invalidates_when_file_changes(entry_point_environment):
    game, _installer, settings = entry_point_environment
    core = _coremodule(game)

    assert get_umm_status(settings)["assembly_injected"] is True

    # 还原成原版（内容变短）：大小变化必须让缓存失效。
    core.write_bytes(ORIGINAL_ASSEMBLY)
    status = get_umm_status(settings)
    assert status["assembly_injected"] is False
    assert status["installed"] is False

    # 重新打补丁：又要能识别出来。
    core.write_bytes(PATCHED_ASSEMBLY)
    assert get_umm_status(settings)["assembly_injected"] is True


def test_status_marker_cache_invalidates_on_mtime_change(entry_point_environment):
    """同样大小、只改 mtime 的写入也不能沿用旧缓存。"""
    game, _installer, settings = entry_point_environment
    core = _coremodule(game)
    assert get_umm_status(settings)["assembly_injected"] is True

    clean_same_size = ORIGINAL_ASSEMBLY + b"\0" + b"\0" * len(ENTRY_MARKER) + b"\0"
    assert len(clean_same_size) == len(PATCHED_ASSEMBLY)
    core.write_bytes(clean_same_size)
    future = core.stat().st_mtime_ns + 10_000_000_000
    os.utime(core, ns=(future, future))

    assert get_umm_status(settings)["assembly_injected"] is False


def test_status_marker_scan_does_not_reread_unchanged_file(
    entry_point_environment, monkeypatch
):
    """第二次刷新命中缓存，不再把 CoreModule 整文件读一遍。"""
    import adofai_publisher.umm as umm_module

    game, _installer, settings = entry_point_environment
    reads: list[str] = []
    real_read = umm_module._read_entry_assembly_bytes

    def counting_read(path):
        reads.append(str(path))
        return real_read(path)

    monkeypatch.setattr(
        "adofai_publisher.umm._read_entry_assembly_bytes", counting_read
    )

    assert get_umm_status(settings)["assembly_injected"] is True
    first_reads = len(reads)
    assert first_reads >= 1
    assert str(_coremodule(game)) in reads

    get_umm_status(settings)
    assert len(reads) == first_reads
