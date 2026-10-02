"""测试 core.complete_manifest_service — 归档优先的「一键补全」流水线（全部离线）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.complete_manifest_service import (
    STATUS_ALREADY,
    STATUS_DOWNLOADED,
    STATUS_FAILED,
    STATUS_INVALID,
    STATUS_MISSING,
    STATUS_SKIPPED,
    CompletionReport,
    ManifestCompletionService,
    parse_lua_bindings,
    read_lua_app_ids,
    update_lua_depot_key,
    update_lua_manifest,
)
from core.manifest_archive import ArchiveManifest, ManifestArchive
from core.manifest_downloader import (
    STEAM_MANIFEST_MAGIC,
    ManifestBatchResult,
    ManifestDownloadResult,
)


def payload(depot: str, gid: str) -> bytes:
    return STEAM_MANIFEST_MAGIC + f"{depot}:{gid}".encode() * 4


def build_archive(
    app_id: str,
    entries: list[tuple[str, str]],
    keys: dict[str, str] | None = None,
    config: dict | None = None,
    repo: str = "Auiowu/ManifestAutoUpdate",
    url: str = "https://codeload.github.com/Auiowu/ManifestAutoUpdate/zip/refs/heads/x",
) -> ManifestArchive:
    manifests = [
        ArchiveManifest(depot_id=depot, gid=gid, data=payload(depot, gid))
        for depot, gid in entries
    ]
    return ManifestArchive(
        app_id=app_id,
        repo=repo,
        source_url=url,
        manifests=manifests,
        depot_keys=dict(keys or {}),
        config=dict(config or {}),
        ok=True,
        message=f"从归档解析出 {len(manifests)} 个清单",
    )


class FakeArchiveClient:
    """返回预置归档的假归档客户端，并记录调用次数。"""

    def __init__(self, archives: dict[str, ManifestArchive] | None = None, default_message: str = "所有社区仓库均无此 AppID 分支"):
        self.archives = dict(archives or {})
        self.default_message = default_message
        self.calls: list[str] = []
        self.closed = False

    def fetch_appid_archive(self, app_id: str) -> ManifestArchive:
        app_id = str(app_id)
        self.calls.append(app_id)
        if app_id in self.archives:
            return self.archives[app_id]
        return ManifestArchive(app_id=app_id, ok=False, message=self.default_message)

    def close(self) -> None:
        self.closed = True


class FakeDownloader:
    """假的按 (depot, gid) 精确下载器。"""

    def __init__(self, succeed: bool = False, write_dir: Path | None = None):
        self.succeed = succeed
        self.write_dir = write_dir
        self.calls: list[tuple] = []
        self.closed = False

    def download_manifests(self, depots, app_id: str = "") -> ManifestBatchResult:
        self.calls.append((tuple(tuple(d) for d in depots), app_id))
        result = ManifestBatchResult(total=len(depots))
        for item in depots:
            depot_id, gid = str(item[0]), str(item[1])
            if self.succeed:
                result.success += 1
                if self.write_dir is not None:
                    self.write_dir.mkdir(parents=True, exist_ok=True)
                    (self.write_dir / f"{depot_id}_{gid}.manifest").write_bytes(
                        payload(depot_id, gid)
                    )
                result.results.append(
                    ManifestDownloadResult(
                        depot_id=depot_id,
                        manifest_gid=gid,
                        success=True,
                        message="GitHub Manifest Cache",
                    )
                )
            else:
                result.failed += 1
                result.results.append(
                    ManifestDownloadResult(
                        depot_id=depot_id,
                        manifest_gid=gid,
                        success=False,
                        message="All sources exhausted",
                    )
                )
        return result

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def steam_dir(tmp_path: Path) -> Path:
    (tmp_path / "depotcache").mkdir(parents=True)
    (tmp_path / "config" / "depotcache").mkdir(parents=True)
    (tmp_path / "config" / "lua").mkdir(parents=True)
    return tmp_path


def make_service(steam_dir: Path, archive: ManifestArchive | None = None, downloader: FakeDownloader | None = None):
    archives = {archive.app_id: archive} if archive is not None else {}
    client = FakeArchiveClient(archives)
    dl = downloader if downloader is not None else FakeDownloader(succeed=False)
    return ManifestCompletionService(str(steam_dir), archive_client=client, downloader=dl), client, dl


# ── Lua 辅助函数 ──────────────────────────────────────────


class TestLuaHelpers:
    def test_parse_lua_bindings(self, steam_dir: Path):
        lua = steam_dir / "config" / "lua" / "100.lua"
        lua.write_text(
            "addappid(100)\n"
            'addappid(101, 0, "bca9a9cde94bb4dff61849c6a87230ee45867a590fdd28826366e35e7d62c08e")\n'
            'setManifestid(101, "7127896784363312296")\n'
            'setManifestid(102, "522925226152885710", 204)\n',
            encoding="utf-8",
        )
        bindings = parse_lua_bindings(lua)
        assert bindings["101"]["gid"] == "7127896784363312296"
        assert bindings["101"]["key"].startswith("bca9a9cd")
        assert bindings["102"]["size"] == 204
        assert set(read_lua_app_ids(lua)) == {"100", "101"}

    def test_update_lua_manifest_replaces_and_appends(self, steam_dir: Path):
        lua = steam_dir / "config" / "lua" / "100.lua"
        lua.write_text('addappid(100)\nsetManifestid(101, "888")\n', encoding="utf-8")

        assert update_lua_manifest(lua, "101", "999") is True
        content = lua.read_text(encoding="utf-8")
        assert 'setManifestid(101, "999")' in content
        assert "888" not in content

        assert update_lua_manifest(lua, "102", "555") is True
        assert 'setManifestid(102, "555")' in lua.read_text(encoding="utf-8")

    def test_update_lua_depot_key_is_idempotent(self, steam_dir: Path):
        lua = steam_dir / "config" / "lua" / "100.lua"
        lua.write_text("addappid(100)\naddappid(101)\n", encoding="utf-8")
        key = "bca9a9cde94bb4dff61849c6a87230ee45867a590fdd28826366e35e7d62c08e"

        assert update_lua_depot_key(lua, "101", key) is True
        first = lua.read_text(encoding="utf-8")
        assert f'addappid(101, 0, "{key}")' in first
        assert first.count("addappid(101") == 1

        assert update_lua_depot_key(lua, "101", key) is True
        assert lua.read_text(encoding="utf-8") == first


# ── 归档优先流水线 ────────────────────────────────────────


class TestArchivedFirstCompletion:
    def test_complete_app_writes_both_cache_dirs_with_archive_gid(self, steam_dir: Path):
        lua = steam_dir / "config" / "lua" / "100.lua"
        lua.write_text('addappid(100)\nsetManifestid(101, "888")\n', encoding="utf-8")
        archive = build_archive("100", [("101", "999")], keys={"101": "ab" * 32})
        service, client, _dl = make_service(steam_dir, archive)

        report = service.complete_app("100")

        expected = payload("101", "999")
        for directory in (steam_dir / "depotcache", steam_dir / "config" / "depotcache"):
            written = directory / "101_999.manifest"
            assert written.is_file(), f"缺少双目录副本: {written}"
            assert written.read_bytes() == expected
            assert written.read_bytes()[:4] == STEAM_MANIFEST_MAGIC

        # Lua 必须使用归档里的真实 GID，而不是元数据/官方 API 的旧 GID
        content = lua.read_text(encoding="utf-8")
        assert 'setManifestid(101, "999")' in content
        assert "888" not in content
        # Key.vdf 中的 depot 密钥被补入
        assert f'addappid(101, 0, "{"ab" * 32}")' in content

        assert report.archive_repo == "Auiowu/ManifestAutoUpdate"
        assert report.downloaded == 1
        assert report.is_complete is True
        assert report.depot_keys == {"101": "ab" * 32}
        assert report.sources_used and "AppID 分支归档" in report.sources_used[0]
        assert report.result_for("101").source == report.sources_used[0]

    def test_lua_gids_match_written_manifest_filenames(self, steam_dir: Path):
        lua = steam_dir / "config" / "lua" / "730.lua"
        lua.write_text("addappid(730)\naddappid(731)\naddappid(732)\n", encoding="utf-8")
        archive = build_archive(
            "730",
            [("731", "7127896784363312296"), ("732", "522925226152885710")],
            config={"appId": 730, "depots": [731, 732], "dlcs": []},
        )
        service, _client, _dl = make_service(steam_dir, archive)

        report = service.complete_app("730")

        content = lua.read_text(encoding="utf-8")
        bindings = parse_lua_bindings(lua)
        assert report.total == 2
        assert report.is_complete is True
        for depot_id, binding in bindings.items():
            if not binding["gid"]:
                continue
            filename = f"{depot_id}_{binding['gid']}.manifest"
            assert (steam_dir / "depotcache" / filename).is_file()
            assert (steam_dir / "config" / "depotcache" / filename).is_file()
            assert (steam_dir / "depotcache" / filename).read_bytes()[:4] == STEAM_MANIFEST_MAGIC
        assert 'setManifestid(731, "7127896784363312296")' in content

    def test_second_run_is_idempotent(self, steam_dir: Path):
        lua = steam_dir / "config" / "lua" / "100.lua"
        lua.write_text("addappid(100)\naddappid(101)\n", encoding="utf-8")
        archive = build_archive("100", [("101", "999")])
        service, client, _dl = make_service(steam_dir, archive)

        first = service.complete_app("100")
        target = steam_dir / "depotcache" / "101_999.manifest"
        first_bytes = target.read_bytes()

        second = service.complete_app("100")

        assert first.downloaded == 1
        assert second.downloaded == 0
        assert second.already == 1
        assert second.is_complete is True
        assert second.results[0].status == STATUS_ALREADY
        assert target.read_bytes() == first_bytes

    def test_precise_per_gid_fallback_runs_when_archive_missing(self, steam_dir: Path):
        lua = steam_dir / "config" / "lua" / "100.lua"
        lua.write_text('addappid(100)\nsetManifestid(101, "201")\n', encoding="utf-8")
        downloader = FakeDownloader(succeed=True, write_dir=steam_dir / "depotcache")
        service, _client, dl = make_service(steam_dir, archive=None, downloader=downloader)

        report = service.complete_app("100")

        assert dl.calls, "精确 (depot,gid) 回退路径未被调用"
        assert dl.calls[0][0] == (("101", "201", 0, "100"),)
        assert report.downloaded == 1
        assert report.is_complete is True
        # 精确路径只写了一侧，服务应把另一侧补齐
        assert (steam_dir / "config" / "depotcache" / "101_201.manifest").is_file()

    def test_dual_directory_mirror_sync_counts_as_downloaded(self, steam_dir: Path):
        lua = steam_dir / "config" / "lua" / "100.lua"
        lua.write_text('addappid(100)\nsetManifestid(101, "999")\n', encoding="utf-8")
        # 只在 depotcache 里预置有效副本
        (steam_dir / "depotcache" / "101_999.manifest").write_bytes(payload("101", "999"))

        service, _client, _dl = make_service(steam_dir, archive=None)
        report = service.complete_app("100")

        assert (steam_dir / "config" / "depotcache" / "101_999.manifest").is_file()
        assert report.downloaded == 1
        assert report.results[0].source == "本地双目录镜像同步"
        assert report.is_complete is True


# ── 失败/缺失原因聚合 ─────────────────────────────────────


class TestCompletionReportReasons:
    def test_failed_depot_reports_reason(self, steam_dir: Path):
        lua = steam_dir / "config" / "lua" / "100.lua"
        lua.write_text('addappid(100)\nsetManifestid(101, "201")\n', encoding="utf-8")
        service, _client, _dl = make_service(steam_dir, archive=None)

        report = service.complete_app("100")

        assert report.is_complete is False
        assert report.failed == 1
        assert report.results[0].status == STATUS_FAILED
        assert "所有来源均未取到" in report.results[0].message
        assert "仍缺" in report.summary()

    def test_missing_gid_for_required_depot_reports_reason(self, steam_dir: Path):
        lua = steam_dir / "config" / "lua" / "100.lua"
        lua.write_text("addappid(100)\n", encoding="utf-8")
        service, _client, _dl = make_service(steam_dir, archive=None)

        report = service.complete_app("100", depots=[("777", "", 0, "100")])

        assert report.results[0].status == STATUS_MISSING
        assert "GID" in report.results[0].message
        assert report.missing == 1
        assert report.is_complete is False

    def test_declared_but_unavailable_depot_is_skipped(self, steam_dir: Path):
        lua = steam_dir / "config" / "lua" / "100.lua"
        lua.write_text("addappid(100)\naddappid(101)\n", encoding="utf-8")
        archive = build_archive("100", [("100", "111")], config={"appId": 100, "depots": [100, 101]})
        service, _client, _dl = make_service(steam_dir, archive)

        report = service.complete_app("100")

        statuses = {r.depot_id: r.status for r in report.results}
        assert statuses["101"] == STATUS_SKIPPED
        assert report.skipped == 1
        assert report.is_complete is True  # 归档未收录的 depot 不计入完整性

    def test_corrupted_existing_file_is_invalid(self, steam_dir: Path):
        lua = steam_dir / "config" / "lua" / "100.lua"
        lua.write_text('addappid(100)\nsetManifestid(101, "999")\n', encoding="utf-8")
        (steam_dir / "depotcache" / "101_999.manifest").write_bytes(b"corrupted")
        service, _client, _dl = make_service(steam_dir, archive=None)

        report = service.complete_app("100")

        assert report.invalid == 1
        assert report.results[0].status == STATUS_INVALID
        assert "魔数" in report.results[0].message
        assert report.is_complete is False

    def test_invalid_steam_path_is_reported(self):
        service = ManifestCompletionService("")
        report = service.complete_app("100")
        assert report.is_complete is False
        assert report.message == "Steam 安装路径无效"


class TestOneClickBatch:
    def test_complete_apps_aggregates_results(self, steam_dir: Path):
        for app_id, gid in (("100", "999"), ("200", "888")):
            lua = steam_dir / "config" / "lua" / f"{app_id}.lua"
            lua.write_text(f"addappid({app_id})\naddappid(1)\n", encoding="utf-8")
        archive = build_archive("100", [("1", "999")])
        service, client, _dl = make_service(steam_dir, archive)
        # 200 无归档
        lua200 = steam_dir / "config" / "lua" / "200.lua"
        lua200.write_text('addappid(200)\nsetManifestid(2, "888")\n', encoding="utf-8")

        batch = service.complete_apps(["100", "200"])

        assert batch.total_apps == 2
        assert batch.complete_apps == 1
        assert batch.downloaded == 1
        assert [r.app_id for r in batch.failed_apps] == ["200"]
        assert "1/2" in batch.summary()
        # 失败的 AppID 也能给出具体原因
        failed = batch.failed_apps[0]
        assert failed.failed_results[0].key == "2_888"

    def test_complete_apps_survives_archive_exception(self, steam_dir: Path):
        class BoomClient(FakeArchiveClient):
            def fetch_appid_archive(self, app_id: str) -> ManifestArchive:
                raise RuntimeError("network down")

        (steam_dir / "config" / "lua" / "100.lua").write_text("addappid(100)\n", encoding="utf-8")
        service = ManifestCompletionService(
            str(steam_dir), archive_client=BoomClient(), downloader=FakeDownloader()
        )
        batch = service.complete_apps(["100"])
        assert batch.total_apps == 1
        assert "异常" in batch.reports[0].message


class TestReportHelpers:
    def test_empty_report_is_not_complete(self):
        report = CompletionReport(app_id="100")
        assert report.is_complete is False
        assert report.summary() == "无需特定清单文件"

    def test_prefetch_bindings_shape(self, steam_dir: Path):
        archive = build_archive(
            "100",
            [("101", "999")],
            keys={"101": "cd" * 32},
            config={"appId": 100, "depots": [101], "dlcs": [555]},
        )
        service, _client, _dl = make_service(steam_dir, archive)
        bindings = service.prefetch_bindings("100")
        assert bindings["ok"] is True
        assert bindings["gid_map"] == {"101": "999"}
        assert bindings["depot_keys"] == {"101": "cd" * 32}
        assert bindings["dlc_ids"] == ["555"]
        assert bindings["repo"] == "Auiowu/ManifestAutoUpdate"

    def test_prefetch_bindings_without_archive(self, steam_dir: Path):
        service, _client, _dl = make_service(steam_dir, archive=None)
        bindings = service.prefetch_bindings("123456")
        assert bindings["ok"] is False
        assert bindings["gid_map"] == {}
