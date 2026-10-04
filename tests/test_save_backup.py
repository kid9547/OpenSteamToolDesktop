"""``core.save_backup`` 单元测试：userdata 存档枚举 / 备份 / 恢复。"""
from __future__ import annotations

import zipfile

import pytest

from core.save_backup import SaveBackupError, SaveBackupManager


@pytest.fixture()
def steam_tree(tmp_path):
    """构造带两个账号、两个游戏存档的 userdata 目录。"""
    steam = tmp_path / "steam"
    remote_a = steam / "userdata" / "1111111" / "500" / "remote"
    remote_b = steam / "userdata" / "2222222" / "600" / "remote"
    (remote_a / "save").mkdir(parents=True)
    (remote_b.mkdir(parents=True))
    (remote_a / "save" / "slot1.dat").write_bytes(b"save-data-500")
    (remote_a / "config.cfg").write_bytes(b"cfg-500")
    (remote_b / "profile.bin").write_bytes(b"save-data-600")
    return steam


def test_list_save_apps_aggregates_across_accounts(steam_tree):
    apps = SaveBackupManager(str(steam_tree)).list_save_apps()
    by_id = {a.app_id: a for a in apps}
    assert set(by_id) == {"500", "600"}
    assert by_id["500"].file_count == 2
    assert by_id["500"].steam_ids == ["1111111"]
    assert by_id["600"].file_count == 1


def test_backup_creates_zip_with_all_accounts(steam_tree, tmp_path):
    manager = SaveBackupManager(str(steam_tree))
    dest = tmp_path / "backups"
    dest.mkdir()

    zip_path = manager.backup(["500"], dest)

    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
    assert "userdata/1111111/500/remote/save/slot1.dat" in names
    assert "userdata/1111111/500/remote/config.cfg" in names


def test_backup_without_selection_raises(steam_tree, tmp_path):
    manager = SaveBackupManager(str(steam_tree))
    with pytest.raises(SaveBackupError):
        manager.backup([], tmp_path)


def test_restore_restores_files_and_baks_existing(steam_tree, tmp_path):
    manager = SaveBackupManager(str(steam_tree))
    dest = tmp_path / "backups"
    dest.mkdir()
    zip_path = manager.backup(["500"], dest)

    # 模拟用户存档被删/被改后再恢复
    target = steam_tree / "userdata" / "1111111" / "500" / "remote" / "save" / "slot1.dat"
    target.write_bytes(b"corrupted")

    summary = manager.restore(zip_path)

    assert "还原" in summary
    assert target.read_bytes() == b"save-data-500"
    bak = target.with_suffix(target.suffix + ".bak")
    assert bak.read_bytes() == b"corrupted"


def test_restore_rejects_bad_zip(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    manager = SaveBackupManager(str(tmp_path / "steam"))
    (tmp_path / "steam" / "userdata").mkdir(parents=True)
    with pytest.raises(SaveBackupError):
        manager.restore(bad)
