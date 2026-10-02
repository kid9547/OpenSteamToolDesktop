"""``gui.manifest_page`` 无头测试。

覆盖清单扫描统计、损坏识别、孤儿判定、双目录同步、清理与手动导入。
``qapp`` fixture 由 ``tests/conftest.py`` 提供（会话级，避免 QConfig 单例被回收）。
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


@pytest.fixture()
def steam_tree(tmp_path):
    """构造一个带双 depotcache + Lua 的假 Steam 目录。"""
    steam = tmp_path / "steam"
    (steam / "depotcache").mkdir(parents=True)
    (steam / "config" / "depotcache").mkdir(parents=True)
    lua = steam / "config" / "lua"
    lua.mkdir(parents=True)

    # 有效且被 Lua 引用
    (lua / "100.lua").write_text(
        'addappid(100)\nsetManifestid(101, "200", 1024)\n', encoding="utf-8"
    )
    (steam / "depotcache" / "101_200.manifest").write_bytes(STEAM_MANIFEST_MAGIC + b"a" * 64)
    # 只在 config/depotcache 存在 → 需要镜像
    (steam / "config" / "depotcache" / "102_201.manifest").write_bytes(
        STEAM_MANIFEST_MAGIC + b"b" * 32
    )
    (lua / "102.lua").write_text('setManifestid(102, "201")\n', encoding="utf-8")
    # 损坏文件
    (steam / "depotcache" / "103_202.manifest").write_bytes(b"not-a-manifest")
    # 孤儿（无任何 Lua 引用）
    (steam / "depotcache" / "104_203.manifest").write_bytes(STEAM_MANIFEST_MAGIC + b"c" * 16)
    return steam, lua


def _page(qapp, steam_tree):
    steam, lua = steam_tree
    return ManifestPage(
        _FakeGameManager(str(steam), str(lua)),
        bridge=_FakeBridge(str(steam), str(lua)),
    )


def test_human_size_formatting():
    assert _human_size(0) == "0 B"
    assert _human_size(512) == "512 B"
    assert _human_size(2048).endswith("KB")
    assert _human_size(5 * 1024 * 1024).endswith("MB")


def test_scan_counts_valid_invalid_and_orphans(qapp, steam_tree):
    page = _page(qapp, steam_tree)
    try:
        steam, lua = steam_tree
        result = ManifestPage._scan_worker(str(steam), str(lua))

        assert result["total"] == 4
        assert result["valid"] == 3
        assert result["invalid"] == 1
        assert result["orphan"] == 2          # 103_202 与 104_203
        assert result["size"] > 0
        keys = {row["key"] for row in result["rows"]}
        assert keys == {"101_200", "102_201", "103_202", "104_203"}
    finally:
        page.deleteLater()


def test_scan_marks_single_copy_rows(qapp, steam_tree):
    steam, lua = steam_tree
    result = ManifestPage._scan_worker(str(steam), str(lua))
    by_key = {row["key"]: row for row in result["rows"]}
    assert by_key["101_200"]["copies"] == 1
    assert by_key["102_201"]["copies"] == 1


def test_scan_worker_tolerates_missing_lua_dir(qapp, steam_tree):
    steam, _lua = steam_tree
    result = ManifestPage._scan_worker(str(steam), "")
    # 没有 Lua 时无法判定引用关系，全部视为孤儿
    assert result["orphan"] == result["total"]


def test_page_builds_and_reports_missing_steam_path(qapp, tmp_path):
    page = ManifestPage(_FakeGameManager("", ""), bridge=None)
    try:
        page._on_scan()
        assert "未检测到 Steam 路径" in page._status_label.text()
    finally:
        page.deleteLater()


def test_sync_mirrors_single_copy_manifest(qapp, steam_tree):
    page = _page(qapp, steam_tree)
    try:
        steam, _lua = steam_tree
        page._on_sync_dual()
        # 两个单份清单都应被镜像到另一目录
        assert (steam / "config" / "depotcache" / "101_200.manifest").is_file()
        assert (steam / "depotcache" / "102_201.manifest").is_file()
    finally:
        page.deleteLater()


def test_delete_invalid_removes_broken_file(qapp, steam_tree):
    page = _page(qapp, steam_tree)
    try:
        steam, _lua = steam_tree
        page._on_delete_invalid()
        assert not (steam / "depotcache" / "103_202.manifest").exists()
        # 有效清单不受影响
        assert (steam / "depotcache" / "101_200.manifest").is_file()
    finally:
        page.deleteLater()


def test_clean_orphans_removes_only_unreferenced(qapp, steam_tree):
    page = _page(qapp, steam_tree)
    try:
        steam, _lua = steam_tree
        page._on_clean_orphans()
        assert not (steam / "depotcache" / "104_203.manifest").exists()
        # 被 102.lua 引用的 102_201 必须保留
        assert (steam / "config" / "depotcache" / "102_201.manifest").is_file()
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
        # 文件名非法时必须拒绝
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
