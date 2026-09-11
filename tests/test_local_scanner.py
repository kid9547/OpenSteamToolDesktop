"""
测试 core.local_scanner — 本地磁盘扫描器与第三方工具兼容
"""
import json
import os
import tempfile
from pathlib import Path

import pytest

from core.local_scanner import LocalGameScanner, parse_vdf
from core.game_manager import LuaGameManager


class TestVDFParser:
    def test_parse_simple_vdf(self):
        text = '''
        "AppState"
        {
            "appid" "1623730"
            "name" "Palworld"
            // comment line
            "InstalledDepots"
            {
                "1623731"
                {
                    "manifest" "868868087024202254"
                }
            }
        }
        '''
        res = parse_vdf(text)
        assert "AppState" in res
        app_state = res["AppState"]
        assert app_state["appid"] == "1623730"
        assert app_state["name"] == "Palworld"
        assert "1623731" in app_state["InstalledDepots"]
        assert app_state["InstalledDepots"]["1623731"]["manifest"] == "868868087024202254"


class TestLocalGameScanner:
    @pytest.fixture
    def mock_steam_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            steam_root = Path(tmp)
            steamapps = steam_root / "steamapps"
            steamapps.mkdir(parents=True, exist_ok=True)
            depotcache = steam_root / "depotcache"
            depotcache.mkdir(parents=True, exist_ok=True)
            lua_dir = steam_root / "config" / "lua"
            lua_dir.mkdir(parents=True, exist_ok=True)

            # 写一个 appmanifest
            acf_content = '''
            "AppState"
            {
                "appid" "2739590"
                "name" "Mad Island"
                "installdir" "Mad Island"
                "SizeOnDisk" "1119946120"
                "InstalledDepots"
                {
                    "2739591"
                    {
                        "manifest" "3631136809996453116"
                    }
                }
            }
            '''
            (steamapps / "appmanifest_2739590.acf").write_text(acf_content, encoding="utf-8")

            # 写对应的清单文件到 depotcache
            (depotcache / "2739591_3631136809996453116.manifest").write_bytes(b"data")

            # 写一个 SteamTools st.json
            st_data = {
                "apps": {
                    "99999": {
                        "name": "SteamTools Demo Game",
                        "depots": {
                            "99991": {"manifest": "88888888"}
                        }
                    }
                }
            }
            (steam_root / "config" / "st.json").write_text(json.dumps(st_data), encoding="utf-8")

            # 写一个非纯数字命名的外部 Lua 文件
            ext_lua = "-- External RPG\naddappid(55555)\nsetManifestid(55556, \"777777\")\n"
            (lua_dir / "External_RPG.lua").write_text(ext_lua, encoding="utf-8")

            yield str(steam_root)

    def test_scan_installed_games(self, mock_steam_env):
        scanner = LocalGameScanner(mock_steam_env)
        games = scanner.scan_installed_games(scan_all_drives=False)
        assert len(games) == 1
        g = games[0]
        assert g["app_id"] == "2739590"
        assert g["name"] == "Mad Island"
        assert g["manifest_ready"] is True
        assert ("2739591", "3631136809996453116") in g["depots"]

    def test_scan_steamtools_games(self, mock_steam_env):
        scanner = LocalGameScanner(mock_steam_env)
        games = scanner.scan_steamtools_games()
        assert len(games) == 1
        g = games[0]
        assert g["app_id"] == "99999"
        assert g["name"] == "SteamTools Demo Game"

    def test_scan_external_lua_files(self, mock_steam_env):
        scanner = LocalGameScanner(mock_steam_env)
        games = scanner.scan_external_lua_files()
        assert len(games) == 1
        g = games[0]
        assert g["app_id"] == "55555"
        assert g["name"] == "External RPG"

    def test_take_over_game_as_ost_lua(self, mock_steam_env):
        scanner = LocalGameScanner(mock_steam_env)
        ok = scanner.take_over_game_as_ost_lua(
            app_id="2739590",
            name="Mad Island",
            depots=[("2739591", "3631136809996453116")],
        )
        assert ok is True
        target_lua = Path(mock_steam_env) / "config" / "lua" / "2739590.lua"
        assert target_lua.exists()
        content = target_lua.read_text(encoding="utf-8")
        assert "addappid(2739590)" in content
        assert 'setManifestid(2739591, "3631136809996453116")' in content

    def test_gamemanager_deep_scan_and_take_over(self, mock_steam_env):
        lua_dir = os.path.join(mock_steam_env, "config", "lua")
        gm = LuaGameManager(lua_dir=lua_dir, steam_path=mock_steam_env)

        # 默认 refresh() 仅扫描标准 Lua（此时未接管 Mad Island，且 External_RPG 是非数字命名）
        assert len(gm.refresh(scan_local=False)) == 0

        # deep_scan() 发现所有安装游戏与外部工具游戏
        all_games = gm.deep_scan(scan_all_drives=False)
        app_ids = {g.app_id for g in all_games}
        assert "2739590" in app_ids
        assert "99999" in app_ids
        assert "55555" in app_ids

        # 执行接管
        took_over = gm.take_over_game("2739590")
        # 接管后标准 refresh 即可直接读取该游戏
        refreshed = gm.refresh(scan_local=False)
        assert any(g.app_id == "2739590" and g.has_lua for g in refreshed)

    def test_scan_with_hidden_app_ids(self, mock_steam_env):
        scanner = LocalGameScanner(mock_steam_env)
        hidden = {"2739590", "99999"}
        games = scanner.scan_installed_games(hidden_app_ids=hidden)
        assert not any(g["app_id"] == "2739590" for g in games)

        st_games = scanner.scan_steamtools_games(hidden_app_ids=hidden)
        assert not any(g["app_id"] == "99999" for g in st_games)

    def test_remove_acf_and_steamtools(self, mock_steam_env):
        scanner = LocalGameScanner(mock_steam_env)
        acf_file = Path(mock_steam_env) / "steamapps" / "appmanifest_2739590.acf"
        assert acf_file.exists()
        ok = scanner.remove_acf("2739590")
        assert ok is True
        assert not acf_file.exists()

        st_file = Path(mock_steam_env) / "config" / "st.json"
        assert "99999" in st_file.read_text(encoding="utf-8")
        ok_st = scanner.remove_from_steamtools("99999")
        assert ok_st is True
        assert "99999" not in st_file.read_text(encoding="utf-8")

    def test_gamemanager_remove_and_hide_acf_game(self, mock_steam_env):
        lua_dir = os.path.join(mock_steam_env, "config", "lua")
        gm = LuaGameManager(lua_dir=lua_dir, steam_path=mock_steam_env)

        # 扫描得到 2739590
        games = gm.refresh(scan_local=True)
        assert any(g.app_id == "2739590" for g in games)

        # 出库并删除 ACF
        removed = gm.remove_game("2739590", delete_acf=True)
        assert removed is True
        assert "2739590" in gm.get_hidden_app_ids()

        # 再次刷新，2739590 绝不再出现
        games_after = gm.refresh(scan_local=True)
        assert not any(g.app_id == "2739590" for g in games_after)

        # 恢复显示并重新创建 ACF 测试 unhide
        gm.unhide_game("2739590")
        assert "2739590" not in gm.get_hidden_app_ids()

