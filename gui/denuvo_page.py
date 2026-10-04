"""DenuvoPage —— D 加密「提取」与「授权」两个独立页面。

背景
----
D 加密（Denuvo）游戏运行时需要 Valve 私钥签名的真实票据（AppTicket /
ETicket），本地无法伪造。参考软件（SteamToolbox / 菜玩）的"一键授权"
依赖其运营方的正版账号池服务器；本项目完全不需要后端，等价路径是：

    登录拥有该游戏的账号 → 启动一次游戏 → Steam 把票据缓存到本机
    → 用本工具提取出来 → 写入注册表凭据存储与 Lua → 重启 Steam

两个页面的分工（对应参考软件的独立 Tab）：

* :class:`DenuvoExtractPage`「D 加密提取」：在拥有游戏的机器上把票据
  提取出来，存入本地票据库（可复用/导出）。
* :class:`DenuvoAuthPage`「D 加密授权」：把已有票据（文件导入 / 手动粘贴 /
  本机提取 / 票据库取用）写入注册表凭据存储与游戏 Lua，并可选重启 Steam。

业务层复用 :mod:`core.ticket_service`、:mod:`core.credential_store` 与
:mod:`core.ticket_vault`，与游戏库卡片菜单里的对话框入口共享同一套逻辑。
"""
from __future__ import annotations

import os
import time

from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
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

from core import credential_store, ticket_service
from core.credential_store import CredentialError, TicketBundle
from utils.async_worker import AsyncWorker
from utils.logger import setup_logger

logger = setup_logger(__name__)


def _human_bytes(hex_len: int) -> str:
    return f"{hex_len // 2} 字节"


