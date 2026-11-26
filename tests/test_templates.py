from adofai_publisher.config import DEFAULT_DESC_TEMPLATE, DEFAULT_TITLE_TEMPLATE, Settings
from adofai_publisher.models import Draft, LevelInfo, empty_judgements
from adofai_publisher.wizard_state import (
    draft_to_record,
    generate_copy,
    render_text,
    template_values,
)


def test_template_render_with_missing_keys():
    assert render_text("{song} / {unknown}", {"song": "A"}) == "A / "


def test_generate_copy_uses_level_and_accuracy(tmp_path):
    settings = Settings(tmp_path / "settings.json")
    draft = Draft(
        level=LevelInfo(tuf_id=12, song="Song", artist="Artist", charter="Charter", difficulty="G12"),
        judgements=empty_judgements(),
        tags=[],
    )
    draft.judgements["perfect"] = 10
    title, description, tags = generate_copy(draft, settings, "100.00%")
    assert "Song" in title
    assert "100.00%" in title
    assert "Song" in description
    assert "Artist" in description
    assert "冰与火之舞" in tags
    assert "Song" in tags


def test_template_values_include_judgements():
    draft = Draft(level=LevelInfo(tuf_id=1, song="S"), judgements=empty_judgements())
    draft.judgements["perfect"] = 42
    values = template_values(draft, "99.00%")
    assert values["perfect"] == "42"
    assert values["tuf_url"] == "https://tuforums.com/levels/1"


def test_record_keeps_level_metadata_for_reload():
    draft = Draft(
        level=LevelInfo(
            tuf_id=17776,
            song="Beneath the Vortex",
            artist="Kuro Games",
            charter="BeholdenEagle",
            bpm=180,
            duration_ms=102000,
            difficulty="Unranked",
            tilecount=1242,
            auto_tile_count=2,
        )
    )
    record = draft_to_record(draft, "100.00%")
    assert record["bpm"] == 180
    assert record["duration_ms"] == 102000
    assert record["tilecount"] == 1242
    assert record["auto_tile_count"] == 2


def test_draft_x_perfect_roundtrip_and_legacy_default():
    draft = Draft(judgements=empty_judgements(), x_perfect=True)
    data = draft.to_dict()
    assert data["x_perfect"] is True
    assert Draft.from_dict(data).x_perfect is True

    legacy = data.copy()
    legacy.pop("x_perfect")
    assert Draft.from_dict(legacy).x_perfect is False


def test_record_keeps_both_cover_ratios():
    draft = Draft(cover_path="C:/covers/wide.png", cover_path_43="C:/covers/square.png")

    record = draft_to_record(draft, "100.00%")

    assert record["cover_path"] == "C:/covers/wide.png"
    assert record["cover_path_43"] == "C:/covers/square.png"


def test_history_db_keeps_cover_path_43_and_accepts_legacy_records(tmp_path):
    from adofai_publisher.histdb import HistoryDB

    draft = Draft(cover_path="C:/covers/wide.png", cover_path_43="C:/covers/square.png")
    database = HistoryDB(tmp_path / "records.json")
    stored = database.add(draft_to_record(draft, "100.00%"))

    reloaded = HistoryDB(tmp_path / "records.json").get(stored["id"])
    assert reloaded is not None
    assert reloaded["cover_path_43"] == "C:/covers/square.png"

    # 旧记录没有 cover_path_43：原样保留、不补键也不报错，回填时按空串处理。
    legacy = database.add({"title": "旧投稿", "cover_path": "C:/covers/old.png"})
    assert "cover_path_43" not in legacy
    assert database.get(legacy["id"])["cover_path"] == "C:/covers/old.png"
