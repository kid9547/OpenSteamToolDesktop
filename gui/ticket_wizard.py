"""D 加密「授权向导」——把用户已有的操作流程变成一键的一条链。

背景
----
经过逆向确认（见 ``DENUVO_REVERSE_ENGINEERING_REPORT.md``），D 加密（Denuvo）的授权
必须使用 Valve 私钥签名的真实票据，本地无法离线伪造。参考软件（SteamToolbox / 菜玩）
的"一键授权"之所以看起来神奇，是因为它们背后有一个**正版账号池服务器**在现签票据。

但有一条**完全不需要后端**的等价路径，而且正是绝大多数用户本来就在做的：

    登录「拥有该游戏」的账号  →  启动一次游戏  →  Steam 把所有权票据缓存到本地
                                                     ↓
                                    此时用本工具把票据提取出来复用

本模块把这个流程固化成向导，并在向导里直接展示每一步的实时状态（账号、
Steam 是否运行、Lua/注册表里票据是否已就绪），避免用户"不知道自己卡在哪一步"。

.. note::

    「本机提取」不需要任何后端、不需要联网、不产生任何费用，也不包含任何
    绕过算法。它只是把 Valve 已经签发给**你自己账号**的票据读出来。
"""
from __future__ import annotations

import os

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core import credential_store, ticket_service
from core.credential_store import CredentialError, TicketBundle
from utils.async_worker import AsyncWorker
from utils.logger import setup_logger

logger = setup_logger(__name__)


