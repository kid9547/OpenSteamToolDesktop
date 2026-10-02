"""
测试 core.manifest_downloader — Depot Manifest 下载与多源解析
"""
import io
import json
import os
import tempfile
import zipfile
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from core.manifest_downloader import (
    ManifestDownloader,
    STEAM_MANIFEST_MAGIC,
    ManifestDownloadResult,
    ManifestBatchResult,
)


@pytest.fixture
def temp_steam_dir():
    with tempfile.TemporaryDirectory() as tmp:
        steam_path = Path(tmp)
        (steam_path / "depotcache").mkdir(parents=True, exist_ok=True)
        (steam_path / "config" / "depotcache").mkdir(parents=True, exist_ok=True)
        yield str(steam_path)


class TestManifestPayloadExtraction:
    """测试清单数据载荷解压与魔数校验"""

    def test_already_standard_binary(self):
        valid_data = STEAM_MANIFEST_MAGIC + b"arbitrary_manifest_body_data_here"
        extracted = ManifestDownloader._extract_manifest_payload(valid_data)
        assert extracted == valid_data

    def test_pro_raw_deflate_format(self):
        """测试 P-ToyStore Pro 格式：10 字节前缀头 + RFC 1951 raw DEFLATE 流"""
        raw_manifest = STEAM_MANIFEST_MAGIC + b"real_manifest_payload_data_inside"
        # 生成 raw DEFLATE (wbits = -15)
        compressor = zlib.compressobj(level=9, method=zlib.DEFLATED, wbits=-15)
        deflate_stream = compressor.compress(raw_manifest) + compressor.flush()

        dummy_header = b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09"  # 10 bytes
        pro_payload = dummy_header + deflate_stream

        extracted = ManifestDownloader._extract_manifest_payload(pro_payload)
        assert extracted is not None
        assert extracted[:4] == STEAM_MANIFEST_MAGIC
        assert extracted == raw_manifest

    def test_standard_zlib_stream(self):
        """测试标准 zlib 压缩流"""
        raw_manifest = STEAM_MANIFEST_MAGIC + b"standard_zlib_payload"
        zlib_data = zlib.compress(raw_manifest)
        extracted = ManifestDownloader._extract_manifest_payload(zlib_data)
        assert extracted == raw_manifest

    def test_zip_container_format(self):
        """测试 ZIP 归档封装格式"""
        raw_manifest = STEAM_MANIFEST_MAGIC + b"zipped_steam_manifest"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("inner_file.manifest", raw_manifest)
        zip_bytes = buf.getvalue()

        extracted = ManifestDownloader._extract_manifest_payload(zip_bytes)
        assert extracted == raw_manifest

    def test_invalid_and_empty_payload(self):
        assert ManifestDownloader._extract_manifest_payload(b"") is None
        assert ManifestDownloader._extract_manifest_payload(b"short") is None
        assert ManifestDownloader._extract_manifest_payload(b"non_manifest_random_bytes_12345678") is None


