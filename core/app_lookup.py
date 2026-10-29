"""AppID 查询与本地更新检测。

数据来源全部在本地：

* ``steamapps/appmanifest_<appid>.acf``：已安装版本、StateFlags、目标 Build、
  InstalledDepots 以及清单文件是否存在；
* ``appcache/appinfo.vdf``：Steam 客户端缓存的游戏名、安装目录、公开分支
  BuildID 与各 Depot 的公开清单 GID；
* ``config/lua``、``config/stplug-in``：是否已有对应的 Lua 规则；
* ``manifest_pins.json``：哪些 Depot 被用户主动固定了旧版本。

不会访问网络，也不会修改任何文件。
"""
from __future__ import annotations

import io
import struct
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from core.local_scanner import LocalGameScanner, parse_vdf
from utils.logger import setup_logger

logger = setup_logger(__name__)

_APPINFO_MAGIC = b"DV\x07"
_APPINFO_VERSIONS = (39, 40, 41)
_MAX_KV_DEPTH = 64

# StateFlags 中表示「正在/等待更新」的位。
_UPDATE_FLAG_LABELS = (
    (2, "Steam 标记为需要更新"),
    (8, "更新已排队"),
    (32, "文件缺失，需要验证"),
    (128, "磁盘读取错误"),
    (512, "更新正在运行"),
    (1024, "更新已暂停"),
    (2048, "更新已开始"),
    (4194304, "正在下载更新"),
)


@dataclass
class AppInfoEntry:
    """``appinfo.vdf`` 中的一条应用记录。"""

    app_id: str
    name: str = ""
    app_type: str = ""
    install_dir: str = ""
    latest_build_id: str = ""
    latest_updated: int = 0
    depot_gids: dict[str, str] = field(default_factory=dict)
    depot_sizes: dict[str, int] = field(default_factory=dict)


@dataclass
class LocalApp:
    """合并 appmanifest / appinfo / Lua / 固定记录后的应用视图。"""

    app_id: str
    name: str = ""
    app_type: str = ""
    installed: bool = False
    install_dir: str = ""
    library: str = ""
    acf_path: str = ""
    build_id: str = ""
    target_build_id: str = ""
    latest_build_id: str = ""
    state_flags: int = 0
    last_updated: int = 0
    size_on_disk: int = 0
    bytes_to_download: int = 0
    bytes_downloaded: int = 0
    last_owner: str = ""
    installed_depots: dict[str, str] = field(default_factory=dict)
    latest_depots: dict[str, str] = field(default_factory=dict)
    latest_depot_sizes: dict[str, int] = field(default_factory=dict)
    missing_manifests: list[str] = field(default_factory=list)
    lua_rules: list[str] = field(default_factory=list)
    lua_enabled: bool = False
    lua_conflict: bool = False
    pinned_depots: dict[str, str] = field(default_factory=dict)
    update_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def update_pending(self) -> bool:
        return bool(self.update_reasons)

    @property
    def lua_status(self) -> str:
        if not self.lua_rules:
            return "无规则"
        if self.lua_conflict:
            return "规则冲突"
        return "已启用" if self.lua_enabled else "已禁用"

    @property
    def update_status(self) -> str:
        if not self.installed:
            return "未安装"
        if self.update_reasons:
            return "；".join(self.update_reasons)
        if self.latest_build_id:
            return "已是最新（本地缓存）"
        return "未知（appinfo 无记录）"


def _read_exact(stream: io.BytesIO, size: int) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise ValueError("unexpected end of appinfo data")
    return data


