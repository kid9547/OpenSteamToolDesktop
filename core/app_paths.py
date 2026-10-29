"""Runtime path helpers for development, PyInstaller and Nuitka builds."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def resource_root() -> Path:
    """Directory that contains bundled ``resources/`` and ``tools/`` data."""
    meipass = getattr(sys, "_MEIPASS", "")
    if getattr(sys, "frozen", False) and meipass:
        return Path(meipass)

    # Nuitka onefile extracts data next to the compiled module; standalone
    # keeps it next to the executable.  Check both before the source tree.
    if hasattr(sys, "__compiled__"):
        module_root = Path(__file__).resolve().parent.parent
        if (module_root / "resources").is_dir() or (module_root / "tools").is_dir():
            return module_root
        exe_root = Path(sys.executable).resolve().parent
        if (exe_root / "resources").is_dir() or (exe_root / "tools").is_dir():
            return exe_root

    return Path(__file__).resolve().parent.parent


def app_data_dir() -> Path:
    """Writable per-user directory for caches, extracted tickets and logs."""
    base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
    root = Path(base) if base else Path.home()
    target = root / "OpenSteamToolDesktop"
    target.mkdir(parents=True, exist_ok=True)
    return target


def bundled_resource(*parts: str) -> Path:
    return resource_root().joinpath("resources", *parts)


def bundled_tool(*parts: str) -> Path:
    return resource_root().joinpath("tools", *parts)
