"""
ManifestResolver — 清单解析与自动化获取服务
=============================================

解决核心痛点：
1. 游戏无 Access Token 时，Steam CDN 下载需要本地 depotcache 中的 .manifest 文件，否则报错 401。
2. 自动化获取流程（归档优先，2026-10 重构）：
   - 检查本地 depotcache 与 config/depotcache 是否已存在清单（双目录幂等跳过）；
   - **优先拉取 AppID 分支归档**：一次拿到 config.json + Key.vdf + 该游戏全部真实
     ``{depot}_{gid}.manifest``，并用归档里的真实 GID 回写 Lua，保证 Lua 与
     depotcache 自洽（旧实现用「官方最新 GID + 社区仓库」必然 404 的根因即在此）；
   - 按 (depot, gid) 精确回退：分支单文件 raw（ghfast / ghproxy / jsDelivr 镜像）
     → ManifestHub API → Manifest Request Code → Valve 官方 CDN；
   - 实时回报清单就绪状态，杜绝"入库成功但无法下载"的误导；
3. 提供外部社区清单检索辅助与一键导入接口。
"""
from __future__ import annotations

import io
import os
import re
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from config import SSL_VERIFY
from core.complete_manifest_service import (
    BatchCompletionReport,
    CompletionReport,
    ManifestCompletionService,
    parse_lua_bindings,
)
from core.complete_manifest_service import update_lua_manifest as _update_lua_manifest_file
from core.complete_manifest_service import update_lua_depot_key
from core.manifest_archive import ManifestArchiveClient
from core.manifest_cache import ManifestCacheManager
from core.manifest_downloader import STEAM_MANIFEST_MAGIC, ManifestDownloader
from utils.http_client import get_system_proxy
from utils.logger import setup_logger

logger = setup_logger(__name__)

