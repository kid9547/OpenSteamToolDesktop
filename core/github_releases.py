"""
github_releases — GitHub Releases 爬取模块

通过 GitHub API / Releases 页面获取 OpenSteamTool 内核最新版本并下载 DLL。

当前内核仓库为 ``hazjy/ZSteamTool``（v1.2.x）。它的 Release ZIP 同时包含
``Release/`` 与 ``Debug/`` 两套 DLL，本模块只解压 ``Release/`` 并扁平化到
版本目录，避免把体积巨大且带控制台日志的 Debug 版本部署到 Steam。
"""

from __future__ import annotations

import re
import warnings
import zipfile
from pathlib import Path
from typing import Optional, Callable

import requests
import urllib3
from PyQt6.QtCore import QObject, pyqtSignal

from utils.logger import setup_logger
from config import (
    HTTP_DEFAULT_TIMEOUT,
    OPENSTEAMTOOL_API_LATEST,
    OPENSTEAMTOOL_API_TAG,
    OPENSTEAMTOOL_REPO_URL,
    OPENSTEAMTOOL_RELEASES_URL,
    SSL_VERIFY,
    GITHUB_API_LATEST_RELEASE,  # re-exported for older callers/tests
)

GITHUB_RELEASES_PAGE: str = OPENSTEAMTOOL_RELEASES_URL

# 禁用 SSL 警告（已主动禁用 verify，避免日志被 InsecureRequestWarning 污染）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

logger = setup_logger(__name__)
logger.info(f"SSL verification: {'disabled' if not SSL_VERIFY else 'enabled'} (from config)")

# DLL asset 文件名模式（64 位）。ZSteamTool 使用 ZSteamTool-<ver>.zip，
# huanyuejue 分支使用 OpenSteamTool-<ver>-Release.zip。
DLL_ASSET_PATTERN: str = r"(?:ZSteamTool|OpenSteamTool)-.*\.zip"

_RELEASE_ASSET_RE = re.compile(DLL_ASSET_PATTERN, re.IGNORECASE)


def _is_dll_zip(name: str) -> bool:
    """Release 资产是否是内核 DLL 压缩包（排除源码包）。"""
    lower = name.lower()
    return lower.endswith(".zip") and "source" not in lower and _RELEASE_ASSET_RE.search(name) is not None


def _asset_score(name: str) -> int:
    """资产优先级：Release 最高，普通 zip 次之，Debug 最低。"""
    lower = name.lower()
    if "debug" in lower:
        return 10
    if "release" in lower:
        return 100
    return 50


def select_dll_asset(assets: list[dict]) -> Optional[dict]:
    """从 GitHub release assets 中选出最合适的内核 ZIP。"""
    candidates = [
        asset for asset in assets
        if isinstance(asset, dict) and _is_dll_zip(asset.get("name", ""))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda asset: _asset_score(asset.get("name", "")))


