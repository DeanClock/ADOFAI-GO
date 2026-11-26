from __future__ import annotations

import ctypes
import logging
import os
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Settings, log_dir

_LOGGING_READY = False


def main() -> int:
    _setup_logging()
    if "--selftest" in sys.argv:
        return _run_selftest()
    try:
        return _run_app()
    except Exception:
        logging.getLogger(__name__).exception("启动失败")
        _report_startup_failure()
        return 1


def _run_app() -> int:
    from PySide6.QtGui import QFont, QFontDatabase, QIcon
    from PySide6.QtWidgets import QApplication

    from .resources import icon_path
    from .ui.interactions import install_interactions
    from .ui.main_window import MainWindow
    from .ui.theme import apply_theme

    _set_windows_app_id()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("ADOFAI GO")
    app.setOrganizationName("AdofaiTufBridge")
    app.setWindowIcon(QIcon(str(icon_path())))
    _load_fonts()
    families = set(QFontDatabase.families())
    family = next(
        (name for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI") if name in families),
        "Segoe UI",
    )
    app.setFont(QFont(family, 10))
    settings = Settings()
    apply_theme(settings.get("theme") or "dark", app)
    install_interactions(app)
    window = MainWindow(settings)
    window.show()
    window.open_page(_page_argument(), _tab_argument())
    window.start_hotkey_listener()
    return app.exec()


def _report_startup_failure() -> None:
    detail = traceback.format_exc()
    log_path = log_dir() / "startup-error.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            _startup_diagnostics() + "\n\n" + detail,
            encoding="utf-8",
        )
    except OSError:
        log_path = None

    text = "程序启动失败。\n\n"
    if log_path is not None:
        text += f"详细信息已写入：\n{log_path}\n\n"
    text += "如果提示 QtGui / DLL 加载失败，请确认只运行最新版本的 exe，并重新解压或重新下载。"
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(0, text, "ADOFAI GO - 启动失败", 0x10)
        except Exception:
            pass


def _startup_diagnostics() -> str:
    suspicious = [
        item
        for item in os.environ.get("PATH", "").split(os.pathsep)
        if "codex-runtimes" in item.lower()
    ]
    lines = [
        f"frozen={getattr(sys, 'frozen', False)}",
        f"executable={sys.executable}",
        f"meipass={getattr(sys, '_MEIPASS', '')}",
        f"python={sys.version}",
    ]
    if suspicious:
        lines.append("PATH suspects=" + ";".join(suspicious))
    return "\n".join(lines)


def _argument_value(flag: str) -> str:
    args = sys.argv[1:]
    for index, value in enumerate(args):
        if value == flag and index + 1 < len(args):
            return args[index + 1]
        if value.startswith(flag + "="):
            return value.split("=", 1)[1]
    return ""


def _page_argument() -> str:
    return _argument_value("--page")


def _tab_argument() -> str:
    return _argument_value("--tab")


def _set_windows_app_id() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "AdofaiTufBridge.AdofaiPublisher"
        )
    except Exception:
        logging.getLogger(__name__).debug("无法设置 Windows AppUserModelID", exc_info=True)


def _load_fonts() -> None:
    from PySide6.QtGui import QFontDatabase

    font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for name in ("msyh.ttc", "msyhbd.ttc", "msyhl.ttc"):
        path = font_dir / name
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))


def _setup_logging() -> None:
    global _LOGGING_READY
    if _LOGGING_READY:
        return
    _LOGGING_READY = True
    try:
        path = log_dir() / "app.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    def _log_uncaught(exc_type, exc, tb) -> None:
        logging.getLogger("adofai").exception("未捕获异常", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _log_uncaught


def _run_selftest() -> int:
    from .browser import has_bili_login, launch_context
    from .config import base_dir
    from .levellookup import LevelLookup

    output = Path(os.environ.get("ADOFAI_SELFTEST_OUT", str(base_dir() / "selftest.log")))
    output.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    ok = True
    try:
        import PySide6
        from PySide6.QtGui import QFont  # noqa: F401
        from PySide6.QtWidgets import QApplication  # noqa: F401

        lines.append(f"qt ok: PySide6 {PySide6.__version__}")
        level = LevelLookup().get(17776)
        lines.append(f"lookup ok: {level.song} / {level.difficulty} / {level.charter}")
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            context = launch_context(playwright, Settings(), headless=True)
            try:
                lines.append(f"browser ok: bili_login={has_bili_login(context)}")
            finally:
                context.close()
    except Exception:
        ok = False
        lines.append("selftest failed:\n" + traceback.format_exc())
    output.write_text("\n".join(lines), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
