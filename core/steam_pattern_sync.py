"""Sync OpenSteamTool Steam-version signature files before Steam restart."""
from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

import httpx

from config import (
    SSL_VERIFY,
    STEAM_MONITOR_PATTERN_RAW,
    STEAM_MONITOR_PATTERN_CDN,
    STEAM_MONITOR_IPC_RAW,
    STEAM_MONITOR_IPC_CDN,
)
from utils.logger import setup_logger

logger = setup_logger(__name__)


class SteamPatternSync:
    """Download the signatures required by the installed OpenSteamTool DLL."""

    _components = {
        "steamclient": "steamclient64.dll",
        "steamui": "steamui.dll",
    }
    _ipc_components = {
        "steamclient": "steamclient64.dll",
    }

    def __init__(self, steam_path: str):
        self._steam_path = Path(steam_path)

    def sync(self) -> tuple[bool, str]:
        if not self._steam_path.is_dir():
            return False, f"Steam 目录不存在：{self._steam_path}"

        pattern_root = self._steam_path / "opensteamtool" / "pattern"
        downloaded = 0
        missing: list[str] = []
        proxy = self._get_system_proxy()

        try:
            with httpx.Client(
                timeout=15.0,
                follow_redirects=True,
                proxy=proxy,
                verify=SSL_VERIFY,
                headers={"User-Agent": "OpenSteamToolDesktop/1.0.3"},
            ) as client:
                for component, dll_name in self._components.items():
                    dll_path = self._steam_path / dll_name
                    if not dll_path.is_file():
                        missing.append(dll_name)
                        continue

                    sha256 = self._sha256(dll_path)
                    target = pattern_root / component / f"{sha256}.toml"
                    if target.is_file() and target.stat().st_size > 0:
                        continue

                    body = self._fetch(client, STEAM_MONITOR_PATTERN_RAW, component, sha256)
                    if body is None:
                        body = self._fetch(client, STEAM_MONITOR_PATTERN_CDN, component, sha256)
                    if body is None:
                        missing.append(f"{component}/{sha256}.toml")
                        continue

                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(body)
                    downloaded += 1

                for component, dll_name in self._ipc_components.items():
                    dll_path = self._steam_path / dll_name
                    if not dll_path.is_file():
                        missing.append(f"ipc/{component}/{dll_name}")
                        continue

                    sha256 = self._sha256(dll_path)
                    target = pattern_root.parent / "ipc" / component / f"{sha256}.toml"
                    if target.is_file() and target.stat().st_size > 0:
                        continue

                    body = self._fetch(client, STEAM_MONITOR_IPC_RAW, component, sha256)
                    if body is None:
                        body = self._fetch(client, STEAM_MONITOR_IPC_CDN, component, sha256)
                    if body is None:
                        missing.append(f"ipc/{component}/{sha256}.toml")
                        continue

                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(body)
                    downloaded += 1

        except (httpx.HTTPError, OSError) as exc:
            logger.warning("Steam signature sync failed: %s", exc)
            return False, f"签名文件同步失败：{exc}"

        if missing:
            return False, f"缺少 Steam 签名文件：{', '.join(missing)}"
        return True, f"Steam 签名文件已就绪（新增 {downloaded} 个）"

    def missing_patterns(self) -> list[str]:
        """Return signature files missing for the installed Steam DLLs."""
        missing: list[str] = []
        root = self._steam_path / "opensteamtool" / "pattern"
        for component, dll_name in self._components.items():
            dll_path = self._steam_path / dll_name
            if not dll_path.is_file():
                missing.append(dll_name)
                continue
            sha256 = self._sha256(dll_path)
            target = root / component / f"{sha256}.toml"
            if not target.is_file() or target.stat().st_size == 0:
                missing.append(str(target))
        for component, dll_name in self._ipc_components.items():
            dll_path = self._steam_path / dll_name
            if not dll_path.is_file():
                missing.append(f"ipc/{component}/{dll_name}")
                continue
            sha256 = self._sha256(dll_path)
            target = root.parent / "ipc" / component / f"{sha256}.toml"
            if not target.is_file() or target.stat().st_size == 0:
                missing.append(str(target))
        return missing

    @staticmethod
    def _fetch(
        client: httpx.Client,
        template: str,
        component: str,
        sha256: str,
    ) -> bytes | None:
        url = template.format(component=component, sha256=sha256)
        try:
            response = client.get(url)
            if response.status_code == 200 and response.content:
                return response.content
            logger.debug("Signature URL returned HTTP %s: %s", response.status_code, url)
        except httpx.HTTPError as exc:
            logger.debug("Signature URL failed: %s (%s)", url, exc)
        return None

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _get_system_proxy() -> str | None:
        proxies = urllib.request.getproxies()
        return proxies.get("https") or proxies.get("http")
