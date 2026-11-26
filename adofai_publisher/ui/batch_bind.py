from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..batch_match import (
    RowMatch,
    check_numeric,
    classify_candidates,
    describe_check,
    format_level_quad,
    format_local_quad,
    local_missing,
    local_numbers,
)
from ..level_paths import entry_band
from ..models import LevelInfo
from .theme import active_theme, palette


class BatchBindDialog(QDialog):
    searchRequested = Signal(list)
    bindRequested = Signal(list)
    cancelSearchRequested = Signal()

    def __init__(
        self,
        entries: list[dict[str, Any]],
        theme: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette(theme or active_theme())
        self._entries = [dict(entry) for entry in entries]
        self._rows: dict[str, int] = {}
        self._matches: dict[str, RowMatch] = {}
        self._selected: dict[str, LevelInfo | None] = {}
        self._status_tone: dict[str, str] = {}
        self._completed = 0
        self._total = 0
        self._searching = False
        self.setObjectName("BatchBindDialog")
        self.setWindowTitle("批量绑定助手")
        self.resize(1080, 680)
        self._build_ui()
        self._fill_rows()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        title = QLabel("批量绑定 TUF 编号")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        hint = QLabel(
            "先自动搜索候选，再复核每行的候选与勾选状态。绑定只更新谱面库，不移动或重命名本地文件。"
        )
        hint.setObjectName("Faint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        toolbar = QHBoxLayout()
        self.search_button = QPushButton("自动搜索全部候选")
        self.search_button.setObjectName("PrimaryButton")
        self.search_button.setFixedWidth(170)
        self.search_button.clicked.connect(self._start_search)
        self.stop_button = QPushButton("停止搜索")
        self.stop_button.setObjectName("GhostButton")
        self.stop_button.setFixedWidth(96)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_search)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        self.progress.hide()
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("Faint")
        toolbar.addWidget(self.search_button)
        toolbar.addWidget(self.stop_button)
        toolbar.addWidget(self.progress, 1)
        toolbar.addWidget(self.progress_label)
        layout.addLayout(toolbar)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["选择", "本地谱面", "难度", "TUF 候选", "状态"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 46)
        self.table.setColumnWidth(2, 68)
        self.table.setColumnWidth(3, 350)
        self.table.setColumnWidth(4, 240)
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.itemChanged.connect(self._update_bind_button)
        layout.addWidget(self.table, 1)

        footer = QHBoxLayout()
        self.summary = QLabel("")
        self.summary.setObjectName("Faint")
        footer.addWidget(self.summary)
        footer.addStretch(1)
        self.bind_button = QPushButton("绑定已勾选 (0)")
        self.bind_button.setObjectName("PrimaryButton")
        self.bind_button.setEnabled(False)
        self.bind_button.clicked.connect(self._emit_bind)
        close = QPushButton("关闭")
        close.setObjectName("GhostButton")
        close.clicked.connect(self.reject)
        footer.addWidget(self.bind_button)
        footer.addWidget(close)
        layout.addLayout(footer)

    def _fill_rows(self) -> None:
        self.table.setRowCount(len(self._entries))
        for row, entry in enumerate(self._entries):
            key = str(entry.get("key") or "")
            self._rows[key] = row

            check = QTableWidgetItem()
            check.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            check.setCheckState(Qt.CheckState.Checked)
            check.setData(Qt.ItemDataRole.UserRole, key)
            self.table.setItem(row, 0, check)

            song = LevelInfo.from_dict(entry)
            name = song.song if song and song.song else Path(str(entry.get("path") or "")).stem
            name_item = QTableWidgetItem(name or "(未命名)")
            name_item.setToolTip(str(entry.get("folder") or entry.get("path") or ""))
            self.table.setItem(row, 1, name_item)

            difficulty = entry_band(entry) or str(entry.get("collection") or "") or "未评级"
            diff_item = QTableWidgetItem(difficulty)
            diff_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 2, diff_item)

            candidate = QTableWidgetItem("搜索后点击选择")
            candidate.setToolTip("自动搜索完成后，点击此单元格选择 TUF 候选")
            self.table.setItem(row, 3, candidate)

            status = QTableWidgetItem("等待搜索")
            status.setForeground(QColor(self._palette["faint"]))
            self._status_tone[key] = "faint"
            self.table.setItem(row, 4, status)
        self.summary.setText(f"共 {len(self._entries)} 条未匹配谱面")
        self._update_bind_button()

    def refresh_theme(self) -> None:
        """主题切换后更新缓存调色板并重绘状态列，不动文本与勾选。"""
        self._palette = palette(active_theme())
        for key, tone in self._status_tone.items():
            row = self._rows.get(key)
            if row is None:
                continue
            status = self.table.item(row, 4)
            if status is not None:
                status.setForeground(QColor(self._palette.get(tone, self._palette["muted"])))
        self.table.viewport().update()

    def targets(self) -> list[dict[str, Any]]:
        """交给后台 worker 的搜索目标；数值字段已换算成 TUF 口径。"""
        targets: list[dict[str, Any]] = []
        for entry in self._entries:
            key = str(entry.get("key") or "")
            if not key:
                continue
            level = LevelInfo.from_dict(entry)
            name = (level.song if level else "") or Path(str(entry.get("path") or "")).stem
            local = local_numbers(entry)
            if not name and local is None:
                continue
            targets.append(
                {
                    "key": key,
                    "name": name,
                    "bpm": local.bpm if local is not None else None,
                    "tilecount": local.tilecount if local is not None else None,
                    "midspin_count": local.midspin if local is not None else None,
                    "auto_tile_count": local.auto if local is not None else None,
                    "playable_hits": entry.get("playable_hits"),
                    "numeric_ready": local is not None,
                    "numeric_missing": local_missing(entry),
                }
            )
        return targets

    def _start_search(self) -> None:
        targets = self.targets()
        if not targets:
            self.summary.setText("没有可搜索的未匹配谱面")
            return
        self._searching = True
        self._completed = 0
        self._total = len(targets)
        self.progress.setRange(0, self._total)
        self.progress.setValue(0)
        self.progress.show()
        self.progress_label.setText(f"0 / {self._total}")
        self.search_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._matches.clear()
        self._selected.clear()
        for row in range(self.table.rowCount()):
            candidate = self.table.item(row, 3)
            if candidate is not None:
                candidate.setText("等待搜索")
                candidate.setToolTip("正在搜索 TUF 候选…")
            status = self.table.item(row, 4)
            if status is not None:
                status.setText("搜索中…")
                status.setForeground(QColor(self._palette["faint"]))
                key = str(self._entries[row].get("key") or "") if row < len(self._entries) else ""
                if key:
                    self._status_tone[key] = "faint"
        self.searchRequested.emit(targets)

    def _stop_search(self) -> None:
        self.stop_button.setEnabled(False)
        self.progress_label.setText("正在停止…")
        self.cancelSearchRequested.emit()

    def set_row_results(self, key: str, payload: list[dict[str, Any]]) -> None:
        row = self._rows.get(key)
        if row is None:
            return
        self._completed += 1
        self.progress.setValue(min(self._completed, self._total))
        self.progress_label.setText(f"{self._completed} / {self._total}")
        entry = self._entries[row]
        levels: list[LevelInfo] = []
        for item in payload:
            level = LevelInfo.from_dict(item)
            if level is not None and level.tuf_id:
                levels.append(level)
        match = classify_candidates(entry, levels)
        self._matches[key] = match
        self._selected[key] = match.selected

        candidate = self.table.item(row, 3)
        if candidate is not None:
            if match.selected is not None:
                candidate.setText(self._candidate_label(match.selected))
            elif match.candidates:
                candidate.setText(
                    f"点击选择 · 最佳 {self._candidate_label(match.candidates[0].level)}"
                )
            else:
                candidate.setText("无候选 · 点击查看")
            candidate.setToolTip(self._row_tooltip(match))

        status = self.table.item(row, 4)
        check = self.table.item(row, 0)
        if status is None:
            return
        if match.kind == "none":
            status.setText("无候选")
            tone = "faint"
            if check is not None:
                check.setCheckState(Qt.CheckState.Unchecked)
        elif match.kind == "numeric_unique":
            status.setText(f"数值唯一 · {format_local_quad(match.local)}")
            tone = "success"
            if check is not None:
                check.setCheckState(Qt.CheckState.Checked)
        elif match.kind == "numeric_multi":
            status.setText(f"数值相同 · {match.equal_count} 张")
            tone = "warning"
            if check is not None:
                check.setCheckState(Qt.CheckState.Unchecked)
        else:
            if match.incomplete_only:
                status.setText("数值不完整 · 需确认")
            else:
                percent = round(match.best_score * 100)
                status.setText(f"仅名称匹配 · {percent}% · 需确认")
            tone = "warning"
            if check is not None:
                check.setCheckState(Qt.CheckState.Unchecked)
        status.setForeground(QColor(self._palette[tone]))
        self._status_tone[key] = tone
        self._update_bind_button()

    def _summary_text(self) -> str:
        counts = Counter(match.kind for match in self._matches.values())
        incomplete = sum(
            1 for match in self._matches.values() if match.incomplete_only
        )
        parts = [
            f"数值唯一 {counts.get('numeric_unique', 0)}",
            f"数值相同 {counts.get('numeric_multi', 0)}",
            f"仅名称 {counts.get('name_only', 0) - incomplete}",
            f"无候选 {counts.get('none', 0)}",
        ]
        if incomplete:
            parts.append(f"数值不完整 {incomplete}")
        return " · ".join(parts)

    def set_search_finished(self, count: int) -> None:
        self._searching = False
        self.search_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.progress.hide()
        self.progress_label.setText("")
        self.summary.setText(
            f"搜索完成：{count} / {self._total} 条 · {self._summary_text()}"
        )
        self._update_bind_button()

    def set_error(self, message: str) -> None:
        self._searching = False
        self.search_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.progress.hide()
        self.progress_label.setText("")
        self.summary.setText(message or "搜索失败")

    @staticmethod
    def _candidate_label(level: LevelInfo) -> str:
        song = (level.song or "(未命名)").strip()
        if len(song) > 34:
            song = song[:33] + "…"
        charter = (level.charter or "未知谱师").strip()
        if len(charter) > 18:
            charter = charter[:17] + "…"
        return (
            f"#{level.tuf_id} · {song} · "
            f"{level.difficulty or '未评级'} · {charter}"
        )

    @staticmethod
    def _row_tooltip(match: RowMatch) -> str:
        lines: list[str] = []
        if match.local is not None:
            lines.append(
                f"本地数值：{format_local_quad(match.local)}"
                "（BPM / 总格 / 中旋 / 自动砖）"
            )
            if match.local.total_tilecount != match.local.tilecount:
                lines.append(
                    f"匹配口径：物量按 TUF 口径 {match.local.tilecount} 查询"
                    f"（总格 − 中旋）"
                )
        else:
            reasons = match.local_missing
            if reasons == ("解析失败",):
                lines.append("本地解析失败，只按名称匹配")
            elif reasons:
                lines.append(
                    "本地数值不完整（缺 " + "、".join(reasons) + "），只按名称匹配"
                )
            else:
                lines.append("本地数值不完整，只按名称匹配")
        if match.candidates:
            lines.append("点击选择候选：")
            for item in match.candidates:
                level = item.level
                lines.append(
                    f"{describe_check(item.check)} · {round(item.score * 100)}% · "
                    f"#{level.tuf_id} · {level.song or '(未命名)'} · "
                    f"{level.difficulty or '未评级'} · "
                    f"{level.charter or '未知谱师'} · "
                    f"数值 {format_level_quad(level)}"
                )
        else:
            lines.append("没有搜索到 TUF 候选")
        return "\n".join(lines)

    def _on_cell_clicked(self, row: int, column: int) -> None:
        if column != 3:
            return
        check = self.table.item(row, 0)
        if check is None:
            return
        key = str(check.data(Qt.ItemDataRole.UserRole) or "")
        if key:
            self._open_candidate_menu(row, key)

    def _open_candidate_menu(self, row: int, key: str) -> None:
        match = self._matches.get(key)
        candidates = match.candidates if match is not None else []
        selected = self._selected.get(key)
        menu = QMenu(self)
        none_action = menu.addAction("不绑定")
        none_action.setCheckable(True)
        none_action.setChecked(selected is None)
        none_action.triggered.connect(
            lambda _checked=False: self._select_candidate(row, None)
        )
        if candidates:
            menu.addSeparator()
            for item in candidates:
                level = item.level
                label = self._candidate_label(level)
                if item.equal:
                    label = "✓ " + label
                action = menu.addAction(label)
                action.setCheckable(True)
                action.setToolTip(describe_check(item.check))
                action.setChecked(
                    selected is not None
                    and str(selected.tuf_id) == str(level.tuf_id)
                )
                action.triggered.connect(
                    lambda _checked=False, chosen=level: self._select_candidate(
                        row, chosen
                    )
                )
        else:
            menu.addSeparator()
            empty = menu.addAction("搜索中…" if self._searching else "尚未搜索到候选")
            empty.setEnabled(False)
        menu.setToolTipsVisible(True)
        item = self.table.item(row, 3)
        if item is not None:
            position = self.table.viewport().mapToGlobal(
                self.table.visualItemRect(item).bottomLeft()
            )
            menu.exec(position)
        else:
            menu.exec()

    def _select_candidate(self, row: int, level: LevelInfo | None) -> None:
        check = self.table.item(row, 0)
        status = self.table.item(row, 4)
        candidate = self.table.item(row, 3)
        if check is None or status is None or candidate is None:
            return
        key = str(check.data(Qt.ItemDataRole.UserRole) or "")
        self._selected[key] = level
        if level is None:
            candidate.setText("不绑定 · 点击选择")
            status.setText("不绑定")
            status.setForeground(QColor(self._palette["muted"]))
            self._status_tone[key] = "muted"
            check.setCheckState(Qt.CheckState.Unchecked)
        else:
            candidate.setText(self._candidate_label(level))
            match = self._matches.get(key)
            entry = self._entries[row] if row < len(self._entries) else {}
            compare = next(
                (
                    item.check
                    for item in (match.candidates if match is not None else [])
                    if str(item.level.tuf_id) == str(level.tuf_id)
                ),
                None,
            )
            if compare is None:
                compare = check_numeric(
                    level, local_numbers(entry) if isinstance(entry, dict) else None
                )
            if compare.complete and compare.equal:
                note = "数值一致"
                tone = "success"
            elif compare.complete:
                note = "数值不同"
                tone = "warning"
            else:
                note = "数值不完整"
                tone = "warning"
            status.setText(f"已选择 · {note}")
            status.setForeground(QColor(self._palette[tone]))
            self._status_tone[key] = tone
            check.setCheckState(Qt.CheckState.Checked)
        self._update_bind_button()

    def _update_bind_button(self, *_args: Any) -> None:
        count = len(self.selected_bindings())
        self.bind_button.setText(f"绑定已勾选 ({count})")
        self.bind_button.setEnabled(count > 0 and not self._searching)

    def selected_bindings(self) -> list[tuple[str, LevelInfo]]:
        bindings: list[tuple[str, LevelInfo]] = []
        for row in range(self.table.rowCount()):
            check = self.table.item(row, 0)
            if check is None:
                continue
            if check.checkState() != Qt.CheckState.Checked:
                continue
            key = str(check.data(Qt.ItemDataRole.UserRole) or "")
            level = self._selected.get(key)
            if level is not None:
                bindings.append((key, level))
        return bindings

    def _emit_bind(self) -> None:
        bindings = self.selected_bindings()
        if not bindings:
            return
        answer = QMessageBox.question(
            self,
            "批量绑定",
            f"将绑定 {len(bindings)} 条谱面到对应的 TUF 编号。\n"
            "绑定只更新谱面库，不会移动或重命名本地文件。继续？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._update_bind_button()
            return
        self.bind_button.setEnabled(False)
        self.bindRequested.emit(bindings)

    def mark_results(self, bound_keys: set[str], skipped: int) -> None:
        self._entries = [
            entry for entry in self._entries if str(entry.get("key") or "") not in bound_keys
        ]
        rows = sorted(
            (
                self._rows[key]
                for key in bound_keys
                if key in self._rows
            ),
            reverse=True,
        )
        for row in rows:
            self.table.removeRow(row)
        for key in bound_keys:
            self._matches.pop(key, None)
            self._selected.pop(key, None)
        self._rows = {}
        for row in range(self.table.rowCount()):
            check = self.table.item(row, 0)
            if check is not None:
                self._rows[str(check.data(Qt.ItemDataRole.UserRole) or "")] = row
        note = f"已绑定 {len(bound_keys)} 条"
        if skipped:
            note += f"，未绑定 {skipped} 条"
        self.summary.setText(note)
        self._update_bind_button()
        if self.table.rowCount() == 0:
            self.accept()

    def refresh_bind_button(self) -> None:
        self._update_bind_button()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._searching:
            self.cancelSearchRequested.emit()
        super().closeEvent(event)
