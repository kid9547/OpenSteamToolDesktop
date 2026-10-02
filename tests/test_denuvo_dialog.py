"""``gui.denuvo_dialog`` 无头构造测试。

只在 Qt 可用的环境下运行（CI/本地无显示时使用 offscreen 平台）。
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from core.credential_store import TicketBundle  # noqa: E402
from core.game_manager import LuaGameManager  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def manager(tmp_path):
    lua_dir = tmp_path / "config" / "lua"
    lua_dir.mkdir(parents=True)
    (lua_dir / "1361510.lua").write_text("addappid(1361510)\n", encoding="utf-8")
    return LuaGameManager(lua_dir=str(lua_dir))


def test_dialog_builds_and_reports_status(qapp, manager):
    from gui.denuvo_dialog import DenuvoTicketDialog

    dialog = DenuvoTicketDialog(manager, "1361510", game_name="Test Game")
    try:
        assert "1361510" in dialog.windowTitle()
        status = dialog._status_label.text()
        assert "Lua 票据" in status
        assert "AppTicket ✗" in status
        assert dialog._apply_btn.isEnabled() is False
    finally:
        dialog.deleteLater()


def test_dialog_accepts_pasted_tickets(qapp, manager):
    from gui.denuvo_dialog import DenuvoTicketDialog

    dialog = DenuvoTicketDialog(manager, "1361510")
    try:
        dialog._app_ticket_edit.setPlainText("aabb")
        dialog._eticket_edit.setPlainText("ccdd")
        dialog._on_use_pasted()

        assert dialog._bundle is not None
        assert dialog._bundle.app_ticket == "aabb"
        assert dialog._bundle.eticket == "ccdd"
        assert dialog._apply_btn.isEnabled() is True
    finally:
        dialog.deleteLater()


def test_dialog_rejects_empty_paste(qapp, manager):
    from gui.denuvo_dialog import DenuvoTicketDialog

    dialog = DenuvoTicketDialog(manager, "1361510")
    try:
        dialog._on_use_pasted()
        assert dialog._bundle is None
        assert dialog._apply_btn.isEnabled() is False
    finally:
        dialog.deleteLater()


def test_dialog_loads_paths_from_disk(qapp, manager, tmp_path):
    from gui.denuvo_dialog import DenuvoTicketDialog

    ticket_file = tmp_path / "tickets.txt"
    ticket_file.write_text(
        "appid:1361510\nappticket(2 bytes):aabb\neticket(2 bytes):ccdd\n",
        encoding="utf-8",
    )

    dialog = DenuvoTicketDialog(manager, "1361510")
    try:
        dialog._load_paths([str(ticket_file)])
        assert dialog._bundle is not None
        assert dialog._bundle.has_both
    finally:
        dialog.deleteLater()


def test_dialog_resolves_bridge_without_crashing(qapp, manager):
    """没有 SteamBridge 时也必须能构造（重启 Steam 只是可选步骤）。"""
    from gui.denuvo_dialog import DenuvoTicketDialog

    dialog = DenuvoTicketDialog(manager, "1361510", steam_bridge=None)
    try:
        # 允许为 None（环境无 Steam 时）或真实桥接对象
        assert dialog._steam_bridge is None or hasattr(dialog._steam_bridge, "is_steam_running")
    finally:
        dialog.deleteLater()


def test_dialog_apply_writes_lua_when_registry_unavailable(qapp, manager, monkeypatch):
    """离线环境（非 Windows）下：注册表写入失败不应阻止 Lua 写入。"""
    from core.credential_store import CredentialError
    from gui.denuvo_dialog import DenuvoTicketDialog

    def boom(bundle, include_steam_id=True):
        raise CredentialError("registry unavailable in test")

    monkeypatch.setattr("core.credential_store.write_bundle", boom)

    dialog = DenuvoTicketDialog(manager, "1361510")
    try:
        dialog._bundle = TicketBundle(app_id="1361510", app_ticket="aabb", eticket="ccdd")
        dialog._restart_check.setChecked(False)
        dialog._on_apply()

        content = (manager.get_lua_dir() and open(
            os.path.join(manager.get_lua_dir(), "1361510.lua"), encoding="utf-8"
        ).read())
        assert 'setAppTicket(1361510, "aabb")' in content
        assert 'setETicket(1361510, "ccdd")' in content
        assert "registry unavailable" in dialog._log.toPlainText()
    finally:
        dialog.deleteLater()
