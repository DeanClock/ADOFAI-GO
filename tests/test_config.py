from __future__ import annotations

import json

from adofai_publisher import config
from adofai_publisher.config import Settings


def test_default_hotkey_is_alt_x(tmp_path):
    settings = Settings(tmp_path / "settings.json")

    assert settings.get("game_hotkey") == "Alt+X"


def test_legacy_shift_default_migrates_to_alt_x(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"game_hotkey": "Shift+Shift"}), encoding="utf-8")

    settings = Settings(path)

    assert settings.get("game_hotkey") == "Alt+X"


def test_custom_hotkey_survives_migration(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"game_hotkey": "Ctrl+Shift+G"}), encoding="utf-8")

    settings = Settings(path)

    assert settings.get("game_hotkey") == "Ctrl+Shift+G"


def test_double_shift_chosen_after_migration_is_kept(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(path)
    settings.set("game_hotkey", "Shift+Shift")
    settings.save()

    assert Settings(path).get("game_hotkey") == "Shift+Shift"


def test_obs_defaults_are_local_and_password_is_empty(tmp_path):
    settings = Settings(tmp_path / "settings.json")

    assert settings.get("obs_host") == "127.0.0.1"
    assert settings.get("obs_port") == 4455
    assert settings.get("obs_password") == ""


def test_obs_settings_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(path)
    settings.set("obs_host", "192.168.1.10")
    settings.set("obs_port", 4466)
    settings.set("obs_password", "local-secret")
    settings.save()

    loaded = Settings(path)
    assert loaded.get("obs_host") == "192.168.1.10"
    assert loaded.get("obs_port") == 4466
    assert loaded.get("obs_password") == "local-secret"


def test_update_repo_defaults_to_public_repository(tmp_path):
    settings = Settings(tmp_path / "settings.json")

    assert settings.get("update_repo") == "DeanClock/ADOFAI-GO"


def test_saved_update_repo_is_not_overwritten_by_new_default(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"update_repo": "someone/custom-repo"}), encoding="utf-8")

    assert Settings(path).get("update_repo") == "someone/custom-repo"


def test_saved_empty_update_repo_is_not_overwritten_by_new_default(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"update_repo": ""}), encoding="utf-8")

    assert Settings(path).get("update_repo") == ""


def test_auto_check_updates_defaults_to_true(tmp_path):
    settings = Settings(tmp_path / "settings.json")

    assert settings.get("auto_check_updates") is True


def test_update_repo_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(path)
    settings.set("update_repo", "DeanColck/ADOFAI-GO")
    settings.save()

    assert Settings(path).get("update_repo") == "DeanColck/ADOFAI-GO"


def test_default_settings_include_umm_installer_dir(tmp_path):
    settings = Settings(tmp_path / "settings.json")

    assert "umm_installer_dir" in config.DEFAULT_SETTINGS
    assert settings.get("umm_installer_dir") == ""


def test_umm_installer_dir_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(path)
    settings.set("umm_installer_dir", r"D:\Tools\UnityModManagerInstaller")
    settings.save()

    assert Settings(path).get("umm_installer_dir") == r"D:\Tools\UnityModManagerInstaller"


def test_publish_mode_defaults_to_dual(tmp_path):
    settings = Settings(tmp_path / "settings.json")

    assert settings.get("publish_mode") == "dual"


def test_publish_mode_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(path)
    settings.set("publish_mode", "bili_only")
    settings.save()

    assert Settings(path).get("publish_mode") == "bili_only"


def test_legacy_settings_without_publish_mode_defaults_to_dual(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")

    assert Settings(path).get("publish_mode") == "dual"


def test_unknown_publish_mode_falls_back_to_dual(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"publish_mode": "something-else"}), encoding="utf-8")

    assert Settings(path).get("publish_mode") == "dual"


def test_portable_marker_is_ignored_for_source_runs(tmp_path, monkeypatch):
    monkeypatch.delenv("ADOFAI_TUF_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(config.sys, "frozen", False, raising=False)
    (tmp_path / "portable.txt").write_text("", encoding="utf-8")

    assert config._portable_root() is None


def test_portable_marker_redirects_frozen_data_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("ADOFAI_TUF_HOME", raising=False)
    exe = tmp_path / "ADOFAI GO.exe"
    exe.write_bytes(b"")
    (tmp_path / "portable.txt").write_text("", encoding="utf-8")
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config.sys, "executable", str(exe))

    assert config.base_dir() == tmp_path / "ADOFAI GO 数据"


def test_env_override_beats_portable_marker(tmp_path, monkeypatch):
    exe = tmp_path / "ADOFAI GO.exe"
    exe.write_bytes(b"")
    (tmp_path / "portable.txt").write_text("", encoding="utf-8")
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config.sys, "executable", str(exe))
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path / "custom"))

    assert config.base_dir() == tmp_path / "custom"


def test_legacy_and_unknown_settings_keys_are_dropped_on_load(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "keep_browser_open": True,
                "tuf_username": "old-user",
                "bili_username": "old-bili",
                "some_future_key": "whatever",
                "theme": "light",
            }
        ),
        encoding="utf-8",
    )

    settings = Settings(path)

    assert settings.get("theme") == "light"
    assert settings.get("keep_browser_open") is None
    assert settings.get("tuf_username") is None
    assert settings.get("bili_username") is None
    assert "some_future_key" not in settings.as_dict()
    settings.save()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "keep_browser_open" not in saved
    assert "tuf_username" not in saved
    assert "bili_username" not in saved
    assert "some_future_key" not in saved
    assert Settings(path).get("theme") == "light"


def test_removed_settings_keys_are_not_defaults():
    for key in ("keep_browser_open", "tuf_username", "bili_username"):
        assert key not in config.DEFAULT_SETTINGS


def test_umm_game_dir_defaults_to_empty_and_round_trips(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(path)
    assert settings.get("umm_game_dir") == ""

    settings.set("umm_game_dir", r"D:\Steam\A Dance of Fire and Ice")
    settings.save()

    assert (
        Settings(path).get("umm_game_dir")
        == r"D:\Steam\A Dance of Fire and Ice"
    )


def test_malformed_settings_file_keeps_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not valid json", encoding="utf-8")

    settings = Settings(path)

    assert settings.get("theme") == "dark"
    assert settings.get("publish_mode") == "dual"


def test_saving_after_runtime_update_drops_removed_keys(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(path)
    settings.update({"tuf_username": "stale", "theme": "light"})
    settings.save()

    loaded = Settings(path)

    assert loaded.get("theme") == "light"
    assert loaded.get("tuf_username") is None
    assert "tuf_username" not in json.loads(path.read_text(encoding="utf-8"))
