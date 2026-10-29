"""Manifest 版本固定对话框。

列出某个游戏 Lua 中声明的 Depot / GID，允许逐项固定。固定后：

* 重新生成 Lua 会优先使用固定 GID；
* 「一键补全」不会覆盖固定 GID；
* 取消固定后恢复跟随最新版本。
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
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
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
)

from core import manifest_pins
from core.complete_manifest_service import parse_lua_bindings
from core.manifest_cache import ManifestCacheManager


class ManifestPinDialog(QDialog):
    """逐 Depot 固定清单版本。"""

    def __init__(
        self,
        app_id: str,
        game_name: str,
        lua_path: str,
        steam_path: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self._app_id = str(app_id)
        self._lua_path = lua_path
        self._steam_path = steam_path
        self._bindings = parse_lua_bindings(lua_path)
        self._pins = manifest_pins.get_pins(self._app_id)
        self._rows: list[tuple[str, int]] = []

        self.setWindowTitle(f"固定清单版本 — {game_name or app_id}")
        self.resize(760, 480)
        self._init_ui(game_name)

    def _init_ui(self, game_name: str) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        title = StrongBodyLabel(f"固定清单版本 — {game_name or self._app_id}", self)
        title.setStyleSheet("font-size: 16px;")
        layout.addWidget(title)

        hint = BodyLabel(
            "勾选要固定的 Depot，并确认 GID。固定后补全/更新不会覆盖该版本；"
            "取消勾选即可恢复跟随。",
            self,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(hint)

        self._table = QTableWidget(0, 5, self)
        self._table.setHorizontalHeaderLabels(
            ["固定", "Depot", "当前 GID", "清单文件", "固定 GID"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table, 1)

        cache = ManifestCacheManager(self._steam_path)
        entries = [
            (depot, binding)
            for depot, binding in sorted(self._bindings.items(), key=lambda kv: int(kv[0]))
            if str(binding.get("gid") or "").isdigit()
        ]
        self._table.setRowCount(len(entries))
        for row, (depot, binding) in enumerate(entries):
            gid = str(binding.get("gid") or "")
            pinned = self._pins.get(depot)
            fixed_gid = str(pinned.get("gid")) if pinned else gid
            check_item = QTableWidgetItem()
            check_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            check_item.setCheckState(
                Qt.CheckState.Checked if pinned else Qt.CheckState.Unchecked
            )
            self._table.setItem(row, 0, check_item)
            self._table.setItem(row, 1, QTableWidgetItem(depot))
            self._table.setItem(row, 2, QTableWidgetItem(gid))
            present = cache.is_present(depot, gid)
            present_item = QTableWidgetItem("存在" if present else "缺失")
            self._table.setItem(row, 3, present_item)
            gid_item = QTableWidgetItem(fixed_gid)
            gid_item.setFlags(
                Qt.ItemFlag.ItemIsEditable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            self._table.setItem(row, 4, gid_item)
            self._rows.append((depot, int(binding.get("size") or 0)))

        if not entries:
            empty = CaptionLabel("当前 Lua 没有 setManifestid 绑定，无法固定版本。", self)
            empty.setStyleSheet("color: #888;")
            layout.addWidget(empty)

        buttons = QHBoxLayout()
        select_all = PushButton("全选", self)
        select_all.clicked.connect(lambda: self._set_all(True))
        buttons.addWidget(select_all)
        clear_all = PushButton("全不选", self)
        clear_all.clicked.connect(lambda: self._set_all(False))
        buttons.addWidget(clear_all)
        buttons.addStretch()
        cancel = PushButton("取消", self)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = PrimaryPushButton(FluentIcon.SAVE, "保存固定", self)
        save.clicked.connect(self._on_save)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def _set_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item:
                item.setCheckState(state)

    def _on_save(self) -> None:
        changed = 0
        for row, (depot, size) in enumerate(self._rows):
            check_item = self._table.item(row, 0)
            gid_item = self._table.item(row, 4)
            if not check_item or not gid_item:
                continue
            if check_item.checkState() == Qt.CheckState.Checked:
                gid = gid_item.text().strip()
                if not gid.isdigit():
                    gid_item.setBackground(QColor("#cf1322"))
                    continue
                if manifest_pins.pin(self._app_id, depot, gid, size):
                    changed += 1
            else:
                changed += manifest_pins.unpin(self._app_id, depot)
        applied = manifest_pins.apply_pins_to_lua(self._app_id, self._lua_path)
        self._changed = changed
        self._applied = applied
        self.accept()

    @property
    def changed_count(self) -> int:
        return getattr(self, "_changed", 0)

    @property
    def applied_count(self) -> int:
        return getattr(self, "_applied", 0)
