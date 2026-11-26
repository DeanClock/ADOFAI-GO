import json
import os
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from adofai_publisher.chart_analyzer import (
    analyze_chart,
    analyze_chart_data,
    analyze_tuf_level,
)
from adofai_publisher.models import LevelInfo
from adofai_publisher.ui.chart_analyzer import ChartAnalyzerDialog


def _chart_payload() -> dict:
    return {
        "settings": {
            "bpm": 120,
            "song": "Analyzer Song",
            "artist": "Artist",
            "author": "Charter",
            "difficulty": "Hard",
        },
        "angleData": [0, 90, 180, 270],
        "actions": [
            {"floor": 0, "eventType": "SetSpeed", "speed": 1.0},
            {"floor": 1, "eventType": "Twirl"},
            {"floor": 2, "eventType": "SetSpeed", "speed": 2.0},
        ],
    }


def test_analyze_adofai_estimates_duration_and_speed(tmp_path):
    chart = tmp_path / "Analyzer Song (12345).adofai"
    chart.write_text(json.dumps(_chart_payload()), encoding="utf-8")

    with patch.object(Path, "read_bytes", autospec=True, side_effect=Path.read_bytes) as read:
        analysis = analyze_chart(chart)

    read.assert_called_once_with(chart)

    assert analysis.ok
    assert analysis.path == str(chart)
    assert analysis.source == "file"
    assert analysis.song == "Analyzer Song"
    assert analysis.author == "Charter"
    assert analysis.game_difficulty == "Hard"
    assert analysis.tuf_id == 12345
    assert analysis.bpm == 120
    assert analysis.tile_count == 4
    # angleData 里没有 999，普通角度不算中旋。
    assert analysis.midspin_count == 0
    assert analysis.speed_min == 1
    assert analysis.speed_max == 2
    assert abs(analysis.chart_duration_ms - 1500) < 5
    assert abs(analysis.hits_per_second - 4 / 1.5) < 0.01


def test_analyze_counts_midspins_from_999(tmp_path):
    payload = _chart_payload()
    payload["angleData"] = [0, 999, 90, "999", 999.0, 180]
    chart = tmp_path / "Midspin Song (54321).adofai"
    chart.write_text(json.dumps(payload), encoding="utf-8")

    analysis = analyze_chart(chart)

    assert analysis.ok
    assert analysis.tile_count == 6
    assert analysis.midspin_count == 3


@pytest.mark.parametrize(
    ("actions", "duration", "maximum"),
    [
        ([], 2000, 1),
        ([{"floor": 0, "eventType": "SetSpeed", "speed": 4},
          {"floor": 0, "eventType": "SetSpeed", "speed": 2}], 1000, 4),
        ([{"floor": 2, "eventType": "SetSpeed", "speed": 4},
          {"floor": 0, "eventType": "SetSpeed", "speed": 2}], 750, 4),
        ([{"floor": 2, "eventType": "", "type": "SetSpeed", "speed": 2}], 1500, 2),
        ([{"floor": 3, "eventType": "SetSpeed", "speed": 2}], 1750, 2),
        ([{"eventType": "SetSpeed", "speed": 2}], 1000, 2),
        ([{"floor": None, "eventType": "SetSpeed", "speed": 2}], 1000, 2),
        ([{"floor": True, "eventType": "SetSpeed", "speed": 2}], 1250, 2),
        ([{"floor": 1.9, "eventType": "SetSpeed", "speed": 2}], 1250, 2),
        ([{"floor": "1", "eventType": "SetSpeed", "speed": "2"}], 1250, 2),
        ([{"floor": "1.0", "eventType": "SetSpeed", "speed": 2}], 2000, 1),
        ([{"floor": -1, "eventType": "SetSpeed", "speed": 8},
          {"floor": 4, "eventType": "SetSpeed", "speed": 16}], 2000, 1),
        ([{"floor": 0, "eventType": "SetSpeed", "speed": value}
          for value in [None, "bad", 0, -1]], 2000, 1),
        # 时长估算目前不按 active 过滤，优化不得改变此口径。
        ([{"floor": 0, "eventType": "SetSpeed", "speed": 2, "active": False}], 1000, 2),
        ([{"floor": 0, "eventType": "Twirl", "type": "SetSpeed", "speed": 8}], 2000, 1),
        ([None, "SetSpeed", [], {"floor": "bad", "eventType": "SetSpeed", "speed": 8}], 2000, 1),
    ],
)
def test_speed_events_preserve_floor_order_aliases_and_existing_boundaries(actions, duration, maximum):
    payload = _chart_payload()
    payload["actions"] = actions
    analysis = analyze_chart_data(payload)
    assert analysis.ok
    assert analysis.chart_duration_ms == duration
    assert analysis.speed_min == 1
    assert analysis.speed_max == maximum
    assert analysis.hits_per_second == 4 / (duration / 1000)


