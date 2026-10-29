"""``core.lua_rules`` 单元测试：扫描 / 冲突 / 启停 / 缓存补全。"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import lua_rules, manifest_pins


ACF_TEMPLATE = """\"AppState\"
{{
    \"appid\"\t\t\"{app_id}\"
    \"name\"\t\t\"Test Game\"
    \"InstalledDepots\"
    {{
        \"501\"
        {{
            \"manifest\"\t\t\"123\"
            \"size\"\t\t\"456\"
        }}
    }}
}}
"""


def _make_tree(tmp_path: Path) -> Path:
    steam = tmp_path / "steam"
    lua = steam / "config" / "lua"
    stplug = steam / "config" / "stplug-in"
    lua.mkdir(parents=True)
    stplug.mkdir(parents=True)
    (steam / "steamapps").mkdir(parents=True)
    (steam / "depotcache").mkdir()
    (steam / "depotcache" / "501_123.manifest").write_bytes(b"\xd0\x17\xf6\x71data")
    return steam


def test_scan_detects_duplicate_enabled_app(tmp_path):
    steam = _make_tree(tmp_path)
    (steam / "config" / "lua" / "500.lua").write_text(
        "-- Test Game\naddappid(500)\n", encoding="utf-8"
    )
    (steam / "config" / "stplug-in" / "500.lua").write_text(
        "-- Test Game Copy\naddappid(500)\n", encoding="utf-8"
    )
    (steam / "config" / "lua" / "600.lua.disabled").write_text(
        "-- Disabled Game\naddappid(600)\n", encoding="utf-8"
    )

    rules = lua_rules.scan_lua_rules(str(steam))

    by_app = {}
    for rule in rules:
        by_app.setdefault(rule.app_id, []).append(rule)
    assert len(by_app["500"]) == 2
    assert all(rule.conflicts for rule in by_app["500"])
    assert by_app["600"][0].enabled is False
    assert by_app["600"][0].conflicts == []
    assert by_app["500"][0].name in {"Test Game", "Test Game Copy"}


def test_set_enabled_renames_and_restores(tmp_path):
    steam = _make_tree(tmp_path)
    lua = steam / "config" / "lua" / "600.lua"
    lua.write_text("addappid(600)\n", encoding="utf-8")

    disabled = lua_rules.set_enabled(lua, False)
    assert disabled.name == "600.lua.disabled"
    assert disabled.is_file()

    enabled = lua_rules.set_enabled(disabled, True)
    assert enabled.name == "600.lua"
    assert enabled.is_file()


def test_set_enabled_rejects_existing_target(tmp_path):
    steam = _make_tree(tmp_path)
    lua = steam / "config" / "lua" / "600.lua"
    lua.write_text("addappid(600)\n", encoding="utf-8")
    (steam / "config" / "lua" / "600.lua.disabled").write_text(
        "addappid(600)\n", encoding="utf-8"
    )

    with pytest.raises(lua_rules.LuaRuleError):
        lua_rules.set_enabled(lua, False)


def test_cache_bindings_reads_acf_and_reuses_key(tmp_path):
    steam = _make_tree(tmp_path)
    (steam / "steamapps" / "appmanifest_500.acf").write_text(
        ACF_TEMPLATE.format(app_id="500"), encoding="utf-8"
    )
    (steam / "config" / "lua" / "600.lua").write_text(
        'addappid(501, 0, "abcdef1234567890")\n', encoding="utf-8"
    )

    bindings = lua_rules.cache_bindings(str(steam), "500")

    assert bindings.has_data is True
    assert bindings.acf_path.endswith("appmanifest_500.acf")
    assert bindings.depots == [
        {
            "depot_id": "501",
            "gid": "123",
            "size": 456,
            "key": "abcdef1234567890",
            "present": True,
        }
    ]


def test_apply_cache_bindings_writes_addappid_and_manifest(tmp_path, monkeypatch):
    steam = _make_tree(tmp_path)
    (steam / "steamapps" / "appmanifest_500.acf").write_text(
        ACF_TEMPLATE.format(app_id="500"), encoding="utf-8"
    )
    (steam / "config" / "lua" / "600.lua").write_text(
        'addappid(501, 0, "abcdef1234567890")\n', encoding="utf-8"
    )
    lua = steam / "config" / "lua" / "500.lua"
    lua.write_text("-- Test Game\naddappid(500)\n", encoding="utf-8")
    bindings = lua_rules.cache_bindings(str(steam), "500")

    summary = lua_rules.apply_cache_bindings(lua, bindings)

    content = lua.read_text(encoding="utf-8")
    assert summary["added"] == 1
    assert summary["manifests"] == 1
    assert 'addappid(501, 0, "abcdef1234567890")' in content
    assert 'setManifestid(501, "123", 456)' in content
    assert (lua.parent / "500.lua.bak").is_file()


def test_apply_cache_bindings_respects_pin(tmp_path, monkeypatch):
    steam = _make_tree(tmp_path)
    (steam / "steamapps" / "appmanifest_500.acf").write_text(
        ACF_TEMPLATE.format(app_id="500"), encoding="utf-8"
    )
    lua = steam / "config" / "lua" / "500.lua"
    lua.write_text('-- Test Game\naddappid(500)\nsetManifestid(501, "999")\n', encoding="utf-8")
    bindings = lua_rules.cache_bindings(str(steam), "500")

    store = tmp_path / "pins.json"
    monkeypatch.setattr(manifest_pins, "_store_file", lambda _path=None: store)
    manifest_pins.pin("500", "501", "999")

    lua_rules.apply_cache_bindings(lua, bindings)

    content = lua.read_text(encoding="utf-8")
    assert 'setManifestid(501, "999")' in content
    assert '"123"' not in content
