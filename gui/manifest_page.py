"""ManifestPage —— 清单管理页面。

把「清单下载不全、需要手动补」这个痛点在界面上一劳永逸地解决掉：

* **总览**：扫描 ``<Steam>/depotcache`` 与 ``<Steam>/config/depotcache`` 两个目录，
  去重统计清单数量、有效/损坏数量、占用空间、孤儿清单数量。
* **损坏校验**：按二进制魔数 ``0x71F617D0`` 校验每个清单文件是否可被 Steam 读取。
* **一键补全**：对 Lua 里配置了 ``setManifestid`` 但本地缺失/损坏的清单，
  走社区分支归档流水线补齐（详见 :mod:`core.complete_manifest_service`）。
* **孤儿清理**：找出没有被任何 Lua ``setManifestid`` 引用的清单并批量删除。
* **双目录同步**：把只在一边存在的清单镜像到另一边，避免 Steam 报「清单不可用」。
* **手动导入**：支持拖入 ``.manifest`` 文件（按 ``{depot}_{gid}.manifest`` 命名解析）。

耗时操作全部走 :class:`utils.async_worker.AsyncWorker`，界面不卡顿。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
)

from core.manifest_cache import ManifestCacheManager
from utils.async_worker import AsyncWorker
from utils.logger import setup_logger

logger = setup_logger(__name__)

_MANIFEST_NAME = re.compile(r"^(?P<depot>\d+)_(?P<gid>\d+)\.manifest$", re.IGNORECASE)


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


class ManifestPage(ScrollArea):
    """清单管理页面。"""

    # 通知其他页面（如游戏库）刷新清单就绪状态
    manifests_changed = pyqtSignal()

    def __init__(self, game_manager, bridge=None, parent=None):
        super().__init__(parent)
        self._game_manager = game_manager
        self._bridge = bridge
        self._worker: AsyncWorker | None = None
        self._records: list = []

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

    def _cache(self) -> ManifestCacheManager:
        return ManifestCacheManager(self._steam_path())

    # ── UI ────────────────────────────────────────────────
    def _init_ui(self) -> None:
        container = QWidget()
        self.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        title = StrongBodyLabel("清单管理", container)
        title.setStyleSheet("font-size: 22px;")
        layout.addWidget(title)

        self._path_label = BodyLabel("", container)
        self._path_label.setStyleSheet("color: #888; font-size: 12px;")
        self._path_label.setWordWrap(True)
        layout.addWidget(self._path_label)

        # ── 统计卡片 ──
        layout.addWidget(self._build_stats_row(container))

        # ── 操作按钮 ──
        layout.addWidget(self._build_action_row(container))

        # ── 清单不全的游戏（逐游戏审计）──
        incomplete_title = StrongBodyLabel("清单不全的游戏", container)
        incomplete_title.setStyleSheet("font-size: 14px; margin-top: 6px;")
        layout.addWidget(incomplete_title)

        self._incomplete_label = BodyLabel("尚未扫描", container)
        self._incomplete_label.setStyleSheet("color: #888; font-size: 12px;")
        self._incomplete_label.setWordWrap(True)
        layout.addWidget(self._incomplete_label)

        self._incomplete_table = QTableWidget(0, 4, container)
        self._incomplete_table.setHorizontalHeaderLabels(["AppID", "就绪/声明", "缺失清单", "损坏清单"])
        self._incomplete_table.verticalHeader().setVisible(False)
        self._incomplete_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._incomplete_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        inc_header = self._incomplete_table.horizontalHeader()
        inc_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        inc_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        inc_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        inc_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._incomplete_table.setMinimumHeight(150)
        self._incomplete_table.setMaximumHeight(240)
        layout.addWidget(self._incomplete_table)

        # ── 清单表格 ──
        self._table = QTableWidget(0, 5, container)
        self._table.setHorizontalHeaderLabels(["Depot", "Manifest GID", "状态", "大小", "所在目录"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self._table.setMinimumHeight(320)
        layout.addWidget(self._table, 1)

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
            ("total", "清单总数"),
            ("valid", "有效"),
            ("invalid", "损坏"),
            ("orphan", "孤儿"),
            ("incomplete", "清单不全的游戏"),
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

        self._fix_btn = PushButton(FluentIcon.BROOM, "删除损坏清单", row)
        self._fix_btn.clicked.connect(self._on_delete_invalid)
        box.addWidget(self._fix_btn)

        self._orphan_btn = PushButton(FluentIcon.DELETE, "清理孤儿清单", row)
        self._orphan_btn.clicked.connect(self._on_clean_orphans)
        box.addWidget(self._orphan_btn)

        box.addStretch()
        return row

    # ── 扫描 ──────────────────────────────────────────────
    def showEvent(self, event):  # noqa: N802 - Qt 接口
        super().showEvent(event)
        if not self._records:
            QTimer.singleShot(120, self._on_scan)

    def _on_scan(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        steam_path = self._steam_path()
        lua_dir = self._lua_dir()
        if not steam_path:
            self._status_label.setText("未检测到 Steam 路径，请先在「设置」中指定。")
            return

        self._scan_btn.setEnabled(False)
        self._status_label.setText("正在扫描清单目录…")
        self._path_label.setText(f"depotcache: {steam_path}\\depotcache    |    "
                                 f"config\\depotcache: {steam_path}\\config\\depotcache    |    "
                                 f"Lua: {lua_dir or '(未配置)'}")

        self._worker = AsyncWorker(self._scan_worker, steam_path, lua_dir)
        self._worker.finished_with_result.connect(self._on_scan_done)
        self._worker.finished_with_error.connect(self._on_scan_error)
        self._worker.start()

    @staticmethod
    def _scan_worker(steam_path: str, lua_dir: str) -> dict:
        cache = ManifestCacheManager(steam_path)
        records = cache.scan()
        referenced = cache.referenced_keys(lua_dir) if lua_dir else set()

        rows: list[dict] = []
        total_size = valid = invalid = orphan = 0
        for record in records:
            size = 0
            for path in record.paths:
                try:
                    size += os.path.getsize(path)
                except OSError:
                    pass
            directories = sorted(
                {Path(p).parent.name + ("/config" if Path(p).parent.parent.name == "config" else "") for p in record.paths}
            )
            is_orphan = record.key not in referenced
            rows.append(
                {
                    "depot": record.depot_id,
                    "gid": record.manifest_gid,
                    "valid": record.valid,
                    "size": size,
                    "dirs": ", ".join(directories),
                    "copies": len(record.paths),
                    "orphan": is_orphan,
                    "key": record.key,
                }
            )
            total_size += size
            if record.valid:
                valid += 1
            else:
                invalid += 1
            if is_orphan:
                orphan += 1

        return {
            "rows": rows,
            "total": len(rows),
            "valid": valid,
            "invalid": invalid,
            "orphan": orphan,
            "size": total_size,
            "referenced": len(referenced),
            "audits": [
                {
                    "app_id": audit.app_id,
                    "declared": audit.declared,
                    "present": audit.present,
                    "missing": list(audit.missing),
                    "damaged": list(audit.damaged),
                    "ok": audit.ok,
                }
                for audit in cache.audit(lua_dir)
            ],
        }

    def _on_scan_done(self, result: dict) -> None:
        self._scan_btn.setEnabled(True)
        self._records = result["rows"]

        self._stat_cards["total"].setText(str(result["total"]))
        self._stat_cards["valid"].setText(str(result["valid"]))
        self._stat_cards["invalid"].setText(str(result["invalid"]))
        self._stat_cards["orphan"].setText(str(result["orphan"]))
        self._stat_cards["size"].setText(_human_size(result["size"]))

        audits = result.get("audits", [])
        incomplete = [item for item in audits if not item["ok"]]
        self._stat_cards["incomplete"].setText(str(len(incomplete)))

        self._incomplete_table.setRowCount(len(incomplete))
        for row, item in enumerate(incomplete):
            self._incomplete_table.setItem(row, 0, QTableWidgetItem(item["app_id"]))
            self._incomplete_table.setItem(
                row, 1, QTableWidgetItem(f"{item['present']}/{item['declared']}")
            )
            self._incomplete_table.setItem(
                row, 2, QTableWidgetItem(", ".join(item["missing"]) or "—")
            )
            self._incomplete_table.setItem(
                row, 3, QTableWidgetItem(", ".join(item["damaged"]) or "—")
            )

        if incomplete:
            self._incomplete_label.setText(
                f"发现 {len(incomplete)} 个游戏的清单不全，点击上方「一键补全缺失清单」即可联网补齐。"
            )
            self._incomplete_label.setStyleSheet("color: #ff9800; font-size: 12px;")
        elif audits:
            self._incomplete_label.setText(f"{len(audits)} 个游戏的清单都已就绪。")
            self._incomplete_label.setStyleSheet("color: #52c41a; font-size: 12px;")
        else:
            self._incomplete_label.setText("没有发现声明了 setManifestid 的游戏。")
            self._incomplete_label.setStyleSheet("color: #888; font-size: 12px;")

        self._table.setRowCount(len(self._records))
        for row, item in enumerate(self._records):
            self._table.setItem(row, 0, QTableWidgetItem(item["depot"]))
            self._table.setItem(row, 1, QTableWidgetItem(item["gid"]))
            status = "有效" if item["valid"] else "损坏"
            if item["copies"] > 1:
                status += f"（{item['copies']} 份）"
            if item["orphan"]:
                status += " · 孤儿"
            self._table.setItem(row, 2, QTableWidgetItem(status))
            self._table.setItem(row, 3, QTableWidgetItem(_human_size(item["size"])))
            self._table.setItem(row, 4, QTableWidgetItem(item["dirs"]))

        self._status_label.setText(
            f"扫描完成：{result['total']} 个清单，Lua 中引用 {result['referenced']} 个。"
            + (f" 有 {result['invalid']} 个损坏，建议删除后重新补全。" if result["invalid"] else "")
            + (f" 有 {result['orphan']} 个孤儿清单可清理释放空间。" if result["orphan"] else "")
            + (f" {len(incomplete)} 个游戏清单不全。" if incomplete else "")
        )
        self.manifests_changed.emit()

    def _on_scan_error(self, message: str) -> None:
        self._scan_btn.setEnabled(True)
        self._status_label.setText(f"扫描失败：{message}")
        logger.error("清单扫描失败: %s", message)

    # ── 一键补全 ──────────────────────────────────────────
    def _on_complete_missing(self) -> None:
        if self._worker and self._worker.isRunning():
            InfoBar.warning("正在忙", "上一个操作还没结束", parent=self, position=InfoBarPosition.TOP, duration=2500)
            return

        steam_path = self._steam_path()
        lua_dir = self._lua_dir()
        if not steam_path or not lua_dir:
            InfoBar.warning("路径缺失", "请先配置 Steam 与 Lua 目录", parent=self, position=InfoBarPosition.TOP)
            return

        self._set_busy(True, "正在扫描缺失清单并联网补全，请稍候…")
        self._worker = AsyncWorker(self._complete_worker, steam_path, lua_dir)
        self._worker.finished_with_result.connect(self._on_complete_done)
        self._worker.finished_with_error.connect(self._on_complete_error)
        self._worker.start()

    @staticmethod
    def _complete_worker(steam_path: str, lua_dir: str) -> dict:
        """后台补全：从 Lua 目录收集 AppID，逐个走归档优先流水线补齐。"""
        from core.complete_manifest_service import ManifestCompletionService

        app_ids: list[str] = []
        seen: set[str] = set()
        if lua_dir and os.path.isdir(lua_dir):
            for name in sorted(os.listdir(lua_dir)):
                if not name.endswith(".lua"):
                    continue
                # 文件名即 AppID（<appid>.lua）；manifest.lua 等全局文件跳过
                stem = name[:-4]
                if not stem.isdigit() or stem in seen:
                    continue
                seen.add(stem)
                app_ids.append(stem)

        service = ManifestCompletionService(steam_path)
        try:
            report = service.complete_apps(app_ids)
        finally:
            service.close()
        return {"summary": report.summary(), "apps": len(app_ids)}

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

    # ── 清理 ──────────────────────────────────────────────
    def _on_delete_invalid(self) -> None:
        cache = self._cache()
        invalid = [record for record in cache.scan() if not record.valid]
        if not invalid:
            InfoBar.info("无需清理", "没有发现损坏的清单文件", parent=self, position=InfoBarPosition.TOP, duration=3000)
            return
        removed = sum(cache.delete(record.key) for record in invalid)
        summary = f"已删除 {removed} 个损坏的清单文件"
        self._status_label.setText(summary)
        InfoBar.success("清理完成", summary, parent=self, position=InfoBarPosition.TOP, duration=4000)
        self._on_scan()

    def _on_clean_orphans(self) -> None:
        lua_dir = self._lua_dir()
        if not lua_dir:
            InfoBar.warning("缺少 Lua 目录", "无法判断哪些清单是孤儿", parent=self, position=InfoBarPosition.TOP)
            return
        cache = self._cache()
        orphans = cache.orphaned(lua_dir)
        if not orphans:
            InfoBar.info("无需清理", "没有发现孤儿清单", parent=self, position=InfoBarPosition.TOP, duration=3000)
            return

        freed = 0
        removed = 0
        for record in orphans:
            for path in record.paths:
                try:
                    freed += os.path.getsize(path)
                except OSError:
                    pass
            removed += cache.delete(record.key)

        summary = f"已清理 {removed} 个孤儿清单，释放 {_human_size(freed)}"
        self._status_label.setText(summary)
        InfoBar.success("清理完成", summary, parent=self, position=InfoBarPosition.TOP, duration=5000)
        self._on_scan()

    # ── 辅助 ──────────────────────────────────────────────
    def _set_busy(self, busy: bool, message: str) -> None:
        for button in (
            self._scan_btn,
            self._complete_btn,
            self._import_btn,
            self._sync_btn,
            self._fix_btn,
            self._orphan_btn,
        ):
            button.setEnabled(not busy)
        self._status_label.setText(message)
