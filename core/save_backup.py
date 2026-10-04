"""Steam 存档备份 —— 对齐参考软件「Steam 解锁文件管理器」的存档管理能力。

覆盖范围（v1，求稳不求全）：

* Steam 云存档目录：``<steam>/userdata/<steamid>/<appid>/remote``。
  大多数游戏的本地存档/云存档落在这里，删除游戏或重装前备份它即可保住进度。

能力：

* :meth:`SaveBackupManager.list_save_apps` —— 枚举各 Steam 账号下拥有存档的
  AppID（含占用的磁盘空间）；
* :meth:`SaveBackupManager.backup` —— 把选定 AppID 的全部账号存档打包为
  一个 zip（文件名含时间戳）；
* :meth:`SaveBackupManager.restore` —— 从备份 zip 还原到 Steam 目录，
  已存在的同名文件会先备份为 ``.bak`` 再覆盖。

不做的事：注册表存档（HKEY_CURRENT_USER\\Software\\Valve\\...）与
Documents 等游戏自定义目录暂不覆盖——这类路径因游戏而异，做不可靠的
"聪明猜测"反而危险，后续按需扩展。
"""
from __future__ import annotations

import os
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from utils.logger import setup_logger

logger = setup_logger(__name__)


@dataclass
class SaveAppInfo:
    """某个 AppID 的存档概况（跨全部 Steam 账号聚合）"""

    app_id: str
    steam_ids: list[str]
    file_count: int
    total_size: int

    @property
    def label(self) -> str:
        return f"AppID {self.app_id}（{len(self.steam_ids)} 个账号 / {self.file_count} 个文件 / {self.total_size / 1024:.0f} KB）"


class SaveBackupError(RuntimeError):
    pass


class SaveBackupManager:
    """userdata 云存档的枚举 / 备份 / 恢复。"""

    def __init__(self, steam_path: str):
        self.steam_path = Path(steam_path) if steam_path else None

    # ── 枚举 ──────────────────────────────────────────────
    def _userdata_root(self) -> Path | None:
        if not self.steam_path:
            return None
        root = self.steam_path / "userdata"
        return root if root.is_dir() else None

    def list_save_apps(self) -> list[SaveAppInfo]:
        """枚举所有账号下有 ``remote`` 存档的 AppID，按 AppID 升序。"""
        root = self._userdata_root()
        if root is None:
            return []
        aggregated: dict[str, dict] = {}
        try:
            steam_ids = [d for d in root.iterdir() if d.is_dir() and d.name.isdigit()]
        except OSError:
            return []
        for sid_dir in steam_ids:
            try:
                app_dirs = [d for d in sid_dir.iterdir() if d.is_dir() and d.name.isdigit()]
            except OSError:
                continue
            for app_dir in app_dirs:
                remote = app_dir / "remote"
                if not remote.is_dir():
                    continue
                file_count = 0
                total_size = 0
                for path in remote.rglob("*"):
                    if path.is_file():
                        try:
                            file_count += 1
                            total_size += path.stat().st_size
                        except OSError:
                            continue
                if file_count == 0:
                    continue
                info = aggregated.setdefault(
                    app_dir.name, {"steam_ids": [], "file_count": 0, "total_size": 0}
                )
                info["steam_ids"].append(sid_dir.name)
                info["file_count"] += file_count
                info["total_size"] += total_size
        return [
            SaveAppInfo(
                app_id=app_id,
                steam_ids=meta["steam_ids"],
                file_count=meta["file_count"],
                total_size=meta["total_size"],
            )
            for app_id, meta in sorted(aggregated.items(), key=lambda kv: int(kv[0]))
        ]

    # ── 备份 / 恢复 ───────────────────────────────────────
    def backup(self, app_ids: list[str], dest_dir: str | Path) -> Path:
        """把选定 AppID 的全部账号存档打包为一个 zip，返回 zip 路径。"""
        root = self._userdata_root()
        if root is None:
            raise SaveBackupError("未找到 userdata 目录，无法备份存档")
        wanted = {str(a) for a in app_ids if str(a).isdigit()}
        if not wanted:
            raise SaveBackupError("请先选择要备份存档的 AppID")

        destination = Path(dest_dir)
        if not destination.is_dir():
            raise SaveBackupError(f"备份目标目录不存在：{destination}")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        zip_path = destination / f"steam_saves_{stamp}.zip"

        entries = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for sid_dir in root.iterdir():
                if not (sid_dir.is_dir() and sid_dir.name.isdigit()):
                    continue
                for app_id in sorted(wanted):
                    remote = sid_dir / app_id / "remote"
                    if not remote.is_dir():
                        continue
                    for path in remote.rglob("*"):
                        if not path.is_file():
                            continue
                        arcname = Path("userdata") / sid_dir.name / app_id / "remote" / path.relative_to(remote)
                        try:
                            archive.write(path, arcname.as_posix())
                            entries += 1
                        except OSError as exc:
                            logger.warning("备份存档文件失败 %s: %s", path, exc)
        if entries == 0:
            zip_path.unlink(missing_ok=True)
            raise SaveBackupError("所选 AppID 没有任何可备份的存档文件")
        logger.info("存档备份完成：%s（%d 个文件）", zip_path, entries)
        return zip_path

    def restore(self, zip_path: str | Path) -> str:
        """从备份 zip 还原存档到 Steam 目录，返回摘要。"""
        root = self._userdata_root()
        if root is None:
            raise SaveBackupError("未找到 userdata 目录，无法还原存档")
        archive_path = Path(zip_path)
        if not archive_path.is_file():
            raise SaveBackupError(f"备份文件不存在：{archive_path}")

        restored = 0
        skipped = 0
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    parts = Path(member.filename).parts
                    # 期望 userdata/<steamid>/<appid>/remote/...
                    if len(parts) < 4 or parts[0] != "userdata" or parts[3] != "remote":
                        skipped += 1
                        continue
                    if not (parts[1].isdigit() and parts[2].isdigit()):
                        skipped += 1
                        continue
                    target = root / Path(*parts[1:])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        backup = target.with_suffix(target.suffix + ".bak")
                        try:
                            target.replace(backup)
                        except OSError:
                            pass
                    try:
                        target.write_bytes(archive.read(member))
                        restored += 1
                    except OSError as exc:
                        logger.warning("还原存档文件失败 %s: %s", target, exc)
        except zipfile.BadZipFile as exc:
            raise SaveBackupError(f"备份文件损坏：{exc}") from exc
        summary = f"已还原 {restored} 个存档文件" + (f"，跳过 {skipped} 个非存档条目" if skipped else "")
        logger.info("存档还原完成：%s", summary)
        return summary

    @staticmethod
    def describe_app_ids(app_ids: list[str]) -> str:
        return "、".join(str(a) for a in app_ids[:8]) + ("…" if len(app_ids) > 8 else "")
