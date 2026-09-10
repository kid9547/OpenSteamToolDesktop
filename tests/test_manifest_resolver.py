"""
测试 core.manifest_resolver — 清单解析与自动化获取服务
"""
import os
import tempfile
import zipfile
import io
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from core.manifest_resolver import ManifestResolver, ManifestReadiness


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

    def test_get_search_queries(self):
        queries = ManifestResolver.get_search_queries("1623730", "Palworld 幻兽帕鲁")
        assert "百度搜索" in queries
        assert "Bilibili 教程与清单" in queries
        assert "GitHub Manifest 仓库" in queries
        assert "1623730" in queries["百度搜索"]
