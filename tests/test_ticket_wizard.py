"""``gui.ticket_wizard`` 无头构造测试。

``qapp`` fixture 由 ``tests/conftest.py`` 提供（会话级，避免 QConfig 单例被回收）。
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")

from core.credential_store import TicketBundle  # noqa: E402
from core.game_manager import LuaGameManager  # noqa: E402


class _StubBridge:
    def __init__(self, running: bool = False):
        self._running = running

    def is_steam_running(self) -> bool:
        return self._running


@pytest.fixture()
def manager(tmp_path):
    lua_dir = tmp_path / "config" / "lua"
    lua_dir.mkdir(parents=True)
    (lua_dir / "1361510.lua").write_text("addappid(1361510)\n", encoding="utf-8")
    return LuaGameManager(lua_dir=str(lua_dir))


def test_wizard_builds_and_shows_steps(qapp, manager):
    from gui.ticket_wizard import TicketWizardDialog

    dialog = TicketWizardDialog(manager, "1361510", game_name="Test Game")
    try:
        assert "1361510" in dialog.windowTitle() or "Test Game" in dialog.windowTitle()
        assert len(dialog._steps) == 5
        assert dialog._apply_btn.isEnabled() is False
        # 状态区必须给出可读信息而不是空白
        assert dialog._status.text().strip()
    finally:
        dialog.deleteLater()


def test_wizard_reports_steam_not_running(qapp, manager):
    from gui.ticket_wizard import TicketWizardDialog

    dialog = TicketWizardDialog(manager, "1361510", steam_bridge=_StubBridge(False))
    try:
        assert "Steam 未运行" in dialog._status.text()
        # 第①步未完成 → 圆点应仍是空心
        assert dialog._steps[0][0].text() == "○"
    finally:
        dialog.deleteLater()


def test_wizard_marks_step1_done_when_logged_in(qapp, manager, monkeypatch):
    from gui import ticket_wizard
    from gui.ticket_wizard import TicketWizardDialog

    monkeypatch.setattr(
        ticket_wizard.credential_store, "active_user", lambda: (1928292277, "Public")
    )
    monkeypatch.setattr(
        ticket_wizard.credential_store, "active_steam_id", lambda: "76561199888558005"
    )

    dialog = TicketWizardDialog(manager, "1361510", steam_bridge=_StubBridge(True))
    try:
        text = dialog._status.text()
        assert "已登录账号" in text
        assert "76561199888558005" in text
        assert dialog._steps[0][0].text() == "●"
    finally:
        dialog.deleteLater()


def test_wizard_apply_writes_lua_and_vault(qapp, manager, monkeypatch, tmp_path):
    from gui.ticket_wizard import TicketWizardDialog

    stored: dict = {}
    monkeypatch.setattr(
        "core.ticket_vault.store_bundle", lambda bundle: stored.update(app_id=bundle.app_id)
    )

    def boom(bundle, include_steam_id=True):
        raise RuntimeError("registry unavailable in test")

    monkeypatch.setattr("core.credential_store.write_bundle", boom)

    dialog = TicketWizardDialog(manager, "1361510", steam_bridge=_StubBridge(False))
    try:
        dialog._bundle = TicketBundle(app_id="1361510", app_ticket="aabb", eticket="ccdd")
        dialog._on_apply()

        content = open(
            os.path.join(manager.get_lua_dir(), "1361510.lua"), encoding="utf-8"
        ).read()
        assert 'setAppTicket(1361510, "aabb")' in content
        assert 'setETicket(1361510, "ccdd")' in content
        assert stored.get("app_id") == "1361510"
        assert "已完成" in dialog._status.text()
    finally:
        dialog.deleteLater()


def test_wizard_apply_blocked_on_invalid_ticket(qapp, manager):
    from gui.ticket_wizard import TicketWizardDialog

    dialog = TicketWizardDialog(manager, "1361510", steam_bridge=_StubBridge(False))
    try:
        dialog._bundle = TicketBundle(app_id="not-an-id", app_ticket="aabb")
        dialog._on_apply()
        assert "校验未通过" in dialog._status.text()
        # Lua 不应被改动
        content = open(
            os.path.join(manager.get_lua_dir(), "1361510.lua"), encoding="utf-8"
        ).read()
        assert "setAppTicket" not in content
    finally:
        dialog.deleteLater()


def test_wizard_apply_without_bundle_is_noop(qapp, manager):
    from gui.ticket_wizard import TicketWizardDialog

    dialog = TicketWizardDialog(manager, "1361510", steam_bridge=_StubBridge(False))
    try:
        before = dialog._status.text()
        dialog._on_apply()
        assert dialog._status.text() == before
    finally:
        dialog.deleteLater()
