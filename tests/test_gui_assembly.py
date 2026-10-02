"""GUI 装配冒烟测试：真实构造 MainWindow 并逐个切换导航页。

这是唯一能证明「新增的清单管理页 + D加密向导入口」真正被装配进界面的测试
（此前只验证过各页面单独构造）。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    # 会话级 conftest 已经建好，这里仅取用
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """把应用数据目录重定向到临时目录，避免污染真实配置。"""
    from config import APP_NAME

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    import utils.path_manager as pm

    pm.PathManager._base = None
    return fake_home


def test_main_window_assembles_with_manifest_page(qapp, workspace):
    from core.config_manager import ConfigManager
    from core.game_manager import LuaGameManager
    from core.steam_bridge import SteamBridge
    from gui.main_window import MainWindow

    config = ConfigManager()
    bridge = SteamBridge()
    manager = LuaGameManager(config_manager=config)

    window = MainWindow(bridge, manager, config)
    try:
        # 新增页面必须存在且已加入导航
        assert hasattr(window, "manifest_page")
        assert hasattr(window, "library_page")
        assert window.manifest_page.objectName() == "manifestPage"

        # 导航按钮都在
        assert window._manifest_nav_btn is not None
        assert window._library_nav_btn is not None
    finally:
        window.close()
        window.deleteLater()


def test_navigation_can_switch_to_every_page(qapp, workspace):
    from core.config_manager import ConfigManager
    from core.game_manager import LuaGameManager
    from core.steam_bridge import SteamBridge
    from gui.main_window import MainWindow

    window = MainWindow(SteamBridge(), LuaGameManager(config_manager=ConfigManager()), ConfigManager())
    try:
        pages = [
            window.home_page,
            window.inject_page,
            window.search_page,
            window.library_page,
            window.manifest_page,
            window.settings_page,
        ]
        for page in pages:
            window.switchTo(page)
            assert window.stackedWidget.currentWidget() is page
    finally:
        window.close()
        window.deleteLater()


def test_game_card_exposes_denuvo_action(qapp, workspace):
    """游戏卡片的更多菜单必须包含 D 加密授权入口，且信号能连上页面处理函数。"""
    from gui.widgets.game_card import GameCard

    card = GameCard(app_id="1361510", game_name="Test", has_lua=True)
    try:
        assert hasattr(card, "denuvo_ticket_requested")
        received: list[str] = []
        card.denuvo_ticket_requested.connect(received.append)
        card._on_denuvo_ticket()
        assert received == ["1361510"]
    finally:
        card.cleanup()
        card.deleteLater()


def test_manifest_page_wired_to_library_refresh(qapp, workspace, monkeypatch):
    """清单变动信号必须连到游戏库刷新，否则补全后界面状态不会更新。

    注意：Qt 在 ``connect`` 时就会捕获绑定方法，所以必须在构造 MainWindow
    **之前**替换类方法，否则替换的是另一个函数对象、不会生效。
    """
    from core.config_manager import ConfigManager
    from core.game_manager import LuaGameManager
    from core.steam_bridge import SteamBridge
    from gui.library_page import LibraryPage
    from gui.main_window import MainWindow

    calls: list[int] = []
    monkeypatch.setattr(
        LibraryPage, "_load_games_async", lambda self: calls.append(1), raising=True
    )

    window = MainWindow(
        SteamBridge(), LuaGameManager(config_manager=ConfigManager()), ConfigManager()
    )
    try:
        # 信号必须有接收者（防止哪天误删 connect）
        assert window.manifest_page.receivers(window.manifest_page.manifests_changed) >= 1

        window.manifest_page.manifests_changed.emit()
        assert calls == [1]
    finally:
        window.close()
        window.deleteLater()
