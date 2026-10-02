"""``core.credential_store`` 单元测试。

注册表读写通过伪造的 ``winreg`` 对象验证，不触碰真实系统注册表，
因此测试可在任意平台上运行。
"""
from __future__ import annotations

import sys
import types

import pytest

from core import credential_store as cs


# ── 纯函数 ────────────────────────────────────────────────────
def test_hex_roundtrip_ignores_separators():
    assert cs.hex_to_bytes("01 02:0a-0b") == b"\x01\x02\x0a\x0b"
    assert cs.bytes_to_hex(b"\x01\x02") == "0102"
    assert cs.normalize_hex("AA:BB") == "aabb"


@pytest.mark.parametrize("value", ["", "0", "zz", "abc"])
def test_hex_to_bytes_rejects_invalid(value):
    with pytest.raises(cs.CredentialError):
        cs.hex_to_bytes(value)


def test_steam_id_from_ticket_reads_second_qword():
    """票据布局 [uint32 Size][uint32 Version][uint64 SteamID]..."""
    import struct

    steam_id = 76561198028121353
    ticket = struct.pack("<IIQ", 52, 2, steam_id) + b"\x00" * 20
    assert cs.steam_id_from_ticket(ticket) == steam_id
    assert cs.steam_id_from_ticket(b"\x00" * 8) == 0


def test_normalize_steam_id_upgrades_account_id_dword():
    """Steam 自己写 DWORD 账号 ID，必须还原成 SteamID64。"""
    assert cs.normalize_steam_id(1402342805) == "76561199362608533"
    assert cs.normalize_steam_id("1402342805") == "76561199362608533"
    assert cs.normalize_steam_id("76561198028121353") == "76561198028121353"
    assert cs.normalize_steam_id("") == ""
    assert cs.normalize_steam_id("not-a-number") == ""


def test_bundle_validate_flags_bad_appid_and_steamid():
    bundle = cs.TicketBundle(app_id="abc", app_ticket="0102", steam_id="12")
    problems = bundle.validate()
    assert any("AppID" in item for item in problems)
    assert any("SteamID" in item for item in problems)


def test_bundle_lua_lines_use_setappticket_and_seteticket():
    bundle = cs.TicketBundle(app_id="1361510", app_ticket="AABB", eticket="CCDD")
    assert bundle.lua_lines() == [
        'setAppTicket(1361510, "aabb")',
        'setETicket(1361510, "ccdd")',
    ]


def test_bundle_derived_steam_id_prefers_explicit_value():
    bundle = cs.TicketBundle(app_id="1", app_ticket="0102", steam_id="76561198028121353")
    assert bundle.derived_steam_id == "76561198028121353"


def test_lua_lines_skip_missing_tickets():
    assert cs.TicketBundle(app_id="1", eticket="aabb").lua_lines() == ['setETicket(1, "aabb")']


# ── 注册表读写（伪造 winreg） ──────────────────────────────────
class _FakeKey:
    """注册表键句柄替身（支持 with 语句）。"""

    def __init__(self, path: str):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeWinreg:
    """最小可用的 winreg 替身，只实现 credential_store 用到的那部分 API。"""

    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"
    KEY_READ = 0x20019
    KEY_SET_VALUE = 0x0002
    KEY_WOW64_32KEY = 0x0200
    REG_BINARY = 3
    REG_SZ = 1
    REG_DWORD = 4
    REG_OPTION_NON_VOLATILE = 0

    def __init__(self):
        self.values: dict[tuple[str, str], tuple[object, int]] = {}
        self.deleted: list[tuple[str, str]] = []
        self.created: set[str] = set()

    def OpenKey(self, hive, path, reserved=0, access=0):
        if path not in self.created:
            raise FileNotFoundError(path)
        return _FakeKey(path)

    def CreateKeyEx(self, hive, path, reserved=0, access=0, options=0):
        self.created.add(path)
        return _FakeKey(path)

    def QueryValueEx(self, key, name):
        try:
            return self.values[(key.path, name)]
        except KeyError:
            raise FileNotFoundError(name) from None

    def SetValueEx(self, key, name, reserved, reg_type, data):
        self.values[(key.path, name)] = (data, reg_type)

    def DeleteValue(self, key, name):
        if (key.path, name) not in self.values:
            raise FileNotFoundError(name)
        del self.values[(key.path, name)]
        self.deleted.append((key.path, name))

    def to_module(self) -> types.ModuleType:
        module = types.ModuleType("winreg")
        for attr in (
            "HKEY_CURRENT_USER",
            "HKEY_LOCAL_MACHINE",
            "KEY_READ",
            "KEY_SET_VALUE",
            "KEY_WOW64_32KEY",
            "REG_BINARY",
            "REG_SZ",
            "REG_DWORD",
            "REG_OPTION_NON_VOLATILE",
        ):
            setattr(module, attr, getattr(self, attr))
        module.OpenKey = self.OpenKey
        module.CreateKeyEx = self.CreateKeyEx
        module.QueryValueEx = self.QueryValueEx
        module.SetValueEx = self.SetValueEx
        module.DeleteValue = self.DeleteValue
        return module


