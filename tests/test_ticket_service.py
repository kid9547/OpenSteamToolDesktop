"""``core.ticket_service`` 单元测试。

覆盖票据文本解析、二进制导入、多文件合并、注册表落地与在线现签客户端契约，
全部离线运行（网络层被替换）。
"""
from __future__ import annotations

import struct

import pytest

from core import ticket_service as ts
from core.credential_store import CredentialError, TicketBundle

STEAM_ID = 76561198028121353
APP_TICKET_HEX = (struct.pack("<IIQ", 52, 2, STEAM_ID) + b"\x00" * 20).hex()


# ── 文本解析 ──────────────────────────────────────────────────
def test_parse_tickets_txt_format():
    text = f"appid:1361510\nappticket(52 bytes):{APP_TICKET_HEX}\neticket(143 bytes):aabbcc\n"
    bundle = ts.parse_ticket_text(text, source="tickets.txt")
    assert bundle.app_id == "1361510"
    assert bundle.app_ticket == APP_TICKET_HEX
    assert bundle.eticket == "aabbcc"
    assert bundle.has_both
    assert str(STEAM_ID) in bundle.derived_steam_id


def test_parse_tickets_txt_with_spaces_and_colons():
    text = "appid = 42\nAppTicket: 01 02:03-04\nETicket: aabb\n"
    bundle = ts.parse_ticket_text(text)
    assert bundle.app_id == "42"
    assert bundle.app_ticket == "01020304"
    assert bundle.eticket == "aabb"


def test_parse_lua_snippet_picks_matching_appid():
    text = (
        'addappid(1361510)\n'
        'setAppTicket(1361510, "1400AABB")\n'
        'setETicket(1361510, "1500CCDD")\n'
    )
    bundle = ts.parse_ticket_text(text, app_id="1361510")
    assert bundle.app_ticket == "1400AABB"
    assert bundle.eticket == "1500CCDD"


def test_parse_lua_snippet_falls_back_to_first_appid():
    bundle = ts.parse_ticket_text('setAppTicket(99, "aabb")\n')
    assert bundle.app_id == "99"
    assert bundle.app_ticket == "aabb"


def test_parse_json_payload_from_mint_backend():
    body = '{"eticket":"ccdd","appticket":"aabb","steam_id":"76561198028121353"}'
    bundle = ts.parse_ticket_text(body, app_id="7")
    assert bundle.app_id == "7"
    assert bundle.eticket == "ccdd"
    assert bundle.app_ticket == "aabb"
    assert bundle.steam_id == "76561198028121353"


def test_parse_json_can_carry_appid():
    bundle = ts.parse_ticket_text('{"appid": 123, "eticket": "aabb"}')
    assert bundle.app_id == "123"


def test_parse_rejects_content_without_tickets():
    with pytest.raises(CredentialError, match="未在内容中识别出"):
        ts.parse_ticket_text("hello world")


def test_parse_infers_appid_from_lua_call():
    bundle = ts.parse_ticket_text('setAppTicket(1, "aabb")', app_id="")
    assert bundle.app_id == "1"
    assert bundle.app_ticket == "aabb"


def test_extract_mint_url_from_lua():
    text = 'seteticketurl("https://example.invalid/eticket")'
    assert ts._LUA_MINT_URL.search(text).group(1) == "https://example.invalid/eticket"
    assert ts.lua_mint_url_line("http://x/y") == 'seteticketurl("http://x/y")'


# ── 二进制与多文件导入 ────────────────────────────────────────
def test_parse_binary_file_detects_kind(tmp_path):
    path = tmp_path / "appticket.bin"
    path.write_bytes(bytes.fromhex("aabb"))
    bundle = ts.parse_binary_file(path, "42", "appticket")
    assert bundle.app_ticket == "aabb"
    assert bundle.eticket == ""


def test_parse_binary_file_rejects_empty(tmp_path):
    path = tmp_path / "eticket.bin"
    path.write_bytes(b"")
    with pytest.raises(CredentialError, match="文件为空"):
        ts.parse_binary_file(path, "42", "eticket")


