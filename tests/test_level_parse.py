from adofai_publisher.models import LevelInfo


def test_parse_tuf_payload():
    payload = {
        "id": 17776,
        "song": "Beneath the Vortex",
        "artist": "Kuro Games",
        "bpm": 180,
        "levelLengthInMs": 102338.54,
        "tilecount": 1242,
        "autoTileCount": 2,
        "videoLink": "https://www.bilibili.com/video/BV1xx411c7mD",
        "baseScore": 0,
        "difficulty": {"name": "Unranked", "baseScore": 0},
        "artists": [{"name": "Kuro Games"}],
        "levelCredits": [
            {"role": "charter", "creator": {"name": "BeholdenEagle"}},
            {"role": "vfxer", "creator": {"name": "Someone"}},
        ],
    }
    level = LevelInfo.from_tuf(payload)
    assert level.tuf_id == 17776
    assert level.song == "Beneath the Vortex"
    assert level.artist == "Kuro Games"
    assert level.charter == "BeholdenEagle"
    assert level.difficulty == "Unranked"
    assert level.expected_hits == 1240
    assert level.duration_text == "1:42"
    assert level.url == "https://tuforums.com/levels/17776"


def test_parse_wrapped_payload():
    level = LevelInfo.from_tuf({"level": {"id": 5, "song": "Wrapped", "tilecount": 10}})
    assert level.tuf_id == 5
    assert level.song == "Wrapped"


def test_duration_handles_missing():
    assert LevelInfo().duration_text == "-"
    assert LevelInfo().expected_hits is None


def test_difficulty_can_be_plain_string():
    level = LevelInfo.from_tuf({"id": 1, "song": "X", "difficulty": "Unranked"})
    assert level.difficulty == "Unranked"


def test_download_fields_are_parsed():
    level = LevelInfo.from_tuf(
        {
            "id": 2,
            "dlLink": "https://api.tuforums.com/cdn/abc",
            "fileId": "abc",
        }
    )
    assert level.dl_link == "https://api.tuforums.com/cdn/abc"
    assert level.file_id == "abc"
