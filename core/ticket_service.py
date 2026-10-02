"""D 加密（Denuvo）票据服务。

本模块负责"票据从哪来、怎么校验、落到哪里"，是 D 加密授权的业务层。它把
:mod:`core.credential_store`（注册表）与 :mod:`core.game_manager`（Lua 规则）
串成一条完整链路：

``提取/导入 → 校验 → 写注册表 → 写 Lua(setAppTicket/setETicket) → 重启 Steam 生效``

票据来源有三种，按优先级：

1. **本机提取**（:func:`extract_local`）：在拥有该游戏的正版账号机器上，通过
   ``extract_tickets`` 工具加载 ``steamclient64.dll``，调用
   ``ISteamAppTicket::GetAppOwnershipTicketData`` 与
   ``ISteamUser::RequestEncryptedAppTicket`` 导出。该工具源码来自上游
   ``tools/extract_tickets``，随本项目携带预编译版本。
2. **文件导入**（:func:`parse_ticket_text` / :func:`import_paths`）：兼容
   ``tickets.txt``（上游提取器输出）、``setAppTicket(...)`` Lua 片段、
   ``appticket.bin`` / ``eticket.bin`` 原始二进制、以及 JSON。
3. **兼容 mint 后端**（:class:`TicketMintClient`）：参考程序（如
   BetterSteamTools）的方案是一个 POST 接口，服务端用其账号池现签票据。
   本项目实现完全相同的客户端契约，但**默认关闭**，仅在用户自行配置
   ``ticket_mint_url`` 后启用。没有可用的自有后端时，这条路径不可用。

.. warning::

   ``AppTicket`` / ``ETicket`` 是 Valve 用私钥签名的真实凭据，**本地无法离线伪造**。
   上游 OpenSteamTool 只对 SteamStub 类保护使用 steamdrmp 的票据解析漏洞伪造；
   Denuvo 必须使用真实票据。票据具有时效性（约 30 分钟 ~ 数小时），过期或硬件
   变动后会报 Denuvo 错误码 ``88500005``，需要重新导入新票据。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from core import credential_store
from core.credential_store import (
    CredentialError,
    TicketBundle,
    bytes_to_hex,
    hex_to_bytes,
    normalize_hex,
)
from utils.logger import setup_logger

logger = setup_logger(__name__)


# ── 文本解析 ──────────────────────────────────────────────────
# tickets.txt 格式（上游 extract_tickets 输出）：
#   appid:1361510
#   appticket(184 bytes):14000000...
#   eticket(143 bytes):...
_TXT_APPID = re.compile(r"^\s*appid\s*[:=]\s*(\d+)\s*$", re.IGNORECASE | re.MULTILINE)
_TXT_TICKET = re.compile(
    r"^\s*(appticket|eticket|app_ticket|e_ticket)\s*(?:\(\s*\d+\s*bytes?\s*\))?\s*[:=]\s*([0-9a-fA-F\s:_-]+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# setAppTicket(123, "hex") / setETicket(123, "hex")
_LUA_TICKET = re.compile(
    r"set(app|e)ticket\s*\(\s*(\d+)\s*,\s*[\"']([0-9a-fA-F]+)[\"']\s*\)",
    re.IGNORECASE,
)
# seteticketurl("http://...")
_LUA_MINT_URL = re.compile(r"seteticketurl\s*\(\s*[\"']([^\"']+)[\"']\s*\)", re.IGNORECASE)


def parse_ticket_text(text: str, app_id: str = "", source: str = "") -> TicketBundle:
    """解析票据文本，自动识别 ``tickets.txt`` / Lua 片段 / JSON 三种格式。

    Args:
        text: 文件内容。
        app_id: 未在文本中声明 AppID 时的回退值。
        source: 来源标记（通常是文件路径），写入结果对象便于界面展示。

    Returns:
        解析出的 :class:`~core.credential_store.TicketBundle`。

    Raises:
        CredentialError: 文本中找不到任何可用票据。
    """
    bundle = TicketBundle(app_id=str(app_id or ""), source=source)

    # 1) JSON（例如 mint 后端响应或自建票据库）
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            bundle.app_id = str(
                payload.get("app_id") or payload.get("appid") or bundle.app_id or ""
            )
            for key in ("appticket", "app_ticket", "appTicket"):
                if payload.get(key):
                    bundle.app_ticket = str(payload[key])
                    break
            for key in ("eticket", "e_ticket", "eTicket", "encrypted_app_ticket"):
                if payload.get(key):
                    bundle.eticket = str(payload[key])
                    break
            for key in ("steam_id", "steamid", "steamId", "SteamID"):
                if payload.get(key):
                    bundle.steam_id = str(payload[key])
                    break

    # 2) tickets.txt 摘要
    if not bundle.has_any:
        app_match = _TXT_APPID.search(text)
        if app_match and not bundle.app_id:
            bundle.app_id = app_match.group(1)
        for name, hex_value in _TXT_TICKET.findall(text):
            key = name.lower().replace("_", "")
            cleaned = re.sub(r"[\s:_-]", "", hex_value)
            if not cleaned:
                continue
            if key == "appticket":
                bundle.app_ticket = cleaned
            else:
                bundle.eticket = cleaned

    # 3) Lua 片段（可能同时含多个 appid，取第一个或匹配 app_id 的那个）
    if not bundle.has_any:
        matches = _LUA_TICKET.findall(text)
        if matches:
            wanted = str(app_id or "")
            chosen = None
            if wanted:
                for kind, lua_app, hex_value in matches:
                    if lua_app == wanted:
                        chosen = (kind, lua_app, hex_value)
                        break
            if chosen is None:
                chosen = matches[0]
            kind, lua_app, hex_value = chosen
            bundle.app_id = wanted or lua_app
            if kind.lower().startswith("app"):
                bundle.app_ticket = hex_value
            else:
                bundle.eticket = hex_value
            # 同一次导入里若两类票据都在，一并取出
            for k, lua_app2, hex_value2 in matches:
                if lua_app2 != lua_app:
                    continue
                if k.lower().startswith("app") and not bundle.app_ticket:
                    bundle.app_ticket = hex_value2
                elif k.lower().startswith("e") and not bundle.eticket:
                    bundle.eticket = hex_value2

    if not bundle.has_any:
        raise CredentialError("未在内容中识别出 AppTicket/ETicket")
    if not bundle.app_id:
        raise CredentialError("内容中没有 AppID，请在界面上手动指定")
    return bundle


def parse_binary_file(path: str | Path, app_id: str, kind: str) -> TicketBundle:
    """把原始二进制票据文件（``appticket.bin`` / ``eticket.bin``）转成 bundle。"""
    data = Path(path).read_bytes()
    if not data:
        raise CredentialError(f"文件为空: {path}")
    bundle = TicketBundle(app_id=str(app_id), source=str(path))
    hex_value = bytes_to_hex(data)
    if kind == "appticket":
        bundle.app_ticket = hex_value
    else:
        bundle.eticket = hex_value
    logger.debug("已解析二进制票据 %s (%d bytes, %s)", path, len(data), kind)
    return bundle


def merge(bundles: list[TicketBundle]) -> TicketBundle:
    """把同一 AppID 的多个来源合并为一份完整 bundle。"""
    if not bundles:
        raise CredentialError("没有可合并的票据")
    app_ids = {b.app_id for b in bundles if b.app_id}
    if len(app_ids) > 1:
        raise CredentialError(f"存在多个 AppID，无法合并: {sorted(app_ids)}")
    merged = TicketBundle(app_id=next(iter(app_ids), ""))
    sources: list[str] = []
    for item in bundles:
        if item.app_ticket and not merged.app_ticket:
            merged.app_ticket = normalize_hex(item.app_ticket)
        if item.eticket and not merged.eticket:
            merged.eticket = normalize_hex(item.eticket)
        if item.steam_id and not merged.steam_id:
            merged.steam_id = item.steam_id
        if item.source:
            sources.append(item.source)
    merged.source = " + ".join(sources)
    return merged


# ── 文件导入 ──────────────────────────────────────────────────
SUPPORTED_SUFFIXES = {".txt", ".lua", ".json", ".bin", ".dat", ".key", ".ticket"}


def import_paths(paths: list[str | Path], app_id: str = "") -> TicketBundle:
    """从拖入的文件列表导入票据。

    支持一次拖入 ``tickets.txt`` + ``appticket.bin`` + ``eticket.bin``
    三个文件并自动合并。
    """
    parsed: list[TicketBundle] = []
    errors: list[str] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            errors.append(f"{path.name}: 不是文件")
            continue
        name = path.name.lower()
        try:
            if name.startswith("appticket") or name.startswith("app_ticket"):
                target = app_id or _app_id_from_sibling(path)
                if not target:
                    errors.append(f"{path.name}: 需要先指定 AppID")
                    continue
                parsed.append(parse_binary_file(path, target, "appticket"))
            elif name.startswith("eticket") or name.startswith("e_ticket"):
                target = app_id or _app_id_from_sibling(path)
                if not target:
                    errors.append(f"{path.name}: 需要先指定 AppID")
                    continue
                parsed.append(parse_binary_file(path, target, "eticket"))
            else:
                text = _read_text(path)
                if text is None:
                    errors.append(f"{path.name}: 无法按文本解析（未知编码）")
                    continue
                parsed.append(parse_ticket_text(text, app_id=app_id, source=str(path)))
        except CredentialError as exc:
            errors.append(f"{path.name}: {exc}")
        except OSError as exc:
            errors.append(f"{path.name}: 读取失败 {exc}")

    if not parsed:
        raise CredentialError("；".join(errors) or "没有可解析的票据文件")
    merged = merge(parsed)
    if errors:
        logger.warning("部分文件导入失败: %s", "；".join(errors))
    return merged


def _read_text(path: Path) -> str | None:
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "gbk"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
        except OSError:
            return None
    return None


def _app_id_from_sibling(path: Path) -> str:
    """从同级目录名推断 AppID（上游提取器会输出 ``<appid>/appticket.bin``）。"""
    parent = path.parent.name
    if parent.isdigit():
        return parent
    # 同级 tickets.txt 里也可能写了 appid
    sibling = path.parent / "tickets.txt"
    if sibling.is_file():
        text = _read_text(sibling)
        if text:
            match = _TXT_APPID.search(text)
            if match:
                return match.group(1)
    return ""


# ── 本机提取 ──────────────────────────────────────────────────
TOOL_DIR = Path(__file__).resolve().parent.parent / "tools" / "extract_tickets"
TOOL_EXE = TOOL_DIR / "extract_tickets.exe"


def extractor_available() -> bool:
    """本机提取工具是否可用。"""
    return TOOL_EXE.is_file()


def extractor_path() -> str:
    return str(TOOL_EXE)


def extract_local(app_id: str, timeout: float = 60.0) -> TicketBundle:
    """在拥有该游戏的正版账号机器上提取票据。

    调用 ``extract_tickets.exe <appid>``，它从注册表读取 Steam 路径、加载
    ``steamclient64.dll`` 并与已登录的 Steam 会话通信，随后在工具目录下写出
    ``<appid>/appticket.bin``、``<appid>/eticket.bin``、``<appid>/tickets.txt``。

    Args:
        app_id: 目标 AppID（必须为该账号已拥有的游戏）。
        timeout: 子进程超时时间（秒）。

    Returns:
        提取到的票据 bundle。

    Raises:
        CredentialError: 工具缺失、Steam 未运行/未登录、该账号不拥有该游戏等。
    """
    if not extractor_available():
        raise CredentialError(f"未找到提取工具: {TOOL_EXE}")
    app_id = str(app_id)
    if not app_id.isdigit():
        raise CredentialError(f"AppID 非法: {app_id!r}")

    logger.info("开始本机提取票据 appid=%s", app_id)
    try:
        proc = subprocess.run(
            [str(TOOL_EXE), app_id],
            cwd=str(TOOL_DIR),
            input=b"\r\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise CredentialError(f"提取超时（{timeout:.0f}s），请确认 Steam 已登录") from exc
    except OSError as exc:
        raise CredentialError(f"无法启动提取工具: {exc}") from exc

    output = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
    logger.debug("extract_tickets 输出:\n%s", output)

    out_dir = TOOL_DIR / app_id
    candidates: list[Path] = []
    if (out_dir / "tickets.txt").is_file():
        candidates.append(out_dir / "tickets.txt")
    if (out_dir / "appticket.bin").is_file():
        candidates.append(out_dir / "appticket.bin")
    if (out_dir / "eticket.bin").is_file():
        candidates.append(out_dir / "eticket.bin")

    if not candidates:
        hint = _diagnose_extract_failure(output)
        raise CredentialError(hint)
    return import_paths(candidates, app_id=app_id)


def _diagnose_extract_failure(output: str) -> str:
    """把提取工具的输出翻译成可读的失败原因。"""
    lowered = output.lower()
    if "createsteampipe failed" in lowered:
        return "Steam 未运行。请先启动并登录 Steam，再重试提取。"
    if "connecttoglobaluser failed" in lowered:
        return "Steam 没有已登录的用户，请登录后再试。"
    if "failed to load" in lowered:
        return "加载 steamclient64.dll 失败，请确认 Steam 安装完整。"
    if "failed to find steam install path" in lowered:
        return "未在注册表中找到 Steam 安装路径。"
    if "returned no ticket" in lowered:
        return "该账号不拥有此游戏，或票据尚未在本地缓存。请先用该账号启动一次游戏。"
    if "encrypted app ticket is empty" in lowered:
        return "该账号无法获取加密票据（游戏未拥有或未启动过）。"
    return "提取失败，未生成票据文件。请确认 Steam 已登录且该账号拥有此游戏。"


# ── 兼容 mint 后端（参考程序方案）────────────────────────────
@dataclass
class MintResult:
    bundle: TicketBundle
    status: int
    detail: str = ""


class TicketMintClient:
    """参考程序（BetterSteamTools）的在线现签票据客户端。

    上游接口契约（``src/Utils/Tickets/EticketClient.cpp``）::

        POST <url>
        Content-Type: application/json
        {"app_id":"<id>","nonce":"<hex>","existing_steam_id":"<decimal>"}   # 后两项可选

        200 -> {"eticket":"<hex>","appticket":"<hex>","steam_id":"<decimal>"}
        409 -> {"foreign_account":true} 或 "该 appid 无账号池归属"

    上游把后端地址做成编译期 ``OST_ETICKET_URL`` + 运行期 ``seteticketurl()``，默认空值
    即"完全关闭、绝不联网"。本项目沿用该设计：未配置 ``ticket_mint_url`` 时本类
    不会发起任何请求。
    """

    def __init__(self, base_url: str = "", timeout: float = 10.0):
        self.base_url = (base_url or "").strip()
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def mint(
        self,
        app_id: str,
        nonce_hex: str = "",
        existing_steam_id: str = "",
    ) -> MintResult:
        """向配置的后端请求现签票据。未配置地址时直接失败（不联网）。"""
        if not self.enabled:
            raise CredentialError("未配置票据服务地址（ticket_mint_url），在线现签不可用")

        payload: dict[str, str] = {"app_id": str(app_id)}
        if nonce_hex:
            payload["nonce"] = nonce_hex
        if existing_steam_id and str(existing_steam_id) != "0":
            payload["existing_steam_id"] = str(existing_steam_id)

        # 延迟导入，避免与 utils 层产生导入环
        import httpx  # noqa: PLC0415

        from config import SSL_VERIFY  # noqa: PLC0415
        from utils.http_client import get_system_proxy  # noqa: PLC0415

        try:
            response = httpx.post(
                self.base_url,
                json=payload,
                timeout=self.timeout,
                proxy=get_system_proxy(),
                verify=SSL_VERIFY,
            )
        except Exception as exc:  # noqa: BLE001 - 网络异常统一转业务异常
            raise CredentialError(f"票据服务请求失败: {exc}") from exc

        status = response.status_code
        body = response.text or ""

        if status == 409:
            if '"foreign_account":true' in body.replace(" ", ""):
                raise CredentialError("已存在的票据属于服务端账号池之外的账号，拒绝现签")
            raise CredentialError("服务端账号池中没有拥有该游戏的账号")
        if status != 200:
            raise CredentialError(f"票据服务返回 HTTP {status}")

        bundle = parse_ticket_text(body, app_id=str(app_id), source="mint")
        return MintResult(bundle=bundle, status=status, detail="")


def mint_supported_by_runtime() -> bool:
    """运行时（OpenSteamTool.dll）是否支持 ``seteticketurl`` 在线现签。

    只有 BetterSteamTools 分支带该函数；官方 OpenSteamTool 需要票据本身就已
    缓存在注册表中（由本项目的导入/提取流程写入）。
    """
    return False  # 官方 v1.4.8 无 seteticketurl，保留钩子便于将来切换分支 DLL


def lua_mint_url_line(url: str) -> str:
    """生成 ``seteticketurl("...")`` 行（供兼容分支 DLL 使用）。"""
    return f'seteticketurl("{url}")'


# ── 便捷流程 ──────────────────────────────────────────────────
def apply_to_registry(bundle: TicketBundle) -> dict[str, int]:
    """仅写入注册表凭据（Steam 与 OpenSteamTool 都会读取该位置）。"""
    return credential_store.write_bundle(bundle)


def status(app_id: str) -> dict[str, object]:
    """查询某 AppID 的票据状态，供界面展示。"""
    try:
        stored = credential_store.read(app_id)
    except CredentialError as exc:
        return {"app_id": str(app_id), "error": str(exc), "available": False}
    app_ticket = stored.app_ticket
    return {
        "app_id": str(app_id),
        "available": True,
        "app_ticket_bytes": len(app_ticket),
        "eticket_bytes": len(stored.eticket),
        "steam_id": stored.steam_id or (str(credential_store.steam_id_from_ticket(app_ticket)) if app_ticket else ""),
        "missing": list(stored.missing),
        "ready": bool(app_ticket and stored.eticket),
        "partial": bool(app_ticket) != bool(stored.eticket),
    }


def extractor_ready_note() -> str:
    """给界面用的说明文案，避免用户误以为可以"破解"。"""
    return (
        "D 加密（Denuvo）授权必须使用 Valve 签名的真实票据，本地无法伪造。\n"
        "票据来自拥有该游戏的正版账号，有效期约 30 分钟~数小时；"
        "过期或更换硬件后会报 Denuvo 错误码 88500005，需要重新导入票据。"
    )


def python_bitness_note() -> str:
    """返回运行环境位数说明（提取工具为 64 位）。"""
    return "64 位" if sys.maxsize > 2**32 else "32 位"


def find_stray_ticket_files(root: str | Path, limit: int = 200) -> list[str]:
    """在目录中查找可能的票据文件（用于界面提示可导入的文件）。"""
    root = Path(root)
    if not root.is_dir():
        return []
    found: list[str] = []
    for path in root.rglob("*"):
        if len(found) >= limit:
            break
        if not path.is_file():
            continue
        name = path.name.lower()
        if name in {"tickets.txt", "appticket.bin", "eticket.bin"} or (
            path.suffix.lower() in SUPPORTED_SUFFIXES and "ticket" in name
        ):
            found.append(str(path))
    return found


def default_import_dir() -> str:
    """默认扫描目录：工具的 AppID 输出目录。"""
    return str(TOOL_DIR)
