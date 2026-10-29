"""诊断中心页面：环境快照、日志检索、崩溃/转储扫描与诊断包导出。"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QPlainTextEdit,
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
    LineEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
)

from core.app_paths import app_data_dir
from core.diagnostics import (
    DiagnosticsSnapshot,
    collect_snapshot,
    export_diagnostics,
    filter_log_entries,
    log_sources,
    parse_log_files,
)
from utils.async_worker import AsyncWorker
from utils.logger import setup_logger
from utils.path_manager import PathManager

logger = setup_logger(__name__)

_LEVEL_OPTIONS = [
    ("全部级别", "DEBUG"),
    ("DEBUG", "DEBUG"),
    ("INFO", "INFO"),
    ("WARNING", "WARNING"),
    ("ERROR", "ERROR"),
]


def _human_size(size: int) -> str:
    value = float(max(size, 0))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _format_time(timestamp: float) -> str:
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return "-"


class DiagnosticsPage(ScrollArea):
    """诊断中心。"""

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self._bridge = bridge
        self._workers: list[AsyncWorker] = []
        self._snapshot: DiagnosticsSnapshot | None = None
        self._log_generation = 0
        self._loaded = False
        self._last_export = ""
        self.setObjectName("diagnosticsPage")
        self._init_ui()

    # ── UI ────────────────────────────────────────────────
    def _init_ui(self) -> None:
        container = QWidget()
        self.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(14)

        title = StrongBodyLabel("诊断中心", container)
        title.setStyleSheet("font-size: 24px;")
        layout.addWidget(title)

        subtitle = BodyLabel(
            "集中查看组件/签名/Lua 状态、按级别与关键词检索日志、扫描崩溃转储，"
            "并一键导出脱敏诊断包。导出内容不会包含账号密码或 API Key。",
            container,
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(subtitle)

        # 环境快照
        self._snapshot_card = CardWidget(container)
        snapshot_layout = QVBoxLayout(self._snapshot_card)
        snapshot_layout.setContentsMargins(18, 14, 18, 14)
        snapshot_layout.setSpacing(6)
        snapshot_header = QHBoxLayout()
        snapshot_title = StrongBodyLabel("环境快照", self._snapshot_card)
        snapshot_header.addWidget(snapshot_title)
        snapshot_header.addStretch()
        self._refresh_snapshot_btn = PushButton(
            FluentIcon.SYNC, "刷新快照", self._snapshot_card
        )
        self._refresh_snapshot_btn.clicked.connect(self._refresh_snapshot)
        snapshot_header.addWidget(self._refresh_snapshot_btn)
        snapshot_layout.addLayout(snapshot_header)
        self._snapshot_label = BodyLabel("尚未检测", self._snapshot_card)
        self._snapshot_label.setWordWrap(True)
        snapshot_layout.addWidget(self._snapshot_label)
        self._snapshot_detail = CaptionLabel("", self._snapshot_card)
        self._snapshot_detail.setWordWrap(True)
        self._snapshot_detail.setStyleSheet("color: #888;")
        snapshot_layout.addWidget(self._snapshot_detail)
        layout.addWidget(self._snapshot_card)

        # 崩溃 / 转储
        self._crash_card = CardWidget(container)
        crash_layout = QVBoxLayout(self._crash_card)
        crash_layout.setContentsMargins(18, 14, 18, 14)
        crash_layout.setSpacing(8)
        crash_header = QHBoxLayout()
        crash_title = StrongBodyLabel("崩溃与转储", self._crash_card)
        crash_header.addWidget(crash_title)
        crash_header.addStretch()
        self._open_logs_btn = PushButton(FluentIcon.FOLDER, "日志目录", self._crash_card)
        self._open_logs_btn.clicked.connect(
            lambda: self._open_path(str(PathManager.logs_dir()))
        )
        crash_header.addWidget(self._open_logs_btn)
        self._open_dumps_btn = PushButton(FluentIcon.FOLDER, "Steam 转储目录", self._crash_card)
        self._open_dumps_btn.clicked.connect(self._open_steam_dumps)
        crash_header.addWidget(self._open_dumps_btn)
        crash_layout.addLayout(crash_header)
        self._crash_summary = CaptionLabel("尚未扫描", self._crash_card)
        self._crash_summary.setWordWrap(True)
        self._crash_summary.setStyleSheet("color: #888;")
        crash_layout.addWidget(self._crash_summary)
        self._crash_table = QTableWidget(0, 4, self._crash_card)
        self._crash_table.setHorizontalHeaderLabels(["类型", "文件", "大小", "修改时间"])
        self._crash_table.verticalHeader().setVisible(False)
        self._crash_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._crash_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self._crash_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._crash_table.setMaximumHeight(150)
        crash_layout.addWidget(self._crash_table)
        layout.addWidget(self._crash_card)

        # 日志查看器
        self._log_card = CardWidget(container)
        log_layout = QVBoxLayout(self._log_card)
        log_layout.setContentsMargins(18, 14, 18, 14)
        log_layout.setSpacing(8)
        log_title = StrongBodyLabel("日志检索", self._log_card)
        log_layout.addWidget(log_title)

        filters = QHBoxLayout()
        filters.setSpacing(8)
        filters.addWidget(BodyLabel("日期/文件：", self._log_card))
        self._source_combo = ComboBox(self._log_card)
        self._source_combo.setMinimumWidth(220)
        self._source_combo.currentIndexChanged.connect(self._reload_logs)
        filters.addWidget(self._source_combo)
        filters.addWidget(BodyLabel("级别：", self._log_card))
        self._level_combo = ComboBox(self._log_card)
        for label, value in _LEVEL_OPTIONS:
            self._level_combo.addItem(label, userData=value)
        self._level_combo.setCurrentIndex(0)
        self._level_combo.currentIndexChanged.connect(self._reload_logs)
        filters.addWidget(self._level_combo)
        self._keyword_edit = LineEdit(self._log_card)
        self._keyword_edit.setPlaceholderText("关键词（模块名、错误信息…）")
        self._keyword_edit.setClearButtonEnabled(True)
        self._keyword_edit.returnPressed.connect(self._reload_logs)
        filters.addWidget(self._keyword_edit, 1)
        self._search_btn = PrimaryPushButton(FluentIcon.SEARCH, "查询", self._log_card)
        self._search_btn.clicked.connect(self._reload_logs)
        filters.addWidget(self._search_btn)
        self._reload_btn = PushButton(FluentIcon.SYNC, "刷新", self._log_card)
        self._reload_btn.clicked.connect(self._load_sources)
        filters.addWidget(self._reload_btn)
        log_layout.addLayout(filters)

        self._log_view = QPlainTextEdit(self._log_card)
        self._log_view.setReadOnly(True)
        self._log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._log_view.setMaximumBlockCount(30000)
        self._log_view.setMinimumHeight(320)
        font = QFont("Consolas", 9)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self._log_view.setFont(font)
        self._log_view.setStyleSheet(
            "QPlainTextEdit { background: rgba(0,0,0,0.12); border: 1px solid rgba(128,128,128,0.25); }"
        )
        log_layout.addWidget(self._log_view)
        self._log_status = CaptionLabel("尚未加载日志", self._log_card)
        self._log_status.setStyleSheet("color: #888;")
        log_layout.addWidget(self._log_status)
        layout.addWidget(self._log_card)

        # 导出
        export_row = QHBoxLayout()
        export_row.setSpacing(10)
        self._export_btn = PrimaryPushButton(
            FluentIcon.SAVE, "导出诊断包", container
        )
        self._export_btn.clicked.connect(self._on_export)
        export_row.addWidget(self._export_btn)
        self._open_export_btn = PushButton(
            FluentIcon.FOLDER, "打开诊断包目录", container
        )
        self._open_export_btn.clicked.connect(self._open_export_dir)
        export_row.addWidget(self._open_export_btn)
        self._open_data_btn = PushButton(FluentIcon.FOLDER, "应用数据目录", container)
        self._open_data_btn.clicked.connect(
            lambda: self._open_path(str(app_data_dir()))
        )
        export_row.addWidget(self._open_data_btn)
        export_row.addStretch()
        layout.addLayout(export_row)
        layout.addStretch(1)

    def showEvent(self, event):  # noqa: N802 - Qt 接口
        super().showEvent(event)
        if not self._loaded:
            self._loaded = True
            self._load_sources()
            self._refresh_snapshot()

    # ── 路径 ──────────────────────────────────────────────
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

    # ── 快照 ──────────────────────────────────────────────
    def _refresh_snapshot(self) -> None:
        steam_path = self._steam_path()
        source_dir = self._source_dir()
        self._snapshot_label.setText("正在收集环境信息…")
        worker = AsyncWorker(collect_snapshot, steam_path, source_dir)
        worker.finished_with_result.connect(self._on_snapshot_ready)
        worker.finished_with_error.connect(self._on_snapshot_error)
        self._register_worker(worker)
        worker.start()

    def _on_snapshot_ready(self, snapshot: DiagnosticsSnapshot) -> None:
        self._snapshot = snapshot
        self._snapshot_label.setText(
            f"{snapshot.component_summary or '组件状态未知'}    "
            f"签名缺失 {len(snapshot.missing_patterns)} 个    "
            f"Lua {snapshot.lua_total} 个（启用 {snapshot.lua_enabled}，冲突 {snapshot.lua_conflicts}）    "
            f"已安装 {snapshot.installed_apps} 个"
        )
        pids = ", ".join(str(pid) for pid in snapshot.steam_pids) or "未运行"
        self._snapshot_detail.setText(
            f"版本 {snapshot.app_version}    Steam：{snapshot.steam_path or '未检测到'}    "
            f"PID：{pids}    内核：{snapshot.kernel_version or '未知'}    "
            f"Denuvo：{snapshot.denuvo_mode or '未配置'}    生成：{snapshot.generated_at}"
        )
        self._fill_crash_table(snapshot.crash_artifacts)
        if snapshot.notes:
            self._log_status.setText("诊断备注：" + "；".join(snapshot.notes))

    def _on_snapshot_error(self, message: str) -> None:
        self._snapshot_label.setText("环境快照收集失败")
        self._snapshot_detail.setText(message)

    def _fill_crash_table(self, artifacts) -> None:
        self._crash_table.setRowCount(len(artifacts))
        for row, artifact in enumerate(artifacts):
            self._crash_table.setItem(row, 0, QTableWidgetItem(artifact.kind))
            name_item = QTableWidgetItem(artifact.name)
            name_item.setToolTip(artifact.path)
            self._crash_table.setItem(row, 1, name_item)
            self._crash_table.setItem(row, 2, QTableWidgetItem(_human_size(artifact.size)))
            self._crash_table.setItem(row, 3, QTableWidgetItem(_format_time(artifact.modified)))
        if artifacts:
            self._crash_summary.setText(
                f"找到 {len(artifacts)} 个崩溃日志/转储文件；诊断包默认包含最近 5 个 dmp。"
            )
        else:
            self._crash_summary.setText("未发现崩溃日志或转储文件。")

    # ── 日志 ──────────────────────────────────────────────
    def _load_sources(self) -> None:
        current = self._source_combo.currentText()
        sources = log_sources()
        self._source_combo.blockSignals(True)
        self._source_combo.clear()
        for source in sources:
            self._source_combo.addItem(source.label)
        if not sources:
            self._source_combo.addItem("没有可用日志")
        else:
            index = self._source_combo.findText(current)
            self._source_combo.setCurrentIndex(index if index >= 0 else 0)
        self._source_combo.blockSignals(False)
        self._reload_logs()

    def _reload_logs(self) -> None:
        if self._source_combo.count() == 0:
            return
        sources = log_sources()
        index = self._source_combo.currentIndex()
        if not sources or index < 0 or index >= len(sources):
            self._log_view.setPlainText("")
            self._log_status.setText("没有可读取的日志文件")
            return
        source = sources[index]
        level = self._level_combo.currentData() or "DEBUG"
        keyword = self._keyword_edit.text().strip()
        self._log_generation += 1
        generation = self._log_generation
        self._log_status.setText("正在读取日志…")
        worker = AsyncWorker(
            _load_log_entries,
            source.paths,
            level,
            keyword,
            generation,
        )
        worker.finished_with_result.connect(self._on_logs_ready)
        worker.finished_with_error.connect(self._on_logs_error)
        self._register_worker(worker)
        worker.start()

    def _on_logs_ready(self, payload) -> None:
        generation, entries, total = payload
        if generation != self._log_generation:
            return
        lines = []
        for entry in entries:
            timestamp = entry.timestamp or "-"
            lines.append(
                f"[{timestamp}] [{entry.level}] [{entry.logger}] {entry.message}"
            )
        self._log_view.setPlainText("\n".join(lines))
        self._log_view.verticalScrollBar().setValue(
            self._log_view.verticalScrollBar().maximum()
        )
        self._log_status.setText(
            f"显示 {len(entries)} / {total} 条日志"
            + ("（已截断到最近 20000 条）" if total > 20000 else "")
        )

    def _on_logs_error(self, message: str) -> None:
        self._log_status.setText(f"读取日志失败：{message}")

    # ── 导出 ──────────────────────────────────────────────
    def _on_export(self) -> None:
        default_name = (
            f"OpenSteamToolDesktop-diagnostics-"
            f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        )
        default_path = str(app_data_dir() / default_name)
        path, _ = QFileDialog.getSaveFileName(
            self, "导出诊断包", default_path, "Zip 文件 (*.zip)"
        )
        if not path:
            return
        self._export_btn.setEnabled(False)
        self._export_btn.setText("正在导出…")
        worker = AsyncWorker(
            export_diagnostics,
            path,
            steam_path=self._steam_path(),
            source_dir=self._source_dir(),
        )
        worker.finished_with_result.connect(self._on_export_done)
        worker.finished_with_error.connect(self._on_export_error)
        self._register_worker(worker)
        worker.start()

    def _on_export_done(self, result) -> None:
        self._export_btn.setEnabled(True)
        self._export_btn.setText("导出诊断包")
        ok, message = result
        if ok:
            self._last_export = message
            InfoBar.success(
                "诊断包已导出",
                message,
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )
        else:
            InfoBar.error(
                "导出失败",
                message,
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )

    def _on_export_error(self, message: str) -> None:
        self._export_btn.setEnabled(True)
        self._export_btn.setText("导出诊断包")
        InfoBar.error(
            "导出失败",
            message,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000,
        )

    def _open_export_dir(self) -> None:
        target = self._last_export or str(app_data_dir())
        self._open_path(str(Path(target).parent) if self._last_export else target)

    def _open_steam_dumps(self) -> None:
        steam_path = self._steam_path()
        if not steam_path:
            return
        dumps = Path(steam_path) / "dumps"
        self._open_path(str(dumps if dumps.is_dir() else Path(steam_path)))

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
        self.wait_for_workers()


def _load_log_entries(paths, level: str, keyword: str, generation: int):
    """后台线程：读取并过滤日志，返回 (generation, entries, total)。"""
    entries = parse_log_files(paths, max_entries=50_000)
    filtered = filter_log_entries(
        entries, min_level=level, keyword=keyword, max_entries=20_000
    )
    return generation, filtered, len(entries)
