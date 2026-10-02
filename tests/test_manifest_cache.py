from pathlib import Path

from core.manifest_cache import ManifestCacheManager
from core.manifest_downloader import STEAM_MANIFEST_MAGIC


def test_scan_deduplicates_two_cache_directories_and_tracks_validity(tmp_path):
    (tmp_path / "depotcache").mkdir()
    (tmp_path / "config" / "depotcache").mkdir(parents=True)
    first = tmp_path / "depotcache" / "100_200.manifest"
    second = tmp_path / "config" / "depotcache" / "100_200.manifest"
    first.write_bytes(STEAM_MANIFEST_MAGIC + b"payload")
    second.write_bytes(b"broken")

    records = ManifestCacheManager(str(tmp_path)).scan()

    assert len(records) == 1
    assert records[0].key == "100_200"
    assert len(records[0].paths) == 2
    assert records[0].valid is True


def test_orphaned_and_sync_are_explicit(tmp_path):
    cache = ManifestCacheManager(str(tmp_path))
    depot_dir = tmp_path / "depotcache"
    lua_dir = tmp_path / "config" / "lua"
    depot_dir.mkdir(parents=True)
    lua_dir.mkdir(parents=True)
    (depot_dir / "100_200.manifest").write_bytes(STEAM_MANIFEST_MAGIC + b"payload")
    (depot_dir / "101_201.manifest").write_bytes(STEAM_MANIFEST_MAGIC + b"payload")
    (lua_dir / "100.lua").write_text(
        'setManifestid(100, "200")\n', encoding="utf-8"
    )

    assert [item.key for item in cache.orphaned()] == ["101_201"]
    record = cache.scan()[0]
    assert cache.sync(record) == 1
    assert (tmp_path / "config" / "depotcache" / "100_200.manifest").exists()
    assert cache.delete("101_201") == 1


def test_delete_rejects_path_traversal(tmp_path):
    cache = ManifestCacheManager(str(tmp_path))
    try:
        cache.delete("../outside")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid manifest key was accepted")


# ── 逐游戏完整性审计（"清单下载不全"的检测手段） ──────────────
def _audit_tree(tmp_path):
    """构造：100 完整 / 200 缺一个 / 300 有一个损坏 / 400 无绑定。"""
    depot_dir = tmp_path / "depotcache"
    lua_dir = tmp_path / "config" / "lua"
    depot_dir.mkdir(parents=True)
    lua_dir.mkdir(parents=True)

    (depot_dir / "101_201.manifest").write_bytes(STEAM_MANIFEST_MAGIC + b"x")
    (depot_dir / "201_301.manifest").write_bytes(STEAM_MANIFEST_MAGIC + b"w")
    (depot_dir / "301_401.manifest").write_bytes(b"broken-bytes")
    (depot_dir / "302_402.manifest").write_bytes(STEAM_MANIFEST_MAGIC + b"y")

    (lua_dir / "100.lua").write_text('addappid(100)\nsetManifestid(101, "201")\n', encoding="utf-8")
    (lua_dir / "200.lua").write_text(
        'addappid(200)\nsetManifestid(201, "301")\nsetManifestid(202, "302")\n',
        encoding="utf-8",
    )
    (lua_dir / "300.lua").write_text(
        'addappid(300)\nsetManifestid(301, "401")\nsetManifestid(302, "402")\n',
        encoding="utf-8",
    )
    (lua_dir / "400.lua").write_text("addappid(400)\n", encoding="utf-8")
    # 非数字文件名必须被忽略
    (lua_dir / "manifest.lua").write_text('setManifestid(999, "999")\n', encoding="utf-8")
    return ManifestCacheManager(str(tmp_path)), lua_dir


def test_audit_reports_complete_missing_and_damaged(tmp_path):
    cache, lua_dir = _audit_tree(tmp_path)
    audits = {item.app_id: item for item in cache.audit(lua_dir)}

    # 没有 setManifestid 绑定 / 非数字文件名的 Lua 不参与判定
    assert set(audits) == {"100", "200", "300"}

    assert audits["100"].ok is True
    assert audits["100"].present == 1
    assert audits["100"].summary().endswith("全部就绪")

    assert audits["200"].ok is False
    assert audits["200"].missing == ["202_302"]
    assert audits["200"].damaged == []
    assert audits["200"].present == 1
    assert audits["200"].declared == 2

    assert audits["300"].ok is False
    assert audits["300"].missing == []
    assert audits["300"].damaged == ["301_401"]
    assert audits["300"].present == 1
    assert "损坏 1 个" in audits["300"].summary()


def test_audit_is_sorted_by_app_id(tmp_path):
    cache, lua_dir = _audit_tree(tmp_path)
    assert [item.app_id for item in cache.audit(lua_dir)] == ["100", "200", "300"]


def test_audit_requires_lua_dir(tmp_path):
    depot_dir = tmp_path / "depotcache"
    depot_dir.mkdir(parents=True)
    (depot_dir / "1_2.manifest").write_bytes(STEAM_MANIFEST_MAGIC + b"x")
    cache = ManifestCacheManager(str(tmp_path))

    assert cache.audit("") == []
    assert cache.audit(tmp_path / "does-not-exist") == []


def test_audit_accepts_string_lua_dir(tmp_path):
    cache, lua_dir = _audit_tree(tmp_path)
    assert len(cache.audit(str(lua_dir))) == 3
