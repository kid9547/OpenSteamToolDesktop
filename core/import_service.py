"""
清单与 Lua 配置文件导入服务
============================

支持：
1. 单个或多个文件导入（.lua, .manifest, .zip 压缩包）
2. 文件夹递归扫描批量导入
3. 拖拽（Drag & Drop）文件或目录导入
4. 自动提取非数字命名的 Lua 文件中的真实 AppID
5. 自动部署清单到 Steam/depotcache 与 Steam/config/depotcache
6. 自动刷新游戏库并确保 manifest.lua 解析器配置
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from utils.logger import setup_logger

if TYPE_CHECKING:
    from core.game_manager import LuaGameManager

logger = setup_logger(__name__)


@dataclass
class ImportResult:
    """导入结果统计"""
    total_files_scanned: int = 0
    lua_count: int = 0
    manifest_count: int = 0
    app_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    success: bool = True

    @property
    def total_imported(self) -> int:
        return self.lua_count + self.manifest_count

    def summary(self) -> str:
        parts = []
        if self.lua_count > 0:
            parts.append(f"{self.lua_count} 个 Lua 配置文件")
        if self.manifest_count > 0:
            parts.append(f"{self.manifest_count} 个 Manifest 清单")
        if not parts:
            if self.errors:
                return f"未找到有效的清单或 Lua 文件，错误：{'; '.join(self.errors[:3])}"
            return "未找到任何 .lua 或 .manifest 文件"
        msg = f"成功导入 {'、'.join(parts)}"
        if self.app_ids:
            unique_ids = list(dict.fromkeys(self.app_ids))
            if len(unique_ids) <= 5:
                msg += f" (AppID: {', '.join(unique_ids)})"
            else:
                msg += f" (共 {len(unique_ids)} 款游戏)"
        if self.errors:
            msg += f"，其中 {len(self.errors)} 个文件处理失败"
        return msg


class ImportService:
    """清单与 Lua 导入管理器"""

    def __init__(self, steam_path: str = "", game_manager: LuaGameManager | None = None):
        self._steam_path = steam_path
        self._game_manager = game_manager
        self._lua_dir = ""
        self._depotcache_dir = ""
        self._config_depotcache_dir = ""
        self._update_paths()

    def set_steam_path(self, steam_path: str) -> None:
        """更新 Steam 安装路径"""
        self._steam_path = steam_path
        self._update_paths()

    def _update_paths(self) -> None:
        if self._steam_path:
            self._lua_dir = os.path.join(self._steam_path, "config", "lua")
            self._depotcache_dir = os.path.join(self._steam_path, "depotcache")
            self._config_depotcache_dir = os.path.join(self._steam_path, "config", "depotcache")
        else:
            self._lua_dir = ""
            self._depotcache_dir = ""
            self._config_depotcache_dir = ""

    def import_paths(self, paths: list[str | Path]) -> ImportResult:
        """从文件或目录列表中批量导入清单与 Lua 文件

        Args:
            paths: 文件或目录路径列表

        Returns:
            ImportResult: 导入结果
        """
        result = ImportResult()

        if not self._steam_path or not os.path.isdir(self._steam_path):
            result.success = False
            result.errors.append("未检测到有效的 Steam 安装目录，无法导入")
            return result

        # 确保目标目录存在
        try:
            os.makedirs(self._lua_dir, exist_ok=True)
            os.makedirs(self._depotcache_dir, exist_ok=True)
            os.makedirs(self._config_depotcache_dir, exist_ok=True)
        except OSError as e:
            result.success = False
            result.errors.append(f"创建目标目录失败: {e}")
            return result

        # 展开并递归查找所有目标文件
        file_candidates = self._collect_files(paths, result)

        logger.info(f"ImportService: found {len(file_candidates)} candidate files to import")

        for file_path in file_candidates:
            result.total_files_scanned += 1
            lower_name = file_path.name.lower()
            try:
                if lower_name.endswith(".lua"):
                    self._import_lua(file_path, result)
                elif lower_name.endswith(".manifest"):
                    self._import_manifest(file_path, result)
            except Exception as e:
                logger.error(f"Error importing {file_path}: {e}", exc_info=True)
                result.errors.append(f"{file_path.name}: {e}")

        # 导入完成后，刷新 GameManager 游戏库并确保 manifest 解析器配置
        if self._game_manager:
            try:
                self._game_manager.refresh()
                if hasattr(self._game_manager, "_ensure_manifest_resolver"):
                    self._game_manager._ensure_manifest_resolver()
            except Exception as e:
                logger.warning(f"Failed to refresh game manager after import: {e}")

        result.success = (result.total_imported > 0)
        return result

    def _collect_files(self, paths: list[str | Path], result: ImportResult) -> list[Path]:
        """递归收集所有需要导入的文件，并解压处理 zip 包"""
        collected: list[Path] = []

        for p in paths:
            path_obj = Path(p).resolve()
            if not path_obj.exists():
                logger.warning(f"Import path not found: {path_obj}")
                continue

            if path_obj.is_dir():
                # 递归遍历目录
                for root, _, files in os.walk(path_obj):
                    for f in files:
                        fp = Path(root) / f
                        ext = fp.suffix.lower()
                        if ext in (".lua", ".manifest"):
                            collected.append(fp)
                        elif ext == ".zip":
                            collected.extend(self._extract_zip(fp, result))
            elif path_obj.is_file():
                ext = path_obj.suffix.lower()
                if ext in (".lua", ".manifest"):
                    collected.append(path_obj)
                elif ext == ".zip":
                    collected.extend(self._extract_zip(path_obj, result))

        return collected

    def _extract_zip(self, zip_path: Path, result: ImportResult) -> list[Path]:
        """从 ZIP 压缩包中提取 .lua 和 .manifest 文件到临时目录"""
        extracted_files: list[Path] = []
        try:
            temp_dir = Path(tempfile.mkdtemp(prefix="ost_import_"))
            with zipfile.ZipFile(zip_path, "r") as zf:
                for member in zf.namelist():
                    member_lower = member.lower()
                    if member_lower.endswith(".lua") or member_lower.endswith(".manifest"):
                        # 仅提取安全文件名
                        filename = os.path.basename(member)
                        if not filename:
                            continue
                        target = temp_dir / filename
                        with zf.open(member) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        extracted_files.append(target)
                        logger.debug(f"Extracted from {zip_path.name}: {filename}")
        except Exception as e:
            logger.error(f"Failed to extract zip file {zip_path}: {e}")
            result.errors.append(f"解压 {zip_path.name} 失败: {e}")

        return extracted_files

    def _import_lua(self, file_path: Path, result: ImportResult) -> None:
        """处理并导入单个 .lua 文件"""
        stem = file_path.stem
        app_id = ""

        # 读取内容
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            result.errors.append(f"读取 {file_path.name} 失败: {e}")
            return

        if stem.isdigit():
            app_id = stem
        else:
            # 文件名非数字，尝试从内容中提取第一个 addappid
            match = re.search(r'addappid\(\s*(\d+)', content, re.IGNORECASE)
            if match:
                app_id = match.group(1)
                logger.info(f"Extracted AppID {app_id} from non-numeric Lua file: {file_path.name}")

        target_name = f"{app_id}.lua" if app_id else file_path.name
        target_path = Path(self._lua_dir) / target_name

        shutil.copy2(file_path, target_path)
        logger.info(f"Imported Lua: {file_path.name} -> {target_path}")

        result.lua_count += 1
        if app_id:
            result.app_ids.append(app_id)

    def _import_manifest(self, file_path: Path, result: ImportResult) -> None:
        """处理并导入单个 .manifest 文件"""
        filename = file_path.name

        # 尝试规范化文件名，例如 Cyberpunk_1091501_12345678.manifest -> 1091501_12345678.manifest
        match = re.search(r'(\d+_\d+)\.manifest$', filename, re.IGNORECASE)
        if match:
            target_name = f"{match.group(1)}.manifest"
        else:
            target_name = filename

        # 拷贝到 Steam/depotcache/
        target_depot = Path(self._depotcache_dir) / target_name
        shutil.copy2(file_path, target_depot)

        # 同时也拷贝到 Steam/config/depotcache/
        if self._config_depotcache_dir:
            try:
                target_config_depot = Path(self._config_depotcache_dir) / target_name
                shutil.copy2(file_path, target_config_depot)
            except Exception as e:
                logger.debug(f"Could not copy to config/depotcache: {e}")

        logger.info(f"Imported Manifest: {file_path.name} -> {target_depot}")
        result.manifest_count += 1
