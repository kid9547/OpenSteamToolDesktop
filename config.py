"""
OpenSteamToolDesktop 全局配置常量模块
======================================

合并自 config.py 和 constants.py，统一管理应用配置、URL、颜色、HTTP 等所有常量。
"""
from __future__ import annotations

import os
from pathlib import Path


# ============ 应用信息 ============
APP_NAME: str = "OpenSteamToolDesktop"
APP_VERSION: str = "1.1.0"

# ============ 功能模块开关 ============
# 科学加速页面开关（默认关闭；可通过环境变量 ENABLE_ACCELERATOR=1 或构建参数 --with-accelerator 开启）
ENABLE_ACCELERATOR_PAGE: bool = os.getenv("ENABLE_ACCELERATOR", "0").lower() in ("1", "true", "yes")

# ============ 配置路径 ============
CONFIG_DIR: str = str(Path.home() / f".{APP_NAME}")
os.makedirs(CONFIG_DIR, exist_ok=True)
CONFIG_FILE: str = os.path.join(CONFIG_DIR, "config.json")

# ============ GitHub 仓库 ============
GITHUB_REPO_OWNER: str = "kid9547"
GITHUB_REPO_NAME: str = "OpenSteamToolDesktop"
GITHUB_REPO_URL: str = f"https://github.com/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}"
GITHUB_RELEASES_URL: str = f"{GITHUB_REPO_URL}/releases"
GITHUB_ISSUES_URL: str = f"{GITHUB_REPO_URL}/issues/new"
GITHUB_API_LATEST_RELEASE: str = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/releases/latest"
GITHUB_API_LATEST: str = GITHUB_API_LATEST_RELEASE  # 别名

# ============ OpenSteamTool 仓库（DLL 发布位置）============
OPENSTEAMTOOL_REPO_URL: str = "https://github.com/OpenSteam001/OpenSteamTool"
OPENSTEAMTOOL_RELEASES_URL: str = "https://github.com/OpenSteam001/OpenSteamTool/releases"

# ============ Steam 下载 ============
STEAM_DOWNLOAD_URL: str = "https://store.steampowered.com/about/"

# ============ CDN & API URLs ============
STEAM_STORE_API: str = "https://store.steampowered.com/api/appdetails"
STEAM_STORE_SEARCH_API: str = "https://store.steampowered.com/api/storesearch"
STEAM_STORE_SEARCH_RESULTS: str = "https://store.steampowered.com/search/results/"
STEAM_CDN_BASE: str = "https://cdn.akamai.steamstatic.com/steam/apps"
STEAM_CDN_API: str = (
    "https://api.steampowered.com/IContentServerDirectoryService/"
    "GetServersForSteamPipe/v1/?cell_id=33&max_servers=30"
)
STEAM_MONITOR_PATTERN_RAW: str = (
    "https://raw.githubusercontent.com/OpenSteam001/steam-monitor/"
    "pattern/{component}/{sha256}.toml"
)
STEAM_MONITOR_PATTERN_CDN: str = (
    "https://cdn.jsdelivr.net/gh/OpenSteam001/steam-monitor@pattern/"
    "{component}/{sha256}.toml"
)
STEAM_MONITOR_IPC_RAW: str = (
    "https://raw.githubusercontent.com/OpenSteam001/steam-monitor/ipc/"
    "{component}/{sha256}.toml"
)
STEAM_MONITOR_IPC_CDN: str = (
    "https://cdn.jsdelivr.net/gh/OpenSteam001/steam-monitor@ipc/"
    "{component}/{sha256}.toml"
)
STEAMCMD_API: str = "https://api.steamcmd.net/v1/info"
SUDAMA_API_DEPOT_KEYS: str = "https://api.sudama.app/v1/depotkeys"
TOKEN_API: str = "https://api.993499094.xyz/appaccesstokens.json"
DEPOT_KEYS_API_ALT: str = "https://api.993499094.xyz/depotkeys.json"

# ============ Manifest 清单仓库与镜像源 ============
# 说明（2026-10 实测）：
#   * ManifestAutoUpdate 生态采用「一个 AppID 一个 git 分支」的归档布局，
#     分支内同时包含 config.json / Key.vdf / appinfo.vdf 与真实的
#     {depot}_{gid}.manifest，因此归档内 GID 与清单文件 100% 自洽。
#   * P-ToyStore/SteamManifestCache_Pro 已不存在（GitHub API 返回 404），
#     不要再把它当作清单来源。
MANIFEST_ARCHIVE_REPOS: list[str] = [
    "Auiowu/ManifestAutoUpdate",
    "tymolu233/ManifestAutoUpdate",
    "sean-who/ManifestAutoUpdate",
]

# 兼容旧代码的别名（仅保留仍然有效的仓库）
MANIFEST_GITHUB_REPOS: list[str] = list(MANIFEST_ARCHIVE_REPOS)

