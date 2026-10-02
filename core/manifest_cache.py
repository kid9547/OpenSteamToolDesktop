"""Steam depotcache 双目录清单管理。

该模块只处理本地文件：扫描、校验、去重、引用关系和定向清理。
它不会删除未明确指定的文件，也不会修改 Steam 授权数据。
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

    def referenced_keys(self, lua_dir: str | Path | None = None) -> set[str]:
        if lua_dir:
            directory = Path(lua_dir)
        elif self.steam_path:
            directory = self.steam_path / "config" / "lua"
        else:
            return set()
        referenced: set[str] = set()
        if not directory.is_dir():
            return referenced
        pattern = re.compile(
            r"setmanifestid\s*\(\s*(\d+)\s*,\s*[\"']?(\d+)",
            re.IGNORECASE,
        )
        for lua_path in directory.glob("*.lua"):
            try:
                text = lua_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            referenced.update(f"{depot}_{gid}" for depot, gid in pattern.findall(text))
        return referenced

    # ── 逐游戏完整性审计 ──────────────────────────────────────
    def audit(self, lua_dir: str | Path | None = None) -> list["AppManifestAudit"]:
        """逐游戏比对「Lua 声明的 (depot, gid)」与「本地实际存在的清单」。

        这是"清单下载不全"的**检测**手段：只有逐游戏列出缺了哪些 depot 的清单，
        用户才能知道该补什么；此前只能看到一堆孤立的 manifest 文件。

        Returns:
            按 AppID 排序的 :class:`AppManifestAudit` 列表（仅包含声称有
            ``setManifestid`` 的游戏；完全没有清单绑定的游戏不参与判定）。
        """
        if lua_dir:
            directory = Path(lua_dir)
        elif self.steam_path:
            directory = self.steam_path / "config" / "lua"
        else:
            return []
        if not directory.is_dir():
            return []

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
        return sorted(audits, key=lambda item: int(item.app_id))

    def orphaned(self, lua_dir: str | Path | None = None) -> list[ManifestRecord]:
        referenced = self.referenced_keys(lua_dir)
        return [record for record in self.scan() if record.key not in referenced]

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