@pytest.fixture()
def fake_registry(monkeypatch):
    fake = _FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake.to_module())
    monkeypatch.setattr(cs.sys, "platform", "win32")
    return fake


def test_write_bundle_then_read_back(fake_registry):
    bundle = cs.TicketBundle(app_id="1361510", app_ticket="1400", eticket="1500")
    written = cs.write_bundle(bundle)

    assert written[cs.VALUE_APP_TICKET] == 2
    assert written[cs.VALUE_ETICKET] == 2

    stored = cs.read("1361510")
    assert stored.app_ticket == b"\x14\x00"
    assert stored.eticket == b"\x15\x00"
    assert stored.present is True


def test_read_missing_key_reports_missing_values(fake_registry):
    stored = cs.read("999")
    assert stored.present is False
    assert set(stored.missing) == {cs.VALUE_APP_TICKET, cs.VALUE_ETICKET, cs.VALUE_STEAM_ID}


def test_write_steam_id_uses_decimal_string_and_derives_from_ticket(fake_registry):
    import struct

    steam_id = 76561198028121353
    ticket = struct.pack("<IIQ", 52, 2, steam_id) + b"\x00" * 20
    bundle = cs.TicketBundle(app_id="42", app_ticket=ticket.hex())
    written = cs.write_bundle(bundle)

    assert written[cs.VALUE_STEAM_ID] == len(str(steam_id))
    stored = cs.read("42")
    assert stored.steam_id == str(steam_id)


def test_write_bundle_rejects_invalid_ticket(fake_registry):
    with pytest.raises(cs.CredentialError):
        cs.write_bundle(cs.TicketBundle(app_id="1", app_ticket="zz"))


def test_write_bundle_refuses_empty_ticket(fake_registry):
    with pytest.raises(cs.CredentialError):
        cs.write_app_ticket("1", "")


def test_delete_only_removes_existing_values(fake_registry):
    cs.write_app_ticket("7", "0102")
    removed = cs.delete("7")
    assert removed == [cs.VALUE_APP_TICKET]
    assert cs.delete("7") == []


def test_steam_install_path_reads_steampath(fake_registry):
    fake_registry.values[(cs.STEAM_KEY, "SteamPath")] = ("d:/program files (x86)/steam", 1)
    fake_registry.created.add(cs.STEAM_KEY)
    assert cs.steam_install_path() == "d:\\program files (x86)\\steam"


def test_active_user_returns_account_and_universe(fake_registry):
    key = rf"{cs.STEAM_KEY}\ActiveProcess"
    fake_registry.values[(key, "ActiveUser")] = (1402342805, 4)
    fake_registry.values[(key, "Universe")] = ("Public\x00", 1)
    fake_registry.created.add(key)
    assert cs.active_user() == (1402342805, "Public")


def test_active_user_zero_when_process_key_missing(fake_registry):
    assert cs.active_user() == (0, "")


def test_active_steam_id_upgrades_account_id(fake_registry):
    key = rf"{cs.STEAM_KEY}\ActiveProcess"
    fake_registry.values[(key, "ActiveUser")] = (1928292277, 4)
    fake_registry.values[(key, "Universe")] = ("Public\x00", 1)
    fake_registry.created.add(key)
    assert cs.active_steam_id() == str(cs.account_id_to_steam_id64(1928292277))


def test_check_account_match_detects_mismatch(fake_registry):
    key = rf"{cs.STEAM_KEY}\ActiveProcess"
    fake_registry.values[(key, "ActiveUser")] = (1000, 4)
    fake_registry.values[(key, "Universe")] = ("Public\x00", 1)
    fake_registry.created.add(key)

    same = cs.TicketBundle(app_id="1", steam_id=cs.active_steam_id())
    assert cs.check_account_match(same) == ""

    other = cs.TicketBundle(app_id="1", steam_id="76561198028121353")
    verdict = cs.check_account_match(other)
    assert verdict.startswith("mismatch:")
    assert "012" in verdict


def test_check_account_match_is_silent_when_not_logged_in(fake_registry):
    bundle = cs.TicketBundle(app_id="1", steam_id="76561198028121353")
    assert cs.check_account_match(bundle) == ""


def test_check_account_match_ignores_bundle_without_steamid(fake_registry):
    key = rf"{cs.STEAM_KEY}\ActiveProcess"
    fake_registry.values[(key, "ActiveUser")] = (1000, 4)
    fake_registry.values[(key, "Universe")] = ("Public\x00", 1)
    fake_registry.created.add(key)
    # AppTicket 太短 → 解析不出 SteamID → 不做判断（避免误报）
    assert cs.check_account_match(cs.TicketBundle(app_id="1", app_ticket="0102")) == ""
