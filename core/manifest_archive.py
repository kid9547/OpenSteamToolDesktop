"""社区 AppID 分支归档清单客户端（ManifestAutoUpdate 风格）。

为什么需要它
------------
项目旧实现先从 Steam 官方 API 解析出「当前 public 分支的最新 GID」，
再拿这个 GID 去社区仓库拼 URL 下载，通常必然 404——因为社区仓库保存的是
入库当时的历史清单版本，GID 与官方最新值不同。

ManifestAutoUpdate 生态采用「一个 AppID 一个 git 分支」的归档布局：

    <repo>/<appid>/config.json          该游戏的 depot / dlc 结构
    <repo>/<appid>/Key.vdf              depot 解密密钥
    <repo>/<appid>/appinfo.vdf          appinfo 快照
    <repo>/<appid>/{depot}_{gid}.manifest   真实存在的清单文件

因此以归档为基础可以做到 100% 自洽：落地到 depotcache 的文件名、
Key.vdf 中的密钥、以及写进 Lua 的 ``setManifestid(depot, gid)``
全部来自同一份归档，不会再出现「下载成功但 Steam 仍报清单缺失」。

2026-10 实测（本机，经系统代理 http://127.0.0.1:7897）：

* ``https://raw.githubusercontent.com/Auiowu/ManifestAutoUpdate/730/config.json`` → 200
* ``https://codeload.github.com/Auiowu/ManifestAutoUpdate/zip/refs/heads/730`` → 200（2.6MB zip）
* ``https://ghfast.top/https://github.com/.../730.zip`` → 200
* ``https://ghproxy.net/https://github.com/.../730.zip`` → 200
* ``https://cdn.jsdelivr.net/gh/Auiowu/ManifestAutoUpdate@730/<file>`` → 200
* ``https://api.github.com/repos/Auiowu/ManifestAutoUpdate/git/trees/730`` → 200（轻量索引）
* ``P-ToyStore/SteamManifestCache_Pro`` → 404（仓库已不存在，不要再使用）

本模块只负责「取回并解析归档」，不负责写盘；写盘与 Lua 同步见
:mod:`core.complete_manifest_service`。
"""
from __future__ import annotations

import io
import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

import httpx

from config import (
    GITHUB_ARCHIVE_MIRRORS,
    GITHUB_RAW_MIRRORS,
    MANIFEST_ARCHIVE_REPOS,
    MANIFEST_ARCHIVE_TIMEOUT,
    SSL_VERIFY,
)
from core.manifest_downloader import STEAM_MANIFEST_MAGIC, ManifestDownloader
from utils.http_client import get_system_proxy
from utils.logger import setup_logger

logger = setup_logger(__name__)

# {depot}_{gid}.manifest —— 归档内清单文件命名规则
MANIFEST_NAME_RE = re.compile(r"^(?P<depot>\d+)_(?P<gid>\d+)\.manifest$", re.IGNORECASE)