class _BinaryKVReader:
    """最小化二进制 KeyValues 读取器，支持 V29 的字符串索引表。"""

    def __init__(self, data: bytes, key_table: list[str] | None):
        self._stream = io.BytesIO(data)
        self._key_table = key_table

    def read_object(self, depth: int = 0) -> dict[str, Any]:
        if depth > _MAX_KV_DEPTH:
            raise ValueError("binary KV nesting too deep")
        result: dict[str, Any] = {}
        while True:
            raw = self._stream.read(1)
            if not raw:
                raise ValueError("unexpected end of binary KV")
            node_type = raw[0]
            if node_type in (8, 11):
                return result
            key = self._read_key()
            if node_type == 0:
                result[key] = self.read_object(depth + 1)
            elif node_type == 1:
                result[key] = self._read_cstring()
            elif node_type in (2, 4, 6):
                result[key] = struct.unpack("<i", _read_exact(self._stream, 4))[0]
            elif node_type == 3:
                result[key] = struct.unpack("<f", _read_exact(self._stream, 4))[0]
            elif node_type == 7:
                result[key] = struct.unpack("<Q", _read_exact(self._stream, 8))[0]
            elif node_type == 10:
                result[key] = struct.unpack("<q", _read_exact(self._stream, 8))[0]
            elif node_type == 5:
                length = struct.unpack("<i", _read_exact(self._stream, 4))[0]
                if length < 0 or length > 1_000_000:
                    raise ValueError(f"invalid wide string length: {length}")
                text = _read_exact(self._stream, length * 2).decode("utf-16-le", "replace")
                result[key] = text.split("\x00", 1)[0]
            else:
                raise ValueError(f"unsupported binary KV node type: {node_type}")

    def _read_key(self) -> str:
        if self._key_table is not None:
            index = struct.unpack("<i", _read_exact(self._stream, 4))[0]
            if not 0 <= index < len(self._key_table):
                raise ValueError(f"binary KV key index out of range: {index}")
            return self._key_table[index]
        return self._read_cstring()

    def _read_cstring(self) -> str:
        buffer = bytearray()
        while True:
            char = self._stream.read(1)
            if not char or char == b"\x00":
                break
            buffer.extend(char)
        return buffer.decode("utf-8", "replace")


def _read_key_table(stream: io.BytesIO, offset: int) -> list[str]:
    stream.seek(offset)
    count = struct.unpack("<I", _read_exact(stream, 4))[0]
    if count > 1_000_000:
        raise ValueError(f"appinfo key table too large: {count}")
    table: list[str] = []
    for _ in range(count):
        buffer = bytearray()
        while True:
            char = stream.read(1)
            if not char or char == b"\x00":
                break
            buffer.extend(char)
        table.append(buffer.decode("utf-8", "replace"))
    return table


