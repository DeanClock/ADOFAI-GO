import zipfile

from adofai_publisher.levellookup import (
    _render_filename,
    _safe_filename,
    _unique_path,
    save_level_file,
)
from adofai_publisher.models import LevelInfo


def _level():
    return LevelInfo(
        tuf_id=17776,
        song="Beneath the Vortex",
        charter="BeholdenEagle",
        artist="Kuro Games",
        difficulty="Unranked",
    )


def test_render_template():
    name = _render_filename("{song} - {charter} ({tuf_id}).ard", _level())
    assert name == "Beneath the Vortex - BeholdenEagle (17776).ard"


def test_render_missing_values_keeps_placeholder_empty():
    level = LevelInfo(tuf_id=5)
    assert _render_filename("{song}-{tuf_id}.ard", level) == "-5.ard"


def test_safe_filename_replaces_illegal_chars():
    level = _level()
    name = _safe_filename('a:b*c?"d<>|.ard', level)
    assert ":" not in name and "*" not in name and "?" not in name
    assert name.endswith(".ard")


def test_safe_filename_appends_extension():
    level = _level()
    assert _safe_filename("plain", level).endswith(".ard")


def test_unique_path_adds_suffix(tmp_path):
    target = tmp_path / "chart.ard"
    target.write_bytes(b"x")
    assert _unique_path(target).name == "chart-1.ard"
    (tmp_path / "chart-1.ard").write_bytes(b"x")
    assert _unique_path(target).name == "chart-2.ard"


def test_save_zip_level_keeps_archive_suffix(tmp_path):
    source = tmp_path / "download.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("level.adofai", "{}")

    target = save_level_file(source, tmp_path / "levels", _level(), "{song} ({tuf_id}).ard")

    assert target.name == "Beneath the Vortex (17776).zip"
    assert target.is_file()
