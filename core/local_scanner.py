"""
本地磁盘与外部游戏库扫描器
===========================

功能：
1. 扫描 Windows 全盘 Steam 库（基于 libraryfolders.vdf 与各盘根目录自动发现）
2. 解析各库目录中的 appmanifest_*.acf 文件，识别已安装/第三方入库游戏
3. 扫描兼容第三方工具配置（如 SteamTools st.json、GreenLuma 等）
4. 扫描非标准命名或多游戏集合的 Lua 文件
5. 提供将外部/本地安装游戏「一键接管为 OST Lua 配置」的能力
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from utils.logger import setup_logger

logger = setup_logger(__name__)

# Steam 官方通用运行时工具 / 环境组件 AppID（默认在已入库游戏列表中过滤，避免杂乱）
IGNORED_APP_IDS: set[str] = {
    "228980",  # Steamworks Common Redistributables
    "250820",  # SteamVR
    "1391110", # Steam Linux Runtime
    "1070560", # Steam Linux Runtime - Soldier
    "1628350", # Steam Linux Runtime - Sniper
}


def parse_vdf(text: str) -> dict[str, Any]:
    """解析 Steam VDF / KeyValues 格式文本为 Python 字典

    支持嵌套节点、字符串键值、注释跳过。
    """
    if not text:
        return {}

    tokens = re.findall(r'"([^"]*)"|([{}]|//[^\n]*)', text)
    root: dict[str, Any] = {}
    stack: list[dict[str, Any]] = [root]
    current_key: str | None = None

    for token, sym in tokens:
        if sym == '{':
            new_dict: dict[str, Any] = {}
            if current_key is not None:
                stack[-1][current_key] = new_dict
                current_key = None
            stack.append(new_dict)
        elif sym == '}':
            if len(stack) > 1:
                stack.pop()
        elif sym and sym.startswith('//'):
            continue
        else:
            val = token
            if current_key is None:
                current_key = val
            else:
                stack[-1][current_key] = val
                current_key = None

    return root


class LocalGameScanner:
    """本地磁盘与多工具入库游戏扫描服务"""

    def __init__(self, steam_root: str = "", lua_dir: str = ""):
        self._steam_root = steam_root
        self._lua_dir = lua_dir or (os.path.join(steam_root, "config", "lua") if steam_root else "")

    def set_paths(self, steam_root: str, lua_dir: str = "") -> None:
        self._steam_root = steam_root
        self._lua_dir = lua_dir or (os.path.join(steam_root, "config", "lua") if steam_root else "")

    # ── 1. Steam 库路径全盘发现 ────────────────────────────────────

    def find_steam_libraries(self, scan_all_drives: bool = False) -> list[str]:
        """发现 Steam 游戏库文件夹（优先检测已配置 Steam 目录，可选全盘驱动器扫描）"""
        libraries: set[str] = set()

        # A. 从当前检测到的 Steam 根目录出发
        if self._steam_root and os.path.isdir(self._steam_root):
            libraries.add(os.path.normpath(self._steam_root))

            # 读取 steamapps/libraryfolders.vdf
            for vdf_sub in [
                os.path.join("steamapps", "libraryfolders.vdf"),
                os.path.join("config", "libraryfolders.vdf"),
            ]:
                vdf_file = os.path.join(self._steam_root, vdf_sub)
                if os.path.isfile(vdf_file):
                    try:
                        with open(vdf_file, "r", encoding="utf-8", errors="replace") as f:
                            parsed = parse_vdf(f.read())
                        lf = parsed.get("libraryfolders", {})
                        if isinstance(lf, dict):
                            for _, val in lf.items():
                                if isinstance(val, dict) and "path" in val:
                                    p = os.path.normpath(val["path"].strip())
                                    if os.path.isdir(p):
                                        libraries.add(p)
                    except Exception as e:
                        logger.debug(f"Error reading {vdf_file}: {e}")

        # B. 全盘驱动器扫描（仅当 scan_all_drives 为 True 时执行）
        if scan_all_drives:
            common_subs = [
                "SteamLibrary",
                "Steam",
                os.path.join("Program Files (x86)", "Steam"),
                os.path.join("Program Files", "Steam"),
                os.path.join("Games", "SteamLibrary"),
                os.path.join("Games", "Steam"),
            ]
            for drive_letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                drive_root = f"{drive_letter}:\\"
                if not os.path.exists(drive_root):
                    continue
                for sub in common_subs:
                    candidate = os.path.normpath(os.path.join(drive_root, sub))
                    if os.path.isdir(candidate):
                        sa = os.path.join(candidate, "steamapps")
                        if os.path.isdir(sa):
                            libraries.add(candidate)

        result = sorted(list(libraries))
        logger.debug(f"Discovered {len(result)} Steam library folders: {result}")
        return result

    # ── 2. 扫描已安装与 appmanifest 游戏 ───────────────────────────

    def scan_installed_games(
        self,
        scan_all_drives: bool = False,
        hidden_app_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """扫描所有发现的 Steam 库目录中的 appmanifest_*.acf 文件"""
        libs = self.find_steam_libraries(scan_all_drives=scan_all_drives)
        games: list[dict[str, Any]] = []
        seen_appids: set[str] = set()
        hidden_ids = hidden_app_ids or set()

        for lib_path in libs:
            steamapps_dir = os.path.join(lib_path, "steamapps")
            if not os.path.isdir(steamapps_dir):
                continue

            try:
                for fname in os.listdir(steamapps_dir):
                    if not (fname.startswith("appmanifest_") and fname.endswith(".acf")):
                        continue

                    # 提取 AppID
                    m = re.match(r"^appmanifest_(\d+)\.acf$", fname, re.IGNORECASE)
                    if not m:
                        continue
                    app_id = m.group(1)

                    if app_id in IGNORED_APP_IDS or app_id in seen_appids or app_id in hidden_ids:
                        continue

                    acf_path = os.path.join(steamapps_dir, fname)
                    game_info = self._parse_appmanifest(acf_path, lib_path)
                    if game_info:
                        games.append(game_info)
                        seen_appids.add(app_id)
            except OSError as e:
                logger.debug(f"Error scanning {steamapps_dir}: {e}")

        logger.info(f"Scanned {len(games)} installed games from {len(libs)} libraries")
        return games

    def _parse_appmanifest(self, acf_path: str, lib_path: str) -> dict[str, Any] | None:
        """解析单份 appmanifest_*.acf 文件"""
        try:
            with open(acf_path, "r", encoding="utf-8", errors="replace") as f:
                parsed = parse_vdf(f.read())
            app_state = parsed.get("AppState", {})
            if not isinstance(app_state, dict):
                return None

            app_id = str(app_state.get("appid", "")).strip()
            if not app_id:
                return None

            name = str(app_state.get("name", "")).strip()
            install_dir_name = str(app_state.get("installdir", "")).strip()
            full_install_dir = os.path.join(lib_path, "steamapps", "common", install_dir_name) if install_dir_name else ""

            size_on_disk = 0
            try:
                size_on_disk = int(app_state.get("SizeOnDisk", 0))
            except (ValueError, TypeError):
                pass

            # 解析已安装 Depot 清单
            installed_depots: list[tuple[str, str]] = []
            depots_dict = app_state.get("InstalledDepots", {})
            if isinstance(depots_dict, dict):
                for did, info in depots_dict.items():
                    if isinstance(info, dict):
                        manifest = str(info.get("manifest", "")).strip()
                        if manifest and manifest != "0":
                            installed_depots.append((str(did), manifest))

            # 诊断清单在本地 depotcache 是否存在
            depotcache_dir = os.path.join(self._steam_root, "depotcache") if self._steam_root else ""
            config_depotcache_dir = os.path.join(self._steam_root, "config", "depotcache") if self._steam_root else ""

            missing_manifests: list[str] = []
            for did, gid in installed_depots:
                fn = f"{did}_{gid}.manifest"
                has_file = False
                if depotcache_dir and os.path.isfile(os.path.join(depotcache_dir, fn)):
                    has_file = True
                elif config_depotcache_dir and os.path.isfile(os.path.join(config_depotcache_dir, fn)):
                    has_file = True
                if not has_file:
                    missing_manifests.append(f"{did}_{gid}")

            return {
                "app_id": app_id,
                "name": name,
                "install_dir": full_install_dir,
                "size_on_disk": size_on_disk,
                "depots": installed_depots,
                "is_installed": os.path.isdir(full_install_dir) if full_install_dir else True,
                "manifest_ready": len(missing_manifests) == 0,
                "missing_manifests": missing_manifests,
                "source": "steam_local",
                "acf_path": acf_path,
            }
        except Exception as e:
            logger.debug(f"Failed to parse {acf_path}: {e}")
            return None

    # ── 3. 扫描兼容第三方工具配置 (SteamTools st.json 等) ────────────

    def scan_steamtools_games(self, hidden_app_ids: set[str] | None = None) -> list[dict[str, Any]]:
        """扫描 SteamTools 的 st.json 配置文件"""
        if not self._steam_root:
            return []

        st_candidates = [
            os.path.join(self._steam_root, "config", "st.json"),
            os.path.join(self._steam_root, "st.json"),
        ]

        hidden_ids = hidden_app_ids or set()
        found_games: list[dict[str, Any]] = []
        for candidate in st_candidates:
            if not os.path.isfile(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8", errors="replace") as f:
                    data = json.load(f)

                apps_dict = data.get("apps", data) if isinstance(data, dict) else {}
                if isinstance(apps_dict, dict):
                    for app_id, details in apps_dict.items():
                        app_id_str = str(app_id)
                        if not app_id_str.isdigit():
                            continue
                        if app_id_str in IGNORED_APP_IDS or app_id_str in hidden_ids:
                            continue
                        name = ""
                        depots: list[tuple[str, str]] = []
                        if isinstance(details, dict):
                            name = details.get("name", "")
                            # depots
                            d_info = details.get("depots", {})
                            if isinstance(d_info, dict):
                                for did, ddata in d_info.items():
                                    gid = ""
                                    if isinstance(ddata, dict):
                                        gid = str(ddata.get("manifest", ""))
                                    elif isinstance(ddata, str):
                                        gid = ddata
                                    if gid:
                                        depots.append((str(did), gid))

                        found_games.append({
                            "app_id": app_id_str,
                            "name": name,
                            "install_dir": "",
                            "size_on_disk": 0,
                            "depots": depots,
                            "is_installed": False,
                            "manifest_ready": True,
                            "missing_manifests": [],
                            "source": "steamtools",
                        })
            except Exception as e:
                logger.debug(f"Error scanning SteamTools file {candidate}: {e}")

        return found_games

    def remove_acf(self, app_id: str, acf_path: str = "") -> bool:
        """从磁盘彻底删除指定游戏的 appmanifest_*.acf 清单文件"""
        target_path = acf_path
        if not target_path or not os.path.isfile(target_path):
            for lib in self.find_steam_libraries(scan_all_drives=False):
                candidate = os.path.join(lib, "steamapps", f"appmanifest_{app_id}.acf")
                if os.path.isfile(candidate):
                    target_path = candidate
                    break

        if target_path and os.path.isfile(target_path):
            try:
                os.remove(target_path)
                logger.info(f"Successfully deleted ACF file: {target_path}")
                return True
            except OSError as e:
                logger.error(f"Failed to delete ACF file {target_path}: {e}")
                return False
        return False

    def remove_from_steamtools(self, app_id: str) -> bool:
        """从 SteamTools 的 st.json 配置文件中移除指定游戏条目"""
        if not self._steam_root:
            return False

        st_candidates = [
            os.path.join(self._steam_root, "config", "st.json"),
            os.path.join(self._steam_root, "st.json"),
        ]
        modified = False
        app_id_str = str(app_id)
        for candidate in st_candidates:
            if not os.path.isfile(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8", errors="replace") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    if "apps" in data and isinstance(data["apps"], dict) and app_id_str in data["apps"]:
                        del data["apps"][app_id_str]
                        modified = True
                    elif app_id_str in data:
                        del data[app_id_str]
                        modified = True
                if modified:
                    with open(candidate, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    logger.info(f"Removed app {app_id_str} from {candidate}")
            except Exception as e:
                logger.warning(f"Failed to remove app {app_id_str} from {candidate}: {e}")
        return modified

    # ── 4. 扫描非数字命名的外部 Lua 文件 ───────────────────────────

    def scan_external_lua_files(self, hidden_app_ids: set[str] | None = None) -> list[dict[str, Any]]:
        """扫描 Lua 目录中非纯数字命名（如 Palworld.lua、SteamTools.lua 等）或多游戏合集的外部文件"""
        if not self._lua_dir or not os.path.isdir(self._lua_dir):
            return []

        results: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        hidden_ids = hidden_app_ids or set()

        try:
            for fname in os.listdir(self._lua_dir):
                if not fname.endswith(".lua"):
                    continue

                stem = os.path.splitext(fname)[0]
                # 纯数字命名的留给主管理器解析，避免重复
                if stem.isdigit():
                    continue

                filepath = os.path.join(self._lua_dir, fname)
                try:
                    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()

                    # 提取所有 addappid 调用
                    app_matches = re.findall(r'addappid\s*\(\s*(\d+)', content, re.IGNORECASE)
                    if not app_matches:
                        continue

                    # 猜测主游戏 AppID（取第一个不在已处理集合中的数字）
                    main_app_id = app_matches[0]
                    if main_app_id in seen_ids or main_app_id in IGNORED_APP_IDS or main_app_id in hidden_ids:
                        continue
                    seen_ids.add(main_app_id)

                    # 提取注释中的名称
                    name = ""
                    name_m = re.search(r'^--\s*(.+?)(?:\s*\(.*?\))?\s*$', content, re.MULTILINE)
                    if name_m and "由" not in name_m.group(1):
                        name = name_m.group(1).strip()
                    if not name:
                        # 用文件名去标点作为回退名
                        name = re.sub(r'[_\-]+', ' ', stem).strip()

                    # 提取清单匹配
                    mf_matches = re.findall(
                        r'setmanifestid\s*\(\s*(\d+)\s*,\s*["\']?(\d+)["\']?',
                        content,
                        re.IGNORECASE,
                    )
                    depots = [(d, g) for d, g in mf_matches]

                    results.append({
                        "app_id": main_app_id,
                        "name": name,
                        "lua_path": filepath,
                        "depots": depots,
                        "source": "external_lua",
                        "is_installed": False,
                        "manifest_ready": True,
                        "missing_manifests": [],
                    })
                except Exception as e:
                    logger.debug(f"Failed parsing external lua {filepath}: {e}")
        except OSError:
            pass

        return results

    # ── 5. 一键接管外部游戏为 OpenSteamTool 标准 Lua ────────────────

    def take_over_game_as_ost_lua(self, app_id: str, name: str = "", depots: list[tuple[str, str]] | None = None) -> bool:
        """为通过本地盘扫描发现的外部入库/已安装游戏一键生成标准 OST Lua 配置文件

        使其完全纳入 OpenSteamToolDesktop 的统一生命周期管理。
        """
        if not self._lua_dir:
            return False

        try:
            os.makedirs(self._lua_dir, exist_ok=True)
            lua_path = os.path.join(self._lua_dir, f"{app_id}.lua")

            lines = []
            display_name = name or f"AppID {app_id}"
            lines.append(f"-- {display_name} (由 OpenSteamToolDesktop 管理)")
            lines.append(f"addappid({app_id})")

            # 写入已知的 depot 与 manifest 绑定
            if depots:
                for item in depots:
                    if len(item) >= 2 and item[0] and item[1]:
                        did, gid = str(item[0]), str(item[1])
                        lines.append(f'setManifestid({did}, "{gid}")')

            lines.append("")

            with open(lua_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))

            logger.info(f"Successfully took over game {app_id} into {lua_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to take over game {app_id}: {e}")
            return False
