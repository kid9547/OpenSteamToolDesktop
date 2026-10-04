"""
Manifest 下载器 — 从 Steam CDN 下载 Depot Manifest 文件到 depotcache

项目的实现：
1. 从 Steam 官方 API 获取 CDN 服务器列表
2. 下载 manifest ZIP 文件并解压提取 payload
3. 存储到 {SteamPath}/depotcache/{DepotID}_{ManifestID}.manifest
4. 自动清理同一 DepotID 的旧版本 manifest

这是解决"游戏启动提示文件缺失"问题的关键模块。
"""
from __future__ import annotations

import io
import logging
import os
import re
import threading
import time
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional, Any

import httpx

from utils.logger import setup_logger

from config import (
    STEAM_CDN_API, SSL_VERIFY,
    FALLBACK_CDN_HOSTS, MANIFEST_ARCHIVE_REPOS, MANIFEST_REQUEST_CODE_APIS,
    MANIFESTHUB_API_URL, MANIFESTHUB_API_KEY,
)
from utils.http_client import get_system_proxy

logger = setup_logger(__name__)

# Steam Depot Manifest 二进制魔数 (0x71F617D0)
STEAM_MANIFEST_MAGIC = b"\xd0\x17\xf6\x71"

# ManifestHub 上游 README 动态获取镜像源
MANIFESTHUB_README_URL = "https://raw.githubusercontent.com/SteamAutoCracks/ManifestHub/refs/heads/main/README.md"
MANIFESTHUB_README_MIRRORS = [
    "https://ghfast.top/https://raw.githubusercontent.com/SteamAutoCracks/ManifestHub/refs/heads/main/README.md",
    "https://ghproxy.net/https://raw.githubusercontent.com/SteamAutoCracks/ManifestHub/refs/heads/main/README.md",
    "https://raw.dgithub.xyz/SteamAutoCracks/ManifestHub/refs/heads/main/README.md",
    "https://raw.githubusercontent.com/SteamAutoCracks/ManifestHub/refs/heads/main/README.md",
]


def fetch_manifesthub_upstream_info(timeout: float = 6.0) -> dict[str, str]:
    """从上游官方 GitHub README 通过国内加速镜像动态获取最新的 ManifestHub API 接口与 Web 网站地址

    Returns:
        {
            "api_url": "https://api.manifesthub2.filegear-sg.me/manifest",
            "web_url": "https://manifesthub2.filegear-sg.me",
            "update_time": "2025-07-24",
            "note": "免费API密钥有效期为24小时",
            "mirror_used": "...",
        }
    """
    default_info = {
        "api_url": MANIFESTHUB_API_URL,
        "web_url": "https://manifesthub2.filegear-sg.me",
        "update_time": "",
        "note": "免费API密钥有效期为24小时",
        "mirror_used": "default",
    }

    proxy = get_system_proxy()
    for mirror_url in MANIFESTHUB_README_MIRRORS:
        try:
            with httpx.Client(proxy=proxy, timeout=timeout, verify=SSL_VERIFY, follow_redirects=True) as client:
                resp = client.get(mirror_url)
                if resp.status_code == 200 and resp.text:
                    text = resp.text
                    api_m = re.search(r'(https?://[a-zA-Z0-9.\-]+/manifest)', text)
                    web_m = re.search(r'(https?://manifesthub[a-zA-Z0-9.\-]+)', text)
                    time_m = re.search(r'Update time:\s*`([^`]+)`', text)
                    note_m = re.search(r'免费API密钥有效期为[^\r\n]+', text)

                    api_url = api_m.group(1) if api_m else default_info["api_url"]
                    web_url = web_m.group(1) if web_m else default_info["web_url"]
                    update_time = time_m.group(1) if time_m else ""
                    note = note_m.group(0) if note_m else default_info["note"]

                    logger.info(f"Dynamically parsed ManifestHub upstream info from {mirror_url}: api={api_url}, web={web_url}")
                    return {
                        "api_url": api_url,
                        "web_url": web_url,
                        "update_time": update_time,
                        "note": note,
                        "mirror_used": mirror_url,
                    }
        except Exception as e:
            logger.debug(f"Failed to fetch ManifestHub README from {mirror_url}: {e}")

    return default_info


