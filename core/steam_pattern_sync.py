"""Ensure the installed kernel has signature tables for the current Steam DLLs.

Resolution order for every missing table:

1. an existing cache entry for this exact DLL SHA-256
2. a pre-generated table bundled with this application
3. local zero-seed generation from the DLL (vendored resolver)
4. the remote mirror chain configured in ``opensteamtool.toml`` / config.py

Local generation is deliberately preferred over the network: Valve updates the
client far more often than the community mirrors, and a 30-second timeout chain
on every Steam start is worse than a 5-second local scan.
"""
from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

import httpx

from config import SSL_VERIFY, STEAM_PATTERN_URL_TEMPLATES
from core.app_paths import bundled_resource
from core.local_pattern_generator import LocalPatternGenerator
from core.opensteamtool_config import get_value
from utils.logger import setup_logger

logger = setup_logger(__name__)

_COMPONENTS = {
    "steamclient": "steamclient64.dll",
    "steamui": "steamui.dll",
}
_IPC_COMPONENT = "steamclient"


class SteamPatternSync:
    """Prepare ``opensteamtool/pattern`` and ``opensteamtool/ipc`` caches."""

    def __init__(self, steam_path: str, *, force: bool = False):
        self._steam_path = Path(steam_path)
        self._force = force
        self._root = self._steam_path / "opensteamtool"

    # ── public API ────────────────────────────────────────────────

    def sync(self) -> tuple[bool, str]:
        if not self._steam_path.is_dir():
            return False, f"Steam 目录不存在：{self._steam_path}"

        if self._force:
            removed = LocalPatternGenerator(self._steam_path).clear_cache()
            if removed:
                logger.info("Cleared %d cached signature table(s)", removed)

        missing: list[tuple[str, str, Path]] = []  # (channel, component, target)
        notes: list[str] = []

        for component, dll_name in _COMPONENTS.items():
            dll_path = self._steam_path / dll_name
            if not dll_path.is_file():
                missing.append(("pattern", component, Path(dll_name)))
                continue
            digest = self._sha256(dll_path)
            target = self._root / "pattern" / component / f"{digest}.toml"
            if self._ready(target):
                continue
            if self._copy_bundled("pattern", component, digest, target):
                notes.append(f"{component} 使用内置签名表")
                continue
            missing.append(("pattern", component, target))

        dll_path = self._steam_path / _COMPONENTS[_IPC_COMPONENT]
        if dll_path.is_file():
            digest = self._sha256(dll_path)
            target = self._root / "ipc" / _IPC_COMPONENT / f"{digest}.toml"
            if not self._ready(target):
                if self._copy_bundled("ipc", _IPC_COMPONENT, digest, target):
                    notes.append("IPC 使用内置签名表")
                else:
                    missing.append(("ipc", _IPC_COMPONENT, target))
        else:
            missing.append(("ipc", _IPC_COMPONENT, Path(_COMPONENTS[_IPC_COMPONENT])))

        if missing:
            # Local generation first (fast, no dead-mirror timeouts).
            ok, message = LocalPatternGenerator(self._steam_path).generate_missing(force=self._force)
            logger.info("Local signature generation: %s (%s)", ok, message)
            notes.append(message)
            if not ok:
                # Last resort: remote mirrors.  Community tables are sometimes
                # more complete than local partial output.
                remote_ok, remote_msg = self._fetch_missing(missing)
                notes.append(remote_msg)
                if not remote_ok:
                    return False, "；".join(n for n in notes if n)
            missing = [item for item in missing if not self._ready(item[2])]

        core_missing = [
            str(target)
            for channel, component, target in missing
            if channel == "ipc" or component == "steamclient"
        ]
        if core_missing:
            return False, "缺少关键 Steam 签名文件：" + "；".join(core_missing)

        if missing:
            notes.insert(0, "SteamUI 签名缺失（仅影响库界面 hook，核心解锁可用）")
        return True, "；".join(n for n in notes if n) or "Steam 签名表已就绪"

    def missing_patterns(self) -> list[str]:
        """Return only *critical* missing tables (steamclient pattern + IPC)."""
        missing: list[str] = []
        dll_path = self._steam_path / _COMPONENTS["steamclient"]
        if not dll_path.is_file():
            return [str(dll_path)]
        digest = self._sha256(dll_path)
        pattern = self._root / "pattern" / "steamclient" / f"{digest}.toml"
        ipc = self._root / "ipc" / "steamclient" / f"{digest}.toml"
        for target in (pattern, ipc):
            if not self._ready(target):
                missing.append(str(target))
        return missing

    # ── helpers ───────────────────────────────────────────────────

    @staticmethod
    def _ready(path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    def _copy_bundled(self, channel: str, component: str, digest: str, target: Path) -> bool:
        source = bundled_resource("steam_patterns", channel, component, f"{digest}.toml")
        if not self._ready(source):
            return False
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            return True
        except OSError as exc:
            logger.warning("Could not copy bundled signature %s: %s", source, exc)
            return False

    def _fetch_missing(self, missing: list[tuple[str, str, Path]]) -> tuple[bool, str]:
        templates = self._url_templates()
        if not templates:
            return False, "没有可用的远程签名镜像"
        proxy = self._get_system_proxy()
        fetched = 0
        failed: list[str] = []
        try:
            with httpx.Client(
                timeout=6.0,
                follow_redirects=True,
                proxy=proxy,
                verify=SSL_VERIFY,
                headers={"User-Agent": "OpenSteamToolDesktop/1.2"},
            ) as client:
                for channel, component, target in missing:
                    if not target.name.endswith(".toml"):
                        failed.append(str(target))
                        continue
                    digest = target.stem
                    body = self._fetch_one(client, templates, channel, component, digest)
                    if body is None:
                        failed.append(f"{channel}/{component}/{digest[:8]}")
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(body)
                    fetched += 1
        except (httpx.HTTPError, OSError) as exc:
            return False, f"远程签名下载失败：{exc}"
        if failed:
            return False, "远程镜像未提供：" + "，".join(failed)
        return True, f"已从远程镜像补全 {fetched} 个签名表"

    def _url_templates(self) -> list[str]:
        templates = list(STEAM_PATTERN_URL_TEMPLATES)
        try:
            configured = get_value(self._steam_path, "remote", "url_template", [])
        except Exception:  # noqa: BLE001 - config is optional
            configured = []
        if isinstance(configured, str):
            configured = [configured]
        if isinstance(configured, list):
            for item in configured:
                if isinstance(item, str) and item not in templates:
                    templates.insert(0, item)
        return [t for t in templates if "{channel}" in t and "{component}" in t and "{sha256}" in t]

    @staticmethod
    def _fetch_one(
        client: httpx.Client,
        templates: list[str],
        channel: str,
        component: str,
        sha256: str,
    ) -> bytes | None:
        for template in templates:
            url = template.format(channel=channel, component=component, sha256=sha256)
            try:
                response = client.get(url)
            except httpx.HTTPError as exc:
                logger.debug("Signature URL failed: %s (%s)", url, exc)
                continue
            if response.status_code != 200 or not response.content:
                logger.debug("Signature URL returned HTTP %s: %s", response.status_code, url)
                continue
            head = response.content.lstrip()[:1]
            if head not in (b"[", b"#"):
                logger.debug("Signature URL returned non-TOML content: %s", url)
                continue
            return response.content
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
