"""UI-4 外壳回归测试：侧边栏、切页动画、状态行与样张页。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ADOFAI_NO_AUTOLOGIN", "1")

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from adofai_publisher.config import Settings
from adofai_publisher.models import Draft, empty_judgements
from adofai_publisher.ui import icons
from adofai_publisher.ui.design_tokens import tokens_for
from adofai_publisher.ui.main_window import MainWindow, NAV_ITEMS
from adofai_publisher.ui.pages import WorkspacePage
from adofai_publisher.ui.shell import (
    AnimatedStack,
    SideBar,
    StatusLine,
    StatusSpinner,
)
from adofai_publisher.ui.styleguide_page import StyleGuidePage
from adofai_publisher.ui.theme import build_qss, metrics

NAV_SAMPLE = [(key, label) for key, _icon, label, _title, _subtitle in NAV_ITEMS]


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# 侧边栏
# ---------------------------------------------------------------------------


def test_sidebar_width_and_nav_texts(app):
    sidebar = SideBar(NAV_SAMPLE)
    try:
        assert sidebar.width() == metrics().sidebar_width == 232
        assert len(sidebar.buttons) == 7
        assert [button.text() for button in sidebar.buttons.values()] == [
            f"{index:02d}   {label}" for index, (_key, label) in enumerate(NAV_SAMPLE)
        ]
        home = sidebar.buttons["home"]
        assert home.text().split()[0] == "00"
        assert home.toolTip() == "Ctrl+0"
        assert sidebar.buttons["settings"].toolTip() == "Ctrl+6"
        assert home.cursor().shape() == Qt.CursorShape.PointingHandCursor
        assert home.focusPolicy() == Qt.FocusPolicy.StrongFocus
        assert home.isCheckable()
    finally:
        sidebar.deleteLater()
        app.processEvents()


def test_nav_item_keyboard_moves_focus_and_activates(app):
    sidebar = SideBar(NAV_SAMPLE)
    sidebar.show()
    app.processEvents()
    seen: list[str] = []
    sidebar.navigate.connect(seen.append)
    try:
        first = sidebar.buttons["home"]
        first.setFocus()
        app.processEvents()
        assert QApplication.focusWidget() is first

        QTest.keyClick(first, Qt.Key.Key_Down)
        app.processEvents()
        assert QApplication.focusWidget() is sidebar.buttons["workspace"]

        QTest.keyClick(sidebar.buttons["workspace"], Qt.Key.Key_Return)
        assert seen == ["workspace"]
    finally:
        sidebar.close()
        sidebar.deleteLater()
        app.processEvents()


# ---------------------------------------------------------------------------
# 切页动画
# ---------------------------------------------------------------------------


def test_animated_stack_switches_synchronously_offscreen(app):
    stack = AnimatedStack()
    first, second = QLabel("A"), QLabel("B")
    stack.addWidget(first)
    stack.addWidget(second)
    stack.show()
    app.processEvents()

    stack.setCurrentIndex(1)
    assert stack.currentIndex() == 1
    assert stack.currentWidget() is second
    assert stack._overlay is None
    assert first.graphicsEffect() is None
    stack.close()
    stack.deleteLater()
    app.processEvents()


def test_animated_stack_uses_snapshot_overlay_when_enabled(app, monkeypatch):
    monkeypatch.setattr(
        "adofai_publisher.ui.shell.animations_enabled", lambda: True
    )
    stack = AnimatedStack()
    stack.addWidget(QLabel("A"))
    stack.addWidget(QLabel("B"))
    stack.resize(320, 200)
    stack.show()
    app.processEvents()

    stack.setCurrentIndex(1)
    assert stack.currentIndex() == 1
    overlay = stack._overlay
    assert overlay is not None
    # 动画期间页面本身不挂透明度效果。
    assert stack.currentWidget().graphicsEffect() is None

    # 把淡出动画直接推到终点，同步触发 finished；不等真实时间。
    animation = stack._animation
    assert animation is not None
    animation.setCurrentTime(animation.duration())
    assert stack._overlay is None
    stack.close()
    stack.deleteLater()
    app.processEvents()


# ---------------------------------------------------------------------------
# 状态行
# ---------------------------------------------------------------------------


def test_status_line_message_timeout_and_spinner(app):
    line = StatusLine()
    try:
        line.show_message("正在同步…")
        assert line.current_message() == "正在同步…"

        spinner = line.spinner
        line.set_busy(True)
        assert spinner.is_spinning()
        assert not spinner.isHidden()
        line.set_busy(False)
        assert not spinner.is_spinning()

        line.show_message("短暂提示", 40)
        # 校验超时时长后直接触发 timeout，不真实等待 40ms。
        assert line._timer.isActive()
        assert line._timer.interval() == 40
        line._timer.timeout.emit()
        assert line.current_message() == "就绪"
    finally:
        line.deleteLater()
        app.processEvents()


def test_status_line_log_panel_is_read_only_and_capped(app):
    host = QWidget()
    host.resize(900, 640)
    line = StatusLine(host)
    line.setGeometry(0, 600, 900, 34)
    host.show()
    app.processEvents()
    try:
        line.toggle_log()
        app.processEvents()
        assert line._panel is not None
        assert line._panel.isVisible()
        assert line._view is not None
        assert line._view.isReadOnly()

        for index in range(260):
            line.append_log(f"line {index}")
        assert line._view.document().blockCount() <= 205

        line.toggle_log()
        assert not line._panel.isVisible()
    finally:
        host.close()
        host.deleteLater()
        app.processEvents()


# ---------------------------------------------------------------------------
# 图标缓存
# ---------------------------------------------------------------------------


def test_line_icons_are_cached_and_colored(app):
    icons.clear_cache()
    first = icons.line_icon("home", "#123456", size=18, dpr=2.0)
    second = icons.line_icon("home", "#123456", size=18, dpr=2.0)
    other = icons.line_icon("home", "#654321", size=18, dpr=2.0)
    assert not first.isNull()
    assert first is second
    assert other is not first
    assert first.devicePixelRatio() == 2.0
    icons.clear_cache()


def test_settings_icon_differs_from_modules(app):
    """侧边栏「模组」键是 umm，曾经和 settings 一起落进同一个分支，撞成滑杆。

    现在 umm 走拼图线稿、settings 走齿轮，这里守住不许再撞。
    """
    icons.clear_cache()
    assert icons._path("settings").elementCount() != icons._path("umm").elementCount()
    assert icons._path("umm").elementCount() == icons._path("modules").elementCount()

    def render(name: str):
        return icons.line_icon(name, "#ffffff", size=18, dpr=1.0).toImage()

    settings_img = render("settings")
    umm_img = render("umm")
    assert not settings_img.isNull() and not umm_img.isNull()
    assert settings_img != umm_img, "侧边栏「模组」和「设置」图标相同"
    assert umm_img == render("modules")
    icons.clear_cache()


def test_modules_icon_draws_a_jigsaw_piece(app):
    """「模组」是一枚拼图线稿：圆角方体 + 顶部凸起 + 右侧凹槽，且不出 18×18 画布。"""
    path = icons._path("umm")
    assert path.elementCount() > 6, "拼图轮廓不该再是几条直线的多边形"

    bounds = path.boundingRect()
    assert bounds.left() >= 2.0 and bounds.top() >= 2.0
    assert bounds.right() <= 16.0 and bounds.bottom() <= 16.0
    assert 11.0 <= bounds.width() <= 13.5
    assert 11.5 <= bounds.height() <= 13.5
    assert abs(bounds.center().x() - 9.0) <= 0.6
    assert abs(bounds.center().y() - 9.0) <= 0.6

    # 顶部凸起：中轴上方靠近画布上沿处仍是实心，同一高度的左侧空白处不是。
    assert path.contains(QPointF(9.0, 3.3))
    assert not path.contains(QPointF(4.4, 3.5))
    # 右侧凹槽：同一列上，凹槽上方是实心，凹槽中间被挖空。
    assert path.contains(QPointF(14.0, 7.2))
    assert not path.contains(QPointF(14.0, 10.6))


# ---------------------------------------------------------------------------
# 主窗口集成
# ---------------------------------------------------------------------------


def test_main_window_shell_and_styleguide(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    monkeypatch.setenv("ADOFAI_NO_AUTOLOGIN", "1")
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    try:
        assert window.stack.count() == len(NAV_ITEMS) == 7
        assert isinstance(window.stack, AnimatedStack)
        assert isinstance(window.status_line, StatusLine)
        assert window.findChild(QStatusBar) is None
        assert [button.text().split()[0] for button in window.nav_buttons.values()] == [
            "00",
            "01",
            "02",
            "03",
            "04",
            "05",
            "06",
        ]
        assert window.status_line.current_message() == "就绪"

        # Toast 定位不能通过 parent.statusBar() 懒创建默认状态栏。
        window.resize(1100, 720)
        window.show()
        app.processEvents()
        window.toast.show_message("定位检查", "info", duration=60000)
        app.processEvents()
        assert window.findChildren(QStatusBar) == []
        window.toast.dismiss()

        window.open_page("styleguide")
        assert window.stack.count() == 8
        assert isinstance(window.stack.currentWidget(), StyleGuidePage)
        assert window.page_title.text() == "控件样张"

        window.settings.set("theme", "light")
        window._apply_theme()
        app.processEvents()
        assert window._styleguide_page.step_bar._theme == "light"
        window.settings.set("theme", "dark")
        window._apply_theme()
        app.processEvents()

        window._navigate("home")
        assert window.stack.currentWidget() is window.home
        assert window.stack.count() == 8
    finally:
        window.close()
        app.processEvents()


def test_workspace_splits_step_header_from_scroll_body(tmp_path, monkeypatch):
    """步骤条头部条与滚动区必须是两层，否则整片同色分不清。"""
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    monkeypatch.setenv("ADOFAI_NO_AUTOLOGIN", "1")
    app = QApplication.instance() or QApplication([])
    page = WorkspacePage(Settings(tmp_path / "settings.json"))
    try:
        assert page.step_header.objectName() == "StepHeader"
        assert page.step_bar.parent() is page.step_header
        margins = page.step_header.layout().contentsMargins()
        assert margins.top() >= 8 and margins.bottom() >= 8
        assert page.stack_inner.objectName() == "WorkspaceBody"
        assert page.stack_scroll.objectName() == "WorkspaceScroll"
        assert page.stack_layout.contentsMargins().top() >= 8
    finally:
        page.deleteLater()
        app.processEvents()


def test_flow_layout_wraps_chips_when_narrow(app):
    from adofai_publisher.ui.widgets import FlowLayout

    host = QWidget()
    host.resize(320, 120)
    flow = FlowLayout(h_spacing=10, v_spacing=8)
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addLayout(flow)
    chips = []
    for index in range(3):
        label = QLabel(f"提示 {index}")
        label.setFixedSize(120, 24)
        chips.append(label)
        flow.addWidget(label)
    host.show()
    app.processEvents()
    try:
        rows = {chip.y() for chip in chips}
        assert len(rows) == 2, "窄宽度下没有换行"
        assert flow.heightForWidth(400) < flow.heightForWidth(320)

        host.resize(600, 120)
        app.processEvents()
        assert len({chip.y() for chip in chips}) == 1, "宽宽度下仍被拆行"
    finally:
        host.close()
        host.deleteLater()
        app.processEvents()


def test_workspace_step_two_fits_minimum_window(tmp_path, monkeypatch):
    """1280/最小窗口下第二步不能出现横向裁切（卡片被切掉的回归）。"""
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    monkeypatch.setenv("ADOFAI_NO_AUTOLOGIN", "1")
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Settings(tmp_path / "settings.json"))
    try:
        window.resize(1080, 720)  # 与 MainWindow.setMinimumSize 一致
        window.show()
        app.processEvents()
        window.open_page("workspace")
        window.workspace.mode_bili_button.setChecked(True)
        window.workspace.go_step(1)
        app.processEvents()

        inner = window.workspace.stack_inner
        viewport = window.workspace.stack_scroll.viewport()
        assert inner.minimumSizeHint().width() <= viewport.width()

        # 超长文件名也不能把卡片最小宽度顶宽（会被中间省略）。
        long_name = "very_long_recording_name_for_a_rhythm_game_run_v2.mp4"
        video = tmp_path / long_name
        video.write_bytes(b"0" * 64)
        window.workspace.drop_zone.set_file(str(video))
        app.processEvents()
        assert window.workspace.drop_zone.minimumSizeHint().width() < 600
        assert window.workspace.drop_zone.file_label.toolTip().endswith("MB")
    finally:
        window.close()
        app.processEvents()


def test_workspace_x_perfect_checkbox_syncs_grid_and_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    monkeypatch.setenv("ADOFAI_NO_AUTOLOGIN", "1")
    app = QApplication.instance() or QApplication([])
    page = WorkspacePage(Settings(tmp_path / "settings.json"))
    try:
        page.x_perfect_check.setChecked(True)
        app.processEvents()
        assert page.judgement_grid.is_x_perfect() is True
        assert page._snapshot().x_perfect is True

        page.judgement_grid.spins["perfectMinus"].setText("3")
        snapshot = page._snapshot()
        assert snapshot.judgements["perfectMinus"] == 3

        draft = Draft(judgements={**empty_judgements(), "perfectMinus": 6, "perfect": 2}, x_perfect=True)
        page.load_draft(draft)
        app.processEvents()
        assert page.x_perfect_check.isChecked() is True
        assert page.judgement_grid.is_x_perfect() is True
        assert page.judgement_grid.values()["perfectMinus"] == 6

        page.x_perfect_check.setChecked(False)
        app.processEvents()
        assert page.judgement_grid.is_x_perfect() is False
        assert page.judgement_grid.values()["perfectMinus"] == 0
    finally:
        page.deleteLater()
        app.processEvents()


def test_workspace_volume_check_uses_hit_count(tmp_path, monkeypatch):
    from adofai_publisher.models import LevelInfo

    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    monkeypatch.setenv("ADOFAI_NO_AUTOLOGIN", "1")
    app = QApplication.instance() or QApplication([])
    page = WorkspacePage(Settings(tmp_path / "settings.json"))
    try:
        page.bind_level(
            LevelInfo(tuf_id=13141, song="Fledgling (Super Easy)", tilecount=1273)
        )
        for key, value in (
            ("perfect", 1195),
            ("ePerfect", 25),
            ("lPerfect", 36),
            ("earlySingle", 9),
            ("lateSingle", 8),
        ):
            page.judgement_grid.spins[key].setText(str(value))
        app.processEvents()

        assert "1273 / 1273" in page.expected_label.text()
        assert "√ 一致" in page.expected_label.text()
        assert "打击数" in page.expected_label.text()
        assert "判定总数（九格）：1273" in page.judgement_grid.counts_text()

        # 提前只进九格和，不改变打击数与物量校验结论。
        page.judgement_grid.spins["earlyDouble"].setText("1")
        app.processEvents()
        assert "1273 / 1273" in page.expected_label.text()
        assert "√ 一致" in page.expected_label.text()
        counts = page.judgement_grid.counts_text()
        assert "判定总数（九格）：1274" in counts
        assert "打击数（排除提前 / 太慢！！）：1273" in counts
    finally:
        page.deleteLater()
        app.processEvents()


def test_theme_qss_separates_workspace_header_and_body():
    for theme in ("light", "dark"):
        qss = build_qss(theme)
        theme_tokens = tokens_for(theme)
        assert "QFrame#StepHeader" in qss
        assert f"background: {theme_tokens.surface_hover}" in qss
        assert "QWidget#WorkspaceBody" in qss
        assert f"background: {theme_tokens.paper_top}" in qss


def test_theme_qss_contains_shell_selectors():
    for theme in ("light", "dark"):
        qss = build_qss(theme)
        for selector in (
            "QFrame#ContentSheet",
            "QFrame#StatusLine",
            "QLabel#StatusText",
            "QLabel#StatusMeta",
            "QFrame#RunLogPanel",
            "QPlainTextEdit#RunLogView",
        ):
            assert selector in qss, selector


# ---------------------------------------------------------------------------
# 按钮辨识度与标题字重（真人反馈修复）
# ---------------------------------------------------------------------------


def _rule_block(qss: str, selector: str) -> str:
    marker = f"{selector} {{"
    start = qss.index(marker) + len(marker)
    end = qss.index("}", start)
    return qss[start:end]


def test_ghost_button_has_visible_border_and_focus_ring():
    """GhostButton 不能再是透明底的裸文字；焦点态要有自己的 clay 描边。"""
    for theme in ("light", "dark"):
        qss = build_qss(theme)
        t = tokens_for(theme)
        body = _rule_block(qss, "QPushButton#GhostButton")
        assert f"border: 1px solid {t.line}" in body
        assert f"background: {t.surface}" in body
        assert "transparent" not in body
        assert "border-color: transparent" not in body

        focus = _rule_block(qss, "QPushButton#GhostButton:focus")
        assert f"border: 1px solid {t.clay}" in focus

        hover = _rule_block(qss, "QPushButton#GhostButton:hover")
        assert f"background: {t.surface_hover}" in hover

        advanced = _rule_block(qss, "QToolButton#ObsAdvancedButton")
        assert "1px solid" in advanced, "折叠按钮没有描边"


def test_title_font_tokens_and_qss():
    from adofai_publisher.ui.design_tokens import TITLE_FAMILY, TITLE_WEIGHT

    assert "Microsoft YaHei" in TITLE_FAMILY
    assert metrics().title_weight == TITLE_WEIGHT >= 700

    title_selectors = (
        "QLabel#PageTitle",
        "QLabel#CardTitle",
        "QLabel#HomeGreeting",
        "QLabel#StatValue",
        "QLabel#AppLogo",
    )
    for theme in ("light", "dark"):
        qss = build_qss(theme)
        for selector in title_selectors:
            body = _rule_block(qss, selector)
            assert f"font-family: {TITLE_FAMILY}" in body, selector
            assert f"font-weight: {metrics().title_weight}" in body, selector
        # 正文小字保持原样：不加标题字重，也不换字族。
        hint = _rule_block(qss, "QLabel#CardHint")
        assert "font-weight" not in hint
        assert "Georgia" not in qss


def test_umm_page_folds_long_notes(tmp_path, monkeypatch):
    """UMM / Bridge / 热键的长说明默认收起，界面只留状态与按钮。"""
    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    from adofai_publisher.ui.umm_page import UmmPage
    from adofai_publisher.ui.widgets import Disclosure

    page = UmmPage(Settings(tmp_path / "settings.json"))
    try:
        notes = page.findChildren(Disclosure)
        titles = [note.button.text() for note in notes]
        assert len(notes) >= 3, titles
        assert any("安装原理" in title for title in titles)
        assert any("Bridge" in title for title in titles)
        assert any("热键" in title for title in titles)
        assert all(not note.is_expanded() for note in notes)
        assert all(note.text_label.isHidden() for note in notes)

        # 没有更新动作时状态行不占位；热键临时提示也默认不显示。
        assert page.update_status.isHidden()
        assert page.hotkey_hint.isHidden()

        notes[0].button.click()
        app.processEvents()
        assert notes[0].is_expanded()
        assert not notes[0].text_label.isHidden()
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()
