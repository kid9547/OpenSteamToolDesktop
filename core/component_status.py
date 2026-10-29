"""组件状态中心后端。

回答三个问题：

1. 三个内核 DLL 是否已经部署到 Steam 根目录；
2. Steam 进程**实际加载**的是哪一个文件（同名系统 DLL 也会被识别出来）；
3. 当前使用的内核版本 / 来源，以及一键移除、重新部署所需的信息。

实现只依赖 Windows 自带的 Toolhelp API（``kernel32``），不引入 psutil 等
额外依赖。读取模块列表失败（例如 Steam 以管理员权限运行）时会返回空列表，
界面会提示"无法确认"，而不是误报为未加载。
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

from core.app_paths import app_data_dir
from utils.logger import setup_logger

logger = setup_logger(__name__)

COMPONENT_DLLS: tuple[str, ...] = ("OpenSteamTool.dll", "dwmapi.dll", "xinput1_4.dll")

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
MAX_PATH = 260
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * MAX_PATH),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.POINTER(wintypes.BYTE)),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * 256),
        ("szExePath", wintypes.WCHAR * MAX_PATH),
    ]


def _kernel32():
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
    kernel32.Module32FirstW.restype = wintypes.BOOL
    kernel32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
    kernel32.Module32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


@dataclass
class ComponentStatus:
    """单个 DLL 的部署 / 加载状态。"""

    name: str
    file_path: str
    deployed: bool
    file_size: int = 0
    sha256: str = ""
    loaded: bool = False
    loaded_path: str = ""
    loaded_from_steam: bool = False
    note: str = ""


@dataclass
class ComponentReport:
    """一次组件状态检测的聚合结果。"""

    steam_path: str = ""
    source_dir: str = ""
    kernel_version: str = ""
    steam_running: bool = False
    steam_pids: list[int] = field(default_factory=list)
    components: list[ComponentStatus] = field(default_factory=list)
    injected: bool = False
    summary: str = ""


def _iter_processes() -> list[tuple[int, str]]:
    kernel32 = _kernel32()
    if kernel32 is None:
        return []
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return []
    result: list[tuple[int, str]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            result.append((int(entry.th32ProcessID), str(entry.szExeFile)))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return result


def find_steam_pids() -> list[int]:
    """返回所有 ``steam.exe`` 进程 PID。"""
    return [pid for pid, name in _iter_processes() if name.lower() == "steam.exe"]


def _iter_modules(pid: int) -> list[tuple[str, str]]:
    kernel32 = _kernel32()
    if kernel32 is None:
        return []
    snapshot = kernel32.CreateToolhelp32Snapshot(
        TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid
    )
    if snapshot == INVALID_HANDLE_VALUE:
        logger.debug("无法枚举 PID %s 的模块（可能需要管理员权限）", pid)
        return []
    result: list[tuple[str, str]] = []
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(MODULEENTRY32W)
        if not kernel32.Module32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            result.append((str(entry.szModule), str(entry.szExePath)))
            if not kernel32.Module32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        logger.debug("计算哈希失败 %s: %s", path, exc)
        return ""
    return digest.hexdigest().upper()


def _same_path(left: str, right: str) -> bool:
    if not left or not right:
        return False
    try:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
            os.path.abspath(right)
        )
    except OSError:
        return False


def _read_kernel_version(source_dir: str) -> str:
    if not source_dir:
        return ""
    marker = Path(source_dir) / "kernel.json"
    if marker.is_file():
        try:
            import json

            data = json.loads(marker.read_text(encoding="utf-8"))
            version = str(data.get("version") or "").strip()
            if version:
                return version
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取 kernel.json 失败 %s: %s", marker, exc)
    version_file = Path(source_dir) / "version.txt"
    if version_file.is_file():
        try:
            return version_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return ""


def collect_components(steam_path: str, source_dir: str = "") -> ComponentReport:
    """检测三个组件的部署与加载状态。"""
    report = ComponentReport(
        steam_path=steam_path or "",
        source_dir=source_dir or "",
        kernel_version=_read_kernel_version(source_dir),
    )
    report.steam_pids = find_steam_pids()
    report.steam_running = bool(report.steam_pids)

    modules: list[tuple[str, str]] = []
    for pid in report.steam_pids:
        modules.extend(_iter_modules(pid))

    for dll_name in COMPONENT_DLLS:
        file_path = Path(steam_path) / dll_name if steam_path else Path()
        deployed = bool(steam_path) and file_path.is_file()
        status = ComponentStatus(
            name=dll_name,
            file_path=str(file_path) if steam_path else "",
            deployed=deployed,
            file_size=file_path.stat().st_size if deployed else 0,
            sha256=_sha256(file_path) if deployed else "",
        )

        matches = [
            (name, path)
            for name, path in modules
            if name.lower() == dll_name.lower()
        ]
        steam_match = next(
            (item for item in matches if _same_path(item[1], str(file_path))), None
        )
        if steam_match:
            status.loaded = True
            status.loaded_path = steam_match[1]
            status.loaded_from_steam = True
            status.note = "已从 Steam 目录加载"
        elif matches:
            status.loaded = True
            status.loaded_path = matches[0][1]
            status.note = f"已加载同名系统模块：{matches[0][1]}"
        elif not deployed:
            status.note = "文件未部署"
        elif not report.steam_running:
            status.note = "Steam 未运行，无法确认加载状态"
        else:
            status.note = "文件已部署，但未在 Steam 进程中检测到"

        if dll_name.lower() == "dwmapi.dll" and status.loaded_from_steam:
            status.note = "已从 Steam 目录加载（代理生效）"
        report.components.append(status)

    core = next(
        (c for c in report.components if c.name.lower() == "opensteamtool.dll"), None
    )
    report.injected = bool(core and core.loaded_from_steam)
    if report.injected:
        report.summary = "内核已加载，组件工作正常"
    elif not report.steam_running:
        report.summary = "Steam 未运行；已部署的文件将在下次启动时加载"
    elif core and core.deployed:
        report.summary = "文件已部署，但内核尚未在 Steam 进程中加载"
    else:
        report.summary = "未检测到可用的内核组件"
    return report


def uninstall_components(steam_path: str) -> tuple[bool, str]:
    """把三个 DLL 移到应用数据目录备份后删除。

    Steam 运行时拒绝操作，避免文件占用导致半删除状态。
    """
    if not steam_path:
        return False, "未检测到 Steam 安装目录"
    if find_steam_pids():
        return False, "Steam 正在运行，请先完全退出 Steam（包括托盘图标）再移除组件"

    existing = [Path(steam_path) / name for name in COMPONENT_DLLS]
    existing = [path for path in existing if path.is_file()]
    if not existing:
        return False, "Steam 目录中没有需要移除的组件"

    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = app_data_dir() / "dll_backup" / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)

    removed: list[str] = []
    failed: list[str] = []
    for path in existing:
        try:
            shutil.copy2(path, backup_dir / path.name)
            path.unlink()
            removed.append(path.name)
        except OSError as exc:
            failed.append(f"{path.name}: {exc}")

    if failed:
        return False, "部分文件移除失败：" + "；".join(failed)
    logger.info("已移除组件 %s，备份到 %s", ", ".join(removed), backup_dir)
    return True, f"已移除 {len(removed)} 个组件，原文件备份在 {backup_dir}"
