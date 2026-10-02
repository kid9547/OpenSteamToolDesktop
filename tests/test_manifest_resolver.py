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
    def test_deploy_manifest_archive_uses_actual_gid(self, temp_steam_dir):
        resolver = ManifestResolver(temp_steam_dir)
        lua_path = Path(temp_steam_dir) / "config" / "lua" / "100.lua"
        lua_path.write_text(
            'addappid(100)\nsetManifestid(101, "888")\n',
            encoding="utf-8",
        )
        payload = STEAM_MANIFEST_MAGIC + b"archive-manifest"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("100-branch/101_999.manifest", payload)

        deployed, count = resolver._deploy_manifest_archive("100", buf.getvalue())

        assert deployed is True
        assert count == 1
        assert (Path(temp_steam_dir) / "depotcache" / "101_999.manifest").exists()
        assert 'setManifestid(101, "999")' in lua_path.read_text(encoding="utf-8")
        assert '"888"' not in lua_path.read_text(encoding="utf-8")
        resolver.close()

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


class _StubArchiveClient:
    """返回预置归档的假归档客户端（archive 为 None 时表示"无此分支"）。"""

    def __init__(self, archive):
        self._archive = archive
        self.calls = []

    def fetch_appid_archive(self, app_id):
        from core.manifest_archive import ManifestArchive

        self.calls.append(str(app_id))
        if self._archive is None:
            return ManifestArchive(app_id=str(app_id), ok=False, message="所有社区仓库均无此 AppID 分支")
        return self._archive

    def close(self):
        pass


class _StubDownloader:
    """假的精确 (depot, gid) 下载器：默认全部失败，避免测试触网。"""

    def __init__(self, succeed: bool = False):
        self.succeed = succeed
        self.calls = []

    def download_manifests(self, depots, app_id: str = ""):
        self.calls.append((tuple(tuple(d) for d in depots), app_id))
        result = ManifestBatchResult(total=len(depots))
        for item in depots:
            if self.succeed:
                result.success += 1
                result.results.append(
                    ManifestDownloadResult(
                        depot_id=str(item[0]), manifest_gid=str(item[1]), success=True
                    )
                )
            else:
                result.failed += 1
                result.results.append(
                    ManifestDownloadResult(
                        depot_id=str(item[0]),
                        manifest_gid=str(item[1]),
                        success=False,
                        message="All sources exhausted",
                    )
                )
        return result

    def close(self):
        pass