# Key.vdf： "depot_id" { "DecryptionKey" "hex" }
_KEY_VDF_ENTRY_RE = re.compile(
    r'"?(?P<depot>\d+)"?\s*\{\s*(?:[^{}]*?)"DecryptionKey"\s*"?(?P<key>[0-9a-fA-F]{16,})"?',
    re.IGNORECASE | re.DOTALL,
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# jsDelivr 的 GitHub 路径形态与其它 raw 镜像不同，需要单独拼接
_JSDELIVR_PREFIX = "https://cdn.jsdelivr.net/gh"


def build_branch_raw_urls(
    repo: str,
    app_id: str,
    relative_path: str,
    mirrors: Iterable[str] | None = None,
) -> list[tuple[str, str]]:
    """构建分支内单个文件的 raw 候选 URL（含镜像回退顺序）。

    Args:
        repo: ``owner/name`` 形式的仓库
        app_id: 分支名（即 AppID）
        relative_path: 分支内相对路径，如 ``730_123.manifest``
        mirrors: 自定义镜像前缀列表，默认取 :data:`config.GITHUB_RAW_MIRRORS`

    Returns:
        ``[(来源标签, URL), ...]``，按可用性优先级排序
    """
    prefixes = list(mirrors) if mirrors is not None else list(GITHUB_RAW_MIRRORS)
    urls: list[tuple[str, str]] = []
    for prefix in prefixes:
        if prefix.rstrip("/") == _JSDELIVR_PREFIX:
            url = f"{_JSDELIVR_PREFIX}/{repo}@{app_id}/{relative_path}"
            label = "jsDelivr"
        elif prefix.rstrip("/") == "https://raw.githubusercontent.com":
            url = f"https://raw.githubusercontent.com/{repo}/{app_id}/{relative_path}"
            label = "GitHub Raw"
        else:
            url = f"{prefix.rstrip('/')}/{repo}/{app_id}/{relative_path}"
            label = prefix.rstrip("/").split("/")[2] if "//" in prefix else prefix
        urls.append((label, url))
    return urls


def build_archive_urls(
    repo: str,
    app_id: str,
    mirrors: Iterable[str] | None = None,
) -> list[tuple[str, str]]:
    """构建「AppID 分支整包 zip」的候选 URL（含镜像回退顺序）。"""
    direct = f"https://github.com/{repo}/archive/refs/heads/{app_id}.zip"
    prefixes = list(mirrors) if mirrors is not None else list(GITHUB_ARCHIVE_MIRRORS)
    urls: list[tuple[str, str]] = []
    for prefix in prefixes:
        if not prefix:
            urls.append(("GitHub Archive", direct))
        else:
            urls.append((prefix.strip("/").split("/")[2], f"{prefix.rstrip('/')}/{direct}"))
    return urls


@dataclass
class ArchiveManifest:
    """归档中单个 depot 的清单文件"""

    depot_id: str
    gid: str
    data: bytes
    size: int = 0

    def __post_init__(self) -> None:
        if not self.size:
            self.size = len(self.data)

    @property
    def filename(self) -> str:
        return f"{self.depot_id}_{self.gid}.manifest"

    @property
    def has_valid_magic(self) -> bool:
        return self.data[:4] == STEAM_MANIFEST_MAGIC


@dataclass
class ManifestArchive:
    """一次分支归档的解析结果"""

    app_id: str
    repo: str = ""
    source_url: str = ""
    manifests: list[ArchiveManifest] = field(default_factory=list)
    depot_keys: dict[str, str] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    appinfo: bytes = b""
    ok: bool = False
    message: str = ""

    @property
    def gid_map(self) -> dict[str, str]:
        """``{depot_id: gid}`` —— 归档内真实存在的 GID，可直接写入 Lua。"""
        return {item.depot_id: item.gid for item in self.manifests}

    @property
    def depot_ids(self) -> list[str]:
        """归档声明的 depot 列表（含没有清单文件的 depot）。"""
        declared = self.config.get("depots") if isinstance(self.config, dict) else None
        ids = [str(d) for d in declared] if isinstance(declared, list) else []
        for item in self.manifests:
            if item.depot_id not in ids:
                ids.append(item.depot_id)
        return ids

    @property
    def dlc_ids(self) -> list[str]:
        declared = self.config.get("dlcs") if isinstance(self.config, dict) else None
        return [str(d) for d in declared] if isinstance(declared, list) else []

    def get(self, depot_id: str) -> ArchiveManifest | None:
        for item in self.manifests:
            if item.depot_id == str(depot_id):
                return item
        return None


class ManifestArchiveClient:
    """分支归档下载与解析客户端（网络层可注入，便于离线测试）。

    用法::

        client = ManifestArchiveClient()
        archive = client.fetch_appid_archive("730")
        if archive.ok:
            for item in archive.manifests:
                print(item.filename, item.size)
    """

    def __init__(
        self,
        repos: list[str] | None = None,
        timeout: float = MANIFEST_ARCHIVE_TIMEOUT,
        http: httpx.Client | None = None,
        direct_http: httpx.Client | None = None,
        retries: int = 1,
    ) -> None:
        self._repos = list(repos) if repos else list(MANIFEST_ARCHIVE_REPOS)
        self._timeout = timeout
        self._retries = max(0, retries)
        self._owns_http = http is None
        self._owns_direct = direct_http is None

        self._http = http or httpx.Client(
            proxy=get_system_proxy(),
            headers={"User-Agent": _UA},
            timeout=timeout,
            follow_redirects=True,
            verify=SSL_VERIFY,
        )
        # 公共加速镜像通常不需要走代理，单独直连可显著提速
        self._direct_http = direct_http or httpx.Client(
            headers={"User-Agent": _UA},
            timeout=timeout,
            follow_redirects=True,
            verify=SSL_VERIFY,
        )
        # 记录已被证伪的 (appid, repo) 组合，避免同一进程内反复重试
        self._missing_repos: set[tuple[str, str]] = set()

    # ── 资源管理 ──────────────────────────────────────────

    def close(self) -> None:
        """释放自建的 HTTP 客户端资源"""
        if self._owns_http:
            try:
                self._http.close()
            except Exception:  # pragma: no cover - 关闭失败不影响业务
                pass
        if self._owns_direct:
            try:
                self._direct_http.close()
            except Exception:  # pragma: no cover
                pass

    def __enter__(self) -> "ManifestArchiveClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ── 静态解析（纯函数，离线可测）────────────────────────

    @staticmethod
    def parse_key_vdf(text: str) -> dict[str, str]:
        """解析 Key.vdf / key.vdf 文本为 ``{depot_id: 32 字节 hex 密钥}``。"""
        if not text:
            return {}
        keys: dict[str, str] = {}
        for match in _KEY_VDF_ENTRY_RE.finditer(text):
            depot = match.group("depot")
            key = match.group("key").strip().lower()
            if depot and key:
                keys[depot] = key
        return keys

    @classmethod
    def parse_archive(
        cls,
        app_id: str,
        payload: bytes,
        repo: str = "",
        source_url: str = "",
    ) -> ManifestArchive:
        """在内存中解析分支归档 zip。

        只接受真正的 ZIP 载荷；归档中每个 ``{depot}_{gid}.manifest``
        都会做魔数校验，校验失败或无法解压的条目会被丢弃并记日志。
        """
        result = ManifestArchive(app_id=str(app_id), repo=repo, source_url=source_url)
        if not payload or payload[:2] != b"PK":
            result.message = "不是有效的 ZIP 归档"
            return result

        try:
            with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    base_name = member.filename.rsplit("/", 1)[-1]
                    lower = base_name.lower()

                    if lower == "config.json":
                        result.config = cls._parse_config_json(archive.read(member))
                        continue
                    if lower in ("key.vdf", "keys.vdf"):
                        text = archive.read(member).decode("utf-8", "replace")
                        result.depot_keys.update(cls.parse_key_vdf(text))
                        continue
                    if lower == "appinfo.vdf":
                        result.appinfo = archive.read(member)
                        continue

                    match = MANIFEST_NAME_RE.match(base_name)
                    if not match:
                        continue

                    data = ManifestDownloader._extract_manifest_payload(archive.read(member))
                    if data is None or data[:4] != STEAM_MANIFEST_MAGIC:
                        logger.warning(
                            "归档 %s 中的 %s 魔数校验失败，已忽略", app_id, base_name
                        )
                        continue

                    result.manifests.append(
                        ArchiveManifest(
                            depot_id=match.group("depot"),
                            gid=match.group("gid"),
                            data=data,
                        )
                    )
        except (zipfile.BadZipFile, OSError, ValueError) as exc:
            result.message = f"归档解析失败: {exc}"
            logger.debug("parse_archive failed for %s: %s", app_id, exc)
            return result

        if not result.manifests:
            result.message = "归档中未找到任何有效清单文件"
            return result

        result.ok = True
        result.message = (
            f"从归档解析出 {len(result.manifests)} 个清单、"
            f"{len(result.depot_keys)} 个 depot 密钥"
        )
        return result

    @staticmethod
    def _parse_config_json(raw: bytes) -> dict[str, Any]:
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    @staticmethod
    def parse_branch_tree(app_id: str, tree_json: Any, repo: str = "") -> ManifestArchive:
        """解析 GitHub ``git/trees/<branch>`` 轻量索引为归档清单列表。

        该接口只返回文件清单（含大小），不返回文件内容，用于在不下载
        整包的前提下确认某分支到底保存了哪些 ``{depot}_{gid}.manifest``。
        """
        result = ManifestArchive(app_id=str(app_id), repo=repo)
        entries = tree_json.get("tree") if isinstance(tree_json, dict) else None
        if not isinstance(entries, list):
            result.message = "索引格式无效"
            return result
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path", ""))
            base_name = path.rsplit("/", 1)[-1]
            match = MANIFEST_NAME_RE.match(base_name)
            if not match:
                continue
            result.manifests.append(
                ArchiveManifest(
                    depot_id=match.group("depot"),
                    gid=match.group("gid"),
                    data=b"",
                    size=int(entry.get("size") or 0),
                )
            )
        result.ok = bool(result.manifests)
        result.message = f"索引中共 {len(result.manifests)} 个清单条目"
        return result

    # ── 网络层 ────────────────────────────────────────────

    def _get(
        self,
        url: str,
        client: httpx.Client | None = None,
        timeout: float | None = None,
    ) -> httpx.Response | None:
        """带重试与异常兜底的 GET；失败返回 ``None``。"""
        target = client or self._http
        last_error = ""
        for attempt in range(self._retries + 1):
            try:
                resp = target.get(url, timeout=timeout or self._timeout)
                return resp
            except Exception as exc:  # httpx 的异常家族较宽，统一兜底
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self._retries:
                    time.sleep(0.4 * (attempt + 1))
        logger.debug("请求失败 %s (%s)", url, last_error)
        return None

    def probe_branch(self, app_id: str, repo: str) -> bool:
        """用极小的 ``config.json`` 探测某个仓库是否存在该 AppID 分支。"""
        key = (str(app_id), repo)
        if key in self._missing_repos:
            return False
        for _label, url in build_branch_raw_urls(repo, str(app_id), "config.json")[:2]:
            resp = self._get(url, timeout=8.0)
            if resp is not None and resp.status_code == 200 and resp.content[:1] == b"{":
                return True
        self._missing_repos.add(key)
        return False

    def fetch_branch_config(self, app_id: str, repo: str) -> dict[str, Any] | None:
        """拉取分支的 config.json（depot / dlc 结构）。"""
        for _label, url in build_branch_raw_urls(repo, str(app_id), "config.json"):
            resp = self._get(url, timeout=8.0)
            if resp is not None and resp.status_code == 200:
                config = self._parse_config_json(resp.content)
                if config:
                    return config
        return None

    def fetch_appid_archive(self, app_id: str, repos: list[str] | None = None) -> ManifestArchive:
        """下载并解析 AppID 分支整包归档。

        流程：先用 config.json 探测哪个仓库真的有该分支（1 个小请求），
        再把整包 zip 依次走 GitHub 直链 / ghfast / ghproxy 下载并解析。
        全部失败时返回 ``ok=False`` 的结果对象，不会抛异常。
        """
        app_id = str(app_id)
        candidates = list(repos) if repos else list(self._repos)
        any_repo_found = False

        for repo in candidates:
            if not self.probe_branch(app_id, repo):
                logger.debug("仓库 %s 不存在 AppID %s 分支", repo, app_id)
                continue
            any_repo_found = True

            for label, url in build_archive_urls(repo, app_id):
                resp = self._get(url)
                if resp is None or resp.status_code != 200:
                    logger.debug(
                        "归档下载失败 (%s) %s -> %s",
                        label,
                        url,
                        getattr(resp, "status_code", "exception"),
                    )
                    continue
                if resp.content[:2] != b"PK":
                    logger.debug("%s 返回的不是 ZIP 归档: %s", label, url)
                    continue

                archive = self.parse_archive(app_id, resp.content, repo=repo, source_url=url)
                if archive.ok:
                    logger.info(
                        "AppID %s 分支归档就绪：%d 个清单、%d 个密钥（来源 %s）",
                        app_id,
                        len(archive.manifests),
                        len(archive.depot_keys),
                        label,
                    )
                    return archive
                logger.debug("归档解析无有效清单 (%s) %s", label, url)

        result = ManifestArchive(app_id=app_id)
        if not any_repo_found:
            result.message = "所有社区仓库均无此 AppID 分支"
        else:
            result.message = "找到分支但归档下载或解析失败"
        return result

    def fetch_keys(self, app_id: str, repos: list[str] | None = None) -> dict[str, str]:
        """只取分支的 Key.vdf（depot 解密密钥），不下载清单整包。"""
        app_id = str(app_id)
        for repo in (list(repos) if repos else list(self._repos)):
            if not self.probe_branch(app_id, repo):
                continue
            for _label, url in build_branch_raw_urls(repo, app_id, "Key.vdf"):
                resp = self._get(url, timeout=10.0)
                if resp is not None and resp.status_code == 200:
                    keys = self.parse_key_vdf(resp.content.decode("utf-8", "replace"))
                    if keys:
                        return keys
        return {}

    def fetch_single_manifest(
        self,
        app_id: str,
        depot_id: str,
        gid: str,
        repos: list[str] | None = None,
    ) -> ArchiveManifest | None:
        """按已知 ``(depot, gid)`` 精确拉取分支内的单个清单文件。

        这是最轻量的「分支归档」取法（188 字节级的请求即可完成），
        只在已经确定 GID 正确时使用；GID 未知时应改用整包归档。
        """
        app_id, depot_id, gid = str(app_id), str(depot_id), str(gid)
        filename = f"{depot_id}_{gid}.manifest"
        for repo in (list(repos) if repos else list(self._repos)):
            for label, url in build_branch_raw_urls(repo, app_id, filename):
                resp = self._get(url, timeout=12.0)
                if resp is None or resp.status_code != 200:
                    continue
                data = ManifestDownloader._extract_manifest_payload(resp.content)
                if data is None or data[:4] != STEAM_MANIFEST_MAGIC:
                    continue
                logger.debug("单文件清单命中 %s (%s)", filename, label)
                return ArchiveManifest(depot_id=depot_id, gid=gid, data=data)
        return None

    def iter_repos(self) -> Iterator[str]:
        """按优先级遍历配置的社区仓库。"""
        return iter(self._repos)
