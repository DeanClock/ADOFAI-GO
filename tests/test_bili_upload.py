import logging
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ADOFAI_NO_AUTOLOGIN", "1")

import pytest

from adofai_publisher.browser import redact_secrets
from adofai_publisher.workers import (
    BiliUploadWorker,
    click_submit,
    cover_confirm_pending,
    editor_ready,
    fill_tags,
    fill_title,
    find_manager_bvid,
    find_submit_block,
    first_visible,
    partition_selected,
    partition_target,
    select_partition,
    tag_chip_texts,
    upload_cover,
    wait_for_cover_confirmed,
    wait_for_manual_finish,
    wait_for_user_step,
    wait_submit_result,
)


# ---------------------------------------------------------------- 测试用的假页面


class FakeNode:
    def __init__(self, text="", visible=True, buttons=None):
        self.text = text
        self.visible = visible
        self.buttons = list(buttons or [])

    def inner_text(self, **_kwargs):
        return self.text

    def is_visible(self):
        return self.visible

    def wait_for(self, state="attached", **_kwargs):
        if state == "visible" and not self.visible:
            raise RuntimeError("node is not visible")
        return None

    def click(self, **_kwargs):
        return None

    def locator(self, selector):
        if selector == 'button, [role="button"]':
            return FakeNodes(self.buttons)
        return FakeNodes([])


class FakeNodes:
    def __init__(self, nodes):
        self.nodes = list(nodes)

    @property
    def first(self):
        return self.nodes[0] if self.nodes else FakeNode("")

    @property
    def last(self):
        return self.nodes[-1] if self.nodes else FakeNode("")

    def count(self):
        return len(self.nodes)

    def nth(self, index):
        return self.nodes[index]

    # Playwright 的 Locator 可以直接 inner_text，取的是第一个匹配节点
    def inner_text(self, **kwargs):
        return self.first.inner_text(**kwargs)

    def is_visible(self):
        return self.first.is_visible()


class FakePage:
    """够 BiliUploadWorker 跑完整流程的最小页面替身，不联网。"""

    def __init__(
        self,
        body="上传完成",
        url="",
        fail_on_submit=None,
        body_after_submit="",
        url_after_submit="",
    ):
        self._body = body
        self.url = url
        self.fail_on_submit = fail_on_submit
        self.body_after_submit = body_after_submit
        self.url_after_submit = url_after_submit
        self.submit_block = ""
        self.option_works = True
        self.backspace_works = True
        self.chips: list[str] = []
        self.waits = 0
        self.gotos: list[str] = []
        self.title = ""
        self.description = ""
        self.files: list[str] = []
        self.declaration = ""
        self.submitted = False
        self.partition_current = "vlog"
        self._tag_input = FakeTagInput(self)

    # -- 页面文本 -----------------------------------------------------------
    def body_text(self) -> str:
        text = self._body
        if self.submitted and self.body_after_submit:
            text = self.body_after_submit
        if self.submit_block:
            text += "\n" + self.submit_block
        return text

    def locator(self, selector):
        if selector == "body":
            return FakeNodes([FakeNode(self.body_text())])
        if "video-human-type" in selector and "select-item-cont" in selector:
            return FakeNodes([FakeNode(self.partition_current)])
        if "video-human-type" in selector and "select-controller" in selector:
            return FakeNodes([FakePartitionController(self)])
        if "video-human-type" in selector and "drop-list-v2-item" in selector:
            return FakeNodes([FakePartitionOption(self, "游戏")])
        if "label-item-v2" in selector or selector == ".tag-pre":
            return FakeNodes([FakeNode(chip) for chip in self.chips])
        if (
            "标签" in selector
            or "回车" in selector
            or "tag-container" in selector
            or "tag-input-wrp" in selector
        ):
            return FakeNodes([self._tag_input])
        if "创作声明" in selector or "declaration" in selector:
            return FakeNodes([FakeDeclarationField(self)])
        if 'accept*="video"' in selector or 'accept*="mp4"' in selector:
            return FakeNodes([FakeFileInput(self)])
        if "file" in selector and "type" in selector:
            return FakeNodes([FakeFileInput(self)])
        if "标题" in selector or "video-title" in selector:
            return FakeNodes([FakeTextInput(self, "title")])
        if "简介" in selector or "textarea" in selector:
            return FakeNodes([FakeTextInput(self, "description")])
        if "立即投稿" in selector or ".submit-add" in selector:
            return FakeNodes([FakeSubmitButton(self)])
        return FakeNodes([])

    def is_closed(self):
        return False

    def get_by_text(self, text, **_kwargs):
        return FakeNodes([FakeOption(self, text=text)])

    # -- 行为 ---------------------------------------------------------------
    def goto(self, url, **_kwargs):
        self.gotos.append(url)
        self.url = url

    def wait_for_timeout(self, _ms):
        self.waits += 1

    def press_backspace(self):
        if self.backspace_works and self.chips:
            self.chips.pop()

    def append_chip(self, text):
        if text:
            self.chips.append(text)


