from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..accuracy import calc_accuracy, format_accuracy
from ..ard_parser import ARDProfile
from ..compliance import TITLE_HARD_LIMIT, TITLE_WARN_LIMIT, check_draft
from ..config import Settings
from ..copy_snippets import (
    SNIPPET_GROUPS,
    default_copy_snippets,
    insert_snippet,
    normalize_copy_snippets,
)
from ..download_tasks import format_bytes
from ..models import (
    JUDGEMENT_FIELDS,
    Draft,
    LevelInfo,
    judgement_hit_count,
    judgement_sum,
)
from ..self_update import pick_exe_asset
from ..tuf_review import (
    REVIEW_CHIP_OBJECTS,
    normalize_review_status,
    review_text,
)
from ..wizard_state import generate_copy, make_tags
from .interactions import set_tone
from .theme import active_theme, palette
from .widgets import (
    AccuracyBadge,
    Card,
    ChoiceCombo,
    CoverPicker,
    DropZone,
    FlowLayout,
    IssueList,
    JudgementGrid,
    LevelPickerDialog,
    SearchSyntaxDialog,
    StepBar,
    chip,
    hline,
)

WORKSPACE_STEPS = 5

CREATIVE_DECLARATIONS = [
    "内容无需标注",
    "含AI生成内容",
    "含虚构演绎内容",
    "内容含营销信息",
    "个人观点，仅供参考",
    "内容为转载",
]


