"""Read/merge/write ``<Steam>/opensteamtool.toml`` safely.

The kernel (ZSteamTool 1.2.x) reads a small set of sections.  Older releases
of this desktop app used to overwrite the whole file, which destroyed user
customisations.  This module keeps every existing value and only fills in
missing keys.  A ``.bak`` copy is written before the first rewrite.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from config import (
    LEGACY_LUA_DIR_RELATIVE,
    LUA_DIR_RELATIVE,
    STEAM_PATTERN_URL_TEMPLATES,
)

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover - project ships 3.13
    tomllib = None  # type: ignore[assignment]


CONFIG_FILE = "opensteamtool.toml"

DEFAULT_CONFIG: dict[str, Any] = {
    "log": {"level": "info"},
    "manifest": {
        "url": "wudrm",
        "timeout_resolve_ms": 5000,
        "timeout_connect_ms": 5000,
        "timeout_send_ms": 10000,
        "timeout_recv_ms": 10000,
    },
    "stats": {"enable_api": True},
    "presence": {"display": "spacewar"},
    "denuvo": {"mode": "normal"},
    "onlinefix": {"env_session": True},
    "cloud": {"enabled": False, "library": "cloud_redirect.dll"},
    "remote": {
        "url_template": list(STEAM_PATTERN_URL_TEMPLATES),
        "force_refresh": False,
    },
}


def config_path(steam_path: str | os.PathLike[str]) -> Path:
    return Path(steam_path) / CONFIG_FILE


def load_config(steam_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Parse the config; return ``{}`` for a missing or invalid file."""
    path = config_path(steam_path)
    if not path.is_file() or tomllib is None:
        return {}
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get_value(steam_path: str | os.PathLike[str], section: str, key: str, default: Any = None) -> Any:
    data = load_config(steam_path)
    table = data.get(section)
    if isinstance(table, dict):
        return table.get(key, default)
    return default


def _merge_defaults(data: dict[str, Any]) -> bool:
    changed = False
    for section, values in DEFAULT_CONFIG.items():
        table = data.get(section)
        if not isinstance(table, dict):
            data[section] = dict(values)
            changed = True
            continue
        for key, value in values.items():
            if key not in table:
                table[key] = list(value) if isinstance(value, list) else value
                changed = True

    # Known-dead default provider: safe to migrate because the kernel's own
    # built-in default is the same value.  Any explicit other value is kept.
    manifest = data.get("manifest")
    if isinstance(manifest, dict) and manifest.get("url") == "opensteamtool":
        manifest["url"] = "wudrm"
        changed = True

    # Keep both the modern and legacy Lua directories visible.
    lua = data.get("lua")
    if not isinstance(lua, dict):
        lua = {}
        data["lua"] = lua
        changed = True
    paths = lua.get("paths")
    if not isinstance(paths, list):
        paths = []
        lua["paths"] = paths
        changed = True
    return changed


def merge_config(
    steam_path: str | os.PathLike[str],
    *,
    lua_dirs: list[str] | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> tuple[bool, str]:
    """Fill missing defaults and optional overrides, preserving user values."""
    steam = Path(steam_path)
    if not steam.is_dir():
        return False, f"Steam 目录不存在：{steam}"
    path = config_path(steam)
    data = load_config(steam)
    changed = _merge_defaults(data)

    # Ensure both Lua paths exist and are listed (deduplicated, order kept).
    wanted = lua_dirs or [
        str((steam / LUA_DIR_RELATIVE).resolve()),
        str((steam / LEGACY_LUA_DIR_RELATIVE).resolve()),
    ]
    lua = data.setdefault("lua", {})
    paths = lua.setdefault("paths", [])
    normalised = {str(p).replace("\\", "/").lower() for p in paths if isinstance(p, str)}
    for item in wanted:
        posix = str(item).replace("\\", "/")
        if posix.lower() not in normalised:
            paths.append(posix)
            normalised.add(posix.lower())
            changed = True

    if overrides:
        for section, values in overrides.items():
            table = data.get(section)
            if not isinstance(table, dict):
                table = {}
                data[section] = table
            for key, value in values.items():
                if table.get(key) != value:
                    table[key] = value
                    changed = True

    if not changed and path.is_file():
        return True, "配置已是最新"

    try:
        if path.is_file():
            backup = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, backup)
        path.write_text(dumps_toml(data), encoding="utf-8", newline="\n")
    except OSError as exc:
        return False, f"写入 {CONFIG_FILE} 失败：{exc}"
    return True, f"{CONFIG_FILE} 已更新"


def update_config(
    steam_path: str | os.PathLike[str],
    updates: dict[str, dict[str, Any]],
) -> tuple[bool, str]:
    """Force specific values (used by the settings UI)."""
    return merge_config(steam_path, overrides=updates)


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _dump_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_dump_value(item) for item in value) + "]"
    return _quote(str(value))


def dumps_toml(data: dict[str, Any]) -> str:
    """Serialize the small, flat-table config schema used by the kernel."""
    lines: list[str] = [
        "# opensteamtool.toml — managed by OpenSteamToolDesktop",
        "# Existing values are preserved; missing sections are added automatically.",
        "",
    ]
    for key, value in data.items():
        if not isinstance(value, dict):
            lines.append(f"{key} = {_dump_value(value)}")
    if any(not isinstance(value, dict) for value in data.values()):
        lines.append("")

    for section, table in data.items():
        if not isinstance(table, dict):
            continue
        lines.append(f"[{section}]")
        for key, value in table.items():
            if isinstance(value, dict):
                continue
            lines.append(f"{key} = {_dump_value(value)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