class FakeTagInput(FakeNode):
    def __init__(self, page):
        super().__init__("")
        self.page = page
        self.value = ""
        self.presses: list[str] = []

    def fill(self, value):
        self.value = value

    def press(self, key):
        self.presses.append(key)
        if key == "Backspace":
            self.page.press_backspace()
        elif key == "Enter":
            if getattr(self.page, "enter_works", True):
                self.page.append_chip(self.value)
            self.value = ""


class FakeTextInput(FakeNode):
    def __init__(self, page, field):
        super().__init__("")
        self.page = page
        self.field = field

    def fill(self, value):
        setattr(self.page, self.field, value)

    def input_value(self, **_kwargs):
        return getattr(self.page, self.field, "")


class FakeFileInput(FakeNode):
    def __init__(self, page):
        super().__init__("")
        self.page = page

    def set_input_files(self, path):
        self.page.files.append(str(path))

    def get_attribute(self, name):
        return "image/jpeg,.jpg,.png" if name == "accept" else None

    def evaluate(self, _script):
        return "视频封面 上传封面"


class FakeDeclarationField(FakeNode):
    def __init__(self, page):
        super().__init__("")
        self.page = page

    def input_value(self, **_kwargs):
        return self.page.declaration

    def click(self, **_kwargs):
        return None


class FakeOption(FakeNode):
    def __init__(self, page, text=""):
        super().__init__(text)
        self.page = page

    def click(self, **_kwargs):
        if self.page.option_works:
            self.page.declaration = self.text


class FakePartitionController(FakeNode):
    def __init__(self, page):
        super().__init__("")
        self.page = page

    def click(self, **_kwargs):
        self.page.partition_open = True


class FakePartitionOption(FakeNode):
    def __init__(self, page, title):
        super().__init__(title)
        self.page = page
        self.title = title

    def get_attribute(self, name):
        return self.title if name == "title" else None

    def click(self, **_kwargs):
        self.page.partition_current = self.title


class FakeSubmitButton(FakeNode):
    def __init__(self, page, text="立即投稿"):
        super().__init__(text)
        self.page = page

    def click(self, **_kwargs):
        self.page.submitted = True
        if self.page.fail_on_submit:
            self.page.submit_block = self.page.fail_on_submit
        if self.page.url_after_submit:
            self.page.url = self.page.url_after_submit


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.closed = False

    @property
    def pages(self):
        return [] if self.closed else [self.page]

    def new_page(self):
        return self.page

    def cookies(self, _url):
        return []

    def close(self):
        self.closed = True


class FakePlaywright:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


# ------------------------------------------------------------------ 脱敏


def test_redact_secrets_masks_cookies_and_tokens():
    raw = (
        "cookie: SESSDATA=abc123; bili_jct=def456\n"
        "accessToken=xyz789 refreshToken: rst000 csrfToken=csrf111\n"
        "Authorization: Bearer secret-token\n"
        "BV1xx411c7mD 上传完成 请先确认封面"
    )
    text = redact_secrets(raw)

    for secret in ("abc123", "def456", "xyz789", "rst000", "csrf111", "secret-token"):
        assert secret not in text
    assert "<已脱敏>" in text
    assert "BV1xx411c7mD" in text
    assert "请先确认封面" in text


def test_redact_secrets_keeps_plain_text():
    assert redact_secrets("B 站拦住了投稿：请先确认封面") == "B 站拦住了投稿：请先确认封面"


# -------------------------------------------------------------- 拦截检测


def test_find_submit_block_reads_toast_text():
    page = FakePage()
    page.locator = lambda selector: (
        FakeNodes([FakeNode("请先确认封面后再投稿")])
        if selector == ".bcc-message"
        else FakeNodes([])
    )

    assert find_submit_block(page) == "请先确认封面后再投稿"


def test_find_submit_block_reads_declaration_required_toast():
    page = FakePage()
    page.locator = lambda selector: (
        FakeNodes([FakeNode("请添加创作声明")])
        if selector == ".bcc-message"
        else FakeNodes([])
    )

    assert find_submit_block(page) == "请添加创作声明"


def test_find_submit_block_ignores_hidden_toast():
    page = FakePage()
    page.locator = lambda selector: (
        FakeNodes([FakeNode(self.body_text())])
        if selector == "body"
        else FakeNodes([FakeNode("请先确认封面", visible=False)])
    )
    page.body_text = lambda: "上传完成"

    assert find_submit_block(page) == ""


def test_find_submit_block_ignores_normal_page():
    page = FakePage(body="上传完成 封面已上传 选择创作声明")

    assert find_submit_block(page) == ""


def test_find_submit_block_falls_back_to_known_phrases():
    page = FakePage(body="上传完成 请先确认封面")

    assert find_submit_block(page) == "请先确认封面"


