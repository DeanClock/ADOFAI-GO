import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from adofai_publisher import workers
from adofai_publisher.level_library import LevelLibraryDB
from adofai_publisher.models import LevelInfo
from adofai_publisher.ui.batch_bind import BatchBindDialog
from adofai_publisher.ui.theme import palette


def _app():
    return QApplication.instance() or QApplication([])


def _entry(key="folder:a", song="Time Leaper", **overrides):
    entry = {
        "key": key,
        "song": song,
        "path": f"C:/levels/{key.rsplit(':', 1)[-1]}.adofai",
        "bpm": 350.0,
        "tilecount": 2629,
        "midspin_count": 25,
        "auto_tile_count": 0,
        "playable_hits": 2604,
        "parse_error": "",
    }
    entry.update(overrides)
    return entry


def _payload(
    level_id=10808,
    song="Time Leaper",
    bpm=350,
    tilecount=2604,
    midspin=25,
    auto=0,
    drop=(),
):
    raw = {
        "id": level_id,
        "song": song,
        "difficulty": "P10",
        "bpm": bpm,
        "tilecount": tilecount,
        "midspinCount": midspin,
        "autoTileCount": auto,
    }
    for key in drop:
        raw.pop(key, None)
    payload = LevelInfo.from_tuf(raw).to_dict()
    payload["difficulty"] = "P10"
    payload["charter"] = "Charter"
    return payload


def _status(dialog, row):
    return dialog.table.item(row, 4).text()


def _checked(dialog, row):
    return dialog.table.item(row, 0).checkState() == Qt.CheckState.Checked


