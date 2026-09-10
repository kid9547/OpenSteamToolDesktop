"""
github_releases — GitHub Releases 爬取模块

通过爬取 GitHub Releases 页面获取 OpenSteamTool 最新版本信息并下载 DLL。
不使用 GitHub API，避免速率限制。
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
    OPENSTEAMTOOL_REPO_URL,
    OPENSTEAMTOOL_RELEASES_URL,
    SSL_VERIFY,
    GITHUB_API_LATEST_RELEASE,
)

GITHUB_RELEASES_PAGE: str = OPENSTEAMTOOL_RELEASES_URL

# 禁用 SSL 警告（已主动禁用 verify，避免日志被 InsecureRequestWarning 污染）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

logger = setup_logger(__name__)
logger.info(f"SSL verification: {'disabled' if not SSL_VERIFY else 'enabled'} (from config)")

# DLL asset 文件名模式（64 位）
DLL_ASSET_PATTERN: str = r"OpenSteamTool-.*\.zip"


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
            if isinstance(a, dict) and a.get("name", "").endswith(".zip")
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

            tag_pattern = r'/OpenSteam001/OpenSteamTool/releases/tag/([^"\'>\s]+)'
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
                GITHUB_API_LATEST_RELEASE,
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
            api_url = f"https://api.github.com/repos/OpenSteam001/OpenSteamTool/releases/tags/{version}"
            resp = self._session.get(api_url, timeout=HTTP_DEFAULT_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                assets = data.get("assets", [])
                for asset in assets:
                    name = asset.get("name", "")
                    if name.endswith(".zip"):
                        return asset.get("browser_download_url")
        except Exception:
            pass

        # 2. 爬取 release 页面
        try:
            release_url = f"{OPENSTEAMTOOL_REPO_URL}/releases/tag/{version}"
            logger.info(f"Scraping release page: {release_url}")

            resp = self._session.get(release_url, timeout=HTTP_DEFAULT_TIMEOUT)
            resp.raise_for_status()
            html = resp.text

            zip_pattern = r'/OpenSteam001/OpenSteamTool/releases/download/[^"]+\.zip'
            matches = re.findall(zip_pattern, html)

            if not matches:
                logger.info(f"No ZIP link in static HTML for {version}, using fallback")
                return self._guess_download_url(version)

            download_path = None
            for match in matches:
                if "-Release.zip" in match:
                    download_path = match
                    break
            if download_path is None:
                for match in matches:
                    if "-Debug.zip" in match:
                        download_path = match
                        break
            if download_path is None:
                download_path = matches[0]

            download_url = f"https://github.com{download_path}"
            logger.info(f"Found ZIP download URL: {download_url}")
            return download_url

        except Exception as e:
            logger.error(f"Failed to get asset download URL: {e}")
            return None

    def _guess_download_url(self, version: str) -> Optional[str]:
        """猜测 ZIP 下载链接（降级方案）

        当无法从页面解析下载链接时，尝试构造可能的下载 URL。

        Args:
            version: 版本号，如 "1.4.8"

        Returns:
            下载 URL，如果构造失败则返回 None
        """
        # 常见的文件名模式（优先 Release）
        possible_names = [
            f"OpenSteamTool-{version}-Release.zip",
            f"OpenSteamTool-{version}-Debug.zip",
            f"OpenSteamTool-{version}.zip",
        ]

        for name in possible_names:
            url = f"{OPENSTEAMTOOL_REPO_URL}/releases/download/{version}/{name}"
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
        """从 ZIP 文件中提取 DLL 文件

        Args:
            zip_path: ZIP 文件路径
            extract_dir: 解压目标目录

        Returns:
            提取的 DLL 文件名列表
        """
        extracted_dlls = []
        try:
            logger.info(f"Extracting DLLs from {zip_path} to {extract_dir}")
            extract_dir.mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(zip_path, "r") as zf:
                # 列出所有文件
                all_files = zf.namelist()
                logger.debug(f"ZIP contains {len(all_files)} files")

                # 只提取 DLL 文件
                for file_info in all_files:
                    if file_info.endswith(".dll"):
                        # 提取文件
                        zf.extract(file_info, extract_dir)
                        dll_name = Path(file_info).name
                        extracted_dlls.append(dll_name)
                        logger.info(f"Extracted: {dll_name}")

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