def test_find_submit_block_ignores_dropdown_placeholders():
    # “请选择分区”这类占位文字在正常投稿页上一直存在，不能算拦截
    page = FakePage(body="上传完成 请选择分区 请选择符合你视频内容的创作声明")

    assert find_submit_block(page) == ""


def test_find_submit_block_ignores_confirm_prompt():
    page = FakePage()
    page.locator = lambda selector: (
        FakeNodes([FakeNode("请确认稿件信息后发布")])
        if selector == ".bcc-message"
        else FakeNodes([FakeNode("上传完成")])
    )

    assert find_submit_block(page) == ""


class FakeClock:
    def __init__(self, step=30.0):
        self.now = 0.0
        self.step = step

    def __call__(self):
        self.now += self.step
        return self.now


def test_wait_submit_result_returns_bvid_from_url():
    page = FakePage(
        url="https://member.bilibili.com/platform/upload-manager/article?bvid=BV1xx411c7mD"
    )

    assert wait_submit_result(page) == ("submitted", "BV1xx411c7mD")


def test_wait_submit_result_blocks_immediately():
    page = FakePage(
        url="https://member.bilibili.com/platform/upload/video/frame",
        body="上传完成 请先确认封面后再投稿",
    )
    clock = FakeClock()

    state, detail = wait_submit_result(page, timeout=180, clock=clock)

    assert state == "blocked"
    assert "请先确认封面" in detail
    assert clock.now < 180  # 没有空等到超时


def test_wait_submit_result_reports_success_without_bvid():
    page = FakePage(
        url="https://member.bilibili.com/platform/upload/video/frame",
        body="稿件投递成功，可在稿件管理中查看",
    )

    assert wait_submit_result(page) == ("submitted", "")


def test_wait_submit_result_accepts_success_toast():
    page = FakePage(
        url="https://member.bilibili.com/platform/upload/video/frame", body="上传完成"
    )
    original = page.locator
    page.locator = lambda selector: (
        FakeNodes([FakeNode("投稿成功")])
        if selector == ".bcc-message"
        else original(selector)
    )

    assert wait_submit_result(page) == ("submitted", "")


def test_wait_submit_result_ignores_help_text_about_success():
    page = FakePage(
        url="https://member.bilibili.com/platform/upload/video/frame",
        body="上传完成 投稿成功后可在稿件管理页修改信息",
    )
    clock = FakeClock()

    assert wait_submit_result(page, timeout=180, clock=clock) == ("timeout", "")


def test_wait_submit_result_times_out_without_evidence():
    page = FakePage(
        url="https://member.bilibili.com/platform/upload/video/frame", body="上传完成"
    )
    clock = FakeClock()

    assert wait_submit_result(page, timeout=180, clock=clock) == ("timeout", "")


def test_find_manager_bvid_only_accepts_the_matching_title():
    page = FakePage(
        body=(
            "旧视频 BV1bb2222222 2026-11-01\n"
            "【冰与火之舞】carnation 97.48% BV1aa1111111 审核中"
        )
    )

    assert find_manager_bvid(page, "【冰与火之舞】carnation 97.48%") == "BV1aa1111111"


def test_find_manager_bvid_ignores_other_videos():
    page = FakePage(body="旧视频 BV1bb2222222 2026-11-01")

    assert find_manager_bvid(page, "【冰与火之舞】carnation 97.48%") == ""


def test_wait_submit_result_stops_when_cancelled():
    page = FakePage()

    class Cancelled:
        def is_set(self):
            return True

    assert wait_submit_result(page, cancelled=Cancelled()) == ("cancelled", "")


def test_wait_submit_result_reports_closed_browser():
    assert wait_submit_result(ClosedPage()) == ("closed", "")


# ------------------------------------------------------- 人工完成等待（不关浏览器）


class Flag:
    def __init__(self, value=False):
        self.value = value

    def is_set(self):
        return self.value

    def set(self):
        self.value = True

    def clear(self):
        self.value = False


class ClosedPage(FakePage):
    def is_closed(self):
        return True

    def wait_for_timeout(self, _ms):
        raise RuntimeError("page closed")


def test_wait_for_manual_finish_returns_bvid_from_body():
    page = FakePage(body="稿件投递成功 BV1xx411c7mD")

    assert wait_for_manual_finish(page) == ("submitted", "BV1xx411c7mD")


def test_wait_for_manual_finish_accepts_success_message():
    page = FakePage(body="稿件投递成功")

    assert wait_for_manual_finish(page) == ("submitted", "")


def test_wait_for_manual_finish_returns_done_when_user_confirms():
    page = FakePage(body="上传完成")
    done = Flag(True)

    assert wait_for_manual_finish(page, done=done) == ("done", "")


def test_wait_for_manual_finish_stops_when_cancelled():
    page = FakePage()

    assert wait_for_manual_finish(page, cancelled=Flag(True)) == ("cancelled", "")


def test_wait_for_manual_finish_reports_closed_browser():
    page = ClosedPage()

    assert wait_for_manual_finish(page) == ("closed", "")


