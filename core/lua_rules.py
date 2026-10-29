"""Lua 规则管理后端。

能力：

* 同时扫描 ``config/lua`` 与 ``config/stplug-in``（以及额外目录）；
* 识别启用 / 禁用文件（``.lua`` ↔ ``.lua.disabled``）；
* 检测同一 AppID 被多个启用规则重复声明；
* 从本地 ``appmanifest_*.acf`` 读取已安装 Depot 与清单 GID，并从现有
  Lua 规则中复用 Depot Key，一键补全当前规则缺失的 ``addappid`` /
  ``setManifestid``。

所有写操作都会先保留 ``.bak``，并且遵循 :mod:`core.manifest_pins` 的固定
版本设置，不会把用户锁定的 GID 覆盖掉。
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.complete_manifest_service import (
    parse_lua_bindings,
    update_lua_depot_key,
    update_lua_manifest,
)
from core.local_scanner import LocalGameScanner, parse_vdf
from core.manifest_cache import ManifestCacheManager
from utils.logger import setup_logger

logger = setup_logger(__name__)

_MANAGED_COMMENT = re.compile(r"^--\s*(.+?)(?:\s*\(.*?\))?\s*$", re.MULTILINE)


class LuaRuleError(RuntimeError):
    pass


@dataclass
class LuaRule:
    """单个 Lua 规则文件。"""

    path: str
    app_id: str
    name: str
    enabled: bool
    size: int
    mtime: float
    directory: str
    conflicts: list[str] = field(default_factory=list)

    @property
    def filename(self) -> str:
        return Path(self.path).name

    @property
    def status(self) -> str:
        return "启用" if self.enabled else "禁用"


@dataclass
class CacheBindings:
    """从 Steam 本地缓存整理出的 Depot 绑定。"""

    app_id: str
    acf_path: str = ""
    app_key: str = ""
    depots: list[dict] = field(default_factory=list)
    source: str = "appmanifest"

    @property
    def has_data(self) -> bool:
        return bool(self.depots)


def lua_dirs(steam_path: str, extra_dirs: list[str] | None = None) -> list[str]:
    """返回需要扫描的 Lua 目录（去重、仅保留存在目录）。"""
    candidates: list[str] = []
    if steam_path:
        candidates.append(os.path.join(steam_path, "config", "lua"))
        candidates.append(os.path.join(steam_path, "config", "stplug-in"))
    candidates.extend(extra_dirs or [])
    result: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        if not path:
            continue
        key = os.path.normcase(os.path.abspath(path))
        if key in seen or not os.path.isdir(path):
            continue
        seen.add(key)
        result.append(path)
    return result


def _parse_name(content: str) -> str:
    noise = (
        "lua",
        "manifest",
        "created",
        "website",
        "total",
        "shared",
        "main application",
        "main app",
        "http",
    )
    for match in _MANAGED_COMMENT.finditer(content):
        name = match.group(1).strip()
        if not name or "由" in name:
            continue
        lowered = name.lower()
        if lowered.startswith("appid") or any(word in lowered for word in noise):
            continue
        return name
    return ""


def _app_id_from_name(filename: str) -> str:
    name = filename
    if name.lower().endswith(".disabled"):
        name = name[: -len(".disabled")]
    if name.lower().endswith(".lua"):
        name = name[: -len(".lua")]
    return name if name.isdigit() else ""


def scan_lua_rules(
    steam_path: str, extra_dirs: list[str] | None = None
) -> list[LuaRule]:
    """扫描全部 Lua 规则并标记重复声明冲突。"""
    rules: list[LuaRule] = []
    for directory in lua_dirs(steam_path, extra_dirs):
        try:
            entries = list(Path(directory).iterdir())
        except OSError as exc:
            logger.debug("读取 Lua 目录失败 %s: %s", directory, exc)
            continue
        for path in entries:
            if not path.is_file():
                continue
            lower = path.name.lower()
            if not (lower.endswith(".lua") or lower.endswith(".lua.disabled")):
                continue
            app_id = _app_id_from_name(path.name)
            if not app_id:
                continue
            if path.name.lower().startswith("manifest.lua"):
                continue
            try:
                stat = path.stat()
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.debug("读取 Lua 失败 %s: %s", path, exc)
                continue
            rules.append(
                LuaRule(
                    path=str(path),
                    app_id=app_id,
                    name=_parse_name(content),
                    enabled=lower.endswith(".lua"),
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    directory=str(path.parent),
                )
            )

    # 冲突：同一 AppID 有多个启用规则；或启用/禁用副本并存。
    by_app: dict[str, list[LuaRule]] = {}
    for rule in rules:
        by_app.setdefault(rule.app_id, []).append(rule)
    for app_id, group in by_app.items():
        enabled = [rule for rule in group if rule.enabled]
        if len(enabled) > 1:
            message = f"重复 AppID：{len(enabled)} 个启用文件"
            for rule in enabled:
                rule.conflicts.append(message)
        elif len(group) > 1:
            for rule in group:
                rule.conflicts.append("启用/禁用副本并存")

    rules.sort(key=lambda item: (int(item.app_id), not item.enabled, item.path))
    return rules


def set_enabled(path: str | Path, enabled: bool) -> Path:
    """启用 / 禁用规则文件（重命名），返回新路径。"""
    source = Path(path)
    if not source.is_file():
        raise LuaRuleError(f"规则文件不存在：{source}")
    lower = source.name.lower()
    if enabled:
        if not lower.endswith(".lua.disabled"):
            return source
        target = source.with_name(source.name[: -len(".disabled")])
    else:
        if not lower.endswith(".lua"):
            return source
        target = source.with_name(source.name + ".disabled")
    if target.exists():
        raise LuaRuleError(f"目标文件已存在：{target}")
    source.rename(target)
    logger.info("Lua 规则已%s：%s", "启用" if enabled else "禁用", target)
    return target


def _installed_depots(steam_path: str, app_id: str) -> tuple[list[dict], str]:
    """从 appmanifest 读取已安装 Depot（含 GID / size）。"""
    scanner = LocalGameScanner(steam_path)
    for library in scanner.find_steam_libraries(scan_all_drives=False):
        acf = Path(library) / "steamapps" / f"appmanifest_{app_id}.acf"
        if not acf.is_file():
            continue
        try:
            parsed = parse_vdf(acf.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        app_state = parsed.get("AppState", {})
        installed = app_state.get("InstalledDepots", {}) if isinstance(app_state, dict) else {}
        depots: list[dict] = []
        if isinstance(installed, dict):
            for depot_id, info in installed.items():
                if not str(depot_id).isdigit() or not isinstance(info, dict):
                    continue
                gid = str(info.get("manifest") or "").strip()
                if not gid.isdigit() or gid == "0":
                    continue
                try:
                    size = int(info.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                depots.append(
                    {"depot_id": str(depot_id), "gid": gid, "size": max(size, 0)}
                )
        return depots, str(acf)
    return [], ""


def _collect_keys(steam_path: str, extra_dirs: list[str] | None = None) -> dict[str, str]:
    """从所有 Lua 规则中收集 Depot Key（后出现的非空值覆盖）。"""
    keys: dict[str, str] = {}
    for directory in lua_dirs(steam_path, extra_dirs):
        try:
            entries = list(Path(directory).iterdir())
        except OSError:
            continue
        for path in entries:
            if not path.is_file():
                continue
            lower = path.name.lower()
            if not (lower.endswith(".lua") or lower.endswith(".lua.disabled")):
                continue
            for depot, binding in parse_lua_bindings(path).items():
                key = str(binding.get("key") or "").strip()
                if key:
                    keys[depot] = key
    return keys


def cache_bindings(
    steam_path: str, app_id: str, extra_dirs: list[str] | None = None
) -> CacheBindings:
    """整理某个 AppID 的本地缓存绑定（Depot / GID / Key）。"""
    app_id = str(app_id).strip()
    depots, acf_path = _installed_depots(steam_path, app_id)
    keys = _collect_keys(steam_path, extra_dirs)
    cache = ManifestCacheManager(steam_path)

    result = CacheBindings(app_id=app_id, acf_path=acf_path)
    result.app_key = keys.get(app_id, "")
    for depot in depots:
        depot_id = depot["depot_id"]
        gid = depot["gid"]
        result.depots.append(
            {
                **depot,
                "key": keys.get(depot_id, ""),
                "present": cache.is_present(depot_id, gid),
            }
        )

    if not result.depots:
        # 没有 appmanifest 时，退回到当前 Lua 中已有的 GID 绑定。
        result.source = "lua"
        for directory in lua_dirs(steam_path, extra_dirs):
            for suffix in (".lua", ".lua.disabled"):
                lua_path = Path(directory) / f"{app_id}{suffix}"
                if not lua_path.is_file():
                    continue
                for depot_id, binding in parse_lua_bindings(lua_path).items():
                    gid = str(binding.get("gid") or "").strip()
                    if not gid.isdigit():
                        continue
                    result.depots.append(
                        {
                            "depot_id": depot_id,
                            "gid": gid,
                            "size": int(binding.get("size") or 0),
                            "key": keys.get(depot_id, ""),
                            "present": cache.is_present(depot_id, gid),
                        }
                    )
                break
    return result


def _backup(path: Path) -> Path:
    backup = path.with_name(path.name + ".bak")
    try:
        backup.write_bytes(path.read_bytes())
    except OSError as exc:
        logger.warning("备份 Lua 失败 %s: %s", path, exc)
    return backup


def apply_cache_bindings(
    lua_path: str | Path, bindings: CacheBindings
) -> dict[str, int]:
    """把缓存绑定写入 Lua（幂等），返回各项计数。"""
    path = Path(lua_path)
    if not path.is_file():
        raise LuaRuleError(f"规则文件不存在：{path}")
    if not bindings.has_data:
        return {"added": 0, "keys": 0, "manifests": 0}

    _backup(path)
    content = path.read_text(encoding="utf-8", errors="replace")
    added = 0
    keys = 0
    manifests = 0

    def _ensure_addappid(depot_id: str, key: str) -> bool:
        nonlocal content
        pattern = re.compile(
            rf"addappid\s*\(\s*{re.escape(depot_id)}\s*(?:,|\))", re.IGNORECASE
        )
        if pattern.search(content):
            return False
        line = f'addappid({depot_id}, 0, "{key}")' if key else f"addappid({depot_id})"
        content = content.rstrip() + f"\n{line}\n"
        return True

    if bindings.app_key and _ensure_addappid(bindings.app_id, bindings.app_key):
        added += 1
    for depot in bindings.depots:
        if _ensure_addappid(str(depot["depot_id"]), str(depot.get("key") or "")):
            added += 1

    # 先落盘新增的 addappid，再由专用 helper 就地更新 key / manifest。
    if content != path.read_text(encoding="utf-8", errors="replace"):
        path.write_text(content, encoding="utf-8")

    for depot in bindings.depots:
        depot_id = str(depot["depot_id"])
        if depot.get("key") and update_lua_depot_key(path, depot_id, str(depot["key"])):
            keys += 1
        if update_lua_manifest(
            path,
            depot_id,
            str(depot["gid"]),
            int(depot.get("size") or 0),
        ):
            manifests += 1

    summary = {"added": added, "keys": keys, "manifests": manifests}
    logger.info("Lua 缓存补全 %s：%s", path.name, summary)
    return summary
