"""诊断中心：日志检索、崩溃/转储扫描、环境快照与诊断包导出。

该模块只读取本地文件，不会修改 Steam 或应用配置。导出的诊断包会主动
脱敏 ``password`` / ``token`` / ``api_key`` 等字段，便于直接发给维护者。
"""
from __future__ import annotations

import json
import os
import platform
import re
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from config import APP_NAME, APP_VERSION
from core.app_paths import app_data_dir
from utils.logger import setup_logger
from utils.path_manager import PathManager

logger = setup_logger(__name__)

_LEVEL_ORDER = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "WARN": 30,
    "ERROR": 40,
    "CRITICAL": 50,
    "FATAL": 50,
    "RAW": 20,
}
_LOG_LINE = re.compile(
    r"^\[(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+"
    r"\[(?P<level>[A-Z]+)\]\s+"
    r"\[(?P<logger>[^\]]+)\]\s?(?P<message>.*)$"
)
_DATE_IN_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.log$", re.IGNORECASE)
_SECRET_KEY = re.compile(r"password|passwd|token|api[_-]?key|secret|credential", re.IGNORECASE)
_SECRET_LINE = re.compile(
    r"(?im)^(\s*(?:password|passwd|token|api[_-]?key|apikey|secret|credential)\s*=\s*).*$"
)
_MAX_LOG_BYTES = 20 * 1024 * 1024
_MAX_DUMP_BYTES = 50 * 1024 * 1024
_MAX_DUMPS = 5


@dataclass
class LogEntry:
    """一行（或一段多行）日志。"""

    timestamp: str
    level: str
    logger: str
    message: str
    line_number: int = 0
    source: str = ""

    @property
    def level_value(self) -> int:
        return _LEVEL_ORDER.get(self.level.upper(), 20)


@dataclass
class LogSource:
    """日志查看器里的一个日期/文件分组。"""

    label: str
    paths: list[str] = field(default_factory=list)
    date: str = ""


@dataclass
class CrashArtifact:
    """崩溃日志或转储文件。"""

    path: str
    kind: str
    size: int = 0
    modified: float = 0.0

    @property
    def name(self) -> str:
        return Path(self.path).name


@dataclass
class DiagnosticsSnapshot:
    """一次诊断环境快照。"""

    generated_at: str = ""
    app_version: str = APP_VERSION
    python: str = ""
    platform: str = ""
    steam_path: str = ""
    steam_running: bool = False
    steam_pids: list[int] = field(default_factory=list)
    kernel_version: str = ""
    injected: bool = False
    component_summary: str = ""
    components: list[Any] = field(default_factory=list)
    missing_patterns: list[str] = field(default_factory=list)
    lua_total: int = 0
    lua_enabled: int = 0
    lua_conflicts: int = 0
    installed_apps: int = 0
    denuvo_mode: str = ""
    opensteamtool_config: str = ""
    log_dir: str = ""
    crash_artifacts: list[CrashArtifact] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _log_dir(log_dir: str | Path | None = None) -> Path:
    return Path(log_dir) if log_dir is not None else PathManager.logs_dir()


def _data_dir(data_dir: str | Path | None = None) -> Path:
    return Path(data_dir) if data_dir is not None else app_data_dir()


def available_log_files(log_dir: str | Path | None = None) -> list[Path]:
    """返回日志目录中的 ``*.log``，按修改时间从新到旧。"""
    directory = _log_dir(log_dir)
    try:
        files = [path for path in directory.glob("*.log") if path.is_file()]
    except OSError as exc:
        logger.debug("读取日志目录失败 %s: %s", directory, exc)
        return []
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)


def log_sources(
    log_dir: str | Path | None = None, *, recent_days: int = 7
) -> list[LogSource]:
    """构造日志查看器的下拉选项。

    日期日志按文件名分组；``crash.log`` / ``stderr.log`` 单独列出。
    """
    files = available_log_files(log_dir)
    dated: list[Path] = []
    special: dict[str, Path] = {}
    for path in files:
        if _DATE_IN_NAME.match(path.name):
            dated.append(path)
        elif path.name.lower() in ("crash.log", "stderr.log"):
            special[path.name.lower()] = path

    dated.sort(key=lambda path: path.name, reverse=True)
    sources: list[LogSource] = []
    if dated:
        recent = dated[: max(int(recent_days), 1)]
        sources.append(
            LogSource(
                label=f"全部日志（最近 {len(recent)} 天）",
                paths=[str(path) for path in reversed(recent)],
            )
        )
    for path in dated:
        sources.append(
            LogSource(label=path.stem, paths=[str(path)], date=path.stem)
        )
    if "crash.log" in special:
        sources.append(
            LogSource(label="崩溃日志 crash.log", paths=[str(special["crash.log"])])
        )
    if "stderr.log" in special:
        sources.append(
            LogSource(label="标准错误 stderr.log", paths=[str(special["stderr.log"])])
        )
    return sources