class TestResolverArchiveFirstPipeline:
    """测试 resolver 层「归档优先」流水线与一键补全报告"""

    @staticmethod
    def _archive(app_id, entries, keys=None, repo="Auiowu/ManifestAutoUpdate"):
        from core.manifest_archive import ArchiveManifest, ManifestArchive

        manifests = [
            ArchiveManifest(
                depot_id=str(depot), gid=str(gid), data=STEAM_MANIFEST_MAGIC + b"payload" * 3
            )
            for depot, gid in entries
        ]
        return ManifestArchive(
            app_id=str(app_id),
            repo=repo,
            source_url=f"https://codeload.github.com/{repo}/zip/refs/heads/{app_id}",
            manifests=manifests,
            depot_keys=dict(keys or {}),
            ok=bool(manifests),
            message="ok",
        )

    @staticmethod
    def _inject(resolver, archive=None, downloader=None):
        """注入归档客户端与下载器，确保测试完全离线。"""
        from core.complete_manifest_service import ManifestCompletionService

        client = _StubArchiveClient(archive)
        dl = downloader if downloader is not None else _StubDownloader()
        resolver._archive_client = client
        resolver._completion_service = ManifestCompletionService(
            resolver._steam_path, archive_client=client, downloader=dl
        )
        return dl

    def test_resolve_manifests_uses_archive_for_gidless_depot(self, temp_steam_dir):
        resolver = ManifestResolver(temp_steam_dir)
        lua_path = Path(temp_steam_dir) / "config" / "lua" / "100.lua"
        lua_path.write_text("addappid(100)\naddappid(101)\n", encoding="utf-8")

        archive = self._archive("100", [("101", "999")], keys={"101": "ef" * 32})
        with patch("core.manifest_resolver.ManifestDownloader") as downloader_cls:
            mock_inst = Mock()
            mock_inst.download_manifests.return_value = ManifestBatchResult()
            downloader_cls.return_value = mock_inst
            self._inject(resolver, archive=archive)
            # 无 GID 的 depot 不会进入精确下载路径，只能由归档补齐
            ready, msg, count = resolver.resolve_manifests("100")

        assert ready is True
        assert count == 1
        assert "已成功自动补全" in msg
        content = lua_path.read_text(encoding="utf-8")
        assert 'setManifestid(101, "999")' in content
        for directory in ("depotcache", os.path.join("config", "depotcache")):
            target = Path(temp_steam_dir) / directory / "101_999.manifest"
            assert target.is_file()
            assert target.read_bytes()[:4] == STEAM_MANIFEST_MAGIC
        resolver.close()

    def test_complete_app_report_exposes_per_depot_reasons(self, temp_steam_dir):
        resolver = ManifestResolver(temp_steam_dir)
        lua_path = Path(temp_steam_dir) / "config" / "lua" / "100.lua"
        lua_path.write_text('addappid(100)\nsetManifestid(101, "888")\n', encoding="utf-8")

        archive = self._archive("100", [("101", "999")])
        self._inject(resolver, archive=archive)

        report = resolver.complete_app("100")

        assert report.is_complete is True
        assert report.downloaded == 1
        assert report.result_for("101").gid == "999"
        assert report.archive_repo == "Auiowu/ManifestAutoUpdate"
        resolver.close()

    def test_complete_app_reports_failure_reason_without_archive(self, temp_steam_dir):
        resolver = ManifestResolver(temp_steam_dir)
        (Path(temp_steam_dir) / "config" / "lua" / "100.lua").write_text(
            'addappid(100)\nsetManifestid(101, "888")\n', encoding="utf-8"
        )
        self._inject(resolver, archive=None)

        report = resolver.complete_app("100")

        assert report.is_complete is False
        assert report.failed == 1
        assert report.result_for("101").status == "failed"
        assert "所有来源均未取到" in report.result_for("101").message
        resolver.close()

    def test_complete_apps_returns_batch_report(self, temp_steam_dir):
        resolver = ManifestResolver(temp_steam_dir)
        (Path(temp_steam_dir) / "config" / "lua" / "100.lua").write_text(
            'addappid(100)\nsetManifestid(101, "888")\n', encoding="utf-8"
        )
        (Path(temp_steam_dir) / "config" / "lua" / "200.lua").write_text(
            'addappid(200)\nsetManifestid(201, "777")\n', encoding="utf-8"
        )

        class _PerAppStub(_StubArchiveClient):
            def fetch_appid_archive(self, app_id):
                self.calls.append(str(app_id))
                if str(app_id) == "100":
                    return TestResolverArchiveFirstPipeline._archive("100", [("101", "999")])
                from core.manifest_archive import ManifestArchive

                return ManifestArchive(app_id=str(app_id), ok=False, message="无分支")

        from core.complete_manifest_service import ManifestCompletionService

        resolver._archive_client = _PerAppStub(None)
        resolver._completion_service = ManifestCompletionService(
            temp_steam_dir,
            archive_client=resolver._archive_client,
            downloader=_StubDownloader(),
        )

        batch = resolver.complete_apps(["100", "200"])

        assert batch.total_apps == 2
        assert batch.complete_apps == 1
        assert batch.downloaded == 1
        assert "1/2" in batch.summary()
        resolver.close()

    def test_prefetch_archive_bindings_exposes_real_gids(self, temp_steam_dir):
        resolver = ManifestResolver(temp_steam_dir)
        archive = self._archive("730", [("731", "7127896784363312296")], keys={"731": "aa" * 32})
        self._inject(resolver, archive=archive)

        bindings = resolver.prefetch_archive_bindings("730")

        assert bindings["ok"] is True
        assert bindings["gid_map"] == {"731": "7127896784363312296"}
        assert bindings["depot_keys"] == {"731": "aa" * 32}
        resolver.close()

    def test_complete_app_with_invalid_steam_path(self):
        resolver = ManifestResolver("")
        report = resolver.complete_app("100")
        assert report.is_complete is False
        assert report.message == "Steam 安装路径无效"
        resolver.close()

    def test_deprecated_tag_lookup_is_not_used_by_pipeline(self, temp_steam_dir):
        """P-ToyStore 已 404，新流水线不应再调用旧的 Tag 查询。"""
        resolver = ManifestResolver(temp_steam_dir)
        lua_path = Path(temp_steam_dir) / "config" / "lua" / "100.lua"
        lua_path.write_text('addappid(100)\nsetManifestid(101, "888")\n', encoding="utf-8")

        with patch.object(resolver, "_lookup_tag_gid_for_depot") as tag_mock, \
             patch.object(resolver, "_fetch_from_mirrors", return_value=(False, 0)), \
             patch("core.manifest_resolver.ManifestDownloader") as downloader_cls:
            mock_inst = Mock()
            mock_inst.download_manifests.return_value = ManifestBatchResult()
            downloader_cls.return_value = mock_inst
            self._inject(resolver, archive=None)
            resolver.resolve_manifests("100")

        assert not tag_mock.called
        resolver.close()
