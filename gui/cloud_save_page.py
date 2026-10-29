"""云存档管理页面。

在原有「清单管理 → 存档备份」基础上提供独立视图：

* 按 Steam 账号筛选存档；
* 查看每个 AppID 的存档大小 / 文件数 / 最后修改时间；
* 一键备份选中游戏、从 zip 还原、打开存档目录；
* 显示 ``[cloud]`` 云存档重定向的当前状态。

底层复用 :mod:`core.save_backup`，不重复实现备份格式。
"""
from __future__ import annotations

import os
import time

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ComboBox,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
)

from core.app_paths import app_data_dir
from core.manifest_cache import ManifestCacheManager
from core.save_backup import SaveBackupError, SaveBackupManager
from utils.async_worker import AsyncWorker
from utils.logger import setup_logger

logger = setup_logger(__name__)


def _human_size(size: int) -> str:
    value = float(max(size, 0))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class CloudSavePage(ScrollArea):
    """云存档管理。"""

    settings_requested = pyqtSignal()

    def __init__(self, bridge, game_manager=None, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self._bridge = bridge
        self._game_manager = game_manager
        self._accounts: list = []
        self._apps: list = []
        self._names: dict[str, str] = {}
        self._workers: list[AsyncWorker] = []
        self._loaded = False
        self.setObjectName("cloudSavePage")
        self._init_ui()

    # ── UI ────────────────────────────────────────────────
    def _init_ui(self) -> None:
        container = QWidget()
        self.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(14)

        title = StrongBodyLabel("云存档管理", container)
        title.setStyleSheet("font-size: 24px;")
        layout.addWidget(title)

        subtitle = BodyLabel(
            "备份 / 还原 Steam userdata 中的云存档。删除游戏或切换账号前先备份，"
            "可以避免进度丢失。",
            container,
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(subtitle)

        # 云存档重定向状态
        self._cloud_card = CardWidget(container)
        cloud_layout = QVBoxLayout(self._cloud_card)
        cloud_layout.setContentsMargins(18, 14, 18, 14)
        cloud_layout.setSpacing(6)
        cloud_title = StrongBodyLabel("云存档重定向（[cloud]）", self._cloud_card)
        cloud_layout.addWidget(cloud_title)
        self._cloud_status = CaptionLabel("未检测", self._cloud_card)
        self._cloud_status.setWordWrap(True)
        self._cloud_status.setStyleSheet("color: #888;")
        cloud_layout.addWidget(self._cloud_status)
        cloud_row = QHBoxLayout()
        cloud_row.addStretch()
        cloud_settings_btn = PushButton(FluentIcon.SETTING, "打开云存档设置", self._cloud_card)
        cloud_settings_btn.clicked.connect(self.settings_requested.emit)
        cloud_row.addWidget(cloud_settings_btn)
        cloud_layout.addLayout(cloud_row)
        layout.addWidget(self._cloud_card)

        # 工具栏
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        toolbar.addWidget(BodyLabel("账号：", container))
        self._account_combo = ComboBox(container)
        self._account_combo.setMinimumWidth(220)
        self._account_combo.currentIndexChanged.connect(self._render_table)
        toolbar.addWidget(self._account_combo)

        self._refresh_btn = PrimaryPushButton(FluentIcon.SYNC, "刷新", container)
        self._refresh_btn.clicked.connect(self._load)
        toolbar.addWidget(self._refresh_btn)

        self._backup_btn = PushButton(FluentIcon.SAVE, "备份选中", container)
        self._backup_btn.clicked.connect(self._on_backup)
        toolbar.addWidget(self._backup_btn)

        self._restore_btn = PushButton(FluentIcon.HISTORY, "从备份还原", container)
        self._restore_btn.clicked.connect(self._on_restore)
        toolbar.addWidget(self._restore_btn)

        self._open_backup_btn = PushButton(FluentIcon.FOLDER, "打开备份目录", container)
        self._open_backup_btn.clicked.connect(self._on_open_backup_dir)
        toolbar.addWidget(self._open_backup_btn)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        self._table = QTableWidget(0, 6, container)
        self._table.setHorizontalHeaderLabels(
            ["游戏", "AppID", "账号", "文件数", "大小", "操作"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setMinimumHeight(260)
        layout.addWidget(self._table)

        self._status_label = BodyLabel("正在读取存档目录…", container)
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(self._status_label)
        layout.addStretch(1)

    def showEvent(self, event):  # noqa: N802 - Qt 接口
        super().showEvent(event)
        self._refresh_cloud_status()
        if not self._loaded:
            self._load()

    # ── 数据加载 ──────────────────────────────────────────
    def _steam_path(self) -> str:
        try:
            return self._bridge.get_steam_path() if self._bridge else ""
        except Exception:
            return ""

    @staticmethod
    def _load_worker(steam_path: str) -> dict:
        manager = SaveBackupManager(steam_path)
        return {
            "accounts": manager.list_accounts(),
            "apps": manager.list_save_apps(),
            "names": ManifestCacheManager(steam_path).installed_app_names(),
        }

    def _load(self) -> None:
        steam_path = self._steam_path()
        if not steam_path:
            self._status_label.setText("未检测到 Steam 安装目录，请先在「设置」中指定。")
            return
        self._refresh_btn.setEnabled(False)
        self._status_label.setText("正在读取存档目录…")
        worker = AsyncWorker(self._load_worker, steam_path)
        worker.finished_with_result.connect(self._on_loaded)
        worker.finished_with_error.connect(self._on_load_error)
        self._workers.append(worker)
        worker.start()

    def _on_loaded(self, result: dict) -> None:
        self._refresh_btn.setEnabled(True)
        self._loaded = True
        self._accounts = result.get("accounts") or []
        self._apps = result.get("apps") or []
        self._names = result.get("names") or {}

        self._account_combo.blockSignals(True)
        current = self._account_combo.currentData()
        self._account_combo.clear()
        self._account_combo.addItem("全部账号", "")
        for account in self._accounts:
            self._account_combo.addItem(
                f"{account.steam_id}（{account.app_count} 个游戏 / {_human_size(account.total_size)}）",
                account.steam_id,
            )
        index = self._account_combo.findData(current)
        self._account_combo.setCurrentIndex(index if index >= 0 else 0)
        self._account_combo.blockSignals(False)

        self._render_table()
        total_size = sum(app.total_size for app in self._apps)
        self._status_label.setText(
            f"共 {len(self._accounts)} 个账号、{len(self._apps)} 个有存档的游戏，"
            f"合计 {_human_size(total_size)}。"
        )

    def _on_load_error(self, message: str) -> None:
        self._refresh_btn.setEnabled(True)
        self._status_label.setText(f"读取失败：{message}")

    def _render_table(self) -> None:
        sid = self._account_combo.currentData() or ""
        apps = [app for app in self._apps if not sid or sid in app.steam_ids]
        self._table.setRowCount(len(apps))
        for row, app in enumerate(apps):
            name = self._names.get(app.app_id) or f"AppID {app.app_id}"
            self._table.setItem(row, 0, QTableWidgetItem(name))
            self._table.setItem(row, 1, QTableWidgetItem(app.app_id))
            self._table.setItem(row, 2, QTableWidgetItem(", ".join(app.steam_ids)))
            self._table.setItem(row, 3, QTableWidgetItem(str(app.file_count)))
            self._table.setItem(row, 4, QTableWidgetItem(_human_size(app.total_size)))

            ops = QWidget(self._table)
            ops_layout = QHBoxLayout(ops)
            ops_layout.setContentsMargins(2, 2, 2, 2)
            ops_layout.setSpacing(6)
            open_btn = PushButton("打开", ops)
            open_btn.clicked.connect(
                lambda _=False, a=app.app_id, s=(sid or app.steam_ids[0]): self._on_open_save(a, s)
            )
            ops_layout.addWidget(open_btn)
            ops_layout.addStretch()
            self._table.setCellWidget(row, 5, ops)

    def _selected_app_ids(self) -> list[str]:
        rows = sorted({index.row() for index in self._table.selectedIndexes()})
        app_ids: list[str] = []
        for row in rows:
            item = self._table.item(row, 1)
            if item and item.text() and item.text() not in app_ids:
                app_ids.append(item.text())
        return app_ids

    # ── 操作 ──────────────────────────────────────────────
    def _backup_dir(self) -> str:
        target = app_data_dir() / "save_backups"
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    def _on_backup(self) -> None:
        app_ids = self._selected_app_ids()
        if not app_ids:
            InfoBar.warning(
                "未选择游戏",
                "请先在表格中选择要备份存档的游戏（可多选）。",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3500,
            )
            return
        dest = QFileDialog.getExistingDirectory(
            self, "选择存档备份目录", self._backup_dir()
        )
        if not dest:
            return
        steam_path = self._steam_path()

        def worker() -> str:
            return str(SaveBackupManager(steam_path).backup(app_ids, dest))

        task = AsyncWorker(worker)
        task.finished_with_result.connect(self._on_backup_done)
        task.finished_with_error.connect(self._on_backup_error)
        self._workers.append(task)
        self._status_label.setText(f"正在备份 {len(app_ids)} 个游戏的存档…")
        task.start()

    def _on_backup_done(self, zip_path: str) -> None:
        self._status_label.setText(f"备份完成：{zip_path}")
        InfoBar.success(
            "备份完成",
            zip_path,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000,
        )

    def _on_backup_error(self, message: str) -> None:
        self._status_label.setText(f"备份失败：{message}")
        InfoBar.error(
            "备份失败",
            message,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000,
        )

    def _on_restore(self) -> None:
        zip_path, _ = QFileDialog.getOpenFileName(
            self, "选择存档备份", self._backup_dir(), "Zip 备份 (*.zip)"
        )
        if not zip_path:
            return
        from PyQt6.QtWidgets import QMessageBox

        if (
            QMessageBox.question(
                self,
                "还原存档",
                "还原会用备份中的文件覆盖当前存档，已存在的文件会先改名为 .bak。\n"
                "确定继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        steam_path = self._steam_path()

        def worker() -> str:
            return SaveBackupManager(steam_path).restore(zip_path)

        task = AsyncWorker(worker)
        task.finished_with_result.connect(self._on_restore_done)
        task.finished_with_error.connect(self._on_restore_error)
        self._workers.append(task)
        self._status_label.setText("正在还原存档…")
        task.start()

    def _on_restore_done(self, summary: str) -> None:
        self._status_label.setText(summary)
        InfoBar.success(
            "还原完成",
            summary,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000,
        )
        self._load()

    def _on_restore_error(self, message: str) -> None:
        self._status_label.setText(f"还原失败：{message}")
        InfoBar.error(
            "还原失败",
            message,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000,
        )

    def _on_open_save(self, app_id: str, steam_id: str) -> None:
        path = SaveBackupManager(self._steam_path()).save_dir(steam_id, app_id)
        if path is None:
            InfoBar.warning(
                "目录不存在",
                f"未找到账号 {steam_id} 的 AppID {app_id} 存档目录。",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3500,
            )
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as exc:
            InfoBar.error(
                "打开失败",
                str(exc),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000,
            )

    def _on_open_backup_dir(self) -> None:
        try:
            os.startfile(self._backup_dir())  # type: ignore[attr-defined]
        except OSError as exc:
            InfoBar.error(
                "打开失败",
                str(exc),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000,
            )

    def _refresh_cloud_status(self) -> None:
        if not self._bridge:
            self._cloud_status.setText("桥接未初始化")
            return
        try:
            enabled = bool(self._bridge.get_kernel_config("cloud", "enabled", False))
            library = str(
                self._bridge.get_kernel_config("cloud", "library", "cloud_redirect.dll")
                or "cloud_redirect.dll"
            )
            path = self._bridge.get_cloud_redirect_path()
        except Exception as exc:  # noqa: BLE001
            self._cloud_status.setText(f"读取云存档配置失败：{exc}")
            return
        if not enabled:
            self._cloud_status.setText("未启用。启用后需要 Steam 重启才能加载 cloud_redirect.dll。")
            self._cloud_status.setStyleSheet("color: #888;")
        elif path:
            self._cloud_status.setText(f"已启用，检测到 {path}。")
            self._cloud_status.setStyleSheet("color: #52c41a;")
        else:
            self._cloud_status.setText(
                f"已启用，但未找到 {library}；请把该 DLL 放到 Steam 根目录。"
            )
            self._cloud_status.setStyleSheet("color: #fa8c16;")

    def shutdown(self) -> None:
        for worker in self._workers:
            try:
                worker.cancel()
                worker.wait(1500)
                worker.deleteLater()
            except Exception:
                pass
        self._workers.clear()