def test_import_directory_layout_produced_by_extractor(tmp_path):
    """上游提取器输出 <appid>/tickets.txt + appticket.bin + eticket.bin。"""
    out = tmp_path / "1361510"
    out.mkdir()
    (out / "tickets.txt").write_text(
        f"appid:1361510\nappticket(52 bytes):{APP_TICKET_HEX}\neticket(2 bytes):cafe\n",
        encoding="utf-8",
    )
    (out / "appticket.bin").write_bytes(bytes.fromhex(APP_TICKET_HEX))
    (out / "eticket.bin").write_bytes(bytes.fromhex("cafe"))

    bundle = ts.import_paths(
        [out / "tickets.txt", out / "appticket.bin", out / "eticket.bin"]
    )
    assert bundle.app_id == "1361510"
    assert bundle.app_ticket == APP_TICKET_HEX
    assert bundle.eticket == "cafe"
    assert bundle.has_both


def test_import_infers_appid_from_parent_directory(tmp_path):
    out = tmp_path / "777"
    out.mkdir()
    (out / "appticket.bin").write_bytes(b"\x01\x02")
    bundle = ts.import_paths([out / "appticket.bin"])
    assert bundle.app_id == "777"
    assert bundle.app_ticket == "0102"


def test_import_paths_raises_when_nothing_parsable(tmp_path):
    bad = tmp_path / "junk.txt"
    bad.write_text("nothing useful here", encoding="utf-8")
    with pytest.raises(CredentialError):
        ts.import_paths([bad])


def test_merge_rejects_mixed_appids():
    with pytest.raises(CredentialError, match="多个 AppID"):
        ts.merge(
            [
                TicketBundle(app_id="1", app_ticket="aabb"),
                TicketBundle(app_id="2", eticket="ccdd"),
            ]
        )


def test_merge_fills_missing_halves():
    merged = ts.merge(
        [
            TicketBundle(app_id="5", app_ticket="aabb"),
            TicketBundle(app_id="5", eticket="ccdd", source="b.txt"),
        ]
    )
    assert merged.has_both
    assert merged.source == "b.txt"


# ── 注册表落地 ────────────────────────────────────────────────
def test_apply_to_registry_delegates_to_credential_store(monkeypatch):
    captured = {}

    def fake_write_bundle(bundle, include_steam_id=True):
        captured["bundle"] = bundle
        return {"AppTicket": 2}

    monkeypatch.setattr(ts.credential_store, "write_bundle", fake_write_bundle)
    bundle = TicketBundle(app_id="11", app_ticket="aabb")
    assert ts.apply_to_registry(bundle) == {"AppTicket": 2}
    assert captured["bundle"] is bundle


def test_status_reports_ready_and_partial(monkeypatch):
    class Stored:
        app_ticket = b"\x01\x02"
        eticket = b"\x03"
        steam_id = "76561198028121353"
        missing: list[str] = []

    monkeypatch.setattr(ts.credential_store, "read", lambda app_id: Stored())
    report = ts.status("9")
    assert report["ready"] is True
    assert report["partial"] is False
    assert report["app_ticket_bytes"] == 2

    class Partial:
        app_ticket = b"\x01\x02"
        eticket = b""
        steam_id = ""
        missing = ["ETicket"]

    monkeypatch.setattr(ts.credential_store, "read", lambda app_id: Partial())
    report = ts.status("9")
    assert report["ready"] is False
    assert report["partial"] is True


def test_status_surfaces_credential_errors(monkeypatch):
    def boom(app_id):
        raise CredentialError("no winreg")

    monkeypatch.setattr(ts.credential_store, "read", boom)
    report = ts.status("9")
    assert report["available"] is False
    assert "no winreg" in str(report["error"])


