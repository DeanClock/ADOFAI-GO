"""ADOFAI GO 的设计 token 层。

这里只放纯数据：语义色、尺寸、间距、字号、动效时长。
不导入 Qt，不依赖任何运行环境，方便测试与其他 UI 模块直接引用。

配色方向：暖灰纸感、陶土橙强调、
粗字重标题、1px 细边框、大留白。浅色与深色各一套。
"""

from __future__ import annotations

from dataclasses import dataclass

# 字体族：QSS 片段与 QFont 共用的字符串。
SANS_FAMILY = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif'
# 标题字族：标题需要更强的字重。系统自带微软雅黑 Bold（msyhbd.ttc），
# 不打包字体文件、不新增依赖；正文仍用 SANS_FAMILY。
TITLE_FAMILY = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif'
# 旧衬线标题字族保留导出，避免外部脚本导入失败；主题已不再使用。
SERIF_FAMILY = '"Georgia", "Noto Serif SC", "Microsoft YaHei UI"'
MONO_FAMILY = '"Cascadia Mono", "Consolas", "JetBrains Mono", monospace'

# 标题字重：与微软雅黑 Bold 面一致。
TITLE_WEIGHT = 700


@dataclass(frozen=True)
class ThemeTokens:
    """一套主题的语义色。字段名描述用途，不描述色相。"""

    name: str

    # 纸底：窗口背景的垂直渐变两端
    paper_top: str
    paper_bottom: str

    # 侧边栏与表面
    sidebar: str
    surface: str
    surface_hover: str

    # 输入框/列表底，比 surface 略浅（浅色）或略深（深色）
    field: str

    # 1px 边框
    line: str
    line_soft: str

    # 文字：主 / 次 / 弱
    ink: str
    ink_muted: str
    ink_faint: str

    # 强调色（陶土橙）
    clay: str
    clay_hover: str
    clay_pressed: str
    on_accent: str
    accent_soft: str

    # 状态色与对应的浅背景
    success: str
    success_soft: str
    warning: str
    warning_soft: str
    danger: str
    danger_soft: str

    # 曲绘占位渐变（QSS 颜色串，允许 rgba）
    cover_top: str
    cover_mid: str
    cover_bottom: str

    # 阴影色（旧调色板的 shadow 键）
    shadow: str


@dataclass(frozen=True)
class Metrics:
    """尺寸、间距、字号与动效时长。单位均为逻辑像素 / 毫秒。"""

    # 外壳
    sidebar_width: int = 232
    nav_item_height: int = 40
    topbar_height: int = 56

    # 圆角
    radius_sheet: int = 14
    radius_card: int = 12
    radius_control: int = 8

    # 间距刻度 4–32
    space_4: int = 4
    space_8: int = 8
    space_12: int = 12
    space_16: int = 16
    space_24: int = 24
    space_32: int = 32

    # 页面留白：(左右, 上下)
    page_pad: tuple[int, int] = (28, 20)

    # 字号
    font_caption: int = 10
    font_small: int = 11
    font_body: int = 13
    font_subtitle: int = 12
    font_card_title: int = 14
    font_page_title: int = 19
    font_heading: int = 24
    font_display: int = 26

    # 标题字重（正文保持 QSS 默认 400，不在这里改）
    title_weight: int = TITLE_WEIGHT

    # 动效时长（毫秒）
    motion_fast: int = 120
    motion_base: int = 160
    motion_slow: int = 220

    @property
    def spacing_scale(self) -> tuple[int, ...]:
        """有序间距刻度，供后续布局代码循环取用。"""
        return (
            self.space_4,
            self.space_8,
            self.space_12,
            self.space_16,
            self.space_24,
            self.space_32,
        )

    @property
    def page_pad_x(self) -> int:
        return self.page_pad[0]

    @property
    def page_pad_y(self) -> int:
        return self.page_pad[1]


LIGHT_TOKENS = ThemeTokens(
    name="light",
    paper_top="#FAF7F1",
    paper_bottom="#F1ECE2",
    sidebar="#F4F0E8",
    surface="#FFFDF9",
    surface_hover="#F6F1E8",
    field="#FFFDFA",
    line="#E6DFD3",
    line_soft="#EFEAE1",
    ink="#2B2A26",
    ink_muted="#6F6A60",
    ink_faint="#8C8578",
    clay="#B85735",
    clay_hover="#A94E2F",
    clay_pressed="#9E4528",
    on_accent="#FFF8F2",
    accent_soft="rgba(184, 87, 53, 0.10)",
    success="#3F7D58",
    success_soft="rgba(63, 125, 88, 0.12)",
    warning="#A66A1F",
    warning_soft="rgba(166, 106, 31, 0.12)",
    danger="#B23A32",
    danger_soft="rgba(178, 58, 50, 0.12)",
    cover_top="rgba(184, 87, 53, 0.45)",
    cover_mid="rgba(184, 87, 53, 0.14)",
    cover_bottom="rgba(241, 236, 226, 0.95)",
    shadow="#E4DCCB",
)

DARK_TOKENS = ThemeTokens(
    name="dark",
    paper_top="#262624",
    paper_bottom="#1F1E1D",
    sidebar="#232220",
    surface="#30302E",
    surface_hover="#3A3833",
    field="#2B2A28",
    line="#3E3A34",
    line_soft="#34312C",
    ink="#EDE8DF",
    ink_muted="#A9A295",
    ink_faint="#8B8377",
    clay="#D97757",
    clay_hover="#CF6B4C",
    clay_pressed="#C26245",
    on_accent="#201510",
    accent_soft="rgba(217, 119, 87, 0.16)",
    success="#7FBF9A",
    success_soft="rgba(127, 191, 154, 0.14)",
    warning="#E0B36A",
    warning_soft="rgba(224, 179, 106, 0.14)",
    danger="#E08A82",
    danger_soft="rgba(224, 138, 130, 0.14)",
    cover_top="rgba(217, 119, 87, 0.50)",
    cover_mid="rgba(217, 119, 87, 0.16)",
    cover_bottom="rgba(31, 30, 29, 0.95)",
    shadow="#141312",
)

TOKENS: dict[str, ThemeTokens] = {"light": LIGHT_TOKENS, "dark": DARK_TOKENS}

METRICS = Metrics()


def tokens_for(theme: str | None) -> ThemeTokens:
    """按主题名取 token；除 ``"light"`` 外一律回退到深色，与旧 palette 行为一致。"""
    return LIGHT_TOKENS if theme == "light" else DARK_TOKENS
