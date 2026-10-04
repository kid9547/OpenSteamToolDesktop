"""``gui.manifest_page`` 无头测试（游戏视角 v3）。

覆盖：扫描统计（以游戏为单位）、游戏表渲染、按游戏删除、
专家模式逐条清理、损坏识别、孤儿判定、双目录同步、手动导入。
``qapp`` fixture 由 ``tests/conftest.py`` 提供（会话级）。
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")

from core.manifest_downloader import STEAM_MANIFEST_MAGIC  # noqa: E402
from gui.manifest_page import ManifestPage, _human_size  # noqa: E402


class _FakeBridge:
    def __init__(self, steam_path: str, lua_dir: str):
        self._steam = steam_path
        self._lua = lua_dir

    def get_steam_path(self) -> str:
        return self._steam

    def get_lua_dir(self) -> str:
        return self._lua


class _FakeGameManager:
    def __init__(self, steam_path: str, lua_dir: str):
        self._steam_path = steam_path
        self._lua_dir = lua_dir

    def get_lua_dir(self) -> str:
        return self._lua_dir

    def get_games(self):
        return []


@pytest.fixture()
def steam_tree(tmp_path):
    """构造一个带双 depotcache + Lua + 已安装游戏的假 Steam 目录。"""
    steam = tmp_path / "steam"
    (steam / "depotcache").mkdir(parents=True)
    (steam / "config" / "depotcache").mkdir(parents=True)
    lua = steam / "config" / "lua"
    lua.mkdir(parents=True)

    # 游戏 100：有效且被 Lua 精确引用
    (lua / "100.lua").write_text(
        'addappid(100)\nsetManifestid(101, "200", 1024)\n', encoding="utf-8"
    )
    (steam / "depotcache" / "101_200.manifest").write_bytes(STEAM_MANIFEST_MAGIC + b"a" * 64)
    # 游戏 102：只在 config/depotcache 存在 → 需要镜像
    (steam / "config" / "depotcache" / "102_201.manifest").write_bytes(
        STEAM_MANIFEST_MAGIC + b"b" * 32
    )
    (lua / "102.lua").write_text('setManifestid(102, "201")\n', encoding="utf-8")
    # 已安装游戏 900：depot 901 属于 appmanifest（受保护，不属于任何游戏行）
    steamapps = steam / "steamapps"
    steamapps.mkdir()
    (steamapps / "appmanifest_900.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"900"\n\t"name"\t\t"Installed Game"\n'
        '\t"InstalledDepots"\n\t{\n\t\t"901"\t\t{ "manifest" "1" }\n\t}\n}\n',
        encoding="utf-8",
    )
    (steam / "depotcache" / "901_300.manifest").write_bytes(STEAM_MANIFEST_MAGIC + b"d" * 8)
    # 损坏文件（无归属）
    (steam / "depotcache" / "103_202.manifest").write_bytes(b"not-a-manifest")
    # 孤儿（无任何 Lua 引用、不属于已安装游戏）
    (steam / "depotcache" / "104_203.manifest").write_bytes(STEAM_MANIFEST_MAGIC + b"c" * 16)
    return steam, lua


def _page(qapp, steam_tree):
    steam, lua = steam_tree
    return ManifestPage(
        _FakeGameManager(str(steam), str(lua)),
        bridge=_FakeBridge(str(steam), str(lua)),
    )


def _scan(steam_tree) -> dict:
    steam, lua = steam_tree
    return ManifestPage._scan_worker(str(steam), [str(lua)], {"100": "Game One", "102": "Game Two"})


def test_human_size_formatting():
    assert _human_size(0) == "0 B"
    assert _human_size(512) == "512 B"
    assert _human_size(2048).endswith("KB")
    assert _human_size(5 * 1024 * 1024).endswith("MB")


def test_scan_worker_reports_games_with_names(qapp, steam_tree):
    """扫描结果必须以游戏为单位，且解析出人类可读的游戏名。"""
    result = _scan(steam_tree)
    games = {g["app_id"]: g for g in result["games"]}
    assert set(games) == {"100", "102"}
    assert games["100"]["name"] == "Game One"
    assert games["102"]["name"] == "Game Two"
    assert games["100"]["ok"] is True
    assert games["100"]["declared"] == 1
    assert games["100"]["file_count"] == 1


def test_scan_worker_game_centric_stats(qapp, steam_tree):
    result = _scan(steam_tree)
    assert result["total_games"] == 2
    assert result["ready_games"] == 2
    assert result["incomplete_games"] == 0
    # 孤儿：103_202（损坏且无归属）与 104_203；901_300 属于已安装游戏
    assert result["orphan_count"] == 2
    assert result["protected_installed"] == 1


def test_scan_worker_reports_incomplete_games(qapp, steam_tree):
    """缺清单的游戏必须体现在游戏行里 —— 这是补全入口的数据源。"""
    steam, lua = steam_tree
    (lua / "100.lua").write_text(
        'addappid(100)\nsetManifestid(101, "200", 1024)\nsetManifestid(999, "888")\n',
        encoding="utf-8",
    )
    result = ManifestPage._scan_worker(str(steam), [str(lua)], {})
    games = {g["app_id"]: g for g in result["games"]}
    assert games["100"]["ok"] is False
    assert games["100"]["missing_keys"] == ["999_888"]
    assert result["incomplete_games"] == 1
    assert result["ready_games"] == 1


def test_scan_worker_tolerates_missing_lua_dir(qapp, steam_tree):
    steam, _lua = steam_tree
    result = ManifestPage._scan_worker(str(steam), [], {})
    # 没有 Lua 时无法判定引用关系：游戏列表为空，除已安装保护外全部算残留
    assert result["games"] == []
    # 101_200, 102_201, 103_202, 104_203 无归属；901_300 属于已安装游戏受保护
    assert result["orphan_count"] == 4


def test_page_builds_and_reports_missing_steam_path(qapp, tmp_path):
    page = ManifestPage(_FakeGameManager("", ""), bridge=None)
    try:
        page._on_scan()
        assert "未检测到 Steam 路径" in page._status_label.text()
    finally:
        page.deleteLater()


def test_page_renders_game_table(qapp, steam_tree):
    page = _page(qapp, steam_tree)
    try:
        page._on_scan_done(_scan(steam_tree))

        assert page._stat_cards["games"].text() == "2"
        assert page._stat_cards["ready"].text() == "2"
        assert page._stat_cards["orphan"].text() == "2"
        assert page._game_table.rowCount() == 3  # 两个游戏 + 未归属残留行
        # 第一行是未归属残留
        assert "未归属残留" in page._game_table.item(0, 0).text()
        # 游戏行显示名字而不是裸 AppID
        assert page._game_table.item(1, 0).text() == "Game One"
    finally:
        page.deleteLater()


def test_expert_mode_hidden_by_default(qapp, steam_tree):
    page = _page(qapp, steam_tree)
    try:
        assert page._table.isHidden()
        assert page._select_btn.isHidden()
        page._expert_check.setChecked(True)
        assert not page._table.isHidden()
        assert not page._select_btn.isHidden()
    finally:
        page.deleteLater()


def test_sync_mirrors_single_copy_manifest(qapp, steam_tree):
    page = _page(qapp, steam_tree)
    try:
        steam, _lua = steam_tree
        page._on_sync_dual()
        assert (steam / "config" / "depotcache" / "101_200.manifest").is_file()
        assert (steam / "depotcache" / "102_201.manifest").is_file()
    finally:
        page.deleteLater()


def test_clean_orphans_moves_unreferenced_to_backup(qapp, steam_tree, monkeypatch):
    """孤儿清理 = 预览确认 + 移入备份目录（绝不直接删除）。"""
    page = _page(qapp, steam_tree)
    try:
        steam, _lua = steam_tree
        monkeypatch.setattr(page, "_confirm_orphan_cleanup", lambda *a, **k: True)
        page._on_clean_orphans()
        assert not (steam / "depotcache" / "104_203.manifest").exists()
        # 被 102.lua 引用的 102_201 必须保留
        assert (steam / "config" / "depotcache" / "102_201.manifest").is_file()
        # 已安装游戏 901 的清单必须保留
        assert (steam / "depotcache" / "901_300.manifest").is_file()
        backups = list((steam / "depotcache_backup").glob("*/104_203.manifest"))
        assert backups, "孤儿清单必须进入备份目录（可恢复）"
        assert backups[0].read_bytes().startswith(STEAM_MANIFEST_MAGIC)
    finally:
        page.deleteLater()


def test_clean_orphans_cancel_keeps_files(qapp, steam_tree, monkeypatch):
    page = _page(qapp, steam_tree)
    try:
        steam, _lua = steam_tree
        monkeypatch.setattr(page, "_confirm_orphan_cleanup", lambda *a, **k: False)
        page._on_clean_orphans()
        assert (steam / "depotcache" / "104_203.manifest").is_file()
    finally:
        page.deleteLater()


def test_clean_orphans_warns_without_lua_dir(qapp, steam_tree):
    steam, _lua = steam_tree
    page = ManifestPage(_FakeGameManager(str(steam), ""), bridge=_FakeBridge(str(steam), ""))
    try:
        page._on_clean_orphans()
        # 没有 Lua 目录时不应删除任何文件
        assert (steam / "depotcache" / "104_203.manifest").is_file()
    finally:
        page.deleteLater()


def test_delete_game_removes_only_that_games_manifests(qapp, steam_tree, monkeypatch):
    """按游戏删除：只移走该游戏声明的 depot，已安装 depot 保留，其余游戏不受影响。"""
    page = _page(qapp, steam_tree)
    try:
        steam, _lua = steam_tree
        monkeypatch.setattr(page, "_confirm_orphan_cleanup", lambda *a, **k: True)

        captured: dict = {}

        def fake_confirm(count, detail, hint):
            captured["count"] = count
            return True

        # MessageBox 由 _confirm_delete_game 承担 —— 覆写后验证 backup_and_delete 的调用
        monkeypatch.setattr(page, "_confirm_delete_game", lambda name, count, detail, kept: True)

        captured: dict = {}

        import core.manifest_cache as mc

        real_backup = mc.ManifestCacheManager.backup_and_delete

        def spy_backup(self, records, backup_root=None, allow_in_use=False):
            captured["keys"] = sorted(r.key for r in records)
            captured["allow_in_use"] = allow_in_use
            return real_backup(self, records, backup_root, allow_in_use)

        monkeypatch.setattr(mc.ManifestCacheManager, "backup_and_delete", spy_backup)
        page._on_delete_game("100", "Game One")

        assert captured["keys"] == ["101_200"]
        assert captured["allow_in_use"] is True
        assert not (steam / "depotcache" / "101_200.manifest").exists()
        # 其他游戏与已安装游戏的清单不受影响
        assert (steam / "config" / "depotcache" / "102_201.manifest").is_file()
        assert (steam / "depotcache" / "901_300.manifest").is_file()
    finally:
        page.deleteLater()


def test_import_rejects_bad_filename(qapp, steam_tree, tmp_path):
    page = _page(qapp, steam_tree)
    try:
        steam, _lua = steam_tree
        bad = tmp_path / "random.manifest"
        good = tmp_path / "105_204.manifest"
        bad.write_bytes(STEAM_MANIFEST_MAGIC + b"x")
        good.write_bytes(STEAM_MANIFEST_MAGIC + b"y")

        cache = page._cache()
        assert cache.write("105", "204", good.read_bytes()) != []
        assert cache.write("0", "0", b"garbage") == []
    finally:
        page.deleteLater()


def test_set_busy_toggles_all_action_buttons(qapp, steam_tree):
    page = _page(qapp, steam_tree)
    try:
        page._set_busy(True, "working")
        assert page._scan_btn.isEnabled() is False
        assert page._complete_btn.isEnabled() is False
        page._set_busy(False, "done")
        assert page._scan_btn.isEnabled() is True
        assert page._orphan_btn.isEnabled() is True
    finally:
        page.deleteLater()