class _DenuvoPageBase(ScrollArea):
    """D 加密页面的公共骨架（AppID 输入 + 状态展示 + 日志）。"""

    applied = pyqtSignal(str)  # 票据变化后通知其它页面刷新

    def __init__(self, game_manager, bridge=None, parent=None):
        super().__init__(parent)
        self._game_manager = game_manager
        self._bridge = bridge
        self._worker: AsyncWorker | None = None
        self._bundle: TicketBundle | None = None

        self.setObjectName(self._object_name())
        self.setWidgetResizable(True)

        self._container = QWidget()
        self.setWidget(self._container)
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(24, 20, 24, 20)
        self._layout.setSpacing(12)

        self._build_ui()

    # ── 子类钩子 ──────────────────────────────────────────
    def _object_name(self) -> str:
        raise NotImplementedError

    def _build_ui(self) -> None:
        raise NotImplementedError

    # ── 公共构件 ──────────────────────────────────────────
    def _add_title(self, title: str, subtitle: str) -> None:
        label = StrongBodyLabel(title, self._container)
        label.setStyleSheet("font-size: 22px;")
        self._layout.addWidget(label)
        sub = BodyLabel(subtitle, self._container)
        sub.setWordWrap(True)
        sub.setStyleSheet("color: #888; font-size: 12px;")
        self._layout.addWidget(sub)

    def _build_appid_row(self, hint: str) -> QWidget:
        """AppID 输入行：输入框 + 下拉候选 + 提示文字"""
        card = CardWidget(self._container)
        row = QHBoxLayout(card)
        row.setContentsMargins(16, 12, 16, 12)
        row.setSpacing(10)

        row.addWidget(BodyLabel("AppID：", card))
        self._appid_edit = QLineEdit(card)
        self._appid_edit.setPlaceholderText("例如 1623730")
        self._appid_edit.setFixedWidth(160)
        self._appid_edit.textChanged.connect(self._on_app_id_changed)
        row.addWidget(self._appid_edit)

        known = []
        names: dict[str, str] = {}
        if self._game_manager:
            try:
                games = self._game_manager.get_games()
                known = sorted(
                    (str(g.app_id) for g in games if str(g.app_id).isdigit()),
                    key=lambda v: int(v),
                )
                names = {str(g.app_id): g.name for g in games}
            except Exception:  # noqa: BLE001 - 游戏库不可用不影响输入
                pass
        if known:
            self._known_combo = PushButton("从游戏库选择", card)
            from qfluentwidgets import RoundMenu, Action

            self._known_menu = RoundMenu(parent=self)
            for app_id in known[:40]:
                name = names.get(app_id, "")
                text = f"{app_id}  {name}" if name else app_id
                action = Action(text, triggered=lambda _=False, a=app_id: self.set_app_id(a))
                self._known_menu.addAction(action)
            self._known_combo.clicked.connect(
                lambda: self._known_menu.exec(self._known_combo.mapToGlobal(self._known_combo.rect().topLeft()))
            )
            row.addWidget(self._known_combo)

        row.addStretch()
        hint_label = BodyLabel(hint, card)
        hint_label.setWordWrap(True)
        hint_label.setStyleSheet("color: #888; font-size: 11px;")
        row.addWidget(hint_label, 1)

        self._layout.addWidget(card)
        return card

    def _build_status_card(self) -> None:
        card = CardWidget(self._container)
        box = QVBoxLayout(card)
        box.setContentsMargins(16, 12, 16, 12)
        title = StrongBodyLabel("当前状态", card)
        title.setStyleSheet("font-size: 13px;")
        box.addWidget(title)
        self._status_label = BodyLabel("", card)
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("font-size: 12px;")
        box.addWidget(self._status_label)
        self._layout.addWidget(card)

    def _build_log(self) -> None:
        self._log = QPlainTextEdit(self._container)
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(130)
        self._log.setPlaceholderText("操作日志")
        self._layout.addWidget(self._log)

    def _append_log(self, text: str) -> None:
        self._log.appendPlainText(text)

    # ── 公共逻辑 ──────────────────────────────────────────
    def set_app_id(self, app_id: str) -> None:
        self._appid_edit.setText(str(app_id))

    def _current_app_id(self) -> str:
        return self._appid_edit.text().strip()

    def _on_app_id_changed(self) -> None:
        self._bundle = None
        self._refresh()

    def _resolve_bridge(self):
        """优先复用 MainWindow 注入的 bridge，兜底自建。"""
        if self._bridge is not None:
            return self._bridge
        try:
            from core.steam_bridge import SteamBridge

            return SteamBridge()
        except Exception as exc:  # noqa: BLE001
            logger.warning("无法创建 SteamBridge: %s", exc)
            return None

    def _steam_runtime_line(self) -> tuple[str, bool, str]:
        """返回 (描述文本, Steam 是否运行, 当前 SteamID)"""
        running = False
        bridge = self._bridge
        try:
            if bridge is not None:
                running = bool(bridge.is_steam_running())
            else:
                running = "steam.exe" in os.popen('tasklist /FI "IMAGENAME eq steam.exe" /NH').read().lower()
        except Exception:  # noqa: BLE001
            running = False
        steam_id = ""
        try:
            steam_id = credential_store.active_steam_id()
        except CredentialError:
            pass
        except Exception:  # noqa: BLE001
            pass
        if running and steam_id:
            return f"✅ Steam 正在运行，已登录账号 SteamID = {steam_id}", running, steam_id
        if running:
            return "⚠️ Steam 正在运行，但还没有登录账号", running, ""
        return "❌ Steam 未运行（提取票据需要先启动并登录 Steam）", running, ""

    def _ticket_status_lines(self, app_id: str) -> list[str]:
        lines: list[str] = []
        try:
            lua = self._game_manager.ticket_status_from_lua(app_id)
            lines.append(
                "Lua 票据：" + ("AppTicket ✓ " if lua["has_app_ticket"] else "AppTicket ✗ ")
                + ("ETicket ✓" if lua["has_eticket"] else "ETicket ✗")
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            report = ticket_service.status(app_id)
            if report.get("available"):
                lines.append(
                    "凭据存储："
                    f"AppTicket {report['app_ticket_bytes']}B / ETicket {report['eticket_bytes']}B"
                    + (f"，SteamID {report['steam_id']}" if report.get("steam_id") else "")
                    + ("（就绪）" if report.get("ready") else "（不完整）")
                )
            else:
                lines.append("凭据存储：不可用（%s）" % report.get("error"))
        except Exception:  # noqa: BLE001
            pass
        try:
            from core.ticket_vault import list_vault_app_ids

            vault_apps = list_vault_app_ids()
            lines.append(
                f"本地票据库：{len(vault_apps)} 份"
                + ("（含本游戏）" if app_id and app_id in vault_apps else "")
            )
        except Exception:  # noqa: BLE001
            pass
        return lines

    def _refresh(self) -> None:
        """刷新状态区（子类可扩展）"""
        runtime, _running, _sid = self._steam_runtime_line()
        app_id = self._current_app_id()
        lines = [runtime]
        if app_id:
            lines.extend(self._ticket_status_lines(app_id))
        if not ticket_service.extractor_available():
            lines.append("❌ 缺少提取工具 tools/extract_tickets/extract_tickets.exe")
        if self._bundle is not None:
            problems = self._bundle.validate()
            lines.append(
                "🧾 待写入票据："
                + f"AppTicket {_human_bytes(len(self._bundle.app_ticket))}"
                + f"，ETicket {_human_bytes(len(self._bundle.eticket))}"
                + ("（校验通过）" if not problems else "（存在问题：" + "；".join(problems) + "）")
            )
        self._status_label.setText("\n".join(lines))

    def _is_busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def wait_for_workers(self) -> None:
        """MainWindow.shutdown 调用：等待后台线程结束，防止退出时
        QThread 在运行中被销毁触发 Qt 致命错误。"""
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)


