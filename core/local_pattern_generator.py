"""Generate Steam hook/IPC TOMLs locally when no remote table exists.

ZSteamTool 1.2.x resolves ``opensteamtool/pattern/<component>/<sha>.toml`` and
``opensteamtool/ipc/steamclient/<sha>.toml``.  When Valve ships a new client
the community mirrors are often a few hours behind, and the kernel then
disables every hook for that module.  The vendored zero-seed resolver closes
that gap without shipping a patched kernel.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from config import LUA_DIR_RELATIVE
from core import pattern_resolver
from utils.logger import setup_logger

logger = setup_logger(__name__)

COMPONENTS = {
    "steamclient": "steamclient64.dll",
    "steamui": "steamui.dll",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class LocalPatternGenerator:
    """Resolve current DLLs into the kernel's cache layout."""

    def __init__(self, steam_path: str | Path):
        self._steam_path = Path(steam_path)
        self._root = self._steam_path / "opensteamtool"

    def pattern_path(self, component: str, sha256: str) -> Path:
        return self._root / "pattern" / component / f"{sha256}.toml"

    def ipc_path(self, sha256: str) -> Path:
        return self._root / "ipc" / "steamclient" / f"{sha256}.toml"

    def installed_channel(self) -> str:
        """Best-effort beta/stable detection from Steam's package manifests."""
        package = self._steam_path / "package"
        beta = package / "steam_client_publicbeta_win64.installed"
        stable = package / "steam_client_win64.installed"
        try:
            if beta.is_file() and (not stable.is_file() or beta.stat().st_mtime > stable.stat().st_mtime):
                return "beta"
        except OSError:
            pass
        return "stable"

    def _channel_order(self) -> list[str]:
        preferred = self.installed_channel()
        return [preferred] + [c for c in ("stable", "beta") if c != preferred]

    def generate_missing(self, *, force: bool = False) -> tuple[bool, str]:
        """Generate any missing tables; tolerate partial pattern output."""
        if not self._steam_path.is_dir():
            return False, f"Steam 目录不存在：{self._steam_path}"

        notes: list[str] = []
        fatal: list[str] = []
        pattern_ok = 0
        pattern_total = 0

        for component, dll_name in COMPONENTS.items():
            dll_path = self._steam_path / dll_name
            if not dll_path.is_file():
                fatal.append(f"{dll_name} 不存在")
                continue
            pattern_total += 1
            digest = _sha256(dll_path)
            target = self.pattern_path(component, digest)
            if target.is_file() and target.stat().st_size > 0 and not force:
                pattern_ok += 1
                continue
            try:
                written, failed = pattern_resolver.write_pattern_toml(dll_path, component, self._root / "pattern")
                pattern_ok += 1
                notes.append(f"{component} 本地生成 {written.name}")
                if failed:
                    notes.append(f"{component} 有 {len(failed)} 个 hook 未解析（内核会自动禁用）")
            except Exception as exc:  # noqa: BLE001 - resolution is best effort
                logger.warning("Local pattern generation failed for %s: %s", component, exc)
                if component == "steamclient":
                    fatal.append(f"steamclient pattern 生成失败：{exc}")
                else:
                    notes.append(f"steamui pattern 生成失败（不影响核心解锁）：{exc}")

        dll_path = self._steam_path / COMPONENTS["steamclient"]
        if dll_path.is_file():
            digest = _sha256(dll_path)
            target = self.ipc_path(digest)
            if not (target.is_file() and target.stat().st_size > 0 and not force):
                errors: list[str] = []
                for channel in self._channel_order():
                    try:
                        written = pattern_resolver.write_ipc_toml(dll_path, channel, self._root / "ipc")
                        notes.append(f"IPC 本地生成（{channel}）{written.name}")
                        target = written
                        break
                    except Exception as exc:  # noqa: BLE001 - try the other channel
                        logger.warning("Local IPC generation failed (%s): %s", channel, exc)
                        errors.append(f"{channel}: {exc}")
                else:
                    fatal.append("IPC 表生成失败：" + "；".join(errors[-2:]))

        if fatal:
            return False, "；".join(fatal + notes)
        if pattern_ok == 0:
            return False, "未生成任何 pattern 表"
        if pattern_ok < pattern_total:
            notes.insert(0, "SteamUI pattern 缺失，仅影响库界面 hook")
        return True, "；".join(notes) if notes else "本地签名表已就绪"

    def current_hashes(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for component, dll_name in COMPONENTS.items():
            path = self._steam_path / dll_name
            if path.is_file():
                result[component] = _sha256(path)
        return result

    def clear_cache(self) -> int:
        """Remove cached tables for the current DLL hashes (force refresh)."""
        removed = 0
        hashes = self.current_hashes()
        for component, digest in hashes.items():
            for path in (
                self.pattern_path(component, digest),
                self._root / "ipc" / "steamclient" / f"{digest}.toml",
            ):
                try:
                    if path.is_file():
                        path.unlink()
                        removed += 1
                except OSError:
                    pass
        return removed


def lua_dirs(steam_path: str | Path) -> list[str]:
    """Both supported Lua directories, modern first."""
    steam = Path(steam_path)
    return [
        str((steam / LUA_DIR_RELATIVE).resolve()),
        str((steam / "config" / "stplug-in").resolve()),
    ]
