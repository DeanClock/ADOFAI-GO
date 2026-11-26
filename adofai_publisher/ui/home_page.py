from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .chart_library import ChartItem
from .theme import metrics
from .widgets import Card


def _greeting() -> str:
    hour = datetime.now().hour
    if 5 <= hour < 12:
        return "早上好，欢迎回来"
    if 12 <= hour < 18:
        return "下午好，欢迎回来"
    return "晚上好，欢迎回来"


class HomePage(QWidget):
    queueRequested = Signal()
    analyzeRequested = Signal()
    browseTufRequested = Signal()
    importRequested = Signal()
    batchBindRequested = Signal()
    organizeRequested = Signal()
    ummRequested = Signal()
    launchGameRequested = Signal()
    openDirRequested = Signal()
    chartRequested = Signal(object)
    recordRequested = Signal(object)
    guideWorkspaceRequested = Signal()
    obsLaunchRequested = Signal()
    obsSettingsRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        m = metrics()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(m.page_pad_x, 8, m.page_pad_x, 18)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        layout = QVBoxLayout(inner)
        # 左右必须对称：旧实现的右侧多留 10px，滚动时内容整体偏左。
        layout.setContentsMargins(2, 4, 2, 12)
        layout.setSpacing(14)

        hero = QHBoxLayout()
        hero_text = QVBoxLayout()
        hero_text.setSpacing(3)
        greeting = QLabel(_greeting())
        greeting.setObjectName("HomeGreeting")
        subtitle = QLabel("ADOFAI GO · 找谱、整理、投稿，一个地方搞定。")
        subtitle.setObjectName("HomeSubtitle")
        hero_text.addWidget(greeting)
        hero_text.addWidget(subtitle)
        hero.addLayout(hero_text, 1)
        self.pending_chip = QLabel("待投稿 0 张")
        self.pending_chip.setObjectName("ChipWarn")
        hero.addWidget(self.pending_chip, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(hero)

        self.guide_card = Card(
            "快速上手ADOFAI GO基础功能",
            "先走最短流程：录制 → 工作台 → 文案与发布。其他功能以后再用也不迟。",
        )
        guide_steps = QHBoxLayout()
        guide_steps.setSpacing(8)
        guide_steps.addWidget(
            self._guide_step(
                "01",
                "录制",
                "直接用 OBS 手动录；也可以让工具启动 OBS，并在设置里配置联动。",
            ),
            1,
        )
        guide_steps.addWidget(
            self._guide_step(
                "02",
                "工作台",
                "先在「01 工作台」第 1 步选双投或仅 B 站，再拖视频、绑定谱面、填判定。",
            ),
            1,
        )
        guide_steps.addWidget(
            self._guide_step(
                "03",
                "文案与发布",
                "在「01 工作台」第 4 步填写文案并直接发布，也可先去「02 文案预览」核对。",
            ),
            1,
        )
        self.guide_card.body.addLayout(guide_steps)

        guide_note = QLabel(
            "第一次发布前，先在「06 设置」登录 B 站和 TUF。"
            "只想手动录制？不配置 OBS 也能正常用工作台。"
        )
        guide_note.setObjectName("CardHint")
        guide_note.setWordWrap(True)
        self.guide_card.body.addWidget(guide_note)

        guide_actions = QHBoxLayout()
        guide_actions.setSpacing(8)
        self.guide_workspace_button = QPushButton("去工作台")
        self.guide_workspace_button.setObjectName("PrimaryButton")
        self.guide_workspace_button.clicked.connect(
            lambda: self.guideWorkspaceRequested.emit()
        )
        self.guide_launch_button = QPushButton("打开 ADOFAI")
        self.guide_launch_button.setToolTip(
            "使用模组页保存的游戏目录启动游戏"
        )
        self.guide_launch_button.clicked.connect(
            lambda: self.launchGameRequested.emit()
        )
        self.guide_obs_launch_button = QPushButton("启动 OBS")
        self.guide_obs_launch_button.setToolTip(
            "OBS 未运行时会启动它，随后在设置页自动测试连接"
        )
        self.guide_obs_launch_button.clicked.connect(
            lambda: self.obsLaunchRequested.emit()
        )
        self.guide_obs_settings_button = QPushButton("OBS 设置")
        self.guide_obs_settings_button.setObjectName("GhostButton")
        self.guide_obs_settings_button.clicked.connect(
            lambda: self.obsSettingsRequested.emit()
        )
        guide_actions.addWidget(self.guide_workspace_button)
        guide_actions.addWidget(self.guide_launch_button)
        guide_actions.addWidget(self.guide_obs_launch_button)
        guide_actions.addWidget(self.guide_obs_settings_button)
        guide_actions.addStretch(1)
        self.guide_card.body.addLayout(guide_actions)
        layout.addWidget(self.guide_card)

        stats = QHBoxLayout()
        stats.setSpacing(10)
        self.stat_values: dict[str, QLabel] = {}
        self.stat_hints: dict[str, QLabel] = {}
        for key, title, hint in (
            ("local", "本地谱面", "导入与下载"),
            ("pending", "待投稿", "已匹配 TUF、未投稿"),
            ("submitted", "已投稿", "有投稿记录"),
            ("collections", "合集", "本地分类"),
        ):
            frame = QFrame()
            frame.setObjectName("StatCard")
            box = QVBoxLayout(frame)
            box.setContentsMargins(16, 13, 16, 13)
            box.setSpacing(1)
            value = QLabel("0")
            value.setObjectName("StatValue")
            label = QLabel(title)
            label.setObjectName("StatLabel")
            hint_label = QLabel(hint)
            hint_label.setObjectName("StatHint")
            box.addWidget(value)
            box.addWidget(label)
            box.addWidget(hint_label)
            self.stat_values[key] = value
            self.stat_hints[key] = hint_label
            stats.addWidget(frame, 1)
        layout.addLayout(stats)

        tools = Card("快捷工具", "不用来回翻侧边栏，常用入口都在这里。")
        tool_grid = QGridLayout()
        tool_grid.setHorizontalSpacing(8)
        tool_grid.setVerticalSpacing(8)
        self.queue_tool = self._tool_button("待投稿队列", "查看已匹配但还没投稿的谱面")
        self.queue_tool.clicked.connect(lambda: self.queueRequested.emit())
        self.analyze_tool = self._tool_button(
            "谱面分析",
            "读取 .adofai / .ard / .zip / .adozip 的 BPM、物量和变速",
        )
        self.analyze_tool.clicked.connect(lambda: self.analyzeRequested.emit())
        self.launch_tool = self._tool_button(
            "启动 ADOFAI",
            "使用模组页保存的游戏目录启动游戏",
        )
        self.launch_tool.clicked.connect(lambda: self.launchGameRequested.emit())
        buttons = [
            self.queue_tool,
            self.analyze_tool,
            self._tool_button("在线找谱", "打开 TUF 在线浏览"),
            self._tool_button("导入本地目录", "扫描谱面文件与压缩包"),
            self._tool_button("批量绑定", "自动匹配 TUF 编号"),
            self._tool_button("磁盘整理", "按分类 / 难度 / 合集整理"),
            self._tool_button("模组管理", "安装 UMM 与 ADOFAI 相关 mod"),
            self.launch_tool,
            self._tool_button("打开谱面目录", "在资源管理器中打开"),
        ]
        buttons[2].clicked.connect(lambda: self.browseTufRequested.emit())
        buttons[3].clicked.connect(lambda: self.importRequested.emit())
        buttons[4].clicked.connect(lambda: self.batchBindRequested.emit())
        buttons[5].clicked.connect(lambda: self.organizeRequested.emit())
        buttons[6].clicked.connect(lambda: self.ummRequested.emit())
        buttons[8].clicked.connect(lambda: self.openDirRequested.emit())
        for index, button in enumerate(buttons):
            tool_grid.addWidget(button, index // 3, index % 3)
        for col in range(3):
            tool_grid.setColumnStretch(col, 1)
        tools.body.addLayout(tool_grid)
        layout.addWidget(tools)

        recent = QHBoxLayout()
        recent.setSpacing(12)

        records_card = Card("最近投稿", "双击载入到工作台。")
        self.recent_records = QListWidget()
        self.recent_records.setObjectName("HomeList")
        self.recent_records.setMinimumHeight(210)
        self.recent_records.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.recent_records.setWordWrap(True)
        self.recent_records.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.recent_records.itemDoubleClicked.connect(self._open_record)
        records_card.body.addWidget(self.recent_records, 1)
        recent.addWidget(records_card, 1)

        charts_card = Card("最近谱面", "双击查看详情。")
        self.recent_charts = QListWidget()
        self.recent_charts.setObjectName("HomeList")
        self.recent_charts.setMinimumHeight(210)
        self.recent_charts.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.recent_charts.setWordWrap(True)
        self.recent_charts.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.recent_charts.itemDoubleClicked.connect(self._open_chart)
        charts_card.body.addWidget(self.recent_charts, 1)
        recent.addWidget(charts_card, 1)

        layout.addLayout(recent)
        layout.addStretch(1)

    def _tool_button(self, text: str, tip: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("ToolTile")
        button.setToolTip(tip)
        button.setMinimumHeight(46)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    @staticmethod
    def _guide_step(number: str, title: str, text: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("GuideStep")
        box = QVBoxLayout(frame)
        box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(3)
        number_label = QLabel(number)
        number_label.setObjectName("GuideStepNumber")
        title_label = QLabel(title)
        title_label.setObjectName("GuideStepTitle")
        text_label = QLabel(text)
        text_label.setObjectName("GuideStepText")
        text_label.setWordWrap(True)
        box.addWidget(number_label)
        box.addWidget(title_label)
        box.addWidget(text_label)
        return frame

    def set_obs_busy(self, busy: bool) -> None:
        self.guide_obs_launch_button.setEnabled(not busy)

    def set_data(
        self,
        *,
        local_count: int,
        matched_count: int,
        pending_count: int,
        submitted_count: int,
        collection_count: int,
        recent_records: list[dict[str, Any]],
        recent_charts: list[ChartItem],
    ) -> None:
        self.stat_values["local"].setText(str(local_count))
        self.stat_hints["local"].setText(f"已匹配 {matched_count}")
        self.stat_values["pending"].setText(str(pending_count))
        self.stat_values["submitted"].setText(str(submitted_count))
        self.stat_values["collections"].setText(str(collection_count))
        self.pending_chip.setText(f"待投稿 {pending_count} 张")
        self.queue_tool.setText(
            f"待投稿队列 · {pending_count}" if pending_count else "待投稿队列"
        )

        self.recent_records.clear()
        if recent_records:
            for record in recent_records[:5]:
                title = str(record.get("title") or record.get("song") or "未命名投稿")
                created = str(record.get("created_at") or "")[:16].replace("T", " ")
                bvid = str(record.get("bvid") or "")
                text = f"{created}  ·  {title}" + (f"  ·  {bvid}" if bvid else "")
                item = QListWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, record)
                item.setToolTip(title)
                self.recent_records.addItem(item)
        else:
            self._empty_item(self.recent_records, "还没有投稿记录")

        self.recent_charts.clear()
        if recent_charts:
            for chart in recent_charts[:5]:
                level = chart.level
                badge = level.difficulty or "未评级"
                tuf = f"#{level.tuf_id}" if level.tuf_id else "未匹配"
                collection = str((chart.entry or {}).get("collection") or "")
                text = f"{level.song or '(未命名谱面)'}  ·  {badge}  ·  {tuf}"
                if collection:
                    text += f"  ·  {collection}"
                item = QListWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, chart)
                item.setToolTip(chart.local_path)
                self.recent_charts.addItem(item)
        else:
            self._empty_item(self.recent_charts, "本地谱面库还是空的")

    @staticmethod
    def _empty_item(view: QListWidget, text: str) -> None:
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        view.addItem(item)

    def _open_record(self, item: QListWidgetItem) -> None:
        record = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(record, dict):
            self.recordRequested.emit(record)

    def _open_chart(self, item: QListWidgetItem) -> None:
        chart = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(chart, ChartItem):
            self.chartRequested.emit(chart)
