"""全局测试兜底。

1. 禁止真实模态对话框：无头环境里 ``QMessageBox`` 会一直阻塞等人点击，
   pytest-timeout 只能整进程杀掉，既看不到失败用例名也跑不完其余测试。
   静态便捷方法统一换成不阻塞的桩，需要特定返回值的测试自行 monkeypatch 覆盖。

2. 每个测试结束后销毁遗留的顶层控件：测试里直接构造的窗口不会自动回收，
   越积越多之后，主题切换的 ``setStyleSheet`` 要对所有存活控件做全量
   polish，整套测试会越跑越慢直到单测超时（O(窗口数²)，见 theme.py 注释）。
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QMessageBox


@pytest.fixture(autouse=True)
def _stub_message_boxes(monkeypatch):
    for level in ("information", "warning", "critical"):
        monkeypatch.setattr(
            QMessageBox,
            level,
            staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Ok),
        )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.No),
    )


@pytest.fixture(autouse=True)
def _reap_leaked_widgets():
    yield
    app = QApplication.instance()
    if app is None:
        return
    # 只删不 close：避免触发各页面的 closeEvent（可能保存设置或弹确认框）。
    for widget in app.topLevelWidgets():
        widget.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
