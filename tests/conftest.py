"""pytest 全局配置。

关键点：**必须**在会话级别持有一个 ``QApplication`` 的强引用。

为什么
------
``qfluentwidgets`` 在导入时创建一个 ``QConfig`` 单例，它挂在 ``QApplication`` 上
（``qconfig`` 与 ``QApplication.instance()`` 同生命周期）。如果某个测试模块用
**模块级 fixture** 构造 ``QApplication``，该模块跑完后 Python 的 GC 会回收这个
app（连同其 C++ 对象），于是后续模块再用 qfluentwidgets 控件就会炸：

    RuntimeError: wrapped C/C++ object of type QConfig has been deleted

因此这里提供**会话级**的 ``qapp`` fixture，并把实例绑到模块全局变量上，
保证整轮 pytest 期间不会被回收。同时统一设置 ``QT_QPA_PLATFORM=offscreen``，
让无显示环境（CI / 远程）也能跑 Qt 测试。
"""
from __future__ import annotations

import os

# 必须在导入 PyQt6 之前设置，否则平台插件已经初始化
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

# 会话级强引用：绝不能让 QApplication 被 GC 回收
_APP = None


def _ensure_app():
    """创建（或复用）唯一的 QApplication 实例。"""
    global _APP
    from PyQt6.QtWidgets import QApplication

    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


@pytest.fixture(scope="session")
def qapp():
    """全测试会话共用的 QApplication（自动保持强引用）。"""
    return _ensure_app()


def pytest_configure(config):  # noqa: ARG001 - pytest 钩子签名
    """收集测试前就建好 QApplication，避免各模块各自创建。"""
    try:
        _ensure_app()
    except ImportError:
        # 未安装 PyQt6 时跳过（纯逻辑测试仍可运行）
        pass
