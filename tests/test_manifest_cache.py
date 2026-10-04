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


def test_orphaned_uses_depot_level_not_exact_gid(tmp_path):
    """回归测试：只有 addappid(depot) 绑定、没有 setManifestid 的 depot，
    其清单同样会被 Steam 使用，绝不能按精确 GID 匹配误判成孤儿。

    历史事故：孤儿清理按 ``setManifestid(depot, gid)`` 精确匹配判定，
    把 DLC / 共享 Redist depot 的在用清单清掉了。
    """
    cache = ManifestCacheManager(str(tmp_path))
    depot_dir = tmp_path / "depotcache"
    lua_dir = tmp_path / "config" / "lua"
    depot_dir.mkdir(parents=True)
    lua_dir.mkdir(parents=True)
    # Lua 只声明 addappid(300, 0, "key")，没有任何 setManifestid
    (lua_dir / "300.lua").write_text(
        'addappid(300, 0, "ab" * 32)\naddappid(301, 0, "cd" * 32)\n',
        encoding="utf-8",
    )
    (depot_dir / "301_999.manifest").write_bytes(STEAM_MANIFEST_MAGIC + b"payload")
    # 完全无人引用的 depot → 真孤儿
    (depot_dir / "404_888.manifest").write_bytes(STEAM_MANIFEST_MAGIC + b"payload")

    assert [item.key for item in cache.orphaned(str(lua_dir))] == ["404_888"]
    # 引用它的游戏可被反查
    assert cache.depot_owner("301", str(lua_dir)) == "300"
    assert cache.depot_owner("404", str(lua_dir)) == ""


def test_orphaned_protects_installed_game_depots(tmp_path):
    """回归测试：已安装正版游戏的清单绝不能被孤儿清理波及。

    历史事故：正版/本地安装游戏的 depot 不在任何解锁 Lua 里，旧规则把它们
    判成孤儿清掉，导致用户已安装的游戏损坏。
    """
    cache = ManifestCacheManager(str(tmp_path))
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()
    (steamapps / "appmanifest_500.acf").write_text(
        '"AppState"\n'
        '{\n'
        '\t"appid"\t\t"500"\n'
        '\t"InstalledDepots"\n'
        '\t{\n'
        '\t\t"501"\t\t{ "manifest" "1000" }\n'
        '\t}\n'
        '\t"MountedDepots"\n'
        '\t{\n'
        '\t\t"502"\t\t{ "manifest" "1001" }\n'
        '\t}\n'
        '}\n',
        encoding="utf-8",
    )
    depot_dir = tmp_path / "depotcache"
    depot_dir.mkdir()
    (depot_dir / "501_1000.manifest").write_bytes(STEAM_MANIFEST_MAGIC + b"a")
    (depot_dir / "502_1001.manifest").write_bytes(STEAM_MANIFEST_MAGIC + b"b")
    # 无任何归属 → 真孤儿
    (depot_dir / "999_1002.manifest").write_bytes(STEAM_MANIFEST_MAGIC + b"c")

    assert [item.key for item in cache.orphaned()] == ["999_1002"]
    categories = {item["key"]: item["category"] for item in cache.classify(cache.scan())}
    assert categories["501_1000"] == "installed"
    assert categories["502_1001"] == "installed"
    assert categories["999_1002"] == "orphan"


def test_backup_and_delete_refuses_in_use_manifests(tmp_path):
    """纵深防御：即使调用方把在用清单误传入 backup_and_delete，也必须拒移。"""
    cache = ManifestCacheManager(str(tmp_path))
    lua_dir = tmp_path / "config" / "lua"
    lua_dir.mkdir(parents=True)
    (lua_dir / "100.lua").write_text('setManifestid(100, "200")\n', encoding="utf-8")
    depot_dir = tmp_path / "depotcache"
    depot_dir.mkdir()
    (depot_dir / "100_200.manifest").write_bytes(STEAM_MANIFEST_MAGIC + b"in-use")
    (depot_dir / "777_888.manifest").write_bytes(STEAM_MANIFEST_MAGIC + b"orphan")

    moved, _freed, backup_dir = cache.backup_and_delete(cache.scan())

    # 在用清单原地不动，只有真孤儿被移动
    assert (depot_dir / "100_200.manifest").is_file()
    assert (depot_dir / "100_200.manifest").read_bytes() == STEAM_MANIFEST_MAGIC + b"in-use"
    assert moved == 1
    assert {p.name for p in Path(backup_dir).glob("*.manifest")} == {"777_888.manifest"}


def test_backup_and_delete_moves_files_to_backup_dir(tmp_path):
    cache = ManifestCacheManager(str(tmp_path))
    depot_dir = tmp_path / "depotcache"
    depot_dir.mkdir()
    (depot_dir / "500_501.manifest").write_bytes(STEAM_MANIFEST_MAGIC + b"payload")

    records = cache.scan()
    moved, freed, backup_dir = cache.backup_and_delete(records)

    assert moved == 1
    assert freed > 0
    assert not (depot_dir / "500_501.manifest").exists()
    backup_path = Path(backup_dir)
    backups = list(backup_path.glob("500_501.manifest"))
    assert backups and backups[0].read_bytes().startswith(STEAM_MANIFEST_MAGIC)


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
