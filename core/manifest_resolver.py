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

    def diagnose_app(self, app_id: str) -> ManifestReadiness:
        """诊断指定 AppID 的游戏清单就绪情况（基于其 Lua 配置文件）"""
        lua_path = os.path.join(self.lua_dir, f"{app_id}.lua") if self.lua_dir else ""
        return self.diagnose_lua_file(lua_path)

    def update_lua_manifest(self, app_id: str, depot_id: str, manifest_gid: str, size: int = 0) -> bool:
        """在游戏的 Lua 配置文件中更新或追加 setManifestid 绑定"""
        if not self.lua_dir:
            return False
        lua_path = os.path.join(self.lua_dir, f"{app_id}.lua")
        if not os.path.isfile(lua_path):
            return False
        try:
            content = Path(lua_path).read_text(encoding="utf-8", errors="replace")
            pattern = rf'setmanifestid\s*\(\s*{depot_id}\s*,\s*["\']?\d+["\']?(?:\s*,\s*[^)]*)?\)'
            new_line = (
                f'setManifestid({depot_id}, "{manifest_gid}", {size})'
                if size > 0 else
                f'setManifestid({depot_id}, "{manifest_gid}")'
            )
            if re.search(pattern, content, re.IGNORECASE):
                new_content = re.sub(pattern, new_line, content, flags=re.IGNORECASE)
            else:
                new_content = content.rstrip() + f"\n{new_line}\n"
            Path(lua_path).write_text(new_content, encoding="utf-8")
            logger.info(f"Updated {lua_path} with {new_line}")
            return True
        except Exception as e:
            logger.warning(f"Failed to update Lua manifest in {lua_path}: {e}")
            return False

    def resolve_manifests(
        self,
        app_id: str,
        depots: list[Any] | None = None,
        dlc_ids: list[str] | None = None,
    ) -> tuple[bool, str, int]:
        """为指定游戏执行自动清单解析与多源补全流水线

        Args:
            app_id: 游戏 AppID
            depots: [(depot_id, manifest_gid, size, [owner_app_id]), ...]（可选）
            dlc_ids: DLC AppID 列表（可选）

        Returns:
            (is_ready: 是否全部就绪, message: 说明信息, downloaded_count: 本次成功下载清单数)
        """
        if not self._steam_path or not os.path.isdir(self._steam_path):
            return False, "Steam 安装路径无效", 0

        if self.depotcache_dir:
            os.makedirs(self.depotcache_dir, exist_ok=True)
        if self.config_depotcache_dir:
            os.makedirs(self.config_depotcache_dir, exist_ok=True)

        downloaded_count = 0
        lua_path = os.path.join(self.lua_dir, f"{app_id}.lua") if self.lua_dir else ""

        # 步骤 1: 整理待获取的 depot 列表
        target_depots: list[tuple[str, str, int, str]] = []

        if depots:
            for item in depots:
                if not item:
                    continue
                did = str(item[0]) if len(item) > 0 else ""
                gid = str(item[1]) if len(item) > 1 and item[1] is not None else ""
                size = int(item[2]) if len(item) > 2 and item[2] else 0
                owner = str(item[3]) if len(item) > 3 and item[3] else app_id
                if did:
                    target_depots.append((did, gid, size, owner))

        # 若未提供 depots 或 Lua 已存在，从 Lua 文件补充可能缺失的条目
        if os.path.isfile(lua_path):
            try:
                lua_content = Path(lua_path).read_text(encoding="utf-8", errors="replace")
                # 提取已有的 setManifestid
                matches = re.findall(
                    r'setmanifestid\s*\(\s*(\d+)\s*,\s*["\']?(\d+)["\']?(?:\s*,\s*(\d+))?\)',
                    lua_content,
                    re.IGNORECASE,
                )
                existing_dids = {d[0] for d in target_depots}
                for did, gid, sz in matches:
                    if did not in existing_dids:
                        target_depots.append((did, gid, int(sz) if sz else 0, app_id))
                        existing_dids.add(did)

                # 提取 addappid 列表作为候选 depot
                add_matches = re.findall(r'addappid\s*\(\s*(\d+)', lua_content, re.IGNORECASE)
                for did in add_matches:
                    if did != app_id and did not in existing_dids:
                        target_depots.append((did, "", 0, app_id))
                        existing_dids.add(did)
            except Exception as e:
                logger.debug(f"Error parsing Lua {lua_path}: {e}")

        # 步骤 2: 检查哪些清单已就绪
        missing_depots = [
            d for d in target_depots
            if not d[1] or not self.check_manifest_exists(d[0], d[1])
        ]

        if not missing_depots and target_depots:
            return True, "游戏清单文件已全部就绪", 0

        # 步骤 3: 使用多源下载器下载缺失的清单
        depots_with_gid = [d for d in missing_depots if d[1]]
        if depots_with_gid:
            try:
                downloader = ManifestDownloader(self._steam_path)
                batch_res = downloader.download_manifests(depots_with_gid, app_id=app_id)
                downloaded_count += batch_res.success
                downloader.close()
            except Exception as e:
                logger.warning(f"Batch manifest download error for {app_id}: {e}")

        # 步骤 4: 对于仍缺少 manifest_gid 或未成功下载的 depot，尝试检索匹配 Tag
        still_missing = [
            d for d in target_depots
            if not d[1] or not self.check_manifest_exists(d[0], d[1])
        ]

        if still_missing:
            downloader = ManifestDownloader(self._steam_path)
            for did, gid, size, owner in still_missing:
                # 尝试从 P-ToyStore 的 matching-refs 检索可用清单版本
                found_gid = self._lookup_tag_gid_for_depot(did)
                if found_gid:
                    dl_res = downloader._download_single(
                        depot_id=did,
                        manifest_gid=found_gid,
                        size=size,
                        cdn_hosts=[],
                        app_id=app_id,
                        owner_app_id=owner,
                    )
                    if dl_res.success:
                        downloaded_count += 1
                        # 自动将发现的清单 GID 同步写回 Lua 文件
                        self.update_lua_manifest(app_id, did, found_gid, size)
            downloader.close()

        # 步骤 5: 尝试从社区 zip 镜像补充
        still_missing_after = [
            d for d in target_depots
            if not d[1] or not self.check_manifest_exists(d[0], d[1])
        ]
        if still_missing_after:
            mirror_success, extracted = self._fetch_from_mirrors(app_id)
            if mirror_success and extracted > 0:
                downloaded_count += extracted

        # 步骤 6: 最终诊断判断就绪状态
        if os.path.isfile(lua_path):
            diag = self.diagnose_lua_file(lua_path)
            if diag.is_ready:
                return True, f"游戏清单已成功自动补全（本次下载 {downloaded_count} 个），Steam 中可直接下载！", downloaded_count
            else:
                return (
                    False,
                    f"未能自动获取全部清单（已下载 {downloaded_count} 个），尚缺少: {', '.join(diag.missing_manifests[:3])}",
                    downloaded_count,
                )

        remaining_missing = [
            f"{d[0]}_{d[1]}"
            for d in target_depots
            if d[1] and not self.check_manifest_exists(d[0], d[1])
        ]
        if not remaining_missing:
            return True, f"游戏清单已全部就绪！（本次下载 {downloaded_count} 个）", downloaded_count

        return (
            False,
            f"未能自动获取全部清单（已下载 {downloaded_count} 个），尚缺少: {', '.join(remaining_missing[:3])}",
            downloaded_count,
        )

    def _lookup_tag_gid_for_depot(self, depot_id: str) -> str | None:
        """从 GitHub 仓库检索指定 Depot 的可用清单 GID"""
        url = f"https://api.github.com/repos/P-ToyStore/SteamManifestCache_Pro/git/matching-refs/tags/{depot_id}_"
        try:
            resp = self._http.get(url, timeout=8.0)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data:
                    best_gid = None
                    for item in data:
                        ref = item.get("ref", "")
                        tag = ref.split("/")[-1]
                        m = re.match(rf'^{depot_id}_(\d+)$', tag)
                        if m:
                            gid = m.group(1)
                            if best_gid is None or int(gid) > int(best_gid):
                                best_gid = gid
                    return best_gid
        except Exception:
            pass
        return None

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
