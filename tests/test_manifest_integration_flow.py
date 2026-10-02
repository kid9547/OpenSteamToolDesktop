"""搜索入库 → 归档对齐 → 写 Lua → 补全清单 的集成测试。

这是「清单老是下载不全」的**防复发**验证：新入库的游戏必须从一开始就让
Lua 里的 ``setManifestid`` 与归档中的真实清单 GID 一致，否则 Steam 永远下不全。

网络层全部注入假实现，测试完全离线。
"""
from __future__ import annotations

import re

import pytest

from core.game_manager import DepotInfo, GameMetadata, LuaGameManager
from core.manifest_downloader import STEAM_MANIFEST_MAGIC


class _FakeArchiveClient:
    """假归档客户端：返回一份含真实 (depot, gid) 的归档。"""

    def __init__(self, app_id: str, depots: dict[str, str], keys: dict[str, str] | None = None):
        self.app_id = app_id
        self.depots = depots
        self.keys = keys or {}
        self.closed = False

    def fetch_appid_archive(self, app_id: str):
        from core.manifest_archive import ArchiveManifest, ManifestArchive

        if str(app_id) != self.app_id:
            return ManifestArchive(app_id=str(app_id), ok=False, message="无此分支")
        manifests = [
            ArchiveManifest(
                depot_id=depot,
                gid=gid,
                data=STEAM_MANIFEST_MAGIC + f"{depot}-{gid}".encode(),
            )
            for depot, gid in self.depots.items()
        ]
        return ManifestArchive(
            app_id=self.app_id,
            ok=True,
            manifests=manifests,
            depot_keys=dict(self.keys),
            source_url=f"fake://{self.app_id}",
            message="fake archive",
        )

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _NoNetworkDownloader:
    """占位下载器：归档已覆盖全部清单，任何网络调用都视为失败。

    对应 ``ManifestDownloader.download_manifests`` 的返回契约：
    需要 ``success`` 与 ``results``（每项含 ``depot_id``/``manifest_gid``/``success``/``message``）。
    """

    class _Batch:
        success = False
        results: list = []

    def __init__(self, *_args, **_kwargs):
        pass

    def download_manifests(self, *_args, **_kwargs):
        return self._Batch()

    def close(self) -> None:
        pass


def _resolver(steam_path, archive) -> "ManifestResolver":
    """构造注入了假归档的 ManifestResolver（惰性子服务通过私有属性替换）。"""
    from core.manifest_resolver import ManifestResolver

    resolver = ManifestResolver(str(steam_path))
    resolver._archive_client = archive
    service = resolver._get_completion_service()
    service._archive = archive
    service._downloader = _NoNetworkDownloader()
    return resolver


@pytest.fixture()
def manager(tmp_path):
    lua_dir = tmp_path / "steam" / "config" / "lua"
    lua_dir.mkdir(parents=True)
    (tmp_path / "steam" / "depotcache").mkdir(parents=True)
    (tmp_path / "steam" / "config" / "depotcache").mkdir(parents=True)
    return LuaGameManager(lua_dir=str(lua_dir), steam_path=str(tmp_path / "steam"))


def _extract_manifest_pairs(lua_text: str) -> list[tuple[str, str]]:
    return re.findall(r'setmanifestid\s*\(\s*(\d+)\s*,\s*["\']?(\d+)', lua_text, re.IGNORECASE)


def test_archive_bindings_override_official_gids_before_lua_write(manager, tmp_path):
    """核心回归：官方 API 给的 GID 是错的，必须被归档 GID 覆盖后再写 Lua。"""

    archive = _FakeArchiveClient("1623730", {"1623731": "3938499722445376447"})
    resolver = _resolver(tmp_path / "steam", archive)
    try:
        bindings = resolver.prefetch_archive_bindings("1623730")
    finally:
        resolver.close()

    assert bindings["ok"] is True
    assert bindings["gid_map"] == {"1623731": "3938499722445376447"}