def parse_log_file(path: str | Path, *, max_entries: int = 50_000) -> list[LogEntry]:
    """解析单个日志文件；无法匹配格式的行会并入上一条消息。"""
    source = Path(path)
    entries: list[LogEntry] = []
    try:
        with source.open("r", encoding="utf-8", errors="replace") as stream:
            for line_number, raw in enumerate(stream, 1):
                line = raw.rstrip("\r\n")
                match = _LOG_LINE.match(line)
                if match:
                    entries.append(
                        LogEntry(
                            timestamp=match.group("time"),
                            level=match.group("level"),
                            logger=match.group("logger"),
                            message=match.group("message"),
                            line_number=line_number,
                            source=source.name,
                        )
                    )
                elif entries:
                    entries[-1].message += "\n" + line
                elif line.strip():
                    entries.append(
                        LogEntry(
                            timestamp="",
                            level="RAW",
                            logger="(raw)",
                            message=line,
                            line_number=line_number,
                            source=source.name,
                        )
                    )
                if len(entries) > max_entries * 2:
                    entries = entries[-max_entries:]
    except OSError as exc:
        logger.debug("读取日志失败 %s: %s", source, exc)
        return []
    return entries[-max_entries:]


def parse_log_files(paths: Iterable[str | Path], *, max_entries: int = 50_000) -> list[LogEntry]:
    """解析多个日志文件，按传入顺序拼接。"""
    entries: list[LogEntry] = []
    for path in paths:
        entries.extend(parse_log_file(path, max_entries=max_entries))
        if len(entries) > max_entries:
            entries = entries[-max_entries:]
    return entries


def filter_log_entries(
    entries: Iterable[LogEntry],
    *,
    min_level: str | int = "DEBUG",
    keyword: str = "",
    date_prefix: str = "",
    max_entries: int = 50_000,
) -> list[LogEntry]:
    """按级别、关键词和日期过滤日志；结果保留最近 ``max_entries`` 条。"""
    if isinstance(min_level, str):
        threshold = _LEVEL_ORDER.get(min_level.upper(), 0)
    else:
        threshold = int(min_level)
    needle = keyword.strip().lower()
    prefix = date_prefix.strip()
    result: list[LogEntry] = []
    for entry in entries:
        if entry.level_value < threshold:
            continue
        if prefix and not entry.timestamp.startswith(prefix):
            continue
        if needle:
            haystack = f"{entry.logger} {entry.level} {entry.message}".lower()
            if needle not in haystack:
                continue
        result.append(entry)
    return result[-max_entries:]


def scan_crash_artifacts(
    steam_path: str = "",
    *,
    log_dir: str | Path | None = None,
    data_dir: str | Path | None = None,
) -> list[CrashArtifact]:
    """扫描崩溃日志、stderr 和 dmp 转储文件。"""
    candidates: dict[str, str] = {}
    logs = _log_dir(log_dir)
    data = _data_dir(data_dir)

    for name in ("crash.log", "stderr.log"):
        path = logs / name
        if path.is_file():
            candidates[str(path.resolve())] = "崩溃日志" if name == "crash.log" else "标准错误"

    for directory, kind in ((logs, "转储"), (data, "转储")):
        try:
            for path in directory.glob("*.dmp"):
                if path.is_file():
                    candidates[str(path.resolve())] = kind
        except OSError:
            continue

    if steam_path:
        root = Path(steam_path)
        for directory, kind in (
            (root / "dumps", "Steam 转储"),
            (root, "Steam 转储"),
        ):
            try:
                for path in directory.glob("*.dmp"):
                    if path.is_file():
                        candidates[str(path.resolve())] = kind
            except OSError:
                continue

    artifacts: list[CrashArtifact] = []
    for path_str, kind in candidates.items():
        path = Path(path_str)
        try:
            stat = path.stat()
        except OSError:
            continue
        artifacts.append(
            CrashArtifact(path=path_str, kind=kind, size=stat.st_size, modified=stat.st_mtime)
        )
    artifacts.sort(key=lambda item: item.modified, reverse=True)
    return artifacts