def test_wait_for_manual_finish_times_out():
    page = FakePage(body="上传完成")
    clock = FakeClock(step=60)

    assert wait_for_manual_finish(page, timeout=600, clock=clock) == ("timeout", "")


def test_wait_for_user_step_returns_ok_when_check_passes():
    page = FakePage()
    state = {"ready": False}

    def check():
        return state["ready"]

    # 前几轮 false，后面变 true
    calls = {"count": 0}
    original_wait = page.wait_for_timeout

    def wait(_ms):
        calls["count"] += 1
        if calls["count"] >= 2:
            state["ready"] = True
        original_wait(_ms)

    page.wait_for_timeout = wait

    assert wait_for_user_step(page, check=check) == "ok"


def test_wait_for_user_step_accepts_manual_done():
    page = FakePage()

    assert wait_for_user_step(page, done=Flag(True)) == "ok"


def test_wait_for_user_step_times_out_and_reports_closed():
    page = FakePage()
    clock = FakeClock(step=60)

    assert wait_for_user_step(page, timeout=300, clock=clock) == "timeout"
    assert wait_for_user_step(ClosedPage()) == "closed"


# ------------------------------------------------------------ 多候选选择器


def test_first_visible_skips_hidden_first_match():
    class TwoNodesPage:
        def locator(self, _selector):
            return FakeNodes([FakeNode("hidden", visible=False), FakeNode("visible")])

    node, attempts = first_visible(TwoNodesPage(), (".x",), timeout=10)

    assert node is not None and node.text == "visible"
    assert any("不可见" in item for item in attempts)


def test_fill_title_uses_new_editor_input():
    page = FakePage()
    logs: list[str] = []
    warns: list[str] = []

    assert fill_title(page, "【冰与火之舞】carnation", logs.append, warns.append) is True
    assert page.title == "【冰与火之舞】carnation"
    assert warns == []
    assert logs == ["标题已填写"]


def test_fill_title_reports_tried_selectors():
    page = FakePage()
    original = page.locator
    page.locator = lambda selector: (
        FakeNodes([])
        if (
            "标题" in selector
            or "video-title" in selector
            or "input-val" in selector
            or "contenteditable" in selector
        )
        else original(selector)
    )
    warns: list[str] = []

    assert fill_title(page, "标题", None, warns.append) is False
    assert "已尝试" in warns[0]
    assert ".video-title input" in warns[0]


def test_click_submit_finds_submit_add_span():
    page = FakePage()
    warns: list[str] = []

    assert click_submit(page, warns.append) is True
    assert page.submitted is True
    assert warns == []


def test_click_submit_reports_tried_selectors():
    page = FakePage()
    original = page.locator
    page.locator = lambda selector: (
        FakeNodes([])
        if ("立即投稿" in selector or ".submit-add" in selector)
        else original(selector)
    )
    warns: list[str] = []

    assert click_submit(page, warns.append) is False
    assert "「立即投稿」" in warns[0]
    assert ".submit-add" in warns[0]
    assert "button:has-text" in warns[0]


def test_editor_ready_detects_new_editor():
    assert editor_ready(FakePage()) is True

    page = FakePage()
    original = page.locator
    page.locator = lambda selector: (
        FakeNodes([])
        if (
            "标题" in selector
            or "video-title" in selector
            or "input-val" in selector
            or "contenteditable" in selector
            or "标签" in selector
            or "回车" in selector
            or "tag-container" in selector
            or "tag-input-wrp" in selector
            or "立即投稿" in selector
            or ".submit-add" in selector
        )
        else original(selector)
    )
    assert editor_ready(page) is False


# ------------------------------------------------------------ 封面人工确认


def _page_with_dialog(dialog):
    page = FakePage()
    page.locator = lambda selector: (
        FakeNodes([dialog]) if selector == '[role="dialog"]' else FakeNodes([])
    )
    return page


def test_cover_confirm_pending_detects_dialog_with_confirm_button():
    page = _page_with_dialog(FakeNode("设置封面 裁剪预览", buttons=[FakeNode("确定")]))

    assert cover_confirm_pending(page) is True


def test_cover_confirm_pending_ignores_text_without_confirm_button():
    page = _page_with_dialog(FakeNode("视频封面 已上传", buttons=[FakeNode("更换")]))

    assert cover_confirm_pending(page) is False


def test_cover_confirm_pending_ignores_page_level_container():
    page = _page_with_dialog(
        FakeNode("封面" * 400, buttons=[FakeNode("确定")])
    )

    assert cover_confirm_pending(page) is False


def test_cover_confirm_pending_detects_crop_dialog_without_labeled_button():
    page = _page_with_dialog(FakeNode("裁剪封面", buttons=[FakeNode("好了")]))

    assert cover_confirm_pending(page) is True


