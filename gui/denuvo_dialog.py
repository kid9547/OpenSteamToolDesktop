"""DenuvoTicketDialog —— D 加密（Denuvo）票据授权对话框。

本对话框把 :mod:`core.ticket_service` 与 :mod:`core.credential_store` 的能力暴露给
用户，对应参考程序（SteamToolbox / 菜玩）中的"D 加密授权"功能，但完全不依赖任何
私有后端，也不含广告。

三条票据来源（界面上分别是三个入口）：

1. **拖入/选择票据文件** —— 支持上游 ``extract_tickets`` 输出的
   ``tickets.txt``、``appticket.bin``、``eticket.bin``，以及
   ``setAppTicket(...)`` 片段、JSON。
2. **本机提取** —— 在拥有该游戏的正版账号机器上直接导出票据
   （需要 Steam 已运行并登录）。
3. **手动粘贴 hex** —— 直接粘贴 AppTicket / ETicket 十六进制串。

导入后写入两处，缺一不可：

* 注册表凭据存储 ``HKCU\\Software\\Valve\\Steam\\Apps\\<AppID>``（``AppTicket`` /
  ``ETicket``，`REG_BINARY`）—— 这是 Steam 与 OpenSteamTool 实际读取的位置；
* 该游戏的 Lua 配置 ``setAppTicket`` / ``setETicket`` —— 重启 Steam 时由
  OpenSteamTool.dll 重新写入凭据存储，避免重装/换机后丢失。

.. warning::

    票据是 Valve 签名的真实凭据，**本地无法伪造**。有效期约 30 分钟 ~ 数小时，
    过期或更换硬件后会报 Denuvo 错误码 ``88500005``，需要重新导入。
"""
from __future__ import annotations

import os
import time

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core import credential_store, ticket_service
from core.credential_store import CredentialError, TicketBundle
from utils.async_worker import AsyncWorker
from utils.logger import setup_logger

logger = setup_logger(__name__)

_DROP_HINT = "把 tickets.txt / appticket.bin / eticket.bin / .lua / .json 拖到这里，或点击下方按钮选择文件"