def collect_snapshot(
    steam_path: str = "",
    source_dir: str = "",
    *,
    log_dir: str | Path | None = None,
    data_dir: str | Path | None = None,
) -> DiagnosticsSnapshot:
    """收集组件、签名、Lua、游戏数量与转储文件等诊断信息。"""
    snapshot = DiagnosticsSnapshot(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        app_version=APP_VERSION,
        python=sys.version.split()[0],
        platform=f"{platform.system()} {platform.release()} ({platform.machine()})",
        steam_path=steam_path or "",
        log_dir=str(_log_dir(log_dir)),
    )
    snapshot.crash_artifacts = scan_crash_artifacts(
        steam_path, log_dir=log_dir, data_dir=data_dir
    )

    if not steam_path:
        snapshot.notes.append("未检测到 Steam 安装路径")
        return snapshot

    try:
        from core.component_status import collect_components

        report = collect_components(steam_path, source_dir)
        snapshot.steam_running = report.steam_running
        snapshot.steam_pids = list(report.steam_pids)
        snapshot.kernel_version = report.kernel_version
        snapshot.injected = report.injected
        snapshot.component_summary = report.summary
        snapshot.components = list(report.components)
    except Exception as exc:  # noqa: BLE001 - 诊断必须尽量给出部分结果
        logger.debug("组件状态收集失败: %s", exc)
        snapshot.notes.append(f"组件状态收集失败：{exc}")

    try:
        from core.steam_pattern_sync import SteamPatternSync

        snapshot.missing_patterns = SteamPatternSync(steam_path).missing_patterns()
    except Exception as exc:  # noqa: BLE001
        logger.debug("签名状态收集失败: %s", exc)
        snapshot.notes.append(f"签名状态收集失败：{exc}")

    try:
        from core.lua_rules import scan_lua_rules

        rules = scan_lua_rules(steam_path)
        snapshot.lua_total = len(rules)
        snapshot.lua_enabled = sum(1 for rule in rules if rule.enabled)
        snapshot.lua_conflicts = sum(1 for rule in rules if rule.conflicts)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Lua 状态收集失败: %s", exc)
        snapshot.notes.append(f"Lua 状态收集失败：{exc}")

    try:
        from core.local_scanner import LocalGameScanner

        games = LocalGameScanner(steam_path).scan_installed_games()
        snapshot.installed_apps = len(games)
    except Exception as exc:  # noqa: BLE001
        logger.debug("游戏数量收集失败: %s", exc)
        snapshot.notes.append(f"游戏数量收集失败：{exc}")

    try:
        from core.opensteamtool_config import config_path, load_config

        config_file = config_path(steam_path)
        snapshot.opensteamtool_config = str(config_file)
        data = load_config(steam_path)
        denuvo = data.get("denuvo") if isinstance(data, dict) else None
        if isinstance(denuvo, dict):
            snapshot.denuvo_mode = str(denuvo.get("mode") or "")
    except Exception as exc:  # noqa: BLE001
        logger.debug("内核配置收集失败: %s", exc)
        snapshot.notes.append(f"内核配置收集失败：{exc}")

    return snapshot


def snapshot_text(snapshot: DiagnosticsSnapshot) -> str:
    """把快照渲染为可读文本，供诊断包与界面使用。"""
    lines = [
        f"{APP_NAME} 诊断快照",
        "=" * 48,
        f"生成时间：{snapshot.generated_at}",
        f"应用版本：{snapshot.app_version}",
        f"Python：{snapshot.python}",
        f"系统：{snapshot.platform}",
        f"Steam 路径：{snapshot.steam_path or '未检测到'}",
        f"Steam 进程：{', '.join(str(pid) for pid in snapshot.steam_pids) or '未运行'}",
        f"内核版本：{snapshot.kernel_version or '未知'}",
        f"注入状态：{'已加载' if snapshot.injected else '未加载'}",
        f"组件摘要：{snapshot.component_summary or '未知'}",
        f"缺失签名：{len(snapshot.missing_patterns)} 个",
        f"Lua 规则：{snapshot.lua_total} 个（启用 {snapshot.lua_enabled}，冲突 {snapshot.lua_conflicts}）",
        f"已安装游戏：{snapshot.installed_apps} 个",
        f"Denuvo 模式：{snapshot.denuvo_mode or '未配置'}",
        f"内核配置：{snapshot.opensteamtool_config or '未找到'}",
        f"日志目录：{snapshot.log_dir}",
        f"崩溃/转储：{len(snapshot.crash_artifacts)} 个",
    ]
    if snapshot.missing_patterns:
        lines.append("")
        lines.append("缺失的签名表：")
        lines.extend(f"  - {item}" for item in snapshot.missing_patterns)
    if snapshot.components:
        lines.append("")
        lines.append("组件明细：")
        for component in snapshot.components:
            lines.append(
                f"  - {component.name}: 部署={'是' if component.deployed else '否'}, "
                f"Steam 加载={'是' if component.loaded_from_steam else ('系统同名' if component.loaded else '否')}, "
                f"大小={component.file_size}, SHA256={component.sha256 or '-'}"
            )
    if snapshot.crash_artifacts:
        lines.append("")
        lines.append("崩溃/转储文件：")
        for artifact in snapshot.crash_artifacts:
            lines.append(
                f"  - [{artifact.kind}] {artifact.path} "
                f"({artifact.size} bytes, {_format_time(artifact.modified)})"
            )
    if snapshot.notes:
        lines.append("")
        lines.append("诊断备注：")
        lines.extend(f"  - {note}" for note in snapshot.notes)
    return "\n".join(lines) + "\n"


