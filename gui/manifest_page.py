"""ManifestPage —— 清单管理页面（游戏视角，参考「Steam 解锁文件管理器」）。

界面按真实使用场景组织：

* **默认视图 = 按游戏**：每行显示游戏名 + AppID + 清单是否齐全 + 文件数，
  操作是「补全」与「删除此游戏全部清单」——玩通关想删游戏时，一眼找到
  游戏名即可清干净，不需要记住 depot 编号；
* **专家模式（开关，默认关闭）**：才显示散装清单文件表（勾选式逐条删除），
  面向调试场景；
* **统计以游戏为单位**：游戏总数 / 清单齐全 / 清单不全 / 未归属残留，
  清单文件数只是补充信息；
* **删除即备份**：任何删除都把文件移动到 ``depotcache_backup/<时间戳>``，
  可随时手动恢复；已安装游戏的 depot 一律不碰；
* **存档备份**：本地打包 + WebDAV 联网备份（远端按设备名隔离，
  zip 内保留多账号 userdata 结构）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    CheckBox,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
)

from core.manifest_cache import ManifestCacheManager, game_manifest_view
from core.config_manager import ConfigManager
from utils.async_worker import AsyncWorker
from utils.logger import setup_logger

logger = setup_logger(__name__)

_MANIFEST_NAME = re.compile(r"^(?P<depot>\d+)_(?P<gid>\d+)\.manifest$", re.IGNORECASE)

_CATEGORY_LABEL = {
    "lua": "Lua 引用（受保护）",
    "installed": "已安装游戏（受保护）",
    "orphan": "未引用",
}


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


class ManifestPage(ScrollArea):
    """清单管理页面（游戏视角）。"""

    # 通知其他页面（如游戏库）刷新清单就绪状态
    manifests_changed = pyqtSignal()

    def __init__(self, game_manager, bridge=None, parent=None):
        super().__init__(parent)
        self._game_manager = game_manager
        self._bridge = bridge
        self._worker: AsyncWorker | None = None
        self._save_worker: AsyncWorker | None = None
        self._games: list[dict] = []
        self._expert_rows: list[dict] = []
        self._names: dict[str, str] = {}

        self.setObjectName("manifestPage")
        self.setWidgetResizable(True)
        self._init_ui()

    # ── 路径 ──────────────────────────────────────────────
    def _steam_path(self) -> str:
        if self._bridge is not None:
            path = self._bridge.get_steam_path()
            if path:
                return path
        return getattr(self._game_manager, "_steam_path", "") or ""

    def _lua_dir(self) -> str:
        if self._bridge is not None:
            path = self._bridge.get_lua_dir()
            if path:
                return path
        return self._game_manager.get_lua_dir()

    def _lua_dirs(self) -> list[str]:
        dirs: list[str] = []
        for candidate in (self._lua_dir(), getattr(self._game_manager, "_lua_dir", "")):
            if candidate and candidate not in dirs:
                dirs.append(candidate)
        return dirs

    def _cache(self) -> ManifestCacheManager:
        return ManifestCacheManager(self._steam_path())

    def _library_names(self) -> dict[str, str]:
        """从游戏库取 {appid: 游戏名}，用于清单页展示人类可读的名字。"""
        names: dict[str, str] = {}
        try:
            for game in self._game_manager.get_games():
                if game.name and str(game.app_id).isdigit():
                    names[str(game.app_id)] = game.name
        except Exception:  # noqa: BLE001 - 游戏库不可用不影响清单页
            pass
        return names

    # ── UI ────────────────────────────────────────────────
    def _init_ui(self) -> None:
        container = QWidget()
        self.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        title = StrongBodyLabel("清单管理", container)
        title.setStyleSheet("font-size: 22px;")
        layout.addWidget(title)

        self._path_label = BodyLabel("", container)
        self._path_label.setStyleSheet("color: #888; font-size: 12px;")
        self._path_label.setWordWrap(True)
        layout.addWidget(self._path_label)

        # ── 统计卡片（以游戏为单位）──
        layout.addWidget(self._build_stats_row(container))

        # ── 常用操作 ──
        layout.addWidget(self._build_action_row(container))

        # ── 游戏列表（默认视图）──
        game_title = StrongBodyLabel("游戏清单（玩通关后点「删除清单」即可清干净一个游戏的全部清单）", container)
        game_title.setStyleSheet("font-size: 13px; margin-top: 4px;")
        layout.addWidget(game_title)

        self._game_table = QTableWidget(0, 5, container)
        self._game_table.setHorizontalHeaderLabels(["游戏", "AppID", "清单状态", "清单文件", "操作"])
        self._game_table.verticalHeader().setVisible(False)
        self._game_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._game_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self._game_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._game_table.setMinimumHeight(180)
        layout.addWidget(self._game_table)

        # ── 专家模式（默认隐藏）──
        self._expert_title = StrongBodyLabel("散装清单文件（专家模式：逐条勾选删除/清理）", container)
        self._expert_title.setStyleSheet("font-size: 13px; margin-top: 4px;")
        layout.addWidget(self._expert_title)

        self._table = QTableWidget(0, 6, container)
        self._table.setHorizontalHeaderLabels(["清理", "Depot_GID", "状态", "归属", "大小", "所在目录"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self._table.setMinimumHeight(240)
        layout.addWidget(self._table)

        self._expert_btn_row = self._build_expert_buttons(container)
        layout.addLayout(self._expert_btn_row)
        self._set_expert_visible(False)

        # ── 存档备份 ──
        layout.addWidget(self._build_save_backup_card(container))

        self._status_label = BodyLabel("点击「扫描清单」开始", container)
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(self._status_label)

    def _build_stats_row(self, parent: QWidget) -> QWidget:
        row = QWidget(parent)
        box = QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(12)

        self._stat_cards: dict[str, QLabel] = {}
        for key, caption in (
            ("games", "游戏总数"),
            ("ready", "清单齐全"),
            ("incomplete", "清单不全"),
            ("orphan", "未归属残留"),
            ("protected", "已安装保护"),
            ("size", "占用空间"),
        ):
            card = CardWidget(row)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(16, 12, 16, 12)
            value_label = QLabel("—", card)
            value_label.setStyleSheet("font-size: 20px; font-weight: 600;")
            caption_label = QLabel(caption, card)
            caption_label.setStyleSheet("color: #888; font-size: 12px;")
            card_layout.addWidget(value_label)
            card_layout.addWidget(caption_label)
            self._stat_cards[key] = value_label
            box.addWidget(card)
        box.addStretch()
        return row

    def _build_action_row(self, parent: QWidget) -> QWidget:
        row = QWidget(parent)
        box = QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)

        self._scan_btn = PrimaryPushButton(FluentIcon.SYNC, "扫描清单", row)
        self._scan_btn.clicked.connect(self._on_scan)
        box.addWidget(self._scan_btn)

        self._complete_btn = PushButton(FluentIcon.DOWNLOAD, "一键补全缺失清单", row)
        self._complete_btn.clicked.connect(self._on_complete_missing)
        box.addWidget(self._complete_btn)

        self._import_btn = PushButton(FluentIcon.FOLDER_ADD, "导入清单文件", row)
        self._import_btn.clicked.connect(self._on_import)
        box.addWidget(self._import_btn)

        self._sync_btn = PushButton(FluentIcon.SYNC, "双目录同步", row)
        self._sync_btn.clicked.connect(self._on_sync_dual)
        box.addWidget(self._sync_btn)

        self._expert_check = CheckBox("专家模式（显示散装清单文件）", row)
        self._expert_check.toggled.connect(self._set_expert_visible)
        box.addWidget(self._expert_check)

        box.addStretch()
        return row

    def _build_expert_buttons(self, parent: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        self._select_btn = PushButton(FluentIcon.CHECKBOX, "全选未引用", parent)
        self._select_btn.clicked.connect(self._on_select_orphans)
        row.addWidget(self._select_btn)

        self._delete_selected_btn = PushButton(FluentIcon.DELETE, "删除选中项", parent)
        self._delete_selected_btn.clicked.connect(self._on_delete_selected)
        row.addWidget(self._delete_selected_btn)

        self._orphan_btn = PushButton(FluentIcon.BROOM, "清理全部未引用", parent)
        self._orphan_btn.clicked.connect(self._on_clean_orphans)
        row.addWidget(self._orphan_btn)

        self._fix_btn = PushButton(FluentIcon.BROOM, "删除损坏清单", parent)
        self._fix_btn.clicked.connect(self._on_delete_invalid)
        row.addWidget(self._fix_btn)

        row.addStretch()
        return row

    # ── 存档备份卡片 ──────────────────────────────────────
    def _build_save_backup_card(self, parent: QWidget) -> QWidget:
        card = CardWidget(parent)
        box = QVBoxLayout(card)
        box.setContentsMargins(16, 12, 16, 12)
        box.setSpacing(8)

        title = StrongBodyLabel("存档备份（Steam 云存档 userdata/<账号>/<AppID>/remote；zip 内保留多账号结构）", card)
        title.setStyleSheet("font-size: 13px;")
        box.addWidget(title)

        # 本地行
        local_row = QHBoxLayout()
        local_row.setSpacing(10)
        self._save_app_combo = QComboBox(card)
        self._save_app_combo.setMinimumWidth(340)
        local_row.addWidget(self._save_app_combo)

        self._save_reload_btn = PushButton(FluentIcon.SYNC, "刷新存档列表", card)
        self._save_reload_btn.clicked.connect(self._on_reload_save_apps)
        local_row.addWidget(self._save_reload_btn)

        self._save_backup_btn = PushButton(FluentIcon.SAVE, "备份到文件夹…", card)
        self._save_backup_btn.clicked.connect(self._on_backup_saves)
        local_row.addWidget(self._save_backup_btn)

        self._save_restore_btn = PushButton(FluentIcon.CANCEL, "从本地备份恢复…", card)
        self._save_restore_btn.clicked.connect(self._on_restore_saves)
        local_row.addWidget(self._save_restore_btn)
        local_row.addStretch()
        box.addLayout(local_row)

        # WebDAV 行
        webdav_title = StrongBodyLabel("WebDAV 联网备份（多设备：远端按设备名隔离；zip 内含多账号存档）", card)
        webdav_title.setStyleSheet("font-size: 12px; color: #888;")
        box.addWidget(webdav_title)

        dav_row = QHBoxLayout()
        dav_row.setSpacing(8)
        self._dav_url_edit = QLineEdit(card)
        self._dav_url_edit.setPlaceholderText("WebDAV 地址，如 https://dav.jianguoyun.com/dav/")
        dav_row.addWidget(self._dav_url_edit, 3)
        self._dav_user_edit = QLineEdit(card)
        self._dav_user_edit.setPlaceholderText("账号")
        dav_row.addWidget(self._dav_user_edit, 1)
        self._dav_pass_edit = QLineEdit(card)
        self._dav_pass_edit.setPlaceholderText("密码/应用密码")
        self._dav_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        dav_row.addWidget(self._dav_pass_edit, 1)

        self._dav_test_btn = PushButton("测试连接", card)
        self._dav_test_btn.clicked.connect(self._on_webdav_test)
        dav_row.addWidget(self._dav_test_btn)

        self._dav_backup_btn = PushButton(FluentIcon.CLOUD, "备份并上传", card)
        self._dav_backup_btn.clicked.connect(self._on_webdav_backup)
        dav_row.addWidget(self._dav_backup_btn)

        self._dav_restore_btn = PushButton(FluentIcon.DOWNLOAD, "从 WebDAV 恢复…", card)
        self._dav_restore_btn.clicked.connect(self._on_webdav_restore)
        dav_row.addWidget(self._dav_restore_btn)
        box.addLayout(dav_row)

        hint = BodyLabel(
            "删除游戏或重装系统前建议先备份存档；本地恢复时同名文件先存为 .bak 再覆盖。"
            "WebDAV 配置保存在本机设置中。",
            card,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 11px;")
        box.addWidget(hint)
        self._load_webdav_config()
        return card

    def _load_webdav_config(self) -> None:
        try:
            config = ConfigManager()
            self._dav_url_edit.setText(str(config.get("webdav_url", "") or ""))
            self._dav_user_edit.setText(str(config.get("webdav_username", "") or ""))
            self._dav_pass_edit.setText(str(config.get("webdav_password", "") or ""))
        except Exception:  # noqa: BLE001 - 配置不可用不影响页面
            pass

    def _save_webdav_config(self) -> None:
        try:
            config = ConfigManager()
            config.set("webdav_url", self._dav_url_edit.text().strip())
            config.set("webdav_username", self._dav_user_edit.text().strip())
            config.set("webdav_password", self._dav_pass_edit.text())
        except Exception:  # noqa: BLE001
            pass

    def _make_webdav_client(self):
        from core.webdav_client import WebDavClient, WebDavError

        url = self._dav_url_edit.text().strip()
        if not url:
            raise WebDavError("请先填写 WebDAV 服务器地址")
        self._save_webdav_config()
        return WebDavClient(url, self._dav_user_edit.text().strip(), self._dav_pass_edit.text())

    # ── 扫描 ──────────────────────────────────────────────
    def showEvent(self, event):  # noqa: N802 - Qt 接口
        super().showEvent(event)
        if not self._games:
            QTimer.singleShot(120, self._on_scan)
        else:
            QTimer.singleShot(60, self._on_reload_save_apps)

    def _on_scan(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        steam_path = self._steam_path()
        if not steam_path:
            self._status_label.setText("未检测到 Steam 路径，请先在「设置」中指定。")
            return

        self._scan_btn.setEnabled(False)
        self._status_label.setText("正在扫描清单目录…")
        self._path_label.setText(f"depotcache: {steam_path}\\depotcache    |    "
                                 f"config\\depotcache: {steam_path}\\config\\depotcache    |    "
                                 f"Lua: {' ; '.join(self._lua_dirs()) or '(未配置)'}")

        self._worker = AsyncWorker(self._scan_worker, steam_path, self._lua_dirs(), self._library_names())
        self._worker.finished_with_result.connect(self._on_scan_done)
        self._worker.finished_with_error.connect(self._on_scan_error)
        self._worker.start()

    @staticmethod
    def _scan_worker(steam_path: str, lua_dirs: list[str], names: dict[str, str]) -> dict:
        cache = ManifestCacheManager(steam_path)
        view = game_manifest_view(steam_path, lua_dirs, names)
        classified = cache.classify(cache.scan(), lua_dirs)

        total_size = 0
        expert_rows: list[dict] = []
        for item in classified:
            record = item["record"]
            size = 0
            for path in record.paths:
                try:
                    size += os.path.getsize(path)
                except OSError:
                    pass
            total_size += size
            directories = sorted(
                {Path(p).parent.name + ("/config" if Path(p).parent.parent.name == "config" else "") for p in record.paths}
            )
            expert_rows.append(
                {
                    "depot": record.depot_id,
                    "gid": record.manifest_gid,
                    "valid": record.valid,
                    "size": size,
                    "dirs": ", ".join(directories),
                    "copies": len(record.paths),
                    "orphan": item["category"] == "orphan",
                    "category": item["category"],
                    "owner": item["owner"],
                    "key": record.key,
                }
            )

        return {
            "games": view["games"],
            "orphans": [r.key for r in view["orphans"]],
            "orphan_count": len(view["orphans"]),
            "protected_installed": view["protected_installed"],
            "total_games": view["total_games"],
            "ready_games": view["ready_games"],
            "incomplete_games": view["incomplete_games"],
            "expert_rows": expert_rows,
            "size": total_size,
        }

    def _on_scan_done(self, result: dict) -> None:
        self._scan_btn.setEnabled(True)
        self._games = result["games"]
        self._expert_rows = result["expert_rows"]
        self._names = {g["app_id"]: g["name"] for g in self._games}

        self._stat_cards["games"].setText(str(result["total_games"]))
        self._stat_cards["ready"].setText(str(result["ready_games"]))
        self._stat_cards["incomplete"].setText(str(result["incomplete_games"]))
        self._stat_cards["orphan"].setText(str(result["orphan_count"]))
        self._stat_cards["protected"].setText(str(result["protected_installed"]))
        self._stat_cards["size"].setText(_human_size(result["size"]))

        self._render_game_table(result["orphan_count"])
        self._render_expert_table()

        summary = (
            f"扫描完成：{result['total_games']} 个游戏"
            f"（清单齐全 {result['ready_games']}，不全 {result['incomplete_games']}），"
            f"未归属残留 {result['orphan_count']} 个，已安装游戏保护 {result['protected_installed']} 个。"
        )
        self._status_label.setText(summary)
        self._on_reload_save_apps()
        self.manifests_changed.emit()

    def _render_game_table(self, orphan_count: int) -> None:
        rows = list(self._games)
        if orphan_count:
            rows = [{"special": "orphan", "name": "未归属残留清单", "app_id": "—",
                     "file_count": orphan_count}] + rows
        self._game_table.setRowCount(len(rows))
        for row, game in enumerate(rows):
            if game.get("special") == "orphan":
                self._game_table.setItem(row, 0, QTableWidgetItem("未归属残留清单（可能是已删除游戏留下的）"))
                self._game_table.setItem(row, 1, QTableWidgetItem("—"))
                self._game_table.setItem(row, 2, QTableWidgetItem("不属于任何游戏"))
                self._game_table.setItem(row, 3, QTableWidgetItem(str(game["file_count"])))
                clean_btn = PushButton("全部删除", self._game_table)
                clean_btn.clicked.connect(self._on_clean_orphans)
                self._game_table.setCellWidget(row, 4, clean_btn)
                continue

            app_id = game["app_id"]
            name_item = QTableWidgetItem(game["name"])
            self._game_table.setItem(row, 0, name_item)
            self._game_table.setItem(row, 1, QTableWidgetItem(app_id))

            if game["declared"] == 0:
                status = "未绑定清单"
            elif game["ok"]:
                status = f"齐全（{game['present']}/{game['declared']}）"
            else:
                parts = []
                if game["missing"]:
                    parts.append(f"缺 {game['missing']} 个")
                if game["damaged"]:
                    parts.append(f"损坏 {game['damaged']} 个")
                status = "、".join(parts) + f"（{game['present']}/{game['declared']}）"
            self._game_table.setItem(row, 2, QTableWidgetItem(status))
            self._game_table.setItem(row, 3, QTableWidgetItem(str(game["file_count"])))

            ops = QWidget(self._game_table)
            ops_layout = QHBoxLayout(ops)
            ops_layout.setContentsMargins(2, 2, 2, 2)
            ops_layout.setSpacing(6)
            complete_btn = PushButton("补全", ops)
            complete_btn.clicked.connect(lambda _=False, a=app_id: self._on_complete_one(a))
            ops_layout.addWidget(complete_btn)
            delete_btn = PushButton("删除清单", ops)
            delete_btn.clicked.connect(lambda _=False, a=app_id, n=game["name"]: self._on_delete_game(a, n))
            ops_layout.addWidget(delete_btn)
            ops_layout.addStretch()
            self._game_table.setCellWidget(row, 4, ops)

    def _render_expert_table(self) -> None:
        self._table.setRowCount(len(self._expert_rows))
        for row, item in enumerate(self._expert_rows):
            check_item = QTableWidgetItem()
            if item["orphan"]:
                check_item.setFlags(
                    Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                )
                check_item.setCheckState(Qt.CheckState.Unchecked)
            else:
                check_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self._table.setItem(row, 0, check_item)

            status = "有效" if item["valid"] else "损坏"
            if item["copies"] > 1:
                status += f"（{item['copies']} 份）"
            category = item["category"]
            if category == "lua":
                owner_text = f"AppID {item['owner']} · Lua"
            elif category == "installed":
                owner_text = f"AppID {item['owner']} · 已安装"
            else:
                owner_text = _CATEGORY_LABEL.get(category, category)

            self._table.setItem(row, 1, QTableWidgetItem(item["key"]))
            self._table.setItem(row, 2, QTableWidgetItem(status))
            self._table.setItem(row, 3, QTableWidgetItem(owner_text))
            self._table.setItem(row, 4, QTableWidgetItem(_human_size(item["size"])))
            self._table.setItem(row, 5, QTableWidgetItem(item["dirs"]))

    def _set_expert_visible(self, visible: bool) -> None:
        self._table.setVisible(visible)
        self._expert_title.setVisible(visible)
        self._select_btn.setVisible(visible)
        self._delete_selected_btn.setVisible(visible)
        self._orphan_btn.setVisible(visible)
        self._fix_btn.setVisible(visible)

    def _on_scan_error(self, message: str) -> None:
        self._scan_btn.setEnabled(True)
        self._status_label.setText(f"扫描失败：{message}")
        logger.error("清单扫描失败: %s", message)

    # ── 按游戏删除 ────────────────────────────────────────
    def _confirm_delete_game(self, game_name: str, count: int, detail: str, kept_note: str) -> bool:
        """按游戏删除前的确认（测试可覆写为直接返回 True/False）。"""
        confirm = MessageBox(
            f"删除《{game_name}》的全部清单？",
            f"将把 {count} 个清单移动到备份目录（depotcache_backup，可随时手动恢复）：\n"
            f"{detail}\n\n"
            + kept_note
            + "删除后若要再次通过 Steam 下载该游戏，需要重新补全清单。是否继续？",
            self,
        )
        confirm.yesButton.setText("备份并删除")
        confirm.cancelButton.setText("取消")
        return bool(confirm.exec())

    def _on_delete_game(self, app_id: str, game_name: str) -> None:
        """删除一个游戏的全部清单（玩通关/移除游戏后的清理动作）。

        属于已安装游戏（appmanifest）的 depot 一律保留 —— Steam 自己管理的
        内容不碰；其余按游戏声明的 depot 全部移入备份目录。
        """
        cache = self._cache()
        depots = cache.declared_depots_for_app(app_id, self._lua_dirs())
        if not depots:
            InfoBar.info("没有可删除的清单", f"{game_name} 的 Lua 没有声明任何 depot", parent=self, position=InfoBarPosition.TOP)
            return
        installed = cache.installed_depot_ids()
        records = [r for r in cache.scan() if r.depot_id in depots]
        removable = [r for r in records if r.depot_id not in installed]
        kept = len(records) - len(removable)
        if not removable:
            InfoBar.info(
                "无需删除",
                f"{game_name} 的 {kept} 个清单全部属于已安装游戏，已保留",
                parent=self, position=InfoBarPosition.TOP,
            )
            return

        keys_preview = "、".join(r.key for r in removable[:6])
        more = f" 等共 {len(removable)} 个" if len(removable) > 6 else ""
        kept_note = f"另有 {kept} 个清单属于已安装游戏，将保留。\n" if kept else ""
        if not self._confirm_delete_game(game_name, len(removable), f"{keys_preview}{more}", kept_note):
            return

        moved, freed, backup_dir = cache.backup_and_delete(removable, allow_in_use=True)
        summary = f"已删除《{game_name}》的 {moved} 个清单（释放 {_human_size(freed)}）"
        if backup_dir:
            summary += f"\n备份位置：{backup_dir}"
        self._status_label.setText(summary)
        InfoBar.success("删除完成", summary, parent=self, position=InfoBarPosition.TOP, duration=5000)
        self._on_scan()

    # ── 专家模式：逐条删除 ────────────────────────────────
    def _selected_keys(self) -> list[str]:
        selected: list[str] = []
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item and (item.flags() & Qt.ItemFlag.ItemIsUserCheckable) and item.checkState() == Qt.CheckState.Checked:
                key_item = self._table.item(row, 1)
                if key_item:
                    selected.append(key_item.text())
        return selected

    def _on_select_orphans(self) -> None:
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item and (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                item.setCheckState(Qt.CheckState.Checked)
        count = len(self._selected_keys())
        self._status_label.setText(f"已勾选 {count} 个未引用清单，确认后点「删除选中项」。")

    def _on_delete_selected(self) -> None:
        keys = self._selected_keys()
        if not keys:
            InfoBar.info("未勾选任何项", "请先勾选要清理的「未引用」清单", parent=self, position=InfoBarPosition.TOP, duration=3000)
            return
        if self._delete_with_backup(keys):
            self._on_scan()

    def _on_clean_orphans(self) -> None:
        """一键清理全部「未引用」清单（已安装游戏与 Lua 引用项受保护）。"""
        if not self._lua_dirs():
            InfoBar.warning("缺少 Lua 目录", "无法判断哪些清单是孤儿", parent=self, position=InfoBarPosition.TOP)
            return
        cache = self._cache()
        orphans = cache.orphaned(self._lua_dirs())
        if not orphans:
            InfoBar.info("无需清理", "没有未引用的清单", parent=self, position=InfoBarPosition.TOP, duration=3000)
            return
        if self._delete_with_backup([record.key for record in orphans]):
            self._on_scan()

    def _confirm_orphan_cleanup(self, orphan_count: int, detail: str, backup_hint: str) -> bool:
        """删除前的明细预览确认（测试可覆写为直接返回 True/False）。"""
        confirm = MessageBox(
            "确认删除？",
            f"以下 {orphan_count} 个清单均不属于任何已安装游戏、也未被 Lua 引用，"
            f"将移动到备份目录（{backup_hint}，可随时手动恢复）：\n\n{detail}\n\n是否继续？",
            self,
        )
        confirm.yesButton.setText("备份并删除")
        confirm.cancelButton.setText("取消")
        return bool(confirm.exec())

    def _delete_with_backup(self, keys: list[str]) -> bool:
        cache = self._cache()
        by_key = {record.key: record for record in cache.scan()}
        targets = [by_key[k] for k in keys if k in by_key]
        if not targets:
            return False

        lines = [record.key for record in targets[:8]]
        detail = "\n".join(lines)
        if len(targets) > 8:
            detail += f"\n… 等共 {len(targets)} 个清单"
        backup_hint = f"{self._steam_path()}\\depotcache_backup\\<时间戳>"
        if not self._confirm_orphan_cleanup(len(targets), detail, backup_hint):
            return False

        moved, freed, backup_dir = cache.backup_and_delete(targets)
        summary = f"已把 {moved} 个清单移入备份目录，释放 {_human_size(freed)}"
        if backup_dir:
            summary += f"\n备份位置：{backup_dir}"
        self._status_label.setText(summary)
        InfoBar.success(
            "整理完成",
            "所选清单已备份移动（可在资源管理器打开备份目录核对），游戏运行正常后可清空备份。",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000,
        )
        return True

    # ── 一键补全 ──────────────────────────────────────────
    def _on_complete_missing(self) -> None:
        self._start_completion([])

    def _on_complete_one(self, app_id: str) -> None:
        """补全单个游戏（游戏列表中的「补全」按钮）"""
        self._start_completion([str(app_id)])

    def _start_completion(self, only_app_ids: list[str]) -> None:
        if self._worker and self._worker.isRunning():
            InfoBar.warning("正在忙", "上一个操作还没结束", parent=self, position=InfoBarPosition.TOP, duration=2500)
            return

        steam_path = self._steam_path()
        lua_dir = self._lua_dir()
        if not steam_path or not lua_dir:
            InfoBar.warning("路径缺失", "请先配置 Steam 与 Lua 目录", parent=self, position=InfoBarPosition.TOP)
            return

        self._set_busy(True, "正在扫描缺失清单并联网补全，请稍候…")
        self._worker = AsyncWorker(self._complete_worker, steam_path, lua_dir, only_app_ids)
        self._worker.progress.connect(
            lambda text: self._status_label.setText(text)
        )
        self._worker.finished_with_result.connect(self._on_complete_done)
        self._worker.finished_with_error.connect(self._on_complete_error)
        self._worker.start()

    @staticmethod
    def _complete_worker(steam_path: str, lua_dir: str, only_app_ids: list[str], progress_cb=None) -> dict:
        """后台补全：从 Lua 目录收集 AppID，逐个走归档优先流水线补齐。"""
        from core.complete_manifest_service import ManifestCompletionService

        def _report(text: str) -> None:
            if progress_cb is not None:
                progress_cb(text)

        app_ids: list[str] = []
        seen: set[str] = set()
        if lua_dir and os.path.isdir(lua_dir):
            for name in sorted(os.listdir(lua_dir)):
                if not name.endswith(".lua"):
                    continue
                stem = name[:-4]
                if not stem.isdigit() or stem in seen:
                    continue
                seen.add(stem)
                if only_app_ids and stem not in only_app_ids:
                    continue
                app_ids.append(stem)

        failed: list[str] = []
        downloaded_total = 0
        service = ManifestCompletionService(steam_path)
        try:
            for index, app_id in enumerate(app_ids, start=1):
                _report(f"正在补全 AppID {app_id}（{index}/{len(app_ids)}）…")
                report = service.complete_app(app_id)
                downloaded_total += report.downloaded
                if not report.is_complete:
                    failed.append(report.summary())
        finally:
            service.close()
        if app_ids:
            overall = f"共 {len(app_ids)} 个游戏，下载 {downloaded_total} 个清单；全部就绪。"
            if failed:
                overall = (
                    f"共 {len(app_ids)} 个游戏，下载 {downloaded_total} 个清单；"
                    f"未就绪 {len(failed)} 个：" + "；".join(failed[:3])
                )
        else:
            overall = "没有需要补全的游戏"
        return {"summary": overall, "apps": len(app_ids), "downloaded": downloaded_total}

    def _on_complete_done(self, result: dict) -> None:
        self._set_busy(False, result["summary"])
        InfoBar.success(
            "补全完成",
            result["summary"],
            parent=self,
            position=InfoBarPosition.TOP,
            duration=6000,
        )
        self._on_scan()
        self.manifests_changed.emit()

    def _on_complete_error(self, message: str) -> None:
        self._set_busy(False, f"补全失败：{message}")
        InfoBar.error("补全失败", message, parent=self, position=InfoBarPosition.TOP, duration=6000)

    # ── 手动导入 ──────────────────────────────────────────
    def _on_import(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择清单文件",
            self._steam_path() or "",
            "Steam 清单 (*.manifest);;所有文件 (*.*)",
        )
        if not files:
            return

        cache = self._cache()
        imported = skipped = failed = 0
        for path in files:
            name = os.path.basename(path)
            match = _MANIFEST_NAME.match(name)
            if not match:
                failed += 1
                logger.warning("清单文件名不符合 {depot}_{gid}.manifest 规则，跳过: %s", name)
                continue
            try:
                data = Path(path).read_bytes()
            except OSError as exc:
                logger.warning("读取清单失败 %s: %s", path, exc)
                failed += 1
                continue
            written = cache.write(
                match.group("depot"), match.group("gid"), data, overwrite=True
            )
            if written:
                imported += 1
            else:
                skipped += 1

        summary = f"导入 {imported} 个清单"
        if skipped:
            summary += f"，{skipped} 个已存在"
        if failed:
            summary += f"，{failed} 个文件名校验失败"
        self._status_label.setText(summary)
        InfoBar.success("导入完成", summary, parent=self, position=InfoBarPosition.TOP, duration=5000)
        self._on_scan()
        self.manifests_changed.emit()

    # ── 双目录同步 ────────────────────────────────────────
    def _on_sync_dual(self) -> None:
        cache = self._cache()
        records = cache.scan()
        copied = 0
        for record in records:
            if len(record.paths) < 2:
                copied += cache.sync(record)
        summary = f"已把 {copied} 份清单镜像到缺失的目录" if copied else "两个目录已经完全一致"
        self._status_label.setText(summary)
        InfoBar.success("同步完成", summary, parent=self, position=InfoBarPosition.TOP, duration=4000)
        self._on_scan()

    # ── 清理（专家模式）──────────────────────────────────
    def _on_delete_invalid(self) -> None:
        cache = self._cache()
        invalid = [record for record in cache.scan() if not record.valid]
        if not invalid:
            InfoBar.info("无需清理", "没有发现损坏的清单文件", parent=self, position=InfoBarPosition.TOP, duration=3000)
            return
        moved, _freed, _backup = cache.backup_and_delete(invalid)
        summary = f"已把 {moved} 个损坏清单移入备份目录（可恢复）"
        self._status_label.setText(summary)
        InfoBar.success("清理完成", summary, parent=self, position=InfoBarPosition.TOP, duration=4000)
        self._on_scan()

    # ── 存档备份：本地 ────────────────────────────────────
    def _on_reload_save_apps(self) -> None:
        steam_path = self._steam_path()
        if not steam_path:
            return
        if self._save_worker and self._save_worker.isRunning():
            return

        def _list_worker(path: str):
            from core.save_backup import SaveBackupManager

            return SaveBackupManager(path).list_save_apps()

        self._save_worker = AsyncWorker(_list_worker, steam_path)
        self._save_worker.finished_with_result.connect(self._on_save_apps_loaded)
        self._save_worker.start()

    def _on_save_apps_loaded(self, apps: list) -> None:
        self._save_apps = apps or []
        self._save_app_combo.clear()
        if not self._save_apps:
            self._save_app_combo.addItem("未发现存档（userdata 下没有 remote 目录）")
            self._save_app_combo.setEnabled(False)
            self._save_backup_btn.setEnabled(False)
            return
        self._save_app_combo.setEnabled(True)
        self._save_backup_btn.setEnabled(True)
        for info in self._save_apps:
            self._save_app_combo.addItem(info.label, userData=info.app_id)

    def _on_backup_saves(self) -> None:
        app_id = self._save_app_combo.currentData()
        if not app_id:
            InfoBar.warning("没有可备份的存档", "请先刷新存档列表", parent=self, position=InfoBarPosition.TOP)
            return
        dest = QFileDialog.getExistingDirectory(self, "选择存档备份保存目录", os.path.expanduser("~"))
        if not dest:
            return

        self._status_label.setText("正在备份存档…")
        self._save_backup_btn.setEnabled(False)

        def _backup_worker(path: str, ids: list, target: str):
            from core.save_backup import SaveBackupManager

            return str(SaveBackupManager(path).backup(ids, target))

        worker = AsyncWorker(_backup_worker, self._steam_path(), [str(app_id)], dest)
        worker.finished_with_result.connect(self._on_backup_done)
        worker.finished_with_error.connect(self._on_save_op_error)
        self._save_worker = worker
        worker.start()

    def _on_backup_done(self, zip_path: str) -> None:
        self._save_backup_btn.setEnabled(True)
        self._status_label.setText(f"存档备份完成：{zip_path}")
        InfoBar.success("备份完成", f"存档已打包到 {zip_path}", parent=self, position=InfoBarPosition.TOP, duration=6000)

    def _on_restore_saves(self) -> None:
        zip_path, _ = QFileDialog.getOpenFileName(
            self, "选择存档备份文件", os.path.expanduser("~"), "存档备份 (*.zip)"
        )
        if not zip_path:
            return
        confirm = MessageBox(
            "确认恢复存档？",
            f"将把 {zip_path} 中的存档还原到 Steam userdata 目录，\n"
            "同名文件会先保存为 .bak 再覆盖。是否继续？",
            self,
        )
        confirm.yesButton.setText("恢复")
        confirm.cancelButton.setText("取消")
        if not confirm.exec():
            return

        def _restore_worker(path: str, archive: str):
            from core.save_backup import SaveBackupManager

            return SaveBackupManager(path).restore(archive)

        worker = AsyncWorker(_restore_worker, self._steam_path(), zip_path)
        worker.finished_with_result.connect(self._on_restore_done)
        worker.finished_with_error.connect(self._on_save_op_error)
        self._save_worker = worker
        worker.start()

    def _on_restore_done(self, summary: str) -> None:
        self._status_label.setText(f"存档恢复完成：{summary}")
        InfoBar.success("恢复完成", summary, parent=self, position=InfoBarPosition.TOP, duration=6000)

    # ── 存档备份：WebDAV ──────────────────────────────────
    def _on_webdav_test(self) -> None:
        try:
            client = self._make_webdav_client()
        except Exception as exc:  # noqa: BLE001 - WebDavError/参数错误
            InfoBar.error("WebDAV 配置无效", str(exc), parent=self, position=InfoBarPosition.TOP)
            return

        def _test_worker(client_obj):
            return client_obj.test_connection()

        self._dav_test_btn.setEnabled(False)
        worker = AsyncWorker(_test_worker, client)
        worker.finished_with_result.connect(self._on_webdav_test_done)
        worker.finished_with_error.connect(self._on_webdav_error)
        self._save_worker = worker
        worker.start()

    def _on_webdav_test_done(self, message: str) -> None:
        self._dav_test_btn.setEnabled(True)
        InfoBar.success("WebDAV 连接成功", message, parent=self, position=InfoBarPosition.TOP, duration=5000)

    def _on_webdav_backup(self) -> None:
        app_id = self._save_app_combo.currentData()
        if not app_id:
            InfoBar.warning("没有可备份的存档", "请先刷新存档列表", parent=self, position=InfoBarPosition.TOP)
            return
        try:
            client = self._make_webdav_client()
        except Exception as exc:  # noqa: BLE001
            InfoBar.error("WebDAV 配置无效", str(exc), parent=self, position=InfoBarPosition.TOP)
            return

        self._set_save_buttons_enabled(False)
        self._status_label.setText("正在打包存档并上传 WebDAV…")

        from core.webdav_client import default_device_name

        device = default_device_name()
        app_ids = [str(app_id)]

        def _dav_backup_worker(path: str, ids: list, client_obj, dev: str):
            from core.save_backup import SaveBackupManager
            from utils.path_manager import PathManager
            import time as _time

            out_dir = PathManager.cache_dir() / "save_backup"
            out_dir.mkdir(parents=True, exist_ok=True)
            zip_path = SaveBackupManager(path).backup(ids, out_dir)
            remote = f"{client_obj.default_backup_dir(dev)}/{zip_path.name}"
            url = client_obj.upload(zip_path, remote)
            try:
                zip_path.unlink(missing_ok=True)
            except OSError:
                pass
            return url

        worker = AsyncWorker(_dav_backup_worker, self._steam_path(), app_ids, client, device)
        worker.finished_with_result.connect(self._on_webdav_backup_done)
        worker.finished_with_error.connect(self._on_webdav_error)
        self._save_worker = worker
        worker.start()

    def _on_webdav_backup_done(self, url: str) -> None:
        self._set_save_buttons_enabled(True)
        self._status_label.setText(f"WebDAV 备份完成：{url}")
        InfoBar.success("备份已上传", url, parent=self, position=InfoBarPosition.TOP, duration=6000)

    def _on_webdav_restore(self) -> None:
        try:
            client = self._make_webdav_client()
        except Exception as exc:  # noqa: BLE001
            InfoBar.error("WebDAV 配置无效", str(exc), parent=self, position=InfoBarPosition.TOP)
            return

        self._set_save_buttons_enabled(False)
        self._status_label.setText("正在获取 WebDAV 备份列表…")

        def _list_worker(client_obj):
            files = []
            base = client_obj.default_backup_dir()
            for entry in client_obj.list_dir(base):
                if entry.is_dir:
                    for sub in client_obj.list_dir(f"{base}/{entry.name}"):
                        if not sub.is_dir and sub.name.endswith(".zip"):
                            files.append((entry.name, sub))
                elif entry.name.endswith(".zip"):
                    files.append(("（根目录）", entry))
            return [
                f"{device} / {f.name}（{_human_size(f.size)}）"
                for device, f in files
            ], [f for _, f in files]

        worker = AsyncWorker(_list_worker, client)
        worker.finished_with_result.connect(self._on_webdav_list_done)
        worker.finished_with_error.connect(self._on_webdav_error)
        self._save_worker = worker
        worker.start()

    def _on_webdav_list_done(self, payload: tuple) -> None:
        labels, files = payload
        self._set_save_buttons_enabled(True)
        if not labels:
            InfoBar.info("没有远端备份", "WebDAV 上还没有任何存档备份", parent=self, position=InfoBarPosition.TOP)
            return
        selection, ok = QInputDialog.getItem(
            self, "从 WebDAV 恢复", "选择备份文件：", labels, 0, False
        )
        if not ok:
            return
        remote_file = files[labels.index(selection)]

        try:
            client = self._make_webdav_client()
        except Exception as exc:  # noqa: BLE001
            InfoBar.error("WebDAV 配置无效", str(exc), parent=self, position=InfoBarPosition.TOP)
            return

        confirm = MessageBox(
            "确认从 WebDAV 恢复存档？",
            f"将下载 {remote_file.name} 并还原到本机 Steam userdata，\n"
            "同名文件先保存为 .bak 再覆盖。是否继续？",
            self,
        )
        confirm.yesButton.setText("下载并恢复")
        confirm.cancelButton.setText("取消")
        if not confirm.exec():
            return

        self._set_save_buttons_enabled(False)
        self._status_label.setText("正在下载并恢复存档…")

        def _restore_worker(path: str, client_obj, remote: object):
            from core.save_backup import SaveBackupManager
            from utils.path_manager import PathManager

            local = PathManager.cache_dir() / "save_backup" / remote.name
            client_obj.download(remote.path, local)
            return SaveBackupManager(path).restore(local)

        worker = AsyncWorker(_restore_worker, self._steam_path(), client, remote_file)
        worker.finished_with_result.connect(self._on_restore_done)
        worker.finished_with_error.connect(self._on_webdav_error)
        self._save_worker = worker
        worker.start()

    def _on_webdav_error(self, message: str) -> None:
        self._set_save_buttons_enabled(True)
        self._dav_test_btn.setEnabled(True)
        self._status_label.setText(f"WebDAV 操作失败：{message}")
        InfoBar.error("WebDAV 操作失败", message, parent=self, position=InfoBarPosition.TOP, duration=8000)

    def _on_save_op_error(self, message: str) -> None:
        self._set_save_buttons_enabled(True)
        self._status_label.setText(f"存档操作失败：{message}")
        InfoBar.error("存档操作失败", message, parent=self, position=InfoBarPosition.TOP, duration=6000)

    def _set_save_buttons_enabled(self, enabled: bool) -> None:
        for button in (
            self._save_reload_btn,
            self._save_backup_btn,
            self._save_restore_btn,
            self._dav_backup_btn,
            self._dav_restore_btn,
        ):
            button.setEnabled(enabled)

    # ── 辅助 ──────────────────────────────────────────────
    def _set_busy(self, busy: bool, message: str) -> None:
        for button in (
            self._scan_btn,
            self._complete_btn,
            self._import_btn,
            self._sync_btn,
            self._select_btn,
            self._delete_selected_btn,
            self._orphan_btn,
            self._fix_btn,
        ):
            button.setEnabled(not busy)
        self._status_label.setText(message)
