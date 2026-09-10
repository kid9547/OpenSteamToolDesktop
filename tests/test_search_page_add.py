import os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication

_app = QApplication.instance()
if not _app:
    _app = QApplication([])

from core.steam_bridge import SteamBridge
from core.game_manager import LuaGameManager
from core.app_state import app_state, STEAM_PATH, DLL_VERSION_MISMATCH
from gui.search_page import SearchPage

from unittest.mock import patch

class TestSearchPageAddGame(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.steam_dir = os.path.join(self.tmpdir.name, 'steam')
        self.lua_dir = os.path.join(self.steam_dir, 'config', 'lua')
        os.makedirs(self.lua_dir, exist_ok=True)
        with open(os.path.join(self.steam_dir, 'steam.exe'), 'w') as f:
            f.write('mock')

        app_state.set(STEAM_PATH, self.steam_dir)
        app_state.set(DLL_VERSION_MISMATCH, False)

        self.bridge = SteamBridge()
        self.bridge.set_steam_path(self.steam_dir)
        self.gm = LuaGameManager(self.lua_dir)
        self.page = SearchPage(self.gm, bridge=self.bridge)
        self.page._rec_timer.stop()

    def tearDown(self):
        self.page.cleanup()
        self.tmpdir.cleanup()

    @patch("gui.search_page.InfoBar")
    @patch("gui.search_page.AsyncWorker")
    def test_on_add_game_palworld_creates_file_immediately(self, mock_worker, mock_info_bar):
        self.page._on_add_game('1623730', 'Palworld')
        lua_file = os.path.join(self.lua_dir, '1623730.lua')
        self.assertTrue(os.path.isfile(lua_file))
        with open(lua_file, 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertIn('addappid(1623730)', content)
        self.assertIn('Palworld', content)
        self.assertTrue(os.path.isfile(os.path.join(self.lua_dir, 'manifest.lua')))

    @patch("gui.search_page.InfoBar")
    @patch("gui.search_page.AsyncWorker")
    def test_on_add_game_recommended_creates_file_immediately(self, mock_worker, mock_info_bar):
        self.page._on_add_game('4001890', 'How to Fish')
        lua_file = os.path.join(self.lua_dir, '4001890.lua')
        self.assertTrue(os.path.isfile(lua_file))
        with open(lua_file, 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertIn('addappid(4001890)', content)

if __name__ == '__main__':
    unittest.main()