# 公共清单/配置文件镜像 API（只提供 lua + key.vdf，不含清单，作为最后的兜底）
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
        self._archive_client: ManifestArchiveClient | None = None
        self._completion_service: ManifestCompletionService | None = None

    def close(self):
        """关闭 HTTP 客户端"""
        self._http.close()
        if self._archive_client is not None:
            self._archive_client.close()
            self._archive_client = None
        if self._completion_service is not None:
            self._completion_service.close()
            self._completion_service = None

    def set_steam_path(self, steam_path: str) -> None:
        self._steam_path = steam_path
        # 路径变化后重建依赖蒸汽路径的子服务
        self._completion_service = None

    @property
    def depotcache_dir(self) -> str:
        return os.path.join(self._steam_path, "depotcache") if self._steam_path else ""

    @property
    def config_depotcache_dir(self) -> str:
        return os.path.join(self._steam_path, "config", "depotcache") if self._steam_path else ""

    @property
    def lua_dir(self) -> str:
        return os.path.join(self._steam_path, "config", "lua") if self._steam_path else ""

    # ── 内部：子服务（惰性构建，便于测试替换）──────────────

    def _get_archive_client(self) -> ManifestArchiveClient:
        if self._archive_client is None:
            self._archive_client = ManifestArchiveClient()
        return self._archive_client

    def _get_completion_service(self) -> ManifestCompletionService:
        if self._completion_service is None:
            self._completion_service = ManifestCompletionService(
                self._steam_path, archive_client=self._get_archive_client()
            )
        return self._completion_service

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
        ok = _update_lua_manifest_file(lua_path, depot_id, manifest_gid, size)
        if ok:
            logger.info(f"Updated {lua_path} with setManifestid({depot_id}, \"{manifest_gid}\")")
        return ok

    # ── 一键补全入口 ──────────────────────────────────────

    def complete_app(
        self,
        app_id: str,
        depots: list[Any] | None = None,
        dlc_ids: list[str] | None = None,
    ) -> CompletionReport:
        """「一键补全」单个游戏：归档优先 + 多源回退，返回逐 depot 结果。

        这是 GUI「补全清单 / 一键补全所有缺失清单」统一调用的入口。
        """
        if not self._steam_path or not os.path.isdir(self._steam_path):
            report = CompletionReport(app_id=str(app_id))
            report.message = "Steam 安装路径无效"
            return report
        return self._get_completion_service().complete_app(app_id, depots=depots, dlc_ids=dlc_ids)

    def complete_apps(self, app_ids: list[str]) -> BatchCompletionReport:
        """「一键补全」多个游戏，返回聚合结果"""
        if not self._steam_path or not os.path.isdir(self._steam_path):
            return BatchCompletionReport()
        return self._get_completion_service().complete_apps(app_ids)

    def prefetch_archive_bindings(self, app_id: str) -> dict[str, Any]:
        """预取归档中的真实 GID/密钥，用于写 Lua 之前对齐 setManifestid。"""
        if not self._steam_path or not os.path.isdir(self._steam_path):
            return {"ok": False, "gid_map": {}, "depot_keys": {}}
        return self._get_completion_service().prefetch_bindings(app_id)

    def resolve_manifests(
        self,
        app_id: str,
        depots: list[Any] | None = None,
        dlc_ids: list[str] | None = None,
    ) -> tuple[bool, str, int]:
        """为指定游戏执行自动清单解析与多源补全流水线（向后兼容的元组返回）

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

        lua_path = os.path.join(self.lua_dir, f"{app_id}.lua") if self.lua_dir else ""

        # 步骤 1: 整理待获取的 depot 列表
        target_depots: list[tuple[str, str, int, str]] = []
        seen_dids: set[str] = set()

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
                    seen_dids.add(did)

        # 若 Lua 已存在，从 Lua 文件补充可能缺失的条目
        if os.path.isfile(lua_path):
            try:
                bindings = parse_lua_bindings(lua_path)
                for did, binding in bindings.items():
                    if did in seen_dids:
                        continue
                    if binding.get("gid"):
                        target_depots.append(
                            (did, binding["gid"], int(binding.get("size") or 0), app_id)
                        )
                        seen_dids.add(did)
                    elif did != app_id:
                        # 仅 addappid 声明、没有 GID 的 depot 交由归档阶段补齐
                        target_depots.append((did, "", 0, app_id))
                        seen_dids.add(did)
            except Exception as e:
                logger.debug(f"Error parsing Lua {lua_path}: {e}")

        # 步骤 2: 检查哪些清单已就绪（记录基线用于最终统计"本次新增"）
        def _missing_now() -> list[tuple[str, str, int, str]]:
            return [
                d for d in target_depots
                if not d[1] or not self.check_manifest_exists(d[0], d[1])
            ]

        # 缺失基线按 depot 记录（含尚无 GID 的 depot，归档阶段可能补齐它们）
        baseline_missing_depots = {d[0] for d in _missing_now()}

        if not baseline_missing_depots and target_depots:
            return True, "游戏清单文件已全部就绪", 0

        # 步骤 3: 使用多源下载器下载缺失的（已知 GID 的）清单
        #   下载器内部源顺序为：分支归档单文件 → ManifestHub API → 内容码 CDN → Steam CDN
        depots_with_gid = [d for d in _missing_now() if d[1]]
        if depots_with_gid:
            try:
                downloader = ManifestDownloader(self._steam_path)
                downloader.download_manifests(depots_with_gid, app_id=app_id)
                downloader.close()
            except Exception as e:
                logger.warning(f"Batch manifest download error for {app_id}: {e}")

        # 步骤 4: 仍缺失的条目 → 拉取自洽的 AppID 分支归档
        #   归档携带仓库实际保存的真实 GID 与 Key.vdf，是唯一能补齐
        #   「Lua 里没有 GID」的 depot 的来源；同时用真实 GID 回写 Lua。
        if _missing_now():
            self._fetch_manifest_bundle(app_id)

        # 步骤 5: 兼容旧的公共 ZIP 接口（只含 lua/key.vdf，作为最后兜底）
        if _missing_now():
            self._fetch_from_mirrors(app_id)

        # 步骤 6: 统计本次真正补齐的 depot 数量（缺失基线 → 现已就绪）
        final_gids: dict[str, str] = {d[0]: d[1] for d in target_depots if d[1]}
        if os.path.isfile(lua_path):
            for did, binding in parse_lua_bindings(lua_path).items():
                if binding.get("gid"):
                    final_gids[did] = binding["gid"]
        downloaded_count = sum(
            1 for depot_id in baseline_missing_depots
            if final_gids.get(depot_id)
            and self.check_manifest_exists(depot_id, final_gids[depot_id])
        )

        # 步骤 7: 最终诊断判断就绪状态
        if os.path.isfile(lua_path):
            diag = self.diagnose_lua_file(lua_path)
            if diag.is_ready:
                return True, f"游戏清单已成功自动补全（本次下载 {downloaded_count} 个），Steam 中可直接下载！", downloaded_count
            return (
                False,
                f"未能自动获取全部清单（已下载 {downloaded_count} 个），尚缺少: {', '.join(diag.missing_manifests[:3])}",
                downloaded_count,
            )

        remaining_missing = sorted(
            depot_id for depot_id in baseline_missing_depots
            if not (final_gids.get(depot_id) and self.check_manifest_exists(depot_id, final_gids[depot_id]))
        )
        if not remaining_missing:
            return True, f"游戏清单已全部就绪！（本次下载 {downloaded_count} 个）", downloaded_count

        return (
            False,
            f"未能自动获取全部清单（已下载 {downloaded_count} 个），尚缺少: {', '.join(remaining_missing[:3])}",
            downloaded_count,
        )

    def _fetch_manifest_bundle(self, app_id: str) -> tuple[bool, int]:
        """下载 AppID 分支归档并落地清单（含 Key.vdf 密钥）。

        与只按 GID 拼接 URL 不同，归档携带了仓库实际保存的 GID，
        因此历史清单也能被发现，并避免 Steam API 的当前 GID 与社区清单
        版本不一致导致的"下载成功但仍显示缺失"。

        Returns:
            (是否取到归档, 落盘的清单数量)
        """
        if not app_id:
            return False, 0

        archive = self._get_archive_client().fetch_appid_archive(str(app_id))
        if not archive.ok:
            logger.info("AppID %s 分支归档不可用: %s", app_id, archive.message)
            return False, 0

        deployed, count = self._deploy_archive_object(app_id, archive)
        return deployed, count

    def _deploy_manifest_archive(self, app_id: str, payload: bytes) -> tuple[bool, int]:
        """安全解压归档中的标准 manifest，并将实际 GID 合并进 Lua。

        保留该方法名以兼容既有调用与测试；实现委托给
        :class:`core.manifest_archive.ManifestArchiveClient` 的内存解析器。
        """
        archive = ManifestArchiveClient.parse_archive(app_id, payload)
        if not archive.ok:
            logger.debug("Invalid manifest archive for %s: %s", app_id, archive.message)
            return False, 0
        return self._deploy_archive_object(app_id, archive)

    def _deploy_archive_object(self, app_id: str, archive: Any) -> tuple[bool, int]:
        """把解析好的归档写入双目录，并用归档真实 GID 回写 Lua。"""
        count = 0
        cache = ManifestCacheManager(self._steam_path) if self._steam_path else None
        for item in archive.manifests:
            if item.data[:4] != STEAM_MANIFEST_MAGIC:
                continue
            if cache is not None:
                written = cache.write(item.depot_id, item.gid, item.data)
            else:  # pragma: no cover - 无 Steam 路径时退化为不落盘
                written = []
            if not written:
                continue
            count += 1
            # 归档 GID 是权威值；不写第三参数 size（与社区 Lua 约定一致，
            # 避免把压缩后体积误当成 Steam 期望的清单大小）
            self.update_lua_manifest(app_id, item.depot_id, item.gid)

        if count:
            logger.info("Deployed %d manifests from AppID archive %s", count, app_id)
        return count > 0, count

    def _lookup_tag_gid_for_depot(self, depot_id: str) -> str | None:
        """【已废弃】从 P-ToyStore Tag 索引检索 Depot 的清单 GID。

        2026-10 实测 ``P-ToyStore/SteamManifestCache_Pro`` 仓库已不存在
        （GitHub API 返回 404），该来源不再可用。此方法仅为向后兼容保留，
        新流水线不再调用它；请使用 :meth:`ManifestArchiveClient.fetch_appid_archive`
        或 :meth:`ManifestArchiveClient.fetch_single_manifest`。
        """
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
        """尝试从公共镜像拉取游戏资源 ZIP 并部署清单/密钥"""
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
                                # 镜像可能提供的是历史 GID；以归档文件名为准，
                                # 同步更新 Lua，避免"文件已下载但仍判定缺失"。
                                if m:
                                    depot_id, gid = m.group(1).split("_", 1)
                                    self.update_lua_manifest(app_id, depot_id, gid)
                                logger.info(f"Extracted manifest {final_name} from mirror")

                            elif lower_name.endswith(".lua") and self.lua_dir:
                                local_lua = os.path.join(self.lua_dir, f"{app_id}.lua")
                                if not os.path.isfile(local_lua):
                                    with open(local_lua, "wb") as f:
                                        f.write(zf.read(name))
                                    logger.info(f"Deployed Lua from mirror: {local_lua}")

                            elif lower_name in ("key.vdf", "keys.vdf") and self.lua_dir:
                                # 把镜像提供的 depot 密钥补进 Lua（幂等）
                                text = zf.read(name).decode("utf-8", "replace")
                                local_lua = os.path.join(self.lua_dir, f"{app_id}.lua")
                                for depot_id, key in ManifestArchiveClient.parse_key_vdf(text).items():
                                    update_lua_depot_key(local_lua, depot_id, key)
                                logger.info("Deployed depot keys from mirror for AppID %s", app_id)

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