class TestDualCacheAndSync:
    """测试双目录缓存及自动镜像同步"""

    def test_check_manifest_exists_none(self, temp_steam_dir):
        downloader = ManifestDownloader(temp_steam_dir)
        assert not downloader.check_manifest_exists("1001", "2001")
        downloader.close()

    def test_sync_from_depotcache_to_config(self, temp_steam_dir):
        downloader = ManifestDownloader(temp_steam_dir)
        p1 = Path(temp_steam_dir) / "depotcache" / "1001_2001.manifest"
        p2 = Path(temp_steam_dir) / "config" / "depotcache" / "1001_2001.manifest"

        p1.write_bytes(STEAM_MANIFEST_MAGIC + b"data")
        assert not p2.exists()

        assert downloader.check_manifest_exists("1001", "2001")
        # 验证自动镜像同步至 config/depotcache
        assert p2.exists()
        assert p2.read_bytes() == p1.read_bytes()
        downloader.close()

    def test_sync_from_config_to_depotcache(self, temp_steam_dir):
        downloader = ManifestDownloader(temp_steam_dir)
        p1 = Path(temp_steam_dir) / "depotcache" / "1002_2002.manifest"
        p2 = Path(temp_steam_dir) / "config" / "depotcache" / "1002_2002.manifest"

        p2.write_bytes(STEAM_MANIFEST_MAGIC + b"data2")
        assert not p1.exists()

        assert downloader.check_manifest_exists("1002", "2002")
        # 验证自动镜像同步至 depotcache
        assert p1.exists()
        assert p1.read_bytes() == p2.read_bytes()
        downloader.close()

    def test_verify_game_manifests(self, temp_steam_dir):
        downloader = ManifestDownloader(temp_steam_dir)
        depots = [
            ("101", "201", 100),
            ("102", "202", 200),
            ("103", "", 0),  # 无 gid 忽略
        ]
        all_ready, missing = downloader.verify_game_manifests(depots)
        assert not all_ready
        assert "101_201" in missing
        assert "102_202" in missing

        # 写入 101_201
        (Path(temp_steam_dir) / "depotcache" / "101_201.manifest").write_bytes(b"x")
        all_ready, missing = downloader.verify_game_manifests(depots)
        assert not all_ready
        assert missing == ["102_202"]

        # 写入 102_202
        (Path(temp_steam_dir) / "depotcache" / "102_202.manifest").write_bytes(b"y")
        all_ready, missing = downloader.verify_game_manifests(depots)
        assert all_ready
        assert missing == []
        downloader.close()

    def test_clean_old_manifests(self, temp_steam_dir):
        downloader = ManifestDownloader(temp_steam_dir)
        d1 = Path(temp_steam_dir) / "depotcache"
        d2 = Path(temp_steam_dir) / "config" / "depotcache"

        # 准备同 depot 的旧版本
        (d1 / "101_11111.manifest").write_bytes(b"old")
        (d2 / "101_11111.manifest").write_bytes(b"old")
        # 其他无关 depot
        (d1 / "999_88888.manifest").write_bytes(b"other")

        downloader._clean_old_manifests("101", "22222")

        assert not (d1 / "101_11111.manifest").exists()
        assert not (d2 / "101_11111.manifest").exists()
        assert (d1 / "999_88888.manifest").exists()
        downloader.close()


class TestManifestDownloadFlow:
    """测试 ManifestDownloader 下载与多源回退逻辑"""

    def test_download_manifests_already_exists(self, temp_steam_dir):
        downloader = ManifestDownloader(temp_steam_dir)
        (Path(temp_steam_dir) / "depotcache" / "101_201.manifest").write_bytes(b"exists")

        batch_res = downloader.download_manifests([("101", "201", 1000)], app_id="100")
        assert batch_res.total == 1
        assert batch_res.success == 1
        assert batch_res.failed == 0
        assert batch_res.results[0].message == "already exists"
        downloader.close()

    def test_download_from_github_repos(self, temp_steam_dir):
        downloader = ManifestDownloader(temp_steam_dir)

        fake_manifest = STEAM_MANIFEST_MAGIC + b"mock_downloaded_payload"

        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.content = fake_manifest

        with patch.object(downloader._http, "get", return_value=mock_resp):
            res = downloader._download_single("101", "201", 100, [], app_id="100")

        assert res.success is True
        assert "GitHub Manifest Cache" in res.message
        assert (Path(temp_steam_dir) / "depotcache" / "101_201.manifest").exists()
        assert (Path(temp_steam_dir) / "config" / "depotcache" / "101_201.manifest").exists()
        downloader.close()

    def test_download_all_sources_exhausted(self, temp_steam_dir):
        downloader = ManifestDownloader(temp_steam_dir)

        mock_fail = Mock()
        mock_fail.status_code = 404
        mock_fail.content = b"Not Found"

        with patch.object(downloader._http, "get", return_value=mock_fail), \
             patch.object(downloader._direct_http, "get", return_value=mock_fail):
            res = downloader._download_single("101", "999", 100, ["cdn.example.com"], app_id="100")

        assert res.success is False
        assert "exhausted" in res.message.lower()
        downloader.close()