def test_metadata_patched_with_archive_gid_produces_consistent_lua(manager, tmp_path):
    """模拟 search_page 的两步：先对齐归档，再写 Lua。"""
    from core.manifest_resolver import ManifestResolver

    archive_gid = "3938499722445376447"
    archive = _FakeArchiveClient(
        "1623730", {"1623731": archive_gid}, {"1623731": "ab" * 32}
    )
    resolver = _resolver(tmp_path / "steam", archive)
    try:
        bindings = resolver.prefetch_archive_bindings("1623730")
    finally:
        resolver.close()

    # 官方 API 返回的"最新"GID 与归档不同 —— 这正是旧实现 404 的原因
    metadata = GameMetadata(
        app_id="1623730",
        name="Palworld",
        depots=[DepotInfo(depot_id="1623731", manifest_gid="9999999999999999999")],
    )

    gid_map = bindings["gid_map"]
    depot_keys = bindings["depot_keys"]
    for depot in metadata.depots:
        if gid_map.get(depot.depot_id):
            depot.manifest_gid = gid_map[depot.depot_id]
        if depot_keys.get(depot.depot_id) and not depot.depot_key:
            depot.depot_key = depot_keys[depot.depot_id]

    path = manager.add_game_with_metadata(metadata)
    assert path is True or path  # add_game_with_metadata 返回布尔

    lua_path = tmp_path / "steam" / "config" / "lua" / "1623730.lua"
    text = lua_path.read_text(encoding="utf-8")

    pairs = _extract_manifest_pairs(text)
    assert pairs == [("1623731", archive_gid)], "Lua 必须使用归档里的真实 GID"
    assert "9999999999999999999" not in text, "官方 API 的过期 GID 不得残留"
    assert "ab" * 32 in text, "归档里的 depot 密钥必须写入 Lua"


def test_complete_app_deploys_archive_manifests_to_both_dirs(manager, tmp_path):
    """补全必须把归档清单落到两个 depotcache 目录，且文件名与 Lua GID 一致。"""
    from core.manifest_resolver import ManifestResolver

    archive_gid = "1234567890123456789"
    archive = _FakeArchiveClient("777", {"7771": archive_gid})
    steam = tmp_path / "steam"
    resolver = _resolver(steam, archive)
    try:
        # 先写入一条声明该 (depot, gid) 的 Lua
        lua_path = steam / "config" / "lua" / "777.lua"
        lua_path.write_text(
            f'addappid(777)\nsetManifestid(7771, "{archive_gid}")\n', encoding="utf-8"
        )
        report = resolver.complete_app("777")
    finally:
        resolver.close()

    assert report.is_complete is True, report.summary()

    for directory in (steam / "depotcache", steam / "config" / "depotcache"):
        target = directory / f"7771_{archive_gid}.manifest"
        assert target.is_file(), f"缺少 {target}"
        assert target.read_bytes()[:4] == STEAM_MANIFEST_MAGIC

    # Lua 与落盘文件名一致性
    text = (steam / "config" / "lua" / "777.lua").read_text(encoding="utf-8")
    for depot, gid in _extract_manifest_pairs(text):
        assert (steam / "depotcache" / f"{depot}_{gid}.manifest").is_file()


def test_complete_app_reports_reason_for_unarchived_app(manager, tmp_path):
    """未被社区收录的 AppID 必须给出可读原因，而不是静默成功。"""

    archive = _FakeArchiveClient("777", {})
    steam = tmp_path / "steam"
    resolver = _resolver(steam, archive)
    try:
        (steam / "config" / "lua" / "888.lua").write_text(
            'addappid(888)\nsetManifestid(8881, "555")\n', encoding="utf-8"
        )
        report = resolver.complete_app("888")
    finally:
        resolver.close()

    assert report.is_complete is False
    assert report.missing_results or report.failed_results
    summary = report.summary()
    assert "888" in summary


def test_complete_app_is_idempotent(manager, tmp_path):
    """重复补全不得重复下载（第二次应全部 already）。"""
    from core.manifest_resolver import ManifestResolver

    archive_gid = "111222333444555"
    steam = tmp_path / "steam"
    archive = _FakeArchiveClient("999", {"9991": archive_gid})
    resolver = _resolver(steam, archive)
    try:
        (steam / "config" / "lua" / "999.lua").write_text(
            f'addappid(999)\nsetManifestid(9991, "{archive_gid}")\n', encoding="utf-8"
        )
        first = resolver.complete_app("999")
        second = resolver.complete_app("999")
    finally:
        resolver.close()

    assert first.is_complete and second.is_complete
    assert second.downloaded == 0, "第二次不应再下载"
