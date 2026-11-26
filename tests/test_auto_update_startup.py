from __future__ import annotations

import logging
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ADOFAI_NO_AUTOLOGIN", "1")

from PySide6.QtWidgets import QApplication

from adofai_publisher.config import DEFAULT_SETTINGS, Settings
from adofai_publisher.ui.main_window import MainWindow


REPO = "DeanClock/ADOFAI-GO"


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _window(tmp_path, monkeypatch, *, enabled: bool = True, repo: str = REPO):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = _app()
    settings = Settings(tmp_path / "settings.json")
    settings.set("auto_check_updates", enabled)
    settings.set("update_repo", repo)
    return app, MainWindow(settings)


def test_startup_check_uses_saved_repo_when_enabled(tmp_path, monkeypatch):
    app, window = _window(tmp_path, monkeypatch)
    checked: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        window,
        "_check_update",
        lambda repo, *, silent=False: checked.append((repo, silent)),
    )

    window._check_updates_on_startup()

    assert checked == [(REPO, True)]
    window.close()
    app.processEvents()


def test_startup_check_skips_when_disabled_and_falls_back_for_empty_repo(
    tmp_path, monkeypatch
):
    app, disabled = _window(tmp_path / "disabled", monkeypatch, enabled=False)
    disabled_calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        disabled,
        "_check_update",
        lambda repo, *, silent=False: disabled_calls.append((repo, silent)),
    )
    disabled._check_updates_on_startup()
    assert disabled_calls == []
    assert not disabled._startup_update_timer.isActive()
    disabled.close()

    app, empty_repo = _window(tmp_path / "empty", monkeypatch, repo="")
    empty_calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        empty_repo,
        "_check_update",
        lambda repo, *, silent=False: empty_calls.append((repo, silent)),
    )
    empty_repo._check_updates_on_startup()
    assert empty_calls == [(DEFAULT_SETTINGS["update_repo"], True)]
    assert empty_repo._startup_update_timer.isActive()
    assert empty_repo._startup_update_timer.interval() == 4000
    empty_repo.close()
    app.processEvents()


def test_startup_check_failure_is_logged_without_ui_or_toast(
    tmp_path, monkeypatch, caplog
):
    app, window = _window(tmp_path, monkeypatch)
    started: dict[str, object] = {}
    toasts: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        window,
        "_start_worker",
        lambda worker, **callbacks: started.update(callbacks),
    )
    monkeypatch.setattr(window.toast, "show_message", lambda *args: toasts.append(args))
    before_status = window.settings_page.update_status.text()
    before_button_text = window.settings_page.update_check_button.text()

    with caplog.at_level(logging.INFO, logger="adofai"):
        window._check_update(REPO, silent=True)
        on_failed = started["on_failed"]
        on_failed("GitHub 网络不可用")

    assert "GitHub 网络不可用" in caplog.text
    assert toasts == []
    assert window.settings_page.update_status.text() == before_status
    assert window.settings_page.update_check_button.text() == before_button_text
    window.close()
    app.processEvents()


def test_startup_check_notifies_when_update_is_available(tmp_path, monkeypatch):
    app, window = _window(tmp_path, monkeypatch)
    started: dict[str, object] = {}
    toasts: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        window,
        "_start_worker",
        lambda worker, **callbacks: started.update(callbacks),
    )
    monkeypatch.setattr(window.toast, "show_message", lambda *args: toasts.append(args))

    window._check_update(REPO, silent=True)
    started["on_finished"](
        {
            "latest": "v0.18.0",
            "current": "0.17.18",
            "has_update": True,
            "body": "测试版更新说明",
            "html_url": "https://github.com/DeanClock/ADOFAI-GO/releases/latest",
        }
    )

    assert "发现新版本 v0.18.0" in window.settings_page.update_status.text()
    assert window.settings_page.update_notes.text() == "测试版更新说明"
    assert not window.settings_page.update_download_button.isHidden()
    assert len(toasts) == 1
    assert "v0.18.0" in toasts[0][0]
    window.close()
    app.processEvents()


def test_startup_and_manual_checks_never_start_in_parallel(tmp_path, monkeypatch):
    app, window = _window(tmp_path, monkeypatch)
    started: list[object] = []
    monkeypatch.setattr(
        window,
        "_start_worker",
        lambda worker, **_callbacks: started.append(worker),
    )

    window._check_update(REPO)
    manual_worker = window._update_check_worker
    window._check_updates_on_startup()

    assert len(started) == 1
    assert window._update_check_worker is manual_worker

    window._on_update_check_finished()
    window._check_updates_on_startup()
    startup_worker = window._update_check_worker
    window._check_update(REPO)

    assert len(started) == 2
    assert window._update_check_worker is startup_worker
    window.close()
    app.processEvents()
