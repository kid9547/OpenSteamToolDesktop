"""``core.webdav_client`` 单元测试（httpx 全部 mock，不联网）。"""
from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from core.webdav_client import RemoteFile, WebDavClient, WebDavError, default_device_name


def _client() -> WebDavClient:
    return WebDavClient("https://dav.example.com/dav/", "user", "pass")


def test_base_url_normalization():
    client = WebDavClient("https://dav.example.com/dav")
    assert client.base_url == "https://dav.example.com/dav/"


def test_base_url_requires_scheme():
    with pytest.raises(WebDavError):
        WebDavClient("ftp://example.com")


def test_url_encoding_preserves_slashes():
    client = _client()
    assert client._url("OpenSteamToolDesktop/saves/My PC/a b.zip") == (
        "https://dav.example.com/dav/OpenSteamToolDesktop/saves/My%20PC/a%20b.zip"
    )


def test_default_device_name_sanitized():
    name = default_device_name()
    assert name and all(c.isalnum() or c in "._-" for c in name)


def test_test_connection_success():
    client = _client()
    resp = Mock(status_code=207)
    with patch.object(client._client, "request", return_value=resp) as mock_req:
        message = client.test_connection()
    assert "连接成功" in message
    assert mock_req.call_args.args[0] == "PROPFIND"


def test_test_connection_auth_failure():
    client = _client()
    resp = Mock(status_code=401)
    with patch.object(client._client, "request", return_value=resp):
        with pytest.raises(WebDavError) as exc:
            client.test_connection()
    assert "401" in str(exc.value)


def test_upload_creates_parent_and_puts(tmp_path):
    client = _client()
    local = tmp_path / "steam_saves.zip"
    local.write_bytes(b"zip-data")

    responses = {"MKCOL": Mock(status_code=405), "PUT": Mock(status_code=201)}

    def fake_request(method, url, **kwargs):
        return responses[method]

    with patch.object(client._client, "request", side_effect=fake_request) as mock_req:
        url = client.upload(local, "OpenSteamToolDesktop/saves/PC/steam_saves.zip")

    assert url.endswith("steam_saves.zip")
    methods = [call.args[0] for call in mock_req.call_args_list]
    assert "MKCOL" in methods and "PUT" in methods
    put_call = next(call for call in mock_req.call_args_list if call.args[0] == "PUT")
    assert put_call.kwargs["content"] == b"zip-data"


def test_list_dir_parses_propfind():
    client = _client()
    xml = """<?xml version="1.0"?>
    <D:multistatus xmlns:D="DAV:">
      <D:response>
        <D:href>/dav/OpenSteamToolDesktop/saves/</D:href>
        <D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop></D:propstat>
      </D:response>
      <D:response>
        <D:href>/dav/OpenSteamToolDesktop/saves/PC/steam_saves_20261004.zip</D:href>
        <D:propstat><D:prop><D:getcontentlength>12345</D:getcontentlength>
        <D:resourcetype/></D:prop></D:propstat>
      </D:response>
    </D:multistatus>"""
    resp = Mock(status_code=207, text=xml)
    with patch.object(client._client, "request", return_value=resp):
        files = client.list_dir("OpenSteamToolDesktop/saves")

    # 自身目录条目被跳过，只剩 zip
    assert len(files) == 1
    entry = files[0]
    assert isinstance(entry, RemoteFile)
    assert entry.name == "steam_saves_20261004.zip"
    assert entry.size == 12345
    assert not entry.is_dir


def test_list_dir_404_returns_empty():
    client = _client()
    resp = Mock(status_code=404)
    with patch.object(client._client, "request", return_value=resp):
        assert client.list_dir("missing/dir") == []


def test_download_writes_local_file(tmp_path):
    client = _client()
    resp = Mock(status_code=200, content=b"downloaded-zip")
    target = tmp_path / "out" / "save.zip"
    with patch.object(client._client, "request", return_value=resp):
        result = client.download("OpenSteamToolDesktop/saves/PC/save.zip", target)
    assert result == target
    assert target.read_bytes() == b"downloaded-zip"


def test_download_404_raises():
    client = _client()
    resp = Mock(status_code=404)
    with patch.object(client._client, "request", return_value=resp):
        with pytest.raises(WebDavError):
            client.download("missing.zip", "out.zip")