def _parse_appinfo(path: Path) -> dict[str, AppInfoEntry]:
    apps: dict[str, AppInfoEntry] = {}
    with path.open("rb") as stream:
        magic = stream.read(4)
        if len(magic) != 4 or magic[1:] != _APPINFO_MAGIC:
            raise ValueError(f"unsupported appinfo magic: {magic!r}")
        version = magic[0]
        if version not in _APPINFO_VERSIONS:
            raise ValueError(f"unsupported appinfo version: {version}")
        _read_exact(stream, 4)  # universe
        key_table: list[str] | None = None
        if version >= 41:
            key_table_offset = struct.unpack("<q", _read_exact(stream, 8))[0]
            resume = stream.tell()
            key_table = _read_key_table(stream, key_table_offset)
            stream.seek(resume)

        while True:
            raw_id = stream.read(4)
            if len(raw_id) != 4:
                break
            app_id = struct.unpack("<I", raw_id)[0]
            if app_id == 0:
                break
            size = struct.unpack("<I", _read_exact(stream, 4))[0]
            if size > 256 * 1024 * 1024:
                raise ValueError(f"appinfo entry too large: {size}")
            payload = _read_exact(stream, size)
            header_size = 40 if version == 39 else 60
            if len(payload) < header_size:
                continue
            info_state, last_updated = struct.unpack("<II", payload[:8])
            data = payload[header_size:]
            try:
                parsed = _BinaryKVReader(data, key_table).read_object()
            except ValueError as exc:
                logger.debug("解析 appinfo %s 失败: %s", app_id, exc)
                continue
            info = parsed.get("appinfo")
            if not isinstance(info, dict):
                continue
            common = info.get("common") if isinstance(info.get("common"), dict) else {}
            config = info.get("config") if isinstance(info.get("config"), dict) else {}
            depots = info.get("depots") if isinstance(info.get("depots"), dict) else {}
            branches = depots.get("branches") if isinstance(depots.get("branches"), dict) else {}
            public = branches.get("public") if isinstance(branches.get("public"), dict) else {}
            latest_build = str(public.get("buildid") or "")
            depot_gids: dict[str, str] = {}
            depot_sizes: dict[str, int] = {}
            for depot_id, depot in depots.items():
                if not str(depot_id).isdigit() or not isinstance(depot, dict):
                    continue
                manifests = depot.get("manifests")
                if not isinstance(manifests, dict):
                    continue
                branch = manifests.get("public")
                if not isinstance(branch, dict):
                    continue
                gid = str(branch.get("gid") or "")
                if gid.isdigit():
                    depot_gids[str(depot_id)] = gid
                    try:
                        depot_sizes[str(depot_id)] = max(int(branch.get("size") or 0), 0)
                    except (TypeError, ValueError):
                        depot_sizes[str(depot_id)] = 0
            apps[str(app_id)] = AppInfoEntry(
                app_id=str(app_id),
                name=str(common.get("name") or ""),
                app_type=str(common.get("type") or ""),
                install_dir=str(config.get("installdir") or ""),
                latest_build_id=latest_build,
                latest_updated=int(public.get("timeupdated") or public.get("timebuildupdated") or 0),
                depot_gids=depot_gids,
                depot_sizes=depot_sizes,
            )
    return apps


@lru_cache(maxsize=8)
def _read_appinfo_cached(path: str, mtime_ns: int, size: int) -> dict[str, AppInfoEntry]:
    del mtime_ns, size  # 仅用于缓存失效
    try:
        return _parse_appinfo(Path(path))
    except (OSError, ValueError) as exc:
        logger.warning("读取 appinfo.vdf 失败 %s: %s", path, exc)
        return {}


def read_appinfo(steam_path: str) -> dict[str, AppInfoEntry]:
    """读取 ``<Steam>/appcache/appinfo.vdf``；不存在或损坏时返回空字典。"""
    if not steam_path:
        return {}
    path = Path(steam_path) / "appcache" / "appinfo.vdf"
    try:
        stat = path.stat()
    except OSError:
        return {}
    return _read_appinfo_cached(str(path), stat.st_mtime_ns, stat.st_size)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_appmanifest(acf_path: Path, library: str) -> LocalApp | None:
    try:
        with acf_path.open("r", encoding="utf-8", errors="replace") as stream:
            parsed = parse_vdf(stream.read())
    except OSError as exc:
        logger.debug("读取 appmanifest 失败 %s: %s", acf_path, exc)
        return None
    state = parsed.get("AppState")
    if not isinstance(state, dict):
        return None
    app_id = str(state.get("appid") or "").strip()
    if not app_id.isdigit():
        return None

    installed_depots: dict[str, str] = {}
    depots = state.get("InstalledDepots")
    if isinstance(depots, dict):
        for depot_id, info in depots.items():
            if not str(depot_id).isdigit() or not isinstance(info, dict):
                continue
            gid = str(info.get("manifest") or "").strip()
            if gid and gid != "0":
                installed_depots[str(depot_id)] = gid

    install_name = str(state.get("installdir") or "").strip()
    install_dir = (
        str(Path(library) / "steamapps" / "common" / install_name) if install_name else ""
    )
    return LocalApp(
        app_id=app_id,
        name=str(state.get("name") or "").strip(),
        installed=True,
        install_dir=install_dir,
        library=str(library),
        acf_path=str(acf_path),
        build_id=str(state.get("buildid") or "").strip(),
        target_build_id=str(state.get("TargetBuildID") or "").strip(),
        state_flags=_to_int(state.get("StateFlags")),
        last_updated=_to_int(state.get("LastUpdated")),
        size_on_disk=_to_int(state.get("SizeOnDisk")),
        bytes_to_download=_to_int(state.get("BytesToDownload")),
        bytes_downloaded=_to_int(state.get("BytesDownloaded")),
        last_owner=str(state.get("LastOwner") or "").strip(),
        installed_depots=installed_depots,
    )


