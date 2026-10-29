"""Manifest 版本固定（pin）存储与 Lua 应用。

用户可以把某个 Depot 的清单 GID 固定下来，之后：

* 重新生成 Lua 时优先写入固定 GID；
* 「一键补全」/归档更新不会把 Lua 里的 ``setManifestid`` 覆盖成新版本；
* 取消固定后恢复正常跟随。

存储位置：``%LOCALAPPDATA%/OpenSteamToolDesktop/manifest_pins.json``。
结构：``{"<appid>": {"<depot>": {"gid": "...", "size": 0, "pinned_at": ...}}}``。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from core.app_paths import app_data_dir
from utils.logger import setup_logger

logger = setup_logger(__name__)

PIN_FILENAME = "manifest_pins.json"
_MANIFEST_RE = re.compile(
    r'setmanifestid\s*\(\s*(?P<depot>\d+)\s*,\s*["\']?(?P<gid>\d+)["\']?'
    r'(?P<rest>\s*,\s*[^)]*)?\)',
    re.IGNORECASE,
)
_ADDAPPID_RE = re.compile(r"addappid\s*\(\s*(\d+)\s*\)", re.IGNORECASE)


def _store_file(store_path: str | Path | None = None) -> Path:
    if store_path is not None:
        return Path(store_path)
    return app_data_dir() / PIN_FILENAME


def load_pins(store_path: str | Path | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    """读取全部固定记录；文件损坏时返回空字典。"""
    path = _store_file(store_path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("读取 Manifest 固定记录失败 %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    cleaned: dict[str, dict[str, dict[str, Any]]] = {}
    for app_id, depots in data.items():
        if not str(app_id).isdigit() or not isinstance(depots, dict):
            continue
        entries: dict[str, dict[str, Any]] = {}
        for depot_id, payload in depots.items():
            if not str(depot_id).isdigit() or not isinstance(payload, dict):
                continue
            gid = str(payload.get("gid") or "").strip()
            if not gid.isdigit():
                continue
            try:
                size = int(payload.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            entries[str(depot_id)] = {
                "gid": gid,
                "size": max(size, 0),
                "pinned_at": payload.get("pinned_at") or 0,
            }
        if entries:
            cleaned[str(app_id)] = entries
    return cleaned


def save_pins(
    data: dict[str, dict[str, dict[str, Any]]],
    store_path: str | Path | None = None,
) -> bool:
    path = _store_file(store_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(path)
        return True
    except OSError as exc:
        logger.warning("保存 Manifest 固定记录失败 %s: %s", path, exc)
        return False


def get_pins(
    app_id: str, store_path: str | Path | None = None
) -> dict[str, dict[str, Any]]:
    return load_pins(store_path).get(str(app_id), {})


def has_pins(app_id: str, store_path: str | Path | None = None) -> bool:
    return bool(get_pins(app_id, store_path))


def pin(
    app_id: str,
    depot_id: str,
    gid: str,
    size: int = 0,
    store_path: str | Path | None = None,
) -> bool:
    """固定一个 Depot 的 GID。"""
    app_id = str(app_id).strip()
    depot_id = str(depot_id).strip()
    gid = str(gid).strip()
    if not (app_id.isdigit() and depot_id.isdigit() and gid.isdigit()):
        return False
    data = load_pins(store_path)
    data.setdefault(app_id, {})[depot_id] = {
        "gid": gid,
        "size": max(int(size or 0), 0),
        "pinned_at": time.time(),
    }
    return save_pins(data, store_path)


def unpin(
    app_id: str,
    depot_id: str | None = None,
    store_path: str | Path | None = None,
) -> int:
    """取消固定；``depot_id`` 为空时取消该游戏的全部固定。返回取消数量。"""
    app_id = str(app_id).strip()
    data = load_pins(store_path)
    if app_id not in data:
        return 0
    if depot_id is None:
        removed = len(data.pop(app_id))
    else:
        removed = 1 if data[app_id].pop(str(depot_id).strip(), None) is not None else 0
        if not data[app_id]:
            data.pop(app_id, None)
    if removed:
        save_pins(data, store_path)
    return removed


def pinned_gid(
    app_id: str, depot_id: str, store_path: str | Path | None = None
) -> str | None:
    entry = get_pins(app_id, store_path).get(str(depot_id))
    return str(entry.get("gid")) if entry else None


def pinned_size(
    app_id: str, depot_id: str, store_path: str | Path | None = None
) -> int:
    entry = get_pins(app_id, store_path).get(str(depot_id))
    return int(entry.get("size") or 0) if entry else 0


def enforce_pinned_gid(
    app_id: str,
    depot_id: str,
    gid: str,
    store_path: str | Path | None = None,
) -> str:
    """返回应写入 Lua 的 GID：有固定值时用固定值，否则用传入值。"""
    pinned = pinned_gid(app_id, depot_id, store_path)
    return pinned or str(gid)


def lua_app_id(lua_path: str | Path) -> str:
    name = Path(lua_path).name
    for suffix in (".disabled", ".lua"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
    return name if name.isdigit() else ""


def apply_pins_to_lua(
    app_id: str,
    lua_path: str | Path,
    store_path: str | Path | None = None,
) -> int:
    """把已固定的 GID 写回 Lua（幂等）。返回实际改写的 Depot 数量。"""
    pins = get_pins(app_id, store_path)
    if not pins:
        return 0
    path = Path(lua_path)
    if not path.is_file():
        return 0
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("读取 Lua 失败 %s: %s", path, exc)
        return 0

    changed = 0
    for depot_id, entry in pins.items():
        gid = str(entry.get("gid") or "")
        if not gid.isdigit():
            continue
        size = int(entry.get("size") or 0)
        new_line = (
            f'setManifestid({depot_id}, "{gid}", {size})'
            if size > 0
            else f'setManifestid({depot_id}, "{gid}")'
        )
        pattern = re.compile(
            rf'setmanifestid\s*\(\s*{depot_id}\s*,\s*["\']?\d+["\']?'
            rf'(?:\s*,\s*[^)]*)?\)',
            re.IGNORECASE,
        )
        if pattern.search(content):
            new_content, count = pattern.subn(new_line, content, count=1)
            if count and new_content != content:
                content = new_content
                changed += 1
        else:
            # 只有该 Depot 已经在 Lua 中注册时才追加，避免凭空添加未知 Depot。
            if re.search(rf"addappid\s*\(\s*{depot_id}\s*(?:,|\))", content, re.IGNORECASE):
                content = content.rstrip() + f"\n{new_line}\n"
                changed += 1

    if changed:
        try:
            path.write_text(content, encoding="utf-8")
            logger.info("已把 %d 个固定 GID 写回 %s", changed, path)
        except OSError as exc:
            logger.warning("写入 Lua 失败 %s: %s", path, exc)
            return 0
    return changed


def apply_all_pins_to_lua(store_path: str | Path | None = None) -> int:
    """把固定记录应用到所有已知 Lua（用于设置页/启动时修复）。"""
    data = load_pins(store_path)
    if not data:
        return 0
    changed = 0
    # 延迟导入，避免模块级循环依赖。
    from core.steam_detector import SteamDetector

    detection = SteamDetector().detect()
    steam_path = detection.path or ""
    if not steam_path:
        return 0
    for app_id in data:
        for directory in (
            Path(steam_path) / "config" / "lua",
            Path(steam_path) / "config" / "stplug-in",
        ):
            for suffix in (".lua", ".lua.disabled"):
                lua_path = directory / f"{app_id}{suffix}"
                if lua_path.is_file():
                    changed += apply_pins_to_lua(app_id, lua_path, store_path)
                    break
    return changed


def describe(app_id: str, store_path: str | Path | None = None) -> str:
    pins = get_pins(app_id, store_path)
    if not pins:
        return "未固定"
    return f"已固定 {len(pins)} 个 Depot"