# GitHub Raw 加速镜像（国内或无代理网络环境回退）——按实测可用性排序
GITHUB_RAW_MIRRORS: list[str] = [
    "https://raw.githubusercontent.com",
    "https://ghfast.top/https://raw.githubusercontent.com",
    "https://ghproxy.net/https://raw.githubusercontent.com",
    "https://cdn.jsdelivr.net/gh",
]

# 分支归档整包下载镜像前缀（{url} 为 https://github.com/<repo>/archive/refs/heads/<appid>.zip）
GITHUB_ARCHIVE_MIRRORS: list[str] = [
    "",
    "https://ghfast.top/",
    "https://ghproxy.net/",
]

# 单文件分支归档超时（秒）
MANIFEST_ARCHIVE_TIMEOUT: float = 30.0

# Manifest Request Code（内容码）查询接口，{gid} 为清单 GID
MANIFEST_REQUEST_CODE_APIS: list[str] = [
    "http://gmrc.wudrm.com/manifest/{gid}",
    "https://manifest.steam.run/api/manifest/{gid}",
]

# ManifestHub API（SteamAutoCracks 生态）——需要 API Key，无 Key 时返回 403
MANIFESTHUB_API_URL: str = "https://api.manifesthub2.filegear-sg.me/manifest"
MANIFESTHUB_API_KEY: str = ""

# ============ D 加密（Denuvo）票据 ============
# 在线「现签」票据服务地址（可选，默认关闭 = 绝不联网）。
#
# 契约与开源分支 BetterSteamTools 的 EticketClient 完全一致：
#   POST <url>  Content-Type: application/json
#   {"app_id":"<id>","nonce":"<hex>","existing_steam_id":"<decimal>"}
#   200 → {"eticket":"<hex>","appticket":"<hex>","steam_id":"<decimal>"}
#   409 → {"foreign_account":true} 或「该 appid 无账号池归属」
#
# 留空时本项目不发起任何网络请求，行为与官方 stock OpenSteamTool 一致。
# 说明：AppTicket/ETicket 由 Valve 私钥签名，本地无法离线伪造；在线现签依赖
# 服务端持有的正版账号池，属运营资源而非可移植代码。
# 详见 DENUVO_REVERSE_ENGINEERING_REPORT.md
TICKET_MINT_URL: str = os.getenv("OST_TICKET_MINT_URL", "")

# 票据有效期提示（分钟）。Valve 会话票据通常 30 分钟 ~ 数小时，过期报 88500005。
TICKET_VALIDITY_HINT_MINUTES: int = 30

# 本机票据提取工具路径（相对项目根目录）
TICKET_EXTRACTOR_RELATIVE: str = "tools/extract_tickets/extract_tickets.exe"

# ============ 主题 ============
DEFAULT_THEME_MODE: str = "dark"
DEFAULT_THEME_COLOR: str = "#0078d4"
COLOR_PRIMARY: str = DEFAULT_THEME_COLOR  # 别名

# ============ 语言 ============
DEFAULT_LANGUAGE: str = "zh_CN"

# ============ 日志 ============
LOG_LEVEL: str = "INFO"
CRASH_LOG_ENABLED: bool = False

# ============ HTTP 配置 ============
HTTP_DEFAULT_TIMEOUT: float = 15.0
HTTP_COVER_TIMEOUT: float = 5.0
HTTP_MAX_RETRIES: int = 2
SSL_VERIFY: bool = False  # Windows 兼容性：禁用 SSL 证书验证

STEAM_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/91.0.4472.124 Safari/537.36"
)

# ============ Lua 配置 ============
LUA_DIR_RELATIVE: str = "config/lua"

# ============ 颜色常量 ============
COLOR_SUCCESS: str = "#52c41a"
COLOR_ERROR: str = "#f5222d"
COLOR_WARNING: str = "#ff9800"

# ============ 文字颜色 ============
TEXT_COLOR: str = "#FFFFFF"  # 所有文字统一白色

# ============ Steam CDN 备用列表 ============
# 说明（2026-10 实测）：
#   * ``steampipe.akamaized.net`` 是 Valve 官方 Akamai 入口，无需 Steam API 即可直连；
#   * 形如 ``cache1-steamcontent.com`` 的无区域后缀主机已无法解析（ConnectError），
#     真实主机均为 ``cache<N>-<区域>.steamcontent.com`` 形态；
#   * 正常路径优先使用 Steam API 动态下发的主机列表，此表仅作 API 失败时的兜底。
FALLBACK_CDN_HOSTS: list[str] = [
    "steampipe.akamaized.net",
    "cache1-hkg1.steamcontent.com",
    "cache2-hkg1.steamcontent.com",
    "cache3-hkg1.steamcontent.com",
    "cache4-hkg1.steamcontent.com",
    "cache1-lax1.steamcontent.com",
    "cache2-lax1.steamcontent.com",
]
