"""AppID 查询与更新检测页面。

按 AppID / 游戏名 / 安装目录搜索本地 appmanifest 与 appinfo 缓存，显示
已安装 Build、Steam 本地缓存的最新 Build、Depot 清单完整性与 Lua 规则状态。
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
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
    CheckBox,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
)

from core.app_lookup import (
    LocalApp,
    build_app_index,
    search_apps,
    update_summary,
)
from utils.async_worker import AsyncWorker
from utils.logger import setup_logger

logger = setup_logger(__name__)


def _human_size(size: int) -> str:
    value = float(max(size, 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _format_timestamp(timestamp: int) -> str:
    if not timestamp:
        return "—"
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return "—"


class AppLookupPage(ScrollArea):
    """AppID 查询与更新检测。"""

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self._bridge = bridge
        self._apps: list[LocalApp] = []
        self._rows: list[LocalApp] = []
        self._workers: list[AsyncWorker] = []
        self._loaded = False
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._render)
        self.setObjectName("appLookupPage")
        self._init_ui()

    # ── UI ────────────────────────────────────────────────
    def _init_ui(self) -> None:
        container = QWidget()
        self.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(14)

        title = StrongBodyLabel("AppID 查询与更新检测", container)
        title.setStyleSheet("font-size: 24px;")
        layout.addWidget(title)

        subtitle = BodyLabel(
            "搜索本地 appmanifest 与 appinfo 缓存，核对已安装 Build、Steam 已知的"
            "最新 Build、Depot 清单是否齐全，以及是否已有 Lua 规则。",
            container,
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(subtitle)

        search_row = QHBoxLayout()
        search_row.setSpacing(10)
        self._search_edit = LineEdit(container)
        self._search_edit.setPlaceholderText("输入 AppID、游戏名或安装目录，例如 1623730 / Palworld")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._schedule_search)
        self._search_edit.returnPressed.connect(self._render)
        search_row.addWidget(self._search_edit, 1)
        self._search_btn = PrimaryPushButton(FluentIcon.SEARCH, "查询", container)
        self._search_btn.clicked.connect(self._render)
        search_row.addWidget(self._search_btn)
        self._refresh_btn = PushButton(FluentIcon.SYNC, "刷新索引", container)
        self._refresh_btn.clicked.connect(self._load_index)
        search_row.addWidget(self._refresh_btn)
        layout.addLayout(search_row)

        options = QHBoxLayout()
        options.setSpacing(14)
        self._include_uninstalled = CheckBox("包含未安装（appinfo 缓存）", container)
        self._include_uninstalled.setChecked(True)
        self._include_uninstalled.stateChanged.connect(self._render)
        options.addWidget(self._include_uninstalled)
        self._pending_only = CheckBox("仅显示待更新/异常", container)
        self._pending_only.stateChanged.connect(self._render)
        options.addWidget(self._pending_only)
        options.addStretch()
        layout.addLayout(options)

        self._summary_label = BodyLabel("正在读取本地索引…", container)
        self._summary_label.setWordWrap(True)
        self._summary_label.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(self._summary_label)

        self._table = QTableWidget(0, 8, container)
        self._table.setHorizontalHeaderLabels(
            ["AppID", "名称", "状态", "已装 Build", "最新 Build", "更新检测", "清单", "Lua"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setMinimumHeight(300)
        self._table.itemSelectionChanged.connect(self._show_details)
        layout.addWidget(self._table)

        self._detail_card = CardWidget(container)
        detail_layout = QVBoxLayout(self._detail_card)
        detail_layout.setContentsMargins(18, 14, 18, 14)
        detail_layout.setSpacing(8)
        detail_title = StrongBodyLabel("详情", self._detail_card)
        detail_layout.addWidget(detail_title)
        self._detail_label = CaptionLabel(
            "选中一行查看安装目录、清单与 Lua 规则详情。", self._detail_card
        )
        self._detail_label.setWordWrap(True)
        self._detail_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._detail_label.setStyleSheet("color: #aaa;")
        detail_layout.addWidget(self._detail_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self._copy_btn = PushButton(FluentIcon.COPY, "复制 AppID", self._detail_card)
        self._copy_btn.clicked.connect(self._copy_app_id)
        buttons.addWidget(self._copy_btn)
        self._open_install_btn = PushButton(
            FluentIcon.FOLDER, "打开安装目录", self._detail_card
        )
        self._open_install_btn.clicked.connect(self._open_install_dir)
        buttons.addWidget(self._open_install_btn)
        self._open_acf_btn = PushButton(
            FluentIcon.DOCUMENT, "打开 ACF 目录", self._detail_card
        )
        self._open_acf_btn.clicked.connect(self._open_acf_dir)
        buttons.addWidget(self._open_acf_btn)
        self._open_lua_btn = PushButton(
            FluentIcon.CODE, "打开 Lua 目录", self._detail_card
        )
        self._open_lua_btn.clicked.connect(self._open_lua_dir)
        buttons.addWidget(self._open_lua_btn)
        buttons.addStretch()
        detail_layout.addLayout(buttons)
        layout.addWidget(self._detail_card)
        layout.addStretch(1)

    def showEvent(self, event):  # noqa: N802 - Qt 接口
        super().showEvent(event)
        if not self._loaded:
            self._loaded = True
            self._load_index()

    # ── 索引 ──────────────────────────────────────────────
    def _steam_path(self) -> str:
        try:
            return self._bridge.get_steam_path() if self._bridge else ""
        except Exception:
            return ""

    def _load_index(self) -> None:
        steam_path = self._steam_path()
        if not steam_path:
            self._summary_label.setText("未检测到 Steam 安装路径，请先在设置中指定。")
            return
        self._summary_label.setText("正在读取 appmanifest 与 appinfo 缓存…")
        self._refresh_btn.setEnabled(False)
        worker = AsyncWorker(build_app_index, steam_path)
        worker.finished_with_result.connect(self._on_index_ready)
        worker.finished_with_error.connect(self._on_index_error)
        self._register_worker(worker)
        worker.start()

    def _on_index_ready(self, apps: list[LocalApp]) -> None:
        self._apps = list(apps)
        self._refresh_btn.setEnabled(True)
        self._summary_label.setText(update_summary(self._apps))
        self._render()

    def _on_index_error(self, message: str) -> None:
        self._refresh_btn.setEnabled(True)
        self._summary_label.setText(f"读取本地索引失败：{message}")

    def _schedule_search(self) -> None:
        self._search_timer.start()

    def _render(self) -> None:
        query = self._search_edit.text().strip()
        include_uninstalled = self._include_uninstalled.isChecked()
        apps = search_apps(
            self._apps, query, include_uninstalled=include_uninstalled, limit=500
        )
        if self._pending_only.isChecked():
            apps = [
                app
                for app in apps
                if app.update_pending or app.missing_manifests
            ]
        self._rows = apps
        self._table.setRowCount(len(apps))
        for row, app in enumerate(apps):
            self._set_row(row, app)

        if not self._apps:
            self._summary_label.setText("本地索引为空；请确认 Steam 路径并点击「刷新索引」。")
        elif query:
            self._summary_label.setText(
                f"搜索“{query}”匹配 {len(apps)} 条（最多显示 500 条）。"
            )
        elif self._pending_only.isChecked():
            self._summary_label.setText(
                f"待更新/异常 {len(apps)} 个。" + update_summary(self._apps)
            )
        else:
            self._summary_label.setText(update_summary(self._apps))

        if apps:
            self._table.selectRow(0)
        else:
            self._detail_label.setText("没有匹配的应用。")

    def _set_row(self, row: int, app: LocalApp) -> None:
        self._table.setItem(row, 0, QTableWidgetItem(app.app_id))
        name = app.name or "(未命名)"
        name_item = QTableWidgetItem(name)
        name_item.setToolTip(app.install_dir or name)
        self._table.setItem(row, 1, name_item)

        installed_item = QTableWidgetItem("已安装" if app.installed else "未安装")
        installed_item.setForeground(
            QColor("#52c41a" if app.installed else "#888888")
        )
        self._table.setItem(row, 2, installed_item)

        self._table.setItem(row, 3, QTableWidgetItem(app.build_id or "—"))
        self._table.setItem(row, 4, QTableWidgetItem(app.latest_build_id or "—"))

        update_item = QTableWidgetItem(app.update_status)
        if not app.installed:
            update_item.setForeground(QColor("#888888"))
        elif app.update_pending:
            update_item.setForeground(QColor("#cf1322"))
        elif app.latest_build_id:
            update_item.setForeground(QColor("#52c41a"))
        else:
            update_item.setForeground(QColor("#fa8c16"))
        tooltip = "\n".join(app.notes + app.update_reasons)
        if tooltip:
            update_item.setToolTip(tooltip)
        self._table.setItem(row, 5, update_item)

        if app.installed:
            manifest_text = (
                "完整"
                if not app.missing_manifests
                else f"缺 {len(app.missing_manifests)} 个"
            )
        else:
            manifest_text = "—"
        manifest_item = QTableWidgetItem(manifest_text)
        if app.installed and app.missing_manifests:
            manifest_item.setForeground(QColor("#cf1322"))
            manifest_item.setToolTip("\n".join(app.missing_manifests))
        self._table.setItem(row, 6, manifest_item)

        lua_item = QTableWidgetItem(app.lua_status)
        if app.lua_status == "已启用":
            lua_item.setForeground(QColor("#52c41a"))
        elif app.lua_status == "规则冲突":
            lua_item.setForeground(QColor("#cf1322"))
        elif app.lua_status == "已禁用":
            lua_item.setForeground(QColor("#fa8c16"))
        self._table.setItem(row, 7, lua_item)

    # ── 详情 ──────────────────────────────────────────────
    def _selected_app(self) -> LocalApp | None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def _show_details(self) -> None:
        app = self._selected_app()
        if app is None:
            self._detail_label.setText("选中一行查看安装目录、清单与 Lua 规则详情。")
            return
        lines = [
            f"AppID：{app.app_id}    名称：{app.name or '(未命名)'}    类型：{app.app_type or '未知'}",
            f"状态：{'已安装' if app.installed else '未安装'}    "
            f"Build：{app.build_id or '—'} / 最新 {app.latest_build_id or '—'}    "
            f"大小：{_human_size(app.size_on_disk)}",
            f"安装目录：{app.install_dir or '—'}",
            f"ACF：{app.acf_path or '—'}",
            f"最后更新：{_format_timestamp(app.last_updated)}    "
            f"StateFlags：{app.state_flags}    LastOwner：{app.last_owner or '—'}",
        ]
        if app.installed_depots:
            depots = "、".join(
                f"{depot}={gid}" for depot, gid in list(app.installed_depots.items())[:6]
            )
            if len(app.installed_depots) > 6:
                depots += f" 等 {len(app.installed_depots)} 个"
            lines.append(f"已装 Depot：{depots}")
        if app.missing_manifests:
            lines.append(f"缺失清单：{'、'.join(app.missing_manifests[:8])}")
        if app.pinned_depots:
            lines.append(
                "固定清单："
                + "、".join(
                    f"{depot}={gid}" for depot, gid in list(app.pinned_depots.items())[:6]
                )
            )
        if app.lua_rules:
            lines.append("Lua 规则：" + "；".join(app.lua_rules[:4]))
        if app.update_reasons:
            lines.append("更新原因：" + "；".join(app.update_reasons))
        if app.notes:
            lines.append("备注：" + "；".join(app.notes))
        self._detail_label.setText("\n".join(lines))

    def _copy_app_id(self) -> None:
        app = self._selected_app()
        if app is None:
            return
        QApplication.clipboard().setText(app.app_id)
        InfoBar.success(
            "已复制",
            f"AppID {app.app_id} 已复制到剪贴板",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=2500,
        )

    def _open_install_dir(self) -> None:
        app = self._selected_app()
        if app is None or not app.install_dir:
            return
        self._open_path(app.install_dir)

    def _open_acf_dir(self) -> None:
        app = self._selected_app()
        if app is None or not app.acf_path:
            return
        self._open_path(str(Path(app.acf_path).parent))

    def _open_lua_dir(self) -> None:
        app = self._selected_app()
        if app and app.lua_rules:
            self._open_path(str(Path(app.lua_rules[0]).parent))
            return
        steam_path = self._steam_path()
        if steam_path:
            lua_dir = Path(steam_path) / "config" / "lua"
            self._open_path(str(lua_dir if lua_dir.is_dir() else Path(steam_path)))

    def _open_path(self, path: str) -> None:
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as exc:
            InfoBar.error(
                "打开失败",
                str(exc),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000,
            )

    # ── 生命周期 ──────────────────────────────────────────
    def _register_worker(self, worker: AsyncWorker) -> None:
        self._workers.append(worker)
        worker.finished.connect(lambda: self._cleanup_worker(worker))

    def _cleanup_worker(self, worker: AsyncWorker) -> None:
        try:
            self._workers.remove(worker)
        except ValueError:
            pass
        worker.deleteLater()

    def wait_for_workers(self) -> None:
        for worker in list(self._workers):
            try:
                worker.cancel()
                worker.wait(3000)
            except Exception:
                pass

    def shutdown(self) -> None:
        self._search_timer.stop()
        self.wait_for_workers()
