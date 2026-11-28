# 第三方组件与许可声明

ADOFAI GO 本体采用 **MIT** 许可证，全文见 [LICENSE](LICENSE)。
本项目依赖并分发下列第三方组件；它们在 `licenses/` 目录下的许可证全文随源码与发行包一同提供。

组件版本为 v0.17.22（2026-11-28）实测锁定，与 [requirements.txt](requirements.txt)、[requirements-dev.txt](requirements-dev.txt) 一致。

## 运行时组件（随 exe 分发）

| 组件 | 版本 | 许可证 | 用途 |
| --- | --- | --- | --- |
| [PySide6](https://pypi.org/project/PySide6/) | 6.12.0 | LGPL-3.0-only（本项目按 **LGPLv3** 使用） | Qt 界面绑定 |
| [shiboken6](https://pypi.org/project/shiboken6/) | 6.12.0 | LGPL-3.0-only | PySide6 的绑定运行时 |
| Qt 6.12（随 PySide6 分发） | 6.12 | LGPL-3.0-only / GPL / 商业 | GUI、网络、WebEngine 等运行库 |
| [Playwright](https://pypi.org/project/playwright/) | 1.65.2 | Apache-2.0 | 驱动本机浏览器完成 B 站 / TUF 操作 |
| [requests](https://pypi.org/project/requests/) | 2.34.2 | Apache-2.0 | HTTP 请求 |
| [urllib3](https://pypi.org/project/urllib3/) | 2.8.0 | MIT | requests 的传输层 |
| [certifi](https://pypi.org/project/certifi/) | 2026.10.23 | MPL-2.0 | CA 根证书包 |
| [charset-normalizer](https://pypi.org/project/charset-normalizer/) | 3.5.2 | MIT | 响应编码识别 |
| [idna](https://pypi.org/project/idna/) | 3.20 | BSD-3-Clause | 国际化域名 |
| Python 3.11 | 3.11 | PSF-2.0 | 打包版内嵌的 Python 运行时 |
| [PyInstaller](https://pypi.org/project/pyinstaller/) | 6.22.3 | GPL-2.0-or-later，附打包引导器例外 | 仅用于生成 exe，见下 |

PyInstaller 的 GPL 只作用于其引导器（bootloader），其许可证附带例外，允许用它打包并分发非自由程序：
本项目 exe 的许可证仍是 MIT，不受 GPL 影响。

## 开发与测试组件（不随发行包分发）

| 组件 | 版本 | 许可证 |
| --- | --- | --- |
| [pytest](https://pypi.org/project/pytest/) | 9.2.0 | MIT |
| [bandit](https://pypi.org/project/bandit/) | 1.9.4 | Apache-2.0 |
| [pip-audit](https://pypi.org/project/pip-audit/) | >=2.7 | Apache-2.0 |

## 许可证全文

以下文件均按上游原文保存（仅文件名不同），位于 [`licenses/`](licenses/)：

| 文件 | 许可证 | 覆盖组件 |
| --- | --- | --- |
| [LGPL-3.0.txt](licenses/LGPL-3.0.txt) | GNU Lesser General Public License v3 | PySide6、shiboken6、Qt 6.12 |
| [GPL-3.0.txt](licenses/GPL-3.0.txt) | GNU General Public License v3 | LGPLv3 所引用的基础条款 |
| [Apache-2.0.txt](licenses/Apache-2.0.txt) | Apache License 2.0 | Playwright、requests |
| [requests-NOTICE.txt](licenses/requests-NOTICE.txt) | Apache-2.0 NOTICE | requests |
| [urllib3-LICENSE.txt](licenses/urllib3-LICENSE.txt) | MIT | urllib3 |
| [charset-normalizer-LICENSE.txt](licenses/charset-normalizer-LICENSE.txt) | MIT | charset-normalizer |
| [idna-LICENSE.txt](licenses/idna-LICENSE.txt) | BSD-3-Clause | idna |
| [certifi-LICENSE.txt](licenses/certifi-LICENSE.txt) | MPL-2.0 声明（正文见 <https://mozilla.org/MPL/2.0/>） | certifi |
| [pyinstaller-COPYING.txt](licenses/pyinstaller-COPYING.txt) | GPL-2.0-or-later + 引导器例外 | PyInstaller |
| [LicenseRef-Qt-Commercial.txt](licenses/LicenseRef-Qt-Commercial.txt) | Qt 商业许可引用 | 仅作说明，本项目不使用商业许可 |

## PySide6 / Qt 的 LGPLv3 说明

本项目以 **LGPLv3**（`LGPL-3.0-only`）使用 PySide6 6.12.0 及其附带的 Qt 6.12 运行库，
**没有修改** PySide6 或 Qt 的源代码，也没有静态链接；PySide6 的扩展模块在运行时动态加载 Qt 动态库。
随 exe 分发的是 PyPI 上原样的 PySide6 / shiboken6 wheel 内容。

### 如何获取对应源码

- PySide6 6.12.0 源码（sdist）：<https://pypi.org/project/PySide6/6.12.0/#files>
- shiboken6 6.12.0 源码（sdist）：<https://pypi.org/project/shiboken6/6.12.0/#files>
- Qt 6.12 源码与版本归档：<https://download.qt.io/official_releases/qt/>
- Qt 源码在线浏览与克隆：<https://code.qt.io/>

如果上述上游地址不可用，可在本仓库提交 Issue 索取对应的第三方源码副本。

### 如何替换或重新链接 PySide6 / Qt

- **从源码运行**：按 [README](README.md#quickstart) 的源码步骤安装依赖后，用你自己的 PySide6 构建替换原有版本
  （例如 `python -m pip install <你的 PySide6 wheel>`），再执行 `python run.py`，即可以修改后的 Qt 运行本应用。
- **使用打包版**：exe 把 PySide6 / Qt 一并封装在单个文件中，无法原地替换其中的动态库。
  如需使用自己编译或修改的 PySide6 / Qt，请按 [开发指南](docs/DEVELOPMENT.md) 从源码重新打包（`.\build.ps1`）：
  先安装替换后的 PySide6，再重新构建即可。
- 本应用不校验、不锁定所加载的 PySide6 / Qt 动态库，也没有添加任何禁止替换、禁止反向工程
  （为调试上述修改）或禁止再分发的条款；MIT 许可证同样不附加此类限制。

Qt 同时提供商业许可，本项目不涉及，`licenses/LicenseRef-Qt-Commercial.txt` 仅用于保留 PySide6 wheel 中的原始引用。