def _scroll_container(inner: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setWidget(inner)
    return area


def _label(text: str, object_name: str = "") -> QLabel:
    label = QLabel(text)
    if object_name:
        label.setObjectName(object_name)
    return label


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
            continue
        child = item.layout()
        if child is not None:
            _clear_layout(child)


def _format_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class WorkspacePage(QWidget):
    changed = Signal()
    lookupRequested = Signal(str)
    previewRequested = Signal()
    recentRequested = Signal(dict)
    uploadRequested = Signal()
    tufRequested = Signal()
    bvidCommitted = Signal(str)
    openBiliRequested = Signal()
    historyRequested = Signal()
    draftRestoreRequested = Signal()

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.draft = Draft()
        self.current_step = 0
        self._level: LevelInfo | None = None
        self._search_button: QPushButton | None = None
        self._search_status: QLabel | None = None
        self._auto_copy = False
        self._filling_copy = False
        self._snippet_buttons: list[QPushButton] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        # 步骤条独占一条「眉页」：带自己的表面色与底部 1px 分割线，
        # 与下方滚动区（纸色底）分开，卡片才能在滚动区里浮出来。
        self.step_header = QFrame()
        self.step_header.setObjectName("StepHeader")
        header_layout = QVBoxLayout(self.step_header)
        header_layout.setContentsMargins(28, 8, 28, 8)
        header_layout.setSpacing(0)
        self.step_bar = StepBar(
            ["发布方式", "选片", "谱面 & 数据", "文案", "检查与发布"]
        )
        self.step_bar.stepClicked.connect(self.go_step)
        header_layout.addWidget(self.step_bar)
        root.addWidget(self.step_header)

        self.stack_inner = QWidget()
        self.stack_inner.setObjectName("WorkspaceBody")
        self.stack_layout = QVBoxLayout(self.stack_inner)
        self.stack_layout.setContentsMargins(28, 12, 28, 8)
        self.stack_layout.setSpacing(14)
        self.stack_scroll = QScrollArea()
        self.stack_scroll.setObjectName("WorkspaceScroll")
        # 常用词条按钮是 NoFocus；若滚动区可点击聚焦，点词条时焦点会被抢到滚动区，
        # 插入时就判断不到当前正在编辑的输入框。
        self.stack_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.stack_scroll.setWidgetResizable(True)
        self.stack_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.stack_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.stack_scroll.setWidget(self.stack_inner)
        root.addWidget(self.stack_scroll, 1)

        self._build_step_one()
        self._build_step_two()
        self._build_step_three()
        self._build_step_four()
        self._build_step_five()
        self.set_publish_mode(self._publish_mode(), persist=False)

        bottom = QWidget()
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(28, 8, 28, 18)
        bottom_layout.addStretch(1)
        self.back_button = QPushButton("上一步")
        self.back_button.clicked.connect(self.previous_step)
        self.next_button = QPushButton("下一步")
        self.next_button.setObjectName("PrimaryButton")
        self.next_button.clicked.connect(self.next_step)
        bottom_layout.addWidget(self.back_button)
        bottom_layout.addWidget(self.next_button)
        root.addWidget(bottom)
        self._show_step(0)

    def _build_step_one(self) -> None:
        self.step_one = QWidget()
        layout = QVBoxLayout(self.step_one)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(14)

        columns = QHBoxLayout()
        columns.setSpacing(14)

        mode_card = Card(
            "第一步 · 发布方式",
            "先决定这次只发 B 站，还是 B 站与 TUF 双投；选择会立即保存。",
        )
        mode_row = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_dual_button = QPushButton("双投（B 站 + TUF）")
        self.mode_bili_button = QPushButton("仅 B 站")
        for button, mode in (
            (self.mode_dual_button, "dual"),
            (self.mode_bili_button, "bili_only"),
        ):
            button.setObjectName("SegmentButton")
            button.setCheckable(True)
            button.clicked.connect(
                lambda _=False, selected=mode: self._set_publish_mode(selected)
            )
            self.mode_group.addButton(button)
            mode_row.addWidget(button)
        mode_row.addStretch(1)
        mode_card.body.addLayout(mode_row)
        for hint in (
            "双投：绑定 TUF 谱面、填判定与体感难度，拿到 BV 号后自动提交记录。",
            "仅 B 站：只上传视频，不校验判定与物量。",
        ):
            hint_label = _label(hint, "Muted")
            hint_label.setWordWrap(True)
            mode_card.body.addWidget(hint_label)
        mode_card.body.addStretch(1)
        columns.addWidget(mode_card, 3)

        side_card = Card("接着做", "恢复上次没填完的草稿，或翻看以前的投稿。")
        self.resume_draft_button = QPushButton("继续上次草稿")
        self.resume_draft_button.setObjectName("GhostButton")
        self.resume_draft_button.clicked.connect(self.draftRestoreRequested)
        side_card.body.addWidget(self.resume_draft_button)
        self.recent_entries_button = QPushButton("最近投稿")
        self.recent_entries_button.setObjectName("GhostButton")
        self.recent_entries_button.clicked.connect(self.historyRequested)
        side_card.body.addWidget(self.recent_entries_button)
        shortcut_tips = FlowLayout(h_spacing=8, v_spacing=8)
        for text in ("Ctrl+N 新建", "Ctrl+J 提交", "Ctrl+0~6 切换页面"):
            shortcut_tips.addWidget(chip(text))
        side_card.body.addLayout(shortcut_tips)
        side_card.body.addStretch(1)
        columns.addWidget(side_card, 2)

        layout.addLayout(columns)
        self.stack_layout.addWidget(self.step_one)

    def _build_step_two(self) -> None:
        self.step_two = QWidget()
        layout = QVBoxLayout(self.step_two)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(14)
        media_row = QHBoxLayout()
        media_row.setSpacing(14)

        card = Card("第二步 · 选择通关视频", "视频只在本机读取，工具不会上传到任何第三方服务器。")
        self.drop_zone = DropZone()
        # 右侧封面卡片放大预览后会更高：让投递区吸收多出来的高度，
        # 卡片标题与副标题仍贴顶，不会被拉开。
        self.drop_zone.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        self.drop_zone.fileSelected.connect(self._on_video_selected)
        card.body.addWidget(self.drop_zone)
        # 提示 chip 用 FlowLayout：窄窗口下换行，不把卡片最小宽度顶宽。
        tips = FlowLayout(h_spacing=10, v_spacing=8)
        for text, level in [
            ("MP4 / FLV / MOV 推荐", "ok"),
            ("建议 1080P 60fps", "info"),
            ("文件名可乱，标题由你定", "info"),
        ]:
            tips.addWidget(chip(text, level))
        card.body.addLayout(tips)
        media_row.addWidget(card, 3)

        cover_card = Card(
            "封面（可选）",
            "16:9 主图进「个人空间封面」，4:3 副图进「首页推荐封面」；"
            "可分别上传，也可用主图一键裁剪；只传主图时投稿会用裁剪版补 4:3；"
            "都不选则由 B 站从视频帧自动生成。",
        )
        self.cover_picker = CoverPicker()
        self.cover_picker.set_slot_bar_visible(True)
        self.cover_picker.fileSelected.connect(self._on_cover_selected)
        self.cover_picker.slotChanged.connect(self._on_cover_slot_changed)
        cover_card.body.addWidget(self.cover_picker)
        # 两个操作按钮用 FlowLayout：窄窗口下换行，不把封面卡片的最小宽度顶宽。
        cover_actions = FlowLayout(h_spacing=8, v_spacing=6)
        self.cover_sync = QPushButton("用这张同步 4:3")
        self.cover_sync.setObjectName("GhostButton")
        self.cover_sync.setToolTip("用当前 16:9 主图居中裁剪，生成一张 4:3 副图")
        self.cover_sync.setEnabled(False)
        self.cover_sync.clicked.connect(self._sync_cover_ratio)
        cover_actions.addWidget(self.cover_sync)
        self.cover_clear = QPushButton("清除封面")
        self.cover_clear.setObjectName("GhostButton")
        self.cover_clear.setEnabled(False)
        self.cover_clear.setToolTip("清除当前选中的槽位")
        self.cover_clear.clicked.connect(self._clear_cover_slot)
        cover_actions.addWidget(self.cover_clear)
        cover_card.body.addLayout(cover_actions)
        self.cover_status = _label("", "Faint")
        self.cover_status.setWordWrap(True)
        cover_card.body.addWidget(self.cover_status)
        media_row.addWidget(cover_card, 2)
        layout.addLayout(media_row)

        self.recent_card = Card("最近投稿", "点击任意一条即可载入谱面、判定与文案。")
        self.recent_box = QVBoxLayout()
        self.recent_box.setSpacing(2)
        self.recent_card.body.addLayout(self.recent_box)
        self.recent_card.hide()
        layout.addWidget(self.recent_card)
        layout.addStretch(1)
        self.stack_layout.addWidget(self.step_two)

    def _build_step_three(self) -> None:
        self.step_three = QWidget()
        layout = QVBoxLayout(self.step_three)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(14)

        self.workspace_mode_hint = _label("", "Faint")
        self.workspace_mode_hint.setWordWrap(True)
        layout.addWidget(self.workspace_mode_hint)

        search_card = Card("TUF 谱面", "输入关卡名、TUF 编号或搜索语法，工具会自动带出曲师、谱师、难度、BPM 与物量。")
        row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            "例如：Beneath the Vortex、17776 或 song:carnation"
        )
        self.search_input.setToolTip(
            "支持 TUF 搜索语法：song:carnation、bpm:>150、id=1234，点「搜索语法」查看全部字段"
        )
        self.search_input.returnPressed.connect(self._emit_lookup)
        self.search_syntax_button = QPushButton("搜索语法")
        self.search_syntax_button.setObjectName("GhostButton")
        self.search_syntax_button.setToolTip("查看 TUF 支持的字段、运算符与示例")
        self.search_syntax_button.clicked.connect(self._show_search_syntax)
        self._search_button = QPushButton("查询")
        self._search_button.setObjectName("PrimaryButton")
        self._search_button.clicked.connect(self._emit_lookup)
        row.addWidget(self.search_input, 1)
        row.addWidget(self.search_syntax_button)
        row.addWidget(self._search_button)
        search_card.body.addLayout(row)
        self._search_status = _label("尚未绑定谱面", "Muted")
        search_card.body.addWidget(self._search_status)

        self.level_info = QFrame()
        self.level_info.setObjectName("PreviewShell")
        info_layout = QGridLayout(self.level_info)
        info_layout.setContentsMargins(14, 12, 14, 12)
        info_layout.setHorizontalSpacing(18)
        info_layout.setVerticalSpacing(8)
        self.level_values: dict[str, QLabel] = {}
        fields = [
            ("song", "曲名"),
            ("artist", "曲师"),
            ("charter", "谱师"),
            ("difficulty", "难度"),
            ("bpm", "BPM"),
            ("duration", "时长"),
            ("hits", "可击中物量"),
            ("tuf", "TUF 编号"),
        ]
        for index, (key, label_text) in enumerate(fields):
            row_index = index // 4
            col_index = (index % 4) * 2
            info_layout.addWidget(_label(label_text, "Faint"), row_index, col_index)
            value = _label("-", "Value")
            value.setWordWrap(True)
            self.level_values[key] = value
            info_layout.addWidget(value, row_index, col_index + 1)
        self.level_info.hide()
        search_card.body.addWidget(self.level_info)

        layout.addWidget(search_card)

        judge_card = Card("判定数据", "照游戏结算页原样填写，精准度会按 TUF 权重实时计算。")
        self.x_perfect_check = QCheckBox("X 完美（结算页有 -完美 / 完美+ 时勾上）")
        self.x_perfect_check.toggled.connect(self._on_x_perfect_toggled)
        judge_card.body.addWidget(self.x_perfect_check)
        judge_row = QHBoxLayout()
        judge_row.setSpacing(24)
        self.judgement_grid = JudgementGrid()
        self.judgement_grid.changed.connect(self._on_judgements_changed)
        judge_row.addWidget(self.judgement_grid, 3)
        side = QVBoxLayout()
        side.setSpacing(12)
        self.accuracy_badge = AccuracyBadge()
        self.expected_label = _label("绑定谱面后会校合物量", "Faint")
        self.expected_label.setWordWrap(True)
        side.addWidget(self.accuracy_badge)
        side.addWidget(self.expected_label)
        speed_row = QHBoxLayout()
        speed_label = _label("速度", "Muted")
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.5, 2.0)
        self.speed_spin.setSingleStep(0.05)
        self.speed_spin.setValue(1.0)
        self.speed_spin.valueChanged.connect(self.changed)
        speed_row.addWidget(speed_label)
        speed_row.addWidget(self.speed_spin)
        speed_row.addStretch(1)
        side.addLayout(speed_row)
        feel_row = QHBoxLayout()
        feel_label = _label("体感难度", "Muted")
        self.feeling_input = QLineEdit()
        self.feeling_input.setPlaceholderText("例如 G12")
        self.feeling_input.textChanged.connect(self.changed)
        feel_row.addWidget(feel_label)
        feel_row.addWidget(self.feeling_input, 1)
        side.addLayout(feel_row)
        flag_row = QHBoxLayout()
        self.no_hold_check = QCheckBox("非默认长按")
        self.k12_check = QCheckBox("12K")
        self.k16_check = QCheckBox("16K")
        for widget in (self.no_hold_check, self.k12_check, self.k16_check):
            widget.toggled.connect(self.changed)
            flag_row.addWidget(widget)
        flag_row.addStretch(1)
        side.addLayout(flag_row)
        side.addStretch(1)
        judge_row.addLayout(side, 2)
        judge_card.body.addLayout(judge_row)
        layout.addWidget(judge_card)
        layout.addStretch(1)
        self.stack_layout.addWidget(self.step_three)

    def _build_step_four(self) -> None:
        self.step_four = QWidget()
        layout = QVBoxLayout(self.step_four)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(14)

        snippet_card = Card(
            "常用词条",
            "先点一下标题、简介或标签，再点词条，就会插入到该框的光标处；"
            "有选区时替换选区。点「新增词条」可直接添加，编辑与删除在「06 设置 → 文案模板」。",
        )
        self.snippet_box = QVBoxLayout()
        self.snippet_box.setSpacing(8)
        snippet_card.body.addLayout(self.snippet_box)
        hint_row = QHBoxLayout()
        hint_row.setSpacing(8)
        self.snippet_hint = _label("", "Faint")
        hint_row.addWidget(self.snippet_hint, 1)
        self.snippet_add_button = QPushButton("新增词条")
        self.snippet_add_button.setObjectName("GhostButton")
        self.snippet_add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.snippet_add_button.setToolTip("添加常用词条并立即保存")
        self.snippet_add_button.clicked.connect(self._add_copy_snippet)
        hint_row.addWidget(self.snippet_add_button)
        snippet_card.body.addLayout(hint_row)
        layout.addWidget(snippet_card)
        self.refresh_snippets()

        copy_card = Card("B 站文案", "只填一次，预览页会同时展示 B 站与 TUF 的最终字段。")
        header = QHBoxLayout()
        header.addWidget(_label("标题", "Muted"))
        header.addStretch(1)
        self.title_counter = _label("0 / 80", "Faint")
        auto_button = QPushButton("自动生成文案")
        auto_button.clicked.connect(lambda: self._auto_fill_copy())
        header.addWidget(self.title_counter)
        header.addWidget(auto_button)
        copy_card.body.addLayout(header)
        self.title_input = QLineEdit()
        self.title_input.setMaxLength(TITLE_HARD_LIMIT)
        self.title_input.setPlaceholderText("例如：【冰与火之舞】Beneath the Vortex G12 通关 99.87%")
        self.title_input.textChanged.connect(self._on_title_changed)
        copy_card.body.addWidget(self.title_input)

        copy_card.body.addWidget(_label("简介", "Muted"))
        self.desc_input = QPlainTextEdit()
        self.desc_input.setPlaceholderText("谱面、曲师、谱师、判定、TUF 链接…")
        self.desc_input.setMinimumHeight(150)
        self.desc_input.textChanged.connect(self._on_desc_changed)
        copy_card.body.addWidget(self.desc_input)

        tag_row = QHBoxLayout()
        tag_row.addWidget(_label("标签", "Muted"))
        self.tags_input = QLineEdit()
        self.tags_input.setPlaceholderText("用逗号分隔，例如：冰与火之舞, ADOFAI, 音游")
        self.tags_input.textChanged.connect(self._on_tags_changed)
        tag_row.addWidget(self.tags_input, 1)
        copy_card.body.addLayout(tag_row)

        part_row = QHBoxLayout()
        part_row.addWidget(_label("分区", "Muted"))
        self.partition_combo = ChoiceCombo()
        self.partition_combo.addItems(
            ["单机游戏", "游戏", "音乐", "生活", "知识", "影视", "动画", "鬼畜", "科技", "时尚"]
        )
        self.partition_combo.currentTextChanged.connect(self.changed)
        part_row.addWidget(self.partition_combo)
        part_row.addStretch(1)
        copy_card.body.addLayout(part_row)

        decl_row = QHBoxLayout()
        decl_row.addWidget(_label("创作声明", "Muted"))
        self.declaration_combo = ChoiceCombo()
        self.declaration_combo.addItems(CREATIVE_DECLARATIONS)
        self.declaration_combo.currentTextChanged.connect(self.changed)
        decl_row.addWidget(self.declaration_combo)
        decl_hint = _label("B 站必填，默认「内容无需标注」", "Faint")
        decl_row.addWidget(decl_hint)
        decl_row.addStretch(1)
        copy_card.body.addLayout(decl_row)
        layout.addWidget(copy_card)

        preview_card = Card("文案预览", "改上面的标题、简介或标签，这里会跟着更新。")
        self.copy_preview = CopyPreview()
        self.partition_combo.currentTextChanged.connect(
            lambda _text: self._refresh_preview()
        )
        preview_card.body.addWidget(self.copy_preview)
        layout.addWidget(preview_card)
        layout.addStretch(1)
        self.stack_layout.addWidget(self.step_four)

    def _build_step_five(self) -> None:
        self.step_five = QWidget()
        layout = QVBoxLayout(self.step_five)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(14)

        check_card = Card("提交前检查", "红色必须修，黄色建议修。")
        self.issue_list = IssueList()
        check_card.body.addWidget(self.issue_list)
        layout.addWidget(check_card)

        self.publish_panel = PublishPanel(self.settings)
        self.publish_panel.uploadRequested.connect(self.uploadRequested)
        self.publish_panel.tufRequested.connect(self.tufRequested)
        self.publish_panel.bvidCommitted.connect(self.bvidCommitted)
        self.publish_panel.openBiliRequested.connect(self.openBiliRequested)
        layout.addWidget(self.publish_panel)
        layout.addStretch(1)
        self.stack_layout.addWidget(self.step_five)

    def refresh_snippets(self) -> None:
        if not hasattr(self, "snippet_box"):
            return
        _clear_layout(self.snippet_box)
        self._snippet_buttons = []
        snippets = normalize_copy_snippets(self.settings.get("copy_snippets"))
        if not snippets:
            self.snippet_box.addWidget(
                _label("暂无词条，点「新增词条」即可添加。", "Faint")
            )
            return
        for group in SNIPPET_GROUPS:
            group_items = [item for item in snippets if item["group"] == group]
            if not group_items:
                continue
            row = QHBoxLayout()
            row.setSpacing(8)
            group_label = _label(group, "SnippetGroup")
            group_label.setFixedWidth(42)
            group_label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            )
            row.addWidget(group_label)
            grid = QGridLayout()
            grid.setHorizontalSpacing(6)
            grid.setVerticalSpacing(6)
            grid.setColumnStretch(6, 1)
            for index, item in enumerate(group_items):
                button = QPushButton(item["text"])
                button.setObjectName("SnippetCard")
                button.setMinimumWidth(96)
                button.setSizePolicy(
                    QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed
                )
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                button.setCheckable(True)
                button.setToolTip(f"插入「{item['text']}」到光标处")
                button.clicked.connect(
                    lambda _checked=False, text=item["text"], source=button: (
                        self._insert_copy_snippet(text, source)
                    )
                )
                grid.addWidget(button, index // 6, index % 6)
                self._snippet_buttons.append(button)
            row.addLayout(grid, 1)
            self.snippet_box.addLayout(row)

    def _add_copy_snippet(self, _checked: bool = False) -> None:
        """在工作台直接新增词条并立即保存，成功后重建词条卡片。"""
        dialog = CopySnippetDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        text, group = dialog.value()
        text = (text or "").strip()
        group = group if group in SNIPPET_GROUPS else "自定义"
        snippets = normalize_copy_snippets(self.settings.get("copy_snippets"))
        if not text or any(
            item["text"] == text and item["group"] == group for item in snippets
        ):
            QMessageBox.information(self, "无法添加", "词条为空或已存在。")
            return
        snippets.append({"text": text, "group": group})
        self.settings.set("copy_snippets", snippets)
        self.settings.save()
        self.refresh_snippets()
        self.snippet_hint.setText(f"已新增词条「{text}」（{group}）")

    def _insert_copy_snippet(
        self, snippet: str, source: QPushButton | None = None
    ) -> bool:
        if self.title_input.hasFocus():
            self._insert_into_line_edit(self.title_input, snippet)
            target_name = "标题"
        elif self.tags_input.hasFocus():
            self._insert_into_line_edit(self.tags_input, snippet)
            target_name = "标签"
        elif self.desc_input.hasFocus():
            self._insert_into_plain_text(self.desc_input, snippet)
            target_name = "简介"
        else:
            for button in self._snippet_buttons:
                button.setChecked(False)
            self.snippet_hint.setText("先点一下标题、简介或标签，再点词条")
            return False
        for button in self._snippet_buttons:
            button.setChecked(button is source)
        self.snippet_hint.setText(f"已插入「{snippet}」到{target_name}")
        return True

    @staticmethod
    def _insert_into_line_edit(widget: QLineEdit, snippet: str) -> None:
        text = widget.text()
        if widget.hasSelectedText():
            start = widget.selectionStart()
            selection_end = start + len(widget.selectedText())
        else:
            start = widget.cursorPosition()
            selection_end = None
        updated, position = insert_snippet(text, start, snippet, selection_end)
        widget.setText(updated)
        widget.setCursorPosition(max(0, min(position, len(widget.text()))))
        widget.setFocus()

    @staticmethod
    def _insert_into_plain_text(widget: QPlainTextEdit, snippet: str) -> None:
        cursor = widget.textCursor()
        if cursor.hasSelection():
            start = cursor.selectionStart()
            selection_end = cursor.selectionEnd()
        else:
            start = cursor.position()
            selection_end = None
        updated, position = insert_snippet(
            widget.toPlainText(), start, snippet, selection_end
        )
        widget.setPlainText(updated)
        new_cursor = widget.textCursor()
        new_cursor.setPosition(max(0, min(position, len(updated))))
        widget.setTextCursor(new_cursor)
        widget.setFocus()

    def _show_step(self, index: int) -> None:
        self.current_step = max(0, min(index, WORKSPACE_STEPS - 1))
        steps = [
            self.step_one,
            self.step_two,
            self.step_three,
            self.step_four,
            self.step_five,
        ]
        for widget in steps:
            widget.setVisible(widget is steps[self.current_step])
        self.step_bar.set_current(self.current_step)
        self.next_button.setText(
            "打开完整预览" if self.current_step == len(steps) - 1 else "下一步"
        )
        self.back_button.setEnabled(self.current_step > 0)
        if self.current_step == 3:
            self._refresh_preview()
        if self.current_step == 4:
            self.publish_panel.set_publish_mode(self._publish_mode())
            self.publish_panel.set_draft(self._snapshot())
        self._refresh_check()

    def go_step(self, index: int) -> None:
        index = max(0, min(index, WORKSPACE_STEPS - 1))
        mode_selected = (
            self.mode_dual_button.isChecked() or self.mode_bili_button.isChecked()
        )
        if index > 0 and not mode_selected:
            return
        if index >= 3 and not self._level and self._publish_mode() != "bili_only":
            return
        self._show_step(index)

    def next_step(self) -> None:
        if self.current_step == 0:
            self._show_step(1)
            return
        if self.current_step == 1:
            if not self.draft.video_path:
                QMessageBox.information(self, "还差一步", "先选择通关视频。")
                return
            self._show_step(2)
            return
        if self.current_step == 2:
            if not self._level and self._publish_mode() != "bili_only":
                QMessageBox.information(self, "还差一步", "先绑定 TUF 谱面。")
                return
            self._show_step(3)
            return
        if self.current_step == 3:
            self._show_step(4)
            return
        self.sync_to_draft()
        self.previewRequested.emit()

    def previous_step(self) -> None:
        if self.current_step == 0:
            return
        self._show_step(self.current_step - 1)

    def open_publish_step(self, focus: bool = True) -> None:
        self._show_step(WORKSPACE_STEPS - 1)
        if focus:
            QTimer.singleShot(0, self._focus_publish_panel)

    def open_step_argument(self, value: str) -> bool:
        """开发验收直达某一步：``--page workspace --tab <步骤>``。

        直接调用 ``_show_step`` 绕过 ``go_step`` 的发布方式、视频与谱面门禁；
        界面上的步骤条和「下一步」仍走原有门禁。未知值返回 False 且不切步。
        """
        text = "".join(str(value or "").split()).lower()
        aliases = {
            "1": 0, "发布方式": 0, "发布": 0, "mode": 0, "publishmode": 0,
            "2": 1, "选片": 1, "视频": 1, "video": 1, "pick": 1,
            "3": 2, "谱面": 2, "谱面&数据": 2, "level": 2, "chart": 2, "data": 2,
            "4": 3, "文案": 3, "copy": 3, "copywriting": 3,
            "5": 4, "检查": 4, "检查与发布": 4, "发布检查": 4, "check": 4, "review": 4,
        }
        index = aliases.get(text)
        if index is None:
            return False
        self._show_step(index)
        return True

    def _focus_publish_panel(self) -> None:
        self.stack_scroll.ensureWidgetVisible(self.publish_panel, 0, 12)
        self.publish_panel.upload_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_video_selected(self, path: str) -> None:
        self.draft.video_path = path
        self.changed.emit()

    def _on_cover_selected(self, path: str) -> None:
        """按当前槽位写回草稿：主槽 → cover_path，副槽 → cover_path_43。"""
        if self.cover_picker.active_slot == CoverPicker.SLOT_ALTERNATE:
            self.draft.cover_path_43 = path
        else:
            self.draft.cover_path = path
        # 「已生成 4:3 副图」是一次同步的结果；换图或清空后不再挂着。
        self.cover_status.clear()
        self._refresh_cover_actions()
        self._refresh_check()
        self.changed.emit()

    def _on_cover_slot_changed(self, _slot: str) -> None:
        self._refresh_cover_actions()

    def _clear_cover_slot(self) -> None:
        self.cover_picker.clear()

    def _refresh_cover_actions(self) -> None:
        """按钮随槽位变：清除按钮指名当前槽，同步按钮只在主图存在时可用。"""
        alternate = self.cover_picker.active_slot == CoverPicker.SLOT_ALTERNATE
        current = (
            self.draft.cover_path_43 if alternate else self.draft.cover_path
        )
        self.cover_clear.setText("清除 4:3 副图" if alternate else "清除 16:9 主图")
        self.cover_clear.setEnabled(bool(current))
        self.cover_sync.setEnabled(bool(self.draft.cover_path))

    def _sync_cover_ratio(self) -> None:
        """用 16:9 主图居中裁剪出 4:3 副图，写进缓存目录并选中副槽。"""
        path = self.cover_picker.save_sync()
        if not path:
            QMessageBox.information(
                self,
                "还没法同步",
                "请先在「16:9 主图」槽里选一张能读到的图片，再点「用这张同步 4:3」。",
            )
            return
        self.cover_picker.set_active_slot(CoverPicker.SLOT_ALTERNATE)
        self.cover_picker.set_file(path)
        self.cover_status.setText(f"已生成 4:3 副图：{Path(path).name}")

    def _show_search_syntax(self) -> None:
        SearchSyntaxDialog(self).exec()

    def _emit_lookup(self) -> None:
        query = self.search_input.text().strip()
        if not query:
            return
        if self._search_button:
            self._search_button.setEnabled(False)
        if self._search_status:
            self._search_status.setText("正在查询 TUF…")
        self.lookupRequested.emit(query)

    def set_lookup_error(self, message: str) -> None:
        if self._search_button:
            self._search_button.setEnabled(True)
        if self._search_status:
            self._search_status.setText(message)

    def show_lookup_results(self, levels: list[LevelInfo]) -> None:
        if self._search_button:
            self._search_button.setEnabled(True)
        if not levels:
            self.set_lookup_error("没有找到匹配的谱面，换个关键词或直接填 TUF 编号")
            return
        level = levels[0]
        if len(levels) > 1:
            dialog = LevelPickerDialog(levels, self)
            if dialog.exec() != QDialog.DialogCode.Accepted or dialog.selected is None:
                self.set_lookup_error("已取消选择")
                return
            level = dialog.selected
        self.bind_level(level)

    def bind_level(self, level: LevelInfo) -> None:
        self._level = level
        self.draft.level = level
        self.level_info.show()
        self.level_values["song"].setText(level.song or "-")
        self.level_values["artist"].setText(level.artist or "-")
        self.level_values["charter"].setText(level.charter or "-")
        self.level_values["difficulty"].setText(level.difficulty or "-")
        self.level_values["bpm"].setText(str(int(level.bpm)) if level.bpm else "-")
        self.level_values["duration"].setText(level.duration_text)
        self.level_values["hits"].setText(str(level.expected_hits) if level.expected_hits else "-")
        self.level_values["tuf"].setText(str(level.tuf_id or "-"))
        if self._search_status:
            self._search_status.setText(f"已绑定：{level.song}（TUF {level.tuf_id}）")
        self._on_judgements_changed()
        if not self.title_input.text().strip():
            self._auto_fill_copy()
        self.changed.emit()

    def _on_x_perfect_toggled(self, checked: bool) -> None:
        self.judgement_grid.set_x_perfect(checked)
        self._on_judgements_changed()
        self.changed.emit()

    def _on_judgements_changed(self) -> None:
        values = self.judgement_grid.values()
        total = judgement_sum(values)
        # 物量校验用 TUF 的 tilecount 口径：提前 / 太慢！！不算打击数。
        hits = judgement_hit_count(values)
        accuracy = calc_accuracy(values)
        self.accuracy_badge.set_accuracy(accuracy, total)
        if self._publish_mode() == "bili_only":
            self.expected_label.setText("仅 B 站模式：不校验判定与物量")
            set_tone(self.expected_label, None)
            if self._auto_copy:
                self._auto_fill_copy(notify=False)
            self._refresh_check()
            self.changed.emit()
            return
        expected = self._level.expected_hits if self._level else None
        if expected:
            ok = hits == expected
            self.expected_label.setText(
                f"物量校验（打击数，排除提前 / 太慢！！）：{hits} / {expected} "
                + ("√ 一致" if ok else f"× 不一致（判定总数 {total}）")
            )
            set_tone(self.expected_label, "ok" if ok else "bad")
        else:
            self.expected_label.setText("绑定谱面后会自动校合物量")
            set_tone(self.expected_label, None)
        if self._auto_copy and self._level:
            self._auto_fill_copy(notify=False)
        self._refresh_check()
        self.changed.emit()

    def _on_title_changed(self, text: str) -> None:
        if not self._filling_copy:
            self._auto_copy = False
        length = len(text)
        tone = (
            "bad"
            if length > TITLE_HARD_LIMIT
            else ("warn" if length > TITLE_WARN_LIMIT else None)
        )
        self.title_counter.setText(f"{length} / {TITLE_HARD_LIMIT}")
        set_tone(self.title_counter, tone)
        self._refresh_check()
        self.changed.emit()

    def _on_desc_changed(self) -> None:
        if not self._filling_copy:
            self._auto_copy = False
        self._refresh_preview()
        self.changed.emit()

    def _on_tags_changed(self) -> None:
        if not self._filling_copy:
            self._auto_copy = False
        self._refresh_preview()
        self.changed.emit()

    def _auto_fill_copy(self, notify: bool = True) -> None:
        if not self._level:
            if notify:
                QMessageBox.information(
                    self,
                    "请手动填写文案",
                    "仅 B 站模式下没有绑定 TUF 谱面，请手动填写标题、简介和标签。"
                    if self._publish_mode() == "bili_only"
                    else "需要先绑定 TUF 谱面，才能生成文案。",
                )
            return
        snapshot = self._snapshot()
        accuracy_text = format_accuracy(snapshot.judgements)
        title, description, tags = generate_copy(snapshot, self.settings, accuracy_text)
        self._filling_copy = True
        try:
            self.title_input.setText(title)
            self.desc_input.setPlainText(description)
            self.tags_input.setText(", ".join(tags))
        finally:
            self._filling_copy = False
        self._auto_copy = True
        self._refresh_check()
        self.changed.emit()

    def _snapshot(self) -> Draft:
        draft = Draft.from_dict(self.draft.to_dict())
        draft.level = self._level
        draft.judgements = self.judgement_grid.values()
        draft.speed = self.speed_spin.value()
        draft.feeling_rating = self.feeling_input.text().strip()
        draft.is_no_hold_tap = self.no_hold_check.isChecked()
        draft.is_12k = self.k12_check.isChecked()
        draft.is_16k = self.k16_check.isChecked()
        draft.x_perfect = self.x_perfect_check.isChecked()
        draft.title = self.title_input.text().strip()
        draft.description = self.desc_input.toPlainText().strip()
        draft.tags = _parse_tags(self.tags_input.text())
        draft.partition = self.partition_combo.currentText()
        draft.creative_declaration = self.declaration_combo.currentText()
        draft.video_path = self.draft.video_path
        draft.cover_path = self.draft.cover_path
        draft.cover_path_43 = self.draft.cover_path_43
        return draft

    def sync_to_draft(self) -> None:
        self.draft = self._snapshot()

    def collect(self) -> Draft:
        self.sync_to_draft()
        return self.draft

    def _refresh_preview(self) -> None:
        if not hasattr(self, "copy_preview"):
            return
        self.copy_preview.refresh(self._snapshot(), self._publish_mode())

    def _refresh_check(self) -> None:
        self._refresh_preview()
        if not hasattr(self, "issue_list"):
            return
        issues = check_draft(
            self._snapshot(),
            banned_words=self.settings.get("banned_words"),
            check_video_file=True,
            publish_mode=self._publish_mode(),
        )
        self.issue_list.set_issues(issues)

    def _publish_mode(self) -> str:
        mode = str(self.settings.get("publish_mode") or "dual")
        return "bili_only" if mode == "bili_only" else "dual"

    def _set_publish_mode(self, mode: str) -> None:
        self.set_publish_mode(mode, persist=True)

    def set_publish_mode(self, mode: str, persist: bool = False) -> None:
        mode = "bili_only" if mode == "bili_only" else "dual"
        bili_only = mode == "bili_only"
        if persist:
            self.settings.set("publish_mode", mode)
            self.settings.save()
        self.mode_dual_button.setChecked(not bili_only)
        self.mode_bili_button.setChecked(bili_only)
        if hasattr(self, "workspace_mode_hint"):
            self.workspace_mode_hint.setText(
                "仅 B 站模式：可不绑定 TUF 谱面，判定与物量不参与校验。"
                if bili_only
                else "双投模式：需要绑定 TUF 谱面，判定与物量会参与校验。"
            )
        if hasattr(self, "publish_panel"):
            self.publish_panel.set_publish_mode(mode)
        self._refresh_check()
        if self._level is None:
            self.expected_label.setText(
                "仅 B 站模式：不需要绑定 TUF 谱面"
                if bili_only
                else "绑定谱面后会自动校合物量"
            )
        elif bili_only:
            self.expected_label.setText("仅 B 站模式：不校验判定与物量")
            set_tone(self.expected_label, None)
        else:
            self._on_judgements_changed()

    def load_draft(self, draft: Draft) -> None:
        self.draft = Draft.from_dict(draft.to_dict())
        self.drop_zone.set_file(draft.video_path)
        # 两个槽各自回填；旧草稿没有 cover_path_43，按空串处理，不报错。
        self.cover_picker.set_active_slot(CoverPicker.SLOT_PRIMARY)
        self.cover_picker.set_file(draft.cover_path)
        self.cover_picker.set_active_slot(CoverPicker.SLOT_ALTERNATE)
        self.cover_picker.set_file(draft.cover_path_43)
        self.cover_picker.set_active_slot(CoverPicker.SLOT_PRIMARY)
        self.cover_status.clear()
        self._refresh_cover_actions()
        self._level = draft.level
        if draft.level:
            self.bind_level(draft.level)
            self.search_input.setText(str(draft.level.tuf_id or draft.level.song))
        else:
            self.level_info.hide()
            self.search_input.clear()
            if self._search_status:
                self._search_status.setText("尚未绑定谱面")
        # 先恢复 X 完美模式，再灌数值，否则 -完美/完美+ 会被当成隐藏格清零。
        self.x_perfect_check.blockSignals(True)
        self.x_perfect_check.setChecked(bool(draft.x_perfect))
        self.x_perfect_check.blockSignals(False)
        self.judgement_grid.set_x_perfect(bool(draft.x_perfect))
        self.judgement_grid.set_values(draft.judgements)
        self.speed_spin.setValue(draft.speed or 1.0)
        self.feeling_input.setText(draft.feeling_rating)
        self.no_hold_check.setChecked(draft.is_no_hold_tap)
        self.k12_check.setChecked(draft.is_12k)
        self.k16_check.setChecked(draft.is_16k)
        self.title_input.setText(draft.title)
        self.desc_input.setPlainText(draft.description)
        self.tags_input.setText(", ".join(draft.tags))
        index = self.partition_combo.findText(draft.partition)
        if index >= 0:
            self.partition_combo.setCurrentIndex(index)
        index = self.declaration_combo.findText(draft.creative_declaration)
        if index < 0:
            index = 0
        self.declaration_combo.setCurrentIndex(index)
        self._auto_copy = not (draft.title.strip() or draft.description.strip())
        self._show_step(0)
        self._on_judgements_changed()
        self.publish_panel.set_draft(self.draft)

    def update_bvid(self, bvid: str) -> None:
        self.draft.bvid = bvid

    def set_recent(self, records: list[dict[str, Any]]) -> None:
        while self.recent_box.count():
            item = self.recent_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if not records:
            self.recent_card.hide()
            return
        for record in records[:4]:
            parts = [
                str(record.get("song") or record.get("title") or "未命名谱面"),
                str(record.get("difficulty") or "-"),
                str(record.get("accuracy") or "-"),
            ]
            if record.get("bvid"):
                parts.append(str(record["bvid"]))
            row = QPushButton("   ·   ".join(parts))
            row.setObjectName("RecentRow")
            row.setCursor(Qt.CursorShape.PointingHandCursor)
            row.clicked.connect(lambda _=False, item=dict(record): self.recentRequested.emit(item))
            self.recent_box.addWidget(row)
        self.recent_card.show()


class CopyPreview(QWidget):
    """B 站最终效果 + TUF 提交字段；预览页与工作台第 4 步共用同一份渲染。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._last_draft: Draft | None = None
        columns = QHBoxLayout(self)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(18)

        left = QVBoxLayout()
        left.addWidget(_label("B 站最终效果", "CardTitle"))
        shell = QFrame()
        shell.setObjectName("PreviewShell")
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(14, 14, 14, 16)
        shell_layout.setSpacing(10)
        cover = QFrame()
        cover.setObjectName("CoverPlaceholder")
        cover.setMinimumHeight(190)
        cover_layout = QVBoxLayout(cover)
        cover_layout.setContentsMargins(0, 0, 0, 0)
        cover_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_image = QLabel("ADOFAI\nA Dance of Fire and Ice")
        self.cover_image.setObjectName("CoverPreview")
        self.cover_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_image.setMinimumHeight(190)
        self._apply_cover_style()
        cover_layout.addWidget(self.cover_image)
        shell_layout.addWidget(cover)
        self.preview_title = _label("标题会显示在这里", "CardTitle")
        self.preview_title.setWordWrap(True)
        shell_layout.addWidget(self.preview_title)
        self.preview_meta = _label("你的账号 · 刚刚 · 单机游戏", "Faint")
        shell_layout.addWidget(self.preview_meta)
        self.preview_desc = _label("", "Muted")
        self.preview_desc.setWordWrap(True)
        shell_layout.addWidget(self.preview_desc)
        self.preview_tags = QHBoxLayout()
        self.preview_tags.setSpacing(6)
        shell_layout.addLayout(self.preview_tags)
        left.addWidget(shell)
        left.addStretch(1)
        columns.addLayout(left, 3)

        right = QVBoxLayout()
        right.addWidget(_label("TUF 提交字段", "CardTitle"))
        tuf_card = QFrame()
        tuf_card.setObjectName("PreviewShell")
        self.tuf_form = QFormLayout(tuf_card)
        self.tuf_form.setContentsMargins(16, 14, 16, 14)
        self.tuf_form.setHorizontalSpacing(16)
        self.tuf_form.setVerticalSpacing(9)
        self.tuf_values: dict[str, QLabel] = {}
        for key, label_text in [
            ("level", "关卡"),
            ("accuracy", "精准度"),
            ("judgements", "判定"),
            ("speed", "速度"),
            ("feeling", "体感难度"),
            ("video", "视频链接"),
            ("bvid", "BV 号"),
        ]:
            value = _label("-", "Value")
            value.setWordWrap(True)
            self.tuf_values[key] = value
            self.tuf_form.addRow(_label(label_text, "Muted"), value)
        right.addWidget(tuf_card)
        self.tuf_hint = _label("TUF 记录会在 B 站拿到 BV 号后自动提交。", "Faint")
        self.tuf_hint.setWordWrap(True)
        right.addWidget(self.tuf_hint)
        right.addStretch(1)
        columns.addLayout(right, 2)

    def _apply_cover_style(self) -> None:
        # 颜色交给 QSS 的 QLabel#CoverPreview（ink_muted），深浅主题都可读；
        # 这里只固定封面占位文字的字号。
        self.cover_image.setStyleSheet("font-size: 26px;")

    def refresh_theme(self) -> None:
        self._apply_cover_style()
        self.cover_image.update()

    def refresh(self, draft: Draft, publish_mode: str = "dual") -> None:
        self._last_draft = draft
        accuracy = format_accuracy(draft.judgements)
        level = draft.level
        bili_only = publish_mode == "bili_only"
        self.tuf_hint.setText(
            "当前为仅 B 站模式：本次不会提交 TUF 记录。"
            if bili_only
            else "TUF 记录会在 B 站拿到 BV 号后自动提交。"
        )
        self._render_cover(draft)
        self.preview_title.setText(draft.title or "（还没有标题）")
        self.preview_meta.setText(f"你的账号 · 刚刚 · {draft.partition}")
        self.preview_desc.setText(draft.description or "（还没有简介）")
        while self.preview_tags.count():
            item = self.preview_tags.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for tag in draft.tags[:8]:
            self.preview_tags.addWidget(chip(tag))
        self.preview_tags.addStretch(1)

        self.tuf_values["level"].setText(
            f"{level.song} / {level.difficulty} / {level.charter}" if level else "-"
        )
        self.tuf_values["accuracy"].setText(accuracy)
        self.tuf_values["judgements"].setText(
            " · ".join(
                f"{label} {draft.judgements.get(key, 0)}"
                for key, label, _weight in JUDGEMENT_FIELDS
                if draft.judgements.get(key, 0)
            )
            or "全 0"
        )
        self.tuf_values["speed"].setText(f"{draft.speed:g}")
        self.tuf_values["feeling"].setText(draft.feeling_rating or "-")
        self.tuf_values["video"].setText(
            f"https://www.bilibili.com/video/{draft.bvid}" if draft.bvid else "发布 B 站后自动填入"
        )
        self.tuf_values["bvid"].setText(draft.bvid or "待获取")

    def _render_cover(self, draft: Draft) -> None:
        cover_path = Path(draft.cover_path) if draft.cover_path else None
        cover_pixmap = QPixmap(str(cover_path)) if cover_path and cover_path.is_file() else QPixmap()
        if not cover_pixmap.isNull():
            target_w = max(320, self.cover_image.width())
            target_h = max(190, self.cover_image.height())
            scaled = cover_pixmap.scaled(
                target_w,
                target_h,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = max(0, (scaled.width() - target_w) // 2)
            y = max(0, (scaled.height() - target_h) // 2)
            self.cover_image.setPixmap(scaled.copy(x, y, target_w, target_h))
            self.cover_image.setText("")
        else:
            self.cover_image.setPixmap(QPixmap())
            self.cover_image.setText("ADOFAI\nA Dance of Fire and Ice")

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._last_draft is not None:
            self._render_cover(self._last_draft)


class PreviewPage(QWidget):
    editRequested = Signal()
    publishRequested = Signal()

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        content = QWidget()
        outer = QVBoxLayout(content)
        outer.setContentsMargins(28, 8, 28, 18)
        outer.setSpacing(14)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(content)
        root.addWidget(area, 1)

        self.copy_preview = CopyPreview()
        outer.addWidget(self.copy_preview, 1)

        button_row = QHBoxLayout()
        edit = QPushButton("返回修改")
        edit.clicked.connect(self.editRequested)
        self.publish_button = QPushButton("去发布")
        self.publish_button.setObjectName("PrimaryButton")
        self.publish_button.clicked.connect(self.publishRequested)
        button_row.addStretch(1)
        button_row.addWidget(edit)
        button_row.addWidget(self.publish_button)
        outer.addLayout(button_row)

    def refresh_theme(self) -> None:
        self.copy_preview.refresh_theme()

    def refresh(self, draft: Draft) -> None:
        mode = str(self.settings.get("publish_mode") or "dual")
        self.copy_preview.refresh(draft, mode)


class PublishPanel(QWidget):
    uploadRequested = Signal()
    tufRequested = Signal()
    openBiliRequested = Signal()
    bvidCommitted = Signal(str)
    manualDoneRequested = Signal()
    manualCancelRequested = Signal()
    reviewSyncRequested = Signal()

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)

        summary = Card("本次投稿", "")
        self.summary_grid = QGridLayout()
        self.summary_grid.setHorizontalSpacing(20)
        self.summary_grid.setVerticalSpacing(7)
        self.summary_values: dict[str, QLabel] = {}
        for index, (key, label_text) in enumerate(
            [
                ("song", "谱面"),
                ("difficulty", "难度"),
                ("accuracy", "精准度"),
                ("cover", "封面"),
                ("title", "标题"),
                ("tags", "标签"),
                ("partition", "分区"),
                ("declaration", "创作声明"),
            ]
        ):
            row = index // 2
            col = (index % 2) * 2
            self.summary_grid.addWidget(_label(label_text, "Muted"), row, col)
            value = _label("-", "Value")
            value.setWordWrap(True)
            self.summary_values[key] = value
            self.summary_grid.addWidget(value, row, col + 1)
        summary.body.addLayout(self.summary_grid)
        outer.addWidget(summary)

        self.action_card = Card(
            "执行发布",
            "确认内容后上传 B 站；双投模式拿到 BV 号后提交 TUF。",
        )
        self.mode_hint = _label("", "Faint")
        self.mode_hint.setWordWrap(True)
        self.action_card.body.addWidget(self.mode_hint)

        self.manual_box = QWidget()
        manual_layout = QVBoxLayout(self.manual_box)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.setSpacing(8)
        self.manual_label = _label("", "Value")
        self.manual_label.setWordWrap(True)
        manual_layout.addWidget(self.manual_label)
        manual_buttons = QHBoxLayout()
        self.manual_done_button = QPushButton("我已完成")
        self.manual_done_button.setObjectName("PrimaryButton")
        self.manual_done_button.setToolTip("已经在浏览器里完成这一步，让工具继续")
        self.manual_done_button.clicked.connect(
            lambda: self.manualDoneRequested.emit()
        )
        self.manual_cancel_button = QPushButton("取消并关闭浏览器")
        self.manual_cancel_button.setObjectName("GhostButton")
        self.manual_cancel_button.setToolTip("取消这次上传并关闭工具打开的浏览器")
        self.manual_cancel_button.clicked.connect(
            lambda: self.manualCancelRequested.emit()
        )
        manual_buttons.addWidget(self.manual_done_button)
        manual_buttons.addWidget(self.manual_cancel_button)
        manual_buttons.addStretch(1)
        manual_layout.addLayout(manual_buttons)
        self.manual_box.setVisible(False)
        self.action_card.body.addWidget(self.manual_box)

        status_row = QHBoxLayout()
        self.bili_status = chip("B 站：等待上传", "warn")
        self.tuf_status = chip("TUF：等待 BV", "warn")
        self.review_chip = chip("审核：未同步")
        self.account_status = chip("登录态：未检查")
        status_row.addWidget(self.bili_status)
        status_row.addWidget(self.tuf_status)
        status_row.addWidget(self.review_chip)
        status_row.addWidget(self.account_status)
        status_row.addStretch(1)
        self.action_card.body.addLayout(status_row)

        button_row = QHBoxLayout()
        self.upload_button = QPushButton("上传到 B 站")
        self.upload_button.setObjectName("PrimaryButton")
        self.upload_button.clicked.connect(self.uploadRequested)
        self.tuf_button = QPushButton("提交 TUF 记录")
        self.tuf_button.setObjectName("PrimaryButton")
        self.tuf_button.setEnabled(False)
        self.tuf_button.clicked.connect(self.tufRequested)
        open_button = QPushButton("打开 B 站投稿页")
        open_button.setObjectName("GhostButton")
        open_button.clicked.connect(self.openBiliRequested)
        self.review_refresh_button = QPushButton("刷新审核状态")
        self.review_refresh_button.setObjectName("GhostButton")
        self.review_refresh_button.setToolTip(
            "从 TUF 读取这条投稿的审核进度（待处理 / 已通过 / 未通过）"
        )
        self.review_refresh_button.setVisible(False)
        self.review_refresh_button.clicked.connect(self.reviewSyncRequested)
        button_row.addWidget(self.upload_button)
        button_row.addWidget(self.tuf_button)
        button_row.addWidget(self.review_refresh_button)
        button_row.addWidget(open_button)
        button_row.addStretch(1)
        self.action_card.body.addLayout(button_row)

        bv_row = QHBoxLayout()
        bv_row.addWidget(_label("BV 号", "Muted"))
        self.bv_input = QLineEdit()
        self.bv_input.setPlaceholderText("如自动获取失败，可手动粘贴 BV 号后回车")
        self.bv_input.returnPressed.connect(self._commit_bv)
        self.bv_save = QPushButton("保存 BV")
        self.bv_save.clicked.connect(self._commit_bv)
        bv_row.addWidget(self.bv_input, 1)
        bv_row.addWidget(self.bv_save)
        self.action_card.body.addLayout(bv_row)
        outer.addWidget(self.action_card)

        progress_card = Card("执行日志", "")
        self.stage_label = _label("等待开始", "CardHint")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("Log")
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(150)
        progress_card.body.addWidget(self.stage_label)
        progress_card.body.addWidget(self.progress)
        progress_card.body.addWidget(self.log_view, 1)
        outer.addWidget(progress_card)

        self._bvid = ""
        self._draft: Draft | None = None
        self._tuf_record_id = ""
        self._busy = False
        self._mode = "dual"
        self._apply_mode(str(settings.get("publish_mode") or "dual"))

    def publish_mode(self) -> str:
        return self._mode

    def set_publish_mode(self, mode: str) -> None:
        self._apply_mode(mode)

    def _apply_mode(self, mode: str) -> None:
        self._mode = "bili_only" if mode == "bili_only" else "dual"
        bili_only = self._mode == "bili_only"
        self.mode_hint.setText(
            "仅 B 站模式：本次不写 TUF 记录，也不校验判定与物量。"
            if bili_only
            else "双投模式：先上传 B 站，拿到 BV 号后再提交 TUF。"
        )
        self.upload_button.setText("仅上传到 B 站" if bili_only else "上传到 B 站")
        self.tuf_button.setVisible(not bili_only)
        if bili_only:
            self.tuf_status.setText("TUF：本次跳过")
            self.tuf_status.setObjectName("Chip")
        elif self._draft is not None and self._draft.tuf_record_id:
            self.tuf_status.setText(f"TUF：已提交 {self._draft.tuf_record_id}".strip())
            self.tuf_status.setObjectName("ChipOk")
        else:
            self.tuf_status.setText("TUF：等待 BV")
            self.tuf_status.setObjectName("ChipWarn")
        self._repolish(self.tuf_status)
        self.set_bvid(self._bvid)

    def set_draft(self, draft: Draft) -> None:
        self._draft = draft
        level = draft.level
        self.summary_values["song"].setText(level.song if level else "-")
        self.summary_values["difficulty"].setText(level.difficulty if level else "-")
        self.summary_values["accuracy"].setText(format_accuracy(draft.judgements))
        cover_name = (
            Path(draft.cover_path).name
            if draft.cover_path and Path(draft.cover_path).is_file()
            else "未选择（B 站自动）"
        )
        if draft.cover_path_43 and Path(draft.cover_path_43).is_file():
            cover_name += " + 4:3 副图"
        self.summary_values["cover"].setText(cover_name)
        self.summary_values["title"].setText(draft.title or "-")
        self.summary_values["tags"].setText(" / ".join(draft.tags) or "-")
        self.summary_values["partition"].setText(draft.partition)
        self.summary_values["declaration"].setText(draft.creative_declaration or "内容无需标注")
        self._tuf_record_id = str(draft.tuf_record_id or "")
        self.set_review(
            draft.review_status,
            draft.review_checked_at,
            draft.review_message,
        )
        self.set_bvid(draft.bvid)

    def set_bvid(self, bvid: str) -> None:
        self._bvid = bvid or ""
        self.bv_input.setText(self._bvid)
        if self._bvid:
            self.bili_status.setText(f"B 站：{self._bvid}")
            self.bili_status.setObjectName("ChipOk")
        else:
            self.bili_status.setText("B 站：等待上传")
            self.bili_status.setObjectName("ChipWarn")
        self.tuf_button.setEnabled(
            not self._busy and bool(self._bvid) and self._mode == "dual"
        )
        self._repolish(self.bili_status)

    def set_tuf_ok(self, record_id: str = "") -> None:
        self.tuf_status.setText(f"TUF：已提交 {record_id}".strip())
        self.tuf_status.setObjectName("ChipOk")
        self._repolish(self.tuf_status)
        if record_id:
            self._tuf_record_id = str(record_id)
        self._update_review_visibility()

    def set_review(
        self, status: str, checked_at: str = "", message: str = ""
    ) -> None:
        """刷新审核 chip：待处理黄、已通过绿、未通过红。"""

        text = review_text(status)
        self.review_chip.setText(f"审核：{text}" if text else "审核：未同步")
        normalized = normalize_review_status(status) if status else ""
        object_name = REVIEW_CHIP_OBJECTS.get(normalized, "Chip") if status else "Chip"
        self.review_chip.setObjectName(object_name)
        tips: list[str] = []
        if checked_at:
            tips.append(f"上次同步：{checked_at}")
        if message:
            tips.append(f"审核说明：{message}")
        self.review_chip.setToolTip("\n".join(tips) or "还没有同步过审核状态")
        self._repolish(self.review_chip)
        self._update_review_visibility()

    def _update_review_visibility(self) -> None:
        self.review_refresh_button.setVisible(bool(self._tuf_record_id))

    def set_accounts(self, bili: bool, tuf: bool, tuf_error: str = "") -> None:
        if bili and tuf:
            self.account_status.setText("登录态：双站已登录")
            self.account_status.setObjectName("ChipOk")
        elif tuf_error and not tuf:
            # TUF 暂时不可用：不显示「未登录」，避免误导用户重新登录。
            prefix = "B 站已登录 · " if bili else ""
            self.account_status.setText(f"登录态：{prefix}TUF 服务暂不可用")
            self.account_status.setObjectName("ChipWarn")
        elif bili or tuf:
            self.account_status.setText(f"登录态：{'B 站' if bili else ''}{' / ' if bili and tuf else ''}{'TUF' if tuf else ''}已登录")
            self.account_status.setObjectName("ChipWarn")
        else:
            self.account_status.setText("登录态：未登录")
            self.account_status.setObjectName("ChipBad")
        self._repolish(self.account_status)

    def set_busy(self, busy: bool, kind: str = "") -> None:
        self._busy = busy
        self.upload_button.setEnabled(not busy)
        self.tuf_button.setEnabled(
            not busy and bool(self._bvid) and self._mode == "dual"
        )
        if busy:
            self.stage_label.setText(f"正在{kind}…")
        else:
            self.stage_label.setText("就绪")
            self.clear_manual_pending()

    def set_manual_pending(self, message: str) -> None:
        """后台把某一步交给浏览器里的用户：显示提示与「我已完成 / 取消」。"""

        self.manual_label.setText(message)
        self.manual_done_button.setEnabled(True)
        self.manual_cancel_button.setEnabled(True)
        self.manual_box.setVisible(True)
        self.stage_label.setText("等待你在浏览器里完成（完成后点「我已完成」）")

    def set_manual_accepted(self) -> None:
        self.manual_done_button.setEnabled(False)
        self.manual_label.setText("已收到「我已完成」，工具继续执行…")
        self.stage_label.setText("正在继续…")

    def set_manual_cancelling(self) -> None:
        self.manual_done_button.setEnabled(False)
        self.manual_cancel_button.setEnabled(False)
        self.manual_label.setText("正在取消并关闭浏览器…")
        self.stage_label.setText("正在取消…")

    def clear_manual_pending(self) -> None:
        self.manual_box.setVisible(False)
        self.manual_label.setText("")

    def set_progress(self, value: int) -> None:
        self.progress.setValue(max(0, min(100, value)))

    def set_stage(self, text: str) -> None:
        self.stage_label.setText(text)

    def append_log(self, text: str) -> None:
        self.log_view.appendPlainText(text)
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def clear_log(self) -> None:
        self.log_view.clear()
        self.progress.setValue(0)

    def _commit_bv(self) -> None:
        value = self.bv_input.text().strip()
        if not value:
            return
        import re

        match = re.search(r"BV[0-9A-Za-z]{10}", value)
        if match is None:
            QMessageBox.warning(
                self,
                "BV 号格式不正确",
                "请填写形如 BV1xx411c7mD 的 BV 号，或直接粘贴 B 站视频链接。",
            )
            return
        bvid = match.group(0)
        self.set_bvid(bvid)
        if self._draft is not None:
            self._draft.bvid = bvid
        self.append_log(f"已手动保存 BV 号：{bvid}")
        self.bvidCommitted.emit(bvid)

    def _repolish(self, widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)


class HistoryPage(QWidget):
    loadRequested = Signal(dict)
    deleteRequested = Signal(str)
    refreshRequested = Signal()
    clearRequested = Signal()
    reviewSyncRequested = Signal()
    tufListRequested = Signal()

    #: 状态下拉的 (值, 文案) —— 文案与 ``_status_text()`` 一致。
    STATUS_FILTERS: tuple[tuple[str, str], ...] = (
        ("draft", "草稿"),
        ("bili_only", "仅 B 站"),
        ("bili_uploaded", "已上 B 站"),
        ("submitted", "已双投"),
        ("failed", "失败"),
    )
    #: 审核下拉的取值 —— 文案走 ``review_text()``，与表格「审核」列一致。
    REVIEW_FILTERS: tuple[str, ...] = ("pending", "approved", "declined")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._records: list[dict[str, Any]] = []
        self._filtered_records: list[dict[str, Any]] = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 8, 28, 18)
        outer.setSpacing(12)
        toolbar = QHBoxLayout()
        self.count_label = _label("0 条记录", "Muted")
        toolbar.addWidget(self.count_label)
        toolbar.addStretch(1)
        self.review_sync_button = QPushButton("同步审核状态")
        self.review_sync_button.setToolTip(
            "从 TUF 读取已投稿记录的审核进度（待处理 / 已通过 / 未通过）"
        )
        self.review_sync_button.clicked.connect(self.reviewSyncRequested)
        self.tuf_list_button = QPushButton("查看 TUF 投稿记录")
        self.tuf_list_button.setToolTip(
            "只读列出 TUF 服务器上当前账号的 pass 投稿，不依赖本地记录"
        )
        self.tuf_list_button.clicked.connect(self.tufListRequested)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refreshRequested)
        load = QPushButton("载入选中")
        load.setObjectName("PrimaryButton")
        load.clicked.connect(self._load_selected)
        delete = QPushButton("删除选中")
        delete.setObjectName("DangerButton")
        delete.clicked.connect(self._delete_selected)
        clear = QPushButton("清空")
        clear.setObjectName("DangerButton")
        clear.clicked.connect(self._clear)
        toolbar.addWidget(self.review_sync_button)
        toolbar.addWidget(self.tuf_list_button)
        toolbar.addWidget(refresh)
        toolbar.addWidget(load)
        toolbar.addWidget(delete)
        toolbar.addWidget(clear)
        outer.addLayout(toolbar)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索曲名 / 标题 / BV 号 / TUF 编号")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setToolTip("输入关键字即时过滤下面的记录")
        self.status_combo = ChoiceCombo()
        self.status_combo.setToolTip("按投稿状态筛选")
        self.status_combo.addItem("全部状态", "")
        for value, text in self.STATUS_FILTERS:
            self.status_combo.addItem(text, value)
        self.review_combo = ChoiceCombo()
        self.review_combo.setToolTip("按 TUF 审核状态筛选")
        self.review_combo.addItem("全部审核", "")
        for value in self.REVIEW_FILTERS:
            self.review_combo.addItem(review_text(value), value)
        filter_row.addWidget(self.search_input, 1)
        filter_row.addWidget(self.status_combo)
        filter_row.addWidget(self.review_combo)
        outer.addLayout(filter_row)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["时间", "谱面", "难度", "精准度", "BV 号", "TUF", "状态", "审核"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setToolTip("右键：载入到工作台、复制 BV 号、打开 B 站视频或删除记录")
        self.table.doubleClicked.connect(lambda _: self._load_selected())
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        outer.addWidget(self.table, 1)

        self.empty_label = _label("还没有投稿记录。完成一次双投后会出现在这里。", "Muted")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.empty_label)

        self.search_input.textChanged.connect(lambda _text: self._apply_filters())
        self.status_combo.currentIndexChanged.connect(
            lambda _index: self._apply_filters()
        )
        self.review_combo.currentIndexChanged.connect(
            lambda _index: self._apply_filters()
        )

    def set_records(self, records: list[dict[str, Any]]) -> None:
        self._records = list(records)
        self._apply_filters()

    def remember(self, records: list[dict[str, Any]]) -> None:
        self._records = list(records)

    # -- 过滤与渲染 ---------------------------------------------------------

    def _matches_query(self, record: dict[str, Any], query: str) -> bool:
        haystack = " ".join(
            str(record.get(key) or "")
            for key in ("song", "title", "bvid", "tuf_record_id", "tuf_id")
        ).lower()
        return query in haystack

    def _apply_filters(self) -> None:
        """按搜索框 / 状态 / 审核过滤，只重建表格，不动完整 ``_records``。"""

        query = self.search_input.text().strip().lower()
        status = self.status_combo.currentData() or ""
        review = self.review_combo.currentData() or ""
        filtered: list[dict[str, Any]] = []
        for record in self._records:
            if query and not self._matches_query(record, query):
                continue
            if status and (record.get("status") or "") != status:
                continue
            if review and normalize_review_status(record.get("review_status")) != review:
                continue
            filtered.append(record)
        self._filtered_records = filtered
        self._render_rows(filtered)
        total = len(self._records)
        self.count_label.setText(f"{len(filtered)} / {total} 条记录")
        if filtered:
            self.empty_label.setVisible(False)
            self.table.setVisible(True)
            return
        self.table.setVisible(False)
        self.empty_label.setText(
            "没有符合筛选条件的记录。"
            if total
            else "还没有投稿记录。完成一次双投后会出现在这里。"
        )
        self.empty_label.setVisible(True)

    def _render_rows(self, records: list[dict[str, Any]]) -> None:
        self.table.setRowCount(0)
        for record in records:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                str(record.get("created_at", ""))[:16].replace("T", " "),
                record.get("song") or record.get("title") or "-",
                record.get("difficulty") or "-",
                record.get("accuracy") or "-",
                record.get("bvid") or "-",
                "是" if record.get("tuf_record_id") else "-",
                _status_text(record.get("status")),
                review_text(record.get("review_status")) or "-",
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, record.get("id"))
                self.table.setItem(row, column, item)
            status_item = self.table.item(row, 6)
            if status_item is not None:
                status_item.setData(
                    Qt.ItemDataRole.UserRole, str(record.get("status") or "")
                )
            review_item = self.table.item(row, 7)
            if review_item is not None:
                review_item.setData(
                    Qt.ItemDataRole.UserRole,
                    normalize_review_status(record.get("review_status")),
                )
        self.table.resizeColumnsToContents()
        self._apply_cell_colors()

    @staticmethod
    def _set_item_color(item: QTableWidgetItem, color: QColor | None) -> None:
        if color is None:
            item.setData(Qt.ItemDataRole.ForegroundRole, None)
        else:
            item.setForeground(color)

    def _apply_cell_colors(self) -> None:
        """给「审核」「状态」列上主题语义色；未同步 / 正常状态保持默认色。"""

        colors = palette(active_theme())
        success = QColor(colors["success"])
        warning = QColor(colors["warning"])
        danger = QColor(colors["danger"])
        review_colors = {
            "pending": warning,
            "approved": success,
            "declined": danger,
        }
        for row in range(self.table.rowCount()):
            status_item = self.table.item(row, 6)
            if status_item is not None:
                failed = status_item.data(Qt.ItemDataRole.UserRole) == "failed"
                self._set_item_color(status_item, danger if failed else None)
            review_item = self.table.item(row, 7)
            if review_item is not None:
                state = review_item.data(Qt.ItemDataRole.UserRole)
                self._set_item_color(review_item, review_colors.get(state))

    def refresh_theme(self) -> None:
        """主题切换后重新取语义色。"""

        self._apply_cell_colors()

    # -- 选中 / 右键菜单 ----------------------------------------------------

    def _record_at(self, row: int) -> dict[str, Any] | None:
        if 0 <= row < len(self._filtered_records):
            return self._filtered_records[row]
        return None

    def _selected_id(self) -> str:
        row = self.table.currentRow()
        if row < 0:
            return ""
        item = self.table.item(row, 0)
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""

    def _load_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        record_id = self._selected_id()
        for item in self._records:
            if item.get("id") == record_id:
                self.loadRequested.emit(item)
                return

    def _delete_selected(self) -> None:
        record_id = self._selected_id()
        if not record_id:
            return
        if QMessageBox.question(self, "删除记录", "确定删除这条历史记录？") == QMessageBox.StandardButton.Yes:
            self.deleteRequested.emit(record_id)

    def _clear(self) -> None:
        if QMessageBox.question(self, "清空历史", "确定清空全部历史记录？此操作不可撤销。") == QMessageBox.StandardButton.Yes:
            self.clearRequested.emit()

    def _copy_bvid(self, bvid: str) -> None:
        if bvid:
            QGuiApplication.clipboard().setText(bvid)

    def _open_bilibili(self, bvid: str) -> None:
        if bvid:
            QDesktopServices.openUrl(QUrl(f"https://www.bilibili.com/video/{bvid}"))

    def _build_row_menu(self, row: int) -> QMenu:
        """构造某一行记录的右键菜单（测试直接调用，不弹出）。"""

        record = self._record_at(row) or {}
        bvid = str(record.get("bvid") or "").strip()
        menu = QMenu(self)
        load_action = menu.addAction("载入到工作台")
        load_action.triggered.connect(lambda: self.loadRequested.emit(record))
        copy_action = menu.addAction("复制 BV 号")
        copy_action.setEnabled(bool(bvid))
        copy_action.triggered.connect(lambda: self._copy_bvid(bvid))
        open_action = menu.addAction("打开 B 站视频页")
        open_action.setEnabled(bool(bvid))
        open_action.triggered.connect(lambda: self._open_bilibili(bvid))
        menu.addSeparator()
        delete_action = menu.addAction("删除记录")
        delete_action.triggered.connect(self._delete_selected)
        return menu

    def _show_context_menu(self, pos: Any) -> None:
        row = self.table.indexAt(pos).row()
        if row < 0:
            return
        self.table.selectRow(row)
        menu = self._build_row_menu(row)
        try:
            menu.exec(self.table.viewport().mapToGlobal(pos))
        finally:
            menu.deleteLater()


def _review_time_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    text = text.replace("T", " ").replace("Z", "")
    return text[:16]


def _status_counts(
    items: list[dict[str, Any]], raw_counts: Any = None
) -> dict[str, int]:
    counts = {"pending": 0, "approved": 0, "declined": 0}
    if isinstance(raw_counts, dict) and any(key in raw_counts for key in counts):
        for key in counts:
            try:
                counts[key] = int(raw_counts.get(key) or 0)
            except (TypeError, ValueError):
                counts[key] = 0
        return counts
    for item in items:
        status = normalize_review_status(item.get("status"))
        if status in counts:
            counts[status] += 1
    return counts


def _field_name_text(names: Any, limit: int = 12) -> str:
    if not isinstance(names, (list, tuple)) or not names:
        return "-"
    return ", ".join(str(name) for name in names[:limit])


def _diagnostics_text(diagnostics: Any) -> str:
    """只展示字段名，不展示任何字段值。"""

    if not isinstance(diagnostics, list):
        return ""
    for diag in diagnostics:
        if not isinstance(diag, dict):
            continue
        missing = "、".join(str(item) for item in (diag.get("missing") or [])) or "-"
        return (
            f"字段诊断：缺少 {missing}；"
            f"顶层字段：{_field_name_text(diag.get('top'))}；"
            f"pass 字段：{_field_name_text(diag.get('pass'))}；"
            f"level 字段：{_field_name_text(diag.get('level'))}；"
            f"extra 字段：{_field_name_text(diag.get('extra'))}"
        )
    return ""


class TufSubmissionListDialog(QDialog):
    """只读展示 TUF 服务器上当前账号的 pass 投稿，不写任何本地记录。"""

    refreshRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("TUF 投稿记录（只读）")
        self.resize(920, 540)
        self._items: list[dict[str, Any]] = []
        self._urls: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        self.status_label = _label("正在读取 TUF 投稿记录…", "Muted")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            [
                "记录编号",
                "谱面编号",
                "曲名 · 艺术家 · 难度",
                "审核状态",
                "提交时间",
                "视频链接",
            ]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setToolTip("双击行可打开视频；链接列可用「复制链接」")
        self.table.doubleClicked.connect(self._open_row)
        self.table.setVisible(False)
        layout.addWidget(self.table, 1)

        self.footer = _label("", "Muted")
        self.footer.setWordWrap(True)
        layout.addWidget(self.footer)

        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("重新读取")
        self.refresh_button.setToolTip("再次从 TUF 读取投稿列表")
        self.refresh_button.clicked.connect(self.refreshRequested)
        self.copy_button = QPushButton("复制链接")
        self.copy_button.setToolTip("复制选中行的视频链接")
        self.copy_button.clicked.connect(self._copy_link)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.reject)
        buttons.addWidget(self.refresh_button)
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    def set_loading(self) -> None:
        self.status_label.setText("正在读取 TUF 投稿记录…")
        self.table.setRowCount(0)
        self.table.setVisible(False)
        self.footer.setText("")
        self._items = []
        self._urls = []

    def set_failed(self, message: str) -> None:
        self.status_label.setText(message or "读取 TUF 投稿记录失败")
        self.table.setRowCount(0)
        self.table.setVisible(False)
        self.footer.setText("")

    def set_result(
        self,
        data: dict[str, Any],
        local_ids: set[str] | None = None,
    ) -> None:
        """data 为 TufSubmissionListWorker.finished 的结构。"""

        items = [item for item in (data.get("items") or []) if isinstance(item, dict)]
        failed = int(data.get("failed") or 0)
        message = str(data.get("message") or "")
        if data.get("auth_error") or data.get("retryable"):
            self.set_failed(message or "TUF 未登录，请先登录后再查看投稿记录")
            return

        self._items = items
        self._urls = []
        local_ids = local_ids or set()
        counts = _status_counts(items, data.get("counts"))
        counts_text = (
            f"（待处理 {counts['pending']} / 已通过 {counts['approved']} / "
            f"未通过 {counts['declined']}）"
        )
        diagnostic_text = _diagnostics_text(data.get("diagnostics"))
        if not items:
            self.status_label.setText(message or "TUF 上还没有 pass 投稿记录。")
            self.table.setRowCount(0)
            self.table.setVisible(False)
            footer = f"共 0 条{counts_text}，读取失败 {failed} 条"
            if diagnostic_text:
                footer += f"\n{diagnostic_text}"
            self.footer.setText(footer)
            return

        note = "以下是 TUF 服务器上当前账号的 pass 投稿（只读，不修改本地记录）。"
        if failed:
            note += " 部分页读取失败，列表可能不完整。"
        if message:
            note += f"\n{message}"
        self.status_label.setText(note)

        self.table.setRowCount(0)
        for item in items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            pass_id = str(item.get("pass_id") or "").strip()
            submission_id = str(
                item.get("submission_id") or item.get("id") or ""
            ).strip()
            record_id = pass_id or submission_id or "-"
            local = bool(
                (pass_id and pass_id in local_ids)
                or (submission_id and submission_id in local_ids)
            )
            if local and record_id != "-":
                record_id = f"{record_id}（本地已有）"
            song = str(item.get("song") or item.get("title") or "").strip()
            artist = str(item.get("artist") or "").strip()
            difficulty = str(item.get("difficulty") or "").strip()
            song_text = (
                " · ".join(part for part in (song, artist, difficulty) if part)
                or "-"
            )
            link = str(item.get("video_link") or "").strip()
            if not link:
                bvid = str(item.get("bvid") or "").strip()
                link = f"https://www.bilibili.com/video/{bvid}" if bvid else ""
            self._urls.append(link)
            values = [
                record_id,
                str(item.get("level_id") or "").strip() or "-",
                song_text,
                review_text(item.get("status")) or "未知",
                _review_time_text(item.get("created_at")),
                link or "-",
            ]
            for column, text in enumerate(values):
                cell = QTableWidgetItem(text)
                if column == 0 and submission_id and submission_id != pass_id:
                    cell.setToolTip(f"submission id：{submission_id}")
                if column == 5 and link:
                    cell.setToolTip(link)
                self.table.setItem(row, column, cell)
        self.table.resizeColumnsToContents()
        self.table.setVisible(True)
        total = int(data.get("total") or len(items))
        footer = f"共 {total} 条{counts_text}，读取失败 {failed} 条"
        if diagnostic_text:
            footer += f"\n{diagnostic_text}"
        self.footer.setText(footer)

    def _open_row(self, index: Any) -> None:
        row = index.row() if hasattr(index, "row") else int(index)
        if 0 <= row < len(self._urls) and self._urls[row]:
            QDesktopServices.openUrl(QUrl(self._urls[row]))

    def _copy_link(self) -> None:
        row = self.table.currentRow()
        if not (0 <= row < len(self._urls)) or not self._urls[row]:
            return
        QGuiApplication.clipboard().setText(self._urls[row])
        self.copy_button.setText("已复制")
        QTimer.singleShot(1500, lambda: self.copy_button.setText("复制链接"))


class CopySnippetDialog(QDialog):
    def __init__(
        self,
        text: str = "",
        group: str = "自定义",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑词条" if text else "新增词条")
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)
        self.text_input = QLineEdit(text)
        self.text_input.setPlaceholderText("例如：严判击破")
        self.text_input.setMaxLength(30)
        form.addRow(_label("词条", "Muted"), self.text_input)
        self.group_combo = ChoiceCombo()
        self.group_combo.addItems(list(SNIPPET_GROUPS))
        index = self.group_combo.findText(group)
        self.group_combo.setCurrentIndex(max(0, index))
        form.addRow(_label("分组", "Muted"), self.group_combo)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.text_input.setFocus()

    def _accept(self) -> None:
        if not self.text_input.text().strip():
            QMessageBox.information(self, "词条为空", "请输入要插入的词语。")
            return
        self.accept()

    def value(self) -> tuple[str, str]:
        return self.text_input.text().strip(), self.group_combo.currentText()


class SettingsPage(QWidget):
    saved = Signal(dict)
    loginRequested = Signal()
    clearLoginRequested = Signal()
    savRequested = Signal(str)
    obsTestRequested = Signal()
    obsLaunchRequested = Signal()
    obsStartRequested = Signal()
    obsStopRequested = Signal()
    obsOpenFolderRequested = Signal(str)
    updateCheckRequested = Signal(str)
    updateOpenRequested = Signal(str)
    updateInstallRequested = Signal()
    updateCancelRequested = Signal()
    diagnosticsRequested = Signal()

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        # 上次 load()/save() 时的表单快照，用来判断是否有未保存修改。
        self._baseline: dict[str, Any] = {}
        # 编辑表单后延迟刷新「有未保存的更改」提示（防抖，约 0.5 秒）。
        self._dirty_timer = QTimer(self)
        self._dirty_timer.setSingleShot(True)
        self._dirty_timer.setInterval(450)
        self._dirty_timer.timeout.connect(self._refresh_dirty_hint)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        content = QWidget()
        outer = QVBoxLayout(content)
        outer.setContentsMargins(28, 8, 28, 18)
        outer.setSpacing(14)
        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll_area.setWidget(content)
        root.addWidget(self._scroll_area, 1)

        form_card = Card("常规", "修改后点击底部“保存设置”。")
        form = QFormLayout()
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(12)
        self.theme_combo = ChoiceCombo()
        self.theme_combo.addItems(["深色（推荐）", "浅色"])
        form.addRow(_label("主题", "Muted"), self.theme_combo)
        self.channel_combo = ChoiceCombo()
        self.channel_combo.addItem("Chrome（推荐）", "chrome")
        self.channel_combo.addItem("Edge", "msedge")
        self.channel_combo.addItem("内置 Chromium（无需安装）", "chromium")
        form.addRow(_label("浏览器", "Muted"), self.channel_combo)
        profile_row = QHBoxLayout()
        self.profile_input = QLineEdit()
        self.profile_input.setPlaceholderText("留空使用默认目录")
        browse = QPushButton("选择")
        browse.clicked.connect(self._browse_profile)
        profile_row.addWidget(self.profile_input, 1)
        profile_row.addWidget(browse)
        form.addRow(_label("浏览器配置目录", "Muted"), profile_row)
        level_dir_row = QHBoxLayout()
        self.level_dir_input = QLineEdit()
        self.level_dir_input.setPlaceholderText("留空使用默认目录")
        level_dir_browse = QPushButton("选择")
        level_dir_browse.clicked.connect(self._browse_level_dir)
        level_dir_row.addWidget(self.level_dir_input, 1)
        level_dir_row.addWidget(level_dir_browse)
        form.addRow(_label("谱面目录", "Muted"), level_dir_row)
        self.level_filename_input = QLineEdit()
        self.level_filename_input.setPlaceholderText("{song} - {charter} ({tuf_id}).ard")
        form.addRow(_label("谱面命名模板", "Muted"), self.level_filename_input)
        self.organize_levels = QCheckBox("下载时按难度分类存放（P / P14 / 曲名）")
        form.addRow("", self.organize_levels)
        self.auto_extract_levels = QCheckBox(
            "TUF 下载后自动解压成文件夹（不保留压缩包）"
        )
        form.addRow("", self.auto_extract_levels)
        self.restore_draft = QCheckBox("启动时恢复上次未完成的草稿")
        form.addRow("", self.restore_draft)
        self.partition_combo = ChoiceCombo()
        self.partition_combo.addItems(
            ["单机游戏", "游戏", "音乐", "生活", "知识", "影视", "动画", "鬼畜", "科技", "时尚"]
        )
        form.addRow(_label("默认分区", "Muted"), self.partition_combo)
        self.tags_input = QLineEdit()
        self.tags_input.setPlaceholderText("用逗号分隔")
        form.addRow(_label("默认标签", "Muted"), self.tags_input)
        form_card.body.addLayout(form)
        outer.addWidget(form_card)

        template_card = Card("文案模板", "可用变量：{song} {artist} {charter} {difficulty} {bpm} {duration} {accuracy} {tuf_url} {bvid}")
        self.title_template = QLineEdit()
        template_card.body.addWidget(_label("标题模板", "Muted"))
        template_card.body.addWidget(self.title_template)
        template_card.body.addWidget(_label("简介模板", "Muted"))
        self.desc_template = QPlainTextEdit()
        self.desc_template.setMinimumHeight(150)
        template_card.body.addWidget(self.desc_template)
        template_card.body.addWidget(hline())
        template_card.body.addWidget(_label("工作台快捷词条", "Muted"))
        snippet_hint = _label(
            "这些词会显示在「01 工作台 → 第 4 步 文案」的卡片区；"
            "点击卡片插入标题或简介的光标处。修改后点击底部「保存设置」。",
            "Faint",
        )
        snippet_hint.setWordWrap(True)
        template_card.body.addWidget(snippet_hint)
        self.snippet_table = QTableWidget(0, 2)
        self.snippet_table.setObjectName("SnippetTable")
        self.snippet_table.setHorizontalHeaderLabels(["分组", "词条"])
        self.snippet_table.horizontalHeader().setStretchLastSection(True)
        self.snippet_table.verticalHeader().setVisible(False)
        self.snippet_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.snippet_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        self.snippet_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.snippet_table.setMinimumHeight(170)
        self.snippet_table.setMaximumHeight(240)
        self.snippet_table.cellDoubleClicked.connect(lambda *_: self._edit_snippet())
        template_card.body.addWidget(self.snippet_table)
        snippet_actions = QHBoxLayout()
        self.snippet_add_button = QPushButton("新增词条")
        self.snippet_add_button.setObjectName("PrimaryButton")
        self.snippet_add_button.clicked.connect(self._add_snippet)
        self.snippet_edit_button = QPushButton("编辑")
        self.snippet_edit_button.clicked.connect(self._edit_snippet)
        self.snippet_delete_button = QPushButton("删除")
        self.snippet_delete_button.setObjectName("DangerButton")
        self.snippet_delete_button.clicked.connect(self._delete_snippet)
        self.snippet_restore_button = QPushButton("恢复内置")
        self.snippet_restore_button.setObjectName("GhostButton")
        self.snippet_restore_button.clicked.connect(self._restore_snippets)
        snippet_actions.addWidget(self.snippet_add_button)
        snippet_actions.addWidget(self.snippet_edit_button)
        snippet_actions.addWidget(self.snippet_delete_button)
        snippet_actions.addStretch(1)
        snippet_actions.addWidget(self.snippet_restore_button)
        template_card.body.addLayout(snippet_actions)
        self.snippet_count = _label("", "Faint")
        template_card.body.addWidget(self.snippet_count)
        self._snippets = normalize_copy_snippets(self.settings.get("copy_snippets"))
        self._refresh_snippet_table()
        outer.addWidget(template_card)

        tool_card = Card("工具与账号", "")
        self.tool_card = tool_card
        sav_row = QHBoxLayout()
        self.sav_input = QLineEdit()
        self.sav_input.setPlaceholderText("自动查找 data.sav，或手动指定")
        sav_browse = QPushButton("选择")
        sav_browse.clicked.connect(self._browse_sav)
        sav_check = QPushButton("校验严格判定")
        sav_check.clicked.connect(lambda: self.savRequested.emit(self.sav_input.text().strip()))
        sav_row.addWidget(self.sav_input, 1)
        sav_row.addWidget(sav_browse)
        sav_row.addWidget(sav_check)
        tool_card.body.addWidget(_label("ADOFAI 存档", "Muted"))
        tool_card.body.addLayout(sav_row)
        self.sav_status = _label("存档未校验", "Faint")
        tool_card.body.addWidget(self.sav_status)
        login_row = QHBoxLayout()
        self.login_status = _label("登录态未检查", "Muted")
        login_button = QPushButton("打开浏览器登录双站")
        login_button.setObjectName("PrimaryButton")
        login_button.clicked.connect(self.loginRequested)
        self.login_button = login_button
        login_row.addWidget(self.login_status, 1)
        login_row.addWidget(login_button)
        tool_card.body.addLayout(login_row)
        clear_row = QHBoxLayout()
        clear_hint = _label("只删除工具专用浏览器配置，不影响日常 Edge / Chrome。", "Faint")
        clear_hint.setWordWrap(True)
        clear_button = QPushButton("清除登录数据")
        clear_button.setObjectName("DangerButton")
        clear_button.setToolTip("删除工具专用浏览器配置里的 Cookie，下次需要重新登录")
        clear_button.clicked.connect(lambda: self.clearLoginRequested.emit())
        clear_row.addWidget(clear_hint, 1)
        clear_row.addWidget(clear_button)
        tool_card.body.addLayout(clear_row)
        diag_row = QHBoxLayout()
        diag_hint = _label(
            "登录失败时把这行信息发给作者：版本、系统、浏览器顺序，不含 Cookie。",
            "Faint",
        )
        diag_hint.setWordWrap(True)
        self.diagnostics_button = QPushButton("复制诊断信息")
        self.diagnostics_button.setObjectName("GhostButton")
        self.diagnostics_button.setToolTip("复制版本、系统、浏览器候选顺序与最近一次登录错误")
        self.diagnostics_button.clicked.connect(self.diagnosticsRequested)
        diag_row.addWidget(diag_hint, 1)
        diag_row.addWidget(self.diagnostics_button)
        tool_card.body.addLayout(diag_row)
        self.diagnostics_status = _label("", "Faint")
        self.diagnostics_status.setWordWrap(True)
        tool_card.body.addWidget(self.diagnostics_status)
        from ..config import base_dir

        data_label = _label(f"数据目录：{base_dir()}", "Faint")
        data_label.setWordWrap(True)
        tool_card.body.addWidget(data_label)
        outer.addWidget(tool_card)

        self.obs_card = Card(
            "录制（OBS）",
            "可选功能。只手动录制不需要配置这里。",
        )
        obs_help = _label(
            "第一次配置？按这三步：\n"
            "1. 点「启动 OBS」。\n"
            "2. 在 OBS 打开「工具 → WebSocket 服务器设置」，启用服务器并复制密码。\n"
            "3. 回到这里粘贴密码，点「测试连接」，再开始录制。",
            "Muted",
        )
        obs_help.setWordWrap(True)
        self.obs_card.body.addWidget(obs_help)

        obs_form = QFormLayout()
        obs_form.setHorizontalSpacing(20)
        obs_form.setVerticalSpacing(12)
        self.obs_password_input = QLineEdit()
        self.obs_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.obs_password_input.setPlaceholderText("OBS WebSocket 密码，默认留空")
        obs_form.addRow(_label("密码", "Muted"), self.obs_password_input)
        self.obs_card.body.addLayout(obs_form)

        obs_buttons = QHBoxLayout()
        self.obs_launch_button = QPushButton("启动 OBS")
        self.obs_launch_button.setObjectName("PrimaryButton")
        self.obs_launch_button.clicked.connect(self._on_obs_launch)
        self.obs_test_button = QPushButton("测试连接")
        self.obs_test_button.clicked.connect(self._on_obs_test)
        self.obs_start_button = QPushButton("开始录制")
        self.obs_start_button.clicked.connect(self._on_obs_start)
        self.obs_stop_button = QPushButton("停止录制")
        self.obs_stop_button.clicked.connect(self._on_obs_stop)
        self.obs_stop_button.setProperty("recording", False)
        self.obs_stop_button.setEnabled(False)
        obs_buttons.addWidget(self.obs_launch_button)
        obs_buttons.addWidget(self.obs_test_button)
        obs_buttons.addWidget(self.obs_start_button)
        obs_buttons.addWidget(self.obs_stop_button)
        obs_buttons.addStretch(1)
        self.obs_card.body.addLayout(obs_buttons)

        self.obs_status = _label(
            "未连接。先点「启动 OBS」，会自动查找 PATH、Program Files、注册表、"
            "Steam 库、用户目录；也可以直接在 OBS 里开始手动录制。",
            "Faint",
        )
        self.obs_status.setWordWrap(True)
        self.obs_card.body.addWidget(self.obs_status)

        self.obs_advanced_button = QToolButton()
        self.obs_advanced_button.setObjectName("ObsAdvancedButton")
        self.obs_advanced_button.setText("高级设置（地址 / 端口 / OBS 程序路径）")
        self.obs_advanced_button.setCheckable(True)
        self.obs_advanced_button.setChecked(False)
        self.obs_advanced_button.setArrowType(Qt.ArrowType.RightArrow)
        self.obs_advanced_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.obs_advanced_button.clicked.connect(self._toggle_obs_advanced)
        self.obs_card.body.addWidget(self.obs_advanced_button)

        self.obs_advanced_panel = QWidget()
        obs_advanced_form = QFormLayout(self.obs_advanced_panel)
        obs_advanced_form.setContentsMargins(0, 0, 0, 0)
        obs_advanced_form.setHorizontalSpacing(20)
        obs_advanced_form.setVerticalSpacing(12)
        self.obs_host_input = QLineEdit()
        self.obs_host_input.setPlaceholderText("127.0.0.1")
        obs_advanced_form.addRow(_label("地址", "Muted"), self.obs_host_input)
        self.obs_port_input = QSpinBox()
        self.obs_port_input.setRange(1, 65535)
        self.obs_port_input.setValue(4455)
        obs_advanced_form.addRow(_label("端口", "Muted"), self.obs_port_input)
        obs_exe_row = QHBoxLayout()
        self.obs_exe_path_input = QLineEdit()
        self.obs_exe_path_input.setPlaceholderText(
            "留空自动查找 obs64.exe（含 Steam 库）"
        )
        obs_exe_browse = QPushButton("选择")
        obs_exe_browse.clicked.connect(self._browse_obs_exe)
        obs_exe_row.addWidget(self.obs_exe_path_input, 1)
        obs_exe_row.addWidget(obs_exe_browse)
        obs_advanced_form.addRow(_label("OBS 程序", "Muted"), obs_exe_row)
        self.obs_advanced_panel.setVisible(False)
        self.obs_card.body.addWidget(self.obs_advanced_panel)

        obs_open_row = QHBoxLayout()
        self.obs_path = _label("", "Faint")
        self.obs_path.setWordWrap(True)
        self.obs_open_button = QPushButton("打开文件夹")
        self.obs_open_button.setObjectName("GhostButton")
        self.obs_open_button.setEnabled(False)
        self.obs_open_button.clicked.connect(self._on_obs_open_folder)
        obs_open_row.addWidget(self.obs_path, 1)
        obs_open_row.addWidget(self.obs_open_button)
        self.obs_card.body.addLayout(obs_open_row)
        outer.addWidget(self.obs_card)

        self.update_card = Card(
            "软件更新",
            "检查 GitHub Release；可下载并校验更新包，退出应用后自动替换程序。"
            "登录状态与设置保存在程序文件外，更新不会影响它们。",
        )
        version_row = QHBoxLayout()
        version_row.addWidget(_label("当前版本", "Muted"))
        current_version = _label(f"v{__version__}", "Value")
        version_row.addWidget(current_version)
        version_row.addStretch(1)
        self.update_card.body.addLayout(version_row)
        self.auto_check_updates_checkbox = QCheckBox("启动时自动检查更新")
        self.update_card.body.addWidget(self.auto_check_updates_checkbox)
        repo_row = QHBoxLayout()
        self.update_repo_input = QLineEdit()
        self.update_repo_input.setPlaceholderText("owner/repo")
        self.update_check_button = QPushButton("检查更新")
        self.update_check_button.setObjectName("PrimaryButton")
        self.update_check_button.clicked.connect(self._on_update_check)
        repo_row.addWidget(self.update_repo_input, 1)
        repo_row.addWidget(self.update_check_button)
        self.update_card.body.addLayout(repo_row)
        self.update_status = _label("未配置更新仓库", "Faint")
        self.update_status.setWordWrap(True)
        self.update_card.body.addWidget(self.update_status)
        self.update_notes = QLabel("")
        self.update_notes.setObjectName("CardHint")
        self.update_notes.setWordWrap(True)
        self.update_notes.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.update_notes.hide()
        self.update_card.body.addWidget(self.update_notes)
        update_actions = QHBoxLayout()
        update_actions.addStretch(1)
        self.update_download_button = QPushButton("打开发布页")
        self.update_download_button.setObjectName("GhostButton")
        self.update_download_button.hide()
        self.update_download_button.clicked.connect(self._on_update_open)
        update_actions.addWidget(self.update_download_button)
        self.update_install_button = QPushButton("下载并安装更新")
        self.update_install_button.setObjectName("PrimaryButton")
        self.update_install_button.hide()
        self.update_install_button.clicked.connect(self._on_update_install)
        update_actions.addWidget(self.update_install_button)
        self.update_card.body.addLayout(update_actions)
        self.update_progress_panel = QWidget()
        progress_row = QHBoxLayout(self.update_progress_panel)
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.setSpacing(8)
        self.update_progress = QProgressBar()
        self.update_progress.setObjectName("DownloadBar")
        self.update_progress.setRange(0, 100)
        self.update_progress.setValue(0)
        self.update_progress.setTextVisible(True)
        self.update_cancel_button = QPushButton("取消下载")
        self.update_cancel_button.setObjectName("GhostButton")
        self.update_cancel_button.clicked.connect(self._on_update_cancel)
        progress_row.addWidget(self.update_progress, 1)
        progress_row.addWidget(self.update_cancel_button)
        self.update_progress_panel.hide()
        self.update_card.body.addWidget(self.update_progress_panel)
        outer.addWidget(self.update_card)

        about_card = Card("关于", "本地运行，不上传任何第三方服务器。")
        about_version_row = QHBoxLayout()
        about_version_row.addWidget(_label("版本", "Muted"))
        about_version_row.addWidget(_label(f"v{__version__}", "Value"))
        about_version_row.addStretch(1)
        about_card.body.addLayout(about_version_row)
        about_author_row = QHBoxLayout()
        about_author_row.addWidget(_label("作者", "Muted"))
        about_author_row.addWidget(_label("DeanClock", "Value"))
        about_author_row.addStretch(1)
        about_card.body.addLayout(about_author_row)
        outer.addWidget(about_card)
        outer.addStretch(1)

        save_row = QHBoxLayout()
        save_row.addStretch(1)
        # 空串时高度不变（QLabel 始终占一行），不会让保存栏跳动。
        self.dirty_hint = QLabel("")
        self.dirty_hint.setToolTip("修改表单后点「保存设置」才会生效")
        set_tone(self.dirty_hint, "warn")
        save_row.addWidget(self.dirty_hint)
        save = QPushButton("保存设置")
        save.setObjectName("PrimaryButton")
        save.clicked.connect(self.save)
        save_row.addWidget(save)
        root.addLayout(save_row)
        self._wire_dirty_tracking()
        self.load()

    def _wire_dirty_tracking(self) -> None:
        """任一表单控件变化后延迟刷新脏标记（不含顶部导航与按钮）。"""

        for combo in self.findChildren(ChoiceCombo):
            combo.currentIndexChanged.connect(self._schedule_dirty_hint)
        for line_edit in self.findChildren(QLineEdit):
            line_edit.textChanged.connect(self._schedule_dirty_hint)
        for text_edit in self.findChildren(QPlainTextEdit):
            text_edit.textChanged.connect(self._schedule_dirty_hint)
        for check_box in self.findChildren(QCheckBox):
            check_box.toggled.connect(self._schedule_dirty_hint)
        for spin_box in self.findChildren(QSpinBox):
            spin_box.valueChanged.connect(self._schedule_dirty_hint)
        for double_spin in self.findChildren(QDoubleSpinBox):
            double_spin.valueChanged.connect(self._schedule_dirty_hint)

    def _schedule_dirty_hint(self, *_args: Any) -> None:
        self._dirty_timer.start()

    def _refresh_dirty_hint(self) -> None:
        self._dirty_timer.stop()
        self.dirty_hint.setText("有未保存的更改" if self.is_dirty() else "")

    def load(self) -> None:
        self.theme_combo.setCurrentIndex(0 if self.settings.get("theme") != "light" else 1)
        index = self.channel_combo.findData(self.settings.get("browser_channel") or "chrome")
        self.channel_combo.setCurrentIndex(max(0, index))
        self.profile_input.setText(self.settings.get("browser_profile_dir") or "")
        self.level_dir_input.setText(self.settings.get("level_dir") or "")
        self.level_filename_input.setText(self.settings.get("level_filename_template") or "")
        self.organize_levels.setChecked(bool(self.settings.get("organize_levels", True)))
        self.auto_extract_levels.setChecked(
            bool(self.settings.get("auto_extract_levels", True))
        )
        self.restore_draft.setChecked(bool(self.settings.get("restore_last_draft")))
        index = self.partition_combo.findText(self.settings.get("default_partition") or "单机游戏")
        self.partition_combo.setCurrentIndex(max(0, index))
        self.tags_input.setText(", ".join(self.settings.get("default_tags") or []))
        self.title_template.setText(self.settings.get("title_template") or "")
        self.desc_template.setPlainText(self.settings.get("desc_template") or "")
        self._snippets = normalize_copy_snippets(self.settings.get("copy_snippets"))
        self._refresh_snippet_table()
        self.sav_input.setText(self.settings.get("adofai_save_path") or "")
        self.obs_host_input.setText(
            str(self.settings.get("obs_host") or "127.0.0.1")
        )
        try:
            obs_port = int(self.settings.get("obs_port") or 4455)
        except (TypeError, ValueError):
            obs_port = 4455
        self.obs_port_input.setValue(max(1, min(65535, obs_port)))
        self.obs_password_input.setText(str(self.settings.get("obs_password") or ""))
        self.obs_exe_path_input.setText(str(self.settings.get("obs_exe_path") or ""))
        self.auto_check_updates_checkbox.setChecked(
            bool(self.settings.get("auto_check_updates", True))
        )
        self.update_repo_input.setText(str(self.settings.get("update_repo") or ""))
        self.update_notes.hide()
        self.update_download_button.hide()
        self.update_install_button.hide()
        self._reset_update_download_ui()
        if not str(self.settings.get("update_repo") or "").strip():
            self.set_update_status("未配置更新仓库", None)
        else:
            self.set_update_status("已配置更新仓库，点击「检查更新」", None)
        self.set_obs_advanced_visible(self._obs_advanced_should_expand())
        self._baseline = self._snapshot()
        self._refresh_dirty_hint()

    def collect(self) -> dict[str, Any]:
        return {
            "theme": "light" if self.theme_combo.currentIndex() == 1 else "dark",
            "browser_channel": self.channel_combo.currentData() or "chrome",
            "browser_profile_dir": self.profile_input.text().strip(),
            "level_dir": self.level_dir_input.text().strip(),
            "level_filename_template": self.level_filename_input.text().strip(),
            "organize_levels": self.organize_levels.isChecked(),
            "auto_extract_levels": self.auto_extract_levels.isChecked(),
            "restore_last_draft": self.restore_draft.isChecked(),
            "default_partition": self.partition_combo.currentText(),
            "default_tags": _parse_tags(self.tags_input.text()),
            "title_template": self.title_template.text(),
            "desc_template": self.desc_template.toPlainText(),
            "copy_snippets": [dict(item) for item in self._snippets],
            "adofai_save_path": self.sav_input.text().strip(),
            "obs_host": self.obs_host_input.text().strip() or "127.0.0.1",
            "obs_port": self.obs_port_input.value(),
            "obs_password": self.obs_password_input.text(),
            "obs_exe_path": self.obs_exe_path_input.text().strip(),
            "auto_check_updates": self.auto_check_updates_checkbox.isChecked(),
            "update_repo": self.update_repo_input.text().strip(),
        }

    def _snapshot(self) -> dict[str, Any]:
        return deepcopy(self.collect())

    def is_dirty(self) -> bool:
        """当前表单是否和上次 load() / save() 时不同。"""
        return self.collect() != self._baseline

    def save(self) -> None:
        """保存按钮与「离开设置页」提示共用的保存入口。"""
        values = self.collect()
        self.saved.emit(values)
        self._baseline = deepcopy(values)
        self._refresh_dirty_hint()

    def discard_changes(self) -> None:
        """丢弃未保存修改，表单复位到已保存的设置。"""
        self.load()

    def _refresh_snippet_table(self) -> None:
        self.snippet_table.setRowCount(len(self._snippets))
        for row, item in enumerate(self._snippets):
            group_item = QTableWidgetItem(item["group"])
            text_item = QTableWidgetItem(item["text"])
            for cell in (group_item, text_item):
                cell.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.snippet_table.setItem(row, 0, group_item)
            self.snippet_table.setItem(row, 1, text_item)
        self.snippet_count.setText(
            f"共 {len(self._snippets)} 条 · 修改后点击底部「保存设置」"
        )
        # 词条表也能影响设置内容，增删改后要刷新脏标记。
        timer = getattr(self, "_dirty_timer", None)
        if timer is not None:
            timer.start()

    def _selected_snippet_index(self) -> int:
        row = self.snippet_table.currentRow()
        return row if 0 <= row < len(self._snippets) else -1

    def add_snippet(self, text: str, group: str) -> int | None:
        text = (text or "").strip()
        if not text:
            return None
        group = group if group in SNIPPET_GROUPS else "自定义"
        if any(
            item["text"] == text and item["group"] == group
            for item in self._snippets
        ):
            return None
        self._snippets.append({"text": text, "group": group})
        self._refresh_snippet_table()
        index = len(self._snippets) - 1
        self.snippet_table.selectRow(index)
        return index

    def update_snippet(self, index: int, text: str, group: str) -> bool:
        if not 0 <= index < len(self._snippets):
            return False
        text = (text or "").strip()
        if not text:
            return False
        group = group if group in SNIPPET_GROUPS else "自定义"
        if any(
            item_index != index
            and item["text"] == text
            and item["group"] == group
            for item_index, item in enumerate(self._snippets)
        ):
            return False
        self._snippets[index] = {"text": text, "group": group}
        self._refresh_snippet_table()
        self.snippet_table.selectRow(index)
        return True

    def remove_snippet(self, index: int) -> bool:
        if not 0 <= index < len(self._snippets):
            return False
        del self._snippets[index]
        self._refresh_snippet_table()
        next_row = min(index, len(self._snippets) - 1)
        if next_row >= 0:
            self.snippet_table.selectRow(next_row)
        return True

    def _add_snippet(self, _checked: bool = False) -> None:
        dialog = CopySnippetDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        text, group = dialog.value()
        if self.add_snippet(text, group) is None:
            QMessageBox.information(self, "无法添加", "词条为空或已存在。")

    def _edit_snippet(self, _checked: bool = False) -> None:
        index = self._selected_snippet_index()
        if index < 0:
            QMessageBox.information(self, "先选一条", "请先在表格里选中要编辑的词条。")
            return
        item = self._snippets[index]
        dialog = CopySnippetDialog(item["text"], item["group"], parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        text, group = dialog.value()
        if not self.update_snippet(index, text, group):
            QMessageBox.information(self, "无法保存", "词条为空或与另一条重复。")

    def _delete_snippet(self, _checked: bool = False) -> None:
        index = self._selected_snippet_index()
        if index < 0:
            QMessageBox.information(self, "先选一条", "请先在表格里选中要删除的词条。")
            return
        text = self._snippets[index]["text"]
        if QMessageBox.question(
            self,
            "删除词条",
            f"确定删除「{text}」？",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.remove_snippet(index)

    def _restore_snippets(self, _checked: bool = False) -> None:
        if QMessageBox.question(
            self,
            "恢复内置词条",
            "将恢复全部内置词条，并清除你新增或修改的词条。继续吗？",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._snippets = default_copy_snippets()
        self._refresh_snippet_table()

    def _browse_profile(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择浏览器配置目录")
        if path:
            self.profile_input.setText(path)

    def _browse_level_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择谱面目录")
        if path:
            self.level_dir_input.setText(path)

    def _browse_sav(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 data.sav", "", "存档 (*.sav);;所有文件 (*.*)")
        if path:
            self.sav_input.setText(path)

    def _browse_obs_exe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 OBS 程序",
            "",
            "OBS Studio (obs64.exe);;可执行文件 (*.exe);;所有文件 (*.*)",
        )
        if path:
            self.obs_exe_path_input.setText(path)
            self.set_obs_advanced_visible(True)

    def _toggle_obs_advanced(self, checked: bool | None = None) -> None:
        visible = (
            bool(checked)
            if checked is not None
            else not self.obs_advanced_panel.isVisibleTo(self)
        )
        self.set_obs_advanced_visible(visible)

    def set_obs_advanced_visible(self, visible: bool) -> None:
        self.obs_advanced_panel.setVisible(bool(visible))
        self.obs_advanced_button.setChecked(bool(visible))
        self.obs_advanced_button.setArrowType(
            Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow
        )

    def _obs_advanced_should_expand(self) -> bool:
        host = self.obs_host_input.text().strip() or "127.0.0.1"
        return (
            host != "127.0.0.1"
            or self.obs_port_input.value() != 4455
            or bool(self.obs_exe_path_input.text().strip())
        )

    def focus_obs_section(self) -> None:
        self._scroll_area.ensureWidgetVisible(self.obs_card, 0, 12)
        self.obs_password_input.setFocus()

    def focus_login_section(self) -> None:
        """把「工具与账号」卡片滚到可见，并聚焦浏览器登录按钮。"""

        self._scroll_area.ensureWidgetVisible(self.tool_card, 0, 12)
        self.login_button.setFocus()

    def focus_update_section(self) -> None:
        """把「软件更新」卡片滚到可见。"""
        self._scroll_area.ensureWidgetVisible(self.update_card, 0, 12)

    def set_sav_result(self, message: str, ok: bool | None) -> None:
        tone = "ok" if ok else ("warn" if ok is None else "bad")
        self.sav_status.setText(message)
        set_tone(self.sav_status, tone)

    def set_login_status(self, bili: bool, tuf: bool) -> None:
        if bili and tuf:
            self.login_status.setText("B 站与 TUF 均已登录")
            set_tone(self.login_status, "ok")
        elif bili or tuf:
            self.login_status.setText(f"{'B 站' if bili else 'TUF'} 已登录，另一个未登录")
            set_tone(self.login_status, "warn")
        else:
            self.login_status.setText("双站均未登录")
            set_tone(self.login_status, "bad")

    def set_diagnostics_status(self, message: str, tone: str = "ok") -> None:
        self.diagnostics_status.setText(message)
        set_tone(self.diagnostics_status, tone)

    def _on_obs_test(self) -> None:
        self.obsTestRequested.emit()

    def _on_obs_launch(self) -> None:
        self.obsLaunchRequested.emit()

    def _on_obs_start(self) -> None:
        self.obsStartRequested.emit()

    def _on_obs_stop(self) -> None:
        self.obsStopRequested.emit()

    def _on_obs_open_folder(self) -> None:
        path = self.obs_path.property("folder") or ""
        if path:
            self.obsOpenFolderRequested.emit(str(path))

    def set_obs_busy(self, busy: bool, text: str = "") -> None:
        self.obs_launch_button.setEnabled(not busy)
        self.obs_test_button.setEnabled(not busy)
        recording = bool(self.obs_stop_button.property("recording"))
        self.obs_stop_button.setEnabled(not busy and recording)
        self.obs_start_button.setEnabled(not busy and not recording)
        if text:
            self.set_obs_status(text, None)

    def set_obs_status(self, message: str, level: str | None) -> None:
        self.obs_status.setText(message)
        set_tone(self.obs_status, level or "faint")

    def set_obs_recording(self, recording: bool) -> None:
        self.obs_stop_button.setProperty("recording", recording)
        self.obs_stop_button.setEnabled(recording)
        self.obs_start_button.setEnabled(not recording)

    def set_obs_output_path(self, path: str) -> None:
        self.obs_path.setText(f"最近保存：{path}" if path else "")
        self.obs_path.setProperty("folder", str(Path(path).parent) if path else "")
        self.obs_open_button.setEnabled(bool(path))

    def _on_update_check(self) -> None:
        self.updateCheckRequested.emit(self.update_repo_input.text().strip())

    def _on_update_open(self) -> None:
        url = str(self.update_download_button.property("url") or "")
        if url:
            self.updateOpenRequested.emit(url)

    def _on_update_install(self) -> None:
        self.updateInstallRequested.emit()

    def _on_update_cancel(self) -> None:
        self.update_cancel_button.setEnabled(False)
        self.set_update_status("正在取消下载…", None)
        self.updateCancelRequested.emit()

    def set_update_busy(self, busy: bool) -> None:
        self.update_check_button.setEnabled(not busy)
        self.update_check_button.setText("检查中…" if busy else "检查更新")
        self.update_repo_input.setEnabled(not busy)
        if busy:
            self.set_update_status("正在检查更新…", None)

    def set_update_status(self, message: str, level: str | None) -> None:
        self._update_status_level = level
        self.update_status.setText(message)
        set_tone(self.update_status, level or "faint")

    def refresh_theme(self) -> None:
        level = getattr(self, "_update_status_level", None)
        self.set_update_status(self.update_status.text(), level)

    # -- 应用内下载更新 -----------------------------------------------------

    def _reset_update_download_ui(self) -> None:
        """下载结束/取消/失败后，把进度区恢复成可再次下载的状态。"""
        self.update_progress_panel.hide()
        self.update_progress.setValue(0)
        self.update_cancel_button.setEnabled(True)
        self.update_install_button.setEnabled(True)
        self.update_install_button.setText("下载并安装更新")
        self.set_update_busy(False)

    def set_update_download_started(self) -> None:
        self.update_install_button.setEnabled(False)
        self.update_install_button.setText("下载中…")
        self.update_check_button.setEnabled(False)
        self.update_repo_input.setEnabled(False)
        self.update_progress.setValue(0)
        self.update_progress_panel.show()
        self.update_cancel_button.setEnabled(True)
        self.set_update_status("正在下载更新包…", None)

    def set_update_download_progress(self, received: int, total: int) -> None:
        received = max(0, int(received or 0))
        total = max(0, int(total or 0))
        if total > 0:
            percent = max(0, min(100, round(received * 100 / total)))
            self.update_progress.setValue(percent)
            self.set_update_status(
                f"正在下载更新包：{percent}%"
                f"（{format_bytes(received)} / {format_bytes(total)}）",
                None,
            )
        else:
            self.set_update_status(
                f"正在下载更新包：{format_bytes(received)}", None
            )

    def set_update_download_stage(self, stage: str) -> None:
        if stage == "checksum":
            self.set_update_status("正在校验 SHA256…", None)

    def set_update_download_cancelling(self) -> None:
        self.update_cancel_button.setEnabled(False)
        self.set_update_status("正在取消下载…", None)

    def set_update_download_finished(
        self, result: dict[str, Any], *, auto_install: bool = True
    ) -> None:
        verified = bool(result.get("checksum_verified"))
        self.update_progress.setValue(100)
        self.update_progress_panel.hide()
        self.update_cancel_button.setEnabled(True)
        self.update_install_button.setEnabled(False)
        self.update_install_button.setText("已下载")
        self.set_update_busy(False)
        if verified:
            message = "更新包已下载并通过 SHA256 校验"
        else:
            message = "更新包已下载（发布未提供 SHA256 校验文件，已跳过校验）"
        if auto_install:
            message += "，退出应用后自动替换"
        else:
            message += "；当前是源码运行，不会自动替换"
        self.set_update_status(
            message, "ok" if verified and auto_install else "warn"
        )

    def set_update_download_error(self, message: str) -> None:
        self._reset_update_download_ui()
        self.set_update_status(f"更新下载失败：{message}", "bad")

    def set_update_download_cancelled(self) -> None:
        self._reset_update_download_ui()
        self.set_update_status("已取消下载更新", None)

    def show_update_result(self, result: dict[str, Any]) -> None:
        latest = str(result.get("latest") or "")
        current = str(result.get("current") or __version__)
        self._reset_update_download_ui()
        if result.get("has_update"):
            assets = result.get("assets") or []
            exe_asset = pick_exe_asset(assets)
            message = f"发现新版本 {latest}（当前 v{current}）"
            if exe_asset is None:
                message += "；该版本没有 exe 安装包，请打开发布页手动下载"
            self.set_update_status(message, "warn")
            body = " ".join(str(result.get("body") or "").split())
            if len(body) > 320:
                body = body[:320].rstrip() + "…"
            self.update_notes.setText(body or "该版本没有填写更新说明。")
            self.update_notes.show()
            self.update_download_button.setProperty(
                "url", str(result.get("html_url") or "")
            )
            self.update_download_button.show()
            self.update_install_button.setVisible(exe_asset is not None)
        else:
            self.set_update_status(
                f"未发现新版本（当前 v{current}，最新 Release {latest}）",
                "ok",
            )
            self.update_notes.hide()
            self.update_download_button.hide()
            self.update_install_button.hide()

    def set_update_error(self, message: str) -> None:
        self.update_notes.hide()
        self.update_download_button.hide()
        self.update_install_button.hide()
        self._reset_update_download_ui()
        self.set_update_status(message, "bad")


class LevelProfileDialog(QDialog):
    def __init__(self, profile: ARDProfile, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("谱面详档")
        self.resize(660, 540)
        layout = QVBoxLayout(self)
        title = _label(Path(profile.path).name or "谱面详档", "CardTitle")
        layout.addWidget(title)
        detail = QPlainTextEdit()
        detail.setReadOnly(True)
        detail.setPlainText(_format_profile(profile))
        layout.addWidget(detail, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("关闭")
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


def _format_profile(profile: ARDProfile) -> str:
    if profile.error:
        return "解析失败：" + profile.error
    lines = [
        "文件：" + (Path(profile.path).name or "-"),
        "物量（tile）：" + str(profile.tile_count),
        "自动/无旋转格：" + str(profile.zero_angle_count),
        "旋转格：" + str(profile.midspin_count),
        "时长：" + _duration_text(profile.song_duration_ms),
        "BPM：" + str(profile.bpm or "-"),
        "版本：" + str(profile.version or "-"),
        "默认判定音：" + (profile.hitsound or "-"),
        "谱面作者：" + (profile.author or "-"),
        "曲名：" + (profile.song or "-"),
        "曲师：" + (profile.artist or "-"),
        "音频：" + (profile.song_filename or "-"),
        "",
        "事件分布：",
    ]
    if profile.event_summary:
        for name, count in sorted(profile.event_summary.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"  {name}：{count}")
    else:
        lines.append("  （无）")
    if profile.notes:
        lines.append("")
        lines.extend(profile.notes)
    return "\n".join(lines)


def _format_profile_summary(profile: dict[str, Any]) -> str:
    if profile.get("error"):
        return "谱面详档解析失败：" + str(profile["error"])
    events = profile.get("event_summary") or {}
    top = "、".join(
        f"{name} {count}"
        for name, count in sorted(events.items(), key=lambda item: (-item[1], item[0]))[:5]
    )
    return (
        f"物量 {profile.get('tile_count', 0)}"
        f" · 自动/无旋转 {profile.get('zero_angle_count', 0)}"
        f" · 旋转格 {profile.get('midspin_count', 0)}"
        f" · 时长 {_duration_text(profile.get('song_duration_ms'))}"
        f" · 事件：{top or '无'}"
    )


def _duration_text(value: Any) -> str:
    try:
        milliseconds = float(value or 0)
    except (TypeError, ValueError):
        return "-"
    if milliseconds <= 0:
        return "-"
    seconds = int(round(milliseconds / 1000))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _parse_tags(text: str) -> list[str]:
    import re

    parts = re.split(r"[,，;；\n]+", text or "")
    result: list[str] = []
    for part in parts:
        tag = part.strip()
        if tag and tag not in result:
            result.append(tag)
    return result


def _status_text(status: str | None) -> str:
    return {
        "draft": "草稿",
        "bili_only": "仅 B 站",
        "bili_uploaded": "已上 B 站",
        "submitted": "已双投",
        "failed": "失败",
    }.get(status or "", status or "-")
