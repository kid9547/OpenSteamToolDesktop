"""
ManifestResolver — 清单解析与自动化获取服务
=============================================

解决核心痛点：
1. 游戏无 Access Token 时，Steam CDN 下载需要本地 depotcache 中的 .manifest 文件，否则报错 401。
2. 自动化获取流程：
   - 检查本地 depotcache 与 config/depotcache 是否已存在清单；
   - 尝试从公共社区镜像下载 manifest/zip 归档；
   - 尝试通过 Steam CDN 下载未受保护的 Depot 清单；
   - 实时回报清单就绪状态，杜绝“入库成功但无法下载”的误导；
3. 提供外部社区清单检索辅助与一键导入接口。
"""
from __future__ import annotations

import io
import os
import re
import shutil
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from config import SSL_VERIFY
from core.manifest_downloader import ManifestDownloader
from utils.http_client import get_system_proxy
from utils.logger import setup_logger

logger = setup_logger(__name__)

# 公共清单/配置文件镜像 API
COMMUNITY_MANIFEST_MIRRORS = [
    "https://steamtoolsapp.com/api/files/{app_id}/zip",
]


@dataclass
class ManifestReadiness:
    """清单就绪状态诊断结果"""
    app_id: str
    total_depots: int = 0
    ready_count: int = 0
    missing_count: int = 0
    is_ready: bool = True
    ready_manifests: list[str] = field(default_factory=list)
    missing_manifests: list[str] = field(default_factory=list)
    message: str = ""


