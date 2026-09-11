"""
测试 core.manifest_resolver — 清单解析与自动化获取服务
"""
import io
import os
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from core.manifest_resolver import ManifestResolver, ManifestReadiness
from core.manifest_downloader import STEAM_MANIFEST_MAGIC, ManifestDownloadResult, ManifestBatchResult


@pytest.fixture
def temp_steam_dir():
    with tempfile.TemporaryDirectory() as tmp:
        steam_path = Path(tmp)
        (steam_path / "depotcache").mkdir(parents=True, exist_ok=True)
        (steam_path / "config" / "depotcache").mkdir(parents=True, exist_ok=True)
        (steam_path / "config" / "lua").mkdir(parents=True, exist_ok=True)
        yield str(steam_path)


class TestManifestResolver:
    def test_check_manifest_exists(self, temp_steam_dir):
        resolver = ManifestResolver(temp_steam_dir)
        depot_file = Path(temp_steam_dir) / "depotcache" / "123_456.manifest"
        assert not resolver.check_manifest_exists("123", "456")

        depot_file.write_bytes(b"manifest_binary_data")
        assert resolver.check_manifest_exists("123", "456")
        resolver.close()

    def test_diagnose_lua_file(self, temp_steam_dir):
        resolver = ManifestResolver(temp_steam_dir)
        lua_path = Path(temp_steam_dir) / "config" / "lua" / "100.lua"
        lua_path.write_text(
            'addappid(100)\n'
            'setManifestid(101, "201", 12345)\n'
            'setManifestid(102, "202")\n',
            encoding="utf-8",
        )

        diag = resolver.diagnose_lua_file(str(lua_path))
        assert diag.total_depots == 2
        assert diag.ready_count == 0
        assert diag.missing_count == 2
        assert not diag.is_ready
        assert "101_201" in diag.missing_manifests
        assert "102_202" in diag.missing_manifests

        # 写入其中一个清单
        (Path(temp_steam_dir) / "depotcache" / "101_201.manifest").write_bytes(b"data")
        diag2 = resolver.diagnose_lua_file(str(lua_path))
        assert diag2.ready_count == 1
        assert diag2.missing_count == 1
        assert not diag2.is_ready

        # 写入另一个清单
        (Path(temp_steam_dir) / "config" / "depotcache" / "102_202.manifest").write_bytes(b"data")
        diag3 = resolver.diagnose_lua_file(str(lua_path))
        assert diag3.ready_count == 2
        assert diag3.missing_count == 0
        assert diag3.is_ready
        resolver.close()

    def test_diagnose_app(self, temp_steam_dir):
        resolver = ManifestResolver(temp_steam_dir)
        lua_path = Path(temp_steam_dir) / "config" / "lua" / "100.lua"
        lua_path.write_text('addappid(100)\n', encoding="utf-8")

        diag = resolver.diagnose_app("100")
        assert diag.app_id == "100"
        assert diag.is_ready is True
        assert diag.total_depots == 0
        resolver.close()

    def test_update_lua_manifest(self, temp_steam_dir):
        resolver = ManifestResolver(temp_steam_dir)
        lua_path = Path(temp_steam_dir) / "config" / "lua" / "1623730.lua"
        lua_path.write_text(
            'addappid(1623730)\n'
            'setManifestid(1623731, "111111")\n',
            encoding="utf-8",
        )

        # 1. 更新已存在的 depot
        updated = resolver.update_lua_manifest("1623730", "1623731", "999999", size=500)
        assert updated is True
        content = lua_path.read_text(encoding="utf-8")
        assert 'setManifestid(1623731, "999999", 500)' in content
        assert "111111" not in content

        # 2. 追加新 depot
        added = resolver.update_lua_manifest("1623730", "2771111", "6155944524181178373")
        assert added is True
        content2 = lua_path.read_text(encoding="utf-8")
        assert 'setManifestid(2771111, "6155944524181178373")' in content2
        resolver.close()

    def test_fetch_from_mirrors(self, temp_steam_dir):
        resolver = ManifestResolver(temp_steam_dir)

        # 构建模拟 ZIP
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Cyber_123_456.manifest", b"manifest_payload")
            zf.writestr("789.lua", b"addappid(789)\n")
        zip_bytes = buf.getvalue()

        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.content = zip_bytes

        with patch.object(resolver._http, "get", return_value=mock_resp):
            success, count = resolver._fetch_from_mirrors("789")

        assert success is True
        assert count == 1
        assert (Path(temp_steam_dir) / "depotcache" / "123_456.manifest").exists()
        assert (Path(temp_steam_dir) / "config" / "lua" / "789.lua").exists()
        resolver.close()

    def test_resolve_manifests_all_ready(self, temp_steam_dir):
        resolver = ManifestResolver(temp_steam_dir)
        (Path(temp_steam_dir) / "depotcache" / "101_201.manifest").write_bytes(b"data")

        ready, msg, count = resolver.resolve_manifests("100", depots=[("101", "201", 10)])
        assert ready is True
        assert count == 0
        assert "就绪" in msg
        resolver.close()

    def test_resolve_manifests_download_cascade(self, temp_steam_dir):
        resolver = ManifestResolver(temp_steam_dir)
        lua_path = Path(temp_steam_dir) / "config" / "lua" / "100.lua"
        lua_path.write_text(
            'addappid(100)\n'
            'setManifestid(101, "201")\n',
            encoding="utf-8",
        )

        fake_batch = ManifestBatchResult(
            total=1,
            success=1,
            results=[ManifestDownloadResult(depot_id="101", manifest_gid="201", success=True)],
        )

        def fake_download(*args, **kwargs):
            # 模拟下载器在下载时将文件写入 depotcache
            (Path(temp_steam_dir) / "depotcache" / "101_201.manifest").write_bytes(b"ok")
            return fake_batch

        with patch("core.manifest_resolver.ManifestDownloader") as mock_downloader_cls:
            mock_inst = Mock()
            mock_inst.download_manifests.side_effect = fake_download
            mock_downloader_cls.return_value = mock_inst

            ready, msg, count = resolver.resolve_manifests("100")
            assert ready is True
            assert count == 1
            assert "已成功自动补全" in msg

        resolver.close()

    def test_resolve_manifests_with_tag_lookup(self, temp_steam_dir):
        """测试缺少 GID 时自动通过 Tag 发现并下载补全"""
        resolver = ManifestResolver(temp_steam_dir)
        lua_path = Path(temp_steam_dir) / "config" / "lua" / "100.lua"
        lua_path.write_text('addappid(100)\naddappid(101)\n', encoding="utf-8")

        mock_api_resp = Mock()
        mock_api_resp.status_code = 200
        mock_api_resp.json.return_value = [
            {"ref": "refs/tags/101_555555555555555"},
            {"ref": "refs/tags/101_888888888888888"},
        ]

        with patch.object(resolver._http, "get", return_value=mock_api_resp):
            gid = resolver._lookup_tag_gid_for_depot("101")
            assert gid == "888888888888888"

        resolver.close()

    def test_get_search_queries(self):
        queries = ManifestResolver.get_search_queries("1623730", "Palworld 幻兽帕鲁")
        assert "百度搜索" in queries
        assert "Bilibili 教程与清单" in queries
        assert "GitHub Manifest 仓库" in queries
        assert "1623730" in queries["百度搜索"]