class TestManifestHubDynamicSync:
    """测试 ManifestHub 动态地址解析与降级"""

    def test_fetch_manifesthub_upstream_info_success(self):
        from core.manifest_downloader import fetch_manifesthub_upstream_info
        mock_readme = (
            "# ManifestHub\nUpdate time: `2025-08-01`\n"
            "API 调用方法: `https://api.manifesthub99.custom.me/manifest?apikey=<key>`\n"
            "获取API密钥: [https://manifesthub99.custom.me](https://manifesthub99.custom.me)\n"
            "免费API密钥有效期为24小时\n"
        )
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.text = mock_readme

        with patch("httpx.Client.get", return_value=mock_resp):
            info = fetch_manifesthub_upstream_info(timeout=1.0)
            assert info["api_url"] == "https://api.manifesthub99.custom.me/manifest"
            assert "https://manifesthub99.custom.me" in info["web_url"]
            assert info["update_time"] == "2025-08-01"

    def test_fetch_manifesthub_upstream_info_fallback(self):
        from core.manifest_downloader import fetch_manifesthub_upstream_info, MANIFESTHUB_API_URL
        with patch("httpx.Client.get", side_effect=Exception("Connection refused")):
            info = fetch_manifesthub_upstream_info(timeout=0.1)
            assert info["api_url"] == MANIFESTHUB_API_URL