def test_cover_confirm_pending_ignores_hidden_dialog():
    page = _page_with_dialog(
        FakeNode("设置封面 裁剪预览", visible=False, buttons=[FakeNode("确定")])
    )

    assert cover_confirm_pending(page) is False


def test_wait_for_cover_confirmed_returns_after_dialog_closes(monkeypatch):
    page = FakePage()
    polls = {"count": 0}

    def pending(_page):
        polls["count"] += 1
        return polls["count"] <= 2

    monkeypatch.setattr("adofai_publisher.workers.cover_confirm_pending", pending)
    messages: list[str] = []

    assert (
        wait_for_cover_confirmed(page, clock=FakeClock(step=1), log=messages.append)
        is True
    )
    assert any("检测到封面确认框" in message for message in messages)


def test_wait_for_cover_confirmed_continues_when_no_dialog(monkeypatch):
    page = FakePage()
    monkeypatch.setattr(
        "adofai_publisher.workers.cover_confirm_pending", lambda _page: False
    )

    assert (
        wait_for_cover_confirmed(page, clock=FakeClock(step=3), grace=10) is True
    )


def test_wait_for_cover_confirmed_times_out_when_dialog_stays(monkeypatch):
    page = FakePage()
    monkeypatch.setattr(
        "adofai_publisher.workers.cover_confirm_pending", lambda _page: True
    )

    assert (
        wait_for_cover_confirmed(page, timeout=300, clock=FakeClock(step=60))
        is False
    )


def test_wait_for_cover_confirmed_stops_when_cancelled():
    page = FakePage()

    class Cancelled:
        def is_set(self):
            return True

    assert wait_for_cover_confirmed(page, cancelled=Cancelled()) is False


class FakeCoverInput(FakeNode):
    def __init__(self, page, accept, context):
        super().__init__("")
        self.page = page
        self.accept = accept
        self.context = context

    def get_attribute(self, name):
        return self.accept if name == "accept" else None

    def evaluate(self, _script):
        return self.context

    def set_input_files(self, path):
        self.page.cover_files.append(str(path))


class _OpenCoverDialog(FakeNode):
    def __init__(self, page):
        super().__init__("")
        self.page = page

    def click(self, **_kwargs):
        self.page.dialog_open = True


class _NoFileChooser:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class CoverDialogPage(FakePage):
    """新编辑器：点「添加封面」打开封面制作弹窗，图片 input 藏在弹窗里。"""

    def __init__(self):
        super().__init__()
        self.dialog_open = False
        self.cover_files: list[str] = []
        self.image_input = FakeCoverInput(
            self, "image/png, image/jpeg", "上传封面 拖拽图片或点击上传"
        )

    def locator(self, selector):
        if selector == ".cover-editor":
            return FakeNodes(
                [FakeNode("封面制作", buttons=[FakeNode("确定")])]
                if self.dialog_open
                else []
            )
        if (
            selector.startswith(".cover-editor ")
            or selector.startswith("#editor_")
            or "cover-editor-" in selector
        ):
            # 认不出两个比例框的老弹窗：交给 _find_cover_image_input 兜底
            return FakeNodes([])
        if selector == 'input[type="file"]':
            return FakeNodes([self.image_input] if self.dialog_open else [])
        return super().locator(selector)

    def get_by_text(self, _text, **_kwargs):
        return FakeNodes([_OpenCoverDialog(self)])

    def expect_file_chooser(self, timeout=0):
        return _NoFileChooser()


def test_upload_cover_opens_cover_editor_and_sets_image(tmp_path):
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"png")
    page = CoverDialogPage()
    logs: list[str] = []

    assert upload_cover(page, str(cover), logs.append) is True
    assert page.dialog_open is True
    assert page.cover_files == [str(cover)]
    assert any("封面制作弹窗" in message for message in logs)


def test_cover_confirm_pending_detects_new_cover_editor():
    page = CoverDialogPage()
    page.dialog_open = True

    assert cover_confirm_pending(page) is True

    page.dialog_open = False

    assert cover_confirm_pending(page) is False


# ------------------------------------------------------------ 自动选分区


def test_partition_target_maps_old_game_subcategories():
    assert partition_target("单机游戏") == "游戏"
    assert partition_target("网络游戏") == "游戏"
    assert partition_target("游戏") == "游戏"
    assert partition_target("") == "游戏"
    assert partition_target("音乐") == "音乐"


def test_select_partition_chooses_main_category():
    page = FakePage()
    messages: list[str] = []

    assert select_partition(page, "单机游戏", messages.append) is True
    assert page.partition_current == "游戏"
    assert partition_selected(page, "游戏") is True
    assert any("分区已选择：游戏" in message for message in messages)


def test_select_partition_keeps_already_selected():
    page = FakePage()
    page.partition_current = "游戏"
    messages: list[str] = []

    assert select_partition(page, "单机游戏", messages.append) is True
    assert any("分区已是：游戏" in message for message in messages)


