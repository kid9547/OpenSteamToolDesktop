"""Steam 平台凭据存储（Windows）读写模块。

本模块对应 OpenSteamTool 的 ``SteamCredentialStore`` 后端实现，是 D 加密（Denuvo）
游戏授权链路中"票据落地"的唯一位置。参考上游源码：

* ``src/OSTPlatform/Windows/SteamCredentialStore.cpp``
* ``src/Utils/Tickets/AppTicket.cpp``

存储位置（Windows，与 Steam 自身以及 OpenSteamTool 完全一致）::

    HKEY_CURRENT_USER\\Software\\Valve\\Steam\\Apps\\<AppID>
        AppTicket  REG_BINARY   应用所有权票据（AppOwnershipTicket）
        ETicket    REG_BINARY   加密应用票据（EncryptedAppTicket）
        SteamID    REG_SZ       十进制 SteamID（用于成就/统计与身份欺骗）

对应 Lua 指令（由 OpenSteamTool.dll 解析并写入上面的键）::

    setAppTicket(<appid>, "<hex>")
    setETicket(<appid>, "<hex>")

注意：``AppTicket`` / ``ETicket`` 必须由 Valve 私钥签名的**真实票据**，本地无法伪造。
所有权票据只对"拥有该游戏的正版账号"有效。
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

from utils.logger import setup_logger

logger = setup_logger(__name__)

# ── 注册表常量 ────────────────────────────────────────────────
STEAM_APPS_KEY: str = r"Software\Valve\Steam\Apps"
STEAM_KEY: str = r"Software\Valve\Steam"
VALUE_APP_TICKET: str = "AppTicket"
VALUE_ETICKET: str = "ETicket"
VALUE_STEAM_ID: str = "SteamID"

# SteamID64 十进制范围（个人账号类型 1，Universe 1/2/3）
_STEAMID_MIN = 76561197960265728
_STEAMID_MAX = 76561209999999999
# SteamID64 → 32 位账号 ID 的固定偏移
_STEAMID64_BASE = 76561197960265728

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


class CredentialError(RuntimeError):
    """凭据读写失败。"""


def is_supported() -> bool:
    """当前平台是否支持凭据存储（仅 Windows）。"""
    return sys.platform == "win32"


def _winreg():
    """延迟导入 winreg，保证非 Windows 平台上模块可被导入。"""
    if not is_supported():
        raise CredentialError("凭据存储仅在 Windows 上可用")
    import winreg  # noqa: PLC0415  (平台限定导入)

    return winreg


def _view_flags() -> int:
    """32 位进程在 64 位系统上需要显式回退到 32 位注册表视图。

    Steam 客户端本身是 32 位进程，其凭据写在 WOW6432Node 视图里；
    64 位进程访问 ``HKCU\\Software`` 会被重定向到同一位置，但显式声明更安全。
    """
    winreg = _winreg()
    if sys.maxsize <= 2**32:  # 32 位 Python
        return getattr(winreg, "KEY_WOW64_32KEY", 0)
    return 0


def hex_to_bytes(value: str) -> bytes:
    """把十六进制字符串转成字节串（忽略空格/换行/冒号/短横线分隔符）。"""
    cleaned = re.sub(r"[\s:_-]", "", value or "")
    if not cleaned:
        raise CredentialError("票据内容为空")
    if len(cleaned) % 2 != 0:
        raise CredentialError("十六进制长度必须为偶数")
    if not _HEX_RE.match(cleaned):
        raise CredentialError("票据包含非十六进制字符")
    return bytes.fromhex(cleaned)


def bytes_to_hex(data: bytes, upper: bool = False) -> str:
    """字节串转十六进制字符串。"""
    text = data.hex()
    return text.upper() if upper else text


def normalize_hex(value: str) -> str:
    """规范化十六进制字符串（去分隔符、统一小写）。"""
    return bytes_to_hex(hex_to_bytes(value))


def steam_id_from_ticket(ticket: bytes) -> int:
    """从 AppOwnershipTicket 字节串中解析 SteamID。

    票据布局（上游 ``AppTicket.cpp`` 注释）::

        [uint32 Size][uint32 Version][uint64 SteamID][...][Signature]

    票据长度不足 16 字节时返回 0。
    """
    if len(ticket) < 16:
        return 0
    return int.from_bytes(ticket[8:16], "little", signed=False)


def account_id_to_steam_id64(account_id: int) -> int:
    """把 32 位账号 ID 还原为 SteamID64（``76561197960265728 + account_id``）。"""
    if account_id <= 0:
        return 0
    return _STEAMID64_BASE + int(account_id)


def normalize_steam_id(raw: object) -> str:
    """把注册表里读到的 SteamID 归一化为十进制 SteamID64 字符串。

    Steam 客户端自身把该值写成 ``REG_DWORD`` 的 **32 位账号 ID**
    （例如 Hogwarts Legacy 键里是 ``1402342805``），而 OpenSteamTool 写入的是
    ``REG_SZ`` 的完整 SteamID64。两种形式都要能正确识别，判断依据是数值量级：
    小于 SteamID64 基准值的按账号 ID 处理。
    """
    if raw is None:
        return ""
    if isinstance(raw, int):
        return str(account_id_to_steam_id64(raw))
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-16-le", "ignore")
        except Exception:  # noqa: BLE001 - 解码失败按空处理
            return ""
    text = str(raw).strip("\x00").strip()
    if not text:
        return ""
    if not text.isdigit():
        logger.debug("SteamID 非数字，忽略: %r", text)
        return ""
    value = int(text)
    if value < _STEAMID64_BASE:
        return str(account_id_to_steam_id64(value))
    return str(value)


# ── 数据模型 ──────────────────────────────────────────────────
@dataclass
class TicketBundle:
    """一个 AppID 的整套 D 加密票据。"""

    app_id: str
    app_ticket: str = ""  # AppOwnershipTicket，hex
    eticket: str = ""  # EncryptedAppTicket，hex
    steam_id: str = ""  # 十进制 SteamID
    source: str = ""  # 票据来源（文件路径 / 提取工具 / 云端）

    @property
    def has_any(self) -> bool:
        return bool(self.app_ticket or self.eticket)

    @property
    def has_both(self) -> bool:
        return bool(self.app_ticket and self.eticket)

    @property
    def derived_steam_id(self) -> str:
        """优先使用显式 SteamID，否则从 AppTicket 解析。"""
        if self.steam_id:
            return self.steam_id
        if self.app_ticket:
            parsed = steam_id_from_ticket(hex_to_bytes(self.app_ticket))
            return str(parsed) if parsed else ""
        return ""

    def validate(self) -> list[str]:
        """返回问题列表；空列表表示结构可用。"""
        problems: list[str] = []
        if not str(self.app_id).isdigit():
            problems.append(f"AppID 非法: {self.app_id!r}")
        for label, value in (("AppTicket", self.app_ticket), ("ETicket", self.eticket)):
            if not value:
                continue
            try:
                hex_to_bytes(value)
            except CredentialError as exc:
                problems.append(f"{label} 无效: {exc}")
        sid = self.derived_steam_id
        if sid:
            if not sid.isdigit():
                problems.append(f"SteamID 非数字: {sid!r}")
            elif not (_STEAMID_MIN <= int(sid) <= _STEAMID_MAX):
                problems.append(f"SteamID 不在个人账号范围: {sid}")
        return problems

    def lua_lines(self) -> list[str]:
        """生成写入 Lua 的 ``setAppTicket`` / ``setETicket`` 指令。

        OpenSteamTool 要求该 appid 必须已在本 Lua 中 ``addappid``，否则票据会被忽略，
        因此调用方需保证先写入 ``addappid``。
        """
        lines: list[str] = []
        if self.app_ticket:
            lines.append(f'setAppTicket({self.app_id}, "{normalize_hex(self.app_ticket)}")')
        if self.eticket:
            lines.append(f'setETicket({self.app_id}, "{normalize_hex(self.eticket)}")')
        return lines


@dataclass
class StoredCredentials:
    """从注册表读出的凭据。"""

    app_id: str
    app_ticket: bytes = b""
    eticket: bytes = b""
    steam_id: str = ""
    missing: list[str] = field(default_factory=list)

    @property
    def present(self) -> bool:
        return bool(self.app_ticket or self.eticket)


# ── 读写实现 ──────────────────────────────────────────────────
def read(app_id: str | int) -> StoredCredentials:
    """读取指定 AppID 的凭据；不存在的值记录在 ``missing`` 中。"""
    winreg = _winreg()
    app_id = str(app_id)
    result = StoredCredentials(app_id=app_id)
    path = f"{STEAM_APPS_KEY}\\{app_id}"
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ | _view_flags()
        )
    except FileNotFoundError:
        result.missing = [VALUE_APP_TICKET, VALUE_ETICKET, VALUE_STEAM_ID]
        return result
    except OSError as exc:  # pragma: no cover - 权限等异常
        raise CredentialError(f"打开注册表键失败 {path}: {exc}") from exc

    with key:
        for name, attr in (
            (VALUE_APP_TICKET, "app_ticket"),
            (VALUE_ETICKET, "eticket"),
        ):
            try:
                value, _kind = winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                result.missing.append(name)
                continue
            except OSError as exc:  # pragma: no cover
                raise CredentialError(f"读取 {name} 失败: {exc}") from exc
            setattr(result, attr, bytes(value) if value else b"")

        try:
            value, _kind = winreg.QueryValueEx(key, VALUE_STEAM_ID)
        except FileNotFoundError:
            result.missing.append(VALUE_STEAM_ID)
        except OSError as exc:  # pragma: no cover
            raise CredentialError(f"读取 {VALUE_STEAM_ID} 失败: {exc}") from exc
        else:
            # Steam 自身写 DWORD 账号 ID，OpenSteamTool 写 SteamID64 字符串
            result.steam_id = normalize_steam_id(value)

    logger.debug(
        "读取凭据 appid=%s appticket=%d bytes eticket=%d bytes steamid=%s missing=%s",
        app_id,
        len(result.app_ticket),
        len(result.eticket),
        result.steam_id or "-",
        ",".join(result.missing) or "-",
    )
    return result


def _write_value(app_id: str, name: str, reg_type: int, data: bytes) -> None:
    winreg = _winreg()
    path = f"{STEAM_APPS_KEY}\\{app_id}"
    try:
        # 注意签名：CreateKeyEx(key, sub_key, reserved=0, access=KEY_WRITE,
        #                      class_name="", options=0, sam=0)
        # REG_OPTION_NON_VOLATILE 即 0，因此只传 access。
        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            path,
            0,
            winreg.KEY_SET_VALUE | _view_flags(),
        )
    except OSError as exc:
        raise CredentialError(f"创建注册表键失败 {path}: {exc}") from exc
    with key:
        try:
            winreg.SetValueEx(key, name, 0, reg_type, data)
        except OSError as exc:
            raise CredentialError(f"写入 {name} 失败: {exc}") from exc


def write_app_ticket(app_id: str | int, value: str | bytes) -> int:
    """写入 AppTicket（REG_BINARY），返回字节数。"""
    data = hex_to_bytes(value) if isinstance(value, str) else bytes(value)
    if not data:
        raise CredentialError("AppTicket 为空，拒绝写入")
    _write_value(str(app_id), VALUE_APP_TICKET, _winreg().REG_BINARY, data)
    logger.info("已写入 AppTicket appid=%s bytes=%d", app_id, len(data))
    return len(data)


def write_eticket(app_id: str | int, value: str | bytes) -> int:
    """写入 ETicket（REG_BINARY），返回字节数。"""
    data = hex_to_bytes(value) if isinstance(value, str) else bytes(value)
    if not data:
        raise CredentialError("ETicket 为空，拒绝写入")
    _write_value(str(app_id), VALUE_ETICKET, _winreg().REG_BINARY, data)
    logger.info("已写入 ETicket appid=%s bytes=%d", app_id, len(data))
    return len(data)


def write_steam_id(app_id: str | int, steam_id: str | int) -> None:
    """写入 SteamID（REG_SZ，十进制字符串）。

    注意：``winreg.SetValueEx`` 对 ``REG_SZ`` 需要传入 ``str``（编码由 Windows
    处理）；传 UTF-16LE 字节会抛 ``ValueError``。REG_BINARY 才传 ``bytes``。
    """
    text = str(steam_id).strip()
    if not text.isdigit():
        raise CredentialError(f"SteamID 必须为十进制数字: {steam_id!r}")
    _write_value(str(app_id), VALUE_STEAM_ID, _winreg().REG_SZ, text)
    logger.info("已写入 SteamID appid=%s steamid=%s", app_id, text)


def write_bundle(bundle: TicketBundle, include_steam_id: bool = True) -> dict[str, int]:
    """整套写入：AppTicket + ETicket(+ SteamID)。返回各值的字节数。"""
    problems = bundle.validate()
    if problems:
        raise CredentialError("；".join(problems))
    written: dict[str, int] = {}
    if bundle.app_ticket:
        written[VALUE_APP_TICKET] = write_app_ticket(bundle.app_id, bundle.app_ticket)
    if bundle.eticket:
        written[VALUE_ETICKET] = write_eticket(bundle.app_id, bundle.eticket)
    if include_steam_id:
        sid = bundle.derived_steam_id
        if sid:
            write_steam_id(bundle.app_id, sid)
            written[VALUE_STEAM_ID] = len(sid)
    return written


def delete(app_id: str | int, names: tuple[str, ...] = (VALUE_APP_TICKET, VALUE_ETICKET, VALUE_STEAM_ID)) -> list[str]:
    """删除指定值，返回实际删除的值名列表（用于"清除票据"）。"""
    winreg = _winreg()
    app_id = str(app_id)
    path = f"{STEAM_APPS_KEY}\\{app_id}"
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE | _view_flags()
        )
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise CredentialError(f"打开注册表键失败 {path}: {exc}") from exc
    removed: list[str] = []
    with key:
        for name in names:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise CredentialError(f"删除 {name} 失败: {exc}") from exc
            removed.append(name)
    if removed:
        logger.info("已删除凭据 appid=%s values=%s", app_id, ",".join(removed))
    return removed


def steam_install_path() -> str:
    """从注册表读取 Steam 安装路径（与上游 extract_tickets 一致）。"""
    winreg = _winreg()
    for hive, path, flags in (
        (winreg.HKEY_CURRENT_USER, STEAM_KEY, _view_flags()),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\{STEAM_KEY}", winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\{STEAM_KEY}", 0),
    ):
        try:
            with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | flags) as key:
                value, _ = winreg.QueryValueEx(key, "SteamPath")
        except (FileNotFoundError, OSError):
            continue
        if value:
            return str(value).replace("/", "\\")
    return ""


def active_user() -> tuple[int, str]:
    """读取当前登录的 Steam 账号 ``(account_id, universe)``，无登录时返回 ``(0, "")``。

    对应上游 ``SteamCredentialStore::GetActiveUser``，读取
    ``HKCU\\Software\\Valve\\Steam\\ActiveProcess\\ActiveUser`` 与 ``Universe``。
    """
    winreg = _winreg()
    path = rf"{STEAM_KEY}\ActiveProcess"
    account_id = 0
    universe = ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ | _view_flags()
        ) as key:
            try:
                account_id = int(winreg.QueryValueEx(key, "ActiveUser")[0])
            except (FileNotFoundError, OSError, TypeError, ValueError):
                account_id = 0
            try:
                universe = str(winreg.QueryValueEx(key, "Universe")[0]).strip("\x00")
            except (FileNotFoundError, OSError):
                universe = ""
    except (FileNotFoundError, OSError):
        return 0, ""
    return account_id, universe


def active_steam_id() -> str:
    """当前登录账号的 SteamID64（未登录时返回空串）。"""
    account_id, _universe = active_user()
    if not account_id:
        return ""
    return str(account_id_to_steam_id64(account_id))


def check_account_match(bundle: TicketBundle) -> str:
    """检查票据所属账号与当前登录账号是否一致。

    上游经验（``Pipe/Features/DenuvoAuth`` 与 BetterSteamTools 的 ``EticketClient``）：
    如果给 Denuvo 的所有权票据与 Steam 当前登录账号不是同一个，Denuvo 会做交叉
    校验并失败（错误码 ``012``）。BetterSteamTools 甚至在缓存里记录票据对应的
    steamid，一旦注册表账号变化就立刻丢弃缓存重新现签。

    Returns:
        ``""``            —— 一致，或无法判断（未登录 / 票据无 SteamID）
        ``"mismatch:..."``—— 不一致，附带可读说明
        ``"unavailable"`` —— 注册表不可用
    """
    try:
        current = active_steam_id()
    except CredentialError:
        return "unavailable"
    if not current:
        return ""
    ticket_id = bundle.derived_steam_id
    if not ticket_id:
        return ""
    if str(ticket_id) != str(current):
        return (
            f"mismatch: 票据属于账号 {ticket_id}，当前 Steam 登录的是 {current}。"
            "Denuvo 交叉校验会失败（错误码 012），请改用当前账号拥有的票据。"
        )
    return ""
