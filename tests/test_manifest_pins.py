"""``core.manifest_pins`` 单元测试：固定 / 取消 / 写回 Lua / 阻止补全覆盖。"""
from __future__ import annotations

from pathlib import Path

from core import manifest_pins
from core.complete_manifest_service import update_lua_manifest
from core.game_manager import GameMetadata, LuaGameManager, DepotInfo


def _store(tmp_path: Path, monkeypatch):
    store = tmp_path / "manifest_pins.json"
    monkeypatch.setattr(manifest_pins, "_store_file", lambda _path=None: store)
    return store


def test_pin_and_unpin_roundtrip(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)

    assert manifest_pins.pin("100", "101", "123456", size=42) is True
    pins = manifest_pins.get_pins("100")
    assert pins["101"]["gid"] == "123456"
    assert pins["101"]["size"] == 42
    assert manifest_pins.has_pins("100") is True
    assert manifest_pins.pinned_gid("100", "101") == "123456"

    assert manifest_pins.unpin("100", "101") == 1
    assert manifest_pins.has_pins("100") is False


def test_apply_pins_rewrites_lua(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    lua = tmp_path / "100.lua"
    lua.write_text(
        'addappid(100)\naddappid(101)\nsetManifestid(101, "111", 10)\n',
        encoding="utf-8",
    )
    manifest_pins.pin("100", "101", "222", size=20)

    changed = manifest_pins.apply_pins_to_lua("100", lua)

    content = lua.read_text(encoding="utf-8")
    assert changed == 1
    assert 'setManifestid(101, "222", 20)' in content
    assert "111" not in content


def test_update_lua_manifest_respects_pin(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    lua = tmp_path / "100.lua"
    lua.write_text('addappid(101)\nsetManifestid(101, "111")\n', encoding="utf-8")
    manifest_pins.pin("100", "101", "222")

    ok = update_lua_manifest(lua, "101", "333")

    assert ok is True
    content = lua.read_text(encoding="utf-8")
    assert 'setManifestid(101, "222")' in content
    assert "333" not in content


def test_update_lua_manifest_without_pin_uses_new_gid(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    lua = tmp_path / "100.lua"
    lua.write_text('addappid(101)\nsetManifestid(101, "111")\n', encoding="utf-8")

    ok = update_lua_manifest(lua, "101", "333")

    assert ok is True
    assert 'setManifestid(101, "333")' in lua.read_text(encoding="utf-8")


def test_build_lua_content_uses_pinned_gid(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    manifest_pins.pin("100", "101", "222", size=20)
    metadata = GameMetadata(
        app_id="100",
        name="Pinned Game",
        depots=[DepotInfo(depot_id="101", manifest_gid="111", size=10)],
    )

    content = LuaGameManager._build_lua_content(metadata)

    assert 'setManifestid(101, "222", 20)' in content
    assert '"111"' not in content
