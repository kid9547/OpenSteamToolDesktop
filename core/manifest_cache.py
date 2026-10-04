"""Steam depotcache 双目录清单管理。

该模块只处理本地文件：扫描、校验、归属分类、去重和定向清理。
它不会删除未明确指定的文件，也不会修改 Steam 授权数据。

孤儿判定必须极端保守（宁漏勿错）。一个清单被视为"在用"只要满足任一条件：

1. 其 ``depot_gid`` 被某个 Lua 的 ``setManifestid`` 精确引用；
2. 其 depot 编号出现在任何 Lua 的 ``addappid`` / ``setManifestid`` 参数中
   （DLC、共享 Redist 等 depot 往往只有 addappid 绑定）；
3. 其 depot 属于**当前已安装的 Steam 游戏**——从各库的
   ``appmanifest_<appid>.acf`` 的 ``InstalledDepots`` / ``MountedDepots``
   解析得到。正版/本地安装游戏的清单不在任何解锁 Lua 里，旧版曾把它们
   当孤儿清掉，导致已安装游戏损坏。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from core.manifest_downloader import STEAM_MANIFEST_MAGIC
from utils.logger import setup_logger

logger = setup_logger(__name__)


@dataclass
class ManifestRecord:
    depot_id: str
    manifest_gid: str
    paths: list[str] = field(default_factory=list)
    valid: bool = False

    @property
    def key(self) -> str:
        return f"{self.depot_id}_{self.manifest_gid}"


@dataclass
class AppManifestAudit:
    """单个游戏（AppID）的清单完整性审计结果。"""

    app_id: str
    lua_path: str = ""
    declared: int = 0
    missing: list[str] = field(default_factory=list)
    damaged: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.damaged

    @property
    def present(self) -> int:
        return self.declared - len(self.missing) - len(self.damaged)

    def summary(self) -> str:
        if self.ok:
            return f"AppID {self.app_id}：{self.declared} 个清单全部就绪"
        parts = [f"AppID {self.app_id}：{self.present}/{self.declared} 就绪"]
        if self.missing:
            parts.append(f"缺失 {len(self.missing)} 个")
        if self.damaged:
            parts.append(f"损坏 {len(self.damaged)} 个")
        return "，".join(parts)


class ManifestCacheManager:
    """统一管理 depotcache 与 config/depotcache 中的清单文件。"""

    _NAME = re.compile(r"^(?P<depot>\d+)_(?P<gid>\d+)\.manifest$", re.IGNORECASE)

    def __init__(self, steam_path: str):
        self.steam_path = Path(steam_path) if steam_path else None
        self.directories = (
            (
                self.steam_path / "depotcache",
                self.steam_path / "config" / "depotcache",
            )
            if self.steam_path
            else ()
        )

    def scan(self) -> list[ManifestRecord]:
        records: dict[str, ManifestRecord] = {}
        for directory in self.directories:
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                match = self._NAME.fullmatch(path.name)
                if not match or not path.is_file():
                    continue
                key = f"{match.group('depot')}_{match.group('gid')}"
                record = records.setdefault(
                    key,
                    ManifestRecord(match.group("depot"), match.group("gid")),
                )
                record.paths.append(str(path))
                record.valid = record.valid or self.is_valid_file(path)
        return sorted(records.values(), key=lambda item: (int(item.depot_id), int(item.manifest_gid)))

    @staticmethod
    def is_valid_file(path: str | Path) -> bool:
        try:
            with Path(path).open("rb") as stream:
                return stream.read(4) == STEAM_MANIFEST_MAGIC
        except OSError:
            return False

    def is_present(self, depot_id: str, manifest_gid: str) -> bool:
        """清单是否已在任一缓存目录中存在（仅要求非空，兼容 Steam 行为）。"""
        key = f"{depot_id}_{manifest_gid}"
        for directory in self.directories:
            target = directory / f"{key}.manifest"
            try:
                if target.is_file() and target.stat().st_size > 0:
                    return True
            except OSError:
                continue
        return False

    def write(
        self,
        depot_id: str,
        manifest_gid: str,
        payload: bytes,
        *,
        clean_old: bool = True,
        overwrite: bool = False,
    ) -> list[str]:
        """把一份清单写入 depotcache 与 config/depotcache 两个目录。

        Args:
            depot_id: Depot ID
            manifest_gid: 清单 GID（必须与 payload 内真实版本一致）
            payload: 标准 Steam 清单二进制（必须以 0x71F617D0 魔数开头）
            clean_old: 是否清理同一 Depot 的其它 GID 旧文件，避免 Steam 读到过期清单
            overwrite: 是否覆盖已存在的同名文件

        Returns:
            实际写入的文件路径列表；魔数非法时返回空列表。
        """
        if not re.fullmatch(r"\d+", str(depot_id)) or not re.fullmatch(r"\d+", str(manifest_gid)):
            raise ValueError("invalid depot or manifest gid")
        if not payload or payload[:4] != STEAM_MANIFEST_MAGIC:
            logger.warning("拒绝写入魔数非法的清单: %s_%s", depot_id, manifest_gid)
            return []

        filename = f"{depot_id}_{manifest_gid}.manifest"
        written: list[str] = []
        for directory in self.directories:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / filename
                if target.exists() and not overwrite:
                    written.append(str(target))
                    continue
                target.write_bytes(payload)
                written.append(str(target))
            except OSError as exc:
                logger.warning("写入清单失败 %s: %s", filename, exc)

        if clean_old and written:
            self.delete_outdated(depot_id, str(manifest_gid))
        return written

    def delete_outdated(self, depot_id: str, keep_gid: str) -> int:
        """删除同一 Depot 下除 keep_gid 之外的旧清单副本。"""
        if not re.fullmatch(r"\d+", str(depot_id)):
            raise ValueError("invalid depot id")
        removed = 0
        keep_name = f"{depot_id}_{keep_gid}.manifest"
        for directory in self.directories:
            if not directory.is_dir():
                continue
            try:
                for path in directory.iterdir():
                    if not path.is_file() or path.name == keep_name:
                        continue
                    match = self._NAME.fullmatch(path.name)
                    if match and match.group("depot") == str(depot_id):
                        try:
                            path.unlink()
                            removed += 1
                        except OSError:
                            pass
            except OSError:
                continue
        return removed

    # ── Lua 引用关系（支持单目录或多个目录）────────────────
    def _normalize_lua_dirs(self, lua_dir) -> list[Path]:
        """归一化 Lua 目录参数。

        * ``None``（未传）→ 回退到 ``<steam>/config/lua``；
        * 空串 / 空列表（显式表示"没有 Lua 目录"）→ 返回空列表，
          调用方据此判定"无引用信息"（孤儿测试依赖这一语义）；
        * 其余 → str/Path/列表混合输入归一为 Path 列表。
        """
        if lua_dir is None:
            return self._default_lua_dirs()
        if isinstance(lua_dir, (str, Path, os.PathLike)):
            return [Path(lua_dir)] if str(lua_dir) else []
        result: list[Path] = []
        for item in lua_dir:
            if item:
                result.append(Path(item))
        return result

    def _default_lua_dirs(self) -> list[Path]:
        if self.steam_path:
            return [self.steam_path / "config" / "lua"]
        return []

    def referenced_keys(self, lua_dir=None) -> set[str]:
        """Lua 中通过 ``setManifestid(depot, gid)`` 精确引用的 ``depot_gid`` 集合。

        ``lua_dir`` 可以是单个目录，也可以是目录列表（双 Lua 路径等场景）。
        """
        directories = self._normalize_lua_dirs(lua_dir)
        referenced: set[str] = set()
        pattern = re.compile(
            r"setmanifestid\s*\(\s*(\d+)\s*,\s*[\"']?(\d+)",
            re.IGNORECASE,
        )
        for directory in directories:
            if not directory.is_dir():
                continue
            for lua_path in directory.glob("*.lua"):
                try:
                    text = lua_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                referenced.update(f"{depot}_{gid}" for depot, gid in pattern.findall(text))
        return referenced

    def referenced_depots(self, lua_dir=None) -> set[str]:
        """Lua 中出现过的全部 Depot 编号（``addappid`` 与 ``setManifestid`` 的首个参数）。

        孤儿判定必须用 depot 级集合而不是精确 GID 匹配：很多 depot 只有
        ``addappid(depot, 0, "key")`` 而没有 ``setManifestid``（DLC、共享
        Redist 等），Steam/OpenSteamTool 对这类 depot 会直接使用 depotcache
        中该 depot 的任意清单。若只按精确 GID 判定，这些在用的清单会被
        误判成孤儿并清掉——这正是"使用中的清单被清理"事故的根因。
        """
        directories = self._normalize_lua_dirs(lua_dir)
        depots: set[str] = set()
        pattern = re.compile(
            r"\b(?:addappid|setmanifestid)\s*\(\s*(\d+)",
            re.IGNORECASE,
        )
        for directory in directories:
            if not directory.is_dir():
                continue
            for lua_path in directory.glob("*.lua"):
                try:
                    text = lua_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                depots.update(pattern.findall(text))
        return depots

    def depot_owner(self, depot_id: str, lua_dir=None) -> str:
        """反查某个 depot 被哪个 Lua（游戏）引用，返回 ``appid`` 或空串。"""
        directories = self._normalize_lua_dirs(lua_dir)
        pattern = re.compile(
            rf"\b(?:addappid|setmanifestid)\s*\(\s*{re.escape(str(depot_id))}\b",
            re.IGNORECASE,
        )
        for directory in directories:
            if not directory.is_dir():
                continue
            for lua_path in sorted(directory.glob("*.lua")):
                try:
                    text = lua_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if pattern.search(text):
                    return lua_path.stem
        return ""

    # ── 逐游戏完整性审计 ──────────────────────────────────────
    def audit(self, lua_dir=None) -> list["AppManifestAudit"]:
        """逐游戏比对「Lua 声明的 (depot, gid)」与「本地实际存在的清单」。

        这是"清单下载不全"的**检测**手段：只有逐游戏列出缺了哪些 depot 的清单，
        用户才能知道该补什么；此前只能看到一堆孤立的 manifest 文件。

        Returns:
            按 AppID 排序的 :class:`AppManifestAudit` 列表（仅包含声称有
            ``setManifestid`` 的游戏；完全没有清单绑定的游戏不参与判定）。
        """
        directories = self._normalize_lua_dirs(lua_dir)

        present: dict[str, str] = {}
        for record in self.scan():
            # 记录有效副本（优先）或至少存在的路径
            valid = next((p for p in record.paths if self.is_valid_file(p)), None)
            if valid:
                present[record.key] = valid
            elif record.paths:
                present[record.key] = ""

        bindings_re = re.compile(
            r"setmanifestid\s*\(\s*(\d+)\s*,\s*[\"']?(\d+)",
            re.IGNORECASE,
        )
        audits: list[AppManifestAudit] = []
        for directory in directories:
            if not directory.is_dir():
                continue
            for lua_path in sorted(directory.glob("*.lua")):
                stem = lua_path.stem
                if not stem.isdigit():
                    continue
                try:
                    text = lua_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                declared = bindings_re.findall(text)
                if not declared:
                    continue

                missing: list[str] = []
                damaged: list[str] = []
                for depot, gid in declared:
                    key = f"{depot}_{gid}"
                    if key not in present:
                        missing.append(key)
                    elif not present[key]:
                        damaged.append(key)

                audits.append(
                    AppManifestAudit(
                        app_id=stem,
                        lua_path=str(lua_path),
                        declared=len(declared),
                        missing=missing,
                        damaged=damaged,
                    )
                )
        # 同名 Lua（多目录场景）只保留一条
        deduped: dict[str, AppManifestAudit] = {}
        for item in audits:
            deduped.setdefault(item.app_id, item)
        return sorted(deduped.values(), key=lambda item: int(item.app_id))

    # ── 已安装游戏 depot 保护（appmanifest_*.acf）──────────
    _ACF_DEPOT_SECTION_RE = re.compile(r'"(InstalledDepots|MountedDepots)"\s*\{')

    def steam_libraries(self) -> list[Path]:
        """解析 libraryfolders.vdf，返回全部 Steam 库根目录。"""
        if not self.steam_path:
            return []
        vdf = self.steam_path / "steamapps" / "libraryfolders.vdf"
        libraries: list[Path] = [self.steam_path]
        if vdf.is_file():
            try:
                text = vdf.read_text(encoding="utf-8", errors="replace")
                for match in re.finditer(r'"path"\s+"([^"]+)"', text):
                    path = match.group(1).replace("\\\\", "\\")
                    candidate = Path(path)
                    if candidate.is_dir() and candidate not in libraries:
                        libraries.append(candidate)
            except OSError as exc:
                logger.debug("读取 libraryfolders.vdf 失败: %s", exc)
        return libraries

    @classmethod
    def _extract_acf_depot_section(cls, text: str, section: str) -> set[str]:
        """从 ACF 文本中取出指定大括号段落里的第一层 depot 键。

        ACF 结构示例：``"InstalledDepots" { "501" { "manifest" "1000" } }``，
        depot 键（``"501"``）位于段落内第一层（depth==1），其自身的大括号
        使 depth 升到 2，块内字段一律忽略。
        """
        opening = re.search(rf'"{section}"\s*\{{', text)
        if not opening:
            return set()
        depth = 1
        index = opening.end()
        keys: set[str] = set()
        key_re = re.compile(r'"(\d+)"\s*\{')
        while index < len(text) and depth > 0:
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            elif char == '"' and depth == 1:
                match = key_re.match(text, index)
                if match:
                    keys.add(match.group(1))
                    index = match.end() - 1
            index += 1
        return keys

    def installed_app_names(self) -> dict[str, str]:
        """从全部 ``appmanifest_*.acf`` 提取 ``{app_id: 游戏名}``（本地可离线解析）。"""
        names: dict[str, str] = {}
        if not self.steam_path:
            return names
        name_re = re.compile(r'"name"\s+"([^"]+)"')
        for library in self.steam_libraries():
            steamapps = library / "steamapps"
            if not steamapps.is_dir():
                continue
            try:
                manifests = list(steamapps.glob("appmanifest_*.acf"))
            except OSError:
                continue
            for acf_path in manifests:
                app_id = acf_path.stem.removeprefix("appmanifest_")
                if not app_id.isdigit() or app_id in names:
                    continue
                try:
                    text = acf_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                match = name_re.search(text)
                if match:
                    names[app_id] = match.group(1)
        return names

    def installed_depot_ids(self) -> dict[str, str]:
        """扫描全部 Steam 库的 ``appmanifest_*.acf``，返回 ``{depot_id: app_id}``。

        覆盖 ``InstalledDepots`` 与 ``MountedDepots`` 两个段落——已安装/已挂载
        的正版游戏其清单同样存放在 depotcache，却不会出现在任何解锁 Lua 中，
        必须受保护，绝不能被孤儿清理波及。
        """
        result: dict[str, str] = {}
        if not self.steam_path:
            return result
        for library in self.steam_libraries():
            steamapps = library / "steamapps"
            if not steamapps.is_dir():
                continue
            try:
                manifests = list(steamapps.glob("appmanifest_*.acf"))
            except OSError:
                continue
            for acf_path in manifests:
                app_id = acf_path.stem.removeprefix("appmanifest_")
                if not app_id.isdigit():
                    continue
                try:
                    text = acf_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for section in ("InstalledDepots", "MountedDepots"):
                    for depot in self._extract_acf_depot_section(text, section):
                        result.setdefault(depot, app_id)
        if result:
            logger.debug("已安装游戏 depot 保护名单：%d 个 depot", len(result))
        return result

    def declared_depots_for_app(self, app_id: str, lua_dirs=None) -> set[str]:
        """收集某个游戏的 Lua 声明的全部 depot 编号（按游戏删除清单用）。"""
        directories = self._normalize_lua_dirs(lua_dirs)
        depots: set[str] = set()
        pattern = re.compile(
            r"\b(?:addappid|setmanifestid)\s*\(\s*(\d+)",
            re.IGNORECASE,
        )
        for directory in directories:
            lua_file = directory / f"{app_id}.lua"
            if not lua_file.is_file():
                continue
            try:
                text = lua_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            depots.update(pattern.findall(text))
        return depots

    def classify(self, records: list[ManifestRecord], lua_dir=None) -> list[dict]:
        """给每条清单做归属分类（孤儿清理前必须逐条判定，宁漏勿错）。

        Returns:
            ``[{record, key, depot, gid, category, owner}]``，category 取值：
            ``lua``（被解锁 Lua 引用）、``installed``（属于已安装的正版游戏）、
            ``orphan``（未引用，可清理）。
        """
        referenced_keys = self.referenced_keys(lua_dir)
        referenced_depots = self.referenced_depots(lua_dir)
        installed = self.installed_depot_ids()
        classified: list[dict] = []
        for record in records:
            if record.key in referenced_keys:
                category, owner = "lua", self.depot_owner(record.depot_id, lua_dir)
            elif record.depot_id in referenced_depots:
                category, owner = "lua", self.depot_owner(record.depot_id, lua_dir)
            elif record.depot_id in installed:
                category, owner = "installed", installed[record.depot_id]
            else:
                category, owner = "orphan", ""
            classified.append(
                {
                    "record": record,
                    "key": record.key,
                    "depot": record.depot_id,
                    "gid": record.manifest_gid,
                    "category": category,
                    "owner": owner,
                }
            )
        return classified

    def orphaned(self, lua_dir=None) -> list[ManifestRecord]:
        """找出孤儿清单（depot 级判定 + 已安装游戏保护，宁漏勿错）。

        判定规则：一个清单是孤儿，当且仅当——
        * 它的 **depot 编号**没有被任何 Lua 的 ``addappid`` / ``setManifestid``
          引用；并且
        * 它的 depot 不属于任何已安装 Steam 游戏（``appmanifest_*.acf`` 的
          ``InstalledDepots`` / ``MountedDepots``）。
        """
        classified = self.classify(self.scan(), lua_dir)
        return [item["record"] for item in classified if item["category"] == "orphan"]

    def backup_and_delete(
        self,
        records: list[ManifestRecord],
        backup_root: str | Path | None = None,
        allow_in_use: bool = False,
    ) -> tuple[int, int, str]:
        """把指定清单从双目录移入备份目录（而非直接删除），返回 ``(份数, 字节数, 备份目录)``。

        安全网设计（纵深防御）：

        1. 孤儿判定永远可能出错，因此清理动作是**可恢复的移动**，文件移动到
           ``<steam>/depotcache_backup/<时间戳>/``；
        2. 默认（``allow_in_use=False``）移动前逐条**复核归属**：凡是"文件有效
           且 depot 被任何 Lua 引用或属于已安装游戏"的清单，一律拒绝移动——
           即使调用方传入了错误的清单列表，也不可能误删在用文件；
        3. ``allow_in_use=True`` 仅供「按游戏删除全部清单」等用户显式确认的
           场景（确认框里写明游戏名与数量），跳过孤儿校验。

        损坏文件两种模式下都可移动（无法被 Steam 使用，删除后由补全重下）。
        """
        import time as _time

        root = Path(backup_root) if backup_root else (
            self.steam_path / "depotcache_backup" if self.steam_path else None
        )
        if root is None:
            return 0, 0, ""

        if not allow_in_use:
            # ── 纵深防御：逐条复核归属 ──
            classified = self.classify(records)
            blocked_keys = [
                item["key"]
                for item in classified
                if item["category"] != "orphan" and item["record"].valid
            ]
            if blocked_keys:
                logger.warning(
                    "backup_and_delete 拒绝移入在用的有效清单（共 %d 个）: %s",
                    len(blocked_keys), blocked_keys[:10],
                )
            targets = [
                item["record"]
                for item in classified
                if not (item["category"] != "orphan" and item["record"].valid)
            ]
        else:
            # 显式按游戏删除等场景：调用方已向用户展示明确的确认信息，
            # 跳过孤儿校验，但仍只移动调用方指定的记录。
            logger.info("backup_and_delete 以显式模式移动 %d 条记录（孤儿校验已跳过）", len(records))
            targets = [r for r in records if r is not None]
        if not targets:
            logger.info("backup_and_delete：没有可安全移动的清单")
            return 0, 0, ""

        target_dir = root / _time.strftime("%Y%m%d_%H%M%S")
        moved = 0
        freed = 0
        for record in targets:
            for path in record.paths:
                source = Path(path)
                if not source.is_file():
                    continue
                try:
                    freed += source.stat().st_size
                    target_dir.mkdir(parents=True, exist_ok=True)
                    destination = target_dir / source.name
                    # 同名冲突时附加序号，避免覆盖
                    if destination.exists():
                        destination = target_dir / f"{record.key}_{moved}{source.suffix}"
                    source.replace(destination)
                    moved += 1
                except OSError as exc:
                    logger.warning("备份移动失败 %s: %s", path, exc)
        if moved:
            logger.info("已把 %d 个清单移入备份目录 %s", moved, target_dir)
        return moved, freed, str(target_dir)

    def sync(self, record: ManifestRecord) -> int:
        """把有效副本镜像到另一目录，返回新增副本数量。"""
        valid_path = next((Path(p) for p in record.paths if self.is_valid_file(p)), None)
        if valid_path is None:
            return 0
        copied = 0
        for directory in self.directories:
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{record.key}.manifest"
            if not target.exists():
                target.write_bytes(valid_path.read_bytes())
                copied += 1
        return copied

    def delete(self, key: str) -> int:
        """仅删除指定 depot_gid 在两个缓存目录中的副本。"""
        if not re.fullmatch(r"\d+_\d+", key):
            raise ValueError("invalid manifest key")
        removed = 0
        for directory in self.directories:
            target = directory / f"{key}.manifest"
            try:
                target.unlink()
                removed += 1
            except FileNotFoundError:
                pass
        return removed


def game_manifest_view(
    steam_path: str,
    lua_dirs,
    names: dict[str, str] | None = None,
) -> dict:
    """以「游戏」为单位汇总清单状态（清单管理页的默认视图数据源）。

    人类关心的是"这个游戏清单齐不齐、能不能玩"，而不是一张散落的
    manifest 文件大表。本函数把磁盘上的清单按 Lua 归属聚合成逐游戏行：

    * 游戏名解析顺序：调用方注入（游戏库/元数据）→ 已安装 appmanifest；
    * ``declared/present/missing/damaged`` 来自逐游戏审计；
    * ``file_count`` 为磁盘上归属该游戏的清单文件数（补充信息）；
    * ``orphans`` 为不属于任何 Lua/已安装游戏的残留清单（可整体清理）。

    Returns:
        ``{"games": [...], "orphans": [...], "protected_installed": int,
           "total_games": int, "ready_games": int, "incomplete_games": int}``
    """
    cache = ManifestCacheManager(steam_path)
    # 显式传空列表 = 没有 Lua 目录（不回退默认），与 classify 的语义保持一致
    directories = cache._normalize_lua_dirs(lua_dirs)
    audits = {a.app_id: a for a in cache.audit(directories)}
    classified = cache.classify(cache.scan(), directories)

    resolved_names: dict[str, str] = {}
    for app_id, name in cache.installed_app_names().items():
        resolved_names.setdefault(app_id, name)
    for app_id, name in (names or {}).items():
        if name:
            resolved_names[app_id] = name

    file_counts: dict[str, int] = {}
    orphan_records: list[ManifestRecord] = []
    protected_installed = 0
    for item in classified:
        category = item["category"]
        if category == "orphan":
            orphan_records.append(item["record"])
        elif category == "lua":
            file_counts[item["owner"]] = file_counts.get(item["owner"], 0) + 1
        else:
            protected_installed += 1

    games: list[dict] = []
    seen: set[str] = set()
    for directory in directories:
        if not directory.is_dir():
            continue
        for lua_path in sorted(directory.glob("*.lua")):
            app_id = lua_path.stem
            if not app_id.isdigit() or app_id in seen:
                continue
            seen.add(app_id)
            audit = audits.get(app_id)
            games.append(
                {
                    "app_id": app_id,
                    "name": resolved_names.get(app_id, f"AppID {app_id}"),
                    "declared": audit.declared if audit else 0,
                    "present": audit.present if audit else 0,
                    "missing": len(audit.missing) if audit else 0,
                    "damaged": len(audit.damaged) if audit else 0,
                    "missing_keys": list(audit.missing) if audit else [],
                    "damaged_keys": list(audit.damaged) if audit else [],
                    "file_count": file_counts.get(app_id, 0),
                    "ok": audit.ok if audit else True,
                }
            )
    games.sort(key=lambda g: int(g["app_id"]))
    return {
        "games": games,
        "orphans": orphan_records,
        "protected_installed": protected_installed,
        "total_games": len(games),
        "ready_games": sum(1 for g in games if g["ok"]),
        "incomplete_games": sum(1 for g in games if not g["ok"]),
    }
