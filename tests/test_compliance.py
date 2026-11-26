from adofai_publisher.compliance import check_draft
from adofai_publisher.models import Draft, LevelInfo, empty_judgements


def _draft(**kwargs) -> Draft:
    judgements = empty_judgements()
    judgements["perfect"] = 100
    draft = Draft(
        video_path="C:/videos/clear.mp4",
        level=LevelInfo(tuf_id=17776, song="Beneath the Vortex", tilecount=100),
        judgements=judgements,
        title="【冰与火之舞】Beneath the Vortex 通关 100%",
        description="谱面信息与成绩",
        tags=["冰与火之舞", "ADOFAI"],
        partition="单机游戏",
        feeling_rating="G12",
    )
    for key, value in kwargs.items():
        setattr(draft, key, value)
    return draft


def _messages(issues, level=None):
    return [issue.message for issue in issues if level is None or issue.level == level]


def test_valid_draft_has_no_errors():
    issues = check_draft(_draft(), check_video_file=False)
    assert not [issue for issue in issues if issue.is_error]


def test_missing_video_is_error_when_checking_file():
    issues = check_draft(_draft(video_path=""), check_video_file=True)
    assert any(issue.field == "video" and issue.is_error for issue in issues)


def test_missing_level_is_error():
    issues = check_draft(_draft(level=None), check_video_file=False)
    assert any(issue.field == "level" and issue.is_error for issue in issues)


def test_dual_mode_requires_feeling_rating():
    issues = check_draft(_draft(feeling_rating=""), check_video_file=False)

    assert any(
        issue.field == "feeling"
        and issue.is_error
        and issue.message == "TUF 投稿需要体感难度，例如 G12"
        for issue in issues
    )


def test_bili_only_does_not_require_feeling_rating():
    issues = check_draft(
        _draft(feeling_rating=""),
        check_video_file=False,
        publish_mode="bili_only",
    )

    assert not any(issue.field == "feeling" for issue in issues)


def test_title_over_hard_limit_is_error():
    issues = check_draft(_draft(title="标" * 81), check_video_file=False)
    assert any(issue.field == "title" and issue.is_error for issue in issues)


def test_title_over_warn_limit_is_warning():
    issues = check_draft(_draft(title="标" * 60), check_video_file=False)
    assert any(issue.field == "title" and issue.is_warning for issue in issues)


def test_too_many_tags_is_error():
    issues = check_draft(
        _draft(tags=[f"tag{index}" for index in range(11)]),
        check_video_file=False,
    )
    assert any(issue.field == "tags" and issue.is_error for issue in issues)


def test_judgement_total_mismatch_is_error():
    judgements = empty_judgements()
    judgements["perfect"] = 90
    issues = check_draft(
        _draft(judgements=judgements),
        check_video_file=False,
    )
    assert any(issue.field == "judgements" and issue.is_error for issue in issues)


def test_banned_word_is_warning():
    issues = check_draft(
        _draft(title="外挂测试通关"),
        check_video_file=False,
    )
    assert any(issue.field == "title" and issue.is_warning for issue in issues)


def test_empty_description_is_warning_only():
    issues = check_draft(_draft(description=""), check_video_file=False)
    assert any(issue.field == "description" and issue.is_warning for issue in issues)
    assert not any(issue.field == "description" and issue.is_error for issue in issues)


def test_bili_only_skips_tuf_level_and_judgement_checks():
    judgements = empty_judgements()
    judgements["perfect"] = 90
    issues = check_draft(
        _draft(level=None, judgements=judgements),
        check_video_file=False,
        publish_mode="bili_only",
    )

    assert not any(issue.field in {"level", "judgements"} for issue in issues)
    assert not [issue for issue in issues if issue.is_error]


def test_bili_only_keeps_bilibili_required_checks():
    issues = check_draft(
        _draft(title="", tags=[], description=""),
        check_video_file=False,
        publish_mode="bili_only",
    )

    assert any(issue.field == "title" and issue.is_error for issue in issues)
    assert any(issue.field == "tags" and issue.is_warning for issue in issues)
    assert any(issue.field == "description" and issue.is_warning for issue in issues)


def test_hit_count_uses_playable_hits_for_local_entries():
    # 本地条目：总格 103、中旋 2、自动砖 1，可击物量 100。
    # 判定打击数 100 应通过；不能按 tilecount - auto 算成 102。
    level = LevelInfo(
        tuf_id=17776,
        song="Beneath the Vortex",
        tilecount=103,
        midspin_count=2,
        auto_tile_count=1,
        playable_hits=100,
    )
    issues = check_draft(_draft(level=level), check_video_file=False)

    assert not [issue for issue in issues if issue.field == "judgements" and issue.is_error]
    ok = [issue for issue in issues if issue.field == "judgements" and issue.level == "ok"]
    assert len(ok) == 1
    assert "打击数 100" in ok[0].message
    assert "共 103 个砖块" in ok[0].message


def test_hit_count_mismatch_message_follows_tuf_wording():
    # 可击中物量 100，打击数 90；九格和还是 90（没填提前 / 太慢！！）。
    judgements = empty_judgements()
    judgements["perfect"] = 90
    issues = check_draft(_draft(judgements=judgements), check_video_file=False)

    errors = [issue for issue in issues if issue.field == "judgements" and issue.is_error]
    assert len(errors) == 1
    message = errors[0].message
    assert "打击数 90" in message
    assert "可击中物量 100" in message
    assert "共 100 个砖块" in message
    assert "自动砖块 0 个" in message
    assert "提前 / 太慢！！不计入打击数" in message


def test_early_and_late_double_do_not_break_hit_count_validation():
    # 九格和 102、打击数 100 = 可击中物量：TUF 接受，工具不应报错。
    judgements = empty_judgements()
    judgements["perfect"] = 100
    judgements["earlyDouble"] = 1
    judgements["lateDouble"] = 1
    issues = check_draft(_draft(judgements=judgements), check_video_file=False)

    assert not any(issue.field == "judgements" and issue.is_error for issue in issues)
    ok = [issue for issue in issues if issue.field == "judgements" and issue.level == "ok"]
    assert len(ok) == 1
    assert "打击数 100" in ok[0].message
    assert "判定总数 102" in ok[0].message
    assert "共 100 个砖块，自动砖块 0 个" in ok[0].message


def test_hit_count_validation_counts_auto_tiles():
    judgements = empty_judgements()
    judgements["perfect"] = 95
    level = LevelInfo(tuf_id=17776, song="Beneath the Vortex", tilecount=100, auto_tile_count=5)
    issues = check_draft(_draft(level=level, judgements=judgements), check_video_file=False)

    assert not any(issue.field == "judgements" and issue.is_error for issue in issues)
    ok = [issue for issue in issues if issue.field == "judgements" and issue.level == "ok"]
    assert "自动砖块 5 个" in ok[0].message


def test_dual_mode_still_enforces_tuf_checks():
    judgements = empty_judgements()
    judgements["perfect"] = 90
    issues = check_draft(
        _draft(
            level=LevelInfo(tuf_id=17776, song="Beneath the Vortex", tilecount=100),
            judgements=judgements,
        ),
        check_video_file=False,
        publish_mode="dual",
    )

    assert any(issue.field == "judgements" and issue.is_error for issue in issues)