def _format_time(timestamp: float) -> str:
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return "-"


def redact_config(value: Any) -> Any:
    """递归脱敏配置中的密码、令牌和 API Key。"""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if _SECRET_KEY.search(str(key)):
                result[key] = "***REDACTED***" if item else item
            else:
                result[key] = redact_config(item)
        return result
    if isinstance(value, list):
        return [redact_config(item) for item in value]
    return value


def redact_text(text: str) -> str:
    """脱敏 TOML / 文本中的敏感赋值行。"""
    return _SECRET_LINE.sub(r'\1"***REDACTED***"', text)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _add_file(
    archive: zipfile.ZipFile,
    path: Path,
    arcname: str,
    *,
    max_bytes: int = _MAX_LOG_BYTES,
    notes: list[str] | None = None,
) -> bool:
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size > max_bytes:
        if notes is not None:
            notes.append(f"跳过过大文件：{path}（{size} bytes）")
        return False
    try:
        archive.write(path, arcname)
        return True
    except OSError as exc:
        if notes is not None:
            notes.append(f"无法写入 {path}：{exc}")
        return False


def export_diagnostics(
    dest_zip: str | Path,
    *,
    steam_path: str = "",
    source_dir: str = "",
    log_dir: str | Path | None = None,
    data_dir: str | Path | None = None,
    config_file: str | Path | None = None,
    log_days: int = 14,
    include_dumps: bool = True,
) -> tuple[bool, str]:
    """导出一个脱敏诊断包（zip），返回 ``(成功, 路径或错误信息)``。"""
    destination = Path(dest_zip)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"无法创建导出目录：{exc}"

    snapshot = collect_snapshot(
        steam_path, source_dir, log_dir=log_dir, data_dir=data_dir
    )
    notes: list[str] = []
    temp = destination.with_suffix(destination.suffix + ".tmp")

    try:
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("summary.txt", snapshot_text(snapshot))

            # 配置：JSON 递归脱敏，TOML 逐行脱敏。
            config_path = Path(config_file) if config_file is not None else Path(
                os.path.join(os.path.expanduser("~"), f".{APP_NAME}", "config.json")
            )
            if config_path.is_file():
                data = _read_json(config_path)
                if data is not None:
                    archive.writestr(
                        "config/config.redacted.json",
                        json.dumps(redact_config(data), ensure_ascii=False, indent=2),
                    )
                else:
                    notes.append(f"无法解析配置：{config_path}")
            else:
                notes.append(f"未找到应用配置：{config_path}")

            if steam_path:
                kernel_config = Path(steam_path) / "opensteamtool.toml"
                if kernel_config.is_file():
                    try:
                        text = kernel_config.read_text(encoding="utf-8", errors="replace")
                        archive.writestr(
                            "config/opensteamtool.redacted.toml", redact_text(text)
                        )
                    except OSError as exc:
                        notes.append(f"无法读取内核配置：{exc}")

            # 日志：最近 N 天的日期日志 + 崩溃/stderr。
            logs = _log_dir(log_dir)
            cutoff = time.time() - max(int(log_days), 1) * 86400
            included_logs = 0
            try:
                log_files = sorted(logs.glob("*.log"), key=lambda p: p.name)
            except OSError:
                log_files = []
            for path in log_files:
                if not path.is_file():
                    continue
                is_special = path.name.lower() in ("crash.log", "stderr.log")
                try:
                    if not is_special and path.stat().st_mtime < cutoff:
                        continue
                except OSError:
                    continue
                if _add_file(archive, path, f"logs/{path.name}", notes=notes):
                    included_logs += 1
            if not included_logs:
                notes.append("没有可导出的日志文件")

            if include_dumps:
                dumps = [item for item in snapshot.crash_artifacts if item.path.lower().endswith(".dmp")]
                for artifact in dumps[:_MAX_DUMPS]:
                    _add_file(
                        archive,
                        Path(artifact.path),
                        f"dumps/{Path(artifact.path).name}",
                        max_bytes=_MAX_DUMP_BYTES,
                        notes=notes,
                    )
                if len(dumps) > _MAX_DUMPS:
                    notes.append(
                        f"转储文件较多，仅包含最近 {_MAX_DUMPS} 个（共 {len(dumps)} 个）"
                    )

            if notes:
                archive.writestr("export_notes.txt", "\n".join(notes) + "\n")
        os.replace(temp, destination)
    except (OSError, zipfile.BadZipFile) as exc:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        return False, f"导出诊断包失败：{exc}"

    return True, str(destination)
