"""交互层回归测试：光标、语义 tone、Toast 队列、步骤条键盘与勾选绘制。"""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QPointF, QStringListModel, Qt
from PySide6.QtGui import QColor, QImage, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from adofai_publisher.ui import widgets
from adofai_publisher.ui.interactions import (
    animations_enabled,
    install_interactions,
    nearest_scroll_area,
    set_tone,
    uninstall_interactions,
)
from adofai_publisher.ui.theme import active_palette, palette
from adofai_publisher.ui.widgets import (
    CoverPicker,
    Disclosure,
    StepBar,
    ThemedCheckBox,
    Toast,
    ToastManager,
    _qcolor,
)


def teardown_module(module):
    """本模块验证完就卸载全局过滤器，避免拖慢后续无关的 UI 测试。"""
    uninstall_interactions(QApplication.instance())


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    install_interactions(application)
    return application


@pytest.fixture()
def host(app):
    widget = QWidget()
    widget.resize(480, 320)
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    app.processEvents()


class _FakeSignal:
    """最小信号对象：只覆盖 Toast 定时器用到的 ``connect`` / ``emit``。"""

    def __init__(self) -> None:
        self._slots: list = []

    def connect(self, slot) -> None:
        self._slots.append(slot)

    def emit(self, *args) -> None:
        for slot in list(self._slots):
            slot(*args)


class _FakeTimer:
    """可手动推进的假定时器，实现 Toast 用到的 QTimer 子集。

    真实 QTimer 按墙钟时间到点触发 timeout；这里由测试调用 ``advance``
    显式推进时间，断言“推进 N 毫秒后行为发生”，结果与机器快慢无关。
    """

    def __init__(self) -> None:
        self.timeout = _FakeSignal()
        self._active = False
        self._remaining_ms = 0
        self._single_shot = False

    def setSingleShot(self, value: bool) -> None:
        self._single_shot = bool(value)

    def start(self, interval: int) -> None:
        self._remaining_ms = int(interval)
        self._active = True

    def stop(self) -> None:
        self._active = False

    def isActive(self) -> bool:
        return self._active

    def remaining_ms(self) -> int:
        return self._remaining_ms

    def advance(self, ms: int) -> None:
        """推进 ``ms`` 毫秒；单次定时器到点后触发 timeout 并停止。"""
        assert ms >= 0
        if not self._active:
            return
        if ms < self._remaining_ms:
            self._remaining_ms -= ms
            return
        self._active = False
        self._remaining_ms = 0
        self.timeout.emit()


@pytest.fixture()
def fake_toast_timer():
    """把 Toast 的倒计时换成假时钟；测试结束自动恢复真实 QTimer。"""
    timer = _FakeTimer()
    widgets.set_timer_factory_for_tests(lambda parent=None: timer)
    try:
        yield timer
    finally:
        widgets.set_timer_factory_for_tests(None)


def _finish_animation(animation) -> None:
    """把 QPropertyAnimation 直接推到终点。

    ``setCurrentTime(duration)`` 会同步算出终值并发出 ``finished``，
    因此淡入淡出不用等真实时间，也不再依赖动画帧率。
    """
    assert animation is not None
    animation.setCurrentTime(animation.duration())


def _color_pixels(widget, color: QColor) -> int:
    image = widget.grab().toImage()
    target = color.rgb() & 0xFFFFFF
    return sum(
        1
        for x in range(image.width())
        for y in range(image.height())
        if (image.pixelColor(x, y).rgb() & 0xFFFFFF) == target
    )


# ---------------------------------------------------------------------------
# interactions.py
# ---------------------------------------------------------------------------


def test_install_interactions_is_idempotent(app):
    assert install_interactions(app) is install_interactions(app)


def test_pointer_cursor_for_buttons_and_combos(app, host):
    button = QPushButton("ok", host)
    combo = QComboBox(host)
    checkbox = QCheckBox("check", host)
    line = QLineEdit(host)
    host.show()
    app.processEvents()

    expected = Qt.CursorShape.PointingHandCursor
    assert button.cursor().shape() == expected
    assert combo.cursor().shape() == expected
    assert checkbox.cursor().shape() == expected
    # 输入框保持默认的 I 型光标，不改成手型。
    assert line.cursor().shape() != expected


def test_pointer_cursor_follows_enabled_state(app, host):
    button = QPushButton("ok", host)
    app.processEvents()
    assert button.cursor().shape() == Qt.CursorShape.PointingHandCursor

    button.setEnabled(False)
    app.processEvents()
    assert button.cursor().shape() == Qt.CursorShape.ArrowCursor

    button.setEnabled(True)
    app.processEvents()
    assert button.cursor().shape() == Qt.CursorShape.PointingHandCursor


