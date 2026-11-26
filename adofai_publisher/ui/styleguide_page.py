"""隐藏的控件样张页：一屏看完主题控件状态。

入口：``python run.py --page styleguide``。不进入侧边栏导航，按需懒创建。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..compliance import Issue
from .shell import StatusLine
from .theme import metrics
from .widgets import Card, ChoiceCombo, IssueList, StepBar, Toast, chip


def _section(title: str, hint: str = "") -> tuple[Card, QVBoxLayout]:
    card = Card(title, hint)
    return card, card.body


class StyleGuidePage(QWidget):
    """控件样张：按钮四态、表单、卡片、Chip、Issue、步骤条、Toast、状态行。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StyleGuidePage")
        self._toast: Toast | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(metrics().page_pad_x, 18, metrics().page_pad_x, 24)
        layout.setSpacing(14)
        area.setWidget(content)
        outer.addWidget(area, 1)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)
        layout.addLayout(grid)

        grid.addWidget(self._buttons_card(), 0, 0)
        grid.addWidget(self._form_card(), 0, 1)
        grid.addWidget(self._chips_card(), 1, 0)
        grid.addWidget(self._issues_card(), 1, 1)
        grid.addWidget(self._step_card(), 2, 0)
        grid.addWidget(self._toast_card(), 2, 1)
        grid.addWidget(self._status_card(), 3, 0, 1, 2)
        layout.addStretch(1)

    # -- 各个样张 ---------------------------------------------------------

    def _buttons_card(self) -> Card:
        card, body = _section(
            "按钮",
            "主操作 / 次操作 / 危险 / 禁用 / 忙碌态。",
        )
        row = QHBoxLayout()
        row.setSpacing(10)
        primary = QPushButton("主操作")
        primary.setObjectName("PrimaryButton")
        ghost = QPushButton("次操作")
        ghost.setObjectName("GhostButton")
        danger = QPushButton("危险操作")
        danger.setObjectName("DangerButton")
        disabled = QPushButton("禁用")
        disabled.setEnabled(False)
        busy = QPushButton("处理中…")
        busy.setObjectName("PrimaryButton")
        busy.setProperty("busy", True)
        busy.setEnabled(False)
        for button in (primary, ghost, danger, disabled, busy):
            row.addWidget(button)
        row.addStretch(1)
        body.addLayout(row)
        return card

    def _form_card(self) -> Card:
        card, body = _section(
            "表单",
            "输入框、下拉框、数值框与复选框。",
        )
        row = QHBoxLayout()
        row.setSpacing(10)
        line = QLineEdit()
        line.setPlaceholderText("输入一些文字…")
        combo = ChoiceCombo()
        combo.addItems(["选项 A", "选项 B", "选项 C"])
        spin = QSpinBox()
        spin.setRange(0, 999)
        spin.setValue(120)
        row.addWidget(line, 2)
        row.addWidget(combo, 1)
        row.addWidget(spin, 0)
        body.addLayout(row)

        checks = QHBoxLayout()
        checks.setSpacing(16)
        checked = QCheckBox("已勾选")
        checked.setChecked(True)
        unchecked = QCheckBox("未勾选")
        disabled = QCheckBox("禁用")
        disabled.setEnabled(False)
        checks.addWidget(checked)
        checks.addWidget(unchecked)
        checks.addWidget(disabled)
        checks.addStretch(1)
        body.addLayout(checks)
        return card

    def _chips_card(self) -> Card:
        card, body = _section(
            "卡片与 Chip",
            "语义标签与卡片容器。",
        )
        row = QHBoxLayout()
        row.setSpacing(8)
        for label in (
            chip("默认", "info"),
            chip("成功", "ok"),
            chip("警告", "warn"),
            chip("失败", "bad"),
        ):
            row.addWidget(label)
        row.addStretch(1)
        body.addLayout(row)
        note = QLabel(
            "卡片里的正文使用 Muted / Faint 两级文字，标题走微软雅黑 Bold。"
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        body.addWidget(note)
        return card

    def _issues_card(self) -> Card:
        card, body = _section(
            "IssueList",
            "合规检查结果的成功、警告与错误三态。",
        )
        issues = IssueList()
        issues.set_issues(
            [
                Issue("ok", "title", "标题长度合适。"),
                Issue("warn", "tags", "标签偏少，建议补到 6 个以上。"),
                Issue("error", "level", "还没有绑定 TUF 谱面。"),
            ]
        )
        body.addWidget(issues)
        return card

    def _step_card(self) -> Card:
        card, body = _section(
            "StepBar",
            "当前步骤、已完成步骤与 hover / 焦点反馈。",
        )
        bar = StepBar(["发布方式", "选片", "谱面 & 数据", "文案"])
        bar.set_current(1)
        bar.setMinimumWidth(360)
        self.step_bar = bar
        body.addWidget(bar)
        return card

    def _toast_card(self) -> Card:
        card, body = _section(
            "Toast",
            "四种级别的右下角提示；点击按钮触发。",
        )
        row = QHBoxLayout()
        row.setSpacing(8)
        samples = (
            ("信息", "info", "正在同步谱面库…"),
            ("成功", "ok", "已加入本地谱面库。"),
            ("警告", "warn", "有站点未登录，请先检查登录。"),
            ("错误", "error", "TUF 提交失败：网络超时。"),
        )
        for label, level, message in samples:
            button = QPushButton(label)
            button.setObjectName("GhostButton")
            button.clicked.connect(
                lambda _=False, lv=level, text=message: self._show_toast(text, lv)
            )
            row.addWidget(button)
        row.addStretch(1)
        body.addLayout(row)
        return card

    def _status_card(self) -> Card:
        card, body = _section(
            "状态行",
            "底部状态行示例：左侧任务状态、右侧版本与运行日志入口。",
        )
        self.status_line = StatusLine()
        self.status_line.show_message("示例状态：正在准备谱面数据…")
        body.addWidget(self.status_line)
        row = QHBoxLayout()
        busy = QPushButton("切换忙碌态")
        busy.setObjectName("GhostButton")
        busy.setCheckable(True)
        busy.toggled.connect(self.status_line.set_busy)
        row.addWidget(busy)
        row.addStretch(1)
        body.addLayout(row)
        return card

    # -- 行为 -------------------------------------------------------------

    def _show_toast(self, text: str, level: str) -> None:
        if self._toast is None:
            self._toast = Toast(self)
        self._toast.show_message(text, level, 4000)

    def refresh_theme(self) -> None:
        self.step_bar.refresh_theme()
        if self._toast is not None:
            self._toast.update()
        self.status_line.refresh_theme()