def test_select_partition_reports_when_option_missing():
    page = FakePage()
    original = page.locator
    page.locator = lambda selector: (
        FakeNodes([])
        if "video-human-type" in selector and "drop-list-v2-item" in selector
        else original(selector)
    )
    messages: list[str] = []

    assert select_partition(page, "单机游戏", messages.append) is False
    assert any("分区没有自动选中" in message for message in messages)
    assert any("目标：游戏" in message for message in messages)
    assert any("vlog" in message for message in messages)


# ------------------------------------------------------------------ 标签


def test_fill_tags_clears_existing_tags_first():
    page = FakePage()
    page.chips = ["冰与火之舞", "音乐", "ADoFAI"]
    messages: list[str] = []

    count = fill_tags(page, ["冰与火之舞", "P10", "carnation"], messages.append)

    assert count == 3
    assert page.chips == ["冰与火之舞", "P10", "carnation"]
    assert any("已清空" in message for message in messages)
    assert messages[-1].startswith("标签核对通过")


def test_fill_tags_reports_mismatch():
    page = FakePage()
    page.chips = ["B 站推荐标签"]
    page.backspace_works = False  # 清不掉时至少要报出来，不能默默继续
    messages: list[str] = []

    count = fill_tags(page, ["冰与火之舞"], messages.append)

    assert count == 1
    mismatch = [message for message in messages if "不一致" in message]
    assert mismatch
    assert "B 站推荐标签" in mismatch[0]


def test_tag_chip_texts_returns_empty_when_container_has_no_chips():
    assert tag_chip_texts(FakePage()) == []


def test_tag_chip_texts_returns_none_when_structure_unknown():
    page = FakePage()
    page.locator = lambda _selector: FakeNodes([])

    assert tag_chip_texts(page) is None


def test_tag_chip_texts_reads_new_editor_chips():
    page = FakePage()
    page.chips = ["冰与火之舞", "P10"]

    assert tag_chip_texts(page) == ["冰与火之舞", "P10"]


def test_fill_tags_no_false_alarm_when_chip_structure_unknown():
    page = FakePage()
    original = page.locator
    blocked = (
        "label-item-v2",
        ".tag-pre",
        ".tag-item",
        ".bili-tag",
        ".vui_tag",
        "tag-input",
        "tag-container",
    )
    page.locator = lambda selector: (
        FakeNodes([]) if any(hint in selector for hint in blocked) else original(selector)
    )
    messages: list[str] = []

    count = fill_tags(page, ["冰与火之舞", "P10"], messages.append)

    assert count == 2
    assert not any("不一致" in message for message in messages)


def test_fill_tags_reports_when_tag_never_appears():
    page = FakePage()
    page.enter_works = False  # 回车没让组件建出标签块
    messages: list[str] = []

    count = fill_tags(page, ["冰与火之舞", "P10"], messages.append)

    assert count == 0
    assert any("没有出现在页面上" in message for message in messages)
    assert page.chips == []


# ------------------------------------------------- 整个上传流程（假浏览器）


def _run_worker(monkeypatch, tmp_path, page, caplog, cover="", context_hook=None):
    from adofai_publisher.config import Settings

    context = FakeContext(page)
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright", lambda: FakePlaywright()
    )
    monkeypatch.setattr(
        "adofai_publisher.workers.launch_context", lambda *_a, **_k: context
    )
    monkeypatch.setattr("adofai_publisher.workers.has_bili_login", lambda *_a, **_k: True)
    video = tmp_path / "clear.mp4"
    video.write_bytes(b"video")

    worker = BiliUploadWorker(
        Settings(tmp_path / "settings.json"),
        str(video),
        cover,
        "【冰与火之舞】carnation 97.48%",
        "简介",
        ["冰与火之舞", "P10"],
        "单机游戏",
        "内容无需标注",
    )
    finished: list[dict] = []
    failed: list[str] = []
    manual: list[str] = []
    worker.finished.connect(finished.append)
    worker.failed.connect(failed.append)
    worker.needs_manual.connect(manual.append)
    if context_hook is not None:
        context_hook(context)
    with caplog.at_level(logging.WARNING, logger="adofai"):
        worker.run()
    return finished, failed, context, manual


def test_upload_worker_blocked_defers_to_manual_and_keeps_browser_open(
    monkeypatch, tmp_path, caplog
):
    page = FakePage(fail_on_submit="请先确认封面后再投稿")
    held: dict = {}

    def fake_manual(*_args, **_kwargs):
        # 进入人工等待时浏览器必须还开着，否则用户没法按提示手动完成
        held["closed_when_waiting"] = held["context"].closed
        return "submitted", "BV1xx411c7mD"

    monkeypatch.setattr("adofai_publisher.workers.wait_for_manual_finish", fake_manual)

    finished, failed, context, manual = _run_worker(
        monkeypatch,
        tmp_path,
        page,
        caplog,
        context_hook=lambda ctx: held.setdefault("context", ctx),
    )

    assert finished and finished[0]["bvid"] == "BV1xx411c7mD"
    assert failed == []
    assert held["closed_when_waiting"] is False
    assert manual and "B 站拦住了投稿" in manual[0]
    assert "B 站拦住了投稿" in caplog.text
    assert context.closed is True  # 真正结束后才关


