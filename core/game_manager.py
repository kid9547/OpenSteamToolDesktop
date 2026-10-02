
"""
游戏库管理器 — 基于 Lua 配置文件的真实实现

管理 OpenSteamTool 的 Lua 配置文件：
- 扫描 <lua_dir>/*.lua
- 解析 addappid/addtoken/setManifestid/setAppTicket/setStat
- 支持添加/删除/搜索游戏
- 支持带完整元数据的入库（depot info、DLC、token）
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from core.config_manager import ConfigManager
from core.local_scanner import LocalGameScanner
from utils.logger import setup_logger

logger = setup_logger(__name__)


def _build_ticket_lines(metadata: "GameMetadata") -> list[str]:
    """构建 D 加密票据的 Lua 指令行。

    OpenSteamTool.dll 注册了 ``setappticket`` / ``seteticket`` 两个 Lua 函数
    （源码 ``src/Utils/Config/LuaConfig.cpp``），它们会把十六进制票据解码后写入
    Windows 凭据存储 ``HKCU\\Software\\Valve\\Steam\\Apps\\<AppID>``：

    * ``AppTicket`` —— 应用所有权票据（AppOwnershipTicket）
    * ``ETicket``   —— 加密应用票据（EncryptedAppTicket）

    .. important::

        只有已在同一 Lua 中 ``addappid`` 的 AppID 才会被应用票据
        （源码 ``AppTicket.cpp`` 中的 ``LuaConfig::HasDepot`` 判断），因此本函数的
        输出必须排在 ``addappid`` 之后。

    .. note::

        票据是 Valve 签名的真实凭据，有效期约 30 分钟~数小时，本地无法伪造。
        过期后需重新导入，否则 Denuvo 校验会报错误码 ``88500005``。
    """
    lines: list[str] = []
    app_id = str(metadata.app_id)

    def _clean(value: str) -> str:
        return re.sub(r"[\s:_-]", "", value or "").lower()

    app_ticket = _clean(metadata.app_ticket)
    eticket = _clean(metadata.eticket)
    if not app_ticket and not eticket:
        return lines

    lines.append(f"-- D 加密票据（来源：凭据导入/本机提取，有效期有限）")
    if app_ticket:
        lines.append(f'setAppTicket({app_id}, "{app_ticket}")')
        logger.debug(f"  D-encryption: setAppTicket({app_id}, {len(app_ticket) // 2} bytes)")
    if eticket:
        lines.append(f'setETicket({app_id}, "{eticket}")')
        logger.debug(f"  D-encryption: setETicket({app_id}, {len(eticket) // 2} bytes)")
    return lines


# ── 数据类 ──────────────────────────────────────────────────


@dataclass
class DepotInfo:
    """单个 Depot 的元数据"""
    depot_id: str
    manifest_gid: str = ""
    size: int = 0
    depot_key: str = ""  # Depot 解密密钥（HEX 字符串）


@dataclass
class GameMetadata:
    """游戏的完整入库元数据"""
    app_id: str
    name: str = ""
    depots: list[DepotInfo] = field(default_factory=list)
    dlc_ids: list[str] = field(default_factory=list)
    dlc_depots: list[tuple[str, DepotInfo]] = field(default_factory=list)
    access_token: str = ""
    workshop_key: str = ""
    app_level_key: str = ""  # 应用级密钥（来自 Sudama，打在 addappid(app_id, 0, key) 主游戏行）
    # D 加密（Denuvo）票据：写入 Lua 的 setAppTicket/setETicket，同时落注册表凭据存储
    app_ticket: str = ""
    eticket: str = ""
    steam_id: str = ""


@dataclass
class GameInfo:
    """游戏信息（兼容标准 OST Lua、外部 Lua、本地磁盘已安装游戏）"""
    app_id: str
    name: str = ""
    has_token: bool = False
    has_manifest: bool = False
    has_appticket: bool = False
    lua_path: str = ""
    manifest_ready: bool = True
    missing_manifests: list[str] = field(default_factory=list)
    source: str = "ost_lua"              # "ost_lua", "steam_local", "external_lua", "steamtools"
    is_installed: bool = False
    install_dir: str = ""
    size_on_disk: int = 0
    has_lua: bool = True
    depots: list[tuple[str, str]] = field(default_factory=list)
    acf_path: str = ""


class LuaGameManager:
    """基于 Lua 配置文件与本地全盘扫描的游戏库管理器"""

    def __init__(self, lua_dir: str = "", steam_path: str = "", config_manager: ConfigManager | None = None):
        """初始化游戏管理器

        Args:
            lua_dir: OpenSteamTool Lua 配置目录路径
            steam_path: Steam 安装根目录路径（可选）
            config_manager: 配置持久化管理器（可选）
        """
        self._lua_dir: str = lua_dir
        self._steam_path: str = steam_path
        self._scanner = LocalGameScanner(self._steam_path, self._lua_dir)
        self._games: list[GameInfo] = []
        self._config_manager = config_manager or ConfigManager()
        logger.debug(f"LuaGameManager initialized: lua_dir={lua_dir or '(empty)'}, steam_path={self._steam_path or '(empty)'}")

    # ---- 忽略/隐藏名单管理 ----

    def get_hidden_app_ids(self) -> set[str]:
        """获取所有已出库或被隐藏忽略的 AppID 集合"""
        raw = self._config_manager.get("hidden_app_ids", [])
        if isinstance(raw, list):
            return set(str(x) for x in raw)
        return set()

    def hide_game(self, app_id: str) -> None:
        """将指定 AppID 加入隐藏/忽略黑名单并持久化保存"""
        hidden = self.get_hidden_app_ids()
        hidden.add(str(app_id))
        self._config_manager.set("hidden_app_ids", sorted(list(hidden)))
        logger.info(f"AppID {app_id} added to hidden blacklist")

    def unhide_game(self, app_id: str) -> None:
        """将指定 AppID 从隐藏/忽略黑名单中移出并持久化保存"""
        hidden = self.get_hidden_app_ids()
        if str(app_id) in hidden:
            hidden.discard(str(app_id))
            self._config_manager.set("hidden_app_ids", sorted(list(hidden)))
            logger.info(f"AppID {app_id} unhidden and removed from blacklist")

    # ---- 目录管理 ----

    def set_steam_path(self, path: str) -> None:
        """设置 Steam 安装根目录并同步扫描器路径"""
        self._steam_path = path
        self._scanner.set_paths(self._steam_path, self._lua_dir)

    def set_lua_dir(self, path: str) -> None:
        """设置 Lua 配置目录并自动创建

        Args:
            path: 目录路径
        """
        logger.debug(f"Setting Lua directory: {path}")
        self._lua_dir = path
        self._scanner.set_paths(self._steam_path, self._lua_dir)
        if path and not os.path.exists(path):
            os.makedirs(path, exist_ok=True)
            logger.info(f"Created Lua directory: {path}")

    def get_lua_dir(self) -> str:
        """获取当前 Lua 配置目录"""
        return self._lua_dir

    # ---- 游戏查询 ----

    def refresh(self, scan_local: bool = False, scan_all_drives: bool = False) -> list[GameInfo]:
        """重新扫描 Lua 目录，可选扫描本地磁盘与全盘 Steam 库，返回最新游戏列表"""
        logger.debug(f"Refreshing game list from: {self._lua_dir or '(no directory)'}, scan_local={scan_local}")
        old_count = len(self._games)

        hidden_ids = self.get_hidden_app_ids()

        # 1. 扫描标准 Lua 文件（过滤已出库/隐藏名单）
        lua_games = self._scan_lua_dir()
        game_map: dict[str, GameInfo] = {g.app_id: g for g in lua_games if g.app_id not in hidden_ids}

        # 2. 深度扫描本地盘与第三方工具
        if scan_local and self._steam_path:
            try:
                self._scanner.set_paths(self._steam_path, self._lua_dir)

                # A. 扫描非纯数字命名/外部 Lua 文件
                ext_luas = self._scanner.scan_external_lua_files(hidden_app_ids=hidden_ids)
                for ext in ext_luas:
                    aid = ext["app_id"]
                    if aid not in game_map and aid not in hidden_ids:
                        game_map[aid] = GameInfo(
                            app_id=aid,
                            name=ext.get("name", ""),
                            lua_path=ext.get("lua_path", ""),
                            has_lua=True,
                            source="external_lua",
                            depots=ext.get("depots", []),
                            has_manifest=bool(ext.get("depots")),
                            manifest_ready=ext.get("manifest_ready", True),
                            missing_manifests=ext.get("missing_manifests", []),
                        )

                # B. 扫描 SteamTools st.json
                st_games = self._scanner.scan_steamtools_games(hidden_app_ids=hidden_ids)
                for st in st_games:
                    aid = st["app_id"]
                    if aid not in game_map and aid not in hidden_ids:
                        game_map[aid] = GameInfo(
                            app_id=aid,
                            name=st.get("name", ""),
                            source="steamtools",
                            has_lua=False,
                            depots=st.get("depots", []),
                            has_manifest=bool(st.get("depots")),
                            manifest_ready=st.get("manifest_ready", True),
                            missing_manifests=st.get("missing_manifests", []),
                        )

                # C. 扫描所有 Steam 库 appmanifest_*.acf 文件
                installed_games = self._scanner.scan_installed_games(scan_all_drives=scan_all_drives, hidden_app_ids=hidden_ids)
                for inst in installed_games:
                    aid = inst["app_id"]
                    if aid in hidden_ids:
                        continue
                    if aid in game_map:
                        g = game_map[aid]
                        g.is_installed = True
                        g.install_dir = inst.get("install_dir", "")
                        g.size_on_disk = inst.get("size_on_disk", 0)
                        g.acf_path = inst.get("acf_path", "")
                        if not g.name and inst.get("name"):
                            g.name = inst["name"]
                        if not g.depots and inst.get("depots"):
                            g.depots = inst["depots"]
                    else:
                        game_map[aid] = GameInfo(
                            app_id=aid,
                            name=inst.get("name", ""),
                            source="steam_local",
                            is_installed=True,
                            install_dir=inst.get("install_dir", ""),
                            size_on_disk=inst.get("size_on_disk", 0),
                            has_lua=False,
                            depots=inst.get("depots", []),
                            has_manifest=bool(inst.get("depots")),
                            manifest_ready=inst.get("manifest_ready", True),
                            missing_manifests=inst.get("missing_manifests", []),
                            acf_path=inst.get("acf_path", ""),
                        )
            except Exception as e:
                logger.warning(f"Error during deep local scan: {e}")

        self._games = list(game_map.values())
        new_count = len(self._games)
        logger.info(f"Game list refreshed: {old_count} -> {new_count} games (OST + Local + External)")
        return self._games

    def deep_scan(self, scan_all_drives: bool = True) -> list[GameInfo]:
        """全盘深度扫描（扫描全盘 Steam 库安装游戏与第三方工具配置）"""
        return self.refresh(scan_local=True, scan_all_drives=scan_all_drives)

    def take_over_game(self, app_id: str) -> bool:
        """为通过本地盘/第三方工具发现的游戏一键生成标准 OST Lua 配置并纳入管理"""
        target = None
        for g in self._games:
            if g.app_id == app_id:
                target = g
                break
        if not target:
            return False

        if not self._lua_dir:
            return False

        ok = self._scanner.take_over_game_as_ost_lua(
            app_id=target.app_id,
            name=target.name,
            depots=target.depots,
        )
        if ok:
            self.unhide_game(app_id)
            target.has_lua = True
            target.source = "ost_lua"
            target.lua_path = os.path.join(self._lua_dir, f"{app_id}.lua")
            self._ensure_manifest_resolver()
            logger.info(f"Took over game {app_id} into OpenSteamTool management")
        return ok

    def get_games(self) -> list[GameInfo]:
        """获取游戏列表（优先返回缓存，首次调用时扫描）"""
        if not self._games and self._lua_dir:
            logger.debug("Game list empty, triggering refresh...")
            self.refresh()
        return list(self._games)

    def has_game(self, app_id: str) -> bool:
        """检查游戏是否已在库中（优先内存，减少文件系统访问）"""
        if self._games:
            found = any(g.app_id == app_id for g in self._games)
            logger.debug(f"Checking game {app_id} in memory: {found}")
            return found
        if not self._lua_dir:
            logger.debug(f"Checking game {app_id}: no lua_dir, returning False")
            return False
        file_exists = os.path.exists(os.path.join(self._lua_dir, f"{app_id}.lua"))
        logger.debug(f"Checking game {app_id} on disk: {file_exists}")
        return file_exists

    def search_games(self, keyword: str) -> list[GameInfo]:
        """按名称或 AppID 搜索游戏"""
        keyword_lower = keyword.lower()
        games = self.get_games()
        return [
            g for g in games
            if keyword_lower in g.name.lower() or keyword_lower in g.app_id
        ]

    # ---- 游戏管理 ----

    def add_game(
        self,
        app_id: str,
        name: str = "",
        token: str = "",
        manifest_id: str = "",
    ) -> bool:
        """将游戏加入库中（写入 Lua 文件）— 简单模式，向后兼容

        Args:
            app_id: Steam AppID
            name: 游戏名称（写入注释）
            token: PICS 访问令牌（可选）
            manifest_id: 已废弃，请使用 add_game_with_metadata

        Returns:
            是否写入成功
        """
        logger.debug(f"add_game called: app_id={app_id}, name={name}, token={'yes' if token else 'no'}, manifest_id={manifest_id or 'none'}")
        metadata = GameMetadata(
            app_id=app_id,
            name=name,
            access_token=token,
        )
        # 旧 manifest_id 是 app_id 级别的，转为 depot（兼容但标记废弃）
        if manifest_id:
            logger.warning(f"manifest_id parameter is deprecated, use add_game_with_metadata instead")
            metadata.depots = [DepotInfo(depot_id=app_id, manifest_gid=manifest_id)]
        return self.add_game_with_metadata(metadata)

    def add_game_basic(self, app_id: str, name: str = "") -> bool:
        """即时入库（立即写入基础 Lua 文件，后续后台拉取完整元数据时覆盖更新）"""
        self.unhide_game(app_id)
        filepath = ""
        if self._lua_dir:
            try:
                os.makedirs(self._lua_dir, exist_ok=True)
                filepath = os.path.join(self._lua_dir, f"{app_id}.lua")
                comment = f"-- {name} (由 OpenSteamToolDesktop 管理)\n" if name else f"-- AppID {app_id} (由 OpenSteamToolDesktop 管理)\n"
                basic_content = f"{comment}addappid({app_id})\n"
                if not os.path.exists(filepath):
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.write(basic_content)
                    logger.info(f"Basic Lua file written for AppID {app_id}: {filepath}")
                    self._ensure_manifest_resolver()
            except Exception as e:
                logger.warning(f"Could not write basic Lua for {app_id}: {e}")

        # 增量或更新内存列表
        for g in self._games:
            if g.app_id == app_id:
                if name and not g.name:
                    g.name = name
                if filepath and not g.lua_path:
                    g.lua_path = filepath
                return False

        info = GameInfo(
            app_id=app_id,
            name=name,
            lua_path=filepath,
        )
        self._games.append(info)
        logger.info(f"Game {app_id} ({name or 'unknown'}) added to library (basic)")
        return True

    def add_game_with_metadata(self, metadata: GameMetadata) -> bool:
        """将游戏加入库中（写入 Lua 文件）— 完整元数据模式

        Args:
            metadata: 包含 depots、dlcs、token 等完整元数据

        Returns:
            是否写入成功
        """
        self.unhide_game(metadata.app_id)
        if not self._lua_dir:
            logger.error("Cannot add game: lua_dir is not set")
            return False

        filepath = os.path.join(self._lua_dir, f"{metadata.app_id}.lua")
        logger.info(f"Adding game {metadata.app_id} ({metadata.name or 'unknown'})")
        logger.debug(f"Lua file path: {filepath}")
        depot_keys_count = sum(1 for d in metadata.depots if d.depot_key)
        logger.debug(f"Metadata: {len(metadata.depots)} depots ({depot_keys_count} with keys), "
                     f"{len(metadata.dlc_ids)} DLCs, token={'yes' if metadata.access_token else 'no'}, "
                     f"workshop_key={'yes' if metadata.workshop_key else 'no'}")

        content = self._build_lua_content(metadata)
        logger.debug(f"Lua content generated ({len(content)} chars)")

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"Game {metadata.app_id} added successfully")

            # 确保全局 manifest.lua 和 opensteamtool.toml 解析器就绪
            self._ensure_manifest_resolver()

            # 增量更新内存列表
            if not any(g.app_id == metadata.app_id for g in self._games):
                info = GameInfo(
                    app_id=metadata.app_id,
                    name=metadata.name,
                    lua_path=filepath,
                )
                if metadata.access_token:
                    info.has_token = True
                if metadata.depots:
                    info.has_manifest = True
                self._games.append(info)
                logger.debug(f"Added game {metadata.app_id} to memory list")
            else:
                # 更新已有条目的元数据标记
                for g in self._games:
                    if g.app_id == metadata.app_id:
                        if metadata.access_token:
                            g.has_token = True
                        if metadata.depots:
                            g.has_manifest = True
                        if metadata.name:
                            g.name = metadata.name
                        logger.debug(f"Updated game {metadata.app_id} in memory list")
                        break
            return True
        except OSError as e:
            logger.error(f"Failed to write Lua file for {metadata.app_id}: {e}")
            return False

    def _ensure_manifest_resolver(self):
        """确保 <Steam>/opensteamtool.toml 与 <Steam>/config/lua/manifest.lua 存在并配置 wudrm

        避免因上游 opensteamtool.com 403 导致 Steam 报告网络错误 / Access Denied。
        """
        if not self._lua_dir or not os.path.isdir(self._lua_dir):
            return

        manifest_lua_path = os.path.join(self._lua_dir, "manifest.lua")
        if not os.path.exists(manifest_lua_path):
            try:
                content = (
                    "-- manifest.lua — OpenSteamTool Manifest Request Code Resolver\n"
                    "-- Auto-generated by OpenSteamToolDesktop\n"
                    "-- Priority: wudrm -> steamrun\n"
                    "function fetch_manifest_code(gid)\n"
                    '    local body, st = http_get("http://gmrc.wudrm.com/manifest/" .. gid)\n'
                    '    if st == 200 and body and body:match("^%d+$") then\n'
                    "        return body\n"
                    "    end\n"
                    '    body, st = http_get("https://manifest.steam.run/api/manifest/" .. gid)\n'
                    "    if st == 200 and body then\n"
                    '        local code = body:match(\'"content":"(%d+)"\')\n'
                    "        if code then\n"
                    "            return code\n"
                    "        end\n"
                    "    end\n"
                    "    return nil\n"
                    "end\n\n"
                    "function fetch_manifest_code_ex(app_id, depot_id, gid)\n"
                    "    return fetch_manifest_code(gid)\n"
                    "end\n"
                )
                with open(manifest_lua_path, "w", encoding="utf-8") as f:
                    f.write(content)
                logger.info("Auto-created manifest.lua in config/lua")
            except Exception as e:
                logger.warning(f"Failed to auto-create manifest.lua: {e}")

        # 检查并补全 <Steam>/opensteamtool.toml
        try:
            steam_root = os.path.dirname(os.path.dirname(self._lua_dir))
            toml_path = os.path.join(steam_root, "opensteamtool.toml")
            if os.path.isdir(steam_root) and not os.path.exists(toml_path):
                toml_content = (
                    "# opensteamtool.toml — OpenSteamTool configuration\n"
                    "# Managed by OpenSteamToolDesktop\n\n"
                    "[log]\n"
                    'level = "info"\n\n'
                    "[manifest]\n"
                    'url = "wudrm"\n'
                    "timeout_resolve_ms = 5000\n"
                    "timeout_connect_ms = 5000\n"
                    "timeout_send_ms    = 10000\n"
                    "timeout_recv_ms    = 10000\n\n"
                    "[stats]\n"
                    "enable_api = true\n"
                )
                with open(toml_path, "w", encoding="utf-8") as f:
                    f.write(toml_content)
                logger.info("Auto-created opensteamtool.toml in Steam root")
        except Exception as e:
            logger.debug(f"Could not verify opensteamtool.toml: {e}")

    def parse_lua_to_metadata(self, app_id: str) -> "GameMetadata | None":
        """解析指定游戏的 Lua 文件，还原为 GameMetadata 对象

        用于编辑功能：将磁盘上的 Lua 文件解析为结构化数据，
        用户编辑后再通过 add_game_with_metadata() 写回。

        Returns:
            GameMetadata 对象；文件不存在或解析失败返回 None
        """
        if not self._lua_dir:
            logger.error("Cannot parse: lua_dir is not set")
            return None

        filepath = os.path.join(self._lua_dir, f"{app_id}.lua")
        if not os.path.exists(filepath):
            logger.warning(f"Lua file not found for {app_id}: {filepath}")
            return None

        logger.info(f"Parsing Lua file to metadata: {filepath}")

        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

        except OSError as e:
            logger.error(f"Failed to read Lua file {filepath}: {e}")
            return None

        # ── 初始化元数据 ──
        metadata = GameMetadata(app_id=app_id)

        # ── 1. 解析游戏名称（第一行注释）──
        name_match = re.search(
            r'^--\s*(.+?)(?:\s*\(由 OpenSteamToolDesktop 管理\))?\s*$',
            content, re.MULTILINE,
        )
        if name_match:
            raw_name = name_match.group(1).strip()
            if "由" not in raw_name:
                metadata.name = raw_name
                logger.debug(f"  Parsed name: {raw_name}")

        # ── 2. 解析所有 addappid 行 ──
        # 正则：addappid(ID)  或  addappid(ID, 0, "KEY")
        addappid_pattern = re.compile(
            r'addappid\(\s*(\d+)(?:\s*,\s*0\s*,\s*"([^"]*)")?\s*\)',
            re.IGNORECASE,
        )

        # 收集所有 addappid 条目，(depot_id, depot_key_or_None)
        addappid_entries: list[tuple[str, str]] = []
        for m in addappid_pattern.finditer(content):
            depot_id = m.group(1)
            depot_key = m.group(2) or ""
            addappid_entries.append((depot_id, depot_key))
            logger.debug(f"  Found addappid: id={depot_id}, key={'yes' if depot_key else 'no'}")

        # 第一个 addappid 是主游戏
        if addappid_entries:
            main_id, main_key = addappid_entries[0]
            if main_key:
                metadata.app_level_key = main_key
                logger.debug(f"  App-level key found for {main_id}")

        # 其余 addappid 条目：先尝试匹配已知的 depots/dlc，
        # 无法区分时暂存为 depot（写入时 depot 行格式一致）
        remaining = addappid_entries[1:]  # 排除主游戏

        # 从现有 metadata.depots 获取已知的 depot_id（如果从其他来源）
        # 此处先从 Lua 文件重新构建
        seen_ids = {app_id}  # 主游戏 ID 已处理

        for depot_id, depot_key in remaining:
            if depot_id == app_id:
                seen_ids.add(depot_id)
                continue
            seen_ids.add(depot_id)

            # 判断是否是 DLC：ID 不在常见 depot 范围（启发式：depot ID 通常 > app_id + 100）
            # 保守策略：全部作为 depot 处理，用户可在 UI 中调整
            depot_info = DepotInfo(depot_id=depot_id, depot_key=depot_key)
            metadata.depots.append(depot_info)

        # ── 3. 解析 addtoken 行 ──
        token_pattern = re.compile(
            r'addtoken\(\s*(\d+)\s*,\s*"([^"]*)"\s*\)',
            re.IGNORECASE,
        )
        for m in token_pattern.finditer(content):
            token_app_id = m.group(1)
            token_value = m.group(2)
            if token_app_id == app_id:
                metadata.access_token = token_value
                logger.debug(f"  Parsed access_token for {app_id}")

        # ── 4. 解析 setManifestid / setAppTicket / setETicket ──
        # 兼容三种写法（_build_lua_content 实际会输出带 size 的形式）：
        #   setManifestid(depot, "gid") / setManifestid(depot, gid)
        #   setManifestid(depot, "gid", 123456)
        manifest_pattern = re.compile(
            r'setManifestid\(\s*(\d+)\s*,\s*["\']?(\d+)["\']?\s*(?:,\s*(\d+)\s*)?\)',
            re.IGNORECASE,
        )
        for m in manifest_pattern.finditer(content):
            depot_id = m.group(1)
            manifest_gid = m.group(2)
            size = int(m.group(3)) if m.group(3) else 0
            # 尝试匹配已有 depot
            found = False
            for d in metadata.depots:
                if d.depot_id == depot_id:
                    d.manifest_gid = manifest_gid
                    if size:
                        d.size = size
                    found = True
                    break
            if not found:
                metadata.depots.append(
                    DepotInfo(depot_id=depot_id, manifest_gid=manifest_gid, size=size)
                )

        # ── 4b. 解析 D 加密票据（setAppTicket / setETicket）──
        # 这两个值由 OpenSteamTool.dll 写入注册表凭据存储
        # HKCU\Software\Valve\Steam\Apps\<appid>，是 Denuvo 游戏授权的关键数据。
        ticket_pattern = re.compile(
            r'set(app|e)ticket\(\s*(\d+)\s*,\s*["\']([0-9a-fA-F]+)["\']\s*\)',
            re.IGNORECASE,
        )
        for m in ticket_pattern.finditer(content):
            kind = m.group(1).lower()
            ticket_app_id = m.group(2)
            value = m.group(3)
            # 只收集主 AppID 自己的票据；depot 级票据不在本项目处理范围内
            if ticket_app_id != app_id:
                continue
            if kind == "app":
                metadata.app_ticket = value
            else:
                metadata.eticket = value

        logger.info(
            f"Parsed metadata for {app_id}: "
            f"name={metadata.name or '(none)'}, "
            f"depots={len(metadata.depots)}, "
            f"token={'yes' if metadata.access_token else 'no'}, "
            f"app_key={'yes' if metadata.app_level_key else 'no'}, "
            f"appticket={'yes' if metadata.app_ticket else 'no'}, "
            f"eticket={'yes' if metadata.eticket else 'no'}"
        )
        return metadata

    # ── D 加密票据写入 Lua ────────────────────────────────────
    _TICKET_LINE_RE = re.compile(
        r"^\s*set(?:app|e)ticket\s*\([^)]*\)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )

    def apply_ticket_bundle(self, app_id: str, bundle) -> str:
        """把 D 加密票据写入（或更新到）该游戏的 Lua 配置文件。

        流程：读取现有 Lua → 去掉旧的 ``setAppTicket``/``setETicket`` 行 →
        在文件末尾追加新票据行。``addappid`` 行保持不变，因此票据一定排在
        ``addappid`` 之后，符合 OpenSteamTool 的 ``LuaConfig::HasDepot`` 前提。

        Args:
            app_id: 目标 AppID。
            bundle: :class:`core.credential_store.TicketBundle`（或任何带
                ``app_ticket`` / ``eticket`` 属性的对象）。

        Returns:
            更新后的 Lua 文件绝对路径。

        Raises:
            ValueError: 未配置 Lua 目录、Lua 文件不存在或票据为空。
        """
        app_id = str(app_id)
        if not self._lua_dir:
            raise ValueError("未配置 Lua 目录，无法写入票据")
        filepath = os.path.join(self._lua_dir, f"{app_id}.lua")
        if not os.path.isfile(filepath):
            raise ValueError(f"Lua 文件不存在: {filepath}")

        app_ticket = re.sub(r"[\s:_-]", "", getattr(bundle, "app_ticket", "") or "").lower()
        eticket = re.sub(r"[\s:_-]", "", getattr(bundle, "eticket", "") or "").lower()

        with open(filepath, "r", encoding="utf-8", errors="replace") as stream:
            content = stream.read()

        kept = [line for line in content.splitlines() if not self._TICKET_LINE_RE.match(line)]
        while kept and not kept[-1].strip():
            kept.pop()

        # 空票据 = 清除模式（供"清除此游戏票据"使用）
        if not app_ticket and not eticket:
            if not kept:
                logger.warning("清除票据后 Lua 内容为空，已放弃写入 appid=%s", app_id)
                return filepath
            with open(filepath, "w", encoding="utf-8") as stream:
                stream.write("\n".join(kept) + "\n")
            logger.info("已从 Lua 清除票据 appid=%s", app_id)
            return filepath

        if app_ticket:
            kept.append(f'setAppTicket({app_id}, "{app_ticket}")')
        if eticket:
            kept.append(f'setETicket({app_id}, "{eticket}")')

        with open(filepath, "w", encoding="utf-8") as stream:
            stream.write("\n".join(kept) + "\n")

        logger.info(
            "已更新 Lua 票据 appid=%s appticket=%s eticket=%s",
            app_id,
            f"{len(app_ticket) // 2}B" if app_ticket else "-",
            f"{len(eticket) // 2}B" if eticket else "-",
        )
        return filepath

    def ticket_status_from_lua(self, app_id: str) -> dict[str, bool]:
        """仅从 Lua 文本判断票据是否存在（不访问注册表，便于界面快速展示）。"""
        app_id = str(app_id)
        if not self._lua_dir:
            return {"has_app_ticket": False, "has_eticket": False}
        filepath = os.path.join(self._lua_dir, f"{app_id}.lua")
        if not os.path.isfile(filepath):
            return {"has_app_ticket": False, "has_eticket": False}
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as stream:
                content = stream.read()
        except OSError:
            return {"has_app_ticket": False, "has_eticket": False}
        pattern = re.compile(
            r"set(app|e)ticket\(\s*(\d+)\s*,", re.IGNORECASE
        )
        has_app = has_e = False
        for kind, ticket_app_id in pattern.findall(content):
            if ticket_app_id != app_id:
                continue
            if kind.lower() == "app":
                has_app = True
            else:
                has_e = True
        return {"has_app_ticket": has_app, "has_eticket": has_e}

    def remove_game(
        self,
        app_id: str,
        delete_acf: bool = False,
        hide_only: bool = False,
    ) -> bool:
        """将游戏移出库或彻底删除

        Args:
            app_id: Steam AppID
            delete_acf: 是否同时删除本地 Steam 清单文件 (appmanifest_*.acf) 及清理第三方工具记录
            hide_only: 是否仅加入忽略隐藏黑名单（不删除物理文件）

        Returns:
            是否删除/移除成功
        """
        app_id_str = str(app_id)
        logger.info(f"Removing/hiding game {app_id_str}: delete_acf={delete_acf}, hide_only={hide_only}")

        # 查找目标 GameInfo
        target = None
        for g in self._games:
            if g.app_id == app_id_str:
                target = g
                break

        # 1. 物理文件清理（非仅隐藏模式）
        if not hide_only:
            # A. 删除 OST Lua 配置文件
            if self._lua_dir:
                filepath = os.path.join(self._lua_dir, f"{app_id_str}.lua")
                if os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                        logger.info(f"Deleted Lua file for {app_id_str}: {filepath}")
                    except OSError as e:
                        logger.error(f"Failed to delete Lua file {filepath}: {e}")

            # B. 删除本地 Steam 清单文件 (.acf) 及清理第三方配置
            if delete_acf:
                acf_path = getattr(target, "acf_path", "") if target else ""
                self._scanner.remove_acf(app_id_str, acf_path)
                self._scanner.remove_from_steamtools(app_id_str)

        # 2. 加入持久化忽略黑名单，防止后续刷新重新带出
        self.hide_game(app_id_str)

        # 3. 增量更新内存列表
        self._games = [g for g in self._games if g.app_id != app_id_str]
        logger.debug(f"Game {app_id_str} removed from memory list")
        return True

    def clear_all(self) -> int:
        """清空所有游戏（删除所有 .lua 文件）

        Returns:
            成功删除的游戏数量
        """
        if not self._lua_dir or not os.path.isdir(self._lua_dir):
            logger.warning("Cannot clear games: lua_dir not set or not a directory")
            return 0

        logger.info(f"Clearing all games from: {self._lua_dir}")
        count = 0
        try:
            for fname in os.listdir(self._lua_dir):
                if fname.endswith(".lua"):
                    filepath = os.path.join(self._lua_dir, fname)
                    try:
                        os.remove(filepath)
                        count += 1
                        logger.debug(f"Deleted: {fname}")
                    except OSError as e:
                        logger.error(f"Failed to delete {fname}: {e}")
            self._games.clear()
            logger.info(f"Cleared {count} games")
        except OSError as e:
            logger.error(f"Failed to clear games: {e}")

        return count

    # ---- 内部方法 ----

    def _scan_lua_dir(self) -> list[GameInfo]:
        """扫描 Lua 目录，解析所有 .lua 文件"""
        if not self._lua_dir or not os.path.isdir(self._lua_dir):
            logger.debug(f"Cannot scan: lua_dir is empty or not a directory")
            return []

        logger.debug(f"Scanning Lua directory: {self._lua_dir}")
        games: list[GameInfo] = []
        try:
            lua_files = [f for f in os.listdir(self._lua_dir) if f.endswith(".lua")]
            logger.debug(f"Found {len(lua_files)} .lua files")
            for fname in lua_files:
                path = os.path.join(self._lua_dir, fname)
                info = self._parse_lua_file(path)
                if info:
                    games.append(info)
            logger.debug(f"Parsed {len(games)} games from Lua files")
        except OSError as e:
            logger.error(f"Failed to scan Lua directory: {e}")

        return games

    def _parse_lua_file(self, path: str) -> GameInfo | None:
        """解析单个 Lua 文件提取游戏信息

        解析规则：
        - 文件名作为 AppID（必须为纯数字）
        - 第一行注释提取游戏名称
        - 检测 addtoken / setManifestid / setAppTicket / setStat 调用
        """
        app_id = os.path.splitext(os.path.basename(path))[0]
        if not app_id.isdigit():
            logger.debug(f"Skipping non-numeric file: {os.path.basename(path)}")
            return None

        info = GameInfo(app_id=app_id, lua_path=path)
        logger.debug(f"Parsing Lua file: {os.path.basename(path)}")

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

            # 从第一行有效注释提取名称
            name_match = re.search(
                r'^--\s*(.+?)(?:\s*\(.*?\))?\s*$',
                content, re.MULTILINE,
            )
            if name_match:
                raw_name = name_match.group(1).strip()
                # 排除管理标记
                if "由" not in raw_name:
                    info.name = raw_name
                    logger.debug(f"  Game name: {raw_name}")

            # 检测配置调用（大小写不敏感）
            content_lower = content.lower()
            if re.search(r'\baddtoken\s*\(', content_lower):
                info.has_token = True
                logger.debug(f"  Found: addtoken")
            if re.search(r'\bsetmanifestid\s*\(', content_lower):
                info.has_manifest = True
                logger.debug(f"  Found: setManifestid")
            if re.search(r'\bsetappticket\s*\(', content_lower):
                info.has_appticket = True
                logger.debug(f"  Found: setAppTicket")

            # 诊断清单是否已就绪于 depotcache
            manifest_matches = re.findall(
                r'setmanifestid\s*\(\s*(\d+)\s*,\s*["\']?(\d+)["\']?(?:\s*,\s*[^)]*)?\)',
                content,
                re.IGNORECASE,
            )
            if manifest_matches:
                steam_root = ""
                if self._lua_dir:
                    try:
                        steam_root = os.path.dirname(os.path.dirname(os.path.abspath(self._lua_dir)))
                    except Exception:
                        pass
                depotcache_dir = os.path.join(steam_root, "depotcache") if steam_root else ""
                config_depotcache_dir = os.path.join(steam_root, "config", "depotcache") if steam_root else ""

                missing = []
                for did, gid in manifest_matches:
                    fn = f"{did}_{gid}.manifest"
                    p1 = os.path.join(depotcache_dir, fn) if depotcache_dir else ""
                    p2 = os.path.join(config_depotcache_dir, fn) if config_depotcache_dir else ""
                    has_file = (
                        (p1 and os.path.isfile(p1) and os.path.getsize(p1) > 0)
                        or (p2 and os.path.isfile(p2) and os.path.getsize(p2) > 0)
                    )
                    if not has_file:
                        missing.append(f"{did}_{gid}")

                info.depots = [(did, gid) for did, gid in manifest_matches]
                info.missing_manifests = missing
                info.manifest_ready = (len(missing) == 0)
            else:
                info.manifest_ready = True
                info.missing_manifests = []

        except (OSError, UnicodeDecodeError) as e:
            logger.error(f"Failed to parse Lua file {path}: {e}")
            return None

        logger.debug(f"Parsed game {app_id}: name={info.name}, token={info.has_token}, manifest={info.has_manifest}")
        return info

    @staticmethod
    def _build_lua_content(metadata: GameMetadata) -> str:
        """构建完整的 Lua 文件内容

        格式遵循 OpenSteamTool README（行 52-61）+ 参考项目验证：

        ```lua
        -- 游戏名 (由 OpenSteamToolDesktop 管理)
        addappid(730, 0, "app_level_key")              -- 主应用 + 应用级密钥（可选）
        addappid(731, 0, "depot_specific_key")          -- Depot 专属密钥
        addappid(732)                                    -- Depot 无密钥
        addtoken(730, "accessToken")                     -- Access Token
        addappid(12345)                                  -- DLC
        addappid(12346, 0, "dlc_depot_key")             -- DLC Depot 密钥
        ```

        1. app_level_key 打在主游戏行 addappid(app_id, 0, key)，NOT 作为 depot 回退
        2. depot 专属密钥只打在有对应密钥的 depot 行，无密钥的 depot 只写 addappid(depot_id)
        3. 对元数据中有 manifest_gid 的 depot 写入 setManifestid，固定清单版本
        """
        logger.debug(f"Building Lua content for {metadata.app_id} ({metadata.name or 'unknown'})")
        lines: list[str] = []

        # ── 注释行 ──
        if metadata.name:
            lines.append(f"-- {metadata.name} (由 OpenSteamToolDesktop 管理)")
        else:
            lines.append(f"-- AppID: {metadata.app_id} (由 OpenSteamToolDesktop 管理)")

        # ── 主游戏入库 ──
        # OpenSteamTool README 行 52: addappid(appid) 无密钥形式
        # OpenSteamTool README 行 54: addappid(depot_id, 0, "key") 带密钥形式
        if metadata.app_level_key:
            lines.append(f'addappid({metadata.app_id}, 0, "{metadata.app_level_key}")')
            logger.debug(f"  Main app: addappid({metadata.app_id}, 0, \"...\") [with app-level key]")
        else:
            lines.append(f"addappid({metadata.app_id})")
            logger.debug(f"  Main app: addappid({metadata.app_id}) [no key]")

        # ── Depot 密钥 ──
        # 关键修复：不再将 app_level_key 作为 depot 回退密钥！
        # 每个 depot 有自己唯一的加密密钥，必须从 Sudama 按 depot_id 单独查找
        main_depot_with_key = 0
        main_depot_without_key = 0
        for depot in metadata.depots:
            if depot.depot_key:
                # 有专属 depot 密钥 → 带密钥注册
                lines.append(f'addappid({depot.depot_id}, 0, "{depot.depot_key}")')
                main_depot_with_key += 1
            else:
                # 无密钥 → 仅注册 depot（DLL 可能通过其他方式获取密钥）
                lines.append(f"addappid({depot.depot_id})")
                main_depot_without_key += 1
        logger.debug(f"  Main depots: {main_depot_with_key} with keys, {main_depot_without_key} without keys")

        # OpenSteamTool 支持绑定 depot 的 manifest，避免 Steam 更新到不可用的
        # 分支或在运行时重新解析到错误的清单。
        manifest_count = 0
        for depot in metadata.depots:
            if depot.manifest_gid:
                if depot.size > 0:
                    lines.append(
                        f"setManifestid({depot.depot_id}, "
                        f"\"{depot.manifest_gid}\", {depot.size})"
                    )
                else:
                    lines.append(
                        f"setManifestid({depot.depot_id}, \"{depot.manifest_gid}\")"
                    )
                manifest_count += 1
        if manifest_count:
            logger.debug(f"  Bound {manifest_count} depot manifest(s)")

        # ── Access Token ──
        if metadata.access_token:
            lines.append(f'addtoken({metadata.app_id}, "{metadata.access_token}")')

        # ── DLC 入库 ──
        dlc_count = 0
        for dlc_id in sorted(set(metadata.dlc_ids), key=int):
            if dlc_id == metadata.app_id:
                continue
            lines.append(f"addappid({dlc_id})")
            dlc_count += 1
        if dlc_count > 0:
            logger.debug(f"  Added {dlc_count} DLC(s)")

        # ── DLC Depot 密钥 ──
        dlc_depot_with_key = 0
        for dlc_appid, depot in metadata.dlc_depots:
            if depot.depot_key:
                lines.append(f'addappid({depot.depot_id}, 0, "{depot.depot_key}")')
                dlc_depot_with_key += 1
            # 无密钥的 DLC depot 不单独注册（已通过 DLC appid 行注册）
        if dlc_depot_with_key > 0:
            logger.debug(f"  Added {dlc_depot_with_key} DLC depot key(s)")

        dlc_manifest_count = 0
        for _dlc_appid, depot in metadata.dlc_depots:
            if depot.manifest_gid:
                if depot.size > 0:
                    lines.append(
                        f"setManifestid({depot.depot_id}, "
                        f"\"{depot.manifest_gid}\", {depot.size})"
                    )
                else:
                    lines.append(
                        f"setManifestid({depot.depot_id}, \"{depot.manifest_gid}\")"
                    )
                dlc_manifest_count += 1
        if dlc_manifest_count:
            logger.debug(f"  Bound {dlc_manifest_count} DLC depot manifest(s)")

        # ── D 加密票据（setAppTicket / setETicket）──
        # 必须放在 addappid 之后：OpenSteamTool 只对已 addappid 的 appid 应用票据
        ticket_lines = _build_ticket_lines(metadata)
        if ticket_lines:
            lines.extend(ticket_lines)
            logger.debug(f"  Added {len(ticket_lines)} D-encryption ticket line(s)")

        lines.append("")
        logger.debug(f"Lua content built: {len(lines)} lines")
        return "\n".join(lines)

    
