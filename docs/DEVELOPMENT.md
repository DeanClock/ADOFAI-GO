# ADOFAI GO 开发指南

仓库结构、测试、打包与发布流程。
返回 [README](../README.md) · [使用指南](GUIDE.md) · [更新日志](../CHANGELOG.md)

---

## 环境要求

- Windows 10 / 11
- Python 3.11+（推荐 3.11）
- 依赖见 `requirements.txt`；开发、测试与打包依赖见 `requirements-dev.txt`

```powershell
python -m pip install -r requirements-dev.txt
```

---

## 仓库结构

```text
adofai_publisher/
  main.py              # 入口、字体注册、日志、--selftest、--page / --tab 参数
  models.py            # Draft / LevelInfo / 判定字段与权重
  accuracy.py          # 精准度计算
  compliance.py        # 标题、简介、标签、物量等合规预检
  config.py            # 设置、默认值与数据目录
  histdb.py            # 历史记录 JSON 数据库
  levellookup.py       # TUF 谱面查询与谱面安装
  tuf_browse.py        # TUF 在线浏览、筛选与分页
  tuf_review.py        # TUF 投稿审核状态读取
  tuf_submitter.py     # TUF 提交接口
  submission_status.py # 投稿状态判定与状态迁移
  level_library.py     # 本地谱面库 JSON 数据库
  batch_match.py       # 批量绑定的数值匹配与 TUF 口径换算
  level_paths.py       # 分类、难度与合集目录规则
  level_organizer.py   # 磁盘整理预览、迁移、备份与撤销
  ard_parser.py        # .ard / .adozip 谱面解析
  chart_analyzer.py    # .adofai 文件分析（BPM、事件、变速等）
  sav.py               # ADOFAI 存档严格模式检查
  wizard_state.py      # 草稿、模板渲染、历史记录转换
  copy_snippets.py     # 工作台快捷词条的默认值与规范化
  text_utils.py        # 富文本清理
  difficulty_icons.py  # P/G/U 与 Q 难度评级图标
  thumbnails.py        # TUF 曲绘缩略图加载与磁盘缓存
  browser.py           # Playwright 持久化上下文与选择器辅助
  adofaitools.py       # ADOFAI Tools 令牌读写
  download_tasks.py    # 下载队列与任务状态
  game_bridge.py       # 通过 Bridge Mod 在游戏中打开谱面
  hotkey.py            # 全局热键解析与监听
  obs_launcher.py      # 查找并启动 OBS
  obs_ws.py            # OBS WebSocket v5 客户端
  umm.py               # UMM 本体安装 / 卸载与 mod 管理
  updates.py           # GitHub Release 更新检查
  self_update.py       # 应用内下载、SHA256 校验与退出后替换
  child_process.py     # 启动外部程序时的干净环境变量
  resources.py         # 图标等打包资源路径
  workers.py           # QThread 后台任务（查询、下载、登录、OBS、UMM 等）
  ui/
    main_window.py       # 导航、线程调度、快捷键、草稿保存
    shell.py             # 侧边栏、状态行与外壳
    theme.py             # 深色/浅色 QSS
    design_tokens.py     # 配色、字号、间距与圆角度量
    interactions.py      # 光标、Toast、焦点框等全局交互层
    widgets.py           # 步骤条、拖拽区、判定格、Toast 等
    icons.py             # 侧边栏与状态图标
    home_page.py         # 首页：统计、待投稿、引导卡与快捷工具
    pages.py             # 工作台、文案预览、历史、设置
    chart_library.py     # 在线 TUF / 本地谱面卡片、筛选与详情面板
    chart_analyzer.py    # 谱面分析器对话框
    batch_bind.py        # 批量绑定谱面对话框
    collection_manager.py # 合集新建、改名与解散
    disk_organizer.py    # 磁盘整理预览与撤销界面
    organize_history.py  # 整理历史列表、明细与指定批次撤销
    download_center.py   # 下载任务中心对话框
    umm_page.py          # 「05 模组」页面
    window_placement.py  # 主窗口位置保存与恢复
    styleguide_page.py   # 隐藏样张页
tools/                   # 开发与打包辅助脚本
tests/                   # pytest 测试
run.py                   # 源码入口（python run.py）
run.cmd                  # 推荐启动入口，自动选择可用的 Python
build.ps1 / build.spec   # 打包脚本与 PyInstaller 配置
```

---

## 运行与调试

推荐用仓库根的 `run.cmd`：它会自动选择 `.venv` 里能导入 PySide6 的 Python，
失败时按 `pyvenv.cfg` 的 `home` 回退并补齐依赖路径。

