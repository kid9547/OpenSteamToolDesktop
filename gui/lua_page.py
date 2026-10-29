"""Lua 规则管理页面。

* 同时展示 ``config/lua`` 与 ``config/stplug-in`` 的规则；
* 一键启用 / 禁用（``.lua`` ↔ ``.lua.disabled``）；
* 检测同一 AppID 的重复声明；
* 从本地 appmanifest / 现有 Lua 中自动补全 Depot、GID 与 Key。
"""
from __future__ import annotations

import os

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
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
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
)

from core.lua_rules import (
    LuaRuleError,
    apply_cache_bindings,
    cache_bindings,
    scan_lua_rules,
    set_enabled,
)
from core.manifest_cache import ManifestCacheManager
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


class LuaRulesPage(ScrollArea):
    """Lua 规则管理。"""

    rules_changed = pyqtSignal()

    def __init__(self, bridge, game_manager=None, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self._bridge = bridge
        self._game_manager = game_manager
        self._rules: list = []
        self._names: dict[str, str] = {}
        self._workers: list[AsyncWorker] = []
        self._loaded = False
        self.setObjectName("luaRulesPage")
        self._init_ui()

    def _init_ui(self) -> None:
        container = QWidget()
        self.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(14)

        title = StrongBodyLabel("Lua 规则管理", container)
        title.setStyleSheet("font-size: 24px;")
        layout.addWidget(title)

        subtitle = BodyLabel(
            "管理 config/lua 与 config/stplug-in 中的规则。禁用会把文件改名为 "
            ".lua.disabled，Steam 不会加载；随时可以恢复。",
            container,
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(subtitle)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self._refresh_btn = PrimaryPushButton(FluentIcon.SYNC, "刷新规则", container)
        self._refresh_btn.clicked.connect(self._load)
        toolbar.addWidget(self._refresh_btn)

        self._enable_btn = PushButton(FluentIcon.PLAY, "启用选中", container)
        self._enable_btn.clicked.connect(lambda: self._on_toggle(True))
        toolbar.addWidget(self._enable_btn)

        self._disable_btn = PushButton(FluentIcon.PAUSE, "禁用选中", container)
        self._disable_btn.clicked.connect(lambda: self._on_toggle(False))
        toolbar.addWidget(self._disable_btn)

        self._autofill_btn = PushButton(FluentIcon.DOWNLOAD, "从 Steam 缓存补全", container)
        self._autofill_btn.clicked.connect(self._on_autofill)
        toolbar.addWidget(self._autofill_btn)

        self._open_btn = PushButton(FluentIcon.FOLDER, "打开所在目录", container)
        self._open_btn.clicked.connect(self._on_open_dir)
        toolbar.addWidget(self._open_btn)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        self._table = QTableWidget(0, 7, container)
        self._table.setHorizontalHeaderLabels(
            ["状态", "AppID", "游戏名", "目录", "大小", "冲突", "操作"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setMinimumHeight(300)
        layout.addWidget(self._table)

        self._status_label = BodyLabel("正在扫描 Lua 规则…", container)
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(self._status_label)

        note = CaptionLabel(
            "「从 Steam 缓存补全」只读取本机 appmanifest 和现有 Lua 中已有的 "
            "Depot Key，不会联网下载密钥；写入前会自动生成 .lua.bak。",
            container,
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888;")
        layout.addWidget(note)
        layout.addStretch(1)

    def showEvent(self, event):  # noqa: N802 - Qt 接口
        super().showEvent(event)
        if not self._loaded:
            self._load()

    def _steam_path(self) -> str:
        try:
            return self._bridge.get_steam_path() if self._bridge else ""
        except Exception:
            return ""

    def _extra_dirs(self) -> list[str]:
        if not self._game_manager:
            return []
        try:
            lua_dir = self._game_manager.get_lua_dir()
            return [lua_dir] if lua_dir else []
        except Exception:
            return []

    @staticmethod
    def _load_worker(steam_path: str, extra_dirs: list[str]) -> dict:
        return {
            "rules": scan_lua_rules(steam_path, extra_dirs),
            "names": ManifestCacheManager(steam_path).installed_app_names(),
        }

    def _load(self) -> None:
        steam_path = self._steam_path()
        if not steam_path:
            self._status_label.setText("未检测到 Steam 安装目录，请先在「设置」中指定。")
            return
        self._refresh_btn.setEnabled(False)
        self._status_label.setText("正在扫描 Lua 规则…")
        worker = AsyncWorker(self._load_worker, steam_path, self._extra_dirs())
        worker.finished_with_result.connect(self._on_loaded)
        worker.finished_with_error.connect(self._on_load_error)
        self._workers.append(worker)
        worker.start()

    def _on_loaded(self, result: dict) -> None:
        self._refresh_btn.setEnabled(True)
        self._loaded = True
        self._rules = result.get("rules") or []
        self._names = result.get("names") or {}
        self._render()
        enabled = sum(1 for rule in self._rules if rule.enabled)
        conflicts = sum(1 for rule in self._rules if rule.conflicts)
        self._status_label.setText(
            f"共 {len(self._rules)} 个规则（启用 {enabled}，禁用 {len(self._rules) - enabled}），"
            f"冲突 {conflicts} 个。"
        )

    def _on_load_error(self, message: str) -> None:
        self._refresh_btn.setEnabled(True)
        self._status_label.setText(f"扫描失败：{message}")

    def _render(self) -> None:
        self._table.setRowCount(len(self._rules))
        for row, rule in enumerate(self._rules):
            status_item = QTableWidgetItem(rule.status)
            status_item.setForeground(
                QColor("#52c41a") if rule.enabled else QColor("#888888")
            )
            self._table.setItem(row, 0, status_item)
            self._table.setItem(row, 1, QTableWidgetItem(rule.app_id))
            name = rule.name or self._names.get(rule.app_id) or f"AppID {rule.app_id}"
            self._table.setItem(row, 2, QTableWidgetItem(name))
            self._table.setItem(row, 3, QTableWidgetItem(rule.directory))
            self._table.setItem(row, 4, QTableWidgetItem(_human_size(rule.size)))
            conflict_item = QTableWidgetItem("；".join(rule.conflicts))
            if rule.conflicts:
                conflict_item.setForeground(QColor("#cf1322"))
                for column in range(6):
                    item = self._table.item(row, column)
                    if item:
                        item.setBackground(QColor("#fff1f0"))
            self._table.setItem(row, 5, conflict_item)

            ops = QWidget(self._table)
            ops_layout = QHBoxLayout(ops)
            ops_layout.setContentsMargins(2, 2, 2, 2)
            ops_layout.setSpacing(6)
            toggle_btn = PushButton("禁用" if rule.enabled else "启用", ops)
            toggle_btn.clicked.connect(
                lambda _=False, r=rule: self._toggle_one(r, not r.enabled)
            )
            ops_layout.addWidget(toggle_btn)
            fill_btn = PushButton("补全", ops)
            fill_btn.clicked.connect(lambda _=False, r=rule: self._autofill_one(r))
            ops_layout.addWidget(fill_btn)
            ops_layout.addStretch()
            self._table.setCellWidget(row, 6, ops)

    def _selected_rules(self) -> list:
        rows = sorted({index.row() for index in self._table.selectedIndexes()})
        return [self._rules[row] for row in rows if 0 <= row < len(self._rules)]

    def _on_toggle(self, enabled: bool) -> None:
        rules = [rule for rule in self._selected_rules() if rule.enabled != enabled]
        if not rules:
            return
        changed = 0
        errors: list[str] = []
        for rule in rules:
            try:
                set_enabled(rule.path, enabled)
                changed += 1
            except (LuaRuleError, OSError) as exc:
                errors.append(f"{rule.filename}: {exc}")
        if changed:
            self.rules_changed.emit()
        if errors:
            InfoBar.warning(
                "部分规则未更新",
                "；".join(errors),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )
        elif changed:
            InfoBar.success(
                "已更新",
                f"已{'启用' if enabled else '禁用'} {changed} 个规则。",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
        self._load()

    def _toggle_one(self, rule, enabled: bool) -> None:
        try:
            set_enabled(rule.path, enabled)
        except (LuaRuleError, OSError) as exc:
            InfoBar.error(
                "操作失败",
                str(exc),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000,
            )
            return
        self.rules_changed.emit()
        self._load()

    def _autofill_one(self, rule) -> None:
        self._start_autofill([rule])

    def _on_autofill(self) -> None:
        rules = self._selected_rules()
        if not rules:
            InfoBar.warning(
                "未选择规则",
                "请先选择要补全的 Lua 规则（可多选）。",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3500,
            )
            return
        self._start_autofill(rules)

    def _start_autofill(self, rules: list) -> None:
        steam_path = self._steam_path()
        if not steam_path:
            return
        paths = [(rule.app_id, rule.path) for rule in rules]
        self._autofill_btn.setEnabled(False)
        self._status_label.setText(f"正在为 {len(paths)} 个规则补全缓存绑定…")
        worker = AsyncWorker(self._autofill_worker, steam_path, self._extra_dirs(), paths)
        worker.finished_with_result.connect(self._on_autofill_done)
        worker.finished_with_error.connect(self._on_autofill_error)
        self._workers.append(worker)
        worker.start()

    @staticmethod
    def _autofill_worker(
        steam_path: str, extra_dirs: list[str], paths: list[tuple[str, str]]
    ) -> dict:
        results: list[str] = []
        errors: list[str] = []
        for app_id, lua_path in paths:
            try:
                bindings = cache_bindings(steam_path, app_id, extra_dirs)
                if not bindings.has_data:
                    results.append(f"{os.path.basename(lua_path)}：没有可用的缓存数据")
                    continue
                summary = apply_cache_bindings(lua_path, bindings)
                results.append(
                    f"{os.path.basename(lua_path)}：新增 {summary['added']} 个 Depot，"
                    f"补 Key {summary['keys']} 个，更新清单 {summary['manifests']} 个"
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{os.path.basename(lua_path)}：{exc}")
        return {"results": results, "errors": errors}

    def _on_autofill_done(self, result: dict) -> None:
        self._autofill_btn.setEnabled(True)
        messages = result.get("results") or []
        errors = result.get("errors") or []
        self._status_label.setText("；".join(messages + errors) or "没有可补全的数据。")
        if errors:
            InfoBar.warning(
                "部分规则补全失败",
                "；".join(errors),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=6000,
            )
        else:
            InfoBar.success(
                "补全完成",
                "；".join(messages) or "没有可补全的数据。",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=6000,
            )
        self.rules_changed.emit()
        self._load()

    def _on_autofill_error(self, message: str) -> None:
        self._autofill_btn.setEnabled(True)
        self._status_label.setText(f"补全失败：{message}")
        InfoBar.error(
            "补全失败",
            message,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000,
        )

    def _on_open_dir(self) -> None:
        rules = self._selected_rules()
        if not rules:
            return
        try:
            os.startfile(rules[0].directory)  # type: ignore[attr-defined]
        except OSError as exc:
            InfoBar.error(
                "打开失败",
                str(exc),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000,
            )

    def shutdown(self) -> None:
        for worker in self._workers:
            try:
                worker.cancel()
                worker.wait(1500)
                worker.deleteLater()
            except Exception:
                pass
        self._workers.clear()