def test_upload_worker_manual_timeout_closes_browser_only_at_the_end(
    monkeypatch, tmp_path, caplog
):
    page = FakePage()
    held: dict = {}

    def fake_manual(*_args, **_kwargs):
        held["closed_when_waiting"] = held["context"].closed
        return "timeout", ""

    monkeypatch.setattr("adofai_publisher.workers.wait_for_manual_finish", fake_manual)
    monkeypatch.setattr(
        "adofai_publisher.workers.wait_submit_result",
        lambda *_a, **_k: ("timeout", ""),
    )

    finished, failed, context, manual = _run_worker(
        monkeypatch,
        tmp_path,
        page,
        caplog,
        context_hook=lambda ctx: held.setdefault("context", ctx),
    )

    assert finished == []
    assert failed and "10 分钟" in failed[0]
    assert held["closed_when_waiting"] is False
    assert context.closed is True
    assert manual


def test_upload_worker_finds_bvid_in_manager_after_manual_done(
    monkeypatch, tmp_path, caplog
):
    page = FakePage(
        body=(
            "上传完成\n【冰与火之舞】carnation 97.48% BV1aa1111111 审核中"
        )
    )
    monkeypatch.setattr(
        "adofai_publisher.workers.wait_for_manual_finish",
        lambda *_a, **_k: ("done", ""),
    )
    monkeypatch.setattr(
        "adofai_publisher.workers.wait_submit_result",
        lambda *_a, **_k: ("timeout", ""),
    )

    finished, failed, _context, manual = _run_worker(
        monkeypatch, tmp_path, page, caplog
    )

    assert failed == []
    assert finished[0]["bvid"] == "BV1aa1111111"
    assert manual


def test_upload_worker_missing_submit_button_defers_to_manual(
    monkeypatch, tmp_path, caplog
):
    page = FakePage()
    original = page.locator
    page.locator = lambda selector: (
        FakeNodes([])
        if ("立即投稿" in selector or ".submit-add" in selector)
        else original(selector)
    )
    monkeypatch.setattr(
        "adofai_publisher.workers.wait_for_manual_finish",
        lambda *_a, **_k: ("submitted", "BV1xx411c7mD"),
    )

    finished, failed, _context, manual = _run_worker(
        monkeypatch, tmp_path, page, caplog
    )

    assert failed == []
    assert finished[0]["bvid"] == "BV1xx411c7mD"
    assert manual and "立即投稿" in manual[0]
    # 失败提示里要能看出试过哪些选择器
    assert "已尝试" in caplog.text


def test_upload_worker_finishes_without_bvid_after_success(monkeypatch, tmp_path, caplog):
    page = FakePage(body_after_submit="上传完成\n稿件投递成功")

    finished, failed, _context, _manual = _run_worker(
        monkeypatch, tmp_path, page, caplog
    )

    assert failed == []
    assert finished == [{"bvid": "", "submitted": True}]


def test_upload_worker_finishes_with_bvid(monkeypatch, tmp_path, caplog):
    page = FakePage(
        url_after_submit="https://www.bilibili.com/video/BV1xx411c7mD"
    )

    finished, failed, _context, _manual = _run_worker(
        monkeypatch, tmp_path, page, caplog
    )

    assert failed == []
    assert finished[0]["bvid"] == "BV1xx411c7mD"
    # 草稿里的「单机游戏」映射成新编辑器的「游戏」并自动选中
    assert page.partition_current == "游戏"


def test_upload_worker_declaration_manual_timeout(monkeypatch, tmp_path, caplog):
    page = FakePage()
    page.option_works = False  # 点选项不生效，模拟创作声明静默失败
    monkeypatch.setattr(
        "adofai_publisher.workers.wait_for_user_step",
        lambda *_args, **_kwargs: "timeout",
    )

    finished, failed, context, manual = _run_worker(
        monkeypatch, tmp_path, page, caplog
    )

    assert finished == []
    assert failed and "创作声明" in failed[0]
    assert "创作声明没有选中" in caplog.text
    assert page.submitted is False
    assert manual and "创作声明" in manual[0]
    assert context.closed is True


def test_upload_worker_cover_manual_step_continues(monkeypatch, tmp_path, caplog):
    page = FakePage(body_after_submit="上传完成\n稿件投递成功")
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"png")
    monkeypatch.setattr(
        "adofai_publisher.workers.wait_for_cover_confirmed", lambda *_a, **_k: False
    )
    monkeypatch.setattr(
        "adofai_publisher.workers.wait_for_user_step",
        lambda *_a, **_k: "ok",
    )

    finished, failed, _context, manual = _run_worker(
        monkeypatch, tmp_path, page, caplog, cover=str(cover)
    )

    assert failed == []
    assert finished == [{"bvid": "", "submitted": True}]
    assert manual and "封面" in manual[0]
    assert str(cover) in page.files


