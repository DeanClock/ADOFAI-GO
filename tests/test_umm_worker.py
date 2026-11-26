from __future__ import annotations

from pathlib import Path

import pytest

from adofai_publisher.config import Settings
from adofai_publisher.game_bridge import GameBridgeError
from adofai_publisher.umm import UmmError
from adofai_publisher.workers import UMM_FILE_ACTIONS, UmmActionWorker


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(tmp_path / "settings.json")


def _run(worker: UmmActionWorker) -> tuple[list[tuple[str, object]], list[str]]:
    finished: list[tuple[str, object]] = []
    failed: list[str] = []
    worker.finished.connect(lambda action, result: finished.append((action, result)))
    worker.failed.connect(failed.append)
    worker.run()
    return finished, failed


def test_install_umm_dispatches_and_emits_finished(settings, monkeypatch):
    calls: list[Settings] = []

    def fake_install(target: Settings) -> Path:
        calls.append(target)
        return Path("C:/game")

    monkeypatch.setattr("adofai_publisher.workers.install_umm", fake_install)

    finished, failed = _run(UmmActionWorker(settings, "install_umm", {}))

    assert calls == [settings]
    assert finished == [("install_umm", str(Path("C:/game")))]
    assert failed == []


def test_uninstall_umm_dispatches(settings, monkeypatch):
    calls: list[Settings] = []
    monkeypatch.setattr(
        "adofai_publisher.workers.uninstall_umm", lambda target: calls.append(target)
    )

    finished, failed = _run(UmmActionWorker(settings, "uninstall_umm", {}))

    assert calls == [settings]
    assert finished == [("uninstall_umm", None)]
    assert failed == []


def test_install_local_update_needs_no_settings(settings, monkeypatch):
    monkeypatch.setattr(
        "adofai_publisher.workers.install_local_update",
        lambda: Path("C:/data/installer"),
    )

    finished, failed = _run(UmmActionWorker(settings, "install_local_update", {}))

    assert finished == [("install_local_update", str(Path("C:/data/installer")))]
    assert failed == []


def test_install_mod_passes_archive_payload(settings, monkeypatch):
    calls: list[tuple[Settings, str, str | None]] = []

    def fake_install(target: Settings, mod_id: str, archive: str | None) -> dict:
        calls.append((target, mod_id, archive))
        return {"display_name": "Tweaks", "installed": True}

    monkeypatch.setattr("adofai_publisher.workers.install_mod", fake_install)

    finished, failed = _run(
        UmmActionWorker(
            settings,
            "install_mod",
            {"mod_id": "AdofaiTweaks", "archive_path": "C:/drop/mod.zip"},
        )
    )

    assert calls == [(settings, "AdofaiTweaks", "C:/drop/mod.zip")]
    assert finished == [("install_mod", {"display_name": "Tweaks", "installed": True})]
    assert failed == []


def test_install_mod_allows_missing_archive(settings, monkeypatch):
    calls: list[tuple[Settings, str, str | None]] = []

    monkeypatch.setattr(
        "adofai_publisher.workers.install_mod",
        lambda target, mod_id, archive=None: calls.append((target, mod_id, archive))
        or {"display_name": mod_id},
    )

    finished, _failed = _run(
        UmmActionWorker(settings, "install_mod", {"mod_id": "AdofaiTweaks"})
    )

    assert calls == [(settings, "AdofaiTweaks", None)]
    assert finished == [("install_mod", {"display_name": "AdofaiTweaks"})]


def test_uninstall_mod_passes_mod_id(settings, monkeypatch):
    calls: list[tuple[Settings, str]] = []
    monkeypatch.setattr(
        "adofai_publisher.workers.uninstall_mod",
        lambda target, mod_id: calls.append((target, mod_id)),
    )

    finished, failed = _run(
        UmmActionWorker(settings, "uninstall_mod", {"mod_id": "AdofaiTweaks"})
    )

    assert calls == [(settings, "AdofaiTweaks")]
    assert finished == [("uninstall_mod", None)]
    assert failed == []


