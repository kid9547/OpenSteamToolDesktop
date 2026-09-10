"""
Unit tests for ImportService
"""
import os
import tempfile
import zipfile
from pathlib import Path
import pytest

from core.import_service import ImportService, ImportResult
from core.game_manager import LuaGameManager


@pytest.fixture
def temp_steam_dir(tmp_path):
    steam = tmp_path / "Steam"
    steam.mkdir()
    (steam / "config" / "lua").mkdir(parents=True)
    (steam / "depotcache").mkdir()
    (steam / "config" / "depotcache").mkdir(parents=True)
    return steam


def test_import_single_lua_numeric(temp_steam_dir):
    gm = LuaGameManager(str(temp_steam_dir / "config" / "lua"))
    service = ImportService(str(temp_steam_dir), gm)

    src_lua = temp_steam_dir.parent / "730.lua"
    src_lua.write_text('-- Counter-Strike 2\naddappid(730)\n', encoding='utf-8')

    result = service.import_paths([src_lua])
    assert result.success is True
    assert result.lua_count == 1
    assert result.manifest_count == 0
    assert "730" in result.app_ids
    assert (temp_steam_dir / "config" / "lua" / "730.lua").exists()


def test_import_lua_non_numeric_extracts_appid(temp_steam_dir):
    gm = LuaGameManager(str(temp_steam_dir / "config" / "lua"))
    service = ImportService(str(temp_steam_dir), gm)

    src_lua = temp_steam_dir.parent / "Cyberpunk2077_Custom.lua"
    src_lua.write_text('-- Cyberpunk 2077\naddappid(1091500, 0, "secretkey")\n', encoding='utf-8')

    result = service.import_paths([src_lua])
    assert result.success is True
    assert result.lua_count == 1
    assert "1091500" in result.app_ids
    assert (temp_steam_dir / "config" / "lua" / "1091500.lua").exists()


def test_import_manifest_and_directory(temp_steam_dir):
    service = ImportService(str(temp_steam_dir))

    # Create folder structure with manifest and lua
    import_dir = temp_steam_dir.parent / "downloads"
    nested_dir = import_dir / "nested"
    nested_dir.mkdir(parents=True)

    manifest1 = import_dir / "1091501_8472918237192.manifest"
    manifest1.write_bytes(b"\x01\x02\x03\x04")

    manifest2 = nested_dir / "Game_200001_9999999999.manifest"
    manifest2.write_bytes(b"\x05\x06\x07\x08")

    lua1 = nested_dir / "200000.lua"
    lua1.write_text('addappid(200000)', encoding='utf-8')

    result = service.import_paths([import_dir])
    assert result.success is True
    assert result.lua_count == 1
    assert result.manifest_count == 2
    assert (temp_steam_dir / "depotcache" / "1091501_8472918237192.manifest").exists()
    assert (temp_steam_dir / "depotcache" / "200001_9999999999.manifest").exists()
    assert (temp_steam_dir / "config" / "depotcache" / "1091501_8472918237192.manifest").exists()


def test_import_zip_file(temp_steam_dir):
    service = ImportService(str(temp_steam_dir))

    zip_path = temp_steam_dir.parent / "package.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("subfolder/300000.lua", "-- Test Game\naddappid(300000)")
        zf.writestr("300001_1111111.manifest", b"manifestdata")

    result = service.import_paths([zip_path])
    assert result.success is True
    assert result.lua_count == 1
    assert result.manifest_count == 1
    assert (temp_steam_dir / "config" / "lua" / "300000.lua").exists()
    assert (temp_steam_dir / "depotcache" / "300001_1111111.manifest").exists()


def test_import_invalid_steam_path(tmp_path):
    service = ImportService(str(tmp_path / "non_existent"))
    result = service.import_paths([tmp_path])
    assert result.success is False
    assert len(result.errors) > 0