class GitHubReleases(QObject):
    """GitHub Releases 页面爬取封装

    提供：
    1. 爬取最新 release 信息
    2. 下载 release asset
    """

    # 信号
    download_progress = pyqtSignal(int, int)  # downloaded_bytes, total_bytes
    download_finished = pyqtSignal(bool, str)  # success, message

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._session = requests.Session()
        # SSL 证书验证设置
        self._session.verify = SSL_VERIFY
        # 使用浏览器级别的 headers 来避免 403/406 错误
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        })

    def _parse_release_data(self, data: dict) -> dict:
        """解析 GitHub API 返回的 release 数据"""
        tag_name = data.get("tag_name", "")
        assets = [
            a for a in data.get("assets", [])
            if isinstance(a, dict) and a.get("name", "").lower().endswith(".zip")
        ]
        return {
            "version": tag_name,
            "tag_name": tag_name,
            "published_at": data.get("published_at", ""),
            "body": data.get("body", ""),
            "html_url": data.get("html_url", ""),
            "assets": assets,
        }

    def _scrape_releases_page(self) -> Optional[dict]:
        """爬取 GitHub releases 页面获取最新版本信息（降级策略）"""
        try:
            logger.info("Scraping releases page...")
            resp = self._session.get(
                OPENSTEAMTOOL_RELEASES_URL,
                timeout=HTTP_DEFAULT_TIMEOUT,
                allow_redirects=True,
            )
            resp.raise_for_status()
            html = resp.text

            repo_path = re.escape(
                OPENSTEAMTOOL_REPO_URL.replace("https://github.com/", "").strip("/")
            )
            tag_pattern = rf'/{repo_path}/releases/tag/([^"\'>\s]+)'
            matches = re.findall(tag_pattern, html)
            if not matches:
                logger.error("Failed to parse releases page: no tags found")
                return None

            latest_tag = matches[0]
            logger.info(f"Latest release from scrape: {latest_tag}")

            return {
                "version": latest_tag,
                "tag_name": latest_tag,
                "published_at": "",
                "body": "",
                "html_url": f"{OPENSTEAMTOOL_RELEASES_URL}/tag/{latest_tag}",
                "assets": [],
            }
        except Exception as e:
            logger.error(f"Failed to scrape releases page: {e}")
            return None

    def get_latest_release_info(self) -> Optional[dict]:
        """获取最新 release 信息（优先 API，失败降级爬取）

        Returns:
            包含版本信息的字典，如果失败则返回 None
        """
        try:
            logger.info("Fetching latest release info from GitHub API...")
            resp = self._session.get(
                OPENSTEAMTOOL_API_LATEST,
                timeout=HTTP_DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            return self._parse_release_data(data)
        except requests.exceptions.RequestException as e:
            logger.warning(f"GitHub API failed ({e}), falling back to scraping releases page...")
            try:
                return self._scrape_releases_page()
            except Exception as se:
                logger.error(f"Scraping fallback failed: {se}")
                return None
        except Exception as e:
            logger.error(f"Failed to get latest release info: {e}")
            return None

    def get_asset_download_url(self, version: str) -> Optional[str]:
        """获取指定版本的 DLL ZIP 下载链接（优先 API，降级爬取和猜测）

        Args:
            version: 版本号，如 "v1.2.3"

        Returns:
            下载 URL，如果未找到则返回 None
        """
        # 1. 尝试从 GitHub API 获取 release 详情与 assets
        try:
            api_url = OPENSTEAMTOOL_API_TAG.format(tag=version)
            resp = self._session.get(api_url, timeout=HTTP_DEFAULT_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                asset = select_dll_asset(data.get("assets", []))
                if asset:
                    return asset.get("browser_download_url")
                logger.info(f"No suitable ZIP asset in API response for {version}")
        except Exception as e:
            logger.debug(f"GitHub tag API failed for {version}: {e}")

        # 2. 爬取 release 页面
        try:
            release_url = f"{OPENSTEAMTOOL_REPO_URL}/releases/tag/{version}"
            logger.info(f"Scraping release page: {release_url}")

            resp = self._session.get(release_url, timeout=HTTP_DEFAULT_TIMEOUT)
            resp.raise_for_status()
            html = resp.text

            repo_path = re.escape(
                OPENSTEAMTOOL_REPO_URL.replace("https://github.com/", "").strip("/")
            )
            zip_pattern = rf'/{repo_path}/releases/download/[^"]+\.zip'
            matches = re.findall(zip_pattern, html)

            if not matches:
                logger.info(f"No ZIP link in static HTML for {version}, using fallback")
                return self._guess_download_url(version)

            # Release > 普通 zip > Debug
            matches.sort(key=lambda match: _asset_score(match), reverse=True)
            download_url = f"https://github.com{matches[0]}"
            logger.info(f"Found ZIP download URL: {download_url}")
            return download_url

        except Exception as e:
            logger.error(f"Failed to get asset download URL: {e}")
            return None

    def _guess_download_url(self, version: str) -> Optional[str]:
        """猜测 ZIP 下载链接（降级方案）

        当无法从页面解析下载链接时，尝试构造可能的下载 URL。

        Args:
            version: 版本号，如 "v1.2.1"

        Returns:
            下载 URL，如果构造失败则返回 None
        """
        bare = version.lstrip("v")
        # 常见的文件名模式（优先 Release）
        possible_names = [
            f"ZSteamTool-{version}.zip",
            f"ZSteamTool-v{bare}.zip",
            f"ZSteamTool-{version}-Release.zip",
            f"ZSteamTool-v{bare}-Release.zip",
            f"OpenSteamTool-{version}-Release.zip",
            f"OpenSteamTool-{bare}-Release.zip",
            f"OpenSteamTool-{version}.zip",
            f"OpenSteamTool-{bare}.zip",
        ]
        tags = [version]
        if not version.startswith("v"):
            tags.append(f"v{version}")
        for tag in tags:
            for name in possible_names:
                url = f"{OPENSTEAMTOOL_REPO_URL}/releases/download/{tag}/{name}"
                logger.info(f"Trying guess URL: {url}")
                # 发送 HEAD 请求检查 URL 是否有效
                try:
                    resp = self._session.head(url, timeout=5, allow_redirects=True)
                    if resp.status_code == 200:
                        logger.info(f"Found valid download URL: {url}")
                        return url
                except Exception:
                    continue

        logger.warning(f"Failed to guess download URL for version {version}")
        return None

    def download_asset(
        self,
        download_url: str,
        save_path: Path,
        progress_callback: Optional[callable] = None,
    ) -> bool:
        """下载 release asset 到指定路径

        Args:
            download_url: 下载链接
            save_path: 保存路径（文件）
            progress_callback: 进度回调函数 (downloaded, total)

        Returns:
            是否下载成功
        """
        try:
            logger.info(f"Downloading asset from {download_url} to {save_path}")
            save_path.parent.mkdir(parents=True, exist_ok=True)

            # 流式下载
            resp = self._session.get(
                download_url,
                stream=True,
                timeout=HTTP_DEFAULT_TIMEOUT * 2,
            )
            resp.raise_for_status()

            total_size = int(resp.headers.get("content-length", 0))
            downloaded_size = 0

            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if progress_callback and total_size > 0:
                            progress_callback(downloaded_size, total_size)

            logger.info(f"Download completed: {save_path}")
            return True

        except Exception as e:
            logger.error(f"Failed to download asset: {e}")
            # 清理未完成的下载
            if save_path.exists():
                try:
                    save_path.unlink()
                except Exception:
                    pass
            return False

    def extract_dll_from_zip(
        self,
        zip_path: Path,
        extract_dir: Path,
    ) -> list[str]:
        """从 ZIP 文件中提取 DLL 文件（扁平化，优先 Release/）

        ZSteamTool 的 Release ZIP 结构为::

            Release/OpenSteamTool.dll
            Release/dwmapi.dll
            Release/xinput1_4.dll
            Debug/OpenSteamTool.dll
            ...

        Args:
            zip_path: ZIP 文件路径
            extract_dir: 解压目标目录

        Returns:
            提取的 DLL 文件名列表
        """
        extracted_dlls: list[str] = []
        try:
            logger.info(f"Extracting DLLs from {zip_path} to {extract_dir}")
            extract_dir.mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(zip_path, "r") as zf:
                dll_members = [
                    info for info in zf.infolist()
                    if not info.is_dir() and info.filename.lower().endswith(".dll")
                ]
                logger.debug(f"ZIP contains {len(zf.namelist())} files, {len(dll_members)} DLLs")

                # 选择一套 DLL：Release/ > 根目录 > 其他目录；Debug/ 仅在别无选择时使用
                def _member_score(info: zipfile.ZipInfo) -> int:
                    parts = [part.lower() for part in Path(info.filename).parts[:-1]]
                    if "debug" in parts:
                        return 10
                    if "release" in parts:
                        return 100
                    return 50 if not parts else 40

                selected: dict[str, zipfile.ZipInfo] = {}
                for info in dll_members:
                    name = Path(info.filename).name
                    current = selected.get(name)
                    if current is None or _member_score(info) > _member_score(current):
                        selected[name] = info

                for name, info in selected.items():
                    target = extract_dir / name
                    with zf.open(info) as source, open(target, "wb") as dest:
                        dest.write(source.read())
                    extracted_dlls.append(name)
                    logger.info(f"Extracted: {name} (from {info.filename})")

            logger.info(f"Total DLLs extracted: {len(extracted_dlls)}")
            return extracted_dlls

        except zipfile.BadZipFile:
            logger.error(f"Bad ZIP file: {zip_path}")
            return []
        except Exception as e:
            logger.error(f"Failed to extract DLL from ZIP: {e}")
            return []

    def close(self):
        """关闭 session"""
        if self._session:
            self._session.close()


def get_latest_release_info() -> Optional[dict]:
    """获取最新 release 信息的便捷函数"""
    client = GitHubReleases()
    try:
        return client.get_latest_release_info()
    finally:
        client.close()


def download_release_asset(
    download_url: str,
    save_path: Path,
) -> bool:
    """下载 release asset 的便捷函数"""
    client = GitHubReleases()
    try:
        return client.download_asset(download_url, save_path)
    finally:
        client.close()
