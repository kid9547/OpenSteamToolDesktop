"""清单「一键补全」服务 —— 归档优先的自洽清单流水线。

设计目标
--------
旧的补全流程是「官方 GID → 社区仓库拼 URL」，社区仓库里没有该 GID 时
必然 404，于是永远补不齐，用户只能手动拖拽 ``*.manifest``。

本模块把补全统一成一条可复用、可测试的流水线（归档优先）：

1. **分支归档（首选）**：下载 ``ManifestAutoUpdate`` 风格的 AppID 分支归档，
   一次性拿到 ``config.json`` + ``Key.vdf`` + 该游戏全部真实
   ``{depot}_{gid}.manifest``。
2. **按 (depot, gid) 精确回退**：对仍有明确 GID 的 depot 走单文件分支 raw
   （含 ghfast / ghproxy / jsDelivr 镜像）→ ManifestHub API → 内容码 → Valve CDN。
3. **内容码 → Valve 官方 CDN**：``gmrc.wudrm.com`` / ``manifest.steam.run`` 取
   Manifest Request Code，再访问 ``https://<cdn>/depot/{depot}/manifest/{gid}/5/{code}``。
4. **社区 ZIP 镜像**：最后的兜底。

所有写盘都通过 :class:`core.manifest_cache.ManifestCacheManager` 同时写入
``<steam>/depotcache`` 与 ``<steam>/config/depotcache``，并在写入前校验
魔数 ``0x71F617D0``。Lua 中的 ``setManifestid`` 一律使用归档中实际存在的 GID，
保证 Lua ↔ 落盘文件名 ↔ Steam 读取路径三者 100% 一致。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from core.manifest_archive import ManifestArchive, ManifestArchiveClient
from core.manifest_cache import ManifestCacheManager
from core.manifest_downloader import STEAM_MANIFEST_MAGIC, ManifestDownloader
from utils.logger import setup_logger

logger = setup_logger(__name__)

# ── 状态常量 ──────────────────────────────────────────────
STATUS_ALREADY = "already"          # 本地已存在且有效，无需处理
STATUS_DOWNLOADED = "downloaded"    # 本次成功补齐
STATUS_INVALID = "invalid"          # 文件存在但魔数非法（损坏）
STATUS_MISSING = "missing"          # 必需的 depot 但找不到任何 GID
STATUS_FAILED = "failed"            # 有目标 GID 但所有下载源均失败
STATUS_SKIPPED = "skipped"          # 归档未提供该 depot 的清单，不计入完整性

_READY_STATUSES = (STATUS_ALREADY, STATUS_DOWNLOADED, STATUS_SKIPPED)

# Lua 解析：setManifestid(depot, "gid"[, size]) / addappid(depot[, 0[, "key"]])
LUA_MANIFEST_RE = re.compile(
    r'setmanifestid\s*\(\s*(\d+)\s*,\s*["\']?(\d+)["\']?(?:\s*,\s*(\d+))?\s*\)',
    re.IGNORECASE,
)
LUA_ADDAPPID_RE = re.compile(
    r'addappid\s*\(\s*(\d+)\s*(?:,\s*\d+\s*(?:,\s*["\']([0-9a-fA-F]{8,})["\'])?)?\s*\)',
    re.IGNORECASE,
)


# ── Lua 辅助函数（单一来源，供 resolver / service / gui 共用）────────


def parse_lua_bindings(lua_path: str | os.PathLike[str]) -> dict[str, dict[str, Any]]:
    """解析 Lua 中的 depot 绑定与密钥。

    Returns:
        ``{depot_id: {"gid": str, "size": int, "key": str}}``
    """
    path = Path(lua_path)
    if not path.is_file():
        return {}
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    bindings: dict[str, dict[str, Any]] = {}
    for depot, gid, size in LUA_MANIFEST_RE.findall(content):
        entry = bindings.setdefault(depot, {"gid": "", "size": 0, "key": ""})
        entry["gid"] = gid
        entry["size"] = int(size) if size else 0
    for depot, key in LUA_ADDAPPID_RE.findall(content):
        entry = bindings.setdefault(depot, {"gid": "", "size": 0, "key": ""})
        if key:
            entry["key"] = key.lower()
    return bindings


def read_lua_app_ids(lua_path: str | os.PathLike[str]) -> list[str]:
    """读取 Lua 中出现的所有 AppID / DepotID（``addappid`` 参数）。"""
    path = Path(lua_path)
    if not path.is_file():
        return []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [depot for depot, _key in LUA_ADDAPPID_RE.findall(content)]


def update_lua_manifest(
    lua_path: str | os.PathLike[str],
    depot_id: str,
    manifest_gid: str,
    size: int = 0,
) -> bool:
    """在 Lua 中更新或追加 ``setManifestid(depot, gid[, size])``（幂等）。"""
    path = Path(lua_path)
    if not path.is_file():
        return False
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        pattern = (
            rf'setmanifestid\s*\(\s*{depot_id}\s*,\s*["\']?\d+["\']?'
            rf'(?:\s*,\s*[^)]*)?\)'
        )
        new_line = (
            f'setManifestid({depot_id}, "{manifest_gid}", {size})'
            if size and size > 0
            else f'setManifestid({depot_id}, "{manifest_gid}")'
        )
        if re.search(pattern, content, re.IGNORECASE):
            new_content = re.sub(pattern, new_line, content, flags=re.IGNORECASE)
        else:
            new_content = content.rstrip() + f"\n{new_line}\n"
        if new_content != content:
            path.write_text(new_content, encoding="utf-8")
            logger.debug("Lua 已更新 %s: %s", path.name, new_line)
        return True
    except OSError as exc:
        logger.warning("更新 Lua 失败 %s: %s", path, exc)
        return False


def update_lua_depot_key(
    lua_path: str | os.PathLike[str],
    depot_id: str,
    depot_key: str,
) -> bool:
    """把 ``addappid(depot)`` 升级为 ``addappid(depot, 0, "key")``（幂等）。

    已带密钥的行不会被改写；完全没有该 depot 的行时追加一行。
    """
    path = Path(lua_path)
    if not path.is_file() or not depot_key:
        return False
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        if re.search(
            rf'addappid\s*\(\s*{depot_id}\s*,\s*\d+\s*,\s*["\'][0-9a-fA-F]+["\']\s*\)',
            content,
            re.IGNORECASE,
        ):
            return True
        new_line = f'addappid({depot_id}, 0, "{depot_key}")'
        pattern = rf'addappid\s*\(\s*{depot_id}\s*\)'
        if re.search(pattern, content, re.IGNORECASE):
            new_content = re.sub(pattern, new_line, content, flags=re.IGNORECASE)
        else:
            new_content = content.rstrip() + f"\n{new_line}\n"
        if new_content != content:
            path.write_text(new_content, encoding="utf-8")
            logger.debug("Lua 已补入 depot 密钥 %s: %s", path.name, depot_id)
        return True
    except OSError as exc:
        logger.warning("写入 Lua 密钥失败 %s: %s", path, exc)
        return False


# ── 结果数据结构 ──────────────────────────────────────────


@dataclass
class DepotCompletionResult:
    """单个 depot 的补全结果（含失败原因）"""

    depot_id: str
    gid: str = ""
    status: str = STATUS_MISSING
    source: str = ""
    path: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status in _READY_STATUSES

    @property
    def key(self) -> str:
        return f"{self.depot_id}_{self.gid}" if self.gid else self.depot_id


@dataclass
class CompletionReport:
    """一次「一键补全」的聚合结果"""

    app_id: str
    total: int = 0
    already: int = 0
    downloaded: int = 0
    missing: int = 0
    failed: int = 0
    invalid: int = 0
    skipped: int = 0
    results: list[DepotCompletionResult] = field(default_factory=list)
    depot_keys: dict[str, str] = field(default_factory=dict)
    archive_repo: str = ""
    archive_url: str = ""
    sources_used: list[str] = field(default_factory=list)
    lua_path: str = ""
    message: str = ""

    @property
    def is_complete(self) -> bool:
        if self.missing or self.failed or self.invalid:
            return False
        return self.total > 0 or self.already > 0 or self.downloaded > 0

    @property
    def handled(self) -> int:
        return self.already + self.downloaded

    @property
    def failed_results(self) -> list[DepotCompletionResult]:
        return [r for r in self.results if r.status in (STATUS_FAILED, STATUS_INVALID)]

    @property
    def missing_results(self) -> list[DepotCompletionResult]:
        return [r for r in self.results if r.status == STATUS_MISSING]

    def result_for(self, depot_id: str) -> DepotCompletionResult | None:
        """取某个 depot 的结果条目（GUI/测试便捷方法）。"""
        for item in self.results:
            if item.depot_id == str(depot_id):
                return item
        return None

    def summary(self) -> str:
        """一行式中文摘要，可直接展示在 GUI。"""
        if self.total == 0:
            return self.message or "无需特定清单文件"
        if self.is_complete:
            return (
                f"清单已全部就绪（本次补齐 {self.downloaded} 个，"
                f"已有 {self.already} 个，跳过 {self.skipped} 个）"
            )
        details = [
            f"{r.key}({r.message or r.status})"
            for r in (self.missing_results + self.failed_results)[:3]
        ]
        return (
            f"仍缺 {self.missing + self.failed + self.invalid}/{self.total} 个清单"
            f"（本次补齐 {self.downloaded} 个）：" + "、".join(details)
        )


@dataclass
class BatchCompletionReport:
    """多游戏批量补全的聚合结果（「一键补全」入口）"""

    reports: list[CompletionReport] = field(default_factory=list)

    @property
    def total_apps(self) -> int:
        return len(self.reports)

    @property
    def complete_apps(self) -> int:
        return sum(1 for r in self.reports if r.is_complete)

    @property
    def downloaded(self) -> int:
        return sum(r.downloaded for r in self.reports)

    @property
    def failed_apps(self) -> list[CompletionReport]:
        return [r for r in self.reports if not r.is_complete]

    def summary(self) -> str:
        if not self.reports:
            return "没有需要补全的游戏"
        line = (
            f"已为 {self.complete_apps}/{self.total_apps} 款游戏补齐清单"
            f"（共下载 {self.downloaded} 个清单文件）"
        )
        if self.failed_apps:
            names = "、".join(r.app_id for r in self.failed_apps[:3])
            line += f"；仍不完整：{names}"
        return line


# ── 服务实现 ──────────────────────────────────────────────


class ManifestCompletionService:
    """归档优先的清单补全服务（网络层可注入，便于离线测试）。

    用法::

        service = ManifestCompletionService(steam_path)
        report = service.complete_app("730")
        print(report.summary())
    """

    def __init__(
        self,
        steam_path: str,
        archive_client: ManifestArchiveClient | None = None,
        downloader: ManifestDownloader | None = None,
    ) -> None:
        self.steam_path = steam_path or ""
        self._owns_archive = archive_client is None
        self._archive = archive_client or ManifestArchiveClient()
        self._downloader = downloader
        self._owns_downloader = False
        self._cache = ManifestCacheManager(self.steam_path) if self.steam_path else None

    # ── 资源管理 ──────────────────────────────────────────

    def close(self) -> None:
        if self._owns_archive:
            self._archive.close()
        if self._downloader is not None and self._owns_downloader:
            self._downloader.close()

    def __enter__(self) -> "ManifestCompletionService":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ── 路径属性 ──────────────────────────────────────────

    @property
    def depotcache_dir(self) -> str:
        return os.path.join(self.steam_path, "depotcache") if self.steam_path else ""

    @property
    def config_depotcache_dir(self) -> str:
        return os.path.join(self.steam_path, "config", "depotcache") if self.steam_path else ""

    @property
    def lua_dir(self) -> str:
        return os.path.join(self.steam_path, "config", "lua") if self.steam_path else ""

    def lua_path(self, app_id: str) -> str:
        return os.path.join(self.lua_dir, f"{app_id}.lua") if self.lua_dir else ""

    def _get_downloader(self) -> ManifestDownloader:
        if self._downloader is None:
            self._downloader = ManifestDownloader(self.steam_path)
            self._owns_downloader = True
        return self._downloader

    # ── 公开接口 ──────────────────────────────────────────

    def prefetch_bindings(self, app_id: str) -> dict[str, Any]:
        """在写入 Lua 之前预取归档中的真实 GID 与密钥。

        供 ``gui/search_page`` 入库流程使用：先用归档给出的真实 GID 填充
        ``setManifestid``，再写 Lua，最后落盘清单，避免「Lua 里是官方最新
        GID、depotcache 里是社区历史清单」的错位。

        Returns:
            ``{"gid_map": {...}, "depot_keys": {...}, "dlc_ids": [...],
               "depot_ids": [...], "repo": str, "ok": bool, "message": str}``
        """
        archive = self._archive.fetch_appid_archive(app_id)
        if not archive.ok:
            return {
                "gid_map": {},
                "depot_keys": {},
                "dlc_ids": [],
                "depot_ids": [],
                "repo": "",
                "ok": False,
                "message": archive.message,
            }
        return {
            "gid_map": archive.gid_map,
            "depot_keys": archive.depot_keys,
            "dlc_ids": archive.dlc_ids,
            "depot_ids": archive.depot_ids,
            "repo": archive.repo,
            "ok": True,
            "message": archive.message,
        }

    def complete_app(
        self,
        app_id: str,
        depots: Iterable[Any] | None = None,
        dlc_ids: Iterable[str] | None = None,
        apply_keys: bool = True,
        write_lua: bool = True,
        archive: ManifestArchive | None = None,
    ) -> CompletionReport:
        """为单个 AppID 补齐所有缺失清单，并返回逐 depot 结果。

        Args:
            app_id: 游戏 AppID
            depots: ``[(depot_id, gid, size, owner_app_id), ...]``；缺省时从 Lua 推断
            dlc_ids: DLC AppID 列表（仅用于日志/统计）
            apply_keys: 是否把归档 ``Key.vdf`` 中的 depot 密钥写入 Lua
            write_lua: 是否用归档中的真实 GID 回写 ``setManifestid``
            archive: 调用方已取到的分支归档（可选）。传入后不再重复下载整包；
                无论其 ``ok`` 与否都视为"已尝试过归档"，避免同一 AppID 的
                归档在一次入库流程里被下载两遍。

        Returns:
            :class:`CompletionReport`
        """
        app_id = str(app_id)
        report = CompletionReport(app_id=app_id, lua_path=self.lua_path(app_id))

        if not self.steam_path or not os.path.isdir(self.steam_path):
            report.message = "Steam 安装路径无效"
            return report

        cache = self._cache or ManifestCacheManager(self.steam_path)
        self._cache = cache
        os.makedirs(self.depotcache_dir, exist_ok=True)
        os.makedirs(self.config_depotcache_dir, exist_ok=True)

        # ── 1. 汇总目标 depot（显式参数 + Lua）────────────────
        targets: dict[str, dict[str, Any]] = {}
        required: set[str] = set()
        for item in depots or []:
            if not item:
                continue
            depot_id = str(item[0]) if len(item) > 0 else ""
            if not depot_id.isdigit():
                continue
            targets[depot_id] = {
                "gid": str(item[1]) if len(item) > 1 and item[1] else "",
                "size": int(item[2]) if len(item) > 2 and item[2] else 0,
                "owner": str(item[3]) if len(item) > 3 and item[3] else app_id,
            }
            required.add(depot_id)

        lua_bindings = parse_lua_bindings(report.lua_path) if report.lua_path else {}
        for depot_id, binding in lua_bindings.items():
            # `addappid(appid)` 是应用本体而不是 depot，不需要清单；
            # 但社区 Lua 常会给主 depot（depot ID == AppID）写
            # `setManifestid(appid, "gid")` —— 这是真实的清单请求，必须下载。
            # 旧逻辑一刀切跳过 depot==app_id，导致主 depot 清单"永远缺一个
            # 却显示补全成功"（游戏库按 Lua 判定仍缺失）。
            if depot_id == app_id and not binding.get("gid"):
                continue
            entry = targets.setdefault(depot_id, {"gid": "", "size": 0, "owner": app_id})
            if not entry.get("gid") and binding.get("gid"):
                entry["gid"] = binding["gid"]
            if not entry.get("size") and binding.get("size"):
                entry["size"] = binding["size"]
            if binding.get("gid"):
                required.add(depot_id)

        # ── 2. 归档优先：一次拿到真实 GID + 密钥 + 全部清单 ──
        write_sources: dict[str, str] = {}
        if archive is None:
            archive = self._archive.fetch_appid_archive(app_id)
        if archive.ok:
            report.archive_repo = archive.repo
            report.archive_url = archive.source_url
            report.depot_keys = dict(archive.depot_keys)
            self._merge_archive_targets(targets, archive, app_id)
            source_label = f"AppID 分支归档 ({archive.repo})"
            written = self._deploy_archive(
                archive, targets, app_id, report, cache, write_lua, source_label
            )
            write_sources.update(written)
            if written:
                report.sources_used.append(source_label)
            report.message = archive.message
        else:
            report.message = archive.message
            logger.info("AppID %s 无可用分支归档: %s", app_id, archive.message)

        # ── 3. 对仍未就绪且有明确 GID 的 depot 走精确多源回退 ──
        pending_precise = [
            (depot_id, entry["gid"], entry["size"], entry["owner"])
            for depot_id, entry in targets.items()
            if entry.get("gid") and not cache.is_present(depot_id, entry["gid"])
        ]
        if pending_precise:
            try:
                batch = self._get_downloader().download_manifests(pending_precise, app_id=app_id)
            except Exception as exc:
                logger.warning("AppID %s 精确路径下载异常: %s", app_id, exc)
                batch = None
            if batch is not None:
                for item in batch.results:
                    if item.success:
                        write_sources[f"{item.depot_id}_{item.manifest_gid}"] = (
                            item.message or "多源精确下载"
                        )
                if batch.success:
                    logger.info("AppID %s 精确路径补齐 %d 个清单", app_id, batch.success)

        # ── 4. 收尾统计（逐 depot 给结论与原因）────────────────
        self._finalize(targets, required, app_id, report, cache, write_lua, write_sources)

        # ── 5. 用归档密钥补全 Lua（depot 加密密钥缺失会导致 Steam 无法下载）──
        if apply_keys and report.depot_keys and report.lua_path and os.path.isfile(report.lua_path):
            for depot_id, key in report.depot_keys.items():
                if depot_id in targets or depot_id in required:
                    update_lua_depot_key(report.lua_path, depot_id, key)

        logger.info("AppID %s 补全结果：%s", app_id, report.summary())
        return report

    def complete_apps(self, app_ids: Iterable[str], **kwargs: Any) -> BatchCompletionReport:
        """「一键补全」多游戏入口，逐游戏聚合结果（单个失败不影响其它）。"""
        batch = BatchCompletionReport()
        for app_id in app_ids:
            try:
                batch.reports.append(self.complete_app(app_id, **kwargs))
            except Exception as exc:
                logger.warning("AppID %s 补全异常: %s", app_id, exc)
                batch.reports.append(
                    CompletionReport(app_id=str(app_id), message=f"异常: {exc}")
                )
        return batch

    # ── 内部实现 ──────────────────────────────────────────

    @staticmethod
    def _merge_archive_targets(
        targets: dict[str, dict[str, Any]],
        archive: ManifestArchive,
        app_id: str,
    ) -> None:
        """把归档声明的 depot 与真实 GID 合并进目标集合。"""
        for depot_id in archive.depot_ids:
            targets.setdefault(depot_id, {"gid": "", "size": 0, "owner": app_id})
        for depot_id, gid in archive.gid_map.items():
            entry = targets.setdefault(depot_id, {"gid": "", "size": 0, "owner": app_id})
            # 归档中的 GID 是权威值：即使 Lua/元数据里有旧 GID 也要覆盖
            entry["gid"] = gid
            item = archive.get(depot_id)
            if item is not None:
                entry["size"] = item.size

    def _deploy_archive(
        self,
        archive: ManifestArchive,
        targets: dict[str, dict[str, Any]],
        app_id: str,
        report: CompletionReport,
        cache: ManifestCacheManager,
        write_lua: bool,
        source_label: str,
    ) -> dict[str, str]:
        """把归档中的清单写入双目录，并用真实 GID 回写 Lua。

        Returns:
            ``{"{depot}_{gid}": 来源标签}`` —— 本次实际落盘的清单。
        """
        written: dict[str, str] = {}
        for item in archive.manifests:
            if item.data[:4] != STEAM_MANIFEST_MAGIC:
                logger.warning("归档清单魔数非法，跳过 %s", item.filename)
                continue
            # 幂等：先判断是否已存在，只有真正新落盘的才算“本次补齐”
            existed = cache.is_present(item.depot_id, item.gid)
            paths = cache.write(item.depot_id, item.gid, item.data)
            if not paths:
                continue
            if not existed:
                written[item.filename[: -len(".manifest")]] = source_label
            entry = targets.setdefault(
                item.depot_id, {"gid": item.gid, "size": item.size, "owner": app_id}
            )
            entry["gid"] = item.gid
            entry["size"] = item.size
            if write_lua and report.lua_path and os.path.isfile(report.lua_path):
                # 归档 GID 是权威值；不写第三参数 size（与社区 Lua 约定一致）
                update_lua_manifest(report.lua_path, item.depot_id, item.gid)
        if written:
            logger.info("AppID %s 从归档落盘 %d 个清单到双目录", app_id, len(written))
        return written

    def _finalize(
        self,
        targets: dict[str, dict[str, Any]],
        required: set[str],
        app_id: str,
        report: CompletionReport,
        cache: ManifestCacheManager,
        write_lua: bool,
        write_sources: dict[str, str],
    ) -> None:
        """逐 depot 判定最终状态，生成带原因的聚合结果。"""
        results: list[DepotCompletionResult] = []
        counters = {
            STATUS_ALREADY: 0,
            STATUS_DOWNLOADED: 0,
            STATUS_INVALID: 0,
            STATUS_MISSING: 0,
            STATUS_FAILED: 0,
            STATUS_SKIPPED: 0,
        }

        for depot_id in sorted(targets, key=int):
            entry = targets[depot_id]
            gid = str(entry.get("gid") or "")
            is_required = depot_id in required

            if not gid:
                if is_required:
                    counters[STATUS_MISSING] += 1
                    results.append(
                        DepotCompletionResult(
                            depot_id=depot_id,
                            status=STATUS_MISSING,
                            message="未找到该 depot 对应的清单 GID（社区归档无此 depot）",
                        )
                    )
                else:
                    counters[STATUS_SKIPPED] += 1
                    results.append(
                        DepotCompletionResult(
                            depot_id=depot_id,
                            status=STATUS_SKIPPED,
                            message="社区归档未收录该 depot 的清单，已跳过",
                        )
                    )
                continue

            paths = [
                os.path.join(d, f"{depot_id}_{gid}.manifest")
                for d in (self.depotcache_dir, self.config_depotcache_dir)
                if d
            ]
            existing = [p for p in paths if os.path.isfile(p) and os.path.getsize(p) > 0]

            if not existing:
                counters[STATUS_FAILED] += 1
                results.append(
                    DepotCompletionResult(
                        depot_id=depot_id,
                        gid=gid,
                        status=STATUS_FAILED,
                        message="所有来源均未取到该清单（归档/镜像/内容码/CDN 全部失败）",
                    )
                )
                continue

            if not all(cache.is_valid_file(p) for p in existing):
                counters[STATUS_INVALID] += 1
                results.append(
                    DepotCompletionResult(
                        depot_id=depot_id,
                        gid=gid,
                        status=STATUS_INVALID,
                        path=existing[0],
                        message="清单文件魔数校验失败，文件可能损坏",
                    )
                )
                continue

            # 双目录一致性：只写了一处时补齐另一处
            if len(existing) < len(paths):
                mirrored = self._mirror_dual(depot_id, gid, existing[0], paths)
                if mirrored:
                    counters[STATUS_DOWNLOADED] += 1
                    results.append(
                        DepotCompletionResult(
                            depot_id=depot_id,
                            gid=gid,
                            status=STATUS_DOWNLOADED,
                            source="本地双目录镜像同步",
                            path=existing[0],
                            message="已从既有副本镜像到另一缓存目录",
                        )
                    )
                    continue

            key = f"{depot_id}_{gid}"
            if key in write_sources:
                counters[STATUS_DOWNLOADED] += 1
                results.append(
                    DepotCompletionResult(
                        depot_id=depot_id,
                        gid=gid,
                        status=STATUS_DOWNLOADED,
                        source=write_sources[key],
                        path=existing[0],
                        message=write_sources[key],
                    )
                )
            else:
                counters[STATUS_ALREADY] += 1
                results.append(
                    DepotCompletionResult(
                        depot_id=depot_id,
                        gid=gid,
                        status=STATUS_ALREADY,
                        source="本地缓存",
                        path=existing[0],
                        message="本地已存在且魔数有效",
                    )
                )

        report.results = results
        report.total = len(results)
        report.already = counters[STATUS_ALREADY]
        report.downloaded = counters[STATUS_DOWNLOADED]
        report.invalid = counters[STATUS_INVALID]
        report.missing = counters[STATUS_MISSING]
        report.failed = counters[STATUS_FAILED]
        report.skipped = counters[STATUS_SKIPPED]
        report.sources_used = list(dict.fromkeys(report.sources_used))

        if write_lua and report.lua_path and os.path.isfile(report.lua_path):
            # 只回写与归档真实 GID 不一致的绑定，避免改动无关行或丢失原有 size
            current = parse_lua_bindings(report.lua_path)
            for depot_id, entry in targets.items():
                gid = str(entry.get("gid") or "")
                if not gid:
                    continue
                if current.get(depot_id, {}).get("gid") == gid:
                    continue
                update_lua_manifest(report.lua_path, depot_id, gid)

    @staticmethod
    def _mirror_dual(depot_id: str, gid: str, source: str, targets: list[str]) -> bool:
        """把已有副本复制到另一缓存目录（缺失的那一侧）。"""
        try:
            data = Path(source).read_bytes()
        except OSError:
            return False
        filename = f"{depot_id}_{gid}.manifest"
        copied = False
        for target in targets:
            path = Path(target)
            if path.name != filename or (path.is_file() and path.stat().st_size > 0):
                continue
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                copied = True
                logger.debug("双目录镜像同步: %s", path)
            except OSError:
                continue
        return copied
