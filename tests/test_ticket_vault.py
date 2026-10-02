"""``core.ticket_vault`` 测试。

覆盖票据库持久化、mint 契约的 HTTP 行为（真实起服务、真实发请求）、
以及账号不一致时的 409 foreign_account 拒绝逻辑。
"""
from __future__ import annotations

import json
import socket

import httpx
import pytest

from core.credential_store import TicketBundle
from core.ticket_vault import LocalTicketServer, TicketVault

STEAM_ID = "76561198028121353"
OTHER_ID = "76561199888558005"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def vault(tmp_path) -> TicketVault:
    return TicketVault(path=tmp_path / "vault.json")


@pytest.fixture()
def server(vault):
    srv = LocalTicketServer(vault=vault, port=_free_port())
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


# ── 票据库 ────────────────────────────────────────────────────
def test_vault_put_get_roundtrip(vault):
    vault.put(TicketBundle(app_id="730", app_ticket="aabb", eticket="ccdd", steam_id=STEAM_ID))
    got = vault.get("730")
    assert got is not None
    assert got.app_ticket == "aabb"
    assert got.eticket == "ccdd"
    assert got.steam_id == STEAM_ID


def test_vault_persists_to_disk_and_reloads(tmp_path):
    path = tmp_path / "vault.json"
    TicketVault(path=path).put(TicketBundle(app_id="42", app_ticket="0a0b"))

    reloaded = TicketVault(path=path)
    assert reloaded.app_ids() == ["42"]
    assert reloaded.get("42").app_ticket == "0a0b"


def test_vault_merge_does_not_blank_existing_fields(vault):
    vault.put(TicketBundle(app_id="7", app_ticket="aabb", steam_id=STEAM_ID))
    vault.put(TicketBundle(app_id="7", eticket="ccdd"))  # 只带 eticket
    got = vault.get("7")
    assert got.app_ticket == "aabb"
    assert got.eticket == "ccdd"
    assert got.steam_id == STEAM_ID


def test_vault_ignores_empty_bundle(vault):
    vault.put(TicketBundle(app_id="9"))
    assert vault.get("9") is None
    assert len(vault) == 0


def test_vault_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "vault.json"
    path.write_text("{ not json", encoding="utf-8")
    assert len(TicketVault(path=path)) == 0


def test_vault_normalizes_hex_on_put(vault):
    vault.put(TicketBundle(app_id="5", app_ticket="AA:BB"))
    assert vault.get("5").app_ticket == "aabb"


def test_vault_skips_non_dict_entries(tmp_path):
    path = tmp_path / "vault.json"
    path.write_text(json.dumps({"1": {"appticket": "aa"}, "2": "junk"}), encoding="utf-8")
    vault = TicketVault(path=path)
    assert vault.app_ids() == ["1"]


# ── HTTP 契约 ─────────────────────────────────────────────────
def test_health_endpoint(server):
    resp = httpx.get(server.url + "health", timeout=5)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_apps_endpoint_does_not_leak_tickets(server, vault):
    vault.put(TicketBundle(app_id="730", app_ticket="aabb"))
    resp = httpx.get(server.url + "apps", timeout=5)
    assert resp.json() == {"apps": ["730"]}
    assert "aabb" not in resp.text


def test_unknown_path_returns_404(server):
    assert httpx.get(server.url + "nope", timeout=5).status_code == 404


def test_mint_contract_success(server, vault):
    vault.put(TicketBundle(app_id="1361510", app_ticket="aabb", eticket="ccdd", steam_id=STEAM_ID))

    resp = httpx.post(
        server.url,
        json={"app_id": "1361510", "nonce": "deadbeef", "existing_steam_id": STEAM_ID},
        timeout=5,
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["appticket"] == "aabb"
    assert payload["eticket"] == "ccdd"
    assert payload["steam_id"] == STEAM_ID


def test_mint_returns_409_when_no_owner(server):
    resp = httpx.post(server.url, json={"app_id": "999"}, timeout=5)
    assert resp.status_code == 409
    assert resp.json()["error"] == "no owner"


def test_mint_returns_409_foreign_account(server, vault):
    vault.put(TicketBundle(app_id="7", app_ticket="aabb", steam_id=STEAM_ID))
    resp = httpx.post(
        server.url, json={"app_id": "7", "existing_steam_id": OTHER_ID}, timeout=5
    )
    assert resp.status_code == 409
    assert resp.json()["foreign_account"] is True


def test_mint_allows_matching_or_absent_steam_id(server, vault):
    vault.put(TicketBundle(app_id="7", app_ticket="aabb", steam_id=STEAM_ID))
    assert httpx.post(server.url, json={"app_id": "7", "existing_steam_id": STEAM_ID}, timeout=5).status_code == 200
    assert httpx.post(server.url, json={"app_id": "7"}, timeout=5).status_code == 200
    assert httpx.post(server.url, json={"app_id": "7", "existing_steam_id": "0"}, timeout=5).status_code == 200


def test_mint_rejects_bad_requests(server):
    assert httpx.post(server.url, json={"app_id": "abc"}, timeout=5).status_code == 400
    assert httpx.post(server.url, content=b"not json", timeout=5).status_code == 400


def test_server_url_and_running_flags(vault):
    srv = LocalTicketServer(vault=vault, port=_free_port())
    assert srv.running is False
    url = srv.start()
    try:
        assert srv.running is True
        assert url == srv.url
        # 重复 start 应幂等
        assert srv.start() == url
    finally:
        srv.stop()
    assert srv.running is False


def test_ticket_service_client_talks_to_local_vault(server, vault):
    """端到端：本项目的 TicketMintClient ↔ 本地票据服务。"""
    from core.ticket_service import TicketMintClient

    vault.put(TicketBundle(app_id="1623730", app_ticket="aabbcc", eticket="ddeeff", steam_id=STEAM_ID))
    client = TicketMintClient(server.url)
    result = client.mint("1623730", nonce_hex="00", existing_steam_id=STEAM_ID)

    assert result.status == 200
    assert result.bundle.app_ticket == "aabbcc"
    assert result.bundle.eticket == "ddeeff"


def test_ticket_service_client_reports_no_owner(server):
    from core.credential_store import CredentialError
    from core.ticket_service import TicketMintClient

    with pytest.raises(CredentialError, match="没有拥有该游戏的账号"):
        TicketMintClient(server.url).mint("404404")