def test_upload_worker_cover_manual_timeout(monkeypatch, tmp_path, caplog):
    page = FakePage()
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"png")
    monkeypatch.setattr(
        "adofai_publisher.workers.wait_for_cover_confirmed", lambda *_a, **_k: False
    )
    monkeypatch.setattr(
        "adofai_publisher.workers.wait_for_user_step",
        lambda *_a, **_k: "timeout",
    )

    finished, failed, context, manual = _run_worker(
        monkeypatch, tmp_path, page, caplog, cover=str(cover)
    )

    assert finished == []
    assert failed and "封面" in failed[0]
    assert page.submitted is False
    assert manual
    assert context.closed is True


def test_upload_worker_continues_after_cover_confirmed(monkeypatch, tmp_path, caplog):
    page = FakePage(body_after_submit="上传完成\n稿件投递成功")
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"png")
    monkeypatch.setattr(
        "adofai_publisher.workers.wait_for_cover_confirmed", lambda *_a, **_k: True
    )

    finished, failed, _context, _manual = _run_worker(
        monkeypatch, tmp_path, page, caplog, cover=str(cover)
    )

    assert failed == []
    assert finished == [{"bvid": "", "submitted": True}]


# ------------------------------------------------- 主窗口落库（无 BV / 失败）


@pytest.fixture
def window(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    monkeypatch.setenv("ADOFAI_TUF_HOME", str(tmp_path))
    from adofai_publisher.config import Settings
    from adofai_publisher.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow(Settings(tmp_path / "settings.json"))
    win.draft.video_path = str(tmp_path / "clear.mp4")
    win.draft.title = "【冰与火之舞】carnation"
    win.draft.status = "draft"
    yield win
    win.close()
    app.processEvents()


def test_upload_finished_without_bvid_still_saves_record(window):
    window._on_upload_finished({"bvid": "", "submitted": True})

    records = window.db.all()
    assert len(records) == 1
    assert records[0]["status"] == "bili_uploaded"
    assert records[0]["bvid"] == ""
    assert window.draft.status == "bili_uploaded"

    # 用户之后手动补 BV：同一条记录被补全，不新增重复记录
    window._on_bvid_committed("BV1xx411c7mD")
    records = window.db.all()
    assert len(records) == 1
    assert records[0]["bvid"] == "BV1xx411c7mD"
    assert records[0]["status"] == "bili_uploaded"


def test_upload_failed_saves_draft_record_with_last_error(window, monkeypatch):
    monkeypatch.setattr(window, "_show_error_dialog", lambda *args, **kwargs: None)

    window._on_upload_failed("B 站拦住了投稿：请先确认封面")

    records = window.db.all()
    assert len(records) == 1
    assert records[0]["status"] == "draft"
    assert "请先确认封面" in records[0]["last_error"]

    # 重试仍然失败：更新同一条记录，不重复堆叠
    window._on_upload_failed("B 站拦住了投稿：请先确认封面")
    assert len(window.db.all()) == 1


# ------------------------------------------------------ 界面：人工完成入口


class StubBrowserWorker:
    def __init__(self):
        self.manual_confirmed = False
        self.cancelled = False

    def confirm_manual(self):
        self.manual_confirmed = True

    def cancel(self):
        self.cancelled = True


def test_manual_prompt_shows_done_and_cancel_buttons(window):
    panel = window.publish_panel
    panel.set_manual_pending("请在浏览器里手动点投稿")

    assert panel.manual_box.isHidden() is False
    assert "手动点投稿" in panel.manual_label.text()
    assert "我已完成" in panel.manual_done_button.text()
    assert "取消" in panel.manual_cancel_button.text()


def test_manual_button_clicks_emit_signals(window):
    panel = window.publish_panel
    events: list[str] = []
    panel.manualDoneRequested.connect(lambda: events.append("done"))
    panel.manualCancelRequested.connect(lambda: events.append("cancel"))

    panel.manual_done_button.click()
    panel.manual_cancel_button.click()

    assert events == ["done", "cancel"]


def test_manual_buttons_call_worker(window):
    worker = StubBrowserWorker()
    window._active_browser_worker = worker
    window.publish_panel.set_manual_pending("请完成")

    window._on_manual_done()

    assert worker.manual_confirmed is True
    assert "已收到" in window.publish_panel.manual_label.text()

    window._on_manual_cancel()

    assert worker.cancelled is True
    assert "正在取消" in window.publish_panel.manual_label.text()


def test_manual_prompt_cleared_when_upload_ends(window):
    panel = window.publish_panel
    window._on_upload_needs_manual("请在浏览器里手动点投稿")
    assert panel.manual_box.isHidden() is False

    panel.set_busy(False)

    assert panel.manual_box.isHidden() is True
    assert panel.manual_label.text() == ""