def test_non_speed_events_stay_in_summary_without_changing_timing():
    payload = _chart_payload()
    payload["actions"].extend(
        {"floor": index % 4, "eventType": "MoveCamera"} for index in range(2000)
    )
    analysis = analyze_chart_data(payload)
    assert analysis.chart_duration_ms == 1500
    assert analysis.speed_min == 1
    assert analysis.speed_max == 2
    assert analysis.event_summary == {"SetSpeed": 2, "Twirl": 1, "MoveCamera": 2000}


@pytest.mark.parametrize("actions", [None, {}, (), "SetSpeed"])
def test_non_list_actions_keep_empty_event_analysis(actions):
    payload = _chart_payload()
    payload["actions"] = []
    expected = analyze_chart_data(payload).to_dict()
    payload["actions"] = actions
    assert analyze_chart_data(payload).to_dict() == expected


@pytest.mark.parametrize("bpm", [0, None, "bad"])
def test_speed_events_without_valid_bpm_do_not_estimate_duration(bpm):
    payload = _chart_payload()
    payload["settings"]["bpm"] = bpm
    analysis = analyze_chart_data(payload)
    assert analysis.chart_duration_ms is None
    assert analysis.hits_per_second is None
    assert analysis.speed_min == analysis.speed_max == 1
    assert analysis.event_summary == {"SetSpeed": 2, "Twirl": 1}


def test_analyze_legacy_path_data_chart(tmp_path):
    chart = tmp_path / "Legacy Song.adofai"
    chart.write_text(
        json.dumps(
            {
                "pathData": "RULJ" * 10,
                "settings": {"bpm": 160, "song": "Legacy Song", "author": "Charter"},
            }
        ),
        encoding="utf-8",
    )

    analysis = analyze_chart(chart)

    assert analysis.ok
    assert analysis.song == "Legacy Song"
    assert analysis.tile_count == 40
    assert analysis.midspin_count == 0


@pytest.mark.parametrize("suffix", [".ard", ".zip"])
def test_analyze_ard_zip_package(tmp_path, suffix):
    archive_path = tmp_path / f"pack{suffix}"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("level.adofai", json.dumps(_chart_payload()))

    with patch.object(
        zipfile.ZipFile, "read", autospec=True, side_effect=zipfile.ZipFile.read
    ) as read:
        analysis = analyze_chart(archive_path)

    assert read.call_count == 1
    assert read.call_args.args[1] == "level.adofai"
    assert analysis.ok
    assert analysis.path == str(archive_path)
    assert analysis.source == "file"
    assert analysis.tile_count == 4
    assert analysis.song == "Analyzer Song"
    assert analysis.bpm == 120
    assert analysis.event_summary == {"SetSpeed": 2, "Twirl": 1}
    assert analysis.chart_duration_ms == 1500


def test_analyze_adozip_uses_nested_main_chart(tmp_path):
    archive_path = tmp_path / "pack.adozip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("backup.adofai", json.dumps({"angleData": [0]}))
        archive.writestr("Pack/main.adofai", json.dumps(_chart_payload()))
        archive.writestr("Pack/song.ogg", b"audio")

    analysis = analyze_chart(archive_path)

    assert analysis.ok
    assert analysis.song == "Analyzer Song"
    assert analysis.tile_count == 4


def test_analyze_invalid_chart_reports_error(tmp_path):
    chart = tmp_path / "broken.adofai"
    chart.write_text("{not json", encoding="utf-8")

    analysis = analyze_chart(chart)

    assert not analysis.ok
    assert "JSON" in analysis.error


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (b"", "谱面文件为空"),
        (b"[]", "谱面 JSON 顶层不是对象"),
        (b"\xff", "谱面文件编码无法识别"),
    ],
)
def test_analyze_chart_content_errors_keep_source_path(tmp_path, payload, error):
    chart = tmp_path / "broken (12345).adofai"
    chart.write_bytes(payload)

    analysis = analyze_chart(chart)

    assert not analysis.ok
    assert analysis.error == error
    assert analysis.path == str(chart)
    assert analysis.source == "file"
    assert analysis.tuf_id is None
    assert analysis.tile_count == 0