def test_set_mod_enabled_passes_flag(settings, monkeypatch):
    calls: list[tuple[Settings, str, bool]] = []
    monkeypatch.setattr(
        "adofai_publisher.workers.set_mod_enabled",
        lambda target, mod_id, enabled: calls.append((target, mod_id, enabled)),
    )

    finished, failed = _run(
        UmmActionWorker(
            settings,
            "set_mod_enabled",
            {"mod_id": "AdofaiTweaks", "enabled": False},
        )
    )

    assert calls == [(settings, "AdofaiTweaks", False)]
    assert finished == [("set_mod_enabled", None)]
    assert failed == []


@pytest.mark.parametrize("action", ["install_bridge", "repair_bridge"])
def test_bridge_install_actions(action, settings, monkeypatch):
    calls: list[Settings] = []

    def fake_bridge(target: Settings) -> Path:
        calls.append(target)
        return Path("C:/game/Mods/ADOFAIGOBridge")

    monkeypatch.setattr(
        f"adofai_publisher.workers.{_backend_name(action)}", fake_bridge
    )

    finished, failed = _run(UmmActionWorker(settings, action, {}))

    assert calls == [settings]
    assert finished == [(action, str(Path("C:/game/Mods/ADOFAIGOBridge")))]
    assert failed == []


def test_uninstall_bridge_dispatches(settings, monkeypatch):
    calls: list[Settings] = []
    monkeypatch.setattr(
        "adofai_publisher.workers.uninstall_bridge_mod",
        lambda target: calls.append(target),
    )

    finished, failed = _run(UmmActionWorker(settings, "uninstall_bridge", {}))

    assert calls == [settings]
    assert finished == [("uninstall_bridge", None)]
    assert failed == []


def test_known_actions_are_covered_by_dispatch(settings, monkeypatch):
    for action in UMM_FILE_ACTIONS:
        monkeypatch.setattr(
            f"adofai_publisher.workers.{_backend_name(action)}",
            lambda *_args, **_kwargs: None,
        )

    for action in UMM_FILE_ACTIONS:
        _finished, failed = _run(
            UmmActionWorker(
                settings,
                action,
                {"mod_id": "AdofaiTweaks", "enabled": True},
            )
        )
        assert failed == [], action


def _backend_name(action: str) -> str:
    return {
        "install_bridge": "install_bridge_mod",
        "repair_bridge": "repair_bridge_mod",
        "uninstall_bridge": "uninstall_bridge_mod",
    }.get(action, action)


def test_unknown_action_fails_without_crashing(settings):
    finished, failed = _run(UmmActionWorker(settings, "explode", {}))

    assert finished == []
    assert failed == ["未知的模组操作：explode"]


def test_umm_error_becomes_failed_signal(settings, monkeypatch):
    def boom(_target: Settings) -> Path:
        raise UmmError("找不到安装器目录")

    monkeypatch.setattr("adofai_publisher.workers.install_umm", boom)

    finished, failed = _run(UmmActionWorker(settings, "install_umm", {}))

    assert finished == []
    assert failed == ["找不到安装器目录"]


def test_game_bridge_error_becomes_failed_signal(settings, monkeypatch):
    def boom(_target: Settings) -> Path:
        raise GameBridgeError("游戏正在运行")

    monkeypatch.setattr("adofai_publisher.workers.install_bridge_mod", boom)

    finished, failed = _run(UmmActionWorker(settings, "install_bridge", {}))

    assert finished == []
    assert failed == ["游戏正在运行"]


def test_unexpected_error_becomes_readable_failed_signal(settings, monkeypatch):
    def boom(_target: Settings) -> None:
        raise RuntimeError("磁盘炸了")

    monkeypatch.setattr("adofai_publisher.workers.uninstall_umm", boom)

    finished, failed = _run(UmmActionWorker(settings, "uninstall_umm", {}))

    assert finished == []
    assert failed == ["RuntimeError: 磁盘炸了"]


def test_missing_mod_id_is_reported_as_failure(settings, monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(
        "adofai_publisher.workers.uninstall_mod",
        lambda *args: calls.append(args),
    )

    finished, failed = _run(UmmActionWorker(settings, "uninstall_mod", {}))

    assert calls == []
    assert finished == []
    assert failed == ["缺少模组标识，请刷新模组列表后重试"]