class DenuvoTicketDialog(QDialog):
    """导入 / 提取 D 加密票据并写入凭据存储与 Lua。"""

    applied = pyqtSignal(str)  # 参数：app_id；通知父页面刷新

    def __init__(
        self,
        game_manager,
        app_id: str,
        game_name: str = "",
        parent=None,
        steam_bridge=None,
    ):
        super().__init__(parent)
        self._game_manager = game_manager
        self._app_id = str(app_id)
        self._bundle: TicketBundle | None = None
        self._worker: AsyncWorker | None = None
        self._steam_bridge = steam_bridge or self._resolve_bridge()

        title = f"D 加密授权 - {game_name} ({self._app_id})" if game_name else f"D 加密授权 - AppID {self._app_id}"
        self.setWindowTitle(title)
        self.setMinimumSize(720, 620)
        self.setModal(True)
        self.setAcceptDrops(True)

        self._init_ui()
        self._refresh_status()

    # ── UI ────────────────────────────────────────────────
    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)

        # 说明
        note = QLabel(ticket_service.extractor_ready_note(), self)
        note.setWordWrap(True)
        note.setStyleSheet("color: #d98a00; font-size: 12px;")
        layout.addWidget(note)

        # 当前状态
        self._status_label = QLabel("", self)
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("font-size: 12px;")
        layout.addWidget(self._status_label)

        tabs = QTabWidget(self)
        tabs.addTab(self._build_file_tab(), "① 文件导入")
        tabs.addTab(self._build_extract_tab(), "② 本机提取")
        tabs.addTab(self._build_paste_tab(), "③ 手动粘贴")
        layout.addWidget(tabs, 1)

        # 写入选项
        self._write_lua_check = QPushButton("同时写入游戏 Lua 配置（推荐）", self)
        self._write_lua_check.setCheckable(True)
        self._write_lua_check.setChecked(True)
        self._write_lua_check.setToolTip(
            "勾选后会把 setAppTicket/setETicket 写入该游戏的 Lua 文件，"
            "重启 Steam 后由 OpenSteamTool.dll 重新落盘到凭据存储。"
        )
        layout.addWidget(self._write_lua_check)

        self._write_reg_check = QPushButton("立即写入注册表凭据存储（推荐）", self)
        self._write_reg_check.setCheckable(True)
        self._write_reg_check.setChecked(True)
        self._write_reg_check.setToolTip(
            r"写入 HKCU\Software\Valve\Steam\Apps\<AppID> 的 AppTicket / ETicket (REG_BINARY)，"
            "这是 Steam 与 OpenSteamTool 实际读取的位置。"
        )
        layout.addWidget(self._write_reg_check)

        self._restart_check = QPushButton("写入后重启 Steam 使票据生效", self)
        self._restart_check.setCheckable(True)
        self._restart_check.setChecked(True)
        layout.addWidget(self._restart_check)

        # 日志
        self._log = QPlainTextEdit(self)
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(120)
        self._log.setPlaceholderText("操作日志")
        layout.addWidget(self._log)

        # 按钮
        btn_row = QHBoxLayout()
        self._apply_btn = QPushButton("写入票据", self)
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._on_apply)
        btn_row.addWidget(self._apply_btn)

        clear_btn = QPushButton("清除此游戏票据", self)
        clear_btn.clicked.connect(self._on_clear)
        btn_row.addWidget(clear_btn)

        btn_row.addStretch()

        close_btn = QPushButton("关闭", self)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _build_file_tab(self) -> QWidget:
        page = QWidget(self)
        box = QVBoxLayout(page)
        box.setContentsMargins(6, 12, 6, 6)

        self._drop_label = QLabel(_DROP_HINT, page)
        self._drop_label.setWordWrap(True)
        self._drop_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_label.setMinimumHeight(110)
        self._drop_label.setStyleSheet(
            "border: 2px dashed #777; border-radius: 8px; padding: 16px; color: #aaa;"
        )
        box.addWidget(self._drop_label)

        row = QHBoxLayout()
        pick_btn = QPushButton("选择票据文件…", page)
        pick_btn.clicked.connect(self._on_pick_files)
        row.addWidget(pick_btn)

        scan_btn = QPushButton("扫描提取工具输出目录", page)
        scan_btn.clicked.connect(self._on_scan_tool_dir)
        row.addWidget(scan_btn)
        row.addStretch()
        box.addLayout(row)

        hint = QLabel(
            "可一次选择多个文件，程序会自动合并（例如 tickets.txt + appticket.bin + eticket.bin）。",
            page,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 11px;")
        box.addWidget(hint)
        box.addStretch()
        return page

    def _build_extract_tab(self) -> QWidget:
        page = QWidget(self)
        box = QVBoxLayout(page)
        box.setContentsMargins(6, 12, 6, 6)

        info = QLabel(
            "在**拥有该游戏的正版账号**机器上使用。需要 Steam 已启动且已登录，"
            "并且该账号至少启动过一次目标游戏。\n"
            f"提取工具：{'已就绪' if ticket_service.extractor_available() else '缺失'}"
            f"（{ticket_service.python_bitness_note()} 运行环境）",
            page,
        )
        info.setWordWrap(True)
        box.addWidget(info)

        self._extract_btn = QPushButton("开始提取", page)
        self._extract_btn.setEnabled(ticket_service.extractor_available())
        self._extract_btn.clicked.connect(self._on_extract)
        box.addWidget(self._extract_btn)

        if ticket_service.extractor_available():
            path_label = QLabel(ticket_service.extractor_path(), page)
            path_label.setWordWrap(True)
            path_label.setStyleSheet("color: #888; font-size: 11px;")
            box.addWidget(path_label)
        else:
            warn = QLabel(
                "未找到 tools/extract_tickets/extract_tickets.exe。"
                "可运行 tools/build_extract_tickets.ps1 自行编译（需 MinGW-w64）。",
                page,
            )
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #e5534b; font-size: 11px;")
            box.addWidget(warn)

        box.addStretch()
        return page

    def _build_paste_tab(self) -> QWidget:
        page = QWidget(self)
        box = QVBoxLayout(page)
        box.setContentsMargins(6, 12, 6, 6)

        box.addWidget(QLabel("AppTicket（应用所有权票据，hex）", page))
        self._app_ticket_edit = QPlainTextEdit(page)
        self._app_ticket_edit.setPlaceholderText("14000000…（可带空格/换行）")
        self._app_ticket_edit.setMaximumHeight(120)
        box.addWidget(self._app_ticket_edit)

        box.addWidget(QLabel("ETicket（加密应用票据，hex）", page))
        self._eticket_edit = QPlainTextEdit(page)
        self._eticket_edit.setPlaceholderText("…")
        self._eticket_edit.setMaximumHeight(120)
        box.addWidget(self._eticket_edit)

        sid_row = QHBoxLayout()
        sid_row.addWidget(QLabel("SteamID（可选，留空则从 AppTicket 解析）", page))
        self._steam_id_edit = QLineEdit(page)
        self._steam_id_edit.setPlaceholderText("7656119…")
        sid_row.addWidget(self._steam_id_edit, 1)
        box.addLayout(sid_row)

        use_btn = QPushButton("使用粘贴的票据", page)
        use_btn.clicked.connect(self._on_use_pasted)
        box.addWidget(use_btn)
        box.addStretch()
        return page

    # ── 状态 ──────────────────────────────────────────────
    def _refresh_status(self) -> None:
        parts: list[str] = []

        lua = self._game_manager.ticket_status_from_lua(self._app_id)
        parts.append(
            "Lua 票据：" + ("AppTicket ✓ " if lua["has_app_ticket"] else "AppTicket ✗ ")
            + ("ETicket ✓" if lua["has_eticket"] else "ETicket ✗")
        )

        report = ticket_service.status(self._app_id)
        if report.get("available"):
            parts.append(
                "凭据存储："
                f"AppTicket {report['app_ticket_bytes']}B / "
                f"ETicket {report['eticket_bytes']}B"
                + (f"，SteamID {report['steam_id']}" if report.get("steam_id") else "")
                + ("（就绪）" if report.get("ready") else "（不完整）")
            )
        else:
            parts.append(f"凭据存储：不可用（{report.get('error')}）")

        if self._bundle:
            problems = self._bundle.validate()
            parts.append(
                "待写入："
                + ("校验通过" if not problems else "存在问题：" + "；".join(problems))
            )
        self._status_label.setText("\n".join(parts))

    def _append_log(self, text: str) -> None:
        self._log.appendPlainText(text)

    # ── 拖放 ──────────────────────────────────────────────
    def dragEnterEvent(self, event):  # noqa: N802 - Qt 接口
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802 - Qt 接口
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self._load_paths(paths)

    # ── 文件导入 ──────────────────────────────────────────
    def _on_pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择票据文件",
            ticket_service.default_import_dir(),
            "票据文件 (*.txt *.bin *.lua *.json *.dat *.key);;所有文件 (*.*)",
        )
        if paths:
            self._load_paths(paths)

    def _on_scan_tool_dir(self) -> None:
        import_dir = ticket_service.default_import_dir()
        found = ticket_service.find_stray_ticket_files(import_dir)
        if not found:
            self._append_log(f"{import_dir} 下没有找到票据文件")
            return
        self._append_log(f"在 {import_dir} 下找到 {len(found)} 个票据文件")
        self._load_paths(found)

    def _load_paths(self, paths: list[str]) -> None:
        try:
            bundle = ticket_service.import_paths(paths, app_id=self._app_id)
        except CredentialError as exc:
            self._append_log(f"导入失败：{exc}")
            return
        self._accept_bundle(bundle, source="文件导入")

    def _on_use_pasted(self) -> None:
        bundle = TicketBundle(
            app_id=self._app_id,
            app_ticket=self._app_ticket_edit.toPlainText().strip(),
            eticket=self._eticket_edit.toPlainText().strip(),
            steam_id=self._steam_id_edit.text().strip(),
            source="手动粘贴",
        )
        if not bundle.has_any:
            self._append_log("请至少粘贴 AppTicket 或 ETicket")
            return
        self._accept_bundle(bundle, source="手动粘贴")

    def _accept_bundle(self, bundle: TicketBundle, source: str) -> None:
        self._bundle = bundle
        problems = bundle.validate()
        detail = (
            f"[{source}] AppID={bundle.app_id} "
            f"AppTicket={len(bundle.app_ticket) // 2}B "
            f"ETicket={len(bundle.eticket) // 2}B "
            f"SteamID={bundle.derived_steam_id or '-'}"
        )
        self._append_log(detail)
        if problems:
            self._append_log("校验告警：" + "；".join(problems))
        self._apply_btn.setEnabled(True)
        self._refresh_status()

    # ── 本机提取 ──────────────────────────────────────────
    def _on_extract(self) -> None:
        if self._worker and self._worker.isRunning():
            self._append_log("提取正在进行中…")
            return
        self._extract_btn.setEnabled(False)
        self._append_log(f"开始提取 AppID {self._app_id}（需要 Steam 已登录且拥有该游戏）…")
        self._worker = AsyncWorker(ticket_service.extract_local, self._app_id)
        self._worker.finished_with_result.connect(self._on_extract_done)
        self._worker.finished_with_error.connect(self._on_extract_failed)
        self._worker.start()

    def _on_extract_done(self, bundle: TicketBundle) -> None:
        self._extract_btn.setEnabled(True)
        self._accept_bundle(bundle, source="本机提取")

    def _on_extract_failed(self, message: str) -> None:
        self._extract_btn.setEnabled(True)
        self._append_log(f"提取失败：{message}")

    # ── 写入 ──────────────────────────────────────────────
    def _on_apply(self) -> None:
        if not self._bundle:
            self._append_log("请先导入或提取票据")
            return
        bundle = self._bundle
        problems = bundle.validate()
        if problems:
            self._append_log("票据校验未通过，已中止：" + "；".join(problems))
            return

        wrote_something = False

        if self._write_reg_check.isChecked():
            try:
                written = credential_store.write_bundle(bundle)
                self._append_log(
                    "已写入凭据存储："
                    + "，".join(f"{k}={v}B" for k, v in written.items())
                )
                wrote_something = True
            except CredentialError as exc:
                self._append_log(f"写入凭据存储失败：{exc}")

        if self._write_lua_check.isChecked():
            try:
                path = self._game_manager.apply_ticket_bundle(self._app_id, bundle)
                self._append_log(f"已写入 Lua：{os.path.basename(path)}")
                wrote_something = True
            except (ValueError, OSError) as exc:
                self._append_log(f"写入 Lua 失败：{exc}")

        self._refresh_status()
        if wrote_something:
            self.applied.emit(self._app_id)

        if self._restart_check.isChecked() and wrote_something:
            self._restart_steam()

    def _restart_steam(self) -> None:
        """重启 Steam 使凭据生效。

        OpenSteamTool.dll 只在 Steam 启动时读取凭据存储，因此票据写入后必须
        重启客户端才会被用于 RequestEncryptedAppTicket 的响应篡改。
        """
        bridge = self._steam_bridge
        if bridge is None:
            self._append_log("无法自动重启 Steam：未获取到 Steam 桥接对象，请手动重启 Steam")
            return

        if bridge.is_steam_running():
            ok, message = bridge.kill_steam()
            self._append_log(f"结束 Steam：{message}")
            if not ok:
                return
            # 等待进程退出，避免新实例直接 attach 到旧进程
            for _ in range(20):
                if not bridge.is_steam_running():
                    break
                time.sleep(0.5)

        ok, message = bridge.start_steam()
        self._append_log(f"启动 Steam：{message}")
        if not ok:
            self._append_log("请手动启动 Steam 以应用票据")

    def _resolve_bridge(self):
        """尽力获取 SteamBridge：优先复用父页面/管理器上的实例。"""
        candidates = [getattr(self._game_manager, "steam_bridge", None)]
        parent = self.parent()
        while parent is not None and len(candidates) < 6:
            candidates.append(getattr(parent, "_steam_bridge", None))
            candidates.append(getattr(parent, "steam_bridge", None))
            parent = parent.parent() if hasattr(parent, "parent") else None
        for candidate in candidates:
            if candidate is not None and hasattr(candidate, "is_steam_running"):
                return candidate
        try:
            from core.steam_bridge import SteamBridge

            return SteamBridge()
        except Exception as exc:  # noqa: BLE001 - 桥接不可用不影响票据写入
            logger.warning("无法创建 SteamBridge，将不会自动重启 Steam: %s", exc)
            return None

    def _on_clear(self) -> None:
        removed = []
        try:
            removed = credential_store.delete(self._app_id)
        except CredentialError as exc:
            self._append_log(f"清除凭据存储失败：{exc}")
        self._append_log(
            "已从凭据存储删除：" + ("，".join(removed) if removed else "（无）")
        )
        # 同时移除 Lua 中的票据行
        try:
            path = self._game_manager.apply_ticket_bundle(
                self._app_id, TicketBundle(app_id=self._app_id)
            )
            self._append_log(f"已清理 Lua：{os.path.basename(path)}")
        except (ValueError, OSError) as exc:
            self._append_log(f"清理 Lua 跳过：{exc}")
        self._bundle = None
        self._apply_btn.setEnabled(False)
        self._refresh_status()
        self.applied.emit(self._app_id)