def test_pointer_cursor_respects_no_pointer_and_explicit(app, host):
    plain = QPushButton("plain", host)
    plain.setProperty("noPointer", True)
    plain.show()
    app.processEvents()
    assert plain.cursor().shape() == Qt.CursorShape.ArrowCursor

    explicit = QPushButton("explicit", host)
    explicit.setCursor(Qt.CursorShape.CrossCursor)
    explicit.setProperty("pointerExplicit", True)
    explicit.show()
    app.processEvents()
    assert explicit.cursor().shape() == Qt.CursorShape.CrossCursor


def test_set_tone_writes_property_and_clears(app):
    label = QLabel("状态")
    set_tone(label, "warn")
    assert label.property("tone") == "warn"
    set_tone(label, "danger")
    assert label.property("tone") == "bad"
    set_tone(label, None)
    assert label.property("tone") is None


def test_animations_enabled_switches(monkeypatch):
    monkeypatch.delenv("ADOFAI_REDUCE_MOTION", raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    assert animations_enabled() is False
    monkeypatch.setenv("ADOFAI_REDUCE_MOTION", "1")
    assert animations_enabled() is False
    monkeypatch.delenv("ADOFAI_REDUCE_MOTION")
    monkeypatch.setenv("QT_QPA_PLATFORM", "windows")
    assert animations_enabled() is True


# ---------------------------------------------------------------------------
# 滚轮防误操作
# ---------------------------------------------------------------------------


def _send_wheel(widget: QWidget, delta: int = -120) -> None:
    center = widget.rect().center()
    event = QWheelEvent(
        QPointF(center),
        QPointF(widget.mapToGlobal(center)),
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)


def _scroll_host(app, factory):
    """把控件放进一个可纵向滚动的页面，返回 (滚动区, 控件)。"""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.resize(360, 180)
    content = QWidget()
    content.setMinimumHeight(900)
    box = QVBoxLayout(content)
    widget = factory()
    box.addWidget(widget)
    box.addStretch(1)
    area.setWidget(content)
    area.show()
    app.processEvents()
    return area, widget


def test_wheel_over_unfocused_combo_scrolls_page_not_options(app):
    def factory():
        combo = QComboBox()
        combo.addItems(["A", "B", "C"])
        combo.setCurrentIndex(1)
        return combo

    area, combo = _scroll_host(app, factory)
    bar = area.verticalScrollBar()
    bar.setValue(0)
    combo.clearFocus()
    try:
        _send_wheel(combo, -120)
        app.processEvents()
        assert combo.currentIndex() == 1, "无焦点时滚轮改了下拉框选项"
        assert bar.value() > 0, "页面没有跟着滚动"
    finally:
        area.close()
        area.deleteLater()
        app.processEvents()


def test_wheel_over_unfocused_spin_scrolls_page_not_value(app):
    def factory():
        spin = QSpinBox()
        spin.setRange(0, 999)
        spin.setValue(120)
        return spin

    area, spin = _scroll_host(app, factory)
    bar = area.verticalScrollBar()
    bar.setValue(0)
    spin.clearFocus()
    try:
        _send_wheel(spin, -120)
        app.processEvents()
        assert spin.value() == 120, "无焦点时滚轮改了数值框"
        assert bar.value() > 0, "页面没有跟着滚动"
    finally:
        area.close()
        area.deleteLater()
        app.processEvents()


def test_wheel_over_focused_spin_still_scrolls_page_not_value(app, monkeypatch):
    """DC 定的规则：点过/已聚焦的数值框也不能被滚轮改值。"""
    area, spin = _scroll_host(app, lambda: QSpinBox())
    spin.setRange(0, 999)
    spin.setValue(120)
    bar = area.verticalScrollBar()
    bar.setValue(0)
    # 离屏窗口拿不到系统焦点，这里只模拟“用户刚点过它”。
    monkeypatch.setattr(spin, "hasFocus", lambda: True)
    try:
        _send_wheel(spin, -120)
        app.processEvents()
        assert spin.value() == 120, "获得焦点后滚轮仍改了数值框"
        assert bar.value() > 0, "聚焦的数值框没有把滚轮交给页面"
    finally:
        area.close()
        area.deleteLater()
        app.processEvents()


def test_wheel_over_focused_combo_still_scrolls_page_not_options(app, monkeypatch):
    def factory():
        combo = QComboBox()
        combo.addItems(["A", "B", "C"])
        combo.setCurrentIndex(1)
        return combo

    area, combo = _scroll_host(app, factory)
    bar = area.verticalScrollBar()
    bar.setValue(0)
    monkeypatch.setattr(combo, "hasFocus", lambda: True)
    center = combo.rect().center()
    event = QWheelEvent(
        QPointF(center),
        QPointF(combo.mapToGlobal(center)),
        QPoint(0, 0),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    pointer_filter = install_interactions(app)
    try:
        assert pointer_filter.guard_wheel(combo, event) is True
        _send_wheel(combo, -120)
        app.processEvents()
        assert combo.currentIndex() == 1, "聚焦后滚轮改了下拉框选项"
        assert bar.value() > 0, "聚焦的下拉框没有把滚轮交给页面"
    finally:
        area.close()
        area.deleteLater()
        app.processEvents()


def test_wheel_over_slider_keeps_value_and_scrolls_page(app):
    def factory():
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(40)
        return slider

    area, slider = _scroll_host(app, factory)
    bar = area.verticalScrollBar()
    bar.setValue(0)
    slider.clearFocus()
    try:
        _send_wheel(slider, -120)
        app.processEvents()
        assert slider.value() == 40, "滚轮改了滑条数值"
        assert bar.value() > 0, "滑条上的滚轮没有交给页面"
    finally:
        area.close()
        area.deleteLater()
        app.processEvents()


def test_wheel_over_tab_bar_keeps_current_tab(app):
    host = QWidget()
    host.resize(360, 160)
    tabs = QTabBar(host)
    tabs.addTab("一")
    tabs.addTab("二")
    tabs.addTab("三")
    tabs.setCurrentIndex(1)
    box = QVBoxLayout(host)
    box.addWidget(tabs)
    box.addStretch(1)
    host.show()
    app.processEvents()
    try:
        _send_wheel(tabs, -120)
        app.processEvents()
        assert tabs.currentIndex() == 1, "滚轮切换了标签页"
    finally:
        host.close()
        host.deleteLater()
        app.processEvents()


def test_wheel_in_dialog_without_scroll_area_is_swallowed(app):
    """弹窗里没有滚动区时，滚轮被吞掉，绝不落到控件上改值。"""
    dialog = QDialog()
    dialog.resize(320, 200)
    combo = QComboBox(dialog)
    combo.addItems(["A", "B", "C"])
    combo.setCurrentIndex(1)
    spin = QSpinBox(dialog)
    spin.setRange(0, 999)
    spin.setValue(120)
    box = QVBoxLayout(dialog)
    box.addWidget(combo)
    box.addWidget(spin)
    box.addStretch(1)
    dialog.show()
    app.processEvents()
    try:
        _send_wheel(combo, -120)
        _send_wheel(spin, -120)
        app.processEvents()
        assert combo.currentIndex() == 1, "弹窗里滚轮改了下拉框选项"
        assert spin.value() == 120, "弹窗里滚轮改了数值框"
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_nearest_scroll_area_accepts_list_view_and_table(app):
    """列表视图也是滚动区：表格 / 列表里的控件应该把滚轮交给它。"""
    host = QWidget()
    view = QListView(host)
    child = QWidget(view)
    assert nearest_scroll_area(child) is view
    host.deleteLater()
    app.processEvents()


def test_wheel_over_combo_scrolls_sibling_list(app):
    """谱面库没有页面级滚动区：下拉框旁边的列表要吃到滚轮。"""
    host = QWidget()
    host.resize(360, 240)
    combo = QComboBox(host)
    combo.addItems(["A", "B", "C"])
    view = QListView(host)
    view.setModel(QStringListModel([f"item {index}" for index in range(120)]))
    view.setMinimumHeight(160)
    box = QVBoxLayout(host)
    box.addWidget(combo)
    box.addWidget(view, 1)
    host.show()
    app.processEvents()
    combo.clearFocus()
    bar = view.verticalScrollBar()
    bar.setValue(0)
    assert bar.maximum() > 0
    try:
        _send_wheel(combo, -120)
        app.processEvents()
        assert combo.currentIndex() == 0, "无焦点时滚轮改了下拉框选项"
        assert bar.value() > 0, "滚轮没有交给旁边的列表"
    finally:
        host.close()
        host.deleteLater()
        app.processEvents()


def test_wheel_outside_scroll_area_is_swallowed(app):
    host = QWidget()
    host.resize(320, 200)
    combo = QComboBox(host)
    combo.addItems(["A", "B", "C"])
    combo.setCurrentIndex(1)
    host.show()
    app.processEvents()
    combo.clearFocus()
    try:
        _send_wheel(combo, -120)
        app.processEvents()
        assert combo.currentIndex() == 1
    finally:
        host.close()
        host.deleteLater()
        app.processEvents()


# ---------------------------------------------------------------------------
# CoverPicker 双槽骨架
# ---------------------------------------------------------------------------


def _make_image(path, width: int, height: int) -> str:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#B85735"))
    assert image.save(str(path))
    return str(path)


def test_cover_picker_keeps_two_slots_and_syncs_cropped_image(app, tmp_path):
    primary = _make_image(tmp_path / "primary.png", 400, 200)
    picker = CoverPicker()
    picker.resize(360, 380)
    picker.show()
    app.processEvents()
    try:
        seen: list[str] = []
        picker.fileSelected.connect(seen.append)
        picker.set_file(primary)
        assert seen == [primary]
        assert picker.active_slot == CoverPicker.SLOT_PRIMARY
        assert picker.path_for("primary") == primary
        # 主槽显示自己的图，等比完整显示（400×200 → 2:1），不居中裁剪。
        assert not picker._slot_image().isNull()
        shown = picker.preview.pixmap()
        assert not shown.isNull()
        assert abs(shown.width() / shown.height() - 2.0) < 0.02
        assert picker.slot_buttons[CoverPicker.SLOT_PRIMARY].text().endswith("✓")
        assert "✓" not in picker.slot_buttons[CoverPicker.SLOT_ALTERNATE].text()
        primary_preview_height = picker.preview.minimumHeight()

        synced = picker.sync_image()
        assert not synced.isNull()
        # 主图是 16:9，副图按 DC 定的 4:3 裁剪，且只裁不缩放。
        assert abs(synced.width() / synced.height() - 4 / 3) < 0.02
        assert synced.height() == 200, "裁剪不应缩放源图"

        calls: list[tuple[str, QImage]] = []
        picker.set_sync_callback(lambda key, image: calls.append((key, image)))
        picker.set_file(primary)
        assert calls and calls[-1][0] == CoverPicker.SLOT_ALTERNATE
        assert not calls[-1][1].isNull()

        changes: list[str] = []
        picker.slotChanged.connect(changes.append)
        picker.set_active_slot(CoverPicker.SLOT_ALTERNATE)
        app.processEvents()
        assert changes == ["alternate"]
        assert picker.path_for("alternate") == ""
        # 副槽没有自己的图：预览是空状态，不再拿主图的裁剪版顶替。
        assert picker._slot_image().isNull()
        assert picker.preview.pixmap().isNull()
        assert "尚未单独上传 4:3 副图" in picker.preview.text()
        assert "用这张同步 4:3" in picker.preview.text()
        assert picker.hint.text() == "投稿时会用主图裁剪版补 4:3"
        # 预览框固定按 4:3 撑开：切换槽位高度不跳，两种比例都能完整放下，
        # 而且比过去的 96px 明显加大。
        assert picker.preview.minimumHeight() == primary_preview_height
        assert picker.preview.minimumHeight() >= 140
        assert (
            abs(
                picker.preview.minimumHeight()
                - round(picker.preview.width() * 3 / 4)
            )
            <= 2
        )

        alt = _make_image(tmp_path / "alt.png", 320, 240)
        picker.set_file(alt)
        assert picker.path_for("alternate") == alt
        assert picker.path_for("primary") == primary
        assert not picker._slot_image().isNull()
        assert "✓" in picker.slot_buttons[CoverPicker.SLOT_ALTERNATE].text()
        assert picker.hint.text() == "alt.png"

        picker.set_active_slot(CoverPicker.SLOT_PRIMARY)
        picker.clear()
        assert picker.path_for("primary") == ""
        assert picker.sync_image().isNull()
        assert "✓" not in picker.slot_buttons[CoverPicker.SLOT_PRIMARY].text()
        assert "✓" in picker.slot_buttons[CoverPicker.SLOT_ALTERNATE].text()

        # 主图已清掉；再清掉副槽自己的图 → 两个槽都空。
        picker.set_active_slot(CoverPicker.SLOT_ALTERNATE)
        app.processEvents()
        assert not picker.preview.pixmap().isNull()  # alt 自己还在
        picker.clear()
        assert picker.path_for("alternate") == ""
        assert picker.preview.pixmap().isNull()
        assert "先选 16:9 主图再同步" in picker.preview.text()
        assert picker.hint.text() == CoverPicker.SLOT_HINTS[CoverPicker.SLOT_ALTERNATE]
        assert "✓" not in picker.slot_buttons[CoverPicker.SLOT_ALTERNATE].text()

        picker.set_slot_bar_visible(True)
        assert not picker.slot_bar.isHidden()
    finally:
        picker.close()
        picker.deleteLater()
        app.processEvents()


def test_cover_picker_preview_shows_whole_image_without_cropping(app, tmp_path):
    """非槽位比例的图也要完整显示：pixmap 保持源图比例，不裁成预览框。"""
    tall = _make_image(tmp_path / "tall.png", 200, 300)
    picker = CoverPicker()
    picker.resize(360, 520)
    picker.show()
    app.processEvents()
    try:
        picker.set_file(tall)
        app.processEvents()
        shown = picker.preview.pixmap()
        assert not shown.isNull()
        assert abs(shown.width() / shown.height() - 200 / 300) < 0.03
        # 整张图都落在预览框内，没有被裁掉上下或左右。
        assert shown.width() <= picker.preview.width()
        assert shown.height() <= picker.preview.height()
    finally:
        picker.close()
        picker.deleteLater()
        app.processEvents()


def test_cover_picker_rejects_unreadable_image(app, tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    picker = CoverPicker()
    try:
        picker.set_file(str(bad))
        assert picker.path_for("primary") == ""
        assert "无法读取" in picker.preview.text()
    finally:
        picker.deleteLater()
        app.processEvents()


def test_cover_picker_save_sync_writes_4_3_png(app, tmp_path):
    primary = _make_image(tmp_path / "wide.png", 400, 200)
    picker = CoverPicker()
    try:
        picker.set_file(primary)
        path = picker.save_sync(tmp_path / "cover_sync")
        assert path and Path(path).is_file()
        saved = QImage(path)
        assert not saved.isNull()
        assert abs(saved.width() / saved.height() - 4 / 3) < 0.02
        assert saved.height() == 200
        assert "wide-4x3-" in Path(path).name
    finally:
        picker.deleteLater()
        app.processEvents()


def test_cover_picker_save_sync_without_primary_returns_empty(app, tmp_path):
    picker = CoverPicker()
    try:
        assert picker.save_sync(tmp_path / "cover_sync") == ""
    finally:
        picker.deleteLater()
        app.processEvents()


# ---------------------------------------------------------------------------
# Disclosure
# ---------------------------------------------------------------------------


def test_disclosure_starts_collapsed_and_toggles(app, host):
    note = Disclosure("安装原理", "一段很长的说明。", host)
    note.show()
    app.processEvents()
    assert not note.is_expanded()
    assert note.text_label.isHidden()

    note.button.click()
    app.processEvents()
    assert note.is_expanded()
    assert not note.text_label.isHidden()

    note.set_expanded(False)
    assert note.text_label.isHidden()


# ---------------------------------------------------------------------------
# Toast
# ---------------------------------------------------------------------------


def test_toast_normalizes_bad_to_error(app, host):
    toast = Toast(host)
    toast.show_message("boom", "bad", duration=60000)
    assert toast.property("level") == "error"
    assert toast.text() == "boom"
    assert toast.isVisible()
    toast.dismiss()

    toast.show_message("fine", "success", duration=60000)
    assert toast.property("level") == "ok"
    toast.dismiss()

    toast.show_message("hmm", "whatever", duration=60000)
    assert toast.property("level") == "info"
    toast.dismiss()


def test_toast_queue_limit_and_error_preemption(app, host):
    toast = Toast(host)
    toast.show_message("first", "info", duration=60000)
    for index in range(6):
        toast.show_message(f"m{index}", "info", duration=60000)

    assert Toast.MAX_QUEUE == 4
    assert len(toast._queue) == 4
    assert [entry["text"] for entry in toast._queue] == ["m2", "m3", "m4", "m5"]
    assert toast.text() == "first"

    toast.show_message("fatal", "error", duration=60000)
    assert toast.text() == "fatal"
    assert toast.property("level") == "error"

    toast.dismiss()
    assert toast.text() == "m2"
    toast.dismiss()


def test_toast_same_text_only_resets_timer(app, host):
    toast = Toast(host)
    toast.show_message("same", "info", duration=60000)

    calls: list[int] = []
    original_start = toast.timer.start

    def spy(interval: int) -> None:
        calls.append(interval)
        original_start(interval)

    toast.timer.start = spy
    toast.show_message("same", "info", duration=45000)

    assert calls == [45000]
    assert toast.timer.isActive()
    assert len(toast._queue) == 0
    toast.dismiss()


def test_toast_auto_hides_after_injected_clock(app, host, fake_toast_timer):
    """推进假时钟：差 1 毫秒仍可见，到点立即收起。"""
    toast = Toast(host)
    toast.show_message("auto", "info", duration=1200)

    assert toast.isVisible()
    assert fake_toast_timer.isActive()
    assert fake_toast_timer.remaining_ms() == 1200

    fake_toast_timer.advance(1199)
    assert toast.isVisible()
    assert fake_toast_timer.isActive()

    fake_toast_timer.advance(1)
    assert toast.isHidden()
    assert not fake_toast_timer.isActive()


def test_toast_action_button_stays_clickable(app, host):
    toast = Toast(host)
    clicked: list[bool] = []
    toast.show_message(
        "done",
        "ok",
        duration=60000,
        action_text="打开",
        action=lambda: clicked.append(True),
    )

    assert not toast.action_button.isHidden()
    assert toast.action_button.text() == "打开"
    toast.action_button.click()

    assert clicked == [True]
    assert toast.isHidden()


def test_toast_card_click_runs_action_and_dismisses(app, host):
    toast = Toast(host)
    clicked: list[str] = []
    toast.show_message(
        "发现新版本 v0.18.0，点击前往设置页更新",
        "warn",
        duration=60000,
        card_action=lambda: clicked.append("settings"),
    )

    assert toast.cursor().shape() == Qt.CursorShape.PointingHandCursor
    QTest.mouseClick(toast, Qt.MouseButton.LeftButton)

    assert clicked == ["settings"]
    assert toast.isHidden()


def test_toast_without_card_action_ignores_clicks(app, host):
    toast = Toast(host)
    toast.show_message("plain", "info", duration=60000)

    assert toast.cursor().shape() == Qt.CursorShape.ArrowCursor
    QTest.mouseClick(toast, Qt.MouseButton.LeftButton)

    assert toast.isVisible()
    toast.dismiss()


def test_toast_card_click_does_not_hijack_action_button(app, host):
    toast = Toast(host)
    button_clicks: list[bool] = []
    card_clicks: list[bool] = []
    toast.show_message(
        "both",
        "info",
        duration=60000,
        action_text="打开",
        action=lambda: button_clicks.append(True),
        card_action=lambda: card_clicks.append(True),
    )

    toast.action_button.click()

    assert button_clicks == [True]
    assert card_clicks == []


def test_toast_queue_entries_keep_their_own_card_action(app, host):
    toast = Toast(host)
    calls: list[str] = []
    toast.show_message(
        "first",
        "info",
        duration=60000,
        card_action=lambda: calls.append("first"),
    )
    toast.show_message(
        "second",
        "info",
        duration=60000,
        card_action=lambda: calls.append("second"),
    )

    assert toast.text() == "first"
    assert toast.cursor().shape() == Qt.CursorShape.PointingHandCursor
    toast.dismiss()
    assert toast.text() == "second"
    QTest.mouseClick(toast, Qt.MouseButton.LeftButton)

    assert calls == ["second"]


def test_toast_cursor_resets_when_next_entry_has_no_card_action(app, host):
    toast = Toast(host)
    toast.show_message(
        "with action", "info", duration=60000, card_action=lambda: None
    )
    toast.show_message("plain", "info", duration=60000)
    assert toast.cursor().shape() == Qt.CursorShape.PointingHandCursor

    toast.dismiss()

    assert toast.text() == "plain"
    assert toast.cursor().shape() == Qt.CursorShape.ArrowCursor
    toast.dismiss()


def test_toast_repositions_on_parent_resize(app, host):
    toast = Toast(host)
    toast.show_message("pos", "info", duration=60000)
    app.processEvents()

    host.resize(520, 360)
    app.processEvents()

    assert toast.x() == max(20, host.width() - toast.width() - Toast.MARGIN_RIGHT)
    assert toast.y() == max(
        20, host.height() - toast.height() - Toast.MARGIN_BOTTOM
    )
    toast.dismiss()


def test_toast_survives_parent_resize_that_hides(app):
    """MainWindow.resizeEvent 会 hide 一次提示，Toast 必须自己恢复可见。"""

    class HidingHost(QWidget):
        def resizeEvent(self, event):
            super().resizeEvent(event)
            toast = getattr(self, "toast", None)
            if toast is not None and toast.isVisible():
                toast.hide()

    window = HidingHost()
    window.resize(400, 300)
    window.show()
    app.processEvents()

    toast = Toast(window)
    window.toast = toast
    toast.show_message("stay", "info", duration=60000)
    app.processEvents()
    assert toast.isVisible()

    window.resize(520, 360)
    app.processEvents()

    assert toast.isVisible()
    assert toast.x() == max(20, window.width() - toast.width() - Toast.MARGIN_RIGHT)
    toast.dismiss()
    window.close()
    window.deleteLater()
    app.processEvents()


def test_toast_fades_in_and_out_when_animations_enabled(app, host, monkeypatch):
    monkeypatch.setattr(
        "adofai_publisher.ui.widgets.animations_enabled", lambda: True
    )
    toast = Toast(host)
    toast.show_message("fade", "ok", duration=60000)

    assert toast._effect is not None
    _finish_animation(toast.animation)
    assert toast._effect.opacity() > 0.9
    assert toast.isVisible()

    toast._fade_out()
    _finish_animation(toast.animation)
    assert toast.isHidden()


def test_toast_manager_caches_per_window(app):
    first = QWidget()
    first.resize(300, 200)
    first.show()
    second = QWidget()
    second.resize(300, 200)
    second.show()
    child = QWidget(first)

    toast_a = ToastManager.show(child, "a", "ok")
    toast_b = ToastManager.show(first, "b", "warn")
    toast_c = ToastManager.show(second, "c", "info")

    assert toast_a is toast_b
    assert toast_c is not toast_a
    assert toast_a.parentWidget() is first
    assert toast_c.parentWidget() is second

    for window in (first, second):
        window.close()
        window.deleteLater()
    app.processEvents()


# ---------------------------------------------------------------------------
# JudgementGrid
# ---------------------------------------------------------------------------


def test_judgement_grid_x_perfect_toggle_visibility_and_reset(app):
    from adofai_publisher.ui.widgets import JudgementGrid

    grid = JudgementGrid()
    grid.show()
    app.processEvents()
    try:
        # 默认 X 关闭：6 格，无 -完美 / 完美+
        assert grid.is_x_perfect() is False
        assert not grid.spins["perfectMinus"].isVisible()
        assert not grid.spins["perfectPlus"].isVisible()
        for key in ("ePerfect", "perfect", "lPerfect", "earlyDouble", "earlySingle", "lateSingle"):
            assert grid.spins[key].isVisible(), key

        grid.set_values({"perfectMinus": 4, "perfectPlus": 5, "lateDouble": 7})
        assert grid.values()["perfectMinus"] == 0
        assert grid.values()["perfectPlus"] == 0

        grid.set_x_perfect(True)
        app.processEvents()
        assert grid.is_x_perfect() is True
        assert grid.spins["perfectMinus"].isVisible()
        assert grid.spins["perfectPlus"].isVisible()

        grid.spins["perfectMinus"].setText("4")
        grid.spins["perfectPlus"].setText("5")
        assert grid.values()["perfectMinus"] == 4

        grid.set_x_perfect(False)
        app.processEvents()
        assert not grid.spins["perfectMinus"].isVisible()
        assert grid.values()["perfectMinus"] == 0
        assert grid.values()["perfectPlus"] == 0
    finally:
        grid.close()
        grid.deleteLater()
        app.processEvents()


def test_judgement_grid_keeps_late_double_and_label_tones(app):
    from adofai_publisher.models import JUDGEMENT_KEYS, JUDGEMENT_LABELS
    from adofai_publisher.ui.widgets import JudgementGrid

    grid = JudgementGrid()
    try:
        assert JUDGEMENT_LABELS["ePerfect"] == "稍快"
        assert JUDGEMENT_LABELS["lPerfect"] == "稍慢"
        assert JUDGEMENT_LABELS["earlyDouble"] == "提前"
        assert JUDGEMENT_LABELS["lateDouble"] == "太慢！！"
        grid.set_values({"lateDouble": 9})
        values = grid.values()
        assert set(values) == set(JUDGEMENT_KEYS) == set(JUDGEMENT_LABELS)
        assert values["lateDouble"] == 9, "未显示的 lateDouble 也要保留"

        # TUF 配色：完美系 + 稍快/稍慢绿，偏差红；标签不再带 ×权重 文字。
        for key, (_cell, label, _spin) in grid._cells.items():
            assert "×" not in label.text()
            expected = "ok" if key in grid._GREEN_KEYS else "bad"
            assert label.property("tone") == expected, key
    finally:
        grid.deleteLater()
        app.processEvents()


def test_judgement_grid_counts_label_uses_two_tuf_measures(app):
    from adofai_publisher.ui.widgets import JudgementGrid

    grid = JudgementGrid()
    try:
        # DC 实测那组：九格和 1273，打击数 1273。
        grid.set_values(
            {
                "perfect": 1195,
                "ePerfect": 25,
                "lPerfect": 36,
                "earlySingle": 9,
                "lateSingle": 8,
            }
        )
        assert grid.counts_text() == (
            "判定总数（九格）：1273\n打击数（排除提前 / 太慢！！）：1273"
        )
        assert grid.counts_label.text() == grid.counts_text()

        # 填 1 个「提前」：九格和 +1，打击数不变。
        grid.spins["earlyDouble"].setText("1")
        assert grid.counts_text() == (
            "判定总数（九格）：1274\n打击数（排除提前 / 太慢！！）：1273"
        )

        # 隐藏的「太慢！！」也保留且计入九格和。
        grid.set_values(
            {
                "perfect": 10,
                "lateDouble": 2,
                "perfectMinus": 0,
                "perfectPlus": 0,
                "ePerfect": 0,
                "lPerfect": 0,
                "earlyDouble": 0,
                "earlySingle": 0,
                "lateSingle": 0,
            }
        )
        assert grid.counts_text() == (
            "判定总数（九格）：12\n打击数（排除提前 / 太慢！！）：10"
        )
        assert grid.values()["lateDouble"] == 2
    finally:
        grid.deleteLater()
        app.processEvents()


# ---------------------------------------------------------------------------
# StepBar
# ---------------------------------------------------------------------------


def test_step_bar_keyboard_emits_and_clamps(app, host):
    bar = StepBar(["发布方式", "选片", "谱面", "文案"], host)
    bar.resize(420, 66)
    bar.show()
    bar.setFocus()
    app.processEvents()

    seen: list[int] = []
    bar.stepClicked.connect(seen.append)
    bar.set_current(1)

    QTest.keyClick(bar, Qt.Key.Key_Right)
    assert seen == [2]
    QTest.keyClick(bar, Qt.Key.Key_Left)
    assert seen == [2, 0]

    bar.set_current(0)
    QTest.keyClick(bar, Qt.Key.Key_Left)  # 已在第一步，不越界
    assert seen == [2, 0]

    bar.set_current(3)
    QTest.keyClick(bar, Qt.Key.Key_Right)  # 已在最后一步，不越界
    assert seen == [2, 0]


def test_step_bar_hover_tracking_and_cursor(app, host):
    bar = StepBar(["一", "二"], host)
    bar.resize(400, 66)
    bar.show()
    app.processEvents()

    assert bar.hasMouseTracking()
    assert bar.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert bar.cursor().shape() == Qt.CursorShape.PointingHandCursor

    QTest.mouseMove(bar, bar.rect().center())
    app.processEvents()
    assert bar._hover == 0


def test_step_bar_focus_ring_hugs_circle_instead_of_whole_step(app, monkeypatch):
    """焦点不能画成覆盖整步的大虚线框（会压在下方内容卡上）。"""
    host = QWidget()
    host.setStyleSheet("background: #ffffff")
    host.resize(800, 66)
    bar = StepBar(["一", "二", "三", "四"], host)
    bar.setGeometry(0, 0, 800, 66)
    bar.set_current(1)
    # 离屏窗口不会被激活，hasFocus() 恒为 False；这里只强制走焦点绘制分支。
    monkeypatch.setattr(StepBar, "hasFocus", lambda self: True)
    host.show()
    app.processEvents()
    try:
        image = host.grab().toImage()
        slot = 800 / len(bar.steps)
        cx = slot * 1.5
        # 旧实现的虚线框顶边横跨整步（y≈2），这里取当前步左半段，避开圆心附近的细环。
        ink = 0
        for x in range(int(cx - slot / 2 + 12), int(cx - 40)):
            for y in (2, 3, 4):
                color = image.pixelColor(x, y)
                if min(color.red(), color.green(), color.blue()) < 235:
                    ink += 1
        assert ink <= 2, f"焦点仍在整步上画框：{ink} 个非背景像素"

        # 细环本身要画出来：圆心正上方（y < 7，尚未进入 15px 半径的实心圆）出现陶土色。
        accent = QColor(palette(bar._theme)["accent"])
        ring_pixels = [image.pixelColor(int(cx), y) for y in range(0, 6)]
        assert any(
            abs(color.red() - accent.red()) < 80
            and abs(color.green() - accent.green()) < 80
            and abs(color.blue() - accent.blue()) < 80
            for color in ring_pixels
        ), "当前步号外没有焦点细环"
    finally:
        host.close()
        host.deleteLater()
        app.processEvents()


def test_accent_soft_parses_to_valid_translucent_color():
    """palette 的 accent_soft 是 rgba(...)，QColor(str) 解析不了会画成黑色。"""
    for theme in ("light", "dark"):
        soft = _qcolor(palette(theme)["accent_soft"])
        assert soft.isValid()
        assert 0 < soft.alpha() < 255
        assert soft.red() or soft.green() or soft.blue()
        solid = _qcolor(palette(theme)["accent"])
        assert solid.isValid()
        assert solid.alpha() == 255


# ---------------------------------------------------------------------------
# 复选框勾选绘制
# ---------------------------------------------------------------------------


def test_themed_checkbox_paints_check_mark(app, host):
    box = ThemedCheckBox("选项", host)
    box.setChecked(True)
    box.resize(140, 28)
    box.show()
    app.processEvents()

    assert not box.grab().isNull()
    assert _color_pixels(box, QColor(active_palette()["selection_text"])) > 0


def test_plain_checkbox_gets_check_mark_from_filter(app, host):
    box = QCheckBox("选项", host)
    box.setChecked(True)
    box.resize(140, 28)
    box.show()
    app.processEvents()

    assert not box.grab().isNull()
    assert _color_pixels(box, QColor(active_palette()["selection_text"])) > 0
