import io
import json
import threading
import zipfile
from pathlib import Path

import pytest

from adofai_publisher.levellookup import (
    DownloadCancelled,
    LevelLookup,
    LookupError,
    extract_level_to_library,
    install_level_file,
)
from adofai_publisher.models import LevelInfo


def _payload(song="Icedust"):
    return json.dumps(
        {
            "angleData": [0, 90, 180, 270],
            "settings": {"song": song, "author": "Charter", "bpm": 184},
        }
    )


def _write_zip(path, entries):
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return path


class _ZipResponse:
    status_code = 200
    url = "https://cdn.tuforums.com/zips/demo/Song.zip"

    def __init__(self, payload):
        self.payload = payload

    def iter_content(self, chunk_size=65536):
        yield self.payload


class _ZipSession:
    def __init__(self, payload):
        self.payload = payload

    def get(self, url, **kwargs):
        return _ZipResponse(self.payload)


def _zip_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def test_extract_flat_package_into_difficulty_folder(tmp_path):
    archive = _write_zip(
        tmp_path / "icedust.zip",
        {
            "level.adofai": _payload(),
            "backup.adofai": _payload("Backup"),
            "Icedust.ogg": b"audio",
            "bg.png": b"image",
        },
    )
    level = LevelInfo(tuf_id=17198, song="Icedust", charter="mik", difficulty="P15")

    main = extract_level_to_library(archive, tmp_path / "levels", level)

    assert main == tmp_path / "levels" / "P" / "P15" / "Icedust" / "level.adofai"
    assert main.is_file()
    assert (main.parent / "Icedust.ogg").read_bytes() == b"audio"
    assert (main.parent / "backup.adofai").is_file()


def test_extract_flattens_single_wrapper_folder(tmp_path):
    archive = _write_zip(
        tmp_path / "wrapped.zip",
        {
            ".DS_Store": b"junk",
            "__MACOSX/._main.adofai": b"junk",
            "Pack/main.adofai": _payload("Wrapped"),
            "Pack/song.ogg": b"audio",
        },
    )
    level = LevelInfo(tuf_id=10001, song="Wrapped", charter="Charter", difficulty="G1")

    main = extract_level_to_library(archive, tmp_path / "levels", level)

    assert main == tmp_path / "levels" / "G" / "G1" / "Wrapped" / "main.adofai"
    assert main.is_file()
    assert not (main.parent / "Pack").exists()


def test_extract_rejects_path_traversal(tmp_path):
    archive = _write_zip(
        tmp_path / "evil.zip",
        {
            "../evil.txt": b"bad",
            "level.adofai": _payload(),
        },
    )
    level = LevelInfo(tuf_id=1, song="Evil", difficulty="U1")

    with pytest.raises(LookupError):
        extract_level_to_library(archive, tmp_path / "levels", level)

    assert not (tmp_path / "evil.txt").exists()


def test_download_level_extracts_and_removes_archive(tmp_path):
    archive_bytes = _zip_bytes(
        {
            "main.adofai": _payload("SUGARPOT"),
            "song.ogg": b"audio",
        }
    )
    lookup = LevelLookup()
    lookup.session = _ZipSession(archive_bytes)
    level = LevelInfo(
        tuf_id=17643,
        song="SUGARPOT",
        charter="3.14",
        difficulty="P10",
        dl_link="https://example.test/chart",
    )
    progress = []
    stages = []

    target = lookup.download_level(
        17643,
        tmp_path,
        level=level,
        extract=True,
        progress=lambda received, total: progress.append((received, total)),
        stage=stages.append,
    )

    assert target == tmp_path / "P" / "P10" / "SUGARPOT" / "main.adofai"
    assert target.is_file()
    assert (target.parent / "song.ogg").is_file()
    assert not list((tmp_path / "P" / "P10" / "SUGARPOT").glob("*.zip"))
    assert progress[-1] == (len(archive_bytes), len(archive_bytes))
    assert stages == ["download", "extract", "done"]


def test_download_level_cancel_removes_partial_file(tmp_path):
    class _CancelResponse:
        status_code = 200
        url = "https://cdn.tuforums.com/files/demo.ard"
        headers: dict[str, str] = {}

        def iter_content(self, chunk_size=65536):
            yield b'{"angleData":'
            yield b" [0, 90, 180]}"

    class _CancelSession:
        def get(self, url, **kwargs):
            return _CancelResponse()

    lookup = LevelLookup()
    lookup.session = _CancelSession()
    level = LevelInfo(
        tuf_id=88,
        song="Cancel Me",
        charter="Charter",
        difficulty="P10",
        dl_link="https://example.test/chart",
    )
    cancelled = threading.Event()

    with pytest.raises(DownloadCancelled):
        lookup.download_level(
            88,
            tmp_path,
            level=level,
            progress=lambda _received, _total: cancelled.set(),
            cancel_event=cancelled,
        )

    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


def test_extract_cancel_cleans_preexisting_empty_folder(tmp_path, monkeypatch):
    archive = _write_zip(
        tmp_path / "cancel.zip",
        {"main.adofai": _payload("Cancel Extract")},
    )
    level = LevelInfo(tuf_id=89, song="Cancel Extract", difficulty="P10")
    folder = tmp_path / "levels" / "Cancel Extract"
    folder.mkdir(parents=True)

    def fail_after_writing(source, destination, cancel_event=None):
        (Path(destination) / "partial.bin").write_bytes(b"partial")
        raise DownloadCancelled("下载已取消")

    monkeypatch.setattr(
        "adofai_publisher.levellookup.extract_level_archive",
        fail_after_writing,
    )

    with pytest.raises(DownloadCancelled):
        extract_level_to_library(
            archive,
            tmp_path / "levels",
            level,
            organize=False,
        )

    assert not folder.exists()


def test_install_preview_package_extracts_when_requested(tmp_path):
    archive = _write_zip(
        tmp_path / "preview.zip",
        {
            "level.adofai": _payload("Preview"),
            "song.ogg": b"audio",
        },
    )
    level = LevelInfo(tuf_id=9, song="Preview", charter="C")

    target = install_level_file(
        archive,
        tmp_path / "levels",
        level,
        "{song} ({tuf_id}).ard",
        organize=False,
        extract=True,
    )

    assert target == tmp_path / "levels" / "Preview" / "level.adofai"
    assert target.is_file()
    assert archive.is_file()