# ── 提取器 ────────────────────────────────────────────────────
def test_extractor_diagnostics_translate_failures():
    assert "Steam 未运行" in ts._diagnose_extract_failure("CreateSteamPipe failed. Is Steam running?")
    assert "没有已登录的用户" in ts._diagnose_extract_failure("ConnectToGlobalUser failed")
    assert "不拥有此游戏" in ts._diagnose_extract_failure("GetAppOwnershipTicketData returned no ticket")
    assert "失败" in ts._diagnose_extract_failure("something else")


def test_extract_local_rejects_bad_appid(monkeypatch):
    monkeypatch.setattr(ts, "extractor_available", lambda: True)
    with pytest.raises(CredentialError, match="AppID 非法"):
        ts.extract_local("abc")


def test_extract_local_reports_missing_tool(monkeypatch):
    monkeypatch.setattr(ts, "extractor_available", lambda: False)
    with pytest.raises(CredentialError, match="未找到提取工具"):
        ts.extract_local("730")


# ── 在线现签客户端（参考程序契约） ────────────────────────────
def test_mint_client_disabled_by_default_never_contacts_network():
    client = ts.TicketMintClient("")
    assert client.enabled is False
    with pytest.raises(CredentialError, match="未配置票据服务地址"):
        client.mint("730")


def test_mint_client_parses_successful_response(monkeypatch):
    captured = {}

    class Response:
        status_code = 200
        text = '{"eticket":"ccdd","appticket":"aabb","steam_id":"76561198028121353"}'

    def fake_post(url, json=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        return Response()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    client = ts.TicketMintClient("https://example.invalid/eticket")
    result = client.mint("730", nonce_hex="deadbeef", existing_steam_id="76561198028121353")

    assert result.status == 200
    assert result.bundle.eticket == "ccdd"
    assert result.bundle.app_ticket == "aabb"
    assert captured["url"] == "https://example.invalid/eticket"
    assert captured["json"]["app_id"] == "730"
    assert captured["json"]["nonce"] == "deadbeef"
    assert captured["json"]["existing_steam_id"] == "76561198028121353"


def test_mint_client_omits_zero_steam_id(monkeypatch):
    captured = {}

    class Response:
        status_code = 200
        text = '{"eticket":"ccdd"}'

    import httpx

    monkeypatch.setattr(
        httpx, "post", lambda url, json=None, **kwargs: (captured.update(json) or Response())
    )
    ts.TicketMintClient("http://x").mint("5", existing_steam_id="0")
    assert "existing_steam_id" not in captured


def test_mint_client_maps_409_foreign_account(monkeypatch):
    class Response:
        status_code = 409
        text = '{"foreign_account":true}'

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: Response())
    with pytest.raises(CredentialError, match="账号池之外"):
        ts.TicketMintClient("http://x").mint("5")


def test_mint_client_maps_409_no_owner(monkeypatch):
    class Response:
        status_code = 409
        text = '{"error":"no owner"}'

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: Response())
    with pytest.raises(CredentialError, match="没有拥有该游戏的账号"):
        ts.TicketMintClient("http://x").mint("5")


def test_mint_client_maps_other_status(monkeypatch):
    class Response:
        status_code = 500
        text = "boom"

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: Response())
    with pytest.raises(CredentialError, match="HTTP 500"):
        ts.TicketMintClient("http://x").mint("5")


def test_runtime_mint_flag_is_off_for_stock_dll():
    """官方 OpenSteamTool v1.4.8 没有 seteticketurl，必须报告为不支持。"""
    assert ts.mint_supported_by_runtime() is False


def test_ready_note_explains_no_offline_forgery():
    note = ts.extractor_ready_note()
    assert "88500005" in note
    assert "无法伪造" in note


def test_find_stray_ticket_files(tmp_path):
    out = tmp_path / "1234"
    out.mkdir()
    (out / "tickets.txt").write_text("appid:1234", encoding="utf-8")
    (out / "appticket.bin").write_bytes(b"\x01")
    (out / "other.dat").write_bytes(b"\x02")
    found = {p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for p in ts.find_stray_ticket_files(tmp_path)}
    assert found == {"tickets.txt", "appticket.bin"}