def _cdn_zip_payload(raw_manifest: bytes) -> bytes:
    """构造 Valve CDN 返回的 ZIP（内含名为 'z' 的条目）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("z", raw_manifest)
    return buf.getvalue()


class _UrlRecordingClient:
    """记录请求 URL 并按键值返回响应的假 httpx 客户端。"""

    def __init__(self, responder):
        self._responder = responder
        self.calls: list[str] = []

    def get(self, url: str, timeout: float | None = None):  # noqa: ARG002
        self.calls.append(url)
        status, body = self._responder(url)
        resp = Mock()
        resp.status_code = status
        resp.content = body
        resp.text = body.decode("utf-8", "replace")
        resp.json = lambda: json.loads(resp.text)
        return resp

    def close(self):
        pass


class TestBranchArchiveSingleFileSource:
    """测试社区分支归档单文件来源与镜像回退顺序（离线）"""

    def test_branch_paths_and_mirror_fallback_order(self, temp_steam_dir):
        downloader = ManifestDownloader(temp_steam_dir)
        manifest = STEAM_MANIFEST_MAGIC + b"branch-archive-payload"

        proxied = _UrlRecordingClient(
            lambda url: (404, b"Not Found") if "raw.githubusercontent.com" in url else (404, b"Not Found")
        )
        mirror = _UrlRecordingClient(
            lambda url: (200, manifest) if "ghproxy.net" in url else (404, b"Not Found")
        )
        downloader._http = proxied
        downloader._direct_http = mirror

        data = downloader._download_from_github_repos("731", "7127896784363312296", app_id="730")

        assert data == manifest
        # 路径必须是分支布局 <repo>/<appid>/{depot}_{gid}.manifest
        assert any(
            "/Auiowu/ManifestAutoUpdate/730/731_7127896784363312296.manifest" in url
            for url in proxied.calls + mirror.calls
        )
        # 镜像按 ghfast → ghproxy 顺序回退
        fast_idx = next(i for i, u in enumerate(mirror.calls) if "ghfast.top" in u)
        proxy_idx = next(i for i, u in enumerate(mirror.calls) if "ghproxy.net" in u)
        assert fast_idx < proxy_idx
        # 不再请求已失效的 P-ToyStore 仓库
        assert not any("SteamManifestCache_Pro" in u for u in proxied.calls + mirror.calls)
        downloader.close()

    def test_owner_and_main_branch_candidates(self, temp_steam_dir):
        downloader = ManifestDownloader(temp_steam_dir)
        manifest = STEAM_MANIFEST_MAGIC + b"dlc-branch-payload"
        proxied = _UrlRecordingClient(lambda url: (200, manifest))
        downloader._http = proxied
        downloader._direct_http = _UrlRecordingClient(lambda url: (404, b"Not Found"))

        data = downloader._download_from_github_repos("9001", "42", app_id="100", owner_app_id="200")

        assert data == manifest
        # DLC 拥有者分支优先于主 AppID 分支
        assert "/200/9001_42.manifest" in proxied.calls[0]
        downloader.close()


class TestRequestCodeCdnSource:
    """测试 Manifest Request Code → Valve CDN 路径（离线）"""

    def test_fetch_request_code_plain_text(self, temp_steam_dir):
        downloader = ManifestDownloader(temp_steam_dir)
        downloader._direct_http = _UrlRecordingClient(lambda url: (200, b"12005229650648827506"))
        assert downloader.fetch_request_code("7138855853134977810") == "12005229650648827506"
        # 命中缓存，不重复请求
        calls = len(downloader._direct_http.calls)
        assert downloader.fetch_request_code("7138855853134977810") == "12005229650648827506"
        assert len(downloader._direct_http.calls) == calls
        downloader.close()

    def test_fetch_request_code_json_form(self, temp_steam_dir):
        downloader = ManifestDownloader(temp_steam_dir)
        responses = {"gmrc": (500, b"oops"), "steam.run": (200, b'{"content":"18049399937474343719"}')}

        def responder(url):
            for key, value in responses.items():
                if key in url:
                    return value
            return (404, b"Not Found")

        downloader._direct_http = _UrlRecordingClient(responder)
        downloader._http = downloader._direct_http
        assert downloader.fetch_request_code("999") == "18049399937474343719"
        downloader.close()

    def test_fetch_request_code_all_sources_fail(self, temp_steam_dir):
        downloader = ManifestDownloader(temp_steam_dir)
        downloader._direct_http = _UrlRecordingClient(lambda url: (404, b"Not Found"))
        downloader._http = downloader._direct_http
        assert downloader.fetch_request_code("999") is None
        assert downloader.fetch_request_code("") is None
        downloader.close()

    def test_download_via_request_code_extracts_manifest(self, temp_steam_dir):
        downloader = ManifestDownloader(temp_steam_dir)
        raw_manifest = STEAM_MANIFEST_MAGIC + b"official-cdn-manifest" * 3
        cdn_zip = _cdn_zip_payload(raw_manifest)

        def responder(url):
            if "gmrc.wudrm.com" in url:
                return (200, b"12005229650648827506")
            if "/depot/2347770/manifest/7138855853134977810/5/12005229650648827506" in url:
                return (200, cdn_zip)
            return (404, b"Not Found")

        downloader._direct_http = _UrlRecordingClient(responder)
        downloader._http = downloader._direct_http

        data = downloader.download_via_request_code(
            "2347770", "7138855853134977810", cdn_hosts=["steampipe.akamaized.net"]
        )

        assert data == raw_manifest
        assert data[:4] == STEAM_MANIFEST_MAGIC
        downloader.close()

    def test_download_single_falls_back_to_request_code(self, temp_steam_dir):
        downloader = ManifestDownloader(temp_steam_dir)
        raw_manifest = STEAM_MANIFEST_MAGIC + b"fallback-manifest" * 3

        with patch.object(downloader, "_download_from_github_repos", return_value=None), \
             patch.object(downloader, "_download_from_manifesthub", return_value=None), \
             patch.object(
                 downloader,
                 "download_via_request_code",
                 return_value=raw_manifest,
             ) as code_mock:
            res = downloader._download_single("2347770", "7138855853134977810", 0, [], app_id="730")

        assert code_mock.called
        assert res.success is True
        assert "request code" in res.message
        assert (Path(temp_steam_dir) / "depotcache" / "2347770_7138855853134977810.manifest").read_bytes() == raw_manifest
        assert (Path(temp_steam_dir) / "config" / "depotcache" / "2347770_7138855853134977810.manifest").exists()
        downloader.close()

