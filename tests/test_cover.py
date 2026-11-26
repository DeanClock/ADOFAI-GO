from adofai_publisher.compliance import check_draft
from adofai_publisher.models import Draft
from adofai_publisher.workers import find_image_input, select_creative_declaration


class FakeInput:
    def __init__(self, accept, context=""):
        self.accept = accept
        self.context = context

    def get_attribute(self, name):
        return self.accept if name == "accept" else None

    def evaluate(self, _script):
        return self.context


class FakeInputs:
    def __init__(self, items):
        self.items = items

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class FakePage:
    def __init__(self, items):
        self.inputs = FakeInputs(items)

    def locator(self, _selector):
        return self.inputs


def test_find_image_input_skips_video_accept():
    page = FakePage(
        [
            FakeInput(".mp4,.flv,.mov"),
            FakeInput(".jpg,.jpeg,.png,.webp"),
        ]
    )

    selected = find_image_input(page)

    assert selected is page.inputs.items[1]


def test_find_image_input_prefers_cover_context():
    page = FakePage(
        [
            FakeInput(".jpg,.png", "创作者保护 上传示例图"),
            FakeInput(".jpg,.png", "视频封面 上传封面 更换封面"),
        ]
    )

    selected = find_image_input(page)

    assert selected is page.inputs.items[1]


def test_draft_roundtrip_keeps_cover_path():
    draft = Draft(
        video_path="C:/videos/clear.mp4",
        cover_path="C:/videos/cover.png",
        creative_declaration="含AI生成内容",
    )

    restored = Draft.from_dict(draft.to_dict())

    assert restored.cover_path == "C:/videos/cover.png"
    assert restored.creative_declaration == "含AI生成内容"


def test_missing_cover_is_warning_not_error(tmp_path):
    video = tmp_path / "clear.mp4"
    video.write_bytes(b"fake")
    draft = Draft(video_path=str(video), cover_path=str(tmp_path / "missing.png"))

    issues = check_draft(draft)

    cover_issues = [issue for issue in issues if issue.field == "cover"]
    assert cover_issues
    assert all(issue.is_warning for issue in cover_issues)


class FakeClickable:
    def __init__(self, log, name):
        self.log = log
        self.name = name

    def click(self, **_kwargs):
        self.log.append(("click", self.name))

    def wait_for(self, **_kwargs):
        return None


class FakeDeclarationField(FakeClickable):
    def __init__(self, page):
        super().__init__(page.log, "field")
        self.page = page

    def input_value(self, **_kwargs):
        return self.page.current

    def inner_text(self, **_kwargs):
        return self.page.current

    def click(self, **kwargs):
        super().click(**kwargs)


class FakeDeclarationOption(FakeClickable):
    def __init__(self, page):
        super().__init__(page.log, "option")
        self.page = page

    def click(self, **_kwargs):
        self.log.append(("click", "option"))
        if self.page.select_works:
            self.page.current = self.page.target


class FakeLocator:
    def __init__(self, items):
        self.items = list(items)

    @property
    def first(self):
        return self.items[0] if self.items else FakeClickable([], "missing")

    @property
    def last(self):
        return self.items[-1] if self.items else FakeClickable([], "missing")

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class FakeDeclarationPage:
    """模拟 B 站创作声明下拉：点选项才会把值写回字段。"""

    def __init__(self, log, select_works=True, current=""):
        self.log = log
        self.current = current
        self.select_works = select_works
        self.target = "内容无需标注"
        self.field = FakeDeclarationField(self)
        self.option = FakeDeclarationOption(self)
        self.url = ""

    def locator(self, selector):
        if any(
            key in selector
            for key in ("创作声明", "符合您视频内容", "declaration")
        ):
            return FakeLocator([self.field])
        return FakeLocator([])

    def get_by_text(self, _text, **_kwargs):
        return FakeLocator([self.option])

    def wait_for_timeout(self, _ms):
        return None


def test_select_creative_declaration_clicks_option_and_reads_back():
    log = []
    page = FakeDeclarationPage(log)
    messages = []

    ok = select_creative_declaration(page, "内容无需标注", messages.append)

    assert ok is True
    assert log == [("click", "field"), ("click", "option")]
    assert messages == ["创作声明已选择：内容无需标注"]


def test_select_creative_declaration_skips_when_already_selected():
    log = []
    page = FakeDeclarationPage(log, current="内容无需标注")
    messages = []

    ok = select_creative_declaration(page, "内容无需标注", messages.append)

    assert ok is True
    assert log == []
    assert messages == ["创作声明已选择：内容无需标注"]


def test_select_creative_declaration_fails_when_readback_mismatches():
    log = []
    page = FakeDeclarationPage(log, select_works=False)
    messages = []

    ok = select_creative_declaration(page, "内容无需标注", messages.append)

    assert ok is False
    assert "创作声明没有选中" in messages[-1]
    assert "内容无需标注" in messages[-1]


def test_draft_roundtrip_keeps_both_cover_ratios():
    draft = Draft(
        video_path="C:/videos/clear.mp4",
        cover_path="C:/covers/wide.png",
        cover_path_43="C:/covers/square.png",
    )

    restored = Draft.from_dict(draft.to_dict())

    assert restored.cover_path == "C:/covers/wide.png"
    assert restored.cover_path_43 == "C:/covers/square.png"


def test_old_draft_without_cover_path_43_loads_without_error():
    legacy = {
        "video_path": "C:/videos/old.mp4",
        "cover_path": "C:/covers/old.png",
        "title": "旧草稿",
    }

    draft = Draft.from_dict(legacy)

    assert draft.cover_path == "C:/covers/old.png"
    assert draft.cover_path_43 == ""
    assert draft.title == "旧草稿"


def test_cover_checks_cover_both_ratios(tmp_path):
    video = tmp_path / "clear.mp4"
    video.write_bytes(b"fake")
    wide = tmp_path / "wide.png"
    wide.write_bytes(b"fakepng")
    square_odd = tmp_path / "square.bmp"
    square_odd.write_bytes(b"fakebmp")
    draft = Draft(
        video_path=str(video),
        cover_path=str(wide),
        cover_path_43=str(square_odd),
    )

    issues = check_draft(draft)

    cover_issues = [issue for issue in issues if issue.field == "cover_43"]
    assert cover_issues
    assert all(issue.is_warning for issue in cover_issues)
    assert any("4:3 副图格式" in issue.message for issue in cover_issues)
    assert not [issue for issue in issues if issue.field == "cover"]


def test_missing_43_cover_is_warning_naming_ratio(tmp_path):
    video = tmp_path / "clear.mp4"
    video.write_bytes(b"fake")
    draft = Draft(video_path=str(video), cover_path_43=str(tmp_path / "nope.png"))

    issues = [issue for issue in check_draft(draft) if issue.field == "cover_43"]

    assert len(issues) == 1
    assert issues[0].is_warning
    assert "4:3" in issues[0].message


def test_cover_over_5mb_warns_per_ratio(tmp_path):
    big = tmp_path / "big.png"
    big.write_bytes(b"\0" * (5 * 1024**2 + 1))
    draft = Draft(cover_path=str(big), cover_path_43=str(big))

    messages = [issue.message for issue in check_draft(draft) if issue.field in ("cover", "cover_43")]

    assert any("16:9 主图超过 5MB" in message for message in messages)
    assert any("4:3 副图超过 5MB" in message for message in messages)
