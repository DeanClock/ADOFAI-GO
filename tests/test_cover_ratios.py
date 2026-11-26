"""新版「封面制作」弹窗：4:3 与 16:9 两张封面分别进各自的框。

页面替身按 B 站 videoup 前端 bundle 里的真实结构搭：
两个比例面板各有一个隐藏 input（面板为空时才有），面板有图后只能走底部
「上传封面」，目标由 activeEditor 决定；勾了「双比例同步改动」时传一张会
同时写进两个比例。
"""

from adofai_publisher.workers import (
    cover_editor_open,
    read_cover_sync,
    set_cover_sync,
    upload_cover,
)

WIDE = "16:9"
SQUARE = "4:3"

EDITOR = ".cover-editor"
TITLE = ".cover-editor-panel-canvas-title .text"
EMPTY_43 = '.cover-editor-panel-canvas-empty.ratio_4_3 input[type="file"]'
EMPTY_169 = '.cover-editor-panel-canvas-empty.ratio_16_9 input[type="file"]'
SHARED = '.cover-editor-panel-select .cover-upload input[type="file"]'
SYNC_LABEL = ".cover-editor .sync-checkbox"
SYNC_INPUT = '.cover-editor .sync-checkbox input[type="checkbox"]'
SYNC_WRAPPER = ".cover-editor .sync-checkbox-wrapper"


class Node:
    def __init__(self, text="", classes="", visible=True, on_click=None):
        self.text = text
        self.classes = classes
        self.visible = visible
        self.on_click = on_click
        self.clicks = 0

    def inner_text(self, **_kwargs):
        return self.text

    def get_attribute(self, name):
        return self.classes if name == "class" else None

    def is_visible(self):
        return self.visible

    def wait_for(self, state="attached", **_kwargs):
        if state == "visible" and not self.visible:
            raise RuntimeError("node is not visible")
        return None

    def click(self, **_kwargs):
        self.clicks += 1
        if self.on_click is not None:
            self.on_click()


class DeadNode(Node):
    """空 locator 上的节点：任何操作都失败，和 Playwright 超时一致。"""

    def __init__(self):
        super().__init__("", visible=False)

    def wait_for(self, **_kwargs):
        raise RuntimeError("locator resolved to 0 elements")

    def click(self, **_kwargs):
        raise RuntimeError("locator resolved to 0 elements")

    def set_input_files(self, _path):
        raise RuntimeError("locator resolved to 0 elements")

    def inner_text(self, **_kwargs):
        return ""


class Locator:
    def __init__(self, nodes):
        self.nodes = list(nodes)

    @property
    def first(self):
        return self.nodes[0] if self.nodes else DeadNode()

    @property
    def last(self):
        return self.nodes[-1] if self.nodes else DeadNode()

    def count(self):
        return len(self.nodes)

    def nth(self, index):
        return self.nodes[index]


class InputNode(Node):
    def __init__(self, page, ratio):
        super().__init__("")
        self.page = page
        self.ratio = ratio

    def set_input_files(self, path):
        self.page.set_cover(self.ratio, str(path))


class CheckInput(Node):
    def __init__(self, page):
        super().__init__("", on_click=page._toggle_sync)
        self.page = page

    def is_checked(self):
        return self.page.sync_checked