def test_analyze_archive_without_chart_reports_error(tmp_path):
    archive_path = tmp_path / "empty.adozip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("Pack/song.ogg", b"audio")

    analysis = analyze_chart(archive_path)

    assert not analysis.ok
    assert analysis.error == "谱面包里没有找到 .adofai 内容"
    assert analysis.path == str(archive_path)
    assert analysis.source == "file"


def test_chart_analyzer_dialog_loads_file_and_emits_level(tmp_path):
    app = QApplication.instance() or QApplication([])
    chart = tmp_path / "level.adofai"
    chart.write_text(json.dumps(_chart_payload()), encoding="utf-8")
    dialog = ChartAnalyzerDialog()

    analysis = dialog.load_path(tmp_path)
    assert analysis.ok
    assert dialog.values["song"].text() == "Analyzer Song"
    assert dialog.values["tiles"].text() == "4"
    assert dialog.values["bpm"].text() == "120"
    assert dialog.add_button.isEnabled()

    captured = []
    dialog.addRequested.connect(captured.append)
    dialog.add_button.click()
    assert captured[0].song == "Analyzer Song"
    assert captured[0].tilecount == 4

    dialog.load_path(tmp_path / "missing.adofai")
    assert not dialog.add_button.isEnabled()
    assert "没有找到" in dialog.status.text()
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def _without_path(analysis_dict):
    data = dict(analysis_dict)
    for key in ("path", "source"):
        data.pop(key, None)
    return data


def test_analyze_chart_data_matches_file_analysis(tmp_path):
    payload = _chart_payload()
    chart = tmp_path / "Analyzer Song (12345).adofai"
    chart.write_text(json.dumps(payload), encoding="utf-8")

    from_file = analyze_chart(chart)
    from_data = analyze_chart_data(payload, tuf_id=12345)

    assert from_data.ok
    assert from_data.source == "tuf"
    assert from_data.tuf_id == 12345
    assert _without_path(from_data.to_dict()) == _without_path(from_file.to_dict())


class _FakeLookup:
    def __init__(self, data, error="", level=None):
        self.data = data
        self.error = error
        self.level = level
        self.level_error = None

    def fetch_chart_json(self, level_id):
        return self.data, self.error

    def get(self, level_id):
        if self.level_error:
            raise self.level_error
        return self.level


def test_analyze_tuf_level_returns_analysis_and_level():
    lookup = _FakeLookup(_chart_payload(), level=LevelInfo(tuf_id=17776, song="TUF Song", difficulty="P10"))

    analysis, level = analyze_tuf_level(17776, lookup=lookup)

    assert analysis.ok
    assert analysis.source == "tuf"
    assert analysis.tuf_id == 17776
    assert analysis.song == "Analyzer Song"
    assert analysis.tile_count == 4
    assert analysis.speed_min == 1
    assert analysis.speed_max == 2
    assert level is not None
    assert level.tuf_id == 17776
    assert level.difficulty == "P10"


def test_analyze_tuf_level_builds_level_when_metadata_missing():
    lookup = _FakeLookup(_chart_payload())
    lookup.level_error = RuntimeError("offline")

    analysis, level = analyze_tuf_level(17776, lookup=lookup)

    assert analysis.ok
    assert level is not None
    assert level.tuf_id == 17776
    assert level.song == "Analyzer Song"
    assert level.tilecount == 4


def test_analyze_tuf_level_reports_fetch_errors():
    for message in (
        "没有找到 TUF 关卡 404 的在线谱面",
        "TUF 返回的不是谱面 JSON（可能是 HTML 页面）",
        "在线谱面文件过大（超过 20 MB），已取消分析",
    ):
        lookup = _FakeLookup(None, error=message)
        lookup.level_error = RuntimeError("offline")

        analysis, level = analyze_tuf_level(404, lookup=lookup)

        assert not analysis.ok
        assert analysis.error == message
        assert analysis.source == "tuf"
        assert level is None