class ManifestResolver:
    """清单解析与自动补全服务"""

    def __init__(self, steam_path: str = ""):
        self._steam_path = steam_path
        self._http = httpx.Client(
            proxy=get_system_proxy(),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "*/*",
            },
            timeout=15.0,
            follow_redirects=True,
            verify=SSL_VERIFY,
        )

    def close(self):
        """关闭 HTTP 客户端"""
        self._http.close()

    def set_steam_path(self, steam_path: str) -> None:
        self._steam_path = steam_path

    @property
    def depotcache_dir(self) -> str:
        return os.path.join(self._steam_path, "depotcache") if self._steam_path else ""

    @property
    def config_depotcache_dir(self) -> str:
        return os.path.join(self._steam_path, "config", "depotcache") if self._steam_path else ""

    @property
    def lua_dir(self) -> str:
        return os.path.join(self._steam_path, "config", "lua") if self._steam_path else ""

    def check_manifest_exists(self, depot_id: str, manifest_gid: str) -> bool:
        """检查特定 Depot 清单是否存在于 depotcache 或 config/depotcache"""
        filename = f"{depot_id}_{manifest_gid}.manifest"
        if self.depotcache_dir:
            p1 = os.path.join(self.depotcache_dir, filename)
            if os.path.isfile(p1) and os.path.getsize(p1) > 0:
                return True
        if self.config_depotcache_dir:
            p2 = os.path.join(self.config_depotcache_dir, filename)
            if os.path.isfile(p2) and os.path.getsize(p2) > 0:
                return True
        return False

    def diagnose_lua_file(self, lua_path: str) -> ManifestReadiness:
        """根据 Lua 配置文件内容诊断该游戏所需的清单就绪情况"""
        app_id = os.path.splitext(os.path.basename(lua_path))[0]
        if not os.path.isfile(lua_path):
            return ManifestReadiness(
                app_id=app_id,
                is_ready=False,
                message="Lua 配置文件不存在",
            )

        try:
            content = Path(lua_path).read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ManifestReadiness(
                app_id=app_id,
                is_ready=False,
                message=f"读取 Lua 配置文件失败: {e}",
            )

        # 匹配所有 setManifestid(depot_id, "manifest_gid", optional_size)
        matches = re.findall(
            r'setmanifestid\s*\(\s*(\d+)\s*,\s*["\']?(\d+)["\']?(?:\s*,\s*[^)]*)?\)',
            content,
            re.IGNORECASE,
        )

        ready_list = []
        missing_list = []

        for depot_id, gid in matches:
            ident = f"{depot_id}_{gid}"
            if self.check_manifest_exists(depot_id, gid):
                ready_list.append(ident)
            else:
                missing_list.append(ident)

        total = len(matches)
        if total == 0:
            return ManifestReadiness(
                app_id=app_id,
                total_depots=0,
                ready_count=0,
                missing_count=0,
                is_ready=True,
                message="无需特定清单文件",
            )

        all_ready = (len(missing_list) == 0)
        msg = "清单已就绪" if all_ready else f"缺少 {len(missing_list)}/{total} 个清单文件"

        return ManifestReadiness(
            app_id=app_id,
            total_depots=total,
            ready_count=len(ready_list),
            missing_count=len(missing_list),
            is_ready=all_ready,
            ready_manifests=ready_list,
            missing_manifests=missing_list,
            message=msg,
        )

    def resolve_manifests(
        self,
        app_id: str,
        depots: list[tuple[str, str, int]] | None = None,
    ) -> tuple[bool, str]:
        """为指定游戏执行自动清单解析与补全流水线"""
        if not self._steam_path or not os.path.isdir(self._steam_path):
            return False, "Steam 安装路径无效"

        if self.depotcache_dir:
            os.makedirs(self.depotcache_dir, exist_ok=True)
        if self.config_depotcache_dir:
            os.makedirs(self.config_depotcache_dir, exist_ok=True)

        # 步骤 1: 检查现有清单状态
        if depots:
            all_exist = all(
                self.check_manifest_exists(d[0], d[1])
                for d in depots
                if len(d) >= 2 and d[1]
            )
            if all_exist:
                return True, "游戏清单文件已全部就绪"

        # 步骤 2: 尝试从社区镜像获取 zip 包
        mirror_success, extracted_manifests = self._fetch_from_mirrors(app_id)
        if mirror_success and extracted_manifests > 0:
            logger.info(
                f"Successfully extracted {extracted_manifests} manifest(s) for AppID {app_id} from mirror"
            )

        # 步骤 3: 尝试从 Steam CDN 补充可能公开的清单
        if depots:
            missing_depots = [
                d for d in depots
                if len(d) >= 2 and d[1] and not self.check_manifest_exists(d[0], d[1])
            ]
            if missing_depots:
                try:
                    downloader = ManifestDownloader(self._steam_path)
                    downloader.download_manifests(missing_depots, app_id=app_id)
                    downloader.close()
                except Exception as e:
                    logger.debug(f"CDN download failed: {e}")

        # 步骤 4: 最终检查
        lua_path = os.path.join(self.lua_dir, f"{app_id}.lua") if self.lua_dir else ""
        if os.path.isfile(lua_path):
            diag = self.diagnose_lua_file(lua_path)
            if diag.is_ready:
                return True, "游戏清单已成功自动补全，Steam 中可直接下载！"
            else:
                return False, f"未能自动获取全部清单，尚缺少: {', '.join(diag.missing_manifests)}"

        if depots:
            remaining_missing = [
                f"{d[0]}_{d[1]}"
                for d in depots
                if len(d) >= 2 and d[1] and not self.check_manifest_exists(d[0], d[1])
            ]
            if not remaining_missing:
                return True, "游戏清单已全部就绪！"
            return False, f"未能自动获取清单，尚缺少: {', '.join(remaining_missing[:3])}"

        return True, "入库完成"

    def _fetch_from_mirrors(self, app_id: str) -> tuple[bool, int]:
        """尝试从公共镜像拉取游戏资源 ZIP 并部署清单"""
        manifests_extracted = 0

        for mirror_tmpl in COMMUNITY_MANIFEST_MIRRORS:
            url = mirror_tmpl.format(app_id=app_id)
            try:
                resp = self._http.get(url, timeout=12.0)
                if resp.status_code != 200 or len(resp.content) < 32:
                    continue

                try:
                    with zipfile.ZipFile(io.BytesIO(resp.content), "r") as zf:
                        for name in zf.namelist():
                            lower_name = name.lower()
                            base_name = os.path.basename(name)
                            if not base_name:
                                continue

                            if lower_name.endswith(".manifest"):
                                m = re.search(r'(\d+_\d+)\.manifest$', base_name, re.IGNORECASE)
                                final_name = f"{m.group(1)}.manifest" if m else base_name
                                payload = zf.read(name)

                                if self.depotcache_dir:
                                    t1 = os.path.join(self.depotcache_dir, final_name)
                                    with open(t1, "wb") as f:
                                        f.write(payload)

                                if self.config_depotcache_dir:
                                    t2 = os.path.join(self.config_depotcache_dir, final_name)
                                    with open(t2, "wb") as f:
                                        f.write(payload)

                                manifests_extracted += 1
                                logger.info(f"Extracted manifest {final_name} from mirror")

                            elif lower_name.endswith(".lua") and self.lua_dir:
                                local_lua = os.path.join(self.lua_dir, f"{app_id}.lua")
                                if not os.path.isfile(local_lua):
                                    with open(local_lua, "wb") as f:
                                        f.write(zf.read(name))
                                    logger.info(f"Deployed Lua from mirror: {local_lua}")

                    if manifests_extracted > 0:
                        return True, manifests_extracted

                except zipfile.BadZipFile:
                    continue

            except Exception as e:
                logger.debug(f"Mirror fetch error from {url}: {e}")
                continue

        return False, manifests_extracted

    @staticmethod
    def get_search_queries(app_id: str, game_name: str) -> dict[str, str]:
        """生成常用社区搜索 URL"""
        clean_name = re.sub(r'[^\w\s\u4e00-\u9fa5]', ' ', game_name).strip()
        query_keyword = f"{clean_name} {app_id} 清单 manifest".strip()
        encoded = urllib.parse.quote(query_keyword)

        return {
            "百度搜索": f"https://www.baidu.com/s?wd={encoded}",
            "Bilibili 教程与清单": f"https://search.bilibili.com/all?keyword={urllib.parse.quote(f'{clean_name} 清单')}",
            "GitHub Manifest 仓库": f"https://github.com/search?q={app_id}+manifest&type=repositories",
        }
