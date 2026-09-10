"""
LibraryPage — 已入库游戏页面

独立选项卡，以列表形式展示已入库的游戏。
支持搜索过滤、排序、刷新、出库操作。
每次切换到此页面时自动刷新。

已移除 QNetworkAccessManager，使用 AsyncWorker+httpx 替代，避免生命周期崩溃
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QDialog, QFormLayout, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QListWidget, QListWidgetItem,
    QGroupBox, QMessageBox, QAbstractItemView, QFileDialog,
)

from qfluentwidgets import (
    ScrollArea, SubtitleLabel, CaptionLabel, BodyLabel,
    PrimaryPushButton, PushButton, SearchLineEdit,
    ComboBox, TransparentToolButton, DropDownPushButton,
    RoundMenu, Action,
    InfoBar, InfoBarPosition, FluentIcon,
    ToolTipFilter, ToolTipPosition,
    MessageBox,
)

from core.import_service import ImportService

from core.game_manager import LuaGameManager, GameInfo, GameMetadata, DepotInfo
from gui.widgets import GameCard
from gui.edit_game_dialog import EditGameDialog
from utils.async_worker import AsyncWorker

from config import STEAM_STORE_API
from utils.logger import setup_logger
from core.app_state import app_state, DLL_VERSION_MISMATCH
logger = setup_logger(__name__)


def _fetch_game_name(app_id: str) -> tuple[str, str]:
    """后台线程：通过 Steam API 获取游戏名称

    Returns:
        (app_id, name) 元组，获取失败时 name 为空字符串
    """
    from utils.http_client import get_json
    url = f"{STEAM_STORE_API}?appids={app_id}&l=zh-CN"
    data = get_json(url, timeout=8.0)
    if data and app_id in data and data[app_id].get("success"):
        name = data[app_id].get("data", {}).get("name", "")
        return (app_id, name)
    return (app_id, "")






class LibraryPage(ScrollArea):
    """已入库游戏页面"""

    # 信号：游戏库发生变化（出库），通知搜索页刷新推荐
    library_changed = pyqtSignal()

    def __init__(
        self,
        game_manager: LuaGameManager,
        bridge=None,
        parent=None,
    ):
        super().__init__(parent)
        self._game_manager = game_manager
        self._bridge = bridge
        self._sort_mode = "default"
        self._games_data: list[GameInfo] = []
        self._card_list: list[GameCard] = []
        self._alive = True  # 安全标志

        # 初始化清单与 Lua 导入服务
        steam_path = self._bridge.get_steam_path() if self._bridge else ""
        self._import_service = ImportService(steam_path, self._game_manager)

        # 异步 Worker 引用（防止回调到已删除对象）
        self._load_worker = None
        self._name_workers: list[AsyncWorker] = []

        self.setObjectName("libraryPage")
        self.setWidgetResizable(True)
        self.setAcceptDrops(True)

        self._container = QWidget()
        self._container.setObjectName("libraryContainer")
        self.setWidget(self._container)
        self._main_layout = QVBoxLayout(self._container)
        self._main_layout.setContentsMargins(30, 30, 30, 30)
        self._main_layout.setSpacing(16)

        self._init_ui()

        self.setStyleSheet("LibraryPage { background: transparent; }")
        self._container.setStyleSheet(
            "QWidget#libraryContainer { background: transparent; }"
        )

    # ---- UI 构建 ----

    def _init_ui(self):
        # 标题行
        header = QHBoxLayout()
        header.addWidget(SubtitleLabel("已入库的游戏", self))

        self.stats_label = CaptionLabel("", self)
        self.stats_label.setTextColor("#606060", "#d2d2d2")
        header.addStretch(1)
        header.addWidget(self.stats_label)

        # 导入清单 / Lua 下拉按钮
        self.import_btn = DropDownPushButton(FluentIcon.FOLDER_ADD, "导入清单/Lua", self)
        import_menu = RoundMenu(parent=self.import_btn)
        action_files = Action(FluentIcon.DOCUMENT, "选择文件导入 (.manifest / .lua / .zip)", self)
        action_files.triggered.connect(self._on_import_files_clicked)
        action_folder = Action(FluentIcon.FOLDER, "选择文件夹导入 (批量扫描目录)", self)
        action_folder.triggered.connect(self._on_import_folder_clicked)
        import_menu.addAction(action_files)
        import_menu.addAction(action_folder)
        import_menu.addSeparator()
        action_open_depot = Action(FluentIcon.FOLDER, "打开清单目录 (depotcache)", self)
        action_open_depot.triggered.connect(self._open_depotcache_dir)
        action_open_lua = Action(FluentIcon.CODE, "打开 Lua 配置目录", self)
        action_open_lua.triggered.connect(self._open_lua_dir)
        action_clean_all = Action(FluentIcon.BROOM, "清理 Steam 异常下载残留缓存", self)
        action_clean_all.triggered.connect(self._on_clean_all_download_cache)
        import_menu.addAction(action_open_depot)
        import_menu.addAction(action_open_lua)
        import_menu.addAction(action_clean_all)
        self.import_btn.setMenu(import_menu)
        header.addWidget(self.import_btn)

        self.refresh_btn = TransparentToolButton(FluentIcon.SYNC, self)
        self.refresh_btn.setFixedSize(32, 32)
        self.refresh_btn.setToolTip("刷新")
        self.refresh_btn.installEventFilter(
            ToolTipFilter(self.refresh_btn, showDelay=150, position=ToolTipPosition.TOP)
        )
        self.refresh_btn.clicked.connect(self._load_games_async)
        header.addWidget(self.refresh_btn)

        self._main_layout.addLayout(header)

        # 搜索 + 排序
        toolbar = QHBoxLayout()

        self.filter_input = SearchLineEdit(self)
        self.filter_input.setPlaceholderText("搜索游戏名称或 AppID...")
        self.filter_input.setFixedHeight(35)
        self.filter_input.textChanged.connect(self._on_filter)
        self.filter_input.clearSignal.connect(self._on_filter_clear)
        toolbar.addWidget(self.filter_input, 1)

        # 排序
        self.sort_combo = ComboBox(self)
        self.sort_combo.addItems(["默认", "A-Z", "Z-A"])
        self.sort_combo.setFixedWidth(100)
        self.sort_combo.currentIndexChanged.connect(self._on_sort)
        toolbar.addWidget(self.sort_combo)

        self._main_layout.addLayout(toolbar)

        # 游戏列表容器（纵向列表）
        self._list_layout = QVBoxLayout()
        self._list_layout.setSpacing(8)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._main_layout.addLayout(self._list_layout)

        # 空状态提示
        self.empty_label = BodyLabel("暂无入库游戏", self)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setVisible(False)
        self._main_layout.addWidget(self.empty_label)

        self._main_layout.addStretch(1)

    # ---- 页面显示刷新 ----

    def _check_injection_required(self) -> bool:
        """检查注入状态（不再阻止用户浏览与管理已入库游戏）"""
        if self._bridge:
            return self._bridge.is_deployed() or self._bridge.is_connected()
        return True

    def showEvent(self, event):
        """每次切换到此页面时自动刷新"""
        super().showEvent(event)
        self._alive = True
        self._load_games_async()

        # 如果未注入 DLL，仅给出温和提示，不阻止游戏浏览与管理
        if self._bridge and not self._bridge.is_deployed():
            InfoBar.info(
                "未注入 Steam",
                "提示：当前尚未注入 Steam，请在「注入管理」完成注入以使游戏在 Steam 中生效",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000,
            )
        
        # 检查 DLL 版本是否不匹配，如果是则显示警告
        if app_state.get(DLL_VERSION_MISMATCH):
            InfoBar.warning(
                "DLL 版本警告",
                "当前 DLL 不是最新版本，建议更新后再使用",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )

    def _show_not_injected(self):
        """未注入状态展示（兼容保留）"""
        self._load_games_async()


    # ---- DLL 版本检查 ----

    def _check_dll_version_mismatch(self):
        """检查 DLL 版本是否匹配，如果不匹配则提示用户"""
        if self._bridge is None:
            return
        
        try:
            mismatch, mismatched_dlls = self._bridge.check_dll_version_mismatch()
            if mismatch:
                msg = "检测到 DLL 文件版本不匹配：\n\n"
                msg += "\n".join([f"• {dll}" for dll in mismatched_dlls])
                msg += "\n\n是否立即更新注入？"
                
                msg_box = MessageBox(
                    "DLL 版本不匹配",
                    msg,
                    self
                )
                msg_box.yesButton.setText("立即更新")
                msg_box.cancelButton.setText("稍后提醒")
                
                if msg_box.exec():
                    self._update_and_inject()
        except Exception as e:
            logger.error(f"DLL 版本检查失败: {e}")

    def _update_and_inject(self):
        """更新 DLL 并重新注入"""
        if self._bridge is None:
            return
        
        # 显示进度提示
        from qfluentwidgets import StateToolTip
        self._inject_progress = StateToolTip(
            "正在更新",
            "正在更新 DLL 并重新注入...",
            self
        )
        self._inject_progress.show()
        
        # 1. 关闭 Steam
        self._do_kill_steam()
    
    def _do_kill_steam(self):
        """关闭 Steam"""
        if self._bridge is None:
            return
        
        success, msg = self._bridge.kill_steam()
        if success:
            # 等待 Steam 关闭
            QTimer.singleShot(2000, self._do_inject)
        else:
            if hasattr(self, '_inject_progress') and self._inject_progress:
                self._inject_progress.close()
            InfoBar.error(
                "错误",
                f"关闭 Steam 失败: {msg}",
                parent=self,
                position=InfoBarPosition.TOP
            )
    
    def _do_inject(self):
        """执行注入"""
        if self._bridge is None:
            return
        
        success, msg = self._bridge.inject()
        if hasattr(self, '_inject_progress') and self._inject_progress:
            self._inject_progress.close()
        
        if success:
            InfoBar.success(
                "更新成功",
                "DLL 已更新并重新注入，请重启 Steam",
                parent=self,
                position=InfoBarPosition.TOP
            )
            # 提示用户重启 Steam（使用 MessageBox 保持样式统一）
            msg_box = MessageBox(
                "重启 Steam",
                "DLL 已更新并重新注入，是否立即重启 Steam？",
                self
            )
            msg_box.yesButton.setText("立即重启")
            msg_box.cancelButton.setText("稍后重启")
            
            if msg_box.exec():
                self._bridge.start_steam()
        else:
            InfoBar.error(
                "更新失败",
                msg,
                parent=self,
                position=InfoBarPosition.TOP
            )


    # ---- 异步加载游戏 ----

    def _load_games_async(self):
        if not self._check_injection_required():
            self._show_not_injected()
            return

        # 取消旧 worker 防止并发
        if self._load_worker and not self._load_worker.isFinished():
            self._load_worker.cancel()

        self.refresh_btn.setEnabled(False)
        self.empty_label.setVisible(False)

        self._load_worker = AsyncWorker(self._load_games_sync)
        self._load_worker.finished_with_result.connect(
            self._on_games_loaded, Qt.ConnectionType.QueuedConnection
        )
        self._load_worker.finished_with_error.connect(
            self._on_games_error, Qt.ConnectionType.QueuedConnection
        )
        self._load_worker.start()

    def _load_games_sync(self):
        games = self._game_manager.refresh()
        missing_ids = [g.app_id for g in games if not g.name]
        return {"games": games, "missing_ids": missing_ids}

    def _on_games_loaded(self, result: dict):
        self._load_worker = None
        if not self._alive:
            return
        self._games_data = result["games"]
        self._display_games(self._games_data)
        self.refresh_btn.setEnabled(True)

        missing = result.get("missing_ids", [])
        if missing:
            self._fetch_missing_names(missing)

    def _on_games_error(self, error: str):
        self._load_worker = None
        if not self._alive:
            return
        self.refresh_btn.setEnabled(True)
        InfoBar.error("错误", error, parent=self,
                      position=InfoBarPosition.TOP)

    # ---- 补充游戏名 ----

    def _fetch_missing_names(self, app_ids: list[str]):
        """为缺少名称的游戏异步获取名称"""
        # 清理旧 worker：先断信号防 QueuedConnection 回调，再取消
        for w in self._name_workers[:]:
            try:
                w.finished_with_result.disconnect(self._on_name_fetched)
            except (TypeError, RuntimeError):
                pass
            w.cancel()
        self._name_workers.clear()

        for app_id in app_ids:
            worker = AsyncWorker(_fetch_game_name, app_id)
            worker.finished_with_result.connect(
                self._on_name_fetched, Qt.ConnectionType.QueuedConnection
            )
            worker.finished_with_error.connect(
                lambda err, aid=app_id: logger.warning(f"获取名称失败 AppID={aid}: {err}"),
                Qt.ConnectionType.QueuedConnection
            )
            worker.start()
            self._name_workers.append(worker)

    def _on_name_fetched(self, result: tuple[str, str]):
        """单个游戏名获取完成（主线程执行）"""
        if not self._alive:
            return
        try:
            app_id, name = result
            if name:
                for card in self._card_list:
                    if card.app_id == app_id:
                        card.game_name = name
                        card.title_label.setText(name)
                        break
                for g in self._games_data:
                    if g.app_id == app_id:
                        g.name = name
                        break
        except (RuntimeError, Exception) as e:
            logger.warning(f"更新游戏名失败 AppID={result[0]}: {e}")

    # ---- 显示游戏列表 ----

    def _display_games(self, games: list[GameInfo]):
        """以列表形式显示游戏数据"""
        self._clear_list()

        if not games:
            self.empty_label.setVisible(True)
            self.stats_label.setText("")
            return

        for idx, game in enumerate(games):
            try:
                card = GameCard(
                    game.app_id,
                    game.name,
                    manifest_ready=getattr(game, "manifest_ready", True),
                    missing_manifests=getattr(game, "missing_manifests", []),
                    parent=self,
                )
                card.removed.connect(self._on_remove_game)
                card.edit_requested.connect(self._on_edit_game)
                card.import_manifest_requested.connect(self._on_card_import_manifest)
                card.clean_cache_requested.connect(self._on_card_clean_cache)
                self._list_layout.addWidget(card)
                self._card_list.append(card)
                # 错峰异步加载封面
                QTimer.singleShot(100 + idx * 150, card.load_cover_async)
            except Exception as e:
                logger.error(f"创建游戏卡片失败 AppID={game.app_id}: {e}")

        self.stats_label.setText("共 {0} 个游戏".format(len(games)))
        # 应用当前排序（非 default 时需要排）
        self._apply_current_sort()

    def _clear_list(self):
        for card in self._card_list:
            card.cleanup()
            self._list_layout.removeWidget(card)
            card.deleteLater()
        self._card_list.clear()

    # ---- 出库 ----

    def _on_remove_game(self, app_id: str):
        ok = self._game_manager.remove_game(app_id)
        if ok:
            target = None
            for card in self._card_list:
                if card.app_id == app_id:
                    target = card
                    break
            if target:
                self._card_list.remove(target)
                self._list_layout.removeWidget(target)
                target.cleanup()
                target.deleteLater()

            InfoBar.success("出库成功", "游戏已从库中移除",
                            parent=self, position=InfoBarPosition.TOP)

            cnt = len(self._card_list)
            self.stats_label.setText("共 {0} 个游戏".format(cnt) if cnt > 0 else "")
            self.empty_label.setVisible(cnt == 0)

            # 通知搜索页刷新推荐（可能需要重新显示）
            self.library_changed.emit()
        else:
            InfoBar.error("出库失败", "",
                          parent=self, position=InfoBarPosition.TOP)

    # ---- 编辑游戏 ---

    def _on_edit_game(self, app_id: str):
        """打开编辑对话框（延迟到事件循环空闲，避免 COM 冲突）"""
        def _do_open():
            dialog = EditGameDialog(self._game_manager, app_id, parent=self)
            dialog.saved.connect(self._on_game_saved)
            dialog.exec()
        QTimer.singleShot(0, _do_open)

    def _on_game_saved(self):
        """编辑保存后刷新列表"""
        self._load_games_async()

    # ---- 过滤 / 排序 ----

    def _on_filter(self, text: str):
        keyword = text.lower().strip()
        if not keyword:
            for card in self._card_list:
                card.setVisible(True)
            self.stats_label.setText("共 {0} 个游戏".format(len(self._card_list)))
            return

        visible = 0
        for card in self._card_list:
            name = getattr(card, "game_name", "").lower()
            app_id = getattr(card, "app_id", "").lower()
            match = keyword in name or keyword in app_id
            card.setVisible(match)
            if match:
                visible += 1

        self.stats_label.setText("共 {0} 个游戏".format(visible))

    def _on_filter_clear(self):
        self._on_filter("")

    def _on_sort(self, index: int):
        mode_map = {0: "default", 1: "az", 2: "za"}
        self._sort_mode = mode_map.get(index, "default")
        self._apply_current_sort()

    def _apply_current_sort(self):
        """按当前排序模式重新排列卡片"""
        if self._sort_mode == "default":
            sorted_cards = [
                card for g in self._games_data
                for card in self._card_list
                if card.app_id == g.app_id
            ]
        elif self._sort_mode == "az":
            sorted_cards = sorted(
                self._card_list,
                key=lambda c: getattr(c, "game_name", "").lower()
            )
        elif self._sort_mode == "za":
            sorted_cards = sorted(
                self._card_list,
                key=lambda c: getattr(c, "game_name", "").lower(),
                reverse=True
            )
        else:
            return

        # 重新排列
        for card in sorted_cards:
            self._list_layout.removeWidget(card)
        for card in sorted_cards:
            self._list_layout.addWidget(card)

    def notify_theme_changed(self):
        for card in self._card_list:
            if hasattr(card, "notify_theme_changed"):
                card.notify_theme_changed()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._alive = False
        # 取消所有活跃 name worker，先断信号防止 QueuedConnection 回调
        for w in self._name_workers[:]:
            try:
                w.finished_with_result.disconnect(self._on_name_fetched)
            except (TypeError, RuntimeError):
                pass
            w.cancel()
            w.wait(2000)
            if w.isFinished():
                w.deleteLater()
            if w in self._name_workers:
                self._name_workers.remove(w)
        self._name_workers.clear()

        if self._load_worker:
            try:
                self._load_worker.finished_with_result.disconnect(self._on_games_loaded)
            except (TypeError, RuntimeError):
                pass
            self._load_worker.cancel()
            self._load_worker.wait(2000)
            self._load_worker = None

    # ---- 批量导入清单与 Lua ----

    def _on_import_files_clicked(self):
        """点击选择文件导入"""
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择清单或 Lua 配置文件",
            "",
            "支持的文件 (*.manifest *.lua *.zip);;Lua 配置文件 (*.lua);;Steam 清单文件 (*.manifest);;ZIP 压缩包 (*.zip);;所有文件 (*.*)",
        )
        if files:
            self.execute_import(files)

    def _on_import_folder_clicked(self):
        """点击选择文件夹导入"""
        folder = QFileDialog.getExistingDirectory(
            self,
            "选择包含清单或 Lua 的文件夹",
            "",
        )
        if folder:
            self.execute_import([folder])

    def execute_import(self, paths: list[str]) -> None:
        """执行批量导入并通知用户"""
        steam_path = self._bridge.get_steam_path() if self._bridge else ""
        if not steam_path:
            InfoBar.error(
                "导入失败",
                "未检测到 Steam 安装路径，请先在注入管理或设置中配置",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000,
            )
            return

        self._import_service.set_steam_path(steam_path)
        result = self._import_service.import_paths(paths)

        if result.success:
            InfoBar.success(
                "导入成功",
                result.summary(),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )
            self._load_games_async()
            self.library_changed.emit()
        else:
            InfoBar.warning(
                "导入提示",
                result.summary(),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )

    # ---- 拖拽事件（Drag & Drop） ----

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                p = url.toLocalFile()
                if p and (os.path.isdir(p) or p.lower().endswith(('.lua', '.manifest', '.zip'))):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if not event.mimeData().hasUrls():
            event.ignore()
            return

        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.toLocalFile()]
        if not paths:
            event.ignore()
            return

        event.acceptProposedAction()
        self.execute_import(paths)

    # ── 便捷工具与卡片操作方法 ──

    def _open_depotcache_dir(self):
        """打开 Steam/depotcache 目录"""
        if self._bridge:
            d = self._bridge.get_depotcache_dir()
            self._bridge.open_directory(d)

    def _open_lua_dir(self):
        """打开 Steam/config/lua 目录"""
        if self._bridge:
            d = self._bridge.get_lua_dir()
            self._bridge.open_directory(d)

    def _on_clean_all_download_cache(self):
        """一键清理所有异常下载缓存"""
        if self._bridge:
            success, msg = self._bridge.clean_download_cache()
            if success:
                InfoBar.success("清理成功", msg, parent=self, position=InfoBarPosition.TOP)
            else:
                InfoBar.error("清理失败", msg, parent=self, position=InfoBarPosition.TOP)

    def _on_card_import_manifest(self, app_id: str):
        """从卡片直接打开文件选择器导入清单或配置文件"""
        files, _ = QFileDialog.getOpenFileNames(
            self,
            f"为 AppID {app_id} 导入清单或配置文件",
            "",
            "Steam 配置文件与清单 (*.manifest *.lua *.zip);;所有文件 (*.*)",
        )
        if files:
            self.execute_import(files)

    def _on_card_clean_cache(self, app_id: str):
        """清理特定游戏的下载缓存与卡死状态"""
        if self._bridge:
            success, msg = self._bridge.clean_download_cache(app_id)
            if success:
                InfoBar.success("清理完成", msg, parent=self, position=InfoBarPosition.TOP)
            else:
                InfoBar.error("清理失败", msg, parent=self, position=InfoBarPosition.TOP)