class DenuvoExtractPage(_DenuvoPageBase):
    """「D 加密提取」页面：本机提取票据并存入本地票据库。"""

    def _object_name(self) -> str:
        return "denuvoExtractPage"

    def _build_ui(self) -> None:
        self._add_title(
            "D 加密提取",
            "在「拥有该游戏」的 Steam 账号机器上，把 Valve 已签发给你的票据提取出来。\n"
            "前置条件：Steam 已启动并登录，且该账号至少启动过一次目标游戏。提取不需要联网，也没有任何费用。",
        )

        self._build_appid_row("输入或从游戏库选择要提取票据的游戏 AppID")

        action_card = CardWidget(self._container)
        row = QHBoxLayout(action_card)
        row.setContentsMargins(16, 12, 16, 12)
        row.setSpacing(10)

        self._extract_btn = PrimaryPushButton(FluentIcon.DOWNLOAD, "提取票据", action_card)
        self._extract_btn.setEnabled(ticket_service.extractor_available())
        self._extract_btn.clicked.connect(self._on_extract)
        row.addWidget(self._extract_btn)

        self._save_btn = PushButton(FluentIcon.SAVE, "存入本地票据库", action_card)
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._on_save_to_vault)
        row.addWidget(self._save_btn)

        refresh_btn = PushButton(FluentIcon.SYNC, "刷新状态", action_card)
        refresh_btn.clicked.connect(self._refresh)
        row.addWidget(refresh_btn)

        row.addStretch()
        self._layout.addWidget(action_card)

        if not ticket_service.extractor_available():
            warn = BodyLabel(
                "未找到 tools/extract_tickets/extract_tickets.exe。"
                "可运行 tools/build_extract_tickets.ps1 自行编译（需 MinGW-w64）。",
                self._container,
            )
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #e5534b; font-size: 12px;")
            self._layout.addWidget(warn)

        # 已提取票据列表
        list_title = StrongBodyLabel("本地票据库", self._container)
        list_title.setStyleSheet("font-size: 14px; margin-top: 6px;")
        self._layout.addWidget(list_title)

        self._vault_label = BodyLabel("尚未加载", self._container)
        self._vault_label.setWordWrap(True)
        self._vault_label.setStyleSheet("color: #888; font-size: 12px;")
        self._layout.addWidget(self._vault_label)

        vault_btn_row = QHBoxLayout()
        reload_btn = PushButton(FluentIcon.SYNC, "刷新票据库", self._container)
        reload_btn.clicked.connect(self._refresh_vault)
        vault_btn_row.addWidget(reload_btn)
        vault_btn_row.addStretch()
        self._layout.addLayout(vault_btn_row)

        self._build_status_card()
        self._build_log()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.setInterval(4000)

    # ── 展示 ──────────────────────────────────────────────
    def showEvent(self, event):  # noqa: N802 - Qt 接口
        super().showEvent(event)
        self._refresh()
        self._refresh_vault()
        self._timer.start()

    def hideEvent(self, event):  # noqa: N802 - Qt 接口
        super().hideEvent(event)
        self._timer.stop()

    def _refresh_vault(self) -> None:
        try:
            from core.ticket_vault import list_vault_app_ids

            apps = list_vault_app_ids()
        except Exception as exc:  # noqa: BLE001
            self._vault_label.setText(f"票据库不可用：{exc}")
            return
        if not apps:
            self._vault_label.setText("票据库为空。提取成功后会自动保存在这里，可在「D 加密授权」页面取用。")
            return
        self._vault_label.setText("已保存 AppID：" + "、".join(sorted(apps, key=lambda v: int(v) if v.isdigit() else 0)))

    # ── 提取 ──────────────────────────────────────────────
    def _on_extract(self) -> None:
        app_id = self._current_app_id()
        if not app_id.isdigit():
            InfoBar.warning("请输入 AppID", "提取前请先输入有效的游戏 AppID", parent=self, position=InfoBarPosition.TOP)
            return
        if self._is_busy():
            self._append_log("提取正在进行中…")
            return
        if not ticket_service.extractor_available():
            self._append_log("缺少提取工具，无法提取")
            return

        self._extract_btn.setEnabled(False)
        self._append_log(f"开始提取 AppID {app_id}（需要 Steam 已登录且拥有该游戏）…")
        self._worker = AsyncWorker(ticket_service.extract_local, app_id)
        self._worker.finished_with_result.connect(self._on_extract_done)
        self._worker.finished_with_error.connect(self._on_extract_failed)
        self._worker.start()

    def _on_extract_done(self, bundle: TicketBundle) -> None:
        self._extract_btn.setEnabled(True)
        self._bundle = bundle
        self._append_log(
            f"[本机提取] AppID={bundle.app_id} "
            f"AppTicket={_human_bytes(len(bundle.app_ticket))} "
            f"ETicket={_human_bytes(len(bundle.eticket))} "
            f"SteamID={bundle.derived_steam_id or '-'}"
        )
        problems = bundle.validate()
        if problems:
            self._append_log("校验告警：" + "；".join(problems))
        else:
            self._append_log("提取成功。可直接「存入本地票据库」，或到「D 加密授权」页面写入。")
        self._save_btn.setEnabled(True)
        self._refresh()

    def _on_extract_failed(self, message: str) -> None:
        self._extract_btn.setEnabled(True)
        self._append_log(f"❌ 提取失败：{message}")
        self._refresh()

    def _on_save_to_vault(self) -> None:
        if self._bundle is None:
            return
        try:
            from core.ticket_vault import store_bundle

            store_bundle(self._bundle)
            self._append_log(f"已把 AppID {self._bundle.app_id} 的票据存入本地票据库")
            InfoBar.success("已保存", "票据已存入本地票据库", parent=self, position=InfoBarPosition.TOP, duration=4000)
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"存入票据库失败：{exc}")
        self._refresh_vault()


