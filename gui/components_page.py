"""组件状态中心页面。

把「文件是否存在」升级为「Steam 进程实际加载了什么」：
显示三个内核 DLL 的部署状态、加载路径、大小、SHA256，并提供重新部署
与安全移除（Steam 关闭时）入口。
"""
from __future__ import annotations

import os

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
)

from core.component_status import collect_components, uninstall_components
from utils.logger import setup_logger

logger = setup_logger(__name__)


def _human_size(size: int) -> str:
    value = float(max(size, 0))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class ComponentsPage(ScrollArea):
    """组件状态中心。"""

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self._bridge = bridge
        self._report = None
        self.setObjectName("componentsPage")
        self._init_ui()

    def _init_ui(self) -> None:
        container = QWidget()
        self.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(16)

        title = StrongBodyLabel("组件状态中心", container)
        title.setStyleSheet("font-size: 24px;")
        layout.addWidget(title)

        subtitle = BodyLabel(
            "确认三个内核 DLL 是否已被 Steam 进程真正加载。文件存在不等于生效，"
            "同名系统 DLL 也可能被误认为组件。",
            container,
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(subtitle)

        self._status_card = CardWidget(container)
        status_layout = QVBoxLayout(self._status_card)
        status_layout.setContentsMargins(18, 14, 18, 14)
        status_layout.setSpacing(6)
        self._summary_label = StrongBodyLabel("尚未检测", self._status_card)
        self._summary_label.setStyleSheet("font-size: 15px;")
        status_layout.addWidget(self._summary_label)
        self._detail_label = CaptionLabel("", self._status_card)
        self._detail_label.setWordWrap(True)
        self._detail_label.setStyleSheet("color: #888;")
        status_layout.addWidget(self._detail_label)
        layout.addWidget(self._status_card)

        self._table = QTableWidget(0, 6, container)
        self._table.setHorizontalHeaderLabels(
            ["组件", "部署", "Steam 加载", "大小", "SHA256", "说明"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self._table.setMinimumHeight(220)
        layout.addWidget(self._table)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self._refresh_btn = PrimaryPushButton(FluentIcon.SYNC, "刷新状态", container)
        self._refresh_btn.clicked.connect(self._refresh)
        buttons.addWidget(self._refresh_btn)

        self._redeploy_btn = PushButton(FluentIcon.DOWNLOAD, "重新部署组件", container)
        self._redeploy_btn.clicked.connect(self._on_redeploy)
        buttons.addWidget(self._redeploy_btn)

        self._open_btn = PushButton(FluentIcon.FOLDER, "打开 Steam 目录", container)
        self._open_btn.clicked.connect(self._on_open_folder)
        buttons.addWidget(self._open_btn)

        self._uninstall_btn = PushButton(FluentIcon.DELETE, "移除组件", container)
        self._uninstall_btn.clicked.connect(self._on_uninstall)
        buttons.addWidget(self._uninstall_btn)
        buttons.addStretch()
        layout.addLayout(buttons)

        note = CaptionLabel(
            "说明：dwmapi.dll 可能被 Windows 系统同名模块抢先加载，只要 "
            "OpenSteamTool.dll 显示“已从 Steam 目录加载”，注入就是成功的。"
            "移除组件前必须先完全退出 Steam，程序会自动把原文件备份到应用数据目录。",
            container,
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888;")
        layout.addWidget(note)
        layout.addStretch(1)

    def showEvent(self, event):  # noqa: N802 - Qt 接口
        super().showEvent(event)
        self._refresh()

    def _steam_path(self) -> str:
        try:
            return self._bridge.get_steam_path() if self._bridge else ""
        except Exception:
            return ""

    def _source_dir(self) -> str:
        try:
            return self._bridge.get_dll_path() if self._bridge else ""
        except Exception:
            return ""

    def _refresh(self) -> None:
        steam_path = self._steam_path()
        if not steam_path:
            self._summary_label.setText("未检测到 Steam 安装目录")
            self._detail_label.setText("请先在「设置」中指定 Steam 路径。")
            self._table.setRowCount(0)
            return
        try:
            self._report = collect_components(steam_path, self._source_dir())
        except Exception as exc:  # noqa: BLE001
            logger.exception("组件状态检测失败")
            self._summary_label.setText("组件状态检测失败")
            self._detail_label.setText(str(exc))
            return

        report = self._report
        self._summary_label.setText(report.summary)
        pids = ", ".join(str(pid) for pid in report.steam_pids) or "未运行"
        version = report.kernel_version or "未知"
        self._detail_label.setText(
            f"Steam：{report.steam_path}    进程 PID：{pids}    "
            f"内核版本：{version}    来源：{report.source_dir or '未知'}"
        )

        self._table.setRowCount(len(report.components))
        for row, component in enumerate(report.components):
            self._table.setItem(row, 0, QTableWidgetItem(component.name))
            deployed = "已部署" if component.deployed else "缺失"
            self._table.setItem(row, 1, QTableWidgetItem(deployed))
            if component.loaded_from_steam:
                loaded = "已加载（Steam）"
            elif component.loaded:
                loaded = "已加载（系统）"
            elif report.steam_running:
                loaded = "未检测到"
            else:
                loaded = "Steam 未运行"
            loaded_item = QTableWidgetItem(loaded)
            if component.loaded_from_steam:
                loaded_item.setForeground(QColor("#52c41a"))
            elif component.loaded:
                loaded_item.setForeground(QColor("#fa8c16"))
            else:
                loaded_item.setForeground(QColor("#cf1322"))
            self._table.setItem(row, 2, loaded_item)
            self._table.setItem(row, 3, QTableWidgetItem(_human_size(component.file_size)))
            self._table.setItem(
                row, 4, QTableWidgetItem((component.sha256[:16] + "…") if component.sha256 else "—")
            )
            note = component.note
            if component.loaded_path and not component.loaded_from_steam:
                note += f"\n{component.loaded_path}"
            note_item = QTableWidgetItem(note)
            note_item.setToolTip(component.loaded_path or component.file_path)
            self._table.setItem(row, 5, note_item)

    def _on_redeploy(self) -> None:
        if not self._bridge:
            return
        if self._report and self._report.steam_running:
            answer = QMessageBox.question(
                self,
                "重新部署组件",
                "Steam 正在运行，DLL 可能被占用导致复制失败。\n"
                "仍要尝试重新部署吗？建议先完全退出 Steam。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        ok, message = self._bridge.inject()
        if ok:
            InfoBar.success(
                "部署完成",
                message,
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3500,
            )
        else:
            InfoBar.error(
                "部署失败",
                message,
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )
        self._refresh()

    def _on_open_folder(self) -> None:
        steam_path = self._steam_path()
        if not steam_path:
            return
        try:
            os.startfile(steam_path)  # type: ignore[attr-defined]
        except OSError as exc:
            InfoBar.error(
                "打开失败",
                str(exc),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000,
            )

    def _on_uninstall(self) -> None:
        steam_path = self._steam_path()
        if not steam_path:
            return
        answer = QMessageBox.question(
            self,
            "移除组件",
            "将把 OpenSteamTool.dll、dwmapi.dll、xinput1_4.dll 从 Steam 目录移除，"
            "并备份到应用数据目录。\n\n移除后需要重新部署才能继续使用。确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        ok, message = uninstall_components(steam_path)
        if ok:
            InfoBar.success(
                "已移除",
                message,
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )
        else:
            InfoBar.warning(
                "未移除",
                message,
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )
        self._refresh()

    def shutdown(self) -> None:
        """兼容 MainWindow 的统一生命周期清理。"""
        return
