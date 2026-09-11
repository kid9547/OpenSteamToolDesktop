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
    MANIFEST_GITHUB_REPOS, GITHUB_RAW_MIRRORS,
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


# 常用 Steam CDN 备用列表（API 不可用时使用）
_FALLBACK_CDN_HOSTS = [
    "cache1-steamcontent.com",
    "cache2-steamcontent.com",
    "cache3-steamcontent.com",
    "cache4-steamcontent.com",
    "cache5-steamcontent.com",
    "cache6-steamcontent.com",
    "cache7-steamcontent.com",
    "cache8-steamcontent.com",
    "cache9-steamcontent.com",
    "cache10-steamcontent.com",
    "cache1-lax1.steamcontent.com",
    "cache2-lax1.steamcontent.com",
]


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
        """下载单个 Depot Manifest（支持多源级联回退）"""
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

        # 2. 尝试从社区 GitHub 仓库下载（按 Tag 或分支快速获取）
        gh_data = self._download_from_github_repos(depot_id, manifest_gid, app_id, owner_app_id)
        if gh_data:
            return self._save_manifest_payload(depot_id, manifest_gid, gh_data, "GitHub Manifest Cache")

        # 3. 尝试从 ManifestHub API 获取
        mhub_data = self._download_from_manifesthub(depot_id, manifest_gid)
        if mhub_data:
            return self._save_manifest_payload(depot_id, manifest_gid, mhub_data, "ManifestHub API")

        # 4. 尝试从 Steam 官方 CDN 获取（未加锁/免令牌 depot 适用）
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
        """从社区 GitHub 清单仓库拉取文件"""
        fn = f"{depot_id}_{manifest_gid}.manifest"

        # 组织候选文件相对路径
        candidates: list[tuple[str, str]] = []

        # 优先路径 1: P-ToyStore 的 Tag 路径（极度精准，无 AppID 归属混淆）
        if manifest_gid:
            candidates.append(
                ("P-ToyStore/SteamManifestCache_Pro", f"refs/tags/{depot_id}_{manifest_gid}/{fn}")
            )

        # 路径 2: 拥有者 AppID 分支（DLC 专有分支）
        if owner_app_id:
            for repo in MANIFEST_GITHUB_REPOS:
                candidates.append((repo, f"{owner_app_id}/{fn}"))

        # 路径 3: 主游戏 AppID 分支
        if app_id and app_id != owner_app_id:
            for repo in MANIFEST_GITHUB_REPOS:
                candidates.append((repo, f"{app_id}/{fn}"))

        # 遍历候选仓库路径，依次尝试直连与镜像源
        for repo, rel_path in candidates:
            raw_url = f"https://raw.githubusercontent.com/{repo}/{rel_path}"

            url_candidates = [
                (raw_url, self._http),                                               # 优先走代理客户端
                (f"https://ghfast.top/{raw_url}", self._direct_http),                # 高速镜像 1
                (f"https://ghproxy.net/{raw_url}", self._direct_http),               # 高速镜像 2
                (f"https://raw.dgithub.xyz/{repo}/{rel_path}", self._direct_http),   # 高速镜像 3
            ]

            for url, client in url_candidates:
                try:
                    resp = client.get(url, timeout=12.0)
                    if resp.status_code == 200 and len(resp.content) >= 16:
                        payload = self._extract_manifest_payload(resp.content)
                        if payload and len(payload) >= 16:
                            logger.info(f"Successfully fetched {fn} from {url.split('/')[2]}")
                            return payload
                except Exception:
                    continue

        return None

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