def test_dialog_autochecks_unique_numeric_match_with_quadruple_status():
    app = _app()
    dialog = BatchBindDialog([_entry()], "light")
    try:
        dialog.set_row_results("folder:a", [_payload()])

        assert "数值唯一 · 350 / 2629 / 25 / 0" in _status(dialog, 0)
        assert _checked(dialog, 0)
        assert dialog.table.cellWidget(0, 3) is None
        assert "#10808" in dialog.table.item(0, 3).text()
        tooltip = dialog.table.item(0, 3).toolTip()
        assert "本地数值：350 / 2629 / 25 / 0" in tooltip
        assert "匹配口径：物量按 TUF 口径 2604" in tooltip
        assert "✓ 数值全等" in tooltip
        assert "数值 350 / 2604 / 25 / 0" in tooltip
        bindings = dialog.selected_bindings()
        assert len(bindings) == 1
        assert bindings[0][1].tuf_id == 10808
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_dialog_marks_multiple_numeric_matches_yellow_and_unchecked():
    app = _app()
    dialog = BatchBindDialog([_entry()], "light")
    try:
        dialog.set_row_results(
            "folder:a",
            [
                _payload(level_id=1, song="Time Leaper"),
                _payload(level_id=2, song="Another Song"),
            ],
        )

        assert _status(dialog, 0) == "数值相同 · 2 张"
        assert not _checked(dialog, 0)
        assert dialog.table.item(0, 4).foreground().color().name() == QColor(
            palette("light")["warning"]
        ).name()
        row_match = dialog._matches["folder:a"]
        assert row_match.selected is not None
        assert row_match.selected.tuf_id == 1
        tooltip = dialog.table.item(0, 3).toolTip()
        assert tooltip.count("✓ 数值全等") == 2
        assert not dialog.selected_bindings()
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_dialog_name_only_match_is_never_autochecked():
    app = _app()
    dialog = BatchBindDialog([_entry()], "dark")
    try:
        dialog.set_row_results(
            "folder:a",
            [_payload(song="Time Leaper", bpm=300, tilecount=2000, midspin=0, auto=0)],
        )

        assert _status(dialog, 0) == "仅名称匹配 · 100% · 需确认"
        assert not _checked(dialog, 0)
        assert "#10808" in dialog.table.item(0, 3).text()
        assert "数值不同（BPM 300≠350；物量 2000≠2604；中旋 0≠25）" in (
            dialog.table.item(0, 3).toolTip()
        )
        assert not dialog.selected_bindings()
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_dialog_marks_incomplete_candidate_numbers():
    app = _app()
    dialog = BatchBindDialog([_entry()], "light")
    try:
        dialog.set_row_results(
            "folder:a",
            [_payload(drop=("midspinCount", "autoTileCount"))],
        )

        assert _status(dialog, 0) == "数值不完整 · 需确认"
        assert not _checked(dialog, 0)
        tooltip = dialog.table.item(0, 3).toolTip()
        assert "数值不完整（缺 中旋、自动砖）" in tooltip
        assert "数值 350 / 2604 / ? / ?" in tooltip
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_manual_selection_checks_row_and_reports_consistency():
    app = _app()
    dialog = BatchBindDialog([_entry()], "light")
    try:
        dialog.set_row_results(
            "folder:a",
            [
                _payload(level_id=1, song="Time Leaper"),
                _payload(
                    level_id=2,
                    song="Another Song",
                    bpm=300,
                    tilecount=2000,
                    midspin=0,
                    auto=0,
                ),
            ],
        )
        match = dialog._matches["folder:a"]
        equal = match.candidates[0].level
        different = match.candidates[1].level

        dialog._select_candidate(0, equal)

        assert _status(dialog, 0) == "已选择 · 数值一致"
        assert _checked(dialog, 0)
        assert dialog.selected_bindings()[0][1].tuf_id == 1

        dialog._select_candidate(0, different)

        assert _status(dialog, 0) == "已选择 · 数值不同"
        assert _checked(dialog, 0)

        dialog._select_candidate(0, None)

        assert _status(dialog, 0) == "不绑定"
        assert not _checked(dialog, 0)
        assert not dialog.selected_bindings()
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_summary_counts_all_four_states_after_search():
    app = _app()
    entries = [
        _entry("folder:unique", "Unique Song"),
        _entry("folder:multi", "Multi Song"),
        _entry("folder:name", "Name Song"),
        _entry("folder:none", "No Song"),
    ]
    dialog = BatchBindDialog(entries, "dark")
    try:
        dialog._start_search()
        dialog.set_row_results(
            "folder:unique",
            [_payload(song="Unique Song")],
        )
        dialog.set_row_results(
            "folder:multi",
            [
                _payload(level_id=1, song="Multi Song"),
                _payload(level_id=2, song="Other Multi"),
            ],
        )
        dialog.set_row_results(
            "folder:name",
            [
                _payload(
                    song="Name Song",
                    bpm=300,
                    tilecount=2000,
                    midspin=0,
                    auto=0,
                )
            ],
        )
        dialog.set_row_results("folder:none", [])
        dialog.set_search_finished(4)

        summary = dialog.summary.text()
        assert "数值唯一 1" in summary
        assert "数值相同 1" in summary
        assert "仅名称 1" in summary
        assert "无候选 1" in summary
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_targets_carry_numeric_state_for_worker():
    app = _app()
    dialog = BatchBindDialog(
        [_entry(), _entry("folder:b", parse_error="谱面文件为空")], "light"
    )
    try:
        targets = dialog.targets()

        assert targets[0]["numeric_ready"] is True
        assert targets[0]["tilecount"] == 2604
        assert targets[0]["numeric_missing"] == []
        assert targets[1]["numeric_ready"] is False
        assert targets[1]["numeric_missing"] == ["解析失败"]
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_dialog_reports_parse_error_in_tooltip():
    app = _app()
    dialog = BatchBindDialog([_entry(parse_error="谱面文件为空")], "light")
    try:
        dialog.set_row_results("folder:a", [_payload()])

        tooltip = dialog.table.item(0, 3).toolTip()
        assert "本地解析失败，只按名称匹配" in tooltip
        assert not _checked(dialog, 0)
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_end_to_end_targets_worker_dialog_bind(tmp_path, monkeypatch):
    app = _app()
    app.processEvents()
    chart = tmp_path / "chart.adofai"
    chart.write_text("{}", encoding="utf-8")
    db = LevelLibraryDB(tmp_path / "library.json")
    db.upsert(
        LevelInfo(
            song="Time Leaper",
            artist="Artist",
            charter="Charter",
            difficulty="P10",
            bpm=350.0,
            tilecount=2629,
            midspin_count=25,
            auto_tile_count=0,
            playable_hits=2604,
        ),
        chart,
        {"source": "scan", "parse_error": "", "folder": str(tmp_path)},
    )
    db.save()
    entries = db.all()
    dialog = BatchBindDialog(entries, "light")
    try:
        targets = dialog.targets()
        assert targets[0]["numeric_ready"] is True
        assert targets[0]["tilecount"] == 2604

        class FakeLookup:
            def __init__(self):
                self.calls = []

            def search(self, query, limit=100):
                self.calls.append((query, limit))
                if query == "bpm=350, tilecount=2604":
                    return [LevelInfo.from_tuf(_payload_raw())]
                return []

        lookup = FakeLookup()
        monkeypatch.setattr(workers, "LevelLookup", lambda: lookup)
        payloads = []
        worker = workers.BatchLevelLookupWorker(targets)
        worker.progress.connect(lambda key, payload: payloads.append((key, payload)))
        worker.run()

        assert lookup.calls == [("bpm=350, tilecount=2604", 100)]
        assert len(payloads) == 1
        key, payload = payloads[0]
        dialog.set_row_results(key, payload)

        assert "数值唯一" in _status(dialog, 0)
        bindings = dialog.selected_bindings()
        assert len(bindings) == 1
        bound = db.bind_tuf(bindings[0][0], bindings[0][1])

        assert bound["tuf_id"] == 10808
        assert bound["path"] == str(chart)
        assert chart.is_file()
    finally:
        dialog.deleteLater()
        app.processEvents()


def _payload_raw():
    return {
        "id": 10808,
        "song": "Time Leaper",
        "difficulty": "P10",
        "bpm": 350,
        "tilecount": 2604,
        "midspinCount": 25,
        "autoTileCount": 0,
    }
