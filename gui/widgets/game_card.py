"""
GameCard — 游戏卡片组件

显示游戏封面、名称、AppID，支持右键菜单（复制/出库/在Steam中查看）
封面使用 AsyncWorker+httpx 异步加载，避免 QNetworkAccessManager 生命周期崩溃
"""
from __future__ import annotations

import webbrowser

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QHBoxLayout, QApplication
from PyQt6.QtGui import QPixmap

from qfluentwidgets import (
    CardWidget, BodyLabel, CaptionLabel,
    TransparentToolButton, FluentIcon, RoundMenu, Action,
    ToolTipFilter, ToolTipPosition, isDarkTheme,
)

from utils.async_worker import AsyncWorker
from utils.download_cover import CoverCache, download_cover

from config import TEXT_COLOR

# 全局封面缓存（与 search_page 共享）
_cover_cache = CoverCache.instance()
_save_cover_disk = _cover_cache.save_to_disk  # 兼容别名


class GameCard(CardWidget):
    """游戏卡片：封面 + 名称 + AppID + 更多菜单"""

    removed = pyqtSignal(str)  # 出库信号，携带 app_id
    edit_requested = pyqtSignal(str)  # 编辑信号，携带 app_id
    import_manifest_requested = pyqtSignal(str)  # 导入清单请求
    clean_cache_requested = pyqtSignal(str)  # 清理下载缓存请求

    def __init__(
        self,
        app_id: str,
        game_name: str = "",
        manifest_ready: bool = True,
        missing_manifests: list[str] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.app_id = app_id
        self.game_name = game_name
        self.manifest_ready = manifest_ready
        self.missing_manifests = missing_manifests or []
        self._cover_worker = None
        self._alive = True  # 安全标志，防止回调到已删除对象

        self._init_ui()

    # ---- UI ----

    def _init_ui(self):
        self.setFixedHeight(80)

        h_layout = QHBoxLayout(self)
        h_layout.setContentsMargins(15, 12, 15, 12)
        h_layout.setSpacing(15)

        # 封面
        self.cover_label = QLabel(self)
        self.cover_label.setFixedSize(120, 56)
        self.cover_label.setScaledContents(True)
        self._theme_cover_bg()
        h_layout.addWidget(self.cover_label)

        # 文字信息
        v_layout = QVBoxLayout()
        v_layout.setContentsMargins(0, 0, 0, 0)
        v_layout.setSpacing(4)

        display_name = self.game_name or f"AppID: {self.app_id}"
        self.title_label = BodyLabel(display_name, self)
        self.title_label.setWordWrap(False)
        self.title_label.setTextColor(TEXT_COLOR, TEXT_COLOR)
        v_layout.addWidget(self.title_label, 0, Qt.AlignmentFlag.AlignVCenter)

        # 标签行：AppID + 清单状态标签
        tag_layout = QHBoxLayout()
        tag_layout.setSpacing(10)
        tag_layout.setContentsMargins(0, 0, 0, 0)

        self.info_label = CaptionLabel(f"AppID: {self.app_id}", self)
        self.info_label.setTextColor(TEXT_COLOR, TEXT_COLOR)
        tag_layout.addWidget(self.info_label)

        self.manifest_badge = CaptionLabel(self)
        if self.manifest_ready:
            self.manifest_badge.setText("清单就绪")
            self.manifest_badge.setStyleSheet(
                "color: #52c41a; font-weight: bold; background: rgba(82, 196, 26, 0.15); "
                "border-radius: 4px; padding: 1px 6px;"
            )
            self.manifest_badge.setToolTip("清单文件已部署在本地，Steam 可直接下载安装")
        else:
            missing_count = len(self.missing_manifests)
            self.manifest_badge.setText(f"待补清单 ({missing_count})" if missing_count else "待补清单")
            self.manifest_badge.setStyleSheet(
                "color: #fa8c16; font-weight: bold; background: rgba(250, 140, 22, 0.15); "
                "border-radius: 4px; padding: 1px 6px;"
            )
            missing_preview = ", ".join(self.missing_manifests[:3])
            self.manifest_badge.setToolTip(
                f"缺少清单文件: {missing_preview}\\n"
                "未购买直接下载可能报错 HTTP 401。点击右侧更多菜单可导入或在线查找清单。"
            )
        tag_layout.addWidget(self.manifest_badge)
        tag_layout.addStretch(1)

        v_layout.addLayout(tag_layout)
        v_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        h_layout.addLayout(v_layout)
        h_layout.addStretch(1)

        # ── 行内功能按钮（靠右排列）──
        self._btn_edit = self._make_action_btn(FluentIcon.EDIT, "编辑", self._on_edit)
        h_layout.addWidget(self._btn_edit, 0, Qt.AlignmentFlag.AlignRight)

        self._btn_steam = self._make_action_btn(FluentIcon.LINK, "在 Steam 中查看", self._open_steam_store)
        h_layout.addWidget(self._btn_steam, 0, Qt.AlignmentFlag.AlignRight)

        self._btn_delete = self._make_action_btn(FluentIcon.DELETE, "出库", self._confirm_remove)
        h_layout.addWidget(self._btn_delete, 0, Qt.AlignmentFlag.AlignRight)

        # 更多按钮
        self.more_button = TransparentToolButton(FluentIcon.MORE, self)
        self.more_button.setFixedSize(32, 32)
        self.more_button.setToolTip("更多操作")
        self.more_button.installEventFilter(
            ToolTipFilter(self.more_button, showDelay=150, position=ToolTipPosition.TOP)
        )
        self.more_button.clicked.connect(self._show_more_menu)
        h_layout.addWidget(self.more_button, 0, Qt.AlignmentFlag.AlignRight)

        # 注意：不在构造函数中启动网络请求，由外部调用 load_cover_async()

    # ---- 封面加载 ----

    def load_cover_async(self):
        """延迟异步加载封面（必须在事件循环启动后调用）"""
        if self._cover_worker is not None:
            return
        if _cover_cache.has(self.app_id):
            pix = _cover_cache.get(self.app_id)
            if pix is None:
                return  # 已知无封面，跳过
            if not pix.isNull():
                self.cover_label.setPixmap(pix)
            return
        self._cover_worker = AsyncWorker(download_cover, self.app_id)
        self._cover_worker.finished_with_result.connect(
            self._on_cover_result, Qt.ConnectionType.QueuedConnection
        )
        self._cover_worker.start()

    def _on_cover_result(self, data: bytes | None):
        self._cover_worker = None
        if not self._alive:
            return
        if data:
            try:
                pix = QPixmap()
                pix.loadFromData(data)
                if not pix.isNull():
                    _cover_cache.set(self.app_id, pix)
                    self.cover_label.setPixmap(pix)
                    _cover_cache.save_to_disk(self.app_id, data)
                    return
            except Exception:
                pass
        _cover_cache.set(self.app_id, None)

    def cleanup(self):
        """安全清理：取消线程、断开信号、等待线程完成"""
        self._alive = False
        if self._cover_worker is not None:
            self._cover_worker.cancel()
            self._cover_worker.finished_with_result.disconnect(self._on_cover_result)
            self._cover_worker.wait(3000)  # 等待线程结束，防止 QThread 销毁时仍在运行
            self._cover_worker = None

    # ---- 主题 ----

    def notify_theme_changed(self):
        """响应主题变化"""
        self._theme_cover_bg()
        self.update()
        self.repaint()

    def _theme_cover_bg(self):
        if isDarkTheme():
            self.cover_label.setStyleSheet(
                "border-radius: 4px; background: #2a2a2a;"
            )
        else:
            self.cover_label.setStyleSheet(
                "border-radius: 4px; background: #f0f0f0;"
            )

    # ---- 行内按钮 ----

    def _make_action_btn(self, icon: FluentIcon, tooltip: str, slot) -> TransparentToolButton:
        """创建一个行内透明图标按钮"""
        btn = TransparentToolButton(icon, self)
        btn.setFixedSize(32, 32)
        btn.setToolTip(tooltip)
        btn.installEventFilter(
            ToolTipFilter(btn, showDelay=200, position=ToolTipPosition.TOP)
        )
        btn.clicked.connect(slot)
        return btn

    # ---- 右键菜单 ----

    def _show_more_menu(self):
        """更多操作菜单"""
        menu = RoundMenu(parent=self)
        menu.addAction(Action(FluentIcon.FOLDER_ADD, "导入清单文件 (.manifest / .zip)", triggered=self._on_import_manifest))
        menu.addAction(Action(FluentIcon.SEARCH, "在线寻找此游戏清单", triggered=self._search_manifest_online))
        menu.addAction(Action(FluentIcon.FOLDER, "打开清单目录 (depotcache)", triggered=self._open_depotcache))
        menu.addAction(Action(FluentIcon.BROOM, "清理此游戏下载残留缓存", triggered=self._on_clean_cache))
        menu.addSeparator()
        menu.addAction(Action(FluentIcon.COPY, "复制 AppID", triggered=self._copy_appid))
        menu.addAction(Action(FluentIcon.COPY, "复制游戏名", triggered=self._copy_name))

        pos = self.more_button.mapToGlobal(
            self.more_button.rect().bottomLeft()
        )
        menu.exec(pos)

    def _on_import_manifest(self):
        self.import_manifest_requested.emit(self.app_id)

    def _on_clean_cache(self):
        self.clean_cache_requested.emit(self.app_id)

    def _open_depotcache(self):
        from core.app_state import app_state, STEAM_PATH
        steam_path = str(app_state.get(STEAM_PATH, ""))
        if steam_path:
            depot_dir = os.path.join(steam_path, "depotcache")
            os.makedirs(depot_dir, exist_ok=True)
            import sys
            if sys.platform == "win32":
                os.startfile(depot_dir)
            else:
                import subprocess
                subprocess.Popen(["xdg-open", depot_dir])

    def _search_manifest_online(self):
        from core.manifest_resolver import ManifestResolver
        queries = ManifestResolver.get_search_queries(self.app_id, self.game_name)
        url = queries.get("百度搜索", "")
        if url:
            webbrowser.open(url)

    def _copy_appid(self):
        QApplication.clipboard().setText(self.app_id)

    def _copy_name(self):
        QApplication.clipboard().setText(self.game_name or f"AppID: {self.app_id}")

    def _open_steam_store(self):
        webbrowser.open(f"steam://store/{self.app_id}")

    def _on_edit(self):
        """发出编辑信号"""
        self.edit_requested.emit(self.app_id)

    def _confirm_remove(self):
        from qfluentwidgets import MessageBox
        dialog = MessageBox(
            "确认出库",
            "确定要将 AppID {0} 从游戏库中移除吗？\n\n这将删除对应的 Lua 配置文件。".format(self.app_id),
            self.window(),
        )
        if dialog.exec():
            self.removed.emit(self.app_id)