class CoverPage:
    """只实现封面弹窗相关选择器的页面替身。"""

    def __init__(
        self,
        *,
        sync_checked=False,
        sync_ui=True,
        ratio_43_ui=True,
        wide_filled=False,
        square_filled=False,
    ):
        self.sync_checked = sync_checked
        self.sync_ui = sync_ui
        self.ratio_43_ui = ratio_43_ui
        self.filled = {WIDE: wide_filled, SQUARE: square_filled}
        self.active = None
        self.uploads = []  # [(ratio, path)]
        self.page_files = []
        self.waits = 0

    # -- 模拟 B 站行为 ------------------------------------------------------
    def set_cover(self, ratio, path):
        self.uploads.append((ratio, path))
        self.filled[ratio] = True
        if self.sync_checked:
            other = WIDE if ratio == SQUARE else SQUARE
            self.filled[other] = True

    def _titles(self):
        nodes = []
        if self.ratio_43_ui:
            nodes.append(Node("首页推荐封面（4:3）", on_click=self._activate(SQUARE)))
        nodes.append(Node("个人空间封面（16:9）", on_click=self._activate(WIDE)))
        return nodes

    def _activate(self, ratio):
        def handler():
            self.active = ratio

        return handler

    def _sync_label(self):
        classes = "bcc-checkbox sync-checkbox"
        if self.sync_checked:
            classes += " bcc-checkbox-checked"
        return Node(
            "双比例同步改动",
            classes=classes,
            on_click=self._toggle_sync,
        )

    def _toggle_sync(self):
        # 勾了同步又上传时，B 站会把同一张图写进另一个比例
        self.sync_checked = not self.sync_checked

    # -- 页面接口 -----------------------------------------------------------
    def locator(self, selector):
        if selector == EDITOR:
            return Locator([Node("封面制作")])
        if selector == TITLE:
            return Locator(self._titles())
        if selector == EMPTY_43:
            if self.ratio_43_ui and not self.filled[SQUARE]:
                return Locator([InputNode(self, SQUARE)])
            return Locator([])
        if selector == EMPTY_169:
            if not self.filled[WIDE]:
                return Locator([InputNode(self, WIDE)])
            return Locator([])
        if selector == SHARED:
            return Locator([InputNode(self, self.active or WIDE)])
        if selector == "#editor_4_3":
            return Locator([Node("")] if self.filled[SQUARE] else [])
        if selector == "#editor_16_9":
            return Locator([Node("")] if self.filled[WIDE] else [])
        if selector == ".cover-editor .sync-checkbox-wrapper":
            return Locator([Node("")])
        if selector == SYNC_INPUT:
            if not self.sync_ui:
                return Locator([])
            return Locator([CheckInput(self)])
        if selector == SYNC_LABEL:
            return Locator([self._sync_label()] if self.sync_ui else [])
        if selector == ".cover-editor label":
            return Locator([])
        return Locator([])

    def wait_for_timeout(self, _ms):
        self.waits += 1

    def get_by_text(self, _text, **_kwargs):
        return Locator([])


def _files(tmp_path):
    wide = tmp_path / "wide.png"
    wide.write_bytes(b"png")
    square = tmp_path / "square.png"
    square.write_bytes(b"png")
    return wide, square


# ---------------------------------------------------------------- 两张图


def test_two_covers_go_into_their_own_ratio(tmp_path):
    wide, square = _files(tmp_path)
    # 上一次留下的勾选状态：两张图时必须先取消
    page = CoverPage(sync_checked=True)
    logs: list[str] = []
    warns: list[str] = []

    ok = upload_cover(
        page, str(wide), logs.append, warns.append, cover_path_43=str(square)
    )

    assert ok is True
    # 顺序：先 4:3 副图，再 16:9 主图
    assert page.uploads == [(SQUARE, str(square)), (WIDE, str(wide))]
    assert page.sync_checked is False
    assert page.filled == {WIDE: True, SQUARE: True}
    assert any("同步勾选状态：未勾选" in message for message in logs)
    assert any("已上传 4:3 副图（首页推荐封面）" in message for message in logs)
    assert any("已上传 16:9 主图（个人空间封面）" in message for message in logs)
    assert warns == []


def test_two_covers_use_shared_uploader_when_panels_already_have_images(tmp_path):
    wide, square = _files(tmp_path)
    # 弹窗打开时两个框都已有 B 站自动封面 → 面板里没有 input，只能切面板 + 底部上传
    page = CoverPage(sync_checked=False, wide_filled=True, square_filled=True)
    logs: list[str] = []

    ok = upload_cover(
        page, str(wide), logs.append, logs.append, cover_path_43=str(square)
    )

    assert ok is True
    assert page.uploads == [(SQUARE, str(square)), (WIDE, str(wide))]
    assert page.active == WIDE


# ---------------------------------------------------------------- 只有主图


def test_single_cover_checks_sync_and_fills_both(tmp_path):
    wide, _square = _files(tmp_path)
    page = CoverPage(sync_checked=False)
    logs: list[str] = []

    ok = upload_cover(page, str(wide), logs.append, logs.append)

    assert ok is True
    assert page.sync_checked is True
    # 勾了同步，一张图就铺满两个框，不再多传一次 4:3
    assert page.uploads == [(WIDE, str(wide))]
    assert page.filled == {WIDE: True, SQUARE: True}
    assert any("同步勾选状态：已勾选" in message for message in logs)
    assert any("4:3 副图未单独提供" in message for message in logs)


