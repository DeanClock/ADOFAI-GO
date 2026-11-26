from adofai_publisher.config import DEFAULT_SETTINGS, Settings
from adofai_publisher.copy_snippets import (
    DEFAULT_COPY_SNIPPETS,
    SNIPPET_GROUPS,
    insert_snippet,
    normalize_copy_snippets,
)


def test_builtin_copy_snippets_exist_and_are_grouped():
    snippets = DEFAULT_SETTINGS["copy_snippets"]

    assert snippets
    assert snippets == DEFAULT_COPY_SNIPPETS
    assert {item["text"] for item in snippets} >= {
        "严判击破",
        "完美无瑕",
        "全连",
        "AP",
        "FC",
        "零失误",
        "手感极佳",
    }
    assert {item["group"] for item in snippets} <= set(SNIPPET_GROUPS)
    groups = {item["text"]: item["group"] for item in snippets}
    assert groups["严判"] == "判定"
    assert groups["严判击破"] == "成绩"


def test_normalize_copy_snippets_accepts_legacy_strings_and_bad_groups():
    values = normalize_copy_snippets(["初见", {"text": "一遍过", "group": "成绩"}])

    assert values == [
        {"text": "初见", "group": "自定义"},
        {"text": "一遍过", "group": "成绩"},
    ]


def test_insert_snippet_at_end_adds_separator():
    text = "【冰与火之舞】Song G12 通关 99.87%"
    updated, cursor = insert_snippet(text, len(text), "严判击破")

    assert updated == text + " 严判击破"
    assert cursor == len(updated)


def test_insert_snippet_in_empty_text_and_middle():
    assert insert_snippet("", 0, "完美无瑕") == ("完美无瑕", 4)

    updated, cursor = insert_snippet("完美测试", 2, "无瑕")

    assert updated == "完美 无瑕 测试"
    assert cursor == 6


def test_insert_snippet_handles_newline_and_selection():
    assert insert_snippet("第一行\n第二行", 4, "手感极佳") == (
        "第一行\n手感极佳 第二行",
        9,
    )
    assert insert_snippet("hello world", 0, "严判击破", 5) == (
        "严判击破 world",
        4,
    )


def test_copy_snippets_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    custom = [
        {"text": "初见", "group": "手感"},
        {"text": "一遍过", "group": "成绩"},
    ]
    settings = Settings(path)
    settings.set("copy_snippets", custom)
    settings.save()

    assert Settings(path).get("copy_snippets") == custom


def test_legacy_settings_without_copy_snippets_use_builtin(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"theme": "dark"}', encoding="utf-8")

    assert Settings(path).get("copy_snippets") == DEFAULT_COPY_SNIPPETS


def test_invalid_copy_snippets_fall_back_to_builtin(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"copy_snippets": "not-a-list"}', encoding="utf-8")

    assert Settings(path).get("copy_snippets") == DEFAULT_COPY_SNIPPETS
