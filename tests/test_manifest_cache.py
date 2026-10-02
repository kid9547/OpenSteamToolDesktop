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