class TicketWizardDialog(QDialog):
    """D 加密授权向导：登录拥有游戏的账号 → 提取票据 → 落盘 → 生效。"""

    applied = pyqtSignal(str)  # app_id

    def __init__(self, game_manager, app_id: str, game_name: str = "", parent=None, steam_bridge=None):
        super().__init__(parent)
        self._game_manager = game_manager
        self._app_id = str(app_id)
        self._steam_bridge = steam_bridge
        self._worker: AsyncWorker | None = None
        self._bundle: TicketBundle | None = None

        name = game_name or self._app_id
        self.setWindowTitle(f"D 加密授权向导 - {name}")
        self.setMinimumSize(720, 560)
        self.setModal(True)

        self._init_ui()
        self._refresh()

        # 每 2 秒刷新一次状态，用户切账号/启动 Steam 后能看到变化
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(2000)

    # ── UI ────────────────────────────────────────────────
    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 14)
        layout.setSpacing(12)

        title = QLabel("为什么需要这一步？", self)
        title.setStyleSheet("font-size: 15px; font-weight: 600;")
        layout.addWidget(title)

        explain = QLabel(
            "D 加密（Denuvo）要求游戏拿到 Valve 签名的真实票据，本地无法伪造。\n"
            "参考软件之所以能「一键授权」，是因为它们背后有服务器用正版账号池现签票据。\n"
            "而你自己就有这个能力 —— 只要登录拥有该游戏的账号并启动一次游戏，"
            "Steam 就会把票据缓存到本机，下面的向导负责把它提取出来复用。",
            self,
        )
        explain.setWordWrap(True)
        explain.setStyleSheet("color: #aaa; font-size: 12px;")
        layout.addWidget(explain)

        # ── 步骤卡片 ──
        self._steps: list[tuple[QLabel, QLabel]] = []
        for index, caption in enumerate(
            (
                "① 登录「拥有这个游戏」的 Steam 账号",
                "② 启动一次该游戏（让 Steam 把所有权票据缓存到本地；D 加密游戏首次能进即可）",
                "③ 点击下面的「提取票据」",
                "④ 票据自动写入注册表凭据存储与该游戏 Lua",
                "⑤ 重启 Steam 后即可用当前账号进入游戏",
            ),
            start=1,
        ):
            row = QWidget(self)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(10)

            dot = QLabel("○", row)
            dot.setFixedWidth(18)
            dot.setStyleSheet("font-size: 13px;")

            text = QLabel(caption, row)
            text.setWordWrap(True)
            text.setStyleSheet("font-size: 12px;")

            row_layout.addWidget(dot)
            row_layout.addWidget(text, 1)
            layout.addWidget(row)
            self._steps.append((dot, text))

        # ── 实时状态 ──
        status_title = QLabel("当前状态", self)
        status_title.setStyleSheet("font-size: 13px; font-weight: 600; margin-top: 6px;")
        layout.addWidget(status_title)

        self._status = QLabel("", self)
        self._status.setWordWrap(True)
        self._status.setStyleSheet("font-size: 12px;")
        layout.addWidget(self._status)

        # ── 操作 ──
        row = QHBoxLayout()
        self._extract_btn = QPushButton("提取票据", self)
        self._extract_btn.clicked.connect(self._on_extract)
        row.addWidget(self._extract_btn)

        self._apply_btn = QPushButton("写入并生效", self)
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._on_apply)
        row.addWidget(self._apply_btn)

        refresh_btn = QPushButton("刷新状态", self)
        refresh_btn.clicked.connect(self._refresh)
        row.addWidget(refresh_btn)

        row.addStretch()

        detail_btn = QPushButton("高级：文件导入 / 手动粘贴", self)
        detail_btn.clicked.connect(self._open_advanced)
        row.addWidget(detail_btn)

        close_btn = QPushButton("关闭", self)
        close_btn.clicked.connect(self.accept)
        row.addWidget(close_btn)
        layout.addLayout(row)

        layout.addStretch()

    # ── 状态刷新 ──────────────────────────────────────────
    def _refresh(self) -> None:
        lines: list[str] = []
        done: list[bool] = [False] * len(self._steps)

        # 1. Steam 是否运行 / 登录账号
        running = False
        bridge = self._steam_bridge
        try:
            if bridge is not None:
                running = bool(bridge.is_steam_running())
            else:
                running = bool(
                    os.popen("tasklist /FI \"IMAGENAME eq steam.exe\" /NH").read().lower().find("steam.exe") >= 0
                )
        except Exception:  # noqa: BLE001 - 状态探测失败不应中断界面
            running = False

        try:
            account_id, universe = credential_store.active_user()
            steam_id = credential_store.active_steam_id()
        except CredentialError:
            account_id, universe, steam_id = 0, "", ""

        if running and account_id:
            done[0] = True
            lines.append(f"✅ Steam 正在运行，已登录账号 SteamID = {steam_id}（Universe={universe or '?'}）")
        elif running:
            lines.append("⚠️ Steam 正在运行，但还没有登录账号")
        else:
            lines.append("❌ Steam 未运行（第①步需要先启动并登录 Steam）")

        # 2. Lua / 注册表票据状态
        lua_status = self._game_manager.ticket_status_from_lua(self._app_id)
        if lua_status["has_app_ticket"]:
            done[3] = True

        report = ticket_service.status(self._app_id)
        if report.get("available") and report.get("app_ticket_bytes"):
            done[3] = True
            lines.append(
                f"✅ 凭据存储已有 AppTicket（{report['app_ticket_bytes']} 字节）"
                + (f"，ETicket {report['eticket_bytes']} 字节" if report.get("eticket_bytes") else "，ETicket 缺失")
            )
        else:
            lines.append("ℹ️ 凭据存储中还没有这个游戏的票据")

        lines.append(
            "📄 Lua 配置：" + ("已写入票据" if lua_status["has_app_ticket"] else "尚未写入票据")
        )

        if self._bundle is not None:
            done[2] = True
            problems = self._bundle.validate()
            lines.append(
                "🧾 已提取：" + ("校验通过" if not problems else "存在问题：" + "；".join(problems))
            )
            done[4] = not problems

        # 3. 提取工具
        if not ticket_service.extractor_available():
            lines.append("❌ 缺少提取工具 tools/extract_tickets/extract_tickets.exe")

        # 4. 本地票据库
        try:
            from core.ticket_vault import list_vault_app_ids

            vault_apps = list_vault_app_ids()
            lines.append(
                f"🗄️ 本地票据库：{len(vault_apps)} 份"
                + ("（含本游戏）" if self._app_id in vault_apps else "")
            )
        except Exception:  # noqa: BLE001 - 票据库不可用不影响主流程
            pass

        # 账号一致性提醒（否则 Denuvo 会报 012）
        if self._bundle is not None:
            verdict = credential_store.check_account_match(self._bundle)
            if verdict.startswith("mismatch:"):
                lines.append("⚠️ " + verdict[8:])

        self._status.setText("\n".join(lines))

        for index, (dot, text) in enumerate(self._steps):
            if done[index]:
                dot.setText("●")
                dot.setStyleSheet("font-size: 13px; color: #52c41a;")
                text.setStyleSheet("font-size: 12px; color: #52c41a;")
            else:
                dot.setText("○")
                dot.setStyleSheet("font-size: 13px; color: #888;")
                text.setStyleSheet("font-size: 12px;")

        self._extract_btn.setEnabled(
            ticket_service.extractor_available() and not (self._worker and self._worker.isRunning())
        )

    # ── 提取 ──────────────────────────────────────────────
    def _on_extract(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        self._extract_btn.setEnabled(False)
        self._status.setText("正在提取票据…（需要 Steam 已登录且该账号拥有此游戏）")

        self._worker = AsyncWorker(ticket_service.extract_local, self._app_id)
        self._worker.finished_with_result.connect(self._on_extract_done)
        self._worker.finished_with_error.connect(self._on_extract_failed)
        self._worker.start()

    def _on_extract_done(self, bundle: TicketBundle) -> None:
        self._bundle = bundle
        self._apply_btn.setEnabled(True)
        self._refresh()

    def _on_extract_failed(self, message: str) -> None:
        self._status.setText(f"❌ 提取失败：{message}")
        self._refresh()

    # ── 写入 ──────────────────────────────────────────────
    def _on_apply(self) -> None:
        if self._bundle is None:
            return
        problems = self._bundle.validate()
        if problems:
            self._status.setText("票据校验未通过：" + "；".join(problems))
            return

        messages: list[str] = []
        try:
            written = credential_store.write_bundle(self._bundle)
            messages.append("凭据存储：" + "，".join(f"{k}={v}B" for k, v in written.items()))
        except CredentialError as exc:
            messages.append(f"凭据存储写入失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - 注册表异常不应中断向导
            logger.exception("写入凭据存储时出现意外错误")
            messages.append(f"凭据存储写入异常：{exc}")

        # 存入本地票据库：供本机其它工具或你自己的其它机器按 mint 契约取用
        try:
            from core.ticket_vault import store_bundle

            store_bundle(self._bundle)
            messages.append("本地票据库：已存入")
        except Exception as exc:  # noqa: BLE001 - 票据库失败不影响主流程
            logger.warning("存入本地票据库失败: %s", exc)

        try:
            path = self._game_manager.apply_ticket_bundle(self._app_id, self._bundle)
            messages.append(f"Lua：{os.path.basename(path)}")
        except (ValueError, OSError) as exc:
            messages.append(f"Lua 写入失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("写入 Lua 时出现意外错误")
            messages.append(f"Lua 写入异常：{exc}")

        self._status.setText("已完成 —— " + "；".join(messages) + "\n请重启 Steam 后进入游戏。")
        self.applied.emit(self._app_id)

    def _open_advanced(self) -> None:
        from gui.denuvo_dialog import DenuvoTicketDialog

        dialog = DenuvoTicketDialog(
            self._game_manager,
            self._app_id,
            parent=self,
            steam_bridge=self._steam_bridge,
        )
        dialog.applied.connect(self.applied)
        dialog.exec()
        self._refresh()