def scan_installed_apps(steam_path: str) -> list[LocalApp]:
    """扫描所有 Steam 库中的 appmanifest（包含被隐藏的游戏）。"""
    if not steam_path:
        return []
    apps: dict[str, LocalApp] = {}
    scanner = LocalGameScanner(steam_path)
    for library in scanner.find_steam_libraries():
        steamapps = Path(library) / "steamapps"
        try:
            manifests = sorted(steamapps.glob("appmanifest_*.acf"))
        except OSError:
            continue
        for acf_path in manifests:
            app = _parse_appmanifest(acf_path, library)
            if app and app.app_id not in apps:
                apps[app.app_id] = app
    return sorted(apps.values(), key=lambda item: (int(item.app_id), item.name.lower()))


def _manifest_present(steam_path: str, library: str, depot_id: str, gid: str) -> bool:
    filename = f"{depot_id}_{gid}.manifest"
    directories: list[Path] = []
    if library:
        directories.append(Path(library) / "steamapps" / "depotcache")
    if steam_path:
        directories.append(Path(steam_path) / "depotcache")
        directories.append(Path(steam_path) / "config" / "depotcache")
    for directory in directories:
        path = directory / filename
        try:
            if path.is_file() and path.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def _lua_index(steam_path: str) -> dict[str, list[Any]]:
    try:
        from core.lua_rules import scan_lua_rules

        rules = scan_lua_rules(steam_path)
    except Exception as exc:  # noqa: BLE001 - 诊断型功能，失败时降级为无规则
        logger.debug("扫描 Lua 规则失败: %s", exc)
        return {}
    index: dict[str, list[Any]] = {}
    for rule in rules:
        index.setdefault(rule.app_id, []).append(rule)
    return index


def _pin_index() -> dict[str, dict[str, dict[str, Any]]]:
    try:
        from core.manifest_pins import load_pins

        return load_pins()
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取 Manifest 固定记录失败: %s", exc)
        return {}


def _detect_update(app: LocalApp) -> None:
    """填充 ``update_reasons`` / ``notes``；只做本地状态判断。"""
    reasons: list[str] = []
    flags = app.state_flags
    for bit, label in _UPDATE_FLAG_LABELS:
        if flags & bit:
            reasons.append(label)

    target = app.target_build_id.strip()
    if target and target != "0" and app.build_id and target != app.build_id:
        reasons.append(f"已排队目标 Build {target}")

    if app.build_id and app.latest_build_id:
        try:
            installed_build = int(app.build_id)
            latest_build = int(app.latest_build_id)
        except ValueError:
            installed_build = latest_build = 0
        if latest_build > installed_build:
            reasons.append(f"本地缓存显示新 Build {app.latest_build_id}")
        elif installed_build > latest_build:
            app.notes.append("本地 Build 高于 appinfo 缓存（测试版/预载）")

    outdated: list[str] = []
    pinned: list[str] = []
    for depot_id, gid in app.installed_depots.items():
        latest = app.latest_depots.get(depot_id)
        if not latest or latest == gid:
            continue
        pinned_gid = app.pinned_depots.get(depot_id)
        if pinned_gid:
            pinned.append(depot_id)
        else:
            outdated.append(depot_id)
    if outdated:
        preview = "、".join(outdated[:4])
        suffix = " 等" if len(outdated) > 4 else ""
        reasons.append(f"{len(outdated)} 个 Depot 清单落后（{preview}{suffix}）")
    if pinned:
        preview = "、".join(pinned[:4])
        suffix = " 等" if len(pinned) > 4 else ""
        app.notes.append(f"{len(pinned)} 个 Depot 已固定清单（{preview}{suffix}）")

    app.update_reasons = reasons


