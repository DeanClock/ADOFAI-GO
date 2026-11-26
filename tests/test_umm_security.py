"""模组 ID 路径穿越防护与安装器路径隐私的回归测试。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import adofai_publisher.umm as umm
from adofai_publisher.config import Settings
from adofai_publisher.umm import (
    MODS_DIR_NAME,
    UmmError,
    install_mod_archive,
    resolve_mod_dir,
    uninstall_mod,
)

# 各类非法模组 ID：相对穿越、绝对路径、上级目录、空串。
TRAVERSAL_IDS = [
    r"..\..\evil",
    "../evil",
    r"C:\temp\evil",
    "..",
    "",
]


def _game(tmp_path: Path) -> Path:
    game = tmp_path / "game"
    game.mkdir()
    (game / "A Dance of Fire and Ice.exe").write_bytes(b"MZ")
    (game / MODS_DIR_NAME).mkdir()
    return game


def _settings(tmp_path: Path, game: Path) -> Settings:
    settings = Settings(tmp_path / "settings.json")
    settings.set("umm_game_dir", str(game))
    settings.save()
    return settings


def _write_mod_zip(path: Path, mod_id: str, *, entry_dir: str | None = None) -> Path:
    """压缩包内 Info.json 的 Id 为 mod_id，条目放在 entry_dir 目录下。"""
    folder = entry_dir or mod_id
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            f"{folder}/Info.json",
            json.dumps(
                {
                    "Id": mod_id,
                    "DisplayName": mod_id,
                    "Author": "Test",
                    "Version": "1.0.0",
                }
            ),
        )
        zf.writestr(f"{folder}/mod.dll", b"binary")
    return path


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    game = _game(tmp_path)
    return game, _settings(tmp_path, game)


@pytest.mark.parametrize("mod_id", TRAVERSAL_IDS)
def test_install_rejects_unsafe_mod_id(environment, tmp_path, mod_id):
    game, settings = environment
    archive = _write_mod_zip(tmp_path / "mod.zip", "SafeMod")

    with pytest.raises(UmmError, match="模组 ID 非法"):
        install_mod_archive(settings, mod_id, archive)

    # Mods 目录内外都不能被创建或删除任何东西。
    assert not (tmp_path / "evil").exists()
    assert not (game.parent / "evil").exists()
    assert list((game / MODS_DIR_NAME).iterdir()) == []


@pytest.mark.parametrize("mod_id", TRAVERSAL_IDS)
def test_resolve_and_uninstall_reject_unsafe_mod_id(environment, tmp_path, mod_id):
    game, settings = environment
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(UmmError, match="模组 ID 非法"):
        resolve_mod_dir(settings, mod_id)
    with pytest.raises(UmmError, match="模组 ID 非法"):
        uninstall_mod(settings, mod_id)

    assert (outside / "keep.txt").is_file()
    assert (game / "A Dance of Fire and Ice.exe").is_file()


def test_install_rejects_info_json_id_traversal(environment, tmp_path):
    """压缩包 Info.json 的 Id 带 .. 时安装失败，且不落到 Mods 之外。"""
    game, settings = environment
    archive = _write_mod_zip(tmp_path / "mod.zip", r"..\..\evil", entry_dir="SafeMod")

    with pytest.raises(UmmError, match="模组 ID 非法"):
        install_mod_archive(settings, "SafeMod", archive)

    assert not (tmp_path / "evil").exists()
    assert not (game.parent / "evil").exists()
    # 失败后不能在 Mods 里留下半装目录。
    assert list((game / MODS_DIR_NAME).iterdir()) == []


def test_unicode_mod_id_installs_resolves_and_uninstalls(environment, tmp_path):
    """中文（display_name 回退）模组 ID 必须继续可用。"""
    game, settings = environment
    mod_id = "测试模组"
    archive = _write_mod_zip(tmp_path / "mod.zip", mod_id)

    result = install_mod_archive(settings, mod_id, archive)

    target = game / MODS_DIR_NAME / mod_id
    assert result["id"] == mod_id
    assert result["installed"] is True
    assert (target / "Info.json").is_file()
    assert resolve_mod_dir(settings, mod_id) == target

    uninstall_mod(settings, mod_id)
    assert not target.exists()


def test_installer_source_has_no_hardcoded_user_path():
    source = Path(umm.__file__).read_text(encoding="utf-8")

    assert "DeskBox" not in source
    assert "REFERENCE_INSTALLER_DIR" not in source