class DenuvoAuthPage(_DenuvoPageBase):
    """「D 加密授权」页面：把票据写入注册表凭据存储与游戏 Lua。"""

    def _object_name(self) -> str:
        return "denuvoAuthPage"

    def _build_ui(self) -> None:
        self._add_title(
            "D 加密授权",
            "把已有票据写入凭据存储（HKCU\\Software\\Valve\\Steam\\Apps\\<AppID>）与游戏 Lua，重启 Steam 后生效。\n"
            "票据来源：本机提取（见「D 加密提取」页）、票据文件导入、手动粘贴或本地票据库。",
        )

        self._build_appid_row("目标游戏 AppID（票据将写入该游戏）")

        # ── 来源区 ──
        source_card = CardWidget(self._container)
        source_box = QVBoxLayout(source_card)
        source_box.setContentsMargins(16, 12, 16, 12)
        source_box.setSpacing(8)

        source_title = StrongBodyLabel("票据来源", source_card)
        source_title.setStyleSheet("font-size: 13px;")
        source_box.addWidget(source_title)

        self._drop_label = BodyLabel(
            "把 tickets.txt / appticket.bin / eticket.bin / .lua / .json 拖到这里，或使用下方按钮",
            source_card,
        )
        self._drop_label.setWordWrap(True)
        self._drop_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_label.setMinimumHeight(64)
        self._drop_label.setStyleSheet(
            "border: 2px dashed #777; border-radius: 8px; padding: 10px; color: #aaa;"
        )
        source_box.addWidget(self._drop_label)

        btn_row = QHBoxLayout()
        pick_btn = PushButton(FluentIcon.FOLDER_ADD, "选择票据文件…", source_card)
        pick_btn.clicked.connect(self._on_pick_files)
        btn_row.addWidget(pick_btn)

        vault_btn = PushButton(FluentIcon.SYNC, "从本地票据库载入", source_card)
        vault_btn.clicked.connect(self._on_load_from_vault)
        btn_row.addWidget(vault_btn)

        extract_btn = PushButton(FluentIcon.DOWNLOAD, "本机提取", source_card)
        extract_btn.setEnabled(ticket_service.extractor_available())
        extract_btn.clicked.connect(self._on_extract_here)
        btn_row.addWidget(extract_btn)
        btn_row.addStretch()
        source_box.addLayout(btn_row)

        paste_row = QHBoxLayout()
        self._paste_edit = QLineEdit(source_card)
        self._paste_edit.setPlaceholderText("手动粘贴：AppTicket 与 ETicket hex（逗号/空格分隔，或分别粘贴到弹窗）")
        paste_row.addWidget(self._paste_edit, 1)
        paste_btn = PushButton("使用粘贴内容", source_card)
        paste_btn.clicked.connect(self._on_use_pasted)
        paste_row.addWidget(paste_btn)
        source_box.addLayout(paste_row)

        self._layout.addWidget(source_card)

        # ── 写入区 ──
        write_card = CardWidget(self._container)
        write_box = QVBoxLayout(write_card)
        write_box.setContentsMargins(16, 12, 16, 12)
        write_box.setSpacing(8)

        write_title = StrongBodyLabel("写入选项", write_card)
        write_title.setStyleSheet("font-size: 13px;")
        write_box.addWidget(write_title)

        self._write_reg_check = PushButton("写入注册表凭据存储（推荐）", write_card)
        self._write_reg_check.setCheckable(True)
        self._write_reg_check.setChecked(True)
        write_box.addWidget(self._write_reg_check)

        self._write_lua_check = PushButton("同时写入游戏 Lua 配置（推荐）", write_card)
        self._write_lua_check.setCheckable(True)
        self._write_lua_check.setChecked(True)
        write_box.addWidget(self._write_lua_check)

        self._restart_check = PushButton("写入后重启 Steam 使票据生效", write_card)
        self._restart_check.setCheckable(True)
        self._restart_check.setChecked(True)
        write_box.addWidget(self._restart_check)

        action_row = QHBoxLayout()
        self._apply_btn = PrimaryPushButton(FluentIcon.ACCEPT, "写入票据", write_card)
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._on_apply)
        action_row.addWidget(self._apply_btn)

        clear_btn = PushButton(FluentIcon.DELETE, "清除此游戏票据", write_card)
        clear_btn.clicked.connect(self._on_clear)
        action_row.addWidget(clear_btn)
        action_row.addStretch()
        write_box.addLayout(action_row)

        self._layout.addWidget(write_card)

        self._build_status_card()
        self._build_log()
        self.setAcceptDrops(True)

    # ── 拖放 ──────────────────────────────────────────────
    def dragEnterEvent(self, event):  # noqa: N802 - Qt 接口
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802 - Qt 接口
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self._load_paths(paths)

    def showEvent(self, event):  # noqa: N802 - Qt 接口
        super().showEvent(event)
        self._refresh()

    # ── 来源动作 ──────────────────────────────────────────
    def _on_pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择票据文件",
            ticket_service.default_import_dir(),
            "票据文件 (*.txt *.bin *.lua *.json *.dat *.key);;所有文件 (*.*)",
        )
        if paths:
            self._load_paths(paths)

    def _load_paths(self, paths: list[str]) -> None:
        app_id = self._current_app_id()
        try:
            bundle = ticket_service.import_paths(paths, app_id=app_id)
        except CredentialError as exc:
            self._append_log(f"导入失败：{exc}")
            return
        # 票据文件内带 AppID 时自动回填
        if not app_id and bundle.app_id:
            self.set_app_id(bundle.app_id)
            app_id = bundle.app_id
        self._accept_bundle(bundle, source="文件导入")

    def _on_load_from_vault(self) -> None:
        app_id = self._current_app_id()
        if not app_id:
            InfoBar.warning("请输入 AppID", "先在上方输入要授权的游戏 AppID", parent=self, position=InfoBarPosition.TOP)
            return
        try:
            from core.ticket_vault import TicketVault

            bundle = TicketVault().get(app_id)
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"读取票据库失败：{exc}")
            return
        if bundle is None:
            self._append_log(f"票据库中没有 AppID {app_id} 的票据，请先在「D 加密提取」页提取或导入文件")
            InfoBar.warning("没有票据", f"票据库中没有 AppID {app_id}", parent=self, position=InfoBarPosition.TOP)
            return
        self._accept_bundle(bundle, source="本地票据库")

    def _on_extract_here(self) -> None:
        app_id = self._current_app_id()
        if not app_id.isdigit():
            InfoBar.warning("请输入 AppID", "提取前请先输入有效的游戏 AppID", parent=self, position=InfoBarPosition.TOP)
            return
        if self._is_busy():
            self._append_log("提取正在进行中…")
            return
        self._append_log(f"开始提取 AppID {app_id}…")
        self._worker = AsyncWorker(ticket_service.extract_local, app_id)
        self._worker.finished_with_result.connect(self._on_extract_done)
        self._worker.finished_with_error.connect(self._on_extract_failed)
        self._worker.start()

    def _on_extract_done(self, bundle: TicketBundle) -> None:
        self._accept_bundle(bundle, source="本机提取")
        try:
            from core.ticket_vault import store_bundle

            store_bundle(bundle)
        except Exception:  # noqa: BLE001
            pass

    def _on_extract_failed(self, message: str) -> None:
        self._append_log(f"❌ 提取失败：{message}")

    def _on_use_pasted(self) -> None:
        raw = self._paste_edit.text().strip()
        # 支持 "appticket eticket"（空格/逗号分隔）或单段 hex
        parts = [p.strip() for p in raw.replace("\n", " ").replace(",", " ").split(" ") if p.strip()]
        if len(parts) >= 2:
            app_ticket, eticket = parts[0], parts[1]
        elif len(parts) == 1:
            app_ticket, eticket = parts[0], ""
        else:
            self._append_log("请先粘贴票据 hex")
            return
        bundle = TicketBundle(
            app_id=self._current_app_id(),
            app_ticket=app_ticket,
            eticket=eticket,
            source="手动粘贴",
        )
        if not bundle.has_any:
            self._append_log("粘贴内容无效")
            return
        self._accept_bundle(bundle, source="手动粘贴")

    def _accept_bundle(self, bundle: TicketBundle, source: str) -> None:
        self._bundle = bundle
        problems = bundle.validate()
        self._append_log(
            f"[{source}] AppID={bundle.app_id} "
            f"AppTicket={_human_bytes(len(bundle.app_ticket))} "
            f"ETicket={_human_bytes(len(bundle.eticket))} "
            f"SteamID={bundle.derived_steam_id or '-'}"
        )
        if problems:
            self._append_log("校验告警：" + "；".join(problems))
        else:
            self._append_log("票据就绪，点击「写入票据」生效。")
        self._apply_btn.setEnabled(True)
        self._refresh()

    # ── 写入 ──────────────────────────────────────────────
    def _on_apply(self) -> None:
        if not self._bundle:
            self._append_log("请先导入、粘贴或提取票据")
            return
        app_id = self._current_app_id() or self._bundle.app_id
        if not app_id:
            self._append_log("请先输入目标 AppID")
            return
        bundle = self._bundle
        problems = bundle.validate()
        if problems:
            self._append_log("票据校验未通过，已中止：" + "；".join(problems))
            return

        # 账号一致性提醒（否则 Denuvo 会报 012）
        verdict = credential_store.check_account_match(bundle)
        if verdict.startswith("mismatch:"):
            self._append_log("⚠️ " + verdict[8:])

        wrote_something = False
        if self._write_reg_check.isChecked():
            try:
                written = credential_store.write_bundle(bundle)
                self._append_log("已写入凭据存储：" + "，".join(f"{k}={v}B" for k, v in written.items()))
                wrote_something = True
            except CredentialError as exc:
                self._append_log(f"写入凭据存储失败：{exc}")

        if self._write_lua_check.isChecked():
            try:
                path = self._game_manager.apply_ticket_bundle(app_id, bundle)
                self._append_log(f"已写入 Lua：{os.path.basename(path)}")
                wrote_something = True
            except (ValueError, OSError) as exc:
                self._append_log(f"写入 Lua 失败：{exc}")

        try:
            from core.ticket_vault import store_bundle

            store_bundle(bundle)
        except Exception:  # noqa: BLE001
            pass

        self._refresh()
        if wrote_something:
            self.applied.emit(app_id)
            InfoBar.success("写入完成", "请重启 Steam 使票据生效", parent=self, position=InfoBarPosition.TOP, duration=5000)

        if self._restart_check.isChecked() and wrote_something:
            self._restart_steam()

    def _restart_steam(self) -> None:
        """重启 Steam 使凭据生效（OpenSteamTool.dll 只在启动时读取凭据）。"""
        bridge = self._resolve_bridge()
        if bridge is None:
            self._append_log("无法自动重启 Steam：未获取到 Steam 桥接对象，请手动重启")
            return
        if bridge.is_steam_running():
            ok, message = bridge.kill_steam()
            self._append_log(f"结束 Steam：{message}")
            if not ok:
                return
            for _ in range(20):
                if not bridge.is_steam_running():
                    break
                time.sleep(0.5)
        ok, message = bridge.start_steam()
        self._append_log(f"启动 Steam：{message}")
        if not ok:
            self._append_log("请手动启动 Steam 以应用票据")

    def _on_clear(self) -> None:
        app_id = self._current_app_id()
        if not app_id:
            return
        removed: list[str] = []
        try:
            removed = credential_store.delete(app_id)
        except CredentialError as exc:
            self._append_log(f"清除凭据存储失败：{exc}")
        self._append_log("已从凭据存储删除：" + ("，".join(removed) if removed else "（无）"))
        try:
            path = self._game_manager.apply_ticket_bundle(app_id, TicketBundle(app_id=app_id))
            self._append_log(f"已清理 Lua：{os.path.basename(path)}")
        except (ValueError, OSError) as exc:
            self._append_log(f"清理 Lua 跳过：{exc}")
        self._bundle = None
        self._apply_btn.setEnabled(False)
        self._refresh()
        self.applied.emit(app_id)
