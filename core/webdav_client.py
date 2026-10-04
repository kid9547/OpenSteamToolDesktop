"""轻量 WebDAV 客户端 —— 用于存档备份的联网同步。

只实现存档备份需要的最小操作集：连接测试、建目录、上传、列目录、下载。
兼容坚果云 / Nextcloud / 群晖等常见 WebDAV 服务。

多设备 / 多账号约定：

* 远端目录结构固定为 ``<base>/OpenSteamToolDesktop/saves/<设备名>/``，
  设备名默认取主机名，避免多台设备互相覆盖；
* 备份 zip 内部保留 ``userdata/<steamid>/<appid>/remote/...`` 结构，
  因此一个 zip 可以同时包含多个 Steam 账号的存档，恢复时按原结构落盘。
"""
from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import httpx

from config import SSL_VERIFY
from utils.http_client import get_system_proxy
from utils.logger import setup_logger

logger = setup_logger(__name__)


class WebDavError(RuntimeError):
    pass


@dataclass
class RemoteFile:
    """PROPFIND 解析出的远端文件/目录"""

    name: str
    path: str  # 完整 URL
    size: int
    is_dir: bool


def default_device_name() -> str:
    """当前设备名（多设备隔离用），非法字符替换为下划线。"""
    name = socket.gethostname() or "PC"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "PC"


class WebDavClient:
    """最小可用的 WebDAV 客户端（httpx 实现）。"""

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        timeout: float = 30.0,
    ):
        base_url = (base_url or "").strip()
        if not base_url.startswith(("http://", "https://")):
            raise WebDavError("WebDAV 地址必须以 http:// 或 https:// 开头")
        if not base_url.endswith("/"):
            base_url += "/"
        self.base_url = base_url
        self.username = username.strip()
        self.password = password
        self.timeout = timeout
        self._client = httpx.Client(
            auth=(self.username, self.password) if self.username else None,
            proxy=get_system_proxy(),
            timeout=timeout,
            follow_redirects=True,
            verify=SSL_VERIFY,
            headers={"User-Agent": "OpenSteamToolDesktop-SaveBackup/1.0"},
        )

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "WebDavClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ── 内部工具 ──────────────────────────────────────────
    def _url(self, remote_path: str) -> str:
        """把 base_url 下的相对路径转成完整 URL（逐段编码，保留 /）。"""
        remote_path = (remote_path or "").strip().lstrip("/")
        if remote_path.startswith(("http://", "https://")):
            return remote_path
        base = urlparse(self.base_url)
        prefix = base.path.rstrip("/")
        segments = [quote(seg) for seg in remote_path.split("/") if seg]
        return f"{base.scheme}://{base.netloc}{prefix}/" + "/".join(segments)

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            return self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise WebDavError(f"WebDAV 请求失败（{method}）: {exc}") from exc

    @staticmethod
    def _raise_for_status(resp: httpx.Response, action: str) -> None:
        if resp.status_code // 100 == 2:
            return
        hint = {
            401: "账号或密码错误（401）",
            403: "没有权限（403）",
            404: "路径不存在（404），请确认服务器地址包含 WebDAV 目录",
        }.get(resp.status_code, f"HTTP {resp.status_code}")
        raise WebDavError(f"WebDAV {action}失败：{hint}")

    # ── 公开操作 ──────────────────────────────────────────
    def test_connection(self) -> str:
        """连通性测试，返回服务器的展示信息。"""
        resp = self._request("PROPFIND", self.base_url, headers={"Depth": "0"})
        if resp.status_code == 401:
            raise WebDavError("连接成功但鉴权失败：请检查账号密码（401）")
        self._raise_for_status(resp, "连接")
        return f"连接成功：{self.base_url}"

    def ensure_dir(self, remote_dir: str) -> None:
        """逐级创建远端目录（已存在时忽略 405）。"""
        remote_dir = remote_dir.strip().lstrip("/")
        if not remote_dir:
            return
        current = ""
        for segment in remote_dir.split("/"):
            if not segment:
                continue
            current = f"{current}/{segment}" if current else segment
            resp = self._request("MKCOL", self._url(current))
            if resp.status_code // 100 != 2 and resp.status_code not in (405, 301, 409):
                self._raise_for_status(resp, f"创建目录 {current}")

    def upload(self, local_path: str | Path, remote_path: str) -> str:
        """上传本地文件到远端路径（自动创建父目录），返回完整 URL。"""
        local = Path(local_path)
        if not local.is_file():
            raise WebDavError(f"本地文件不存在：{local}")
        url = self._url(remote_path)
        parent = remote_path.rsplit("/", 1)[0] if "/" in remote_path.strip("/") else ""
        if parent:
            self.ensure_dir(parent)
        data = local.read_bytes()
        resp = self._request("PUT", url, content=data, headers={"Content-Length": str(len(data))})
        self._raise_for_status(resp, f"上传 {Path(remote_path).name}")
        logger.info("WebDAV 上传完成：%s（%d 字节）", url, len(data))
        return url

    def list_dir(self, remote_dir: str = "") -> list[RemoteFile]:
        """列出一个目录（Depth 1），返回文件与子目录列表。"""
        url = self._url(remote_dir)
        resp = self._request("PROPFIND", url, headers={"Depth": "1"})
        if resp.status_code == 404:
            return []
        self._raise_for_status(resp, f"列出 {remote_dir or '/'}")

        xml = resp.text
        results: list[RemoteFile] = []
        parsed_base = urlparse(self.base_url)
        base_path = parsed_base.path.rstrip("/")
        request_path = urlparse(url).path.rstrip("/")
        for block in re.findall(r"<(?:[\w.-]+:)?response\b.*?</(?:[\w.-]+:)?response>", xml, re.S | re.I):
            href_match = re.search(r"<(?:[\w.-]+:)?href>\s*([^<]+?)\s*</", block, re.I)
            if not href_match:
                continue
            href = unquote(href_match.group(1))
            if href.startswith("http"):
                path_part = urlparse(href).path
            else:
                path_part = href
            if path_part.rstrip("/") == request_path:
                continue  # 跳过目录自身
            name = path_part.rstrip("/").rsplit("/", 1)[-1]
            is_dir = bool(re.search(r"<(?:[\w.-]+:)?collection\s*/?>", block, re.I))
            size_match = re.search(r"<(?:[\w.-]+:)?getcontentlength>\s*(\d+)\s*</", block, re.I)
            if href.startswith("http"):
                full_url = href
            elif path_part.startswith(base_path + "/") or path_part.rstrip("/") == base_path:
                # 服务器返回的是绝对路径（已含 base 前缀），不要重复拼接
                full_url = f"{parsed_base.scheme}://{parsed_base.netloc}{path_part}"
            else:
                full_url = self._url(path_part)
            results.append(
                RemoteFile(
                    name=name,
                    path=full_url,
                    size=int(size_match.group(1)) if size_match else 0,
                    is_dir=is_dir,
                )
            )
        return results

    def download(self, remote_path: str, local_path: str | Path) -> Path:
        """下载远端文件到本地路径。"""
        url = self._url(remote_path)
        resp = self._request("GET", url)
        self._raise_for_status(resp, f"下载 {Path(remote_path).name}")
        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(resp.content)
        logger.info("WebDAV 下载完成：%s -> %s", url, local)
        return local

    def default_backup_dir(self, device: str | None = None) -> str:
        """远端备份目录：``OpenSteamToolDesktop/saves/<设备名>``。"""
        return f"OpenSteamToolDesktop/saves/{device or default_device_name()}"
