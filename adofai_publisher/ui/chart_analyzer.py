from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..chart_analyzer import ChartAnalysis, analyze_chart
from ..models import LevelInfo
from .theme import active_theme

CHART_SUFFIXES = (".adofai", ".ard", ".zip", ".adozip")


class ChartAnalyzerDialog(QDialog):
    addRequested = Signal(object)
    publishRequested = Signal(object)
    # 用户在对话框里输入 TUF 编号后请求在线分析；由主窗口起后台 worker。
    onlineAnalyzeRequested = Signal(int)

    def __init__(
        self,
        parent: QWidget | None = None,
        theme: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme or active_theme()
        self.analysis: ChartAnalysis | None = None
        self._online_level: LevelInfo | None = None
        self.setObjectName("ChartAnalyzerDialog")
        self.setWindowTitle("谱面文件分析")
        self.resize(720, 620)
        self.setAcceptDrops(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 14)
        outer.setSpacing(12)

        title = QLabel("谱面文件分析")
        title.setObjectName("PageTitle")
        outer.addWidget(title)
        subtitle = QLabel(
            "拖入 .adofai、.ard、.zip 或 .adozip，读取 BPM、物量、midspin 和变速信息。"
            "时长与密度为按 BPM / 变速事件估算。"
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        online_row = QHBoxLayout()
        online_label = QLabel("TUF 编号")
        online_label.setObjectName("Faint")
        self.online_input = QLineEdit()
        self.online_input.setPlaceholderText("例如 17776")
        self.online_input.setMaximumWidth(160)
        self.online_input.setToolTip("输入 TUF 谱面编号，直接分析服务器上的公开谱面 JSON")
        self.online_input.returnPressed.connect(self._request_online)
        self.online_button = QPushButton("分析在线谱面")
        self.online_button.setObjectName("GhostButton")
        self.online_button.clicked.connect(self._request_online)
        online_hint = QLabel("直接读取公开谱面 JSON，不下载整个谱面包")
        online_hint.setObjectName("Faint")
        online_row.addWidget(online_label)
        online_row.addWidget(self.online_input)
        online_row.addWidget(self.online_button)
        online_row.addWidget(online_hint, 1)
        outer.addLayout(online_row)

        self.drop_area = QFrame()
        self.drop_area.setObjectName("DropArea")
        drop_box = QVBoxLayout(self.drop_area)
        drop_box.setContentsMargins(18, 22, 18, 22)
        drop_box.setSpacing(4)
        drop_title = QLabel("把谱面文件拖到这里")
        drop_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_title.setStyleSheet("font-size: 15px; font-weight: 600;")
        drop_hint = QLabel(
            "支持 .adofai、.ard、.zip、.adozip 谱面包，也可以拖入包含谱面的文件夹"
        )
        drop_hint.setObjectName("Faint")
        drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_box.addWidget(drop_title)
        drop_box.addWidget(drop_hint)
        outer.addWidget(self.drop_area)

        file_row = QHBoxLayout()
        self.path_label = QLabel("还没有选择文件")
        self.path_label.setObjectName("Muted")
        self.path_label.setWordWrap(True)
        choose = QPushButton("选择文件")
        choose.setObjectName("GhostButton")
        choose.clicked.connect(self._choose_file)
        file_row.addWidget(self.path_label, 1)
        file_row.addWidget(choose)
        outer.addLayout(file_row)

        result_frame = QFrame()
        result_frame.setObjectName("Card")
        result_layout = QGridLayout(result_frame)
        result_layout.setContentsMargins(16, 14, 16, 14)
        result_layout.setHorizontalSpacing(18)
        result_layout.setVerticalSpacing(8)
        self.values: dict[str, QLabel] = {}
        rows = [
            ("song", "曲名"),
            ("artist", "曲师"),
            ("author", "谱师"),
            ("game_difficulty", "游戏难度"),
            ("bpm", "BPM"),
            ("duration", "歌曲时长"),
            ("chart_duration", "谱面估算"),
            ("tiles", "物量"),
            ("midspin", "midspin"),
            ("density", "物量密度"),
            ("speed", "变速范围"),
            ("tuf", "TUF 编号"),
            ("events", "事件"),
        ]
        for index, (key, text) in enumerate(rows):
            row, col = divmod(index, 2)
            label = QLabel(text)
            label.setObjectName("Faint")
            value = QLabel("-")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.values[key] = value
            result_layout.addWidget(label, row, col * 2)
            result_layout.addWidget(value, row, col * 2 + 1)
        result_layout.setColumnStretch(1, 1)
        result_layout.setColumnStretch(3, 1)
        outer.addWidget(result_frame)

        self.status = QLabel("等待选择谱面文件。")
        self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        outer.addWidget(self.status, 1)

        actions = QHBoxLayout()
        self.add_button = QPushButton("加入本地谱面库")
        self.add_button.setObjectName("PrimaryButton")
        self.add_button.setEnabled(False)
        self.add_button.clicked.connect(self._emit_add)
        self.publish_button = QPushButton("用这张谱面去投稿")
        self.publish_button.setObjectName("PrimaryButton")
        self.publish_button.setEnabled(False)
        self.publish_button.setToolTip("未绑定 TUF 时也可以先准备 B 站投稿")
        self.publish_button.clicked.connect(self._emit_publish)
        close = QPushButton("关闭")
        close.setObjectName("GhostButton")
        close.clicked.connect(self.reject)
        actions.addWidget(self.add_button)
        actions.addWidget(self.publish_button)
        actions.addStretch(1)
        actions.addWidget(close)
        outer.addLayout(actions)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._first_local_path(event):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        path = self._first_local_path(event)
        if path:
            self.load_path(path)
            event.acceptProposedAction()

    @staticmethod
    def _first_local_path(event: QDragEnterEvent | QDropEvent) -> Path | None:
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            if url.isLocalFile():
                return Path(url.toLocalFile())
        return None

    def _choose_file(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self,
            "选择谱面文件",
            "",
            "谱面文件 (*.adofai *.ard *.zip *.adozip);;所有文件 (*.*)",
        )
        if path:
            self.load_path(path)

    def _resolve_chart(self, path: Path) -> Path | None:
        if path.is_file() and path.suffix.lower() in CHART_SUFFIXES:
            return path
        if not path.is_dir():
            return None
        preferred = path / "level.adofai"
        if preferred.is_file():
            return preferred
        for suffix in CHART_SUFFIXES:
            matches = sorted(path.glob(f"*{suffix}"))
            if matches:
                return matches[0]
        return None

    def load_path(self, path: Path | str) -> ChartAnalysis:
        target = self._resolve_chart(Path(path))
        if target is None:
            self.analysis = None
            self._online_level = None
            self.path_label.setText(str(path))
            self.status.setText("没有找到 .adofai、.ard、.zip 或 .adozip 文件。")
            self.add_button.setEnabled(False)
            self.publish_button.setEnabled(False)
            return ChartAnalysis(path=str(path), error="没有找到谱面文件")

        analysis = analyze_chart(target)
        self._present_analysis(analysis)
        return analysis

    def _request_online(self) -> None:
        text = self.online_input.text().strip().lstrip("#").strip()
        if not text.isdigit() or int(text) <= 0:
            self.status.setText("请输入正确的 TUF 编号（纯数字，例如 17776）。")
            return
        self.request_online(int(text))

    def request_online(self, level_id: int) -> None:
        """请求分析在线谱面；实际网络请求由主窗口的后台 worker 完成。"""
        number = int(level_id)
        self.online_input.setText(str(number))
        self.path_label.setText(f"TUF #{number} · 在线谱面")
        self.status.setText(f"正在读取 TUF #{number} 的在线谱面…")
        self.add_button.setEnabled(False)
        self.publish_button.setEnabled(False)
        self.onlineAnalyzeRequested.emit(number)

    def present_online(
        self,
        analysis: ChartAnalysis,
        level: LevelInfo | None = None,
    ) -> None:
        self._present_analysis(analysis, level=level)

    def _present_analysis(
        self,
        analysis: ChartAnalysis,
        level: LevelInfo | None = None,
    ) -> None:
        self.analysis = analysis
        self._online_level = level if analysis.source == "tuf" else None
        online = analysis.source == "tuf"
        if analysis.path:
            self.path_label.setText(analysis.path)
        elif analysis.tuf_id:
            self.path_label.setText(f"TUF #{analysis.tuf_id} · 在线谱面")
        if not analysis.ok:
            self.status.setText(f"解析失败：{analysis.error}")
            self.add_button.setEnabled(False)
            self.publish_button.setEnabled(False)
            self._clear_values()
            if analysis.tuf_id:
                self.values["tuf"].setText(f"#{analysis.tuf_id}")
            return

        self._fill_values(analysis)
        if online:
            self.status.setText(
                "在线谱面解析完成。可以直接去投稿；在线谱面没有本地文件，"
                "不能加入本地库。"
            )
            self.add_button.setEnabled(False)
            self.add_button.setToolTip(
                "在线谱面没有本地文件；需要加入本地库请先下载谱面"
            )
            self.publish_button.setEnabled(True)
        else:
            self.status.setText(
                "解析完成。确认信息后可以加入本地库或直接去投稿。"
            )
            self.add_button.setEnabled(True)
            self.add_button.setToolTip("")
            self.publish_button.setEnabled(True)

    def _clear_values(self) -> None:
        for label in self.values.values():
            label.setText("-")

    def _fill_values(self, analysis: ChartAnalysis) -> None:
        bpm = f"{analysis.bpm:g}" if analysis.bpm else "-"
        values = {
            "song": analysis.song or "(未命名谱面)",
            "artist": analysis.artist or "-",
            "author": analysis.author or "-",
            "game_difficulty": analysis.game_difficulty or "-",
            "bpm": bpm,
            "duration": analysis.duration_text,
            "chart_duration": analysis.chart_duration_text,
            "tiles": f"{analysis.tile_count:,}" if analysis.tile_count else "-",
            "midspin": str(analysis.midspin_count),
            "density": analysis.density_text,
            "speed": analysis.speed_text,
            "tuf": f"#{analysis.tuf_id}" if analysis.tuf_id else "未绑定",
            "events": analysis.event_text,
        }
        for key, text in values.items():
            self.values[key].setText(text)

    def level(self) -> LevelInfo | None:
        analysis = self.analysis
        if analysis is None or not analysis.ok:
            return None
        if analysis.source == "tuf" and self._online_level is not None:
            return self._online_level
        return LevelInfo(
            tuf_id=analysis.tuf_id,
            song=analysis.song,
            artist=analysis.artist,
            charter=analysis.author,
            bpm=analysis.bpm,
            duration_ms=analysis.song_duration_ms or analysis.chart_duration_ms,
            tilecount=analysis.tile_count or None,
            midspin_count=analysis.midspin_count,
        )

    def _emit_add(self) -> None:
        level = self.level()
        if level is not None:
            self.addRequested.emit(level)

    def _emit_publish(self) -> None:
        level = self.level()
        if level is not None:
            self.publishRequested.emit(level)

    def set_status(self, message: str) -> None:
        self.status.setText(message)