def test_single_cover_without_sync_checkbox_still_fills_both(tmp_path):
    wide, _square = _files(tmp_path)
    page = CoverPage(sync_checked=False, sync_ui=False)
    logs: list[str] = []
    warns: list[str] = []

    ok = upload_cover(page, str(wide), logs.append, warns.append)

    assert ok is True
    # 找不到同步勾选框：两张框各传一次同一张图
    assert page.uploads == [(WIDE, str(wide)), (SQUARE, str(wide))]
    assert any("同步勾选状态：未找到" in message for message in logs)
    assert any("没有找到「双比例同步改动」勾选框" in message for message in warns)


def test_single_cover_with_prefilled_panels_uploads_both_when_sync_missing(tmp_path):
    # 实测：视频传完后 B 站会自动把两个框填上帧封面，所以「面板里有图」不等于同步生效
    wide, _square = _files(tmp_path)
    page = CoverPage(sync_ui=False, wide_filled=True, square_filled=True)
    logs: list[str] = []
    warns: list[str] = []

    ok = upload_cover(page, str(wide), logs.append, warns.append)

    assert ok is True
    assert page.uploads == [(WIDE, str(wide)), (SQUARE, str(wide))]
    assert not any("已用同一张主图填充" in message for message in logs)
    assert any("16:9 主图也放进了「首页推荐封面」" in message for message in logs)


def test_only_43_cover_uploads_that_ratio(tmp_path):
    _wide, square = _files(tmp_path)
    page = CoverPage(sync_checked=False)
    logs: list[str] = []

    ok = upload_cover(page, "", logs.append, logs.append, cover_path_43=str(square))

    assert ok is True
    assert page.uploads == [(SQUARE, str(square))]


# ---------------------------------------------------------------- 找不到框


def test_missing_43_panel_warns_and_still_uploads_169(tmp_path):
    wide, square = _files(tmp_path)
    page = CoverPage(sync_checked=False, ratio_43_ui=False)
    logs: list[str] = []
    warns: list[str] = []

    ok = upload_cover(
        page, str(wide), logs.append, warns.append, cover_path_43=str(square)
    )

    assert ok is True
    assert page.uploads == [(WIDE, str(wide))]
    assert any("没有找到「4:3」封面框" in message for message in warns)
    assert any("已上传 16:9 主图" in message for message in logs)


def test_missing_main_cover_is_warning_not_crash(tmp_path):
    _wide, square = _files(tmp_path)
    page = CoverPage(sync_checked=False)
    warns: list[str] = []

    ok = upload_cover(
        page,
        str(tmp_path / "missing.png"),
        warns.append,
        warns.append,
        cover_path_43=str(square),
    )

    assert ok is True
    assert any("16:9 主图文件不存在" in message for message in warns)
    assert page.uploads == [(SQUARE, str(square))]


def test_no_cover_files_at_all_returns_false(tmp_path):
    page = CoverPage()
    warns: list[str] = []

    assert upload_cover(page, "", warns.append, warns.append) is False
    assert any("封面文件不存在" in message for message in warns)


# ---------------------------------------------------------------- 勾选框


def test_read_and_set_sync_state(tmp_path):
    page = CoverPage(sync_checked=False)
    logs: list[str] = []

    assert cover_editor_open(page) is True
    assert read_cover_sync(page) is False

    assert set_cover_sync(page, True, logs.append, logs.append) == "已勾选"
    assert page.sync_checked is True
    # 已经是目标状态时不再点
    clicks_before = page.waits
    assert set_cover_sync(page, True, logs.append, logs.append) == "已勾选"
    assert page.waits == clicks_before

    assert set_cover_sync(page, False, logs.append, logs.append) == "未勾选"
    assert page.sync_checked is False


def test_set_sync_without_checkbox_reports_missing(tmp_path):
    page = CoverPage(sync_ui=False)
    warns: list[str] = []

    assert read_cover_sync(page) is None
    assert set_cover_sync(page, True, warns.append, warns.append) == "未找到"
    assert any("勾选框" in message for message in warns)