def build_app_index(steam_path: str, *, include_appinfo: bool = True) -> list[LocalApp]:
    """构建完整的本地应用索引。"""
    installed = scan_installed_apps(steam_path)
    appinfo = read_appinfo(steam_path) if include_appinfo else {}
    lua = _lua_index(steam_path)
    pins = _pin_index()

    apps: dict[str, LocalApp] = {}
    for app in installed:
        apps[app.app_id] = app

    if include_appinfo:
        for app_id, info in appinfo.items():
            if app_id not in apps:
                apps[app_id] = LocalApp(
                    app_id=app_id,
                    name=info.name,
                    app_type=info.app_type,
                    install_dir=info.install_dir,
                    latest_build_id=info.latest_build_id,
                    latest_depots=dict(info.depot_gids),
                    latest_depot_sizes=dict(info.depot_sizes),
                )

    for app_id, app in apps.items():
        info = appinfo.get(app_id)
        if info:
            app.name = app.name or info.name
            app.app_type = app.app_type or info.app_type
            app.latest_build_id = info.latest_build_id
            app.latest_depots = dict(info.depot_gids)
            app.latest_depot_sizes = dict(info.depot_sizes)
        rules = lua.get(app_id, [])
        app.lua_rules = [rule.path for rule in rules]
        app.lua_enabled = any(rule.enabled for rule in rules)
        app.lua_conflict = any(rule.conflicts for rule in rules)
        app.pinned_depots = {
            depot_id: str(entry.get("gid") or "")
            for depot_id, entry in pins.get(app_id, {}).items()
            if entry.get("gid")
        }
        if app.installed:
            app.missing_manifests = [
                f"{depot_id}_{gid}"
                for depot_id, gid in app.installed_depots.items()
                if not _manifest_present(steam_path, app.library, depot_id, gid)
            ]
            if app.missing_manifests:
                app.notes.append(f"缺失 {len(app.missing_manifests)} 个 Depot 清单文件")
            _detect_update(app)

    return sorted(
        apps.values(),
        key=lambda item: (not item.installed, int(item.app_id), item.name.lower()),
    )


def search_apps(
    apps: Iterable[LocalApp],
    query: str,
    *,
    include_uninstalled: bool = True,
    limit: int = 500,
) -> list[LocalApp]:
    """按 AppID、游戏名或安装目录搜索。空查询只返回已安装游戏。"""
    keyword = query.strip().lower()
    if not keyword:
        return [app for app in apps if app.installed][:limit]

    exact: list[LocalApp] = []
    prefix: list[LocalApp] = []
    other: list[LocalApp] = []
    for app in apps:
        if not include_uninstalled and not app.installed:
            continue
        haystack = f"{app.app_id} {app.name} {app.install_dir}".lower()
        if keyword not in haystack:
            continue
        if app.app_id == keyword:
            exact.append(app)
        elif app.app_id.startswith(keyword):
            prefix.append(app)
        else:
            other.append(app)

    def sort_key(app: LocalApp) -> tuple[bool, int, str]:
        return (not app.installed, int(app.app_id), app.name.lower())

    exact.sort(key=sort_key)
    prefix.sort(key=sort_key)
    other.sort(key=sort_key)
    return (exact + prefix + other)[:limit]


def update_summary(apps: Iterable[LocalApp]) -> str:
    """生成页面顶部的一行摘要。"""
    items = list(apps)
    installed = [app for app in items if app.installed]
    pending = [app for app in installed if app.update_pending]
    missing = [app for app in installed if app.missing_manifests]
    return (
        f"已安装 {len(installed)} 个游戏；"
        f"发现 {len(pending)} 个待更新/异常；"
        f"{len(missing)} 个游戏缺少 Depot 清单"
    )
