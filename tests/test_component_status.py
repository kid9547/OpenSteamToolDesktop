"""``core.component_status`` 单元测试：部署 / 加载状态与安全移除。"""
from __future__ import annotations

from pathlib import Path

from core import component_status


def _make_steam(tmp_path: Path) -> Path:
    steam = tmp_path / "steam"
    steam.mkdir()
    for name in component_status.COMPONENT_DLLS:
        (steam / name).write_bytes(f"fake-{name}".encode())
    return steam


def test_collect_components_detects_loaded_core(tmp_path, monkeypatch):
    steam = _make_steam(tmp_path)
    monkeypatch.setattr(component_status, "find_steam_pids", lambda: [1234])
    monkeypatch.setattr(
        component_status,
        "_iter_modules",
        lambda pid: [
            ("OpenSteamTool.dll", str(steam / "OpenSteamTool.dll")),
            ("dwmapi.dll", r"C:\Windows\System32\dwmapi.dll"),
            ("xinput1_4.dll", str(steam / "xinput1_4.dll")),
        ],
    )

    report = component_status.collect_components(str(steam), str(steam))

    assert report.steam_running is True
    assert report.injected is True
    assert "已加载" in report.summary
    by_name = {c.name.lower(): c for c in report.components}
    assert by_name["opensteamtool.dll"].loaded_from_steam is True
    assert by_name["dwmapi.dll"].loaded is True
    assert by_name["dwmapi.dll"].loaded_from_steam is False
    assert "系统模块" in by_name["dwmapi.dll"].note
    assert by_name["xinput1_4.dll"].loaded_from_steam is True


def test_collect_components_steam_not_running(tmp_path, monkeypatch):
    steam = _make_steam(tmp_path)
    monkeypatch.setattr(component_status, "find_steam_pids", lambda: [])
    monkeypatch.setattr(component_status, "_iter_modules", lambda pid: [])

    report = component_status.collect_components(str(steam), str(steam))

    assert report.steam_running is False
    assert report.injected is False
    assert "Steam 未运行" in report.summary
    assert all("无法确认" in c.note for c in report.components)


def test_uninstall_refuses_when_steam_running(tmp_path, monkeypatch):
    steam = _make_steam(tmp_path)
    monkeypatch.setattr(component_status, "find_steam_pids", lambda: [1])

    ok, message = component_status.uninstall_components(str(steam))

    assert ok is False
    assert "正在运行" in message
    assert (steam / "OpenSteamTool.dll").is_file()


def test_uninstall_moves_files_to_backup(tmp_path, monkeypatch):
    steam = _make_steam(tmp_path)
    backup_root = tmp_path / "appdata"
    monkeypatch.setattr(component_status, "find_steam_pids", lambda: [])
    monkeypatch.setattr(component_status, "app_data_dir", lambda: backup_root)

    ok, message = component_status.uninstall_components(str(steam))

    assert ok is True
    assert "3 个组件" in message
    for name in component_status.COMPONENT_DLLS:
        assert not (steam / name).exists()
    backups = list((backup_root / "dll_backup").rglob("OpenSteamTool.dll"))
    assert backups
    assert backups[0].read_bytes() == b"fake-OpenSteamTool.dll"


def test_collect_components_without_steam_path():
    report = component_status.collect_components("", "")
    assert report.injected is False
    assert report.components == [] or all(not c.deployed for c in report.components)
