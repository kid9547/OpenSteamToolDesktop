"""本地「现签」票据服务（可选，默认不启动）。

为什么需要它
------------
参考软件（SteamToolbox / 菜玩·紫电）的 D 加密「一键授权」依赖一个**云端账号池**：
客户端把 ``{app_id, nonce, existing_steam_id}`` POST 给服务器，服务器用一批拥有游戏的
正版账号向 Valve 现签票据再下发。这个后端是它们的运营资源，代码里没有、也无法移植。

但**你自己也能提供同样的接口**：本模块实现一个与上游契约完全一致的本地 HTTP 服务，
数据源是你在本机提取/导入过的票据。于是：

* 任何支持 ``seteticketurl()`` 的 DLL 分支（如 BetterSteamTools）都能直接指向它；
* 本项目的 :class:`core.ticket_service.TicketMintClient` 也能指向它；
* 你可以在**自己的多台机器**之间共享自己合法持有的票据，不依赖任何第三方。

接口契约（与上游 ``src/Utils/Tickets/EticketClient.cpp`` 逐字一致）::

    POST /
    Content-Type: application/json
    {"app_id":"<id>","nonce":"<hex>","existing_steam_id":"<decimal>"}   # 后两项可选

    200 → {"eticket":"<hex>","appticket":"<hex>","steam_id":"<decimal>"}
    409 → {"foreign_account":true}   已存在的票据属于本库之外的账号
    409 → {"error":"no owner"}       本库中没有该 appid 的票据

安全边界
--------
* 默认**只监听 127.0.0.1**，不对外网暴露。
* 需要显式启动（``start_server()`` 或界面开关），不开机自启。
* 存储是本地 JSON 文件，内容就是你已经合法持有的票据，不上传任何地方。

.. warning::

    本服务不"生成"任何票据。它只能返回**你自己已经提取/导入**过的票据，
    因此不构成任何绕过；把它暴露到公网等同于公开你自己的凭据，请勿这样做。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from core.credential_store import TicketBundle, normalize_hex
from utils.logger import setup_logger
from utils.path_manager import PathManager

logger = setup_logger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 47653  # 随机高位端口，避免与常见服务冲突


# ── 票据库 ────────────────────────────────────────────────────
@dataclass
class TicketVault:
    """本地票据库：``{app_id: {"appticket":..., "eticket":..., "steam_id":...}}``。"""

    path: Path = field(default_factory=lambda: PathManager.cache_dir() / "ticket_vault.json")
    _items: dict[str, dict[str, str]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._items = self._load()

    # -- 持久化 --
    def _load(self) -> dict[str, dict[str, str]]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("票据库读取失败 %s: %s", self.path, exc)
            return {}
        if not isinstance(raw, dict):
            return {}
        clean: dict[str, dict[str, str]] = {}
        for app_id, item in raw.items():
            if isinstance(item, dict):
                clean[str(app_id)] = {
                    "appticket": str(item.get("appticket") or ""),
                    "eticket": str(item.get("eticket") or ""),
                    "steam_id": str(item.get("steam_id") or ""),
                }
        return clean

    def save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self._items, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            logger.error("票据库写入失败 %s: %s", self.path, exc)

    # -- 读写 --
    def put(self, bundle: TicketBundle) -> None:
        """存入/合并一份票据（已有字段不覆盖为空）。空票据直接忽略。"""
        app_id = str(bundle.app_id)
        if not bundle.has_any:
            logger.debug("票据为空，未存入票据库 appid=%s", app_id)
            return
        entry = self._items.setdefault(app_id, {"appticket": "", "eticket": "", "steam_id": ""})
        if bundle.app_ticket:
            entry["appticket"] = normalize_hex(bundle.app_ticket)
        if bundle.eticket:
            entry["eticket"] = normalize_hex(bundle.eticket)
        steam_id = bundle.derived_steam_id
        if steam_id:
            entry["steam_id"] = steam_id
        self.save()
        logger.info("票据库已存入 appid=%s", app_id)

    def get(self, app_id: str) -> TicketBundle | None:
        entry = self._items.get(str(app_id))
        if not entry:
            return None
        bundle = TicketBundle(
            app_id=str(app_id),
            app_ticket=entry.get("appticket", ""),
            eticket=entry.get("eticket", ""),
            steam_id=entry.get("steam_id", ""),
            source="vault",
        )
        return bundle if bundle.has_any else None

    def app_ids(self) -> list[str]:
        return sorted(self._items)

    def __len__(self) -> int:
        return len(self._items)


# ── HTTP 服务 ─────────────────────────────────────────────────
class _MintHandler(BaseHTTPRequestHandler):
    """实现上游 mint 契约的最小 HTTP 处理器。"""

    vault: TicketVault  # 由服务器工厂注入
    server_version = "OpenSteamToolDesktop-TicketVault/1.0"

    # 默认实现会把每个请求打到 stderr，这里静音以免污染应用日志
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.debug("ticket-vault: " + fmt, *args)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            request = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid json"})
            return
        if not isinstance(request, dict):
            self._send_json(400, {"error": "invalid payload"})
            return

        app_id = str(request.get("app_id") or "").strip()
        if not app_id.isdigit():
            self._send_json(400, {"error": "app_id required"})
            return

        bundle = self.vault.get(app_id)
        if bundle is None:
            # 与上游一致：账号池里没有拥有该 appid 的账号
            self._send_json(409, {"error": "no owner"})
            return

        # 上游行为：注册表里已有的票据若属于本库之外/其它账号，拒绝现签，
        # 避免给 Denuvo 提供与当前登录账号不一致的票据（会报 012）。
        existing = str(request.get("existing_steam_id") or "").strip()
        vault_steam_id = bundle.derived_steam_id
        if existing and existing != "0" and vault_steam_id and existing != vault_steam_id:
            self._send_json(409, {"foreign_account": True})
            return

        self._send_json(
            200,
            {
                "appticket": bundle.app_ticket,
                "eticket": bundle.eticket,
                "steam_id": vault_steam_id,
            },
        )

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口
        """健康检查与票据清单（不含票据内容，避免误泄露）。"""
        if self.path.rstrip("/") in ("", "/health"):
            self._send_json(200, {"ok": True, "count": len(self.vault)})
            return
        if self.path.rstrip("/") == "/apps":
            self._send_json(200, {"apps": self.vault.app_ids()})
            return
        self._send_json(404, {"error": "not found"})


class LocalTicketServer:
    """本地票据服务（后台线程，可随时启停）。"""

    def __init__(
        self,
        vault: TicketVault | None = None,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ):
        self.vault = vault if vault is not None else TicketVault()
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def url(self) -> str:
        """供 ``seteticketurl()`` / ``TICKET_MINT_URL`` 使用的地址。"""
        return f"http://{self.host}:{self.port}/"

    def start(self, port: int | None = None) -> str:
        """启动服务，返回监听地址。已在运行时直接返回当前地址。"""
        if self.running:
            return self.url
        if port:
            self.port = port

        handler = type("_BoundMintHandler", (_MintHandler,), {"vault": self.vault})
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="ticket-vault", daemon=True
        )
        self._thread.start()
        logger.info("本地票据服务已启动: %s（%d 份票据）", self.url, len(self.vault))
        return self.url

    def stop(self) -> None:
        if not self._server:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        logger.info("本地票据服务已停止")


# ── 便捷接口 ──────────────────────────────────────────────────
def vault_path() -> str:
    return str(TicketVault().path)


def list_vault_app_ids() -> list[str]:
    return TicketVault().app_ids()


def store_bundle(bundle: TicketBundle) -> None:
    """把一份票据存入本地票据库（供其他机器/工具按契约取用）。"""
    TicketVault().put(bundle)