```powershell
.\run.cmd
.\run.cmd --page workspace --tab 4   # 直接打开工作台第 4 步
.\run.cmd --print-command --page library  # 只打印将执行的命令，不启动界面
```

`python run.py` 仍然可用，但要自己保证用的是 `.venv` 的解释器。

常用页面直达参数：

```powershell
python run.py --page library       # 04 谱面库
python run.py --page analyzer      # 谱面分析器
python run.py --page umm           # 05 模组
python run.py --page styleguide    # 隐藏样张页，集中展示全部控件状态
```

`--page workspace --tab N` 可直达工作台指定步骤，`--tab` 也接受「文案」等步骤名。
界面上的正常步骤门禁不受影响。

---

## 测试

```powershell
python -m pytest -q
```

v0.17.25 时全量测试全部通过（含 1–2 项按本机资源自动跳过的用例）。测试覆盖：

- 9 项判定权重、精准度公式与 TUF 判定字段映射
- 标题/简介/标签/物量合规校验
- 历史记录兼容与增删改查
- TUF 关卡 JSON 解析、浏览参数、搜索语法、筛选与分页
- TUF 审核状态、投稿状态迁移与提交结果自检
- 本地谱面库元数据持久化与状态关联
- 合集归类、重命名、解散与筛选
- 分类目录规则、磁盘整理预览、勾选整理、整夹迁移、冲突保护、历史记录与指定批次撤销
- 文案模板渲染与工作台快捷词条
- 设置项默认值、旧版/未知键兼容与热键、发布模式迁移
- 下载队列状态、下载面板与谱面安装 / 解压 / 归类
- 谱面解析容错：旧版 pathData、逗号修复、裸控制字符、变体与自动砖
- 批量绑定的数值匹配与保守兜底
- Bridge 谱面开打、UMM 安装与 mod 管理、OBS 录制辅助
- 软件更新检查、应用内下载与 SHA256 校验
- 工作台五步流程、页面导航、`--page` 参数与 UI 构建
- 交互层光标/Toast/步骤条、页面主题一致性与外壳（侧边栏、切页、状态行、样张页）

想看所有控件的状态，可运行 `python run.py --page styleguide` 打开隐藏的样张页。

---

## 打包

```powershell
.\build.ps1
```

手工调试时也可以直接调用 PyInstaller（**不会**写入 Windows 文件版本信息，也不会生成版本副本与 `.sha256`；正式出包请用 `build.ps1`）：

```powershell
python -m PyInstaller --noconfirm --clean build.spec
```

产物：

```text
dist\ADOFAI GO.exe
dist\ADOFAI GO_v<版本号>.exe
dist\ADOFAI GO_v<版本号>.exe.sha256
```

`build.ps1` 会先跑测试，再检查打包结果是否混入外部运行库，最后运行打包后的自检。
版本副本先写入临时文件、校验一致后再改名，避免留下写了一半的 exe。
构建使用干净 PATH，优先使用仓库根的 `.venv`。

---

## 自检

```powershell
$env:ADOFAI_NO_AUTOLOGIN = "1"
$env:ADOFAI_SELFTEST_OUT = "$env:TEMP\adofai_selftest.log"
dist\ADOFAI GO.exe --selftest
```

自检会验证 Qt 运行库、TUF 谱面查询与 Playwright 浏览器启动，结果写入日志。
若图形界面启动失败，详细信息会写入 `%APPDATA%\AdofaiTufBridge\logs\startup-error.log`。

---

## 开源发布前检查

- 不要提交 `browser_profile/`、`records.json`、`draft.json`、日志、`.env`、证书或密钥。
- 截图和示例数据使用假账号、假 BV 号。
- 推送与 PR 会由 [GitHub Actions](../.github/workflows/tests.yml) 在 `windows-latest` / Python 3.11 上自动跑 `python -m pytest -q`，合并前确认 CI 通过。
- 运行 `python -m pytest -q`、`python -m pip_audit`、`python -m bandit -r adofai_publisher -ll`。
- 真实 UMM 0.33.3 安装包的端到端用例默认跳过；本机要跑的话，把安装包路径设进环境变量 `UMM_0333_ARCHIVE`（例如 `$env:UMM_0333_ARCHIVE = "D:\下载\UnityModManager.zip"`）后再执行 pytest。
- 依赖使用 `requirements.txt` / `requirements-dev.txt` 管理（v0.17.22 起为精确锁定版本），发布前审查升级。
- 分发 zip 必须带 `THIRD_PARTY_NOTICES.md` 与 `licenses/` 目录，许可证全文不可省略；升级依赖时同步更新两者。
- 发布的 exe 提供 SHA256；有条件时进行代码签名。
- 阅读 [SECURITY.md](../SECURITY.md)。