# 常用 Steam CDN 备用列表（API 不可用时使用）——统一收敛到 config.FALLBACK_CDN_HOSTS
_FALLBACK_CDN_HOSTS = FALLBACK_CDN_HOSTS


@dataclass
class ManifestDownloadResult:
    """单个 Manifest 的下载结果"""
    depot_id: str
    manifest_gid: str
    success: bool
    message: str = ""
    file_path: str = ""


@dataclass
class ManifestBatchResult:
    """批量下载结果"""
    total: int = 0
    success: int = 0
    skipped: int = 0
    failed: int = 0
    results: list[ManifestDownloadResult] = field(default_factory=list)


class ManifestDownloader:
    """Depot Manifest 下载器

    用法：
        downloader = ManifestDownloader(steam_path)
        result = downloader.download_manifests(depots, app_id)
        print(f"Downloaded {result.success}/{result.total}")
    """

    _CDN_CACHE: list[str] = []          # CDN 主机缓存
    _CDN_CACHE_TIME: float = 0
    _CDN_CACHE_TTL: float = 1800.0      # 30 分钟

    def __init__(self, steam_path: str, max_workers: int = 4):
        """初始化下载器

        Args:
            steam_path: Steam 安装根目录
            max_workers: 并发下载线程数
        """
        self._steam_path = steam_path
        self._depotcache_dir = os.path.join(steam_path, "depotcache") if steam_path else ""
        self._config_depotcache_dir = os.path.join(steam_path, "config", "depotcache") if steam_path else ""
        self._max_workers = max_workers

        # 主 HTTP 客户端（优先使用系统代理）
        self._http = httpx.Client(
            proxy=get_system_proxy(),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            },
            timeout=25.0,
            follow_redirects=True,
            verify=SSL_VERIFY,
        )

        # 直连 HTTP 客户端（用于无需/绕过代理的公共镜像）
        self._direct_http = httpx.Client(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            },
            timeout=20.0,
            follow_redirects=True,
            verify=SSL_VERIFY,
        )

        logger.debug(f"ManifestDownloader initialized: steam_path={steam_path}")

    def close(self):
        """释放 HTTP 资源"""
        try:
            self._http.close()
        except Exception:
            pass
        try:
            self._direct_http.close()
        except Exception:
            pass

    # ── 公开接口 ──────────────────────────────────────────

    def download_manifests(
        self,
        depots: list[Any],  # [(depot_id, manifest_gid, size, [owner_app_id]), ...]
        app_id: str = "",
    ) -> ManifestBatchResult:
        """批量下载 Depot Manifest 文件

        Args:
            depots: [(depot_id, manifest_gid, size, [owner_app_id]), ...] 列表
            app_id: 游戏 AppID（用于日志与分支检索）

        Returns:
            ManifestBatchResult: 批量下载结果
        """
        if not self._depotcache_dir:
            logger.error("Cannot download manifests: depotcache directory not set")
            return ManifestBatchResult()

        # 确保 depotcache 目录与 config/depotcache 均存在
        os.makedirs(self._depotcache_dir, exist_ok=True)
        if self._config_depotcache_dir:
            os.makedirs(self._config_depotcache_dir, exist_ok=True)

        logger.info(f"Downloading {len(depots)} manifest(s) for AppID {app_id or 'unknown'}")

        # 标准化 depot 元组，支持 (depot_id, gid), (depot_id, gid, size), (depot_id, gid, size, owner_app_id)
        valid_depots = []
        for item in depots:
            if not item:
                continue
            did = str(item[0]) if len(item) > 0 else ""
            gid = str(item[1]) if len(item) > 1 and item[1] is not None else ""
            size = int(item[2]) if len(item) > 2 and item[2] else 0
            owner = str(item[3]) if len(item) > 3 and item[3] else app_id
            if did and gid:
                valid_depots.append((did, gid, size, owner))

        if not valid_depots:
            logger.debug("No valid depots (all missing manifest_gid)")
            return ManifestBatchResult(total=len(depots), skipped=len(depots))

        logger.debug(f"Valid depots to download: {len(valid_depots)}")

        # 获取 CDN 服务器列表
        cdn_hosts = self._get_cdn_hosts()

        # 并发下载
        result = ManifestBatchResult(total=len(depots), skipped=len(depots) - len(valid_depots))

        with ThreadPoolExecutor(max_workers=min(self._max_workers, len(valid_depots))) as executor:
            futures = {}
            for did, gid, size, owner in valid_depots:
                future = executor.submit(
                    self._download_single, did, gid, size, cdn_hosts, app_id, owner
                )
                futures[future] = (did, gid)

            for future in as_completed(futures):
                did, gid = futures[future]
                try:
                    dl_result = future.result()
                    result.results.append(dl_result)
                    if dl_result.success:
                        result.success += 1
                    else:
                        result.failed += 1
                except Exception as e:
                    logger.error(f"Download failed for depot {did}/{gid}: {e}")
                    result.results.append(ManifestDownloadResult(
                        depot_id=did,
                        manifest_gid=gid,
                        success=False,
                        message=str(e),
                    ))
                    result.failed += 1

        logger.info(
            f"Manifest download complete for AppID {app_id or 'unknown'}: "
            f"{result.success} success, {result.skipped} skipped, {result.failed} failed"
        )
        return result

    def check_manifest_exists(self, depot_id: str, manifest_gid: str) -> bool:
        """检查 manifest 文件是否已存在于 depotcache 或 config/depotcache

        若仅存在于其中一个目录，自动镜像同步到另一目录。
        """
        if not self._depotcache_dir:
            return False
        fn = f"{depot_id}_{manifest_gid}.manifest"
        p1 = os.path.join(self._depotcache_dir, fn)
        p2 = os.path.join(self._config_depotcache_dir, fn) if self._config_depotcache_dir else ""

        has_p1 = os.path.isfile(p1) and os.path.getsize(p1) > 0
        has_p2 = bool(p2 and os.path.isfile(p2) and os.path.getsize(p2) > 0)

        # 保持双向同步
        if has_p1 and not has_p2 and p2:
            try:
                os.makedirs(os.path.dirname(p2), exist_ok=True)
                with open(p1, "rb") as fsrc, open(p2, "wb") as fdst:
                    fdst.write(fsrc.read())
            except Exception:
                pass
        elif has_p2 and not has_p1:
            try:
                os.makedirs(os.path.dirname(p1), exist_ok=True)
                with open(p2, "rb") as fsrc, open(p1, "wb") as fdst:
                    fdst.write(fsrc.read())
            except Exception:
                pass

        return has_p1 or has_p2

    def verify_game_manifests(
        self,
        depots: list[Any],
    ) -> tuple[bool, list[str]]:
        """验证游戏的 Depot Manifest 文件是否完整

        Returns:
            (全部就绪, 缺失的 manifest 列表)
        """
        missing = []
        for item in depots:
            if not item:
                continue
            did = str(item[0]) if len(item) > 0 else ""
            gid = str(item[1]) if len(item) > 1 and item[1] is not None else ""
            if not gid:
                continue
            if not self.check_manifest_exists(did, gid):
                missing.append(f"{did}_{gid}")
                logger.debug(f"Missing manifest: depot={did}, gid={gid}")

        all_ready = len(missing) == 0
        if not all_ready:
            logger.info(f"Found {len(missing)} missing manifests: {missing[:5]}...")
        return all_ready, missing

    # ── 私有方法 ──────────────────────────────────────────

    def _download_single(
        self,
        depot_id: str,
        manifest_gid: str,
        size: int,
        cdn_hosts: list[str],
        app_id: str = "",
        owner_app_id: str = "",
    ) -> ManifestDownloadResult:
        """下载单个 Depot Manifest（支持多源级联回退）

        源顺序（2026-10 实测）：
        1. 社区 AppID 分支归档的单文件 raw 路径（含 ghfast / ghproxy / jsDelivr 镜像）
        2. ManifestHub API（需要 API Key）
        3. Manifest Request Code → Valve 官方 CDN（社区内容码接口实测可用）
        4. Valve CDN 直连（仅未加锁 / 免令牌 depot）
        """
        target_path = os.path.join(self._depotcache_dir, f"{depot_id}_{manifest_gid}.manifest")

        # 1. 如已存在则直接返回成功
        if self.check_manifest_exists(depot_id, manifest_gid):
            logger.debug(f"Manifest exists, skipping: {depot_id}_{manifest_gid}")
            return ManifestDownloadResult(
                depot_id=depot_id,
                manifest_gid=manifest_gid,
                success=True,
                message="already exists",
                file_path=target_path,
            )

        # 2. 社区分支归档单文件（精确 (depot, gid)，最轻量的可靠来源）
        gh_data = self._download_from_github_repos(depot_id, manifest_gid, app_id, owner_app_id)
        if gh_data:
            return self._save_manifest_payload(depot_id, manifest_gid, gh_data, "GitHub Manifest Cache")

        # 3. 尝试从 ManifestHub API 获取
        mhub_data = self._download_from_manifesthub(depot_id, manifest_gid)
        if mhub_data:
            return self._save_manifest_payload(depot_id, manifest_gid, mhub_data, "ManifestHub API")

        # 4. 通过 Manifest Request Code 走 Valve 官方 CDN
        code_data = self.download_via_request_code(depot_id, manifest_gid)
        if code_data:
            return self._save_manifest_payload(
                depot_id, manifest_gid, code_data, "Valve CDN (request code)"
            )

        # 5. 尝试从 Steam 官方 CDN 获取（未加锁/免令牌 depot 适用）
        for host in cdn_hosts:
            url = self._build_manifest_url(host, depot_id, manifest_gid, size)
            try:
                resp = self._http.get(url, timeout=15.0)
                if resp.status_code == 200:
                    payload = self._extract_manifest_payload(resp.content)
                    if payload:
                        return self._save_manifest_payload(depot_id, manifest_gid, payload, f"Steam CDN ({host})")
            except Exception:
                continue

        logger.warning(f"All sources exhausted for depot {depot_id}/{manifest_gid}")
        return ManifestDownloadResult(
            depot_id=depot_id,
            manifest_gid=manifest_gid,
            success=False,
            message="All sources exhausted",
        )

    def _download_from_github_repos(
        self,
        depot_id: str,
        manifest_gid: str,
        app_id: str = "",
        owner_app_id: str = "",
    ) -> bytes | None:
        """从社区 GitHub 分支归档按精确文件名拉取单个清单。

        分支布局为 ``<repo>/<appid>/{depot}_{gid}.manifest``，这是社区清单
        仓库真实可用的路径（旧实现的 ``P-ToyStore/SteamManifestCache_Pro``
        Tag 路径实测已 404，仓库不存在，已移除）。
        """
        # 延迟导入避免与 core.manifest_archive 形成模块级循环依赖
        from core.manifest_archive import build_branch_raw_urls

        fn = f"{depot_id}_{manifest_gid}.manifest"

        # 候选 (仓库, 分支)：优先拥有者 AppID 分支（DLC 专有分支），再主 AppID 分支
        repo_branches: list[tuple[str, str]] = []
        if owner_app_id:
            for repo in MANIFEST_ARCHIVE_REPOS:
                repo_branches.append((repo, str(owner_app_id)))
        if app_id and str(app_id) != str(owner_app_id):
            for repo in MANIFEST_ARCHIVE_REPOS:
                repo_branches.append((repo, str(app_id)))

        for repo, branch in repo_branches:
            for label, url in build_branch_raw_urls(repo, branch, fn):
                # 官方 raw 走系统代理客户端，公共加速镜像直连（实测二者皆可用）
                client = self._http if label == "GitHub Raw" else self._direct_http
                try:
                    resp = client.get(url, timeout=12.0)
                except Exception:
                    continue
                if resp.status_code != 200 or len(resp.content) < 16:
                    continue
                payload = self._extract_manifest_payload(resp.content)
                if payload and payload[:4] == STEAM_MANIFEST_MAGIC and len(payload) >= 16:
                    logger.info(f"Successfully fetched {fn} from {label}")
                    return payload

        return None

    def fetch_request_code(self, manifest_gid: str) -> str | None:
        """查询清单内容码（Manifest Request Code）。

        实测可用的社区接口（2026-10）：
        * ``http://gmrc.wudrm.com/manifest/{gid}`` → 纯文本内容码
        * ``https://manifest.steam.run/api/manifest/{gid}`` → ``{"content": "..."}``

        内容码与 GID 绑定（同一 GID 对任意 Depot 返回同一个码），因此可以
        在多个 Depot 之间复用；缓存于实例内避免重复请求。

        注意：``gmrc.wudrm.com`` 对并发请求会返回 503 限流（实测 8 并发下
        5/30 失败），而批量下载是 4 线程并发的，因此这里用进程级信号量把
        内容码请求串行化，并对 503/429 做退避重试。
        """
        if not manifest_gid:
            return None

        cache = getattr(self, "_request_code_cache", None)
        if cache is None:
            cache = {}
            self._request_code_cache = cache
        if manifest_gid in cache:
            return cache[manifest_gid]

        code = self._fetch_request_code_throttled(manifest_gid)
        if code:
            cache[manifest_gid] = code
        return code

    # 内容码接口串行化信号量：gmrc.wudrm.com 并发即 503
    _REQUEST_CODE_SEMAPHORE = threading.Semaphore(1)
    _RETRYABLE_STATUS = (429, 500, 502, 503, 504)

    def _fetch_request_code_throttled(self, manifest_gid: str) -> str | None:
        """串行 + 退避重试地请求内容码（进程内全局节流）。"""
        with self._REQUEST_CODE_SEMAPHORE:
            # 二次检查缓存：前一个线程可能刚刚取到同一个 GID 的内容码
            cache = getattr(self, "_request_code_cache", {})
            if manifest_gid in cache:
                return cache[manifest_gid]

            for attempt in range(3):
                if attempt:
                    time.sleep(1.0 + 0.8 * attempt)  # 1.0s / 2.6s 退避
                retryable_seen = False
                code: str | None = None
                for template in MANIFEST_REQUEST_CODE_APIS:
                    url = template.format(gid=manifest_gid, depot="", appid="")
                    client = self._direct_http if "gmrc.wudrm.com" in url else self._http
                    try:
                        resp = client.get(url, timeout=12.0)
                    except Exception as exc:
                        logger.debug("内容码接口异常 %s: %s", url, exc)
                        retryable_seen = True
                        continue
                    if resp.status_code in self._RETRYABLE_STATUS:
                        logger.debug(
                            "内容码接口限流 %s（HTTP %d）",
                            url.split("/")[2], resp.status_code,
                        )
                        retryable_seen = True
                        continue
                    if resp.status_code != 200:
                        continue
                    text = resp.text.strip()
                    if not text:
                        continue
                    if text.startswith("{"):
                        try:
                            code = str(resp.json().get("content") or "").strip() or None
                        except ValueError:
                            code = None
                    else:
                        code = text
                    if code and code.isdigit():
                        logger.debug("Manifest Request Code 命中 %s: %s", url.split("/")[2], code)
                        return code
                    code = None
                # 全部接口都是确定性失败（404/空响应）时不再退避重试
                if not retryable_seen:
                    break

            logger.debug("未能获取 GID %s 的内容码", manifest_gid)
            return None

    def download_via_request_code(
        self,
        depot_id: str,
        manifest_gid: str,
        cdn_hosts: list[str] | None = None,
    ) -> bytes | None:
        """用内容码从 Valve 官方 CDN 拉取清单并解出标准 manifest。

        实测（2026-10）：``https://steampipe.akamaized.net/depot/2347770/manifest/
        7138855853134977810/5/12005229650648827506`` → 200，返回 ZIP（内含 'z' 条目）。

        ``steampipe.akamaized.net`` 固定排在首位：它是 Valve 官方 Akamai
        入口且无需鉴权信息即可直连，实测稳定性优于 Steam API 下发的区域主机。
        """
        code = self.fetch_request_code(manifest_gid)
        if not code:
            return None

        api_hosts = self._get_cdn_hosts()
        # akamaized 官方入口优先，Steam API 主机随后（去重）
        hosts = ["steampipe.akamaized.net"] + [h for h in api_hosts if h != "steampipe.akamaized.net"]
        if cdn_hosts:
            hosts = list(cdn_hosts)
        for host in hosts:
            url = f"https://{host}/depot/{depot_id}/manifest/{manifest_gid}/5/{code}"
            try:
                resp = self._direct_http.get(url, timeout=25.0)
            except Exception:
                continue
            if resp.status_code != 200 or len(resp.content) < 16:
                continue
            payload = self._extract_manifest_payload(resp.content)
            if payload and payload[:4] == STEAM_MANIFEST_MAGIC:
                logger.info("内容码下载成功: %s/%s (%s)", depot_id, manifest_gid, host)
                return payload
        return None

    def save_manifest(
        self,
        depot_id: str,
        manifest_gid: str,
        payload: bytes,
        source_name: str = "external",
    ) -> ManifestDownloadResult:
        """公开的清单落盘接口（同时写入 depotcache 与 config/depotcache）。"""
        return self._save_manifest_payload(depot_id, manifest_gid, payload, source_name)

    def _download_from_manifesthub(self, depot_id: str, manifest_gid: str) -> bytes | None:
        """从 ManifestHub API 拉取清单（若有 API Key）"""
        from core.config_manager import ConfigManager
        cm = ConfigManager()
        api_key = cm.get("manifesthub_api_key", MANIFESTHUB_API_KEY)
        if not api_key:
            return None

        base_api_url = cm.get("manifesthub_api_url", MANIFESTHUB_API_URL)
        url = f"{base_api_url}?apikey={api_key}&depotid={depot_id}&manifestid={manifest_gid}"
        try:
            resp = self._http.get(url, timeout=20.0)
            if resp.status_code == 200 and len(resp.content) >= 16:
                payload = self._extract_manifest_payload(resp.content)
                if payload:
                    return payload
        except Exception as e:
            logger.debug(f"ManifestHub API fetch failed: {e}")

        return None

    def _save_manifest_payload(
        self,
        depot_id: str,
        manifest_gid: str,
        payload: bytes,
        source_name: str,
    ) -> ManifestDownloadResult:
        """保存解压验证后的清单到 depotcache 与 config/depotcache"""
        target_path = os.path.join(self._depotcache_dir, f"{depot_id}_{manifest_gid}.manifest")
        try:
            # 确保目录存在
            if self._depotcache_dir:
                os.makedirs(self._depotcache_dir, exist_ok=True)
            if self._config_depotcache_dir:
                os.makedirs(self._config_depotcache_dir, exist_ok=True)

            # 写入主 depotcache
            with open(target_path, "wb") as f:
                f.write(payload)

            # 镜像写入 config/depotcache
            if self._config_depotcache_dir:
                cfg_path = os.path.join(self._config_depotcache_dir, f"{depot_id}_{manifest_gid}.manifest")
                try:
                    with open(cfg_path, "wb") as f:
                        f.write(payload)
                except Exception:
                    pass

            # 清理旧版本清单
            self._clean_old_manifests(depot_id, manifest_gid)

            logger.info(
                f"Manifest saved: {depot_id}_{manifest_gid} "
                f"({len(payload)} bytes) from {source_name}"
            )
            return ManifestDownloadResult(
                depot_id=depot_id,
                manifest_gid=manifest_gid,
                success=True,
                message=f"Downloaded from {source_name} ({len(payload)} bytes)",
                file_path=target_path,
            )
        except Exception as e:
            logger.error(f"Failed to save manifest file {target_path}: {e}")
            return ManifestDownloadResult(
                depot_id=depot_id,
                manifest_gid=manifest_gid,
                success=False,
                message=f"Save failed: {e}",
            )

    @classmethod
    def _extract_manifest_payload(cls, data: bytes) -> bytes | None:
        """从任意格式的原始数据中提取标准的 Steam 二进制清单

        支持格式：
        1. 标准未经压缩的 Steam 清单（以 0x71F617D0 魔数开头）
        2. Pro 体系压缩清单（10 字节头 + raw DEFLATE 流，RFC 1951）
        3. Steam CDN ZIP 封装（包含名为 'z' 或以 '.manifest' 结尾的载荷）
        4. 标准 zlib 封装流
        """
        if not data or len(data) < 16:
            return None

        # 1. 已经是解压好的标准 Steam Manifest
        if data[:4] == STEAM_MANIFEST_MAGIC:
            return data

        # 2. 检测并解压 ZIP 格式封装
        if data[:2] == b"PK":
            try:
                with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
                    for name in zf.namelist():
                        raw_entry = zf.read(name)
                        extracted = cls._extract_manifest_payload(raw_entry)
                        if extracted:
                            return extracted
            except Exception:
                pass

        # 3. 检测 Pro 体系的 10 字节头 + raw DEFLATE，或标准 DEFLATE/zlib 流
        for offset in (10, 2, 0):
            if len(data) <= offset:
                continue
            # 尝试 Raw DEFLATE (-15)
            try:
                decompressor = zlib.decompressobj(-15)
                decomp = decompressor.decompress(data[offset:])
                if decomp and decomp[:4] == STEAM_MANIFEST_MAGIC:
                    return decomp
            except Exception:
                pass

            # 尝试标准 zlib (15)
            try:
                decomp = zlib.decompress(data[offset:])
                if decomp and decomp[:4] == STEAM_MANIFEST_MAGIC:
                    return decomp
            except Exception:
                pass

        return None

    def _clean_old_manifests(self, depot_id: str, current_gid: str):
        """清理同一 DepotID 的旧版本 manifest 文件（保留当前版本）"""
        dirs_to_clean = [self._depotcache_dir]
        if self._config_depotcache_dir:
            dirs_to_clean.append(self._config_depotcache_dir)

        current_file = f"{depot_id}_{current_gid}.manifest"
        for d in dirs_to_clean:
            if not d or not os.path.isdir(d):
                continue
            try:
                for fname in os.listdir(d):
                    if fname.startswith(f"{depot_id}_") and fname.endswith(".manifest"):
                        if fname != current_file:
                            old_path = os.path.join(d, fname)
                            try:
                                os.remove(old_path)
                                logger.debug(f"Removed old manifest: {fname}")
                            except OSError:
                                pass
            except OSError:
                pass

    @staticmethod
    def _build_manifest_url(host: str, depot_id: str, manifest_gid: str, size: int = 0) -> str:
        """构建 Steam CDN manifest 下载 URL

        Steam CDN URL 格式：
        - 主模式：/{host}/depot/{depot_id}/manifest/{manifest_gid}/5/{size or 0}
        """
        return f"https://{host}/depot/{depot_id}/manifest/{manifest_gid}/5/{size or 0}"

    def _get_cdn_hosts(self) -> list[str]:
        """获取 Steam CDN 服务器列表（带缓存）

        优先从 Steam 官方 API 获取，失败时使用备用列表。
        """
        # 检查缓存
        now = time.time()
        if self._CDN_CACHE and (now - self._CDN_CACHE_TIME) < self._CDN_CACHE_TTL:
            logger.debug(f"Using cached CDN hosts ({len(self._CDN_CACHE)} hosts)")
            return self._CDN_CACHE

        hosts = self._fetch_cdn_from_api()
        if hosts:
            self._CDN_CACHE = hosts
            self._CDN_CACHE_TIME = now
            logger.info(f"Fetched {len(hosts)} CDN hosts from Steam API")
            return hosts

        # 降级：使用备用列表
        logger.warning("Failed to fetch CDN hosts from API, using fallback list")
        self._CDN_CACHE = _FALLBACK_CDN_HOSTS
        self._CDN_CACHE_TIME = now
        return _FALLBACK_CDN_HOSTS

    def _fetch_cdn_from_api(self) -> list[str]:
        """从 Steam 官方 API 获取 CDN 服务器列表"""
        try:
            resp = self._http.get(STEAM_CDN_API, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()

            servers = data.get("response", {}).get("servers", [])

            # Steam's weighted_load is dynamic; older code accepted only 130,
            # which now rejects every current CDN entry and forces stale
            # fallback hosts.
            hosts = []
            for server in servers:
                if isinstance(server, dict):
                    srv_type = server.get("type", "")
                    host = server.get("host", "")

                    if not host:
                        continue

                    # SteamCache 类型优先
                    if srv_type == "SteamCache":
                        hosts.append(host)
                    elif srv_type == "CDN":
                        hosts.append(host)

            # Prefer SteamCache entries, then ordinary CDN entries.
            logger.debug(f"Filtered {len(hosts)} usable CDN hosts from {len(servers)} total")
            return hosts

        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as e:
            logger.warning(f"Failed to fetch CDN hosts from Steam API: {e}")
            return []
